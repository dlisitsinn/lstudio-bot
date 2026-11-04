"""Загрузка настроек из файла .env (без сторонних библиотек)."""
import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path) -> dict:
    values = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


@dataclass
class Config:
    vk_user_token: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    sender_name: str
    daily_email_limit: int
    email_delay_min: int
    email_delay_max: int
    min_members: int
    default_city: str
    portfolio_url: str
    studio_vk_url: str
    db_path: Path
    port: int = 8765
    vk_daily_limit: int = 2000
    only_vk: bool = True
    skip_dead_sites: bool = True
    auto_queue: bool = True
    vk_group_token: str = ""
    niche_only: tuple = ()          # писать только этим нишам (слова через запятую в NICHE_ONLY)

    def email_ready(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_password)


def _vk_token(value: str) -> str:
    """Принимает и сам токен, и весь адрес из браузера: …#access_token=XXX&expires_in=0&user_id=…"""
    import re
    value = (value or "").strip().strip("\"'")
    m = re.search(r"access_token=([^&\s#]+)", value)
    if m:
        value = m.group(1)
    # оставляем только допустимые символы: убирает невидимые пробелы, кавычки-«ёлочки» и т.п.
    return re.sub(r"[^A-Za-z0-9._\-]", "", value)


def _int(value, default):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def load_config(env_path: Path = None) -> Config:
    env = _load_env_file(env_path or BASE_DIR / ".env")

    def get(key, default=""):
        return os.environ.get(key, env.get(key, default))

    smtp_user = get("SMTP_USER")
    return Config(
        vk_user_token=_vk_token(get("VK_USER_TOKEN")),
        smtp_host=get("SMTP_HOST") or _guess_smtp_host(smtp_user),
        smtp_port=_int(get("SMTP_PORT"), 465),
        smtp_user=smtp_user,
        smtp_password=get("SMTP_PASSWORD").replace(" ", ""),
        sender_name=get("SENDER_NAME", "LStudio"),
        daily_email_limit=_int(get("DAILY_EMAIL_LIMIT"), 25),
        vk_daily_limit=max(1, min(_int(get("VK_DAILY_LIMIT"), 2000), 5000)),
        only_vk=get("ONLY_VK", "да").strip().lower() not in ("нет", "no", "0", "false"),
        auto_queue=get("AUTO_QUEUE", "да").strip().lower() not in ("нет", "no", "0", "false"),
        skip_dead_sites=get("SKIP_DEAD_SITES", "да").strip().lower() not in ("нет", "no", "0", "false"),
        vk_group_token=_vk_token(get("VK_GROUP_TOKEN")),
        niche_only=tuple(w.strip().lower() for w in get("NICHE_ONLY").split(",") if w.strip()),
        email_delay_min=_int(get("EMAIL_DELAY_MIN"), 60),
        email_delay_max=_int(get("EMAIL_DELAY_MAX"), 150),
        min_members=_int(get("MIN_MEMBERS"), 30),
        default_city=get("DEFAULT_CITY"),
        portfolio_url=get("PORTFOLIO_URL", "https://dlisitsinn.github.io/"),
        studio_vk_url=get("STUDIO_VK_URL", "https://vk.ru/lssttudio"),
        db_path=BASE_DIR / get("DB_FILE", "data/leads.sqlite3"),
        port=_int(get("PORT"), 8765),
    )


def _guess_smtp_host(email: str) -> str:
    domain = email.split("@")[-1].lower() if "@" in email else ""
    return {
        "gmail.com": "smtp.gmail.com",
        "yandex.ru": "smtp.yandex.ru",
        "ya.ru": "smtp.yandex.ru",
        "yandex.com": "smtp.yandex.ru",
        "mail.ru": "smtp.mail.ru",
        "bk.ru": "smtp.mail.ru",
        "inbox.ru": "smtp.mail.ru",
        "list.ru": "smtp.mail.ru",
    }.get(domain, "")
