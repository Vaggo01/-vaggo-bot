"""Низкоуровневые вызовы Telegram Bot API."""
from __future__ import annotations

import json
from typing import Any

import requests


def _proxies(cfg: dict) -> dict | None:
    url = (cfg.get("proxy_url") or "").strip()
    if not url:
        return None
    return {"http": url, "https": url}


def api(cfg: dict, method: str, *, data: dict | None = None, files=None, timeout: int = 45) -> dict:
    token = (cfg.get("bot_token") or "").strip()
    if not token:
        raise RuntimeError("bot_token пустой — вставь токен в config.json")
    url = f"https://api.telegram.org/bot{token}/{method}"
    proxies = _proxies(cfg)
    if files:
        resp = requests.post(url, data=data or {}, files=files, timeout=timeout, proxies=proxies)
    else:
        resp = requests.post(url, data=data or {}, timeout=timeout, proxies=proxies)
    # Telegram часто отдаёт 400 + JSON с description — не теряем текст
    # (иначе edit «message is not modified» выглядит как HTTPError и бот спамит send)
    try:
        payload = resp.json()
    except Exception:
        resp.raise_for_status()
        raise RuntimeError(f"Telegram API bad response: {resp.status_code} {resp.text[:200]}")
    if not payload.get("ok"):
        desc = payload.get("description") or str(payload)
        raise RuntimeError(f"Telegram API error: {desc}")
    return payload.get("result")


def get_me(cfg: dict) -> dict:
    return api(cfg, "getMe")


def get_updates(cfg: dict, offset: int | None = None, timeout: int = 25) -> list:
    data: dict[str, Any] = {"timeout": timeout, "allowed_updates": json.dumps(["message", "callback_query", "channel_post"])}
    if offset is not None:
        data["offset"] = offset
    return api(cfg, "getUpdates", data=data, timeout=timeout + 10) or []


def send_message(
    cfg: dict,
    chat_id: int | str,
    text: str,
    *,
    parse_mode: str | None = "HTML",
    reply_markup: dict | None = None,
    reply_to: int | None = None,
    disable_preview: bool = False,
    message_thread_id: int | None = None,
    allow_without_reply: bool = True,
) -> dict:
    data: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text[:4096],
        "disable_web_page_preview": "true" if disable_preview else "false",
    }
    if parse_mode:
        data["parse_mode"] = parse_mode
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    if reply_to is not None:
        data["reply_to_message_id"] = int(reply_to)
        # false = строго reply; иначе Telegram кидает «просто в чат» (комменты отваливаются)
        data["allow_sending_without_reply"] = "true" if allow_without_reply else "false"
    if message_thread_id is not None:
        data["message_thread_id"] = int(message_thread_id)
    return api(cfg, "sendMessage", data=data)


def comment_on_channel_post(
    cfg: dict,
    channel_message_id: int,
    text: str,
    *,
    parse_mode: str | None = None,
    disable_preview: bool = True,
    discuss_root_id: int | None = None,
) -> dict:
    """
    Коммент ПОД постом канала (виден в «комментариях» у поста).

    Правильный способ: reply_to на авто-форвард поста в группе обсуждений
    (id корня треда), а не reply_parameters на id поста канала —
    иначе сообщение падает в общий чат Admin и из комментариев «пропадает».
    """
    import requests

    from state import load_state

    token = (cfg.get("bot_token") or "").strip()
    if not token:
        raise RuntimeError("bot_token пустой")
    disc = cfg.get("discussion_group_id")
    if not disc:
        raise RuntimeError("discussion_group_id не задан — /bind")

    root = discuss_root_id
    if root is None:
        roots = (load_state().get("channel_discuss_root") or {})
        root = roots.get(str(channel_message_id)) or roots.get(int(channel_message_id))
    if not root:
        raise RuntimeError(
            f"Нет discuss root для channel msg {channel_message_id}. "
            "Нужен id авто-форварда в группе (пишется при seed)."
        )

    payload: dict[str, Any] = {
        "chat_id": disc,
        "text": (text or "")[:4096],
        "disable_web_page_preview": disable_preview,
        "reply_to_message_id": int(root),
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    proxies = _proxies(cfg)
    resp = requests.post(url, json=payload, timeout=45, proxies=proxies)
    body = resp.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram API error: {body}")
    return body.get("result") or {}


def send_document(cfg: dict, chat_id: int | str, path: str, caption: str = "") -> dict:
    with open(path, "rb") as f:
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption[:1024]
        return api(cfg, "sendDocument", data=data, files={"document": f})


def get_file(cfg: dict, file_id: str) -> dict:
    return api(cfg, "getFile", data={"file_id": file_id})


def download_file(cfg: dict, file_id: str, *, dest_dir: str | None = None, suffix: str = "") -> str:
    """Скачать файл Telegram (фото/документ) на диск. Возвращает path str."""
    import time
    from pathlib import Path

    info = get_file(cfg, file_id)
    fpath = info.get("file_path") or ""
    if not fpath:
        raise RuntimeError(f"getFile без file_path: {info}")
    token = (cfg.get("bot_token") or "").strip()
    url = f"https://api.telegram.org/file/bot{token}/{fpath}"
    proxies = _proxies(cfg)
    resp = requests.get(url, timeout=120, proxies=proxies)
    resp.raise_for_status()
    root = Path(__file__).resolve().parent / "media" / "uploads"
    out_dir = Path(dest_dir) if dest_dir else root
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = suffix or Path(fpath).suffix or ".jpg"
    if not ext.startswith("."):
        ext = "." + ext
    name = f"owner_{int(time.time())}_{abs(hash(file_id)) % 10**8}{ext}"
    path = out_dir / name
    path.write_bytes(resp.content)
    return str(path)


def send_photo(
    cfg: dict,
    chat_id: int | str,
    path: str,
    caption: str = "",
    *,
    parse_mode: str | None = "HTML",
    reply_markup: dict | None = None,
) -> dict:
    token = (cfg.get("bot_token") or "").strip()
    if not token:
        raise RuntimeError("bot_token пустой")
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    proxies = _proxies(cfg)
    data: dict[str, Any] = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption[:1024]
        if parse_mode:
            data["parse_mode"] = parse_mode
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup)
    with open(path, "rb") as f:
        resp = requests.post(url, data=data, files={"photo": f}, timeout=120, proxies=proxies)
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("ok"):
        raise RuntimeError(f"sendPhoto: {payload}")
    return payload.get("result")


def send_video(
    cfg: dict,
    chat_id: int | str,
    path: str,
    caption: str = "",
    *,
    parse_mode: str | None = "HTML",
) -> dict:
    token = (cfg.get("bot_token") or "").strip()
    if not token:
        raise RuntimeError("bot_token пустой")
    url = f"https://api.telegram.org/bot{token}/sendVideo"
    proxies = _proxies(cfg)
    data: dict[str, Any] = {"chat_id": chat_id, "supports_streaming": "true"}
    if caption:
        data["caption"] = caption[:1024]
        if parse_mode:
            data["parse_mode"] = parse_mode
    with open(path, "rb") as f:
        resp = requests.post(url, data=data, files={"video": f}, timeout=300, proxies=proxies)
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("ok"):
        raise RuntimeError(f"sendVideo: {payload}")
    return payload.get("result")


def answer_callback(
    cfg: dict,
    callback_id: str,
    text: str = "",
    *,
    show_alert: bool = False,
) -> None:
    data: dict[str, Any] = {"callback_query_id": callback_id}
    if text:
        data["text"] = text[:200]
    if show_alert:
        data["show_alert"] = "true"
    api(cfg, "answerCallbackQuery", data=data)


def edit_reply_markup(cfg: dict, chat_id: int | str, message_id: int, markup: dict | None = None) -> None:
    data: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id}
    if markup is not None:
        data["reply_markup"] = json.dumps(markup)
    try:
        api(cfg, "editMessageReplyMarkup", data=data)
    except Exception:
        pass


def edit_message_text(
    cfg: dict,
    chat_id: int | str,
    message_id: int,
    text: str,
    *,
    parse_mode: str | None = "HTML",
    reply_markup: dict | None = None,
    disable_preview: bool = True,
) -> dict | None:
    """Редактировать своё сообщение (замена вместо спама)."""
    data: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": int(message_id),
        "text": (text or "")[:4096],
        "disable_web_page_preview": "true" if disable_preview else "false",
    }
    if parse_mode:
        data["parse_mode"] = parse_mode
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup)
    try:
        return api(cfg, "editMessageText", data=data)
    except Exception as e:
        err = str(e).lower()
        if "message is not modified" in err:
            return None
        raise


def set_commands(
    cfg: dict,
    commands: list[dict],
    *,
    scope: dict | None = None,
) -> None:
    data: dict[str, Any] = {"commands": json.dumps(commands)}
    if scope:
        data["scope"] = json.dumps(scope)
    api(cfg, "setMyCommands", data=data)


def set_my_description(cfg: dict, description: str, *, language_code: str | None = None) -> None:
    """«Что может этот бот» (до 512)."""
    data: dict[str, Any] = {"description": (description or "")[:512]}
    if language_code:
        data["language_code"] = language_code
    try:
        api(cfg, "setMyDescription", data=data)
    except Exception:
        pass


def set_my_short_description(
    cfg: dict, description: str, *, language_code: str | None = None
) -> None:
    """Короткое описание в профиле (до 120)."""
    data: dict[str, Any] = {"short_description": (description or "")[:120]}
    if language_code:
        data["language_code"] = language_code
    try:
        api(cfg, "setMyShortDescription", data=data)
    except Exception:
        pass


def delete_commands(cfg: dict, *, scope: dict | None = None) -> None:
    data: dict[str, Any] = {}
    if scope:
        data["scope"] = json.dumps(scope)
    try:
        api(cfg, "deleteMyCommands", data=data or None)
    except Exception:
        pass


def get_chat(cfg: dict, chat_id: int | str) -> dict:
    return api(cfg, "getChat", data={"chat_id": chat_id})


def get_chat_member(cfg: dict, chat_id: int | str, user_id: int) -> dict:
    return api(cfg, "getChatMember", data={"chat_id": chat_id, "user_id": user_id})


def get_chat_administrators(cfg: dict, chat_id: int | str) -> list:
    return api(cfg, "getChatAdministrators", data={"chat_id": chat_id}) or []


def pin_chat_message(cfg: dict, chat_id: int | str, message_id: int, *, silent: bool = True) -> bool:
    """Закрепить сообщение. В личке с ботом обычно ок; ошибки глотаем."""
    data = {
        "chat_id": chat_id,
        "message_id": int(message_id),
        "disable_notification": "true" if silent else "false",
    }
    try:
        api(cfg, "pinChatMessage", data=data)
        return True
    except Exception as e:
        err = str(e).lower()
        # already pinned / not modified — ок
        if "already" in err or "not modified" in err:
            return True
        print("pin fail", str(e)[:120], flush=True)
        return False


def delete_message(cfg: dict, chat_id: int | str, message_id: int) -> bool:
    api(cfg, "deleteMessage", data={"chat_id": chat_id, "message_id": int(message_id)})
    return True


def try_delete_message(cfg: dict, chat_id: int | str, message_id: int | None) -> bool:
    """Удалить сообщение без падения (чужое / старое / уже удалено)."""
    if not message_id:
        return False
    try:
        delete_message(cfg, chat_id, int(message_id))
        return True
    except Exception:
        return False


def _no_msg_permissions() -> dict:
    """Полный mute / отстранение от переписки."""
    return {
        "can_send_messages": False,
        "can_send_audios": False,
        "can_send_documents": False,
        "can_send_photos": False,
        "can_send_videos": False,
        "can_send_video_notes": False,
        "can_send_voice_notes": False,
        "can_send_polls": False,
        "can_send_other_messages": False,
        "can_add_web_page_previews": False,
        "can_change_info": False,
        "can_invite_users": False,
        "can_pin_messages": False,
        "can_manage_topics": False,
    }


def restrict_chat_member(
    cfg: dict,
    chat_id: int | str,
    user_id: int,
    *,
    until_date: int = 0,
    permissions: dict | None = None,
) -> dict:
    """Мут / временное отстранение (бот должен быть админом с can_restrict_members)."""
    perms = permissions or _no_msg_permissions()
    data: dict[str, Any] = {
        "chat_id": chat_id,
        "user_id": int(user_id),
        "permissions": json.dumps(perms),
    }
    if until_date:
        data["until_date"] = int(until_date)
    return api(cfg, "restrictChatMember", data=data)


def ban_chat_member(
    cfg: dict,
    chat_id: int | str,
    user_id: int,
    *,
    until_date: int = 0,
    revoke_messages: bool = False,
) -> dict:
    """Бан в группе/чате обсуждений."""
    data: dict[str, Any] = {
        "chat_id": chat_id,
        "user_id": int(user_id),
        "revoke_messages": "true" if revoke_messages else "false",
    }
    if until_date:
        data["until_date"] = int(until_date)
    return api(cfg, "banChatMember", data=data)


def unban_chat_member(
    cfg: dict,
    chat_id: int | str,
    user_id: int,
    *,
    only_if_banned: bool = True,
) -> dict:
    data = {
        "chat_id": chat_id,
        "user_id": int(user_id),
        "only_if_banned": "true" if only_if_banned else "false",
    }
    return api(cfg, "unbanChatMember", data=data)


# Базовые эмодзи реакций Telegram (не любые)
ALLOWED_REACTIONS = {
    "👍", "👎", "❤", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱",
    "🤬", "😢", "🎉", "🤩", "🤮", "💩", "🙏", "👌", "🕊", "🤡",
    "🥱", "🥴", "😍", "🐳", "❤‍🔥", "🌚", "🌭", "💯", "🤣", "⚡",
    "🍌", "🏆", "💔", "🤨", "😐", "🍓", "🍾", "💋", "🖕", "😈",
    "😴", "😭", "🤓", "👻", "👨‍💻", "👀", "🎃", "🙈", "😇", "😨",
    "🤝", "✍", "🤗", "🫡", "🎅", "🎄", "☃", "💅", "🤪", "🗿",
    "🆒", "💘", "🙉", "🦄", "😘", "💊", "🙊", "😎", "👾", "🤷‍♂",
    "🤷", "🤷‍♀", "😡",
}


def set_message_reaction(
    cfg: dict,
    chat_id: int | str,
    message_id: int,
    emoji: str = "🔥",
    *,
    big: bool = False,
) -> dict:
    """Поставить реакцию ботом на сообщение (канал / группа / личка)."""
    emoji = (emoji or "🔥").strip()
    # нормализация частых вариантов
    if emoji in ("❤️", "♥"):
        emoji = "❤"
    if emoji not in ALLOWED_REACTIONS:
        emoji = "🔥"
    reaction = json.dumps([{"type": "emoji", "emoji": emoji}])
    data = {
        "chat_id": chat_id,
        "message_id": int(message_id),
        "reaction": reaction,
        "is_big": "true" if big else "false",
    }
    try:
        return api(cfg, "setMessageReaction", data=data)
    except Exception:
        # повтор с numeric channel id
        num = cfg.get("channel_numeric_id")
        if num and str(chat_id) != str(num):
            data["chat_id"] = num
            return api(cfg, "setMessageReaction", data=data)
        raise
