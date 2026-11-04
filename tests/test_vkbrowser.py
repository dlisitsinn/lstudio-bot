"""Проверка отправки через окно браузера на поддельной странице ВК (реальный Chromium, без интернета)."""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pw = pytest.importorskip("playwright.sync_api")

from lsbot.vkbrowser import VKBrowser  # noqa: E402

STATE = {"logged_in": False, "sent": []}

BOX = """<div id="box" contenteditable="true" role="textbox" style="min-height:40px;border:1px solid #ccc"></div>
<script>
const box = document.getElementById('box');
box.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    fetch('/sent', {method: 'POST', body: JSON.stringify({peer: location.search + location.pathname, text: box.innerText})});
    box.innerHTML = '';
  }
});
</script>"""


class FakeVK(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _html(self, body):
        data = f"<!doctype html><meta charset=utf-8><body>{body}</body>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        sel = parse_qs(u.query).get("sel", [""])[0]
        if u.path == "/feed":
            return self._html("""Лента<script>setInterval(async () => {
                const s = await (await fetch('/state')).json();
                if (s.logged_in) document.cookie = 'remixsid=abc; path=/';
            }, 300)</script>""")
        if u.path == "/state":
            data = json.dumps(STATE).encode()
            self.send_response(200); self.end_headers(); self.wfile.write(data); return
        if u.path == "/im" and sel == "-999":
            return self._html("Сообщество ограничило круг лиц, которые могут написать")
        if u.path == "/im" and sel == "-555":
            return self._html('<img src="/captcha.php?sid=1" width=130 height=50>')
        if u.path == "/im" and sel == "-444":     # в диалоге уже есть наше сообщение
            return self._html('<div class="msg">Здравствуйте! Заметил, что у «Ромашка» пока нет сайта</div>' + BOX)
        if u.path == "/im":
            return self._html(BOX)
        if u.path == "/lavanda74":   # как настоящая группа: кнопка открывает чат в другой вкладке, есть поле комментария
            return self._html("""<h1>Лаванда</h1><a href="/im?sel=-777" target="_blank">Написать сообщение</a>
                <a href="/wall-777_15">Пост</a>
                <div id="comment" contenteditable="true" role="textbox"></div>
                <script>document.getElementById('comment').addEventListener('keydown', e => {
                  if (e.key === 'Enter') fetch('/comment', {method: 'POST', body: '{"peer":"COMMENT","text":"x"}'}); });</script>""")
        if u.path == "/closedgroup":
            return self._html('<h1>Группа без сообщений</h1><a href="/wall-999_3">Пост</a>')
        self._html("ok")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        STATE["sent"].append(json.loads(self.rfile.read(n)))
        self.send_response(200); self.end_headers()


@pytest.fixture(scope="module")
def fake_vk():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeVK)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def wait_state(b, state, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if b.state == state:
            return True
        time.sleep(0.2)
    return False


def test_browser_flow(fake_vk, tmp_path):
    b = VKBrowser(tmp_path / "profile", base=fake_vk, channels=(None,), headless=True, auto_install=False)
    try:
        assert b.send("123", "x")["error"].startswith("Окно ВК не готово")   # до включения ничего не уходит
        b.open()
        assert wait_state(b, "login"), b.status()                            # ждём входа во ВК
        STATE["logged_in"] = True
        assert wait_state(b, "ready"), b.status()

        r = b.send("123", "Здравствуйте!\nВторая строка")
        assert r.get("ok"), r
        assert STATE["sent"][-1]["text"].replace("\r", "") .strip() == "Здравствуйте!\nВторая строка"
        assert "-123" in STATE["sent"][-1]["peer"]

        r = b.send("lavanda74", "Привет, Лаванда")                         # по короткому адресу
        assert r.get("ok") and r["gid"] == "777", r                        # ID найден на странице группы
        assert STATE["sent"][-1]["text"].strip() == "Привет, Лаванда"
        assert "-777" in STATE["sent"][-1]["peer"]
        assert not any(x["peer"] == "COMMENT" for x in STATE["sent"])     # в комментарий не написано

        sent_before = len(STATE["sent"])
        r = b.send("444", "Здравствуйте! Заметил, что у «Ромашка» пока нет сайта", marker="«Ромашка»")
        assert r.get("ok") and r.get("already"), r                         # уже писали — второй раз нет
        r = b.send("123", "Привет, «Ромашка»", marker="«Ромашка»")         # в другом диалоге — пишем
        assert r.get("ok") and not r.get("already"), r
        sent_before = len(STATE["sent"])
        t0 = time.monotonic()
        assert b.send("closedgroup", "x").get("closed")                    # сообщения закрыты
        assert time.monotonic() - t0 < 5                                   # распознано быстро
        r = b.send("555", "x")
        assert "капчу" in r["error"]
        assert len(STATE["sent"]) == sent_before                           # ничего лишнего не ушло
    finally:
        b.close()


def test_app_send_via_browser(fake_vk, tmp_path):
    from test_bot import make_import_app
    app, store = make_import_app()
    app._sleep = lambda s: None
    app.vkb = VKBrowser(tmp_path / "p2", base=fake_vk, channels=(None,), headless=True, auto_install=False)
    STATE["logged_in"] = True
    try:
        store.add_lead({"uid": "t:1", "source": "2gis", "name": "Ноготок", "vk_group_id": 321})
        lid = store.by_status("new")[0]["id"]
        app.action("approve", [lid])
        app.vkb_open()
        assert wait_state(app.vkb, "ready"), app.vkb.status()
        assert app.state()["vkb"]["state"] == "ready"
        r = app.send_vk_browser(lid, "Текст для Ноготка")
        assert r["ok"] and store.get(lid)["status"] == "contacted" and store.vk_sent_today() == 1
        assert STATE["sent"][-1]["text"].strip() == "Текст для Ноготка"
        assert "уже обработана" in app.send_vk_browser(lid, "ещё")["error"]
    finally:
        app.vkb.close()
