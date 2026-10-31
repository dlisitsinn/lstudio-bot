"""Хранилище найденных организаций (SQLite)."""
import csv
import re
import sqlite3
import threading
from datetime import datetime, date
from pathlib import Path

# Статусы:
#   new        — найдена, ждёт решения
#   approved   — одобрена, письмо в очереди
#   sent       — письмо отправлено
#   manual     — почты нет: написать вручную (ВК / телефон / мессенджер)
#   contacted  — связались вручную
#   skipped    — пропущена
#   failed     — письмо не ушло

STATUS_NAMES = {
    "new": "новые",
    "approved": "в очереди писем",
    "sent": "письмо отправлено",
    "manual": "написать вручную",
    "contacted": "связались вручную",
    "skipped": "пропущены",
    "failed": "ошибка отправки",
}

SOURCE_NAMES = {"vk": "ВКонтакте", "osm": "OpenStreetMap", "link": "по ссылке",
                "2gis": "2ГИС (файл)", "yandex": "Яндекс Карты (файл)", "file": "из таблицы"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    uid           TEXT UNIQUE NOT NULL,
    source        TEXT NOT NULL,
    name          TEXT NOT NULL,
    vk_group_id   INTEGER,
    screen_name   TEXT,
    city          TEXT,
    address       TEXT,
    activity      TEXT,
    members       INTEGER DEFAULT 0,
    email         TEXT,
    phone         TEXT,
    other_links   TEXT,
    info          TEXT,
    query         TEXT,
    status        TEXT NOT NULL DEFAULT 'new',
    note          TEXT,
    found_at      TEXT NOT NULL,
    contacted_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE TABLE IF NOT EXISTS blacklist (
    value     TEXT PRIMARY KEY,
    added_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS send_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id   INTEGER,
    email     TEXT,
    ok        INTEGER,
    error     TEXT,
    sent_at   TEXT NOT NULL
);
"""

FIELDS = ("uid", "source", "name", "vk_group_id", "screen_name", "city", "address",
          "activity", "members", "email", "phone", "other_links", "info", "query", "note")


def _now():
    return datetime.now().isoformat(timespec="seconds")


def norm_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits[0] in "78":
        digits = "7" + digits[1:]
    return digits


class Store:
    def __init__(self, path):
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(leads)")}
            if "info" not in cols:  # база от старой версии бота
                self.conn.execute("ALTER TABLE leads ADD COLUMN info TEXT")
            self.conn.commit()

    # --- добавление ---
    def add_lead(self, lead: dict) -> bool:
        """Добавляет организацию. False — если уже есть (по ID, почте или телефону) или в чёрном списке."""
        lead = dict(lead)
        if not lead.get("uid"):
            lead["uid"] = f"vk:{lead['vk_group_id']}"
        lead.setdefault("source", lead["uid"].split(":")[0])
        for value in (lead.get("email"), lead.get("screen_name"),
                      str(lead.get("vk_group_id") or ""), norm_phone(lead.get("phone"))):
            if value and self.is_blacklisted(value):
                return False
        if lead.get("email") and self._exists("lower(email)=lower(?)", lead["email"]):
            return False
        if lead.get("vk_group_id") and self._exists("vk_group_id=?", lead["vk_group_id"]):
            return False
        phone = norm_phone(lead.get("phone"))
        if phone and any(norm_phone(r["phone"]) == phone for r in self._with_phone()):
            return False
        cols = [f for f in FIELDS if lead.get(f) not in (None, "")]
        with self.lock:
            cur = self.conn.execute(
                f"INSERT OR IGNORE INTO leads ({','.join(cols)}, status, found_at) "
                f"VALUES ({','.join('?' * len(cols))}, 'new', ?)",
                [lead[c] for c in cols] + [_now()],
            )
            self.conn.commit()
            return cur.rowcount > 0

    def _exists(self, where, value) -> bool:
        with self.lock:
            return self.conn.execute(f"SELECT 1 FROM leads WHERE {where}", (value,)).fetchone() is not None

    def _with_phone(self):
        with self.lock:
            return self.conn.execute("SELECT phone FROM leads WHERE phone IS NOT NULL").fetchall()

    def known_uids(self) -> set:
        with self.lock:
            return {r[0] for r in self.conn.execute("SELECT uid FROM leads")}

    def known_group_ids(self) -> set:
        with self.lock:
            return {r[0] for r in self.conn.execute(
                "SELECT vk_group_id FROM leads WHERE vk_group_id IS NOT NULL")}

    # --- чтение ---
    def get(self, lead_id: int):
        with self.lock:
            return self.conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()

    def by_status(self, *statuses, limit=None, offset=0):
        marks = ",".join("?" * len(statuses))
        sql = f"SELECT * FROM leads WHERE status IN ({marks}) ORDER BY id"
        params = list(statuses)
        if limit:
            sql += " LIMIT ? OFFSET ?"
            params += [limit, offset]
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def count_by_status(self) -> dict:
        with self.lock:
            rows = self.conn.execute("SELECT status, COUNT(*) FROM leads GROUP BY status").fetchall()
        return {r[0]: r[1] for r in rows}

    # --- изменение ---
    def set_status(self, lead_id: int, status: str, note: str = None):
        with self.lock:
            contacted = _now() if status in ("sent", "contacted") else None
            self.conn.execute(
                """UPDATE leads SET status=?, note=COALESCE(?, note),
                       contacted_at=COALESCE(?, contacted_at) WHERE id=?""",
                (status, note, contacted, lead_id),
            )
            self.conn.commit()

    def update_fields(self, lead_id: int, **fields):
        fields = {k: v for k, v in fields.items() if k in FIELDS}
        if not fields:
            return
        with self.lock:
            self.conn.execute(
                f"UPDATE leads SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                [*fields.values(), lead_id],
            )
            self.conn.commit()

    # --- чёрный список ---
    def blacklist_add(self, value: str) -> str:
        value = value.strip().lower()
        m = re.search(r"vk\.(?:com|ru)/([\w.]+)", value)
        if m:
            value = m.group(1)
            club = re.fullmatch(r"(?:club|public)(\d+)", value)
            value = club.group(1) if club else value
        elif "@" not in value and len(norm_phone(value)) >= 10:
            value = norm_phone(value)
        with self.lock:
            self.conn.execute("INSERT OR IGNORE INTO blacklist(value, added_at) VALUES (?,?)",
                              (value, _now()))
            rows = self.conn.execute(
                "SELECT id, email, phone, screen_name, vk_group_id FROM leads "
                "WHERE status IN ('new','approved','manual')").fetchall()
            for r in rows:
                keys = {(r["email"] or "").lower(), (r["screen_name"] or "").lower(),
                        str(r["vk_group_id"] or ""), norm_phone(r["phone"])}
                if value in keys:
                    self.conn.execute("UPDATE leads SET status='skipped', note='чёрный список' "
                                      "WHERE id=?", (r["id"],))
            self.conn.commit()
        return value

    def is_blacklisted(self, value: str) -> bool:
        with self.lock:
            return self.conn.execute("SELECT 1 FROM blacklist WHERE value=?",
                                     (value.strip().lower(),)).fetchone() is not None

    # --- журнал писем ---
    def log_send(self, lead_id, email, ok, error=None):
        with self.lock:
            self.conn.execute(
                "INSERT INTO send_log(lead_id, email, ok, error, sent_at) VALUES (?,?,?,?,?)",
                (lead_id, email, 1 if ok else 0, error, _now()))
            self.conn.commit()

    def sent_today(self) -> int:
        """Писем на почту сегодня."""
        with self.lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM send_log WHERE ok=1 AND sent_at >= ? AND email NOT LIKE 'vk:%'",
                (date.today().isoformat(),)).fetchone()[0]

    def vk_sent_today(self) -> int:
        """Сообщений во ВК сегодня."""
        with self.lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM send_log WHERE ok=1 AND sent_at >= ? AND email LIKE 'vk:%'",
                (date.today().isoformat(),)).fetchone()[0]

    # --- выгрузка ---
    def export_csv(self, path) -> int:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            rows = self.conn.execute("SELECT * FROM leads ORDER BY id").fetchall()
        headers = ["№", "Название", "Источник", "Ссылка ВК", "Город", "Адрес", "Сфера",
                   "Почта", "Телефон", "Другие ссылки", "Запрос", "Статус", "Заметка",
                   "Найдено", "Связались"]
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(headers)
            for r in rows:
                w.writerow([r["id"], r["name"], SOURCE_NAMES.get(r["source"], r["source"]),
                            vk_link(r), r["city"], r["address"], r["activity"], r["email"],
                            r["phone"], r["other_links"], r["query"],
                            STATUS_NAMES.get(r["status"], r["status"]), r["note"],
                            r["found_at"], r["contacted_at"]])
        return len(rows)


def vk_link(row) -> str:
    if row["screen_name"]:
        return f"https://vk.ru/{row['screen_name']}"
    if row["vk_group_id"]:
        return f"https://vk.ru/club{row['vk_group_id']}"
    return ""


def row_to_dict(row) -> dict:
    d = dict(row)
    d["vk_link"] = vk_link(row)
    d["status_name"] = STATUS_NAMES.get(row["status"], row["status"])
    d["source_name"] = SOURCE_NAMES.get(row["source"], row["source"])
    return d
