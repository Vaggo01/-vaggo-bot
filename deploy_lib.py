# -*- coding: utf-8 -*-
"""
Обновление кода с GitHub без панели Bothost.
1) Смотрит latest SHA ветки main
2) Качает zip
3) Кладёт .py и нужные файлы в ROOT
4) (опционально) self-restart через Bothost agent API
"""
from __future__ import annotations

import io
import json
import os
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
SHA_FILE = ROOT / ".deploy_sha"
# репо с «-» в имени — для raw/api нужен %2D
OWNER = "vladislavbondarev230-cloud"
REPO = "-vaggo-bot"
BRANCH = "main"

# что перезаписывать при pull (только cloud-пакет 2.0)
INCLUDE_SUFFIXES = {".py", ".txt", ".json", ".yml", ".yaml", ".md"}
INCLUDE_NAMES = {
    "bot.py",
    "content.py",
    "state.py",
    "tg.py",
    "orders_lib.py",
    "moderation_lib.py",
    "terms_lib.py",
    "support_lib.py",
    "balance_lib.py",
    "giveaway_lib.py",
    "queue_lib.py",
    "promo_lib.py",
    "growth_lib.py",
    "chat_mod_lib.py",
    "docs_lib.py",
    "platega_lib.py",
    "grok_auth.py",
    "imagine.py",
    "deploy_lib.py",
    "requirements.txt",
    "Dockerfile",
    "docker-compose.yml",
    "config.example.json",
    "bridge_endpoint.json",
    "README.md",
    "giveaway_restore.json",  # 3.0: участники розыгрыша (seed)
}
SKIP_NAMES = {
    "config.json",  # секреты/локальные правки
    "state.json",
    "auth.json",
    "grok_bridge.py",  # только на домашнем ПК
    "restaurant_lib.py",
    "panel.py",
    "app.py",
}


def _http_get(url: str, timeout: float = 45) -> bytes:
    req = Request(
        url,
        headers={
            "User-Agent": "VaggoBot-Deploy/1.0",
            "Accept": "application/vnd.github+json,application/zip,*/*",
        },
    )
    with urlopen(req, timeout=timeout) as r:
        return r.read()


def latest_commit_sha(branch: str = BRANCH) -> str:
    """SHA последнего коммита main через GitHub API."""
    # repo name starts with - → encode
    repo_enc = REPO if not REPO.startswith("-") else "%2D" + REPO[1:]
    url = f"https://api.github.com/repos/{OWNER}/{repo_enc}/commits/{branch}"
    raw = _http_get(url, timeout=20)
    data = json.loads(raw.decode("utf-8"))
    sha = (data.get("sha") or "").strip()
    if not sha:
        raise RuntimeError("empty sha from github")
    return sha


def local_sha() -> str:
    if SHA_FILE.is_file():
        return SHA_FILE.read_text(encoding="utf-8").strip()
    return ""


def save_local_sha(sha: str) -> None:
    try:
        SHA_FILE.write_text(sha.strip(), encoding="utf-8")
    except Exception as e:
        print("save sha fail", e, flush=True)


def needs_update() -> tuple[bool, str, str]:
    """(needs, remote_sha, local_sha)."""
    remote = latest_commit_sha()
    local = local_sha()
    return (remote != local and bool(remote), remote, local)


def _should_extract(name: str) -> bool:
    base = Path(name).name
    if base in SKIP_NAMES:
        return False
    if base.startswith("."):
        return False
    if base in INCLUDE_NAMES:
        return True
    suf = Path(base).suffix.lower()
    if suf in INCLUDE_SUFFIXES and base.endswith(".py"):
        return True
    return False


def pull_github_zip(branch: str = BRANCH) -> dict[str, Any]:
    """
    Скачать zip ветки и разложить файлы в ROOT.
    """
    repo_enc = REPO if not REPO.startswith("-") else "%2D" + REPO[1:]
    # archive link
    url = f"https://github.com/{OWNER}/{REPO}/archive/refs/heads/{branch}.zip"
    try:
        raw = _http_get(url, timeout=90)
    except Exception:
        # fallback encoded
        url = f"https://codeload.github.com/{OWNER}/{repo_enc}/zip/refs/heads/{branch}"
        raw = _http_get(url, timeout=90)

    written: list[str] = []
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        # top folder: -vaggo-bot-main/ or similar
        names = zf.namelist()
        if not names:
            raise RuntimeError("empty zip")
        prefix = names[0].split("/")[0] + "/"
        for info in zf.infolist():
            if info.is_dir():
                continue
            rel = info.filename
            if not rel.startswith(prefix):
                continue
            inner = rel[len(prefix) :]
            if not inner or "/" in inner:
                # only root-level files (Bothost layout)
                # allow media/ empty skip
                if inner.count("/") == 1 and inner.startswith("media/"):
                    continue
                if "/" in inner:
                    continue
            base = Path(inner).name
            if not _should_extract(base):
                continue
            target = ROOT / base
            data = zf.read(info)
            target.write_bytes(data)
            written.append(base)

    sha = ""
    try:
        sha = latest_commit_sha(branch)
        save_local_sha(sha)
    except Exception as e:
        print("sha after pull", e, flush=True)

    return {
        "ok": True,
        "files": written,
        "count": len(written),
        "sha": sha[:12] if sha else "",
        "branch": branch,
    }


def _agent_urls() -> list[str]:
    """Возможные адреса Bothost agent (DNS внутри/снаружи сети)."""
    urls: list[str] = []
    env = (os.environ.get("BOTHOST_AGENT_URL") or "").strip()
    if env:
        urls.append(env.rstrip("/"))
    # из доки Bothost
    for u in (
        "http://agent:8000",
        "http://agent.bothost.ru",
        "http://msk1.bothost.ru",
        "http://127.0.0.1:8000",
    ):
        if u not in urls:
            urls.append(u)
    return urls


def bothost_self_restart() -> dict[str, Any]:
    """
    Перезапуск: 1) agent API (несколько URL) 2) exit → Bothost поднимет контейнер.
    """
    bot_id = (os.environ.get("BOT_ID") or "").strip()
    headers = {"Content-Type": "application/json", "User-Agent": "VaggoDeploy/1"}
    if bot_id:
        headers["X-Bot-ID"] = bot_id

    tried: list[str] = []
    last_err = ""
    for base in _agent_urls():
        url = f"{base}/api/bots/self/restart"
        tried.append(url)
        req = Request(url, data=b"{}", headers=headers, method="POST")
        try:
            with urlopen(req, timeout=8) as r:
                body = r.read().decode("utf-8", "replace")
                try:
                    data = json.loads(body)
                except Exception:
                    data = {"ok": True, "raw": body[:200]}
                data["agent"] = base
                data["bot_id"] = bot_id or "-"
                data["method"] = "agent_api"
                return data
        except Exception as e:
            last_err = str(e)[:120]
            continue

    # Fallback: process exit — Bothost/Docker обычно поднимает контейнер снова
    on_host = bool(bot_id)
    if on_host:
        return {
            "ok": True,
            "method": "process_exit",
            "message": "agent fail → exit (контейнер перезапустится)",
            "agent_error": last_err,
            "tried": tried[:4],
            "bot_id": bot_id,
            "will_exit": True,
        }
    return {
        "ok": False,
        "error": last_err or "no agent",
        "tried": tried[:4],
        "bot_id": bot_id or "-",
        "method": "none",
    }


def schedule_exit_if_needed(restart_result: dict) -> None:
    """Выйти через пару секунд (после ответа в TG), чтобы подтянуть новый код."""
    if not restart_result.get("will_exit"):
        # На Bothost после удачного agent API тоже лучше exit — код уже на диске
        if (os.environ.get("BOT_ID") or "").strip() and restart_result.get(
            "method"
        ) == "agent_api":
            restart_result["will_exit"] = True
        else:
            return

    def _bye() -> None:
        time.sleep(3.0)
        print("deploy: process exit for container restart", flush=True)
        # ненулевой код — надёжнее для restart-policy
        os._exit(1)

    try:
        import threading

        threading.Thread(target=_bye, name="deploy-exit", daemon=True).start()
    except Exception:
        time.sleep(1)
        os._exit(1)


def redeploy_now(*, restart: bool = True) -> dict[str, Any]:
    """Pull + restart (на Bothost всегда exit после pull). Всегда тянем zip."""
    out: dict[str, Any] = {"ts": int(time.time())}
    on_host = bool((os.environ.get("BOT_ID") or "").strip())
    try:
        need, remote, local = needs_update()
        out["remote_sha"] = remote[:12]
        out["local_sha"] = (local or "")[:12]
        out["was_current"] = not need
    except Exception as e:
        out["sha_check_error"] = str(e)[:160]
        need = True

    try:
        pull = pull_github_zip()
        out["pull"] = pull
    except Exception as e:
        out["ok"] = False
        out["pull_error"] = str(e)[:300]
        # на Bothost всё равно пробуем exit — поднять контейнер с volume/image
        if restart and on_host:
            out["restart"] = {"ok": True, "method": "process_exit", "will_exit": True}
            schedule_exit_if_needed(out["restart"])
        return out

    if restart:
        if on_host or os.environ.get("BOTHOST_AGENT_URL"):
            out["restart"] = bothost_self_restart()
            out["restart"]["will_exit"] = True
            if not out["restart"].get("method"):
                out["restart"]["method"] = "process_exit"
            schedule_exit_if_needed(out["restart"])
        else:
            # локально тоже можно exit-ить, если FORCE_EXIT=1
            if (os.environ.get("FORCE_EXIT") or "").strip() in ("1", "true", "yes"):
                out["restart"] = {"ok": True, "method": "process_exit", "will_exit": True}
                schedule_exit_if_needed(out["restart"])
            else:
                out["restart"] = {
                    "ok": False,
                    "skipped": True,
                    "reason": "not on Bothost (no BOT_ID)",
                }
    out["ok"] = True
    return out
