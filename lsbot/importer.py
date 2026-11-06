"""Загрузка организаций из таблицы (CSV или Excel), например выписанных из 2ГИС или Яндекс Карт.

Колонки определяются по названиям (регистр и порядок не важны). Если заголовков нет
или они непонятные, бот сам ищет в строке почту, телефон, ссылки на ВК и сайт.
"""
import csv
import hashlib
import io
import re
import zipfile
import xml.etree.ElementTree as ET

from .finder import EMAIL_RE, PHONE_RE, extract_urls, is_social_or_aggregator, _domain

COLUMNS = {
    "name": ("название", "наименование", "организация", "компания", "name", "title", "имя"),
    "phone": ("телефон", "тел", "phone", "phones", "номер", "contacts", "контакты"),
    "email": ("почта", "email", "e-mail", "емейл", "имейл", "mail", "emails"),
    "social": ("instagram", "инстаграм", "telegram", "телеграм", "whatsapp", "viber", "соцсети", "соц. сети",
               "sociallinks", "social", "socials", "social links"),
    "site": ("сайт", "сайты", "website", "websites", "site", "web", "url", "urls", "links", "веб"),
    "vk": ("вк", "vk", "вконтакте", "vkontakte"),
    "address": ("адрес", "address", "улица"),
    "city": ("город", "city", "населенный пункт", "населённый пункт"),
    "activity": ("рубрика", "рубрики", "категория", "сфера", "вид деятельности", "category"),
    "description": ("описание", "description"),
    "rating": ("рейтинг", "rating"),
    "reviews": ("количество отзывов", "отзывов", "reviews", "review count"),
    "hours": ("часы работы", "режим работы", "workingtimetext", "working hours"),
}

SOURCES = {"2gis": "2ГИС", "yandex": "Яндекс Карты", "file": "таблица"}

TEMPLATE_HEADERS = ["Название", "Рубрика", "Город", "Адрес", "Телефон", "Почта", "Сайт", "ВКонтакте", "Соцсети"]


# ---------- чтение файлов ----------
def read_table(filename: str, data: bytes) -> list:
    """Возвращает список строк (каждая — список ячеек-строк)."""
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")) or data[:2] == b"PK":
        return _read_xlsx(data)
    if name.endswith(".xls"):
        raise ValueError("Старый формат .xls не поддерживается. В Excel: «Файл → Сохранить как → "
                         "Книга Excel (.xlsx)» или «CSV UTF-8».")
    return _read_csv(data)


def _read_csv(data: bytes) -> list:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = data.decode("utf-8", errors="replace")
    sample = text[:5000]
    delim = max(";,\t", key=sample.count) if any(c in sample for c in ";,\t") else ";"
    return [row for row in csv.reader(io.StringIO(text), delimiter=delim)]


_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def _read_xlsx(data: bytes) -> list:
    """Первый лист Excel-файла без сторонних библиотек."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("Не получилось открыть файл как таблицу Excel")
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root.findall("m:si", _NS):
            shared.append("".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t")))
    sheet = _first_sheet_path(z)
    root = ET.fromstring(z.read(sheet))
    rows = []
    for row in root.iter(f"{{{_NS['m']}}}row"):
        cells = {}
        for c in row.findall("m:c", _NS):
            ref = c.get("r", "")
            col = _col_index(re.sub(r"\d", "", ref)) if ref else len(cells)
            typ = c.get("t")
            v = c.find("m:v", _NS)
            if typ == "s" and v is not None:
                value = shared[int(v.text)]
            elif typ == "inlineStr":
                value = "".join(t.text or "" for t in c.iter(f"{{{_NS['m']}}}t"))
            else:
                value = v.text if v is not None else ""
                if value and re.fullmatch(r"\d+\.0", value):
                    value = value[:-2]
                elif value and re.fullmatch(r"\d\.\d+E\+\d+", value):  # телефон, сохранённый как число
                    value = str(int(float(value)))
            cells[col] = value or ""
        if cells:
            rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
    return rows


def _first_sheet_path(z) -> str:
    try:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rid = wb.find("m:sheets/m:sheet", _NS).get(_REL)
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        for rel in rels:
            if rel.get("Id") == rid:
                target = rel.get("Target").lstrip("/")
                return target if target.startswith("xl/") else "xl/" + target
    except Exception:
        pass
    sheets = sorted(n for n in z.namelist() if n.startswith("xl/worksheets/sheet"))
    if not sheets:
        raise ValueError("В файле Excel не нашлось листов с данными")
    return sheets[0]


def _col_index(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + ord(ch) - 64
    return n - 1


# ---------- разбор строк ----------
def _norm(h: str) -> str:
    return re.sub(r"\s+", " ", (h or "").strip().lower().replace("ё", "е").strip(":*"))


MULTI = ("phone", "email", "site", "vk", "social")   # могут быть «Телефон 1», «Телефон 2», …


def detect_columns(header: list) -> dict:
    found = {}
    for i, h in enumerate(header):
        h = _norm(h)
        if not h or h in ("2gis url", "url 2gis") or "индекс" in h:
            continue
        for key, names in COLUMNS.items():
            if key in found and key not in MULTI:
                continue
            if any(h == n or h.startswith(n + " ") or (len(n) > 3 and n in h) for n in names):
                if key in MULTI:
                    found.setdefault(key, []).append(i)
                else:
                    found[key] = i
                break
    return found


_DASHES = re.compile("[\u2010-\u2015\u2212\u00ad]")


def _clean(value) -> str:
    text = "" if value is None else str(value)
    return _DASHES.sub("-", text).replace("\u00a0", " ").replace("\u202f", " ").strip()


def _cell(row, idx):
    if isinstance(idx, list):
        return " ; ".join(v for v in (_cell(row, i) for i in idx) if v)
    return _clean(row[idx]) if idx is not None and idx < len(row) and row[idx] else ""


def _vk_screen(text: str):
    m = re.search(r"vk\.(?:com|ru)/([A-Za-z0-9_.]+)", text or "", re.IGNORECASE)
    if not m:
        return None
    name = m.group(1).strip(".").lower()
    name = re.sub(r"^(?:m\.)?vk\.(?:com|ru)\.", "", name)  # «vk.com/vk.com.salon» — ошибка в карточке
    club = re.fullmatch(r"(?:club|public)(\d+)", name)
    return club.group(1) if club else name


def parse_rows(rows: list, source: str = "file", default_city: str = None):
    """Превращает строки таблицы в организации. Возвращает (лиды, статистика)."""
    stats = {"rows": 0, "with_site": 0, "no_name": 0, "hidden": 0}
    rows = [r for r in rows if any((c or "").strip() for c in r)]
    if not rows:
        return [], stats
    cols = detect_columns(rows[0])
    if "name" in cols:
        body = rows[1:]
    else:
        cols = {"name": 0}  # заголовков нет: первая колонка — название, остальное ищем по содержимому
        body = rows
    leads = []
    for row in body:
        stats["rows"] += 1
        name = _cell(row, cols.get("name"))
        if not name:
            stats["no_name"] += 1
            continue
        if not re.search(r"[\wа-яё]", name.replace("_", "")) or (
                "тариф" in name.lower() and "http" in name.lower()):
            stats["hidden"] = stats.get("hidden", 0) + 1   # скрытые строки «*****» и служебные надписи
            continue
        whole = " ".join(_clean(c) for c in row if c)
        email_text = _cell(row, cols.get("email")) or whole
        emails = [e.lower().rstrip(".") for e in EMAIL_RE.findall(email_text)]
        phone_text = _cell(row, cols.get("phone")) or whole
        phones = PHONE_RE.findall(phone_text) or ([phone_text] if cols.get("phone") is not None
                                                   and len(re.sub(r"\D", "", phone_text)) >= 6 else [])
        vk = _vk_screen(_cell(row, cols.get("vk"))) or _vk_screen(whole)

        site_text = _cell(row, cols.get("site"))
        candidates = extract_urls(site_text) if site_text else extract_urls(whole.replace(name, " "))
        sites = [u for u in candidates if not is_social_or_aggregator(u)]
        socials = [u for u in candidates if is_social_or_aggregator(u)
                   and not _domain(u).endswith(("vk.com", "vk.ru"))]
        for idx in cols.get("social", []):
            if _cell(row, idx):
                socials.append(_cell(row, idx))

        uid_src = "|".join([name.lower(), (phones[0] if phones else ""), _cell(row, cols.get("address")).lower()])
        lead = {
            "uid": f"{source}:{hashlib.md5(uid_src.encode()).hexdigest()[:16]}",
            "source": source,
            "name": name,
            "screen_name": vk if vk and not vk.isdigit() else None,
            "vk_group_id": int(vk) if vk and vk.isdigit() else None,
            "city": _cell(row, cols.get("city")) or default_city,
            "address": _cell(row, cols.get("address")) or None,
            "activity": re.split(r"[,;]", _cell(row, cols.get("activity")))[0].strip() or None,
            "email": emails[0] if emails else None,
            "phone": phones[0].strip() if phones else None,
            "other_links": ", ".join(dict.fromkeys(socials))[:300] or None,
            "info": _info(row, cols),
            "query": f"файл: {SOURCES.get(source, source)}",
            "_sites": sites,
        }
        leads.append(lead)
    leads, stats["branches"] = merge_branches(leads)
    return leads, stats


# Слова, из которых состоят «безликие» названия: «Студия маникюра», «Салон красоты».
# Организации с такими названиями — разные мастера, их нельзя склеивать по имени.
GENERIC_WORDS = set("""студия студио салон салона кабинет кабинета мастер мастерская мастера
маникюр маникюра маникюрный педикюр педикюра ногтевая ногтевой ногтей ногти nail nails
красоты красота beauty бьюти парикмахерская парикмахер парикмахерские стрижка стрижки
брови бровей ресницы ресниц студия-салон и для на в эстетики эстетика спа spa массаж
массажа косметология косметолог косметологии барбершоп барбер ателье шиномонтаж автосервис
кафе кофейня ремонт центр студии школа
дом домик домики дома гостевой гостевые гостиница отель мини-отель хостел база отдыха отдых
коттедж коттеджи усадьба шале баня бани сауна баня-сауна глэмпинг кемпинг загородный
уютный уютная у озера реки леса лесу пруда""".split())


def _name_key(name: str) -> str:
    return re.sub(r"[^\wа-яё ]", " ", name.lower().replace("ё", "е")).split(",")[0].strip()


def is_generic(name: str) -> bool:
    words = re.findall(r"[\wа-яё-]+", name.lower().replace("ё", "е"))
    return not words or all(w in GENERIC_WORDS for w in words)


def _contacts(lead) -> set:
    """Чем можно опознать одну и ту же компанию: телефон, почта, группа ВК, домен сайта."""
    from .finder import _domain
    out = set()
    if lead.get("phone"):
        d = re.sub(r"\D", "", lead["phone"])[-10:]
        if len(d) >= 6:
            out.add("p:" + d)
    if lead.get("email"):
        out.add("e:" + lead["email"].lower())
    if lead.get("screen_name"):
        out.add("v:" + lead["screen_name"].lower())
    if lead.get("vk_group_id"):
        out.add("v:" + str(lead["vk_group_id"]))
    for u in lead.get("_sites") or []:
        out.add("s:" + _domain(u))
    return out


def merge_branches(leads: list) -> tuple:
    """Склеивает филиалы одной сети: если у любого филиала есть сайт — сайт есть у всей сети.
    Филиалами считаются строки с одинаковым названием и общим контактом (телефон, почта, ВК, сайт)
    или с одинаковым «фирменным» названием в одном городе. «Гостевой дом» в разных деревнях
    с разными телефонами — это разные хозяева, их не склеиваем.
    Возвращает (лиды, сколько строк-филиалов склеено)."""
    groups, order = {}, []
    for lead in leads:
        name = _name_key(lead["name"])
        contacts = _contacts(lead)
        key = None
        city = (lead.get("city") or "").strip().lower()
        for k in order:
            if k[0] != name:
                continue
            same_contact = bool(contacts & groups[k][1])
            same_city = bool(city) and city == k[2] and not is_generic(lead["name"])
            if same_contact or same_city:
                key = k
                break
        if key is None:
            key = (name, lead["uid"], city)
            groups[key] = ([], set())
            order.append(key)
        groups[key][0].append(lead)
        groups[key][1].update(contacts)
    groups = {k: v[0] for k, v in groups.items()}
    result, merged = [], 0
    for key in order:
        group = groups[key]
        main = dict(group[0])
        sites = [u for l in group for u in l.get("_sites", [])]
        main["_sites"] = list(dict.fromkeys(sites))
        for other in group[1:]:
            for field in ("email", "phone", "screen_name", "vk_group_id", "other_links", "activity"):
                if not main.get(field) and other.get(field):
                    main[field] = other[field]
        if len(group) > 1:
            merged += len(group) - 1
            main["note"] = f"сеть: {len(group)} филиала(ов) в файле"
        result.append(main)
    return result, merged


def _info(row, cols) -> str:
    """Короткая справка для карточки: описание, все рубрики, рейтинг, часы работы."""
    parts = []
    desc = _cell(row, cols.get("description"))
    if desc:
        parts.append(desc[:120])
    rubrics = _cell(row, cols.get("activity"))
    if ";" in rubrics or "," in rubrics:
        parts.append("Рубрики: " + rubrics[:160])
    rating = _cell(row, cols.get("rating"))
    m = re.search(r"\d+(?:[.,]\d+)?", rating)
    if m and m.group(0) not in ("0", "0.0"):
        text = f"Рейтинг {m.group(0).replace(',', '.')}"
        reviews = re.search(r"\d+", _cell(row, cols.get("reviews")) or rating.split("reviewCount")[-1] if "reviewCount" in rating else _cell(row, cols.get("reviews")))
        if reviews:
            text += f", отзывов {reviews.group(0)}"
        parts.append(text)
    hours = _cell(row, cols.get("hours"))
    if hours:
        parts.append("Часы: " + _short_hours(hours))
    return "\n".join(parts) or None


def _short_hours(text: str) -> str:
    """«Пн: 09:00-20:00; Вт: 09:00-20:00; …» → «Пн–Пт 09:00-20:00, Сб–Вс 10:00-18:00»."""
    days = re.findall(r"(Пн|Вт|Ср|Чт|Пт|Сб|Вс)\s*:\s*([^;]+)", text)
    if len(days) < 2:
        return text[:80]
    groups = []
    for day, time_ in days:
        time_ = re.sub(r"\s+", " ", time_.strip().rstrip(","))
        if groups and groups[-1][2] == time_:
            groups[-1][1] = day
        else:
            groups.append([day, day, time_])
    if len(groups) == 1 and len(days) == 7:
        return "ежедневно, " + groups[0][2]
    return ", ".join((f"{a}–{b}" if a != b else a) + " " + t for a, b, t in groups)[:120]


def make_template_csv() -> bytes:
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(TEMPLATE_HEADERS)
    w.writerow(["Салон «Пример»", "Салон красоты", "Челябинск", "ул. Ленина, 1", "+7 900 000-00-00",
                "salon@mail.ru", "", "vk.com/salon_primer", "instagram.com/salon"])
    return out.getvalue().encode("utf-8-sig")
