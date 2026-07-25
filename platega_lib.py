# -*- coding: utf-8 -*-
"""
Platega: создание платежа + webhook «пришло / не пришло».

Когда будет ключ и webhook URL (Bothost/tunnel):
  1) payments.topup_enabled = true
  2) payments.provider = platega
  3) env: PLATEGA_API_KEY, PLATEGA_WEBHOOK_SECRET, PLATEGA_SHOP_ID (по доке)
  4) В кабинете Platega: webhook → https://YOUR_HOST/platega/webhook
     (если Bothost не даёт HTTP — туннель на ПК или reverse proxy)

Сейчас: заготовки + apply_success → credit баланса (через balance_lib,
на cloud уходит на home-bridge).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Any

import balance_lib as bal
from state import new_id

ROOT = Path(__file__).resolve().parent
PAYMENTS_PATH = ROOT / "media" / "platega_payments.json"


def _cfg_payments(cfg: dict | None = None) -> dict:
    cfg = cfg or {}
    pay = cfg.get("payments") if isinstance(cfg.get("payments"), dict) else {}
    return pay


def enabled(cfg: dict | None = None) -> bool:
    pay = _cfg_payments(cfg)
    if str(pay.get("provider") or "platega").lower() != "platega":
        return False
    return bool(pay.get("topup_enabled")) and bool(
        (os.environ.get("PLATEGA_API_KEY") or pay.get("api_key") or "").strip()
    )


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


def create_payment(
    cfg: dict,
    *,
    user_id: int,
    amount: int,
    username: str = "",
    description: str = "Пополнение баланса Vaggo",
) -> dict:
    """
    Создать платёж. Пока без реального API — запись pending + инструкция.
    Когда будет API key — здесь POST в Platega, вернётся pay_url.
    """
    amount = int(amount)
    if amount < bal.TOPUP_MIN or amount > bal.TOPUP_MAX:
        raise ValueError(f"Сумма от {bal.TOPUP_MIN} до {bal.TOPUP_MAX} ₽")

    pid = new_id()
    item = {
        "id": pid,
        "user_id": int(user_id),
        "username": (username or "").lstrip("@"),
        "amount": amount,
        "status": "pending",  # pending|paid|failed|expired
        "provider": "platega",
        "pay_url": "",
        "external_id": "",
        "description": (description or "")[:200],
        "created_at": int(time.time()),
        "paid_at": None,
        "raw": {},
    }

    # TODO: реальный create payment API Platega
    api_key = (
        (os.environ.get("PLATEGA_API_KEY") or "").strip()
        or str(_cfg_payments(cfg).get("api_key") or "").strip()
    )
    if api_key:
        item["status"] = "pending_api"
        item["note"] = "API key есть — подключите endpoint create в platega_lib"
    else:
        item["status"] = "pending"
        item["note"] = "Ждём PLATEGA_API_KEY; баланс можно /baladd"

    data = _load()
    data["items"][pid] = item
    _save(data)
    return item


def get_payment(pid: str) -> dict | None:
    return (_load().get("items") or {}).get(str(pid))


def apply_success(
    pid: str,
    *,
    external_id: str = "",
    raw: dict | None = None,
) -> dict:
    """
    Оплата подтверждена (webhook / ручная проверка).
    Идемпотентно: повторный success не зачисляет дважды.
    """
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
        ref=str(external_id or pid)[:64],
        username=str(item.get("username") or ""),
    )
    item["status"] = "paid"
    item["paid_at"] = int(time.time())
    item["external_id"] = str(external_id or item.get("external_id") or "")
    if raw:
        item["raw"] = raw
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


def verify_webhook_signature(body: bytes, signature: str, secret: str) -> bool:
    """HMAC-SHA256(body, secret) == signature (типовой вариант; сверить с докой Platega)."""
    if not secret or not signature:
        return False
    dig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(dig, signature.strip().lower().removeprefix("sha256="))


def handle_webhook_payload(payload: dict) -> dict:
    """
    Универсальный разбор webhook.
    Подстрой поля под реальную доку Platega (status/order_id/amount).
    """
    status = str(
        payload.get("status")
        or payload.get("payment_status")
        or payload.get("state")
        or ""
    ).lower()
    meta = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    pid = str(
        payload.get("order_id")
        or payload.get("payment_id")
        or meta.get("pid")
        or payload.get("id")
        or ""
    )
    external = str(payload.get("transaction_id") or payload.get("external_id") or "")

    # если pid не наш — ищем по external
    if pid and not get_payment(pid):
        data = _load()
        for k, it in (data.get("items") or {}).items():
            if str(it.get("external_id") or "") == pid:
                pid = k
                break

    paid_ok = status in (
        "paid",
        "success",
        "succeeded",
        "completed",
        "done",
        "ok",
        "confirmed",
    )
    if paid_ok and pid:
        return {"ok": True, "action": "credit", "payment": apply_success(pid, external_id=external, raw=payload)}
    if status in ("fail", "failed", "canceled", "cancelled", "expired") and pid:
        return {"ok": True, "action": "fail", "payment": apply_fail(pid, reason=status)}
    return {"ok": False, "error": "unhandled", "status": status, "pid": pid}
