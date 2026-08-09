# -*- coding: utf-8 -*-
"""
Platega.io — создание платежа (СБП QR) + подтверждение.

Документация: https://docs.platega.io/
Base: https://app.platega.io/
Auth headers:
  X-MerchantId
  X-Secret

Создание: POST /transaction/process
Статус:   GET  /transaction/{id}
Callback: status CONFIRMED | CANCELED, id = transaction uuid

Env (Bothost):
  PLATEGA_MERCHANT_ID=...
  PLATEGA_SECRET=...
  PLATEGA_RETURN_URL=https://t.me/DirectorVaggobot  (опц.)
  PLATEGA_PAYMENT_METHOD=2  (2=СБП QR)

На Bothost часто нет входящего HTTP → webhook опционален,
подтверждение через poll_pending() в цикле бота.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import requests

import balance_lib as bal
from state import new_id

ROOT = Path(__file__).resolve().parent
PAYMENTS_PATH = ROOT / "media" / "platega_payments.json"
API_BASE = (os.environ.get("PLATEGA_API_BASE") or "https://app.platega.io").rstrip("/")

# 2 = СБП QR (docs)
METHOD_SBP = 2


def _cfg_payments(cfg: dict | None = None) -> dict:
    cfg = cfg or {}
    pay = cfg.get("payments") if isinstance(cfg.get("payments"), dict) else {}
    return pay


SEALED_PATH = ROOT / "platega_sealed.json"


def _xor_seal(raw: bytes, token: str) -> bytes:
    import hashlib

    key = hashlib.sha256((token or "vaggo").encode("utf-8")).digest()
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(raw))


def write_sealed_credentials(
    merchant_id: str,
    secret: str,
    *,
    bot_token: str,
    path: Path | None = None,
) -> Path:
    """Зашифровать ключи под bot_token (Bothost уже знает token). Не plaintext в git."""
    import base64

    payload = json.dumps(
        {"merchant_id": merchant_id.strip(), "secret": secret.strip(), "v": 1},
        ensure_ascii=False,
    ).encode("utf-8")
    blob = base64.urlsafe_b64encode(_xor_seal(payload, bot_token)).decode("ascii")
    out = path or SEALED_PATH
    out.write_text(
        json.dumps(
            {
                "note": "sealed with bot_token; do not edit by hand",
                "alg": "xor-sha256-v1",
                "blob": blob,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return out


def _read_sealed(cfg: dict | None = None) -> tuple[str, str]:
    cfg = cfg or {}
    token = (cfg.get("bot_token") or os.environ.get("BOT_TOKEN") or "").strip()
    if not token:
        return "", ""
    path = SEALED_PATH
    if not path.is_file():
        # deploy package path
        alt = ROOT / "media" / "platega_sealed.json"
        path = alt if alt.is_file() else path
    if not path.is_file():
        return "", ""
    try:
        import base64

        meta = json.loads(path.read_text(encoding="utf-8"))
        blob = base64.urlsafe_b64decode((meta.get("blob") or "").encode("ascii"))
        raw = _xor_seal(blob, token)  # xor again = decrypt
        data = json.loads(raw.decode("utf-8"))
        return (
            str(data.get("merchant_id") or "").strip(),
            str(data.get("secret") or "").strip(),
        )
    except Exception as e:
        print("platega sealed read fail", type(e).__name__, str(e)[:80], flush=True)
        return "", ""


def credentials(cfg: dict | None = None) -> tuple[str, str]:
    pay = _cfg_payments(cfg)
    mid = (
        (os.environ.get("PLATEGA_MERCHANT_ID") or "").strip()
        or (os.environ.get("PLATEGA_SHOP_ID") or "").strip()
        or str(pay.get("merchant_id") or pay.get("shop_id") or "").strip()
    )
    secret = (
        (os.environ.get("PLATEGA_SECRET") or "").strip()
        or (os.environ.get("PLATEGA_API_KEY") or "").strip()
        or str(pay.get("secret") or pay.get("api_key") or "").strip()
    )
    if not mid or not secret:
        sm, ss = _read_sealed(cfg)
        mid = mid or sm
        secret = secret or ss
    return mid, secret


def ready(cfg: dict | None = None) -> bool:
    """Ключи есть + (опц.) topup_enabled не выключен жёстко."""
    mid, sec = credentials(cfg)
    if not mid or not sec:
        return False
    pay = _cfg_payments(cfg)
    if pay.get("topup_enabled") is False and not pay.get("force_platega"):
        # если явно false — но ключи есть, всё равно ready для API;
        # включение UI — через topup_enabled / bal.topup_enabled
        pass
    return True


def enabled(cfg: dict | None = None) -> bool:
    """UI пополнения через Platega."""
    if not ready(cfg):
        return False
    pay = _cfg_payments(cfg)
    if "topup_enabled" in pay:
        return bool(pay.get("topup_enabled"))
    return True


def _headers(cfg: dict | None = None) -> dict[str, str]:
    mid, sec = credentials(cfg)
    return {
        "X-MerchantId": mid,
        "X-Secret": sec,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _load() -> dict:
    if not PAYMENTS_PATH.exists():
        return {"items": {}, "updated_at": 0}
    try:
        data = json.loads(PAYMENTS_PATH.read_text(encoding="utf-8"))
        data.setdefault("items", {})
        return data
    except Exception:
        return {"items": {}, "updated_at": 0}


def _save(data: dict) -> None:
    data["updated_at"] = int(time.time())
    PAYMENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PAYMENTS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PAYMENTS_PATH)


def get_payment(pid: str) -> dict | None:
    return (_load().get("items") or {}).get(str(pid))


def find_by_external(external_id: str) -> dict | None:
    ext = str(external_id or "").strip()
    if not ext:
        return None
    for it in (_load().get("items") or {}).values():
        if str(it.get("external_id") or "") == ext:
            return it
    return None


def list_pending(limit: int = 40) -> list[dict]:
    items = []
    for it in (_load().get("items") or {}).values():
        if str(it.get("status") or "") in ("pending", "pending_api", "waiting"):
            if it.get("external_id") or it.get("pay_url"):
                items.append(it)
    items.sort(key=lambda x: int(x.get("created_at") or 0), reverse=True)
    return items[:limit]


def create_payment(
    cfg: dict,
    *,
    user_id: int,
    amount: int,
    username: str = "",
    description: str = "Пополнение баланса Vaggo",
) -> dict:
    """
    Создать платёж в Platega. Возвращает item с pay_url при успехе.
    """
    amount = int(amount)
    if amount < bal.TOPUP_MIN or amount > bal.TOPUP_MAX:
        raise ValueError(f"Сумма от {bal.TOPUP_MIN} до {bal.TOPUP_MAX} ₽")
    if not ready(cfg):
        raise RuntimeError(
            "Platega: нет PLATEGA_MERCHANT_ID / PLATEGA_SECRET "
            "(Bothost env или payments.merchant_id + secret)"
        )

    pid = new_id()
    pay = _cfg_payments(cfg)
    method = int(
        os.environ.get("PLATEGA_PAYMENT_METHOD")
        or pay.get("payment_method")
        or METHOD_SBP
    )
    bot_user = (
        (cfg.get("support") or {}).get("bot")
        or (cfg.get("bot_username") or "DirectorVaggobot")
    )
    bot_user = str(bot_user).lstrip("@")
    return_url = (
        (os.environ.get("PLATEGA_RETURN_URL") or "").strip()
        or str(pay.get("return_url") or "").strip()
        or f"https://t.me/{bot_user}?start=pay_ok"
    )
    failed_url = (
        (os.environ.get("PLATEGA_FAILED_URL") or "").strip()
        or str(pay.get("failed_url") or "").strip()
        or f"https://t.me/{bot_user}?start=pay_fail"
    )

    item: dict[str, Any] = {
        "id": pid,
        "user_id": int(user_id),
        "username": (username or "").lstrip("@"),
        "amount": amount,
        "status": "pending",
        "provider": "platega",
        "pay_url": "",
        "external_id": "",
        "description": (description or "")[:200],
        "created_at": int(time.time()),
        "paid_at": None,
        "raw": {},
        "payment_method": method,
    }

    body = {
        "paymentMethod": method,
        "paymentDetails": {"amount": float(amount), "currency": "RUB"},
        "description": (description or f"Vaggo balance {amount} RUB")[:200],
        "return": return_url,
        "failedUrl": failed_url,
        # payload — наш внутренний id, вернётся/сохраним для связки
        "payload": pid,
    }

    try:
        r = requests.post(
            f"{API_BASE}/transaction/process",
            headers=_headers(cfg),
            json=body,
            timeout=30,
        )
        raw_text = (r.text or "")[:2000]
        try:
            data = r.json() if r.content else {}
        except Exception:
            data = {"_raw": raw_text}
        if r.status_code >= 400:
            item["status"] = "failed"
            item["fail_reason"] = f"HTTP {r.status_code}: {raw_text[:300]}"
            item["raw"] = data if isinstance(data, dict) else {"_raw": raw_text}
            store = _load()
            store["items"][pid] = item
            _save(store)
            raise RuntimeError(
                f"Platega create fail HTTP {r.status_code}: {raw_text[:200]}"
            )

        if not isinstance(data, dict):
            data = {}
        ext = str(
            data.get("transactionId")
            or data.get("transaction_id")
            or data.get("id")
            or ""
        ).strip()
        redirect = str(
            data.get("redirect")
            or data.get("paymentUrl")
            or data.get("payment_url")
            or data.get("url")
            or data.get("qr")
            or ""
        ).strip()
        item["external_id"] = ext
        item["pay_url"] = redirect
        item["status"] = "waiting" if redirect else "pending_api"
        item["raw"] = data
        item["expires_in"] = data.get("expiresIn") or data.get("expires_in") or ""
        # фактическая сумма к оплате (иногда +комиссия)
        pay_amt = None
        pd = data.get("paymentDetails")
        if isinstance(pd, dict) and pd.get("amount") is not None:
            try:
                pay_amt = float(pd.get("amount"))
            except Exception:
                pay_amt = None
        elif isinstance(pd, str):
            # "114.00 RUB"
            try:
                pay_amt = float(pd.replace("RUB", "").replace("₽", "").strip().split()[0])
            except Exception:
                pay_amt = None
        if pay_amt is not None:
            item["pay_amount"] = pay_amt
        if not redirect:
            item["note"] = "API ok, но нет redirect URL — проверь ответ Platega"
    except RuntimeError:
        raise
    except Exception as e:
        item["status"] = "failed"
        item["fail_reason"] = f"{type(e).__name__}: {e}"[:300]
        store = _load()
        store["items"][pid] = item
        _save(store)
        raise RuntimeError(f"Platega network/API: {e}") from e

    store = _load()
    store["items"][pid] = item
    _save(store)
    return item


def get_transaction_status(cfg: dict, external_id: str) -> dict:
    """GET /transaction/{id}"""
    ext = str(external_id or "").strip()
    if not ext:
        raise ValueError("no external_id")
    r = requests.get(
        f"{API_BASE}/transaction/{ext}",
        headers=_headers(cfg),
        timeout=20,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"status HTTP {r.status_code}: {(r.text or '')[:200]}")
    data = r.json() if r.content else {}
    return data if isinstance(data, dict) else {}


def apply_success(
    pid: str,
    *,
    external_id: str = "",
    raw: dict | None = None,
) -> dict:
    """Оплата подтверждена. Идемпотентно."""
    data = _load()
    item = (data.get("items") or {}).get(str(pid))
    if not item:
        raise ValueError("payment not found")
    if item.get("status") == "paid":
        return item

    uid = int(item.get("user_id") or 0)
    amount = int(item.get("amount") or 0)
    if uid <= 0 or amount <= 0:
        raise ValueError("bad payment")

    bal.credit(
        uid,
        amount,
        kind="platega",
        note=f"Platega {pid}",
        ref=str(external_id or item.get("external_id") or pid)[:64],
        username=str(item.get("username") or ""),
    )
    item["status"] = "paid"
    item["paid_at"] = int(time.time())
    item["external_id"] = str(external_id or item.get("external_id") or "")
    if raw:
        item["raw_paid"] = raw
    data["items"][str(pid)] = item
    _save(data)
    return item


def apply_fail(pid: str, *, reason: str = "") -> dict:
    data = _load()
    item = (data.get("items") or {}).get(str(pid))
    if not item:
        raise ValueError("payment not found")
    if item.get("status") == "paid":
        return item
    item["status"] = "failed"
    item["fail_reason"] = (reason or "")[:200]
    item["failed_at"] = int(time.time())
    data["items"][str(pid)] = item
    _save(data)
    return item


def _status_is_paid(status: str) -> bool:
    s = (status or "").strip().upper()
    return s in (
        "CONFIRMED",
        "PAID",
        "SUCCESS",
        "SUCCEEDED",
        "COMPLETED",
        "DONE",
        "OK",
    )


def _status_is_fail(status: str) -> bool:
    s = (status or "").strip().upper()
    return s in (
        "CANCELED",
        "CANCELLED",
        "FAILED",
        "FAIL",
        "EXPIRED",
        "REJECTED",
    )


def _status_is_chargeback(status: str) -> bool:
    s = (status or "").strip().upper()
    return s in ("CHARGEBACK", "REFUNDED", "REFUND", "REVERSED")


def handle_webhook_payload(payload: dict) -> dict:
    """
    Callback Platega:
      { id, amount, currency, status: CONFIRMED|CANCELED, paymentMethod }
    """
    if not isinstance(payload, dict):
        return {"ok": False, "error": "not object"}

    status = str(payload.get("status") or payload.get("payment_status") or "")
    ext = str(payload.get("id") or payload.get("transactionId") or "").strip()
    payload_pid = str(payload.get("payload") or "").strip()

    item = None
    pid = ""
    if payload_pid and get_payment(payload_pid):
        item = get_payment(payload_pid)
        pid = payload_pid
    if not item and ext:
        item = find_by_external(ext)
        if item:
            pid = str(item.get("id") or "")
    if not item and ext:
        # иногда id в callback = наш payload
        if get_payment(ext):
            item = get_payment(ext)
            pid = ext

    if not item or not pid:
        return {"ok": False, "error": "payment not found", "external": ext, "status": status}

    if _status_is_paid(status):
        paid = apply_success(pid, external_id=ext or str(item.get("external_id") or ""), raw=payload)
        return {"ok": True, "action": "credit", "payment": paid}
    if _status_is_fail(status):
        failed = apply_fail(pid, reason=status)
        return {"ok": True, "action": "fail", "payment": failed}
    if _status_is_chargeback(status):
        # возврат: помечаем failed + пробуем списать с баланса (если ещё есть)
        try:
            uid = int(item.get("user_id") or 0)
            amount = int(item.get("amount") or 0)
            if uid and amount and item.get("status") == "paid":
                try:
                    bal.debit(
                        uid,
                        amount,
                        kind="platega_chargeback",
                        note=f"CHARGEBACK {pid}",
                        ref=str(ext or pid)[:64],
                    )
                except Exception as de:
                    print("platega chargeback debit", de, flush=True)
        except Exception:
            pass
        failed = apply_fail(pid, reason="CHARGEBACK")
        return {"ok": True, "action": "chargeback", "payment": failed}
    return {"ok": False, "error": "unhandled", "status": status, "pid": pid}


def sync_payment_status(cfg: dict, item: dict) -> dict | None:
    """Проверить один платёж в API. Вернёт updated item или None."""
    if not item:
        return None
    if item.get("status") == "paid":
        return item
    ext = str(item.get("external_id") or "").strip()
    if not ext:
        return None
    try:
        data = get_transaction_status(cfg, ext)
    except Exception as e:
        print("platega status", ext, type(e).__name__, str(e)[:100], flush=True)
        return None
    st = str(data.get("status") or "")
    pid = str(item.get("id") or "")
    if _status_is_paid(st):
        return apply_success(pid, external_id=ext, raw=data)
    if _status_is_fail(st):
        return apply_fail(pid, reason=st)
    # обновить raw
    store = _load()
    it = (store.get("items") or {}).get(pid)
    if it:
        it["raw_status"] = data
        it["last_poll"] = int(time.time())
        store["items"][pid] = it
        _save(store)
        return it
    return None


def poll_pending(cfg: dict, *, limit: int = 25) -> list[dict]:
    """Опросить pending — зачислить оплаченные. Возвращает список событий."""
    if not ready(cfg):
        return []
    events: list[dict] = []
    for it in list_pending(limit):
        # не дёргать API сразу (дать время на оплату)
        age = int(time.time()) - int(it.get("created_at") or 0)
        if age < 8:
            continue
        # старше 6 часов — помечаем expired без API-spam
        if age > 6 * 3600 and it.get("status") != "paid":
            try:
                apply_fail(str(it["id"]), reason="expired_local")
            except Exception:
                pass
            continue
        updated = sync_payment_status(cfg, it)
        if updated and updated.get("status") == "paid":
            events.append({"action": "credit", "payment": updated})
        elif updated and updated.get("status") == "failed":
            events.append({"action": "fail", "payment": updated})
    return events


def format_pay_message(item: dict) -> str:
    import html as H

    amount = int(item.get("amount") or 0)
    pay_amt = item.get("pay_amount")
    try:
        pay_amt_f = float(pay_amt) if pay_amt is not None else float(amount)
    except Exception:
        pay_amt_f = float(amount)
    pid = H.escape(str(item.get("id") or ""))
    url = str(item.get("pay_url") or "").strip()
    exp = H.escape(str(item.get("expires_in") or ""))
    lines = [
        "💳 <b>Пополнение через Platega</b>",
        "",
        f"На баланс: <b>{amount}</b> ₽",
    ]
    if abs(pay_amt_f - float(amount)) >= 0.01:
        lines.append(f"К оплате (с комиссией): <b>{pay_amt_f:g}</b> ₽")
    else:
        lines.append(f"К оплате: <b>{amount}</b> ₽")
    lines += [
        f"Заявка: <code>{pid}</code>",
        "",
        "1) Жми «Оплатить» — СБП / страница оплаты",
        "2) Оплати сумму «к оплате»",
        "3) На баланс придёт выбранная сумма (авто, 1–2 мин)",
        "",
        "Не пришло — «Проверить оплату» или /balance · /support",
    ]
    if exp:
        lines.insert(5, f"Таймер: <code>{exp}</code>")
    if not url:
        lines.append("")
        lines.append("⚠️ Ссылка оплаты не пришла — напиши /support")
    return "\n".join(lines)


def pay_keyboard(item: dict) -> dict:
    url = str(item.get("pay_url") or "").strip()
    pid = str(item.get("id") or "")
    rows: list[list[dict]] = []
    if url:
        rows.append([{"text": "💳 Оплатить", "url": url}])
    rows.append(
        [
            {"text": "🔄 Проверить оплату", "callback_data": f"pay:check:{pid}"},
            {"text": "❌ Отмена", "callback_data": f"pay:cancel:{pid}"},
        ]
    )
    rows.append([{"text": "◀️ Баланс", "callback_data": "bal:show"}])
    return {"inline_keyboard": rows}
