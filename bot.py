"""
Director Vaggo — Channel Manager (Bothost cloud + optional home Grok bridge).

Владелец: пульт, очередь, черновики, комменты, розыгрыш, заказы
Клиент: terms → заказ → support
Grok: XAI_API_KEY на Bothost  ИЛИ  bridge (ПК + tunnel)  ИЛИ  local session
Deploy: GitHub main → push_bothost.ps1 / /redeploy

Не ломаем: media/giveaways.json + giveaway_restore.json
"""
from __future__ import annotations

import html
import json
import re
import sys
import threading
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

from content import (
    generate_comment_reply,
    generate_guide,
    generate_ideas,
    generate_post,
    generate_seed_comment,
    pick_reaction_for_text,
    rewrite_post,
    series_topics,
    status_text,
    week_plan,
)
from queue_lib import (
    cancel_item,
    due_items,
    format_queue_report,
    get_item,
    mark_item,
    publish_now as queue_publish_now,
    summary as queue_summary,
)
import giveaway_lib as gw
import orders_lib as orders
import balance_lib as bal
import terms_lib as terms
import moderation_lib as mod
import support_lib as support
from state import (
    add_draft,
    add_pending_comment,
    get_draft,
    load_config,
    load_state,
    mark_published,
    save_config,
    save_state,
)
import tg

# chat_mod опционален — пакет Bothost не должен падать, если файл забыли
try:
    import chat_mod_lib as chatmod
except ImportError:  # pragma: no cover
    def _chatmod_noop(*_a, **_k):
        return None

    chatmod = SimpleNamespace(
        is_chat_banned=lambda *_a, **_k: False,
        detect_toxicity=lambda *_a, **_k: None,
        process_offense=lambda *_a, **_k: {"ok": False},
        owner_pardon=lambda *_a, **_k: None,
        status_line=lambda *_a, **_k: "mod off",
        REASON_RU={},
    )
    print("WARN: chat_mod_lib missing — chat moderation disabled", flush=True)

_last_queue_tick = 0.0
# 4.6.8 — orders without Grok ok; hide similar to client; pay gate; balance seed
BOT_CODE_VERSION = "4.8.0"


def is_owner(cfg: dict, user: dict | None) -> bool:
    """Владелец строго по Telegram id (не по «похожему» username / каналу)."""
    if not user:
        return False
    try:
        uid = int(user.get("id") or 0)
    except Exception:
        return False
    if not uid:
        return False
    ids = {5740061551}  # канон
    for x in cfg.get("owner_user_ids") or []:
        try:
            ids.add(int(x))
        except Exception:
            pass
    return uid in ids


def is_giveaway_excluded(cfg: dict, user: dict | None) -> bool:
    """Владелец / тестовые акки — не в барабане."""
    if not user:
        return False
    if is_owner(cfg, user):
        return True
    uid = user.get("id")
    uname = (user.get("username") or "").lower().lstrip("@")
    ids = set(cfg.get("giveaway_exclude_user_ids") or [])
    names = {
        n.lower().lstrip("@") for n in (cfg.get("giveaway_exclude_usernames") or [])
    }
    if uid in ids:
        return True
    if uname and uname in names:
        return True
    return False


def owner_chat_id(cfg: dict) -> int | None:
    ids = cfg.get("owner_user_ids") or []
    return int(ids[0]) if ids else None


def draft_keyboard(draft_id: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ В канал", "callback_data": f"pub:{draft_id}"},
                {"text": "✏️ Переписать", "callback_data": f"rew:{draft_id}"},
            ],
            [
                {"text": "🗑 Удалить", "callback_data": f"drop:{draft_id}"},
                {"text": "🏠 Меню", "callback_data": "menu:home"},
            ],
        ]
    }


def comment_keyboard(cid: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Ответить", "callback_data": f"creply:{cid}"},
                {"text": "✏️ Ещё вариант", "callback_data": f"crewrite:{cid}"},
            ],
            [
                {"text": "⏭ Пропуск", "callback_data": f"cskip:{cid}"},
                {"text": "🏠 Меню", "callback_data": "menu:home"},
            ],
        ]
    }


def main_menu_keyboard() -> dict:
    """Главный пульт владельца — чисто, крупно, без каши."""
    return {
        "inline_keyboard": [
            [{"text": "📊  Сегодня", "callback_data": "menu:stats"}],
            [
                {"text": "📅  Очередь", "callback_data": "menu:queue"},
                {"text": "🚀  Выложить", "callback_data": "menu:qnow"},
            ],
            [
                {"text": "🎁  Розыгрыш", "callback_data": "menu:giveaway"},
                {"text": "🛠  Заказы", "callback_data": "menu:orders"},
            ],
            [
                {"text": "💰  Прайс / скидки", "callback_data": "menu:prices"},
                {"text": "⭐  Баллы", "callback_data": "menu:points"},
            ],
            [
                {"text": "💬  Комменты", "callback_data": "menu:comments"},
                {"text": "📝  Черновики", "callback_data": "menu:drafts"},
            ],
            [
                {"text": "⏸  Пауза", "callback_data": "menu:toggle_pause"},
                {"text": "⚙️  Ещё", "callback_data": "menu:more"},
            ],
            [{"text": "🔄  Обновить пульт", "callback_data": "menu:fresh"}],
        ]
    }


def owner_more_keyboard() -> dict:
    """Сервис — вторичный экран."""
    return {
        "inline_keyboard": [
            [{"text": "🧠  Grok / мозг", "callback_data": "menu:brains"}],
            [{"text": "💰  Балансы", "callback_data": "menu:balance"}],
            [{"text": "🏷  Прайс / скидки", "callback_data": "menu:prices"}],
            [
                {"text": "📡  Радар", "callback_data": "menu:radar"},
                {"text": "🔥  Горящие", "callback_data": "menu:hot"},
            ],
            [
                {"text": "♻️  Restore GW", "callback_data": "menu:gwrestore"},
                {"text": "📌  Кнопки GW", "callback_data": "menu:gfixkb"},
            ],
            [{"text": "🧹  Почистить личку", "callback_data": "menu:clean"}],
            [{"text": "«  В пульт", "callback_data": "menu:home"}],
        ]
    }


def menu_result_keyboard(_group: str | None = None) -> dict:
    """Низ любого экрана результата — всегда путь домой."""
    return {
        "inline_keyboard": [
            [
                {"text": "🏠  Пульт", "callback_data": "menu:home"},
                {"text": "🔄  Обновить", "callback_data": "menu:fresh"},
            ],
            [
                {"text": "🎁  Розыгрыш", "callback_data": "menu:giveaway"},
                {"text": "🛠  Заказы", "callback_data": "menu:orders"},
            ],
        ]
    }


def toast_keyboard() -> dict:
    """Кнопки под отдельным уведомлением (не перетирает пульт)."""
    return {
        "inline_keyboard": [
            [{"text": "🏠  Открыть пульт", "callback_data": "menu:fresh"}],
            [
                {"text": "🛠  Заказы", "callback_data": "menu:orders"},
                {"text": "🎁  Розыгрыш", "callback_data": "menu:giveaway"},
            ],
        ]
    }


def _host_brain_line(cfg: dict | None = None) -> str:
    """Короткая строка: host + brain source (для пульта / ping)."""
    try:
        from content import brain_status

        cfg = cfg or load_config()
        mode = str(cfg.get("bot_host_mode") or "local").lower()
        host = "☁️ cloud" if mode in ("cloud", "bothost", "hosting") else "💻 local"
        bst = brain_status(cfg, use_cache=True, probe_ollama=False)
        active = str(bst.get("active") or "—")
        src = str(bst.get("grok_source") or "—")
        if active == "grok":
            brain = f"🧠 grok · {html.escape(src)}"
        elif active == "ollama":
            brain = "🧠 ollama"
        else:
            brain = "🧠 off"
            hint = bst.get("hint")
            if hint:
                brain += f" · {html.escape(str(hint)[:48])}"
        return f"{host}  ·  {brain}"
    except Exception:
        return "host/brain · ?"


def owner_home_html() -> str:
    paused = False
    cfg_home: dict | None = None
    try:
        from state import load_config as _lc

        cfg_home = _lc() or {}
        paused = bool(cfg_home.get("paused"))
    except Exception:
        pass
    ch_status = "⏸  на паузе" if paused else "🟢  в эфире"
    hb = _host_brain_line(cfg_home)

    # розыгрыш
    gw_block = "🎁  <b>Розыгрыш</b>  ·  нет активного"
    try:
        act = gw.get_active()
        if act:
            n = gw.entry_count(act, complete_only=True)
            need = gw.min_complete_needed(act) or 10
            mid = act.get("channel_message_id") or "—"
            prize = html.escape(str(act.get("prize") or "")[:40])
            filled = min(10, max(0, int(round(10 * n / max(need, 1)))))
            bar = "▓" * filled + "░" * (10 - filled)
            gw_block = (
                f"🎁  <b>Розыгрыш</b>  ·  <code>{n}/{need}</code>\n"
                f"     <code>{bar}</code>\n"
                f"     {prize}\n"
                f"     пост  ·  {mid}"
            )
    except Exception:
        pass

    # очередь
    q_block = "📅  <b>Очередь</b>  ·  пусто"
    try:
        from queue_lib import summary as queue_summary

        qs = queue_summary()
        nxt = qs.get("next") or {}
        nq = int(qs.get("queued") or 0)
        if nq:
            when = html.escape(str(nxt.get("publish_at") or "—")[:16])
            title = html.escape(str(nxt.get("title") or nxt.get("id") or "—")[:32])
            q_block = (
                f"📅  <b>Очередь</b>  ·  <code>{nq}</code>\n"
                f"     next  ·  {when}\n"
                f"     {title}"
            )
    except Exception:
        pass

    # комменты + заказы
    pend = 0
    try:
        from state import load_state as _ls

        pend = len((_ls() or {}).get("pending_comments") or [])
    except Exception:
        pass
    c_block = (
        f"💬  <b>Комменты</b>  ·  ждут  <code>{pend}</code>"
        if pend
        else "💬  <b>Комменты</b>  ·  чисто"
    )

    ord_n = 0
    try:
        open_o = [
            x
            for x in orders.list_orders(limit=50)
            if str(x.get("status") or "") in ("new", "accepted", "in_progress")
        ]
        ord_n = len(open_o)
    except Exception:
        pass
    o_block = f"🛠  <b>Заказы</b>  ·  в работе  <code>{ord_n}</code>"

    # прайс / скидка
    p_block = "💰  <b>Прайс</b>  ·  /prices"
    try:
        pr = orders.load_pricing()
        g = int(pr.get("discount_pct") or 0)
        bot_p = orders.effective_price("bot")
        site_p = orders.effective_price("site")
        if g > 0:
            p_block = (
                f"💰  <b>Прайс</b>  ·  скидка <code>−{g}%</code>\n"
                f"     бот <code>{bot_p}</code> ₽ · сайт <code>{site_p}</code> ₽"
            )
        else:
            p_block = (
                f"💰  <b>Прайс</b>  ·  бот <code>{bot_p}</code> ₽ · "
                f"сайт <code>{site_p}</code> ₽"
            )
    except Exception:
        pass

    return (
        f"✦  <b>Вагго</b>  ·  пульт\n"
        f"{ch_status}  ·  <code>{BOT_CODE_VERSION}</code>\n"
        f"{hb}\n"
        f"{'─' * 18}\n\n"
        f"{gw_block}\n\n"
        f"{q_block}\n\n"
        f"{c_block}\n"
        f"{o_block}\n"
        f"{p_block}\n\n"
        f"{'─' * 18}\n"
        f"<i>Раздел — кнопки ниже</i>\n"
        f"<i>Пропал пульт —</i>  /menu  <i>или</i>  «Обновить»"
    )


def _owner_panel(
    cfg: dict,
    state: dict,
    chat_id: int | str,
    mid: int | None,
    uid: int | None,
    text: str,
    markup: dict,
    *,
    force_new: bool = False,
) -> int | None:
    """
    Главный пульт: edit живого окна; force_new — свежее сообщение
    (старое тихо убираем, если можем). Не теряемся.
    """
    return ui_edit_or_send(
        cfg,
        chat_id,
        text[:4000],
        reply_markup=markup,
        message_id=mid,
        state=state,
        uid=uid,
        store_key="owner_ui_msg",
        force_new=force_new,
    )


def publish_queue_item(cfg: dict, item: dict) -> int | None:
    """Выложить один пункт очереди (фото+подпись)."""
    from publish_queue_today import publish_item

    mid = publish_item(cfg, item)
    mark_item(
        str(item.get("id")),
        status="published",
        message_id=mid,
    )
    try:
        st = load_state()
        d = add_draft(
            st,
            item.get("text") or "",
            rubric=item.get("rubric") or "",
            source="schedule",
        )
        d["media_path"] = item.get("photo")
        d["media_type"] = "photo"
        st = load_state()
        for x in st.get("drafts") or []:
            if x.get("id") == d.get("id"):
                x["media_path"] = item.get("photo")
                x["media_type"] = "photo"
        save_state(st)
        mark_published(load_state(), d, mid)
    except Exception as e:
        print("queue draft track fail", e)
    return mid


def tick_schedule_queue(cfg: dict) -> list[str]:
    """Авто-выкладка due-постов. Возвращает id опубликованных."""
    global _last_queue_tick
    now = time.time()
    if now - _last_queue_tick < 20:
        return []
    _last_queue_tick = now
    if cfg.get("paused"):
        return []
    done: list[str] = []
    for item in due_items(now):
        iid = str(item.get("id") or "?")
        # короткая блокировка, чтобы не задвоить с внешним publisher
        fresh = get_item(iid)
        if not fresh or fresh.get("status") != "queued":
            continue
        mark_item(iid, status="publishing")
        try:
            mid = publish_queue_item(cfg, fresh)
            done.append(iid)
            print("queue auto-published", iid, mid, flush=True)
            notify_owner(
                cfg,
                f"✅ По расписанию <code>{html.escape(iid)}</code>\n"
                f"https://t.me/Vaggo01/{mid}",
            )
        except Exception as e:
            print("queue fail", iid, e, flush=True)
            mark_item(iid, status="error", error=str(e)[:200])
            notify_owner(
                cfg,
                f"❌ Очередь <code>{html.escape(iid)}</code>: {html.escape(str(e)[:200])}",
            )
    return done


def publish_to_channel(cfg: dict, text: str, *, draft: dict | None = None) -> dict:
    if cfg.get("paused"):
        raise RuntimeError("Пауза включена — /resume")
    channel = cfg.get("channel_id") or "@Vaggo01"
    media_path = (draft or {}).get("media_path") or ""
    media_type = (draft or {}).get("media_type") or ""
    body = (text or "").strip()
    if media_path and Path(media_path).is_file():
        cap = body[:1024]
        rest = body[1024:].strip() if len(body) > 1024 else ""
        if media_type == "video":
            result = tg.send_video(cfg, channel, media_path, caption=cap)
        else:
            result = tg.send_photo(cfg, channel, media_path, caption=cap)
        # длинный текст — вторым сообщением (продолжение)
        if rest:
            try:
                tg.send_message(cfg, channel, rest, parse_mode="HTML", disable_preview=False)
            except Exception as e:
                print("publish continuation failed", e, flush=True)
        return result
    return tg.send_message(cfg, channel, body, parse_mode="HTML", disable_preview=False)


def _owner_photo_file_id(msg: dict) -> tuple[str | None, str]:
    """Вернуть (file_id, kind) для фото/картинки-документа из сообщения владельца."""
    photos = msg.get("photo") or []
    if photos:
        # largest
        best = max(photos, key=lambda p: int(p.get("file_size") or 0) or int(p.get("width") or 0))
        return best.get("file_id"), "photo"
    doc = msg.get("document") or {}
    mime = (doc.get("mime_type") or "").lower()
    name = (doc.get("file_name") or "").lower()
    if doc.get("file_id") and (
        mime.startswith("image/")
        or name.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))
    ):
        return doc.get("file_id"), "photo"
    # video as document or video message
    if msg.get("video") and msg["video"].get("file_id"):
        return msg["video"]["file_id"], "video"
    if doc.get("file_id") and (mime.startswith("video/") or name.endswith((".mp4", ".mov", ".webm"))):
        return doc.get("file_id"), "video"
    return None, ""


def handle_owner_media(cfg: dict, state: dict, msg: dict) -> bool:
    """Владелец прислал фото/видео — сохранить и предложить выложить."""
    chat_id = msg["chat"]["id"]
    file_id, kind = _owner_photo_file_id(msg)
    if not file_id:
        return False

    caption = (msg.get("caption") or "").strip()
    try:
        if kind == "video":
            path = tg.download_file(cfg, file_id, suffix=".mp4")
        else:
            # photo from Telegram is usually jpeg
            path = tg.download_file(cfg, file_id, suffix=".jpg")
    except Exception as e:
        tg.send_message(cfg, chat_id, f"❌ Не скачал файл: {html.escape(str(e))}")
        return True

    text = caption or "✨"
    item = add_draft(state, text, source="owner_upload")
    item["media_path"] = path
    item["media_type"] = "video" if kind == "video" else "photo"
    # update in state list
    for d in state.get("drafts") or []:
        if d.get("id") == item["id"]:
            d["media_path"] = path
            d["media_type"] = item["media_type"]
            break
    if not caption:
        state["await_upload_text"] = item["id"]
    else:
        state.pop("await_upload_text", None)
    save_state(state)

    hint = (
        f"🖼 <b>Медиа сохранено</b>\n"
        f"Черновик <code>{item['id']}</code>\n"
        f"Файл: <code>{html.escape(Path(path).name)}</code>\n\n"
    )
    if caption:
        hint += "Подпись уже есть. Можно сразу в канал."
    else:
        hint += (
            "Подписи нет — пришли <b>следующим сообщением текст поста</b>\n"
            "(можно длинный; >1024 уйдёт продолжением),\n"
            "или жми «В канал» с короткой подписью ✨"
        )
    tg.send_message(cfg, chat_id, hint, reply_markup=draft_keyboard(item["id"]))
    return True


def notify_owner(cfg: dict, text: str, reply_markup: dict | None = None) -> None:
    """
    Важное — ОТДЕЛЬНЫМ сообщением (не затирает пульт).
    Внизу всегда кнопка «Открыть пульт».
    """
    oid = owner_chat_id(cfg)
    if not oid:
        return
    try:
        raw = (text or "").strip()
        if raw.startswith("🔔"):
            raw = raw[1:].strip()
        body = f"🔔  <b>Событие</b>\n{'─' * 14}\n\n{raw[:3000]}"
        kb = reply_markup or toast_keyboard()
        # гарантируем путь домой, если разметка своя без меню
        try:
            rows = list((kb.get("inline_keyboard") or []))
            flat = " ".join(
                (b.get("callback_data") or "") for row in rows for b in row
            )
            if "menu:home" not in flat and "menu:fresh" not in flat:
                rows.append([{"text": "🏠  Пульт", "callback_data": "menu:fresh"}])
                kb = {"inline_keyboard": rows}
        except Exception:
            kb = toast_keyboard()
        tg.send_message(
            cfg,
            oid,
            body[:4000],
            parse_mode="HTML",
            reply_markup=kb,
            disable_preview=True,
        )
    except Exception as e:
        print("notify_owner failed:", e)
        try:
            tg.send_message(
                cfg,
                oid,
                (text or "")[:3500],
                reply_markup=reply_markup or toast_keyboard(),
                disable_preview=True,
            )
        except Exception:
            pass


def check_channel_report(cfg: dict) -> str:
    channel = cfg.get("channel_id") or "@Vaggo01"
    lines = [f"🔎 <b>Проверка канала</b> {html.escape(str(channel))}", ""]
    try:
        me = tg.get_me(cfg)
        lines.append(f"Бот: @{me.get('username')} (id {me.get('id')})")
    except Exception as e:
        return f"❌ getMe: {html.escape(str(e))}"

    try:
        chat = tg.get_chat(cfg, channel)
        lines.append(f"Канал: <b>{html.escape(chat.get('title') or '?')}</b>")
        lines.append(f"chat_id: <code>{chat.get('id')}</code>")
        # запомним числовой id
        if chat.get("id"):
            cfg["channel_numeric_id"] = chat["id"]
            save_config(cfg)
    except Exception as e:
        lines.append(f"❌ getChat: {html.escape(str(e))}")
        lines.append("Канал не виден боту.")
        return "\n".join(lines)

    try:
        m = tg.get_chat_member(cfg, channel, me["id"])
        st = m.get("status")
        lines.append(f"Статус бота: <b>{html.escape(str(st))}</b>")
        if st in ("administrator", "creator"):
            lines.append(f"can_post: {m.get('can_post_messages')}")
            lines.append(f"can_edit: {m.get('can_edit_messages')}")
            lines.append(f"can_delete: {m.get('can_delete_messages')}")
            if m.get("can_post_messages") or st == "creator":
                lines.append("")
                lines.append("✅ Можно постить в канал.")
            else:
                lines.append("")
                lines.append("⚠️ Админ, но без «Публикация сообщений».")
        else:
            lines.append("")
            lines.append("❌ Бот не админ. Добавь @DirectorVaggobot в админы канала.")
    except Exception as e:
        lines.append(f"❌ getChatMember: {html.escape(str(e))}")
        lines.append("Обычно значит: бот ещё не админ канала.")

    disc = cfg.get("discussion_group_id") or 0
    lines.append("")
    if disc:
        lines.append(f"Группа комментов: <code>{disc}</code>")
        try:
            g = tg.get_chat(cfg, disc)
            lines.append(f"Название: {html.escape(g.get('title') or '?')}")
            lines.append("✅ Группа видна боту.")
        except Exception as e:
            lines.append(f"⚠️ Группа не доступна: {html.escape(str(e))}")
    else:
        lines.append("Группа комментов: не задана.")
        lines.append("Добавь бота в группу обсуждений и напиши там /bind")

    return "\n".join(lines)


def handle_command(cfg: dict, state: dict, msg: dict) -> None:
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    user = msg.get("from") or {}
    if not is_owner(cfg, user):
        # чужим не светим меню/команды управления
        mid = None
        act = None
        try:
            act = gw.get_active()
            mid = (act or {}).get("channel_message_id")
        except Exception:
            pass
        link = f"https://t.me/Vaggo01/{mid}" if mid else "https://t.me/Vaggo01"
        tg.send_message(
            cfg,
            chat_id,
            "Доступ к управлению только у владельца.\n\n"
            "Если розыгрыш: открой пост и нажми «Участвовать».\n"
            f"{link}",
            parse_mode=None,
        )
        return

    # remember owner id if missing
    if user.get("id") and user["id"] not in (cfg.get("owner_user_ids") or []):
        # don't auto-add strangers; only if list empty-ish
        pass

    # Фото/видео от владельца → черновик с медиа
    if msg.get("photo") or msg.get("video") or msg.get("document"):
        if handle_owner_media(cfg, state, msg):
            return

    # Текст после фото без подписи → привязать к черновику
    if (
        text
        and not text.startswith("/")
        and state.get("await_upload_text")
        and not msg.get("photo")
    ):
        did = state.get("await_upload_text")
        draft = get_draft(state, did)
        if draft:
            draft["text"] = text
            state["await_upload_text"] = None
            save_state(state)
            preview = text if len(text) < 900 else text[:900] + "…"
            tg.send_message(
                cfg,
                chat_id,
                f"📝 Текст привязан к <code>{did}</code>\n"
                f"Медиа: <code>{html.escape(Path(draft.get('media_path') or '').name)}</code>\n\n"
                f"{preview}",
                reply_markup=draft_keyboard(did),
            )
            return

    lower = text.lower()
    cmd = lower.split()[0].split("@")[0] if lower.startswith("/") else ""
    arg = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""

    if cmd in ("/start", "/help", "/menu", "/panel", "/пульт"):
        ui_delete_user_message(cfg, msg)
        uid_o = int(user.get("id") or 0) or None
        # /menu /start — всегда свежий пульт (не потеряется)
        _owner_panel(
            cfg,
            state,
            chat_id,
            None,
            uid_o,
            owner_home_html(),
            main_menu_keyboard(),
            force_new=True,
        )
        return

    # --- модерация чата ---
    if cmd in ("/modstat", "/modstatus"):
        target = arg.strip().lstrip("@")
        if not target:
            tg.send_message(
                cfg,
                chat_id,
                "🛡 <b>Модерация чата</b>\n"
                "4 предупр. → мут 1ч · 3 мута → неделя · "
                "2 недели → 2 мес · ещё неделя → бан\n\n"
                "<code>/modstat ID</code> · <code>/modpardon ID</code>",
                parse_mode="HTML",
            )
            return
        try:
            tid = int(target) if target.isdigit() else 0
        except Exception:
            tid = 0
        if not tid:
            tg.send_message(cfg, chat_id, "Нужен numeric user_id")
            return
        tg.send_message(
            cfg,
            chat_id,
            f"🛡 <code>{tid}</code>\n{html.escape(chatmod.status_line(tid))}",
            parse_mode="HTML",
        )
        return

    if cmd in ("/modpardon", "/modforgive", "/unmute"):
        target = arg.strip().lstrip("@")
        if not target.isdigit():
            tg.send_message(cfg, chat_id, "Пример: <code>/modpardon 123456</code>", parse_mode="HTML")
            return
        tid = int(target)
        u = chatmod.owner_pardon(tid)
        disc = cfg.get("discussion_group_id")
        if disc:
            try:
                # снять restrict / unban
                tg.unban_chat_member(cfg, disc, tid, only_if_banned=True)
                # вернуть права писать (пустые permissions с can_send = true)
                tg.restrict_chat_member(
                    cfg,
                    disc,
                    tid,
                    until_date=0,
                    permissions={
                        "can_send_messages": True,
                        "can_send_audios": True,
                        "can_send_documents": True,
                        "can_send_photos": True,
                        "can_send_videos": True,
                        "can_send_video_notes": True,
                        "can_send_voice_notes": True,
                        "can_send_polls": True,
                        "can_send_other_messages": True,
                        "can_add_web_page_previews": True,
                        "can_invite_users": True,
                    },
                )
            except Exception as e:
                print("modpardon tg", e, flush=True)
        tg.send_message(
            cfg,
            chat_id,
            f"✅ Снято с <code>{tid}</code>\n{html.escape(chatmod.status_line(tid)) if u else 'ок'}",
            parse_mode="HTML",
        )
        return

    # ---------- розыгрыш ----------
    if cmd in ("/giveaway", "/ghelp", "/raffle"):
        tg.send_message(
            cfg,
            chat_id,
            "🎁 <b>Розыгрыш</b> (как @GiveShareBot)\n\n"
            "1. /gnew Gemini Pro 18 месяцев\n"
            "2. /gpost — пост в канал с кнопкой «Участвовать»\n"
            "3. Люди жмут кнопку (проверка подписки + счётчик)\n"
            "4. По таймеру бот сам выбирает победителя\n"
            "   или /gdraw вручную\n\n"
            "Опции: /gnew приз | 48 — на 48 часов\n"
            "/gstatus · /gentries · /gend · /gcancel\n\n"
            + gw.format_status(gw.get_active(state)),
        )
        return

    if cmd == "/gnew":
        if not arg:
            tg.send_message(
                cfg,
                chat_id,
                "Пример:\n"
                "<code>/gnew Gemini Pro 18 месяцев</code>\n"
                "<code>/gnew Gemini Pro 18 мес | 48</code> — на 48 часов",
            )
            return
        prize, hours = arg, 72
        if "|" in arg:
            left, right = arg.rsplit("|", 1)
            prize = left.strip()
            try:
                hours = int(right.strip().split()[0])
            except ValueError:
                hours = 72
        try:
            item = gw.create(
                prize,
                hours=hours,
                mode="quest",
                auto_draw=True,
                require_sub=True,
                require_repost=True,
                require_invites=0,
            )
            tg.send_message(
                cfg,
                chat_id,
                f"✅ Черновик <code>{item['id']}</code>\n"
                f"Приз: {html.escape(item['prize'])}\n"
                f"Срок: {item['hours']} ч\n"
                f"Квест: подписка + репост другу (скрин) · авто-шоу\n\n"
                f"Дальше: /gpost",
            )
        except Exception as e:
            tg.send_message(cfg, chat_id, f"❌ {html.escape(str(e))}")
        return

    if cmd == "/gpost":
        item = gw.get_active(state) or (gw.list_items(state)[0] if gw.list_items(state) else None)
        if arg:
            item = gw.get_by_id(arg, state) or item
        if not item or item.get("status") in ("ended", "cancelled"):
            # last draft
            drafts = [x for x in gw.list_items(state) if x.get("status") == "draft"]
            item = drafts[0] if drafts else item
        if not item:
            tg.send_message(cfg, chat_id, "Сначала /gnew приз")
            return
        if cfg.get("paused"):
            tg.send_message(cfg, chat_id, "⏸ Пауза. /resume")
            return
        try:
            item = gw.activate(item)  # ends_at до текста
            body = gw.announce_text(item)
            channel = cfg.get("channel_id") or "@Vaggo01"
            res = tg.send_message(
                cfg,
                channel,
                body,
                parse_mode="HTML",
                disable_preview=True,
                reply_markup=gw.join_keyboard(item, bot_username=_bot_username(cfg)),
            )
            mid = int(res.get("message_id"))
            item = gw.bind_channel_post(item, mid)
            item = gw.activate(item, channel_message_id=mid)
            try:
                tg.set_message_reaction(cfg, channel, mid, "🔥")
            except Exception:
                pass
            tg.send_message(
                cfg,
                chat_id,
                f"🚀 Розыгрыш-квест в канале\n"
                f"https://t.me/Vaggo01/{mid}\n"
                f"id: <code>{item['id']}</code>\n"
                f"Шаги: подписки · репост другу · друзья×{item.get('require_invites', 1)}\n"
                f"Авто-шоу победителя: {'да' if item.get('auto_draw', True) else 'нет'}\n"
                f"/gstatus · /gentries · /gdraw",
            )
        except Exception as e:
            tg.send_message(cfg, chat_id, f"❌ {html.escape(str(e))}")
        return

    if cmd in ("/gfixkb", "/gfix_kb", "/gkeyboard"):
        # вернуть кнопку «Участвовать» на пост канала (после restore)
        item = gw.get_active(state)
        if not item:
            try:
                gw.apply_restore_seed(force=True)
                item = gw.get_active()
            except Exception:
                item = None
        if not item:
            tg.send_message(cfg, chat_id, "Нет активного розыгрыша. /gwrestore")
            return
        mid = item.get("channel_message_id")
        if arg:
            try:
                mid = int(arg.strip().split()[0])
                item = gw.bind_channel_post(item, mid)
            except ValueError:
                tg.send_message(cfg, chat_id, "id поста — число")
                return
        if not mid:
            tg.send_message(cfg, chat_id, "Нет channel_message_id. /gbind 102")
            return
        channel = cfg.get("channel_id") or "@Vaggo01"
        try:
            tg.edit_reply_markup(
                cfg,
                channel,
                int(mid),
                gw.join_keyboard(item, bot_username=_bot_username(cfg)),
            )
            tg.send_message(
                cfg,
                chat_id,
                f"✅ Кнопки на посте обновлены\n"
                f"https://t.me/Vaggo01/{mid}\n"
                f"complete: <b>{gw.entry_count(item, complete_only=True)}</b>\n"
                f"id: <code>{html.escape(str(item.get('id')))}</code>",
            )
        except Exception as e:
            tg.send_message(
                cfg,
                chat_id,
                f"❌ не смог edit кнопок: {html.escape(str(e)[:250])}\n"
                f"Пост: https://t.me/Vaggo01/{mid}\n"
                f"Можно /gpost заново (новый пост).",
            )
        return

    if cmd == "/gbind":
        if not arg:
            tg.send_message(cfg, chat_id, "Пример: /gbind 86 — id поста в канале")
            return
        try:
            mid = int(arg.strip().split()[0])
        except ValueError:
            tg.send_message(cfg, chat_id, "Нужен числовой id поста")
            return
        item = gw.get_active(state)
        if not item:
            drafts = [x for x in gw.list_items(state) if x.get("status") in ("draft", "active")]
            item = drafts[0] if drafts else None
        if not item:
            tg.send_message(cfg, chat_id, "Сначала /gnew приз")
            return
        try:
            item = gw.bind_channel_post(item, mid)
            # сразу повесить кнопки
            try:
                channel = cfg.get("channel_id") or "@Vaggo01"
                tg.edit_reply_markup(
                    cfg,
                    channel,
                    int(mid),
                    gw.join_keyboard(item, bot_username=_bot_username(cfg)),
                )
            except Exception as e:
                print("gbind markup", e, flush=True)
            tg.send_message(
                cfg,
                chat_id,
                f"✅ Привязан пост <code>{mid}</code>\n"
                f"https://t.me/Vaggo01/{mid}\n"
                f"статус: {item.get('status')}\n"
                f"discuss_root: {item.get('discuss_root_id') or 'появится после первого коммента/форварда'}\n"
                + gw.format_status(item),
            )
        except Exception as e:
            tg.send_message(cfg, chat_id, f"❌ {html.escape(str(e))}")
        return

    if cmd in ("/gstatus", "/gstat"):
        tg.send_message(cfg, chat_id, gw.format_status(gw.get_active(state) or (gw.list_items(state)[0] if gw.list_items(state) else None)))
        return

    if cmd == "/gentries":
        item = gw.get_active(state) or (gw.list_items(state)[0] if gw.list_items(state) else None)
        if not item:
            tg.send_message(cfg, chat_id, "Нет розыгрыша. /gnew")
            return
        tg.send_message(cfg, chat_id, gw.format_entries(item))
        return

    if cmd == "/gdraw":
        item = gw.get_active(state)
        if not item:
            # allow draw on expired active in list
            for it in gw.list_items(state):
                if it.get("status") == "active":
                    item = it
                    break
        if not item:
            tg.send_message(cfg, chat_id, "Нет активного розыгрыша")
            return
        if gw.entry_count(item) == 0:
            tg.send_message(cfg, chat_id, "Участников 0 — некого выбирать")
            return
        try:
            finish_giveaway_draw(cfg, item, notify_chat=chat_id)
        except Exception as e:
            tg.send_message(cfg, chat_id, f"❌ {html.escape(str(e))}")
        return

    if cmd == "/gend":
        item = gw.get_active(state)
        if not item:
            tg.send_message(cfg, chat_id, "Активного нет")
            return
        gw.end(item)
        tg.send_message(cfg, chat_id, f"⏹ Розыгрыш <code>{item['id']}</code> закрыт без розыгрыша.\nУчастников: {gw.entry_count(item)}")
        return

    if cmd == "/gcancel":
        item = gw.get_active(state) or (gw.list_items(state)[0] if gw.list_items(state) else None)
        if not item:
            tg.send_message(cfg, chat_id, "Нечего отменять")
            return
        gw.cancel(item)
        tg.send_message(cfg, chat_id, f"🗑 Розыгрыш <code>{item['id']}</code> отменён")
        return

    if cmd in ("/queue", "/today"):
        tg.send_message(cfg, chat_id, format_queue_report())
        return

    if cmd == "/stats":
        s = queue_summary()
        drafts = [d for d in (state.get("drafts") or []) if d.get("status") == "draft"]
        pending = [c for c in (state.get("pending_comments") or []) if c.get("status") == "pending"]
        pub = state.get("published") or []
        nxt = s.get("next") or {}
        nxt_line = "—"
        if nxt:
            nxt_line = (
                f"<code>{html.escape(str(nxt.get('id')))}</code> "
                f"· {html.escape(str(nxt.get('publish_at') or '?'))}\n"
                f"   {(html.escape(str(nxt.get('title') or '')))[:50]}"
            )
        pause = "⏸ пауза" if cfg.get("paused") else "▶️ online"
        subs = "—"
        try:
            subs = str(
                tg.api(cfg, "getChatMemberCount", data={"chat_id": cfg.get("channel_id") or "@Vaggo01"})
            )
        except Exception:
            pass
        gw_line = "нет активного"
        try:
            act = gw.get_active(state)
            if act:
                ok_n = gw.entry_count(act, complete_only=True)
                all_n = gw.entry_count(act, complete_only=False)
                mid = act.get("channel_message_id")
                ends = act.get("ends_at")
                ends_s = (
                    time.strftime("%d.%m %H:%M", time.localtime(int(ends))) if ends else "—"
                )
                gw_line = (
                    f"{html.escape(str(act.get('status')))} · "
                    f"✅{ok_n} / начали {all_n} · до {ends_s}"
                )
                if mid:
                    gw_line += f"\n   https://t.me/Vaggo01/{mid}"
        except Exception:
            pass
        tg.send_message(
            cfg,
            chat_id,
            "📊 <b>Сводка Вагго</b>\n\n"
            f"👥 Подписчики: <b>{html.escape(subs)}</b>\n"
            f"Бот: {pause}\n\n"
            f"<b>Очередь</b>\n"
            f"⏳ {s['queued']} · ✅ {s['published']}\n"
            f"След.: {nxt_line}\n\n"
            f"<b>Розыгрыш</b>\n{gw_line}\n\n"
            f"<b>Контент</b>\n"
            f"Черновики: {len(drafts)} · комменты ждут: {len(pending)}\n"
            f"Instant: {'да' if not cfg.get('comment_needs_owner_ok', True) else 'нет'} · "
            f"реакции: {'да' if cfg.get('auto_react_posts', True) else 'нет'}\n\n"
            f"/promo · /gentries · /queue · /menu",
            reply_markup=main_menu_keyboard(),
        )
        return

    if cmd in ("/promo", "/ad", "/реклама"):
        try:
            from promo_lib import PROMO_HTML

            tg.send_message(
                cfg, chat_id, "📢 <b>Текст для рекламы</b> — копируй:\n\n" + PROMO_HTML,
                parse_mode="HTML",
                disable_preview=True,
            )
        except Exception as e:
            tg.send_message(cfg, chat_id, f"❌ {html.escape(str(e))}")
        return

    if cmd == "/qnow":
        if cfg.get("paused"):
            tg.send_message(cfg, chat_id, "⏸ Сейчас пауза. /resume")
            return
        item = None
        if arg:
            item = get_item(arg)
            if not item:
                tg.send_message(cfg, chat_id, f"Нет id <code>{html.escape(arg)}</code>. Смотри /queue")
                return
            if item.get("status") != "queued":
                tg.send_message(
                    cfg,
                    chat_id,
                    f"Пункт <code>{html.escape(arg)}</code> статус: {html.escape(str(item.get('status')))}",
                )
                return
        else:
            due = due_items()
            if due:
                item = due[0]
            else:
                s = queue_summary()
                item = s.get("next")
        if not item:
            tg.send_message(cfg, chat_id, "Нечего выкладывать. /queue")
            return
        iid = str(item.get("id"))
        tg.send_message(cfg, chat_id, f"⏳ Выкладываю <code>{html.escape(iid)}</code>…")
        try:
            queue_publish_now(iid)
            item = get_item(iid) or item
            mid = publish_queue_item(cfg, item)
            tg.send_message(
                cfg,
                chat_id,
                f"✅ Готово <code>{html.escape(iid)}</code>\n"
                f"https://t.me/Vaggo01/{mid}",
            )
        except Exception as e:
            mark_item(iid, status="error", error=str(e)[:200])
            tg.send_message(cfg, chat_id, f"❌ {html.escape(str(e))}")
        return

    if cmd == "/qskip":
        if not arg:
            tg.send_message(cfg, chat_id, "Пример: /qskip ai2")
            return
        if cancel_item(arg):
            tg.send_message(cfg, chat_id, f"⏭ Отменил <code>{html.escape(arg)}</code>")
        else:
            tg.send_message(cfg, chat_id, f"Не нашёл queued <code>{html.escape(arg)}</code>")
        return

    if cmd == "/status":
        tg.send_message(cfg, chat_id, status_text(cfg, state))
        return

    if cmd == "/plan":
        tg.send_message(cfg, chat_id, week_plan())
        return

    if cmd == "/check":
        tg.send_message(cfg, chat_id, "⏳ Проверяю…")
        tg.send_message(cfg, chat_id, check_channel_report(cfg))
        return

    if cmd == "/brains":
        from content import brain_status, grok_ok, ollama_ok

        st = brain_status(cfg)
        src = st.get("grok_source") or "—"
        src_h = {
            "session": "SuperGrok сессия (grok login) ✅",
            "api_key": "ключ console.x.ai ✅",
            "": "нет",
            "—": "нет",
        }.get(src, src)
        sess = st.get("session") or {}
        sess_line = ""
        if sess.get("ok"):
            exp = "истекла ⚠" if sess.get("expired") else "жива"
            sess_line = f"Сессия: {html.escape(str(sess.get('email') or ''))} · {exp}\n"
        tg.send_message(
            cfg,
            chat_id,
            "🧠 <b>Мозги бота</b>\n\n"
            f"Режим: <code>{html.escape(st['mode'])}</code> "
            f"(auto = Grok → Ollama → шаблон)\n"
            f"Сейчас активен: <b>{html.escape(st['active'])}</b>\n\n"
            f"Grok: {'✅' if grok_ok(cfg) else '❌'} · {html.escape(src_h)}\n"
            f"  модель: <code>{html.escape(st['grok_model'])}</code>\n"
            f"{sess_line}"
            f"Ollama: {'✅' if ollama_ok(cfg) else '❌'} · "
            f"<code>{html.escape(st['ollama_model'])}</code>\n\n"
            "Бот ходит в Grok через твою подписку Super "
            "(файл входа Grok Build) или через xai_api_key.\n"
            "Если 401 — в терминале: <code>grok login</code>",
        )
        return

    if cmd in ("/redeploy", "/deploy", "/update"):
        # владелец: pull с GitHub + restart на Bothost
        tg.send_message(cfg, chat_id, "⏳ Тяну код с GitHub…")
        try:
            import deploy_lib

            res = deploy_lib.redeploy_now(restart=True)
            pull = res.get("pull") or {}
            files = ", ".join((pull.get("files") or [])[:12])
            rst = res.get("restart") or {}
            body = (
                "🚀 <b>Redeploy</b>\n\n"
                f"ver: <code>{html.escape(BOT_CODE_VERSION)}</code>\n"
                f"remote: <code>{html.escape(str(res.get('remote_sha') or pull.get('sha') or '—'))}</code>\n"
                f"files: {pull.get('count') or 0}\n"
                f"<code>{html.escape(files[:400])}</code>\n\n"
                f"restart: {html.escape(str(rst.get('message') or rst.get('error') or rst.get('reason') or rst))[:200]}\n"
            )
            if res.get("pull_error"):
                body = f"❌ Pull fail: {html.escape(str(res['pull_error'])[:300])}"
            tg.send_message(cfg, chat_id, body)
        except Exception as e:
            tg.send_message(
                cfg, chat_id, f"❌ redeploy: {html.escape(str(e)[:300])}"
            )
        return

    if cmd in ("/deploy_status", "/depstatus"):
        try:
            import deploy_lib

            need, remote, local = deploy_lib.needs_update()
            tg.send_message(
                cfg,
                chat_id,
                "📦 <b>Deploy status</b>\n\n"
                f"ver: <code>{html.escape(BOT_CODE_VERSION)}</code>\n"
                f"local sha: <code>{html.escape((local or '—')[:12])}</code>\n"
                f"remote: <code>{html.escape((remote or '—')[:12])}</code>\n"
                f"need update: <b>{'YES' if need else 'no'}</b>\n\n"
                "Обновить: /redeploy",
            )
        except Exception as e:
            tg.send_message(cfg, chat_id, f"❌ {html.escape(str(e)[:250])}")
        return

    if cmd == "/react_on":
        cfg["auto_react_comments"] = True
        save_config(cfg)
        tg.send_message(cfg, chat_id, "❤️ Авто-реакции на комменты: ВКЛ")
        return

    if cmd == "/react_off":
        cfg["auto_react_comments"] = False
        save_config(cfg)
        tg.send_message(cfg, chat_id, "Авто-реакции: ВЫКЛ")
        return

    if cmd == "/react":
        # /react 🔥   или  /react 🔥 123  или  /react 123
        parts = arg.split()
        emoji = "🔥"
        mid = None
        if not parts:
            pub = state.get("published") or []
            if not pub or not pub[0].get("channel_message_id"):
                tg.send_message(cfg, chat_id, "Нет поста. Сначала /post или /react 🔥 message_id")
                return
            mid = int(pub[0]["channel_message_id"])
        elif len(parts) == 1:
            if parts[0].isdigit():
                mid = int(parts[0])
            else:
                emoji = parts[0]
                pub = state.get("published") or []
                if not pub or not pub[0].get("channel_message_id"):
                    tg.send_message(cfg, chat_id, "Нет message_id. /react 🔥 123")
                    return
                mid = int(pub[0]["channel_message_id"])
        else:
            if parts[0].isdigit():
                mid, emoji = int(parts[0]), parts[1]
            else:
                emoji, mid = parts[0], int(parts[1]) if parts[1].isdigit() else None
            if mid is None:
                tg.send_message(cfg, chat_id, "Пример: /react 🔥 42")
                return
        try:
            channel = cfg.get("channel_id") or "@Vaggo01"
            tg.set_message_reaction(cfg, channel, mid, emoji)
            tg.send_message(cfg, chat_id, f"{emoji} реакция на msg {mid}")
        except Exception as e:
            tg.send_message(
                cfg,
                chat_id,
                f"❌ Реакция: {html.escape(str(e))}\n"
                "Нужны права в канале. Эмодзи — из списка Telegram (🔥❤👍🎉…).",
            )
        return

    if cmd == "/pause":
        cfg["paused"] = True
        save_config(cfg)
        tg.send_message(cfg, chat_id, "⏸ Пауза. Посты и авто-комменты не уходят.")
        return

    if cmd == "/resume":
        cfg["paused"] = False
        save_config(cfg)
        tg.send_message(cfg, chat_id, "▶️ Снято с паузы.")
        return

    if cmd == "/auto_on":
        cfg["auto_reply_comments"] = True
        save_config(cfg)
        tg.send_message(cfg, chat_id, "Комменты: черновики ответов тебе на ок (если не instant).")
        return

    if cmd == "/auto_off":
        cfg["auto_reply_comments"] = False
        save_config(cfg)
        tg.send_message(cfg, chat_id, "Обработка комментов выкл.")
        return

    if cmd == "/instant_on":
        cfg["auto_reply_comments"] = True
        cfg["comment_needs_owner_ok"] = False
        save_config(cfg)
        tg.send_message(cfg, chat_id, "⚡ Instant: ответы в комменты сразу (осторожно).")
        return

    if cmd == "/instant_off":
        cfg["comment_needs_owner_ok"] = True
        save_config(cfg)
        tg.send_message(cfg, chat_id, "Снова: сначала черновик ответа тебе.")
        return

    if cmd == "/bind":
        # must be used from the discussion group OR with id arg
        if arg:
            try:
                gid = int(arg)
            except ValueError:
                tg.send_message(cfg, chat_id, "Пример: /bind -1001234567890")
                return
            cfg["discussion_group_id"] = gid
            save_config(cfg)
            tg.send_message(cfg, chat_id, f"✅ discussion_group_id = <code>{gid}</code>")
            return
        tg.send_message(
            cfg,
            chat_id,
            "Чтобы привязать группу комментов:\n"
            "1) Добавь бота в группу обсуждений канала\n"
            "2) В <b>этой группе</b> напиши /bind\n"
            "Или: /bind -100xxxxxxxxxx",
        )
        return

    if cmd == "/comments":
        pending = [c for c in (state.get("pending_comments") or []) if c.get("status") == "pending"]
        if not pending:
            tg.send_message(cfg, chat_id, "Очередь комментов пуста.")
            return
        for c in pending[:8]:
            preview = html.escape((c.get("comment_text") or "")[:300])
            reply = html.escape((c.get("reply_text") or "")[:500])
            tg.send_message(
                cfg,
                chat_id,
                f"💬 <b>Коммент</b> <code>{c['id']}</code>\n"
                f"От: {html.escape(str(c.get('from_name') or '?'))}\n"
                f"<i>{preview}</i>\n\n"
                f"<b>Ответ:</b>\n{reply}",
                reply_markup=comment_keyboard(c["id"]),
            )
        return

    if cmd == "/drafts":
        drafts = [d for d in (state.get("drafts") or []) if d.get("status") == "draft"]
        if not drafts:
            tg.send_message(cfg, chat_id, "Черновиков нет. /draft тема")
            return
        lines = ["📝 <b>Черновики</b>\n"]
        for d in drafts[:12]:
            prev = html.escape((d.get("text") or "")[:80].replace("\n", " "))
            lines.append(f"• <code>{d['id']}</code> — {prev}…")
        lines.append("\n/post — выложить последний\nИли кнопка под черновиком.")
        tg.send_message(cfg, chat_id, "\n".join(lines))
        return

    if cmd == "/last":
        pub = state.get("published") or []
        if not pub:
            tg.send_message(cfg, chat_id, "Пока ничего не публиковали из бота/пульта.")
            return
        lines = ["📤 <b>Последние публикации</b>\n"]
        for p in pub[:8]:
            prev = html.escape((p.get("text_preview") or "")[:100])
            lines.append(f"• msg {p.get('channel_message_id')} · {prev}")
        tg.send_message(cfg, chat_id, "\n".join(lines))
        return

    if cmd == "/pin":
        pub = state.get("published") or []
        if not pub or not pub[0].get("channel_message_id"):
            tg.send_message(cfg, chat_id, "Нет message_id. Сначала /post.")
            return
        try:
            channel = cfg.get("channel_id") or "@Vaggo01"
            mid = int(pub[0]["channel_message_id"])
            tg.pin_chat_message(cfg, channel, mid, silent=True)
            tg.send_message(cfg, chat_id, f"📌 Закрепил msg {mid}")
        except Exception as e:
            tg.send_message(cfg, chat_id, f"❌ Pin: {html.escape(str(e))}\nНужно право «Закрепление».")
        return

    if cmd == "/ideas":
        tg.send_message(cfg, chat_id, "⏳ Идеи…")
        try:
            ideas = generate_ideas(7, rubric=arg)
            tg.send_message(
                cfg,
                chat_id,
                f"💡 <b>Идеи</b>\n\n{html.escape(ideas)}\n\n"
                f"Бери строку → <code>/draft …</code>",
            )
        except Exception as e:
            tg.send_message(cfg, chat_id, f"❌ {html.escape(str(e))}")
        return

    if cmd == "/series":
        tg.send_message(cfg, chat_id, "⏳ Пишу серию из 5 черновиков… это минута-две")
        made = []
        try:
            for rubric, topic in series_topics():
                body = generate_post(topic, rubric=rubric)
                item = add_draft(state, body, rubric=rubric, source="series")
                made.append(item)
                state = load_state()
            tg.send_message(cfg, chat_id, f"✅ Готово черновиков: {len(made)}")
            for item in made:
                tg.send_message(
                    cfg,
                    chat_id,
                    f"📝 <code>{item['id']}</code> · {html.escape(item.get('rubric') or '')}\n\n{item['text'][:3500]}",
                    reply_markup=draft_keyboard(item["id"]),
                )
        except Exception as e:
            tg.send_message(cfg, chat_id, f"❌ {html.escape(str(e))}")
        return

    if cmd == "/rewrite":
        draft = get_draft(state, arg if arg else None)
        if not draft:
            tg.send_message(cfg, chat_id, "Нет черновика.")
            return
        tg.send_message(cfg, chat_id, "⏳ Переписываю…")
        try:
            body = rewrite_post(draft["text"])
            item = add_draft(state, body, rubric=draft.get("rubric") or "", source="rewrite")
            tg.send_message(
                cfg,
                chat_id,
                f"✏️ Новый вариант <code>{item['id']}</code>\n\n{body}",
                reply_markup=draft_keyboard(item["id"]),
            )
        except Exception as e:
            tg.send_message(cfg, chat_id, f"❌ {html.escape(str(e))}")
        return

    if cmd == "/post":
        draft = get_draft(state, arg if arg else None)
        if not draft:
            tg.send_message(cfg, chat_id, "Нет черновика. /draft тема")
            return
        try:
            result = publish_to_channel(cfg, draft["text"], draft=draft)
            mark_published(state, draft, result.get("message_id"))
            mid = result.get("message_id")
            if mid and cfg.get("auto_react_posts", True):
                try:
                    emo = pick_reaction_for_text(draft["text"])
                    tg.set_message_reaction(
                        cfg, cfg.get("channel_id") or "@Vaggo01", int(mid), emo
                    )
                except Exception as re:
                    print("react on post fail", re)
            tg.send_message(
                cfg,
                chat_id,
                f"✅ В канале. draft=<code>{draft['id']}</code> msg={mid}\n"
                f"/pin — закрепить · /react 🎉 {mid}",
            )
        except Exception as e:
            tg.send_message(cfg, chat_id, f"❌ {html.escape(str(e))}\nСделай /check")
        return

    if cmd == "/draft":
        if not arg:
            tg.send_message(cfg, chat_id, "Пример: /draft Вечерний Вагго про цифровой шум")
            return
        tg.send_message(cfg, chat_id, "⏳ Пишу черновик…")
        try:
            body = generate_post(arg)
            item = add_draft(state, body, source="bot")
            tg.send_message(
                cfg,
                chat_id,
                f"📝 Черновик <code>{item['id']}</code>\n\n{body}",
                reply_markup=draft_keyboard(item["id"]),
            )
        except Exception as e:
            tg.send_message(cfg, chat_id, f"❌ {html.escape(str(e))}")
        return

    if cmd == "/guide":
        if not arg:
            tg.send_message(
                cfg,
                chat_id,
                "Длинный полезный гайд (~2500–3800 символов).\n"
                "Пример: /guide 5 промптов для обложек Midjourney\n"
                "или: /guide как выбрать между ChatGPT и Claude",
            )
            return
        tg.send_message(cfg, chat_id, "⏳ Пишу гайд… минута")
        try:
            body = generate_guide(arg)
            item = add_draft(state, body, rubric="Гайд", source="guide")
            tg.send_message(
                cfg,
                chat_id,
                f"📋 Гайд <code>{item['id']}</code> · {len(body)} симв.\n\n{body[:3500]}",
                reply_markup=draft_keyboard(item["id"]),
            )
            if len(body) > 3500:
                tg.send_message(cfg, chat_id, body[3500:], parse_mode="HTML")
        except Exception as e:
            tg.send_message(cfg, chat_id, f"❌ {html.escape(str(e))}")
        return

    if cmd in ("/photo", "/img", "/video"):
        if not arg:
            tg.send_message(
                cfg,
                chat_id,
                "Пример:\n"
                "/photo Вечерний Вагго: цифровой шум\n"
                "/video Абстрактный нейро-город ночью\n"
                "/img только картинка без текста поста",
            )
            return
        want_video = cmd == "/video"
        caption_mode = cmd != "/img"
        tg.send_message(
            cfg,
            chat_id,
            "🎨 Imagine… " + ("видео 10–40 сек" if want_video else "фото") + " — жду",
        )
        try:
            from imagine import generate_image, generate_video, style_prompt_for_channel

            media_prompt = style_prompt_for_channel(arg)
            if want_video:
                path = generate_video(media_prompt if len(arg) < 40 else arg, cfg=cfg)
            else:
                path = generate_image(media_prompt, cfg=cfg)

            caption = ""
            draft_id = ""
            if caption_mode:
                body = generate_post(arg)
                item = add_draft(state, body, source="imagine")
                caption = body[:1024]
                draft_id = item["id"]
                # store media path on draft
                item["media_path"] = str(path)
                item["media_type"] = "video" if want_video else "photo"
                state = load_state()
                for d in state.get("drafts") or []:
                    if d.get("id") == draft_id:
                        d["media_path"] = str(path)
                        d["media_type"] = "video" if want_video else "photo"
                save_state(state)

            # preview to owner
            if want_video:
                tg.send_video(cfg, chat_id, str(path), caption=caption or arg[:200])
            else:
                tg.send_photo(cfg, chat_id, str(path), caption=caption or arg[:200])

            if draft_id:
                tg.send_message(
                    cfg,
                    chat_id,
                    f"✅ Медиа + черновик <code>{draft_id}</code>\n"
                    f"Жми «В канал» — уйдёт фото/видео с подписью.",
                    reply_markup=draft_keyboard(draft_id),
                )
            else:
                tg.send_message(cfg, chat_id, f"✅ Файл: <code>{html.escape(path.name)}</code>")
        except Exception as e:
            tg.send_message(cfg, chat_id, f"❌ Imagine: {html.escape(str(e))}")
        return

    if cmd == "/raw":
        state["await_raw"] = True
        save_state(state)
        tg.send_message(cfg, chat_id, "Пришли следующим сообщением готовый текст поста (можно HTML).")
        return

    if state.get("await_raw") and not text.startswith("/"):
        state["await_raw"] = False
        item = add_draft(state, text, source="raw")
        save_state(state)
        tg.send_message(
            cfg,
            chat_id,
            f"📝 Черновик <code>{item['id']}</code> сохранён.",
            reply_markup=draft_keyboard(item["id"]),
        )
        return

    if text and not text.startswith("/"):
        tg.send_message(cfg, chat_id, "⏳ Делаю пост…")
        try:
            body = generate_post(text)
            item = add_draft(state, body, source="owner_chat")
            tg.send_message(
                cfg,
                chat_id,
                f"📝 Черновик <code>{item['id']}</code>\n\n{body}",
                reply_markup=draft_keyboard(item["id"]),
            )
        except Exception as e:
            tg.send_message(cfg, chat_id, f"❌ {html.escape(str(e))}")


def handle_owner_menu_callback(
    cfg: dict, state: dict, cq: dict, *, data: str, chat_id, mid, user, uid
) -> bool:
    """
    Пульт владельца. Любой menu:* (кроме userhome).
    Быстро, без Grok, без «неизвестная кнопка».
    """
    if not data.startswith("menu:"):
        return False
    if data == "menu:userhome":
        return False
    if not is_owner(cfg, user):
        try:
            tg.answer_callback(cfg, cq["id"], "Только владелец", show_alert=True)
        except Exception:
            pass
        return True

    raw = (data[5:] or "").strip()
    # старые вложенные форматы → home
    if raw.startswith(("g:", "grp_", "grp:")):
        raw = "home"

    try:
        tg.answer_callback(cfg, cq["id"], "…")
    except Exception:
        pass

    uid_m = uid or None

    def home(force: bool = False) -> None:
        _owner_panel(
            cfg,
            state,
            chat_id,
            None if force else mid,
            uid_m,
            owner_home_html(),
            main_menu_keyboard(),
            force_new=force,
        )

    if raw in ("home", "main", "root", ""):
        # home: edit текущего; если мёртв — сам восстановится
        home(force=False)
        return True
    if raw == "fresh":
        home(force=True)
        return True
    if raw == "more":
        # сервисное подменю — только UI, без смены логики
        _owner_panel(
            cfg,
            state,
            chat_id,
            mid,
            uid_m,
            f"⚙️  <b>Ещё · сервис</b>\n"
            f"{'─' * 18}\n\n"
            f"Grok · касса · прайс · радар\n"
            f"restore · кнопки GW · чистка\n\n"
            f"<i>Или напиши:</i> «какие заказы горят»\n"
            f"<i>Назад — в пульт</i>",
            owner_more_keyboard(),
        )
        return True
    if raw in ("prices", "price", "pricing", "прайс"):
        _owner_panel(
            cfg,
            state,
            chat_id,
            mid,
            uid_m,
            orders.pricing_admin_html()
            + "\n\n<code>/setprice bot 400</code> · "
            "<code>/discount 10</code>",
            orders.pricing_admin_keyboard(),
        )
        return True
    if raw in ("points", "баллы"):
        pts = bal.get_points(int(uid or 0))
        _owner_panel(
            cfg,
            state,
            chat_id,
            mid,
            uid_m,
            bal.format_points_help()
            + f"\n\nТвои баллы (тест): <b>{pts}</b> ⭐\n"
            f"<i>Клиентам: /points</i>",
            bal.points_keyboard(),
        )
        return True
    if raw == "radar":
        try:
            import growth_lib as growth

            body = growth.finance_radar_html()
        except Exception as e:
            body = f"❌ {html.escape(str(e)[:200])}"
        _owner_panel(cfg, state, chat_id, mid, uid_m, body, menu_result_keyboard())
        return True
    if raw == "hot":
        try:
            import growth_lib as growth

            body = growth.team_lead_html("какие заказы горят") or "Нет данных."
        except Exception as e:
            body = f"❌ {html.escape(str(e)[:200])}"
        _owner_panel(cfg, state, chat_id, mid, uid_m, body, menu_result_keyboard())
        return True
    if raw == "clean":
        handle_owner_system(
            cfg,
            state,
            {
                "chat": {"id": chat_id, "type": "private"},
                "from": user,
                "text": "/clean",
                "message_id": mid,
            },
        )
        return True

    body = ""
    try:
        if raw == "queue":
            body = format_queue_report()
        elif raw == "stats":
            body = status_text(cfg, state)
        elif raw == "qnow":
            if cfg.get("paused"):
                body = "⏸ Пауза. Жми ▶️ в меню."
            else:
                due = due_items()
                item = due[0] if due else (queue_summary().get("next"))
                if not item:
                    body = "Очередь пуста."
                else:
                    iid = str(item.get("id"))
                    try:
                        queue_publish_now(iid)
                        item = get_item(iid) or item
                        pmid = publish_queue_item(cfg, item)
                        body = (
                            f"✅ <code>{html.escape(iid)}</code>\n"
                            f"https://t.me/Vaggo01/{pmid}"
                        )
                    except Exception as e:
                        body = f"❌ {html.escape(str(e)[:300])}"
        elif raw == "pause":
            cfg["paused"] = True
            save_config(cfg)
            body = "⏸ Пауза."
        elif raw == "resume":
            cfg["paused"] = False
            save_config(cfg)
            body = "▶️ Resume."
        elif raw == "toggle_pause":
            cfg["paused"] = not bool(cfg.get("paused"))
            save_config(cfg)
            body = "⏸ Пауза." if cfg["paused"] else "▶️ Resume."
        elif raw == "drafts":
            drafts = (state.get("drafts") or [])[-12:]
            if not drafts:
                body = "📝 Черновиков нет."
            else:
                lines = ["📝 <b>Черновики</b>\n"]
                for d in reversed(drafts):
                    lines.append(
                        f"• <code>{html.escape(str(d.get('id')))}</code> "
                        f"{html.escape((d.get('text') or '')[:40])}"
                    )
                body = "\n".join(lines)
        elif raw == "ideas":
            # без Grok — не жрём лимит; короткий шаблон
            body = (
                "💡 <b>Идеи (быстро)</b>\n\n"
                "• Вечерний Вагго: 1 мысль + 1 действие\n"
                "• Битва сеток: Claude vs ChatGPT на одну задачу\n"
                "• Прокачка: 15 мин без телефона\n"
                "• Кибер-лайфхак: 1 фишка Windows/телефона\n"
                "• Проект: что сделали за день\n\n"
                "Полные идеи с Grok: /ideas"
            )
        elif raw == "comments":
            pend = state.get("pending_comments") or []
            body = f"💬 Комменты ждут: <b>{len(pend)}</b>"
        elif raw == "giveaway":
            try:
                act = gw.get_active(state)
                body = gw.format_status(act)
                if act:
                    mid_ch = act.get("channel_message_id")
                    if mid_ch:
                        body += f"\n\nПост: https://t.me/Vaggo01/{mid_ch}"
                # специальная клавиатура — не menu_result
                kb = {
                    "inline_keyboard": [
                        [
                            {"text": "👥 Участники", "callback_data": "menu:gentries"},
                            {"text": "🎲 Розыгрыш", "callback_data": "menu:gdraw"},
                        ],
                        [
                            {"text": "🔄 Обновить", "callback_data": "menu:giveaway"},
                            {"text": "🏠 Меню", "callback_data": "menu:home"},
                        ],
                        [
                            {
                                "text": "📣 Как создать",
                                "callback_data": "menu:ghelp",
                            }
                        ],
                    ]
                }
                _owner_panel(cfg, state, chat_id, mid, uid_m, body, kb)
                return True
            except Exception as e:
                body = f"❌ Розыгрыш: {html.escape(str(e)[:200])}"
        elif raw == "gentries":
            try:
                act = gw.get_active(state)
                if not act:
                    body = "👥 Нет активного розыгрыша.\n/gnew приз → /gpost"
                else:
                    body = "👥 <b>Участники</b>\n\n" + gw.format_entries(act)
                kb = {
                    "inline_keyboard": [
                        [
                            {"text": "🎁 К розыгрышу", "callback_data": "menu:giveaway"},
                            {"text": "🎲 Draw", "callback_data": "menu:gdraw"},
                        ],
                        [{"text": "🏠 Меню", "callback_data": "menu:home"}],
                    ]
                }
                _owner_panel(cfg, state, chat_id, mid, uid_m, body, kb)
                return True
            except Exception as e:
                body = f"❌ {html.escape(str(e)[:200])}"
        elif raw == "gdraw":
            try:
                act = gw.get_active(state)
                if not act:
                    body = "Нет активного. /gnew"
                elif gw.entry_count(act, complete_only=True) == 0:
                    body = "Нет complete-участников — тянуть некого."
                else:
                    winners = finish_giveaway_draw(cfg, act, notify_chat=chat_id)
                    if winners:
                        names = ", ".join(
                            html.escape(
                                str(w.get("username") or w.get("name") or w.get("user_id"))
                            )
                            for w in winners
                        )
                        body = f"🏆 Победитель(и): <b>{names}</b>"
                    else:
                        body = "Никого не вытянули (пул пуст после live-check)."
            except Exception as e:
                body = f"❌ draw: {html.escape(str(e)[:200])}"
        elif raw == "ghelp":
            body = (
                "🎁 <b>Розыгрыш — как запустить</b>\n\n"
                "1. <code>/gnew Google AI Pro 18 мес | 72</code>\n"
                "   (часы до авто-итога)\n"
                "2. <code>/gpost</code> — пост в канал с кнопкой\n"
                "3. Люди жмут «Участвовать» → подписка + скрин репоста другу\n"
                "4. По таймеру сам / или кнопка 🎲 / <code>/gdraw</code>\n\n"
                "<code>/gstatus</code> · <code>/gentries</code> · <code>/gend</code>\n"
                "<code>/gwrestore</code> — вернуть участников с бэкапа"
            )
        elif raw == "gwrestore":
            try:
                res = gw.apply_restore_seed(force=True)
                body = (
                    "♻️ <b>Restore</b>\n"
                    f"{html.escape(str(res.get('message') or res))}\n"
                    f"complete: <b>{res.get('complete') or 0}</b> · "
                    f"started: {res.get('started') or 0}\n"
                    f"id: <code>{html.escape(str(res.get('active_id') or '—'))}</code>\n\n"
                    "Дальше: <code>/gfixkb</code> — кнопки на пост канала"
                )
            except Exception as e:
                body = f"❌ {html.escape(str(e)[:200])}"
        elif raw == "gfixkb":
            try:
                item = gw.get_active(state)
                if not item:
                    gw.apply_restore_seed(force=True)
                    item = gw.get_active()
                if not item:
                    body = "Нет розыгрыша. /gwrestore"
                else:
                    mid_ch = item.get("channel_message_id")
                    if not mid_ch:
                        body = "Нет id поста. /gbind 102"
                    else:
                        channel = cfg.get("channel_id") or "@Vaggo01"
                        tg.edit_reply_markup(
                            cfg,
                            channel,
                            int(mid_ch),
                            gw.join_keyboard(item, bot_username=_bot_username(cfg)),
                        )
                        body = (
                            f"✅ Кнопки на посте\n"
                            f"https://t.me/Vaggo01/{mid_ch}\n"
                            f"complete: <b>{gw.entry_count(item, complete_only=True)}</b>"
                        )
            except Exception as e:
                body = f"❌ gfixkb: {html.escape(str(e)[:200])}"
        elif raw == "promo":
            try:
                from promo_lib import PROMO_HTML

                body = "📢 <b>Реклама</b>\n\n" + PROMO_HTML
            except Exception as e:
                body = f"❌ {html.escape(str(e)[:200])}"
        elif raw == "orders":
            items = orders.list_orders(limit=15)
            body = orders.format_owner_orders_list(items)
            _owner_panel(
                cfg,
                state,
                chat_id,
                mid,
                uid_m,
                body,
                orders.owner_orders_list_keyboard(items),
            )
            return True
        elif raw == "balance":
            try:
                b = bal.get_balance(int(uid_m or 0))
                body = f"💰 Баланс: <b>{b}</b> ₽"
            except Exception as e:
                body = f"❌ {html.escape(str(e)[:200])}"
        elif raw == "brains":
            # быстро: без probe сети
            from content import brain_status, grok_ok

            bst = brain_status(cfg, use_cache=True, probe_ollama=False)
            body = (
                "🧠 <b>Мозг</b>\n"
                f"active: {html.escape(str(bst.get('active')))}\n"
                f"source: {html.escape(str(bst.get('grok_source') or '—'))}\n"
                f"ver: <code>{html.escape(BOT_CODE_VERSION)}</code>\n"
                f"bridge: {'да' if grok_ok(cfg) else 'нет/fallback'}"
            )
        elif raw == "deploy":
            try:
                import deploy_lib

                need, remote, local = deploy_lib.needs_update()
                body = (
                    "📦 <b>Deploy</b>\n"
                    f"ver: <code>{html.escape(BOT_CODE_VERSION)}</code>\n"
                    f"local: <code>{html.escape((local or '—')[:12])}</code>\n"
                    f"remote: <code>{html.escape((remote or '—')[:12])}</code>\n"
                    f"need: {'YES' if need else 'no'}\n"
                    "/redeploy"
                )
            except Exception as e:
                body = f"❌ {html.escape(str(e)[:200])}"
        else:
            # любое непонятное → домой, без слова «неизвестная»
            home(force=True)
            return True

        _owner_panel(
            cfg,
            state,
            chat_id,
            mid,
            uid_m,
            body,
            menu_result_keyboard(),
        )
    except Exception as e:
        print("menu action fail", raw, e, flush=True)
        _owner_panel(
            cfg,
            state,
            chat_id,
            mid,
            uid_m,
            f"❌ {html.escape(str(e)[:250])}\n\nЖми 🏠 В меню",
            menu_result_keyboard(),
        )
    return True


def handle_callback(cfg: dict, state: dict, cq: dict) -> None:
    user = cq.get("from") or {}
    data = cq.get("data") or ""
    msg = cq.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    mid = msg.get("message_id")
    uid = int(user.get("id") or 0)

    # рефералка (всем)
    if data == "ref:me" and uid:
        try:
            import growth_lib as growth

            code = growth.ref_code_for_user(uid)
            bot_un = "DirectorVaggobot"
            try:
                me = tg.get_me(cfg)
                if me.get("username"):
                    bot_un = me["username"]
            except Exception:
                pass
            link = f"https://t.me/{bot_un}?start={code}"
            tg.answer_callback(cfg, cq["id"], "Ссылка")
            if chat_id:
                tg.send_message(
                    cfg,
                    chat_id,
                    f"🔗 <b>Твоя ссылка</b>\n\n"
                    f"<code>{html.escape(link)}</code>\n\n"
                    f"Друг заходит → первый оплаченный заказ → "
                    f"тебе <b>+{growth.REF_BONUS_RUB} ₽</b> на баланс.",
                    parse_mode="HTML",
                    disable_preview=True,
                )
        except Exception as e:
            tg.answer_callback(cfg, cq["id"], str(e)[:100], show_alert=True)
        return

    # ПУЛЬТ ВЛАДЕЛЬЦА — первым (чтобы ничего не перехватывало)
    if data.startswith("menu:") and data != "menu:userhome":
        if handle_owner_menu_callback(
            cfg, state, cq, data=data, chat_id=chat_id, mid=mid, user=user, uid=uid
        ):
            return

    # Условия — всегда (даже без принятия)
    if data.startswith("terms:"):
        handle_terms_callback(cfg, state, cq)
        return

    # Юр.док / тарифы — всегда (Platega: постоянный доступ)
    if data.startswith("legal:"):
        handle_legal_callback(cfg, state, cq)
        return

    # Тикеты поддержки — всегда (и до принятия условий)
    if data.startswith("sup:"):
        handle_support_callback(cfg, state, cq)
        return

    # Модерация: разблок — только owner callback
    if data.startswith("mod:"):
        handle_mod_callback(cfg, state, cq)
        return

    # Блок — жёсткий (кроме владельца). terms/legal уже обработаны выше → документы доступны
    if uid and not is_owner(cfg, user) and mod.is_blocked(uid):
        try:
            tg.answer_callback(cfg, cq["id"], "Аккаунт заблокирован", show_alert=True)
        except Exception:
            pass
        if chat_id:
            tg.send_message(
                cfg,
                chat_id,
                mod.blocked_user_message(),
                parse_mode="HTML",
                disable_preview=False,
                reply_markup={
                    "inline_keyboard": [
                        [
                            {"text": "🔒 Политика", "url": terms.PRIVACY_URL},
                            {"text": "📜 Оферта", "url": terms.AGREEMENT_URL},
                        ],
                        [{"text": "📋 Документы", "callback_data": "legal:hub"}],
                    ]
                },
            )
        return

    # Меню пользователя (после accept)
    if data == "menu:userhome":
        _safe_answer_cq(cfg, cq["id"], "Меню")
        if chat_id:
            # если mid мёртв — force_new через edit fail recovery;
            # клик по «Обновить» предпочитает edit, иначе send
            ui_edit_or_send(
                cfg,
                chat_id,
                terms.user_home_html(uid),
                reply_markup=terms.after_accept_keyboard(uid),
                message_id=mid,
                state=state,
                uid=uid or None,
                store_key="terms_ui_msg",
                force_new=False,
            )
        return

    # Розыгрыш — ДО gate условий (иначе «Участвовать» / кнопки квеста тупят)
    if data.startswith("gw:"):
        handle_giveaway_callback(cfg, state, cq)
        return

    # Без принятия — только terms/legal/sup (кроме владельца)
    if uid and not is_owner(cfg, user) and not terms.is_accepted(uid):
        try:
            tg.answer_callback(
                cfg, cq["id"], "Сначала прими условия", show_alert=True
            )
        except Exception:
            pass
        if chat_id:
            send_terms_gate(cfg, chat_id, state=state, uid=uid, message_id=mid)
        return

    # Заказы — всем (ord:type / ord:ok) + owner (ord:own:)
    if data.startswith("ord:"):
        handle_orders_callback(cfg, state, cq)
        return

    # Баланс / СБП
    if data.startswith("bal:"):
        handle_balance_callback(cfg, state, cq)
        return

    # Platega: проверить / отмена
    if data.startswith("pay:"):
        handle_platega_callback(cfg, state, cq)
        return

    # Прайс / скидки — только владелец
    if data.startswith("price:"):
        if not is_owner(cfg, user):
            try:
                tg.answer_callback(cfg, cq["id"], "Только владелец", show_alert=True)
            except Exception:
                pass
            return
        handle_pricing_callback(cfg, state, cq)
        return

    # Всё остальное (меню, черновики, пауза…) — ТОЛЬКО владелец
    if not is_owner(cfg, user):
        try:
            tg.answer_callback(
                cfg,
                cq["id"],
                "Меню: /start · /support · /legal",
                show_alert=True,
            )
        except Exception:
            pass
        return

    if data.startswith("pub:"):
        did = data.split(":", 1)[1]
        draft = get_draft(state, did)
        if not draft:
            tg.answer_callback(cfg, cq["id"], "Не найден")
            return
        try:
            result = publish_to_channel(cfg, draft["text"], draft=draft)
            mark_published(state, draft, result.get("message_id"))
            pmid = result.get("message_id")
            if pmid and cfg.get("auto_react_posts", True):
                try:
                    tg.set_message_reaction(
                        cfg,
                        cfg.get("channel_id") or "@Vaggo01",
                        int(pmid),
                        pick_reaction_for_text(draft.get("text") or ""),
                    )
                except Exception as re:
                    print("react fail", re)
            tg.answer_callback(cfg, cq["id"], "Опубликовано")
            tg.edit_reply_markup(cfg, chat_id, mid, None)
            tg.send_message(cfg, chat_id, f"✅ В канале. draft={did} msg={pmid}")
        except Exception as e:
            tg.answer_callback(cfg, cq["id"], "Ошибка")
            tg.send_message(cfg, chat_id, f"❌ {html.escape(str(e))}")
        return

    if data.startswith("rew:"):
        did = data.split(":", 1)[1]
        draft = get_draft(state, did)
        if not draft:
            tg.answer_callback(cfg, cq["id"], "Не найден")
            return
        tg.answer_callback(cfg, cq["id"], "Переписываю…")
        try:
            body = rewrite_post(draft["text"])
            item = add_draft(state, body, rubric=draft.get("rubric") or "", source="rewrite")
            tg.send_message(
                cfg,
                chat_id,
                f"✏️ <code>{item['id']}</code>\n\n{body}",
                reply_markup=draft_keyboard(item["id"]),
            )
        except Exception as e:
            tg.send_message(cfg, chat_id, f"❌ {html.escape(str(e))}")
        return

    if data.startswith("drop:"):
        did = data.split(":", 1)[1]
        for d in state.get("drafts") or []:
            if d.get("id") == did:
                d["status"] = "dropped"
        save_state(state)
        tg.answer_callback(cfg, cq["id"], "Удалено")
        tg.edit_reply_markup(cfg, chat_id, mid, None)
        return

    if data.startswith("creply:"):
        cid = data.split(":", 1)[1]
        item = next((c for c in state.get("pending_comments") or [] if c.get("id") == cid), None)
        if not item:
            tg.answer_callback(cfg, cq["id"], "Не найдено")
            return
        try:
            tg.send_message(
                cfg,
                item["chat_id"],
                item.get("reply_text") or "👍",
                reply_to=item.get("message_id"),
                parse_mode=None,
                message_thread_id=item.get("message_thread_id"),
                allow_without_reply=False,
                disable_preview=True,
            )
            item["status"] = "replied"
            save_state(state)
            tg.answer_callback(cfg, cq["id"], "Ответ ушёл")
            tg.edit_reply_markup(cfg, chat_id, mid, None)
        except Exception as e:
            tg.answer_callback(cfg, cq["id"], "Ошибка")
            tg.send_message(cfg, chat_id, f"❌ {html.escape(str(e))}")
        return

    if data.startswith("cskip:"):
        cid = data.split(":", 1)[1]
        for c in state.get("pending_comments") or []:
            if c.get("id") == cid:
                c["status"] = "skipped"
        save_state(state)
        tg.answer_callback(cfg, cq["id"], "Пропуск")
        tg.edit_reply_markup(cfg, chat_id, mid, None)
        return

    if data.startswith("crewrite:"):
        cid = data.split(":", 1)[1]
        item = next((c for c in state.get("pending_comments") or [] if c.get("id") == cid), None)
        if not item:
            tg.answer_callback(cfg, cq["id"], "Не найдено")
            return
        try:
            new_reply = generate_comment_reply(item.get("comment_text") or "")
            item["reply_text"] = new_reply
            save_state(state)
            tg.answer_callback(cfg, cq["id"], "Новый")
            tg.send_message(
                cfg,
                chat_id,
                f"✏️ <code>{cid}</code>:\n{html.escape(new_reply)}",
                reply_markup=comment_keyboard(cid),
            )
        except Exception as e:
            tg.send_message(cfg, chat_id, f"❌ {html.escape(str(e))}")
        return

    tg.answer_callback(cfg, cq["id"])


def maybe_bind_group(cfg: dict, msg: dict) -> bool:
    """В группе: /bind привязывает discussion_group_id."""
    chat = msg.get("chat") or {}
    if chat.get("type") not in ("group", "supergroup"):
        return False
    text = (msg.get("text") or "").strip()
    user = msg.get("from") or {}
    if not is_owner(cfg, user):
        return False
    cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
    if cmd != "/bind":
        # soft hint once: if no disc set and owner messages in group
        return False
    gid = chat.get("id")
    cfg["discussion_group_id"] = gid
    save_config(cfg)
    try:
        tg.send_message(
            cfg,
            gid,
            f"✅ Группа привязана как обсуждение канала.\n"
            f"id=<code>{gid}</code>\nКомменты пойдут владельцу на ок (или instant).",
        )
    except Exception as e:
        print("bind notify", e)
    notify_owner(cfg, f"✅ discussion_group_id = <code>{gid}</code> ({html.escape(chat.get('title') or '')})")
    return True


def maybe_hint_unknown_group(cfg: dict, state: dict, msg: dict) -> None:
    """Если бот видит группу без bind — подсказать id владельцу (редко)."""
    chat = msg.get("chat") or {}
    if chat.get("type") not in ("group", "supergroup"):
        return
    disc = cfg.get("discussion_group_id") or 0
    gid = chat.get("id")
    if disc and gid == disc:
        return
    if msg.get("from", {}).get("is_bot"):
        return
    # only when owner writes
    if not is_owner(cfg, msg.get("from")):
        return
    text = (msg.get("text") or "")
    if text.startswith("/"):
        return
    key = f"hinted_{gid}"
    if state.get(key):
        return
    state[key] = int(time.time())
    save_state(state)
    notify_owner(
        cfg,
        f"👁 Бот видит группу <b>{html.escape(chat.get('title') or '?')}</b>\n"
        f"id=<code>{gid}</code>\n"
        f"Если это обсуждение канала — напиши там /bind\n"
        f"или /bind {gid} в личке боту.",
    )


# простой антиспам для комментов (в памяти процесса)
_comment_rate: dict[int, float] = {}
_comment_global: list[float] = []


def _rate_ok(user_id: int, cfg: dict) -> bool:
    # владелец — без лимита (тесты/ответы не глушим)
    try:
        owners = {int(x) for x in (cfg.get("owner_user_ids") or [])}
        if int(user_id) in owners:
            return True
    except Exception:
        pass
    now = time.time()
    per_user = float(cfg.get("comment_rate_user_sec") or 5)
    per_min = int(cfg.get("comment_rate_global_per_min") or 30)
    last = _comment_rate.get(user_id) or 0
    if now - last < per_user:
        return False
    # global window
    while _comment_global and now - _comment_global[0] > 60:
        _comment_global.pop(0)
    if len(_comment_global) >= per_min:
        return False
    _comment_rate[user_id] = now
    _comment_global.append(now)
    return True


def _log_comment_event(state: dict, event: dict) -> None:
    log = state.setdefault("comment_log", [])
    log.insert(0, {**event, "ts": int(time.time())})
    state["comment_log"] = log[:80]
    save_state(state)


def _channel_ids_match(cfg: dict, chat_id, uname: str = "") -> bool:
    """Сверить id/username канала (int/str не должны ломать фильтр)."""
    ch_user = (cfg.get("channel_username") or "Vaggo01").lower().lstrip("@")
    uname = (uname or "").lower().lstrip("@")
    if uname and uname == ch_user:
        return True
    try:
        cid = int(chat_id)
    except Exception:
        cid = None
    try:
        ch_num = int(cfg.get("channel_numeric_id") or 0) or None
    except Exception:
        ch_num = None
    if ch_num and cid and cid == ch_num:
        return True
    ch_id = str(cfg.get("channel_id") or "").strip()
    if ch_id and str(chat_id) == ch_id:
        return True
    if ch_id.startswith("@") and uname and uname == ch_id.lstrip("@").lower():
        return True
    # публичный канал без username в апдейте — если numeric совпал выше; иначе нет
    return False


def maybe_react_channel_post(cfg: dict, state: dict, post: dict) -> None:
    """Реакция на каждый новый пост канала."""
    if cfg.get("paused"):
        return
    if not cfg.get("auto_react_posts", True):
        return
    chat = post.get("chat") or {}
    chat_id = chat.get("id")
    mid = post.get("message_id")
    if not chat_id or not mid:
        return
    uname = (chat.get("username") or "").lower()
    if not _channel_ids_match(cfg, chat_id, uname):
        return

    text = (post.get("text") or post.get("caption") or "")[:300]
    # ключ без «плавающего» chat_id (str/int)
    key = f"reacted_ch_{mid}"
    if state.get(key):
        return
    emoji = (
        (cfg.get("channel_react_emoji") or "").strip()
        or pick_reaction_for_text(text)
        or "🔥"
    )
    try:
        tg.set_message_reaction(cfg, chat_id, int(mid), emoji)
        state[key] = int(time.time())
        # cleanup
        keys = [k for k in state if str(k).startswith("reacted_ch_")]
        if len(keys) > 200:
            for k in sorted(keys, key=lambda x: state.get(x) or 0)[:50]:
                state.pop(k, None)
        save_state(state)
        print(f"channel react ok mid={mid} emoji={emoji}", flush=True)
    except Exception as e:
        print("channel react fail", mid, str(e)[:120], flush=True)
        # не помечаем key — попробуем ещё раз с discussion-форварда

def maybe_seed_under_channel_forward(cfg: dict, state: dict, msg: dict) -> bool:
    """Когда в обсуждении появился авто-форвард поста — реакция на пост + seed-коммент."""
    if cfg.get("paused"):
        return False
    if not msg.get("is_automatic_forward") and not msg.get("forward_from_message_id"):
        return False
    disc = cfg.get("discussion_group_id") or 0
    if not disc or (msg.get("chat") or {}).get("id") != disc:
        return False
    ch_mid = msg.get("forward_from_message_id")
    if not ch_mid:
        return False

    post_ctx = (msg.get("text") or msg.get("caption") or "")[:1500]
    channel = cfg.get("channel_id") or "@Vaggo01"

    # backup: реакция на пост канала (если channel_post не дошёл / эмодзи упал)
    rkey = f"reacted_ch_{ch_mid}"
    if cfg.get("auto_react_posts", True) and not state.get(rkey):
        emoji = (
            (cfg.get("channel_react_emoji") or "").strip()
            or pick_reaction_for_text(post_ctx)
            or "🔥"
        )
        try:
            tg.set_message_reaction(cfg, channel, int(ch_mid), emoji)
            state[rkey] = int(time.time())
            save_state(state)
            print(f"channel react via discuss mid={ch_mid} emoji={emoji}", flush=True)
        except Exception as e:
            print("channel react via discuss fail", ch_mid, str(e)[:100], flush=True)

    if not cfg.get("auto_seed_comment", True):
        return True  # реакцию уже попытались

    ckey = f"seeded_ch_{ch_mid}"
    if state.get(ckey):
        return True
    reply_to = msg.get("message_id")

    def work():
        try:
            seed = (cfg.get("seed_comment_text") or "").strip()
            if not seed:
                seed = generate_seed_comment(post_ctx)
            tg.send_message(cfg, disc, seed, reply_to=reply_to, parse_mode=None)
            st = load_state()
            st[ckey] = int(time.time())
            roots = st.setdefault("channel_discuss_root", {})
            roots[str(ch_mid)] = int(reply_to)
            save_state(st)
        except Exception as e:
            print("seed fail", str(e)[:100], flush=True)

    state[ckey] = int(time.time())  # reserve early to avoid double
    save_state(state)
    threading.Thread(target=work, daemon=True).start()
    return True


def _channel_subscribed(cfg: dict, user_id: int) -> bool:
    """member/admin/creator — подписан на канал."""
    channel = cfg.get("channel_numeric_id") or cfg.get("channel_id") or "@Vaggo01"
    try:
        m = tg.get_chat_member(cfg, channel, int(user_id))
        return (m.get("status") or "") in ("member", "administrator", "creator", "restricted")
    except Exception as e:
        print("sub check fail", e, flush=True)
        # если не смогли проверить (бот не админ?) — не блокируем жёстко
        return True


def _bot_username(cfg: dict) -> str:
    try:
        me = tg.get_me(cfg)
        return (me.get("username") or "DirectorVaggobot").lstrip("@")
    except Exception:
        return "DirectorVaggobot"


def _check_all_subs(cfg: dict, item: dict, user_id: int) -> tuple[bool, list[str]]:
    """Проверить все каналы. Возвращает (all_ok, missing_list)."""
    missing = []
    # канонические идентификаторы канала (username + numeric)
    chans = list(gw.all_required_channels(cfg, item))
    try:
        num = int(cfg.get("channel_numeric_id") or -1004445937686)
        if str(num) not in chans and f"@{cfg.get('channel_username') or 'Vaggo01'}" in chans:
            # для API иногда numeric стабильнее
            pass
    except Exception:
        num = -1004445937686

    for ch in chans:
        ok_here = False
        # пробуем username и numeric для главного канала
        candidates = [ch]
        if str(ch).lower() in ("@vaggo01", "vaggo01") or str(ch).endswith("Vaggo01"):
            candidates = [ch, num, "@Vaggo01", -1004445937686]
        seen = set()
        for cand in candidates:
            key = str(cand)
            if key in seen:
                continue
            seen.add(key)
            try:
                m = tg.get_chat_member(cfg, cand, int(user_id))
                st = (m.get("status") or "")
                if st in ("member", "administrator", "creator", "restricted"):
                    ok_here = True
                    break
            except Exception:
                continue
        if not ok_here:
            # в UI всегда показываем @Vaggo01, не id человека
            label = ch
            if str(ch).lstrip("-").isdigit():
                label = "@Vaggo01"
            missing.append(str(label))
    return (len(missing) == 0, missing)


def refresh_subs_and_enroll(
    cfg: dict,
    item: dict,
    user_id: int,
    *,
    username: str = "",
    name: str = "",
) -> tuple[dict, list[str], bool]:
    """
    Живая проверка подписки + пересчёт complete.
    В конкурс (complete) только если: подписка ОК + репост + друзья.
    Returns (entry, missing_channels, just_enrolled).
    """
    entry = gw.ensure_entry(item, user_id=user_id, username=username, name=name)
    was = bool(entry.get("complete"))
    if item.get("require_sub", True):
        ok, missing = _check_all_subs(cfg, item, user_id)
        entry = gw.set_subs_ok(item, user_id, ok)
    else:
        missing = []
        entry = gw.set_subs_ok(item, user_id, True)
    now_ok = bool(entry.get("complete"))
    just = now_ok and not was
    return entry, missing, just


def live_filter_draw_pool(cfg: dict, item: dict) -> list[dict]:
    """Перед розыгрышем: перепроверить подписку; без подписки/репоста — не в барабане."""
    # копия uid, т.к. set_subs_ok меняет entries
    uids = [int(e.get("user_id") or 0) for e in list((item.get("entries") or {}).values())]
    excl_ids = set(cfg.get("giveaway_exclude_user_ids") or []) | set(
        cfg.get("owner_user_ids") or []
    )
    excl_names = {
        n.lower().lstrip("@")
        for n in (cfg.get("giveaway_exclude_usernames") or [])
        + (cfg.get("owner_usernames") or [])
    }
    for uid in uids:
        if not uid:
            continue
        e = (item.get("entries") or {}).get(str(uid)) or {}
        un = (e.get("username") or "").lower().lstrip("@")
        if uid in excl_ids or (un and un in excl_names):
            e["complete"] = False
            e["excluded"] = True
            continue
        if item.get("require_repost", True) and not e.get("repost_ok"):
            e["complete"] = False
            e["subs_ok"] = bool(e.get("subs_ok"))
            gw._recompute_complete(item, e)
            continue
        if item.get("require_sub", True):
            ok, _ = _check_all_subs(cfg, item, uid)
            gw.set_subs_ok(item, uid, ok)
        else:
            gw.set_subs_ok(item, uid, True)
    try:
        gw.save_item(item)
    except Exception:
        pass
    fresh = gw.get_by_id(str(item.get("id"))) or item
    item.clear()
    item.update(fresh)
    return gw.eligible_for_draw(item)


def _refresh_channel_button(cfg: dict, item: dict) -> None:
    """Обновить только кнопки (без счётчиков в тексте — приватно)."""
    ch_mid = item.get("channel_message_id")
    if not ch_mid:
        return
    channel = cfg.get("channel_id") or "@Vaggo01"
    try:
        fresh = gw.get_by_id(str(item.get("id"))) or item
        # не трогаем кнопку на каждый клик — только если разметка реально меняется
        # (сейчас label статичный; refresh нужен после ended)
        if fresh.get("status") in ("ended", "cancelled"):
            tg.edit_reply_markup(
                cfg,
                channel,
                int(ch_mid),
                gw.ended_keyboard(fresh),
            )
        else:
            tg.edit_reply_markup(
                cfg,
                channel,
                int(ch_mid),
                gw.join_keyboard(fresh, bot_username=_bot_username(cfg)),
            )
    except Exception as e:
        print("gw refresh btn", e, flush=True)


def _quest_card_body(
    cfg: dict,
    item: dict,
    entry: dict,
    *,
    notice: str = "",
    tip: str = "",
) -> str:
    prize = html.escape(str(item.get("prize") or ""))
    inv_need = int(item.get("require_invites") or 0)
    bot_u = _bot_username(cfg)
    uid = entry.get("user_id")
    ref = f"https://t.me/{bot_u}?start=gwref_{item.get('id')}_{uid}"
    chans = ", ".join(gw.all_required_channels(cfg, item))
    mid = item.get("channel_message_id")
    post_link = f"https://t.me/Vaggo01/{mid}" if mid else "https://t.me/Vaggo01"
    parts: list[str] = []
    if notice:
        parts.append(notice.strip())
        parts.append("")
    parts.append(f"🎁 <b>Квест розыгрыша</b>")
    parts.append(f"Приз: <b>{prize}</b>")
    parts.append("")
    parts.append(gw.progress_bar(item, entry))
    parts.append("")
    parts.append(f"<b>1. Подписки</b> (проверяем): {html.escape(chans)}")
    if item.get("require_repost", True):
        parts.append("")
        parts.append(
            f"<b>2. Репост другу</b>\n"
            f"↗ Переслать → <b>живой человек</b> → скрин сюда.\n"
            f"Бот жёстко проверяет: не бот, не Избранное, не себе.\n"
            f"Пост: {post_link}"
        )
    if inv_need > 0:
        parts.append("")
        parts.append(
            f"<b>3. Друзья</b> ({inv_need})\n<code>{html.escape(ref)}</code>"
        )
    if tip:
        parts.append("")
        parts.append(tip.strip())
    parts.append("")
    parts.append("В конкурс — после проверки подписки и репоста. Кнопки ниже.")
    return "\n".join(parts)


def send_quest_card(
    cfg: dict,
    chat_id: int | str,
    item: dict,
    entry: dict,
    *,
    notice: str = "",
    tip: str = "",
) -> int | None:
    """
    Одна карточка квеста: edit старого сообщения или delete+send.
    Не спамит новыми сообщениями при каждом клике.
    """
    body = _quest_card_body(cfg, item, entry, notice=notice, tip=tip)
    markup = gw.quest_keyboard(item, entry)
    old_mid = entry.get("quest_msg_id")
    if old_mid:
        try:
            tg.edit_message_text(
                cfg,
                chat_id,
                int(old_mid),
                body,
                parse_mode="HTML",
                reply_markup=markup,
                disable_preview=True,
            )
            return int(old_mid)
        except Exception as e:
            err = str(e).lower()
            # Telegram: тот же текст/кнопки — не ошибка
            if "message is not modified" in err:
                return int(old_mid)
            print("gw card edit fail", str(e).split("for url:")[0][:100], flush=True)
            try:
                tg.delete_message(cfg, chat_id, int(old_mid))
            except Exception:
                pass
    res = tg.send_message(
        cfg,
        chat_id,
        body,
        parse_mode="HTML",
        reply_markup=markup,
        disable_preview=True,
    )
    new_mid = res.get("message_id")
    if new_mid:
        entry["quest_msg_id"] = int(new_mid)
        uid = entry.get("user_id")
        if uid is not None:
            e2 = (item.get("entries") or {}).get(str(int(uid)))
            if e2 is not None:
                e2["quest_msg_id"] = int(new_mid)
        try:
            gw.save_item(item)
        except Exception:
            pass
    return int(new_mid) if new_mid else None


def _delete_bot_msg(cfg: dict, chat_id: int | str, message_id: int | None) -> None:
    if not message_id:
        return
    try:
        tg.delete_message(cfg, chat_id, int(message_id))
    except Exception:
        pass


def finish_giveaway_draw(cfg: dict, item: dict, *, notify_chat: int | str | None = None) -> list:
    """Шоу-выдача: 4 поста + обновление кнопки."""
    # финальная live-проверка подписки и репоста
    pool = live_filter_draw_pool(cfg, item)
    n_ok = len(pool)
    if n_ok == 0:
        raise RuntimeError(
            "Нет участников с проверенной подпиской и репостом (и друзьями)"
        )
    winners = gw.draw_winners(item, pool=pool)
    if not winners:
        raise RuntimeError("Не удалось выбрать")
    channel = cfg.get("channel_id") or "@Vaggo01"
    ch_mid = item.get("channel_message_id")
    if ch_mid:
        try:
            tg.edit_reply_markup(cfg, channel, int(ch_mid), gw.ended_keyboard(item))
        except Exception as e:
            print("gw markup end fail", e, flush=True)

    script = gw.reveal_script(item, winners)
    last_mid = None
    for i, text in enumerate(script):
        res = tg.send_message(cfg, channel, text, parse_mode="HTML", disable_preview=True)
        last_mid = res.get("message_id")
        if i < len(script) - 1:
            time.sleep(2.2)

    # коммент под постом розыгрыша — финал
    if ch_mid and last_mid:
        try:
            w = winners[0]
            un = w.get("username")
            who = f"@{un}" if un else w.get("name")
            tg.comment_on_channel_post(
                cfg,
                int(ch_mid),
                f"🏆 Победитель: {who}! Пиши @DirectorVaggobot в личку.",
                parse_mode=None,
            )
        except Exception as e:
            print("giveaway comment fail", e, flush=True)

    names = []
    for w in winners:
        un = w.get("username")
        names.append(f"@{un}" if un else str(w.get("name") or w.get("user_id")))
    link = f"https://t.me/Vaggo01/{last_mid}" if last_mid else ""
    msg = (
        f"🏆 <b>Шоу-итоги</b>\n"
        f"Победитель: <b>{html.escape(', '.join(names))}</b>\n"
        f"Прошли квест: {n_ok}\n"
        f"{link}\n\n"
        f"Выдай приз в личку победителю (ссылку не свети в канале)."
    )
    if notify_chat:
        tg.send_message(cfg, notify_chat, msg)
    else:
        notify_owner(cfg, msg)
    # DM winner if possible
    for w in winners:
        try:
            tg.send_message(
                cfg,
                int(w["user_id"]),
                f"🏆 Поздравляю! Ты выиграл(а): <b>{html.escape(str(item.get('prize') or ''))}</b>\n\n"
                f"Напиши сюда «хочу приз» — выдадим в течение 48 часов.",
                parse_mode="HTML",
            )
        except Exception:
            pass
    print("giveaway drawn", item.get("id"), names, flush=True)
    return winners


def tick_giveaways(cfg: dict) -> None:
    """Авто-draw / продление до min_complete (по умолчанию 10)."""
    if cfg.get("paused"):
        return
    try:
        due = gw.due_auto_draw()
    except Exception as e:
        print("gw due fail", e, flush=True)
        return
    for item in due:
        try:
            n_ok = gw.entry_count(item, complete_only=True)
            need = gw.min_complete_needed(item)

            # мало людей → сдвигаем дедлайн, не тянем и не закрываем
            if need > 0 and n_ok < need:
                if gw.is_expired(item):
                    ext = gw.maybe_extend_for_min_complete(item)
                    if ext:
                        ends = ext.get("ends_at")
                        when = (
                            time.strftime("%d.%m %H:%M", time.localtime(int(ends)))
                            if ends
                            else "—"
                        )
                        notify_owner(
                            cfg,
                            f"⏳ Розыгрыш продлён (мало людей)\n"
                            f"complete <b>{n_ok}/{need}</b>\n"
                            f"новый срок: <b>{when}</b>\n"
                            f"продлений: {ext.get('extend_count')}\n"
                            f"id <code>{html.escape(str(ext.get('id')))}</code>",
                        )
                        print(
                            "gw extend",
                            item.get("id"),
                            f"{n_ok}/{need}",
                            "until",
                            when,
                            flush=True,
                        )
                    else:
                        # лимит продлений — не закрываем с 0, просто лог
                        print(
                            "gw extend skip/limit",
                            item.get("id"),
                            n_ok,
                            need,
                            flush=True,
                        )
                # ещё не expired, но already in due из-за need? only when n_ok>=need
                continue

            if n_ok == 0:
                # без min_complete и никого — можно закрыть
                if need > 0:
                    continue
                gw.end(item)
                ch_mid = item.get("channel_message_id")
                channel = cfg.get("channel_id") or "@Vaggo01"
                if ch_mid:
                    try:
                        tg.edit_reply_markup(
                            cfg, channel, int(ch_mid), gw.ended_keyboard(item)
                        )
                    except Exception:
                        pass
                tg.send_message(
                    cfg,
                    channel,
                    f"⏹ Розыгрыш <b>{html.escape(str(item.get('prize') or ''))}</b> завершён.\n"
                    f"Никто не закрыл все шаги квеста — победителя нет.\n"
                    f"В следующий раз будет жарче 🔥",
                    parse_mode="HTML",
                )
                notify_owner(
                    cfg,
                    f"⏹ Розыгрыш <code>{html.escape(str(item.get('id')))}</code> "
                    f"истёк, complete=0.",
                )
                continue
            finish_giveaway_draw(cfg, item)
        except Exception as e:
            print("auto draw fail", item.get("id"), e, flush=True)


def ui_try_delete(cfg: dict, chat_id: int | str, message_id: int | None) -> bool:
    return bool(tg.try_delete_message(cfg, chat_id, message_id))


def ui_delete_user_message(cfg: dict, msg: dict | None) -> None:
    """Убрать сообщение пользователя в личке (чтобы опрос не расползался)."""
    if not msg:
        return
    chat = msg.get("chat") or {}
    if chat.get("type") != "private":
        return
    mid = msg.get("message_id")
    cid = chat.get("id")
    if mid and cid is not None:
        ui_try_delete(cfg, cid, mid)


def _priv_track(
    cfg: dict,
    chat_id: int | str,
    uid: int,
    message_id: int,
    *,
    keep: int = 2,
) -> None:
    """
    Запомнить mid бота в ЛС и удалить всё старее keep.
    Держим 1–2 окна (меню + notify), остальное — спам.
    """
    try:
        st = load_state()
        bag = st.setdefault("priv_bot_msgs", {})
        lst = list(bag.get(str(uid)) or [])
        mid = int(message_id)
        if mid not in lst:
            lst.append(mid)
        # unique keep order
        seen: set[int] = set()
        uniq: list[int] = []
        for x in lst:
            try:
                xi = int(x)
            except Exception:
                continue
            if xi in seen:
                continue
            seen.add(xi)
            uniq.append(xi)
        while len(uniq) > max(1, keep):
            old = uniq.pop(0)
            if old != mid:
                ui_try_delete(cfg, chat_id, old)
        bag[str(uid)] = uniq[-keep:]
        save_state(st)
    except Exception as e:
        print("priv_track fail", e, flush=True)


def ui_purge_recent_bot_msgs(
    cfg: dict,
    chat_id: int | str,
    *,
    around_mid: int,
    lookback: int = 120,
    keep_mids: set[int] | None = None,
) -> int:
    """
    Агрессивно: снести mid-1 … mid-lookback (только сообщения бота удалятся).
    Telegram не даёт историю чата — идём назад по id.
    """
    keep = keep_mids or set()
    n = 0
    base = int(around_mid)
    for i in range(1, max(1, lookback) + 1):
        mid = base - i
        if mid <= 0 or mid in keep:
            continue
        if ui_try_delete(cfg, chat_id, mid):
            n += 1
    return n


def ui_clean_private(
    cfg: dict,
    chat_id: int | str,
    uid: int,
    *,
    keep_mids: list[int] | None = None,
    deep: bool = True,
) -> int:
    """Удалить tracked + (deep) последние ~120 mid бота. keep_mids сохраняем."""
    keep = {int(x) for x in (keep_mids or []) if x}
    n = 0
    try:
        st = load_state()
        bag = st.setdefault("priv_bot_msgs", {})
        lst = list(bag.get(str(uid)) or [])
        for sk in (
            "owner_ui_msg",
            "owner_notify_msg",
            "order_ui_msg",
            "terms_ui_msg",
            "bal_ui_msg",
            "sup_ui_msg",
        ):
            v = (st.get(sk) or {}).get(str(uid))
            if v:
                try:
                    lst.append(int(v))
                except Exception:
                    pass
        left: list[int] = []
        max_mid = max(keep) if keep else 0
        for x in lst:
            try:
                xi = int(x)
            except Exception:
                continue
            max_mid = max(max_mid, xi)
            if xi in keep:
                left.append(xi)
                continue
            if ui_try_delete(cfg, chat_id, xi):
                n += 1
        if deep and max_mid > 0:
            n += ui_purge_recent_bot_msgs(
                cfg, chat_id, around_mid=max_mid, lookback=120, keep_mids=keep
            )
        bag[str(uid)] = list(dict.fromkeys(left))[-3:]
        for sk in (
            "owner_ui_msg",
            "owner_notify_msg",
            "order_ui_msg",
            "terms_ui_msg",
            "bal_ui_msg",
            "sup_ui_msg",
        ):
            d = st.setdefault(sk, {})
            old = d.get(str(uid))
            if old and int(old) not in keep:
                d.pop(str(uid), None)
        # keep store for main UI
        if keep:
            main = max(keep)
            st.setdefault("owner_ui_msg", {})[str(uid)] = main
            bag[str(uid)] = [main]
        save_state(st)
    except Exception as e:
        print("ui_clean_private fail", e, flush=True)
    return n


def _strip_html(s: str) -> str:
    plain = s or ""
    for tag in (
        "<b>",
        "</b>",
        "<i>",
        "</i>",
        "<code>",
        "</code>",
        "<u>",
        "</u>",
        "<pre>",
        "</pre>",
    ):
        plain = plain.replace(tag, "")
    return plain


# Все «панели» в личке = ОДНО сообщение
_MAIN_UI_KEYS = frozenset(
    {
        "owner_ui_msg",
        "owner_notify_msg",
        "order_ui_msg",
        "terms_ui_msg",
        "bal_ui_msg",
        "sup_ui_msg",
        "main_ui_msg",
    }
)


def ui_edit_or_send(
    cfg: dict,
    chat_id: int | str,
    text: str,
    *,
    reply_markup: dict | None = None,
    message_id: int | None = None,
    state: dict | None = None,
    uid: int | None = None,
    store_key: str = "order_ui_msg",
    delete_extra: list[int] | None = None,
    force_new: bool = False,
) -> int | None:
    """
    Умный UI (шедевр без потери окна):

    • обычный режим — edit живого сообщения (без спама);
    • force_new / мёртвый mid — новое сообщение + pin;
    • старое окно тихо удаляем, если можем (чат не зарастает);
    • никогда не молчим: edit fail → send.
    """
    _ = delete_extra
    _ = store_key  # все ключи пишем в main_ui_msg

    mid_click = None
    try:
        if message_id is not None:
            mid_click = int(message_id)
    except Exception:
        mid_click = None

    stored = None
    if state is not None and uid is not None:
        try:
            s = (state.get("main_ui_msg") or {}).get(str(uid))
            if s is not None:
                stored = int(s)
        except Exception:
            stored = None

    candidates: list[int] = []
    if not force_new:
        for m in (mid_click, stored):
            if m and m not in candidates:
                candidates.append(m)

    text_use = (text or "")[:4090]
    old_mids = [m for m in (mid_click, stored) if m]

    def _store(m: int, *, do_pin: bool) -> None:
        if state is None or uid is None:
            return
        uid_s = str(uid)
        state.setdefault("main_ui_msg", {})[uid_s] = int(m)
        for sk in _MAIN_UI_KEYS:
            state.setdefault(sk, {})[uid_s] = int(m)
        if do_pin:
            prev = (state.get("pinned_ui_msg") or {}).get(uid_s)
            if not prev or int(prev) != int(m):
                if tg.pin_chat_message(cfg, chat_id, int(m), silent=True):
                    state.setdefault("pinned_ui_msg", {})[uid_s] = int(m)
        try:
            save_state(state)
        except Exception:
            pass

    def _try_edit(m: int, body: str, parse_mode: str | None) -> bool:
        try:
            tg.edit_message_text(
                cfg,
                chat_id,
                int(m),
                body,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                disable_preview=True,
            )
            return True
        except Exception as e:
            err = str(e).lower()
            if "message is not modified" in err:
                return True
            print("ui edit fail", m, str(e)[:120], flush=True)
            return False

    def _try_delete(m: int) -> None:
        try:
            tg.api(
                cfg,
                "deleteMessage",
                data={"chat_id": chat_id, "message_id": int(m)},
            )
        except Exception:
            pass

    # 1) edit, если не force_new
    for mid in candidates:
        if _try_edit(mid, text_use, "HTML"):
            _store(mid, do_pin=False)
            return mid
        if _try_edit(mid, _strip_html(text_use)[:4090], None):
            _store(mid, do_pin=False)
            return mid

    # 2) recovery / force_new — новое окно
    if state is not None and uid is not None:
        try:
            uid_s = str(uid)
            state.setdefault("main_ui_msg", {}).pop(uid_s, None)
            state.setdefault("pinned_ui_msg", {}).pop(uid_s, None)
            for sk in _MAIN_UI_KEYS:
                state.setdefault(sk, {}).pop(uid_s, None)
            save_state(state)
        except Exception:
            pass

    try:
        res = tg.send_message(
            cfg,
            chat_id,
            text_use,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_preview=True,
        )
    except Exception:
        res = tg.send_message(
            cfg,
            chat_id,
            _strip_html(text_use)[:4000],
            parse_mode=None,
            reply_markup=reply_markup,
            disable_preview=True,
        )
    new_mid = None
    if isinstance(res, dict):
        new_mid = res.get("message_id") or (res.get("result") or {}).get("message_id")
    if new_mid:
        new_mid = int(new_mid)
        _store(new_mid, do_pin=True)
        # убрать старые панели, чтобы не терялись в куче
        for om in old_mids:
            if om and int(om) != new_mid:
                _try_delete(int(om))
        print(
            f"ui send new mid={new_mid} force_new={force_new} chat={chat_id}",
            flush=True,
        )
        return new_mid
    return None


def send_terms_gate(
    cfg: dict,
    chat_id: int | str,
    *,
    state: dict | None = None,
    uid: int | None = None,
    message_id: int | None = None,
    full: bool = False,
) -> None:
    text = terms.terms_full_html() if full else terms.terms_short_html()
    # Telegram 4096 limit — full may be long
    if len(text) > 4000:
        text = text[:3990] + "…"
    kb = terms.full_keyboard(
        accepted=bool(uid and terms.is_accepted(uid))
    ) if full else terms.gate_keyboard()
    ui_edit_or_send(
        cfg,
        chat_id,
        text,
        reply_markup=kb,
        message_id=message_id,
        state=state,
        uid=uid,
        store_key="terms_ui_msg",
    )


def handle_terms_private(cfg: dict, state: dict, msg: dict) -> bool:
    """Показ/повтор условий. Не требует принятия."""
    chat = msg.get("chat") or {}
    if chat.get("type") != "private":
        return False
    user = msg.get("from") or {}
    uid = int(user.get("id") or 0)
    if not uid:
        return False
    text = (msg.get("text") or "").strip()
    lower = text.lower()
    cmd = lower.split()[0].split("@")[0] if lower.startswith("/") else ""
    chat_id = chat.get("id")

    if cmd in ("/ref", "/реф", "/invite", "/реферал"):
        try:
            import growth_lib as growth

            code = growth.ref_code_for_user(uid)
            bot_un = "DirectorVaggobot"
            try:
                me = tg.get_me(cfg)
                if me.get("username"):
                    bot_un = me["username"]
            except Exception:
                pass
            link = f"https://t.me/{bot_un}?start={code}"
            tg.send_message(
                cfg,
                chat_id,
                f"🔗 <b>Реферальная ссылка</b>\n\n"
                f"<code>{html.escape(link)}</code>\n\n"
                f"Друг жмёт Start → его первый оплаченный заказ → "
                f"тебе <b>+{growth.REF_BONUS_RUB} ₽</b>.\n/balance",
                parse_mode="HTML",
                disable_preview=True,
            )
        except Exception as e:
            tg.send_message(cfg, chat_id, f"❌ {html.escape(str(e)[:150])}")
        return True

    if cmd in (
        "/terms",
        "/rules",
        "/policy",
        "/правила",
        "/политика",
        "/условия",
        "/оферта",
    ):
        try:
            send_terms_gate(cfg, chat_id, state=state, uid=uid, full=True)
        except Exception as e:
            print("terms send fail", e, flush=True)
            try:
                tg.send_message(
                    cfg,
                    chat_id,
                    terms.terms_short_html(),
                    parse_mode="HTML",
                    reply_markup=terms.gate_keyboard(),
                    disable_preview=True,
                )
            except Exception as e2:
                print("terms fallback fail", e2, flush=True)
        return True

    if cmd in ("/privacy", "/конфиденциальность"):
        tg.send_message(
            cfg,
            chat_id,
            "🔒 <b>Политика конфиденциальности</b>\n"
            "Director Vaggo · для Platega и клиентов\n\n"
            f'<a href="{terms.PRIVACY_URL}">Открыть документ</a>\n'
            f"<code>{terms.PRIVACY_URL}</code>",
            parse_mode="HTML",
            reply_markup=terms.legal_menu_keyboard(),
            disable_preview=False,
        )
        return True

    if cmd in (
        "/agreement",
        "/offer",
        "/соглашение",
        "/оферта_док",
        "/public_offer",
        "/публичная_оферта",
    ):
        tg.send_message(
            cfg,
            chat_id,
            "📜 <b>Пользовательское соглашение / оферта</b>\n"
            "Director Vaggo · для Platega и клиентов\n\n"
            f'<a href="{terms.AGREEMENT_URL}">Открыть документ</a>\n'
            f"<code>{terms.AGREEMENT_URL}</code>",
            parse_mode="HTML",
            reply_markup=terms.legal_menu_keyboard(),
            disable_preview=False,
        )
        return True

    if cmd in ("/prices", "/pricing", "/tariffs", "/тарифы", "/цены"):
        # владелец — редактор; клиент — публичный прайс
        if is_owner(cfg, user):
            tg.send_message(
                cfg,
                chat_id,
                orders.pricing_admin_html()
                + "\n\n<i>Клиентам: тот же /prices без кнопок правок.</i>",
                parse_mode="HTML",
                reply_markup=orders.pricing_admin_keyboard(),
                disable_preview=True,
            )
        else:
            tg.send_message(
                cfg,
                chat_id,
                terms.prices_html(cfg),
                parse_mode="HTML",
                reply_markup=terms.legal_menu_keyboard(),
                disable_preview=True,
            )
        return True

    if cmd in ("/setprice", "/setprices", "/прайс_ред"):
        if not is_owner(cfg, user):
            return True
        # /setprice bot 400
        parts = (arg or "").split()
        if len(parts) >= 2:
            kind, p_s = parts[0].lower(), parts[1]
            try:
                orders.set_base_price(kind, int(float(p_s.replace("₽", ""))))
                tg.send_message(
                    cfg,
                    chat_id,
                    f"✅ <code>{html.escape(kind)}</code> = <b>{int(float(p_s.replace('₽','')))}</b> ₽\n\n"
                    + orders.pricing_admin_html(),
                    parse_mode="HTML",
                    reply_markup=orders.pricing_admin_keyboard(),
                )
            except Exception as e:
                tg.send_message(
                    cfg, chat_id, f"⚠️ {html.escape(str(e)[:200])}\n"
                    f"Пример: <code>/setprice bot 400</code>",
                    parse_mode="HTML",
                )
            return True
        tg.send_message(
            cfg,
            chat_id,
            orders.pricing_admin_html(),
            parse_mode="HTML",
            reply_markup=orders.pricing_admin_keyboard(),
        )
        return True

    if cmd in ("/discount", "/скидка"):
        if not is_owner(cfg, user):
            return True
        parts = (arg or "").split()
        try:
            if not parts:
                tg.send_message(
                    cfg,
                    chat_id,
                    "Примеры:\n"
                    "<code>/discount 10</code> — всем −10%\n"
                    "<code>/discount bot 20</code> — только Telegram-бот −20%\n"
                    "<code>/discount 0</code> — снять глобальную",
                    parse_mode="HTML",
                    reply_markup=orders.pricing_admin_keyboard(),
                )
                return True
            if len(parts) == 1:
                orders.set_global_discount(int(float(parts[0].replace("%", ""))))
            else:
                kind = parts[0].lower()
                if kind in orders.ORDER_TYPES:
                    orders.set_kind_discount(
                        kind, int(float(parts[1].replace("%", "")))
                    )
                else:
                    orders.set_global_discount(int(float(parts[0].replace("%", ""))))
            tg.send_message(
                cfg,
                chat_id,
                "✅ Скидка обновлена\n\n" + orders.pricing_admin_html(),
                parse_mode="HTML",
                reply_markup=orders.pricing_admin_keyboard(),
            )
        except Exception as e:
            tg.send_message(
                cfg, chat_id, f"⚠️ {html.escape(str(e)[:200])}", parse_mode="HTML"
            )
        return True

    if cmd in ("/support", "/help_support", "/поддержка", "/контакт"):
        open_t = support.open_ticket_for_user(uid)
        tg.send_message(
            cfg,
            chat_id,
            support.support_home_html(),
            parse_mode="HTML",
            reply_markup=support.support_keyboard(has_open=bool(open_t)),
            disable_preview=True,
        )
        return True

    if cmd in ("/tickets", "/mytickets", "/тикеты", "/обращения"):
        if is_owner(cfg, user):
            tg.send_message(
                cfg,
                chat_id,
                support.staff_list_html(),
                parse_mode="HTML",
                reply_markup={
                    "inline_keyboard": [
                        [{"text": "📋 Обновить", "callback_data": "sup:stafflist"}]
                    ]
                },
            )
        else:
            tg.send_message(
                cfg,
                chat_id,
                support.user_ticket_list_html(uid),
                parse_mode="HTML",
                reply_markup=support.support_keyboard(
                    has_open=bool(support.open_ticket_for_user(uid))
                ),
            )
        return True

    if cmd in ("/legal", "/docs", "/документы"):
        ui_edit_or_send(
            cfg,
            chat_id,
            terms.legal_hub_html(cfg),
            reply_markup=terms.legal_menu_keyboard(),
            state=state,
            uid=uid,
            store_key="terms_ui_msg",
        )
        return True

    if cmd in ("/menu", "/меню") and not is_owner(cfg, user):
        ui_edit_or_send(
            cfg,
            chat_id,
            terms.user_home_html(uid),
            reply_markup=terms.after_accept_keyboard(uid),
            state=state,
            uid=uid,
            store_key="terms_ui_msg",
            force_new=True,
        )
        return True

    # /start пользователя: свежее меню, не теряется
    if cmd == "/start" and not is_owner(cfg, user):
        arg = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
        # рефералка ref_USERID
        if arg.startswith("ref_"):
            try:
                import growth_lib as growth

                inv = growth.parse_ref_start(arg)
                if growth.apply_referral_on_start(state, uid, inv):
                    save_state(state)
            except Exception as e:
                print("ref start", e, flush=True)
        if not terms.is_accepted(uid):
            send_terms_gate(cfg, chat_id, state=state, uid=uid, full=False)
            if arg and not arg.startswith("ref_"):
                state.setdefault("pending_start_arg", {})[str(uid)] = arg[:80]
                save_state(state)
            return True
        # deep-link розыгрыша — отдаём giveaway_private
        if arg and (arg.startswith("gw_") or arg.startswith("gwref_")):
            return False
        if arg.startswith("ref_"):
            arg = ""  # уже учли
        if arg:
            return False
        ui_edit_or_send(
            cfg,
            chat_id,
            terms.user_home_html(uid),
            reply_markup=terms.after_accept_keyboard(uid),
            state=state,
            uid=uid,
            store_key="terms_ui_msg",
            force_new=True,
        )
        return True
    return False


def _safe_answer_cq(cfg: dict, cq_id: str, text: str = "ok") -> None:
    try:
        tg.answer_callback(cfg, cq_id, text)
    except Exception as e:
        print("answer_cq fail", str(e)[:80], flush=True)


def handle_legal_callback(cfg: dict, state: dict, cq: dict) -> bool:
    """Документы/тарифы: edit того же сообщения (без спама)."""
    data = cq.get("data") or ""
    if not data.startswith("legal:"):
        return False
    action = data.split(":", 1)[1] if ":" in data else ""
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    mid = msg.get("message_id")
    user = cq.get("from") or {}
    uid = int(user.get("id") or 0)

    if action == "prices":
        _safe_answer_cq(cfg, cq["id"], "Прайс")
        if chat_id:
            ui_edit_or_send(
                cfg,
                chat_id,
                terms.prices_html(cfg),
                reply_markup=terms.legal_menu_keyboard(),
                message_id=mid,
                state=state,
                uid=uid or None,
                store_key="terms_ui_msg",
            )
        return True

    if action == "support":
        _safe_answer_cq(cfg, cq["id"], "Поддержка")
        if chat_id:
            open_t = support.open_ticket_for_user(uid) if uid else None
            ui_edit_or_send(
                cfg,
                chat_id,
                support.support_home_html(),
                reply_markup=support.support_keyboard(has_open=bool(open_t)),
                message_id=mid,
                state=state,
                uid=uid or None,
                store_key="terms_ui_msg",
            )
        return True

    if action == "hub":
        _safe_answer_cq(cfg, cq["id"], "Документы")
        if chat_id:
            ui_edit_or_send(
                cfg,
                chat_id,
                terms.legal_hub_html(cfg),
                reply_markup=terms.legal_menu_keyboard(),
                message_id=mid,
                state=state,
                uid=uid or None,
                store_key="terms_ui_msg",
            )
        return True

    _safe_answer_cq(cfg, cq["id"], "ok")
    return True


def _support_set_await(state: dict, uid: int, payload: dict | None) -> None:
    aw = state.setdefault("support_await", {})
    if payload is None:
        aw.pop(str(uid), None)
    else:
        aw[str(uid)] = payload
    save_state(state)


def handle_support_callback(cfg: dict, state: dict, cq: dict) -> bool:
    data = cq.get("data") or ""
    if not data.startswith("sup:"):
        return False
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else ""
    user = cq.get("from") or {}
    uid = int(user.get("id") or 0)
    uname = user.get("username") or ""
    name = user.get("first_name") or uname or str(uid)
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    mid = msg.get("message_id")
    owner = is_owner(cfg, user)

    def _edit(text: str, kb: dict | None = None) -> None:
        if not chat_id:
            return
        ui_edit_or_send(
            cfg,
            chat_id,
            text,
            reply_markup=kb,
            message_id=mid,
            state=state,
            uid=uid or None,
            store_key="terms_ui_msg",
        )

    if action == "home":
        tg.answer_callback(cfg, cq["id"], "Поддержка")
        open_t = support.open_ticket_for_user(uid) if uid else None
        _edit(support.support_home_html(), support.support_keyboard(has_open=bool(open_t)))
        return True

    if action == "mine":
        tg.answer_callback(cfg, cq["id"], "Мои")
        _edit(
            support.user_ticket_list_html(uid),
            support.support_keyboard(
                has_open=bool(support.open_ticket_for_user(uid))
            ),
        )
        return True

    if action == "new":
        tg.answer_callback(cfg, cq["id"], "Новый тикет")
        _support_set_await(
            state, uid, {"mode": "new", "ts": int(time.time())}
        )
        _edit(
            "✉️ <b>Новый тикет</b>\n\n"
            "Напиши одним сообщением: что случилось, номер заказа "
            "(если есть), как с тобой связаться.\n\n"
            "Отмена: /cancel",
            {
                "inline_keyboard": [
                    [{"text": "❌ Отмена", "callback_data": "sup:home"}]
                ]
            },
        )
        return True

    if action == "continue":
        open_t = support.open_ticket_for_user(uid)
        if not open_t:
            tg.answer_callback(cfg, cq["id"], "Нет открытого", show_alert=True)
            return True
        tid = str(open_t.get("id"))
        tg.answer_callback(cfg, cq["id"], "Пиши")
        _support_set_await(
            state, uid, {"mode": "write", "ticket_id": tid, "ts": int(time.time())}
        )
        _edit(
            f"✍️ Дописываем в тикет <code>{tid}</code>\n\n"
            "Следующее сообщение уйдёт в обращение.\n/cancel — отмена",
            support.ticket_user_keyboard(tid),
        )
        return True

    if action == "write" and arg:
        it = support.get_ticket(arg)
        if not it or int(it.get("user_id") or 0) != uid:
            tg.answer_callback(cfg, cq["id"], "Не найден", show_alert=True)
            return True
        if it.get("status") == "closed":
            tg.answer_callback(cfg, cq["id"], "Закрыт", show_alert=True)
            return True
        tg.answer_callback(cfg, cq["id"], "Пиши")
        _support_set_await(
            state, uid, {"mode": "write", "ticket_id": arg, "ts": int(time.time())}
        )
        _edit(
            f"✍️ Тикет <code>{arg}</code> — жду сообщение.\n/cancel — отмена",
            support.ticket_user_keyboard(arg),
        )
        return True

    if action == "uclose" and arg:
        it = support.get_ticket(arg)
        if not it or int(it.get("user_id") or 0) != uid:
            tg.answer_callback(cfg, cq["id"], "Не найден", show_alert=True)
            return True
        support.close_ticket(arg)
        _support_set_await(state, uid, None)
        tg.answer_callback(cfg, cq["id"], "Закрыт")
        _edit(
            f"✅ Тикет <code>{arg}</code> закрыт.\nСпасибо!",
            support.support_keyboard(has_open=False),
        )
        notify_owner(
            cfg,
            f"⚫ Клиент закрыл тикет <code>{arg}</code> (@{uname})",
            reply_markup=support.ticket_staff_keyboard(arg),
        )
        return True

    # --- staff ---
    if action == "reply" and arg and owner:
        it = support.get_ticket(arg)
        if not it:
            tg.answer_callback(cfg, cq["id"], "Нет", show_alert=True)
            return True
        tg.answer_callback(cfg, cq["id"], "Ответ")
        _support_set_await(
            state, uid, {"mode": "staff_reply", "ticket_id": arg, "ts": int(time.time())}
        )
        tg.send_message(
            cfg,
            chat_id,
            f"💬 Ответ в тикет <code>{arg}</code>\n"
            f"Клиент: @{it.get('username') or it.get('user_id')}\n\n"
            "Напиши текст ответа одним сообщением.\n/cancel — отмена",
            parse_mode="HTML",
        )
        return True

    if action == "close" and arg and owner:
        it = support.close_ticket(arg)
        if not it:
            tg.answer_callback(cfg, cq["id"], "Нет", show_alert=True)
            return True
        tg.answer_callback(cfg, cq["id"], "Закрыт")
        try:
            tg.send_message(
                cfg,
                int(it["user_id"]),
                f"✅ Тикет <code>{arg}</code> закрыт поддержкой.\n"
                "Новый вопрос — /support",
                parse_mode="HTML",
                reply_markup=support.support_keyboard(has_open=False),
            )
        except Exception as e:
            print("ticket close dm", e)
        tg.send_message(
            cfg,
            chat_id,
            f"⚫ Тикет <code>{arg}</code> закрыт.",
            parse_mode="HTML",
        )
        return True

    if action == "stafflist" and owner:
        tg.answer_callback(cfg, cq["id"], "Список")
        tg.send_message(
            cfg,
            chat_id,
            support.staff_list_html(),
            parse_mode="HTML",
            reply_markup={
                "inline_keyboard": [
                    [{"text": "📋 Обновить", "callback_data": "sup:stafflist"}]
                ]
            },
        )
        return True

    tg.answer_callback(cfg, cq["id"], "ok")
    return True


def handle_support_private(cfg: dict, state: dict, msg: dict) -> bool:
    """
    Ожидание текста тикета / ответ staff / авто-допись в открытый тикет.
    True = сообщение съели.
    """
    chat = msg.get("chat") or {}
    if chat.get("type") != "private":
        return False
    user = msg.get("from") or {}
    uid = int(user.get("id") or 0)
    if not uid:
        return False
    text = (msg.get("text") or "").strip()
    chat_id = chat.get("id")
    uname = user.get("username") or ""
    name = user.get("first_name") or uname or str(uid)
    lower = text.lower()
    cmd = lower.split()[0].split("@")[0] if lower.startswith("/") else ""

    if cmd in ("/cancel", "/отмена"):
        if str(uid) in (state.get("support_await") or {}):
            _support_set_await(state, uid, None)
            tg.send_message(
                cfg,
                chat_id,
                "Ок, отменил.",
                reply_markup=support.support_keyboard(
                    has_open=bool(support.open_ticket_for_user(uid))
                ),
            )
            return True
        return False

    # owner: /treply CODE text
    if is_owner(cfg, user) and cmd == "/treply":
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            tg.send_message(cfg, chat_id, "Формат: /treply КОД текст ответа")
            return True
        tid, body = parts[1], parts[2]
        it = support.add_message(tid, from_role="staff", text=body)
        if not it:
            tg.send_message(cfg, chat_id, "Тикет не найден или закрыт")
            return True
        try:
            tg.send_message(
                cfg,
                int(it["user_id"]),
                f"💬 <b>Ответ поддержки</b> · тикет <code>{tid}</code>\n\n"
                f"{html.escape(body)}\n\n"
                "Можешь дописать в этот тикет — просто напиши сюда.",
                parse_mode="HTML",
                reply_markup=support.ticket_user_keyboard(tid),
            )
        except Exception as e:
            tg.send_message(cfg, chat_id, f"Клиенту не ушло: {e}")
            return True
        tg.send_message(cfg, chat_id, f"✅ Ответ в <code>{tid}</code> отправлен")
        return True

    aw = (state.get("support_await") or {}).get(str(uid))

    # staff reply await
    if aw and aw.get("mode") == "staff_reply" and is_owner(cfg, user):
        if not text or text.startswith("/"):
            return False
        tid = str(aw.get("ticket_id") or "")
        it = support.add_message(tid, from_role="staff", text=text)
        _support_set_await(state, uid, None)
        if not it:
            tg.send_message(cfg, chat_id, "Не удалось (закрыт?)")
            return True
        try:
            tg.send_message(
                cfg,
                int(it["user_id"]),
                f"💬 <b>Ответ поддержки</b> · <code>{tid}</code>\n\n"
                f"{html.escape(text)}\n\n"
                "Дописать — просто напиши сообщение.",
                parse_mode="HTML",
                reply_markup=support.ticket_user_keyboard(tid),
            )
        except Exception as e:
            tg.send_message(cfg, chat_id, f"DM fail: {e}")
            return True
        tg.send_message(
            cfg,
            chat_id,
            f"✅ Ушло в тикет <code>{tid}</code>",
            parse_mode="HTML",
            reply_markup=support.ticket_staff_keyboard(tid),
        )
        return True

    # new ticket / write to ticket
    if aw and aw.get("mode") in ("new", "write"):
        if not text or text.startswith("/"):
            if not text:
                # photo caption?
                text = (msg.get("caption") or "").strip()
            if not text:
                tg.send_message(cfg, chat_id, "Нужен текст. Или /cancel")
                return True
            if text.startswith("/"):
                return False
        if aw.get("mode") == "new":
            it = support.create_ticket(
                uid, text, username=uname, name=name
            )
            _support_set_await(state, uid, None)
            tid = it["id"]
            tg.send_message(
                cfg,
                chat_id,
                f"✅ <b>Тикет создан</b> <code>{tid}</code>\n\n"
                f"Мы ответим сюда. Пока открыт — можно просто писать дальше.\n\n"
                f"<i>{html.escape(text[:200])}</i>",
                parse_mode="HTML",
                reply_markup=support.ticket_user_keyboard(tid),
            )
            notify_owner(
                cfg,
                f"🆘 <b>Новый тикет</b> <code>{tid}</code>\n"
                f"От: {html.escape(name)} (@{html.escape(uname)}) "
                f"<code>{uid}</code>\n\n"
                f"{html.escape(text[:1500])}",
                reply_markup=support.ticket_staff_keyboard(tid),
            )
            return True
        # write
        tid = str(aw.get("ticket_id") or "")
        it = support.add_message(tid, from_role="user", text=text)
        _support_set_await(state, uid, None)
        if not it:
            tg.send_message(cfg, chat_id, "Тикет закрыт. /support — новый.")
            return True
        tg.send_message(
            cfg,
            chat_id,
            f"✅ Добавлено в <code>{tid}</code>",
            parse_mode="HTML",
            reply_markup=support.ticket_user_keyboard(tid),
        )
        notify_owner(
            cfg,
            f"💬 Тикет <code>{tid}</code> · допись от @{html.escape(uname)}\n\n"
            f"{html.escape(text[:1500])}",
            reply_markup=support.ticket_staff_keyboard(tid),
        )
        return True

    # auto: open ticket + plain text (not command) → append
    if (
        text
        and not text.startswith("/")
        and not is_owner(cfg, user)
        and terms.is_accepted(uid)
    ):
        # не перехватывать если идёт заказ/баланс await
        if (state.get("order_draft") or {}).get(str(uid)):
            return False
        if (state.get("balance_await") or {}).get(str(uid)):
            return False
        open_t = support.open_ticket_for_user(uid)
        if open_t:
            tid = str(open_t.get("id"))
            it = support.add_message(tid, from_role="user", text=text)
            if it:
                tg.send_message(
                    cfg,
                    chat_id,
                    f"✅ В тикет <code>{tid}</code>\n"
                    "Если это новый вопрос — /support → «Новый тикет»",
                    parse_mode="HTML",
                    reply_markup=support.ticket_user_keyboard(tid),
                )
                notify_owner(
                    cfg,
                    f"💬 Тикет <code>{tid}</code> · @{html.escape(uname)}\n\n"
                    f"{html.escape(text[:1500])}",
                    reply_markup=support.ticket_staff_keyboard(tid),
                )
                return True
    return False


def handle_terms_callback(cfg: dict, state: dict, cq: dict) -> bool:
    data = cq.get("data") or ""
    if not data.startswith("terms:"):
        return False
    user = cq.get("from") or {}
    uid = int(user.get("id") or 0)
    uname = user.get("username") or ""
    name = user.get("first_name") or uname or str(uid)
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    mid = msg.get("message_id")
    action = data.split(":", 1)[1] if ":" in data else ""

    if action == "full":
        # один экран, без спама 4 сообщениями
        _safe_answer_cq(cfg, cq["id"], "Полный текст")
        if chat_id:
            body = (
                "📜 <b>Полные условия</b>\n\n"
                "Читай по ссылкам (всегда доступны):\n"
                f'• <a href="{terms.PRIVACY_URL}">Политика конфиденциальности</a>\n'
                f'• <a href="{terms.AGREEMENT_URL}">Пользовательское соглашение</a>\n'
                "• Прайс — кнопка ниже\n\n"
                f"Кратко в боте: гарантия {terms.GUARANTEE_DAYS} сут., "
                f"правки {terms.REWORK_DAYS} сут., free нет, хостинг не входит.\n"
                f"<i>{terms.TERMS_VERSION}</i>"
            )
            ui_edit_or_send(
                cfg,
                chat_id,
                body,
                reply_markup=terms.full_keyboard(accepted=terms.is_accepted(uid)),
                message_id=mid,
                state=state,
                uid=uid or None,
                store_key="terms_ui_msg",
            )
        return True

    if action == "short":
        tg.answer_callback(cfg, cq["id"], "Кратко")
        if chat_id:
            send_terms_gate(
                cfg, chat_id, state=state, uid=uid, message_id=mid, full=False
            )
        return True

    if action == "ok":
        tg.answer_callback(cfg, cq["id"], "Уже принято")
        return True

    if action == "yes":
        terms.accept(uid, username=uname, name=name)
        tg.answer_callback(cfg, cq["id"], "Принято!")
        if chat_id:
            ui_edit_or_send(
                cfg,
                chat_id,
                f"✅ Условия приняты · гарантия {terms.GUARANTEE_DAYS} сут.\n\n"
                + terms.user_home_html(uid),
                reply_markup=terms.after_accept_keyboard(uid),
                message_id=mid,
                state=state,
                uid=uid,
                store_key="terms_ui_msg",
            )
            # pending deep-link (розыгрыш) — сразу в квест, без «иди жми ещё раз»
            pend = (state.get("pending_start_arg") or {}).pop(str(uid), None)
            if pend:
                save_state(state)
                pend_s = str(pend)
                if pend_s.startswith("gw_") or pend_s.startswith("gwref_"):
                    try:
                        fake = {
                            "chat": {"id": chat_id, "type": "private"},
                            "from": user,
                            "text": f"/start {pend_s}",
                            "message_id": mid,
                        }
                        handle_giveaway_private(cfg, state, fake)
                    except Exception as e:
                        print("gw after terms", e, flush=True)
                        tg.send_message(
                            cfg,
                            chat_id,
                            "Условия приняты. Ещё раз нажми «Участвовать» в посте розыгрыша.",
                            parse_mode=None,
                        )
                else:
                    tg.send_message(
                        cfg,
                        chat_id,
                        "Можно продолжать: /start · /order · /support",
                        parse_mode=None,
                    )
        return True

    if action == "no":
        terms.decline(uid, username=uname)
        tg.answer_callback(cfg, cq["id"], "Ок")
        if chat_id:
            ui_edit_or_send(
                cfg,
                chat_id,
                "❌ <b>Без принятия условий бот недоступен</b>\n\n"
                "Заказы, баланс и сервисы закрыты.\n"
                "Если передумаешь — /terms или /start.",
                reply_markup={
                    "inline_keyboard": [
                        [{"text": "📜 Ещё раз условия", "callback_data": "terms:short"}],
                        [{"text": "✅ Всё-таки принимаю", "callback_data": "terms:yes"}],
                    ]
                },
                message_id=mid,
                state=state,
                uid=uid,
                store_key="terms_ui_msg",
            )
        return True

    tg.answer_callback(cfg, cq["id"], "ok")
    return True


def require_terms_or_gate(
    cfg: dict, state: dict, msg: dict
) -> bool:
    """
    True = обработку нужно СТОПНУТЬ (показали gate).
    Владелец всегда проходит.
    """
    chat = msg.get("chat") or {}
    if chat.get("type") != "private":
        return False
    user = msg.get("from") or {}
    if is_owner(cfg, user):
        return False
    uid = int(user.get("id") or 0)
    if not uid:
        return False
    if terms.is_accepted(uid):
        return False
    # скрин/фото для розыгрыша — если уже нажал «Участвовать», не режем terms
    if msg.get("photo") or (
        (msg.get("document") or {}).get("mime_type") or ""
    ).startswith("image/"):
        try:
            act = gw.get_active(state)
            if act and act.get("status") == "active" and gw.get_entry(act, uid):
                return False
        except Exception:
            pass
    # deep-link розыгрыша — handle_giveaway / terms pending
    text_raw = (msg.get("text") or "").strip()
    if text_raw.startswith("/start") and (
        " gw_" in f" {text_raw}" or " gwref_" in f" {text_raw}"
    ):
        return False
    # команды terms/start обрабатывает handle_terms_private
    text = text_raw.lower()
    cmd = text.split()[0].split("@")[0] if text.startswith("/") else ""
    if cmd in (
        "/terms",
        "/rules",
        "/policy",
        "/privacy",
        "/agreement",
        "/offer",
        "/prices",
        "/pricing",
        "/tariffs",
        "/support",
        "/legal",
        "/docs",
        "/help_support",
        "/tickets",
        "/mytickets",
        "/menu",
        "/cancel",
        "/правила",
        "/политика",
        "/условия",
        "/оферта",
        "/соглашение",
        "/тарифы",
        "/цены",
        "/поддержка",
        "/контакт",
        "/документы",
        "/конфиденциальность",
        "/тикеты",
        "/обращения",
        "/меню",
        "/отмена",
        "/start",
        "/help",
    ):
        return False
    send_terms_gate(cfg, chat.get("id"), state=state, uid=uid, full=False)
    return True


def require_not_blocked(cfg: dict, msg: dict) -> bool:
    """True = стоп (показали блок). Владелец проходит. Документы — всегда."""
    chat = msg.get("chat") or {}
    if chat.get("type") != "private":
        return False
    user = msg.get("from") or {}
    if is_owner(cfg, user):
        return False
    uid = int(user.get("id") or 0)
    if not uid or not mod.is_blocked(uid):
        return False
    text = (msg.get("text") or "").strip().lower()
    cmd = text.split()[0].split("@")[0] if text.startswith("/") else ""
    # юр. документы и прайс — не режем (Platega / банк / прозрачность)
    if cmd in (
        "/legal",
        "/docs",
        "/документы",
        "/privacy",
        "/конфиденциальность",
        "/agreement",
        "/offer",
        "/соглашение",
        "/оферта_док",
        "/public_offer",
        "/публичная_оферта",
        "/prices",
        "/pricing",
        "/tariffs",
        "/тарифы",
        "/цены",
        "/terms",
        "/rules",
        "/policy",
        "/правила",
        "/политика",
        "/условия",
        "/оферта",
    ):
        return False
    if cmd in ("/start", "/help", "/support", "/помощь"):
        tg.send_message(
            cfg,
            chat.get("id"),
            mod.blocked_user_message(),
            parse_mode="HTML",
            disable_preview=False,
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "🔒 Политика", "url": terms.PRIVACY_URL},
                        {"text": "📜 Оферта", "url": terms.AGREEMENT_URL},
                    ],
                    [{"text": "📋 Документы", "callback_data": "legal:hub"}],
                ]
            },
        )
        return True
    tg.send_message(
        cfg,
        chat.get("id"),
        mod.blocked_user_message(),
        parse_mode="HTML",
        disable_preview=False,
        reply_markup={
            "inline_keyboard": [
                [
                    {"text": "🔒 Политика", "url": terms.PRIVACY_URL},
                    {"text": "📜 Оферта", "url": terms.AGREEMENT_URL},
                ]
            ]
        },
    )
    return True


def apply_tz_moderation(
    cfg: dict,
    state: dict,
    *,
    uid: int,
    uname: str,
    name: str,
    brief: str,
    chat_id: int | str,
) -> bool:
    """
    True = заблокировали (дальше не продолжать заказ).
    """
    illegal, reason, hits = mod.check_tz(brief)
    if not illegal:
        return False
    mod.block_user(
        uid,
        reason=reason,
        source="tz_auto",
        snippet=brief[:400],
        username=uname,
        name=name,
        by="auto",
        category=mod.primary_category(hits),
    )
    # сброс черновика
    try:
        state.setdefault("order_draft", {}).pop(str(uid), None)
        save_state(state)
    except Exception:
        pass
    tg.send_message(
        cfg,
        chat_id,
        "🚫 <b>Заказ отклонён · аккаунт заблокирован</b>\n\n"
        f"{html.escape(reason)}\n\n"
        "Мы не принимаем незаконные и мошеннические задачи.\n"
        "Если сработала ошибка — напиши владельцу: снять блок может только он.\n\n"
        + mod.blocked_user_message(),
        parse_mode="HTML",
    )
    un = f"@{html.escape(uname)}" if uname else html.escape(name or str(uid))
    notify_owner(
        cfg,
        "🚨 <b>Автоблок · незаконное ТЗ</b>\n\n"
        f"user {un} · <code>{uid}</code>\n"
        f"{html.escape(reason)}\n"
        f"hits: <code>{html.escape(', '.join(hits[:6]))}</code>\n\n"
        f"<b>ТЗ:</b>\n{html.escape(brief[:900])}",
        reply_markup=mod.owner_block_keyboard(uid),
    )
    return True


def handle_mod_callback(cfg: dict, state: dict, cq: dict) -> bool:
    data = cq.get("data") or ""
    if not data.startswith("mod:"):
        return False
    user = cq.get("from") or {}
    if not is_owner(cfg, user):
        tg.answer_callback(cfg, cq["id"], "Только владелец", show_alert=True)
        return True
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    mid = msg.get("message_id")

    if action == "un" and len(parts) >= 3:
        try:
            target = int(parts[2])
        except ValueError:
            tg.answer_callback(cfg, cq["id"], "bad id", show_alert=True)
            return True
        ent = mod.unblock_user(target, by=f"owner:{user.get('id')}")
        if not ent:
            tg.answer_callback(cfg, cq["id"], "Не был в блоке", show_alert=True)
            return True
        tg.answer_callback(cfg, cq["id"], "Разблокирован")
        if chat_id and mid:
            try:
                tg.edit_message_text(
                    cfg,
                    chat_id,
                    mid,
                    f"✅ Разблокирован <code>{target}</code>",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        try:
            tg.send_message(
                cfg,
                target,
                "✅ <b>Доступ восстановлен владельцем</b>\n\n"
                "Можно снова пользоваться ботом.\n"
                "Помни: незаконные задачи = повторный блок.\n"
                "/terms · /order",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return True

    tg.answer_callback(cfg, cq["id"], "ok")
    return True


def handle_mod_owner_commands(cfg: dict, state: dict, msg: dict) -> bool:
    """Команды владельца: /block /unblock /blocks"""
    chat = msg.get("chat") or {}
    if chat.get("type") != "private":
        return False
    user = msg.get("from") or {}
    if not is_owner(cfg, user):
        return False
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return False
    lower = text.lower()
    cmd = lower.split()[0].split("@")[0]
    arg = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
    chat_id = chat.get("id")

    if cmd in ("/blocks", "/blocked", "/баны"):
        items = mod.list_blocked(25)
        if not items:
            tg.send_message(cfg, chat_id, "Заблокированных нет.")
            return True
        lines = ["🚫 <b>Блоки</b>\n"]
        for b in items:
            un = b.get("username")
            who = f"@{un}" if un else b.get("name") or b.get("user_id")
            lines.append(
                f"• <code>{b.get('user_id')}</code> {html.escape(str(who))}\n"
                f"  {html.escape(str(b.get('reason') or '')[:120])}"
            )
        tg.send_message(cfg, chat_id, "\n".join(lines), parse_mode="HTML")
        for b in items[:8]:
            tg.send_message(
                cfg,
                chat_id,
                f"user <code>{b.get('user_id')}</code>",
                parse_mode="HTML",
                reply_markup=mod.owner_block_keyboard(int(b["user_id"])),
            )
        return True

    if cmd in ("/unblock", "/разблок", "/unban"):
        if not arg:
            tg.send_message(cfg, chat_id, "Пример: /unblock 123456789")
            return True
        try:
            target = int(arg.split()[0])
        except ValueError:
            tg.send_message(cfg, chat_id, "user_id числом")
            return True
        ent = mod.unblock_user(target, by=f"owner:{user.get('id')}")
        if not ent:
            tg.send_message(cfg, chat_id, "Не найден в блоке (или уже снят)")
            return True
        tg.send_message(
            cfg, chat_id, f"✅ Разблокирован <code>{target}</code>", parse_mode="HTML"
        )
        try:
            tg.send_message(
                cfg,
                target,
                "✅ Доступ восстановлен владельцем.\n/order · /terms",
                parse_mode=None,
            )
        except Exception:
            pass
        return True

    if cmd in ("/block", "/бан"):
        parts = arg.split(maxsplit=1)
        if not parts:
            tg.send_message(
                cfg, chat_id, "Пример: /block 123456789 спам"
            )
            return True
        try:
            target = int(parts[0])
        except ValueError:
            tg.send_message(cfg, chat_id, "user_id числом")
            return True
        reason = parts[1] if len(parts) > 1 else "manual"
        mod.block_user(
            target,
            reason=reason,
            source="manual",
            by=f"owner:{user.get('id')}",
        )
        tg.send_message(
            cfg,
            chat_id,
            f"🚫 Заблокирован <code>{target}</code>\n{html.escape(reason)}",
            parse_mode="HTML",
            reply_markup=mod.owner_block_keyboard(target),
        )
        try:
            tg.send_message(
                cfg, target, mod.blocked_user_message(), parse_mode="HTML"
            )
        except Exception:
            pass
        return True

    return False


def handle_balance_private(cfg: dict, state: dict, msg: dict) -> bool:
    """Баланс + пополнение СБП (всем)."""
    chat = msg.get("chat") or {}
    if chat.get("type") != "private":
        return False
    user = msg.get("from") or {}
    uid = int(user.get("id") or 0)
    if not uid:
        return False
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()
    uname = user.get("username") or ""
    name = user.get("first_name") or uname or str(uid)
    owner = is_owner(cfg, user)
    lower = text.lower()
    cmd = lower.split()[0].split("@")[0] if lower.startswith("/") else ""
    arg = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""

    # await custom topup amount
    await_b = state.setdefault("balance_await", {})
    aw = await_b.get(str(uid))
    if aw and aw.get("step") == "custom_amount" and text and not text.startswith("/"):
        if not bal.topup_enabled(cfg):
            await_b.pop(str(uid), None)
            save_state(state)
            tg.send_message(cfg, chat_id, bal.topup_disabled_text(), parse_mode="HTML")
            return True
        try:
            amount = int("".join(c for c in text if c.isdigit()) or "0")
        except ValueError:
            amount = 0
        if amount < bal.TOPUP_MIN or amount > bal.TOPUP_MAX:
            tg.send_message(
                cfg,
                chat_id,
                f"Сумма от {bal.TOPUP_MIN} до {bal.TOPUP_MAX} ₽. Ещё раз числом:",
                parse_mode=None,
            )
            return True
        await_b.pop(str(uid), None)
        save_state(state)
        return _start_topup_flow(
            cfg, state, chat_id, uid, amount, uname=uname, name=name, message_id=None
        )

    if cmd in ("/balance", "/bal", "/баланс", "/кошелёк", "/кошелек"):
        ui_edit_or_send(
            cfg,
            chat_id,
            bal.format_balance_card(uid, cfg),
            reply_markup=bal.balance_keyboard(cfg),
            state=state,
            uid=uid,
            store_key="bal_ui_msg",
        )
        return True
    if cmd in ("/points", "/баллы", "/pts"):
        pts = bal.get_points(uid)
        ui_edit_or_send(
            cfg,
            chat_id,
            bal.format_points_help() + f"\n\nУ тебя: <b>{pts}</b> ⭐",
            reply_markup=bal.points_keyboard(),
            state=state,
            uid=uid,
            store_key="bal_ui_msg",
        )
        return True

    if cmd in ("/topup", "/пополнить", "/sbp", "/сбп"):
        if not bal.topup_enabled(cfg):
            ui_edit_or_send(
                cfg,
                chat_id,
                bal.topup_disabled_text(),
                reply_markup=bal.balance_keyboard(cfg),
                state=state,
                uid=uid,
                store_key="bal_ui_msg",
            )
            return True
        if arg:
            try:
                amount = int("".join(c for c in arg if c.isdigit()) or "0")
            except ValueError:
                amount = 0
            if amount >= bal.TOPUP_MIN:
                return _start_topup_flow(
                    cfg, state, chat_id, uid, amount, uname=uname, name=name
                )
        ui_edit_or_send(
            cfg,
            chat_id,
            "💳 <b>Пополнение через СБП</b>\n\n"
            f"Баланс: <b>{bal.get_balance(uid)}</b> ₽\n"
            f"Сумма от {bal.TOPUP_MIN} до {bal.TOPUP_MAX} ₽.\n\n"
            "Выбери сумму или /topup 500",
            reply_markup=bal.topup_amounts_keyboard(),
            state=state,
            uid=uid,
            store_key="bal_ui_msg",
        )
        return True

    # owner: pending topups / manual credit
    if owner and cmd in ("/balpend", "/sbppend", "/пополнения"):
        pending = bal.list_pending_topups(20)
        if not pending:
            tg.send_message(cfg, chat_id, "Нет открытых заявок на пополнение.")
            return True
        lines = [
            "💳 <b>Заявки СБП</b>\n"
            "<i>В банке ищи сумму pay_exact → Зачислить</i>\n"
        ]
        for t in pending:
            un = t.get("username")
            who = f"@{un}" if un else t.get("name")
            pay = t.get("pay_exact") or t.get("amount")
            lines.append(
                f"• <code>{t.get('id')}</code> · <b>{pay}</b> ₽ · "
                f"{bal.topup_status_label(str(t.get('status')))}\n"
                f"  {html.escape(str(who))} · код <code>{t.get('code')}</code>"
            )
        tg.send_message(cfg, chat_id, "\n".join(lines), parse_mode="HTML")
        for t in pending[:5]:
            pay = t.get("pay_exact") or t.get("amount")
            tg.send_message(
                cfg,
                chat_id,
                f"Заявка <code>{html.escape(str(t.get('id')))}</code>\n"
                f"Ищи в банке: <b>{pay}</b> ₽ · код "
                f"<code>{html.escape(str(t.get('code')))}</code>",
                parse_mode="HTML",
                reply_markup=bal.topup_owner_keyboard(str(t["id"])),
            )
        return True

    if owner and cmd in ("/baladd", "/balset"):
        # /baladd USER_ID|@username 500 [коммент]
        parts = arg.split()
        if len(parts) < 2:
            tg.send_message(
                cfg,
                chat_id,
                "Пример:\n<code>/baladd 123456789 500</code>\n"
                "<code>/baladd @Ibramosta 10000</code>\n"
                "<code>/balset 123456789 0</code> — выставить баланс\n\n"
                "Баланс общий: на Bothost идёт через мост на ПК.",
                parse_mode="HTML",
            )
            return True
        target_raw = parts[0].strip()
        try:
            amount = int(parts[1])
        except ValueError:
            tg.send_message(cfg, chat_id, "Сумма — числом")
            return True
        target = 0
        try:
            target = int(target_raw.lstrip("@"))
        except ValueError:
            target = 0
        if not target:
            un = target_raw.lstrip("@").lower()
            # known wallets + giveaway entries
            try:
                data = bal.load()
                for k, w in (data.get("wallets") or {}).items():
                    if str(w.get("username") or "").lower() == un:
                        target = int(k)
                        break
            except Exception:
                pass
            if not target:
                try:
                    act = gw.get_active()
                    for e in (act.get("entries") or {}).values():
                        if str(e.get("username") or "").lower() == un:
                            target = int(e.get("user_id") or 0)
                            break
                except Exception:
                    pass
        if not target:
            tg.send_message(
                cfg,
                chat_id,
                "Не нашёл user_id. Укажи числом или @username из розыгрыша/кошелька.",
            )
            return True
        note = " ".join(parts[2:])[:100] or "owner"
        if cmd == "/balset":
            cur = bal.get_balance(target)
            delta = amount - cur
            if delta > 0:
                new_b = bal.credit(target, delta, kind="owner_set", note=note)
            elif delta < 0:
                ok, new_b, err = bal.try_debit(
                    target, -delta, kind="owner_set", note=note
                )
                if not ok:
                    tg.send_message(cfg, chat_id, err)
                    return True
            else:
                new_b = cur
        else:
            if amount <= 0:
                tg.send_message(cfg, chat_id, "Сумма > 0")
                return True
            new_b = bal.credit(target, amount, kind="owner_add", note=note)
        tg.send_message(
            cfg,
            chat_id,
            f"✅ Баланс <code>{target}</code> → <b>{new_b}</b> ₽",
            parse_mode="HTML",
        )
        try:
            tg.send_message(
                cfg,
                target,
                f"💰 Баланс пополнен владельцем: сейчас <b>{new_b}</b> ₽\n/balance",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return True

    if owner and cmd in ("/treasury", "/касса", "/cash"):
        tg.send_message(cfg, chat_id, bal.format_treasury(), parse_mode="HTML")
        return True

    def _save_sbp_qr_from_msg(m: dict) -> tuple[bool, str]:
        photos = m.get("photo") or []
        doc = m.get("document")
        file_id = None
        if photos:
            file_id = photos[-1].get("file_id")
        elif doc and str(doc.get("mime_type") or "").startswith("image/"):
            file_id = doc.get("file_id")
        if not file_id:
            return False, "Нужно фото"
        dest = Path(__file__).resolve().parent / "media" / "sbp_qr.jpg"
        dest.parent.mkdir(parents=True, exist_ok=True)
        meta = tg.api(cfg, "getFile", data={"file_id": file_id})
        fpath = meta.get("file_path") or ""
        token = (cfg.get("bot_token") or "").strip()
        import requests as _req

        url = f"https://api.telegram.org/file/bot{token}/{fpath}"
        r = _req.get(url, timeout=60)
        r.raise_for_status()
        dest.write_bytes(r.content)
        c2 = load_config()
        sbp = c2.setdefault("sbp", {})
        if isinstance(sbp, dict):
            sbp["qr_file"] = "media/sbp_qr.jpg"
            sbp["hide_phone"] = True
            sbp["auto_credit"] = True
            c2["sbp"] = sbp
            save_config(c2)
        return True, str(dest)

    # фото с подписью /setqr — сразу
    if owner and (msg.get("photo") or msg.get("document")):
        cap = (msg.get("caption") or "").strip().lower()
        if cap.startswith("/setqr") or cap.startswith("/sbp_qr") or cap.startswith("/qr"):
            ok, info = _save_sbp_qr_from_msg(msg)
            if ok:
                state.pop("await_sbp_qr", None)
                save_state(state)
                tg.send_message(
                    cfg,
                    chat_id,
                    f"✅ QR сохранён: <code>{html.escape(info)}</code>\n"
                    "Автозачисление ночью: <b>вкл</b>.\nТест: /topup 100",
                    parse_mode="HTML",
                )
            else:
                tg.send_message(cfg, chat_id, f"❌ {html.escape(info)}")
            return True

    if owner and cmd in ("/setqr", "/sbp_qr", "/qr"):
        state["await_sbp_qr"] = True
        save_state(state)
        tg.send_message(
            cfg,
            chat_id,
            "📷 Пришли <b>фото QR</b> для СБП одним сообщением.\n"
            "Или: фото с подписью <code>/setqr</code>\n"
            "Сохраню → клиенты получат при /topup.\n"
            "Отмена: /cancel_qr",
            parse_mode="HTML",
        )
        return True

    if owner and cmd in ("/cancel_qr",):
        state.pop("await_sbp_qr", None)
        save_state(state)
        tg.send_message(cfg, chat_id, "Ок, загрузку QR отменил.")
        return True

    if owner and state.get("await_sbp_qr"):
        if msg.get("photo") or msg.get("document"):
            try:
                ok, info = _save_sbp_qr_from_msg(msg)
                state.pop("await_sbp_qr", None)
                save_state(state)
                if ok:
                    tg.send_message(
                        cfg,
                        chat_id,
                        f"✅ QR сохранён: <code>{html.escape(info)}</code>\n"
                        "Клиентам при /topup уйдёт картинка.\n"
                        "⚡ Автозачисление 24/7: <b>вкл</b> "
                        "(«Я оплатил» → сразу баланс, утром сверишь банк).\n"
                        "Тест: /topup 100",
                        parse_mode="HTML",
                    )
                else:
                    tg.send_message(cfg, chat_id, f"❌ {html.escape(info)}")
            except Exception as e:
                tg.send_message(
                    cfg, chat_id, f"❌ Не сохранил QR: {html.escape(str(e)[:200])}"
                )
            return True
        if text and not text.startswith("/"):
            tg.send_message(cfg, chat_id, "Нужно <b>фото</b> QR, не текст.", parse_mode="HTML")
            return True

    if owner and cmd in ("/cashout", "/вывод", "/withdraw"):
        # /cashout 5000 [заметка] — учёт вывода (деньги уже на твоей карте)
        parts = arg.split(maxsplit=1)
        if not parts:
            tg.send_message(
                cfg,
                chat_id,
                "Учёт вывода (деньги с СБП уже у тебя в банке):\n"
                "<code>/cashout 5000</code>\n"
                "<code>/cashout 5000 на карту</code>\n\n"
                "Касса: /treasury",
                parse_mode="HTML",
            )
            return True
        try:
            amount = int("".join(c for c in parts[0] if c.isdigit()) or "0")
        except ValueError:
            amount = 0
        note = parts[1].strip() if len(parts) > 1 else "вывод"
        try:
            entry = bal.owner_cashout(amount, note=note)
        except ValueError as e:
            tg.send_message(cfg, chat_id, str(e))
            return True
        tg.send_message(
            cfg,
            chat_id,
            f"📤 Записал вывод <b>{entry.get('amount')}</b> ₽\n"
            f"Всего выведено (учёт): <b>{entry.get('withdrawn_total')}</b> ₽\n\n"
            + bal.format_treasury(),
            parse_mode="HTML",
        )
        return True

    return False


def _start_topup_flow(
    cfg: dict,
    state: dict,
    chat_id: int | str,
    uid: int,
    amount: int,
    *,
    uname: str = "",
    name: str = "",
    message_id: int | None = None,
) -> bool:
    if not bal.topup_enabled(cfg):
        tg.send_message(cfg, chat_id, bal.topup_disabled_text(), parse_mode="HTML")
        return True

    # 1) Platega (основной путь)
    try:
        import platega_lib as platega

        if platega.ready(cfg):
            try:
                item = platega.create_payment(
                    cfg,
                    user_id=uid,
                    amount=int(amount),
                    username=uname,
                    description=f"Vaggo balance +{int(amount)} RUB @{uname or uid}",
                )
            except Exception as e:
                print("platega create", type(e).__name__, e, flush=True)
                tg.send_message(
                    cfg,
                    chat_id,
                    "⚠️ Не удалось создать платёж Platega.\n"
                    f"<code>{html.escape(str(e)[:200])}</code>\n"
                    "Попробуй позже или /support",
                    parse_mode="HTML",
                )
                notify_owner(
                    cfg,
                    f"⚠️ Platega create fail uid={uid} {amount}₽\n"
                    f"<code>{html.escape(str(e)[:300])}</code>",
                )
                return True
            text = platega.format_pay_message(item)
            ui_edit_or_send(
                cfg,
                chat_id,
                text,
                reply_markup=platega.pay_keyboard(item),
                message_id=message_id,
                state=state,
                uid=uid,
                store_key="bal_ui_msg",
            )
            notify_owner(
                cfg,
                f"💳 Platega заявка {html.escape(str(item.get('id')))}\n"
                f"user <code>{uid}</code> @{html.escape(uname or '—')} · "
                f"<b>{int(amount)}</b> ₽",
            )
            return True
    except Exception as e:
        print("platega topup branch", e, flush=True)

    # 2) fallback — старый ручной СБП (если включён)
    sbp = bal.sbp_cfg(cfg)
    if not (cfg.get("sbp") or {}).get("enabled"):
        tg.send_message(
            cfg,
            chat_id,
            "⚠️ Оплата: Platega не настроена (нет ключей на хосте).\n"
            "Владелец: env <code>PLATEGA_MERCHANT_ID</code> + "
            "<code>PLATEGA_SECRET</code>.\n/support",
            parse_mode="HTML",
        )
        return True
    try:
        top = bal.create_topup(
            user_id=uid, amount=amount, username=uname, name=name
        )
    except ValueError as e:
        tg.send_message(cfg, chat_id, f"⚠️ {html.escape(str(e))}", parse_mode="HTML")
        return True
    text = bal.format_sbp_instructions(cfg, top)
    ui_edit_or_send(
        cfg,
        chat_id,
        text,
        reply_markup=bal.topup_user_keyboard(str(top["id"]), cfg),
        message_id=message_id,
        state=state,
        uid=uid,
        store_key="bal_ui_msg",
    )
    pay = int(top.get("pay_exact") or top.get("amount") or amount)
    qp = bal.qr_path(cfg)
    if qp:
        try:
            tg.send_photo(
                cfg,
                chat_id,
                str(qp),
                caption=(
                    f"QR СБП\n"
                    f"Сумма: <b>{pay}</b> ₽ (ровно)\n"
                    f"Код: <code>{html.escape(str(top.get('code')))}</code>"
                ),
            )
        except Exception as e:
            print("sbp qr send", e, flush=True)
    if not sbp.get("qr_ok"):
        notify_owner(
            cfg,
            "⚠️ Клиент /topup, но QR ещё не загружен.\n"
            "Пришли боту: /setqr → фото QR.\n"
            f"user <code>{uid}</code> · {pay} ₽ · "
            f"<code>{html.escape(str(top['id']))}</code>",
        )
    return True


def handle_pricing_callback(cfg: dict, state: dict, cq: dict) -> bool:
    """Владелец: price:menu | kind | set | ask | gdisc | kdisc..."""
    data = cq.get("data") or ""
    if not data.startswith("price:"):
        return False
    user = cq.get("from") or {}
    uid = int(user.get("id") or 0)
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    mid = msg.get("message_id")
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else "menu"

    def show(text: str, markup: dict | None = None) -> None:
        if not chat_id:
            return
        ui_edit_or_send(
            cfg,
            chat_id,
            text,
            reply_markup=markup,
            message_id=mid,
            state=state,
            uid=uid,
            store_key="price_ui_msg",
        )

    if action == "menu":
        tg.answer_callback(cfg, cq["id"], "Прайс")
        show(orders.pricing_admin_html(), orders.pricing_admin_keyboard())
        return True

    if action == "kind" and len(parts) > 2:
        kind = parts[2]
        if kind not in orders.ORDER_TYPES:
            tg.answer_callback(cfg, cq["id"], "?", show_alert=True)
            return True
        meta = orders.ORDER_TYPES[kind]
        base = orders.base_price(kind)
        final = orders.effective_price(kind)
        d = orders.discount_pct_for(kind)
        tg.answer_callback(cfg, cq["id"], meta.get("title") or kind)
        show(
            f"💰 <b>{html.escape(str(meta.get('title') or kind))}</b>\n"
            f"<code>{html.escape(kind)}</code>\n\n"
            f"База: <b>{base}</b> ₽\n"
            f"Скидка: <b>−{d}%</b>\n"
            f"К оплате: <b>{final}</b> ₽\n\n"
            f"Выбери новую базу или скидку на этот тип:",
            orders.pricing_kind_keyboard(kind),
        )
        return True

    if action == "set" and len(parts) > 3:
        kind, p_s = parts[2], parts[3]
        try:
            orders.set_base_price(kind, int(p_s))
            tg.answer_callback(cfg, cq["id"], f"{p_s} ₽")
        except Exception as e:
            tg.answer_callback(cfg, cq["id"], str(e)[:100], show_alert=True)
            return True
        show(orders.pricing_admin_html(), orders.pricing_admin_keyboard())
        return True

    if action == "ask" and len(parts) > 2:
        kind = parts[2]
        state.setdefault("await", {})[str(uid)] = {
            "type": "pricing_price",
            "kind": kind,
        }
        save_state(state)
        tg.answer_callback(cfg, cq["id"], "Жду число")
        show(
            f"✏️ Новая <b>базовая</b> цена для <code>{html.escape(kind)}</code>\n"
            f"Сейчас база {orders.base_price(kind)} ₽ · к оплате {orders.effective_price(kind)} ₽\n\n"
            f"Пришли число, например <code>400</code>\n"
            f"Отмена: /cancel",
            {"inline_keyboard": [[{"text": "◀️ Назад", "callback_data": f"price:kind:{kind}"}]]},
        )
        return True

    if action == "gdisc" and len(parts) > 2:
        try:
            orders.set_global_discount(int(parts[2]))
            tg.answer_callback(cfg, cq["id"], f"−{parts[2]}%")
        except Exception as e:
            tg.answer_callback(cfg, cq["id"], str(e)[:80], show_alert=True)
            return True
        show(orders.pricing_admin_html(), orders.pricing_admin_keyboard())
        return True

    if action == "gdisc_ask":
        state.setdefault("await", {})[str(uid)] = {"type": "pricing_gdisc"}
        save_state(state)
        tg.answer_callback(cfg, cq["id"], "Жду %")
        show(
            "🏷 Глобальная скидка % для <b>всех</b> услуг (0–90).\n"
            "Пример: <code>15</code>\n"
            "0 = без скидки. /cancel",
            {"inline_keyboard": [[{"text": "◀️ Назад", "callback_data": "price:menu"}]]},
        )
        return True

    if action == "kdisc" and len(parts) > 3:
        kind, pct_s = parts[2], parts[3]
        try:
            orders.set_kind_discount(kind, int(pct_s))
            tg.answer_callback(cfg, cq["id"], f"{kind} −{pct_s}%")
        except Exception as e:
            tg.answer_callback(cfg, cq["id"], str(e)[:80], show_alert=True)
            return True
        show(orders.pricing_admin_html(), orders.pricing_admin_keyboard())
        return True

    if action == "kdisc_ask" and len(parts) > 2:
        kind = parts[2]
        state.setdefault("await", {})[str(uid)] = {
            "type": "pricing_kdisc",
            "kind": kind,
        }
        save_state(state)
        tg.answer_callback(cfg, cq["id"], "Жду %")
        show(
            f"🏷 Скидка % только для <code>{html.escape(kind)}</code> (0–90).\n"
            f"Перебивает глобальную. 0 = снять kind-скидку.\n"
            f"/cancel",
            {
                "inline_keyboard": [
                    [{"text": "◀️ Назад", "callback_data": f"price:kind:{kind}"}]
                ]
            },
        )
        return True

    tg.answer_callback(cfg, cq["id"], "ok")
    return True


def handle_pricing_await(cfg: dict, state: dict, msg: dict) -> bool:
    """Await ввода цены / % скидки владельцем."""
    user = msg.get("from") or {}
    if not is_owner(cfg, user):
        return False
    uid = int(user.get("id") or 0)
    chat_id = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    aw = (state.get("await") or {}).get(str(uid)) or {}
    at = str(aw.get("type") or "")
    if at not in ("pricing_price", "pricing_gdisc", "pricing_kdisc"):
        return False
    if text.lower() in ("/cancel", "отмена", "cancel"):
        (state.get("await") or {}).pop(str(uid), None)
        save_state(state)
        tg.send_message(cfg, chat_id, "Ок, отменил.", parse_mode="HTML")
        return True
    # число
    num_s = text.replace("%", "").replace("₽", "").replace(" ", "").strip()
    try:
        num = int(float(num_s.replace(",", ".")))
    except Exception:
        tg.send_message(cfg, chat_id, "Нужно число. Пример: <code>400</code>", parse_mode="HTML")
        return True
    try:
        if at == "pricing_price":
            kind = str(aw.get("kind") or "")
            orders.set_base_price(kind, num)
            msg_ok = f"✅ База <code>{html.escape(kind)}</code> = <b>{num}</b> ₽"
        elif at == "pricing_gdisc":
            orders.set_global_discount(num)
            msg_ok = f"✅ Глобальная скидка <b>−{max(0, min(90, num))}%</b>"
        else:
            kind = str(aw.get("kind") or "")
            orders.set_kind_discount(kind, num)
            msg_ok = (
                f"✅ Скидка <code>{html.escape(kind)}</code> "
                f"<b>−{max(0, min(90, num))}%</b>"
            )
        (state.get("await") or {}).pop(str(uid), None)
        save_state(state)
        tg.send_message(
            cfg,
            chat_id,
            msg_ok + "\n\n" + orders.pricing_admin_html(),
            parse_mode="HTML",
            reply_markup=orders.pricing_admin_keyboard(),
        )
    except Exception as e:
        tg.send_message(cfg, chat_id, f"⚠️ {html.escape(str(e)[:200])}", parse_mode="HTML")
    return True


def handle_platega_callback(cfg: dict, state: dict, cq: dict) -> bool:
    """pay:check:ID | pay:cancel:ID"""
    data = cq.get("data") or ""
    if not data.startswith("pay:"):
        return False
    try:
        import platega_lib as platega
    except Exception as e:
        tg.answer_callback(cfg, cq["id"], "Platega offline", show_alert=True)
        print("platega import", e, flush=True)
        return True

    user = cq.get("from") or {}
    uid = int(user.get("id") or 0)
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    mid = msg.get("message_id")
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    pid = parts[2] if len(parts) > 2 else ""

    item = platega.get_payment(pid) if pid else None
    if not item:
        tg.answer_callback(cfg, cq["id"], "Заявка не найдена", show_alert=True)
        return True
    if int(item.get("user_id") or 0) != uid and not is_owner(cfg, user):
        tg.answer_callback(cfg, cq["id"], "Чужая заявка", show_alert=True)
        return True

    if action == "cancel":
        if item.get("status") == "paid":
            tg.answer_callback(cfg, cq["id"], "Уже оплачено")
            return True
        try:
            platega.apply_fail(pid, reason="user_cancel")
        except Exception:
            pass
        tg.answer_callback(cfg, cq["id"], "Отменено")
        if chat_id:
            tg.send_message(
                cfg,
                chat_id,
                "❌ Заявка отменена. /topup — новая.",
                parse_mode="HTML",
            )
        return True

    if action == "check":
        tg.answer_callback(cfg, cq["id"], "Проверяю…")
        updated = item
        try:
            if item.get("external_id"):
                updated = platega.sync_payment_status(cfg, item) or item
        except Exception as e:
            print("pay check", e, flush=True)
        st = str((updated or item).get("status") or "")
        if st == "paid":
            bal_now = bal.get_balance(uid)
            text = (
                f"✅ <b>Оплата прошла</b>\n"
                f"+{int(item.get('amount') or 0)} ₽\n"
                f"Баланс: <b>{bal_now}</b> ₽\n\n"
                f"/order — заказ · /balance — карта"
            )
            if chat_id:
                try:
                    tg.edit_message_text(
                        cfg, chat_id, int(mid), text, parse_mode="HTML"
                    )
                except Exception:
                    tg.send_message(cfg, chat_id, text, parse_mode="HTML")
            notify_owner(
                cfg,
                f"✅ Platega paid uid=<code>{uid}</code> "
                f"+{int(item.get('amount') or 0)} ₽ · <code>{html.escape(pid)}</code>",
            )
            return True
        if st == "failed":
            if chat_id:
                tg.send_message(
                    cfg,
                    chat_id,
                    "❌ Платёж не прошёл / отменён. /topup — снова.",
                    parse_mode="HTML",
                )
            return True
        if chat_id:
            tg.send_message(
                cfg,
                chat_id,
                "⏳ Ещё не видно оплаты. Оплати по кнопке и нажми «Проверить» через 20–60 сек.",
                parse_mode="HTML",
            )
        return True

    tg.answer_callback(cfg, cq["id"], "ok")
    return True


def handle_balance_callback(cfg: dict, state: dict, cq: dict) -> bool:
    data = cq.get("data") or ""
    if not data.startswith("bal:"):
        return False
    user = cq.get("from") or {}
    uid = int(user.get("id") or 0)
    uname = user.get("username") or ""
    name = user.get("first_name") or uname or str(uid)
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    mid = msg.get("message_id")
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    def show(text: str, markup: dict | None = None) -> None:
        if not chat_id:
            return
        ui_edit_or_send(
            cfg,
            chat_id,
            text,
            reply_markup=markup,
            message_id=mid,
            state=state,
            uid=uid,
            store_key="bal_ui_msg",
        )

    if action == "show":
        tg.answer_callback(cfg, cq["id"], "Баланс")
        show(bal.format_balance_card(uid, cfg), bal.balance_keyboard(cfg))
        return True
    if action == "points":
        tg.answer_callback(cfg, cq["id"], "Баллы")
        pts = bal.get_points(uid)
        show(
            bal.format_points_help() + f"\n\nУ тебя: <b>{pts}</b> ⭐",
            bal.points_keyboard(),
        )
        return True
    if action == "redeem":
        packs = 1
        try:
            packs = max(1, int(parts[2] if len(parts) > 2 else 1))
        except Exception:
            packs = 1
        need = packs * int(bal.POINTS_REDEEM_PTS)
        rub = packs * int(bal.POINTS_REDEEM_RUB)
        try:
            spent, got, left = bal.redeem_points_to_balance(uid, packs)
            tg.answer_callback(cfg, cq["id"], f"+{got} ₽")
            show(
                f"✅ Обмен: −{spent} ⭐ → +<b>{got}</b> ₽\n"
                f"Баланс: <b>{bal.get_balance(uid)}</b> ₽ · "
                f"баллы: <b>{left}</b> ⭐",
                bal.balance_keyboard(cfg),
            )
        except ValueError as e:
            tg.answer_callback(cfg, cq["id"], str(e)[:120], show_alert=True)
            show(
                bal.format_points_help()
                + f"\n\nУ тебя: <b>{bal.get_points(uid)}</b> ⭐\n"
                f"Нужно минимум <b>{need}</b> для обмена на {rub} ₽.",
                bal.points_keyboard(),
            )
        return True

    if action == "topup":
        if not bal.topup_enabled(cfg):
            tg.answer_callback(cfg, cq["id"], "Пополнение выкл", show_alert=True)
            show(bal.topup_disabled_text(), bal.balance_keyboard(cfg))
            return True
        tg.answer_callback(cfg, cq["id"], "Сумма")
        show(
            "💳 <b>Пополнение СБП</b>\n\n"
            f"Баланс: <b>{bal.get_balance(uid)}</b> ₽\n"
            "Выбери сумму:",
            bal.topup_amounts_keyboard(),
        )
        return True

    if action == "mytop":
        tg.answer_callback(cfg, cq["id"], "Заявки")
        items = bal.list_user_topups(uid, 8)
        if not items:
            show("Заявок пока нет.", bal.balance_keyboard(cfg))
            return True
        lines = ["📜 <b>Мои пополнения</b>\n"]
        for t in items:
            lines.append(
                f"• <code>{html.escape(str(t.get('id')))}</code> · "
                f"{t.get('amount')} ₽ · {bal.topup_status_label(str(t.get('status')))}\n"
                f"  код <code>{html.escape(str(t.get('code')))}</code>"
            )
        show("\n".join(lines), bal.balance_keyboard(cfg))
        return True

    if action == "custom":
        if not bal.topup_enabled(cfg):
            tg.answer_callback(cfg, cq["id"], "Пополнение выкл", show_alert=True)
            show(bal.topup_disabled_text(), bal.balance_keyboard(cfg))
            return True
        state.setdefault("balance_await", {})[str(uid)] = {"step": "custom_amount"}
        save_state(state)
        tg.answer_callback(cfg, cq["id"], "Своя сумма")
        show(
            f"Напиши сумму числом ({bal.TOPUP_MIN}–{bal.TOPUP_MAX} ₽):",
            {"inline_keyboard": [[{"text": "◀️ Назад", "callback_data": "bal:topup"}]]},
        )
        return True

    if action == "amt" and len(parts) >= 3:
        if not bal.topup_enabled(cfg):
            tg.answer_callback(cfg, cq["id"], "Пополнение выкл", show_alert=True)
            show(bal.topup_disabled_text(), bal.balance_keyboard(cfg))
            return True
        try:
            amount = int(parts[2])
        except ValueError:
            tg.answer_callback(cfg, cq["id"], "Ошибка", show_alert=True)
            return True
        tg.answer_callback(cfg, cq["id"], f"{amount} ₽")
        _start_topup_flow(
            cfg,
            state,
            chat_id,
            uid,
            amount,
            uname=uname,
            name=name,
            message_id=mid,
        )
        return True

    if action == "reveal" and len(parts) >= 3:
        # разовый показ реквизитов (номер) — только по кнопке
        tid = parts[2]
        top = bal.get_topup(tid)
        if not top or int(top.get("user_id") or 0) != uid:
            tg.answer_callback(cfg, cq["id"], "Заявка не найдена", show_alert=True)
            return True
        s = bal.sbp_cfg(cfg)
        if not s.get("phone"):
            tg.answer_callback(cfg, cq["id"], "Реквизиты не заданы", show_alert=True)
            return True
        tg.answer_callback(cfg, cq["id"], "Реквизиты")
        phone_line = f"<code>{html.escape(s['phone'])}</code>"
        extra = []
        if s.get("bank"):
            extra.append(f"🏦 {html.escape(s['bank'])}")
        if s.get("name"):
            extra.append(f"👤 {html.escape(s['name'])}")
        tg.send_message(
            cfg,
            chat_id,
            "📋 <b>Реквизиты для этой оплаты</b>\n"
            "(не пересылай в чаты)\n\n"
            f"📱 {phone_line}\n"
            + ("\n".join(extra) + "\n" if extra else "")
            + f"\nСумма: <b>{top.get('amount')}</b> ₽\n"
            f"Комментарий: <code>{html.escape(str(top.get('code')))}</code>",
            parse_mode="HTML",
            reply_markup=bal.topup_user_keyboard(tid, cfg),
        )
        return True

    if action == "paid" and len(parts) >= 3:
        tid = parts[2]
        try:
            top = bal.mark_paid(tid, uid)
        except ValueError as e:
            tg.answer_callback(cfg, cq["id"], str(e)[:180], show_alert=True)
            return True
        pay = int(top.get("pay_exact") or top.get("amount") or 0)
        un = top.get("username")
        who = f"@{un}" if un else top.get("name")
        sbp = bal.sbp_cfg(cfg)
        auto = bool(sbp.get("auto_credit")) and pay <= int(
            sbp.get("auto_credit_max") or 3000
        )

        if auto:
            # 24/7: сразу на баланс, владелец сверит банк когда проснётся
            try:
                top, new_bal = bal.confirm_topup(tid, 0)
            except ValueError as e:
                tg.answer_callback(cfg, cq["id"], str(e)[:180], show_alert=True)
                return True
            top["auto_credit"] = True
            bal.save_topup(top)
            tg.answer_callback(cfg, cq["id"], "Зачислено!")
            show(
                f"✅ <b>Баланс пополнен</b>\n\n"
                f"+<b>{pay}</b> ₽ (СБП)\n"
                f"Сейчас: <b>{new_bal}</b> ₽\n"
                f"Заявка <code>{html.escape(tid)}</code>\n\n"
                f"/balance · /order",
                bal.balance_keyboard(cfg),
            )
            notify_owner(
                cfg,
                "⚡ <b>СБП автозачисление</b> (клиент не ждал)\n\n"
                f"Проверь банк когда сможешь: <b>{pay}</b> ₽\n"
                f"код <code>{html.escape(str(top.get('code')))}</code>\n"
                f"заявка <code>{html.escape(tid)}</code>\n"
                f"от {html.escape(str(who))} · <code>{uid}</code>\n\n"
                "Нет перевода → «Списать (фейк)»\n"
                "Есть → «В банке ок»",
                reply_markup=bal.topup_owner_keyboard(tid, mode="review"),
            )
            return True

        tg.answer_callback(cfg, cq["id"], "На проверке")
        show(
            f"🔍 <b>Оплата на проверке</b>\n\n"
            f"Заявка <code>{html.escape(tid)}</code>\n"
            f"Сумма: <b>{pay}</b> ₽\n"
            f"Код: <code>{html.escape(str(top.get('code')))}</code>\n\n"
            "Зачислим после проверки.\n/balance",
            bal.balance_keyboard(cfg),
        )
        notify_owner(
            cfg,
            "💳 <b>СБП: «Я оплатил»</b> (ручная проверка)\n\n"
            f"Ищи в банке <b>{pay}</b> ₽ · код "
            f"<code>{html.escape(str(top.get('code')))}</code>\n"
            f"от {html.escape(str(who))} · <code>{uid}</code>",
            reply_markup=bal.topup_owner_keyboard(tid),
        )
        return True

    if action == "cancel" and len(parts) >= 3:
        tid = parts[2]
        try:
            bal.cancel_topup(tid, uid)
        except ValueError as e:
            tg.answer_callback(cfg, cq["id"], str(e)[:180], show_alert=True)
            return True
        tg.answer_callback(cfg, cq["id"], "Отменено")
        show(
            f"Заявка <code>{html.escape(tid)}</code> отменена.\n\n"
            + bal.format_balance_card(uid, cfg),
            bal.balance_keyboard(cfg),
        )
        return True

    # owner: confirm / reject / review after auto / reverse
    if action in ("ok", "no", "rev", "seen") and len(parts) >= 3:
        if not is_owner(cfg, user):
            tg.answer_callback(cfg, cq["id"], "Только владелец", show_alert=True)
            return True
        tid = parts[2]
        if action == "seen":
            tg.answer_callback(cfg, cq["id"], "Ок")
            if chat_id and mid:
                try:
                    tg.edit_message_text(
                        cfg,
                        chat_id,
                        mid,
                        f"👍 Проверено в банке · <code>{html.escape(tid)}</code>",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            return True
        if action == "rev":
            try:
                top, new_bal = bal.reverse_topup(tid, uid, note="owner reverse")
            except ValueError as e:
                tg.answer_callback(cfg, cq["id"], str(e)[:180], show_alert=True)
                return True
            pay = top.get("pay_exact") or top.get("amount")
            tg.answer_callback(cfg, cq["id"], "Списано")
            if chat_id and mid:
                try:
                    tg.edit_message_text(
                        cfg,
                        chat_id,
                        mid,
                        f"↩️ Отмена +{pay} ₽ снята · "
                        f"<code>{html.escape(tid)}</code>\n"
                        f"баланс клиента: {new_bal} ₽",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            try:
                tg.send_message(
                    cfg,
                    int(top["user_id"]),
                    f"↩️ Пополнение <code>{html.escape(tid)}</code> отменено "
                    f"(−{pay} ₽).\n"
                    f"Если перевод был — напиши владельцу с чеком.\n"
                    f"Баланс: <b>{new_bal}</b> ₽ · /balance",
                    parse_mode="HTML",
                )
            except Exception:
                pass
            return True
        if action == "ok":
            try:
                top, new_bal = bal.confirm_topup(tid, uid)
            except ValueError as e:
                tg.answer_callback(cfg, cq["id"], str(e)[:180], show_alert=True)
                return True
            pay = top.get("pay_exact") or top.get("amount")
            tg.answer_callback(cfg, cq["id"], "Зачислено")
            if chat_id:
                try:
                    tg.edit_message_text(
                        cfg,
                        chat_id,
                        mid,
                        f"✅ Зачислено <b>{pay}</b> ₽ · "
                        f"<code>{html.escape(tid)}</code>\n"
                        f"баланс клиента: {new_bal} ₽",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            try:
                tg.send_message(
                    cfg,
                    int(top["user_id"]),
                    f"✅ <b>Баланс пополнен</b>\n"
                    f"+{pay} ₽ (СБП)\n"
                    f"Сейчас: <b>{new_bal}</b> ₽\n\n"
                    f"/balance · /order",
                    parse_mode="HTML",
                    reply_markup=bal.balance_keyboard(cfg),
                )
            except Exception as e:
                print("bal client notify", e, flush=True)
            return True
        try:
            top = bal.reject_topup(tid, uid)
        except ValueError as e:
            tg.answer_callback(cfg, cq["id"], str(e)[:180], show_alert=True)
            return True
        pay = top.get("pay_exact") or top.get("amount")
        tg.answer_callback(cfg, cq["id"], "Отклонено")
        if chat_id:
            try:
                tg.edit_message_text(
                    cfg,
                    chat_id,
                    mid,
                    f"❌ Отклонено <code>{html.escape(tid)}</code> · {pay} ₽",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        try:
            tg.send_message(
                cfg,
                int(top["user_id"]),
                f"❌ Пополнение <code>{html.escape(tid)}</code> не подтверждено.\n"
                f"Если переводил — напиши владельцу с кодом "
                f"<code>{html.escape(str(top.get('code')))}</code>.\n"
                f"/topup — новая заявка",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return True

    tg.answer_callback(cfg, cq["id"], "ok")
    return True


def _order_advance_after_answer(
    cfg: dict,
    state: dict,
    chat_id: int | str,
    uid: int,
    *,
    draft: dict,
    step: dict,
    step_id: str,
    answers: dict,
    uname: str = "",
    name: str = "",
    message_id: int | None = None,
) -> None:
    """Следующий шаг опроса или AI-ревью (одно окно)."""
    drafts = state.setdefault("order_draft", {})
    nxt = orders.next_tz_step(step_id)
    if nxt:
        d2 = dict(draft)
        d2["kind"] = draft.get("kind")
        d2["tz_step"] = nxt["id"]
        d2["answers"] = answers
        d2.pop("await_confirm", None)
        drafts[str(uid)] = d2
        n = orders.tz_step_index(nxt["id"]) + 1
        total = len(orders.TZ_STEPS)
        ui_edit_or_send(
            cfg,
            chat_id,
            f"✅ <b>{n - 1}/{total}</b> · {html.escape(str(step.get('title')))}\n\n"
            + str(nxt.get("ask") or ""),
            reply_markup=orders.order_step_keyboard(),
            message_id=message_id,
            state=state,
            uid=uid,
            store_key="order_ui_msg",
        )
        save_state(state)
        return
    kind = draft.get("kind")
    _order_run_ai_review(
        cfg,
        state,
        chat_id,
        uid,
        kind=str(kind),
        answers=answers,
        uname=uname,
        name=name,
    )


def _order_start_text(cfg: dict | None = None, uid: int | None = None) -> str:
    bal_line = ""
    if uid:
        if cfg is not None and bal.topup_enabled(cfg):
            bal_line = f"💳 Баланс: <b>{bal.get_balance(uid)}</b> ₽ · /balance · /topup\n\n"
        else:
            bal_line = (
                f"💳 Баланс: <b>{bal.get_balance(uid)}</b> ₽ · /balance\n"
                f"<i>Пополнение — скоро (Platega)</i>\n\n"
            )
    prices_block = "\n".join(orders.price_catalog_lines())
    return (
        "🛠 <b>Заказ</b>\n"
        "━━━━━━━━━━━━\n\n"
        f"{bal_line}"
        f"{prices_block}\n\n"
        "1) Услуга → 2) 4 коротких ответа (можно пропуск)\n"
        "3) Grok соберёт ТЗ → 4) подтверди → оплата\n\n"
        "⚠️ Хостинг не в цене · 🛡 гарантия 2 сут."
    )


def _order_start_msg(
    cfg: dict,
    chat_id: int | str,
    *,
    state: dict | None = None,
    uid: int | None = None,
    message_id: int | None = None,
) -> None:
    ui_edit_or_send(
        cfg,
        chat_id,
        _order_start_text(cfg, uid),
        reply_markup=orders.order_keyboard_types(),
        message_id=message_id,
        state=state,
        uid=uid,
        store_key="order_ui_msg",
    )


def _order_show_estimate(
    cfg: dict,
    state: dict,
    chat_id: int | str,
    uid: int,
    *,
    kind: str,
    brief: str,
    answers: dict | None = None,
    message_id: int | None = None,
    review: dict | None = None,
) -> None:
    """Предоценка + кнопки отправить/заново (+ блок AI-сводки)."""
    est = orders.estimate(kind, brief)
    cur_bal = bal.get_balance(uid)
    price_s = f"<b>{est['price']} ₽</b> с баланса"
    drafts = state.setdefault("order_draft", {})
    drafts[str(uid)] = {
        "kind": kind,
        "brief": brief,
        "answers": answers or {},
        "await_confirm": True,
        "est": est,
        "ai_review": review or {},
    }
    save_state(state)
    bal_warn = ""
    if cur_bal < int(est["price"]):
        if bal.topup_enabled(cfg):
            bal_warn = (
                f"\n⚠️ На балансе <b>{cur_bal}</b> ₽ — не хватает "
                f"<b>{int(est['price']) - cur_bal}</b> ₽. Сначала /topup."
            )
        else:
            bal_warn = (
                f"\n⚠️ На балансе <b>{cur_bal}</b> ₽, нужно "
                f"<b>{est['price']}</b> ₽.\n"
                f"Пополнение скоро (Platega) · /support — тикет"
            )
    incl = html.escape(str(est.get("includes") or ""))
    ninc = html.escape(str(est.get("not_includes") or ""))
    rev = review or {}
    ai_block = ""
    if rev:
        risk = str(rev.get("risk") or "ok")
        risk_h = {"ok": "✅", "warn": "⚠️", "block": "🚫"}.get(risk, "•")
        parts = [f"🧠 <b>Проверка ТЗ</b> {risk_h}"]
        if rev.get("summary"):
            parts.append(html.escape(str(rev["summary"])[:500]))

        def _clean_client_ai_text(s: str) -> str:
            s = (s or "").strip()
            if not s:
                return ""
            # не показывать сырой JSON / dump полей (как на скрине «Добавил от себя»)
            low40 = s[:80].lower()
            if (
                s.startswith("{")
                or s.startswith('"')
                or '"brief"' in low40
                or "'brief'" in low40
                or low40.lstrip().startswith("brief")
                or s.count("\\n") >= 3
                or (s.count('"') >= 6 and ":" in s[:60])
            ):
                return ""
            if s.count("\\n") >= 2 and "\n" not in s[:100]:
                s = s.replace("\\n", "\n")
            return s[:400]

        add = _clean_client_ai_text(str(rev.get("additions") or ""))
        if add:
            parts.append("<b>Добавил от себя:</b> " + html.escape(add))
        if rev.get("feasible_reason"):
            parts.append(
                "<b>Выполнимость:</b> "
                + html.escape(str(rev["feasible_reason"])[:300])
            )
        if rev.get("legal_reason") and risk != "ok":
            parts.append(
                "<b>Законность:</b> " + html.escape(str(rev["legal_reason"])[:250])
            )
        if rev.get("risk_delay") or rev.get("risk_scope"):
            parts.append(
                f"📉 риски: сроки <b>{html.escape(str(rev.get('risk_delay') or '—'))}</b> · "
                f"объём <b>{html.escape(str(rev.get('risk_scope') or '—'))}</b>"
            )
        if rev.get("upsell"):
            up = _clean_client_ai_text(str(rev.get("upsell") or ""))
            if up:
                parts.append("💡 <b>Имеет смысл добавить:</b> " + html.escape(up[:280]))
        eng = str(rev.get("engine") or "")
        if eng == "grok":
            parts.append("<i>✅ обработано Grok</i>")
        elif eng in ("cloud", "groq", "gemini", "openrouter", "ai_bus"):
            parts.append("<i>✅ обработано AI</i>")
        elif eng.startswith("fallback"):
            # клиенту не орём «offline» — ТЗ уже нормальное из опроса
            parts.append("<i>ТЗ собрано по твоим ответам</i>")
        ai_block = "\n".join(parts) + "\n\n"
    warn_line = ""
    if str(rev.get("risk") or "") == "warn":
        warn_line = (
            "⚠️ Есть оговорки по объёму/ясности — можно слать, "
            "но уточни детали в /support если что.\n\n"
        )
    need = int(est["price"])
    can_pay = cur_bal >= need
    body = (
        f"📋 <b>Подтверждение</b>\n\n"
        + ai_block
        + warn_line
        + f"<b>{html.escape(est['title'])}</b> — {price_s}\n"
        f"💳 баланс: <b>{cur_bal}</b> ₽"
        + bal_warn
        + "\n\n"
        + (f"входит: {incl}\n" if incl else "")
        + (f"не входит: {ninc}\n" if ninc else "")
        + "\n"
        f"<b>Итоговое ТЗ:</b>\n{html.escape(brief[:1400])}\n\n"
        + (
            "Всё верно? «Отправить» = заказ + списание с баланса."
            if can_pay
            else "Сначала нужен баланс ≥ цены заказа — иначе «Отправить» не пройдёт."
        )
    )
    kb_rows: list = []
    if can_pay:
        kb_rows.append(
            [
                {"text": "✅ Всё верно · отправить", "callback_data": "ord:commit"},
                {"text": "✏️ Заново", "callback_data": "ord:restart"},
            ]
        )
    else:
        if bal.topup_enabled(cfg):
            kb_rows.append(
                [{"text": "💳 Пополнить баланс", "callback_data": "bal:topup"}]
            )
        kb_rows.append(
            [{"text": "💬 Написать в поддержку", "callback_data": "sup:new"}]
        )
        kb_rows.append(
            [
                {"text": "✏️ Заново", "callback_data": "ord:restart"},
                {"text": "❌ Отмена", "callback_data": "ord:cancel"},
            ]
        )
    if can_pay:
        kb_rows.append([{"text": "❌ Отмена", "callback_data": "ord:cancel"}])
    ui_edit_or_send(
        cfg,
        chat_id,
        body,
        reply_markup={"inline_keyboard": kb_rows},
        message_id=message_id,
        state=state,
        uid=uid,
        store_key="order_ui_msg",
    )
    save_state(state)


def _order_run_ai_review(
    cfg: dict,
    state: dict,
    chat_id: int | str,
    uid: int,
    *,
    kind: str,
    answers: dict,
    uname: str = "",
    name: str = "",
    extra_note: str = "",
) -> None:
    """
    Grok: собрать ТЗ, уточнить, законность + выполнимость.
    """
    ui_edit_or_send(
        cfg,
        chat_id,
        "🧠 <b>Собираю ТЗ и проверяю…</b>\n"
        "Единый бриф · законность · реально ли сделать в тарифе.\n"
        "Секунду.",
        reply_markup={
            "inline_keyboard": [[{"text": "❌ Отмена", "callback_data": "ord:cancel"}]]
        },
        state=state,
        uid=uid,
        store_key="order_ui_msg",
    )
    review = orders.review_tz_with_ai(
        cfg, kind, answers, extra_client_note=extra_note
    )
    # блок незаконного
    if not review.get("legal_ok") or str(review.get("risk") or "") == "block":
        brief_b = str(review.get("brief") or orders.build_brief_from_answers(kind, answers))
        if apply_tz_moderation(
            cfg,
            state,
            uid=uid,
            uname=uname,
            name=name,
            brief=brief_b,
            chat_id=chat_id,
        ):
            return
        # AI block без rule-hit — мягкий отказ без автобана
        state.setdefault("order_draft", {}).pop(str(uid), None)
        save_state(state)
        ui_edit_or_send(
            cfg,
            chat_id,
            "🚫 <b>Заказ не можем взять</b>\n\n"
            f"{html.escape(str(review.get('legal_reason') or 'Не проходит проверку.'))}\n\n"
            "Если ошибка — /support · владелец разберёт.",
            reply_markup={
                "inline_keyboard": [
                    [{"text": "🛠 Другой заказ", "callback_data": "ord:restart"}],
                    [{"text": "💬 Поддержка", "callback_data": "sup:new"}],
                ]
            },
            state=state,
            uid=uid,
            store_key="order_ui_msg",
        )
        notify_owner(
            cfg,
            "⚠️ <b>AI отклонил ТЗ</b> (без автобана)\n"
            f"user <code>{uid}</code> @{html.escape(uname)}\n"
            f"{html.escape(str(review.get('legal_reason') or '')[:300])}\n\n"
            f"{html.escape(brief_b[:800])}",
        )
        return

    # нереалистично жёстко — предложить упростить
    if not review.get("feasible") and str(review.get("risk") or "") == "warn":
        pass  # покажем warn в estimate

    questions = list(review.get("questions") or [])
    brief = str(review.get("brief") or orders.build_brief_from_answers(kind, answers))
    # сохранить заметки AI в answers
    ans2 = dict(answers or {})
    if review.get("additions"):
        ans2["ai_notes"] = str(review.get("additions"))[:500]
    if extra_note:
        ans2["ai_clarify"] = (str(ans2.get("ai_clarify") or "") + "\n" + extra_note).strip()[
            :1500
        ]

    if questions and not extra_note:
        # ещё не отвечали на уточнения — спросить
        drafts = state.setdefault("order_draft", {})
        drafts[str(uid)] = {
            "kind": kind,
            "tz_step": "ai_clarify",
            "answers": ans2,
            "brief": brief,
            "ai_review": review,
            "ai_questions": questions,
            "await_confirm": False,
        }
        save_state(state)
        q_lines = "\n".join(f"• {html.escape(q)}" for q in questions)
        ui_edit_or_send(
            cfg,
            chat_id,
            "🧠 <b>Почти готово — уточни, пожалуйста</b>\n\n"
            f"{html.escape(str(review.get('summary') or '')[:400])}\n\n"
            f"<b>Вопросы:</b>\n{q_lines}\n\n"
            "Ответь <b>одним сообщением</b> (можно списком).\n"
            "Или жми «Пропустить» — оформим как есть.",
            reply_markup={
                "inline_keyboard": [
                    [{"text": "⏭ Пропустить уточнения", "callback_data": "ord:ai_skip"}],
                    [{"text": "✏️ Заново", "callback_data": "ord:restart"}],
                    [{"text": "❌ Отмена", "callback_data": "ord:cancel"}],
                ]
            },
            state=state,
            uid=uid,
            store_key="order_ui_msg",
        )
        return

    _order_show_estimate(
        cfg,
        state,
        chat_id,
        uid,
        kind=kind,
        brief=brief,
        answers=ans2,
        review=review,
    )


def handle_owner_teamlead(cfg: dict, state: dict, msg: dict) -> bool:
    """
    Тимлид: владелец пишет обычным языком
    «горят заказы», «финрадар», «сводка» — или /tl /radar /hot.
    """
    chat = msg.get("chat") or {}
    if chat.get("type") != "private":
        return False
    user = msg.get("from") or {}
    if not is_owner(cfg, user):
        return False
    text = (msg.get("text") or "").strip()
    if not text:
        return False
    chat_id = chat.get("id")
    lower = text.lower()
    cmd = lower.split()[0].split("@")[0] if lower.startswith("/") else ""

    # явные команды
    force = False
    q = text
    if cmd in ("/tl", "/тимлид", "/team", "/lead"):
        force = True
        q = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else "сводка"
    elif cmd in ("/radar", "/finance", "/фин", "/кассa", "/касса"):
        force = True
        q = "финансовый радар"
    elif cmd in ("/hot", "/горит", "/горят"):
        force = True
        q = "какие заказы горят"
    elif text.startswith("/"):
        return False
    else:
        # естественный язык — только если похоже на бизнес-вопрос
        keys = (
            "заказ",
            "горят",
            "горит",
            "сводк",
            "маржин",
            "финанс",
            "касс",
            "деньг",
            "радар",
            "в работе",
            "сдать",
            "дедлайн",
            "открыт",
            "кто ",
            "покажи",
            "сколько",
        )
        if not any(k in lower for k in keys):
            return False
        # не перехватывать короткие ответы в чужих флоу
        if state.get("order_draft", {}).get(str(user.get("id"))):
            return False

    try:
        import growth_lib as growth

        html_out = growth.team_lead_html(q)
        if not html_out and force:
            html_out = growth.team_lead_grok(cfg, q)
        if not html_out:
            return False
        tg.send_message(cfg, chat_id, html_out, parse_mode="HTML", disable_preview=True)
        return True
    except Exception as e:
        print("teamlead", e, flush=True)
        if force:
            tg.send_message(cfg, chat_id, f"❌ тимлид: {html.escape(str(e)[:200])}")
            return True
        return False


def tick_order_reports(cfg: dict) -> None:
    """Авто-прогресс клиентам по in_progress раз в ~2.5 суток."""
    if cfg.get("paused"):
        return
    try:
        import growth_lib as growth

        due = growth.due_interim_reports()
    except Exception as e:
        print("interim due", e, flush=True)
        return
    for item in due[:3]:  # не спамить пачкой
        try:
            uid = int(item.get("user_id") or 0)
            if not uid:
                continue
            body = growth.build_interim_report(cfg, item)
            tg.send_message(cfg, uid, body, parse_mode="HTML", disable_preview=True)
            growth.mark_report_sent(item)
            # клиент может ответить
            try:
                st = load_state()
                st.setdefault("client_reply_ctx", {})[str(uid)] = {
                    "order_id": str(item.get("id") or ""),
                    "ts": int(time.time()),
                }
                save_state(st)
            except Exception:
                pass
            print("interim report", item.get("id"), "->", uid, flush=True)
        except Exception as e:
            print("interim send", item.get("id"), e, flush=True)


_last_finance_digest = 0.0


def tick_finance_digest(cfg: dict) -> None:
    """Раз в ~сутки — короткий радар владельцу."""
    global _last_finance_digest
    now = time.time()
    if now - _last_finance_digest < 20 * 3600:
        return
    # только если есть открытые заказы
    try:
        work = [
            x
            for x in orders.list_orders(limit=40)
            if str(x.get("status") or "") in ("new", "accepted", "in_progress")
        ]
        if not work:
            _last_finance_digest = now
            return
        import growth_lib as growth

        body = "📬 <b>Ежедневный радар</b>\n\n" + growth.finance_radar_html()
        oid = owner_chat_id(cfg)
        if oid:
            tg.send_message(cfg, oid, body, parse_mode="HTML", disable_preview=True)
        _last_finance_digest = now
    except Exception as e:
        print("finance digest", e, flush=True)


def handle_owner_system(cfg: dict, state: dict, msg: dict) -> bool:
    """
    Служебные команды владельца — САМЫЕ ПЕРВЫЕ (до заказов/тикетов),
    чтобы /redeploy не съедался черновиком ТЗ.
    """
    chat = msg.get("chat") or {}
    if chat.get("type") != "private":
        return False
    user = msg.get("from") or {}
    if not is_owner(cfg, user):
        return False
    text = (msg.get("text") or "").strip()
    # тимлид: и /команды, и естественный язык
    if handle_owner_teamlead(cfg, state, msg):
        return True
    if not text.startswith("/"):
        return False
    chat_id = chat.get("id")
    lower = text.lower()
    cmd = lower.split()[0].split("@")[0]

    uid = int(user.get("id") or 0)

    if cmd in ("/ref", "/реф", "/invite"):
        import growth_lib as growth

        code = growth.ref_code_for_user(uid)
        bot_un = "DirectorVaggobot"
        try:
            me = tg.get_me(cfg)
            if me.get("username"):
                bot_un = me["username"]
        except Exception:
            pass
        link = f"https://t.me/{bot_un}?start={code}"
        tg.send_message(
            cfg,
            chat_id,
            f"🔗 <b>Реферальная ссылка</b>\n\n"
            f"<code>{html.escape(link)}</code>\n\n"
            f"Друг жмёт Start → его первый оплаченный заказ "
            f"даёт тебе <b>+{growth.REF_BONUS_RUB} ₽</b> на баланс.\n"
            f"/balance",
            parse_mode="HTML",
            disable_preview=True,
        )
        return True

    if cmd in ("/contract", "/dogovor", "/docs_order") and len(text.split()) >= 2:
        oid = text.split()[1].strip()
        item = orders.get_order(oid)
        if not item:
            tg.send_message(cfg, chat_id, "Заказ не найден")
            return True
        try:
            import docs_lib

            cpath, apath = docs_lib.write_contract_files(item)
            tg.send_document(cfg, chat_id, str(cpath), caption=f"Договор {oid}")
            tg.send_document(cfg, chat_id, str(apath), caption=f"Акт {oid}")
        except Exception as e:
            tg.send_message(cfg, chat_id, f"❌ {html.escape(str(e)[:200])}")
        return True

    if cmd in ("/ping", "/alive", "/ver", "/version", "/health", "/diag"):
        import os as _os

        ui_delete_user_message(cfg, msg)
        # force seed if empty
        try:
            act0 = gw.get_active(state)
            if not act0 or gw.entry_count(act0, complete_only=True) == 0:
                gw.apply_restore_seed(force=True)
                state = load_state()
        except Exception:
            pass
        act = gw.get_active(state)
        gw_line = "🎁 розыгрыш: нет — жми /gwrestore"
        if act:
            mid_ch = act.get("channel_message_id") or "—"
            gw_line = (
                f"🎁 complete: <b>{gw.entry_count(act, complete_only=True)}</b> · "
                f"всего: {gw.entry_count(act, complete_only=False)}\n"
                f"id <code>{html.escape(str(act.get('id')))}</code> · пост {mid_ch}"
            )
        host_mode = str(cfg.get("bot_host_mode") or "local")
        brain_line = "brain: ?"
        br_line = "bridge: off"
        try:
            from content import _bridge_url, brain_status

            bru = _bridge_url(cfg) or ""
            bst = brain_status(cfg, use_cache=False, probe_ollama=False)
            active = str(bst.get("active") or "—")
            src = str(bst.get("grok_source") or "—")
            model = str(bst.get("grok_model") or "—")
            brain_line = (
                f"brain: <b>{html.escape(active)}</b> · src <code>{html.escape(src)}</code>\n"
                f"model: <code>{html.escape(model)}</code>"
            )
            if bst.get("hint"):
                brain_line += f"\n💡 {html.escape(str(bst.get('hint'))[:100])}"
            if bru:
                try:
                    import requests as _rq

                    sec = (cfg.get("grok_bridge_secret") or "").strip()
                    hdrs = {"X-Bridge-Secret": sec} if sec else {}
                    hr = _rq.get(f"{bru.rstrip('/')}/health", headers=hdrs, timeout=6)
                    ok = hr.ok and "ok" in (hr.text or "").lower()
                    br_line = (
                        f"bridge: {'✅' if ok else '❌ http '+str(hr.status_code)}\n"
                        f"<code>{html.escape(bru[:56])}</code>"
                    )
                except Exception as be:
                    br_line = (
                        f"bridge: ❌ {html.escape(str(be)[:80])}\n"
                        f"<code>{html.escape(bru[:56])}</code>\n"
                        "ПК: start_grok_bridge.bat"
                    )
            else:
                br_line = "bridge: off (API key / session / none)"
        except Exception as e:
            br_line = f"bridge: ❌ {html.escape(str(e)[:100])}"

        _owner_panel(
            cfg,
            state,
            chat_id,
            None,
            uid,
            f"pong ✅ · <b>Vaggo {html.escape(BOT_CODE_VERSION)}</b>\n"
            f"ver: <code>{html.escape(BOT_CODE_VERSION)}</code>\n"
            f"host: <code>{html.escape(host_mode)}</code>\n"
            f"BOT_ID: <code>{html.escape((_os.environ.get('BOT_ID') or 'local')[:40])}</code>\n\n"
            f"{brain_line}\n"
            f"{br_line}\n\n"
            f"{gw_line}\n\n"
            f"🏠 /start · 🎁 /gstatus · ♻️ /gwrestore\n"
            f"🔄 /redeploy · 📌 /gfixkb",
            {
                "inline_keyboard": [
                    [{"text": "🏠 Меню", "callback_data": "menu:home"}],
                    [
                        {"text": "🎁 Розыгрыш", "callback_data": "menu:giveaway"},
                        {"text": "♻️ Restore", "callback_data": "menu:gwrestore"},
                    ],
                    [{"text": "📌 Кнопки на пост", "callback_data": "menu:gfixkb"}],
                ]
            },
            force_new=False,
        )
        return True

    if cmd in ("/gwrestore", "/restore_gw", "/fixgiveaway"):
        ui_delete_user_message(cfg, msg)
        try:
            res = gw.apply_restore_seed(force=True)
            body = (
                "♻️ <b>Restore розыгрыша</b>\n\n"
                f"{html.escape(str(res.get('message') or res))}\n"
                f"active: <code>{html.escape(str(res.get('active_id') or '—'))}</code>\n"
                f"complete: <b>{res.get('complete') or 0}</b>\n"
                f"started: {res.get('started') or 0}\n"
                f"prize: {html.escape(str(res.get('prize') or '—')[:80])}\n"
            )
            mid_ch = res.get("channel_message_id")
            if mid_ch:
                body += f"\nпост: https://t.me/Vaggo01/{mid_ch}"
            tg.send_message(cfg, chat_id, body, parse_mode="HTML", disable_preview=True)
        except Exception as e:
            tg.send_message(cfg, chat_id, f"❌ gwrestore: {html.escape(str(e)[:300])}")
        return True

    if cmd in ("/clean", "/clearchat", "/purge"):
        ui_delete_user_message(cfg, msg)
        # 1) новое меню
        mid = ui_edit_or_send(
            cfg,
            chat_id,
            owner_home_html() + "\n\n🧹 <i>Чищу чат…</i>",
            reply_markup=main_menu_keyboard(),
            state=state,
            uid=uid,
            store_key="owner_ui_msg",
        )
        # 2) deep purge: tracked + mid-1…mid-120
        n = ui_clean_private(
            cfg,
            chat_id,
            uid,
            keep_mids=[int(mid)] if mid else None,
            deep=True,
        )
        if mid:
            state.setdefault("owner_ui_msg", {})[str(uid)] = int(mid)
            _priv_track(cfg, chat_id, uid, int(mid), keep=1)
            save_state(state)
            # 3) финальный текст на том же окне
            try:
                tg.edit_message_text(
                    cfg,
                    chat_id,
                    int(mid),
                    owner_home_html()
                    + f"\n\n🧹 <b>Готово</b> · убрано ~{n} сообщ. бота\n"
                    f"<i>Старше ~48ч Telegram не даёт удалять боту — смахни вручную.</i>",
                    parse_mode="HTML",
                    reply_markup=main_menu_keyboard(),
                )
            except Exception:
                pass
        print("clean deleted", n, flush=True)
        return True

    if cmd in ("/ordercancel", "/ocancel", "/cancel_order"):
        try:
            state.setdefault("order_draft", {}).pop(str(uid), None)
            save_state(state)
        except Exception:
            pass
        ui_delete_user_message(cfg, msg)
        ui_edit_or_send(
            cfg,
            chat_id,
            "🗑 Черновик заказа сброшен.",
            reply_markup=main_menu_keyboard(),
            state=state,
            uid=uid,
            store_key="owner_ui_msg",
        )
        return True

    if cmd in ("/redeploy", "/deploy", "/update"):
        ui_delete_user_message(cfg, msg)
        ui_edit_or_send(
            cfg,
            chat_id,
            "⏳ Тяну код с GitHub…",
            state=state,
            uid=uid,
            store_key="owner_ui_msg",
        )
        try:
            import deploy_lib

            res = deploy_lib.redeploy_now(restart=True)
            pull = res.get("pull") or {}
            files = ", ".join((pull.get("files") or [])[:14])
            rst = res.get("restart") or {}
            if res.get("pull_error"):
                ui_edit_or_send(
                    cfg,
                    chat_id,
                    f"❌ Pull: {html.escape(str(res['pull_error'])[:400])}",
                    state=state,
                    uid=uid,
                    store_key="owner_ui_msg",
                )
                return True
            ui_edit_or_send(
                cfg,
                chat_id,
                "🚀 <b>Redeploy</b>\n\n"
                f"ver: <code>{html.escape(BOT_CODE_VERSION)}</code>\n"
                f"sha: <code>{html.escape(str(res.get('remote_sha') or pull.get('sha') or '—'))}</code>\n"
                f"files: {int(pull.get('count') or 0)}\n"
                f"<code>{html.escape(files[:400])}</code>\n\n"
                f"restart: {html.escape(str(rst.get('message') or rst.get('method') or rst.get('error') or rst)[:200])}\n"
                f"{'⏳ Перезапуск…' if rst.get('will_exit') else ''}",
                state=state,
                uid=uid,
                store_key="owner_ui_msg",
            )
        except Exception as e:
            ui_edit_or_send(
                cfg,
                chat_id,
                f"❌ redeploy: {html.escape(str(e)[:400])}",
                state=state,
                uid=uid,
                store_key="owner_ui_msg",
            )
        return True

    if cmd in ("/deploy_status", "/depstatus"):
        ui_delete_user_message(cfg, msg)
        try:
            import deploy_lib

            need, remote, local = deploy_lib.needs_update()
            ui_edit_or_send(
                cfg,
                chat_id,
                "📦 <b>Deploy</b>\n\n"
                f"ver: <code>{html.escape(BOT_CODE_VERSION)}</code>\n"
                f"local: <code>{html.escape((local or '—')[:12])}</code>\n"
                f"remote: <code>{html.escape((remote or '—')[:12])}</code>\n"
                f"need update: <b>{'YES' if need else 'no'}</b>\n\n"
                "/redeploy · /clean",
                reply_markup=main_menu_keyboard(),
                state=state,
                uid=uid,
                store_key="owner_ui_msg",
            )
        except Exception as e:
            ui_edit_or_send(
                cfg,
                chat_id,
                f"❌ {html.escape(str(e)[:300])}",
                state=state,
                uid=uid,
                store_key="owner_ui_msg",
            )
        return True

    return False


def handle_orders_private(cfg: dict, state: dict, msg: dict) -> bool:
    """Заказы: приём заявок + сдача. Результат всегда отдельно от канала/бота."""
    chat = msg.get("chat") or {}
    if chat.get("type") != "private":
        return False
    user = msg.get("from") or {}
    uid = int(user.get("id") or 0)
    if not uid:
        return False
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()
    uname = user.get("username") or ""
    name = user.get("first_name") or uname or str(uid)
    owner = is_owner(cfg, user)
    lower = text.lower()
    cmd = lower.split()[0].split("@")[0] if lower.startswith("/") else ""

    # owner: правка ТЗ
    if owner:
        aetz = (state.get("await_owner_edit_tz") or {}).get(str(uid))
        if aetz and text and not text.startswith("/"):
            oid = str(aetz.get("order_id") or "")
            state.setdefault("await_owner_edit_tz", {}).pop(str(uid), None)
            save_state(state)
            item = orders.get_order(oid)
            if not item:
                tg.send_message(cfg, chat_id, "Заказ не найден")
                return True
            item["brief"] = text.strip()[:2000]
            orders.save_order(item)
            try:
                orders.append_event(item, "tz_edit", "ТЗ обновлено владельцем")
            except Exception:
                pass
            item = orders.get_order(oid) or item
            try:
                _push_client_order_card(
                    cfg,
                    item,
                    delta=f"✏️ ТЗ по заказу <code>{html.escape(oid)}</code> обновлено.",
                )
            except Exception:
                pass
            tg.send_message(
                cfg,
                chat_id,
                f"✅ ТЗ сохранено · <code>{html.escape(oid)}</code>",
                parse_mode="HTML",
                reply_markup=orders.owner_order_hub_keyboard(
                    oid, user_id=int(item.get("user_id") or 0) or None
                ),
            )
            return True
        if aetz and cmd in ("/cancel", "/отмена"):
            state.setdefault("await_owner_edit_tz", {}).pop(str(uid), None)
            save_state(state)
            tg.send_message(cfg, chat_id, "Правка ТЗ отменена.")
            return True

        # owner: сообщение клиенту
        amsg = (state.get("await_owner_msg_client") or {}).get(str(uid))
        if amsg and text and not text.startswith("/"):
            oid = str(amsg.get("order_id") or "")
            cid = int(amsg.get("client_id") or 0)
            state.setdefault("await_owner_msg_client", {}).pop(str(uid), None)
            save_state(state)
            if not cid:
                tg.send_message(cfg, chat_id, "Нет client_id")
                return True
            try:
                # force_reply — клиент жмёт «ответить» на сообщение
                payload = {
                    "chat_id": cid,
                    "text": (
                        f"Сообщение по заказу <code>{html.escape(oid)}</code>\n\n"
                        f"{html.escape(text[:2800])}\n\n"
                        f"<i>Ответь на это сообщение — я передам исполнителю.</i>"
                    ),
                    "parse_mode": "HTML",
                    "reply_markup": json.dumps(
                        {
                            "force_reply": True,
                            "input_field_placeholder": "Ваш ответ…",
                            "selective": False,
                        }
                    ),
                }
                res = tg.api(cfg, "sendMessage", data=payload)
                mid_sent = None
                if isinstance(res, dict):
                    mid_sent = res.get("message_id") or (res.get("result") or {}).get(
                        "message_id"
                    )
                # контекст: любой следующий ответ клиента (reply или просто текст)
                state.setdefault("client_reply_ctx", {})[str(cid)] = {
                    "order_id": oid,
                    "owner_msg_id": int(mid_sent) if mid_sent else None,
                    "ts": int(time.time()),
                }
                save_state(state)
                # подтверждение владельцу — edit главного окна
                ui_edit_or_send(
                    cfg,
                    chat_id,
                    f"✅ Ушло клиенту\nзаказ <code>{html.escape(oid)}</code>\n\n"
                    f"Он может <b>ответить</b> — придёт сюда.",
                    reply_markup=orders.owner_order_hub_keyboard(oid, user_id=cid),
                    state=state,
                    uid=uid,
                    store_key="owner_ui_msg",
                )
                item = orders.get_order(oid)
                if item:
                    orders.append_event(item, "msg", "сообщение клиенту")
            except Exception as e:
                tg.send_message(
                    cfg, chat_id, f"❌ Не смог написать: {html.escape(str(e)[:200])}"
                )
            return True
        if amsg and cmd in ("/cancel", "/отмена"):
            state.setdefault("await_owner_msg_client", {}).pop(str(uid), None)
            save_state(state)
            tg.send_message(cfg, chat_id, "Сообщение отменено.")
            return True

    # клиент отвечает на сообщение владельца (reply или диалог)
    if not owner and text and not text.startswith("/"):
        # не перехватывать опрос ТЗ / confirm
        _dr = (state.get("order_draft") or {}).get(str(uid)) or {}
        in_order_flow = bool(_dr.get("kind") and not _dr.get("await_confirm")) or bool(
            _dr.get("await_confirm")
        )
        ctx = (state.get("client_reply_ctx") or {}).get(str(uid))
        rt = msg.get("reply_to_message") or {}
        is_reply_to_bot = bool(rt.get("message_id"))
        use_ctx = False
        if ctx and not in_order_flow:
            age = int(time.time()) - int(ctx.get("ts") or 0)
            if age < 7 * 86400:
                use_ctx = True
            else:
                state.setdefault("client_reply_ctx", {}).pop(str(uid), None)
                save_state(state)
                ctx = None
        # reply всегда можно; free-text — только вне оформления заказа
        if (use_ctx and not in_order_flow) or is_reply_to_bot:
            oid = str((ctx or {}).get("order_id") or "")
            if not oid and is_reply_to_bot:
                # попробуем вытащить id из текста, на который ответили
                rtxt = (rt.get("text") or rt.get("caption") or "")
                m = re.search(r"заказу?\s+([a-f0-9]{8,12})", rtxt, re.I)
                if not m:
                    m = re.search(r"\b([a-f0-9]{10})\b", rtxt)
                if m:
                    oid = m.group(1)
            if not oid:
                # открытый заказ клиента
                pend = orders.user_pending_order(uid)
                if pend:
                    oid = str(pend.get("id") or "")
            who = f"@{uname}" if uname else name
            notify_owner(
                cfg,
                f"💬 <b>Ответ клиента</b>\n"
                f"{html.escape(str(who))} · <code>{uid}</code>\n"
                f"заказ <code>{html.escape(oid or '—')}</code>\n\n"
                f"{html.escape(text[:1500])}",
                reply_markup={
                    "inline_keyboard": (
                        [
                            [
                                {
                                    "text": "📂 Заказ",
                                    "callback_data": f"ord:o:open:{oid}",
                                },
                                {
                                    "text": "💬 Ответить",
                                    "callback_data": f"ord:o:msg:{oid}",
                                },
                            ]
                        ]
                        if oid
                        else [[{"text": "🏠 Пульт", "callback_data": "menu:home"}]]
                    )
                },
            )
            # не сбрасываем ctx — можно переписываться
            if oid and not ctx:
                state.setdefault("client_reply_ctx", {})[str(uid)] = {
                    "order_id": oid,
                    "ts": int(time.time()),
                }
                save_state(state)
            elif ctx:
                ctx["ts"] = int(time.time())
                state.setdefault("client_reply_ctx", {})[str(uid)] = ctx
                save_state(state)
            # коротко, без спама если уже в переписке
            last_ack = int((ctx or {}).get("last_ack") or 0)
            if int(time.time()) - last_ack > 120:
                tg.send_message(cfg, chat_id, "✅ Принял.", parse_mode="HTML")
                if ctx is not None:
                    ctx["last_ack"] = int(time.time())
                    state.setdefault("client_reply_ctx", {})[str(uid)] = ctx
                    save_state(state)
            return True

    # вопрос по проекту (с карточки заказа)
    aq = (state.get("await_order_question") or {}).get(str(uid))
    if aq and text and not text.startswith("/"):
        oid = str(aq.get("order_id") or "")
        state.setdefault("await_order_question", {}).pop(str(uid), None)
        # оставляем reply-ctx для дальнейшей переписки
        state.setdefault("client_reply_ctx", {})[str(uid)] = {
            "order_id": oid,
            "ts": int(time.time()),
        }
        save_state(state)
        item = orders.get_order(oid) if oid else None
        tg.send_message(
            cfg,
            chat_id,
            f"✅ Отправил. Можешь писать дальше — отвечу здесь.",
            parse_mode="HTML",
        )
        try:
            who = f"@{uname}" if uname else name
            notify_owner(
                cfg,
                f"💬 <b>Вопрос по заказу</b> <code>{html.escape(oid)}</code>\n"
                f"{html.escape(str(who))} · <code>{uid}</code>\n\n"
                f"{html.escape(text[:1200])}",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {
                                "text": "📂 Заказ",
                                "callback_data": f"ord:o:open:{oid}",
                            },
                            {
                                "text": "💬 Ответить",
                                "callback_data": f"ord:o:msg:{oid}",
                            },
                        ]
                    ]
                },
            )
        except Exception:
            pass
        return True
    if aq and cmd in ("/cancel", "/отмена"):
        state.setdefault("await_order_question", {}).pop(str(uid), None)
        save_state(state)
        tg.send_message(cfg, chat_id, "Ок, отменено.")
        return True

    # любые slash-команды — не трогаем (иначе /redeploy «проглатывается»)
    if text.startswith("/"):
        # кроме order-команд ниже
        if cmd not in (
            "/order",
            "/заказ",
            "/orders",
            "/заказы",
            "/myorders",
            "/мои",
            "/история",
            "/myorder",
            "/prices",
            "/odeliver",
        ):
            return False

    # --- owner: список / выдача ---
    if owner and cmd in ("/orders", "/заказы"):
        items = orders.list_orders(limit=15)
        ui_edit_or_send(
            cfg,
            chat_id,
            orders.format_owner_orders_list(items),
            reply_markup=orders.owner_orders_list_keyboard(items),
            state=state,
            uid=uid,
            store_key="owner_ui_msg",
        )
        save_state(state)
        return True

    if owner and cmd == "/odeliver":
        arg = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
        if not arg:
            tg.send_message(cfg, chat_id, "Пример: /odeliver abc123\nПотом файл с подписью /odeliver abc123")
            return True
        oid = arg.split()[0]
        item = orders.get_order(oid)
        if not item:
            tg.send_message(cfg, chat_id, "Заказ не найден")
            return True
        state["await_order_deliver"] = oid
        save_state(state)
        tg.send_message(
            cfg,
            chat_id,
            f"Жду файл для заказа <code>{html.escape(oid)}</code>\n"
            f"Пришли документ/архив/фото (можно с подписью).",
            parse_mode="HTML",
        )
        return True

    if owner and state.get("await_order_deliver"):
        oid = state.get("await_order_deliver")
        item = orders.get_order(str(oid))
        # media?
        doc = msg.get("document")
        photos = msg.get("photo") or []
        file_id = None
        kind = "document"
        if doc:
            file_id = doc.get("file_id")
            kind = "document"
        elif photos:
            file_id = photos[-1].get("file_id")
            kind = "photo"
        elif msg.get("video"):
            file_id = (msg.get("video") or {}).get("file_id")
            kind = "video"
        if file_id and item:
            cap = (msg.get("caption") or text or "Готово по заказу").strip()
            try:
                client_id = int(item["user_id"])
                if kind == "photo":
                    tg.api(
                        cfg,
                        "sendPhoto",
                        data={
                            "chat_id": client_id,
                            "photo": file_id,
                            "caption": f"✅ Заказ <code>{oid}</code>\n{cap}"[:1024],
                            "parse_mode": "HTML",
                        },
                    )
                elif kind == "video":
                    tg.api(
                        cfg,
                        "sendVideo",
                        data={
                            "chat_id": client_id,
                            "video": file_id,
                            "caption": f"✅ Заказ <code>{oid}</code>\n{cap}"[:1024],
                            "parse_mode": "HTML",
                        },
                    )
                else:
                    tg.api(
                        cfg,
                        "sendDocument",
                        data={
                            "chat_id": client_id,
                            "document": file_id,
                            "caption": f"✅ Заказ <code>{oid}</code>\n{cap}"[:1024],
                            "parse_mode": "HTML",
                        },
                    )
                item["status"] = "done"
                item["result_file_id"] = file_id
                item["deliver_note"] = cap[:500]
                orders.save_order(item)
                state.pop("await_order_deliver", None)
                save_state(state)
                tg.send_message(cfg, chat_id, f"✅ Файл ушёл клиенту · заказ {oid} = done")
            except Exception as e:
                tg.send_message(cfg, chat_id, f"❌ Не смог отправить клиенту: {html.escape(str(e)[:200])}")
            return True

    # --- user: history ---
    if cmd in ("/myorders", "/мои", "/история", "/myorder"):
        ui_edit_or_send(
            cfg,
            chat_id,
            f"💰 Баланс: <b>{bal.get_balance(uid)}</b> ₽"
            + (
                " · /topup\n\n"
                if bal.topup_enabled(cfg)
                else "\n<i>Пополнение временно выкл.</i>\n\n"
            )
            + orders.format_user_history(uid),
            reply_markup={
                "inline_keyboard": [
                    [{"text": "🔄 Обновить", "callback_data": "ord:mine"}],
                    [{"text": "💰 Баланс", "callback_data": "bal:show"}],
                    [{"text": "🛠 Новый заказ", "callback_data": "ord:restart"}],
                ]
            },
            state=state,
            uid=uid,
            store_key="order_ui_msg",
        )
        save_state(state)
        return True

    # --- user: start order ---
    if cmd in ("/order", "/заказ") or (cmd == "/orders" and not owner):
        # убрать /order из чата
        ui_delete_user_message(cfg, msg)
        _order_start_msg(cfg, chat_id, state=state, uid=uid)
        save_state(state)
        return True

    # await TZ questionnaire (or legacy confirm)
    drafts = state.setdefault("order_draft", {})
    draft = drafts.get(str(uid))
    if draft and draft.get("kind"):
        # фото/док как референс на шаге «пример»
        if (
            draft.get("tz_step") == "example"
            and not draft.get("await_confirm")
            and (msg.get("photo") or msg.get("document"))
        ):
            file_id = None
            if msg.get("photo"):
                file_id = (msg.get("photo") or [])[-1].get("file_id")
                label = "фото-референс"
            else:
                file_id = (msg.get("document") or {}).get("file_id")
                label = "файл-референс"
            cap = (msg.get("caption") or text or "").strip()
            ans = f"{label}" + (f": {cap}" if cap else "")
            if file_id:
                ans += f" [file_id:{file_id[:40]}…]"
            text = ans if len(ans) >= 2 else "референс во вложении"
        elif msg.get("photo") or msg.get("document") or msg.get("video"):
            return False
        if text.startswith("/"):
            return False
        # уже ждём только подтверждение кнопкой
        if draft.get("await_confirm"):
            ui_delete_user_message(cfg, msg)
            ui_edit_or_send(
                cfg,
                chat_id,
                "Жми кнопку <b>«Всё верно · отправить»</b> или <b>«Заново»</b> в сообщении выше.",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {
                                "text": "✅ Всё верно · отправить",
                                "callback_data": "ord:commit",
                            },
                            {"text": "✏️ Заново", "callback_data": "ord:restart"},
                        ],
                        [{"text": "❌ Отмена", "callback_data": "ord:cancel"}],
                    ]
                },
                state=state,
                uid=uid,
                store_key="order_ui_msg",
            )
            save_state(state)
            return True
        # AI-уточнения после опроса
        if draft.get("tz_step") == "ai_clarify":
            if not (text or "").strip():
                return True
            if apply_tz_moderation(
                cfg,
                state,
                uid=uid,
                uname=uname,
                name=name,
                brief=text,
                chat_id=chat_id,
            ):
                ui_delete_user_message(cfg, msg)
                return True
            ui_delete_user_message(cfg, msg)
            kind = draft.get("kind")
            answers = dict(draft.get("answers") or {})
            answers["ai_clarify"] = text.strip()[:1500]
            _order_run_ai_review(
                cfg,
                state,
                chat_id,
                uid,
                kind=str(kind),
                answers=answers,
                uname=uname,
                name=name,
                extra_note=text.strip()[:1500],
            )
            return True
        if not (text or "").strip():
            return True
        step_id = draft.get("tz_step") or "what"
        if step_id == "ai_clarify":
            return True
        step = orders.tz_step(step_id)
        ok, err = orders.validate_step_answer(step, text)
        if not ok:
            ui_delete_user_message(cfg, msg)
            ui_edit_or_send(
                cfg,
                chat_id,
                f"⚠️ {html.escape(err)}\n\n" + str(step.get("ask") or ""),
                reply_markup=orders.order_step_keyboard(),
                state=state,
                uid=uid,
                store_key="order_ui_msg",
            )
            save_state(state)
            return True
        if apply_tz_moderation(
            cfg,
            state,
            uid=uid,
            uname=uname,
            name=name,
            brief=text,
            chat_id=chat_id,
        ):
            ui_delete_user_message(cfg, msg)
            return True
        answers = dict(draft.get("answers") or {})
        answers[str(step.get("key") or step_id)] = text.strip()
        ui_delete_user_message(cfg, msg)
        _order_advance_after_answer(
            cfg,
            state,
            chat_id,
            uid,
            draft=draft,
            step=step,
            step_id=step_id,
            answers=answers,
            uname=uname,
            name=name,
        )
        return True

    # soft: keywords from non-owner — НЕ трогать, если уже идёт опрос/черновик
    if not owner and text and not text.startswith("/"):
        soft = ("заказ", "сделать сайт", "сделать бота", "хочу сайт", "разработ", "заказать")
        tl = text.lower()
        if any(s in tl for s in soft) and not drafts.get(str(uid)):
            ui_edit_or_send(
                cfg,
                chat_id,
                "Похоже на заказ.\n\nЖми кнопку или /order — выберем тип и оценим.",
                reply_markup={
                    "inline_keyboard": [
                        [{"text": "🛠 Оформить заказ", "callback_data": "ord:restart"}],
                        [{"text": "📦 Мои заказы", "callback_data": "ord:mine"}],
                    ]
                },
                state=state,
                uid=uid,
            )
            return True

    return False


def _push_client_order_card(cfg: dict, item: dict, *, delta: str | None = None) -> None:
    """
    Живая карточка: edit старого сообщения, иначе send + запомнить mid.
    Опционально короткий delta-пуш.
    """
    uid = int(item.get("user_id") or 0)
    if not uid:
        return
    body = orders.format_order_card(item)
    kb = orders.user_order_actions_keyboard(str(item.get("id")))
    card_chat = item.get("client_card_chat_id") or uid
    card_mid = item.get("client_card_message_id")
    edited = False
    if card_mid:
        try:
            tg.edit_message_text(
                cfg,
                card_chat,
                int(card_mid),
                body,
                parse_mode="HTML",
                reply_markup=kb,
                disable_preview=True,
            )
            edited = True
        except Exception as e:
            print("order card edit", e, flush=True)
    if not edited:
        try:
            res = tg.send_message(
                cfg, uid, body, parse_mode="HTML", reply_markup=kb, disable_preview=True
            )
            mid = (res or {}).get("message_id")
            if mid:
                orders.set_client_card(item, chat_id=uid, message_id=int(mid))
        except Exception as e:
            print("order card send", e, flush=True)
    if delta:
        try:
            tg.send_message(cfg, uid, delta, parse_mode="HTML", disable_preview=True)
        except Exception:
            pass


def _owner_set_order_status(cfg: dict, state: dict, cq: dict, oid: str, status: str) -> bool:
    """Владелец: сменить статус + уведомить клиента. Всегда load свежий state."""
    user = cq.get("from") or {}
    if not is_owner(cfg, user):
        tg.answer_callback(cfg, cq["id"], "Только владелец", show_alert=True)
        return True
    item = orders.get_order(oid)
    if not item:
        # диагностика
        try:
            all_ids = list((orders.load_orders().get("items") or {}).keys())
        except Exception:
            all_ids = []
        tg.answer_callback(
            cfg,
            cq["id"],
            f"Не найден {oid[:8]}… (в базе {len(all_ids)})",
            show_alert=True,
        )
        return True
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    item["status"] = status
    orders.save_order(item)
    # timeline
    ev_map = {
        "in_progress": "взят в работу",
        "done": "готов · сдача",
        "cancelled": "отменён",
        "accepted": "принят",
        "new": "новый",
    }
    try:
        orders.append_event(item, status, ev_map.get(status, status))
        item = orders.get_order(oid) or item
    except Exception:
        pass
    client_msgs = {
        "in_progress": (
            f"🛠 <b>#{html.escape(oid[:8])}</b> — в работе.\n"
            f"Карточка обновлена. Вопросы — кнопкой «Вопрос по проекту»."
        ),
        "done": (
            f"✔️ <b>#{html.escape(oid[:8])}</b> — готово.\n"
            f"Файл/результат придёт отдельно. Прими или напиши правки."
        ),
        "cancelled": (
            f"❌ <b>#{html.escape(oid[:8])}</b> отменён.\n"
            f"Новый: /order"
        ),
    }
    if status == "in_progress":
        tg.answer_callback(cfg, cq["id"], "В работе", show_alert=False)
    elif status == "done":
        tg.answer_callback(cfg, cq["id"], "Готово — пришли файл", show_alert=False)
        state["await_order_deliver"] = oid
        save_state(state)
        if chat_id:
            tg.send_message(
                cfg,
                chat_id,
                f"Пришли файл для <code>{html.escape(oid)}</code>\n"
                f"или /odeliver {html.escape(oid)}",
                parse_mode="HTML",
            )
    elif status == "cancelled":
        tg.answer_callback(cfg, cq["id"], "Отменён", show_alert=False)
    else:
        tg.answer_callback(cfg, cq["id"], status)
    # живая карточка + короткий delta
    try:
        _push_client_order_card(cfg, item, delta=client_msgs.get(status))
    except Exception as e:
        print("push client card", e, flush=True)
    # при сдаче — акт + черновик кейса владельцу
    if status == "done":
        try:
            import docs_lib

            _c, apath = docs_lib.write_contract_files(item)
            uid_c = int(item.get("user_id") or 0)
            if uid_c:
                tg.send_document(
                    cfg,
                    uid_c,
                    str(apath),
                    caption=f"📋 <b>Акт выполненных работ</b>\nзаказ <code>{html.escape(oid)}</code>",
                )
        except Exception as e:
            print("act send", e, flush=True)
        try:
            import docs_lib

            case = docs_lib.build_case_draft(item)
            notify_owner(
                cfg,
                case
                + "\n\n<i>Опубликовать? Отредактируй и кинь /draft или в канал вручную.</i>",
            )
        except Exception as e:
            print("case draft", e, flush=True)
    # владельцу — edit карточки, без лишнего «OK» в чат
    if chat_id and status != "done":
        try:
            item2 = orders.get_order(oid) or item
            ui_edit_or_send(
                cfg,
                chat_id,
                orders.format_order_card(item2, for_owner=True),
                reply_markup=orders.owner_order_hub_keyboard(
                    oid, user_id=int(item2.get("user_id") or 0) or None
                ),
                state=state,
                uid=int((cq.get("from") or {}).get("id") or 0) or None,
                store_key="owner_ui_msg",
            )
            save_state(state)
        except Exception as e:
            print("owner hub refresh", e, flush=True)
    return True


def handle_orders_callback(cfg: dict, state: dict, cq: dict) -> bool:
    data = cq.get("data") or ""
    if not data.startswith("ord:"):
        return False
    user = cq.get("from") or {}
    uid = int(user.get("id") or 0)
    uname = user.get("username") or ""
    name = user.get("first_name") or uname or str(uid)
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "noop":
        tg.answer_callback(cfg, cq["id"], "Выбери услугу ниже")
        return True

    # owner CRM: ord:o:open|tz|editz|msg|docs|docc|doca|case|docall|list|clients|prof
    if action == "o" and len(parts) >= 3 and is_owner(cfg, user):
        sub = parts[2]
        cb_mid = msg.get("message_id")

        def _panel(body: str, kb: dict) -> None:
            if not chat_id:
                return
            ui_edit_or_send(
                cfg,
                chat_id,
                body,
                reply_markup=kb,
                message_id=cb_mid,
                state=state,
                uid=uid,
                store_key="owner_ui_msg",
            )
            save_state(state)

        if sub == "list":
            items = orders.list_orders(limit=15)
            tg.answer_callback(cfg, cq["id"], "Заказы")
            _panel(
                orders.format_owner_orders_list(items),
                orders.owner_orders_list_keyboard(items),
            )
            return True

        if sub == "clients":
            clients = orders.list_clients(limit=20)
            tg.answer_callback(cfg, cq["id"], "Клиенты")
            body = (
                f"👥 <b>Клиенты</b> · {len(clients)}\n"
                f"{'━' * 16}\n"
                f"Профиль: заказы · баланс · написать"
            )
            _panel(body, orders.owner_clients_keyboard(clients))
            return True

        if sub == "prof" and len(parts) >= 4:
            try:
                cuid = int(parts[3])
            except ValueError:
                tg.answer_callback(cfg, cq["id"], "bad id", show_alert=True)
                return True
            tg.answer_callback(cfg, cq["id"], "Профиль")
            items = orders.list_user_orders(cuid, limit=20)
            _panel(
                orders.format_client_profile(cuid),
                orders.client_profile_keyboard(cuid, items),
            )
            return True

        if sub in (
            "open",
            "tz",
            "editz",
            "msg",
            "docs",
            "docc",
            "doca",
            "case",
            "docall",
        ) and len(parts) >= 4:
            oid = parts[3]
            item = orders.get_order(oid)
            if not item:
                tg.answer_callback(cfg, cq["id"], "Не найден", show_alert=True)
                return True
            cuid = int(item.get("user_id") or 0)

            if sub == "open":
                # сброс ожидания правки/сообщения
                state.setdefault("await_owner_edit_tz", {}).pop(str(uid), None)
                state.setdefault("await_owner_msg_client", {}).pop(str(uid), None)
                save_state(state)
                tg.answer_callback(cfg, cq["id"], "Заказ")
                _panel(
                    orders.format_order_card(item, for_owner=True),
                    orders.owner_order_hub_keyboard(oid, user_id=cuid or None),
                )
                return True

            if sub == "tz":
                tg.answer_callback(cfg, cq["id"], "ТЗ")
                _panel(
                    orders.format_tz_full(item),
                    {
                        "inline_keyboard": [
                            [
                                {
                                    "text": "✏️ Править ТЗ",
                                    "callback_data": f"ord:o:editz:{oid}",
                                }
                            ],
                            [
                                {
                                    "text": "« К заказу",
                                    "callback_data": f"ord:o:open:{oid}",
                                }
                            ],
                        ]
                    },
                )
                return True

            if sub == "editz":
                state.setdefault("await_owner_edit_tz", {})[str(uid)] = {
                    "order_id": oid,
                    "ts": int(time.time()),
                }
                save_state(state)
                tg.answer_callback(cfg, cq["id"], "Жду новое ТЗ")
                _panel(
                    f"✏️ <b>Правка ТЗ</b> <code>{html.escape(oid)}</code>\n\n"
                    f"Пришли <b>полным сообщением</b> новый текст ТЗ.\n"
                    f"/cancel — отмена\n\n"
                    f"<i>Сейчас:</i>\n"
                    f"{html.escape((item.get('brief') or '')[:900])}",
                    {
                        "inline_keyboard": [
                            [
                                {
                                    "text": "« Отмена",
                                    "callback_data": f"ord:o:open:{oid}",
                                }
                            ]
                        ]
                    },
                )
                return True

            if sub == "msg":
                state.setdefault("await_owner_msg_client", {})[str(uid)] = {
                    "order_id": oid,
                    "client_id": cuid,
                    "ts": int(time.time()),
                }
                save_state(state)
                tg.answer_callback(cfg, cq["id"], "Жду текст")
                who = item.get("username") or item.get("name") or cuid
                _panel(
                    f"💬 <b>Сообщение клиенту</b>\n"
                    f"заказ <code>{html.escape(oid)}</code> · {html.escape(str(who))}\n\n"
                    f"Напиши текст — уйдёт клиенту в личку.\n"
                    f"/cancel — отмена",
                    {
                        "inline_keyboard": [
                            [
                                {
                                    "text": "« Отмена",
                                    "callback_data": f"ord:o:open:{oid}",
                                }
                            ]
                        ]
                    },
                )
                return True

            if sub == "docs":
                tg.answer_callback(cfg, cq["id"], "Документы")
                _panel(
                    f"📄 <b>Документы</b> · <code>{html.escape(oid)}</code>\n\n"
                    f"Выбери стопку: договор · акт · кейс · всё сразу",
                    orders.owner_docs_keyboard(oid),
                )
                return True

            if sub in ("docc", "doca", "case", "docall"):
                tg.answer_callback(cfg, cq["id"], "Отправляю…")
                try:
                    import docs_lib

                    cpath, apath = docs_lib.write_contract_files(item)
                    if sub in ("docc", "docall") and chat_id:
                        tg.send_document(
                            cfg,
                            chat_id,
                            str(cpath),
                            caption=f"📄 Договор · {oid}",
                        )
                    if sub in ("doca", "docall") and chat_id:
                        tg.send_document(
                            cfg, chat_id, str(apath), caption=f"📋 Акт · {oid}"
                        )
                    if sub in ("case", "docall") and chat_id:
                        tg.send_message(
                            cfg,
                            chat_id,
                            docs_lib.build_case_draft(item),
                            parse_mode="HTML",
                        )
                except Exception as e:
                    if chat_id:
                        tg.send_message(
                            cfg, chat_id, f"❌ {html.escape(str(e)[:200])}"
                        )
                return True

        tg.answer_callback(cfg, cq["id"], "ok")
        return True

    # owner short: ord:w:ID / ord:d:ID / ord:x:ID
    if action in ("w", "d", "x") and len(parts) >= 3:
        oid = parts[2]
        status = {"w": "in_progress", "d": "done", "x": "cancelled"}[action]
        return _owner_set_order_status(cfg, state, cq, oid, status)

    # legacy: ord:own:work:ID
    if action == "own" and len(parts) >= 4:
        oid = parts[3]
        status = {"work": "in_progress", "done": "done", "cancel": "cancelled"}.get(parts[2])
        if status:
            return _owner_set_order_status(cfg, state, cq, oid, status)

    drafts = state.setdefault("order_draft", {})
    cb_mid = msg.get("message_id")  # правим это сообщение, не шлём новое

    if action == "mine":
        tg.answer_callback(cfg, cq["id"], "История")
        if chat_id:
            ui_edit_or_send(
                cfg,
                chat_id,
                orders.format_user_history(uid),
                reply_markup={
                    "inline_keyboard": [
                        [{"text": "🔄 Обновить", "callback_data": "ord:mine"}],
                        [{"text": "🛠 Новый заказ", "callback_data": "ord:restart"}],
                    ]
                },
                message_id=cb_mid,
                state=state,
                uid=uid,
                store_key="order_ui_msg",
            )
            save_state(state)
        return True

    if action == "status" and len(parts) >= 3:
        oid = parts[2]
        item = orders.get_order(oid)
        if not item or int(item.get("user_id") or 0) != uid:
            if not (item and is_owner(cfg, user)):
                tg.answer_callback(cfg, cq["id"], "Заказ не найден", show_alert=True)
                return True
        tg.answer_callback(cfg, cq["id"], orders.status_label(str(item.get("status"))))
        if chat_id:
            mid_out = ui_edit_or_send(
                cfg,
                chat_id,
                orders.format_order_card(item),
                reply_markup=orders.user_order_actions_keyboard(oid),
                message_id=cb_mid,
                state=state,
                uid=uid,
                store_key="order_ui_msg",
            )
            try:
                if mid_out:
                    orders.set_client_card(item, chat_id=int(chat_id), message_id=int(mid_out))
            except Exception:
                pass
            save_state(state)
        return True

    if action == "ask" and len(parts) >= 3:
        # вопрос по проекту → ждём текст, уйдёт в support/owner
        oid = parts[2]
        item = orders.get_order(oid)
        if not item or int(item.get("user_id") or 0) != uid:
            tg.answer_callback(cfg, cq["id"], "Заказ не найден", show_alert=True)
            return True
        state.setdefault("await_order_question", {})[str(uid)] = {
            "order_id": oid,
            "ts": int(time.time()),
        }
        save_state(state)
        tg.answer_callback(cfg, cq["id"], "Пиши вопрос")
        if chat_id:
            ui_edit_or_send(
                cfg,
                chat_id,
                f"💬 <b>Вопрос по заказу</b> <code>{html.escape(oid)}</code>\n\n"
                f"Напиши одним сообщением, что уточнить.\n"
                f"Ответим в личку.\n\n"
                f"<i>/cancel — отмена</i>",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {
                                "text": "« К карточке",
                                "callback_data": f"ord:status:{oid}",
                            }
                        ],
                        [{"text": "❌ Отмена", "callback_data": "ord:cancel"}],
                    ]
                },
                message_id=cb_mid,
                state=state,
                uid=uid,
                store_key="order_ui_msg",
            )
            save_state(state)
        return True

    if action == "docs" and len(parts) >= 3:
        oid = parts[2]
        item = orders.get_order(oid)
        if not item or (
            int(item.get("user_id") or 0) != uid and not is_owner(cfg, user)
        ):
            tg.answer_callback(cfg, cq["id"], "Заказ не найден", show_alert=True)
            return True
        tg.answer_callback(cfg, cq["id"], "Готовлю документы…")
        try:
            import docs_lib

            cpath, apath = docs_lib.write_contract_files(item)
            if chat_id:
                tg.send_document(
                    cfg,
                    chat_id,
                    str(cpath),
                    caption=f"📄 Договор-оферта · заказ <code>{html.escape(oid)}</code>",
                )
                tg.send_document(
                    cfg,
                    chat_id,
                    str(apath),
                    caption=f"📋 Акт · заказ <code>{html.escape(oid)}</code>\n"
                    f"<i>Акт актуален после сдачи; можно скачать заранее.</i>",
                )
            try:
                orders.append_event(item, "docs", "выданы договор и акт")
            except Exception:
                pass
        except Exception as e:
            if chat_id:
                tg.send_message(
                    cfg, chat_id, f"❌ Документы: {html.escape(str(e)[:200])}"
                )
        return True

    if action == "cancel":
        drafts.pop(str(uid), None)
        tg.answer_callback(cfg, cq["id"], "Отменено")
        if chat_id:
            ui_edit_or_send(
                cfg,
                chat_id,
                "Ок, отменено.\n\n/order — новый · /myorders — история",
                reply_markup={
                    "inline_keyboard": [
                        [{"text": "🛠 Новый заказ", "callback_data": "ord:restart"}],
                        [{"text": "📦 Мои заказы", "callback_data": "ord:mine"}],
                    ]
                },
                message_id=cb_mid,
                state=state,
                uid=uid,
                store_key="order_ui_msg",
            )
            save_state(state)
        return True

    if action == "restart":
        drafts.pop(str(uid), None)
        tg.answer_callback(cfg, cq["id"], "Заново")
        if chat_id:
            _order_start_msg(
                cfg, chat_id, state=state, uid=uid, message_id=cb_mid
            )
            save_state(state)
        return True

    if action == "type" and len(parts) >= 3:
        kind = parts[2]
        if kind not in orders.ORDER_TYPES:
            tg.answer_callback(cfg, cq["id"], "Неизвестный тип", show_alert=True)
            return True
        first = orders.TZ_STEPS[0]
        drafts[str(uid)] = {
            "kind": kind,
            "tz_step": first["id"],
            "answers": {},
        }
        meta = orders.ORDER_TYPES[kind]
        tg.answer_callback(cfg, cq["id"], meta["title"])
        if chat_id:
            ui_edit_or_send(
                cfg,
                chat_id,
                f"Тип: <b>{html.escape(meta['title'])}</b> · "
                f"<b>{int(meta.get('price') or 0)} ₽</b>\n"
                f"<i>{html.escape(meta['hint'])}</i>\n\n"
                "Всего <b>4 коротких шага</b> (можно «Пропустить»).\n"
                "Grok сам соберёт полное ТЗ.\n"
                "<i>Одно окно — ответы подчищаются.</i>\n\n"
                + str(first.get("ask") or ""),
                reply_markup=orders.order_step_keyboard(),
                message_id=cb_mid,
                state=state,
                uid=uid,
                store_key="order_ui_msg",
            )
            save_state(state)
        return True

    if action == "skip":
        # пропуск шага опроса → дефолт из step["skip"]
        draft = drafts.get(str(uid)) or {}
        if not draft.get("kind") or draft.get("await_confirm"):
            tg.answer_callback(cfg, cq["id"], "Нечего пропускать")
            return True
        if draft.get("tz_step") == "ai_clarify":
            tg.answer_callback(cfg, cq["id"], "Ок")
            # treat as ai_skip
            action = "ai_skip"
        else:
            step_id = draft.get("tz_step") or "what"
            step = orders.tz_step(step_id)
            skip_txt = str(step.get("skip") or "на усмотрение")
            answers = dict(draft.get("answers") or {})
            answers[str(step.get("key") or step_id)] = skip_txt
            tg.answer_callback(cfg, cq["id"], "Пропуск")
            _order_advance_after_answer(
                cfg,
                state,
                chat_id,
                uid,
                draft=draft,
                step=step,
                step_id=step_id,
                answers=answers,
                uname=uname,
                name=name,
                message_id=cb_mid,
            )
            return True

    if action == "ai_skip":
        # пропустить уточнения AI → сразу подтверждение
        draft = drafts.get(str(uid)) or {}
        kind = draft.get("kind")
        answers = dict(draft.get("answers") or {})
        brief = draft.get("brief") or orders.build_brief_from_answers(str(kind), answers)
        review = draft.get("ai_review") or {}
        if not kind:
            tg.answer_callback(cfg, cq["id"], "Нет черновика", show_alert=True)
            return True
        tg.answer_callback(cfg, cq["id"], "Ок")
        _order_show_estimate(
            cfg,
            state,
            chat_id,
            uid,
            kind=str(kind),
            brief=str(brief),
            answers=answers,
            review=review if isinstance(review, dict) else {},
            message_id=cb_mid,
        )
        return True

    if action == "commit":
        draft = drafts.get(str(uid)) or {}
        kind = draft.get("kind")
        brief = draft.get("brief")
        if not kind or not brief:
            tg.answer_callback(cfg, cq["id"], "Сначала опиши задачу", show_alert=True)
            return True
        if apply_tz_moderation(
            cfg,
            state,
            uid=uid,
            uname=uname,
            name=name,
            brief=str(brief),
            chat_id=chat_id,
        ):
            tg.answer_callback(cfg, cq["id"], "Заблокировано", show_alert=True)
            return True
        # ещё раз по полному ТЗ (на случай обхода по шагам)
        if apply_tz_moderation(
            cfg,
            state,
            uid=uid,
            uname=uname,
            name=name,
            brief=str(brief),
            chat_id=chat_id or uid,
        ):
            tg.answer_callback(cfg, cq["id"], "Заблокировано", show_alert=True)
            return True
        # платный заказ — фикс. цена, списание с баланса
        est = orders.estimate(kind, brief)
        need_pay = int(est["price"])
        if need_pay > 0:
            cur = bal.get_balance(uid)
            if cur < need_pay:
                tg.answer_callback(cfg, cq["id"], "Не хватает баланса", show_alert=True)
                if chat_id:
                    ui_edit_or_send(
                        cfg,
                        chat_id,
                        f"💰 <b>Недостаточно средств</b>\n\n"
                        f"Нужно: <b>{need_pay}</b> ₽\n"
                        f"Баланс: <b>{cur}</b> ₽\n"
                        f"Не хватает: <b>{need_pay - cur}</b> ₽\n\n"
                        + (
                            "Пополни → /topup, потом снова «Отправить заказ»."
                            if bal.topup_enabled(cfg)
                            else "Пополнение скоро (Platega). Тикет: /support"
                        ),
                        reply_markup={
                            "inline_keyboard": (
                                [
                                    [
                                        {
                                            "text": "💳 Пополнить",
                                            "callback_data": "bal:topup",
                                        }
                                    ]
                                ]
                                if bal.topup_enabled(cfg)
                                else []
                            )
                            + [
                                [
                                    {
                                        "text": "✅ Отправить заказ",
                                        "callback_data": "ord:commit",
                                    }
                                ],
                                [{"text": "❌ Отмена", "callback_data": "ord:cancel"}],
                            ]
                        },
                        message_id=cb_mid,
                        state=state,
                        uid=uid,
                    )
                return True
        item = orders.create_order(
            user_id=uid,
            username=uname,
            name=name,
            kind=kind,
            brief=brief,
        )
        pay = int(item.get("price") or 0)
        if pay > 0:
            ok, new_bal, err = bal.try_debit(
                uid,
                pay,
                kind="order",
                note=f"заказ {item['id']}",
                ref=str(item["id"]),
            )
            if not ok:
                item["status"] = "cancelled"
                item["deliver_note"] = f"cancel: balance {err}"
                orders.save_order(item)
                tg.answer_callback(cfg, cq["id"], "Оплата не прошла", show_alert=True)
                if chat_id:
                    ui_edit_or_send(
                        cfg,
                        chat_id,
                        f"❌ {html.escape(err)}\n"
                        + (
                            "Пополни /topup"
                            if bal.topup_enabled(cfg)
                            else "Пополнение временно выкл. · /support"
                        ),
                        reply_markup=bal.balance_keyboard(cfg),
                        message_id=cb_mid,
                        state=state,
                        uid=uid,
                        store_key="order_ui_msg",
                    )
                    save_state(state)
                return True
            item["paid_from_balance"] = pay
            item["balance_after"] = new_bal
            try:
                pts = bal.maybe_earn_points(
                    uid,
                    pay,
                    kind="order",
                    note=f"заказ {item['id']}",
                    ref=str(item["id"]),
                )
                if pts:
                    item["points_earned"] = pts
            except Exception as pe:
                print("order points", pe, flush=True)
            orders.save_order(item)
        drafts.pop(str(uid), None)
        check = orders.get_order(item["id"])
        if not check:
            print("WARN order not on disk after create", item["id"], flush=True)
        try:
            orders.append_event(item, "new", "заказ создан")
            if item.get("paid_from_balance"):
                orders.append_event(
                    item, "paid", f"оплата {item.get('paid_from_balance')} ₽"
                )
            item = orders.get_order(item["id"]) or item
        except Exception:
            pass
        # реферальный бонус пригласившему
        try:
            import growth_lib as growth

            ref_msg = growth.maybe_reward_referral(
                cfg, state, uid, str(item.get("id"))
            )
            if ref_msg:
                save_state(state)
                notify_owner(cfg, ref_msg)
        except Exception as e:
            print("ref reward", e, flush=True)
        tg.answer_callback(cfg, cq["id"], "Заказ создан")
        pay_note = ""
        if item.get("paid_from_balance"):
            pay_note = (
                f"\n💰 Списано: <b>{item.get('paid_from_balance')}</b> ₽"
                f" · остаток {item.get('balance_after')} ₽"
            )
            if item.get("points_earned"):
                pay_note += (
                    f"\n⭐ +{int(item['points_earned'])} баллов "
                    f"(всего {bal.get_points(uid)}) · /points"
                )
        if chat_id:
            mid_card = ui_edit_or_send(
                cfg,
                chat_id,
                "✅ <b>Заказ принят</b>\n\n"
                + orders.format_order_card(item)
                + pay_note
                + "\n\n📄 Договор — кнопкой на карточке.",
                reply_markup=orders.user_order_actions_keyboard(item["id"]),
                message_id=cb_mid,
                state=state,
                uid=uid,
                store_key="order_ui_msg",
            )
            try:
                if mid_card:
                    orders.set_client_card(
                        item, chat_id=int(chat_id), message_id=int(mid_card)
                    )
            except Exception:
                pass
            # авто-выдача договора
            try:
                import docs_lib

                cpath, apath = docs_lib.write_contract_files(item)
                tg.send_document(
                    cfg,
                    chat_id,
                    str(cpath),
                    caption=f"📄 Договор · <code>{html.escape(str(item['id']))}</code>",
                )
                tg.send_document(
                    cfg,
                    chat_id,
                    str(apath),
                    caption=f"📋 Акт (шаблон) · <code>{html.escape(str(item['id']))}</code>",
                )
            except Exception as e:
                print("auto docs", e, flush=True)
            save_state(state)
        notify_owner(
            cfg,
            "🆕 <b>Новый заказ</b>\n\n"
            + orders.format_order_card(item, for_owner=True)
            + pay_note,
            reply_markup=orders.owner_order_keyboard(item["id"]),
        )
        return True

    tg.answer_callback(cfg, cq["id"], "ok")
    return True


def handle_giveaway_private(cfg: dict, state: dict, msg: dict) -> bool:
    """
    Личка: /start gw_ / gwref_ + скрин репоста другу.
    Доступно всем (не только owner).
    """
    chat = msg.get("chat") or {}
    if chat.get("type") != "private":
        return False
    user = msg.get("from") or {}
    uid = int(user.get("id") or 0)
    if not uid:
        return False
    uname = user.get("username") or ""
    name = user.get("first_name") or uname or str(uid)
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()
    owner = is_owner(cfg, user)

    # 1) пересылка поста канала боту — больше НЕ засчитываем
    if msg.get("forward_from_chat") or msg.get("forward_origin") or msg.get("forward_from_message_id"):
        item = gw.get_active(state)
        if item and item.get("status") == "active" and item.get("require_repost", True):
            mid = item.get("channel_message_id")
            link = f"https://t.me/Vaggo01/{mid}" if mid else "https://t.me/Vaggo01"
            tg.send_message(
                cfg,
                chat_id,
                "📨 Репост нужно сделать <b>другу</b>, не боту.\n\n"
                f"1. Открой пост: {link}\n"
                "2. ↗ → Переслать → выбери <b>друга</b>\n"
                "3. Сделай <b>скрин</b> пересланного сообщения\n"
                "4. Пришли скрин <b>сюда</b> — засчитаю",
                parse_mode="HTML",
                disable_preview=True,
            )
            return True
        if not owner:
            return True  # чужие форварды не в команды

    # 2) скрин репоста — только если уже в квесте (нажал «Участвовать»)
    photos = msg.get("photo") or []
    doc = msg.get("document") or {}
    mime = (doc.get("mime_type") or "") if doc else ""
    is_img_doc = bool(doc.get("file_id") and mime.startswith("image/"))
    if (photos or is_img_doc) and not owner:
        item = gw.get_active(state)
        if item and item.get("status") == "active" and item.get("require_repost", True):
            entry = gw.get_entry(item, uid)
            if not entry:
                mid = item.get("channel_message_id")
                link = f"https://t.me/Vaggo01/{mid}" if mid else "https://t.me/Vaggo01"
                tg.send_message(
                    cfg,
                    chat_id,
                    "Сначала открой пост розыгрыша и нажми <b>«Участвовать»</b>.\n"
                    f"{link}\n\n"
                    "Просто написать боту ≠ участие.",
                    parse_mode="HTML",
                    disable_preview=True,
                )
                return True
            if not entry.get("repost_ok"):
                file_id = None
                if photos:
                    file_id = photos[-1].get("file_id")
                elif is_img_doc:
                    file_id = doc.get("file_id")
                # 1) живая проверка подписки
                entry, missing, _ = refresh_subs_and_enroll(
                    cfg, item, uid, username=uname, name=name
                )
                if item.get("require_sub", True) and missing:
                    send_quest_card(
                        cfg,
                        chat_id,
                        item,
                        entry,
                        notice="❌ <b>Сначала подписка</b> — без неё скрин не засчитаем.\n"
                        "Не хватает: " + html.escape(", ".join(missing[:5])),
                    )
                    return True
                # 2) проверка скрина — статус в той же карточке
                send_quest_card(
                    cfg, chat_id, item, entry, notice="🔍 <b>Проверяю скрин…</b>"
                )
                try:
                    path = tg.download_file(cfg, file_id, suffix=".jpg")
                    from content import verify_giveaway_repost_screenshot

                    ok_scr, reason = verify_giveaway_repost_screenshot(
                        cfg,
                        path,
                        channel_username=(cfg.get("channel_username") or "Vaggo01"),
                        prize_hint=str(item.get("prize") or ""),
                    )
                except Exception as e:
                    print("gw screenshot verify", e, flush=True)
                    ok_scr, reason = False, f"ошибка проверки: {e}"
                if not ok_scr:
                    low_r = (reason or "").lower()
                    auto_fail = any(
                        x in low_r
                        for x in (
                            "не удалось проверить",
                            "ошибка проверки",
                            "no bridge",
                            "bridge",
                            "timeout",
                            "timed out",
                            "connection",
                            "502",
                            "503",
                            "401",
                            "403",
                            "vision",
                            "недоступ",
                        )
                    )
                    # на облаке без vision / мёртвый bridge — ручная проверка, не отшиваем
                    if auto_fail:
                        send_quest_card(
                            cfg,
                            chat_id,
                            item,
                            entry,
                            notice="⏳ <b>Скрин на ручной проверке</b>\n"
                            "Авто-проверка сейчас недоступна. Владелец глянет и зачислит.\n"
                            f"<i>{html.escape(reason)[:100]}</i>",
                        )
                        try:
                            oid = (cfg.get("owner_user_ids") or [None])[0]
                            cap = (
                                f"⏳ Скрин ждут ручную проверку\n"
                                f"@{html.escape(uname) if uname else '—'} · "
                                f"{html.escape(name)}\n"
                                f"id <code>{uid}</code> · gw <code>{item.get('id')}</code>\n"
                                f"{html.escape(reason)[:180]}"
                            )
                            kb = {
                                "inline_keyboard": [
                                    [
                                        {
                                            "text": "✅ Засчитать репост",
                                            "callback_data": f"gw:okrep:{item.get('id')}:{uid}",
                                        },
                                        {
                                            "text": "❌ Отклонить",
                                            "callback_data": f"gw:norep:{item.get('id')}:{uid}",
                                        },
                                    ]
                                ]
                            }
                            if file_id and oid:
                                tg.api(
                                    cfg,
                                    "sendPhoto",
                                    data={
                                        "chat_id": oid,
                                        "photo": file_id,
                                        "caption": cap[:1024],
                                        "parse_mode": "HTML",
                                        "reply_markup": __import__("json").dumps(kb),
                                    },
                                )
                            else:
                                notify_owner(cfg, cap, reply_markup=kb)
                        except Exception as e:
                            print("gw manual queue", e, flush=True)
                        return True
                    send_quest_card(
                        cfg,
                        chat_id,
                        item,
                        entry,
                        notice="❌ <b>Скрин не принят</b>\n"
                        f"{html.escape(reason)[:140]}\n\n"
                        "Нужно: личка с <b>живым другом</b> + пересланный пост @Vaggo01.\n"
                        "Нельзя: бот, Избранное, себе, просто канал.",
                    )
                    try:
                        notify_owner(
                            cfg,
                            f"❌ Скрин отклонён · @{html.escape(uname) if uname else '—'}\n"
                            f"{html.escape(reason)[:200]}\n"
                            f"id <code>{uid}</code>",
                            reply_markup={
                                "inline_keyboard": [
                                    [
                                        {
                                            "text": "✅ Всё же засчитать",
                                            "callback_data": f"gw:okrep:{item.get('id')}:{uid}",
                                        }
                                    ]
                                ]
                            },
                        )
                    except Exception:
                        pass
                    return True
                gw.set_repost_ok(item, uid, True, proof_file_id=file_id)
                e = gw.get_entry(item, uid) or entry
                e["repost_verify_reason"] = reason
                # сохранить quest_msg_id
                if entry.get("quest_msg_id"):
                    e["quest_msg_id"] = entry.get("quest_msg_id")
                gw.save_item(item)
                entry, missing, just = refresh_subs_and_enroll(
                    cfg, item, uid, username=uname, name=name
                )
                if entry.get("quest_msg_id") is None and e.get("quest_msg_id"):
                    entry["quest_msg_id"] = e["quest_msg_id"]
                if entry.get("complete"):
                    notice = (
                        f"✅ <b>Скрин ок</b> ({html.escape(reason)[:70]})\n"
                        "Подписка и репост проверены — <b>ты в розыгрыше!</b>"
                    )
                else:
                    gaps = gw.enrollment_gaps(item, entry)
                    notice = (
                        f"✅ <b>Скрин принят</b> ({html.escape(reason)[:70]})\n"
                        "Ещё: " + html.escape(", ".join(gaps) if gaps else "—")
                    )
                send_quest_card(cfg, chat_id, item, entry, notice=notice)
                try:
                    cap = (
                        f"✅ Скрин ок · @{html.escape(uname) if uname else '—'}\n"
                        f"{html.escape(name)} · enrolled={entry.get('complete')}\n"
                        f"{html.escape(reason)[:120]}"
                    )
                    oid = (cfg.get("owner_user_ids") or [None])[0]
                    if file_id and oid:
                        tg.api(
                            cfg,
                            "sendPhoto",
                            data={
                                "chat_id": oid,
                                "photo": file_id,
                                "caption": cap[:1024],
                                "parse_mode": "HTML",
                            },
                        )
                    else:
                        notify_owner(cfg, cap)
                except Exception as e:
                    print("gw proof notify", e, flush=True)
                if just or entry.get("complete"):
                    notify_owner(
                        cfg,
                        f"{'✅ Зачислен' if entry.get('complete') else '⏳ прогресс'}: "
                        f"{html.escape(name)} (@{html.escape(uname) if uname else '—'})\n"
                        f"complete={gw.entry_count(item, complete_only=True)}",
                    )
                return True
            entry, missing, just = refresh_subs_and_enroll(
                cfg, item, uid, username=uname, name=name
            )
            send_quest_card(
                cfg, chat_id, item, entry, notice="ℹ️ Репост уже засчитан."
            )
            return True

    # 3) /start deep links
    if text.startswith("/start"):
        arg = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
        invited_by = None
        gid = None
        join_src = "quest"
        if arg.startswith("gwref_"):
            rest = arg[6:]
            parts = rest.split("_")
            if len(parts) >= 2:
                gid = parts[0]
                try:
                    invited_by = int(parts[1])
                except ValueError:
                    invited_by = None
            elif parts:
                gid = parts[0]
            join_src = "gwref"
        elif arg.startswith("gw_"):
            gid = arg[3:].split("_")[0]
            join_src = "gw"
        if gid:
            item = gw.get_by_id(gid) or gw.get_active(state)
            if not item or item.get("status") != "active":
                tg.send_message(cfg, chat_id, "Розыгрыш не активен или закончился.", parse_mode=None)
                return True
            if gw.is_expired(item):
                tg.send_message(cfg, chat_id, "Срок розыгрыша вышел.", parse_mode=None)
                return True
            if is_giveaway_excluded(cfg, user):
                tg.send_message(
                    cfg,
                    chat_id,
                    "Тестовый / владелец — в розыгрыш не зачисляем (ок для проверки квеста).",
                    parse_mode=None,
                )
                return True
            # ТОЛЬКО здесь создаём участника — явный «Участвовать»
            gw.ensure_entry(
                item,
                user_id=uid,
                username=uname,
                name=name,
                invited_by=invited_by,
                source=join_src,
            )
            entry, missing, just = refresh_subs_and_enroll(
                cfg, item, uid, username=uname, name=name
            )
            if invited_by and gw.get_entry(item, int(invited_by)):
                try:
                    refresh_subs_and_enroll(cfg, item, int(invited_by))
                except Exception:
                    pass
            if missing:
                notice = (
                    f"Привет{', ' + html.escape(name) if name else ''}!\n"
                    "❌ Подписка не подтверждена: "
                    + html.escape(", ".join(missing[:5]))
                )
            elif entry.get("complete"):
                notice = (
                    f"Привет{', ' + html.escape(name) if name else ''}!\n"
                    "✅ Ты в розыгрыше."
                )
            else:
                notice = (
                    f"Привет{', ' + html.escape(name) if name else ''}!\n"
                    "Прохожу шаги — в конкурс после подписки + репоста."
                )
            send_quest_card(cfg, chat_id, item, entry, notice=notice)
            if just:
                notify_owner(
                    cfg,
                    f"✅ Зачислен: {html.escape(name)} "
                    f"(@{html.escape(uname) if uname else '—'})\n"
                    f"complete={gw.entry_count(item, complete_only=True)}",
                )
            return True
        # plain /start — НЕ участник; owner не перехватываем (менеджер)
        if not owner:
            item = gw.get_active(state)
            mid = (item or {}).get("channel_message_id")
            link = f"https://t.me/Vaggo01/{mid}" if mid else "https://t.me/Vaggo01"
            if item and item.get("status") == "active":
                entry = gw.get_entry(item, uid)
                if entry:
                    entry, _, _ = refresh_subs_and_enroll(
                        cfg, item, uid, username=uname, name=name
                    )
                    send_quest_card(cfg, chat_id, item, entry)
                    return True
            # одно короткое сообщение; не создаём entry
            tg.send_message(
                cfg,
                chat_id,
                "Бот канала @Vaggo01.\n\n"
                "Участие: пост → <b>«Участвовать»</b>\n"
                f"{link}",
                parse_mode="HTML",
                disable_preview=True,
            )
            return True

    # 4) non-owner other messages — только квест, без чужих команд
    if not owner:
        item = gw.get_active(state)
        if item and item.get("status") == "active":
            entry = gw.get_entry(item, uid)
            if not entry:
                mid = item.get("channel_message_id")
                link = f"https://t.me/Vaggo01/{mid}" if mid else "https://t.me/Vaggo01"
                tg.send_message(
                    cfg,
                    chat_id,
                    "Ты не в квесте. Пост → <b>«Участвовать»</b>\n" + link,
                    parse_mode="HTML",
                    disable_preview=True,
                )
                return True
            if item.get("require_repost", True) and not entry.get("repost_ok"):
                mid = item.get("channel_message_id")
                link = f"https://t.me/Vaggo01/{mid}" if mid else "https://t.me/Vaggo01"
                send_quest_card(
                    cfg,
                    chat_id,
                    item,
                    entry,
                    tip=f"Перешли пост другу: {link}\nПотом скрин сюда.",
                )
            else:
                send_quest_card(cfg, chat_id, item, entry)
            return True
        # нет розыгрыша — чужим закрыто
        tg.send_message(
            cfg,
            chat_id,
            "Это бот канала @Vaggo01. Сейчас нет активного квеста.\n"
            "Канал: https://t.me/Vaggo01",
            parse_mode=None,
        )
        return True

    return False


def handle_giveaway_callback(cfg: dict, state: dict, cq: dict) -> bool:
    """Кнопки квеста + инфо на посте."""
    data = cq.get("data") or ""
    if not data.startswith("gw:"):
        return False
    try:
        return _handle_giveaway_callback_inner(cfg, state, cq)
    except Exception as e:
        print("gw callback error", e, flush=True)
        try:
            tg.answer_callback(cfg, cq["id"], "Ошибка, попробуй ещё раз", show_alert=True)
        except Exception:
            pass
        return True


def _handle_giveaway_callback_inner(cfg: dict, state: dict, cq: dict) -> bool:
    data = cq.get("data") or ""
    parts = data.split(":")
    if len(parts) < 3:
        tg.answer_callback(cfg, cq["id"], "Ошибка")
        return True
    action, gid = parts[1], parts[2]
    # всегда свежий store (media/giveaways.json + state)
    item = gw.get_by_id(gid) or gw.get_active()
    user = cq.get("from") or {}
    uid = int(user.get("id") or 0)
    uname = user.get("username") or ""
    name = user.get("first_name") or uname or str(uid)
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")

    if not item and action not in ("ended", "rules"):
        tg.answer_callback(
            cfg,
            cq["id"],
            "Розыгрыш не найден на сервере. Владелец: /gstatus или /gpost заново.",
            show_alert=True,
        )
        return True

    if action == "ended":
        tg.answer_callback(cfg, cq["id"], "Розыгрыш уже завершён", show_alert=True)
        return True

    # владелец: вручную засчитать / отклонить репост
    if action in ("okrep", "norep") and len(parts) >= 4:
        if not is_owner(cfg, user):
            tg.answer_callback(cfg, cq["id"], "Только владелец", show_alert=True)
            return True
        try:
            target_uid = int(parts[3])
        except ValueError:
            tg.answer_callback(cfg, cq["id"], "bad id", show_alert=True)
            return True
        if not item or item.get("status") != "active":
            tg.answer_callback(cfg, cq["id"], "Нет активного розыгрыша", show_alert=True)
            return True
        if action == "norep":
            tg.answer_callback(cfg, cq["id"], "Отклонено")
            try:
                tg.send_message(
                    cfg,
                    target_uid,
                    "❌ Скрин не принят владельцем.\n"
                    "Нужен репост поста @Vaggo01 <b>живому другу</b> + новый скрин.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
            return True
        # okrep
        entry = gw.ensure_entry(item, user_id=target_uid)
        gw.set_repost_ok(item, target_uid, True)
        e = gw.get_entry(item, target_uid) or entry
        e["repost_verify_reason"] = "засчитано вручную владельцем"
        gw.save_item(item)
        entry, missing, just = refresh_subs_and_enroll(
            cfg,
            item,
            target_uid,
            username=str(e.get("username") or ""),
            name=str(e.get("name") or ""),
        )
        tg.answer_callback(
            cfg,
            cq["id"],
            f"OK complete={bool(entry.get('complete'))}",
            show_alert=True,
        )
        try:
            if entry.get("complete"):
                notice = "✅ Репост засчитан вручную — <b>ты в розыгрыше!</b>"
            else:
                gaps = gw.enrollment_gaps(item, entry)
                notice = (
                    "✅ Репост засчитан вручную.\nЕщё: "
                    + html.escape(", ".join(gaps) if gaps else "—")
                )
            send_quest_card(cfg, target_uid, item, entry, notice=notice)
        except Exception as e:
            print("okrep dm", e, flush=True)
        return True

    if action == "rules":
        prize = str((item or {}).get("prize") or "Google AI Pro 18 мес")[:50]
        inv = int((item or {}).get("require_invites") or 0)
        short = (
            f"Приз: {prize}\n"
            f"1) Подписка @Vaggo01\n"
            f"2) Репост другу + скрин (бот проверит)\n"
        )
        if inv > 0:
            short += f"3) {inv} друг(а)\n"
        short += "→ «Участвовать»"
        tg.answer_callback(cfg, cq["id"], short[:200], show_alert=True)
        return True

    if action == "count":
        # статистика только владельцу
        if is_owner(cfg, user) and item:
            n = gw.entry_count(item, complete_only=True)
            t = gw.entry_count(item, complete_only=False)
            tg.answer_callback(
                cfg, cq["id"], f"(только ты) complete={n} · начали={t}", show_alert=True
            )
        else:
            tg.answer_callback(cfg, cq["id"], "Жми «Участвовать»", show_alert=True)
        return True

    if action == "join":
        tg.answer_callback(
            cfg, cq["id"], "Открой кнопку «Участвовать» ещё раз", show_alert=True
        )
        return True

    # --- quest steps (private) ---
    if not item or item.get("status") != "active":
        tg.answer_callback(cfg, cq["id"], "Розыгрыш не активен", show_alert=True)
        return True
    if gw.is_expired(item):
        tg.answer_callback(cfg, cq["id"], "Срок вышел", show_alert=True)
        return True

    # квест-кнопки только для тех, кто уже нажал «Участвовать»
    if action in ("chksub", "rephow", "inv", "prog"):
        if not gw.get_entry(item, uid):
            tg.answer_callback(
                cfg,
                cq["id"],
                "Сначала «Участвовать» на посте розыгрыша",
                show_alert=True,
            )
            return True

    entry = gw.get_entry(item, uid) or {}

    if action == "chksub":
        # сохранить id карточки из callback (то сообщение, на котором кнопка)
        if msg.get("message_id") and not entry.get("quest_msg_id"):
            entry["quest_msg_id"] = int(msg["message_id"])
        entry, missing, just = refresh_subs_and_enroll(
            cfg, item, uid, username=uname, name=name
        )
        if entry.get("quest_msg_id") is None and msg.get("message_id"):
            entry["quest_msg_id"] = int(msg["message_id"])
        if not missing:
            if entry.get("complete"):
                tg.answer_callback(cfg, cq["id"], "Подписка ок · в розыгрыше!", show_alert=False)
                notice = "✅ Подписка ок — <b>ты в розыгрыше!</b>"
            else:
                gaps = gw.enrollment_gaps(item, entry)
                tg.answer_callback(
                    cfg, cq["id"], ("Подписка ок. Ещё: " + ", ".join(gaps))[:200], show_alert=False
                )
                notice = "✅ Подписка ок. Ещё: " + html.escape(", ".join(gaps))
        else:
            tg.answer_callback(
                cfg, cq["id"], ("Нет подписки: " + ", ".join(missing[:5]))[:200], show_alert=False
            )
            notice = "❌ Нет подписки: " + html.escape(", ".join(missing[:5]))
        if chat_id:
            send_quest_card(cfg, chat_id, item, entry, notice=notice)
        if just:
            notify_owner(
                cfg,
                f"✅ Зачислен: {html.escape(name)}\n"
                f"complete={gw.entry_count(item, complete_only=True)}",
            )
        return True

    if action == "rephow":
        if msg.get("message_id"):
            entry["quest_msg_id"] = int(msg["message_id"])
        mid = item.get("channel_message_id")
        link = f"https://t.me/Vaggo01/{mid}" if mid else "https://t.me/Vaggo01"
        tg.answer_callback(cfg, cq["id"], "Смотри подсказку на карточке", show_alert=False)
        if chat_id:
            send_quest_card(
                cfg,
                chat_id,
                item,
                entry,
                tip=(
                    f"📨 <b>Как сделать репост</b>\n"
                    f"1. {link}\n"
                    f"2. ↗ Переслать → <b>живой друг</b> (человек)\n"
                    f"3. Скрин чата (видна шапка + «переслано») → сюда\n\n"
                    f"❌ Нельзя: бот, Избранное, себе, рандом без друга.\n"
                    f"Бот смотрит скрин строго."
                ),
            )
        return True

    if action == "inv":
        if msg.get("message_id"):
            entry["quest_msg_id"] = int(msg["message_id"])
        bot_u = _bot_username(cfg)
        ref = f"https://t.me/{bot_u}?start=gwref_{item.get('id')}_{uid}"
        need = int(item.get("require_invites") or 0)
        have = len(entry.get("invites") or [])
        tg.answer_callback(cfg, cq["id"], f"Друзья: {have}/{need}", show_alert=False)
        if chat_id:
            send_quest_card(
                cfg,
                chat_id,
                item,
                entry,
                tip=(
                    f"👥 Друзья: <b>{have}/{need}</b>\n"
                    f"<code>{html.escape(ref)}</code>"
                ),
            )
        return True

    if action == "prog":
        if msg.get("message_id"):
            entry["quest_msg_id"] = int(msg["message_id"])
        entry, missing, just = refresh_subs_and_enroll(
            cfg, item, uid, username=uname, name=name
        )
        if entry.get("quest_msg_id") is None and msg.get("message_id"):
            entry["quest_msg_id"] = int(msg["message_id"])
        if entry.get("complete"):
            tg.answer_callback(cfg, cq["id"], "В розыгрыше ✅", show_alert=False)
            notice = "✅ <b>Ты в розыгрыше</b>"
        elif missing:
            tg.answer_callback(
                cfg, cq["id"], ("Нет подписки: " + ", ".join(missing[:4]))[:200], show_alert=False
            )
            notice = "❌ " + html.escape(", ".join(missing[:4]))
        else:
            gaps = gw.enrollment_gaps(item, entry)
            tg.answer_callback(
                cfg,
                cq["id"],
                ("Ещё: " + ", ".join(gaps))[:200] if gaps else "Обновлено",
                show_alert=False,
            )
            notice = "🔄 Обновлено" + (
                ". Ещё: " + html.escape(", ".join(gaps)) if gaps else ""
            )
        if chat_id:
            send_quest_card(cfg, chat_id, item, entry, notice=notice)
        if just:
            notify_owner(
                cfg,
                f"✅ Зачислен: {html.escape(name)}\n"
                f"complete={gw.entry_count(item, complete_only=True)}",
            )
        return True

    tg.answer_callback(cfg, cq["id"], "ok")
    return True


def maybe_handle_giveaway_entry(cfg: dict, state: dict, msg: dict) -> bool:
    """Засчитать участника розыгрыша из коммента. True = дальше AI-ответ не нужен."""
    item = gw.get_active(state)
    if not item:
        return False
    if item.get("status") != "active":
        return False
    if gw.is_expired(item):
        return False
    user = msg.get("from") or {}
    if user.get("is_bot") or is_owner(cfg, user):
        return False
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not text:
        return False
    # только тред поста розыгрыша (если пост привязан)
    if item.get("channel_message_id") and not gw.matches_giveaway_thread(item, msg, state):
        return False
    uid = int(user.get("id") or 0)
    if not uid:
        return False
    uname = user.get("username") or ""
    name = user.get("first_name") or uname or str(uid)
    mid = msg.get("message_id")
    chat_id = (msg.get("chat") or {}).get("id")
    thread_id = msg.get("message_thread_id")
    mode = item.get("mode") or "button"
    if mode == "button":
        return False  # только кнопка
    ok, reason = gw.try_register_entry(
        item,
        user_id=uid,
        username=uname,
        name=name,
        text=text,
        message_id=mid,
        discuss_root_hint=int(thread_id) if thread_id else None,
        source="comment",
    )
    marker = item.get("marker") or "🎯"
    if reason == "no_marker":
        # не наш розыгрыш-коммент — пусть обычный AI
        return False
    if reason == "too_short":
        try:
            tg.send_message(
                cfg,
                chat_id,
                f"Чтобы участвовать: 1–2 предложения + {marker}",
                reply_to=mid,
                parse_mode=None,
                message_thread_id=int(thread_id) if thread_id else None,
            )
        except Exception:
            pass
        return True
    if reason == "duplicate":
        try:
            if mid:
                tg.set_message_reaction(cfg, chat_id, int(mid), marker if marker in ("🎯", "🔥", "❤", "👍") else "🔥")
        except Exception:
            pass
        return True
    if ok:
        try:
            if mid:
                tg.set_message_reaction(cfg, chat_id, int(mid), "🔥")
        except Exception:
            pass
        try:
            tg.send_message(
                cfg,
                chat_id,
                f"Участие засчитано ✅ ({gw.entry_count(item)} чел.)",
                reply_to=mid,
                parse_mode=None,
                message_thread_id=int(thread_id) if thread_id else None,
            )
        except Exception as e:
            print("giveaway ack fail", e, flush=True)
        notify_owner(
            cfg,
            f"🎟 <b>Участник розыгрыша</b> · {gw.entry_count(item)} всего\n"
            f"От: {html.escape(name)}"
            + (f" (@{html.escape(uname)})" if uname else "")
            + f"\n<code>{uid}</code>\n"
            f"<i>{html.escape(text[:300])}</i>\n"
            f"/gentries · /gdraw",
        )
        print(f"giveaway entry uid={uid} total={gw.entry_count(item)}", flush=True)
        return True
    return False


def maybe_moderate_discussion(cfg: dict, msg: dict) -> bool:
    """
    Модератор чата: ИИ → удалить + варн/мут/бан.
    True = обработано (обычный AI-ответ не шлём).
    """
    if cfg.get("chat_mod_enabled") is False:
        return False
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    disc = cfg.get("discussion_group_id") or 0
    if not disc or int(chat_id or 0) != int(disc):
        return False
    if msg.get("is_automatic_forward"):
        return False
    user = msg.get("from") or {}
    if not user or user.get("is_bot"):
        return False
    uid = int(user.get("id") or 0)
    if not uid:
        return False

    # owner: по умолчанию ТОЖЕ модерируем (иначе тесты «с себя» не работают).
    # Иммунитет: chat_mod_skip_owner=true
    owner_here = is_owner(cfg, user)
    if owner_here and cfg.get("chat_mod_skip_owner", False):
        print("chatmod skip owner (config)", uid, flush=True)
        return False

    mid = msg.get("message_id")
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not text:
        return False

    # пермач — снести и всё
    if chatmod.is_chat_banned(uid):
        ok_del = tg.try_delete_message(cfg, chat_id, mid) if mid else False
        print(f"chatmod banned-user del={ok_del} mid={mid}", flush=True)
        return True

    # ИИ-вердикт
    try:
        reason = chatmod.detect_toxicity(text, cfg=cfg)
    except Exception as e:
        print("chatmod detect err", e, flush=True)
        reason = None
    print(
        f"chatmod check uid={uid} mid={mid} verdict={reason!r} text={text[:60]!r}",
        flush=True,
    )
    if not reason:
        return False

    uname = (user.get("username") or "").lstrip("@")
    name = (
        f"{user.get('first_name') or ''} {user.get('last_name') or ''}".strip()
        or uname
        or str(uid)
    )

    # 1) удалить СРАЗУ
    ok_del = False
    if mid:
        ok_del = tg.try_delete_message(cfg, chat_id, mid)
        if not ok_del:
            # повтор через raw api
            try:
                tg.delete_message(cfg, chat_id, int(mid))
                ok_del = True
            except Exception as e:
                print("chatmod DELETE FAIL", mid, e, flush=True)

    # 2) лестница (owner не мутим/баним жёстко — только delete+warn в лог)
    if owner_here:
        res = {
            "action": "warn",
            "warnings": "—",
            "mutes": "—",
            "week_bans": "—",
            "public_text": f"⚠️ Сообщение снято (токсик). Owner — без мута.",
            "private_text": "Тест модерации: сообщение удалено. Лестница на owner не капает.",
            "reason_h": chatmod.REASON_RU.get(reason, reason),
        }
        action = "warn"
        until = 0
    else:
        res = chatmod.process_offense(
            uid,
            reason=reason,
            username=uname,
            name=name,
            snippet=text[:200],
        )
        action = res.get("action") or "warn"
        until = int(res.get("until") or 0)
        try:
            if action == "mute" and until:
                tg.restrict_chat_member(cfg, chat_id, uid, until_date=until)
            elif action in ("week", "month") and until:
                tg.restrict_chat_member(cfg, chat_id, uid, until_date=until)
            elif action == "ban":
                tg.ban_chat_member(cfg, chat_id, uid, revoke_messages=False)
        except Exception as e:
            print("chatmod restrict fail", action, e, flush=True)

    # 3) публично
    thr = msg.get("message_thread_id")
    try:
        tg.send_message(
            cfg,
            chat_id,
            res.get("public_text") or "⚠️",
            parse_mode=None,
            message_thread_id=int(thr) if thr else None,
            disable_preview=True,
        )
    except Exception as e:
        print("chatmod public fail", e, flush=True)

    # 4) ЛС
    try:
        tg.send_message(
            cfg,
            uid,
            res.get("private_text") or "⚠️ Нарушение в чате Vaggo.",
            parse_mode=None,
            disable_preview=True,
        )
    except Exception:
        pass

    # 5) owner notify (если не сам owner)
    if not owner_here:
        try:
            notify_owner(
                cfg,
                f"🛡 <b>Модерация</b> · {html.escape(str(action))}\n"
                f"del={'ok' if ok_del else 'FAIL'}\n"
                f"От: {html.escape(name)}"
                + (f" (@{html.escape(uname)})" if uname else "")
                + f"\n<code>{uid}</code>\n"
                f"Причина: {html.escape(str(res.get('reason_h') or reason))}\n"
                f"Статус: ⚠{res.get('warnings')}/4 · мут {res.get('mutes')}/3 · "
                f"нед {res.get('week_bans')}/2\n"
                f"<i>{html.escape(text[:200])}</i>\n"
                f"/modstat {uid} · /modpardon {uid}",
            )
        except Exception:
            pass

    print(
        f"chatmod DONE action={action} del={ok_del} uid={uid} reason={reason}",
        flush=True,
    )
    return True


def maybe_handle_discussion(cfg: dict, state: dict, msg: dict) -> None:
    """Комменты: модерация → розыгрыш → Grok. В фоне — polling не блокируется."""
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    disc = cfg.get("discussion_group_id") or 0
    if not disc or chat_id != disc:
        return
    if msg.get("is_automatic_forward"):
        return
    # сообщение канала-переслалка без автора-человека
    if msg.get("sender_chat") and (msg.get("sender_chat") or {}).get("type") == "channel":
        if not msg.get("from"):
            return
    user = msg.get("from") or {}
    if user.get("is_bot"):
        return
    # модератор первым
    try:
        if maybe_moderate_discussion(cfg, msg):
            return
    except Exception as e:
        print("chatmod error", e, flush=True)
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not text or text.startswith("/"):
        return
    if cfg.get("paused"):
        print("comment skip: paused", flush=True)
        return

    # розыгрыш — до rate-limit AI
    try:
        if maybe_handle_giveaway_entry(cfg, load_state(), msg):
            return
    except Exception as e:
        print("giveaway entry error", e, flush=True)

    if not cfg.get("auto_reply_comments", True) and not cfg.get("auto_react_comments", True):
        return
    # не отвечать самому себе / служебному
    if user.get("id") and cfg.get("skip_owner_comments", False) and is_owner(cfg, user):
        return

    uid = int(user.get("id") or 0)
    if uid and not _rate_ok(uid, cfg):
        print("comment rate limit", uid, flush=True)
        return

    mid = msg.get("message_id")
    thread_id = msg.get("message_thread_id")
    uname = user.get("username") or user.get("first_name") or "?"
    print(f"comment in: from={uname} mid={mid} text={text[:80]!r}", flush=True)

    # реакция сразу (лёгкая) — не валим ответ, если реакция недоступна
    if cfg.get("auto_react_comments", True) and mid:
        try:
            tg.set_message_reaction(cfg, chat_id, int(mid), pick_reaction_for_text(text))
        except Exception as e:
            # часто: нет прав / privacy / message not found — коммент всё равно отвечаем
            print("comment react fail", str(e)[:120], flush=True)

    # instant по умолчанию (если не выключено явно)
    can_reply = bool(cfg.get("auto_reply_comments", True))
    instant = can_reply and not bool(cfg.get("comment_needs_owner_ok", False))
    if not can_reply:
        return

    def work():
        try:
            thr = int(thread_id) if thread_id else None
            # «печатает…» сразу
            try:
                tg.api(
                    cfg,
                    "sendChatAction",
                    {
                        "chat_id": chat_id,
                        "action": "typing",
                        **({"message_thread_id": thr} if thr else {}),
                    },
                )
            except Exception:
                pass
            # пост / тред как ФОН: reply chain + top of thread
            post_ctx = ""
            rt = msg.get("reply_to_message") or {}
            if rt:
                post_ctx = (rt.get("text") or rt.get("caption") or "")[:700]
                if not post_ctx and rt.get("reply_to_message"):
                    rr = rt["reply_to_message"]
                    post_ctx = (rr.get("text") or rr.get("caption") or "")[:700]
                # авто-форвард поста канала
                if not post_ctx and (
                    rt.get("is_automatic_forward") or rt.get("forward_from_message_id")
                ):
                    post_ctx = (rt.get("text") or rt.get("caption") or "")[:700]

            def _send_comment_reply(body: str) -> dict:
                """Reply на коммент (под постом). Если target пропал — всё равно отправим, не молчим."""
                kwargs = dict(
                    parse_mode=None,
                    message_thread_id=thr,
                    disable_preview=True,
                )
                if mid:
                    try:
                        return tg.send_message(
                            cfg,
                            chat_id,
                            body,
                            reply_to=int(mid),
                            allow_without_reply=False,
                            **kwargs,
                        )
                    except Exception as e1:
                        print("comment strict reply fail, retry soft", e1, flush=True)
                        return tg.send_message(
                            cfg,
                            chat_id,
                            body,
                            reply_to=int(mid),
                            allow_without_reply=True,
                            **kwargs,
                        )
                return tg.send_message(cfg, chat_id, body, **kwargs)

            # stub «…» ВЫКЛ по умолчанию (точки бесили + ломали reply)
            stub_mid = None
            use_stub = bool(cfg.get("comment_stub_then_edit", False)) and instant
            if use_stub:
                try:
                    from content import try_instant_comment

                    stub = try_instant_comment(text, username=str(uname))
                    if stub:
                        # полный instant — один reply, без Grok
                        _send_comment_reply(stub)
                        print(f"comment instant out mid={mid}", flush=True)
                        st = load_state()
                        _log_comment_event(
                            st,
                            {
                                "status": "replied",
                                "from_name": uname,
                                "from_id": uid,
                                "comment_text": text[:400],
                                "reply_text": stub[:400],
                                "message_id": mid,
                                "fast": True,
                            },
                        )
                        return
                    # плейсхолдер только если явно включён stub
                    r0 = _send_comment_reply("⏳")
                    stub_mid = (r0 or {}).get("message_id")
                except Exception as e:
                    print("comment stub fail", e, flush=True)
                    stub_mid = None

            reply = generate_comment_reply(
                text,
                post_context=post_ctx,
                username=str(uname),
            )
            if not (reply or "").strip():
                reply = "Йо, я тут 🔥"
            st = load_state()
            if instant:
                sent_ok = False
                if stub_mid:
                    try:
                        tg.edit_message_text(
                            cfg,
                            chat_id,
                            int(stub_mid),
                            reply,
                            parse_mode=None,
                        )
                        print(
                            f"comment edit mid={stub_mid} reply={reply[:80]!r}",
                            flush=True,
                        )
                        sent_ok = True
                    except Exception as e:
                        print("comment edit fail, send new", e, flush=True)
                if not sent_ok:
                    try:
                        _send_comment_reply(reply)
                        print(f"comment out: mid={mid} reply={reply[:80]!r}", flush=True)
                        sent_ok = True
                    except Exception as e:
                        # fallback: без thread, но ВСЕГДА reply_to
                        print("comment reply strict fail", e, flush=True)
                        try:
                            tg.send_message(
                                cfg,
                                chat_id,
                                reply,
                                reply_to=int(mid) if mid else None,
                                parse_mode=None,
                                allow_without_reply=False,
                                disable_preview=True,
                            )
                            print(f"comment out fallback reply_to mid={mid}", flush=True)
                            sent_ok = True
                        except Exception as e2:
                            print("comment out hard fail", e2, flush=True)
                            raise
                _log_comment_event(
                    st,
                    {
                        "status": "replied",
                        "from_name": uname,
                        "from_id": uid,
                        "comment_text": text[:400],
                        "reply_text": reply[:400],
                        "message_id": mid,
                        "thread_id": thr,
                    },
                )
                if cfg.get("notify_owner_on_comment", False):
                    notify_owner(
                        cfg,
                        f"💬 <b>Ответил в комменты</b>\n"
                        f"От: {html.escape(str(uname))}\n"
                        f"<i>{html.escape(text[:280])}</i>\n\n"
                        f"→ {html.escape(reply[:280])}",
                    )
            else:
                item = add_pending_comment(
                    st,
                    {
                        "chat_id": chat_id,
                        "message_id": mid,
                        "from_id": uid,
                        "from_name": uname,
                        "comment_text": text,
                        "reply_text": reply,
                        "message_thread_id": thread_id,
                    },
                )
                notify_owner(
                    cfg,
                    f"💬 Коммент → <code>{item['id']}</code>\n"
                    f"От: {html.escape(str(uname))}\n"
                    f"<i>{html.escape(text[:400])}</i>\n\n"
                    f"<b>Ответ:</b>\n{html.escape(reply)}",
                    reply_markup=comment_keyboard(item["id"]),
                )
        except Exception as e:
            print("comment work fail", e, flush=True)
            traceback.print_exc()
            try:
                notify_owner(
                    cfg,
                    f"❌ Не смог ответить в комменты\n"
                    f"От: {html.escape(str(uname))}\n"
                    f"<i>{html.escape(text[:200])}</i>\n"
                    f"{html.escape(str(e)[:300])}",
                )
            except Exception:
                pass

    threading.Thread(target=work, daemon=True).start()


def run() -> None:
    # Домашний ПК (Windows): не polling'ить, если облако должно крутить бота.
    # На Bothost (Linux) этот gate НЕ срабатывает — иначе бот молчит!
    try:
        import os as _os

        cfg0 = load_config()
        is_home_windows = _os.name == "nt"
        cloudish = bool(cfg0.get("local_bot_disabled")) or str(
            cfg0.get("bot_host_mode") or ""
        ).lower() in ("cloud", "bothost", "hosting")
        if is_home_windows and cloudish:
            msg = (
                "Локальный bot.py ВЫКЛЮЧЕН (Windows + cloud/local_bot_disabled).\n"
                "Крутится Bothost. Локально: local_bot_disabled=false, bot_host_mode=local."
            )
            print(msg, flush=True)
            try:
                (Path(__file__).resolve().parent / "bot_run.log").open("a", encoding="utf-8").write(
                    f"\n=== skip local {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n{msg}\n"
                )
            except Exception:
                pass
            return
    except Exception as e:
        print("cloud-gate", e, flush=True)

    # баланс: seed с GitHub (Bothost) + remote home-bridge если жив
    try:
        n = bal.apply_balance_seed(force=False)
        print(f"balance seed wallets_updated={n}", flush=True)
        print(
            f"balance remote={bal._use_remote_balance()} "
            f"ibramosta={bal.get_balance(8581306681)}",
            flush=True,
        )
    except Exception as e:
        print("balance boot", e, flush=True)

    # один bot.py — иначе Telegram 409 Conflict
    try:
        from single_instance import acquire_lock

        acquire_lock("bot")
    except SystemExit:
        raise
    except Exception as e:
        print("lock fail", e)

    # лог в файл — чтобы видеть, почему молчит
    log_path = Path(__file__).resolve().parent / "bot_run.log"
    try:
        class _Tee:
            def __init__(self, *streams):
                self.streams = streams

            def write(self, data):
                for s in self.streams:
                    try:
                        s.write(data)
                        s.flush()
                    except Exception:
                        pass

            def flush(self):
                for s in self.streams:
                    try:
                        s.flush()
                    except Exception:
                        pass

        _logf = open(log_path, "a", encoding="utf-8", errors="replace")
        sys.stdout = _Tee(sys.__stdout__, _logf)
        sys.stderr = _Tee(sys.__stderr__, _logf)
        print(f"\n=== bot start {time.strftime('%Y-%m-%d %H:%M:%S')} ===", flush=True)
    except Exception as e:
        print("log open fail", e)

    cfg = load_config()
    if not (cfg.get("bot_token") or "").strip():
        print("ОШИБКА: bot_token пустой")
        sys.exit(1)

    me = tg.get_me(cfg)
    print(f"Бот: @{me.get('username')} id={me.get('id')}")
    print(f"CODE_VERSION={BOT_CODE_VERSION}", flush=True)

    import os as _os

    on_bothost = bool((_os.environ.get("BOT_ID") or "").strip())

    # 3.0: ВСЕГДА подтянуть участников из giveaway_restore.json (force merge)
    try:
        res_gw = gw.apply_restore_seed(force=True)
        print("giveaway restore 3.0:", res_gw, flush=True)
    except Exception as e:
        print("giveaway restore fail", e, flush=True)

    # при старте на Bothost — сверить SHA и подтянуть код
    def _boot_pull_once() -> None:
        flag = (_os.environ.get("AUTO_GITHUB_PULL") or "").strip().lower()
        enabled = flag in ("1", "true", "yes") or (
            on_bothost and flag not in ("0", "false", "no")
        )
        if not enabled:
            print("boot pull: off", flush=True)
            return
        try:
            import deploy_lib

            need, remote, local = deploy_lib.needs_update()
            print(
                f"boot pull: need={need} remote={(remote or '')[:12]} local={(local or '')[:12]}",
                flush=True,
            )
            if need:
                res = deploy_lib.redeploy_now(restart=True)
                print("boot pull result", res, flush=True)
                time.sleep(8)
        except Exception as e:
            print("boot pull err", e, flush=True)

    if on_bothost:
        _boot_pull_once()

    # авто-pull с GitHub (Bothost без кнопки auto-deploy)
    def _github_autopull_loop() -> None:
        flag = (_os.environ.get("AUTO_GITHUB_PULL") or "").strip().lower()
        enabled = flag in ("1", "true", "yes") or (
            on_bothost and flag not in ("0", "false", "no")
        )
        if not enabled:
            print("github autopull: off", flush=True)
            return
        interval = int(_os.environ.get("AUTO_GITHUB_PULL_SEC") or "120")
        print(f"github autopull: every {interval}s", flush=True)
        time.sleep(90)
        while True:
            try:
                import deploy_lib

                need, remote, local = deploy_lib.needs_update()
                if need:
                    print(
                        f"github autopull: new {remote[:12]} (was {local[:12] or 'none'})",
                        flush=True,
                    )
                    res = deploy_lib.redeploy_now(restart=True)
                    print("github autopull result", res, flush=True)
                    try:
                        stn = load_state()
                        pull = res.get("pull") or {}
                        rst = res.get("restart") or {}
                        ui_edit_or_send(
                            cfg,
                            5740061551,
                            "🔄 <b>Auto-pull</b>\n"
                            f"sha <code>{html.escape(remote[:12])}</code>\n"
                            f"files {pull.get('count')}\n"
                            f"restart: {html.escape(str(rst.get('method') or rst.get('message') or rst)[:120])}",
                            state=stn,
                            uid=5740061551,
                            store_key="owner_notify_msg",
                        )
                    except Exception:
                        pass
                    time.sleep(30)
                else:
                    print("github autopull: up to date", flush=True)
            except Exception as e:
                print("github autopull err", e, flush=True)
            time.sleep(max(60, interval))

    try:
        threading.Thread(target=_github_autopull_loop, name="gh-pull", daemon=True).start()
    except Exception as e:
        print("autopull thread fail", e, flush=True)
    # мозг / мост — сразу в лог Bothost
    try:
        from content import brain_status, _bridge_url

        st = brain_status(cfg, use_cache=False, probe_ollama=False)
        bru = _bridge_url(cfg)
        print(
            f"brain active={st.get('active')} grok={st.get('grok')} "
            f"source={st.get('grok_source')} model={st.get('grok_model')} "
            f"tools={st.get('grok_tools')} bridge={bru or '-'}",
            flush=True,
        )
        # Одно меню 4.0 при старте
        try:
            st_ui = load_state()
            uid_boot = int((cfg.get("owner_user_ids") or [5740061551])[0])
            mode = str(cfg.get("bot_host_mode") or "local")
            src = html.escape(str(st.get("grok_source") or "—"))
            _owner_panel(
                cfg,
                st_ui,
                uid_boot,
                None,
                uid_boot,
                owner_home_html()
                + f"\n\n🟢 <b>online {BOT_CODE_VERSION}</b> · {html.escape(mode)}\n"
                + f"brain: <code>{src}</code>"
                + (
                    f"\nbridge: <code>{html.escape((bru or '')[:48])}</code>"
                    if bru
                    else "\nbridge: off (local session)"
                ),
                main_menu_keyboard(),
                force_new=False,
            )
            save_state(st_ui)
        except Exception as e:
            print("boot ui fail", e, flush=True)
    except Exception as e:
        print("brain boot fail", e, flush=True)
    if not me.get("can_read_all_group_messages"):
        print(
            "WARN: privacy mode ON (can_read_all_group_messages=false). "
            "Бот-админ группы обычно всё равно видит комменты. "
            "Если молчит — BotFather → /setprivacy → Disable",
            flush=True,
        )
    # профиль бота (как у SaveMod-style: коротко и ясно)
    try:
        tg.set_my_short_description(
            cfg,
            "Director Vaggo · заказы, розыгрыши, ИИ-помощник канала @Vaggo01",
        )
        tg.set_my_description(
            cfg,
            "Вагго — заказы (бот/сайт/дизайн), розыгрыши, ответы в комментах канала.\n"
            "/start — меню · /order — заказ · /support — поддержка\n"
            "/legal — политика и оферта (всегда доступны)\n"
            "Канал: @Vaggo01",
        )
    except Exception as e:
        print("set description fail", e, flush=True)

    public_cmds = [
        {"command": "start", "description": "🏠 Меню"},
        {"command": "order", "description": "🛠 Заказать"},
        {"command": "myorders", "description": "📦 Мои заказы"},
        {"command": "balance", "description": "💳 Баланс"},
        {"command": "prices", "description": "💰 Прайс"},
        {"command": "legal", "description": "📋 Документы (всегда)"},
        {"command": "privacy", "description": "🔒 Политика"},
        {"command": "offer", "description": "📜 Оферта"},
        {"command": "support", "description": "🆘 Поддержка"},
        {"command": "ref", "description": "🔗 Реферальная ссылка"},
    ]
    owner_cmds = [
        {"command": "start", "description": "🏠 Пульт"},
        {"command": "ping", "description": "Версия / Grok / GW"},
        {"command": "queue", "description": "Очередь постов"},
        {"command": "gstatus", "description": "Розыгрыш"},
        {"command": "orders", "description": "Заказы"},
        {"command": "radar", "description": "Финрадар"},
        {"command": "hot", "description": "Горящие заказы"},
        {"command": "tl", "description": "Тимлид"},
        {"command": "contract", "description": "Договор: /contract id"},
        {"command": "ref", "description": "Рефералка"},
        {"command": "clean", "description": "Почистить ЛС"},
        {"command": "brains", "description": "Grok статус"},
    ]
    try:
        tg.set_commands(cfg, public_cmds)  # default
        tg.set_commands(cfg, public_cmds, scope={"type": "all_private_chats"})
        for oid in cfg.get("owner_user_ids") or []:
            try:
                tg.set_commands(
                    cfg, owner_cmds, scope={"type": "chat", "chat_id": int(oid)}
                )
            except Exception as e:
                print("set owner commands", oid, e, flush=True)
    except Exception as e:
        print("set_commands fail", e, flush=True)

    state = load_state()
    offset = int(state.get("offset") or 0)
    print("Polling… Ctrl+C стоп")

    while True:
        try:
            cfg = load_config()
            state = load_state()
            # Очередь постов — внутри бота, отдельный publisher не обязателен
            try:
                tick_schedule_queue(cfg)
            except Exception as qe:
                print("queue tick error", qe, flush=True)
            try:
                tick_giveaways(cfg)
            except Exception as ge:
                print("giveaway tick error", ge, flush=True)
            try:
                tick_order_reports(cfg)
            except Exception as re:
                print("order report tick", re, flush=True)
            try:
                tick_finance_digest(cfg)
            except Exception as fe:
                print("finance digest tick", fe, flush=True)
            # Platega: автозачисление по статусу API (Bothost без webhook)
            try:
                import platega_lib as _platega

                if _platega.ready(cfg):
                    for ev in _platega.poll_pending(cfg, limit=20):
                        pay = ev.get("payment") or {}
                        if ev.get("action") == "credit" and pay:
                            uid_p = int(pay.get("user_id") or 0)
                            amt = int(pay.get("amount") or 0)
                            if uid_p:
                                try:
                                    bal_now = bal.get_balance(uid_p)
                                    tg.send_message(
                                        cfg,
                                        uid_p,
                                        f"✅ <b>Баланс пополнен</b>\n"
                                        f"+{amt} ₽ · итого <b>{bal_now}</b> ₽\n"
                                        f"/order · /balance",
                                        parse_mode="HTML",
                                    )
                                except Exception as ne:
                                    print("platega notify user", ne, flush=True)
                                notify_owner(
                                    cfg,
                                    f"✅ Platega auto uid=<code>{uid_p}</code> "
                                    f"+{amt} ₽ · <code>{html.escape(str(pay.get('id')))}</code>",
                                )
            except Exception as pe:
                print("platega poll", type(pe).__name__, str(pe)[:120], flush=True)
            updates = tg.get_updates(cfg, offset=offset, timeout=25)
            dirty = False
            for u in updates:
                offset = u["update_id"] + 1
                state["offset"] = offset
                dirty = True
                try:
                    if "callback_query" in u:
                        # кнопки — без лишнего load_state (быстрее)
                        handle_callback(cfg, state, u["callback_query"])
                    elif "channel_post" in u:
                        maybe_react_channel_post(cfg, state, u["channel_post"])
                    elif "message" in u:
                        msg = u["message"]
                        chat_type = (msg.get("chat") or {}).get("type")
                        if chat_type in ("group", "supergroup"):
                            if maybe_bind_group(cfg, msg):
                                continue
                            if maybe_seed_under_channel_forward(cfg, state, msg):
                                continue
                            maybe_hint_unknown_group(cfg, state, msg)
                            maybe_handle_discussion(cfg, state, msg)
                        elif chat_type == "private":
                            # 0) служебное владельца — ДО всего (redeploy/ping)
                            if handle_owner_system(cfg, state, msg):
                                continue
                            # прайс: await цены/скидки
                            if handle_pricing_await(cfg, state, msg):
                                continue
                            # владелец: блок/разблок
                            if handle_mod_owner_commands(cfg, state, msg):
                                continue
                            # условия — до всего остального
                            if handle_terms_private(cfg, state, msg):
                                continue
                            # тикеты: await / auto-допись (до orders, чтобы не съедать ТЗ)
                            if handle_support_private(cfg, state, msg):
                                continue
                            # автоблок (незаконное ТЗ)
                            if require_not_blocked(cfg, msg):
                                continue
                            if require_terms_or_gate(cfg, state, msg):
                                continue
                            if handle_balance_private(cfg, state, msg):
                                continue
                            if handle_orders_private(cfg, state, msg):
                                continue
                            if handle_giveaway_private(cfg, state, msg):
                                continue
                            handle_command(cfg, state, msg)
                except Exception as ue:
                    # не роняем весь polling из‑за одной кнопки
                    print("update err", type(ue).__name__, str(ue)[:160], flush=True)
            # один save на пачку — иначе каждый offset = тяжёлый merge+disk
            if dirty:
                save_state(state)
        except KeyboardInterrupt:
            print("\nСтоп.")
            break
        except Exception as e:
            err = str(e)
            short = err.split("for url:")[0].strip() if "for url:" in err else err[:200]
            print(f"loop: {short}", flush=True)
            if "NameError" in err or "Traceback" in err:
                print(traceback.format_exc()[-500:], flush=True)
            state = load_state()
            state["last_error"] = short[:500]
            save_state(state)
            if "409" in err or "Conflict" in err:
                print("409 Conflict: жду 10с", flush=True)
                time.sleep(10)
            else:
                time.sleep(2)


if __name__ == "__main__":
    run()
