"""Отправка сообщений во ВК через отдельное окно браузера — строго по одному, по нажатию человека.

Бот открывает ваш Chrome (или Edge) с отдельным профилем, где вы один раз входите во ВК.
По команде send() он открывает диалог с сообществом, вставляет текст и нажимает «Отправить».
Сам по себе модуль ничего не отправляет: каждый вызов send() — это одно нажатие пользователя.
"""
import queue
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

    def send(self, target: str, text: str, timeout: float = 60) -> dict:
        """Отправить одно сообщение. target — ID сообщества (число) или короткий адрес."""
        if self.state != "ready":
            return {"error": "Окно ВК не готово: " + (self.message or "нажмите «Включить отправку клавишей»")}
        done = queue.Queue()
        self._tasks.put(("send", (target, text), done))
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
        if self._logged_in():
            self._set("ready", "Окно ВК готово. Пробел — отправить, S — пропустить")
        else:
            self._set("login", "Войдите во ВК в открывшемся окне браузера — бот подождёт")

    def _find_input(self, timeout_ms=10000):
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            for sel in CAPTCHA_SELECTORS:
                loc = self._page.locator(sel)
                if loc.count() and loc.first.is_visible():
                    return "captcha"
            for sel in INPUT_SELECTORS:
                loc = self._page.locator(sel)
                if loc.count() and loc.first.is_visible():
                    return loc.first
            try:
                body = (self._page.inner_text("body", timeout=1000) or "").lower()
            except Exception:
                body = ""
            if any(m in body for m in CLOSED_MARKERS):
                return "closed"
            self._page.wait_for_timeout(300)
        return None

    def _send(self, target, text):
        self._check_login()
        if self.state != "ready":
            return {"error": self.message}
        target = str(target)
        # по ID — сразу диалог; по короткому адресу — страница сообщества и кнопка «Написать сообщение»
        url = f"{self.base}/im?sel=-{target}" if target.isdigit() else f"{self.base}/{target}"
        self._page.bring_to_front()
        self._page.goto(url, wait_until="domcontentloaded")
        if not target.isdigit():
            btn = self._page.get_by_role("button", name="Написать сообщение")
            link = self._page.get_by_role("link", name="Написать сообщение")
            for loc in (btn, link):
                if loc.count():
                    loc.first.click()
                    break
            else:
                return {"error": "У сообщества нет кнопки «Написать сообщение» — сообщения закрыты",
                        "closed": True}
        box = self._find_input()
        if box == "captcha":
            return {"error": "ВК показал капчу — решите её в окне браузера и нажмите пробел ещё раз"}
        if box == "closed" or box is None:
            return {"error": "Не нашла поле для сообщения — возможно, у сообщества закрыты сообщения",
                    "closed": True}
        box.click()
        lines = text.replace("\r\n", "\n").split("\n")
        for i, line in enumerate(lines):
            if line:
                self._page.keyboard.insert_text(line)
            if i < len(lines) - 1:
                self._page.keyboard.press("Shift+Enter")
        self._page.keyboard.press("Enter")
        # проверяем, что сообщение ушло: поле ввода очистилось
        for _ in range(20):
            self._page.wait_for_timeout(200)
            try:
                if not (box.inner_text() or "").strip():
                    return {"ok": True}
            except Exception:
                return {"ok": True}
        if self._find_input(1000) == "captcha":
            return {"error": "ВК показал капчу — решите её в окне браузера и нажмите пробел ещё раз"}
        return {"error": "Текст вставлен, но не отправился — посмотрите на окно браузера"}

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
