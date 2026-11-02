"""Одноразовые поправки настроек (.env), которые вы попросили. Каждая выполняется ровно один раз."""
import re
from pathlib import Path

# имя → (ключ, значение)
SETTINGS = [
    ("vk_limit_2000", "VK_DAILY_LIMIT", "2000"),
    # (имя, ключ, значение, только_если_нет_строки)
    ("skip_dead_sites", "SKIP_DEAD_SITES", "да", True),
    ("vk_group_token_line", "VK_GROUP_TOKEN", "", True),
]


# Тексты, которые вы попросили заменить: стандартный текст копируется поверх вашего (старый — в *.old.txt)
TEXTS = [
    ("vk_text_demo_offer", "vk.txt"),
]


def apply_texts(base: Path) -> list:
    done_file = base / "data" / ".migrations"
    done = set(done_file.read_text(encoding="utf-8").split()) if done_file.exists() else set()
    applied = []
    for name, fname in TEXTS:
        if name in done:
            continue
        src, user = base / "templates" / fname, base / "data" / "templates" / fname
        if src.exists():
            user.parent.mkdir(parents=True, exist_ok=True)
            if user.exists():
                user.with_name(user.stem + ".old.txt").write_text(user.read_text(encoding="utf-8"), encoding="utf-8")
            user.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            applied.append(fname)
        done.add(name)
    if applied or not done_file.exists() or set(done_file.read_text(encoding="utf-8").split()) != done:
        done_file.parent.mkdir(parents=True, exist_ok=True)
        done_file.write_text("\n".join(sorted(done)), encoding="utf-8")
    return applied


def apply(base: Path) -> list:
    env = base / ".env"
    done_file = base / "data" / ".migrations"
    done = set(done_file.read_text(encoding="utf-8").split()) if done_file.exists() else set()
    applied = []
    before = set(done)
    if not env.exists():
        return applied
    text = env.read_text(encoding="utf-8")
    for name, key, value, *rest in SETTINGS:
        if name in done:
            continue
        line = f"{key}={value}"
        exists = re.search(rf"^{key}=.*$", text, flags=re.M)
        if exists and rest and rest[0]:
            done.add(name)                  # строка уже есть — ваше значение не трогаем
            continue
        if exists:
            text = re.sub(rf"^{key}=.*$", line, text, flags=re.M)
        else:
            text = text.rstrip("\n") + f"\n{line}\n"
        done.add(name)
        applied.append(line)
    if applied:
        env.write_text(text, encoding="utf-8")
    if applied or done != before:
        done_file.parent.mkdir(parents=True, exist_ok=True)
        done_file.write_text("\n".join(sorted(done)), encoding="utf-8")
    return applied
