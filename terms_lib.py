# -*- coding: utf-8 -*-
"""
Политика, правила, риски, гарантии.
Пользователь должен принять TERMS_VERSION, иначе сервисы бота закрыты.
Хранение: media/terms_accept.json
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
PATH = ROOT / "media" / "terms_accept.json"
_LOCK = threading.Lock()

# при изменении текста — подними версию, всех попросит принять заново
TERMS_VERSION = "2026-07-18-v6"

# Гарантийные сроки (сутки = 24 часа с момента сдачи / статуса done)
GUARANTEE_DAYS = 2  # общая гарантия на проект после сдачи
REWORK_DAYS = 1  # бесплатные правки/изменения в рамках ТЗ

# Постоянные ссылки для банка / Platega (всегда доступны кнопками)
PRIVACY_URL = "https://telegra.ph/Politika-konfidencialnosti-06-21-31"
AGREEMENT_URL = "https://telegra.ph/Polzovatelskoe-soglashenie-04-01-19"

# Контакты поддержки (не группа — username / бот / почта)
DEFAULT_SUPPORT = {
    "telegram": "Vagdar1",  # личка владельца
    "bot": "DirectorVaggobot",
    "email": "",  # опционально, из config.support.email
    "channel": "Vaggo01",
}


def _default() -> dict:
    return {"version": TERMS_VERSION, "users": {}, "updated_at": 0}


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
            return data
        except Exception:
            return _default()


def save(data: dict) -> None:
    with _LOCK:
        data["updated_at"] = int(time.time())
        PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(PATH)


def is_accepted(user_id: int) -> bool:
    data = load()
    u = (data.get("users") or {}).get(str(int(user_id))) or {}
    return bool(u.get("accepted")) and str(u.get("version") or "") == TERMS_VERSION


def accept(user_id: int, *, username: str = "", name: str = "") -> dict:
    data = load()
    data.setdefault("users", {})[str(int(user_id))] = {
        "accepted": True,
        "version": TERMS_VERSION,
        "username": (username or "").lstrip("@"),
        "name": name or "",
        "accepted_at": int(time.time()),
        "declined": False,
    }
    save(data)
    return data["users"][str(int(user_id))]


def decline(user_id: int, *, username: str = "") -> dict:
    data = load()
    data.setdefault("users", {})[str(int(user_id))] = {
        "accepted": False,
        "version": TERMS_VERSION,
        "username": (username or "").lstrip("@"),
        "declined": True,
        "declined_at": int(time.time()),
    }
    save(data)
    return data["users"][str(int(user_id))]


def terms_parts() -> list[str]:
    """Полный текст по частям (лимит Telegram 4096)."""
    v = TERMS_VERSION
    g = GUARANTEE_DAYS
    r = REWORK_DAYS
    p1 = (
        f"📜 <b>Условия · ч.1/4</b>\n"
        f"<i>версия {v}</i>\n\n"
        "Принимая условия, ты заключаешь пользовательское соглашение с "
        "исполнителем услуг через бота. Без принятия сервисы недоступны.\n\n"
        "━━━━━━━━━━━━\n"
        "📌 <b>1. Предмет</b>\n"
        "• Бот @DirectorVaggobot — витрина: заказы (сайт, бот, скрипт, дизайн и т.п.), "
        "баланс, розыгрыши канала, служебные функции.\n"
        "• Результат заказа — <b>отдельный продукт</b> (файлы / отдельный бот / сайт). "
        "Это <b>не</b> доступ к нашим серверам, не «наш» прод-сервис и не "
        "обязанность сопровождать 24/7.\n"
        "• Канал и бренд исполнителя не являются частью сданного продукта, "
        "если иное не оговорено в ТЗ.\n\n"
        "📌 <b>2. Конфиденциальность</b>\n"
        "• Обрабатываем: Telegram id, username, имя, переписку по заказу, "
        "ТЗ, статусы, файлы сдачи, данные баланса/платежей (когда оплата вкл).\n"
        "• Цель: выполнить заказ, связь, учёт, безопасность, споры.\n"
        "• <b>Не продаём</b> и не передаём базу «кому попало». Передача — "
        "только по закону, платёжке (Platega и др.) в объёме оплаты, "
        "или подрядчику по твоему ТЗ с той же задачей.\n"
        "• Сообщения идут через Telegram — действуют их правила и хранение.\n"
        "• Платёжные данные карт/СБП обрабатывает <b>платёжный сервис</b>, "
        "не мы (когда касса подключена).\n"
        "• Ты не должен присылать пароли от чужих аккаунтов, полные данные "
        "карт, секреты третьих лиц без права.\n"
        "• Запрос удалить/ограничить данные — в бота владельцу. Удалим, "
        "что можем; чеки оплаченных работ и id сделки можем хранить "
        "для бухучёта/споров разумный срок.\n"
        "• Скриншоты, материалы ТЗ и сданный код/файлы могут храниться "
        "для гарантии и доказательств сдачи."
    )
    p2 = (
        f"📜 <b>Условия · ч.2/4 — Правила и заказы</b>\n"
        f"<i>версия {v}</i>\n\n"
        "📌 <b>3. Правила пользования</b>\n"
        "• Запрещено заказывать / описывать в ТЗ:\n"
        "  — CSAM / любой сексуальный контент с детьми и «школой» в этом смысле;\n"
        "  — порно-сайты/боты 18+ (запрет правил сервиса);\n"
        "  — взлом аккаунтов, обход 2FA, кража доступа;\n"
        "  — стиллеры, трояны, вирусы, ransomware, кейлоггеры;\n"
        "  — фишинг, скам, поддельные банки/документы, CVV/дампы;\n"
        "  — пробив/деанон людей, слив баз без права;\n"
        "  — наркотики, закладки; оружие/взрывчатка; заказное насилие;\n"
        "  — DDoS, ботнеты; отмыв/обнал/дропы;\n"
        "  — террор/экстремизм; иное незаконное.\n"
        "• <b>Автопроверка ТЗ:</b> бот сканирует каждый шаг опроса. "
        "При срабатывании — <b>мгновенный блок</b> аккаунта. "
        "Снять блок может только владелец (если ложное срабатывание).\n"
        "• Фейковая оплата, спам, угрозы, оскорбления — тоже блок/отказ.\n"
        "• Мультиаккаунты для обхода банов/оплаты — отказ и блок.\n"
        "• <b>Бесплатных заказов нет.</b> Цены фиксированы по типу — /prices.\n"
        "• <b>Кто платит:</b> заказчик (ты) платит исполнителю за разработку "
        "по прайсу. Комиссии платёжки (когда касса вкл.) — по тарифу сервиса "
        "оплаты (Platega и т.п.), отображаются при оплате.\n"
        "• <b>Не входят в цену:</b> сервер, VPS, хостинг, домен, SSL, "
        "App Store / Google Play, реклама, чужие API-ключи, 24/7 поддержка.\n"
        "• Хостинг/доступы — <b>только если ты даёшь</b> сам.\n"
        "• Срок в ТЗ — ориентир, не штрафной SLA, если не согласовано иначе.\n"
        "• Отказ/отмена: незаконно, невозможно, пустое ТЗ, токсичность, "
        "неоплата.\n\n"
        "📌 <b>4. Заказы (порядок)</b>\n"
        "1) Тип + цена из прайса → опрос ТЗ в боте.\n"
        "2) Подтверждение → оплата с баланса (когда /topup вкл) → работа.\n"
        "3) Сдача: файлы / отдельный бот + статус «готово».\n"
        "4) Момент сдачи = готовность / файл / done — что раньше.\n"
        "• ТЗ — то, что в заказе <b>до</b> сдачи. «А ещё…» после = новый заказ.\n"
        "• Приёмка: нет замечаний в срок правок → работа принята.\n"
        "• Споры — в боте /support (тикет) или владельцу, с id заказа."
    )
    p3 = (
        f"📜 <b>Условия · ч.3/4 — Гарантии</b>\n"
        f"<i>версия {v}</i>\n\n"
        "📌 <b>5. Гарантии (что обещаем)</b>\n"
        f"• <b>Гарантия на каждый проект: {g} суток (48 часов)</b> с момента сдачи.\n"
        "  В этот срок чиним <b>ошибки и баги</b>, из‑за которых результат "
        "не соответствует согласованному ТЗ (не запускается, ломает оговорённый "
        "сценарий, явный дефект нашей вины).\n"
        f"• <b>Правки / небольшие изменения: в течение {r} суток (24 часа)</b> "
        "после сдачи — бесплатно, <b>в рамках исходного ТЗ</b> "
        "(формулировки, мелочи UI/текста, точечные правки, не новая фича).\n"
        "• Что <b>не</b> входит в гарантию и правки:\n"
        "  — новый функционал, «сделай как у конкурента», смена концепции;\n"
        "  — поломки из‑за твоего хостинга, чужих правок кода, смены API Telegram/"
        "банка, блокировок, вирусов, форс-мажора;\n"
        "  — контент/данные, которые дал ты (ошибки в текстах, картинках, доступах);\n"
        "  — поддержка «навсегда», обучение, доработки через месяцы.\n"
        "• После истечения сроков гарантии/правок — только новый заказ или "
        "отдельная договорённость.\n"
        "• Работаем после подтверждённой оплаты (когда касса/баланс вкл).\n"
        "• Возврат: если <b>мы</b> отменили до начала/без сдачи по нашей "
        "вине — возврат. Если сдали по ТЗ и сроки прошли — «вернуть всё» "
        "не принимается, кроме грубого несоответствия ТЗ в гарантийный срок.\n"
        "• Не гарантируем: прибыль, охваты, SEO, апрув сторов, юридическую "
        "чистоту твоего бизнеса, работу вечно при смене API."
    )
    p4 = (
        f"📜 <b>Условия · ч.4/4 — Риски и ответственность</b>\n"
        f"<i>версия {v}</i>\n\n"
        "📌 <b>6. Риски (осознаёшь и принимаешь)</b>\n"
        "• Бот/ПК/интернет могут лежать — это не «штраф за каждый час».\n"
        "• ИИ и автоответы могут ошибаться; итог за исполнителем, но "
        "ты проверяешь сдачу в гарантийный срок.\n"
        "• Платформы (Telegram, банки, Platega и т.д.) — их сбои не наша вина.\n"
        "• Ты рискуешь, если не тестируешь вовремя, не даёшь доступы, "
        "меняешь ТЗ на ходу.\n"
        "• Хранение секретов в чате — твой риск; лучше временные токены.\n\n"
        "📌 <b>7. Ограничение ответственности</b>\n"
        "• Максимум ответственности по заказу — сумма, фактически оплаченная "
        "за этот заказ (см. прайс /prices).\n"
        "• Не возмещаем упущенную выгоду, косвенные убытки, репутацию, "
        "штрафы третьих лиц, простой бизнеса.\n"
        "• Принимая условия, ты <b>отказываешься от претензий</b> сверх "
        "описанных гарантий и сроков, кроме требований, которые нельзя "
        "ограничить законом.\n"
        "• Претензии только письменно в боте в сроки гарантии/правок, "
        "с id заказа и описанием бага. После сроков — отказ в бесплатной "
        "доработке законен.\n\n"
        "📌 <b>8. Возраст и закон</b>\n"
        "Подтверждаешь дееспособность / согласие законного представителя "
        "и законность ТЗ. Ответственность за контент и цели — на тебе.\n\n"
        "📌 <b>9. Изменение условий</b>\n"
        "Текст может обновляться (новая версия). Продолжение работы после "
        "запроса принять новую версию = согласие. Отказ = сервисы закрыты.\n\n"
        "📌 <b>10. Контакт</b>\n"
        "Споры и вопросы — в @DirectorVaggobot владельцу, с номером заказа.\n\n"
        f"✅ Гарантия: <b>{g} сут.</b> · Правки: <b>{r} сут.</b> после сдачи.\n"
        "Нажимая «Принимаю», подтверждаешь, что прочитал все части и согласен."
    )
    return [p1, p2, p3, p4]


def terms_full_html() -> str:
    """Склейка (может быть >4096 — для отправки используй terms_parts)."""
    return "\n\n".join(terms_parts())


def terms_short_html() -> str:
    return (
        "📜 <b>Условия</b>\n\n"
        "Без «Принимаю» заказы и баланс недоступны.\n\n"
        f"<b>Коротко</b>\n"
        f"• Гарантия <b>{GUARANTEE_DAYS} сут.</b> · правки <b>{REWORK_DAYS} сут.</b>\n"
        "• Цены <b>фиксированные</b> — кнопка «Тарифы» / /prices\n"
        "• Платит <b>заказчик</b> · free-слотов <b>нет</b>\n"
        "• Хостинг / VPS / домен — <b>не в цене</b>\n"
        "• Заказ = <b>отдельный</b> продукт (не «наш» сервер)\n"
        "• Запрещённое ТЗ → <b>автоблок</b>\n\n"
        "Ниже: политика, соглашение, прайс. Затем — «Принимаю».\n"
        f"<i>{TERMS_VERSION}</i>"
    )


def legal_docs_row() -> list:
    """Постоянный доступ к документам (Platega/банк)."""
    return [
        [
            {"text": "🔒 Политика", "url": PRIVACY_URL},
            {"text": "📜 Соглашение", "url": AGREEMENT_URL},
        ],
    ]


def gate_keyboard() -> dict:
    """Вход как у про-ботов: один главный CTA, доки вторично."""
    return {
        "inline_keyboard": [
            [{"text": "✅ Принимаю и продолжаю", "callback_data": "terms:yes"}],
            [
                {"text": "💰 Прайс", "callback_data": "legal:prices"},
                {"text": "📖 Условия", "callback_data": "terms:full"},
            ],
            [
                {"text": "🔒 Политика", "url": PRIVACY_URL},
                {"text": "📜 Соглашение", "url": AGREEMENT_URL},
            ],
            [{"text": "❌ Не принимаю", "callback_data": "terms:no"}],
        ]
    }


def full_keyboard(*, accepted: bool = False) -> dict:
    if accepted:
        rows = [
            [{"text": "✅ Уже принято", "callback_data": "terms:ok"}],
            [{"text": "🏠 В меню", "callback_data": "menu:userhome"}],
        ]
    else:
        rows = [
            [
                {"text": "✅ Принимаю", "callback_data": "terms:yes"},
                {"text": "❌ Не принимаю", "callback_data": "terms:no"},
            ],
            [{"text": "« Назад", "callback_data": "terms:short"}],
        ]
    rows.extend(legal_docs_row())
    return {"inline_keyboard": rows}


def after_accept_keyboard(user_id: int | None = None) -> dict:
    """Меню клиента — крупный CTA, всё под рукой."""
    rows = [[{"text": "🛠  Заказать", "callback_data": "ord:restart"}]]
    if user_id:
        try:
            import orders_lib as _o

            pend = _o.user_pending_order(int(user_id))
            if pend:
                oid = str(pend.get("id") or "")
                rows.append(
                    [{"text": "📂  Мой заказ", "callback_data": f"ord:status:{oid}"}]
                )
        except Exception:
            pass
    rows.extend(
        [
            [
                {"text": "📦  Заказы", "callback_data": "ord:mine"},
                {"text": "💳  Баланс", "callback_data": "bal:show"},
            ],
            [
                {"text": "💰  Прайс", "callback_data": "legal:prices"},
                {"text": "🆘  Поддержка", "callback_data": "sup:home"},
            ],
            [
                {"text": "📢  Канал", "url": "https://t.me/Vaggo01"},
                {"text": "🔗  Реферал", "callback_data": "ref:me"},
            ],
            [{"text": "🔄  Обновить меню", "callback_data": "menu:userhome"}],
        ]
    )
    return {"inline_keyboard": rows}


def legal_menu_keyboard() -> dict:
    """Документы — без смешивания с заказом."""
    rows = list(legal_docs_row())
    rows.append([{"text": "💰 Прайс (кто и за что)", "callback_data": "legal:prices"}])
    rows.append(
        [
            {"text": "📜 Условия", "callback_data": "terms:short"},
            {"text": "🆘 Поддержка", "callback_data": "sup:home"},
        ]
    )
    rows.append([{"text": "🏠 Меню", "callback_data": "menu:userhome"}])
    return {"inline_keyboard": rows}


def support_from_cfg(cfg: dict | None = None) -> dict:
    cfg = cfg or {}
    s = cfg.get("support") if isinstance(cfg.get("support"), dict) else {}
    out = dict(DEFAULT_SUPPORT)
    for k in ("telegram", "bot", "email", "channel"):
        if s.get(k):
            out[k] = str(s[k]).lstrip("@")
    # owner username fallback
    owners = cfg.get("owner_usernames") or []
    if owners and not s.get("telegram"):
        out["telegram"] = str(owners[0]).lstrip("@")
    return out


def support_html(cfg: dict | None = None) -> str:
    """Коротко + тикеты. Детали контактов — вторично."""
    s = support_from_cfg(cfg)
    lines = [
        "🆘 <b>Поддержка</b>\n",
        "Пиши <b>тикетом в боте</b> — так удобнее и для банка (не группа).\n",
        "• «Новый тикет» → опиши вопрос → получишь номер",
        "• Ответ придёт сюда в личку",
        "• Пока тикет открыт — просто пиши дальше\n",
    ]
    if s.get("telegram"):
        lines.append(f"Резерв: @{s['telegram']}")
    if s.get("email"):
        lines.append(f"Email: <code>{s['email']}</code>")
    lines.append("\n/support · /tickets · /legal")
    return "\n".join(lines)


def prices_html(cfg: dict | None = None) -> str:
    """Услуга — цена. Без воды (для банка / Platega / клиента)."""
    try:
        import orders_lib as orders

        catalog = "\n".join(orders.price_catalog_lines())
    except Exception:
        catalog = (
            "• Дизайн / обложки — <b>100 ₽</b>\n"
            "• Скрипт / автоматизация — <b>150 ₽</b>\n"
            "• Telegram-бот — <b>200 ₽</b>\n"
            "• Сайт / лендинг — <b>200 ₽</b>\n"
            "• Приложение (MVP) — <b>300 ₽</b>\n"
            "• Другое — <b>200 ₽</b>"
        )

    pay = (cfg or {}).get("payments") if isinstance((cfg or {}).get("payments"), dict) else {}
    topup_on = bool(pay.get("topup_enabled"))
    provider = (pay.get("provider") or "platega").strip()

    lines = [
        "💰 <b>Прайс-лист</b>\n",
        "<b>Услуга — цена</b> (фиксировано, без доплат «по настроению»):\n",
        catalog,
        "",
        "<b>Кто платит</b>",
        "• Заказчик → исполнителю (разработка по прайсу)",
        "• Канал @Vaggo01 и вход в бота — <b>бесплатно</b>",
        "",
        "<b>Не входит в цену</b>",
        "• хостинг, VPS, домен, реклама, сторы, 24/7 поддержка",
        "",
        "<b>Оплата</b>",
        "• с баланса бота при подтверждении заказа",
    ]
    if topup_on:
        lines.append(f"• пополнение: /topup ({provider})")
    else:
        lines.append(f"• пополнение: скоро ({provider}) · /support")
    lines.extend(
        [
            "",
            f"<b>Гарантия</b> {GUARANTEE_DAYS} сут. · правки {REWORK_DAYS} сут. (/terms)",
            "",
            "/order — заказать · /legal — документы",
            f"<i>{TERMS_VERSION}</i>",
        ]
    )
    return "\n".join(lines)


def legal_hub_html(cfg: dict | None = None) -> str:
    return (
        "📋 <b>Документы</b>\n\n"
        "Раздельно, всегда доступно:\n\n"
        "🔒 <b>Политика</b> — персональные данные\n"
        "📜 <b>Соглашение</b> — правила сервиса\n"
        "💰 <b>Прайс</b> — кто платит и сколько\n"
        "🆘 <b>Поддержка</b> — тикеты в боте\n\n"
        f"<i>{TERMS_VERSION}</i>"
    )


def user_home_html(user_id: int | None = None) -> str:
    lines = [
        "✦  <b>Director Vaggo</b>",
        "бот · сайт · скрипт · дизайн",
        "─" * 16,
    ]
    if user_id:
        try:
            import orders_lib as _o

            pend = _o.user_pending_order(int(user_id))
            if pend:
                st = _o.status_label(str(pend.get("status") or ""))
                lines.append(
                    f"\n🛠  Заказ  <code>{pend.get('id')}</code>\n     {st}"
                )
        except Exception:
            pass
        try:
            import balance_lib as _b

            bal = _b.get_balance(int(user_id))
            if bal:
                lines.append(f"💳  Баланс:  <b>{bal}</b> ₽")
        except Exception:
            pass
    try:
        import giveaway_lib as _gw

        act = _gw.get_active()
        if act and act.get("status") == "active":
            n = _gw.entry_count(act, complete_only=True)
            need = _gw.min_complete_needed(act) or 10
            mid = act.get("channel_message_id")
            link = f"https://t.me/Vaggo01/{mid}" if mid else "https://t.me/Vaggo01"
            lines.append(
                f"🎁  Розыгрыш  <b>{n}/{need}</b>  ·  "
                f"<a href=\"{link}\">пост</a>"
            )
    except Exception:
        pass
    lines.append("\nВыбери действие  👇")
    lines.append("<i>Меню пропало — /menu</i>")
    return "\n".join(lines)
