"""Тесты на фейковых данных: VK и почта подменены, в интернет ничего не уходит."""
import io
import json
import sys
import smtplib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lsbot.config import Config, load_config
from lsbot.db import Store
from lsbot.drafts import Drafter, clean_name, match_example, load_portfolio
from lsbot.finder import Finder, analyze_group, own_sites, extract_urls
from lsbot.mailer import BatchSender, build_message
from lsbot.vk import VK, VKError, split_text
from lsbot.osm import OSMFinder, element_to_lead, build_query
from lsbot.web import App


def cfg(**kw):
    base = dict(
        vk_user_token="u",
        smtp_host="smtp.test", smtp_port=465, smtp_user="studio@test.ru", smtp_password="pw",
        sender_name="Анастасия, LStudio", daily_email_limit=3, email_delay_min=0, email_delay_max=0,
        min_members=10, default_city="Челябинск", portfolio_url="https://dlisitsinn.github.io/",
        studio_vk_url="https://vk.ru/lssttudio", db_path=Path(":memory:"), only_vk=False,
        skip_dead_sites=False, auto_queue=False,
    )
    base.update(kw)
    return Config(**base)


GROUPS = {
    1: {"id": 1, "name": "Салон красоты «Лаванда» | Челябинск", "screen_name": "lavanda74",
        "is_closed": 0, "members_count": 500, "activity": "Салон красоты", "city": {"title": "Челябинск"},
        "site": "", "description": "Запись: +7 (912) 345-67-89, instagram.com/lavanda, taplink.cc/lavanda",
        "contacts": [{"email": "Lavanda@mail.ru"}]},
    2: {"id": 2, "name": "Кофейня Бодрость", "screen_name": "bodrost", "is_closed": 0,
        "members_count": 300, "activity": "Кафе", "site": "https://bodrost-coffee.ru",
        "description": "", "contacts": []},
    3: {"id": 3, "name": "Барбершоп Борода", "screen_name": "boroda", "is_closed": 0,
        "members_count": 120, "activity": "Барбершоп", "site": "vk.com/boroda",
        "description": "Пишите в лс, почта boroda.shop@yandex.ru", "contacts": []},
    4: {"id": 4, "name": "Закрытый клуб", "screen_name": "secret", "is_closed": 1, "members_count": 900},
    5: {"id": 5, "name": "Маленькая группа", "screen_name": "tiny", "is_closed": 0, "members_count": 3},
    6: {"id": 6, "name": "Стоматология Улыбка", "screen_name": "smile", "is_closed": 0,
        "members_count": 800, "activity": "Стоматология", "site": "smile-dead-site.ru",
        "description": "", "contacts": []},
    7: {"id": 7, "name": "Мебель на заказ", "screen_name": "mebel", "is_closed": 0,
        "members_count": 200, "activity": "Мебель", "site": "",
        "description": "Наш сайт: mebel-chel.рф", "contacts": []},
}


class FakeVK:
    def __init__(self):
        self.calls = []

    def call(self, method, **p):
        self.calls.append((method, p))
        if method == "database.getCities":
            return {"count": 1, "items": [{"id": 158, "title": "Челябинск"}]} if p["q"] != "Нигде" else {"items": []}
        if method == "groups.search":
            if p.get("offset", 0) > 0:
                return {"count": 7, "items": []}
            return {"count": 7, "items": [{"id": i} for i in GROUPS]}
        if method == "account.getAppPermissions":
            return getattr(self, "perms", 4096 + 65536)
        if method == "messages.send":
            plan = getattr(self, "send_plan", [])
            outcome = plan.pop(0) if plan else "ok"
            if outcome != "ok":
                code, data = outcome
                raise VKError(code, "fake", method, data)
            self.sent = getattr(self, "sent", []) + [p]
            return 123
        if method == "groups.getById":
            by_name = {g["screen_name"]: g for g in GROUPS.values()}
            out = []
            for i in p["group_ids"]:
                g = GROUPS.get(i) or GROUPS.get(int(i) if str(i).isdigit() else -1) or by_name.get(i)
                if g:
                    out.append(g)
            return {"groups": out, "profiles": []}
        raise AssertionError(method)


ALIVE = {"bodrost-coffee.ru": True, "smile-dead-site.ru": False, "mebel-chel.рф": True}


def fake_alive(url):
    return ALIVE.get(url.replace("https://", "").replace("http://", "").strip("/"), True)


# ---------- поиск ----------
def test_site_detection():
    assert own_sites(GROUPS[1]) == []                         # соцсети и taplink — не сайт
    assert own_sites(GROUPS[2]) == ["https://bodrost-coffee.ru"]
    assert own_sites(GROUPS[3]) == []                         # ссылка на ВК — не сайт, e-mail — не сайт
    assert own_sites(GROUPS[7]) == ["mebel-chel.рф"]          # кириллический домен в описании


def test_extract_urls_ignores_email_and_files():
    urls = extract_urls("почта info@salon.ru, фото price.jpg, сайт salon.ru")
    assert urls == ["salon.ru"]


def test_analyze_group():
    lead = analyze_group(GROUPS[1], 10, fake_alive)
    assert lead["email"] == "lavanda@mail.ru"
    assert lead["phone"] == "+79123456789"
    assert "instagram.com/lavanda" in lead["other_links"]
    assert analyze_group(GROUPS[2], 10, fake_alive) is None   # есть рабочий сайт
    assert analyze_group(GROUPS[4], 10, fake_alive) is None   # закрытая
    dead = analyze_group(GROUPS[6], 10, fake_alive)
    assert dead and "не открывается" in dead["note"]          # мёртвый сайт = тоже клиент
    assert analyze_group(GROUPS[3], 10, fake_alive)["email"] == "boroda.shop@yandex.ru"


def test_finder_search():
    f = Finder(FakeVK(), min_members=10, check_alive=fake_alive)
    leads, stats = f.search("салон", "Челябинск", want=20)
    names = sorted(l["vk_group_id"] for l in leads)
    assert names == [1, 3, 6]
    assert stats["checked"] == 7 and stats["with_site"] == 2
    try:
        f.search("салон", "Нигде")
        assert False
    except ValueError:
        pass


def test_finder_respects_known_ids_and_limit():
    f = Finder(FakeVK(), min_members=10, check_alive=fake_alive)
    leads, _ = f.search("салон", None, want=20, skip_ids={1})
    assert 1 not in [l["vk_group_id"] for l in leads]
    leads, _ = f.search("салон", None, want=1)
    assert len(leads) == 1


# ---------- тексты ----------
def test_drafts():
    portfolio = load_portfolio()
    assert match_example("Барбершоп Борода Барбершоп", portfolio)["name"] == "Velvet"
    assert match_example("Кофейня Бодрость Кафе", portfolio)["name"] == "Пион"
    assert match_example("Стоматология Улыбка", portfolio)["name"] == "DENTAL"
    assert clean_name("✨ Салон красоты «Лаванда» | Челябинск") == "Салон красоты Лаванда"

    store = Store(":memory:")
    store.add_lead(analyze_group(GROUPS[1], 10, fake_alive) | {"query": "салон"})
    lead = store.by_status("new")[0]
    d = Drafter(cfg())
    subject, body = d.email(lead)
    assert subject == "Сделал черновик сайта для «Салон красоты Лаванда»"
    assert "dlisitsinn.github.io" in body and "Анастасия, LStudio" in body and "{" not in body
    assert "ответьте «нет»" in body
    vk = d.vk(lead)
    assert "Салон красоты Лаванда" in vk and "#" not in vk.splitlines()[0]


# ---------- база ----------
def test_store_dedupe_and_blacklist():
    s = Store(":memory:")
    lead = analyze_group(GROUPS[1], 10, fake_alive)
    assert s.add_lead(dict(lead)) is True
    assert s.add_lead(dict(lead)) is False                         # та же группа
    other = dict(lead, vk_group_id=999)
    assert s.add_lead(other) is False                              # та же почта у другой группы
    s.blacklist_add("someone@x.ru")
    assert s.add_lead(dict(lead, vk_group_id=1000, email="someone@x.ru")) is False
    s.blacklist_add("lavanda74")
    assert s.by_status("new") == [] and s.count_by_status()["skipped"] == 1


# ---------- почта ----------
class FakeSMTP:
    sent = []
    fail_for = set()
    bad_login = False

    def __init__(self, cfg):
        pass

    def login(self, u, p):
        if FakeSMTP.bad_login:
            raise smtplib.SMTPAuthenticationError(535, b"bad")

    def send_message(self, msg):
        if msg["To"] in FakeSMTP.fail_for:
            raise smtplib.SMTPRecipientsRefused({msg["To"]: (550, b"no user")})
        FakeSMTP.sent.append(msg)

    def quit(self):
        pass


def test_message_headers():
    m = build_message(cfg(), "a@b.ru", "Тема", "Текст")
    assert "LStudio" in m["From"] and m["List-Unsubscribe"].startswith("<mailto:studio@test.ru")


# ---------- мелочи ----------
def test_split():
    parts = split_text("a" * 5000 + "\nb", 3900)
    assert all(len(p) <= 3900 for p in parts) and "".join(parts).replace("\n", "") == "a" * 5000 + "b"


def test_vk_client_error_handling():
    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass

    answers = [
        {"error": {"error_code": 6, "error_msg": "Too many requests"}},
        {"response": {"ok": 1}},
        {"error": {"error_code": 5, "error_msg": "User authorization failed"}},
    ]
    def opener(url, data=None, timeout=None):
        return Resp(json.dumps(answers.pop(0)).encode())
    vk = VK("t", min_interval=0, opener=opener)
    assert vk.call("x") == {"ok": 1}            # повтор после «слишком часто»
    try:
        vk.call("y")
        assert False
    except VKError as e:
        assert e.code == 5


def test_env_parsing(tmp_path):
    env = tmp_path / ".env"
    env.write_text('SMTP_USER=me@gmail.com\nSMTP_PASSWORD="abcd efgh ijkl mnop"\n# коммент\nVK_USER_TOKEN= \n',
                   encoding="utf-8")
    c = load_config(env)
    assert c.smtp_host == "smtp.gmail.com" and c.smtp_password == "abcdefghijklmnop"
    assert c.vk_user_token == "" and c.email_ready() and c.port == 8765


# ---------- OpenStreetMap ----------
OSM_ELEMENTS = [
    {"type": "node", "id": 1, "tags": {"name": "Ноготок", "shop": "beauty", "phone": "+7 912 000-11-22",
                                         "addr:street": "улица Ленина", "addr:housenumber": "5",
                                         "contact:vk": "https://vk.com/lavanda74"}},
    {"type": "node", "id": 2, "tags": {"name": "С сайтом", "shop": "beauty", "website": "https://x.ru"}},
    {"type": "way", "id": 3, "tags": {"name": "Сеть красоты", "shop": "beauty", "brand": "Сеть"}},
    {"type": "node", "id": 4, "tags": {"shop": "beauty"}},
    {"type": "node", "id": 5, "tags": {"name": "Бьюти Лофт", "shop": "beauty", "email": "loft@mail.ru",
                                         "contact:instagram": "beautyloft"}},
    {"type": "node", "id": 6, "tags": {"name": "Студия Ч", "shop": "beauty", "contact:vk": "vk.com/bodrost"}},
]


class FakeOpener:
    def __init__(self, payloads):
        self.payloads, self.urls = list(payloads), []

    def __call__(self, req, timeout=None):
        self.urls.append(req.full_url)
        item = self.payloads.pop(0)
        if isinstance(item, Exception):
            raise item
        class R(io.BytesIO):
            def __enter__(s): return s
            def __exit__(s, *a): pass
        return R(json.dumps(item).encode())


def test_osm_query_and_parsing():
    q = build_query("маникюр", "Челябинск")
    assert 'area["name"="Челябинск"]' in q and '"shop"="beauty"' in q and 'маник' in q
    assert element_to_lead(OSM_ELEMENTS[1], "Ч") is None       # есть сайт
    assert element_to_lead(OSM_ELEMENTS[2], "Ч") is None       # сеть
    assert element_to_lead(OSM_ELEMENTS[3], "Ч") is None       # без названия
    lead = element_to_lead(OSM_ELEMENTS[0], "Челябинск", "Маникюр")
    assert lead["uid"] == "osm:node/1" and lead["screen_name"] == "lavanda74"
    assert lead["address"] == "улица Ленина, 5" and lead["activity"] == "Маникюр"


def test_osm_search_with_vk_enrichment_and_fallback_server():
    from urllib.error import URLError
    opener = FakeOpener([URLError("down"), {"elements": OSM_ELEMENTS}])
    vkf = Finder(FakeVK(), min_members=10, check_alive=fake_alive)
    f = OSMFinder(vk_finder=vkf, opener=opener, endpoints=("https://a/", "https://b/"))
    leads, stats = f.search("маникюр", "Челябинск", 20)
    assert opener.urls == ["https://a/", "https://b/"]           # первый сервер упал — взяли второй
    names = {l["name"]: l for l in leads}
    assert set(names) == {"Ноготок", "Бьюти Лофт"}               # «Студия Ч» отсеяна: у группы ВК есть сайт
    assert names["Ноготок"]["email"] == "lavanda@mail.ru"        # почта подтянута из группы ВК
    assert names["Ноготок"]["vk_group_id"] == 1
    assert stats["with_site"] == 4


def test_osm_without_vk_token_and_errors():
    f = OSMFinder(vk_finder=Finder(None), opener=FakeOpener([{"elements": OSM_ELEMENTS}]), endpoints=("https://a/",))
    leads, _ = f.search("маникюр", "Челябинск", 1)
    assert len(leads) == 1
    from urllib.error import URLError
    f = OSMFinder(opener=FakeOpener([URLError("x")]), endpoints=("https://a/",))
    try:
        f.search("маникюр", "Челябинск")
        assert False
    except ConnectionError as e:
        assert "не отвечает" in str(e)


# ---------- окно управления (логика) ----------
def make_app(vk=True, **kw):
    c = cfg(**kw)
    store = Store(":memory:")
    drafter = Drafter(c)
    vkf = Finder(FakeVK() if vk else None, min_members=10, check_alive=fake_alive)
    osm = OSMFinder(vk_finder=vkf, opener=FakeOpener([{"elements": OSM_ELEMENTS}]), endpoints=("https://a/",))
    app = App(c, store, vkf, osm, drafter,
              sender_factory=lambda n: BatchSender(c, store, drafter, n, smtp_factory=FakeSMTP, sleep=lambda s: None))
    return app, store


def test_app_full_flow():
    FakeSMTP.sent, FakeSMTP.fail_for, FakeSMTP.bad_login = [], {"boroda.shop@yandex.ru"}, False
    app, store = make_app()
    assert app.start_search("both", "салон красоты", "Челябинск", 20)["ok"]
    app.search_thread.join(5)
    st = app.state()
    names = sorted(l["name"] for l in st["leads"]["new"])
    # ВК: Лаванда, Борода, Улыбка; карта: Бьюти Лофт (Ноготок = та же группа lavanda74 → дубль отсеян)
    assert names == sorted(["Салон красоты «Лаванда» | Челябинск", "Барбершоп Борода",
                            "Стоматология Улыбка", "Бьюти Лофт"]), names
    assert any("ВКонтакте" in l["text"] for l in st["log"]) and any("карте" in l["text"] for l in st["log"])

    ids = [l["id"] for l in st["leads"]["new"]]
    osm_lead = next(l for l in st["leads"]["new"] if l["source"] == "osm")
    assert "на карте города" in app.lead(osm_lead["id"])["email_body"]
    vk_lead = next(l for l in st["leads"]["new"] if l["source"] == "vk")
    assert "во ВКонтакте" in app.lead(vk_lead["id"])["email_body"]
    detail = app.lead(ids[0])
    assert detail["email_subject"].startswith("Сделал черновик") and detail["vk_text"]
    assert app.action("approve", ids)["done"] == 4
    st = app.state()
    assert len(st["leads"]["approved"]) == 3 and len(st["leads"]["manual"]) == 1   # Улыбка без почты

    assert "3 писем" in app.send()["message"]
    app.sender.thread.join(5)
    assert len(FakeSMTP.sent) == 2 and store.count_by_status()["failed"] == 1
    assert app.state()["sent_today"] == 2

    manual = app.state()["leads"]["manual"][0]
    app.action("contacted", [manual["id"]])
    assert store.get(manual["id"])["status"] == "contacted"

    failed = store.by_status("failed")[0]
    app.action("restore", [failed["id"]])
    assert store.get(failed["id"])["status"] == "new"

    app.blacklist("vk.ru/boroda")
    assert store.get(failed["id"])["status"] == "skipped"


def test_app_without_vk_token():
    app, store = make_app(vk=False)
    assert "токен" in app.start_search("vk", "маникюр", "Челябинск", 5)["error"]
    assert app.start_search("both", "маникюр", "Челябинск", 5)["ok"]
    app.search_thread.join(5)
    log = " ".join(l["text"] for l in app.state()["log"])
    assert "только по карте" in log
    assert {l["source"] for l in app.state()["leads"]["new"]} == {"osm"}
    assert app.state()["settings"]["vk"] is False


def test_app_send_limits_and_errors():
    FakeSMTP.sent, FakeSMTP.fail_for, FakeSMTP.bad_login = [], set(), False
    app, store = make_app(daily_email_limit=1)
    for i in range(3):
        store.add_lead({"uid": f"t:{i}", "source": "vk", "name": f"Орг {i}", "email": f"o{i}@t.ru"})
    app.action("approve", [1, 2, 3])
    assert "1 писем" in app.send()["message"]
    app.sender.thread.join(5)
    assert "лимит" in app.send()["message"]

    FakeSMTP.bad_login = True
    app2, store2 = make_app()
    store2.add_lead({"uid": "t:1", "source": "vk", "name": "X", "email": "x@t.ru"})
    app2.action("approve", [1])
    app2.send()
    app2.sender.thread.join(5)
    assert "логин или пароль" in app2.state()["log"][0]["text"]
    assert store2.by_status("approved")
    FakeSMTP.bad_login = False

    app3, _ = make_app(smtp_user="")
    assert "не настроена" in app3.send()["error"]


def test_store_phone_dedupe():
    s = Store(":memory:")
    assert s.add_lead({"uid": "osm:node/1", "name": "A", "phone": "+7 (912) 000-11-22"})
    assert not s.add_lead({"uid": "vk:9", "vk_group_id": 9, "name": "A2", "phone": "89120001122"})
    s.blacklist_add("8 912 333 44 55")
    assert not s.add_lead({"uid": "osm:node/2", "name": "B", "phone": "+79123334455"})


def test_http_server_end_to_end():
    import threading, urllib.request
    app, store = make_app()
    from lsbot.web import serve
    server = serve(app, 0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{port}"
        html = urllib.request.urlopen(base + "/").read().decode()
        assert "LStudio" in html
        state = json.loads(urllib.request.urlopen(base + "/api/state").read())
        assert state["counts"] == {}
        req = urllib.request.Request(base + "/api/search", data=b'{"query":"x"}',
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req)
            assert False, "без заголовка X-LS запрос должен отклоняться"
        except urllib.error.HTTPError as e:
            assert e.code == 403
        store.add_lead({"uid": "t:1", "source": "vk", "name": "Тест", "email": "a@b.ru"})
        csv_text = urllib.request.urlopen(base + "/api/export.csv").read().decode("utf-8-sig")
        assert "Тест" in csv_text
    finally:
        server.shutdown()


# ---------- загрузка таблиц ----------
from lsbot.importer import read_table, parse_rows, detect_columns, make_template_csv

CSV_2GIS = (
    "Название;Рубрики;Адрес;Телефоны;Email;Сайт;ВКонтакте\n"
    "Студия маникюра «Ноготок»;Ногтевая студия, Салон красоты;ул. Ленина, 5;+7 (351) 222-33-44;nogotok@mail.ru;;\n"
    "Барбершоп Топор;Барбершоп;ул. Кирова, 1;+7 900 111-22-33;;https://topor-barber.ru;\n"
    "Цветы у дома;Цветы;пр. Победы, 3;8 912 444 55 66;;dead-flowers.ru;\n"
    "Лаванда;Салон красоты;ул. Труда, 7;;;taplink.cc/lavanda;https://vk.com/lavanda74\n"
    ";;;;;;\n"
    ";Без названия;;;;;\n"
)


def alive_stub(url):
    return "dead" not in url


def test_read_csv_encodings_and_columns():
    for enc in ("utf-8-sig", "cp1251"):
        rows = read_table("export.csv", CSV_2GIS.encode(enc))
        assert rows[0][0] == "Название" and rows[1][0].startswith("Студия маникюра")
    rows = read_table("x.csv", "name,phone,email\nКафе,+79001234567,a@b.ru\n".encode())
    assert detect_columns(rows[0]) == {"name": 0, "phone": [1], "email": [2]}
    try:
        read_table("old.xls", b"\xd0\xcf\x11\xe0")
        assert False
    except ValueError as e:
        assert ".xlsx" in str(e)


def test_parse_rows():
    leads, stats = parse_rows(read_table("e.csv", CSV_2GIS.encode()), "2gis", "Челябинск")
    by = {l["name"]: l for l in leads}
    assert stats["no_name"] == 1 and len(leads) == 4
    n = by["Студия маникюра «Ноготок»"]
    assert n["email"] == "nogotok@mail.ru" and n["phone"] == "+7 (351) 222-33-44"
    assert n["activity"] == "Ногтевая студия" and n["city"] == "Челябинск" and n["source"] == "2gis"
    assert by["Барбершоп Топор"]["_sites"] == ["https://topor-barber.ru"]
    assert by["Лаванда"]["_sites"] == [] and by["Лаванда"]["screen_name"] == "lavanda74"
    assert "taplink" in by["Лаванда"]["other_links"]


def test_parse_rows_without_headers():
    rows = [["Кафе Уют", "звоните +7 912 000 11 22", "uyut@yandex.ru vk.com/club777"]]
    leads, _ = parse_rows(rows, "file")
    assert leads[0]["email"] == "uyut@yandex.ru" and leads[0]["vk_group_id"] == 777
    assert leads[0]["phone"].replace(" ", "") == "+79120001122"


def test_read_xlsx_with_stdlib():
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Наименование", "Телефон", "E-mail", "Адрес сайта"])
    ws.append(["Автомойка Блеск", 79001112233, "blesk@mail.ru", None])
    ws.append(["Шиномонтаж", "+7 900 555-66-77", None, "shina.ru"])
    buf = io.BytesIO(); wb.save(buf)
    rows = read_table("book.xlsx", buf.getvalue())
    assert rows[0] == ["Наименование", "Телефон", "E-mail", "Адрес сайта"]
    assert rows[1][:3] == ["Автомойка Блеск", "79001112233", "blesk@mail.ru"]
    leads, _ = parse_rows(rows, "yandex")
    assert leads[0]["phone"] == "79001112233" and leads[1]["_sites"] == ["shina.ru"]


def make_import_app(vk=True):
    app, store = make_app(vk=vk)
    app.check_alive = alive_stub
    return app, store


def test_app_import_flow():
    app, store = make_import_app()
    res = app.import_file("export.csv", CSV_2GIS.encode("cp1251"), "2gis")
    assert res["ok"], res
    app.search_thread.join(5)
    names = {l["name"] for l in app.state()["leads"]["new"]}
    # Топор — живой сайт (отсеян); Цветы — сайт мёртвый (оставлен с пометкой);
    # Лаванда — в группе ВК указана почта → подтянута
    assert names == {"Студия маникюра «Ноготок»", "Цветы у дома", "Лаванда"}, names
    lav = next(l for l in app.state()["leads"]["new"] if l["name"] == "Лаванда")
    assert lav["email"] == "lavanda@mail.ru" and lav["vk_group_id"] == 1
    flowers = next(l for l in app.state()["leads"]["new"] if l["name"] == "Цветы у дома")
    assert "не открывается" in flowers["note"]
    log = app.state()["log"][0]["text"]
    assert "строк 5" in log and "новых без сайта 3 (с почтой 2)" in log
    body = app.lead(lav["id"])["email_body"]
    assert "«Лаванда» в 2ГИС" in body

    # повторная загрузка того же файла — ничего нового
    app.import_file("export.csv", CSV_2GIS.encode(), "2gis")
    app.search_thread.join(5)
    assert "новых без сайта 0" in app.state()["log"][0]["text"]
    assert "уже были в базе 3" in app.state()["log"][0]["text"]


def test_app_import_errors_and_no_vk():
    app, _ = make_import_app(vk=False)
    assert "не нашлось" in app.import_file("e.csv", b"", "2gis")["error"]
    assert ".xlsx" in app.import_file("e.xls", b"x", "2gis")["error"]
    app.import_file("e.csv", CSV_2GIS.encode(), "yandex")
    app.search_thread.join(5)
    lav = next(l for l in app.state()["leads"]["new"] if l["name"] == "Лаванда")
    assert lav["email"] is None and lav["source_name"] == "Яндекс Карты (файл)"


def test_http_import_and_template():
    import base64, threading, urllib.request
    app, store = make_import_app()
    from lsbot.web import serve
    server = serve(app, 0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{port}"
        tpl = urllib.request.urlopen(base + "/api/template.csv").read()
        assert tpl == make_template_csv()
        body = json.dumps({"filename": "t.csv", "source": "2gis",
                           "data": base64.b64encode(CSV_2GIS.encode()).decode()}).encode()
        req = urllib.request.Request(base + "/api/import", data=body,
                                     headers={"Content-Type": "application/json", "X-LS": "1"})
        assert json.loads(urllib.request.urlopen(req).read())["ok"]
        app.search_thread.join(5)
        assert store.count_by_status()["new"] == 3
    finally:
        server.shutdown()


def test_2gis_parser_format():
    """Формат выгрузки парсера 2ГИС: английские заголовки, многострочные ячейки, тире U+2012."""
    rows = [["title", "address", "rating", "contacts", "urls", "socialLinks", "emails", "workingTimeText"],
            ["Салон Елены", "Ленинградская улица, 126", "ratingValue = 4.8", "+7\u2012908\u2012137\u201263\u201280",
             "http://elena-salon.ru\nhttp://taplink.cc/elena", "https://vk.com/studio.lena\nhttps://ok.ru/x",
             "elena@mail.ru", "ежедневно"],
            ["Мягкий Квадрат, ногтевая студия", "Ленинский проспект, 108а", "", "+7\u2012915\u2012585\u201265\u201216",
             "http://n1946170.yclients.com/\nhttp://t.me/mkvadrat", "https://vk.com/mkvadrat", "", ""]]
    cols = detect_columns(rows[0])
    assert cols["name"] == 0 and cols["phone"] == [3] and cols["site"] == [4] and cols["email"] == [6]
    assert cols["social"] == [5]
    leads, _ = parse_rows(rows, "2gis")
    a, b = leads
    assert a["phone"] == "+7-908-137-63-80" and a["_sites"] == ["http://elena-salon.ru"]
    assert a["screen_name"] == "studio.lena" and a["email"] == "elena@mail.ru"
    assert b["_sites"] == [] and b["phone"] == "+7-915-585-65-16"          # yclients и t.me — не сайт
    assert "t.me/mkvadrat" in b["other_links"]


def test_masked_rows_and_vk_typo():
    rows = [["title", "contacts", "socialLinks"],
            ["Леди", "+7\u2012952\u2012951\u201231\u201274", "https://vk.com/vk.com.ledivrn36"],
            ["\u26a0\ufe0f На вашем тарифе лимит открытых контактов первые 15 строк, оплатите https://x.com/prices", "", ""],
            ["*******", "", "*****"], ["***", "", ""]]
    leads, stats = parse_rows(rows, "2gis")
    assert [l["name"] for l in leads] == ["Леди"] and stats["hidden"] == 3
    assert leads[0]["screen_name"] == "ledivrn36"
    app, _ = make_import_app()
    import openpyxl
    wb = openpyxl.Workbook(); ws = wb.active
    for r in rows: ws.append(r)
    buf = io.BytesIO(); wb.save(buf)
    assert app.import_file("x.xlsx", buf.getvalue(), "2gis")["ok"]
    app.search_thread.join(5)
    assert "3 строк в файле скрыты" in app.state()["log"][0]["text"]


def test_parser2gis_interlark_format():
    """Таблица программы Parser2GIS (github.com/interlark/parser-2gis)."""
    header = ["Наименование", "Описание", "Рубрики", "Адрес", "Комментарий к адресу", "Почтовый индекс",
              "Город", "Телефон 1", "Телефон 2", "E-mail 1", "Веб-сайт 1", "Веб-сайт 2", "ВКонтакте 1",
              "Instagram 1", "WhatsApp 1", "Telegram 1", "Часы работы", "Рейтинг", "Широта", "Долгота",
              "2GIS URL", "Тип"]
    def row(**kw):
        r = [""] * len(header)
        for k, v in kw.items():
            r[header.index(k)] = v
        return r
    rows = [header,
            row(**{"Наименование": "Ателье Иголка", "Рубрики": "Ателье по пошиву одежды; Ремонт одежды",
                   "Адрес": "ул. Ленина, 5", "Почтовый индекс": "454000", "Город": "Челябинск",
                   "Телефон 1": "+7 (351) 222‒33‒44 (Администратор)", "E-mail 1": "igolka74@mail.ru (для заказов)",
                   "ВКонтакте 1": "https://vk.com/igolka74", "Широта": "55.16", "Долгота": "61.40",
                   "2GIS URL": "https://2gis.ru/chelyabinsk/firm/70000001", "Тип": "branch"}),
            row(**{"Наименование": "Шиномонтаж 24", "Город": "Челябинск", "Телефон 1": "8 900 111 22 33",
                   "Веб-сайт 1": "https://dikidi.net/123", "Веб-сайт 2": "https://shina24.ru",
                   "2GIS URL": "https://2gis.ru/chelyabinsk/firm/70000002"})]
    cols = detect_columns(header)
    assert cols["phone"] == [7, 8] and cols["site"] == [10, 11] and cols["city"] == 6
    assert "activity" in cols and cols["activity"] == 2
    leads, _ = parse_rows(rows, "2gis")
    a, b = leads
    assert a["activity"] == "Ателье по пошиву одежды" and a["email"] == "igolka74@mail.ru"
    assert a["phone"] == "+7 (351) 222-33-44" and a["screen_name"] == "igolka74" and a["_sites"] == []
    assert a["city"] == "Челябинск" and a["address"] == "ул. Ленина, 5"
    assert b["_sites"] == ["https://shina24.ru"]   # сайт во второй колонке тоже найден


def test_branches_and_generic_names():
    from lsbot.importer import merge_branches, is_generic
    assert is_generic("Студия маникюра") and is_generic("студия маникюра и педикюра")
    assert not is_generic("Kaif beauty") and not is_generic("Ли-Лак, дом маникюра")
    header = ["Наименование", "Телефон 1", "E-mail 1", "Веб-сайт 1", "Веб-сайт 2"]
    rows = [header,
            ["Kaif beauty", "89056571313", "", "http://kaifbeauty.ru/", ""],
            ["Kaif beauty", "89056570000", "izabel@mail.ru", "", ""],          # филиал без сайта
            ["Студия маникюра", "89000000001", "a@mail.ru", "", ""],
            ["Студия маникюра", "89000000002", "b@mail.ru", "", ""],           # другой мастер
            ["Ли-Лак", "89518533311", "", "", ""],
            ["Ли-Лак", "", "lilak@yandex.ru", "", ""],                          # второй филиал с почтой
            ["Мастер Оля", "89000000003", "", "https://mst.link/olya", "https://n1.yclients.ru/"]]
    leads, stats = parse_rows(rows, "2gis")
    by = {}
    for l in leads:
        by.setdefault(l["name"], []).append(l)
    assert by["Kaif beauty"][0]["_sites"] == ["http://kaifbeauty.ru/"] and len(by["Kaif beauty"]) == 1
    assert len(by["Студия маникюра"]) == 2
    assert len(by["Ли-Лак"]) == 1 and by["Ли-Лак"][0]["email"] == "lilak@yandex.ru"
    assert by["Мастер Оля"][0]["_sites"] == []                              # mst.link и yclients — не сайт
    assert stats["branches"] == 2


def test_parallel_site_checks():
    import time as _t
    app, store = make_import_app()
    def slow_alive(url):
        _t.sleep(0.2)
        return "dead" not in url
    app.check_alive = slow_alive
    header = "Наименование;Сайт\n"
    body = "".join(f"Орг {i};site{i}.ru\n" for i in range(24)) + "Без сайта;\nМёртвый;dead.ru\n"
    start = _t.monotonic()
    app.import_file("x.csv", (header + body).encode(), "2gis")
    app.search_thread.join(10)
    assert _t.monotonic() - start < 2.0          # 26 проверок по 0.2 с в 12 потоков
    assert {l["name"] for l in app.state()["leads"]["new"]} == {"Без сайта", "Мёртвый"}


# ---------- отправка во ВК одной кнопкой ----------
def vk_app(limit=20, perms=4096):
    app, store = make_import_app(vk=True)
    app.cfg.vk_daily_limit = limit
    app.vk_finder.vk.perms = perms
    app._vk_last_send = -100
    app._sleep = lambda s: None   # без пауз в тестах
    ids = []
    for i, (name, gid, screen) in enumerate([("Ногти", 11, None), ("Брови", None, "lavanda74"), ("Ресницы", 13, None)]):
        store.add_lead({"uid": f"t:{i}", "source": "2gis", "name": name, "vk_group_id": gid, "screen_name": screen,
                        "info": "Рейтинг 4.9"})
        ids.append(store.by_status("new")[-1]["id"])
    app.action("approve", ids)
    return app, store, ids


def test_send_vk_success_and_limit():
    app, store, ids = vk_app(limit=2)
    st = app.state()
    assert len(st["manual_queue"]) == 3 and st["manual_queue"][0]["vk_text"] and st["manual_queue"][0]["info"]
    r = app.send_vk(ids[0], "Привет, Ногти!")
    assert r == {"ok": True, "sent_today": 1}
    sent = app.vk_finder.vk.sent[-1]
    assert sent["peer_id"] == -11 and sent["message"] == "Привет, Ногти!"
    assert store.get(ids[0])["status"] == "contacted"
    assert app.send_vk(ids[0], "ещё раз")["error"].startswith("Эта организация уже")   # повторно нельзя
    assert app.send_vk(ids[1], "Привет")["ok"]                                          # id нашёлся по адресу
    assert app.vk_finder.vk.sent[-1]["peer_id"] == -1 and store.get(ids[1])["vk_group_id"] == 1
    assert "лимит" in app.send_vk(ids[2], "Привет")["error"]
    assert store.sent_today() == 0 and store.vk_sent_today() == 2                      # письма не смешиваются


def test_send_vk_captcha_closed_flood_and_permissions():
    app, store, ids = vk_app()
    vk = app.vk_finder.vk
    vk.send_plan = [(14, {"captcha_sid": "777", "captcha_img": "https://api.vk.com/captcha.php?sid=777"}), "ok"]
    r = app.send_vk(ids[0], "текст")
    assert r["captcha"]["sid"] == "777" and store.get(ids[0])["status"] == "manual"
    r = app.send_vk(ids[0], "текст", "777", "abcd")
    assert r["ok"] and vk.sent[-1]["captcha_key"] == "abcd"

    vk.send_plan = [(902, {})]
    r = app.send_vk(ids[2], "текст")
    assert r["closed"] and "закрыты" in store.get(ids[2])["note"] and store.get(ids[2])["status"] == "manual"

    vk.send_plan = [(9, {})]
    assert "перерыв" in app.send_vk(ids[1], "текст")["error"]
    assert store.vk_sent_today() == 1

    app2, _, ids2 = vk_app(perms=65536)     # токен без права messages
    assert "нет доступа к сообщениям" in app2.send_vk(ids2[0], "текст")["error"]
    assert app2.state()["settings"]["vk_messages"] is False

    app3, _, ids3 = vk_app()
    app3.vk_finder.vk = None
    assert "токен" in app3.send_vk(ids3[0], "текст")["error"]
    assert "пустой" in app.send_vk(ids[1], "   ")["error"]


def test_db_migration_adds_info_column(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite3"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE leads (id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT UNIQUE NOT NULL, source TEXT NOT NULL, "
                "name TEXT NOT NULL, vk_group_id INTEGER, screen_name TEXT, city TEXT, address TEXT, activity TEXT, "
                "members INTEGER DEFAULT 0, email TEXT, phone TEXT, other_links TEXT, query TEXT, "
                "status TEXT NOT NULL DEFAULT 'new', note TEXT, found_at TEXT NOT NULL, contacted_at TEXT)")
    con.commit(); con.close()
    store = Store(path)
    assert store.add_lead({"uid": "x:1", "source": "file", "name": "A", "info": "Рейтинг 5"})
    assert store.by_status("new")[0]["info"] == "Рейтинг 5"


def test_short_hours():
    from lsbot.importer import _short_hours
    week = "; ".join(f"{d}: 09:00-20:00" for d in ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"])
    assert _short_hours(week) == "ежедневно, 09:00-20:00"
    mixed = "Пн: 10:00-20:00; Вт: 10:00-20:00; Ср: 10:00-20:00; Чт: 10:00-20:00; Пт: 10:00-20:00; Сб: 11:00-18:00; Вс: выходной"
    assert _short_hours(mixed) == "Пн–Пт 10:00-20:00, Сб 11:00-18:00, Вс выходной"
    assert _short_hours("ежедневно, 09:00–20:00") == "ежедневно, 09:00–20:00"


def test_vk_token_cleanup():
    from lsbot.config import _vk_token
    t = "vk1.a.ABC-def_123"
    assert _vk_token(t) == t
    assert _vk_token(f"https://oauth.vk.com/blank.html#access_token={t}&expires_in=0&user_id=1") == t
    assert _vk_token(f"access_token={t}&expires_in=0") == t
    assert _vk_token(f' "{t}" ') == t and _vk_token("") == ""
    assert _vk_token("\u200b" + t + "\u00a0") == t and _vk_token("«" + t + "»") == t


# ---------- самообновление ----------
def _repo_zip(files: dict, root="owner-lstudio-bot-abc123/") -> bytes:
    import zipfile as _z
    buf = io.BytesIO()
    with _z.ZipFile(buf, "w") as z:
        z.writestr(root, "")
        for name, text in files.items():
            z.writestr(root + name, text)
    return buf.getvalue()


class FakeGitHub:
    def __init__(self, sha, zip_bytes):
        self.sha, self.zip, self.calls = sha, zip_bytes, []

    def __call__(self, req, timeout=None):
        self.calls.append(req.full_url)
        body = self.sha.encode() if "/commits/" in req.full_url else self.zip
        class R(io.BytesIO):
            def __enter__(s): return s
            def __exit__(s, *a): pass
        return R(body)


def test_updater_updates_code_and_keeps_user_data(tmp_path):
    import updater
    base = tmp_path
    (base / ".env").write_text("SMTP_PASSWORD=secret\n", encoding="utf-8")
    (base / "data").mkdir()
    (base / "data" / "leads.sqlite3").write_bytes(b"DB")
    (base / "templates").mkdir()
    (base / "templates" / "vk.txt").write_text("мой исправленный текст", encoding="utf-8")
    (base / "main.py").write_text("old", encoding="utf-8")
    z = _repo_zip({"main.py": "new", "lsbot/web.py": "web", "templates/vk.txt": "стандартный",
                   ".env": "SMTP_PASSWORD=HACKED", "data/leads.sqlite3": "EMPTY", "../evil.py": "x"})
    gh = FakeGitHub("sha1", z)
    res = updater.update(base, gh, {"repo": "owner/lstudio-bot", "branch": "main", "token": ""})
    assert res.startswith("updated")
    assert (base / "main.py").read_text() == "new" and (base / "lsbot" / "web.py").read_text() == "web"
    assert (base / ".env").read_text() == "SMTP_PASSWORD=secret\n"           # настройки не тронуты
    assert (base / "data" / "leads.sqlite3").read_bytes() == b"DB"            # база не тронута
    assert (base / "data" / "templates" / "vk.txt").read_text(encoding="utf-8") == "мой исправленный текст"
    assert not (tmp_path.parent / "evil.py").exists()
    assert (base / "data" / ".version").read_text() == "sha1"
    # повторный запуск — уже последняя версия, архив не качается
    gh2 = FakeGitHub("sha1", z)
    assert updater.update(base, gh2, {"repo": "owner/lstudio-bot", "branch": "main"}) == "latest"
    assert len(gh2.calls) == 1
    # без репозитория — ничего не делаем
    assert updater.update(base, gh2, {"repo": ""}) == "skip"


def test_user_templates_override(tmp_path, monkeypatch):
    import lsbot.drafts as drafts
    monkeypatch.setattr(drafts, "USER_TEMPLATES_DIR", tmp_path)
    (tmp_path / "vk.txt").write_text("Привет, {name}!", encoding="utf-8")
    d = Drafter(cfg())
    store = Store(":memory:")
    store.add_lead({"uid": "t:1", "source": "2gis", "name": "Ноготок"})
    lead = store.by_status("new")[0]
    assert d.vk(lead).strip() == "Привет, Ноготок!"
    assert "Здравствуйте" in d.email(lead)[1]          # письмо — стандартное


# ---------- режим «только ВК» ----------
def test_only_vk_import_and_approve():
    app, store = make_import_app()
    app.cfg.only_vk = True
    app.import_file("export.csv", CSV_2GIS.encode(), "2gis")
    app.search_thread.join(5)
    names = {l["name"] for l in app.state()["leads"]["new"]}
    # Лаванда — группа ВК; Ноготок — без ВК, но с почтой (остаётся для писем); Цветы — ни ВК, ни почты
    assert names == {"Лаванда", "Студия маникюра «Ноготок»"}, names
    assert "без ВК и почты пропущено" in app.state()["log"][0]["text"]
    lav = next(l for l in app.state()["leads"]["new"] if l["name"] == "Лаванда")
    app.action("approve", [lav["id"]])
    assert store.get(lav["id"])["status"] == "manual"            # есть ВК — во вкладку ВК, не в письма


def test_auto_queue_routes_vk_and_email():
    app, store = make_import_app()
    app.cfg.only_vk = True
    app.cfg.auto_queue = True
    app.import_file("export.csv", CSV_2GIS.encode(), "2gis")
    app.search_thread.join(5)
    st = app.state()
    assert st["leads"]["new"] == []
    assert [l["name"] for l in st["leads"]["manual"]] == ["Лаванда"]
    assert [l["name"] for l in st["leads"]["approved"]] == ["Студия маникюра «Ноготок»"]
    assert "во ВК 1, в письма 1" in st["log"][0]["text"]


def test_vk_opened_marks_contacted_and_undo():
    app, store, ids = vk_app(limit=2)
    r = app.vk_opened(ids[0])
    assert r == {"ok": True, "sent_today": 1}
    assert store.get(ids[0])["status"] == "contacted"
    assert [l["id"] for l in app.state()["manual_queue"]] == ids[1:]   # следующая карточка сразу на месте
    assert "уже обработана" in app.vk_opened(ids[0])["error"]
    app.action("undo_contacted", [ids[0]])                              # «Отменить»
    assert store.get(ids[0])["status"] == "manual" and store.vk_sent_today() == 0
    app.vk_opened(ids[0]); app.vk_opened(ids[1])
    assert "лимит" in app.vk_opened(ids[2])["error"]


def test_only_vk_hides_old_leads_without_vk():
    c = cfg(only_vk=True, auto_queue=True)
    store = Store(":memory:")
    store.add_lead({"uid": "a", "source": "2gis", "name": "С ВК", "screen_name": "club_a"})
    store.add_lead({"uid": "b", "source": "2gis", "name": "Без ВК", "phone": "+79000000000"})
    store.add_lead({"uid": "c", "source": "2gis", "name": "Только почта", "email": "c@mail.ru"})
    store.set_status(3, "skipped", "нет группы ВК")             # раньше убрали за отсутствие ВК
    d = Drafter(c)
    app = App(c, store, Finder(None), OSMFinder(), d, sender_factory=lambda n: BatchSender(c, store, d, n))
    assert [l["name"] for l in app.state()["leads"]["new"]] == ["С ВК"]
    assert store.count_by_status()["skipped"] == 1
    assert [l["name"] for l in app.state()["leads"]["approved"]] == ["Только почта"]   # вернулась в письма


# ---------- отправка отмеченных карточек ----------
class FakeVKB:
    def __init__(self, plan=None):
        self.state, self.plan, self.sent = "ready", list(plan or []), []

    def status(self):
        return {"state": self.state, "message": ""}

    def send(self, target, text, marker=""):
        outcome = self.plan.pop(0) if self.plan else "ok"
        if outcome == "already":
            return {"ok": True, "already": True}
        if outcome == "lost":
            return {"error": "Окно ВК не готово: Окно ВК закрыто"}
        if outcome == "ok":
            self.sent.append((str(target), text))
            return {"ok": True}
        if outcome == "closed":
            return {"error": "закрыты", "closed": True}
        if outcome == "captcha":
            return {"error": "ВК показал капчу — решите её"}
        return {"error": "что-то не так"}

    def open(self):
        pass


def batch_app(n=12, plan=None, limit=100):
    c = cfg(only_vk=True, vk_daily_limit=limit)
    store = Store(":memory:")
    d = Drafter(c)
    app = App(c, store, Finder(None), OSMFinder(), d, sender_factory=lambda x: BatchSender(c, store, d, x),
              vkb=FakeVKB(plan))
    app._sleep = lambda s: None
    app.batch_pause = (0, 0)
    for i in range(n):
        store.add_lead({"uid": f"b:{i}", "source": "2gis", "name": f"Орг {i}", "vk_group_id": 100 + i})
    ids = [r["id"] for r in store.by_status("new")]
    app.action("approve", ids)
    return app, store, ids


def _wait_batch(app, timeout=5):
    import time as _t
    end = _t.monotonic() + timeout
    while app.batch["running"] and _t.monotonic() < end:
        _t.sleep(0.02)


def test_batch_sends_max_and_skips_closed_quietly():
    m = App.BATCH_MAX
    app, store, ids = batch_app(n=m + 2, plan=["ok", "closed", "ok"], limit=m + 10)
    r = app.start_vk_batch(ids, {str(ids[0]): "Свой текст"})
    assert r == {"ok": True, "total": m}                                # больше BATCH_MAX за раз нельзя
    _wait_batch(app)
    b = app.state()["batch"]
    assert b["sent"] == m - 1 and b["closed"] == 1 and b["errors"] == 0 and not b["running"]
    assert app.vkb.sent[0] == ("100", "Свой текст")                     # правка текста сохранилась
    assert store.get(ids[1])["status"] == "skipped" and "закрыты" in store.get(ids[1])["note"]
    assert store.get(ids[m])["status"] == "manual"                      # следующая за пределом не тронута
    assert "у 1 закрыты сообщения" in app.state()["log"][0]["text"]


def test_batch_captcha_pause_resume_stop_and_limit():
    app, store, ids = batch_app(plan=["captcha", "ok"])
    app.start_vk_batch(ids[:3])
    import time as _t
    for _ in range(100):
        if app.batch["captcha"]:
            break
        _t.sleep(0.02)
    assert app.state()["batch"]["captcha"] and app.batch["sent"] == 0  # ждёт капчу
    app.resume_vk_batch()
    _wait_batch(app)
    assert app.batch["sent"] == 3

    app2, store2, ids2 = batch_app(limit=2)
    app2.start_vk_batch(ids2)
    _wait_batch(app2)
    assert app2.batch["total"] == 2 and store2.vk_sent_today() == 2     # дневной лимит
    assert "лимит" in app2.start_vk_batch(ids2[2:])["error"]

    app3, _, ids3 = batch_app()
    app3.batch_pause = (0.3, 0.3)
    app3.start_vk_batch(ids3)
    _t.sleep(0.1); app3.stop_vk_batch(); _wait_batch(app3)
    assert app3.batch["sent"] < 10 and "остановлено" in app3.state()["log"][0]["text"]
    app3.vkb.state = "login"
    assert "войдите" in app3.start_vk_batch(ids3)["error"]


def test_batch_stops_when_window_closed_and_keeps_rest():
    app, store, ids = batch_app(n=10, plan=["ok", "ok", "lost", "lost", "lost"])
    app.start_vk_batch(ids)
    _wait_batch(app)
    b = app.state()["batch"]
    assert b["sent"] == 2 and b["errors"] == 1 and b["done"] == 3        # после закрытия окна сразу стоп
    assert "окно ВК закрылось" in app.state()["log"][0]["text"]
    left = [r for r in store.by_status("manual")]
    assert len(left) == 8                                                # неотправленные остались в очереди
    assert store.vk_sent_today() == 2
    assert store.restore_window_failures() == 1                          # при запуске заметка снимается
    assert all(not r["note"] for r in store.by_status("manual"))


def test_restore_closed_skips(tmp_path):
    from lsbot import migrations
    app, store, ids = batch_app(n=4, plan=["ok", "closed", "closed", "closed"])
    app.start_vk_batch(ids); _wait_batch(app)
    assert store.count_by_status().get("skipped") == 3
    store.set_status(ids[0], "skipped", "сообщения сообщества закрыты")   # этой уже писали — не возвращаем
    assert store.restore_closed_skips() == 3
    assert store.get(ids[0])["status"] == "skipped" and store.get(ids[1])["status"] == "manual"
    assert migrations.once(tmp_path, "x") and not migrations.once(tmp_path, "x")


def test_niche_only_filters_queue_and_batch():
    from lsbot.web import niche_match
    words = ("бан", "саун", "баз отдых", "гостев", "коттедж")
    row = lambda n, a="": {"name": n, "activity": a, "query": "", "info": ""}
    assert niche_match(row("Баня на дровах"), words) and niche_match(row("Тихое место", "Базы отдыха"), words)
    assert niche_match(row("Гостевой дом Лесной"), words)
    assert not niche_match(row("Банкетный зал Пион"), words)          # «банкет» — не баня
    assert not niche_match(row("Студия маникюра"), words) and not niche_match(row("Двери Мира"), words)
    assert not niche_match(row("Сбербанк"), words)
    app, store, ids = batch_app(n=3)
    store.add_lead({"uid": "r:1", "source": "2gis", "name": "Баня у озера", "vk_group_id": 900})
    app.action("approve", [store.by_status("new")[0]["id"]])
    app.cfg.niche_only = words
    st = app.state()
    assert [l["name"] for l in st["manual_queue"]] == ["Баня у озера"] and st["manual_niche_total"] == 1
    app.start_vk_batch([r["id"] for r in store.by_status("manual")]); _wait_batch(app)
    assert [t for t, _ in app.vkb.sent] == ["900"]                     # маникюр и прочее не тронуты
    assert store.count_by_status()["manual"] == 3


def test_never_writes_twice_to_same_group():
    app, store, ids = batch_app(n=3)
    app.start_vk_batch(ids[:1]); _wait_batch(app)
    assert len(app.vkb.sent) == 1
    assert not store.add_lead({"uid": "dup:1", "source": "2gis", "name": "Орг 0 копия", "vk_group_id": 100})
    # карточку вернули в очередь вручную, а сообщение этой группе уже ушло — журнал не даст написать снова
    store.set_status(ids[0], "manual")
    dup = ids[0]
    r = app.send_vk_browser(dup, "текст")
    assert r["ok"] and r["already"] and len(app.vkb.sent) == 1           # повторно не отправлено
    assert store.get(dup)["status"] == "contacted" and store.vk_sent_today() == 1
    # в диалоге уже есть наше сообщение (журнала нет — например, окно закрылось на отправке)
    app.vkb.plan = ["already"]
    app.start_vk_batch(ids[1:2]); _wait_batch(app)
    assert app.batch["already"] == 1 and app.batch["sent"] == 0 and len(app.vkb.sent) == 1
    assert store.get(ids[1])["status"] == "contacted" and "уже писали" in store.get(ids[1])["note"]
    assert "уже получали" in app.state()["log"][0]["text"]


def test_settings_migration(tmp_path):
    from lsbot import migrations
    (tmp_path / ".env").write_text("SMTP_USER=a@b.ru\nVK_DAILY_LIMIT=20\n", encoding="utf-8")
    got = migrations.apply(tmp_path)
    assert got[:4] == ["VK_DAILY_LIMIT=2000", "SKIP_DEAD_SITES=да", "VK_GROUP_TOKEN=", "AUTO_QUEUE=да"]
    assert got[4:] == ["NICHE_ONLY="]          # фильтр ниш в итоге выключен
    assert "VK_DAILY_LIMIT=2000" in (tmp_path / ".env").read_text(encoding="utf-8")
    assert "SMTP_USER=a@b.ru" in (tmp_path / ".env").read_text(encoding="utf-8")
    (tmp_path / ".env").write_text("VK_DAILY_LIMIT=50\n", encoding="utf-8")   # потом поменяли сами
    assert migrations.apply(tmp_path) == []                                   # второй раз не трогаем
    assert "VK_DAILY_LIMIT=50" in (tmp_path / ".env").read_text(encoding="utf-8")


def test_skip_dead_sites_everywhere():
    # в группе указан сайт, который не открылся, — при skip_dead организацию не берём
    assert analyze_group(GROUPS[6], 10, fake_alive, skip_dead=True) is None
    assert analyze_group(GROUPS[1], 10, fake_alive, skip_dead=True) is not None   # сайта нет — клиент


def test_import_skips_listed_sites():
    app, store = make_app(skip_dead_sites=True)
    app.check_alive = alive_stub
    app.import_file("export.csv", CSV_2GIS.encode("cp1251"), "2gis")
    app.search_thread.join(5)
    names = {l["name"] for l in app.state()["leads"]["new"]}
    assert "Цветы у дома" not in names and "Лаванда" in names     # сайт указан (хоть и мёртвый) — не пишем


def test_skip_dead_moves_queue_and_import(tmp_path):
    app, store = make_app(skip_dead_sites=True)
    lead = analyze_group(GROUPS[6], 10, fake_alive)
    store.add_lead(lead)
    store.add_lead(analyze_group(GROUPS[1], 10, fake_alive))
    assert app.skip_dead_site_leads() == 1
    assert [r["name"] for r in store.by_status("skipped")] == [lead["name"]]
    assert app.skip_dead_site_leads() == 0


def test_site_is_alive_falls_back_on_certificate(monkeypatch):
    import ssl
    import urllib.request
    from lsbot import finder
    calls = []

    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake_open(req, timeout=0, context=None):
        calls.append((req.full_url, context is not None))
        if context is None:
            raise urllib.error.URLError(ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED"))
        return Resp()
    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    assert finder.site_is_alive("salon-roza.ru")
    assert calls == [("https://salon-roza.ru", False), ("https://salon-roza.ru", True)]


def test_text_migration_replaces_user_vk_text(tmp_path):
    from lsbot import migrations
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "vk.txt").write_text("НОВЫЙ {name}", encoding="utf-8")
    (tmp_path / "data" / "templates").mkdir(parents=True)
    (tmp_path / "data" / "templates" / "vk.txt").write_text("старый", encoding="utf-8")
    assert migrations.apply_texts(tmp_path) == ["vk.txt"]
    assert (tmp_path / "data" / "templates" / "vk.txt").read_text(encoding="utf-8") == "НОВЫЙ {name}"
    assert (tmp_path / "data" / "templates" / "vk.old.txt").read_text(encoding="utf-8") == "старый"
    (tmp_path / "data" / "templates" / "vk.txt").write_text("поправила сама", encoding="utf-8")
    assert migrations.apply_texts(tmp_path) == []                       # второй раз не трогаем
    assert (tmp_path / "data" / "templates" / "vk.txt").read_text(encoding="utf-8") == "поправила сама"
