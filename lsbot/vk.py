"""Минимальный клиент VK API на стандартной библиотеке Python."""
import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API_URL = "https://api.vk.ru/method/"
API_VERSION = "5.199"


class VKError(Exception):
    def __init__(self, code, message, method="", data=None):
        super().__init__(f"VK API {method}: [{code}] {message}")
        self.code = code
        self.message = message
        self.data = data or {}


class VK:
    """Вызов методов VK API с ограничением частоты (не больше 3 запросов в секунду)."""

    def __init__(self, token: str, min_interval: float = 0.36, opener=None):
        self.token = token
        self.min_interval = min_interval
        self._last = 0.0
        self._lock = threading.Lock()
        self._opener = opener or urllib.request.urlopen

    def call(self, method: str, _retry_flood: bool = True, **params):
        data = {k: _fmt(v) for k, v in params.items() if v is not None}
        data["access_token"] = self.token
        data["v"] = API_VERSION
        body = urllib.parse.urlencode(data).encode()

        for attempt in range(5):
            with self._lock:
                wait = self.min_interval - (time.monotonic() - self._last)
                if wait > 0:
                    time.sleep(wait)
                self._last = time.monotonic()
            try:
                with self._opener(API_URL + method, data=body, timeout=30) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt == 4:
                    raise VKError(-1, f"нет связи с VK: {exc}", method)
                time.sleep(2 * (attempt + 1))
                continue

            if "error" in payload:
                err = payload["error"]
                code = err.get("error_code")
                retry = (6, 9, 10) if _retry_flood else (6, 10)
                if code in retry and attempt < 4:  # слишком часто / внутренняя ошибка
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise VKError(code, err.get("error_msg", ""), method, err)
            return payload.get("response")
        raise VKError(-1, "превышено число попыток", method)


def _fmt(value):
    if isinstance(value, (list, tuple, set)):
        return ",".join(str(v) for v in value)
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (dict,)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


class GroupLongPoll:
    """Bots Long Poll API: получение входящих сообщений сообщества."""

    def __init__(self, vk: VK, group_id: int, opener=None):
        self.vk = vk
        self.group_id = group_id
        self._opener = opener or urllib.request.urlopen
        self.server = self.key = self.ts = None

    def _refresh(self, keep_ts=False):
        info = self.vk.call("groups.getLongPollServer", group_id=self.group_id)
        self.server, self.key = info["server"], info["key"]
        if not keep_ts or self.ts is None:
            self.ts = info["ts"]

    def listen(self):
        self._refresh()
        while True:
            query = urllib.parse.urlencode(
                {"act": "a_check", "key": self.key, "ts": self.ts, "wait": 25}
            )
            try:
                with self._opener(f"{self.server}?{query}", timeout=40) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError):
                time.sleep(3)
                continue

            failed = data.get("failed")
            if failed == 1:
                self.ts = data["ts"]
                continue
            if failed in (2, 3):
                self._refresh(keep_ts=failed == 2)
                continue

            self.ts = data.get("ts", self.ts)
            for update in data.get("updates", []):
                yield update


def send_message(vk: VK, peer_id: int, text: str, keyboard: dict = None):
    """Отправка сообщения с разбиением на части (лимит VK — 4096 символов)."""
    chunks = split_text(text, 3900)
    for i, chunk in enumerate(chunks):
        vk.call(
            "messages.send",
            peer_id=peer_id,
            message=chunk,
            random_id=random.randint(1, 2**31 - 1),
            keyboard=keyboard if (keyboard and i == len(chunks) - 1) else None,
            dont_parse_links=1,
        )


def split_text(text: str, limit: int) -> list:
    if len(text) <= limit:
        return [text]
    parts, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts
