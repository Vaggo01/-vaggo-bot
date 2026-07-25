# -*- coding: utf-8 -*-
"""
Модерация ТЗ + блок пользователей.
Незаконное / запрещённое ТЗ → мгновенный блок, снять — владелец (бот/пульт).
Хранение: media/user_blocks.json
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
PATH = ROOT / "media" / "user_blocks.json"
_LOCK = threading.Lock()

# Категории для логов/UI
CAT_LABELS = {
    "csam": "CSAM / дети",
    "hack": "взлом / доступ",
    "malware": "вредонос / стиллеры",
    "fraud": "мошенничество / фишинг",
    "drugs": "наркотики",
    "weapons": "оружие / взрывчатка",
    "violence": "насилие / заказное",
    "ddos": "DDoS / атаки",
    "launder": "отмыв / дропы",
    "porn": "порно / 18+ продажа",
    "extremism": "экстремизм / террор",
    "privacy": "пробив / деанон",
    "other": "запрещено правилами",
}

# (фраза, категория) — lower
ILLEGAL_PHRASES: tuple[tuple[str, str], ...] = (
    # --- CSAM ---
    ("детское порно", "csam"),
    ("детский порн", "csam"),
    ("детск порн", "csam"),
    ("порно с детьми", "csam"),
    ("порно с ребен", "csam"),
    ("порно с ребён", "csam"),
    ("порно с дет", "csam"),
    ("порн с детьми", "csam"),
    ("порнуха с дет", "csam"),
    ("child porn", "csam"),
    ("childporn", "csam"),
    ("kids porn", "csam"),
    ("csam", "csam"),
    ("педофил", "csam"),
    ("педоф", "csam"),
    ("лоликон", "csam"),
    ("lolicon", "csam"),
    ("школот порн", "csam"),
    ("школьниц порн", "csam"),
    ("малолетн", "csam"),
    # --- взлом ---
    ("взломать", "hack"),
    ("взломай", "hack"),
    ("взлом акк", "hack"),
    ("взлом аккаунт", "hack"),
    ("хакни", "hack"),
    ("хакнуть", "hack"),
    ("вскрыть аккаунт", "hack"),
    ("вскрыть акк", "hack"),
    ("обойти 2fa", "hack"),
    ("обойти двухфактор", "hack"),
    ("снять 2fa", "hack"),
    ("брутфорс", "hack"),
    ("brute force", "hack"),
    ("sql injection для взлома", "hack"),
    ("украсть парол", "hack"),
    ("спиздить акк", "hack"),
    # --- malware ---
    ("стиллер", "malware"),
    ("стилер", "malware"),
    ("stealer", "malware"),
    ("grabber", "malware"),
    ("граббер", "malware"),
    ("кейлогер", "malware"),
    ("кейлоггер", "malware"),
    ("keylogger", "malware"),
    ("троян", "malware"),
    ("вирус для", "malware"),
    ("майнер без ведома", "malware"),
    ("remote access trojan", "malware"),
    ("бекендор", "malware"),
    ("backdoor", "malware"),
    ("ransomware", "malware"),
    ("шифровальщик", "malware"),
    # --- fraud ---
    ("фишинг", "fraud"),
    ("phishing", "fraud"),
    ("фейковый банк", "fraud"),
    ("фейк банк", "fraud"),
    ("поддельный сайт банка", "fraud"),
    ("скам бот", "fraud"),
    ("скамбот", "fraud"),
    ("скам схема", "fraud"),
    ("развод на деньги", "fraud"),
    ("кинуть людей", "fraud"),
    ("обман людей", "fraud"),
    ("мошенническ", "fraud"),
    ("карта жертвы", "fraud"),
    ("данные карт жертв", "fraud"),
    ("купить cvv", "fraud"),
    ("продать cvv", "fraud"),
    ("дамп карт", "fraud"),
    ("пирамида ммм", "fraud"),
    ("накрутка голосов избират", "fraud"),
    ("фейк документы", "fraud"),
    ("поддельные документы", "fraud"),
    ("липовые документы", "fraud"),
    # --- privacy ---
    ("пробить человека", "privacy"),
    ("пробив ", "privacy"),
    ("пробивка", "privacy"),
    ("деанон", "privacy"),
    ("деанонимиз", "privacy"),
    ("deanonym", "privacy"),
    ("слить базу", "privacy"),
    ("слив базы", "privacy"),
    ("купить базу паспорт", "privacy"),
    # --- drugs ---
    ("закладк", "drugs"),
    ("закладки", "drugs"),
    ("торг нарко", "drugs"),
    ("наркот", "drugs"),
    ("наркомагаз", "drugs"),
    ("мефедрон", "drugs"),
    ("амфетамин", "drugs"),
    ("кокаин", "drugs"),
    ("героин", "drugs"),
    ("спайс ", "drugs"),
    # --- weapons ---
    ("купить ствол", "weapons"),
    ("продать ствол", "weapons"),
    ("оружие без", "weapons"),
    ("изготовление взрыв", "weapons"),
    ("бомбу", "weapons"),
    ("самодельн взрыв", "weapons"),
    ("черный порох инструкция", "weapons"),
    # --- violence ---
    ("заказное убийство", "violence"),
    ("убить человека", "violence"),
    ("нанять киллера", "violence"),
    ("избить человека", "violence"),
    # --- ddos ---
    ("заказать ddos", "ddos"),
    ("заказать ддос", "ddos"),
    ("ддос атака", "ddos"),
    ("ddos атака", "ddos"),
    ("положить сайт", "ddos"),
    ("положить сервер", "ddos"),
    ("ботнет", "ddos"),
    # --- launder ---
    ("отмыв денег", "launder"),
    ("отмыть деньги", "launder"),
    ("отмывание", "launder"),
    ("дроп карты", "launder"),
    ("дропы ", "launder"),
    ("обнал ", "launder"),
    ("обналич", "launder"),
    # --- porn (запрет по правилам сервиса) ---
    ("порно сайт", "porn"),
    ("порносайт", "porn"),
    ("сайт для порно", "porn"),
    ("порнобот", "porn"),
    ("onlyfans бот слив", "porn"),
    ("слив onlyfans", "porn"),
    ("xxx сайт", "porn"),
    ("18+ сайт порн", "porn"),
    # --- extremism ---
    ("теракт", "extremism"),
    ("терроризм", "extremism"),
    ("взорвать ", "extremism"),
)

ILLEGAL_REGEX: tuple[tuple[str, str], ...] = (
    (r"\bвзлом\w*\s+(акк|аккаунт|телеграм|whatsapp|instagram|вк|почт)", "hack"),
    (r"\b(украсть|укради|спиздить)\s+(акк|деньг|парол|базу)", "hack"),
    (r"\b(фишинг|phishing)\b", "fraud"),
    (r"\b(стиллер|stealer|keylogger|trojan|ransomware)\b", "malware"),
    (
        r"(детск\w*.{0,12}порн|порн\w*.{0,24}дет|порн\w*.{0,24}ребен|порн\w*.{0,24}ребён)",
        "csam",
    ),
    (r"(child\s*porn|kids?\s*porn|underage\s*porn|cp\s*content)", "csam"),
    (r"\b(педофил|педоф|lolicon|лоликон)\b", "csam"),
    (r"(несовершеннолет\w*.{0,18}(секс|порн|интим|nude|эротик))", "csam"),
    (r"\b(закладк\w+|мефедрон|амфетамин|героин|кокаин)\b", "drugs"),
    (r"\bнаркот\w+", "drugs"),
    (r"\b(отмыв\w*\s+денег|money\s*launder|обнал\w*)\b", "launder"),
    (r"\b(ддос|ddos|ботнет)\b", "ddos"),
    (r"\b(киллер|заказн\w+\s+убийств)\b", "violence"),
    (r"(порн\w*\s*сайт|сайт\w*\s*порн|xxx\s*site)", "porn"),
    (r"\b(теракт|террор\w*)\b", "extremism"),
)


def _default() -> dict:
    return {"blocked": {}, "log": [], "updated_at": 0}


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
            data.setdefault("blocked", {})
            data.setdefault("log", [])
            return data
        except Exception:
            return _default()


def save(data: dict) -> None:
    with _LOCK:
        data["updated_at"] = int(time.time())
        data["log"] = (data.get("log") or [])[:300]
        PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(PATH)


def is_blocked(user_id: int) -> bool:
    b = (load().get("blocked") or {}).get(str(int(user_id))) or {}
    return bool(b.get("active"))


def get_block(user_id: int) -> dict | None:
    b = (load().get("blocked") or {}).get(str(int(user_id)))
    if b and b.get("active"):
        return dict(b)
    return None


def block_user(
    user_id: int,
    *,
    reason: str = "",
    source: str = "tz",
    snippet: str = "",
    username: str = "",
    name: str = "",
    by: str = "auto",
    category: str = "other",
) -> dict:
    data = load()
    entry = {
        "user_id": int(user_id),
        "active": True,
        "reason": (reason or "illegal_tz")[:300],
        "category": category or "other",
        "source": source[:40],
        "snippet": (snippet or "")[:400],
        "username": (username or "").lstrip("@"),
        "name": name or "",
        "by": by[:40],
        "blocked_at": int(time.time()),
        "unblocked_at": None,
        "unblocked_by": None,
    }
    data.setdefault("blocked", {})[str(int(user_id))] = entry
    data.setdefault("log", []).insert(
        0,
        {
            "action": "block",
            "user_id": int(user_id),
            "reason": entry["reason"],
            "category": entry["category"],
            "ts": int(time.time()),
            "by": by,
        },
    )
    save(data)
    return entry


def unblock_user(user_id: int, *, by: str = "owner") -> dict | None:
    data = load()
    key = str(int(user_id))
    entry = (data.get("blocked") or {}).get(key)
    if not entry:
        return None
    entry["active"] = False
    entry["unblocked_at"] = int(time.time())
    entry["unblocked_by"] = by
    data["blocked"][key] = entry
    data.setdefault("log", []).insert(
        0,
        {
            "action": "unblock",
            "user_id": int(user_id),
            "ts": int(time.time()),
            "by": by,
        },
    )
    save(data)
    return entry


def list_blocked(limit: int = 50) -> list[dict]:
    items = [
        dict(v) for v in (load().get("blocked") or {}).values() if v.get("active")
    ]
    items.sort(key=lambda x: int(x.get("blocked_at") or 0), reverse=True)
    return items[:limit]


def list_log(limit: int = 40) -> list[dict]:
    return list((load().get("log") or [])[:limit])


def check_tz(text: str) -> tuple[bool, str, list[str]]:
    """
    Проверка ТЗ.
    Returns: (is_illegal, reason_ru, hits)
    hits: ["cat:phrase", ...]
    """
    raw = (text or "").strip()
    if not raw:
        return False, "", []
    low = raw.lower().replace("ё", "е")
    low_n = re.sub(r"[\s_]+", " ", low)
    low_n = re.sub(r"[«»\"'`]+", "", low_n)
    hits: list[str] = []
    cats: set[str] = set()

    for phrase, cat in ILLEGAL_PHRASES:
        p = phrase.lower().replace("ё", "е").strip()
        if p and p in low_n:
            hits.append(f"{cat}:{p}")
            cats.add(cat)

    for rx, cat in ILLEGAL_REGEX:
        try:
            m = re.search(rx, low_n, flags=re.IGNORECASE | re.DOTALL)
            if m:
                hits.append(f"{cat}:{m.group(0)[:40]}")
                cats.add(cat)
        except re.error:
            pass

    # CSAM: порно + дети/школа/малолет
    if re.search(r"порн", low_n) and re.search(
        r"(дет|ребен|ребён|малолет|несоверш|schoolgirl|loli|лоли|школот)", low_n
    ):
        hits.append("csam:порно+дети")
        cats.add("csam")

    seen: set[str] = set()
    uniq: list[str] = []
    for h in hits:
        if h not in seen:
            seen.add(h)
            uniq.append(h)

    if not uniq:
        return False, "", []

    cat_names = ", ".join(CAT_LABELS.get(c, c) for c in sorted(cats)[:4])
    reason = f"Запрещённое ТЗ ({cat_names}): " + ", ".join(
        u.split(":", 1)[-1] for u in uniq[:4]
    )
    return True, reason, uniq[:10]


def primary_category(hits: list[str]) -> str:
    if not hits:
        return "other"
    # priority
    order = (
        "csam",
        "violence",
        "extremism",
        "drugs",
        "weapons",
        "malware",
        "hack",
        "fraud",
        "launder",
        "ddos",
        "privacy",
        "porn",
        "other",
    )
    found = set()
    for h in hits:
        found.add(h.split(":", 1)[0] if ":" in h else "other")
    for c in order:
        if c in found:
            return c
    return "other"


def blocked_user_message() -> str:
    # документы (Platega/банк) — всегда, даже при блоке
    try:
        from terms_lib import PRIVACY_URL, AGREEMENT_URL
    except Exception:
        PRIVACY_URL = "https://telegra.ph/Politika-konfidencialnosti-Director-Vaggo-07-25"
        AGREEMENT_URL = (
            "https://telegra.ph/Polzovatelskoe-soglashenie-Director-Vaggo-07-25"
        )
    return (
        "🚫 <b>Доступ ограничен</b>\n\n"
        "Аккаунт заблокирован: в ТЗ/запросе найдены признаки "
        "<b>запрещённой</b> задачи (закон / правила сервиса).\n\n"
        "Снять блок может <b>только владелец</b> (если ошибка — напиши ему).\n\n"
        "Пока блок активен: заказы, баланс и сервисы недоступны.\n\n"
        "📋 <b>Документы сервиса (всегда доступны)</b>\n"
        f'🔒 <a href="{PRIVACY_URL}">Политика конфиденциальности</a>\n'
        f'📜 <a href="{AGREEMENT_URL}">Оферта / соглашение</a>\n'
        "/legal · /privacy · /offer"
    )


def owner_block_keyboard(user_id: int) -> dict:
    return {
        "inline_keyboard": [
            [
                {
                    "text": "✅ Разблокировать",
                    "callback_data": f"mod:un:{int(user_id)}",
                }
            ]
        ]
    }


def format_block_line(b: dict) -> str:
    un = b.get("username")
    who = f"@{un}" if un else (b.get("name") or "")
    cat = CAT_LABELS.get(str(b.get("category") or ""), b.get("category") or "—")
    return (
        f"id {b.get('user_id')} {who}\n"
        f"  [{cat}] {str(b.get('reason') or '')[:140]}\n"
        f"  by {b.get('by')} · {b.get('source')}"
    )
