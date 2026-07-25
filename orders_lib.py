# -*- coding: utf-8 -*-
"""
Заказы: приём заявки → фиксированная цена → исполнение → сдача.

ВАЖНО: заказ — независимый продукт клиента.
  Не встраивать в Вагго / @DirectorVaggobot / канал.
  Боты → client_bots/<id>/ (свой token, свой процесс).
  Клиенту — готовый результат, не «наш сервис».

Хранилище: media/orders.json (не state.json — polling не затирает).
Бесплатных слотов нет — все заказы платные (прайс ниже).
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from state import new_id

ROOT = Path(__file__).resolve().parent
ORDERS_PATH = ROOT / "media" / "orders.json"
_LOCK = threading.Lock()

# Free-слоты отключены. Цена = услуга → фикс. сумма (для банка/Platega).
FREE_LIMIT = 0
PRICE_MAX = 500
PRICE_MIN = 100

# Услуга → цена. Всё. Без «оценок» и плавающих сумм.
ORDER_TYPES: dict[str, dict[str, Any]] = {
    "design": {
        "title": "Дизайн / обложки",
        "price": 100,
        "hint": "аватар, обложка, 1–3 картинки",
        "includes": "макеты PNG/JPG по ТЗ",
        "not_includes": "хостинг, печать, брендбук",
    },
    "script": {
        "title": "Скрипт / автоматизация",
        "price": 150,
        "hint": "парсер, утилита, мелкий скрипт",
        "includes": "скрипт + короткая инструкция",
        "not_includes": "сервер, платные API",
    },
    "bot": {
        "title": "Telegram-бот",
        "price": 200,
        "hint": "меню, логика, простая админка",
        "includes": "код бота + README",
        "not_includes": "хостинг, VPS",
    },
    "site": {
        "title": "Сайт / лендинг",
        "price": 200,
        "hint": "1–5 страниц, HTML/простая сборка",
        "includes": "вёрстка/исходники по ТЗ",
        "not_includes": "домен, хостинг, SEO",
    },
    "app": {
        "title": "Приложение (MVP)",
        "price": 300,
        "hint": "базовый MVP по ТЗ",
        "includes": "функционал по ТЗ",
        "not_includes": "сторы, сервер 24/7",
    },
    "other": {
        "title": "Другое",
        "price": 200,
        "hint": "задача вне списка",
        "includes": "объём по ТЗ",
        "not_includes": "хостинг, домен, реклама",
    },
}

STATUS_LABELS = {
    "new": "🆕 новый — ждём принятия",
    "accepted": "✅ принят / оплачен",
    "in_progress": "🛠 в работе",
    "done": "✔️ готов · сдан",
    "cancelled": "❌ отменён",
}

# этап для живой карточки: (шаг, всего, подпись)
STATUS_PIPELINE: dict[str, tuple[int, int, str]] = {
    "new": (1, 4, "Заявка создана · ждём оплату / старт"),
    "accepted": (2, 4, "В очереди · скоро в работу"),
    "in_progress": (3, 4, "Делаем · можно писать по проекту"),
    "done": (4, 4, "Сдано · гарантия и правки по /terms"),
    "cancelled": (0, 4, "Заказ закрыт"),
}

# Опрос ТЗ: после «что сделать» — цвета, функционал, пример…
# 4 шага — коротко; Grok допишет остальное. Пропуск = «на усмотрение».
TZ_STEPS: list[dict[str, Any]] = [
    {
        "id": "what",
        "key": "what",
        "min": 1,
        "title": "Что сделать",
        "ask": (
            "✍️ <b>1/4 · Что сделать?</b>\n\n"
            "Хоть 2 слова: «бот для кафе», «лендинг».\n"
            "<i>Grok допишет ТЗ сам.</i>"
        ),
        "skip": "бот/сайт по ТЗ",
    },
    {
        "id": "audience",
        "key": "audience",
        "min": 1,
        "title": "Для кого",
        "ask": (
            "👥 <b>2/4 · Для кого?</b>\n\n"
            "Клиенты / гости / админ…\n"
            "Или жми «Пропустить»."
        ),
        "skip": "на усмотрение",
    },
    {
        "id": "features",
        "key": "features",
        "min": 1,
        "title": "Что важно",
        "ask": (
            "⚙️ <b>3/4 · Что обязательно?</b>\n\n"
            "Кнопки, меню, форма… или «как обычно»."
        ),
        "skip": "базовый набор",
    },
    {
        "id": "deadline",
        "key": "deadline",
        "min": 1,
        "title": "Срок",
        "ask": (
            "⏱ <b>4/4 · Срок?</b>\n\n"
            "«2 дня» / «не горит»."
        ),
        "skip": "не горит",
    },
]


def order_step_keyboard() -> dict:
    """На каждом шаге ТЗ — пропуск / отмена / меню."""
    return {
        "inline_keyboard": [
            [{"text": "⏭ Пропустить", "callback_data": "ord:skip"}],
            [
                {"text": "❌ Отмена", "callback_data": "ord:cancel"},
                {"text": "🏠 Меню", "callback_data": "menu:userhome"},
            ],
        ]
    }


def is_tz_too_vague(text: str) -> tuple[bool, str]:
    """
    Устарело для опроса: Grok дополняет ТЗ.
    Оставляем только «совсем пусто» на случай внешних вызовов.
    """
    t = (text or "").strip()
    if not t:
        return True, "Пусто. Напиши хоть пару слов."
    return False, ""


def tz_step_index(step_id: str) -> int:
    for i, s in enumerate(TZ_STEPS):
        if s["id"] == step_id:
            return i
    return 0


def tz_step(step_id: str) -> dict[str, Any]:
    for s in TZ_STEPS:
        if s["id"] == step_id:
            return s
    return TZ_STEPS[0]


def next_tz_step(step_id: str) -> dict[str, Any] | None:
    i = tz_step_index(step_id)
    if i + 1 < len(TZ_STEPS):
        return TZ_STEPS[i + 1]
    return None


def build_brief_from_answers(kind: str, answers: dict) -> str:
    """Собрать ТЗ из ответов (короткий опрос 4 шага + Grok-дополнения)."""
    meta = ORDER_TYPES.get(kind) or ORDER_TYPES["other"]
    title = str(meta.get("title") or kind)
    what = (answers.get("what") or answers.get("goal") or "по типу услуги").strip()
    audience = (answers.get("audience") or answers.get("for_who") or "на усмотрение").strip()
    features = (
        answers.get("features") or answers.get("platform") or "базовый набор"
    ).strip()
    deadline = (answers.get("deadline") or answers.get("extra") or "не горит").strip()
    # структурированное ТЗ (не «сырой» dump ключей)
    lines = [
        f"ТЗ: {title}",
        "",
        "1. Цель / что сделать",
        what,
        "",
        "2. Для кого / контекст",
        audience,
        "",
        "3. Функции / важные детали",
        features,
        "",
        "4. Срок / приоритет",
        deadline,
        "",
        "5. Рамки тарифа",
        f"• Фикс-цена: {int(meta.get('price') or PRICE_MIN)} ₽",
        f"• Входит: {meta.get('includes') or '—'}",
        f"• Не входит: {meta.get('not_includes') or 'хостинг, домен, сторы, 24/7'}",
        "",
        "6. Результат сдачи",
        "Исходники + краткий README по запуску (если применимо). Без хостинга, если не оговорено.",
    ]
    if (answers.get("colors") or "").strip():
        lines.extend(["", "Стиль / цвета:", str(answers.get("colors")).strip()])
    if (answers.get("example") or "").strip():
        lines.extend(["", "Референс / пример:", str(answers.get("example")).strip()])
    if (answers.get("ai_clarify") or "").strip():
        lines.extend(["", "Уточнения клиента:", str(answers.get("ai_clarify")).strip()])
    if (answers.get("ai_notes") or "").strip():
        lines.extend(["", "Заметки AI:", str(answers.get("ai_notes")).strip()])
    return "\n".join(lines)


def _extract_json_obj(text: str) -> dict:
    """Достать JSON-объект из ответа модели."""
    t = (text or "").strip()
    if not t:
        return {}
    # fence
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.DOTALL | re.IGNORECASE)
    if m:
        t = m.group(1)
    else:
        a, b = t.find("{"), t.rfind("}")
        if a >= 0 and b > a:
            t = t[a : b + 1]
    try:
        data = json.loads(t)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def review_tz_with_ai(
    cfg: dict | None,
    kind: str,
    answers: dict,
    *,
    extra_client_note: str = "",
) -> dict[str, Any]:
    """
    После опроса: Grok собирает единое ТЗ, может добавить/уточнить,
    проверяет законность и реальность выполнения в рамках фикс-тарифа.

    Возвращает:
      brief, summary, additions, questions (list[str]),
      legal_ok, legal_reason, feasible, feasible_reason,
      risk (ok|warn|block), engine (grok|fallback)
    """
    raw_brief = build_brief_from_answers(kind, answers)
    meta = ORDER_TYPES.get(kind) or ORDER_TYPES["other"]
    price = int(meta.get("price") or PRICE_MIN)
    title = str(meta.get("title") or kind)
    includes = str(meta.get("includes") or "")
    not_includes = str(meta.get("not_includes") or "")

    # rule-based legal first (быстро, без AI)
    legal_hits: list[str] = []
    legal_reason = ""
    legal_ok = True
    try:
        import moderation_lib as mod

        illegal, reason, hits = mod.check_tz(raw_brief + "\n" + (extra_client_note or ""))
        if illegal:
            legal_ok = False
            legal_reason = reason
            legal_hits = list(hits or [])
    except Exception:
        pass

    polished = build_brief_from_answers(kind, answers)
    if (extra_client_note or "").strip():
        polished += f"\n\nУточнения клиента:\n{extra_client_note.strip()}"

    fallback = {
        "brief": polished,
        "summary": (
            f"ТЗ по опросу: «{title}», фикс {price} ₽. "
            "Структура: цель → аудитория → функции → срок → рамки сдачи."
        ),
        "additions": (
            "Дефолты тарифа: исходники + README; хостинг/VPS/домен — отдельно, "
            "если не оговорено."
        ),
        "questions": [],
        "legal_ok": legal_ok,
        "legal_reason": legal_reason or ("ок" if legal_ok else "подозрение на запрещённое"),
        "feasible": True,
        "feasible_reason": f"Фикс-тариф {price} ₽ · объём в рамках «{title}».",
        "risk": "block" if not legal_ok else "ok",
        "engine": "fallback",
        "legal_hits": legal_hits,
    }
    if not legal_ok:
        return fallback

    system = (
        "Ты менеджер заказов студии «Вагго». Клиент прошёл опрос ТЗ.\n"
        "Собери единое чистое ТЗ, проверь законность и реалистичность.\n"
        "Ответ — ТОЛЬКО JSON без markdown-ограждений.\n"
        "Схема:\n"
        "{\n"
        '  "brief": "полное ТЗ 800-2000 знаков, структурировано",\n'
        '  "summary": "2-4 предложения: что понял",\n'
        '  "additions": "что добавил от себя как разумные дефолты (или пусто)",\n'
        '  "questions": ["уточнение1", "..."]  // 0-3 шт, только если критично,\n'
        '  "legal_ok": true/false,\n'
        '  "legal_reason": "почему",\n'
        '  "feasible": true/false,\n'
        '  "feasible_reason": "влезает ли в тариф/срок",\n'
        '  "risk": "ok" | "warn" | "block",\n'
        '  "upsell": "1 мягкое предложение доп.услуги (или пусто)",\n'
        '  "risk_delay": "низкий|средний|высокий — срыв сроков",\n'
        '  "risk_scope": "низкий|средний|высокий — раздувание ТЗ",\n'
        '  "dev_brief": "короткое ТЗ для исполнителя 400-900 знаков"\n'
        "}\n"
        "block = незаконно / мошенничество / вред / обход закона.\n"
        "warn = слишком большой объём на тариф, неясность, но можно взять с оговорками.\n"
        "ok = можно брать.\n"
        "upsell: мягко, 1 идея (бот к сайту, админка, оплата…) — без давления.\n"
        "Не выдумывай факты о клиенте. Не предлагай взлом, скам, malware.\n"
        "Цена фиксирована — не меняй сумму, только оцени объём.\n"
        "legal_ok=false / risk=block ТОЛЬКО при явной незаконности "
        "(взлом, скам, malware, насилие, CSAM, наркотики, пробив…).\n"
        "Обычный сайт/бот/дизайн/скрипт для бизнеса — legal_ok=true.\n"
        "Если объём велик на тариф — feasible=false, risk=warn (не block)."
    )
    user = (
        f"Услуга: {title} ({kind})\n"
        f"Фикс-цена: {price} ₽\n"
        f"Входит: {includes}\n"
        f"Не входит: {not_includes}\n\n"
        f"Ответы опроса / черновик ТЗ:\n{polished}\n"
    )
    if (extra_client_note or "").strip():
        user += f"\nДоп. ответ клиента на уточнения:\n{extra_client_note.strip()}\n"

    try:
        from content import grok_chat

        cfg = cfg or {}
        model = (
            cfg.get("grok_order_model")
            or cfg.get("grok_full_model")
            or cfg.get("grok_model")
            or "grok-4.5"
        )
        raw = ""
        last_err: Exception | None = None
        # 2 попытки: full → fast (мост иногда таймаутит)
        for model_try in (model, cfg.get("grok_fast_model") or "grok-4.3"):
            try:
                raw = grok_chat(
                    cfg,
                    system,
                    user,
                    model=str(model_try),
                    temperature=0.3,
                    tools=False,
                    max_tokens=900,
                )
                if (raw or "").strip():
                    break
            except Exception as e:
                last_err = e
                print(
                    "review_tz grok try fail",
                    model_try,
                    type(e).__name__,
                    str(e)[:120],
                    flush=True,
                )
                raw = ""
        if not (raw or "").strip():
            fallback["summary"] = (
                f"ТЗ по опросу «{title}», фикс {price} ₽ — "
                "цель, аудитория, функции и рамки сдачи собраны. Можно отправлять."
            )
            fallback["engine"] = "fallback-no-grok"
            return fallback

        data = _extract_json_obj(raw)
        if not data:
            # не пихаем сырой JSON клиенту в «добавил от себя»
            fallback["summary"] = (
                f"ТЗ по опросу «{title}» собрано. Можно отправлять."
            )
            fallback["additions"] = ""
            fallback["engine"] = "fallback-parse"
            return fallback

        brief = str(data.get("brief") or polished or raw_brief).strip() or polished
        # если brief пришёл с литералами \n — развернём
        if "\\n" in brief and "\n" not in brief[:80]:
            brief = brief.replace("\\n", "\n")
        # слишком короткое/мусорное brief от модели → структурированный fallback
        if len(brief) < 120 or brief.strip().startswith("{") or '"brief"' in brief[:40]:
            brief = polished
        questions = data.get("questions") or []
        if not isinstance(questions, list):
            questions = []
        questions = [str(q).strip() for q in questions if str(q).strip()][:3]

        legal_ok_ai = bool(data.get("legal_ok", True))
        risk = str(data.get("risk") or "ok").lower().strip()
        if risk not in ("ok", "warn", "block"):
            risk = "ok" if legal_ok_ai else "warn"

        # повторная rule-check — только правила банят жёстко
        rule_illegal = False
        try:
            import moderation_lib as mod

            illegal2, reason2, hits2 = mod.check_tz(brief)
            if illegal2:
                rule_illegal = True
                legal_ok_ai = False
                risk = "block"
                data["legal_reason"] = reason2
                legal_hits = list(hits2 or [])
        except Exception:
            pass

        # AI «незаконно», но rule-check чист → не баним, только warn
        if not legal_ok_ai and not rule_illegal:
            legal_ok_ai = True
            risk = "warn"
            data["legal_reason"] = (
                "AI насторожился, автофильтр чист. "
                + str(data.get("legal_reason") or "")
            )[:400]

        feasible = bool(data.get("feasible", True))
        # «не влезает в тариф» — warn, не block
        if not feasible and risk == "block" and not rule_illegal:
            risk = "warn"
        if risk == "block" and rule_illegal:
            feasible = False

        upsell = str(data.get("upsell") or "").strip()[:300]
        dev_brief = str(data.get("dev_brief") or "").strip()[:1200]
        risk_delay = str(data.get("risk_delay") or "средний").lower()[:20]
        risk_scope = str(data.get("risk_scope") or "средний").lower()[:20]
        additions_raw = str(data.get("additions") or "").strip()
        # не отдаём клиенту сырой JSON (часто модель кладёт весь объект в additions)
        if (
            not additions_raw
            or additions_raw.startswith("{")
            or additions_raw.startswith('"')
            or '"brief"' in additions_raw[:50]
            or additions_raw.count("\\n") >= 3
        ):
            additions_raw = ""
        else:
            if "\\n" in additions_raw and "\n" not in additions_raw[:80]:
                additions_raw = additions_raw.replace("\\n", "\n")
            additions_raw = additions_raw[:800]
        return {
            "brief": brief[:4000],
            "summary": str(data.get("summary") or "")[:800],
            "additions": additions_raw,
            "questions": questions,
            "legal_ok": legal_ok_ai,
            "legal_reason": str(data.get("legal_reason") or ("ок" if legal_ok_ai else "запрещено"))[
                :400
            ],
            "feasible": feasible,
            "feasible_reason": str(data.get("feasible_reason") or "")[:400],
            "risk": risk,
            "upsell": upsell,
            "dev_brief": dev_brief,
            "risk_delay": risk_delay,
            "risk_scope": risk_scope,
            "engine": "grok",
            "legal_hits": legal_hits,
        }
    except Exception as e:
        print("review_tz_with_ai fail", type(e).__name__, str(e)[:160], flush=True)
        fallback["summary"] = (
            f"ТЗ по опросу «{title}», фикс {price} ₽ — можно отправлять."
        )
        return fallback


def validate_step_answer(step: dict, text: str) -> tuple[bool, str]:
    """ok, error_message. Минимумы сняты — Grok допишет ТЗ сам."""
    t = (text or "").strip()
    if not t:
        return False, "Пусто. Напиши хоть слово или «нет» / «на твоё усмотрение»."
    # любые 1+ символы ок (эмодзи тоже)
    return True, ""


def _default() -> dict:
    return {"items": {}, "free_used": 0, "updated_at": 0}


def _migrate_from_state() -> dict:
    """Если в state.json остались заказы — перенести."""
    try:
        from state import load_state

        st = load_state()
        o = st.get("orders")
        if isinstance(o, dict) and (o.get("items") or o.get("free_used")):
            return {
                "items": dict(o.get("items") or {}),
                "free_used": int(o.get("free_used") or 0),
                "updated_at": int(time.time()),
            }
    except Exception:
        pass
    return _default()


def load_orders() -> dict:
    with _LOCK:
        if not ORDERS_PATH.exists():
            data = _migrate_from_state()
            ORDERS_PATH.parent.mkdir(parents=True, exist_ok=True)
            ORDERS_PATH.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return data
        try:
            data = json.loads(ORDERS_PATH.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return _default()
            data.setdefault("items", {})
            data.setdefault("free_used", 0)
            if not isinstance(data["items"], dict):
                data["items"] = {}
            return data
        except Exception:
            return _default()


def save_orders(data: dict) -> None:
    """Только media/orders.json — не через state.json (polling не затрёт)."""
    with _LOCK:
        data["updated_at"] = int(time.time())
        ORDERS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = ORDERS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(ORDERS_PATH)


def free_left(state: dict | None = None) -> int:
    """Бесплатных слотов больше нет."""
    return 0


def price_of(kind: str) -> int:
    meta = ORDER_TYPES.get(kind) or ORDER_TYPES["other"]
    return int(meta.get("price") or PRICE_MIN)


def price_catalog_lines() -> list[str]:
    """Только «услуга — цена» (как надо банку)."""
    lines = []
    for _k, meta in ORDER_TYPES.items():
        p = int(meta.get("price") or 0)
        lines.append(f"• {meta['title']} — <b>{p} ₽</b>")
    return lines


def estimate(kind: str, brief: str) -> dict:
    """Цена строго из прайса услуги. brief не меняет сумму."""
    meta = ORDER_TYPES.get(kind) or ORDER_TYPES["other"]
    price = int(meta.get("price") or PRICE_MIN)
    return {
        "complexity": 1,
        "complexity_label": "фикс. тариф",
        "price": price,
        "price_min": price,
        "price_max": price,
        "title": meta["title"],
        "hint": meta.get("hint") or "",
        "includes": meta.get("includes") or "",
        "not_includes": meta.get("not_includes") or "",
    }


def create_order(
    *,
    user_id: int,
    username: str = "",
    name: str = "",
    kind: str,
    brief: str,
) -> dict:
    data = load_orders()
    items = data.setdefault("items", {})
    est = estimate(kind, brief)
    oid = new_id()
    item = {
        "id": oid,
        "user_id": int(user_id),
        "username": (username or "").lstrip("@"),
        "name": name or username or str(user_id),
        "kind": kind if kind in ORDER_TYPES else "other",
        "brief": (brief or "").strip()[:2000],
        "complexity": est["complexity"],
        "complexity_label": est["complexity_label"],
        "price": int(est["price"]),
        "price_list": int(est["price"]),
        "is_free": False,
        "free_slot": None,
        "status": "new",
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
        "deliver_note": "",
        "result_file_id": None,
    }
    items[oid] = item
    save_orders(data)
    return item


def get_order(oid: str) -> dict | None:
    return (load_orders().get("items") or {}).get(str(oid))


def save_order(item: dict) -> dict:
    data = load_orders()
    item["updated_at"] = int(time.time())
    data.setdefault("items", {})[str(item["id"])] = item
    save_orders(data)
    return item


def append_event(item: dict, code: str, public_text: str) -> dict:
    """Лента событий на карточке (последние 3)."""
    ev = list(item.get("events") or [])
    ev.append(
        {
            "ts": int(time.time()),
            "code": str(code or "")[:32],
            "text": str(public_text or "")[:200],
        }
    )
    item["events"] = ev[-12:]
    return save_order(item)


def set_client_card(item: dict, *, chat_id: int, message_id: int) -> dict:
    item["client_card_chat_id"] = int(chat_id)
    item["client_card_message_id"] = int(message_id)
    return save_order(item)


def delete_order(oid: str, *, restore_free: bool = True) -> dict | None:
    """
    Удалить заказ из media/orders.json.
    Если free и restore_free — вернуть free_used (не ниже 0).
    """
    data = load_orders()
    items = data.setdefault("items", {})
    key = str(oid)
    item = items.pop(key, None)
    if not item:
        return None
    if restore_free and item.get("is_free"):
        # free_slot считался — откатываем счётчик, если заказ ещё не «съел» слот бесполезно
        data["free_used"] = max(0, int(data.get("free_used") or 0) - 1)
        # free_used не ниже числа оставшихся free-заказов
        free_n = sum(1 for x in items.values() if x.get("is_free"))
        data["free_used"] = max(int(data["free_used"]), free_n)
    save_orders(data)
    return item


def delete_orders_by_status(status: str, *, restore_free: bool = True) -> int:
    """Удалить все заказы со статусом. Возвращает число удалённых."""
    data = load_orders()
    items = data.setdefault("items", {})
    to_del = [
        (k, v)
        for k, v in list(items.items())
        if str(v.get("status") or "") == str(status)
    ]
    free_restore = 0
    for k, v in to_del:
        items.pop(k, None)
        if restore_free and v.get("is_free"):
            free_restore += 1
    if free_restore:
        data["free_used"] = max(0, int(data.get("free_used") or 0) - free_restore)
        free_n = sum(1 for x in items.values() if x.get("is_free"))
        data["free_used"] = max(int(data["free_used"]), free_n)
    save_orders(data)
    return len(to_del)


def list_orders(*, status: str | None = None, limit: int = 30) -> list[dict]:
    items = list((load_orders().get("items") or {}).values())
    if status:
        items = [x for x in items if x.get("status") == status]
    items.sort(key=lambda x: int(x.get("created_at") or 0), reverse=True)
    return items[:limit]


def user_pending_order(user_id: int) -> dict | None:
    for x in list_orders(limit=100):
        if int(x.get("user_id") or 0) != int(user_id):
            continue
        if x.get("status") in ("done", "cancelled"):
            continue
        return x
    return None


def list_user_orders(user_id: int, *, limit: int = 20) -> list[dict]:
    out = [
        x for x in list_orders(limit=100) if int(x.get("user_id") or 0) == int(user_id)
    ]
    return out[:limit]


def status_label(status: str) -> str:
    return STATUS_LABELS.get(str(status or ""), str(status or "—"))


def progress_bar(step: int, total: int = 4, width: int = 8) -> str:
    total = max(1, int(total))
    step = max(0, min(int(step), total))
    filled = int(round(width * step / total))
    return "█" * filled + "░" * (width - filled)


def pipeline_info(status: str) -> tuple[int, int, str]:
    return STATUS_PIPELINE.get(str(status or ""), (1, 4, status_label(status)))


def eta_hint(item: dict) -> str:
    """Грубый дедлайн для клиента (не юридический)."""
    st = str(item.get("status") or "")
    # из ТЗ если клиент писал срок
    brief = str(item.get("brief") or "")
    for line in brief.splitlines():
        if "Срок:" in line or "срок:" in line:
            raw = line.split(":", 1)[-1].strip()
            if raw and raw.lower() not in ("не горит", "на усмотрение", "—"):
                return raw[:80]
    if st == "new":
        return "после оплаты · обычно в тот же / след. день"
    if st == "accepted":
        return "1–3 дня до старта (загрузка)"
    if st == "in_progress":
        return "смотри срок в ТЗ · пиши, если горит"
    if st == "done":
        return "сдан · гарантия 2 сут."
    return "—"


def format_user_history(user_id: int) -> str:
    import html as H

    items = list_user_orders(user_id)
    head = "📦 <b>Мои заказы</b>\n\n"
    if not items:
        return head + "Пока пусто. Жми /order — прайс и оформление."
    lines = [head]
    for it in items:
        kind = ORDER_TYPES.get(it.get("kind") or "", {}).get("title") or it.get("kind")
        price = f"{it.get('price')} ₽"
        st = status_label(str(it.get("status") or ""))
        lines.append(
            f"• <code>{H.escape(str(it.get('id')))}</code>\n"
            f"  {H.escape(str(kind))} · {price}\n"
            f"  {st}\n"
            f"  <i>{H.escape((it.get('brief') or '')[:80])}</i>\n"
        )
    lines.append("Обновить: /myorders · Новый: /order · Прайс: /prices")
    return "\n".join(lines)


def order_keyboard_types() -> dict:
    """Кнопки: иконка · услуга · цена (Vaggo 1.0)."""
    icons = {
        "design": "🎨",
        "script": "⚙️",
        "bot": "🤖",
        "site": "🌐",
        "app": "📱",
        "other": "✨",
    }
    rows = [[{"text": "🛠 Выбери услугу", "callback_data": "ord:noop"}]]
    for k, meta in ORDER_TYPES.items():
        p = int(meta.get("price") or 0)
        title = str(meta.get("title") or k)
        short = title if len(title) < 22 else title[:20] + "…"
        ic = icons.get(k, "•")
        rows.append(
            [{"text": f"{ic} {short} · {p} ₽", "callback_data": f"ord:type:{k}"}]
        )
    rows.append(
        [
            {"text": "💰 Прайс", "callback_data": "legal:prices"},
            {"text": "📦 Мои", "callback_data": "ord:mine"},
            {"text": "💳 Баланс", "callback_data": "bal:show"},
        ]
    )
    rows.append(
        [
            {"text": "🏠 Меню", "callback_data": "menu:userhome"},
            {"text": "❌ Отмена", "callback_data": "ord:cancel"},
        ]
    )
    return {"inline_keyboard": rows}


def user_order_actions_keyboard(oid: str) -> dict:
    oid = str(oid)
    return {
        "inline_keyboard": [
            [{"text": "🔄 Обновить статус", "callback_data": f"ord:status:{oid}"}],
            [{"text": "💬 Вопрос по проекту", "callback_data": f"ord:ask:{oid}"}],
            [{"text": "📄 Договор + акт", "callback_data": f"ord:docs:{oid}"}],
            [
                {"text": "📦 Все заказы", "callback_data": "ord:mine"},
                {"text": "🛠 Новый", "callback_data": "ord:restart"},
            ],
            [
                {"text": "💳 Баланс", "callback_data": "bal:show"},
                {"text": "🏠 Меню", "callback_data": "menu:userhome"},
            ],
        ]
    }


def owner_order_keyboard(oid: str) -> dict:
    """Короткое уведомление о новом заказе + вход в карточку."""
    oid = str(oid)
    return {
        "inline_keyboard": [
            [{"text": "📂 Открыть заказ", "callback_data": f"ord:o:open:{oid}"}],
            [
                {"text": "✅ В работу", "callback_data": f"ord:w:{oid}"},
                {"text": "✔️ Готово", "callback_data": f"ord:d:{oid}"},
            ],
            [{"text": "❌ Отменить", "callback_data": f"ord:x:{oid}"}],
        ]
    }


def owner_order_hub_keyboard(oid: str, *, user_id: int | None = None) -> dict:
    """Полное управление заказом для владельца."""
    oid = str(oid)
    rows = [
        [
            {"text": "✅ В работу", "callback_data": f"ord:w:{oid}"},
            {"text": "✔️ Готово", "callback_data": f"ord:d:{oid}"},
        ],
        [
            {"text": "📜 Смотреть ТЗ", "callback_data": f"ord:o:tz:{oid}"},
            {"text": "✏️ Править ТЗ", "callback_data": f"ord:o:editz:{oid}"},
        ],
        [
            {"text": "💬 Написать клиенту", "callback_data": f"ord:o:msg:{oid}"},
            {"text": "📄 Документы", "callback_data": f"ord:o:docs:{oid}"},
        ],
    ]
    if user_id:
        rows.append(
            [{"text": "👤 Профиль клиента", "callback_data": f"ord:o:prof:{int(user_id)}"}]
        )
    rows.append([{"text": "❌ Отменить заказ", "callback_data": f"ord:x:{oid}"}])
    rows.append(
        [
            {"text": "📋 Все заказы", "callback_data": "ord:o:list"},
            {"text": "👥 Клиенты", "callback_data": "ord:o:clients"},
        ]
    )
    rows.append([{"text": "🏠 Пульт", "callback_data": "menu:home"}])
    return {"inline_keyboard": rows}


def owner_docs_keyboard(oid: str) -> dict:
    """Стопка документов по заказу."""
    oid = str(oid)
    return {
        "inline_keyboard": [
            [{"text": "📄 Договор", "callback_data": f"ord:o:docc:{oid}"}],
            [{"text": "📋 Акт", "callback_data": f"ord:o:doca:{oid}"}],
            [{"text": "📰 Кейс (черновик)", "callback_data": f"ord:o:case:{oid}"}],
            [{"text": "📦 Все документы пачкой", "callback_data": f"ord:o:docall:{oid}"}],
            [{"text": "« К заказу", "callback_data": f"ord:o:open:{oid}"}],
        ]
    }


def owner_orders_list_keyboard(items: list[dict], *, limit: int = 12) -> dict:
    """Список заказов кнопками."""
    rows = []
    for it in items[:limit]:
        oid = str(it.get("id") or "")
        st = str(it.get("status") or "?")
        un = (it.get("username") or "").lstrip("@")
        who = f"@{un}" if un else str(it.get("name") or it.get("user_id") or "")[:12]
        kind = str(it.get("kind") or "")[:8]
        price = it.get("price") or 0
        mark = {
            "new": "🆕",
            "accepted": "✅",
            "in_progress": "🛠",
            "done": "✔️",
            "cancelled": "❌",
        }.get(st, "•")
        label = f"{mark} {oid[:6]} · {kind} · {price}₽ · {who}"[:64]
        rows.append([{"text": label, "callback_data": f"ord:o:open:{oid}"}])
    rows.append(
        [
            {"text": "🔄 Обновить", "callback_data": "ord:o:list"},
            {"text": "👥 Клиенты", "callback_data": "ord:o:clients"},
        ]
    )
    rows.append([{"text": "🏠 Пульт", "callback_data": "menu:home"}])
    return {"inline_keyboard": rows}


def list_clients(*, limit: int = 20) -> list[dict]:
    """Уникальные клиенты с заказами + агрегаты."""
    by: dict[int, dict] = {}
    for it in list_orders(limit=100):
        try:
            uid = int(it.get("user_id") or 0)
        except Exception:
            continue
        if not uid:
            continue
        row = by.setdefault(
            uid,
            {
                "user_id": uid,
                "username": (it.get("username") or "").lstrip("@"),
                "name": it.get("name") or "",
                "orders": 0,
                "open": 0,
                "spent": 0,
                "last_at": 0,
            },
        )
        row["orders"] += 1
        if it.get("username"):
            row["username"] = str(it.get("username")).lstrip("@")
        if it.get("name"):
            row["name"] = it.get("name")
        st = str(it.get("status") or "")
        if st in ("new", "accepted", "in_progress"):
            row["open"] += 1
        if st == "done":
            row["spent"] += int(it.get("price") or 0)
        row["last_at"] = max(int(row.get("last_at") or 0), int(it.get("updated_at") or it.get("created_at") or 0))
    out = sorted(by.values(), key=lambda x: -int(x.get("last_at") or 0))
    return out[:limit]


def owner_clients_keyboard(clients: list[dict]) -> dict:
    rows = []
    for c in clients[:15]:
        uid = int(c.get("user_id") or 0)
        un = c.get("username") or ""
        who = f"@{un}" if un else str(c.get("name") or uid)[:14]
        label = f"👤 {who} · {c.get('orders')}зак · {c.get('open')}откр"[:64]
        rows.append([{"text": label, "callback_data": f"ord:o:prof:{uid}"}])
    rows.append(
        [
            {"text": "📋 Заказы", "callback_data": "ord:o:list"},
            {"text": "🏠 Пульт", "callback_data": "menu:home"},
        ]
    )
    return {"inline_keyboard": rows}


def format_client_profile(user_id: int) -> str:
    """Профиль клиента: заказы, баланс, контакты."""
    import html as H

    uid = int(user_id)
    items = list_user_orders(uid, limit=30)
    un = ""
    name = ""
    if items:
        un = (items[0].get("username") or "").lstrip("@")
        name = str(items[0].get("name") or "")
    bal_s = "—"
    try:
        import balance_lib as bal

        bal_s = f"{bal.get_balance(uid)} ₽"
    except Exception:
        pass
    open_n = sum(
        1 for x in items if str(x.get("status") or "") in ("new", "accepted", "in_progress")
    )
    done_n = sum(1 for x in items if str(x.get("status") or "") == "done")
    spent = sum(int(x.get("price") or 0) for x in items if str(x.get("status") or "") == "done")
    who = f"@{un}" if un else (name or str(uid))
    lines = [
        f"👤 <b>Профиль клиента</b>",
        f"{'━' * 16}",
        f"{H.escape(str(who))}",
        f"id <code>{uid}</code>",
        f"💳 баланс: <b>{H.escape(str(bal_s))}</b>",
        f"📦 заказов: <b>{len(items)}</b> · открытых <b>{open_n}</b> · сдано <b>{done_n}</b>",
        f"💰 оплачено (сдано): <b>{spent}</b> ₽",
        "",
        "<b>Заказы</b>",
    ]
    if not items:
        lines.append("пока нет")
    for it in items[:12]:
        kind = ORDER_TYPES.get(it.get("kind") or "", {}).get("title") or it.get("kind")
        lines.append(
            f"· <code>{H.escape(str(it.get('id')))}</code> · "
            f"{H.escape(str(kind))} · {it.get('price')}₽ · "
            f"{status_label(str(it.get('status') or ''))}"
        )
    return "\n".join(lines)


def client_profile_keyboard(user_id: int, orders_list: list[dict] | None = None) -> dict:
    uid = int(user_id)
    rows = [[{"text": "💬 Написать в TG", "url": f"tg://user?id={uid}"}]]
    # deep link by username if any
    items = orders_list if orders_list is not None else list_user_orders(uid, limit=8)
    un = ""
    if items:
        un = (items[0].get("username") or "").lstrip("@")
    if un:
        rows = [[{"text": f"💬 @{un}", "url": f"https://t.me/{un}"}]]
    for it in items[:6]:
        oid = str(it.get("id") or "")
        st = str(it.get("status") or "")[:4]
        rows.append(
            [{"text": f"📂 {oid[:8]} · {st}", "callback_data": f"ord:o:open:{oid}"}]
        )
    rows.append(
        [
            {"text": "📋 Все заказы", "callback_data": "ord:o:list"},
            {"text": "👥 Клиенты", "callback_data": "ord:o:clients"},
        ]
    )
    rows.append([{"text": "🏠 Пульт", "callback_data": "menu:home"}])
    return {"inline_keyboard": rows}


def format_owner_orders_list(items: list[dict]) -> str:
    import html as H

    if not items:
        return "🛠 <b>Заказы</b>\nПока пусто."
    open_n = sum(
        1 for x in items if str(x.get("status") or "") in ("new", "accepted", "in_progress")
    )
    lines = [
        f"🛠 <b>Заказы</b> · {len(items)} · открытых <b>{open_n}</b>",
        "Жми кнопку ниже",
    ]
    return "\n".join(lines)


def format_tz_full(item: dict) -> str:
    import html as H

    oid = H.escape(str(item.get("id") or ""))
    kind = ORDER_TYPES.get(item.get("kind") or "", {}).get("title") or item.get("kind")
    brief = H.escape(item.get("brief") or "—")
    dev = H.escape(str(item.get("dev_brief") or "")[:1500])
    lines = [
        f"<b>ТЗ</b> · <code>{oid}</code> · {H.escape(str(kind))} · {item.get('price')} ₽",
        status_label(str(item.get("status") or "")),
        "",
        brief,
    ]
    if dev:
        lines.extend(["", "<b>Для исполнителя</b>", dev])
    return "\n".join(lines)


def format_order_card(item: dict, *, for_owner: bool = False) -> str:
    """Живая карточка: этап, прогресс, кто ведёт, дедлайн, ТЗ."""
    import html as H

    kind = ORDER_TYPES.get(item.get("kind") or "", {}).get("title") or item.get("kind")
    price = f"{item.get('price')} ₽"
    st = str(item.get("status") or "new")
    step, total, stage = pipeline_info(st)
    bar = progress_bar(step, total, width=8)
    worker = str(item.get("assignee_name") or "команда Вагго")
    eta = eta_hint(item)
    oid = H.escape(str(item.get("id") or ""))

    next_map = {
        "new": "Ждём оплату / старт",
        "accepted": "В очереди",
        "in_progress": "В работе — можно писать",
        "done": "Сдан · гарантия 2 сут.",
        "cancelled": "Закрыт",
    }
    lines = [
        f"<b>Заказ</b> <code>{oid}</code> · {H.escape(str(kind))} · {price}",
        f"{status_label(st)}  <code>{bar}</code>",
        f"{H.escape(next_map.get(st, stage))}",
        f"Ведёт: {H.escape(worker)} · срок: {H.escape(eta)}",
    ]
    if for_owner:
        un = item.get("username")
        who = f"@{un}" if un else item.get("name")
        lines.insert(1, f"Клиент: {H.escape(str(who))} · <code>{item.get('user_id')}</code>")
    events = list(item.get("events") or [])[-2:]
    if events:
        tail = []
        for e in events:
            ts = int(e.get("ts") or 0)
            when = time.strftime("%d.%m %H:%M", time.localtime(ts)) if ts else ""
            tail.append(f"{when} {H.escape(str(e.get('text') or ''))}")
        lines.append("· " + " · ".join(tail))
    brief = (item.get("brief") or "—").strip()
    if for_owner:
        lines.append(f"\n<b>ТЗ</b>\n{H.escape(brief[:900])}")
    else:
        lines.append(f"\n{H.escape(brief[:400])}")
    if st == "done" and not for_owner:
        lines.append("\nНужны доработки — «Вопрос по проекту» или /order")
    return "\n".join(lines)


def orders_path() -> str:
    return str(ORDERS_PATH)
