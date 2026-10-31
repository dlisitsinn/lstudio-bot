"""Самообновление бота с GitHub. Запускается перед ботом из «Запустить бота.bat».

Что делает:
  1. смотрит, какая версия лежит на GitHub (репозиторий указан в update.json);
  2. если она новее той, что на компьютере, скачивает и заменяет файлы программы;
  3. никогда не трогает ваши данные: .env (почта, токены) и папку data (база, ваши тексты).
Если интернета нет или GitHub недоступен — просто запускается текущая версия.
"""
import io
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
VERSION_FILE = BASE / "data" / ".version"
PROTECTED = (".env", "data/", ".git/")           # это обновление не трогает никогда
USER_TEXTS = ("email.txt", "vk.txt")             # тексты, которые вы могли поправить


def _read_env_value(key: str) -> str:
    env = BASE / ".env"
    if not env.exists():
        return ""
    for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.strip().startswith(key + "="):
            return line.split("=", 1)[1].strip().strip("\"'")
    return ""


def load_settings() -> dict:
    try:
        cfg = json.loads((BASE / "update.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    cfg["token"] = _read_env_value("GITHUB_TOKEN")
    if _read_env_value("AUTO_UPDATE").lower() in ("0", "no", "нет", "false"):
        cfg["repo"] = ""
    return cfg


def _get(url: str, token: str = "", accept: str = "application/vnd.github+json", opener=None):
    headers = {"User-Agent": "lstudio-bot-updater", "Accept": accept}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with (opener or urllib.request.urlopen)(req, timeout=20) as resp:
        return resp.read()


def latest_sha(repo: str, branch: str, token: str = "", opener=None) -> str:
    raw = _get(f"https://api.github.com/repos/{repo}/commits/{branch}", token,
               "application/vnd.github.sha", opener)
    return raw.decode().strip()


def is_protected(rel: str) -> bool:
    rel = rel.replace("\\", "/")
    return any(rel == p.rstrip("/") or rel.startswith(p) for p in PROTECTED)


def save_user_texts(base: Path = BASE):
    """Перед первым обновлением переносит ваши правки текстов в data/templates."""
    user_dir = base / "data" / "templates"
    for name in USER_TEXTS:
        src, dst = base / "templates" / name, user_dir / name
        if src.exists() and not dst.exists() and not (base / "data" / ".version").exists():
            user_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def apply_zip(data: bytes, base: Path = BASE) -> int:
    """Распаковывает архив репозитория поверх программы. Возвращает число обновлённых файлов."""
    z = zipfile.ZipFile(io.BytesIO(data))
    names = [n for n in z.namelist() if not n.endswith("/")]
    if not names:
        raise ValueError("пустой архив")
    root = names[0].split("/", 1)[0] + "/"
    count = 0
    for name in names:
        rel = name[len(root):] if name.startswith(root) else name
        if not rel or ".." in Path(rel).parts or rel.startswith("/") or is_protected(rel):
            continue
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        new = z.read(name)
        if target.exists() and target.read_bytes() == new:
            continue
        tmp = target.with_name(target.name + ".new")
        tmp.write_bytes(new)
        os.replace(tmp, target)
        count += 1
    return count


def update(base: Path = BASE, opener=None, settings: dict = None) -> str:
    settings = settings if settings is not None else load_settings()
    repo, branch, token = settings.get("repo", ""), settings.get("branch", "main"), settings.get("token", "")
    if not repo:
        return "skip"
    version_file = base / "data" / ".version"
    current = version_file.read_text().strip() if version_file.exists() else ""
    sha = latest_sha(repo, branch, token, opener)
    if sha == current:
        return "latest"
    save_user_texts(base)
    data = _get(f"https://api.github.com/repos/{repo}/zipball/{sha}", token, opener=opener)
    changed = apply_zip(data, base)
    version_file.parent.mkdir(parents=True, exist_ok=True)
    version_file.write_text(sha)
    return f"updated:{changed}"


def main():
    try:
        result = update()
    except urllib.error.HTTPError as exc:
        hint = " (для закрытого репозитория нужен GITHUB_TOKEN в .env)" if exc.code in (401, 403, 404) else ""
        print(f"Не удалось проверить обновления: GitHub ответил {exc.code}{hint}. Запускаю текущую версию.")
        return
    except Exception as exc:  # нет интернета и т.п. — не мешаем запуску
        print(f"Не удалось проверить обновления ({type(exc).__name__}). Запускаю текущую версию.")
        return
    if result.startswith("updated"):
        print(f"✅ Бот обновлён до последней версии (изменено файлов: {result.split(':')[1]}).")
    elif result == "latest":
        print("Бот последней версии.")


if __name__ == "__main__":
    main()
    sys.exit(0)
