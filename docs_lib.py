# -*- coding: utf-8 -*-
"""
Договор / акт по заказу — текстовые документы (UTF-8 .txt).
PDF с кириллицей без шрифтов в чистом Python кривой → .txt официально ок для MVP.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DOCS_DIR = ROOT / "media" / "docs"


def _ensure_dir() -> Path:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    return DOCS_DIR


def _party_client(item: dict) -> str:
    un = (item.get("username") or "").lstrip("@")
    name = item.get("name") or un or str(item.get("user_id") or "")
    if un:
        return f"{name} (@{un}, Telegram id {item.get('user_id')})"
    return f"{name} (Telegram id {item.get('user_id')})"


def build_contract_text(item: dict, *, studio: str = "Вагго / Director Vaggo") -> str:
    oid = item.get("id") or "—"
    kind = item.get("kind") or "other"
    try:
        import orders_lib as orders

        title = orders.ORDER_TYPES.get(kind, {}).get("title") or kind
        includes = orders.ORDER_TYPES.get(kind, {}).get("includes") or "по ТЗ"
        not_inc = orders.ORDER_TYPES.get(kind, {}).get("not_includes") or "хостинг, домен"
    except Exception:
        title, includes, not_inc = kind, "по ТЗ", "хостинг"
    price = int(item.get("price") or 0)
    brief = (item.get("brief") or "—").strip()
    ts = time.strftime("%d.%m.%Y", time.localtime(int(item.get("created_at") or time.time())))
    client = _party_client(item)

    return f"""ДОГОВОР-ОФЕРТА НА ВЫПОЛНЕНИЕ РАБОТ № {oid}
Дата: {ts}

1. СТОРОНЫ
Исполнитель: {studio} (далее — Исполнитель).
Заказчик: {client} (далее — Заказчик).

2. ПРЕДМЕТ
Исполнитель обязуется выполнить работы: «{title}» по техническому заданию Заказчика,
а Заказчик — принять и оплатить результат.

3. СТОИМОСТЬ И ОПЛАТА
3.1. Цена фиксирована: {price} (рублей РФ).
3.2. Оплата: с баланса в Telegram-боте @DirectorVaggobot либо иным согласованным способом.
3.3. Изменение объёма работ — только по письменному согласию сторон (в т.ч. в чате бота).

4. СОСТАВ РАБОТ
4.1. Входит: {includes}
4.2. Не входит: {not_inc}
4.3. Хостинг, VPS, домен, реклама, сторы — за счёт Заказчика.

5. ТЕХНИЧЕСКОЕ ЗАДАНИЕ
{brief[:3500]}

6. СРОКИ
Срок выполнения — ориентировочный, согласно переписке и загрузке.
Задержки по вине Заказчика (нет доступов/ответов) сдвигают срок.

7. ПРИЁМКА И ГАРАНТИЯ
7.1. Результат передаётся в личку бота / файлом.
7.2. Гарантия 2 (двое) суток с момента сдачи; бесплатные правки в рамках ТЗ — 1 сутки.
7.3. Правки вне ТЗ — отдельный заказ.

8. ОТВЕТСТВЕННОСТЬ
Стороны не несут ответственности за косвенные убытки.
Исполнитель не отвечает за действия третьих лиц и работу чужого хостинга.

9. ПЕРСОНАЛЬНЫЕ ДАННЫЕ
Обработка данных — в соответствии с политикой и пользовательским соглашением бота.

10. РЕКВИЗИТЫ / КОНТАКТ
Заказчик: {client}
Исполнитель: поддержка в боте / @Vagdar1 · канал @Vaggo01

Документ сформирован автоматически ботом Director Vaggo.
Номер заказа: {oid}
"""


def build_act_text(item: dict, *, studio: str = "Вагго / Director Vaggo") -> str:
    oid = item.get("id") or "—"
    kind = item.get("kind") or "other"
    try:
        import orders_lib as orders

        title = orders.ORDER_TYPES.get(kind, {}).get("title") or kind
    except Exception:
        title = kind
    price = int(item.get("price") or 0)
    ts = time.strftime("%d.%m.%Y", time.localtime())
    client = _party_client(item)
    note = (item.get("deliver_note") or "Результат передан в Telegram.").strip()

    return f"""АКТ ВЫПОЛНЕННЫХ РАБОТ № {oid}
Дата: {ts}

Исполнитель: {studio}
Заказчик: {client}

1. По договору-оферте (заказ {oid}) выполнены работы:
   «{title}»

2. Стоимость: {price} ₽ (оплачено / списано с баланса согласно учёту бота).

3. Результат: {note[:800]}

4. Заказчик претензий по объёму в рамках ТЗ на момент подписания не имеет
   (либо направит в срок гарантии 2 суток).

5. Стороны подтверждают исполнение обязательств по данному заказу.

Документ сформирован автоматически. Заказ: {oid}
"""


def write_contract_files(item: dict) -> tuple[Path, Path]:
    """Пишет договор и акт, возвращает пути."""
    d = _ensure_dir()
    oid = str(item.get("id") or "order")
    cpath = d / f"dogovor_{oid}.txt"
    apath = d / f"akt_{oid}.txt"
    cpath.write_text(build_contract_text(item), encoding="utf-8")
    apath.write_text(build_act_text(item), encoding="utf-8")
    return cpath, apath


def build_case_draft(item: dict) -> str:
    """Черновик кейса для канала (владелец решает публиковать)."""
    kind = item.get("kind") or "other"
    try:
        import orders_lib as orders

        title = orders.ORDER_TYPES.get(kind, {}).get("title") or kind
    except Exception:
        title = kind
    price = item.get("price") or 0
    brief = (item.get("brief") or "")[:400]
    return (
        f"📰 <b>Кейс · черновик</b> (не опубликован)\n\n"
        f"<b>Проект:</b> {title}\n"
        f"<b>Бюджет:</b> {price} ₽ (фикс)\n"
        f"<b>Стек / суть:</b>\n{brief}\n\n"
        f"<b>До:</b> не было решения / ручной процесс\n"
        f"<b>После:</b> готовый результат по ТЗ, сдан клиенту\n\n"
        f"<i>Отредактируй и опубликуй вручную или /draft</i>\n"
        f"id заказа <code>{item.get('id')}</code>"
    )
