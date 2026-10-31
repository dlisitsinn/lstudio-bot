"""Подготовка персональных текстов писем и сообщений по шаблонам."""
import json
import re
from pathlib import Path

from .config import BASE_DIR

TEMPLATES_DIR = BASE_DIR / "templates"          # тексты по умолчанию (обновляются с GitHub)
USER_TEMPLATES_DIR = BASE_DIR / "data" / "templates"   # ваши правки текстов (обновление их не трогает)


def template_path(name: str, base: Path = None) -> Path:
    """Сначала ваш вариант текста из data/templates, иначе — стандартный."""
    if base is None:
        user = USER_TEMPLATES_DIR / name
        if user.exists():
            return user
        base = TEMPLATES_DIR
    return base / name


class _Safe(dict):
    def __missing__(self, key):
        return ""


def _read_template(path: Path) -> str:
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("#")]
    return "\n".join(lines).strip("\n")


def load_portfolio(path: Path = None) -> list:
    path = path or TEMPLATES_DIR / "portfolio.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def match_example(text: str, portfolio: list):
    """Подбирает работу из портфолио по совпадению ключевых слов с названием и сферой."""
    text = f" {text.lower()} "
    best, best_score = None, 0
    for item in portfolio:
        score = sum(1 for kw in item.get("keywords", []) if kw.lower() in text)
        if score > best_score:
            best, best_score = item, score
    return best


def clean_name(name: str) -> str:
    """Убирает из названия группы мусор: эмодзи, кавычки, «| Челябинск» и т.п."""
    name = re.sub(r"[\U0001F000-\U0001FFFF☀-➿️]", "", name or "")
    name = name.replace("«", "").replace("»", "").replace('"', "").strip()
    name = re.split(r"\s[|•·—–]\s|\s\|", name)[0].strip()
    return re.sub(r"\s{2,}", " ", name) or "вашей организации"


class Drafter:
    def __init__(self, config, templates_dir: Path = None):
        self.config = config
        self.dir = templates_dir  # None — стандартное расположение с учётом ваших правок
        self.portfolio = load_portfolio((templates_dir or TEMPLATES_DIR) / "portfolio.json")

    def _values(self, lead) -> dict:
        name = clean_name(lead["name"])
        example = match_example(f"{lead['name']} {lead['activity'] or ''}", self.portfolio)
        example_text = ""
        if example:
            example_text = (
                f"\nВот пример нашей работы для похожей сферы — {example['niche']} "
                f"«{example['name']}»: {example['url']}\n"
            )
        keys = lead.keys() if hasattr(lead, "keys") else lead
        source = lead["source"] if "source" in keys else "vk"
        where = {
            "vk": f"вашу группу «{name}» во ВКонтакте",
            "link": f"вашу группу «{name}» во ВКонтакте",
            "osm": f"«{name}» на карте города",
            "2gis": f"«{name}» в 2ГИС",
            "yandex": f"«{name}» на Яндекс Картах",
        }.get(source, f"«{name}» в интернете")
        return _Safe(
            name=name,
            where=where,
            example=example_text,
            portfolio_url=self.config.portfolio_url,
            studio_vk_url=self.config.studio_vk_url,
            sender_name=self.config.sender_name,
        )

    def email(self, lead):
        """Возвращает (тема, текст) письма."""
        raw = _read_template(template_path("email.txt", self.dir))
        text = raw.format_map(self._values(lead))
        subject, body = "Сайт для вашего бизнеса", text
        first, _, rest = text.partition("\n")
        if first.lower().startswith("тема:"):
            subject, body = first.split(":", 1)[1].strip(), rest
        return subject, _tidy(body)

    def vk(self, lead) -> str:
        raw = _read_template(template_path("vk.txt", self.dir))
        return _tidy(raw.format_map(self._values(lead)))


def _tidy(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"
