"""Поиск групп ВКонтакте без собственного сайта и сбор их публичных контактов."""
import re
import urllib.error
import urllib.request
from urllib.parse import urlparse

from .vk import VK, VKError

GROUP_FIELDS = "site,contacts,description,activity,city,members_count,links,status,can_message"

# Ссылки, которые НЕ считаются собственным сайтом организации:
# соцсети, мессенджеры, «мультиссылки», агрегаторы, онлайн-запись, маркетплейсы.
NOT_A_SITE = (
    "vk.com", "vk.ru", "vk.me", "vk.cc", "vkontakte.ru", "m.vk.com",
    "t.me", "telegram.me", "telegram.org", "wa.me", "whatsapp.com", "viber.com",
    "instagram.com", "instagr.am", "facebook.com", "fb.com", "ok.ru", "odnoklassniki.ru",
    "youtube.com", "youtu.be", "rutube.ru", "tiktok.com", "dzen.ru", "zen.yandex.ru",
    "pinterest.com", "pinterest.ru", "twitter.com", "x.com", "max.ru",
    "taplink.cc", "taplink.ru", "taplink.at", "linktr.ee", "hipolink.me", "hipolink.net",
    "mssg.me", "msg.link", "lnk.bio", "bio.link", "clck.ru",
    "2gis.ru", "2gis.com", "go.2gis.com", "yandex.ru", "ya.ru", "yandex.com",
    "google.com", "goo.gl", "maps.app.goo.gl", "zoon.ru", "flamp.ru", "yell.ru",
    "avito.ru", "ozon.ru", "wildberries.ru", "wb.ru", "market.yandex.ru",
    "dikidi.net", "dikidi.ru", "dikidi.online", "dikidi.app", "yclients.com", "n.yclients.com", "sonline.su",
    "booksy.com", "profi.ru", "uslugi.yandex.ru", "youdo.com",
    "mail.ru", "gmail.com",
    # мультиссылки, онлайн-запись, каталоги мастеров — это не собственный сайт
    "mst.link", "taplink.ws", "yclients.ru", "moiprofi.ru", "arnica.pro", "jivo.chat", "jivosite.com",
    "pulscen.ru", "obiz.ru", "tbookme.ru", "booknow.ru", "beautyagent.ru", "tetradka.io",
    "2gis.com", "flamp.com", "restoclub.ru", "tripadvisor.ru", "orgpage.ru", "spr.ru",
)

URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?((?:[a-zа-яё0-9-]+\.)+(?:[a-z]{2,24}|рф|рус))(/[^\s,;()<>\"']*)?",
    re.IGNORECASE,
)
EMAIL_RE = re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,24}", re.IGNORECASE)
PHONE_RE = re.compile(r"(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}")

# Частые «ложные» домены, которые попадают из текста (форматы файлов и т.п.)
FAKE_TLDS = {"jpg", "jpeg", "png", "gif", "pdf", "doc", "docx", "mp4", "txt", "zip", "rar", "html"}


def _domain(url_or_host: str) -> str:
    text = url_or_host.strip().lower()
    if "://" not in text:
        text = "http://" + text
    host = urlparse(text).hostname or ""
    return host[4:] if host.startswith("www.") else host


def is_social_or_aggregator(url: str) -> bool:
    host = _domain(url)
    if not host:
        return True
    return any(host == d or host.endswith("." + d) for d in NOT_A_SITE)


def extract_urls(text: str) -> list:
    if not text:
        return []
    text = EMAIL_RE.sub(" ", text)  # адрес почты — не сайт
    found = []
    for m in URL_RE.finditer(text):
        host = m.group(1).lower()
        tld = host.rsplit(".", 1)[-1]
        if tld in FAKE_TLDS or host[0].isdigit() and host.replace(".", "").isdigit():
            continue
        # отбрасываем куски e-mail (user@domain.ru): перед доменом стоит «@»
        start = m.start()
        if start > 0 and text[start - 1] == "@":
            continue
        found.append(m.group(0).rstrip(".!?:"))
    return found


def extract_emails(text: str) -> list:
    return [e.lower().rstrip(".") for e in EMAIL_RE.findall(text or "")]


def extract_phones(text: str) -> list:
    return [re.sub(r"[^\d+]", "", p) for p in PHONE_RE.findall(text or "")]


def own_sites(group: dict) -> list:
    """Все ссылки группы, похожие на собственный сайт организации."""
    candidates = []
    if group.get("site"):
        candidates += re.split(r"[\s,]+", group["site"])
    for link in group.get("links") or []:
        if link.get("url"):
            candidates.append(link["url"])
    candidates += extract_urls(group.get("description", ""))
    candidates += extract_urls(group.get("status", ""))
    result = []
    for url in candidates:
        url = url.strip()
        if url and "@" not in url and not is_social_or_aggregator(url):
            if _domain(url) not in [_domain(u) for u in result]:
                result.append(url)
    return result


def site_is_alive(url: str, timeout: int = 12) -> bool:
    """Открывается ли сайт. Любой ответ сервера, кроме 404/410/5xx, считаем «живым».

    Пробуем и https, и http, а при ошибке сертификата — без проверки сертификата:
    у многих российских сайтов сертификат Минцифры, который Python не знает, хотя в браузере сайт открывается.
    """
    import ssl
    bare = re.sub(r"^https?://", "", url.strip(), flags=re.I)
    tries = [url if "://" in url else "https://" + bare, "http://" + bare]
    insecure = ssl.create_default_context()
    insecure.check_hostname = False
    insecure.verify_mode = ssl.CERT_NONE
    for u in dict.fromkeys(tries):
        for ctx in (None, insecure):
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                                                   "AppleWebKit/537.36 Chrome/130 Safari/537.36"})
            try:
                with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                    if resp.status < 500:
                        return True
            except urllib.error.HTTPError as exc:
                if exc.code not in (404, 410) and exc.code < 500:
                    return True
            except Exception as exc:
                if ctx is None and "CERTIFICATE" in str(exc).upper():
                    continue            # повторим без проверки сертификата
                break
    return False


def analyze_group(group: dict, min_members: int = 0, check_alive=site_is_alive, skip_dead: bool = False):
    """Возвращает словарь-лид, если у группы нет работающего сайта, иначе None.

    skip_dead=True: если в группе вообще указан сайт (даже неоткрывающийся) — организацию пропускаем.
    """
    if group.get("deactivated") or group.get("is_closed", 0) != 0:
        return None
    if group.get("members_count", 0) < min_members:
        return None

    note = None
    sites = own_sites(group)
    if sites:
        if check_alive is None or skip_dead:
            return None
        if any(check_alive(u) for u in sites):
            return None
        note = "указан сайт, но он не открывается: " + ", ".join(sites[:2])

    emails, phones = [], []
    for c in group.get("contacts") or []:
        if c.get("email"):
            emails.append(c["email"].strip().lower())
        if c.get("phone"):
            phones.append(c["phone"].strip())
    emails += extract_emails(group.get("description", ""))
    phones += extract_phones(group.get("description", ""))

    other = [u for u in extract_urls(group.get("description", "")) if is_social_or_aggregator(u)]
    other = [u for u in other if not _domain(u).endswith(("vk.com", "vk.ru"))]

    if group.get("can_message") == 0:
        note = "; ".join(x for x in (note, "сообщения сообщества закрыты") if x)
    return {
        "uid": f"vk:{group['id']}",
        "source": "vk",
        "vk_group_id": group["id"],
        "name": group.get("name", "").strip(),
        "screen_name": group.get("screen_name"),
        "city": (group.get("city") or {}).get("title"),
        "activity": group.get("activity"),
        "members": group.get("members_count", 0),
        "email": _first_unique(emails),
        "phone": _first_unique(phones),
        "other_links": ", ".join(dict.fromkeys(other))[:300] or None,
        "note": note,
    }


def _first_unique(items):
    for item in items:
        if item:
            return item
    return None


VK_LINK_RE = re.compile(r"(?:https?://)?(?:m\.)?vk\.(?:com|ru)/([A-Za-z0-9_.]+)", re.IGNORECASE)
NOT_GROUP_PATHS = {"wall", "photo", "video", "im", "feed", "search", "id", "away.php", "app"}


def parse_group_links(text: str) -> list:
    """Достаёт из текста короткие адреса/ID групп по ссылкам vk.ru/... и vk.com/..."""
    names = []
    for name in VK_LINK_RE.findall(text or ""):
        name = name.strip(".").lower()
        m = re.fullmatch(r"(?:club|public|event)(\d+)", name)
        if m:
            name = m.group(1)
        elif re.fullmatch(r"id\d+", name) or name.split(".")[0] in NOT_GROUP_PATHS:
            continue
        if name and name not in names:
            names.append(name)
    return names


class Finder:
    def __init__(self, vk: VK, min_members: int = 30, check_alive=site_is_alive, group_vk: VK = None,
                 skip_dead: bool = False):
        self.vk = vk                    # токен страницы: нужен только для поиска (может быть None)
        self.group_vk = group_vk or vk  # токен сообщества: проверка групп по ссылкам
        self.min_members = min_members
        self.check_alive = check_alive
        self.skip_dead = skip_dead
        self._city_cache = {}

    def city_id(self, city: str):
        if not city:
            return None
        key = city.strip().lower()
        if key not in self._city_cache:
            resp = self.vk.call("database.getCities", country_id=1, q=city.strip(), count=1)
            items = resp.get("items", []) if resp else []
            self._city_cache[key] = items[0]["id"] if items else None
        return self._city_cache[key]

    @property
    def can_search(self) -> bool:
        return self.vk is not None

    @property
    def can_check_groups(self) -> bool:
        return self.group_vk is not None

    def check_links(self, text: str, skip_ids=()):
        """Проверяет группы по ссылкам. Возвращает (лиды без сайта, с сайтом, уже были в базе)."""
        names = parse_group_links(text)
        if not names:
            return [], [], []
        leads, with_site, known = [], [], []
        for group in self.get_groups(names):
            if group["id"] in skip_ids:
                known.append(group.get("name", ""))
                continue
            lead = analyze_group(group, 0, self.check_alive, self.skip_dead)
            if lead is None:
                with_site.append(group.get("name", ""))
            else:
                lead["query"] = "по ссылке"
                leads.append(lead)
        return leads, with_site, known

    def get_groups(self, ids: list) -> list:
        groups = []
        for i in range(0, len(ids), 400):
            resp = self.group_vk.call("groups.getById", group_ids=ids[i:i + 400], fields=GROUP_FIELDS)
            if isinstance(resp, dict):
                resp = resp.get("groups", [])
            groups += resp or []
        return groups

    def search(self, query: str, city: str = None, want: int = 20, skip_ids=(), progress=None):
        """Ищет до `want` новых групп без сайта. Возвращает (лиды, статистика)."""
        if self.vk is None:
            raise ValueError("Для автоматического поиска нужен VK_USER_TOKEN. "
                             "Пока можно присылать ссылки на группы — я их проверю.")
        city_id = self.city_id(city) if city else None
        if city and not city_id:
            raise ValueError(f"Не нашёл город «{city}» во ВКонтакте")

        stats = {"checked": 0, "with_site": 0, "skipped": 0}
        leads, seen, offset = [], set(skip_ids), 0
        batch = 200
        while len(leads) < want and offset < 1000:
            resp = self.vk.call(
                "groups.search", q=query, city_id=city_id, country_id=1 if city_id else None,
                count=batch, offset=offset,
            )
            items = resp.get("items", []) if resp else []
            if not items:
                break
            offset += batch
            ids = [g["id"] for g in items if g["id"] not in seen]
            seen.update(ids)
            for group in self.get_groups(ids):
                stats["checked"] += 1
                if group.get("is_closed", 0) != 0 or group.get("members_count", 0) < self.min_members:
                    stats["skipped"] += 1
                    continue
                lead = analyze_group(group, self.min_members, self.check_alive, self.skip_dead)
                if lead is None:
                    stats["with_site"] += 1
                    continue
                lead["query"] = f"{query} / {city}" if city else query
                leads.append(lead)
                if progress and len(leads) % 10 == 0:
                    progress(len(leads))
                if len(leads) >= want:
                    break
            if len(items) < batch:
                break
        return leads, stats


def enrich_with_vk(vk_finder, leads, stats):
    """Если у организации указана группа ВК — смотрим её: вдруг там есть сайт, почта или телефон."""
    keys = []
    for l in leads:
        k = l.get("screen_name") or (str(l["vk_group_id"]) if l.get("vk_group_id") else None)
        if k and k not in keys:
            keys.append(k)
    if not keys or not vk_finder or not vk_finder.can_check_groups:
        return leads
    try:
        found = vk_finder.get_groups(keys)
    except Exception:
        return leads
    groups = {}
    for g in found:
        groups[str(g["id"])] = g
        if g.get("screen_name"):
            groups[g["screen_name"].lower()] = g
    result = []
    for lead in leads:
        g = groups.get((lead.get("screen_name") or "").lower()) or groups.get(str(lead.get("vk_group_id") or ""))
        if g:
            vk_lead = analyze_group(g, 0, vk_finder.check_alive, getattr(vk_finder, "skip_dead", False))
            if vk_lead is None:
                stats["with_site"] = stats.get("with_site", 0) + 1
                continue
            lead["vk_group_id"] = g["id"]
            lead["screen_name"] = g.get("screen_name") or lead.get("screen_name")
            lead["email"] = lead.get("email") or vk_lead.get("email")
            lead["phone"] = lead.get("phone") or vk_lead.get("phone")
            lead["members"] = vk_lead.get("members", 0)
            if vk_lead.get("note"):
                lead["note"] = vk_lead["note"]
        result.append(lead)
    return result


__all__ = ["Finder", "enrich_with_vk", "parse_group_links", "analyze_group", "own_sites", "is_social_or_aggregator", "VKError"]
