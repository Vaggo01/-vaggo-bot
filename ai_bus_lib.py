# -*- coding: utf-8 -*-
"""
Обратный AI-bus без туннеля (Bothost ↔ домашний ПК).

Идея: оба конца ходят ТОЛЬКО наружу (HTTPS), URL туннеля не нужен.
  • Bothost публикует задачу на ntfy.sh
  • ПК (ai_worker.py) слушает, гоняет Grok Super-сессию, публикует ответ
  • Bothost коротко поллит результат

Топики = секрет из grok_bridge_secret (тот же, что для моста).
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

import requests

NTFY_BASE = (os.environ.get("VAGGO_NTFY_BASE") or "https://ntfy.sh").rstrip("/")
DEFAULT_SECRET = "ftW0PH-ZJQOaeXvFuL2mu0lEFIPsremU"


def _secret(cfg: dict | None = None) -> str:
    cfg = cfg or {}
    return (
        (cfg.get("grok_bridge_secret") or "").strip()
        or (os.environ.get("GROK_BRIDGE_SECRET") or "").strip()
        or (os.environ.get("VAGGO_BUS_SECRET") or "").strip()
        or DEFAULT_SECRET
    )


def topics(cfg: dict | None = None) -> tuple[str, str, str]:
    """(jobs_topic, results_topic, heartbeat_topic)."""
    s = _secret(cfg)
    # короткий стабильный хвост (не светим весь secret в URL-логах целиком)
    tail = s.replace("-", "")[:20]
    return f"vgg-job-{tail}", f"vgg-res-{tail}", f"vgg-hb-{tail}"


def worker_online(cfg: dict | None = None, *, max_age: float = 120.0) -> bool:
    """
    Worker шлёт heartbeat на ntfy. Если свежий — можно ждать Grok через bus.
    Без heartbeat не ждём 40с впустую на Bothost.
    """
    try:
        _, _, hb = topics(cfg)
        r = requests.get(
            f"{NTFY_BASE}/{hb}/json",
            params={"poll": "1", "since": "all"},
            timeout=6,
            headers={"Accept": "application/x-ndjson, application/json"},
        )
        if not r.ok or not r.content:
            return False
        newest = 0.0
        for line in r.content.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if str(msg.get("event") or "message") not in ("message", ""):
                continue
            ts = float(msg.get("time") or 0)
            if ts > newest:
                newest = ts
            raw = str(msg.get("message") or "").lower()
            if "online" in raw or "ok" in raw:
                if ts and (time.time() - ts) <= max_age:
                    return True
        if newest and (time.time() - newest) <= max_age:
            return True
    except Exception as e:
        print("ai_bus worker_online", type(e).__name__, str(e)[:60], flush=True)
    return False


def publish_heartbeat(cfg: dict | None = None) -> None:
    try:
        _, _, hb = topics(cfg)
        requests.post(
            f"{NTFY_BASE}/{hb}",
            data=f"online {int(time.time())}".encode("utf-8"),
            headers={"Title": "worker-hb", "Priority": "min", "Tags": "green_circle"},
            timeout=8,
        )
    except Exception as e:
        print("ai_bus hb fail", e, flush=True)


def bus_enabled(cfg: dict | None = None) -> bool:
    """Выключить: VAGGO_BUS_DISABLE=1 или config ai_bus_disable."""
    cfg = cfg or {}
    if (os.environ.get("VAGGO_BUS_DISABLE") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return False
    if cfg.get("ai_bus_disable"):
        return False
    # на самом worker / bridge не шлём задачи в bus (рекурсия)
    if (os.environ.get("VAGGO_AI_WORKER") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return False
    if (os.environ.get("VAGGO_IS_BRIDGE") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return False
    return True


def publish_job(cfg: dict | None, payload: dict[str, Any]) -> str:
    """Опубликовать задачу. Возвращает job_id."""
    job_id = str(payload.get("id") or uuid.uuid4().hex[:16])
    payload = dict(payload)
    payload["id"] = job_id
    payload["ts"] = time.time()
    jobs, _, _ = topics(cfg)
    body = json.dumps(payload, ensure_ascii=False)
    if len(body) > 60000:
        # урезаем user/system
        for k in ("user", "system"):
            if k in payload and isinstance(payload[k], str):
                payload[k] = payload[k][:12000]
        body = json.dumps(payload, ensure_ascii=False)[:60000]
    r = requests.post(
        f"{NTFY_BASE}/{jobs}",
        data=body.encode("utf-8"),
        headers={
            "Title": f"job:{job_id}",
            "Priority": "default",
            "Tags": "computer",
            "Content-Type": "text/plain; charset=utf-8",
        },
        timeout=15,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"ntfy publish job {r.status_code}: {r.text[:120]}")
    return job_id


def wait_result(
    cfg: dict | None,
    job_id: str,
    *,
    timeout: float = 40.0,
    poll_every: float = 1.2,
) -> str:
    """Ждать ответ worker'а. Пустая строка = таймаут."""
    _, res, _ = topics(cfg)
    deadline = time.time() + max(5.0, timeout)
    # since=all на первом запросе — вдруг ответ уже есть
    since = "all"
    seen: set[str] = set()
    while time.time() < deadline:
        try:
            r = requests.get(
                f"{NTFY_BASE}/{res}/json",
                params={"poll": "1", "since": since},
                timeout=12,
                headers={"Accept": "application/x-ndjson, application/json, text/plain"},
            )
            if r.ok and r.content:
                text = r.content.decode("utf-8", errors="replace").strip()
                for line in text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except Exception:
                        continue
                    mid = str(msg.get("id") or "")
                    if mid and mid in seen:
                        continue
                    if mid:
                        seen.add(mid)
                    event = str(msg.get("event") or "")
                    if event and event != "message":
                        continue
                    raw = str(msg.get("message") or msg.get("title") or "").strip()
                    if not raw:
                        continue
                    try:
                        data = json.loads(raw)
                    except Exception:
                        # title: res:JOBID
                        title = str(msg.get("title") or "")
                        if job_id in title and raw:
                            return raw
                        continue
                    if str(data.get("id") or "") != job_id:
                        continue
                    if data.get("ok") is False:
                        err = str(data.get("error") or "worker fail")
                        raise RuntimeError(err)
                    out = str(data.get("text") or "").strip()
                    if out:
                        return out
            # следующие опросы — только новое
            since = str(int(time.time()) - 2)
        except RuntimeError:
            raise
        except Exception as e:
            print("ai_bus wait poll", type(e).__name__, str(e)[:80], flush=True)
        time.sleep(poll_every)
    return ""


def bus_chat(
    cfg: dict | None,
    system: str,
    user: str,
    *,
    model: str | None = None,
    temperature: float = 0.55,
    tools: bool = False,
    max_tokens: int | None = None,
    timeout: float = 45.0,
) -> str:
    """Синхронный чат через bus (Bothost)."""
    if not bus_enabled(cfg):
        raise RuntimeError("ai bus disabled")
    if not worker_online(cfg):
        raise RuntimeError("ai worker offline (no heartbeat)")
    job_id = publish_job(
        cfg,
        {
            "type": "chat",
            "system": system or "",
            "user": user or "",
            "model": model,
            "temperature": temperature,
            "tools": bool(tools),
            "max_tokens": max_tokens,
        },
    )
    text = wait_result(cfg, job_id, timeout=timeout)
    if not text:
        raise RuntimeError(f"ai bus timeout job={job_id}")
    return text


def publish_result(cfg: dict | None, job_id: str, text: str, *, error: str = "") -> None:
    _, res, _ = topics(cfg)
    payload = {
        "id": job_id,
        "ok": not bool(error),
        "text": (text or "")[:50000],
        "error": (error or "")[:500],
        "ts": time.time(),
    }
    body = json.dumps(payload, ensure_ascii=False)
    r = requests.post(
        f"{NTFY_BASE}/{res}",
        data=body.encode("utf-8"),
        headers={
            "Title": f"res:{job_id}",
            "Priority": "default",
            "Tags": "white_check_mark" if not error else "x",
            "Content-Type": "text/plain; charset=utf-8",
        },
        timeout=15,
    )
    if r.status_code >= 400:
        print("ai_bus publish result fail", r.status_code, r.text[:100], flush=True)


def fetch_jobs(cfg: dict | None, *, since: str = "all", timeout: float = 25.0) -> list[dict]:
    """Worker: забрать задачи (long-poll)."""
    jobs, _, _ = topics(cfg)
    try:
        r = requests.get(
            f"{NTFY_BASE}/{jobs}/json",
            params={"poll": "1", "since": since},
            timeout=timeout,
            headers={"Accept": "application/x-ndjson, application/json"},
        )
    except Exception as e:
        print("ai_bus fetch_jobs", type(e).__name__, str(e)[:80], flush=True)
        return []
    out: list[dict] = []
    if not r.ok or not r.content:
        return out
    for line in r.content.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if str(msg.get("event") or "message") not in ("message", ""):
            continue
        raw = str(msg.get("message") or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        data["_ntfy_id"] = str(msg.get("id") or "")
        out.append(data)
    return out


def probe_bus(cfg: dict | None = None) -> dict[str, Any]:
    """Лёгкая проверка, что ntfy доступен (не значит, что worker online)."""
    jobs, res, hb = topics(cfg)
    try:
        r = requests.head(f"{NTFY_BASE}/{jobs}", timeout=5)
        ok = r.status_code < 500
    except Exception as e:
        return {"ok": False, "error": str(e)[:80], "jobs": jobs, "res": res, "hb": hb}
    return {
        "ok": ok,
        "jobs": jobs,
        "res": res,
        "hb": hb,
        "base": NTFY_BASE,
        "worker": worker_online(cfg),
    }
