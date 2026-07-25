# -*- coding: utf-8 -*-
"""
Баланс пользователей + пополнение через СБП / Platega.

Хранение: media/balance.json (на домашнем ПК — source of truth).
На Bothost (cloud): чтение/запись через Grok-bridge /balance/* — один кошелёк везде.
СБП: перевод → «Я оплатил» → confirm.
Platega: webhook success → credit (см. platega_lib).
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from state import new_id

ROOT = Path(__file__).resolve().parent
BALANCE_PATH = ROOT / "media" / "balance.json"
_LOCK = threading.Lock()

# Суммы быстрого пополнения (₽)
TOPUP_PRESETS = (100, 200, 300, 400, 500, 700, 1000, 2000)
TOPUP_MIN = 50
TOPUP_MAX = 50_000
TOPUP_TTL_SEC = 6 * 3600  # заявка живёт 6 часов


def _default() -> dict:
    return {"wallets": {}, "topups": {}, "ledger": [], "updated_at": 0}


def load() -> dict:
    with _LOCK:
        if not BALANCE_PATH.exists():
            data = _default()
            BALANCE_PATH.parent.mkdir(parents=True, exist_ok=True)
            BALANCE_PATH.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return data
        try:
            data = json.loads(BALANCE_PATH.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return _default()
            data.setdefault("wallets", {})
            data.setdefault("topups", {})
            data.setdefault("ledger", [])
            if not isinstance(data["wallets"], dict):
                data["wallets"] = {}
            if not isinstance(data["topups"], dict):
                data["topups"] = {}
            if not isinstance(data["ledger"], list):
                data["ledger"] = []
            return data
        except Exception:
            return _default()


def save(data: dict) -> None:
    with _LOCK:
        data["updated_at"] = int(time.time())
        BALANCE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = BALANCE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(BALANCE_PATH)


def topup_enabled(cfg: dict | None = None) -> bool:
    """Пополнение вкл/выкл. Пока false — ждём Platega (честная оплата)."""
    if not cfg:
        try:
            from state import load_config

            cfg = load_config()
        except Exception:
            return False
    pay = cfg.get("payments") if isinstance(cfg.get("payments"), dict) else {}
    if "topup_enabled" in pay:
        return bool(pay.get("topup_enabled"))
    block = cfg.get("sbp") if isinstance(cfg.get("sbp"), dict) else {}
    if "enabled" in block:
        return bool(block.get("enabled"))
    return True


def topup_disabled_text() -> str:
    return (
        "💳 <b>Пополнение временно выключено</b>\n\n"
        "Скоро подключим нормальную оплату (Platega) — "
        "без личных номеров и кривых QR.\n\n"
        "Баланс и заказы уже есть; пополнить можно будет сразу после подключения.\n"
        "/balance — посмотреть баланс · /order — заказ"
    )


def sbp_cfg(cfg: dict) -> dict:
    """
    Своя схема СБП (без чужих агрегаторов).
    phone — только для владельца (проверка в банке), клиенту по умолчанию НЕ показываем.
    Клиенту: bank + amount + code + link и/или QR-картинка.
    """
    block = cfg.get("sbp") if isinstance(cfg.get("sbp"), dict) else {}
    phone = (block.get("phone") or cfg.get("sbp_phone") or "").strip()
    bank = (block.get("bank") or cfg.get("sbp_bank") or "").strip()
    name = (block.get("name") or cfg.get("sbp_name") or "").strip()
    link = (block.get("link") or cfg.get("sbp_link") or "").strip()
    hint = (block.get("hint") or cfg.get("sbp_hint") or "").strip()
    qr = (block.get("qr_file") or cfg.get("sbp_qr_file") or "").strip()
    hide = block.get("hide_phone")
    if hide is None:
        hide = cfg.get("sbp_hide_phone")
    if hide is None:
        hide = True  # по умолчанию номер не светим
    hide = bool(hide)
    show_phone = (not hide) and bool(phone)
    qr_ok = False
    if qr:
        qp = Path(qr) if Path(qr).is_absolute() else (ROOT / qr)
        qr_ok = qp.is_file()
    # ready: link / живой QR / номер — и только если пополнение включено
    enabled = topup_enabled(cfg)
    ready = bool(enabled and (link or qr_ok or phone))
    # auto_credit: зачислить сразу по «Я оплатил» (24/7 без ожидания владельца)
    ac = block.get("auto_credit")
    if ac is None:
        ac = cfg.get("sbp_auto_credit")
    if ac is None:
        ac = True
    auto_max = int(block.get("auto_credit_max") or cfg.get("sbp_auto_credit_max") or 3000)
    return {
        "phone": phone,
        "bank": bank,
        "name": name,
        "link": link,
        "hint": hint,
        "qr_file": qr,
        "hide_phone": hide,
        "show_phone": show_phone,
        "ready": ready,
        "has_secret_phone": bool(phone),
        "qr_ok": qr_ok,
        "auto_credit": bool(ac),
        "auto_credit_max": max(TOPUP_MIN, auto_max),
        "enabled": enabled,
    }


def qr_path(cfg: dict) -> Path | None:
    s = sbp_cfg(cfg)
    q = s.get("qr_file") or ""
    if not q:
        return None
    p = Path(q)
    if not p.is_absolute():
        p = ROOT / p
    return p if p.is_file() else None


def _use_remote_balance() -> bool:
    """
    Cloud/Bothost → баланс на домашнем ПК через bridge.
    На самом bridge (ПК) всегда локальный файл.
    """
    if (os.environ.get("BALANCE_LOCAL") or "").strip() in ("1", "true", "yes"):
        return False
    if (os.environ.get("GROK_BRIDGE_DISABLE") or "").strip() in ("1", "true", "yes"):
        return False
    try:
        from state import load_config

        cfg = load_config() or {}
    except Exception:
        cfg = {}
    mode = str(cfg.get("bot_host_mode") or "").lower()
    cloud = mode in ("cloud", "bothost", "hosting", "remote") or bool(
        (os.environ.get("BOT_ID") or "").strip()
    )
    if not cloud:
        return False
    try:
        from content import _bridge_url

        return bool(_bridge_url(cfg))
    except Exception:
        return False


def _remote_call(path: str, payload: dict | None = None, *, method: str = "POST") -> dict:
    import requests
    from content import _bridge_secret, _bridge_url
    from state import load_config

    cfg = load_config()
    url = (_bridge_url(cfg) or "").rstrip("/")
    if not url:
        raise RuntimeError("no bridge for remote balance")
    headers = {"Content-Type": "application/json"}
    sec = _bridge_secret(cfg)
    if sec:
        headers["X-Bridge-Secret"] = sec
    if method.upper() == "GET":
        r = requests.get(f"{url}{path}", headers=headers, timeout=20)
    else:
        r = requests.post(f"{url}{path}", headers=headers, json=payload or {}, timeout=25)
    if r.status_code >= 400:
        raise RuntimeError(f"remote balance {r.status_code}: {r.text[:200]}")
    data = r.json() if r.content else {}
    if not data.get("ok"):
        raise RuntimeError(str(data.get("error") or data)[:200])
    return data


def get_balance(user_id: int) -> int:
    if _use_remote_balance():
        try:
            data = _remote_call(f"/balance?user_id={int(user_id)}", method="GET")
            return int(data.get("balance") or 0)
        except Exception as e:
            print("remote get_balance fail", e, flush=True)
            # не врём 0 молча — но UI ждёт int; 0 + лог
            return 0
    data = load()
    w = data["wallets"].get(str(int(user_id))) or {}
    return int(w.get("balance") or 0)


def _wallet(data: dict, user_id: int) -> dict:
    k = str(int(user_id))
    w = data["wallets"].setdefault(k, {"balance": 0, "username": "", "name": ""})
    w.setdefault("balance", 0)
    return w


def _ledger(
    data: dict,
    *,
    user_id: int,
    amount: int,
    kind: str,
    note: str = "",
    ref: str = "",
) -> None:
    data["ledger"].insert(
        0,
        {
            "id": new_id(),
            "user_id": int(user_id),
            "amount": int(amount),
            "kind": kind,
            "note": (note or "")[:200],
            "ref": (ref or "")[:64],
            "ts": int(time.time()),
            "balance_after": get_balance(user_id)
            if kind == "noop"
            else None,  # filled after mutate
        },
    )
    # trim
    data["ledger"] = data["ledger"][:500]


def credit(
    user_id: int,
    amount: int,
    *,
    kind: str = "credit",
    note: str = "",
    ref: str = "",
    username: str = "",
    name: str = "",
) -> int:
    """Зачислить. Возвращает новый баланс."""
    amount = int(amount)
    if amount <= 0:
        raise ValueError("amount must be > 0")
    if _use_remote_balance():
        data = _remote_call(
            "/balance/credit",
            {
                "user_id": int(user_id),
                "amount": amount,
                "kind": kind,
                "note": note,
                "ref": ref,
                "username": username,
                "name": name,
            },
        )
        return int(data.get("balance") or 0)
    data = load()
    w = _wallet(data, user_id)
    if username:
        w["username"] = username.lstrip("@")
    if name:
        w["name"] = name
    w["balance"] = int(w.get("balance") or 0) + amount
    bal = int(w["balance"])
    entry = {
        "id": new_id(),
        "user_id": int(user_id),
        "amount": amount,
        "kind": kind,
        "note": (note or "")[:200],
        "ref": (ref or "")[:64],
        "ts": int(time.time()),
        "balance_after": bal,
    }
    data["ledger"].insert(0, entry)
    data["ledger"] = data["ledger"][:500]
    save(data)
    return bal


def debit(
    user_id: int,
    amount: int,
    *,
    kind: str = "debit",
    note: str = "",
    ref: str = "",
) -> int:
    """Списать. ValueError если не хватает. Возвращает новый баланс."""
    amount = int(amount)
    if amount <= 0:
        raise ValueError("amount must be > 0")
    if _use_remote_balance():
        try:
            data = _remote_call(
                "/balance/debit",
                {
                    "user_id": int(user_id),
                    "amount": amount,
                    "kind": kind,
                    "note": note,
                    "ref": ref,
                },
            )
            return int(data.get("balance") or 0)
        except RuntimeError as e:
            msg = str(e)
            if "Недостаточно" in msg or "insufficient" in msg.lower():
                raise ValueError(msg)
            raise ValueError(msg)
    data = load()
    w = _wallet(data, user_id)
    bal = int(w.get("balance") or 0)
    if bal < amount:
        raise ValueError(f"Недостаточно средств: {bal} ₽, нужно {amount} ₽")
    w["balance"] = bal - amount
    new_bal = int(w["balance"])
    data["ledger"].insert(
        0,
        {
            "id": new_id(),
            "user_id": int(user_id),
            "amount": -amount,
            "kind": kind,
            "note": (note or "")[:200],
            "ref": (ref or "")[:64],
            "ts": int(time.time()),
            "balance_after": new_bal,
        },
    )
    data["ledger"] = data["ledger"][:500]
    save(data)
    return new_bal


def try_debit(user_id: int, amount: int, **kwargs: Any) -> tuple[bool, int, str]:
    """(ok, balance, err)."""
    try:
        bal = debit(user_id, amount, **kwargs)
        return True, bal, ""
    except ValueError as e:
        return False, get_balance(user_id), str(e)


def _unique_pay_amount(data: dict, base: int) -> int:
    """Уникальная сумма среди открытых заявок — проще найти перевод в банке."""
    used = set()
    for t in (data.get("topups") or {}).values():
        if t.get("status") not in ("wait_pay", "wait_confirm"):
            continue
        used.add(int(t.get("pay_exact") or t.get("amount") or 0))
    a = int(base)
    for _ in range(200):
        if a not in used and a >= TOPUP_MIN:
            return a
        a += 1
    return int(base) + (int(time.time()) % 97)


def create_topup(
    *,
    user_id: int,
    amount: int,
    username: str = "",
    name: str = "",
) -> dict:
    amount = int(amount)
    if amount < TOPUP_MIN or amount > TOPUP_MAX:
        raise ValueError(f"Сумма от {TOPUP_MIN} до {TOPUP_MAX} ₽")
    data = load()
    # один pending на пользователя
    for t in data["topups"].values():
        if (
            int(t.get("user_id") or 0) == int(user_id)
            and t.get("status") in ("wait_pay", "wait_confirm")
            and int(t.get("expires_at") or 0) > int(time.time())
        ):
            raise ValueError(
                f"Уже есть заявка #{t.get('id')} на {t.get('pay_exact') or t.get('amount')} ₽. "
                "Дождись подтверждения или отмени."
            )
    tid = new_id()
    code = f"VG{tid[-6:].upper()}"
    now = int(time.time())
    pay_exact = _unique_pay_amount(data, amount)
    item = {
        "id": tid,
        "code": code,
        "user_id": int(user_id),
        "username": (username or "").lstrip("@"),
        "name": name or username or str(user_id),
        "amount": amount,  # что выбрал клиент
        "pay_exact": pay_exact,  # сколько перевести (уникально, для сверки в банке)
        "status": "wait_pay",  # wait_pay → wait_confirm → done | rejected | expired | cancelled
        "created_at": now,
        "expires_at": now + TOPUP_TTL_SEC,
        "paid_at": None,
        "confirmed_at": None,
        "confirmed_by": None,
        "note": "",
    }
    data["topups"][tid] = item
    w = _wallet(data, user_id)
    if username:
        w["username"] = username.lstrip("@")
    if name:
        w["name"] = name
    save(data)
    return item


def get_topup(tid: str) -> dict | None:
    return (load().get("topups") or {}).get(str(tid))


def save_topup(item: dict) -> dict:
    data = load()
    item["updated_at"] = int(time.time())
    data.setdefault("topups", {})[str(item["id"])] = item
    save(data)
    return item


def mark_paid(tid: str, user_id: int) -> dict:
    item = get_topup(tid)
    if not item:
        raise ValueError("Заявка не найдена")
    if int(item.get("user_id") or 0) != int(user_id):
        raise ValueError("Это не ваша заявка")
    if item.get("status") not in ("wait_pay", "wait_confirm"):
        raise ValueError(f"Статус: {item.get('status')}")
    if int(item.get("expires_at") or 0) < int(time.time()):
        item["status"] = "expired"
        save_topup(item)
        raise ValueError("Заявка просрочена — создайте новую")
    item["status"] = "wait_confirm"
    item["paid_at"] = int(time.time())
    return save_topup(item)


def cancel_topup(tid: str, user_id: int, *, as_owner: bool = False) -> dict:
    item = get_topup(tid)
    if not item:
        raise ValueError("Заявка не найдена")
    if not as_owner and int(item.get("user_id") or 0) != int(user_id):
        raise ValueError("Это не ваша заявка")
    if item.get("status") in ("done", "rejected", "cancelled"):
        raise ValueError("Уже закрыта")
    item["status"] = "cancelled"
    return save_topup(item)


def confirm_topup(tid: str, owner_id: int) -> tuple[dict, int]:
    """Подтвердить СБП → зачислить. (topup, new_balance).

    Бот сам банк не видит. Зачисление = ты нажал «Зачислить» после проверки
    перевода в приложении банка (сумма pay_exact + код).
    """
    item = get_topup(tid)
    if not item:
        raise ValueError("Заявка не найдена")
    if item.get("status") == "done":
        raise ValueError("Уже зачислено")
    if item.get("status") not in ("wait_pay", "wait_confirm"):
        raise ValueError(f"Статус: {item.get('status')}")
    uid = int(item["user_id"])
    # зачисляем ровно то, что должны были перевести
    amount = int(item.get("pay_exact") or item.get("amount") or 0)
    bal = credit(
        uid,
        amount,
        kind="sbp_topup",
        note=f"СБП {item.get('code')} · {amount}₽",
        ref=str(tid),
        username=item.get("username") or "",
        name=item.get("name") or "",
    )
    item["status"] = "done"
    item["confirmed_at"] = int(time.time())
    item["confirmed_by"] = int(owner_id)
    save_topup(item)
    return item, bal


def reject_topup(tid: str, owner_id: int, note: str = "") -> dict:
    item = get_topup(tid)
    if not item:
        raise ValueError("Заявка не найдена")
    if item.get("status") == "done":
        raise ValueError("Уже зачислено — /balrev ID чтобы списать")
    item["status"] = "rejected"
    item["note"] = (note or "")[:200]
    item["confirmed_by"] = int(owner_id)
    item["confirmed_at"] = int(time.time())
    return save_topup(item)


def reverse_topup(tid: str, owner_id: int, note: str = "") -> tuple[dict, int]:
    """Отмена ошибочного/фейкового автозачисления: списать с баланса клиента."""
    item = get_topup(tid)
    if not item:
        raise ValueError("Заявка не найдена")
    if item.get("status") != "done":
        raise ValueError("Отменять можно только уже зачисленные (done)")
    if item.get("reversed"):
        raise ValueError("Уже отменено")
    amount = int(item.get("pay_exact") or item.get("amount") or 0)
    uid = int(item["user_id"])
    ok, new_bal, err = try_debit(
        uid,
        amount,
        kind="sbp_reverse",
        note=f"отмена {item.get('code')} {note}"[:200],
        ref=str(tid),
    )
    if not ok:
        raise ValueError(
            f"Не списать {amount} ₽: {err}. "
            "Клиент уже потратил — спиши вручную /balset"
        )
    item["status"] = "reversed"
    item["reversed"] = True
    item["reversed_at"] = int(time.time())
    item["reversed_by"] = int(owner_id)
    item["note"] = (note or item.get("note") or "")[:200]
    save_topup(item)
    return item, new_bal


def list_pending_topups(limit: int = 20) -> list[dict]:
    items = [
        t
        for t in (load().get("topups") or {}).values()
        if t.get("status") in ("wait_pay", "wait_confirm")
    ]
    items.sort(key=lambda x: int(x.get("created_at") or 0), reverse=True)
    return items[:limit]


def list_user_topups(user_id: int, limit: int = 10) -> list[dict]:
    items = [
        t
        for t in (load().get("topups") or {}).values()
        if int(t.get("user_id") or 0) == int(user_id)
    ]
    items.sort(key=lambda x: int(x.get("created_at") or 0), reverse=True)
    return items[:limit]


def list_ledger(user_id: int, limit: int = 12) -> list[dict]:
    items = [
        e
        for e in (load().get("ledger") or [])
        if int(e.get("user_id") or 0) == int(user_id)
    ]
    return items[:limit]


def format_balance_card(user_id: int, cfg: dict | None = None) -> str:
    import html as H

    bal = get_balance(user_id)
    lines = [
        "💰 <b>Баланс</b>",
        f"Доступно: <b>{bal}</b> ₽",
        "",
    ]
    if cfg is not None and not topup_enabled(cfg):
        lines.extend(
            [
                "Пополнение <b>скоро</b> (честная оплата через кассу).",
                "Сейчас пополнить нельзя — ждём подключение.",
                "",
                "/balance — обновить",
            ]
        )
    else:
        lines.extend(
            [
                "Пополнение — <b>СБП</b> / касса.",
                "Оплатил → зачисление на баланс.",
                "",
                "/topup — пополнить · /balance — обновить",
            ]
        )
    ledger = list_ledger(user_id, limit=5)
    if ledger:
        lines.append("")
        lines.append("<b>Последние операции:</b>")
        for e in ledger:
            amt = int(e.get("amount") or 0)
            sign = f"+{amt}" if amt >= 0 else str(amt)
            kind = H.escape(str(e.get("kind") or ""))
            note = H.escape(str(e.get("note") or "")[:40])
            lines.append(f"• {sign} ₽ · {kind}" + (f" · {note}" if note else ""))
    return "\n".join(lines)


def format_sbp_instructions(cfg: dict, topup: dict) -> str:
    import html as H

    s = sbp_cfg(cfg)
    pay = int(topup.get("pay_exact") or topup.get("amount") or 0)
    code = H.escape(str(topup.get("code") or ""))
    tid = H.escape(str(topup.get("id") or ""))
    lines = [
        "💳 <b>Пополнение через СБП</b>",
        "",
        f"Переведи <b>ровно {pay}</b> ₽",
        f"Код (если есть поле комментария): <code>{code}</code>",
        f"Заявка: <code>{tid}</code>",
        "",
    ]
    if not s["ready"]:
        lines.append(
            "⚠️ Приём СБП временно не настроен.\n"
            "Напишите владельцу или попробуйте позже."
        )
        return "\n".join(lines)

    lines.append("<b>Как оплатить:</b>")
    if s.get("bank"):
        lines.append(f"🏦 Банк: <b>{H.escape(s['bank'])}</b>")
    if s.get("qr_ok"):
        lines.append("📷 Отсканируй <b>QR</b> ниже (СБП).")
    elif s.get("link"):
        lines.append("📱 Кнопка «Оплатить СБП» ниже.")
    elif s.get("show_phone") and s.get("phone"):
        lines.append(f"📱 Телефон: <code>{H.escape(s['phone'])}</code>")
    else:
        lines.append("📱 Жми «Показать реквизиты», если QR нет.")

    lines.extend(
        [
            "",
            f"1) Сканируй QR / СБП",
            f"2) Сумма <b>строго {pay}</b> ₽ (не округляй)",
            f"3) Комментарий: <code>{code}</code> (если банк даёт)",
            "4) Оплати → «Я оплатил»",
            "",
            "⏱ После «Я оплатил» баланс обычно зачисляется <b>сразу</b> (24/7).",
            "Владелец потом сверяет перевод в банке.",
        ]
    )
    if s.get("hint"):
        lines.append(f"\n<i>{H.escape(s['hint'])}</i>")
    return "\n".join(lines)


def topup_user_keyboard(tid: str, cfg: dict | None = None) -> dict:
    s = sbp_cfg(cfg or {})
    rows: list[list[dict]] = []
    if s.get("link"):
        rows.append([{"text": "💳 Оплатить СБП", "url": s["link"]}])
    # показать реквизиты (номер) — только если phone есть и (не hide или нет link/qr)
    if s.get("has_secret_phone") and (
        not s.get("show_phone") or not (s.get("link") or s.get("qr_file"))
    ):
        rows.append(
            [{"text": "📋 Показать реквизиты", "callback_data": f"bal:reveal:{tid}"}]
        )
    rows.append([{"text": "✅ Я оплатил", "callback_data": f"bal:paid:{tid}"}])
    rows.append([{"text": "🚫 Отменить заявку", "callback_data": f"bal:cancel:{tid}"}])
    rows.append([{"text": "💰 Баланс", "callback_data": "bal:show"}])
    return {"inline_keyboard": rows}


def treasury_stats() -> dict:
    """Сводка «кассы» для владельца."""
    data = load()
    wallets = data.get("wallets") or {}
    topups = data.get("topups") or {}
    treasury = data.setdefault("treasury", {})
    total_balances = sum(int((w or {}).get("balance") or 0) for w in wallets.values())
    confirmed = 0
    pending_amt = 0
    pending_n = 0
    for t in topups.values():
        st = t.get("status")
        amt = int(t.get("amount") or 0)
        if st == "done":
            confirmed += amt
        elif st in ("wait_pay", "wait_confirm"):
            pending_amt += amt
            pending_n += 1
    withdrawn = int(treasury.get("withdrawn_total") or 0)
    # «на руках» по учёту: сколько реально пришло минус вывод (оценка)
    in_pocket = max(0, confirmed - withdrawn)
    return {
        "users_balance_sum": total_balances,
        "confirmed_topups": confirmed,
        "pending_amount": pending_amt,
        "pending_count": pending_n,
        "withdrawn_total": withdrawn,
        "in_pocket_estimate": in_pocket,
        "withdrawals": list(treasury.get("withdrawals") or [])[:20],
    }


def owner_cashout(amount: int, note: str = "") -> dict:
    """
    Учёт вывода владельцем (деньги уже на карте/в банке после СБП).
    Не трогает балансы клиентов — только журнал treasury.
    """
    amount = int(amount)
    if amount <= 0:
        raise ValueError("Сумма > 0")
    data = load()
    tr = data.setdefault("treasury", {})
    tr.setdefault("withdrawn_total", 0)
    tr.setdefault("withdrawals", [])
    tr["withdrawn_total"] = int(tr["withdrawn_total"]) + amount
    entry = {
        "id": new_id(),
        "amount": amount,
        "note": (note or "")[:200],
        "ts": int(time.time()),
        "withdrawn_total": int(tr["withdrawn_total"]),
    }
    tr["withdrawals"].insert(0, entry)
    tr["withdrawals"] = tr["withdrawals"][:100]
    data["treasury"] = tr
    save(data)
    return entry


def format_treasury() -> str:
    st = treasury_stats()
    lines = [
        "🏦 <b>Касса (учёт)</b>",
        "",
        f"Балансы клиентов суммарно: <b>{st['users_balance_sum']}</b> ₽",
        f"Подтверждённые пополнения СБП: <b>{st['confirmed_topups']}</b> ₽",
        f"Ждут оплаты/проверки: <b>{st['pending_amount']}</b> ₽ "
        f"({st['pending_count']} заявок)",
        f"Ты вывел (учёт): <b>{st['withdrawn_total']}</b> ₽",
        f"Оценка «пришло − вывод»: <b>{st['in_pocket_estimate']}</b> ₽",
        "",
        "<i>Реальные деньги приходят на твой банк по СБП.</i>",
        "<i>Баланс в боте — виртуальный (для заказов).</i>",
        "<i>Вывод: деньги уже у тебя → /cashout 5000 — записать в учёт.</i>",
        "",
        "/balpend · /cashout СУММА · /treasury",
    ]
    wds = st.get("withdrawals") or []
    if wds:
        lines.append("")
        lines.append("<b>Последние выводы:</b>")
        for w in wds[:5]:
            lines.append(f"• −{w.get('amount')} ₽ · {w.get('note') or '—'}")
    return "\n".join(lines)


def topup_status_label(st: str) -> str:
    return {
        "wait_pay": "⏳ ждём оплату",
        "wait_confirm": "🔍 проверка оплаты",
        "done": "✅ зачислено",
        "reversed": "↩️ отменено (списано)",
        "rejected": "❌ отклонено",
        "expired": "⌛ просрочено",
        "cancelled": "🚫 отменено",
    }.get(str(st or ""), str(st or "—"))


def balance_keyboard(cfg: dict | None = None) -> dict:
    rows: list[list[dict]] = []
    if cfg is None or topup_enabled(cfg):
        rows.append([{"text": "💳 Пополнить", "callback_data": "bal:topup"}])
        rows.append([{"text": "📜 Мои заявки", "callback_data": "bal:mytop"}])
    rows.append([{"text": "🔄 Обновить", "callback_data": "bal:show"}])
    return {"inline_keyboard": rows}


def topup_amounts_keyboard() -> dict:
    rows = []
    row = []
    for a in TOPUP_PRESETS:
        row.append({"text": f"{a} ₽", "callback_data": f"bal:amt:{a}"})
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "✏️ Своя сумма", "callback_data": "bal:custom"}])
    rows.append([{"text": "◀️ К балансу", "callback_data": "bal:show"}])
    return {"inline_keyboard": rows}


def topup_owner_keyboard(tid: str, *, mode: str = "confirm") -> dict:
    """mode=confirm | review (после автозачисления)."""
    if mode == "review":
        return {
            "inline_keyboard": [
                [
                    {"text": "👍 В банке ок", "callback_data": f"bal:seen:{tid}"},
                    {"text": "↩️ Списать (фейк)", "callback_data": f"bal:rev:{tid}"},
                ]
            ]
        }
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Зачислить", "callback_data": f"bal:ok:{tid}"},
                {"text": "❌ Отклонить", "callback_data": f"bal:no:{tid}"},
            ]
        ]
    }
