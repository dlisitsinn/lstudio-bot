"""Окно управления: маленький веб-сервер на вашем компьютере (только для вас, адрес 127.0.0.1)."""
import re
import json
import random
import threading
import time
import traceback
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from .db import row_to_dict
from .drafts import USER_TEMPLATES_DIR, template_path
from .finder import VKError, enrich_with_vk, site_is_alive
from .importer import SOURCES, make_template_csv, parse_rows, read_table

STATIC = Path(__file__).resolve().parent / "static"
EDITABLE_TEMPLATES = {"email": "email.txt", "vk": "vk.txt"}


def has_vk(lead) -> bool:
    return bool(lead.get("screen_name") or lead.get("vk_group_id"))


def reachable(lead) -> bool:
    """Есть куда написать: группа ВК или почта."""
    return has_vk(lead) or bool(lead.get("email"))


NICHE_EXCLUDE = ("банк", "банкет", "бандаж", "бантик", "баннер", "бандероль")


def niche_match(row, words) -> bool:
    """Подходит ли организация под выбранные ниши: ищем слова в названии, виде деятельности, запросе, описании."""
    if not words:
        return True
    text = " ".join(str(row[k] or "") for k in ("name", "activity", "query", "info")).lower().replace("ё", "е")
    for w in words:
        w = w.replace("ё", "е")
        pat = r"[а-яa-z]*[\s-]+".join(re.escape(part) for part in w.split())   # «баз отдых» → «базы отдыха»
        for m in re.finditer(r"(?<![а-яa-z])" + pat, text):
            tail = text[m.start():m.start() + 12]
            if not tail.startswith(NICHE_EXCLUDE):
                return True
    return False


# окно ВК закрылось/вылетело или ВК разлогинил — пробуем открыть заново
WINDOW_GONE = ("Окно ВК не готово", "Окно ВК закрыто", "has been closed", "Target closed", "Browser closed")
# разовый сбой (долго грузилось, страница дёрнулась) — карточку оставляем в очереди и идём дальше
TRANSIENT = ("слишком долго не отвечает", "Ошибка окна ВК", "Текст вставлен, но не отправился",
             "Не нашла поле для сообщения")
WINDOW_LOST = WINDOW_GONE + TRANSIENT       # для совместимости: всё, где карточка не виновата


class App:
    def __init__(self, cfg, store, vk_finder, osm_finder, drafter, sender_factory, mail_check=None,
                 check_alive=site_is_alive, vkb=None):
        self.check_alive = check_alive
        self.cfg = cfg
        self.store = store
        self.vk_finder = vk_finder
        self.osm_finder = osm_finder
        self.drafter = drafter
        self.mail_check = mail_check
        self.log = deque(maxlen=80)
        self.sender = sender_factory(self.notify)
        self.search_thread = None
        self.search_label = ""
        vf = Path(__file__).resolve().parent.parent / "data" / ".version"
        self.version = vf.read_text().strip()[:7] if vf.exists() else "локальная"
        self.vk_messages = None       # есть ли у токена доступ к сообщениям (None — ещё не проверяли)
        self._vk_lock = threading.Lock()
        self._vk_last_send = 0.0
        self._sleep = time.sleep
        if vkb is None:
            from .vkbrowser import VKBrowser
            vkb = VKBrowser(Path(__file__).resolve().parent.parent / "data" / "vk-browser", notify=self.notify)
        self.vkb = vkb
        self.batch = {"running": False, "total": 0, "done": 0, "sent": 0, "closed": 0, "errors": 0,
                      "already": 0, "window_lost": False, "current": "", "captcha": False}
        self._batch_stop = threading.Event()
        self._batch_resume = threading.Event()
        self.batch_pause = (3, 6)    # пауза между сообщениями, секунд
        if cfg.only_vk:
            self._hide_without_vk()

    def _hide_without_vk(self):
        """Режим «только ВК»: организации без группы ВК и без почты — в «Пропущенные».
        Те, кого раньше убрали за отсутствие группы ВК, но у кого есть почта, возвращаются в очередь писем."""
        with self.store.lock:
            cur = self.store.conn.execute(
                "UPDATE leads SET status='skipped', note='нет группы ВК и почты' "
                "WHERE status IN ('new','manual','approved','failed') "
                "AND vk_group_id IS NULL AND (screen_name IS NULL OR screen_name='') "
                "AND (email IS NULL OR email='')")
            back = self.store.conn.execute(
                "UPDATE leads SET status=?, note=NULL WHERE status='skipped' AND note='нет группы ВК' "
                "AND email IS NOT NULL AND email!=''", ("approved" if self.cfg.auto_queue else "new",))
            self.store.conn.commit()
        if cur.rowcount:
            self.notify(f"Режим «только ВК»: {cur.rowcount} организаций без группы ВК и почты — в «Пропущенных»")
        if back.rowcount:
            self.notify(f"📧 {back.rowcount} организаций с почтой вернула из «Пропущенных» в очередь писем")

    def _auto_route(self, added):
        """Найденные сразу раскладываются: есть группа ВК — в «Написать во ВК», только почта — в «Письма»."""
        if not self.cfg.auto_queue or not added:
            return 0, 0
        vk = mail = 0
        for lead in added:
            with self.store.lock:
                uid = lead.get("uid") or (f"vk:{lead['vk_group_id']}" if lead.get("vk_group_id") else "")
                row = self.store.conn.execute("SELECT id FROM leads WHERE uid=?", (uid,)).fetchone()
            if not row:
                continue
            r = self.store.get(row["id"])
            if not r or r["status"] != "new":
                continue
            if r["vk_group_id"] or r["screen_name"]:
                self.store.set_status(r["id"], "manual")
                vk += 1
            elif r["email"]:
                self.store.set_status(r["id"], "approved")
                mail += 1
        return vk, mail

    def _route_msg(self, added) -> str:
        vk, mail = self._auto_route(added)
        return f" → во ВК {vk}, в письма {mail}" if (vk or mail) else ""

    def check_vk_messages(self):
        """Проверяет, может ли токен отправлять сообщения (право «messages» = 4096)."""
        if not self.vk_finder.can_search:
            self.vk_messages = False
            return False
        try:
            mask = self.vk_finder.vk.call("account.getAppPermissions")
            self.vk_messages = bool(int(mask) & 4096)
        except Exception:
            self.vk_messages = False
        return self.vk_messages

    def notify(self, text, *_):
        self.log.append({"t": datetime.now().strftime("%H:%M"), "text": text})

    # ---------- состояние ----------
    def state(self):
        leads = {}
        for status in ("new", "approved", "manual", "sent", "contacted", "failed", "skipped"):
            leads[status] = [row_to_dict(r) for r in self.store.by_status(status)]
            if status in ("sent", "contacted", "skipped"):
                leads[status] = leads[status][::-1][:200]
        manual_queue, niche_total = [], 0
        words = self.cfg.niche_only
        for r in self.store.by_status("manual", limit=None if words else 100):
            if not niche_match(r, words):
                continue
            niche_total += 1
            if len(manual_queue) < 100:
                d = row_to_dict(r)
                d["vk_text"] = self.drafter.vk(r)
                manual_queue.append(d)
        return {
            "leads": leads,
            "manual_queue": manual_queue,
            "manual_niche_total": niche_total,
            "niche_only": list(words),
            "vk_sent_today": self.store.vk_sent_today(),
            "vk_daily_limit": self.cfg.vk_daily_limit,
            "counts": self.store.count_by_status(),
            "sent_today": self.store.sent_today(),
            "daily_limit": self.cfg.daily_email_limit,
            "sending": self.sender.running,
            "searching": bool(self.search_thread and self.search_thread.is_alive()),
            "search_label": self.search_label,
            "log": list(self.log)[::-1],
            "version": self.version,
            "vkb": self.vkb.status(),
            "batch": dict(self.batch),
            "settings": {
                "vk": self.vk_finder.can_search,
                "vk_messages": bool(self.vk_messages),
                "only_vk": self.cfg.only_vk,
                "email": self.cfg.email_ready(),
                "email_user": self.cfg.smtp_user,
                "city": self.cfg.default_city,
            },
        }

    def lead(self, lead_id):
        r = self.store.get(lead_id)
        if not r:
            return {"error": "Не найдено"}
        d = row_to_dict(r)
        subject, body = self.drafter.email(r)
        d.update(email_subject=subject, email_body=body, vk_text=self.drafter.vk(r))
        return d

    # ---------- поиск ----------
    def start_search(self, source, query, city, want):
        if self.search_thread and self.search_thread.is_alive():
            return {"error": "Поиск уже идёт, дождитесь результата"}
        query, city = (query or "").strip(), (city or "").strip() or self.cfg.default_city
        if not query:
            return {"error": "Напишите, что искать: например, «маникюр»"}
        want = max(1, min(int(want or 20), 100))
        sources = ["vk", "osm"] if source == "both" else [source]
        if "vk" in sources and not self.vk_finder.can_search:
            if source == "vk":
                return {"error": "Для поиска во ВК нужен токен страницы (VK_USER_TOKEN в .env). "
                                 "Пока можно искать по карте."}
            sources.remove("vk")
            self.notify("ℹ️ Токена ВК нет — ищу только по карте OpenStreetMap")
        self.search_label = f"«{query}», {city}"
        self.search_thread = threading.Thread(target=self._search, args=(sources, query, city, want),
                                              daemon=True)
        self.search_thread.start()
        return {"ok": True, "message": f"Ищу {self.search_label}…"}

    def _search(self, sources, query, city, want):
        for src in sources:
            name = "ВКонтакте" if src == "vk" else "карте OpenStreetMap"
            try:
                if src == "vk":
                    leads, stats = self.vk_finder.search(
                        query, city, want, skip_ids=self.store.known_group_ids())
                else:
                    leads, stats = self.osm_finder.search(
                        query, city, want, skip_uids=self.store.known_uids())
                no_vk = 0
                if self.cfg.only_vk:
                    no_vk = sum(1 for l in leads if not reachable(l))
                    leads = [l for l in leads if reachable(l)]
                added = [l for l in leads if self.store.add_lead(l)]
                with_email = sum(1 for l in added if l.get("email"))
                msg = (f"✅ {name}: проверено {stats['checked']}, с сайтом {stats['with_site']}, "
                       f"новых без сайта {len(added)}")
                msg += f", без ВК и почты пропущено {no_vk}" if self.cfg.only_vk else f" (с почтой {with_email})"
                msg += self._route_msg(added)
                self.notify(msg)
            except (ValueError, ConnectionError) as exc:
                self.notify(f"⚠️ {name}: {exc}")
            except VKError as exc:
                hint = " — токен устарел или неверный, получите новый" if exc.code == 5 else ""
                self.notify(f"⚠️ {name}: {exc}{hint}")
            except Exception as exc:
                traceback.print_exc()
                self.notify(f"⚠️ {name}: ошибка {exc}")

    # ---------- загрузка таблицы ----------
    def import_file(self, filename, data: bytes, source="file"):
        if self.search_thread and self.search_thread.is_alive():
            return {"error": "Идёт поиск или загрузка, дождитесь окончания"}
        if source not in SOURCES:
            source = "file"
        try:
            rows = read_table(filename, data)
        except ValueError as exc:
            return {"error": str(exc)}
        except Exception:
            return {"error": "Не получилось прочитать файл. Сохраните его как .xlsx или CSV."}
        leads, stats = parse_rows(rows, source, self.cfg.default_city)
        if not leads and stats.get("hidden"):
            return {"error": "Все строки в файле скрыты звёздочками: их не открыл тариф сервиса выгрузки"}
        if not leads:
            return {"error": "В файле не нашлось организаций. Первая строка должна быть с заголовками: "
                             "Название, Телефон, Почта, Сайт… (шаблон можно скачать рядом с кнопкой)"}
        self.search_label = f"файл «{filename}»"
        self.search_thread = threading.Thread(target=self._import, args=(leads, stats), daemon=True)
        self.search_thread.start()
        return {"ok": True, "message": f"Читаю {len(leads)} организаций из файла…"}

    def _import(self, leads, stats):
        try:
            no_vk = 0
            if self.cfg.only_vk:   # нужны организации с группой ВК или почтой (считаем только тех, кто без сайта)
                no_vk = sum(1 for l in leads if not reachable(l) and not l.get("_sites"))
                stats = dict(stats, with_site=stats["with_site"] + sum(
                    1 for l in leads if not reachable(l) and l.get("_sites")))
                leads = [l for l in leads if reachable(l)]
            from concurrent.futures import ThreadPoolExecutor
            result, with_site, dead = [], stats["with_site"], 0
            if self.cfg.skip_dead_sites:   # сайт указан — значит, сайт у них есть; не проверяем и не пишем
                listed = [l for l in leads if l.get("_sites")]
                with_site += len(listed)
                leads = [l for l in leads if not l.get("_sites")]
            to_check = [l for l in leads if l.get("_sites")]
            if len(to_check) > 20:
                self.notify(f"Проверяю, открываются ли {len(to_check)} сайтов из файла…")
            def alive(lead):
                return any(self.check_alive(u) for u in lead["_sites"])
            with ThreadPoolExecutor(max_workers=12) as pool:
                status = dict(zip((id(l) for l in to_check), pool.map(alive, to_check)))
            for lead in leads:
                sites = lead.pop("_sites", [])
                if sites:
                    if status.get(id(lead)):
                        with_site += 1
                        continue
                    lead["note"] = "указан сайт, но он не открывается: " + ", ".join(sites[:2])
                    dead += 1
                result.append(lead)
            st = {"with_site": 0}
            if self.vk_finder.can_check_groups:
                result = enrich_with_vk(self.vk_finder, result, st)
            added = [l for l in result if self.store.add_lead(l)]
            dupes = len(result) - len(added)
            with_email = sum(1 for l in added if l.get("email"))
            hidden = stats.get("hidden", 0)
            msg = (f"📄 Файл: строк {stats['rows'] - hidden}, с сайтом {with_site + st['with_site']}, "
                   f"новых без сайта {len(added)}")
            msg += f", без ВК и почты пропущено {no_vk}" if self.cfg.only_vk else f" (с почтой {with_email})"
            msg += self._route_msg(added)
            if dupes:
                msg += f", уже были в базе {dupes}"
            if dead:
                msg += f". У {dead} сайт указан, но не открывается"
            if stats.get("branches"):
                msg += f". Филиалы сетей склеены ({stats['branches']} строк)"
            if hidden:
                msg += (f". ⚠️ {hidden} строк в файле скрыты звёздочками — "
                        f"их не открыл бесплатный тариф сервиса, из которого выгружен файл")
            self.notify(msg)
        except Exception as exc:
            traceback.print_exc()
            self.notify(f"⚠️ Файл: ошибка {exc}")

    def check_links(self, text):
        if not self.vk_finder.can_check_groups:
            return {"error": "Для проверки групп нужен токен ВК (VK_USER_TOKEN)"}
        try:
            leads, with_site, known = self.vk_finder.check_links(text, self.store.known_group_ids())
        except VKError as exc:
            return {"error": str(exc)}
        added = [l for l in leads if self.store.add_lead(l)]
        msg = f"Проверено: без сайта {len(added)}, с сайтом {len(with_site)}, уже были {len(known)}"
        msg += self._route_msg(added)
        self.notify("🔗 " + msg)
        return {"ok": True, "message": msg}

    # ---------- действия ----------
    def action(self, action, ids):
        done = 0
        for lead_id in ids:
            r = self.store.get(int(lead_id))
            if not r:
                continue
            if action == "approve" and r["status"] in ("new", "failed"):
                to_mail = r["email"] and not (self.cfg.only_vk and (r["vk_group_id"] or r["screen_name"]))
                self.store.set_status(r["id"], "approved" if to_mail else "manual")
            elif action == "manual" and r["status"] in ("new", "approved", "failed"):
                self.store.set_status(r["id"], "manual")
            elif action == "skip" and r["status"] in ("new", "approved", "manual", "failed"):
                self.store.set_status(r["id"], "skipped")
            elif action == "contacted" and r["status"] in ("manual", "new", "approved"):
                self.store.set_status(r["id"], "contacted")
            elif action == "restore" and r["status"] in ("skipped", "failed"):
                self.store.set_status(r["id"], "new")
            elif action == "restore_skip" and r["status"] == "skipped":
                self.store.set_status(r["id"], "manual")
            elif action == "undo_contacted" and r["status"] == "contacted":
                self.store.set_status(r["id"], "manual")
                with self.store.lock:   # убираем отметку из счётчика «сегодня во ВК»
                    self.store.conn.execute(
                        "DELETE FROM send_log WHERE id = (SELECT MAX(id) FROM send_log WHERE lead_id=? AND email LIKE 'vk:%')",
                        (r["id"],))
                    self.store.conn.commit()
            else:
                continue
            done += 1
        return {"ok": True, "done": done}

    def vkb_open(self):
        self.vkb.open()
        return {"ok": True, **self.vkb.status()}

    def send_vk_browser(self, lead_id, text):
        """Пробел в окне бота: одно сообщение одному сообществу через окно браузера."""
        r = self.store.get(int(lead_id or 0))
        if not r or r["status"] not in ("manual", "new", "approved", "failed"):
            return {"error": "Эта организация уже обработана"}
        target = r["vk_group_id"] or r["screen_name"]
        if not target:
            return {"error": "У организации нет группы ВК"}
        if self.store.vk_sent_today() >= self.cfg.vk_daily_limit:
            return {"error": f"Дневной лимит во ВК ({self.cfg.vk_daily_limit}) исчерпан. "
                             f"Его можно поднять в настройках (VK_DAILY_LIMIT)"}
        text = (text or "").strip()
        if not text:
            return {"error": "Текст сообщения пустой"}
        if self.store.vk_already_sent(r["vk_group_id"], r["screen_name"], r["id"]):
            # этому сообществу бот уже писал (в том числе из другой карточки) — второй раз не пишем
            self.store.set_status(r["id"], "contacted", "уже писали раньше")
            return {"ok": True, "already": True, "sent_today": self.store.vk_sent_today()}
        name = (r["name"] or "").strip()
        marker = f"«{name}»" if name and f"«{name}»" in text else ""
        with self._vk_lock:
            wait = 1.5 - (time.monotonic() - self._vk_last_send)   # защита от двойного нажатия
            if wait > 0:
                self._sleep(wait)
            try:
                res = self.vkb.send(target, text, marker=marker)
            finally:
                self._vk_last_send = time.monotonic()
        if res.get("gid") and not r["vk_group_id"]:
            try:   # запоминаем ID — в следующий раз сразу в диалог
                self.store.update_fields(r["id"], vk_group_id=int(res["gid"]))
            except (ValueError, TypeError):
                pass
        if res.get("ok") and res.get("already"):     # в диалоге уже есть наше сообщение
            self.store.set_status(r["id"], "contacted", "уже писали раньше (нашла в диалоге)")
            return {"ok": True, "already": True, "sent_today": self.store.vk_sent_today()}
        if res.get("ok"):
            self.store.set_status(r["id"], "contacted", "написали во ВК")
            self.store.log_send(r["id"], f"vk:{res.get('gid') or target}", True)
            return {"ok": True, "sent_today": self.store.vk_sent_today()}
        if res.get("closed"):
            self.store.update_fields(r["id"], note="сообщения сообщества закрыты")
        return res

    # ---------- отправка отмеченных карточек (не больше BATCH_MAX за раз) ----------
    BATCH_MAX = 100

    def start_vk_batch(self, ids, texts=None):
        if self.batch["running"]:
            return {"error": "Отправка уже идёт"}
        if self.vkb.state != "ready":
            return {"error": "Сначала включите отправку и войдите во ВК в окне браузера"}
        ids = [int(i) for i in (ids or [])][: self.BATCH_MAX]
        rows = [r for r in (self.store.get(i) for i in ids)
                if r and r["status"] in ("manual", "new", "approved", "failed") and (r["vk_group_id"] or r["screen_name"])
                and niche_match(r, self.cfg.niche_only)]
        if not rows:
            return {"error": "Отметьте карточки, которым писать"}
        left = self.cfg.vk_daily_limit - self.store.vk_sent_today()
        if left <= 0:
            return {"error": f"Дневной лимит во ВК ({self.cfg.vk_daily_limit}) исчерпан"}
        rows = rows[:left]
        texts = {int(k): v for k, v in (texts or {}).items() if v and v.strip()}
        self._batch_stop.clear()
        self.batch = {"running": True, "total": len(rows), "done": 0, "sent": 0, "closed": 0, "errors": 0,
                      "already": 0, "window_lost": False, "current": "", "captcha": False}
        threading.Thread(target=self._run_batch, args=(rows, texts), daemon=True).start()
        return {"ok": True, "total": len(rows)}

    def stop_vk_batch(self):
        self._batch_stop.set()
        self._batch_resume.set()
        return {"ok": True}

    def resume_vk_batch(self):
        self._batch_resume.set()
        return {"ok": True}

    recover_wait = 90          # сколько ждать, пока окно ВК откроется заново (секунд)

    def _recover_window(self) -> bool:
        """Окно ВК пропало посреди рассылки — открываем снова и ждём, пока будет готово."""
        self.notify("🔄 Окно ВК закрылось или ВК попросил войти — открываю заново и продолжу. "
                    "Если просит пароль — войдите в окне, бот подождёт")
        try:
            self.vkb.open()
        except Exception:
            pass
        end = time.monotonic() + self.recover_wait
        while time.monotonic() < end and not self._batch_stop.is_set():
            if self.vkb.state == "ready":
                return True
            self._batch_stop.wait(1)
        return self.vkb.state == "ready"

    def _run_batch(self, rows, texts):
        b = self.batch
        recoveries, transient = 0, 0
        try:
            for i, r in enumerate(rows):
                if self._batch_stop.is_set():
                    break
                b["current"] = r["name"]
                text = texts.get(r["id"]) or self.drafter.vk(r)
                while True:
                    res = self.send_vk_browser(r["id"], text)
                    if res.get("ok") and res.get("already"):
                        b["already"] += 1
                        break
                    if res.get("ok"):
                        b["sent"] += 1
                        break
                    if res.get("closed"):          # сообщения закрыты — тихо пропускаем
                        self.store.set_status(r["id"], "skipped", "сообщения сообщества закрыты")
                        b["closed"] += 1
                        break
                    if "капч" in (res.get("error") or ""):   # ждём, пока решат капчу в окне браузера
                        b["captcha"] = True
                        self._batch_resume.clear()
                        self._batch_resume.wait()
                        b["captcha"] = False
                        if self._batch_stop.is_set():
                            break
                        continue
                    err = res.get("error") or ""
                    if any(m in err for m in WINDOW_GONE) and recoveries < 3 and not self._batch_stop.is_set():
                        recoveries += 1
                        if self._recover_window():
                            continue                       # окно снова готово — повторяем эту же карточку
                    b["errors"] += 1
                    if any(m in err for m in WINDOW_GONE):
                        # окно так и не вернулось — карточка не виновата, оставляем в очереди и стоп
                        self.store.update_fields(r["id"], note="не отправилось: " + err[:120])
                        b["window_lost"] = True
                        self._batch_stop.set()
                    elif any(m in err for m in TRANSIENT):
                        # разовый сбой — оставляем в очереди (если всё же ушло, повторно бот не напишет)
                        self.store.update_fields(r["id"], note="не отправилось: " + err[:120])
                        transient += 1
                        if transient >= 3:
                            b["auto_stop"] = True
                            self._batch_stop.set()
                    else:
                        # не отправилось по вине группы/страницы — убираем в «Пропущенные», чтобы не мешалась
                        self.store.set_status(r["id"], "skipped", "не отправилось: " + err[:120])
                    break
                if res.get("ok"):
                    transient = 0
                b["done"] = i + 1
                if self._batch_stop.is_set() or "лимит" in str(res.get("error", "")):
                    break
                if b["errors"] >= 3 and b["sent"] == 0:
                    self.notify("⛔ Три ошибки подряд — остановила отправку. Посмотрите на окно ВК")
                    break
                if i < len(rows) - 1:
                    self._batch_stop.wait(random.uniform(*self.batch_pause))
        finally:
            msg = f"📨 ВК: отправлено {b['sent']} из {b['total']}"
            if b.get("already"):
                msg += f", {b['already']} уже получали сообщение раньше (не писала повторно)"
            if b["closed"]:
                msg += f", у {b['closed']} закрыты сообщения (пропущены)"
            if b["errors"]:
                msg += f", не отправилось {b['errors']}"
            if b.get("window_lost"):
                msg += " — окно ВК закрылось, отправку остановила. Неотправленные остались в очереди"
            elif b.get("auto_stop"):
                msg += (" — три сообщения подряд не ушли, остановила. Похоже, ВК ограничил отправку "
                        "(капча или лимит сообщений на сегодня) — посмотрите на окно ВК. Эти карточки остались в очереди")
            elif self._batch_stop.is_set():
                msg += " — остановлено вами"
            self.notify(msg)
            b["running"] = False
            b["current"] = ""

    def vk_opened(self, lead_id):
        """Нажата «Скопировать и открыть диалог»: организация сразу уходит в «Связались»."""
        r = self.store.get(int(lead_id or 0))
        if not r or r["status"] not in ("manual", "new", "approved", "failed"):
            return {"error": "Эта организация уже обработана"}
        if self.store.vk_sent_today() >= self.cfg.vk_daily_limit:
            return {"error": f"Дневной лимит во ВК ({self.cfg.vk_daily_limit}) исчерпан. "
                             f"Его можно поднять в настройках (VK_DAILY_LIMIT)"}
        self.store.set_status(r["id"], "contacted", "написали во ВК")
        self.store.log_send(r["id"], f"vk:{r['vk_group_id'] or r['screen_name']}", True)
        return {"ok": True, "sent_today": self.store.vk_sent_today()}

    # ---------- отправка во ВК: одна кнопка на одно сообщение ----------
    def send_vk(self, lead_id, text, captcha_sid=None, captcha_key=None):
        from .finder import VKError
        r = self.store.get(int(lead_id or 0))
        if not r or r["status"] not in ("manual", "new", "approved", "failed"):
            return {"error": "Эта организация уже обработана"}
        if not (r["vk_group_id"] or r["screen_name"]):
            return {"error": "У организации нет группы ВК"}
        if not self.vk_finder.can_search:
            return {"error": "Нужен токен ВК с доступом к сообщениям (см. «Настройки»)"}
        if self.vk_messages is None:
            self.check_vk_messages()
        if not self.vk_messages:
            return {"error": "У токена ВК нет доступа к сообщениям. Получите токен заново по ссылке "
                             "из инструкции (в ней есть разрешение messages)"}
        if self.store.vk_sent_today() >= self.cfg.vk_daily_limit:
            return {"error": f"На сегодня отправлено {self.cfg.vk_daily_limit} сообщений во ВК — это дневной "
                             f"лимит. Продолжим завтра, так страницу не ограничат"}
        text = (text or "").strip()
        if not text:
            return {"error": "Текст сообщения пустой"}
        if len(text) > 4000:
            return {"error": "Сообщение слишком длинное (больше 4000 символов)"}

        with self._vk_lock:
            wait = 3 - (time.monotonic() - self._vk_last_send)   # защита от двойного клика
            if wait > 0:
                self._sleep(wait)
            gid = r["vk_group_id"]
            try:
                if not gid:
                    groups = self.vk_finder.get_groups([r["screen_name"]])
                    if not groups:
                        return {"error": "Группа ВК не найдена — возможно, её удалили"}
                    gid = groups[0]["id"]
                    self.store.update_fields(r["id"], vk_group_id=gid)
                self.vk_finder.vk.call(
                    "messages.send", _retry_flood=False, peer_id=-int(gid), message=text,
                    random_id=random.randint(1, 2**31 - 1), captcha_sid=captcha_sid, captcha_key=captcha_key)
            except VKError as exc:
                if exc.code == 14:
                    return {"captcha": {"sid": exc.data.get("captcha_sid"), "img": exc.data.get("captcha_img")}}
                self.store.log_send(r["id"], f"vk:{gid}", False, str(exc))
                if exc.code == 9:
                    return {"error": "ВК ограничил частоту сообщений. Сделайте перерыв хотя бы на час"}
                if exc.code in (5, 7, 15, 27):
                    self.vk_messages = False
                    return {"error": "ВК не разрешает этому токену отправлять сообщения. Получите токен заново"}
                if exc.code in (900, 901, 902, 945, 917, 100):
                    self.store.update_fields(r["id"], note="сообщения сообщества закрыты")
                    return {"error": "У этого сообщества закрыты сообщения — попробуйте WhatsApp или телефон",
                            "closed": True}
                return {"error": f"ВК не принял сообщение: {exc.message}"}
            finally:
                self._vk_last_send = time.monotonic()
        self.store.set_status(r["id"], "contacted", "написали во ВК")
        self.store.log_send(r["id"], f"vk:{gid}", True)
        return {"ok": True, "sent_today": self.store.vk_sent_today()}

    def blacklist(self, value):
        if not (value or "").strip():
            return {"error": "Укажите почту, телефон или ссылку на группу"}
        v = self.store.blacklist_add(value)
        self.notify(f"⛔ {v} — в чёрном списке, писать больше не будем")
        return {"ok": True}

    def send(self):
        if not self.cfg.email_ready():
            return {"error": "Почта не настроена: заполните SMTP_USER и SMTP_PASSWORD в .env"}
        msg = self.sender.start()
        self.notify("📨 " + msg)
        return {"ok": True, "message": msg}

    def stop(self):
        if self.sender.running:
            self.sender.stop()
            return {"ok": True, "message": "Останавливаю после текущего письма"}
        return {"ok": True, "message": "Рассылка не идёт"}

    def skip_dead_site_leads(self) -> int:
        """Организации из очереди, у которых указан (пусть и неоткрывающийся) сайт, — в «Пропущенные»."""
        moved = 0
        for r in self.store.by_status("new", "approved", "manual", "failed"):
            if "не открывается" in (r["note"] or ""):
                self.store.set_status(r["id"], "skipped", (r["note"] or "")[:200])
                moved += 1
        return moved

    def check(self):
        lines = []
        if not self.vk_finder.can_search:
            lines.append(["warn", "Токена ВК нет — поиск только по карте, отправка во ВК вручную"])
            if not self.cfg.vk_group_token:
                lines.append(["warn", "Нет ключа сообщества (VK_GROUP_TOKEN) — бот не видит сайты, указанные "
                                      "в группах ВК, и пишет тем, у кого сайт уже есть"])
        else:
            try:
                self.vk_finder.vk.call("database.getCities", country_id=1, q="Москва", count=1)
                lines.append(["ok", "Токен ВК работает — поиск во ВКонтакте включён"])
                if self.check_vk_messages():
                    lines.append(["ok", f"Отправка во ВК одной кнопкой включена "
                                        f"(не больше {self.cfg.vk_daily_limit} в день)"])
                else:
                    lines.append(["warn", "У токена нет доступа к сообщениям — отправка во ВК вручную"])
            except Exception as exc:
                t = self.cfg.vk_user_token
                shown = f"{t[:10]}…{t[-4:]}, {len(t)} символов" if len(t) > 16 else f"{len(t)} символов"
                hint = (" Обычно токен начинается с vk1.a. и длиной 200+ символов — похоже, скопирован не полностью."
                        if not t.startswith("vk1.a.") or len(t) < 150 else
                        " Сам токен выглядит целым — значит, ВК его отозвал: получите новый и вставьте именно его.")
                lines.append(["err", f"Токен ВК не принят ({shown}).{hint}"])
        if self.cfg.vk_group_token:
            try:
                self.vk_finder.group_vk.call("groups.getById", group_ids="vk")
                lines.append(["ok", "Ключ сообщества работает — бот проверяет сайты в группах ВК"])
            except Exception as exc:
                lines.append(["err", f"Ключ сообщества не принят: {exc}"])
        if not self.cfg.email_ready():
            lines.append(["warn", "Почта не настроена — письма отправлять нельзя"])
        elif self.mail_check:
            err = self.mail_check()
            lines.append(["ok", f"Почта {self.cfg.smtp_user} работает"] if not err else ["err", f"Почта: {err}"])
        return {"lines": lines}

    def templates(self):
        return {k: template_path(f).read_text(encoding="utf-8") for k, f in EDITABLE_TEMPLATES.items()}

    def save_template(self, kind, text):
        if kind not in EDITABLE_TEMPLATES:
            return {"error": "Неизвестный шаблон"}
        USER_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
        (USER_TEMPLATES_DIR / EDITABLE_TEMPLATES[kind]).write_text(text.replace("\r\n", "\n"), encoding="utf-8")
        return {"ok": True}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _json(self, data, code=200):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlparse(self.path)
            q = parse_qs(url.query)
            try:
                if url.path in ("/", "/index.html"):
                    body = (STATIC / "index.html").read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif url.path == "/api/state":
                    self._json(app.state())
                elif url.path == "/api/lead":
                    self._json(app.lead(int(q.get("id", ["0"])[0])))
                elif url.path == "/api/templates":
                    self._json(app.templates())
                elif url.path == "/api/template.csv":
                    body = make_template_csv()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/csv; charset=utf-8")
                    self.send_header("Content-Disposition", 'attachment; filename="shablon_organizacii.csv"')
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif url.path == "/api/export.csv":
                    import tempfile, os
                    fd, tmp = tempfile.mkstemp(suffix=".csv")
                    os.close(fd)
                    app.store.export_csv(tmp)
                    body = Path(tmp).read_bytes()
                    os.remove(tmp)
                    name = f"lstudio_{datetime.now():%Y-%m-%d}.csv"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/csv; charset=utf-8")
                    self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self._json({"error": "not found"}, 404)
            except Exception as exc:
                traceback.print_exc()
                self._json({"error": str(exc)}, 500)

        def do_POST(self):
            # Защита: запросы принимаются только от самой страницы бота
            if self.headers.get("X-LS") != "1":
                return self._json({"error": "forbidden"}, 403)
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            path = urlparse(self.path).path
            try:
                if path == "/api/search":
                    res = app.start_search(data.get("source", "both"), data.get("query"),
                                           data.get("city"), data.get("want", 20))
                elif path == "/api/import":
                    import base64
                    try:
                        raw = base64.b64decode(data.get("data", ""))
                    except ValueError:
                        return self._json({"error": "Файл повреждён"}, 400)
                    res = app.import_file(data.get("filename", ""), raw, data.get("source", "file"))
                elif path == "/api/vkb_open":
                    res = app.vkb_open()
                elif path == "/api/vk_batch":
                    res = app.start_vk_batch(data.get("ids"), data.get("texts"))
                elif path == "/api/vk_batch_stop":
                    res = app.stop_vk_batch()
                elif path == "/api/vk_batch_resume":
                    res = app.resume_vk_batch()
                elif path == "/api/send_vkb":
                    res = app.send_vk_browser(data.get("id"), data.get("text"))
                elif path == "/api/vk_opened":
                    res = app.vk_opened(data.get("id"))
                elif path == "/api/send_vk":
                    res = app.send_vk(data.get("id"), data.get("text"), data.get("captcha_sid"),
                                      data.get("captcha_key"))
                elif path == "/api/links":
                    res = app.check_links(data.get("text", ""))
                elif path == "/api/action":
                    res = app.action(data.get("action"), data.get("ids", []))
                elif path == "/api/blacklist":
                    res = app.blacklist(data.get("value", ""))
                elif path == "/api/send":
                    res = app.send()
                elif path == "/api/stop":
                    res = app.stop()
                elif path == "/api/check":
                    res = app.check()
                elif path == "/api/templates":
                    res = app.save_template(data.get("kind"), data.get("text", ""))
                else:
                    return self._json({"error": "not found"}, 404)
                self._json(res, 400 if "error" in res else 200)
            except Exception as exc:
                traceback.print_exc()
                self._json({"error": str(exc)}, 500)

    return Handler


def serve(app: App, port: int = 8765):
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    server.daemon_threads = True
    return server
