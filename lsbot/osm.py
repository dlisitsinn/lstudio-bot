"""Поиск организаций без сайта на OpenStreetMap (бесплатно, без ключей).

Данные © участники OpenStreetMap, лицензия ODbL (openstreetmap.org/copyright).
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

ENDPOINTS = (
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)

# Слово из запроса → теги OSM и человеческое название сферы
CATEGORIES = [
    (("салон красоты", "красот", "beauty"), [("shop", "beauty"), ("shop", "hairdresser")], "Салон красоты"),
    (("маникюр", "ногт", "nail"), [("shop", "beauty")], "Маникюр"),
    (("парикмах", "барбер", "стрижк"), [("shop", "hairdresser")], "Парикмахерская"),
    (("косметолог", "спа", "spa"), [("shop", "beauty"), ("leisure", "spa")], "Косметология"),
    (("массаж",), [("shop", "massage"), ("healthcare", "massage")], "Массаж"),
    (("тату", "tattoo"), [("shop", "tattoo")], "Тату-салон"),
    (("кафе", "кофейн", "coffee"), [("amenity", "cafe")], "Кафе"),
    (("ресторан",), [("amenity", "restaurant")], "Ресторан"),
    (("бар", "паб"), [("amenity", "bar"), ("amenity", "pub")], "Бар"),
    (("фастфуд", "шаурм", "бургер", "пицц"), [("amenity", "fast_food")], "Кафе быстрого питания"),
    (("пекарн", "выпечк"), [("shop", "bakery")], "Пекарня"),
    (("кондитер", "торт"), [("shop", "confectionery"), ("shop", "pastry")], "Кондитерская"),
    (("стомат", "зуб", "dental"), [("amenity", "dentist"), ("healthcare", "dentist")], "Стоматология"),
    (("клиник", "медцентр", "медицин"), [("amenity", "clinic"), ("healthcare", "clinic")], "Клиника"),
    (("ветеринар", "ветклиник"), [("amenity", "veterinary")], "Ветклиника"),
    (("груминг", "зоомагаз", "зоотовар"), [("shop", "pet"), ("shop", "pet_grooming")], "Зоотовары"),
    (("фитнес", "тренаж", "спортзал", "gym"), [("leisure", "fitness_centre"), ("leisure", "sports_centre")], "Фитнес"),
    (("йог", "танц", "студия танц"), [("leisure", "dance"), ("leisure", "fitness_centre")], "Студия"),
    (("автосервис", "сто", "ремонт авто"), [("shop", "car_repair")], "Автосервис"),
    (("шиномонтаж", "шины"), [("shop", "tyres")], "Шиномонтаж"),
    (("автомойк", "мойка", "детейлинг"), [("amenity", "car_wash")], "Автомойка"),
    (("запчаст", "автозапчаст"), [("shop", "car_parts")], "Автозапчасти"),
    (("цвет", "флорист", "букет"), [("shop", "florist")], "Цветы"),
    (("мебел",), [("shop", "furniture")], "Мебель"),
    (("одежд", "бутик"), [("shop", "clothes"), ("shop", "boutique")], "Одежда"),
    (("обув",), [("shop", "shoes")], "Обувь"),
    (("ювелир",), [("shop", "jewelry")], "Ювелирный"),
    (("оптик", "очки"), [("shop", "optician")], "Оптика"),
    (("фотограф", "фотостуд"), [("craft", "photographer"), ("shop", "photo")], "Фотостудия"),
    (("ремонт телефон", "ремонт техник"), [("shop", "mobile_phone"), ("craft", "electronics_repair")], "Ремонт техники"),
    (("ателье", "пошив", "швей"), [("craft", "tailor"), ("shop", "tailor")], "Ателье"),
    (("химчистк", "прачечн"), [("shop", "dry_cleaning"), ("shop", "laundry")], "Химчистка"),
    (("строител", "стройматериал"), [("shop", "doityourself"), ("shop", "hardware")], "Стройматериалы"),
    (("гостиниц", "отель", "хостел"), [("tourism", "hotel"), ("tourism", "hostel"), ("tourism", "guest_house")], "Гостиница"),
    (("школ", "курс", "репетитор", "обучен"), [("amenity", "language_school"), ("amenity", "school"), ("office", "educational_institution")], "Обучение"),
    (("юрист", "адвокат", "нотариус"), [("office", "lawyer"), ("office", "notary")], "Юристы"),
    (("бухгалтер",), [("office", "accountant")], "Бухгалтерия"),
    (("недвижим", "риелтор"), [("office", "estate_agent")], "Недвижимость"),
]

NAMED_KEYS = "^(shop|amenity|craft|office|leisure|healthcare|tourism)$"
SITE_TAGS = ("website", "contact:website", "url", "contact:url", "website:ru")
VK_TAGS = ("contact:vk", "vk", "contact:vkontakte")
SOCIAL_TAGS = {"contact:instagram": "Instagram", "contact:telegram": "Telegram",
               "contact:whatsapp": "WhatsApp", "contact:ok": "OK"}


def _q(text: str) -> str:
    return re.sub(r"[\"\\\\]", "", text).strip()


def _matches(word: str, q: str, tokens: list) -> bool:
    if " " in word:
        return word in q
    if len(word) <= 3:             # короткие слова («сто», «бар») — только целиком
        return word in tokens
    return any(t.startswith(word) for t in tokens)


def categories_for(query: str):
    q = query.lower().replace("ё", "е")
    tokens = re.findall(r"[\wа-я]+", q)
    tags, label = [], None
    for words, cat_tags, cat_label in CATEGORIES:
        if any(_matches(w, q, tokens) for w in words):
            tags += [t for t in cat_tags if t not in tags]
            label = label or cat_label
    return tags, label


def build_query(query: str, city: str, limit: int = 400) -> str:
    tags, _ = categories_for(query)
    city = _q(city)
    parts = [f'nwr["{k}"="{v}"]["name"](area.a);' for k, v in tags]
    words = [w for w in re.split(r"\s+", _q(query)) if len(w) >= 4][:2]
    for w in words:
        stem = w[:-2] if len(w) > 6 else w  # грубое отсечение окончаний: «маникюра» → «маникю»
        parts.append(f'nwr[~"{NAMED_KEYS}"~"."]["name"~"{_q(stem)}",i](area.a);')
    return (
        f'[out:json][timeout:90];\n'
        f'area["name"="{city}"]["boundary"="administrative"]->.a;\n'
        f'(\n  ' + "\n  ".join(parts) + f'\n);\nout center tags {limit};'
    )


def overpass(query: str, opener=None, endpoints=ENDPOINTS) -> list:
    opener = opener or urllib.request.urlopen
    data = urllib.parse.urlencode({"data": query}).encode()
    last = None
    for url in endpoints:
        req = urllib.request.Request(url, data=data, headers={"User-Agent": "LStudio-lead-finder/1.0"})
        for attempt in range(2):
            try:
                with opener(req, timeout=120) as resp:
                    return json.loads(resp.read().decode("utf-8")).get("elements", [])
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code in (429, 504):
                    time.sleep(5)
                    continue
                break
            except Exception as exc:  # сеть, таймаут, битый ответ — пробуем следующий сервер
                last = exc
                break
    raise ConnectionError(f"Карта OpenStreetMap сейчас не отвечает ({last}). Попробуйте позже.")


def _vk_screen(value: str):
    if not value:
        return None
    m = re.search(r"vk\.(?:com|ru)/([\w.]+)", value)
    name = m.group(1) if m else value.strip().lstrip("@")
    return name if re.fullmatch(r"[\w.]+", name) else None


def element_to_lead(el: dict, city: str, label: str = None):
    tags = el.get("tags", {})
    name = tags.get("name")
    if not name:
        return None
    if any(tags.get(t) for t in SITE_TAGS) or any(k.startswith("website") for k in tags):
        return None
    if tags.get("brand") or tags.get("brand:wikidata") or tags.get("operator:wikidata"):
        return None  # сети — у них свои сайты
    phone = (tags.get("phone") or tags.get("contact:phone") or "").split(";")[0].strip() or None
    email = (tags.get("email") or tags.get("contact:email") or "").split(";")[0].strip().lower() or None
    street = tags.get("addr:street")
    address = ", ".join(x for x in (street, tags.get("addr:housenumber")) if x) or None
    vk = next((_vk_screen(tags[t]) for t in VK_TAGS if tags.get(t)), None)
    socials = [f"{title}: {tags[k]}" for k, title in SOCIAL_TAGS.items() if tags.get(k)]
    return {
        "uid": f"osm:{el['type']}/{el['id']}",
        "source": "osm",
        "name": name.strip(),
        "screen_name": vk,
        "city": city,
        "address": address,
        "activity": label or _guess_label(tags),
        "email": email,
        "phone": phone,
        "other_links": ", ".join(socials) or None,
    }


def _guess_label(tags: dict):
    for _, cat_tags, label in CATEGORIES:
        if any(tags.get(k) == v for k, v in cat_tags):
            return label
    return None


class OSMFinder:
    def __init__(self, vk_finder=None, opener=None, endpoints=ENDPOINTS):
        self.vk_finder = vk_finder  # если есть токен ВК — проверяем группы организаций
        self.opener = opener
        self.endpoints = endpoints

    def search(self, query: str, city: str, want: int = 20, skip_uids=(), progress=None):
        if not city:
            raise ValueError("Для поиска по карте укажите город")
        _, label = categories_for(query)
        elements = overpass(build_query(query, city), self.opener, self.endpoints)
        stats = {"checked": len(elements), "with_site": 0, "skipped": 0}
        leads = []
        for el in elements:
            lead = element_to_lead(el, city, label)
            if lead is None:
                stats["with_site"] += 1
                continue
            if lead["uid"] in skip_uids:
                stats["skipped"] += 1
                continue
            lead["query"] = f"{query} / {city}"
            leads.append(lead)
        if self.vk_finder and self.vk_finder.can_check_groups:
            leads = self._enrich_with_vk(leads, stats)
        return leads[:want], stats

    def _enrich_with_vk(self, leads, stats):
        from .finder import enrich_with_vk
        return enrich_with_vk(self.vk_finder, leads, stats)
