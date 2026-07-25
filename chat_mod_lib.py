# -*- coding: utf-8 -*-
"""
Модератор чата обсуждений @Vaggo01.

Лестница:
  1) токсик (решает ИИ, не словарь) → удалить + предупреждение
  2) 4 предупреждения → мут 1 час
  3) 3 мута → отстранение 1 неделя
  4) 2 недельных → отстранение 2 месяца
  5) после месячного ещё одна «неделя» → полный бан в чате

Хранение: media/chat_mod.json
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
PATH = ROOT / "media" / "chat_mod.json"
_LOCK = threading.Lock()

HOUR = 3600
WEEK = 7 * 24 * HOUR
MONTH2 = 60 * 24 * HOUR  # ~2 месяца

MOD_SYSTEM = """Ты модератор Telegram-чата канала «Вагго» (ИИ, технологии, дружеский тон).

Задача: решить, удалять ли сообщение как токсичное.

УДАЛЯТЬ (bad), если есть:
- оскорбления человека/группы, унижение, травля;
- пожелания смерти/болезни («умри», «сдохни» и смысл);
- разжигание ссоры, агрессия ради конфликта;
- тяжёлый мат направленный на человека (не лёгкий сленг в обсуждении темы);
- спам-ненависть, угрозы.

НЕ удалять (ok):
- спор по теме, критика поста/ИИ/моделей;
- сарказм без травли;
- лёгкий мат не в адрес человека;
- шутки, мемы, вопросы, оффтоп без яда;
- «не согласен», «бред пост» без оскорбления людей.

Ответ СТРОГО одна строка JSON без markdown:
{"bad":true/false,"reason":"insult|fight|neg|threat|ok","note":"3-6 слов"}
reason=ok только если bad=false."""


def _default() -> dict:
    return {"users": {}, "log": [], "updated_at": 0}


def load() -> dict:
    with _LOCK:
        if not PATH.exists():
            data = _default()
            PATH.parent.mkdir(parents=True, exist_ok=True)
            PATH.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return data
        try:
            data = json.loads(PATH.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return _default()
            data.setdefault("users", {})
            data.setdefault("log", [])
            return data
        except Exception:
            return _default()


def save(data: dict) -> None:
    with _LOCK:
        data["updated_at"] = int(time.time())
        data["log"] = (data.get("log") or [])[:400]
        PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(PATH)


def _user(data: dict, uid: int) -> dict:
    key = str(int(uid))
    u = data.setdefault("users", {}).setdefault(
        key,
        {
            "user_id": int(uid),
            "warnings": 0,
            "mutes": 0,
            "week_bans": 0,
            "had_month_ban": False,
            "permanent": False,
            "username": "",
            "name": "",
            "until": 0,
            "status": "ok",  # ok|muted|week|month|banned
        },
    )
    return u


def is_chat_banned(user_id: int) -> bool:
    u = (load().get("users") or {}).get(str(int(user_id))) or {}
    return bool(u.get("permanent"))


REASON_RU = {
    "insult": "оскорбления",
    "fight": "конфликт / травля",
    "neg": "агрессия / негатив",
    "threat": "угрозы",
    "ok": "ок",
}


def detect_toxicity(text: str, *, cfg: dict | None = None) -> str | None:
    """
    ИИ-модерация (Grok). Не словарь слов.
    None = чисто; иначе reason: insult|fight|neg|threat.
    """
    t = (text or "").strip()
    if not t or len(t) < 1:
        return None
    # слишком длинное — режем для классификатора
    sample = t[:800]
    try:
        from content import grok_chat, grok_ok, llm_chat
        from state import load_config

        cfg = cfg or load_config()
        user = (
            f"Сообщение в комментариях канала:\n«{sample}»\n\n"
            f"Классифицируй. JSON одной строкой."
        )
        if grok_ok(cfg):
            model = (
                cfg.get("grok_fast_model")
                or cfg.get("grok_model")
                or "grok-4.5"
            )
            raw = grok_chat(
                cfg,
                MOD_SYSTEM,
                user,
                model=model,
                temperature=0.1,
                tools=False,
                max_tokens=80,
            )
        else:
            raw, _ = llm_chat(
                cfg,
                MOD_SYSTEM,
                user,
                temperature=0.1,
                prefer_fast=True,
                tools=False,
                max_tokens=80,
            )
        return _parse_mod_verdict(raw)
    except Exception as e:
        print("chatmod AI fail", type(e).__name__, str(e)[:120], flush=True)
        # без ИИ не баним «на всякий» — только явный яд fallback
        return _fallback_extreme(sample)


def _parse_mod_verdict(raw: str) -> str | None:
    s = (raw or "").strip()
    if not s:
        return None
    # вытащить JSON
    m = re.search(r"\{[^{}]+\}", s, re.S)
    blob = m.group(0) if m else s
    try:
        data = json.loads(blob)
    except Exception:
        low = s.lower()
        if any(x in low for x in ('"bad": true', "bad:true", "bad: true")):
            return "neg"
        if low.startswith("bad") or "удал" in low:
            return "neg"
        return None
    if not isinstance(data, dict):
        return None
    bad = data.get("bad")
    if bad is True or str(bad).lower() in ("1", "true", "yes", "да"):
        reason = str(data.get("reason") or "neg").lower().strip()
        if reason in ("ok", "clean", "fine", "none"):
            reason = "neg"
        if reason not in REASON_RU:
            reason = "neg"
        return reason
    return None


def _fallback_extreme(text: str) -> str | None:
    """Только если Grok недоступен — крайний минимум, не основной режим."""
    low = (text or "").lower()
    extreme = (
        "умри",
        "сдохни",
        "убей себя",
        "нахуй иди",
        "иди нахуй",
        "пидор",
        "педик",
        "уёбок",
        "уебок",
    )
    if any(x in low for x in extreme):
        return "insult"
    return None


def process_offense(
    user_id: int,
    *,
    reason: str = "insult",
    username: str = "",
    name: str = "",
    snippet: str = "",
) -> dict[str, Any]:
    """
    Учесть нарушение и вернуть действие для Telegram.

    return {
      action: warn|mute|week|month|ban,
      warnings, mutes, week_bans,
      until: unix or 0,
      public_text, private_text,
      user: {...}
    }
    """
    data = load()
    u = _user(data, user_id)
    if username:
        u["username"] = username.lstrip("@")
    if name:
        u["name"] = name[:80]

    if u.get("permanent"):
        return {
            "action": "ban",
            "already": True,
            "warnings": int(u.get("warnings") or 0),
            "mutes": int(u.get("mutes") or 0),
            "week_bans": int(u.get("week_bans") or 0),
            "until": 0,
            "public_text": "⛔ Ты в бане чата.",
            "private_text": "Полный бан в чате обсуждений. Снять может только владелец.",
            "user": dict(u),
            "reason": reason,
        }

    now = int(time.time())
    u["warnings"] = int(u.get("warnings") or 0) + 1
    reason_h = REASON_RU.get(reason, "нарушение")
    action = "warn"
    until = 0
    note = ""

    # 4 предупреждения → мут 1ч
    if u["warnings"] >= 4:
        u["warnings"] = 0
        u["mutes"] = int(u.get("mutes") or 0) + 1
        action = "mute"
        until = now + HOUR
        u["until"] = until
        u["status"] = "muted"
        note = f"мут 1 час (мутов: {u['mutes']}/3)"

        # 3 мута → неделя
        if u["mutes"] >= 3:
            u["mutes"] = 0
            u["week_bans"] = int(u.get("week_bans") or 0) + 1
            # после месячного ещё одна неделя → пермач
            if u.get("had_month_ban"):
                action = "ban"
                until = 0
                u["permanent"] = True
                u["status"] = "banned"
                u["until"] = 0
                note = "полный бан (после 2 мес. + ещё неделя)"
            elif u["week_bans"] >= 2:
                # 2 недельных → 2 месяца
                action = "month"
                until = now + MONTH2
                u["until"] = until
                u["status"] = "month"
                u["had_month_ban"] = True
                u["week_bans"] = 0
                note = "отстранение 2 месяца"
            else:
                action = "week"
                until = now + WEEK
                u["until"] = until
                u["status"] = "week"
                note = f"отстранение 1 неделя (нед.: {u['week_bans']}/2)"

    data.setdefault("log", []).insert(
        0,
        {
            "ts": now,
            "user_id": int(user_id),
            "action": action,
            "reason": reason,
            "snippet": (snippet or "")[:200],
            "warnings": u["warnings"],
            "mutes": u["mutes"],
            "week_bans": u["week_bans"],
            "until": until,
        },
    )
    save(data)

    w, m, wb = u["warnings"], u["mutes"], u["week_bans"]
    who = f"@{u['username']}" if u.get("username") else (u.get("name") or str(user_id))

    if action == "warn":
        pub = (
            f"⚠️ {who}, предупреждение ({w}/4) — {reason_h}.\n"
            f"4 предупреждения = мут на час."
        )
        priv = (
            f"⚠️ Предупреждение {w}/4 в чате Vaggo ({reason_h}).\n"
            f"4 шт → мут 1 час · 3 мута → неделя · дальше жёстче."
        )
    elif action == "mute":
        pub = f"🔇 {who}: мут 1 час. ({note})"
        priv = f"🔇 Мут в чате Vaggo на 1 час.\n{note}\n3 мута → отстранение на неделю."
    elif action == "week":
        pub = f"⏳ {who}: отстранение 1 неделя. ({note})"
        priv = f"⏳ Отстранение от чата на 1 неделю.\n{note}"
    elif action == "month":
        pub = f"🚫 {who}: отстранение 2 месяца."
        priv = (
            "🚫 Отстранение от чата на 2 месяца.\n"
            "Ещё одна «неделя» после этого = полный бан."
        )
    else:
        pub = f"⛔ {who}: полный бан в чате."
        priv = "⛔ Полный бан в чате обсуждений Vaggo."

    return {
        "action": action,
        "already": False,
        "warnings": w,
        "mutes": m,
        "week_bans": wb,
        "until": until,
        "public_text": pub,
        "private_text": priv,
        "user": dict(u),
        "reason": reason,
        "reason_h": reason_h,
    }


def owner_pardon(user_id: int) -> dict | None:
    """Сброс наказаний (владелец)."""
    data = load()
    key = str(int(user_id))
    u = (data.get("users") or {}).get(key)
    if not u:
        return None
    u.update(
        {
            "warnings": 0,
            "mutes": 0,
            "week_bans": 0,
            "had_month_ban": False,
            "permanent": False,
            "until": 0,
            "status": "ok",
        }
    )
    data.setdefault("log", []).insert(
        0, {"ts": int(time.time()), "user_id": int(user_id), "action": "pardon"}
    )
    save(data)
    return dict(u)


def status_line(user_id: int) -> str:
    u = (load().get("users") or {}).get(str(int(user_id))) or {}
    if not u:
        return "чисто"
    if u.get("permanent"):
        return "⛔ бан"
    return (
        f"⚠ {u.get('warnings') or 0}/4 · "
        f"мут {u.get('mutes') or 0}/3 · "
        f"нед {u.get('week_bans') or 0}/2 · "
        f"{'был 2мес' if u.get('had_month_ban') else 'ок'}"
    )
