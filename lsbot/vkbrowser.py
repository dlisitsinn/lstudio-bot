"""Отправка сообщений во ВК через отдельное окно браузера — строго по одному, по нажатию человека.

Бот открывает ваш Chrome (или Edge) с отдельным профилем, где вы один раз входите во ВК.
По команде send() он открывает диалог с сообществом, вставляет текст и нажимает «Отправить».
Сам по себе модуль ничего не отправляет: каждый вызов send() — это одно нажатие пользователя.
"""
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

VK_BASE = "https://vk.com"

INPUT_SELECTORS = (
    '[contenteditable="true"][role="textbox"]',
    "div.im_editable",
    ".ComposerInput__input",
    '[data-testid="composer_input"] [contenteditable="true"]',
    '[contenteditable="true"]',
)
CAPTCHA_SELECTORS = ('img[src*="captcha"]', '[class*="captcha" i]')
# фразы ВК, когда сообществу нельзя написать — пропускаем сразу, не дожидаясь поля ввода
CLOSED_MARKERS = ("ограничил", "не принимает сообщ", "нельзя отправить", "не можете отправить",
                  "сообщения отключены", "сообщения сообщества отключены")


class VKBrowser:
    """Все обращения к браузеру идут через один рабочий поток (так требует Playwright)."""

    def __init__(self, profile_dir: Path, base: str = VK_BASE, channels=("chrome", "msedge", None),
                 headless: bool = False, notify=None, auto_install: bool = True):
        self.profile_dir = Path(profile_dir)
        self.base = base.rstrip("/")
        self.channels = channels
        self.headless = headless
        self.notify = notify or (lambda *_: None)
        self.auto_install = auto_install
        self.state = "off"        # off | installing | starting | login | ready | error
        self.message = ""
        self._tasks = queue.Queue()
        self._thread = None
        self._pw = self._ctx = self._page = None

    # ---------- публичное ----------
    def open(self):
        """Запустить браузер (и при необходимости установить Playwright). Не блокирует."""
        if self._thread and self._thread.is_alive():
            if self.state in ("error", "off"):
                self._tasks.put(("start", None, None))
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._tasks.put(("start", None, None))

    def status(self) -> dict:
        return {"state": self.state, "message": self.message}

    def send(self, target: str, text: str, timeout: float = 60, marker: str = "") -> dict:
        """Отправить одно сообщение. target — ID сообщества (число) или короткий адрес.
        marker — кусок текста (например «Название»): если он уже есть в диалоге, второй раз не пишем."""
        if self.state != "ready":
            return {"error": "Окно ВК не готово: " + (self.message or "нажмите «Включить отправку клавишей»")}
        done = queue.Queue()
        self._tasks.put(("send", (target, text, marker), done))
        try:
            return done.get(timeout=timeout)
        except queue.Empty:
            return {"error": "ВК слишком долго не отвечает. Посмотрите на окно браузера"}

    def close(self):
        self._tasks.put(("stop", None, None))

    # ---------- рабочий поток ----------
    def _set(self, state, message=""):
        self.state, self.message = state, message

    def _loop(self):
        while True:
            try:
                cmd, args, done = self._tasks.get(timeout=3)
            except queue.Empty:
                if self.state == "login":
                    self._check_login()
                continue
            try:
                if cmd == "start":
                    self._start()
                elif cmd == "send":
                    done.put(self._send(*args))
                elif cmd == "stop":
                    self._shutdown()
                    return
            except Exception as exc:  # браузер закрыли вручную и т.п.
                if done is not None:
                    done.put({"error": f"Ошибка окна ВК: {exc}"})
                if cmd == "send" and "closed" in str(exc).lower():
                    self._set("off", "Окно ВК закрыто. Нажмите «Включить отправку клавишей»")
                    self._shutdown()

    def _ensure_playwright(self):
        try:
            import playwright.sync_api  # noqa: F401
            return True
        except ImportError:
            if not self.auto_install:
                return False
        self._set("installing", "Устанавливаю модуль для управления браузером (один раз, ~1 минута)…")
        self.notify("Устанавливаю модуль для окна ВК (один раз, около минуты)…")
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "playwright"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            self._set("error", "Не удалось установить модуль playwright: " + (r.stderr or r.stdout)[-300:])
            return False
        import importlib
        importlib.invalidate_caches()
        return True

    def _start(self):
        if self._ctx is not None:
            try:
                self._page.bring_to_front()
                self._check_login()
                return
            except Exception:
                self._shutdown()
        if not self._ensure_playwright():
            if self.state != "error":
                self._set("error", "Модуль playwright не установлен")
            return
        from playwright.sync_api import sync_playwright
        self._set("starting", "Открываю окно браузера…")
        self._pw = sync_playwright().start()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        last = None
        for channel in self.channels:
            try:
                kw = {"headless": self.headless, "viewport": None}
                if channel:
                    kw["channel"] = channel
                self._ctx = self._pw.chromium.launch_persistent_context(str(self.profile_dir), **kw)
                break
            except Exception as exc:
                last = exc
        if self._ctx is None and self.auto_install:
            # нет ни Chrome, ни Edge — скачиваем встроенный браузер (один раз)
            self._set("installing", "Скачиваю браузер (один раз, ~150 МБ)…")
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], capture_output=True)
            try:
                self._ctx = self._pw.chromium.launch_persistent_context(
                    str(self.profile_dir), headless=self.headless, viewport=None)
            except Exception as exc:
                last = exc
        if self._ctx is None:
            self._set("error", f"Не удалось открыть браузер: {last}")
            return
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self._page.goto(self.base + "/feed", wait_until="domcontentloaded")
        self._check_login()

    def _logged_in(self) -> bool:
        try:
            cookies = self._ctx.cookies()
        except Exception:
            return False
        return any(c["name"].startswith(("remixsid", "remixnsid")) and c.get("value") for c in cookies)

    def _check_login(self):
        if self._ctx is None:
            return
        if self._logged_in() and not (self._page is not None and self._needs_login()):
            self._set("ready", "Окно ВК готово. Пробел — отправить, S — пропустить")
        else:
            self._set("login", "Войдите во ВК в открывшемся окне браузера — бот подождёт")

    def _find_input(self, timeout_ms=8000):
        """Ждём поле ввода сообщения, капчу или надпись «сообщения закрыты» — что появится раньше."""
        combined = ", ".join(INPUT_SELECTORS)
        captcha = ", ".join(CAPTCHA_SELECTORS)
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            try:
                self._page.wait_for_selector(f"{combined}, {captcha}", state="visible", timeout=1200)
            except Exception:
                pass
            cap = self._page.locator(captcha)
            if cap.count() and cap.first.is_visible():
                return "captcha"
            box = self._page.locator(combined)
            if box.count() and box.first.is_visible():
                return box.first
            try:
                body = (self._page.inner_text("body", timeout=800) or "").lower()
            except Exception:
                body = ""
            if any(m in body for m in CLOSED_MARKERS):
                return "closed"
        return None

    def _base_for_cookies(self):
        """ВК переехал на vk.ru: открываем тот домен, где вы вошли."""
        try:
            for c in self._ctx.cookies():
                if c["name"].startswith(("remixsid", "remixnsid")) and c.get("value"):
                    dom = c.get("domain", "").lstrip(".")
                    if dom.endswith("vk.ru"):
                        return "https://vk.ru"
                    if dom.endswith("vk.com"):
                        return "https://vk.com"
        except Exception:
            pass
        return self.base

    def resolve_id(self, screen: str):
        """Числовой ID сообщества по короткому адресу — берём со страницы группы."""
        import re
        screen = str(screen).strip().strip("/")
        m = re.fullmatch(r"(?:club|public|event)?(\d+)", screen)
        if m:
            return m.group(1)
        base = self._base_for_cookies() if self.base in (VK_BASE, "https://vk.ru") else self.base
        self._page.goto(f"{base}/{screen}", wait_until="domcontentloaded")
        html = self._page.content()
        for pat in (r'"group_id"\s*:\s*"?(\d+)', r'im\?sel=-(\d+)', r'/im/convo/-(\d+)', r'wall-(\d+)_',
                    r'"oid"\s*:\s*-(\d+)', r'/(?:club|public)(\d+)', r'photo-(\d+)_', r'video-(\d+)_'):
            m = re.search(pat, html)
            if m:
                return m.group(1)
        return None

    def _needs_login(self) -> bool:
        """ВК выкинул на страницу входа (сменили пароль, вышли из аккаунта)."""
        try:
            url = self._page.url or ""
            if "/login" in url or "id.vk." in url or "oauth.vk" in url:
                return True
            pw = self._page.locator('input[type="password"], input[name="login"], input[name="phone"]')
            return bool(pw.count() and pw.first.is_visible())
        except Exception:
            return False

    def _dialog_has(self, marker: str, box=None) -> bool:
        """Есть ли в открытом диалоге наше прошлое сообщение (ищем кусок текста с названием)."""
        if not marker:
            return False
        try:
            self._page.wait_for_timeout(700)            # даём подгрузиться истории переписки
            body = self._page.inner_text("body", timeout=2000) or ""
            draft = (box.inner_text(timeout=1000) or "") if box is not None else ""
        except Exception:
            return False
        norm = lambda t: re.sub(r"\s+", " ", t.replace("\u00a0", " ")).strip().lower()
        body, draft = norm(body), norm(draft)
        if draft:                                     # неотправленный черновик в поле ввода не считается
            body = body.replace(draft, " ", 1)
        return norm(marker) in body

    def _send(self, target, text, marker=""):
        self._check_login()
        if self.state != "ready":
            return {"error": self.message}
        target = str(target)
        self._page.bring_to_front()
        gid = target if target.isdigit() else self.resolve_id(target)
        if not gid:
            return {"error": "Не удалось открыть сообщество — возможно, его удалили или адрес неверный",
                    "closed": True}
        base = self._base_for_cookies() if self.base in (VK_BASE, "https://vk.ru") else self.base
        self._page.goto(f"{base}/im?sel=-{gid}", wait_until="domcontentloaded")
        if self._needs_login():
            self._set("login", "ВК просит войти заново — войдите в окне браузера, бот подождёт")
            return {"error": "Окно ВК не готово: ВК просит войти заново (например, после смены пароля)"}
        box = self._find_input()
        if box == "captcha":
            return {"error": "ВК показал капчу — решите её в окне браузера и нажмите пробел ещё раз", "gid": gid}
        if box == "closed":
            return {"error": "У сообщества закрыты сообщения", "closed": True, "gid": gid}
        if box is None:
            if self._needs_login():
                self._set("login", "ВК просит войти заново — войдите в окне браузера, бот подождёт")
                return {"error": "Окно ВК не готово: ВК просит войти заново (например, после смены пароля)"}
            # поле не нашлось, но и «закрыто» не написано — не пропускаем карточку, оставляем в очереди
            return {"error": "Не нашла поле для сообщения — карточка осталась в очереди", "gid": gid}
        if "/im" not in self._page.url:
            # защита: печатаем только в мессенджере, никогда — в комментарии на странице
            return {"error": "Открылся не диалог, а другая страница — ничего не отправила", "gid": gid}
        if self._dialog_has(marker, box):
            # в этом диалоге наше сообщение уже есть — второй раз не пишем
            return {"ok": True, "already": True, "gid": gid}
        box.click()
        try:                                          # убрать черновик, оставшийся от прошлой попытки
            if (box.inner_text(timeout=500) or "").strip():
                self._page.keyboard.press("Control+A")
                self._page.keyboard.press("Delete")
        except Exception:
            pass
        lines = text.replace("\r\n", "\n").split("\n")
        for i, line in enumerate(lines):
            if line:
                self._page.keyboard.insert_text(line)
            if i < len(lines) - 1:
                self._page.keyboard.press("Shift+Enter")
        self._page.keyboard.press("Enter")
        for _ in range(25):               # сообщение ушло, когда поле очистилось
            self._page.wait_for_timeout(120)
            try:
                if not (box.inner_text() or "").strip():
                    return {"ok": True, "gid": gid}
            except Exception:
                return {"ok": True, "gid": gid}
        if self._find_input(800) == "captcha":
            return {"error": "ВК показал капчу — решите её в окне браузера и нажмите пробел ещё раз", "gid": gid}
        return {"error": "Текст вставлен, но не отправился — посмотрите на окно браузера", "gid": gid}

    def _shutdown(self):
        for obj in (self._ctx, self._pw):
            try:
                if obj is self._pw and obj is not None:
                    obj.stop()
                elif obj is not None:
                    obj.close()
            except Exception:
                pass
        self._ctx = self._pw = self._page = None
