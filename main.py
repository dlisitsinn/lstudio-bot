"""Запуск бота LStudio.

    python main.py          — открыть окно управления в браузере
    python main.py check    — проверить настройки в консоли
    python main.py --no-browser — запустить, не открывая окно (для автозапуска)
"""
import sys
import threading
import time
import webbrowser

from lsbot.config import BASE_DIR, load_config
from lsbot.db import Store
from lsbot.drafts import Drafter
from lsbot.finder import Finder
from lsbot.mailer import BatchSender, check_login
from lsbot.osm import OSMFinder
from lsbot.vk import VK, VKError
from lsbot.web import App, serve


def build(cfg):
    user_vk = VK(cfg.vk_user_token) if cfg.vk_user_token else None
    vk_finder = Finder(user_vk, min_members=cfg.min_members)
    osm_finder = OSMFinder(vk_finder=vk_finder)
    store = Store(cfg.db_path)
    drafter = Drafter(cfg)
    return store, vk_finder, osm_finder, drafter


def cmd_check(cfg):
    print("Проверяю настройки из файла .env …\n")
    _, vk_finder, _, _ = build(cfg)
    if not vk_finder.can_search:
        print("⚠️  VK_USER_TOKEN не указан — поиск во ВК выключен, по карте работает")
    else:
        try:
            vk_finder.vk.call("database.getCities", country_id=1, q="Москва", count=1)
            print("✅ Токен ВК работает — поиск во ВКонтакте включён")
        except VKError as exc:
            print(f"❌ Токен ВК: {exc}")
    if cfg.email_ready():
        err = check_login(cfg)
        print(f"✅ Почта {cfg.smtp_user} работает" if not err else f"❌ Почта: {err}")
    else:
        print("⚠️  Почта не настроена — письма отправлять не получится")


def run(cfg, open_browser=True):
    store, vk_finder, osm_finder, drafter = build(cfg)
    app = App(cfg, store, vk_finder, osm_finder, drafter,
              sender_factory=lambda notify: BatchSender(cfg, store, drafter, notify),
              mail_check=lambda: check_login(cfg))
    url = f"http://127.0.0.1:{cfg.port}"
    try:
        server = serve(app, cfg.port)
    except OSError:
        print(f"Похоже, бот уже запущен. Окно: {url}")
        if open_browser:
            webbrowser.open(url)
        time.sleep(3)
        return
    version_file = BASE_DIR / "data" / ".version"
    version = version_file.read_text().strip()[:7] if version_file.exists() else "локальная"
    app.notify(f"Бот запущен (версия {version})")
    threading.Thread(target=app.check_vk_messages, daemon=True).start()
    print("🤖 Бот LStudio запущен.")
    print(f"   Окно управления: {url}  (откроется в браузере само)")
    print("   Не закрывайте это чёрное окно, пока работаете. Чтобы выключить бота — закройте его.\n")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nБот остановлен.")


def main():
    if not (BASE_DIR / ".env").exists():
        example = BASE_DIR / ".env.example"
        if example.exists():
            (BASE_DIR / ".env").write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
            print("Создала файл .env — почту и токен можно вписать позже.")
    cfg = load_config()
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        cmd_check(cfg)
    else:
        run(cfg, open_browser="--no-browser" not in sys.argv)


if __name__ == "__main__":
    main()
