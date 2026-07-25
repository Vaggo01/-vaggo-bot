"""Состояние: черновики, очередь комментов, пауза."""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.json"
CONFIG_PATH = ROOT / "config.json"
_STATE_LOCK = threading.Lock()


# Жёсткие дефолты Вагго (облако Bothost часто без полного config.json)
DEFAULT_OWNER_IDS = [5740061551]
DEFAULT_OWNER_USERNAMES = ["vagdar1"]
DEFAULT_CHANNEL_ID = "@Vaggo01"
DEFAULT_CHANNEL_USERNAME = "Vaggo01"
DEFAULT_CHANNEL_NUMERIC = -1004445937686
# linked discussion for @Vaggo01 (комменты канала)
DEFAULT_DISCUSSION_GROUP_ID = -1004496516466


def load_config() -> dict:
    """
    config.json + оверлей из env (удобно для 24/7 в облаке):
      BOT_TOKEN / TELEGRAM_BOT_TOKEN
      CHANNEL_ID, CHANNEL_USERNAME, DISCUSSION_GROUP_ID
      OWNER_USER_IDS  (через запятую: 123,456)
      PROXY_URL
      GROK_BRIDGE_URL / GROK_BRIDGE_SECRET / XAI_API_KEY
    """
    import os

    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        # cloud: можно без файла, только env
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}

    token = (os.environ.get("BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if token:
        cfg["bot_token"] = token
    ch = (os.environ.get("CHANNEL_ID") or "").strip()
    if ch:
        cfg["channel_id"] = ch
    cun = (os.environ.get("CHANNEL_USERNAME") or "").strip()
    if cun:
        cfg["channel_username"] = cun.lstrip("@")
    owners = (os.environ.get("OWNER_USER_IDS") or "").strip()
    if owners:
        ids = []
        for p in owners.replace(";", ",").split(","):
            p = p.strip()
            if p.isdigit():
                ids.append(int(p))
        if ids:
            cfg["owner_user_ids"] = ids
    proxy = (os.environ.get("PROXY_URL") or "").strip()
    if proxy:
        cfg["proxy_url"] = proxy
    # discussion group (комменты) — критично для Bothost без полного config.json
    disc_env = (
        os.environ.get("DISCUSSION_GROUP_ID")
        or os.environ.get("DISCUSSION_ID")
        or os.environ.get("GROUP_ID")
        or ""
    ).strip()
    if disc_env:
        try:
            cfg["discussion_group_id"] = int(disc_env)
        except Exception:
            pass
    # Grok Super bridge (домашний ПК)
    bru = (os.environ.get("GROK_BRIDGE_URL") or "").strip()
    if bru:
        cfg["grok_bridge_url"] = bru.rstrip("/")
    bsec = (os.environ.get("GROK_BRIDGE_SECRET") or "").strip()
    if bsec:
        cfg["grok_bridge_secret"] = bsec
    bdisc = (os.environ.get("GROK_BRIDGE_DISCOVERY") or "").strip()
    if bdisc:
        cfg["grok_bridge_discovery"] = bdisc
    xai = (os.environ.get("XAI_API_KEY") or os.environ.get("GROK_API_KEY") or "").strip()
    if xai:
        cfg["xai_api_key"] = xai

    # host / brain overrides (Bothost)
    bhm = (os.environ.get("BOT_HOST_MODE") or os.environ.get("HOST_MODE") or "").strip().lower()
    if bhm:
        cfg["bot_host_mode"] = bhm
    elif (os.environ.get("BOT_ID") or "").strip() or (
        os.environ.get("BOTHOST") or os.environ.get("BOTHHOST") or ""
    ).strip():
        # Bothost обычно ставит BOT_ID — включаем cloud-режим автоматически
        if not str(cfg.get("bot_host_mode") or "").strip():
            cfg["bot_host_mode"] = "cloud"
        elif str(cfg.get("bot_host_mode")).lower() == "local" and (
            os.environ.get("BOT_ID") or ""
        ).strip():
            cfg["bot_host_mode"] = "cloud"
    brain_env = (os.environ.get("BRAIN") or "").strip().lower()
    if brain_env:
        cfg["brain"] = brain_env
    if (os.environ.get("GROK_BRIDGE_DISABLE") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        cfg["grok_bridge_disable"] = True
    ugs = (os.environ.get("USE_GROK_SESSION") or "").strip().lower()
    if ugs in ("0", "false", "no", "off"):
        cfg["use_grok_session"] = False
    elif ugs in ("1", "true", "yes", "on"):
        cfg["use_grok_session"] = True

    # --- дефолты: владелец / канал (чтобы в облаке не «все юзеры» и не чужой юз) ---
    oids = cfg.get("owner_user_ids") or []
    if not isinstance(oids, list):
        oids = []
    oids = [int(x) for x in oids if str(x).lstrip("-").isdigit()]
    if not oids:
        oids = list(DEFAULT_OWNER_IDS)
    # всегда держим канонического владельца
    for oid in DEFAULT_OWNER_IDS:
        if oid not in oids:
            oids.insert(0, oid)
    cfg["owner_user_ids"] = oids

    onames = cfg.get("owner_usernames") or []
    if not isinstance(onames, list):
        onames = []
    onames = [str(n).lstrip("@").lower() for n in onames if n]
    for n in DEFAULT_OWNER_USERNAMES:
        if n not in onames:
            onames.append(n)
    cfg["owner_usernames"] = onames

    # канал: никогда не оставляем пустым / «как у человека»
    ch_id = str(cfg.get("channel_id") or "").strip()
    ch_un = str(cfg.get("channel_username") or "").strip().lstrip("@")
    # если CHANNEL_ID похож на user-id (положительное число) — это ошибка, сбрасываем
    if ch_id.lstrip("-").isdigit() and not ch_id.startswith("-100"):
        ch_id = ""
    if not ch_id or ch_id in ("0", "None"):
        ch_id = DEFAULT_CHANNEL_ID
    if not ch_un:
        ch_un = DEFAULT_CHANNEL_USERNAME
    # юзернейм канала только Vaggo01 (не путать с личным @vagdar1)
    if ch_un.lower() in {n.lower() for n in DEFAULT_OWNER_USERNAMES}:
        ch_un = DEFAULT_CHANNEL_USERNAME
        ch_id = DEFAULT_CHANNEL_ID
    cfg["channel_id"] = ch_id if ch_id.startswith("@") or ch_id.startswith("-") else f"@{ch_id}"
    cfg["channel_username"] = ch_un
    try:
        cfg["channel_numeric_id"] = int(
            cfg.get("channel_numeric_id") or DEFAULT_CHANNEL_NUMERIC
        )
    except Exception:
        cfg["channel_numeric_id"] = DEFAULT_CHANNEL_NUMERIC

    # discussion group — без этого Bothost видит ЛС, но МОЛЧИТ в комментах
    try:
        disc = int(cfg.get("discussion_group_id") or 0)
    except Exception:
        disc = 0
    if not disc:
        disc = DEFAULT_DISCUSSION_GROUP_ID
    cfg["discussion_group_id"] = disc

    # канальные AI-фичи: на cloud без config.json не выключать «случайно»
    for k, default in (
        ("auto_reply_comments", True),
        ("auto_react_comments", True),
        ("auto_react_posts", True),
        ("auto_seed_comment", True),
        ("comment_needs_owner_ok", False),
        ("chat_mod_enabled", True),
        ("paused", False),
        ("use_grok_session", True),
        ("brain", "auto"),
        ("grok_tools", True),
        ("grok_web_search", True),
        ("grok_x_search", True),
    ):
        if k not in cfg or cfg.get(k) is None:
            cfg[k] = default

    # secret для bridge — env → config → дефолт (совпадает с home bridge)
    if not (cfg.get("grok_bridge_secret") or "").strip():
        env_sec = (os.environ.get("GROK_BRIDGE_SECRET") or "").strip()
        if env_sec:
            cfg["grok_bridge_secret"] = env_sec
        else:
            cfg["grok_bridge_secret"] = "ftW0PH-ZJQOaeXvFuL2mu0lEFIPsremU"

    # cloud: если URL моста пуст — Bothost сам подхватит через discovery GitHub
    if str(cfg.get("bot_host_mode") or "").lower() in ("cloud", "bothost", "hosting"):
        if cfg.get("grok_bridge_disable") is None:
            cfg["grok_bridge_disable"] = False

    if not (cfg.get("bot_token") or "").strip():
        raise FileNotFoundError(
            f"Нет bot_token: положи в {CONFIG_PATH} или env BOT_TOKEN"
        )
    return cfg


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _default_state() -> dict:
    return {
        "offset": 0,
        "drafts": [],
        "pending_comments": [],
        "published": [],
        "last_error": "",
        "updated_at": 0,
    }


def _read_state_unlocked() -> dict:
    if not STATE_PATH.exists():
        return _default_state()
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return _default_state()
    base = _default_state()
    base.update(data)
    return base


def load_state() -> dict:
    with _STATE_LOCK:
        st = _read_state_unlocked()
        if not STATE_PATH.exists():
            st["updated_at"] = int(time.time())
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False, indent=2)
        return st


def _merge_entry(a: dict, b: dict) -> dict:
    """Склеить двух участников: OR по флагам, union инвайтов."""
    out = {**a, **b}
    # excluded=True побеждает (тест/владелец не «воскресает»)
    if a.get("excluded") or b.get("excluded"):
        out["excluded"] = True
        out["complete"] = False
        out["exclude_reason"] = (
            b.get("exclude_reason") or a.get("exclude_reason") or "excluded"
        )
        inv = list(a.get("invites") or [])
        for x in b.get("invites") or []:
            if x not in inv:
                inv.append(x)
        out["invites"] = inv
        return out
    out["subs_ok"] = bool(a.get("subs_ok") or b.get("subs_ok"))
    out["repost_ok"] = bool(a.get("repost_ok") or b.get("repost_ok"))
    out["complete"] = bool(a.get("complete") or b.get("complete"))
    inv = list(a.get("invites") or [])
    for x in b.get("invites") or []:
        if x not in inv:
            inv.append(x)
    out["invites"] = inv
    if b.get("repost_proof_file_id"):
        out["repost_proof_file_id"] = b.get("repost_proof_file_id")
    elif a.get("repost_proof_file_id"):
        out["repost_proof_file_id"] = a.get("repost_proof_file_id")
    # имя/username — свежее непустое
    if b.get("username"):
        out["username"] = b["username"]
    if b.get("name"):
        out["name"] = b["name"]
    if out["complete"]:
        out["completed_at"] = b.get("completed_at") or a.get("completed_at") or int(time.time())
    return out


def _merge_giveaway_item(a: dict, b: dict) -> dict:
    """Память (b) + диск (a): не теряем entries и active."""
    out = {**a, **b}
    ea = a.get("entries") or {}
    eb = b.get("entries") or {}
    if not isinstance(ea, dict):
        ea = {}
    if not isinstance(eb, dict):
        eb = {}
    merged_e: dict = {}
    for uid in set(ea) | set(eb):
        if uid in ea and uid in eb:
            merged_e[uid] = _merge_entry(ea[uid], eb[uid])
        else:
            merged_e[uid] = eb.get(uid) or ea.get(uid)
    out["entries"] = merged_e
    # статус: ended/cancelled не откатываем в active/draft
    rank = {"cancelled": 3, "ended": 3, "active": 2, "draft": 1}
    sa, sb = a.get("status") or "draft", b.get("status") or "draft"
    out["status"] = sb if rank.get(sb, 0) >= rank.get(sa, 0) else sa
    # id поста — любой ненулевой
    out["channel_message_id"] = b.get("channel_message_id") or a.get("channel_message_id")
    out["ends_at"] = b.get("ends_at") or a.get("ends_at")
    if b.get("winners_list"):
        out["winners_list"] = b["winners_list"]
    if b.get("winner"):
        out["winner"] = b["winner"]
    return out


def _merge_giveaways(disk_g: Any, mem_g: Any) -> dict:
    d = disk_g if isinstance(disk_g, dict) else {}
    m = mem_g if isinstance(mem_g, dict) else {}
    di = d.get("items") if isinstance(d.get("items"), dict) else {}
    mi = m.get("items") if isinstance(m.get("items"), dict) else {}
    items: dict = {}
    for gid in set(di) | set(mi):
        if gid in di and gid in mi:
            items[gid] = _merge_giveaway_item(di[gid], mi[gid])
        else:
            items[gid] = mi.get(gid) or di.get(gid)
    # active_id: предпочитаем тот, у кого item.status==active
    candidates = [m.get("active_id"), d.get("active_id")]
    active = None
    for cand in candidates:
        if cand and str(cand) in items and items[str(cand)].get("status") == "active":
            active = str(cand)
            break
    if not active:
        for gid, it in items.items():
            if it.get("status") == "active":
                active = str(gid)
                break
    return {"items": items, "active_id": active}


def _merge_orders(disk_o: Any, mem_o: Any) -> dict:
    """Не затирать заказы при save offset из polling."""
    d = disk_o if isinstance(disk_o, dict) else {}
    m = mem_o if isinstance(mem_o, dict) else {}
    di = d.get("items") if isinstance(d.get("items"), dict) else {}
    mi = m.get("items") if isinstance(m.get("items"), dict) else {}
    items: dict = {}
    for oid in set(di) | set(mi):
        a, b = di.get(oid), mi.get(oid)
        if a and b:
            # новее updated_at / status rank
            ta = int(a.get("updated_at") or a.get("created_at") or 0)
            tb = int(b.get("updated_at") or b.get("created_at") or 0)
            rank = {
                "cancelled": 1,
                "new": 2,
                "accepted": 3,
                "in_progress": 4,
                "done": 5,
            }
            ra = rank.get(str(a.get("status")), 0)
            rb = rank.get(str(b.get("status")), 0)
            if rb > ra or (rb == ra and tb >= ta):
                items[oid] = {**a, **b}
            else:
                items[oid] = {**b, **a}
            # file_id / note — любой непустой
            if b.get("result_file_id"):
                items[oid]["result_file_id"] = b["result_file_id"]
            elif a.get("result_file_id"):
                items[oid]["result_file_id"] = a["result_file_id"]
        else:
            items[oid] = b or a
    free_used = max(int(d.get("free_used") or 0), int(m.get("free_used") or 0))
    # free_used не меньше числа free-заказов
    free_n = sum(1 for x in items.values() if x.get("is_free"))
    free_used = max(free_used, free_n)
    return {"items": items, "free_used": free_used}


def save_state(state: dict) -> None:
    """Атомарная запись + merge giveaways/orders, чтобы polling не затирал данные."""
    with _STATE_LOCK:
        disk = _read_state_unlocked()
        state["giveaways"] = _merge_giveaways(disk.get("giveaways"), state.get("giveaways"))
        state["orders"] = _merge_orders(disk.get("orders"), state.get("orders"))
        # order_draft: union по user id
        dd = disk.get("order_draft") if isinstance(disk.get("order_draft"), dict) else {}
        md = state.get("order_draft") if isinstance(state.get("order_draft"), dict) else {}
        state["order_draft"] = {**dd, **md}
        # await_order_deliver — memory wins if set
        if not state.get("await_order_deliver") and disk.get("await_order_deliver"):
            state["await_order_deliver"] = disk.get("await_order_deliver")
        # offset — всегда берём max (не откатываем polling)
        try:
            state["offset"] = max(int(state.get("offset") or 0), int(disk.get("offset") or 0))
        except Exception:
            pass
        state["updated_at"] = int(time.time())
        tmp = STATE_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        tmp.replace(STATE_PATH)


def new_id() -> str:
    return uuid.uuid4().hex[:10]


def add_draft(state: dict, text: str, *, rubric: str = "", source: str = "owner") -> dict:
    item = {
        "id": new_id(),
        "text": text.strip(),
        "rubric": rubric,
        "source": source,
        "created_at": int(time.time()),
        "status": "draft",
    }
    state.setdefault("drafts", []).insert(0, item)
    # keep last 50
    state["drafts"] = state["drafts"][:50]
    save_state(state)
    return item


def get_draft(state: dict, draft_id: str | None = None) -> dict | None:
    drafts = state.get("drafts") or []
    if not drafts:
        return None
    if draft_id:
        for d in drafts:
            if d.get("id") == draft_id:
                return d
        return None
    for d in drafts:
        if d.get("status") == "draft":
            return d
    return drafts[0]


def mark_published(state: dict, draft: dict, message_id: Any = None) -> None:
    draft["status"] = "published"
    draft["published_at"] = int(time.time())
    draft["channel_message_id"] = message_id
    state.setdefault("published", []).insert(0, {
        "id": draft.get("id"),
        "text_preview": (draft.get("text") or "")[:120],
        "published_at": draft["published_at"],
        "channel_message_id": message_id,
    })
    state["published"] = state["published"][:100]
    save_state(state)


def add_pending_comment(state: dict, payload: dict) -> dict:
    item = {
        "id": new_id(),
        "created_at": int(time.time()),
        "status": "pending",
        **payload,
    }
    state.setdefault("pending_comments", []).insert(0, item)
    state["pending_comments"] = state["pending_comments"][:80]
    save_state(state)
    return item
