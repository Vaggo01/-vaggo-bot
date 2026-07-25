# -*- coding: utf-8 -*-
"""
Рост / бизнес-логика Vaggo 4.2:
- похожие прошлые заказы (база знаний)
- финрадар
- тимлид (простые вопросы владельца)
- авто-прогресс-отчёты клиенту
"""
from __future__ import annotations

import re
import time
from typing import Any

import orders_lib as orders


REPORT_INTERVAL_SEC = 2.5 * 24 * 3600  # ~2.5 суток
FINANCE_DIGEST_SEC = 20 * 3600  # ~раз в сутки (с антиспамом)

# --- рефералка ---
REF_BONUS_RUB = 30  # бонус пригласившему при первом оплаченном заказе реферала
REF_DISCOUNT_NOTE = "друг пришёл по ссылке"


def ref_code_for_user(user_id: int) -> str:
    return f"ref_{int(user_id)}"


def parse_ref_start(arg: str) -> int | None:
    """Из /start ref_123 → 123."""
    a = (arg or "").strip()
    if not a.startswith("ref_"):
        return None
    try:
        return int(a[4:].split("_")[0])
    except Exception:
        return None


def apply_referral_on_start(state: dict, user_id: int, inviter_id: int | None) -> bool:
    """Запомнить inviter один раз. True если записали."""
    if not inviter_id or int(inviter_id) == int(user_id):
        return False
    refs = state.setdefault("referrals", {})
    key = str(int(user_id))
    if key in refs:
        return False
    refs[key] = {
        "inviter_id": int(inviter_id),
        "ts": int(time.time()),
        "rewarded": False,
    }
    return True


def maybe_reward_referral(cfg: dict, state: dict, buyer_id: int, order_id: str) -> str | None:
    """
    После оплаты заказа: бонус пригласившему (один раз на реферала).
    Возвращает текст для notify owner или None.
    """
    refs = state.get("referrals") or {}
    row = refs.get(str(int(buyer_id)))
    if not row or row.get("rewarded"):
        return None
    inv = int(row.get("inviter_id") or 0)
    if not inv:
        return None
    try:
        import balance_lib as bal

        bal.credit(
            inv,
            REF_BONUS_RUB,
            kind="referral",
            note=f"реферал {buyer_id} заказ {order_id}",
            ref=str(order_id),
        )
    except TypeError:
        try:
            import balance_lib as bal

            bal.credit(inv, REF_BONUS_RUB, note=f"ref {order_id}")
        except Exception as e2:
            print("ref reward fail", e2, flush=True)
            return None
    except Exception as e2:
        print("ref reward fail", e2, flush=True)
        return None
    row["rewarded"] = True
    row["reward_order"] = str(order_id)
    row["reward_rub"] = REF_BONUS_RUB
    refs[str(int(buyer_id))] = row
    state["referrals"] = refs
    return (
        f"🎁 Реферал: +{REF_BONUS_RUB} ₽ → user {inv}\n"
        f"от клиента {buyer_id} · заказ {order_id}"
    )


def find_similar_orders(brief: str, *, kind: str = "", limit: int = 3) -> list[dict]:
    """Грубый поиск похожих сданных/принятых заказов по словам."""
    text = (brief or "").lower()
    words = {w for w in re.findall(r"[a-zа-яё0-9]{4,}", text) if w}
    if not words:
        return []
    scored: list[tuple[int, dict]] = []
    for it in orders.list_orders(limit=80):
        if str(it.get("status") or "") not in ("done", "accepted", "in_progress", "new"):
            continue
        if kind and it.get("kind") and it.get("kind") != kind:
            # soft: lower score, don't skip entirely
            pass
        b = str(it.get("brief") or "").lower()
        hit = sum(1 for w in words if w in b)
        if kind and it.get("kind") == kind:
            hit += 2
        if hit >= 2:
            scored.append((hit, it))
    scored.sort(key=lambda x: (-x[0], -int(x[1].get("created_at") or 0)))
    return [it for _, it in scored[:limit]]


def similar_orders_html(brief: str, *, kind: str = "") -> str:
    import html as H

    sims = find_similar_orders(brief, kind=kind)
    if not sims:
        return ""
    lines = ["📚 <b>Похожие проекты</b> (из базы):"]
    for it in sims:
        age_d = max(0, int((time.time() - int(it.get("created_at") or time.time())) // 86400))
        title = orders.ORDER_TYPES.get(it.get("kind") or "", {}).get("title") or it.get("kind")
        lines.append(
            f"• <code>{H.escape(str(it.get('id')))}</code> · "
            f"{H.escape(str(title))} · {it.get('price')} ₽ · "
            f"{H.escape(str(it.get('status')))} · ~{age_d} дн. назад\n"
            f"  <i>{H.escape((it.get('brief') or '')[:90])}</i>"
        )
    return "\n".join(lines)


def finance_radar_html() -> str:
    """Сколько в работе / закрыто / по балансам."""
    import html as H

    items = orders.list_orders(limit=200)
    by_st: dict[str, list] = {}
    for it in items:
        by_st.setdefault(str(it.get("status") or "?"), []).append(it)

    def sum_price(lst: list) -> int:
        return sum(int(x.get("price") or 0) for x in lst)

    work = by_st.get("in_progress", []) + by_st.get("accepted", []) + by_st.get("new", [])
    done = by_st.get("done", [])
    cancel = by_st.get("cancelled", [])

    # маржа по типу (done)
    by_kind: dict[str, int] = {}
    for it in done:
        k = str(it.get("kind") or "other")
        by_kind[k] = by_kind.get(k, 0) + int(it.get("price") or 0)
    top_kinds = sorted(by_kind.items(), key=lambda x: -x[1])[:4]

    bal_total = 0
    try:
        import balance_lib as bal

        data = bal.load() if hasattr(bal, "load") else {}
        users = (data.get("users") or data.get("balances") or {}) if isinstance(data, dict) else {}
        if isinstance(users, dict):
            for v in users.values():
                if isinstance(v, dict):
                    bal_total += int(v.get("balance") or v.get("amount") or 0)
                else:
                    try:
                        bal_total += int(v)
                    except Exception:
                        pass
    except Exception:
        bal_total = -1

    lines = [
        "💰 <b>Финансовый радар</b>",
        f"{'━' * 16}",
        f"🛠 В работе / очередь: <b>{len(work)}</b> · "
        f"<b>{sum_price(work)}</b> ₽",
        f"✔️ Сдано (в базе): <b>{len(done)}</b> · <b>{sum_price(done)}</b> ₽",
        f"❌ Отменено: {len(cancel)}",
    ]
    if bal_total >= 0:
        lines.append(f"💳 На балансах клиентов: <b>{bal_total}</b> ₽")
    if top_kinds:
        lines.append("\n<b>Топ типов (сданные)</b>")
        for k, s in top_kinds:
            title = orders.ORDER_TYPES.get(k, {}).get("title") or k
            lines.append(f"• {H.escape(str(title))}: <b>{s}</b> ₽")
    # «горящие»
    hot = [x for x in work if "сроч" in str(x.get("brief") or "").lower() or "горит" in str(x.get("brief") or "").lower()]
    if hot:
        lines.append(f"\n🔥 Горят (по ТЗ): <b>{len(hot)}</b>")
        for x in hot[:5]:
            lines.append(f"• <code>{H.escape(str(x.get('id')))}</code> · {x.get('price')} ₽")
    lines.append(f"\n<i>{time.strftime('%d.%m %H:%M')}</i>")
    return "\n".join(lines)


def team_lead_html(query: str) -> str | None:
    """
    Простой тимлид без Grok: ключевые фразы.
    None = не распознано (можно отдать Grok).
    """
    q = (query or "").strip().lower()
    if not q or len(q) < 4:
        return None

    # финансы
    if any(
        w in q
        for w in (
            "маржин",
            "финанс",
            "касс",
            "деньг",
            "выручк",
            "радар",
            "баланс",
            "сколько денег",
        )
    ):
        return finance_radar_html()

    items = orders.list_orders(limit=100)
    work = [
        x
        for x in items
        if str(x.get("status") or "") in ("new", "accepted", "in_progress")
    ]

    if any(w in q for w in ("горят", "горит", "срочн", "дедлайн", "сдать сегодня", "сегодня сдать")):
        import html as H

        lines = ["🔥 <b>Горящие / открытые</b>\n"]
        if not work:
            return "Открытых заказов нет."
        for x in work[:15]:
            brief = (x.get("brief") or "")[:60]
            hot = "🔥" if any(t in brief.lower() for t in ("сроч", "горит", "asap")) else "•"
            lines.append(
                f"{hot} <code>{H.escape(str(x.get('id')))}</code> · "
                f"{orders.status_label(str(x.get('status')))} · "
                f"{x.get('price')} ₽\n"
                f"  <i>{H.escape(brief)}</i>"
            )
        return "\n".join(lines)

    if any(w in q for w in ("все заказ", "список заказ", "открыт", "в работе", "заказы")):
        import html as H

        lines = [f"🛠 <b>Открытые заказы</b> ({len(work)})\n"]
        if not work:
            return "Открытых заказов нет. /orders"
        for x in work[:20]:
            kind = orders.ORDER_TYPES.get(x.get("kind") or "", {}).get("title") or x.get("kind")
            lines.append(
                f"• <code>{H.escape(str(x.get('id')))}</code> · "
                f"{H.escape(str(kind))} · {x.get('price')} ₽ · "
                f"{orders.status_label(str(x.get('status')))}"
            )
        return "\n".join(lines)

    if any(w in q for w in ("сводк", "статус дел", "что по дел", "обзор")):
        return (
            finance_radar_html()
            + "\n\n"
            + team_lead_html("открытые заказы")
        )

    return None


def team_lead_grok(cfg: dict, query: str) -> str:
    """Fallback: короткий ответ Grok по данным заказов."""
    work = [
        x
        for x in orders.list_orders(limit=40)
        if str(x.get("status") or "") in ("new", "accepted", "in_progress", "done")
    ]
    lines = []
    for x in work[:25]:
        lines.append(
            f"- id={x.get('id')} status={x.get('status')} "
            f"kind={x.get('kind')} price={x.get('price')} "
            f"brief={(x.get('brief') or '')[:120].replace(chr(10), ' ')}"
        )
    system = (
        "Ты тимлид студии Вагго. Ответь владельцу по-русски, кратко, списком.\n"
        "Только факты из данных. Не выдумывай заказы."
    )
    user = f"Вопрос: {query}\n\nДанные заказов:\n" + ("\n".join(lines) or "пусто")
    try:
        from content import grok_chat

        ans = grok_chat(
            cfg,
            system,
            user,
            model=(cfg.get("grok_fast_model") or "grok-4.3"),
            temperature=0.2,
            tools=False,
            max_tokens=450,
        )
        return (ans or "").strip() or "Пусто."
    except Exception as e:
        return f"Grok недоступен: {e}"


def due_interim_reports() -> list[dict]:
    """Заказы in_progress, которым пора написать клиенту."""
    now = time.time()
    out = []
    for it in orders.list_orders(status="in_progress", limit=50):
        last = float(it.get("last_client_report_at") or it.get("updated_at") or it.get("created_at") or 0)
        if now - last >= REPORT_INTERVAL_SEC:
            out.append(it)
    return out


def mark_report_sent(item: dict) -> dict:
    item["last_client_report_at"] = int(time.time())
    item["client_report_count"] = int(item.get("client_report_count") or 0) + 1
    return orders.save_order(item)


def build_interim_report(cfg: dict, item: dict) -> str:
    """Короткий прогресс-отчёт клиенту (Grok или шаблон)."""
    kind = orders.ORDER_TYPES.get(item.get("kind") or "", {}).get("title") or item.get("kind")
    oid = item.get("id")
    brief = (item.get("brief") or "")[:500]
    n = int(item.get("client_report_count") or 0) + 1
    system = (
        "Ты менеджер студии Вагго. Напиши клиенту КОРОТКИЙ прогресс (3–6 предложений, HTML-теги <b> можно).\n"
        "Структура: что уже сделано (разумно по этапу), что дальше, есть ли вопросы.\n"
        "Без воды, без обещаний сроков «завтра точно», тон дружеский."
    )
    user = (
        f"Заказ {oid}, услуга {kind}, отчёт №{n}.\n"
        f"Статус: in_progress.\nТЗ:\n{brief}"
    )
    try:
        from content import grok_chat

        text = grok_chat(
            cfg,
            system,
            user,
            model=(cfg.get("grok_fast_model") or "grok-4.3"),
            temperature=0.45,
            tools=False,
            max_tokens=280,
        )
        text = (text or "").strip()
        if text:
            return (
                f"Прогресс · заказ <code>{oid}</code>\n\n"
                f"{text}\n\n"
                f"<i>Ответь на это сообщение — передам.</i>"
            )
    except Exception as e:
        print("interim grok", e, flush=True)
    return (
        f"Прогресс · заказ <code>{oid}</code> ({kind}).\n"
        f"Работа идёт. Есть вопросы — ответь на это сообщение."
    )
