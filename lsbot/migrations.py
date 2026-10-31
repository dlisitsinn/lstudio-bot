"""Одноразовые поправки настроек (.env), которые вы попросили. Каждая выполняется ровно один раз."""
import re
from pathlib import Path

# имя → (ключ, значение)
SETTINGS = [
    ("vk_limit_2000", "VK_DAILY_LIMIT", "2000"),
]


def apply(base: Path) -> list:
    env = base / ".env"
    done_file = base / "data" / ".migrations"
    done = set(done_file.read_text(encoding="utf-8").split()) if done_file.exists() else set()
    applied = []
    if not env.exists():
        return applied
    text = env.read_text(encoding="utf-8")
    for name, key, value in SETTINGS:
        if name in done:
            continue
        line = f"{key}={value}"
        if re.search(rf"^{key}=.*$", text, flags=re.M):
            text = re.sub(rf"^{key}=.*$", line, text, flags=re.M)
        else:
            text = text.rstrip("\n") + f"\n{line}\n"
        done.add(name)
        applied.append(line)
    if applied:
        env.write_text(text, encoding="utf-8")
        done_file.parent.mkdir(parents=True, exist_ok=True)
        done_file.write_text("\n".join(sorted(done)), encoding="utf-8")
    return applied
