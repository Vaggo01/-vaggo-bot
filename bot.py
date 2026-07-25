"""
Director Vaggo вЂ” Channel Manager (Bothost cloud + optional home Grok bridge).

Р’Р»Р°РґРµР»РµС†: РїСѓР»СЊС‚, РѕС‡РµСЂРµРґСЊ, С‡РµСЂРЅРѕРІРёРєРё, РєРѕРјРјРµРЅС‚С‹, СЂРѕР·С‹РіСЂС‹С€, Р·Р°РєР°Р·С‹
РљР»РёРµРЅС‚: terms в†’ Р·Р°РєР°Р· в†’ support
Grok: XAI_API_KEY РЅР° Bothost  РР›Р  bridge (РџРљ + tunnel)  РР›Р  local session
Deploy: GitHub main в†’ push_bothost.ps1 / /redeploy

РќРµ Р»РѕРјР°РµРј: media/giveaways.json + giveaway_restore.json
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

# chat_mod РѕРїС†РёРѕРЅР°Р»РµРЅ вЂ” РїР°РєРµС‚ Bothost РЅРµ РґРѕР»Р¶РµРЅ РїР°РґР°С‚СЊ, РµСЃР»Рё С„Р°Р№Р» Р·Р°Р±С‹Р»Рё
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
    print("WARN: chat_mod_lib missing вЂ” chat moderation disabled", flush=True)

_last_queue_tick = 0.0
# 4.6.8 вЂ” orders without Grok ok; hide similar to client; pay gate; balance seed
BOT_CODE_VERSION = "4.7.1"


def is_owner(cfg: dict, user: dict | None) -> bool:
    """Р’Р»Р°РґРµР»РµС† СЃС‚СЂРѕРіРѕ РїРѕ Telegram id (РЅРµ РїРѕ В«РїРѕС…РѕР¶РµРјСѓВ» username / РєР°РЅР°Р»Сѓ)."""
    if not user:
        return False
    try:
        uid = int(user.get("id") or 0)
    except Exception:
        return False
    if not uid:
        return False
    ids = {5740061551}  # РєР°РЅРѕРЅ
    for x in cfg.get("owner_user_ids") or []:
        try:
            ids.add(int(x))
        except Exception:
            pass
    return uid in ids


def is_giveaway_excluded(cfg: dict, user: dict | None) -> bool:
    """Р’Р»Р°РґРµР»РµС† / С‚РµСЃС‚РѕРІС‹Рµ Р°РєРєРё вЂ” РЅРµ РІ Р±Р°СЂР°Р±Р°РЅРµ."""
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
                {"text": "вњ… Р’ РєР°РЅР°Р»", "callback_data": f"pub:{draft_id}"},
                {"text": "вњЏпёЏ РџРµСЂРµРїРёСЃР°С‚СЊ", "callback_data": f"rew:{draft_id}"},
            ],
            [
                {"text": "рџ—‘ РЈРґР°Р»РёС‚СЊ", "callback_data": f"drop:{draft_id}"},
                {"text": "рџЏ  РњРµРЅСЋ", "callback_data": "menu:home"},
            ],
        ]
    }


def comment_keyboard(cid: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "вњ… РћС‚РІРµС‚РёС‚СЊ", "callback_data": f"creply:{cid}"},
                {"text": "вњЏпёЏ Р•С‰С‘ РІР°СЂРёР°РЅС‚", "callback_data": f"crewrite:{cid}"},
            ],
            [
                {"text": "вЏ­ РџСЂРѕРїСѓСЃРє", "callback_data": f"cskip:{cid}"},
                {"text": "рџЏ  РњРµРЅСЋ", "callback_data": "menu:home"},
            ],
        ]
    }


def main_menu_keyboard() -> dict:
    """Р“Р»Р°РІРЅС‹Р№ РїСѓР»СЊС‚ РІР»Р°РґРµР»СЊС†Р° вЂ” С‡РёСЃС‚Рѕ, РєСЂСѓРїРЅРѕ, Р±РµР· РєР°С€Рё."""
    return {
        "inline_keyboard": [
            [{"text": "рџ“Љ  РЎРµРіРѕРґРЅСЏ", "callback_data": "menu:stats"}],
            [
                {"text": "рџ“…  РћС‡РµСЂРµРґСЊ", "callback_data": "menu:queue"},
                {"text": "рџљЂ  Р’С‹Р»РѕР¶РёС‚СЊ", "callback_data": "menu:qnow"},
            ],
            [
                {"text": "рџЋЃ  Р РѕР·С‹РіСЂС‹С€", "callback_data": "menu:giveaway"},
                {"text": "рџ›   Р—Р°РєР°Р·С‹", "callback_data": "menu:orders"},
            ],
            [
                {"text": "рџ’¬  РљРѕРјРјРµРЅС‚С‹", "callback_data": "menu:comments"},
                {"text": "рџ“ќ  Р§РµСЂРЅРѕРІРёРєРё", "callback_data": "menu:drafts"},
            ],
            [
                {"text": "вЏё  РџР°СѓР·Р°", "callback_data": "menu:toggle_pause"},
                {"text": "вљ™пёЏ  Р•С‰С‘", "callback_data": "menu:more"},
            ],
            [{"text": "рџ”„  РћР±РЅРѕРІРёС‚СЊ РїСѓР»СЊС‚", "callback_data": "menu:fresh"}],
        ]
    }


def owner_more_keyboard() -> dict:
    """РЎРµСЂРІРёСЃ вЂ” РІС‚РѕСЂРёС‡РЅС‹Р№ СЌРєСЂР°РЅ."""
    return {
        "inline_keyboard": [
            [{"text": "рџ§   Grok / РјРѕР·Рі", "callback_data": "menu:brains"}],
            [{"text": "рџ’°  Р‘Р°Р»Р°РЅСЃС‹", "callback_data": "menu:balance"}],
            [
                {"text": "рџ“Ў  Р Р°РґР°СЂ", "callback_data": "menu:radar"},
                {"text": "рџ”Ґ  Р“РѕСЂСЏС‰РёРµ", "callback_data": "menu:hot"},
            ],
            [
                {"text": "в™»пёЏ  Restore GW", "callback_data": "menu:gwrestore"},
                {"text": "рџ“Њ  РљРЅРѕРїРєРё GW", "callback_data": "menu:gfixkb"},
            ],
            [{"text": "рџ§№  РџРѕС‡РёСЃС‚РёС‚СЊ Р»РёС‡РєСѓ", "callback_data": "menu:clean"}],
            [{"text": "В«  Р’ РїСѓР»СЊС‚", "callback_data": "menu:home"}],
        ]
    }


def menu_result_keyboard(_group: str | None = None) -> dict:
    """РќРёР· Р»СЋР±РѕРіРѕ СЌРєСЂР°РЅР° СЂРµР·СѓР»СЊС‚Р°С‚Р° вЂ” РІСЃРµРіРґР° РїСѓС‚СЊ РґРѕРјРѕР№."""
    return {
        "inline_keyboard": [
            [
                {"text": "рџЏ   РџСѓР»СЊС‚", "callback_data": "menu:home"},
                {"text": "рџ”„  РћР±РЅРѕРІРёС‚СЊ", "callback_data": "menu:fresh"},
            ],
            [
                {"text": "рџЋЃ  Р РѕР·С‹РіСЂС‹С€", "callback_data": "menu:giveaway"},
                {"text": "рџ›   Р—Р°РєР°Р·С‹", "callback_data": "menu:orders"},
            ],
        ]
    }


def toast_keyboard() -> dict:
    """РљРЅРѕРїРєРё РїРѕРґ РѕС‚РґРµР»СЊРЅС‹Рј СѓРІРµРґРѕРјР»РµРЅРёРµРј (РЅРµ РїРµСЂРµС‚РёСЂР°РµС‚ РїСѓР»СЊС‚)."""
    return {
        "inline_keyboard": [
            [{"text": "рџЏ   РћС‚РєСЂС‹С‚СЊ РїСѓР»СЊС‚", "callback_data": "menu:fresh"}],
            [
                {"text": "рџ›   Р—Р°РєР°Р·С‹", "callback_data": "menu:orders"},
                {"text": "рџЋЃ  Р РѕР·С‹РіСЂС‹С€", "callback_data": "menu:giveaway"},
            ],
        ]
    }


def _host_brain_line(cfg: dict | None = None) -> str:
    """РљРѕСЂРѕС‚РєР°СЏ СЃС‚СЂРѕРєР°: host + brain source (РґР»СЏ РїСѓР»СЊС‚Р° / ping)."""
    try:
        from content import brain_status

        cfg = cfg or load_config()
        mode = str(cfg.get("bot_host_mode") or "local").lower()
        host = "вЃпёЏ cloud" if mode in ("cloud", "bothost", "hosting") else "рџ’» local"
        bst = brain_status(cfg, use_cache=True, probe_ollama=False)
        active = str(bst.get("active") or "вЂ”")
        src = str(bst.get("grok_source") or "вЂ”")
        if active == "grok":
            brain = f"рџ§  grok В· {html.escape(src)}"
        elif active == "ollama":
            brain = "рџ§  ollama"
        else:
            brain = "рџ§  off"
            hint = bst.get("hint")
            if hint:
                brain += f" В· {html.escape(str(hint)[:48])}"
        return f"{host}  В·  {brain}"
    except Exception:
        return "host/brain В· ?"


def owner_home_html() -> str:
    paused = False
    cfg_home: dict | None = None
    try:
        from state import load_config as _lc

        cfg_home = _lc() or {}
        paused = bool(cfg_home.get("paused"))
    except Exception:
        pass
    ch_status = "вЏё  РЅР° РїР°СѓР·Рµ" if paused else "рџџў  РІ СЌС„РёСЂРµ"
    hb = _host_brain_line(cfg_home)

    # СЂРѕР·С‹РіСЂС‹С€
    gw_block = "рџЋЃ  <b>Р РѕР·С‹РіСЂС‹С€</b>  В·  РЅРµС‚ Р°РєС‚РёРІРЅРѕРіРѕ"
    try:
        act = gw.get_active()
        if act:
            n = gw.entry_count(act, complete_only=True)
            need = gw.min_complete_needed(act) or 10
            mid = act.get("channel_message_id") or "вЂ”"
            prize = html.escape(str(act.get("prize") or "")[:40])
            filled = min(10, max(0, int(round(10 * n / max(need, 1)))))
            bar = "в–“" * filled + "в–‘" * (10 - filled)
            gw_block = (
                f"рџЋЃ  <b>Р РѕР·С‹РіСЂС‹С€</b>  В·  <code>{n}/{need}</code>\n"
                f"     <code>{bar}</code>\n"
                f"     {prize}\n"
                f"     РїРѕСЃС‚  В·  {mid}"
            )
    except Exception:
        pass

    # РѕС‡РµСЂРµРґСЊ
    q_block = "рџ“…  <b>РћС‡РµСЂРµРґСЊ</b>  В·  РїСѓСЃС‚Рѕ"
    try:
        from queue_lib import summary as queue_summary

        qs = queue_summary()
        nxt = qs.get("next") or {}
        nq = int(qs.get("queued") or 0)
        if nq:
            when = html.escape(str(nxt.get("publish_at") or "вЂ”")[:16])
            title = html.escape(str(nxt.get("title") or nxt.get("id") or "вЂ”")[:32])
            q_block = (
                f"рџ“…  <b>РћС‡РµСЂРµРґСЊ</b>  В·  <code>{nq}</code>\n"
                f"     next  В·  {when}\n"
                f"     {title}"
            )
    except Exception:
        pass

    # РєРѕРјРјРµРЅС‚С‹ + Р·Р°РєР°Р·С‹
    pend = 0
    try:
        from state import load_state as _ls

        pend = len((_ls() or {}).get("pending_comments") or [])
    except Exception:
        pass
    c_block = (
        f"рџ’¬  <b>РљРѕРјРјРµРЅС‚С‹</b>  В·  Р¶РґСѓС‚  <code>{pend}</code>"
        if pend
        else "рџ’¬  <b>РљРѕРјРјРµРЅС‚С‹</b>  В·  С‡РёСЃС‚Рѕ"
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
    o_block = f"рџ›   <b>Р—Р°РєР°Р·С‹</b>  В·  РІ СЂР°Р±РѕС‚Рµ  <code>{ord_n}</code>"

    return (
        f"вњ¦  <b>Р’Р°РіРіРѕ</b>  В·  РїСѓР»СЊС‚\n"
        f"{ch_status}  В·  <code>{BOT_CODE_VERSION}</code>\n"
        f"{hb}\n"
        f"{'в”Ђ' * 18}\n\n"
        f"{gw_block}\n\n"
        f"{q_block}\n\n"
        f"{c_block}\n"
        f"{o_block}\n\n"
        f"{'в”Ђ' * 18}\n"
        f"<i>Р Р°Р·РґРµР» вЂ” РєРЅРѕРїРєРё РЅРёР¶Рµ</i>\n"
        f"<i>РџСЂРѕРїР°Р» РїСѓР»СЊС‚ вЂ”</i>  /menu  <i>РёР»Рё</i>  В«РћР±РЅРѕРІРёС‚СЊВ»"
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
    Р“Р»Р°РІРЅС‹Р№ РїСѓР»СЊС‚: edit Р¶РёРІРѕРіРѕ РѕРєРЅР°; force_new вЂ” СЃРІРµР¶РµРµ СЃРѕРѕР±С‰РµРЅРёРµ
    (СЃС‚Р°СЂРѕРµ С‚РёС…Рѕ СѓР±РёСЂР°РµРј, РµСЃР»Рё РјРѕР¶РµРј). РќРµ С‚РµСЂСЏРµРјСЃСЏ.
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
    """Р’С‹Р»РѕР¶РёС‚СЊ РѕРґРёРЅ РїСѓРЅРєС‚ РѕС‡РµСЂРµРґРё (С„РѕС‚Рѕ+РїРѕРґРїРёСЃСЊ)."""
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
    """РђРІС‚Рѕ-РІС‹РєР»Р°РґРєР° due-РїРѕСЃС‚РѕРІ. Р’РѕР·РІСЂР°С‰Р°РµС‚ id РѕРїСѓР±Р»РёРєРѕРІР°РЅРЅС‹С…."""
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
        # РєРѕСЂРѕС‚РєР°СЏ Р±Р»РѕРєРёСЂРѕРІРєР°, С‡С‚РѕР±С‹ РЅРµ Р·Р°РґРІРѕРёС‚СЊ СЃ РІРЅРµС€РЅРёРј publisher
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
                f"вњ… РџРѕ СЂР°СЃРїРёСЃР°РЅРёСЋ <code>{html.escape(iid)}</code>\n"
                f"https://t.me/Vaggo01/{mid}",
            )
        except Exception as e:
            print("queue fail", iid, e, flush=True)
            mark_item(iid, status="error", error=str(e)[:200])
            notify_owner(
                cfg,
                f"вќЊ РћС‡РµСЂРµРґСЊ <code>{html.escape(iid)}</code>: {html.escape(str(e)[:200])}",
            )
    return done


def publish_to_channel(cfg: dict, text: str, *, draft: dict | None = None) -> dict:
    if cfg.get("paused"):
        raise RuntimeError("РџР°СѓР·Р° РІРєР»СЋС‡РµРЅР° вЂ” /resume")
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
        # РґР»РёРЅРЅС‹Р№ С‚РµРєСЃС‚ вЂ” РІС‚РѕСЂС‹Рј СЃРѕРѕР±С‰РµРЅРёРµРј (РїСЂРѕРґРѕР»Р¶РµРЅРёРµ)
        if rest:
            try:
                tg.send_message(cfg, channel, rest, parse_mode="HTML", disable_preview=False)
            except Exception as e:
                print("publish continuation failed", e, flush=True)
        return result
    return tg.send_message(cfg, channel, body, parse_mode="HTML", disable_preview=False)


def _owner_photo_file_id(msg: dict) -> tuple[str | None, str]:
    """Р’РµСЂРЅСѓС‚СЊ (file_id, kind) РґР»СЏ С„РѕС‚Рѕ/РєР°СЂС‚РёРЅРєРё-РґРѕРєСѓРјРµРЅС‚Р° РёР· СЃРѕРѕР±С‰РµРЅРёСЏ РІР»Р°РґРµР»СЊС†Р°."""
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
    """Р’Р»Р°РґРµР»РµС† РїСЂРёСЃР»Р°Р» С„РѕС‚Рѕ/РІРёРґРµРѕ вЂ” СЃРѕС…СЂР°РЅРёС‚СЊ Рё РїСЂРµРґР»РѕР¶РёС‚СЊ РІС‹Р»РѕР¶РёС‚СЊ."""
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
        tg.send_message(cfg, chat_id, f"вќЊ РќРµ СЃРєР°С‡Р°Р» С„Р°Р№Р»: {html.escape(str(e))}")
        return True

    text = caption or "вњЁ"
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
        f"рџ–ј <b>РњРµРґРёР° СЃРѕС…СЂР°РЅРµРЅРѕ</b>\n"
        f"Р§РµСЂРЅРѕРІРёРє <code>{item['id']}</code>\n"
        f"Р¤Р°Р№Р»: <code>{html.escape(Path(path).name)}</code>\n\n"
    )
    if caption:
        hint += "РџРѕРґРїРёСЃСЊ СѓР¶Рµ РµСЃС‚СЊ. РњРѕР¶РЅРѕ СЃСЂР°Р·Сѓ РІ РєР°РЅР°Р»."
    else:
        hint += (
            "РџРѕРґРїРёСЃРё РЅРµС‚ вЂ” РїСЂРёС€Р»Рё <b>СЃР»РµРґСѓСЋС‰РёРј СЃРѕРѕР±С‰РµРЅРёРµРј С‚РµРєСЃС‚ РїРѕСЃС‚Р°</b>\n"
            "(РјРѕР¶РЅРѕ РґР»РёРЅРЅС‹Р№; >1024 СѓР№РґС‘С‚ РїСЂРѕРґРѕР»Р¶РµРЅРёРµРј),\n"
            "РёР»Рё Р¶РјРё В«Р’ РєР°РЅР°Р»В» СЃ РєРѕСЂРѕС‚РєРѕР№ РїРѕРґРїРёСЃСЊСЋ вњЁ"
        )
    tg.send_message(cfg, chat_id, hint, reply_markup=draft_keyboard(item["id"]))
    return True


def notify_owner(cfg: dict, text: str, reply_markup: dict | None = None) -> None:
    """
    Р’Р°Р¶РЅРѕРµ вЂ” РћРўР”Р•Р›Р¬РќР«Рњ СЃРѕРѕР±С‰РµРЅРёРµРј (РЅРµ Р·Р°С‚РёСЂР°РµС‚ РїСѓР»СЊС‚).
    Р’РЅРёР·Сѓ РІСЃРµРіРґР° РєРЅРѕРїРєР° В«РћС‚РєСЂС‹С‚СЊ РїСѓР»СЊС‚В».
    """
    oid = owner_chat_id(cfg)
    if not oid:
        return
    try:
        raw = (text or "").strip()
        if raw.startswith("рџ””"):
            raw = raw[1:].strip()
        body = f"рџ””  <b>РЎРѕР±С‹С‚РёРµ</b>\n{'в”Ђ' * 14}\n\n{raw[:3000]}"
        kb = reply_markup or toast_keyboard()
        # РіР°СЂР°РЅС‚РёСЂСѓРµРј РїСѓС‚СЊ РґРѕРјРѕР№, РµСЃР»Рё СЂР°Р·РјРµС‚РєР° СЃРІРѕСЏ Р±РµР· РјРµРЅСЋ
        try:
            rows = list((kb.get("inline_keyboard") or []))
            flat = " ".join(
                (b.get("callback_data") or "") for row in rows for b in row
            )
            if "menu:home" not in flat and "menu:fresh" not in flat:
                rows.append([{"text": "рџЏ   РџСѓР»СЊС‚", "callback_data": "menu:fresh"}])
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
    lines = [f"рџ”Ћ <b>РџСЂРѕРІРµСЂРєР° РєР°РЅР°Р»Р°</b> {html.escape(str(channel))}", ""]
    try:
        me = tg.get_me(cfg)
        lines.append(f"Р‘РѕС‚: @{me.get('username')} (id {me.get('id')})")
    except Exception as e:
        return f"вќЊ getMe: {html.escape(str(e))}"

    try:
        chat = tg.get_chat(cfg, channel)
        lines.append(f"РљР°РЅР°Р»: <b>{html.escape(chat.get('title') or '?')}</b>")
        lines.append(f"chat_id: <code>{chat.get('id')}</code>")
        # Р·Р°РїРѕРјРЅРёРј С‡РёСЃР»РѕРІРѕР№ id
        if chat.get("id"):
            cfg["channel_numeric_id"] = chat["id"]
            save_config(cfg)
    except Exception as e:
        lines.append(f"вќЊ getChat: {html.escape(str(e))}")
        lines.append("РљР°РЅР°Р» РЅРµ РІРёРґРµРЅ Р±РѕС‚Сѓ.")
        return "\n".join(lines)

    try:
        m = tg.get_chat_member(cfg, channel, me["id"])
        st = m.get("status")
        lines.append(f"РЎС‚Р°С‚СѓСЃ Р±РѕС‚Р°: <b>{html.escape(str(st))}</b>")
        if st in ("administrator", "creator"):
            lines.append(f"can_post: {m.get('can_post_messages')}")
            lines.append(f"can_edit: {m.get('can_edit_messages')}")
            lines.append(f"can_delete: {m.get('can_delete_messages')}")
            if m.get("can_post_messages") or st == "creator":
                lines.append("")
                lines.append("вњ… РњРѕР¶РЅРѕ РїРѕСЃС‚РёС‚СЊ РІ РєР°РЅР°Р».")
            else:
                lines.append("")
                lines.append("вљ пёЏ РђРґРјРёРЅ, РЅРѕ Р±РµР· В«РџСѓР±Р»РёРєР°С†РёСЏ СЃРѕРѕР±С‰РµРЅРёР№В».")
        else:
            lines.append("")
            lines.append("вќЊ Р‘РѕС‚ РЅРµ Р°РґРјРёРЅ. Р”РѕР±Р°РІСЊ @DirectorVaggobot РІ Р°РґРјРёРЅС‹ РєР°РЅР°Р»Р°.")
    except Exception as e:
        lines.append(f"вќЊ getChatMember: {html.escape(str(e))}")
        lines.append("РћР±С‹С‡РЅРѕ Р·РЅР°С‡РёС‚: Р±РѕС‚ РµС‰С‘ РЅРµ Р°РґРјРёРЅ РєР°РЅР°Р»Р°.")

    disc = cfg.get("discussion_group_id") or 0
    lines.append("")
    if disc:
        lines.append(f"Р“СЂСѓРїРїР° РєРѕРјРјРµРЅС‚РѕРІ: <code>{disc}</code>")
        try:
            g = tg.get_chat(cfg, disc)
            lines.append(f"РќР°Р·РІР°РЅРёРµ: {html.escape(g.get('title') or '?')}")
            lines.append("вњ… Р“СЂСѓРїРїР° РІРёРґРЅР° Р±РѕС‚Сѓ.")
        except Exception as e:
            lines.append(f"вљ пёЏ Р“СЂСѓРїРїР° РЅРµ РґРѕСЃС‚СѓРїРЅР°: {html.escape(str(e))}")
    else:
        lines.append("Р“СЂСѓРїРїР° РєРѕРјРјРµРЅС‚РѕРІ: РЅРµ Р·Р°РґР°РЅР°.")
        lines.append("Р”РѕР±Р°РІСЊ Р±РѕС‚Р° РІ РіСЂСѓРїРїСѓ РѕР±СЃСѓР¶РґРµРЅРёР№ Рё РЅР°РїРёС€Рё С‚Р°Рј /bind")

    return "\n".join(lines)


def handle_command(cfg: dict, state: dict, msg: dict) -> None:
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    user = msg.get("from") or {}
    if not is_owner(cfg, user):
        # С‡СѓР¶РёРј РЅРµ СЃРІРµС‚РёРј РјРµРЅСЋ/РєРѕРјР°РЅРґС‹ СѓРїСЂР°РІР»РµРЅРёСЏ
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
            "Р”РѕСЃС‚СѓРї Рє СѓРїСЂР°РІР»РµРЅРёСЋ С‚РѕР»СЊРєРѕ Сѓ РІР»Р°РґРµР»СЊС†Р°.\n\n"
            "Р•СЃР»Рё СЂРѕР·С‹РіСЂС‹С€: РѕС‚РєСЂРѕР№ РїРѕСЃС‚ Рё РЅР°Р¶РјРё В«РЈС‡Р°СЃС‚РІРѕРІР°С‚СЊВ».\n"
            f"{link}",
            parse_mode=None,
        )
        return

    # remember owner id if missing
    if user.get("id") and user["id"] not in (cfg.get("owner_user_ids") or []):
        # don't auto-add strangers; only if list empty-ish
        pass

    # Р¤РѕС‚Рѕ/РІРёРґРµРѕ РѕС‚ РІР»Р°РґРµР»СЊС†Р° в†’ С‡РµСЂРЅРѕРІРёРє СЃ РјРµРґРёР°
    if msg.get("photo") or msg.get("video") or msg.get("document"):
        if handle_owner_media(cfg, state, msg):
            return

    # РўРµРєСЃС‚ РїРѕСЃР»Рµ С„РѕС‚Рѕ Р±РµР· РїРѕРґРїРёСЃРё в†’ РїСЂРёРІСЏР·Р°С‚СЊ Рє С‡РµСЂРЅРѕРІРёРєСѓ
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
            preview = text if len(text) < 900 else text[:900] + "вЂ¦"
            tg.send_message(
                cfg,
                chat_id,
                f"рџ“ќ РўРµРєСЃС‚ РїСЂРёРІСЏР·Р°РЅ Рє <code>{did}</code>\n"
                f"РњРµРґРёР°: <code>{html.escape(Path(draft.get('media_path') or '').name)}</code>\n\n"
                f"{preview}",
                reply_markup=draft_keyboard(did),
            )
            return

    lower = text.lower()
    cmd = lower.split()[0].split("@")[0] if lower.startswith("/") else ""
    arg = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""

    if cmd in ("/start", "/help", "/menu", "/panel", "/РїСѓР»СЊС‚"):
        ui_delete_user_message(cfg, msg)
        uid_o = int(user.get("id") or 0) or None
        # /menu /start вЂ” РІСЃРµРіРґР° СЃРІРµР¶РёР№ РїСѓР»СЊС‚ (РЅРµ РїРѕС‚РµСЂСЏРµС‚СЃСЏ)
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

    # --- РјРѕРґРµСЂР°С†РёСЏ С‡Р°С‚Р° ---
    if cmd in ("/modstat", "/modstatus"):
        target = arg.strip().lstrip("@")
        if not target:
            tg.send_message(
                cfg,
                chat_id,
                "рџ›Ў <b>РњРѕРґРµСЂР°С†РёСЏ С‡Р°С‚Р°</b>\n"
                "4 РїСЂРµРґСѓРїСЂ. в†’ РјСѓС‚ 1С‡ В· 3 РјСѓС‚Р° в†’ РЅРµРґРµР»СЏ В· "
                "2 РЅРµРґРµР»Рё в†’ 2 РјРµСЃ В· РµС‰С‘ РЅРµРґРµР»СЏ в†’ Р±Р°РЅ\n\n"
                "<code>/modstat ID</code> В· <code>/modpardon ID</code>",
                parse_mode="HTML",
            )
            return
        try:
            tid = int(target) if target.isdigit() else 0
        except Exception:
            tid = 0
        if not tid:
            tg.send_message(cfg, chat_id, "РќСѓР¶РµРЅ numeric user_id")
            return
        tg.send_message(
            cfg,
            chat_id,
            f"рџ›Ў <code>{tid}</code>\n{html.escape(chatmod.status_line(tid))}",
            parse_mode="HTML",
        )
        return

    if cmd in ("/modpardon", "/modforgive", "/unmute"):
        target = arg.strip().lstrip("@")
        if not target.isdigit():
            tg.send_message(cfg, chat_id, "РџСЂРёРјРµСЂ: <code>/modpardon 123456</code>", parse_mode="HTML")
            return
        tid = int(target)
        u = chatmod.owner_pardon(tid)
        disc = cfg.get("discussion_group_id")
        if disc:
            try:
                # СЃРЅСЏС‚СЊ restrict / unban
                tg.unban_chat_member(cfg, disc, tid, only_if_banned=True)
                # РІРµСЂРЅСѓС‚СЊ РїСЂР°РІР° РїРёСЃР°С‚СЊ (РїСѓСЃС‚С‹Рµ permissions СЃ can_send = true)
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
            f"вњ… РЎРЅСЏС‚Рѕ СЃ <code>{tid}</code>\n{html.escape(chatmod.status_line(tid)) if u else 'РѕРє'}",
            parse_mode="HTML",
        )
        return

    # ---------- СЂРѕР·С‹РіСЂС‹С€ ----------
    if cmd in ("/giveaway", "/ghelp", "/raffle"):
        tg.send_message(
            cfg,
            chat_id,
            "рџЋЃ <b>Р РѕР·С‹РіСЂС‹С€</b> (РєР°Рє @GiveShareBot)\n\n"
            "1. /gnew Gemini Pro 18 РјРµСЃСЏС†РµРІ\n"
            "2. /gpost вЂ” РїРѕСЃС‚ РІ РєР°РЅР°Р» СЃ РєРЅРѕРїРєРѕР№ В«РЈС‡Р°СЃС‚РІРѕРІР°С‚СЊВ»\n"
            "3. Р›СЋРґРё Р¶РјСѓС‚ РєРЅРѕРїРєСѓ (РїСЂРѕРІРµСЂРєР° РїРѕРґРїРёСЃРєРё + СЃС‡С‘С‚С‡РёРє)\n"
            "4. РџРѕ С‚Р°Р№РјРµСЂСѓ Р±РѕС‚ СЃР°Рј РІС‹Р±РёСЂР°РµС‚ РїРѕР±РµРґРёС‚РµР»СЏ\n"
            "   РёР»Рё /gdraw РІСЂСѓС‡РЅСѓСЋ\n\n"
            "РћРїС†РёРё: /gnew РїСЂРёР· | 48 вЂ” РЅР° 48 С‡Р°СЃРѕРІ\n"
            "/gstatus В· /gentries В· /gend В· /gcancel\n\n"
            + gw.format_status(gw.get_active(state)),
        )
        return

    if cmd == "/gnew":
        if not arg:
            tg.send_message(
                cfg,
                chat_id,
                "РџСЂРёРјРµСЂ:\n"
                "<code>/gnew Gemini Pro 18 РјРµСЃСЏС†РµРІ</code>\n"
                "<code>/gnew Gemini Pro 18 РјРµСЃ | 48</code> вЂ” РЅР° 48 С‡Р°СЃРѕРІ",
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
                f"вњ… Р§РµСЂРЅРѕРІРёРє <code>{item['id']}</code>\n"
                f"РџСЂРёР·: {html.escape(item['prize'])}\n"
                f"РЎСЂРѕРє: {item['hours']} С‡\n"
                f"РљРІРµСЃС‚: РїРѕРґРїРёСЃРєР° + СЂРµРїРѕСЃС‚ РґСЂСѓРіСѓ (СЃРєСЂРёРЅ) В· Р°РІС‚Рѕ-С€РѕСѓ\n\n"
                f"Р”Р°Р»СЊС€Рµ: /gpost",
            )
        except Exception as e:
            tg.send_message(cfg, chat_id, f"вќЊ {html.escape(str(e))}")
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
            tg.send_message(cfg, chat_id, "РЎРЅР°С‡Р°Р»Р° /gnew РїСЂРёР·")
            return
        if cfg.get("paused"):
            tg.send_message(cfg, chat_id, "вЏё РџР°СѓР·Р°. /resume")
            return
        try:
            item = gw.activate(item)  # ends_at РґРѕ С‚РµРєСЃС‚Р°
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
                tg.set_message_reaction(cfg, channel, mid, "рџ”Ґ")
            except Exception:
                pass
            tg.send_message(
                cfg,
                chat_id,
                f"рџљЂ Р РѕР·С‹РіСЂС‹С€-РєРІРµСЃС‚ РІ РєР°РЅР°Р»Рµ\n"
                f"https://t.me/Vaggo01/{mid}\n"
                f"id: <code>{item['id']}</code>\n"
                f"РЁР°РіРё: РїРѕРґРїРёСЃРєРё В· СЂРµРїРѕСЃС‚ РґСЂСѓРіСѓ В· РґСЂСѓР·СЊСЏГ—{item.get('require_invites', 1)}\n"
                f"РђРІС‚Рѕ-С€РѕСѓ РїРѕР±РµРґРёС‚РµР»СЏ: {'РґР°' if item.get('auto_draw', True) else 'РЅРµС‚'}\n"
                f"/gstatus В· /gentries В· /gdraw",
            )
        except Exception as e:
            tg.send_message(cfg, chat_id, f"вќЊ {html.escape(str(e))}")
        return

    if cmd in ("/gfixkb", "/gfix_kb", "/gkeyboard"):
        # РІРµСЂРЅСѓС‚СЊ РєРЅРѕРїРєСѓ В«РЈС‡Р°СЃС‚РІРѕРІР°С‚СЊВ» РЅР° РїРѕСЃС‚ РєР°РЅР°Р»Р° (РїРѕСЃР»Рµ restore)
        item = gw.get_active(state)
        if not item:
            try:
                gw.apply_restore_seed(force=True)
                item = gw.get_active()
            except Exception:
                item = None
        if not item:
            tg.send_message(cfg, chat_id, "РќРµС‚ Р°РєС‚РёРІРЅРѕРіРѕ СЂРѕР·С‹РіСЂС‹С€Р°. /gwrestore")
            return
        mid = item.get("channel_message_id")
        if arg:
            try:
                mid = int(arg.strip().split()[0])
                item = gw.bind_channel_post(item, mid)
            except ValueError:
                tg.send_message(cfg, chat_id, "id РїРѕСЃС‚Р° вЂ” С‡РёСЃР»Рѕ")
                return
        if not mid:
            tg.send_message(cfg, chat_id, "РќРµС‚ channel_message_id. /gbind 102")
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
                f"вњ… РљРЅРѕРїРєРё РЅР° РїРѕСЃС‚Рµ РѕР±РЅРѕРІР»РµРЅС‹\n"
                f"https://t.me/Vaggo01/{mid}\n"
                f"complete: <b>{gw.entry_count(item, complete_only=True)}</b>\n"
                f"id: <code>{html.escape(str(item.get('id')))}</code>",
            )
        except Exception as e:
            tg.send_message(
                cfg,
                chat_id,
                f"вќЊ РЅРµ СЃРјРѕРі edit РєРЅРѕРїРѕРє: {html.escape(str(e)[:250])}\n"
                f"РџРѕСЃС‚: https://t.me/Vaggo01/{mid}\n"
                f"РњРѕР¶РЅРѕ /gpost Р·Р°РЅРѕРІРѕ (РЅРѕРІС‹Р№ РїРѕСЃС‚).",
            )
        return

    if cmd == "/gbind":
        if not arg:
            tg.send_message(cfg, chat_id, "РџСЂРёРјРµСЂ: /gbind 86 вЂ” id РїРѕСЃС‚Р° РІ РєР°РЅР°Р»Рµ")
            return
        try:
            mid = int(arg.strip().split()[0])
        except ValueError:
            tg.send_message(cfg, chat_id, "РќСѓР¶РµРЅ С‡РёСЃР»РѕРІРѕР№ id РїРѕСЃС‚Р°")
            return
        item = gw.get_active(state)
        if not item:
            drafts = [x for x in gw.list_items(state) if x.get("status") in ("draft", "active")]
            item = drafts[0] if drafts else None
        if not item:
            tg.send_message(cfg, chat_id, "РЎРЅР°С‡Р°Р»Р° /gnew РїСЂРёР·")
            return
        try:
            item = gw.bind_channel_post(item, mid)
            # СЃСЂР°Р·Сѓ РїРѕРІРµСЃРёС‚СЊ РєРЅРѕРїРєРё
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
                f"вњ… РџСЂРёРІСЏР·Р°РЅ РїРѕСЃС‚ <code>{mid}</code>\n"
                f"https://t.me/Vaggo01/{mid}\n"
                f"СЃС‚Р°С‚СѓСЃ: {item.get('status')}\n"
                f"discuss_root: {item.get('discuss_root_id') or 'РїРѕСЏРІРёС‚СЃСЏ РїРѕСЃР»Рµ РїРµСЂРІРѕРіРѕ РєРѕРјРјРµРЅС‚Р°/С„РѕСЂРІР°СЂРґР°'}\n"
                + gw.format_status(item),
            )
        except Exception as e:
            tg.send_message(cfg, chat_id, f"вќЊ {html.escape(str(e))}")
        return

    if cmd in ("/gstatus", "/gstat"):
        tg.send_message(cfg, chat_id, gw.format_status(gw.get_active(state) or (gw.list_items(state)[0] if gw.list_items(state) else None)))
        return

    if cmd == "/gentries":
        item = gw.get_active(state) or (gw.list_items(state)[0] if gw.list_items(state) else None)
        if not item:
            tg.send_message(cfg, chat_id, "РќРµС‚ СЂРѕР·С‹РіСЂС‹С€Р°. /gnew")
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
            tg.send_message(cfg, chat_id, "РќРµС‚ Р°РєС‚РёРІРЅРѕРіРѕ СЂРѕР·С‹РіСЂС‹С€Р°")
            return
        if gw.entry_count(item) == 0:
            tg.send_message(cfg, chat_id, "РЈС‡Р°СЃС‚РЅРёРєРѕРІ 0 вЂ” РЅРµРєРѕРіРѕ РІС‹Р±РёСЂР°С‚СЊ")
            return
        try:
            finish_giveaway_draw(cfg, item, notify_chat=chat_id)
        except Exception as e:
            tg.send_message(cfg, chat_id, f"вќЊ {html.escape(str(e))}")
        return

    if cmd == "/gend":
        item = gw.get_active(state)
        if not item:
            tg.send_message(cfg, chat_id, "РђРєС‚РёРІРЅРѕРіРѕ РЅРµС‚")
            return
        gw.end(item)
        tg.send_message(cfg, chat_id, f"вЏ№ Р РѕР·С‹РіСЂС‹С€ <code>{item['id']}</code> Р·Р°РєСЂС‹С‚ Р±РµР· СЂРѕР·С‹РіСЂС‹С€Р°.\nРЈС‡Р°СЃС‚РЅРёРєРѕРІ: {gw.entry_count(item)}")
        return

    if cmd == "/gcancel":
        item = gw.get_active(state) or (gw.list_items(state)[0] if gw.list_items(state) else None)
        if not item:
            tg.send_message(cfg, chat_id, "РќРµС‡РµРіРѕ РѕС‚РјРµРЅСЏС‚СЊ")
            return
        gw.cancel(item)
        tg.send_message(cfg, chat_id, f"рџ—‘ Р РѕР·С‹РіСЂС‹С€ <code>{item['id']}</code> РѕС‚РјРµРЅС‘РЅ")
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
        nxt_line = "вЂ”"
        if nxt:
            nxt_line = (
                f"<code>{html.escape(str(nxt.get('id')))}</code> "
                f"В· {html.escape(str(nxt.get('publish_at') or '?'))}\n"
                f"   {(html.escape(str(nxt.get('title') or '')))[:50]}"
            )
        pause = "вЏё РїР°СѓР·Р°" if cfg.get("paused") else "в–¶пёЏ online"
        subs = "вЂ”"
        try:
            subs = str(
                tg.api(cfg, "getChatMemberCount", data={"chat_id": cfg.get("channel_id") or "@Vaggo01"})
            )
        except Exception:
            pass
        gw_line = "РЅРµС‚ Р°РєС‚РёРІРЅРѕРіРѕ"
        try:
            act = gw.get_active(state)
            if act:
                ok_n = gw.entry_count(act, complete_only=True)
                all_n = gw.entry_count(act, complete_only=False)
                mid = act.get("channel_message_id")
                ends = act.get("ends_at")
                ends_s = (
                    time.strftime("%d.%m %H:%M", time.localtime(int(ends))) if ends else "вЂ”"
                )
                gw_line = (
                    f"{html.escape(str(act.get('status')))} В· "
                    f"вњ…{ok_n} / РЅР°С‡Р°Р»Рё {all_n} В· РґРѕ {ends_s}"
                )
                if mid:
                    gw_line += f"\n   https://t.me/Vaggo01/{mid}"
        except Exception:
            pass
        tg.send_message(
            cfg,
            chat_id,
            "рџ“Љ <b>РЎРІРѕРґРєР° Р’Р°РіРіРѕ</b>\n\n"
            f"рџ‘Ґ РџРѕРґРїРёСЃС‡РёРєРё: <b>{html.escape(subs)}</b>\n"
            f"Р‘РѕС‚: {pause}\n\n"
            f"<b>РћС‡РµСЂРµРґСЊ</b>\n"
            f"вЏі {s['queued']} В· вњ… {s['published']}\n"
            f"РЎР»РµРґ.: {nxt_line}\n\n"
            f"<b>Р РѕР·С‹РіСЂС‹С€</b>\n{gw_line}\n\n"
            f"<b>РљРѕРЅС‚РµРЅС‚</b>\n"
            f"Р§РµСЂРЅРѕРІРёРєРё: {len(drafts)} В· РєРѕРјРјРµРЅС‚С‹ Р¶РґСѓС‚: {len(pending)}\n"
            f"Instant: {'РґР°' if not cfg.get('comment_needs_owner_ok', True) else 'РЅРµС‚'} В· "
            f"СЂРµР°РєС†РёРё: {'РґР°' if cfg.get('auto_react_posts', True) else 'РЅРµС‚'}\n\n"
            f"/promo В· /gentries В· /queue В· /menu",
            reply_markup=main_menu_keyboard(),
        )
        return

    if cmd in ("/promo", "/ad", "/СЂРµРєР»Р°РјР°"):
        try:
            from promo_lib import PROMO_HTML

            tg.send_message(
                cfg, chat_id, "рџ“ў <b>РўРµРєСЃС‚ РґР»СЏ СЂРµРєР»Р°РјС‹</b> вЂ” РєРѕРїРёСЂСѓР№:\n\n" + PROMO_HTML,
                parse_mode="HTML",
                disable_preview=True,
            )
        except Exception as e:
            tg.send_message(cfg, chat_id, f"вќЊ {html.escape(str(e))}")
        return

    if cmd == "/qnow":
        if cfg.get("paused"):
            tg.send_message(cfg, chat_id, "вЏё РЎРµР№С‡Р°СЃ РїР°СѓР·Р°. /resume")
            return
        item = None
        if arg:
            item = get_item(arg)
            if not item:
                tg.send_message(cfg, chat_id, f"РќРµС‚ id <code>{html.escape(arg)}</code>. РЎРјРѕС‚СЂРё /queue")
                return
            if item.get("status") != "queued":
                tg.send_message(
                    cfg,
                    chat_id,
                    f"РџСѓРЅРєС‚ <code>{html.escape(arg)}</code> СЃС‚Р°С‚СѓСЃ: {html.escape(str(item.get('status')))}",
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
            tg.send_message(cfg, chat_id, "РќРµС‡РµРіРѕ РІС‹РєР»Р°РґС‹РІР°С‚СЊ. /queue")
            return
        iid = str(item.get("id"))
        tg.send_message(cfg, chat_id, f"вЏі Р’С‹РєР»Р°РґС‹РІР°СЋ <code>{html.escape(iid)}</code>вЂ¦")
        try:
            queue_publish_now(iid)
            item = get_item(iid) or item
            mid = publish_queue_item(cfg, item)
            tg.send_message(
                cfg,
                chat_id,
                f"вњ… Р“РѕС‚РѕРІРѕ <code>{html.escape(iid)}</code>\n"
                f"https://t.me/Vaggo01/{mid}",
            )
        except Exception as e:
            mark_item(iid, status="error", error=str(e)[:200])
            tg.send_message(cfg, chat_id, f"вќЊ {html.escape(str(e))}")
        return

    if cmd == "/qskip":
        if not arg:
            tg.send_message(cfg, chat_id, "РџСЂРёРјРµСЂ: /qskip ai2")
            return
        if cancel_item(arg):
            tg.send_message(cfg, chat_id, f"вЏ­ РћС‚РјРµРЅРёР» <code>{html.escape(arg)}</code>")
        else:
            tg.send_message(cfg, chat_id, f"РќРµ РЅР°С€С‘Р» queued <code>{html.escape(arg)}</code>")
        return

    if cmd == "/status":
        tg.send_message(cfg, chat_id, status_text(cfg, state))
        return

    if cmd == "/plan":
        tg.send_message(cfg, chat_id, week_plan())
        return

    if cmd == "/check":
        tg.send_message(cfg, chat_id, "вЏі РџСЂРѕРІРµСЂСЏСЋвЂ¦")
        tg.send_message(cfg, chat_id, check_channel_report(cfg))
        return

    if cmd == "/brains":
        from content import brain_status, grok_ok, ollama_ok

        st = brain_status(cfg)
        src = st.get("grok_source") or "вЂ”"
        src_h = {
            "session": "SuperGrok СЃРµСЃСЃРёСЏ (grok login) вњ…",
            "api_key": "РєР»СЋС‡ console.x.ai вњ…",
            "": "РЅРµС‚",
            "вЂ”": "РЅРµС‚",
        }.get(src, src)
        sess = st.get("session") or {}
        sess_line = ""
        if sess.get("ok"):
            exp = "РёСЃС‚РµРєР»Р° вљ " if sess.get("expired") else "Р¶РёРІР°"
            sess_line = f"РЎРµСЃСЃРёСЏ: {html.escape(str(sess.get('email') or ''))} В· {exp}\n"
        tg.send_message(
            cfg,
            chat_id,
            "рџ§  <b>РњРѕР·РіРё Р±РѕС‚Р°</b>\n\n"
            f"Р РµР¶РёРј: <code>{html.escape(st['mode'])}</code> "
            f"(auto = Grok в†’ Ollama в†’ С€Р°Р±Р»РѕРЅ)\n"
            f"РЎРµР№С‡Р°СЃ Р°РєС‚РёРІРµРЅ: <b>{html.escape(st['active'])}</b>\n\n"
            f"Grok: {'вњ…' if grok_ok(cfg) else 'вќЊ'} В· {html.escape(src_h)}\n"
            f"  РјРѕРґРµР»СЊ: <code>{html.escape(st['grok_model'])}</code>\n"
            f"{sess_line}"
            f"Ollama: {'вњ…' if ollama_ok(cfg) else 'вќЊ'} В· "
            f"<code>{html.escape(st['ollama_model'])}</code>\n\n"
            "Р‘РѕС‚ С…РѕРґРёС‚ РІ Grok С‡РµСЂРµР· С‚РІРѕСЋ РїРѕРґРїРёСЃРєСѓ Super "
            "(С„Р°Р№Р» РІС…РѕРґР° Grok Build) РёР»Рё С‡РµСЂРµР· xai_api_key.\n"
            "Р•СЃР»Рё 401 вЂ” РІ С‚РµСЂРјРёРЅР°Р»Рµ: <code>grok login</code>",
        )
        return

    if cmd in ("/redeploy", "/deploy", "/update"):
        # РІР»Р°РґРµР»РµС†: pull СЃ GitHub + restart РЅР° Bothost
        tg.send_message(cfg, chat_id, "вЏі РўСЏРЅСѓ РєРѕРґ СЃ GitHubвЂ¦")
        try:
            import deploy_lib

            res = deploy_lib.redeploy_now(restart=True)
            pull = res.get("pull") or {}
            files = ", ".join((pull.get("files") or [])[:12])
            rst = res.get("restart") or {}
            body = (
                "рџљЂ <b>Redeploy</b>\n\n"
                f"ver: <code>{html.escape(BOT_CODE_VERSION)}</code>\n"
                f"remote: <code>{html.escape(str(res.get('remote_sha') or pull.get('sha') or 'вЂ”'))}</code>\n"
                f"files: {pull.get('count') or 0}\n"
                f"<code>{html.escape(files[:400])}</code>\n\n"
                f"restart: {html.escape(str(rst.get('message') or rst.get('error') or rst.get('reason') or rst))[:200]}\n"
            )
            if res.get("pull_error"):
                body = f"вќЊ Pull fail: {html.escape(str(res['pull_error'])[:300])}"
            tg.send_message(cfg, chat_id, body)
        except Exception as e:
            tg.send_message(
                cfg, chat_id, f"вќЊ redeploy: {html.escape(str(e)[:300])}"
            )
        return

    if cmd in ("/deploy_status", "/depstatus"):
        try:
            import deploy_lib

            need, remote, local = deploy_lib.needs_update()
            tg.send_message(
                cfg,
                chat_id,
                "рџ“¦ <b>Deploy status</b>\n\n"
                f"ver: <code>{html.escape(BOT_CODE_VERSION)}</code>\n"
                f"local sha: <code>{html.escape((local or 'вЂ”')[:12])}</code>\n"
                f"remote: <code>{html.escape((remote or 'вЂ”')[:12])}</code>\n"
                f"need update: <b>{'YES' if need else 'no'}</b>\n\n"
                "РћР±РЅРѕРІРёС‚СЊ: /redeploy",
            )
        except Exception as e:
            tg.send_message(cfg, chat_id, f"вќЊ {html.escape(str(e)[:250])}")
        return

    if cmd == "/react_on":
        cfg["auto_react_comments"] = True
        save_config(cfg)
        tg.send_message(cfg, chat_id, "вќ¤пёЏ РђРІС‚Рѕ-СЂРµР°РєС†РёРё РЅР° РєРѕРјРјРµРЅС‚С‹: Р’РљР›")
        return

    if cmd == "/react_off":
        cfg["auto_react_comments"] = False
        save_config(cfg)
        tg.send_message(cfg, chat_id, "РђРІС‚Рѕ-СЂРµР°РєС†РёРё: Р’Р«РљР›")
        return

    if cmd == "/react":
        # /react рџ”Ґ   РёР»Рё  /react рџ”Ґ 123  РёР»Рё  /react 123
        parts = arg.split()
        emoji = "рџ”Ґ"
        mid = None
        if not parts:
            pub = state.get("published") or []
            if not pub or not pub[0].get("channel_message_id"):
                tg.send_message(cfg, chat_id, "РќРµС‚ РїРѕСЃС‚Р°. РЎРЅР°С‡Р°Р»Р° /post РёР»Рё /react рџ”Ґ message_id")
                return
            mid = int(pub[0]["channel_message_id"])
        elif len(parts) == 1:
            if parts[0].isdigit():
                mid = int(parts[0])
            else:
                emoji = parts[0]
                pub = state.get("published") or []
                if not pub or not pub[0].get("channel_message_id"):
                    tg.send_message(cfg, chat_id, "РќРµС‚ message_id. /react рџ”Ґ 123")
                    return
                mid = int(pub[0]["channel_message_id"])
        else:
            if parts[0].isdigit():
                mid, emoji = int(parts[0]), parts[1]
            else:
                emoji, mid = parts[0], int(parts[1]) if parts[1].isdigit() else None
            if mid is None:
                tg.send_message(cfg, chat_id, "РџСЂРёРјРµСЂ: /react рџ”Ґ 42")
                return
        try:
            channel = cfg.get("channel_id") or "@Vaggo01"
            tg.set_message_reaction(cfg, channel, mid, emoji)
            tg.send_message(cfg, chat_id, f"{emoji} СЂРµР°РєС†РёСЏ РЅР° msg {mid}")
        except Exception as e:
            tg.send_message(
                cfg,
                chat_id,
                f"вќЊ Р РµР°РєС†РёСЏ: {html.escape(str(e))}\n"
                "РќСѓР¶РЅС‹ РїСЂР°РІР° РІ РєР°РЅР°Р»Рµ. Р­РјРѕРґР·Рё вЂ” РёР· СЃРїРёСЃРєР° Telegram (рџ”Ґвќ¤рџ‘ЌрџЋ‰вЂ¦).",
            )
        return

    if cmd == "/pause":
        cfg["paused"] = True
        save_config(cfg)
        tg.send_message(cfg, chat_id, "вЏё РџР°СѓР·Р°. РџРѕСЃС‚С‹ Рё Р°РІС‚Рѕ-РєРѕРјРјРµРЅС‚С‹ РЅРµ СѓС…РѕРґСЏС‚.")
        return

    if cmd == "/resume":
        cfg["paused"] = False
        save_config(cfg)
        tg.send_message(cfg, chat_id, "в–¶пёЏ РЎРЅСЏС‚Рѕ СЃ РїР°СѓР·С‹.")
        return

    if cmd == "/auto_on":
        cfg["auto_reply_comments"] = True
        save_config(cfg)
        tg.send_message(cfg, chat_id, "РљРѕРјРјРµРЅС‚С‹: С‡РµСЂРЅРѕРІРёРєРё РѕС‚РІРµС‚РѕРІ С‚РµР±Рµ РЅР° РѕРє (РµСЃР»Рё РЅРµ instant).")
        return

    if cmd == "/auto_off":
        cfg["auto_reply_comments"] = False
        save_config(cfg)
        tg.send_message(cfg, chat_id, "РћР±СЂР°Р±РѕС‚РєР° РєРѕРјРјРµРЅС‚РѕРІ РІС‹РєР».")
        return

    if cmd == "/instant_on":
        cfg["auto_reply_comments"] = True
        cfg["comment_needs_owner_ok"] = False
        save_config(cfg)
        tg.send_message(cfg, chat_id, "вљЎ Instant: РѕС‚РІРµС‚С‹ РІ РєРѕРјРјРµРЅС‚С‹ СЃСЂР°Р·Сѓ (РѕСЃС‚РѕСЂРѕР¶РЅРѕ).")
        return

    if cmd == "/instant_off":
        cfg["comment_needs_owner_ok"] = True
        save_config(cfg)
        tg.send_message(cfg, chat_id, "РЎРЅРѕРІР°: СЃРЅР°С‡Р°Р»Р° С‡РµСЂРЅРѕРІРёРє РѕС‚РІРµС‚Р° С‚РµР±Рµ.")
        return

    if cmd == "/bind":
        # must be used from the discussion group OR with id arg
        if arg:
            try:
                gid = int(arg)
            except ValueError:
                tg.send_message(cfg, chat_id, "РџСЂРёРјРµСЂ: /bind -1001234567890")
                return
            cfg["discussion_group_id"] = gid
            save_config(cfg)
            tg.send_message(cfg, chat_id, f"вњ… discussion_group_id = <code>{gid}</code>")
            return
        tg.send_message(
            cfg,
            chat_id,
            "Р§С‚РѕР±С‹ РїСЂРёРІСЏР·Р°С‚СЊ РіСЂСѓРїРїСѓ РєРѕРјРјРµРЅС‚РѕРІ:\n"
            "1) Р”РѕР±Р°РІСЊ Р±РѕС‚Р° РІ РіСЂСѓРїРїСѓ РѕР±СЃСѓР¶РґРµРЅРёР№ РєР°РЅР°Р»Р°\n"
            "2) Р’ <b>СЌС‚РѕР№ РіСЂСѓРїРїРµ</b> РЅР°РїРёС€Рё /bind\n"
            "РР»Рё: /bind -100xxxxxxxxxx",
        )
        return

    if cmd == "/comments":
        pending = [c for c in (state.get("pending_comments") or []) if c.get("status") == "pending"]
        if not pending:
            tg.send_message(cfg, chat_id, "РћС‡РµСЂРµРґСЊ РєРѕРјРјРµРЅС‚РѕРІ РїСѓСЃС‚Р°.")
            return
        for c in pending[:8]:
            preview = html.escape((c.get("comment_text") or "")[:300])
            reply = html.escape((c.get("reply_text") or "")[:500])
            tg.send_message(
                cfg,
                chat_id,
                f"рџ’¬ <b>РљРѕРјРјРµРЅС‚</b> <code>{c['id']}</code>\n"
                f"РћС‚: {html.escape(str(c.get('from_name') or '?'))}\n"
                f"<i>{preview}</i>\n\n"
                f"<b>РћС‚РІРµС‚:</b>\n{reply}",
                reply_markup=comment_keyboard(c["id"]),
            )
        return

    if cmd == "/drafts":
        drafts = [d for d in (state.get("drafts") or []) if d.get("status") == "draft"]
        if not drafts:
            tg.send_message(cfg, chat_id, "Р§РµСЂРЅРѕРІРёРєРѕРІ РЅРµС‚. /draft С‚РµРјР°")
            return
        lines = ["рџ“ќ <b>Р§РµСЂРЅРѕРІРёРєРё</b>\n"]
        for d in drafts[:12]:
            prev = html.escape((d.get("text") or "")[:80].replace("\n", " "))
            lines.append(f"вЂў <code>{d['id']}</code> вЂ” {prev}вЂ¦")
        lines.append("\n/post вЂ” РІС‹Р»РѕР¶РёС‚СЊ РїРѕСЃР»РµРґРЅРёР№\nРР»Рё РєРЅРѕРїРєР° РїРѕРґ С‡РµСЂРЅРѕРІРёРєРѕРј.")
        tg.send_message(cfg, chat_id, "\n".join(lines))
        return

    if cmd == "/last":
        pub = state.get("published") or []
        if not pub:
            tg.send_message(cfg, chat_id, "РџРѕРєР° РЅРёС‡РµРіРѕ РЅРµ РїСѓР±Р»РёРєРѕРІР°Р»Рё РёР· Р±РѕС‚Р°/РїСѓР»СЊС‚Р°.")
            return
        lines = ["рџ“¤ <b>РџРѕСЃР»РµРґРЅРёРµ РїСѓР±Р»РёРєР°С†РёРё</b>\n"]
        for p in pub[:8]:
            prev = html.escape((p.get("text_preview") or "")[:100])
            lines.append(f"вЂў msg {p.get('channel_message_id')} В· {prev}")
        tg.send_message(cfg, chat_id, "\n".join(lines))
        return

    if cmd == "/pin":
        pub = state.get("published") or []
        if not pub or not pub[0].get("channel_message_id"):
            tg.send_message(cfg, chat_id, "РќРµС‚ message_id. РЎРЅР°С‡Р°Р»Р° /post.")
            return
        try:
            channel = cfg.get("channel_id") or "@Vaggo01"
            mid = int(pub[0]["channel_message_id"])
            tg.pin_chat_message(cfg, channel, mid, silent=True)
            tg.send_message(cfg, chat_id, f"рџ“Њ Р—Р°РєСЂРµРїРёР» msg {mid}")
        except Exception as e:
            tg.send_message(cfg, chat_id, f"вќЊ Pin: {html.escape(str(e))}\nРќСѓР¶РЅРѕ РїСЂР°РІРѕ В«Р—Р°РєСЂРµРїР»РµРЅРёРµВ».")
        return

    if cmd == "/ideas":
        tg.send_message(cfg, chat_id, "вЏі РРґРµРёвЂ¦")
        try:
            ideas = generate_ideas(7, rubric=arg)
            tg.send_message(
                cfg,
                chat_id,
                f"рџ’Ў <b>РРґРµРё</b>\n\n{html.escape(ideas)}\n\n"
                f"Р‘РµСЂРё СЃС‚СЂРѕРєСѓ в†’ <code>/draft вЂ¦</code>",
            )
        except Exception as e:
            tg.send_message(cfg, chat_id, f"вќЊ {html.escape(str(e))}")
        return

    if cmd == "/series":
        tg.send_message(cfg, chat_id, "вЏі РџРёС€Сѓ СЃРµСЂРёСЋ РёР· 5 С‡РµСЂРЅРѕРІРёРєРѕРІвЂ¦ СЌС‚Рѕ РјРёРЅСѓС‚Р°-РґРІРµ")
        made = []
        try:
            for rubric, topic in series_topics():
                body = generate_post(topic, rubric=rubric)
                item = add_draft(state, body, rubric=rubric, source="series")
                made.append(item)
                state = load_state()
            tg.send_message(cfg, chat_id, f"вњ… Р“РѕС‚РѕРІРѕ С‡РµСЂРЅРѕРІРёРєРѕРІ: {len(made)}")
            for item in made:
                tg.send_message(
                    cfg,
                    chat_id,
                    f"рџ“ќ <code>{item['id']}</code> В· {html.escape(item.get('rubric') or '')}\n\n{item['text'][:3500]}",
                    reply_markup=draft_keyboard(item["id"]),
                )
        except Exception as e:
            tg.send_message(cfg, chat_id, f"вќЊ {html.escape(str(e))}")
        return

    if cmd == "/rewrite":
        draft = get_draft(state, arg if arg else None)
        if not draft:
            tg.send_message(cfg, chat_id, "РќРµС‚ С‡РµСЂРЅРѕРІРёРєР°.")
            return
        tg.send_message(cfg, chat_id, "вЏі РџРµСЂРµРїРёСЃС‹РІР°СЋвЂ¦")
        try:
            body = rewrite_post(draft["text"])
            item = add_draft(state, body, rubric=draft.get("rubric") or "", source="rewrite")
            tg.send_message(
                cfg,
                chat_id,
                f"вњЏпёЏ РќРѕРІС‹Р№ РІР°СЂРёР°РЅС‚ <code>{item['id']}</code>\n\n{body}",
                reply_markup=draft_keyboard(item["id"]),
            )
        except Exception as e:
            tg.send_message(cfg, chat_id, f"вќЊ {html.escape(str(e))}")
        return

    if cmd == "/post":
        draft = get_draft(state, arg if arg else None)
        if not draft:
            tg.send_message(cfg, chat_id, "РќРµС‚ С‡РµСЂРЅРѕРІРёРєР°. /draft С‚РµРјР°")
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
                f"вњ… Р’ РєР°РЅР°Р»Рµ. draft=<code>{draft['id']}</code> msg={mid}\n"
                f"/pin вЂ” Р·Р°РєСЂРµРїРёС‚СЊ В· /react рџЋ‰ {mid}",
            )
        except Exception as e:
            tg.send_message(cfg, chat_id, f"вќЊ {html.escape(str(e))}\nРЎРґРµР»Р°Р№ /check")
        return

    if cmd == "/draft":
        if not arg:
            tg.send_message(cfg, chat_id, "РџСЂРёРјРµСЂ: /draft Р’РµС‡РµСЂРЅРёР№ Р’Р°РіРіРѕ РїСЂРѕ С†РёС„СЂРѕРІРѕР№ С€СѓРј")
            return
        tg.send_message(cfg, chat_id, "вЏі РџРёС€Сѓ С‡РµСЂРЅРѕРІРёРєвЂ¦")
        try:
            body = generate_post(arg)
            item = add_draft(state, body, source="bot")
            tg.send_message(
                cfg,
                chat_id,
                f"рџ“ќ Р§РµСЂРЅРѕРІРёРє <code>{item['id']}</code>\n\n{body}",
                reply_markup=draft_keyboard(item["id"]),
            )
        except Exception as e:
            tg.send_message(cfg, chat_id, f"вќЊ {html.escape(str(e))}")
        return

    if cmd == "/guide":
        if not arg:
            tg.send_message(
                cfg,
                chat_id,
                "Р”Р»РёРЅРЅС‹Р№ РїРѕР»РµР·РЅС‹Р№ РіР°Р№Рґ (~2500вЂ“3800 СЃРёРјРІРѕР»РѕРІ).\n"
                "РџСЂРёРјРµСЂ: /guide 5 РїСЂРѕРјРїС‚РѕРІ РґР»СЏ РѕР±Р»РѕР¶РµРє Midjourney\n"
                "РёР»Рё: /guide РєР°Рє РІС‹Р±СЂР°С‚СЊ РјРµР¶РґСѓ ChatGPT Рё Claude",
            )
            return
        tg.send_message(cfg, chat_id, "вЏі РџРёС€Сѓ РіР°Р№РґвЂ¦ РјРёРЅСѓС‚Р°")
        try:
            body = generate_guide(arg)
            item = add_draft(state, body, rubric="Р“Р°Р№Рґ", source="guide")
            tg.send_message(
                cfg,
                chat_id,
                f"рџ“‹ Р“Р°Р№Рґ <code>{item['id']}</code> В· {len(body)} СЃРёРјРІ.\n\n{body[:3500]}",
                reply_markup=draft_keyboard(item["id"]),
            )
            if len(body) > 3500:
                tg.send_message(cfg, chat_id, body[3500:], parse_mode="HTML")
        except Exception as e:
            tg.send_message(cfg, chat_id, f"вќЊ {html.escape(str(e))}")
        return

    if cmd in ("/photo", "/img", "/video"):
        if not arg:
            tg.send_message(
                cfg,
                chat_id,
                "РџСЂРёРјРµСЂ:\n"
                "/photo Р’РµС‡РµСЂРЅРёР№ Р’Р°РіРіРѕ: С†РёС„СЂРѕРІРѕР№ С€СѓРј\n"
                "/video РђР±СЃС‚СЂР°РєС‚РЅС‹Р№ РЅРµР№СЂРѕ-РіРѕСЂРѕРґ РЅРѕС‡СЊСЋ\n"
                "/img С‚РѕР»СЊРєРѕ РєР°СЂС‚РёРЅРєР° Р±РµР· С‚РµРєСЃС‚Р° РїРѕСЃС‚Р°",
            )
            return
        want_video = cmd == "/video"
        caption_mode = cmd != "/img"
        tg.send_message(
            cfg,
            chat_id,
            "рџЋЁ ImagineвЂ¦ " + ("РІРёРґРµРѕ 10вЂ“40 СЃРµРє" if want_video else "С„РѕС‚Рѕ") + " вЂ” Р¶РґСѓ",
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
                    f"вњ… РњРµРґРёР° + С‡РµСЂРЅРѕРІРёРє <code>{draft_id}</code>\n"
                    f"Р–РјРё В«Р’ РєР°РЅР°Р»В» вЂ” СѓР№РґС‘С‚ С„РѕС‚Рѕ/РІРёРґРµРѕ СЃ РїРѕРґРїРёСЃСЊСЋ.",
                    reply_markup=draft_keyboard(draft_id),
                )
            else:
                tg.send_message(cfg, chat_id, f"вњ… Р¤Р°Р№Р»: <code>{html.escape(path.name)}</code>")
        except Exception as e:
            tg.send_message(cfg, chat_id, f"вќЊ Imagine: {html.escape(str(e))}")
        return

    if cmd == "/raw":
        state["await_raw"] = True
        save_state(state)
        tg.send_message(cfg, chat_id, "РџСЂРёС€Р»Рё СЃР»РµРґСѓСЋС‰РёРј СЃРѕРѕР±С‰РµРЅРёРµРј РіРѕС‚РѕРІС‹Р№ С‚РµРєСЃС‚ РїРѕСЃС‚Р° (РјРѕР¶РЅРѕ HTML).")
        return

    if state.get("await_raw") and not text.startswith("/"):
        state["await_raw"] = False
        item = add_draft(state, text, source="raw")
        save_state(state)
        tg.send_message(
            cfg,
            chat_id,
            f"рџ“ќ Р§РµСЂРЅРѕРІРёРє <code>{item['id']}</code> СЃРѕС…СЂР°РЅС‘РЅ.",
            reply_markup=draft_keyboard(item["id"]),
        )
        return

    if text and not text.startswith("/"):
        tg.send_message(cfg, chat_id, "вЏі Р”РµР»Р°СЋ РїРѕСЃС‚вЂ¦")
        try:
            body = generate_post(text)
            item = add_draft(state, body, source="owner_chat")
            tg.send_message(
                cfg,
                chat_id,
                f"рџ“ќ Р§РµСЂРЅРѕРІРёРє <code>{item['id']}</code>\n\n{body}",
                reply_markup=draft_keyboard(item["id"]),
            )
        except Exception as e:
            tg.send_message(cfg, chat_id, f"вќЊ {html.escape(str(e))}")


def handle_owner_menu_callback(
    cfg: dict, state: dict, cq: dict, *, data: str, chat_id, mid, user, uid
) -> bool:
    """
    РџСѓР»СЊС‚ РІР»Р°РґРµР»СЊС†Р°. Р›СЋР±РѕР№ menu:* (РєСЂРѕРјРµ userhome).
    Р‘С‹СЃС‚СЂРѕ, Р±РµР· Grok, Р±РµР· В«РЅРµРёР·РІРµСЃС‚РЅР°СЏ РєРЅРѕРїРєР°В».
    """
    if not data.startswith("menu:"):
        return False
    if data == "menu:userhome":
        return False
    if not is_owner(cfg, user):
        try:
            tg.answer_callback(cfg, cq["id"], "РўРѕР»СЊРєРѕ РІР»Р°РґРµР»РµС†", show_alert=True)
        except Exception:
            pass
        return True

    raw = (data[5:] or "").strip()
    # СЃС‚Р°СЂС‹Рµ РІР»РѕР¶РµРЅРЅС‹Рµ С„РѕСЂРјР°С‚С‹ в†’ home
    if raw.startswith(("g:", "grp_", "grp:")):
        raw = "home"

    try:
        tg.answer_callback(cfg, cq["id"], "вЂ¦")
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
        # home: edit С‚РµРєСѓС‰РµРіРѕ; РµСЃР»Рё РјС‘СЂС‚РІ вЂ” СЃР°Рј РІРѕСЃСЃС‚Р°РЅРѕРІРёС‚СЃСЏ
        home(force=False)
        return True
    if raw == "fresh":
        home(force=True)
        return True
    if raw == "more":
        # СЃРµСЂРІРёСЃРЅРѕРµ РїРѕРґРјРµРЅСЋ вЂ” С‚РѕР»СЊРєРѕ UI, Р±РµР· СЃРјРµРЅС‹ Р»РѕРіРёРєРё
        _owner_panel(
            cfg,
            state,
            chat_id,
            mid,
            uid_m,
            f"вљ™пёЏ  <b>Р•С‰С‘ В· СЃРµСЂРІРёСЃ</b>\n"
            f"{'в”Ђ' * 18}\n\n"
            f"Grok В· РєР°СЃСЃР° В· СЂР°РґР°СЂ\n"
            f"restore В· РєРЅРѕРїРєРё GW В· С‡РёСЃС‚РєР°\n\n"
            f"<i>РР»Рё РЅР°РїРёС€Рё:</i> В«РєР°РєРёРµ Р·Р°РєР°Р·С‹ РіРѕСЂСЏС‚В»\n"
            f"<i>РќР°Р·Р°Рґ вЂ” РІ РїСѓР»СЊС‚</i>",
            owner_more_keyboard(),
        )
        return True
    if raw == "radar":
        try:
            import growth_lib as growth

            body = growth.finance_radar_html()
        except Exception as e:
            body = f"вќЊ {html.escape(str(e)[:200])}"
        _owner_panel(cfg, state, chat_id, mid, uid_m, body, menu_result_keyboard())
        return True
    if raw == "hot":
        try:
            import growth_lib as growth

            body = growth.team_lead_html("РєР°РєРёРµ Р·Р°РєР°Р·С‹ РіРѕСЂСЏС‚") or "РќРµС‚ РґР°РЅРЅС‹С…."
        except Exception as e:
            body = f"вќЊ {html.escape(str(e)[:200])}"
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
                body = "вЏё РџР°СѓР·Р°. Р–РјРё в–¶пёЏ РІ РјРµРЅСЋ."
            else:
                due = due_items()
                item = due[0] if due else (queue_summary().get("next"))
                if not item:
                    body = "РћС‡РµСЂРµРґСЊ РїСѓСЃС‚Р°."
                else:
                    iid = str(item.get("id"))
                    try:
                        queue_publish_now(iid)
                        item = get_item(iid) or item
                        pmid = publish_queue_item(cfg, item)
                        body = (
                            f"вњ… <code>{html.escape(iid)}</code>\n"
                            f"https://t.me/Vaggo01/{pmid}"
                        )
                    except Exception as e:
                        body = f"вќЊ {html.escape(str(e)[:300])}"
        elif raw == "pause":
            cfg["paused"] = True
            save_config(cfg)
            body = "вЏё РџР°СѓР·Р°."
        elif raw == "resume":
            cfg["paused"] = False
            save_config(cfg)
            body = "в–¶пёЏ Resume."
        elif raw == "toggle_pause":
            cfg["paused"] = not bool(cfg.get("paused"))
            save_config(cfg)
            body = "вЏё РџР°СѓР·Р°." if cfg["paused"] else "в–¶пёЏ Resume."
        elif raw == "drafts":
            drafts = (state.get("drafts") or [])[-12:]
            if not drafts:
                body = "рџ“ќ Р§РµСЂРЅРѕРІРёРєРѕРІ РЅРµС‚."
            else:
                lines = ["рџ“ќ <b>Р§РµСЂРЅРѕРІРёРєРё</b>\n"]
                for d in reversed(drafts):
                    lines.append(
                        f"вЂў <code>{html.escape(str(d.get('id')))}</code> "
                        f"{html.escape((d.get('text') or '')[:40])}"
                    )
                body = "\n".join(lines)
        elif raw == "ideas":
            # Р±РµР· Grok вЂ” РЅРµ Р¶СЂС‘Рј Р»РёРјРёС‚; РєРѕСЂРѕС‚РєРёР№ С€Р°Р±Р»РѕРЅ
            body = (
                "рџ’Ў <b>РРґРµРё (Р±С‹СЃС‚СЂРѕ)</b>\n\n"
                "вЂў Р’РµС‡РµСЂРЅРёР№ Р’Р°РіРіРѕ: 1 РјС‹СЃР»СЊ + 1 РґРµР№СЃС‚РІРёРµ\n"
                "вЂў Р‘РёС‚РІР° СЃРµС‚РѕРє: Claude vs ChatGPT РЅР° РѕРґРЅСѓ Р·Р°РґР°С‡Сѓ\n"
                "вЂў РџСЂРѕРєР°С‡РєР°: 15 РјРёРЅ Р±РµР· С‚РµР»РµС„РѕРЅР°\n"
                "вЂў РљРёР±РµСЂ-Р»Р°Р№С„С…Р°Рє: 1 С„РёС€РєР° Windows/С‚РµР»РµС„РѕРЅР°\n"
                "вЂў РџСЂРѕРµРєС‚: С‡С‚Рѕ СЃРґРµР»Р°Р»Рё Р·Р° РґРµРЅСЊ\n\n"
                "РџРѕР»РЅС‹Рµ РёРґРµРё СЃ Grok: /ideas"
            )
        elif raw == "comments":
            pend = state.get("pending_comments") or []
            body = f"рџ’¬ РљРѕРјРјРµРЅС‚С‹ Р¶РґСѓС‚: <b>{len(pend)}</b>"
        elif raw == "giveaway":
            try:
                act = gw.get_active(state)
                body = gw.format_status(act)
                if act:
                    mid_ch = act.get("channel_message_id")
                    if mid_ch:
                        body += f"\n\nРџРѕСЃС‚: https://t.me/Vaggo01/{mid_ch}"
                # СЃРїРµС†РёР°Р»СЊРЅР°СЏ РєР»Р°РІРёР°С‚СѓСЂР° вЂ” РЅРµ menu_result
                kb = {
                    "inline_keyboard": [
                        [
                            {"text": "рџ‘Ґ РЈС‡Р°СЃС‚РЅРёРєРё", "callback_data": "menu:gentries"},
                            {"text": "рџЋІ Р РѕР·С‹РіСЂС‹С€", "callback_data": "menu:gdraw"},
                        ],
                        [
                            {"text": "рџ”„ РћР±РЅРѕРІРёС‚СЊ", "callback_data": "menu:giveaway"},
                            {"text": "рџЏ  РњРµРЅСЋ", "callback_data": "menu:home"},
                        ],
                        [
                            {
                                "text": "рџ“Ј РљР°Рє СЃРѕР·РґР°С‚СЊ",
                                "callback_data": "menu:ghelp",
                            }
                        ],
                    ]
                }
                _owner_panel(cfg, state, chat_id, mid, uid_m, body, kb)
                return True
            except Exception as e:
                body = f"вќЊ Р РѕР·С‹РіСЂС‹С€: {html.escape(str(e)[:200])}"
        elif raw == "gentries":
            try:
                act = gw.get_active(state)
                if not act:
                    body = "рџ‘Ґ РќРµС‚ Р°РєС‚РёРІРЅРѕРіРѕ СЂРѕР·С‹РіСЂС‹С€Р°.\n/gnew РїСЂРёР· в†’ /gpost"
                else:
                    body = "рџ‘Ґ <b>РЈС‡Р°СЃС‚РЅРёРєРё</b>\n\n" + gw.format_entries(act)
                kb = {
                    "inline_keyboard": [
                        [
                            {"text": "рџЋЃ Рљ СЂРѕР·С‹РіСЂС‹С€Сѓ", "callback_data": "menu:giveaway"},
                            {"text": "рџЋІ Draw", "callback_data": "menu:gdraw"},
                        ],
                        [{"text": "рџЏ  РњРµРЅСЋ", "callback_data": "menu:home"}],
                    ]
                }
                _owner_panel(cfg, state, chat_id, mid, uid_m, body, kb)
                return True
            except Exception as e:
                body = f"вќЊ {html.escape(str(e)[:200])}"
        elif raw == "gdraw":
            try:
                act = gw.get_active(state)
                if not act:
                    body = "РќРµС‚ Р°РєС‚РёРІРЅРѕРіРѕ. /gnew"
                elif gw.entry_count(act, complete_only=True) == 0:
                    body = "РќРµС‚ complete-СѓС‡Р°СЃС‚РЅРёРєРѕРІ вЂ” С‚СЏРЅСѓС‚СЊ РЅРµРєРѕРіРѕ."
                else:
                    winners = finish_giveaway_draw(cfg, act, notify_chat=chat_id)
                    if winners:
                        names = ", ".join(
                            html.escape(
                                str(w.get("username") or w.get("name") or w.get("user_id"))
                            )
                            for w in winners
                        )
                        body = f"рџЏ† РџРѕР±РµРґРёС‚РµР»СЊ(Рё): <b>{names}</b>"
                    else:
                        body = "РќРёРєРѕРіРѕ РЅРµ РІС‹С‚СЏРЅСѓР»Рё (РїСѓР» РїСѓСЃС‚ РїРѕСЃР»Рµ live-check)."
            except Exception as e:
                body = f"вќЊ draw: {html.escape(str(e)[:200])}"
        elif raw == "ghelp":
            body = (
                "рџЋЃ <b>Р РѕР·С‹РіСЂС‹С€ вЂ” РєР°Рє Р·Р°РїСѓСЃС‚РёС‚СЊ</b>\n\n"
                "1. <code>/gnew Google AI Pro 18 РјРµСЃ | 72</code>\n"
                "   (С‡Р°СЃС‹ РґРѕ Р°РІС‚Рѕ-РёС‚РѕРіР°)\n"
                "2. <code>/gpost</code> вЂ” РїРѕСЃС‚ РІ РєР°РЅР°Р» СЃ РєРЅРѕРїРєРѕР№\n"
                "3. Р›СЋРґРё Р¶РјСѓС‚ В«РЈС‡Р°СЃС‚РІРѕРІР°С‚СЊВ» в†’ РїРѕРґРїРёСЃРєР° + СЃРєСЂРёРЅ СЂРµРїРѕСЃС‚Р° РґСЂСѓРіСѓ\n"
                "4. РџРѕ С‚Р°Р№РјРµСЂСѓ СЃР°Рј / РёР»Рё РєРЅРѕРїРєР° рџЋІ / <code>/gdraw</code>\n\n"
                "<code>/gstatus</code> В· <code>/gentries</code> В· <code>/gend</code>\n"
                "<code>/gwrestore</code> вЂ” РІРµСЂРЅСѓС‚СЊ СѓС‡Р°СЃС‚РЅРёРєРѕРІ СЃ Р±СЌРєР°РїР°"
            )
        elif raw == "gwrestore":
            try:
                res = gw.apply_restore_seed(force=True)
                body = (
                    "в™»пёЏ <b>Restore</b>\n"
                    f"{html.escape(str(res.get('message') or res))}\n"
                    f"complete: <b>{res.get('complete') or 0}</b> В· "
                    f"started: {res.get('started') or 0}\n"
                    f"id: <code>{html.escape(str(res.get('active_id') or 'вЂ”'))}</code>\n\n"
                    "Р”Р°Р»СЊС€Рµ: <code>/gfixkb</code> вЂ” РєРЅРѕРїРєРё РЅР° РїРѕСЃС‚ РєР°РЅР°Р»Р°"
                )
            except Exception as e:
                body = f"вќЊ {html.escape(str(e)[:200])}"
        elif raw == "gfixkb":
            try:
                item = gw.get_active(state)
                if not item:
                    gw.apply_restore_seed(force=True)
                    item = gw.get_active()
                if not item:
                    body = "РќРµС‚ СЂРѕР·С‹РіСЂС‹С€Р°. /gwrestore"
                else:
                    mid_ch = item.get("channel_message_id")
                    if not mid_ch:
                        body = "РќРµС‚ id РїРѕСЃС‚Р°. /gbind 102"
                    else:
                        channel = cfg.get("channel_id") or "@Vaggo01"
                        tg.edit_reply_markup(
                            cfg,
                            channel,
                            int(mid_ch),
                            gw.join_keyboard(item, bot_username=_bot_username(cfg)),
                        )
                        body = (
                            f"вњ… РљРЅРѕРїРєРё РЅР° РїРѕСЃС‚Рµ\n"
                            f"https://t.me/Vaggo01/{mid_ch}\n"
                            f"complete: <b>{gw.entry_count(item, complete_only=True)}</b>"
                        )
            except Exception as e:
                body = f"вќЊ gfixkb: {html.escape(str(e)[:200])}"
        elif raw == "promo":
            try:
                from promo_lib import PROMO_HTML

                body = "рџ“ў <b>Р РµРєР»Р°РјР°</b>\n\n" + PROMO_HTML
            except Exception as e:
                body = f"вќЊ {html.escape(str(e)[:200])}"
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
                body = f"рџ’° Р‘Р°Р»Р°РЅСЃ: <b>{b}</b> в‚Ѕ"
            except Exception as e:
                body = f"вќЊ {html.escape(str(e)[:200])}"
        elif raw == "brains":
            # Р±С‹СЃС‚СЂРѕ: Р±РµР· probe СЃРµС‚Рё
            from content import brain_status, grok_ok

            bst = brain_status(cfg, use_cache=True, probe_ollama=False)
            body = (
                "рџ§  <b>РњРѕР·Рі</b>\n"
                f"active: {html.escape(str(bst.get('active')))}\n"
                f"source: {html.escape(str(bst.get('grok_source') or 'вЂ”'))}\n"
                f"ver: <code>{html.escape(BOT_CODE_VERSION)}</code>\n"
                f"bridge: {'РґР°' if grok_ok(cfg) else 'РЅРµС‚/fallback'}"
            )
        elif raw == "deploy":
            try:
                import deploy_lib

                need, remote, local = deploy_lib.needs_update()
                body = (
                    "рџ“¦ <b>Deploy</b>\n"
                    f"ver: <code>{html.escape(BOT_CODE_VERSION)}</code>\n"
                    f"local: <code>{html.escape((local or 'вЂ”')[:12])}</code>\n"
                    f"remote: <code>{html.escape((remote or 'вЂ”')[:12])}</code>\n"
                    f"need: {'YES' if need else 'no'}\n"
                    "/redeploy"
                )
            except Exception as e:
                body = f"вќЊ {html.escape(str(e)[:200])}"
        else:
            # Р»СЋР±РѕРµ РЅРµРїРѕРЅСЏС‚РЅРѕРµ в†’ РґРѕРјРѕР№, Р±РµР· СЃР»РѕРІР° В«РЅРµРёР·РІРµСЃС‚РЅР°СЏВ»
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
            f"вќЊ {html.escape(str(e)[:250])}\n\nР–РјРё рџЏ  Р’ РјРµРЅСЋ",
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

    # СЂРµС„РµСЂР°Р»РєР° (РІСЃРµРј)
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
            tg.answer_callback(cfg, cq["id"], "РЎСЃС‹Р»РєР°")
            if chat_id:
                tg.send_message(
                    cfg,
                    chat_id,
                    f"рџ”— <b>РўРІРѕСЏ СЃСЃС‹Р»РєР°</b>\n\n"
                    f"<code>{html.escape(link)}</code>\n\n"
                    f"Р”СЂСѓРі Р·Р°С…РѕРґРёС‚ в†’ РїРµСЂРІС‹Р№ РѕРїР»Р°С‡РµРЅРЅС‹Р№ Р·Р°РєР°Р· в†’ "
                    f"С‚РµР±Рµ <b>+{growth.REF_BONUS_RUB} в‚Ѕ</b> РЅР° Р±Р°Р»Р°РЅСЃ.",
                    parse_mode="HTML",
                    disable_preview=True,
                )
        except Exception as e:
            tg.answer_callback(cfg, cq["id"], str(e)[:100], show_alert=True)
        return

    # РџРЈР›Р¬Рў Р’Р›РђР”Р•Р›Р¬Р¦Рђ вЂ” РїРµСЂРІС‹Рј (С‡С‚РѕР±С‹ РЅРёС‡РµРіРѕ РЅРµ РїРµСЂРµС…РІР°С‚С‹РІР°Р»Рѕ)
    if data.startswith("menu:") and data != "menu:userhome":
        if handle_owner_menu_callback(
            cfg, state, cq, data=data, chat_id=chat_id, mid=mid, user=user, uid=uid
        ):
            return

    # РЈСЃР»РѕРІРёСЏ вЂ” РІСЃРµРіРґР° (РґР°Р¶Рµ Р±РµР· РїСЂРёРЅСЏС‚РёСЏ)
    if data.startswith("terms:"):
        handle_terms_callback(cfg, state, cq)
        return

    # Р®СЂ.РґРѕРє / С‚Р°СЂРёС„С‹ вЂ” РІСЃРµРіРґР° (Platega: РїРѕСЃС‚РѕСЏРЅРЅС‹Р№ РґРѕСЃС‚СѓРї)
    if data.startswith("legal:"):
        handle_legal_callback(cfg, state, cq)
        return

    # РўРёРєРµС‚С‹ РїРѕРґРґРµСЂР¶РєРё вЂ” РІСЃРµРіРґР° (Рё РґРѕ РїСЂРёРЅСЏС‚РёСЏ СѓСЃР»РѕРІРёР№)
    if data.startswith("sup:"):
        handle_support_callback(cfg, state, cq)
        return

    # РњРѕРґРµСЂР°С†РёСЏ: СЂР°Р·Р±Р»РѕРє вЂ” С‚РѕР»СЊРєРѕ owner callback
    if data.startswith("mod:"):
        handle_mod_callback(cfg, state, cq)
        return

    # Р‘Р»РѕРє вЂ” Р¶С‘СЃС‚РєРёР№ (РєСЂРѕРјРµ РІР»Р°РґРµР»СЊС†Р°). terms/legal СѓР¶Рµ РѕР±СЂР°Р±РѕС‚Р°РЅС‹ РІС‹С€Рµ в†’ РґРѕРєСѓРјРµРЅС‚С‹ РґРѕСЃС‚СѓРїРЅС‹
    if uid and not is_owner(cfg, user) and mod.is_blocked(uid):
        try:
            tg.answer_callback(cfg, cq["id"], "РђРєРєР°СѓРЅС‚ Р·Р°Р±Р»РѕРєРёСЂРѕРІР°РЅ", show_alert=True)
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
                            {"text": "рџ”’ РџРѕР»РёС‚РёРєР°", "url": terms.PRIVACY_URL},
                            {"text": "рџ“њ РћС„РµСЂС‚Р°", "url": terms.AGREEMENT_URL},
                        ],
                        [{"text": "рџ“‹ Р”РѕРєСѓРјРµРЅС‚С‹", "callback_data": "legal:hub"}],
                    ]
                },
            )
        return

    # РњРµРЅСЋ РїРѕР»СЊР·РѕРІР°С‚РµР»СЏ (РїРѕСЃР»Рµ accept)
    if data == "menu:userhome":
        _safe_answer_cq(cfg, cq["id"], "РњРµРЅСЋ")
        if chat_id:
            # РµСЃР»Рё mid РјС‘СЂС‚РІ вЂ” force_new С‡РµСЂРµР· edit fail recovery;
            # РєР»РёРє РїРѕ В«РћР±РЅРѕРІРёС‚СЊВ» РїСЂРµРґРїРѕС‡РёС‚Р°РµС‚ edit, РёРЅР°С‡Рµ send
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

    # Р РѕР·С‹РіСЂС‹С€ вЂ” Р”Рћ gate СѓСЃР»РѕРІРёР№ (РёРЅР°С‡Рµ В«РЈС‡Р°СЃС‚РІРѕРІР°С‚СЊВ» / РєРЅРѕРїРєРё РєРІРµСЃС‚Р° С‚СѓРїСЏС‚)
    if data.startswith("gw:"):
        handle_giveaway_callback(cfg, state, cq)
        return

    # Р‘РµР· РїСЂРёРЅСЏС‚РёСЏ вЂ” С‚РѕР»СЊРєРѕ terms/legal/sup (РєСЂРѕРјРµ РІР»Р°РґРµР»СЊС†Р°)
    if uid and not is_owner(cfg, user) and not terms.is_accepted(uid):
        try:
            tg.answer_callback(
                cfg, cq["id"], "РЎРЅР°С‡Р°Р»Р° РїСЂРёРјРё СѓСЃР»РѕРІРёСЏ", show_alert=True
            )
        except Exception:
            pass
        if chat_id:
            send_terms_gate(cfg, chat_id, state=state, uid=uid, message_id=mid)
        return

    # Р—Р°РєР°Р·С‹ вЂ” РІСЃРµРј (ord:type / ord:ok) + owner (ord:own:)
    if data.startswith("ord:"):
        handle_orders_callback(cfg, state, cq)
        return

    # Р‘Р°Р»Р°РЅСЃ / РЎР‘Рџ
    if data.startswith("bal:"):
        handle_balance_callback(cfg, state, cq)
        return

    # Р’СЃС‘ РѕСЃС‚Р°Р»СЊРЅРѕРµ (РјРµРЅСЋ, С‡РµСЂРЅРѕРІРёРєРё, РїР°СѓР·Р°вЂ¦) вЂ” РўРћР›Р¬РљРћ РІР»Р°РґРµР»РµС†
    if not is_owner(cfg, user):
        try:
            tg.answer_callback(
                cfg,
                cq["id"],
                "РњРµРЅСЋ: /start В· /support В· /legal",
                show_alert=True,
            )
        except Exception:
            pass
        return

    if data.startswith("pub:"):
        did = data.split(":", 1)[1]
        draft = get_draft(state, did)
        if not draft:
            tg.answer_callback(cfg, cq["id"], "РќРµ РЅР°Р№РґРµРЅ")
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
            tg.answer_callback(cfg, cq["id"], "РћРїСѓР±Р»РёРєРѕРІР°РЅРѕ")
            tg.edit_reply_markup(cfg, chat_id, mid, None)
            tg.send_message(cfg, chat_id, f"вњ… Р’ РєР°РЅР°Р»Рµ. draft={did} msg={pmid}")
        except Exception as e:
            tg.answer_callback(cfg, cq["id"], "РћС€РёР±РєР°")
            tg.send_message(cfg, chat_id, f"вќЊ {html.escape(str(e))}")
        return

    if data.startswith("rew:"):
        did = data.split(":", 1)[1]
        draft = get_draft(state, did)
        if not draft:
            tg.answer_callback(cfg, cq["id"], "РќРµ РЅР°Р№РґРµРЅ")
            return
        tg.answer_callback(cfg, cq["id"], "РџРµСЂРµРїРёСЃС‹РІР°СЋвЂ¦")
        try:
            body = rewrite_post(draft["text"])
            item = add_draft(state, body, rubric=draft.get("rubric") or "", source="rewrite")
            tg.send_message(
                cfg,
                chat_id,
                f"вњЏпёЏ <code>{item['id']}</code>\n\n{body}",
                reply_markup=draft_keyboard(item["id"]),
            )
        except Exception as e:
            tg.send_message(cfg, chat_id, f"вќЊ {html.escape(str(e))}")
        return

    if data.startswith("drop:"):
        did = data.split(":", 1)[1]
        for d in state.get("drafts") or []:
            if d.get("id") == did:
                d["status"] = "dropped"
        save_state(state)
        tg.answer_callback(cfg, cq["id"], "РЈРґР°Р»РµРЅРѕ")
        tg.edit_reply_markup(cfg, chat_id, mid, None)
        return

    if data.startswith("creply:"):
        cid = data.split(":", 1)[1]
        item = next((c for c in state.get("pending_comments") or [] if c.get("id") == cid), None)
        if not item:
            tg.answer_callback(cfg, cq["id"], "РќРµ РЅР°Р№РґРµРЅРѕ")
            return
        try:
            tg.send_message(
                cfg,
                item["chat_id"],
                item.get("reply_text") or "рџ‘Ќ",
                reply_to=item.get("message_id"),
                parse_mode=None,
                message_thread_id=item.get("message_thread_id"),
                allow_without_reply=False,
                disable_preview=True,
            )
            item["status"] = "replied"
            save_state(state)
            tg.answer_callback(cfg, cq["id"], "РћС‚РІРµС‚ СѓС€С‘Р»")
            tg.edit_reply_markup(cfg, chat_id, mid, None)
        except Exception as e:
            tg.answer_callback(cfg, cq["id"], "РћС€РёР±РєР°")
            tg.send_message(cfg, chat_id, f"вќЊ {html.escape(str(e))}")
        return

    if data.startswith("cskip:"):
        cid = data.split(":", 1)[1]
        for c in state.get("pending_comments") or []:
            if c.get("id") == cid:
                c["status"] = "skipped"
        save_state(state)
        tg.answer_callback(cfg, cq["id"], "РџСЂРѕРїСѓСЃРє")
        tg.edit_reply_markup(cfg, chat_id, mid, None)
        return

    if data.startswith("crewrite:"):
        cid = data.split(":", 1)[1]
        item = next((c for c in state.get("pending_comments") or [] if c.get("id") == cid), None)
        if not item:
            tg.answer_callback(cfg, cq["id"], "РќРµ РЅР°Р№РґРµРЅРѕ")
            return
        try:
            new_reply = generate_comment_reply(item.get("comment_text") or "")
            item["reply_text"] = new_reply
            save_state(state)
            tg.answer_callback(cfg, cq["id"], "РќРѕРІС‹Р№")
            tg.send_message(
                cfg,
                chat_id,
                f"вњЏпёЏ <code>{cid}</code>:\n{html.escape(new_reply)}",
                reply_markup=comment_keyboard(cid),
            )
        except Exception as e:
            tg.send_message(cfg, chat_id, f"вќЊ {html.escape(str(e))}")
        return

    tg.answer_callback(cfg, cq["id"])


def maybe_bind_group(cfg: dict, msg: dict) -> bool:
    """Р’ РіСЂСѓРїРїРµ: /bind РїСЂРёРІСЏР·С‹РІР°РµС‚ discussion_group_id."""
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
            f"вњ… Р“СЂСѓРїРїР° РїСЂРёРІСЏР·Р°РЅР° РєР°Рє РѕР±СЃСѓР¶РґРµРЅРёРµ РєР°РЅР°Р»Р°.\n"
            f"id=<code>{gid}</code>\nРљРѕРјРјРµРЅС‚С‹ РїРѕР№РґСѓС‚ РІР»Р°РґРµР»СЊС†Сѓ РЅР° РѕРє (РёР»Рё instant).",
        )
    except Exception as e:
        print("bind notify", e)
    notify_owner(cfg, f"вњ… discussion_group_id = <code>{gid}</code> ({html.escape(chat.get('title') or '')})")
    return True


def maybe_hint_unknown_group(cfg: dict, state: dict, msg: dict) -> None:
    """Р•СЃР»Рё Р±РѕС‚ РІРёРґРёС‚ РіСЂСѓРїРїСѓ Р±РµР· bind вЂ” РїРѕРґСЃРєР°Р·Р°С‚СЊ id РІР»Р°РґРµР»СЊС†Сѓ (СЂРµРґРєРѕ)."""
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
        f"рџ‘Ѓ Р‘РѕС‚ РІРёРґРёС‚ РіСЂСѓРїРїСѓ <b>{html.escape(chat.get('title') or '?')}</b>\n"
        f"id=<code>{gid}</code>\n"
        f"Р•СЃР»Рё СЌС‚Рѕ РѕР±СЃСѓР¶РґРµРЅРёРµ РєР°РЅР°Р»Р° вЂ” РЅР°РїРёС€Рё С‚Р°Рј /bind\n"
        f"РёР»Рё /bind {gid} РІ Р»РёС‡РєРµ Р±РѕС‚Сѓ.",
    )


# РїСЂРѕСЃС‚РѕР№ Р°РЅС‚РёСЃРїР°Рј РґР»СЏ РєРѕРјРјРµРЅС‚РѕРІ (РІ РїР°РјСЏС‚Рё РїСЂРѕС†РµСЃСЃР°)
_comment_rate: dict[int, float] = {}
_comment_global: list[float] = []


def _rate_ok(user_id: int, cfg: dict) -> bool:
    # РІР»Р°РґРµР»РµС† вЂ” Р±РµР· Р»РёРјРёС‚Р° (С‚РµСЃС‚С‹/РѕС‚РІРµС‚С‹ РЅРµ РіР»СѓС€РёРј)
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
    """РЎРІРµСЂРёС‚СЊ id/username РєР°РЅР°Р»Р° (int/str РЅРµ РґРѕР»Р¶РЅС‹ Р»РѕРјР°С‚СЊ С„РёР»СЊС‚СЂ)."""
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
    # РїСѓР±Р»РёС‡РЅС‹Р№ РєР°РЅР°Р» Р±РµР· username РІ Р°РїРґРµР№С‚Рµ вЂ” РµСЃР»Рё numeric СЃРѕРІРїР°Р» РІС‹С€Рµ; РёРЅР°С‡Рµ РЅРµС‚
    return False


def maybe_react_channel_post(cfg: dict, state: dict, post: dict) -> None:
    """Р РµР°РєС†РёСЏ РЅР° РєР°Р¶РґС‹Р№ РЅРѕРІС‹Р№ РїРѕСЃС‚ РєР°РЅР°Р»Р°."""
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
    # РєР»СЋС‡ Р±РµР· В«РїР»Р°РІР°СЋС‰РµРіРѕВ» chat_id (str/int)
    key = f"reacted_ch_{mid}"
    if state.get(key):
        return
    emoji = (
        (cfg.get("channel_react_emoji") or "").strip()
        or pick_reaction_for_text(text)
        or "рџ”Ґ"
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
        # РЅРµ РїРѕРјРµС‡Р°РµРј key вЂ” РїРѕРїСЂРѕР±СѓРµРј РµС‰С‘ СЂР°Р· СЃ discussion-С„РѕСЂРІР°СЂРґР°

def maybe_seed_under_channel_forward(cfg: dict, state: dict, msg: dict) -> bool:
    """РљРѕРіРґР° РІ РѕР±СЃСѓР¶РґРµРЅРёРё РїРѕСЏРІРёР»СЃСЏ Р°РІС‚Рѕ-С„РѕСЂРІР°СЂРґ РїРѕСЃС‚Р° вЂ” СЂРµР°РєС†РёСЏ РЅР° РїРѕСЃС‚ + seed-РєРѕРјРјРµРЅС‚."""
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

    # backup: СЂРµР°РєС†РёСЏ РЅР° РїРѕСЃС‚ РєР°РЅР°Р»Р° (РµСЃР»Рё channel_post РЅРµ РґРѕС€С‘Р» / СЌРјРѕРґР·Рё СѓРїР°Р»)
    rkey = f"reacted_ch_{ch_mid}"
    if cfg.get("auto_react_posts", True) and not state.get(rkey):
        emoji = (
            (cfg.get("channel_react_emoji") or "").strip()
            or pick_reaction_for_text(post_ctx)
            or "рџ”Ґ"
        )
        try:
            tg.set_message_reaction(cfg, channel, int(ch_mid), emoji)
            state[rkey] = int(time.time())
            save_state(state)
            print(f"channel react via discuss mid={ch_mid} emoji={emoji}", flush=True)
        except Exception as e:
            print("channel react via discuss fail", ch_mid, str(e)[:100], flush=True)

    if not cfg.get("auto_seed_comment", True):
        return True  # СЂРµР°РєС†РёСЋ СѓР¶Рµ РїРѕРїС‹С‚Р°Р»РёСЃСЊ

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
    """member/admin/creator вЂ” РїРѕРґРїРёСЃР°РЅ РЅР° РєР°РЅР°Р»."""
    channel = cfg.get("channel_numeric_id") or cfg.get("channel_id") or "@Vaggo01"
    try:
        m = tg.get_chat_member(cfg, channel, int(user_id))
        return (m.get("status") or "") in ("member", "administrator", "creator", "restricted")
    except Exception as e:
        print("sub check fail", e, flush=True)
        # РµСЃР»Рё РЅРµ СЃРјРѕРіР»Рё РїСЂРѕРІРµСЂРёС‚СЊ (Р±РѕС‚ РЅРµ Р°РґРјРёРЅ?) вЂ” РЅРµ Р±Р»РѕРєРёСЂСѓРµРј Р¶С‘СЃС‚РєРѕ
        return True


def _bot_username(cfg: dict) -> str:
    try:
        me = tg.get_me(cfg)
        return (me.get("username") or "DirectorVaggobot").lstrip("@")
    except Exception:
        return "DirectorVaggobot"


def _check_all_subs(cfg: dict, item: dict, user_id: int) -> tuple[bool, list[str]]:
    """РџСЂРѕРІРµСЂРёС‚СЊ РІСЃРµ РєР°РЅР°Р»С‹. Р’РѕР·РІСЂР°С‰Р°РµС‚ (all_ok, missing_list)."""
    missing = []
    # РєР°РЅРѕРЅРёС‡РµСЃРєРёРµ РёРґРµРЅС‚РёС„РёРєР°С‚РѕСЂС‹ РєР°РЅР°Р»Р° (username + numeric)
    chans = list(gw.all_required_channels(cfg, item))
    try:
        num = int(cfg.get("channel_numeric_id") or -1004445937686)
        if str(num) not in chans and f"@{cfg.get('channel_username') or 'Vaggo01'}" in chans:
            # РґР»СЏ API РёРЅРѕРіРґР° numeric СЃС‚Р°Р±РёР»СЊРЅРµРµ
            pass
    except Exception:
        num = -1004445937686

    for ch in chans:
        ok_here = False
        # РїСЂРѕР±СѓРµРј username Рё numeric РґР»СЏ РіР»Р°РІРЅРѕРіРѕ РєР°РЅР°Р»Р°
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
            # РІ UI РІСЃРµРіРґР° РїРѕРєР°Р·С‹РІР°РµРј @Vaggo01, РЅРµ id С‡РµР»РѕРІРµРєР°
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
    Р–РёРІР°СЏ РїСЂРѕРІРµСЂРєР° РїРѕРґРїРёСЃРєРё + РїРµСЂРµСЃС‡С‘С‚ complete.
    Р’ РєРѕРЅРєСѓСЂСЃ (complete) С‚РѕР»СЊРєРѕ РµСЃР»Рё: РїРѕРґРїРёСЃРєР° РћРљ + СЂРµРїРѕСЃС‚ + РґСЂСѓР·СЊСЏ.
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
    """РџРµСЂРµРґ СЂРѕР·С‹РіСЂС‹С€РµРј: РїРµСЂРµРїСЂРѕРІРµСЂРёС‚СЊ РїРѕРґРїРёСЃРєСѓ; Р±РµР· РїРѕРґРїРёСЃРєРё/СЂРµРїРѕСЃС‚Р° вЂ” РЅРµ РІ Р±Р°СЂР°Р±Р°РЅРµ."""
    # РєРѕРїРёСЏ uid, С‚.Рє. set_subs_ok РјРµРЅСЏРµС‚ entries
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
    """РћР±РЅРѕРІРёС‚СЊ С‚РѕР»СЊРєРѕ РєРЅРѕРїРєРё (Р±РµР· СЃС‡С‘С‚С‡РёРєРѕРІ РІ С‚РµРєСЃС‚Рµ вЂ” РїСЂРёРІР°С‚РЅРѕ)."""
    ch_mid = item.get("channel_message_id")
    if not ch_mid:
        return
    channel = cfg.get("channel_id") or "@Vaggo01"
    try:
        fresh = gw.get_by_id(str(item.get("id"))) or item
        # РЅРµ С‚СЂРѕРіР°РµРј РєРЅРѕРїРєСѓ РЅР° РєР°Р¶РґС‹Р№ РєР»РёРє вЂ” С‚РѕР»СЊРєРѕ РµСЃР»Рё СЂР°Р·РјРµС‚РєР° СЂРµР°Р»СЊРЅРѕ РјРµРЅСЏРµС‚СЃСЏ
        # (СЃРµР№С‡Р°СЃ label СЃС‚Р°С‚РёС‡РЅС‹Р№; refresh РЅСѓР¶РµРЅ РїРѕСЃР»Рµ ended)
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
    parts.append(f"рџЋЃ <b>РљРІРµСЃС‚ СЂРѕР·С‹РіСЂС‹С€Р°</b>")
    parts.append(f"РџСЂРёР·: <b>{prize}</b>")
    parts.append("")
    parts.append(gw.progress_bar(item, entry))
    parts.append("")
    parts.append(f"<b>1. РџРѕРґРїРёСЃРєРё</b> (РїСЂРѕРІРµСЂСЏРµРј): {html.escape(chans)}")
    if item.get("require_repost", True):
        parts.append("")
        parts.append(
            f"<b>2. Р РµРїРѕСЃС‚ РґСЂСѓРіСѓ</b>\n"
            f"в†— РџРµСЂРµСЃР»Р°С‚СЊ в†’ <b>Р¶РёРІРѕР№ С‡РµР»РѕРІРµРє</b> в†’ СЃРєСЂРёРЅ СЃСЋРґР°.\n"
            f"Р‘РѕС‚ Р¶С‘СЃС‚РєРѕ РїСЂРѕРІРµСЂСЏРµС‚: РЅРµ Р±РѕС‚, РЅРµ РР·Р±СЂР°РЅРЅРѕРµ, РЅРµ СЃРµР±Рµ.\n"
            f"РџРѕСЃС‚: {post_link}"
        )
    if inv_need > 0:
        parts.append("")
        parts.append(
            f"<b>3. Р”СЂСѓР·СЊСЏ</b> ({inv_need})\n<code>{html.escape(ref)}</code>"
        )
    if tip:
        parts.append("")
        parts.append(tip.strip())
    parts.append("")
    parts.append("Р’ РєРѕРЅРєСѓСЂСЃ вЂ” РїРѕСЃР»Рµ РїСЂРѕРІРµСЂРєРё РїРѕРґРїРёСЃРєРё Рё СЂРµРїРѕСЃС‚Р°. РљРЅРѕРїРєРё РЅРёР¶Рµ.")
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
    РћРґРЅР° РєР°СЂС‚РѕС‡РєР° РєРІРµСЃС‚Р°: edit СЃС‚Р°СЂРѕРіРѕ СЃРѕРѕР±С‰РµРЅРёСЏ РёР»Рё delete+send.
    РќРµ СЃРїР°РјРёС‚ РЅРѕРІС‹РјРё СЃРѕРѕР±С‰РµРЅРёСЏРјРё РїСЂРё РєР°Р¶РґРѕРј РєР»РёРєРµ.
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
            # Telegram: С‚РѕС‚ Р¶Рµ С‚РµРєСЃС‚/РєРЅРѕРїРєРё вЂ” РЅРµ РѕС€РёР±РєР°
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
    """РЁРѕСѓ-РІС‹РґР°С‡Р°: 4 РїРѕСЃС‚Р° + РѕР±РЅРѕРІР»РµРЅРёРµ РєРЅРѕРїРєРё."""
    # С„РёРЅР°Р»СЊРЅР°СЏ live-РїСЂРѕРІРµСЂРєР° РїРѕРґРїРёСЃРєРё Рё СЂРµРїРѕСЃС‚Р°
    pool = live_filter_draw_pool(cfg, item)
    n_ok = len(pool)
    if n_ok == 0:
        raise RuntimeError(
            "РќРµС‚ СѓС‡Р°СЃС‚РЅРёРєРѕРІ СЃ РїСЂРѕРІРµСЂРµРЅРЅРѕР№ РїРѕРґРїРёСЃРєРѕР№ Рё СЂРµРїРѕСЃС‚РѕРј (Рё РґСЂСѓР·СЊСЏРјРё)"
        )
    winners = gw.draw_winners(item, pool=pool)
    if not winners:
        raise RuntimeError("РќРµ СѓРґР°Р»РѕСЃСЊ РІС‹Р±СЂР°С‚СЊ")
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

    # РєРѕРјРјРµРЅС‚ РїРѕРґ РїРѕСЃС‚РѕРј СЂРѕР·С‹РіСЂС‹С€Р° вЂ” С„РёРЅР°Р»
    if ch_mid and last_mid:
        try:
            w = winners[0]
            un = w.get("username")
            who = f"@{un}" if un else w.get("name")
            tg.comment_on_channel_post(
                cfg,
                int(ch_mid),
                f"рџЏ† РџРѕР±РµРґРёС‚РµР»СЊ: {who}! РџРёС€Рё @DirectorVaggobot РІ Р»РёС‡РєСѓ.",
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
        f"рџЏ† <b>РЁРѕСѓ-РёС‚РѕРіРё</b>\n"
        f"РџРѕР±РµРґРёС‚РµР»СЊ: <b>{html.escape(', '.join(names))}</b>\n"
        f"РџСЂРѕС€Р»Рё РєРІРµСЃС‚: {n_ok}\n"
        f"{link}\n\n"
        f"Р’С‹РґР°Р№ РїСЂРёР· РІ Р»РёС‡РєСѓ РїРѕР±РµРґРёС‚РµР»СЋ (СЃСЃС‹Р»РєСѓ РЅРµ СЃРІРµС‚Рё РІ РєР°РЅР°Р»Рµ)."
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
                f"рџЏ† РџРѕР·РґСЂР°РІР»СЏСЋ! РўС‹ РІС‹РёРіСЂР°Р»(Р°): <b>{html.escape(str(item.get('prize') or ''))}</b>\n\n"
                f"РќР°РїРёС€Рё СЃСЋРґР° В«С…РѕС‡Сѓ РїСЂРёР·В» вЂ” РІС‹РґР°РґРёРј РІ С‚РµС‡РµРЅРёРµ 48 С‡Р°СЃРѕРІ.",
                parse_mode="HTML",
            )
        except Exception:
            pass
    print("giveaway drawn", item.get("id"), names, flush=True)
    return winners


def tick_giveaways(cfg: dict) -> None:
    """РђРІС‚Рѕ-draw / РїСЂРѕРґР»РµРЅРёРµ РґРѕ min_complete (РїРѕ СѓРјРѕР»С‡Р°РЅРёСЋ 10)."""
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

            # РјР°Р»Рѕ Р»СЋРґРµР№ в†’ СЃРґРІРёРіР°РµРј РґРµРґР»Р°Р№РЅ, РЅРµ С‚СЏРЅРµРј Рё РЅРµ Р·Р°РєСЂС‹РІР°РµРј
            if need > 0 and n_ok < need:
                if gw.is_expired(item):
                    ext = gw.maybe_extend_for_min_complete(item)
                    if ext:
                        ends = ext.get("ends_at")
                        when = (
                            time.strftime("%d.%m %H:%M", time.localtime(int(ends)))
                            if ends
                            else "вЂ”"
                        )
                        notify_owner(
                            cfg,
                            f"вЏі Р РѕР·С‹РіСЂС‹С€ РїСЂРѕРґР»С‘РЅ (РјР°Р»Рѕ Р»СЋРґРµР№)\n"
                            f"complete <b>{n_ok}/{need}</b>\n"
                            f"РЅРѕРІС‹Р№ СЃСЂРѕРє: <b>{when}</b>\n"
                            f"РїСЂРѕРґР»РµРЅРёР№: {ext.get('extend_count')}\n"
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
                        # Р»РёРјРёС‚ РїСЂРѕРґР»РµРЅРёР№ вЂ” РЅРµ Р·Р°РєСЂС‹РІР°РµРј СЃ 0, РїСЂРѕСЃС‚Рѕ Р»РѕРі
                        print(
                            "gw extend skip/limit",
                            item.get("id"),
                            n_ok,
                            need,
                            flush=True,
                        )
                # РµС‰С‘ РЅРµ expired, РЅРѕ already in due РёР·-Р·Р° need? only when n_ok>=need
                continue

            if n_ok == 0:
                # Р±РµР· min_complete Рё РЅРёРєРѕРіРѕ вЂ” РјРѕР¶РЅРѕ Р·Р°РєСЂС‹С‚СЊ
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
                    f"вЏ№ Р РѕР·С‹РіСЂС‹С€ <b>{html.escape(str(item.get('prize') or ''))}</b> Р·Р°РІРµСЂС€С‘РЅ.\n"
                    f"РќРёРєС‚Рѕ РЅРµ Р·Р°РєСЂС‹Р» РІСЃРµ С€Р°РіРё РєРІРµСЃС‚Р° вЂ” РїРѕР±РµРґРёС‚РµР»СЏ РЅРµС‚.\n"
                    f"Р’ СЃР»РµРґСѓСЋС‰РёР№ СЂР°Р· Р±СѓРґРµС‚ Р¶Р°СЂС‡Рµ рџ”Ґ",
                    parse_mode="HTML",
                )
                notify_owner(
                    cfg,
                    f"вЏ№ Р РѕР·С‹РіСЂС‹С€ <code>{html.escape(str(item.get('id')))}</code> "
                    f"РёСЃС‚С‘Рє, complete=0.",
                )
                continue
            finish_giveaway_draw(cfg, item)
        except Exception as e:
            print("auto draw fail", item.get("id"), e, flush=True)


def ui_try_delete(cfg: dict, chat_id: int | str, message_id: int | None) -> bool:
    return bool(tg.try_delete_message(cfg, chat_id, message_id))


def ui_delete_user_message(cfg: dict, msg: dict | None) -> None:
    """РЈР±СЂР°С‚СЊ СЃРѕРѕР±С‰РµРЅРёРµ РїРѕР»СЊР·РѕРІР°С‚РµР»СЏ РІ Р»РёС‡РєРµ (С‡С‚РѕР±С‹ РѕРїСЂРѕСЃ РЅРµ СЂР°СЃРїРѕР»Р·Р°Р»СЃСЏ)."""
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
    Р—Р°РїРѕРјРЅРёС‚СЊ mid Р±РѕС‚Р° РІ Р›РЎ Рё СѓРґР°Р»РёС‚СЊ РІСЃС‘ СЃС‚Р°СЂРµРµ keep.
    Р”РµСЂР¶РёРј 1вЂ“2 РѕРєРЅР° (РјРµРЅСЋ + notify), РѕСЃС‚Р°Р»СЊРЅРѕРµ вЂ” СЃРїР°Рј.
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
    РђРіСЂРµСЃСЃРёРІРЅРѕ: СЃРЅРµСЃС‚Рё mid-1 вЂ¦ mid-lookback (С‚РѕР»СЊРєРѕ СЃРѕРѕР±С‰РµРЅРёСЏ Р±РѕС‚Р° СѓРґР°Р»СЏС‚СЃСЏ).
    Telegram РЅРµ РґР°С‘С‚ РёСЃС‚РѕСЂРёСЋ С‡Р°С‚Р° вЂ” РёРґС‘Рј РЅР°Р·Р°Рґ РїРѕ id.
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
    """РЈРґР°Р»РёС‚СЊ tracked + (deep) РїРѕСЃР»РµРґРЅРёРµ ~120 mid Р±РѕС‚Р°. keep_mids СЃРѕС…СЂР°РЅСЏРµРј."""
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


# Р’СЃРµ В«РїР°РЅРµР»РёВ» РІ Р»РёС‡РєРµ = РћР”РќРћ СЃРѕРѕР±С‰РµРЅРёРµ
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
    РЈРјРЅС‹Р№ UI (С€РµРґРµРІСЂ Р±РµР· РїРѕС‚РµСЂРё РѕРєРЅР°):

    вЂў РѕР±С‹С‡РЅС‹Р№ СЂРµР¶РёРј вЂ” edit Р¶РёРІРѕРіРѕ СЃРѕРѕР±С‰РµРЅРёСЏ (Р±РµР· СЃРїР°РјР°);
    вЂў force_new / РјС‘СЂС‚РІС‹Р№ mid вЂ” РЅРѕРІРѕРµ СЃРѕРѕР±С‰РµРЅРёРµ + pin;
    вЂў СЃС‚Р°СЂРѕРµ РѕРєРЅРѕ С‚РёС…Рѕ СѓРґР°Р»СЏРµРј, РµСЃР»Рё РјРѕР¶РµРј (С‡Р°С‚ РЅРµ Р·Р°СЂР°СЃС‚Р°РµС‚);
    вЂў РЅРёРєРѕРіРґР° РЅРµ РјРѕР»С‡РёРј: edit fail в†’ send.
    """
    _ = delete_extra
    _ = store_key  # РІСЃРµ РєР»СЋС‡Рё РїРёС€РµРј РІ main_ui_msg

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

    # 1) edit, РµСЃР»Рё РЅРµ force_new
    for mid in candidates:
        if _try_edit(mid, text_use, "HTML"):
            _store(mid, do_pin=False)
            return mid
        if _try_edit(mid, _strip_html(text_use)[:4090], None):
            _store(mid, do_pin=False)
            return mid

    # 2) recovery / force_new вЂ” РЅРѕРІРѕРµ РѕРєРЅРѕ
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
        # СѓР±СЂР°С‚СЊ СЃС‚Р°СЂС‹Рµ РїР°РЅРµР»Рё, С‡С‚РѕР±С‹ РЅРµ С‚РµСЂСЏР»РёСЃСЊ РІ РєСѓС‡Рµ
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
    # Telegram 4096 limit вЂ” full may be long
    if len(text) > 4000:
        text = text[:3990] + "вЂ¦"
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
    """РџРѕРєР°Р·/РїРѕРІС‚РѕСЂ СѓСЃР»РѕРІРёР№. РќРµ С‚СЂРµР±СѓРµС‚ РїСЂРёРЅСЏС‚РёСЏ."""
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

    if cmd in ("/ref", "/СЂРµС„", "/invite", "/СЂРµС„РµСЂР°Р»"):
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
                f"рџ”— <b>Р РµС„РµСЂР°Р»СЊРЅР°СЏ СЃСЃС‹Р»РєР°</b>\n\n"
                f"<code>{html.escape(link)}</code>\n\n"
                f"Р”СЂСѓРі Р¶РјС‘С‚ Start в†’ РµРіРѕ РїРµСЂРІС‹Р№ РѕРїР»Р°С‡РµРЅРЅС‹Р№ Р·Р°РєР°Р· в†’ "
                f"С‚РµР±Рµ <b>+{growth.REF_BONUS_RUB} в‚Ѕ</b>.\n/balance",
                parse_mode="HTML",
                disable_preview=True,
            )
        except Exception as e:
            tg.send_message(cfg, chat_id, f"вќЊ {html.escape(str(e)[:150])}")
        return True

    if cmd in (
        "/terms",
        "/rules",
        "/policy",
        "/РїСЂР°РІРёР»Р°",
        "/РїРѕР»РёС‚РёРєР°",
        "/СѓСЃР»РѕРІРёСЏ",
        "/РѕС„РµСЂС‚Р°",
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

    if cmd in ("/privacy", "/РєРѕРЅС„РёРґРµРЅС†РёР°Р»СЊРЅРѕСЃС‚СЊ"):
        tg.send_message(
            cfg,
            chat_id,
            "рџ”’ <b>РџРѕР»РёС‚РёРєР° РєРѕРЅС„РёРґРµРЅС†РёР°Р»СЊРЅРѕСЃС‚Рё</b>\n"
            "Director Vaggo В· РґР»СЏ Platega Рё РєР»РёРµРЅС‚РѕРІ\n\n"
            f'<a href="{terms.PRIVACY_URL}">РћС‚РєСЂС‹С‚СЊ РґРѕРєСѓРјРµРЅС‚</a>\n'
            f"<code>{terms.PRIVACY_URL}</code>",
            parse_mode="HTML",
            reply_markup=terms.legal_menu_keyboard(),
            disable_preview=False,
        )
        return True

    if cmd in (
        "/agreement",
        "/offer",
        "/СЃРѕРіР»Р°С€РµРЅРёРµ",
        "/РѕС„РµСЂС‚Р°_РґРѕРє",
        "/public_offer",
        "/РїСѓР±Р»РёС‡РЅР°СЏ_РѕС„РµСЂС‚Р°",
    ):
        tg.send_message(
            cfg,
            chat_id,
            "рџ“њ <b>РџРѕР»СЊР·РѕРІР°С‚РµР»СЊСЃРєРѕРµ СЃРѕРіР»Р°С€РµРЅРёРµ / РѕС„РµСЂС‚Р°</b>\n"
            "Director Vaggo В· РґР»СЏ Platega Рё РєР»РёРµРЅС‚РѕРІ\n\n"
            f'<a href="{terms.AGREEMENT_URL}">РћС‚РєСЂС‹С‚СЊ РґРѕРєСѓРјРµРЅС‚</a>\n'
            f"<code>{terms.AGREEMENT_URL}</code>",
            parse_mode="HTML",
            reply_markup=terms.legal_menu_keyboard(),
            disable_preview=False,
        )
        return True

    if cmd in ("/prices", "/pricing", "/tariffs", "/С‚Р°СЂРёС„С‹", "/С†РµРЅС‹"):
        tg.send_message(
            cfg,
            chat_id,
            terms.prices_html(cfg),
            parse_mode="HTML",
            reply_markup=terms.legal_menu_keyboard(),
            disable_preview=True,
        )
        return True

    if cmd in ("/support", "/help_support", "/РїРѕРґРґРµСЂР¶РєР°", "/РєРѕРЅС‚Р°РєС‚"):
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

    if cmd in ("/tickets", "/mytickets", "/С‚РёРєРµС‚С‹", "/РѕР±СЂР°С‰РµРЅРёСЏ"):
        if is_owner(cfg, user):
            tg.send_message(
                cfg,
                chat_id,
                support.staff_list_html(),
                parse_mode="HTML",
                reply_markup={
                    "inline_keyboard": [
                        [{"text": "рџ“‹ РћР±РЅРѕРІРёС‚СЊ", "callback_data": "sup:stafflist"}]
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

    if cmd in ("/legal", "/docs", "/РґРѕРєСѓРјРµРЅС‚С‹"):
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

    if cmd in ("/menu", "/РјРµРЅСЋ") and not is_owner(cfg, user):
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

    # /start РїРѕР»СЊР·РѕРІР°С‚РµР»СЏ: СЃРІРµР¶РµРµ РјРµРЅСЋ, РЅРµ С‚РµСЂСЏРµС‚СЃСЏ
    if cmd == "/start" and not is_owner(cfg, user):
        arg = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
        # СЂРµС„РµСЂР°Р»РєР° ref_USERID
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
        # deep-link СЂРѕР·С‹РіСЂС‹С€Р° вЂ” РѕС‚РґР°С‘Рј giveaway_private
        if arg and (arg.startswith("gw_") or arg.startswith("gwref_")):
            return False
        if arg.startswith("ref_"):
            arg = ""  # СѓР¶Рµ СѓС‡Р»Рё
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
    """Р”РѕРєСѓРјРµРЅС‚С‹/С‚Р°СЂРёС„С‹: edit С‚РѕРіРѕ Р¶Рµ СЃРѕРѕР±С‰РµРЅРёСЏ (Р±РµР· СЃРїР°РјР°)."""
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
        _safe_answer_cq(cfg, cq["id"], "РџСЂР°Р№СЃ")
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
        _safe_answer_cq(cfg, cq["id"], "РџРѕРґРґРµСЂР¶РєР°")
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
        _safe_answer_cq(cfg, cq["id"], "Р”РѕРєСѓРјРµРЅС‚С‹")
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
        tg.answer_callback(cfg, cq["id"], "РџРѕРґРґРµСЂР¶РєР°")
        open_t = support.open_ticket_for_user(uid) if uid else None
        _edit(support.support_home_html(), support.support_keyboard(has_open=bool(open_t)))
        return True

    if action == "mine":
        tg.answer_callback(cfg, cq["id"], "РњРѕРё")
        _edit(
            support.user_ticket_list_html(uid),
            support.support_keyboard(
                has_open=bool(support.open_ticket_for_user(uid))
            ),
        )
        return True

    if action == "new":
        tg.answer_callback(cfg, cq["id"], "РќРѕРІС‹Р№ С‚РёРєРµС‚")
        _support_set_await(
            state, uid, {"mode": "new", "ts": int(time.time())}
        )
        _edit(
            "вњ‰пёЏ <b>РќРѕРІС‹Р№ С‚РёРєРµС‚</b>\n\n"
            "РќР°РїРёС€Рё РѕРґРЅРёРј СЃРѕРѕР±С‰РµРЅРёРµРј: С‡С‚Рѕ СЃР»СѓС‡РёР»РѕСЃСЊ, РЅРѕРјРµСЂ Р·Р°РєР°Р·Р° "
            "(РµСЃР»Рё РµСЃС‚СЊ), РєР°Рє СЃ С‚РѕР±РѕР№ СЃРІСЏР·Р°С‚СЊСЃСЏ.\n\n"
            "РћС‚РјРµРЅР°: /cancel",
            {
                "inline_keyboard": [
                    [{"text": "вќЊ РћС‚РјРµРЅР°", "callback_data": "sup:home"}]
                ]
            },
        )
        return True

    if action == "continue":
        open_t = support.open_ticket_for_user(uid)
        if not open_t:
            tg.answer_callback(cfg, cq["id"], "РќРµС‚ РѕС‚РєСЂС‹С‚РѕРіРѕ", show_alert=True)
            return True
        tid = str(open_t.get("id"))
        tg.answer_callback(cfg, cq["id"], "РџРёС€Рё")
        _support_set_await(
            state, uid, {"mode": "write", "ticket_id": tid, "ts": int(time.time())}
        )
        _edit(
            f"вњЌпёЏ Р”РѕРїРёСЃС‹РІР°РµРј РІ С‚РёРєРµС‚ <code>{tid}</code>\n\n"
            "РЎР»РµРґСѓСЋС‰РµРµ СЃРѕРѕР±С‰РµРЅРёРµ СѓР№РґС‘С‚ РІ РѕР±СЂР°С‰РµРЅРёРµ.\n/cancel вЂ” РѕС‚РјРµРЅР°",
            support.ticket_user_keyboard(tid),
        )
        return True

    if action == "write" and arg:
        it = support.get_ticket(arg)
        if not it or int(it.get("user_id") or 0) != uid:
            tg.answer_callback(cfg, cq["id"], "РќРµ РЅР°Р№РґРµРЅ", show_alert=True)
            return True
        if it.get("status") == "closed":
            tg.answer_callback(cfg, cq["id"], "Р—Р°РєСЂС‹С‚", show_alert=True)
            return True
        tg.answer_callback(cfg, cq["id"], "РџРёС€Рё")
        _support_set_await(
            state, uid, {"mode": "write", "ticket_id": arg, "ts": int(time.time())}
        )
        _edit(
            f"вњЌпёЏ РўРёРєРµС‚ <code>{arg}</code> вЂ” Р¶РґСѓ СЃРѕРѕР±С‰РµРЅРёРµ.\n/cancel вЂ” РѕС‚РјРµРЅР°",
            support.ticket_user_keyboard(arg),
        )
        return True

    if action == "uclose" and arg:
        it = support.get_ticket(arg)
        if not it or int(it.get("user_id") or 0) != uid:
            tg.answer_callback(cfg, cq["id"], "РќРµ РЅР°Р№РґРµРЅ", show_alert=True)
            return True
        support.close_ticket(arg)
        _support_set_await(state, uid, None)
        tg.answer_callback(cfg, cq["id"], "Р—Р°РєСЂС‹С‚")
        _edit(
            f"вњ… РўРёРєРµС‚ <code>{arg}</code> Р·Р°РєСЂС‹С‚.\nРЎРїР°СЃРёР±Рѕ!",
            support.support_keyboard(has_open=False),
        )
        notify_owner(
            cfg,
            f"вљ« РљР»РёРµРЅС‚ Р·Р°РєСЂС‹Р» С‚РёРєРµС‚ <code>{arg}</code> (@{uname})",
            reply_markup=support.ticket_staff_keyboard(arg),
        )
        return True

    # --- staff ---
    if action == "reply" and arg and owner:
        it = support.get_ticket(arg)
        if not it:
            tg.answer_callback(cfg, cq["id"], "РќРµС‚", show_alert=True)
            return True
        tg.answer_callback(cfg, cq["id"], "РћС‚РІРµС‚")
        _support_set_await(
            state, uid, {"mode": "staff_reply", "ticket_id": arg, "ts": int(time.time())}
        )
        tg.send_message(
            cfg,
            chat_id,
            f"рџ’¬ РћС‚РІРµС‚ РІ С‚РёРєРµС‚ <code>{arg}</code>\n"
            f"РљР»РёРµРЅС‚: @{it.get('username') or it.get('user_id')}\n\n"
            "РќР°РїРёС€Рё С‚РµРєСЃС‚ РѕС‚РІРµС‚Р° РѕРґРЅРёРј СЃРѕРѕР±С‰РµРЅРёРµРј.\n/cancel вЂ” РѕС‚РјРµРЅР°",
            parse_mode="HTML",
        )
        return True

    if action == "close" and arg and owner:
        it = support.close_ticket(arg)
        if not it:
            tg.answer_callback(cfg, cq["id"], "РќРµС‚", show_alert=True)
            return True
        tg.answer_callback(cfg, cq["id"], "Р—Р°РєСЂС‹С‚")
        try:
            tg.send_message(
                cfg,
                int(it["user_id"]),
                f"вњ… РўРёРєРµС‚ <code>{arg}</code> Р·Р°РєСЂС‹С‚ РїРѕРґРґРµСЂР¶РєРѕР№.\n"
                "РќРѕРІС‹Р№ РІРѕРїСЂРѕСЃ вЂ” /support",
                parse_mode="HTML",
                reply_markup=support.support_keyboard(has_open=False),
            )
        except Exception as e:
            print("ticket close dm", e)
        tg.send_message(
            cfg,
            chat_id,
            f"вљ« РўРёРєРµС‚ <code>{arg}</code> Р·Р°РєСЂС‹С‚.",
            parse_mode="HTML",
        )
        return True

    if action == "stafflist" and owner:
        tg.answer_callback(cfg, cq["id"], "РЎРїРёСЃРѕРє")
        tg.send_message(
            cfg,
            chat_id,
            support.staff_list_html(),
            parse_mode="HTML",
            reply_markup={
                "inline_keyboard": [
                    [{"text": "рџ“‹ РћР±РЅРѕРІРёС‚СЊ", "callback_data": "sup:stafflist"}]
                ]
            },
        )
        return True

    tg.answer_callback(cfg, cq["id"], "ok")
    return True


def handle_support_private(cfg: dict, state: dict, msg: dict) -> bool:
    """
    РћР¶РёРґР°РЅРёРµ С‚РµРєСЃС‚Р° С‚РёРєРµС‚Р° / РѕС‚РІРµС‚ staff / Р°РІС‚Рѕ-РґРѕРїРёСЃСЊ РІ РѕС‚РєСЂС‹С‚С‹Р№ С‚РёРєРµС‚.
    True = СЃРѕРѕР±С‰РµРЅРёРµ СЃСЉРµР»Рё.
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

    if cmd in ("/cancel", "/РѕС‚РјРµРЅР°"):
        if str(uid) in (state.get("support_await") or {}):
            _support_set_await(state, uid, None)
            tg.send_message(
                cfg,
                chat_id,
                "РћРє, РѕС‚РјРµРЅРёР».",
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
            tg.send_message(cfg, chat_id, "Р¤РѕСЂРјР°С‚: /treply РљРћР” С‚РµРєСЃС‚ РѕС‚РІРµС‚Р°")
            return True
        tid, body = parts[1], parts[2]
        it = support.add_message(tid, from_role="staff", text=body)
        if not it:
            tg.send_message(cfg, chat_id, "РўРёРєРµС‚ РЅРµ РЅР°Р№РґРµРЅ РёР»Рё Р·Р°РєСЂС‹С‚")
            return True
        try:
            tg.send_message(
                cfg,
                int(it["user_id"]),
                f"рџ’¬ <b>РћС‚РІРµС‚ РїРѕРґРґРµСЂР¶РєРё</b> В· С‚РёРєРµС‚ <code>{tid}</code>\n\n"
                f"{html.escape(body)}\n\n"
                "РњРѕР¶РµС€СЊ РґРѕРїРёСЃР°С‚СЊ РІ СЌС‚РѕС‚ С‚РёРєРµС‚ вЂ” РїСЂРѕСЃС‚Рѕ РЅР°РїРёС€Рё СЃСЋРґР°.",
                parse_mode="HTML",
                reply_markup=support.ticket_user_keyboard(tid),
            )
        except Exception as e:
            tg.send_message(cfg, chat_id, f"РљР»РёРµРЅС‚Сѓ РЅРµ СѓС€Р»Рѕ: {e}")
            return True
        tg.send_message(cfg, chat_id, f"вњ… РћС‚РІРµС‚ РІ <code>{tid}</code> РѕС‚РїСЂР°РІР»РµРЅ")
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
            tg.send_message(cfg, chat_id, "РќРµ СѓРґР°Р»РѕСЃСЊ (Р·Р°РєСЂС‹С‚?)")
            return True
        try:
            tg.send_message(
                cfg,
                int(it["user_id"]),
                f"рџ’¬ <b>РћС‚РІРµС‚ РїРѕРґРґРµСЂР¶РєРё</b> В· <code>{tid}</code>\n\n"
                f"{html.escape(text)}\n\n"
                "Р”РѕРїРёСЃР°С‚СЊ вЂ” РїСЂРѕСЃС‚Рѕ РЅР°РїРёС€Рё СЃРѕРѕР±С‰РµРЅРёРµ.",
                parse_mode="HTML",
                reply_markup=support.ticket_user_keyboard(tid),
            )
        except Exception as e:
            tg.send_message(cfg, chat_id, f"DM fail: {e}")
            return True
        tg.send_message(
            cfg,
            chat_id,
            f"вњ… РЈС€Р»Рѕ РІ С‚РёРєРµС‚ <code>{tid}</code>",
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
                tg.send_message(cfg, chat_id, "РќСѓР¶РµРЅ С‚РµРєСЃС‚. РР»Рё /cancel")
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
                f"вњ… <b>РўРёРєРµС‚ СЃРѕР·РґР°РЅ</b> <code>{tid}</code>\n\n"
                f"РњС‹ РѕС‚РІРµС‚РёРј СЃСЋРґР°. РџРѕРєР° РѕС‚РєСЂС‹С‚ вЂ” РјРѕР¶РЅРѕ РїСЂРѕСЃС‚Рѕ РїРёСЃР°С‚СЊ РґР°Р»СЊС€Рµ.\n\n"
                f"<i>{html.escape(text[:200])}</i>",
                parse_mode="HTML",
                reply_markup=support.ticket_user_keyboard(tid),
            )
            notify_owner(
                cfg,
                f"рџ† <b>РќРѕРІС‹Р№ С‚РёРєРµС‚</b> <code>{tid}</code>\n"
                f"РћС‚: {html.escape(name)} (@{html.escape(uname)}) "
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
            tg.send_message(cfg, chat_id, "РўРёРєРµС‚ Р·Р°РєСЂС‹С‚. /support вЂ” РЅРѕРІС‹Р№.")
            return True
        tg.send_message(
            cfg,
            chat_id,
            f"вњ… Р”РѕР±Р°РІР»РµРЅРѕ РІ <code>{tid}</code>",
            parse_mode="HTML",
            reply_markup=support.ticket_user_keyboard(tid),
        )
        notify_owner(
            cfg,
            f"рџ’¬ РўРёРєРµС‚ <code>{tid}</code> В· РґРѕРїРёСЃСЊ РѕС‚ @{html.escape(uname)}\n\n"
            f"{html.escape(text[:1500])}",
            reply_markup=support.ticket_staff_keyboard(tid),
        )
        return True

    # auto: open ticket + plain text (not command) в†’ append
    if (
        text
        and not text.startswith("/")
        and not is_owner(cfg, user)
        and terms.is_accepted(uid)
    ):
        # РЅРµ РїРµСЂРµС…РІР°С‚С‹РІР°С‚СЊ РµСЃР»Рё РёРґС‘С‚ Р·Р°РєР°Р·/Р±Р°Р»Р°РЅСЃ await
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
                    f"вњ… Р’ С‚РёРєРµС‚ <code>{tid}</code>\n"
                    "Р•СЃР»Рё СЌС‚Рѕ РЅРѕРІС‹Р№ РІРѕРїСЂРѕСЃ вЂ” /support в†’ В«РќРѕРІС‹Р№ С‚РёРєРµС‚В»",
                    parse_mode="HTML",
                    reply_markup=support.ticket_user_keyboard(tid),
                )
                notify_owner(
                    cfg,
                    f"рџ’¬ РўРёРєРµС‚ <code>{tid}</code> В· @{html.escape(uname)}\n\n"
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
        # РѕРґРёРЅ СЌРєСЂР°РЅ, Р±РµР· СЃРїР°РјР° 4 СЃРѕРѕР±С‰РµРЅРёСЏРјРё
        _safe_answer_cq(cfg, cq["id"], "РџРѕР»РЅС‹Р№ С‚РµРєСЃС‚")
        if chat_id:
            body = (
                "рџ“њ <b>РџРѕР»РЅС‹Рµ СѓСЃР»РѕРІРёСЏ</b>\n\n"
                "Р§РёС‚Р°Р№ РїРѕ СЃСЃС‹Р»РєР°Рј (РІСЃРµРіРґР° РґРѕСЃС‚СѓРїРЅС‹):\n"
                f'вЂў <a href="{terms.PRIVACY_URL}">РџРѕР»РёС‚РёРєР° РєРѕРЅС„РёРґРµРЅС†РёР°Р»СЊРЅРѕСЃС‚Рё</a>\n'
                f'вЂў <a href="{terms.AGREEMENT_URL}">РџРѕР»СЊР·РѕРІР°С‚РµР»СЊСЃРєРѕРµ СЃРѕРіР»Р°С€РµРЅРёРµ</a>\n'
                "вЂў РџСЂР°Р№СЃ вЂ” РєРЅРѕРїРєР° РЅРёР¶Рµ\n\n"
                f"РљСЂР°С‚РєРѕ РІ Р±РѕС‚Рµ: РіР°СЂР°РЅС‚РёСЏ {terms.GUARANTEE_DAYS} СЃСѓС‚., "
                f"РїСЂР°РІРєРё {terms.REWORK_DAYS} СЃСѓС‚., free РЅРµС‚, С…РѕСЃС‚РёРЅРі РЅРµ РІС…РѕРґРёС‚.\n"
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
        tg.answer_callback(cfg, cq["id"], "РљСЂР°С‚РєРѕ")
        if chat_id:
            send_terms_gate(
                cfg, chat_id, state=state, uid=uid, message_id=mid, full=False
            )
        return True

    if action == "ok":
        tg.answer_callback(cfg, cq["id"], "РЈР¶Рµ РїСЂРёРЅСЏС‚Рѕ")
        return True

    if action == "yes":
        terms.accept(uid, username=uname, name=name)
        tg.answer_callback(cfg, cq["id"], "РџСЂРёРЅСЏС‚Рѕ!")
        if chat_id:
            ui_edit_or_send(
                cfg,
                chat_id,
                f"вњ… РЈСЃР»РѕРІРёСЏ РїСЂРёРЅСЏС‚С‹ В· РіР°СЂР°РЅС‚РёСЏ {terms.GUARANTEE_DAYS} СЃСѓС‚.\n\n"
                + terms.user_home_html(uid),
                reply_markup=terms.after_accept_keyboard(uid),
                message_id=mid,
                state=state,
                uid=uid,
                store_key="terms_ui_msg",
            )
            # pending deep-link (СЂРѕР·С‹РіСЂС‹С€) вЂ” СЃСЂР°Р·Сѓ РІ РєРІРµСЃС‚, Р±РµР· В«РёРґРё Р¶РјРё РµС‰С‘ СЂР°Р·В»
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
                            "РЈСЃР»РѕРІРёСЏ РїСЂРёРЅСЏС‚С‹. Р•С‰С‘ СЂР°Р· РЅР°Р¶РјРё В«РЈС‡Р°СЃС‚РІРѕРІР°С‚СЊВ» РІ РїРѕСЃС‚Рµ СЂРѕР·С‹РіСЂС‹С€Р°.",
                            parse_mode=None,
                        )
                else:
                    tg.send_message(
                        cfg,
                        chat_id,
                        "РњРѕР¶РЅРѕ РїСЂРѕРґРѕР»Р¶Р°С‚СЊ: /start В· /order В· /support",
                        parse_mode=None,
                    )
        return True

    if action == "no":
        terms.decline(uid, username=uname)
        tg.answer_callback(cfg, cq["id"], "РћРє")
        if chat_id:
            ui_edit_or_send(
                cfg,
                chat_id,
                "вќЊ <b>Р‘РµР· РїСЂРёРЅСЏС‚РёСЏ СѓСЃР»РѕРІРёР№ Р±РѕС‚ РЅРµРґРѕСЃС‚СѓРїРµРЅ</b>\n\n"
                "Р—Р°РєР°Р·С‹, Р±Р°Р»Р°РЅСЃ Рё СЃРµСЂРІРёСЃС‹ Р·Р°РєСЂС‹С‚С‹.\n"
                "Р•СЃР»Рё РїРµСЂРµРґСѓРјР°РµС€СЊ вЂ” /terms РёР»Рё /start.",
                reply_markup={
                    "inline_keyboard": [
                        [{"text": "рџ“њ Р•С‰С‘ СЂР°Р· СѓСЃР»РѕРІРёСЏ", "callback_data": "terms:short"}],
                        [{"text": "вњ… Р’СЃС‘-С‚Р°РєРё РїСЂРёРЅРёРјР°СЋ", "callback_data": "terms:yes"}],
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
    True = РѕР±СЂР°Р±РѕС‚РєСѓ РЅСѓР¶РЅРѕ РЎРўРћРџРќРЈРўР¬ (РїРѕРєР°Р·Р°Р»Рё gate).
    Р’Р»Р°РґРµР»РµС† РІСЃРµРіРґР° РїСЂРѕС…РѕРґРёС‚.
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
    # СЃРєСЂРёРЅ/С„РѕС‚Рѕ РґР»СЏ СЂРѕР·С‹РіСЂС‹С€Р° вЂ” РµСЃР»Рё СѓР¶Рµ РЅР°Р¶Р°Р» В«РЈС‡Р°СЃС‚РІРѕРІР°С‚СЊВ», РЅРµ СЂРµР¶РµРј terms
    if msg.get("photo") or (
        (msg.get("document") or {}).get("mime_type") or ""
    ).startswith("image/"):
        try:
            act = gw.get_active(state)
            if act and act.get("status") == "active" and gw.get_entry(act, uid):
                return False
        except Exception:
            pass
    # deep-link СЂРѕР·С‹РіСЂС‹С€Р° вЂ” handle_giveaway / terms pending
    text_raw = (msg.get("text") or "").strip()
    if text_raw.startswith("/start") and (
        " gw_" in f" {text_raw}" or " gwref_" in f" {text_raw}"
    ):
        return False
    # РєРѕРјР°РЅРґС‹ terms/start РѕР±СЂР°Р±Р°С‚С‹РІР°РµС‚ handle_terms_private
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
        "/РїСЂР°РІРёР»Р°",
        "/РїРѕР»РёС‚РёРєР°",
        "/СѓСЃР»РѕРІРёСЏ",
        "/РѕС„РµСЂС‚Р°",
        "/СЃРѕРіР»Р°С€РµРЅРёРµ",
        "/С‚Р°СЂРёС„С‹",
        "/С†РµРЅС‹",
        "/РїРѕРґРґРµСЂР¶РєР°",
        "/РєРѕРЅС‚Р°РєС‚",
        "/РґРѕРєСѓРјРµРЅС‚С‹",
        "/РєРѕРЅС„РёРґРµРЅС†РёР°Р»СЊРЅРѕСЃС‚СЊ",
        "/С‚РёРєРµС‚С‹",
        "/РѕР±СЂР°С‰РµРЅРёСЏ",
        "/РјРµРЅСЋ",
        "/РѕС‚РјРµРЅР°",
        "/start",
        "/help",
    ):
        return False
    send_terms_gate(cfg, chat.get("id"), state=state, uid=uid, full=False)
    return True


def require_not_blocked(cfg: dict, msg: dict) -> bool:
    """True = СЃС‚РѕРї (РїРѕРєР°Р·Р°Р»Рё Р±Р»РѕРє). Р’Р»Р°РґРµР»РµС† РїСЂРѕС…РѕРґРёС‚. Р”РѕРєСѓРјРµРЅС‚С‹ вЂ” РІСЃРµРіРґР°."""
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
    # СЋСЂ. РґРѕРєСѓРјРµРЅС‚С‹ Рё РїСЂР°Р№СЃ вЂ” РЅРµ СЂРµР¶РµРј (Platega / Р±Р°РЅРє / РїСЂРѕР·СЂР°С‡РЅРѕСЃС‚СЊ)
    if cmd in (
        "/legal",
        "/docs",
        "/РґРѕРєСѓРјРµРЅС‚С‹",
        "/privacy",
        "/РєРѕРЅС„РёРґРµРЅС†РёР°Р»СЊРЅРѕСЃС‚СЊ",
        "/agreement",
        "/offer",
        "/СЃРѕРіР»Р°С€РµРЅРёРµ",
        "/РѕС„РµСЂС‚Р°_РґРѕРє",
        "/public_offer",
        "/РїСѓР±Р»РёС‡РЅР°СЏ_РѕС„РµСЂС‚Р°",
        "/prices",
        "/pricing",
        "/tariffs",
        "/С‚Р°СЂРёС„С‹",
        "/С†РµРЅС‹",
        "/terms",
        "/rules",
        "/policy",
        "/РїСЂР°РІРёР»Р°",
        "/РїРѕР»РёС‚РёРєР°",
        "/СѓСЃР»РѕРІРёСЏ",
        "/РѕС„РµСЂС‚Р°",
    ):
        return False
    if cmd in ("/start", "/help", "/support", "/РїРѕРјРѕС‰СЊ"):
        tg.send_message(
            cfg,
            chat.get("id"),
            mod.blocked_user_message(),
            parse_mode="HTML",
            disable_preview=False,
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "рџ”’ РџРѕР»РёС‚РёРєР°", "url": terms.PRIVACY_URL},
                        {"text": "рџ“њ РћС„РµСЂС‚Р°", "url": terms.AGREEMENT_URL},
                    ],
                    [{"text": "рџ“‹ Р”РѕРєСѓРјРµРЅС‚С‹", "callback_data": "legal:hub"}],
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
                    {"text": "рџ”’ РџРѕР»РёС‚РёРєР°", "url": terms.PRIVACY_URL},
                    {"text": "рџ“њ РћС„РµСЂС‚Р°", "url": terms.AGREEMENT_URL},
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
    True = Р·Р°Р±Р»РѕРєРёСЂРѕРІР°Р»Рё (РґР°Р»СЊС€Рµ РЅРµ РїСЂРѕРґРѕР»Р¶Р°С‚СЊ Р·Р°РєР°Р·).
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
    # СЃР±СЂРѕСЃ С‡РµСЂРЅРѕРІРёРєР°
    try:
        state.setdefault("order_draft", {}).pop(str(uid), None)
        save_state(state)
    except Exception:
        pass
    tg.send_message(
        cfg,
        chat_id,
        "рџљ« <b>Р—Р°РєР°Р· РѕС‚РєР»РѕРЅС‘РЅ В· Р°РєРєР°СѓРЅС‚ Р·Р°Р±Р»РѕРєРёСЂРѕРІР°РЅ</b>\n\n"
        f"{html.escape(reason)}\n\n"
        "РњС‹ РЅРµ РїСЂРёРЅРёРјР°РµРј РЅРµР·Р°РєРѕРЅРЅС‹Рµ Рё РјРѕС€РµРЅРЅРёС‡РµСЃРєРёРµ Р·Р°РґР°С‡Рё.\n"
        "Р•СЃР»Рё СЃСЂР°Р±РѕС‚Р°Р»Р° РѕС€РёР±РєР° вЂ” РЅР°РїРёС€Рё РІР»Р°РґРµР»СЊС†Сѓ: СЃРЅСЏС‚СЊ Р±Р»РѕРє РјРѕР¶РµС‚ С‚РѕР»СЊРєРѕ РѕРЅ.\n\n"
        + mod.blocked_user_message(),
        parse_mode="HTML",
    )
    un = f"@{html.escape(uname)}" if uname else html.escape(name or str(uid))
    notify_owner(
        cfg,
        "рџљЁ <b>РђРІС‚РѕР±Р»РѕРє В· РЅРµР·Р°РєРѕРЅРЅРѕРµ РўР—</b>\n\n"
        f"user {un} В· <code>{uid}</code>\n"
        f"{html.escape(reason)}\n"
        f"hits: <code>{html.escape(', '.join(hits[:6]))}</code>\n\n"
        f"<b>РўР—:</b>\n{html.escape(brief[:900])}",
        reply_markup=mod.owner_block_keyboard(uid),
    )
    return True


def handle_mod_callback(cfg: dict, state: dict, cq: dict) -> bool:
    data = cq.get("data") or ""
    if not data.startswith("mod:"):
        return False
    user = cq.get("from") or {}
    if not is_owner(cfg, user):
        tg.answer_callback(cfg, cq["id"], "РўРѕР»СЊРєРѕ РІР»Р°РґРµР»РµС†", show_alert=True)
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
            tg.answer_callback(cfg, cq["id"], "РќРµ Р±С‹Р» РІ Р±Р»РѕРєРµ", show_alert=True)
            return True
        tg.answer_callback(cfg, cq["id"], "Р Р°Р·Р±Р»РѕРєРёСЂРѕРІР°РЅ")
        if chat_id and mid:
            try:
                tg.edit_message_text(
                    cfg,
                    chat_id,
                    mid,
                    f"вњ… Р Р°Р·Р±Р»РѕРєРёСЂРѕРІР°РЅ <code>{target}</code>",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        try:
            tg.send_message(
                cfg,
                target,
                "вњ… <b>Р”РѕСЃС‚СѓРї РІРѕСЃСЃС‚Р°РЅРѕРІР»РµРЅ РІР»Р°РґРµР»СЊС†РµРј</b>\n\n"
                "РњРѕР¶РЅРѕ СЃРЅРѕРІР° РїРѕР»СЊР·РѕРІР°С‚СЊСЃСЏ Р±РѕС‚РѕРј.\n"
                "РџРѕРјРЅРё: РЅРµР·Р°РєРѕРЅРЅС‹Рµ Р·Р°РґР°С‡Рё = РїРѕРІС‚РѕСЂРЅС‹Р№ Р±Р»РѕРє.\n"
                "/terms В· /order",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return True

    tg.answer_callback(cfg, cq["id"], "ok")
    return True


def handle_mod_owner_commands(cfg: dict, state: dict, msg: dict) -> bool:
    """РљРѕРјР°РЅРґС‹ РІР»Р°РґРµР»СЊС†Р°: /block /unblock /blocks"""
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

    if cmd in ("/blocks", "/blocked", "/Р±Р°РЅС‹"):
        items = mod.list_blocked(25)
        if not items:
            tg.send_message(cfg, chat_id, "Р—Р°Р±Р»РѕРєРёСЂРѕРІР°РЅРЅС‹С… РЅРµС‚.")
            return True
        lines = ["рџљ« <b>Р‘Р»РѕРєРё</b>\n"]
        for b in items:
            un = b.get("username")
            who = f"@{un}" if un else b.get("name") or b.get("user_id")
            lines.append(
                f"вЂў <code>{b.get('user_id')}</code> {html.escape(str(who))}\n"
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

    if cmd in ("/unblock", "/СЂР°Р·Р±Р»РѕРє", "/unban"):
        if not arg:
            tg.send_message(cfg, chat_id, "РџСЂРёРјРµСЂ: /unblock 123456789")
            return True
        try:
            target = int(arg.split()[0])
        except ValueError:
            tg.send_message(cfg, chat_id, "user_id С‡РёСЃР»РѕРј")
            return True
        ent = mod.unblock_user(target, by=f"owner:{user.get('id')}")
        if not ent:
            tg.send_message(cfg, chat_id, "РќРµ РЅР°Р№РґРµРЅ РІ Р±Р»РѕРєРµ (РёР»Рё СѓР¶Рµ СЃРЅСЏС‚)")
            return True
        tg.send_message(
            cfg, chat_id, f"вњ… Р Р°Р·Р±Р»РѕРєРёСЂРѕРІР°РЅ <code>{target}</code>", parse_mode="HTML"
        )
        try:
            tg.send_message(
                cfg,
                target,
                "вњ… Р”РѕСЃС‚СѓРї РІРѕСЃСЃС‚Р°РЅРѕРІР»РµРЅ РІР»Р°РґРµР»СЊС†РµРј.\n/order В· /terms",
                parse_mode=None,
            )
        except Exception:
            pass
        return True

    if cmd in ("/block", "/Р±Р°РЅ"):
        parts = arg.split(maxsplit=1)
        if not parts:
            tg.send_message(
                cfg, chat_id, "РџСЂРёРјРµСЂ: /block 123456789 СЃРїР°Рј"
            )
            return True
        try:
            target = int(parts[0])
        except ValueError:
            tg.send_message(cfg, chat_id, "user_id С‡РёСЃР»РѕРј")
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
            f"рџљ« Р—Р°Р±Р»РѕРєРёСЂРѕРІР°РЅ <code>{target}</code>\n{html.escape(reason)}",
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
    """Р‘Р°Р»Р°РЅСЃ + РїРѕРїРѕР»РЅРµРЅРёРµ РЎР‘Рџ (РІСЃРµРј)."""
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
                f"РЎСѓРјРјР° РѕС‚ {bal.TOPUP_MIN} РґРѕ {bal.TOPUP_MAX} в‚Ѕ. Р•С‰С‘ СЂР°Р· С‡РёСЃР»РѕРј:",
                parse_mode=None,
            )
            return True
        await_b.pop(str(uid), None)
        save_state(state)
        return _start_topup_flow(
            cfg, state, chat_id, uid, amount, uname=uname, name=name, message_id=None
        )

    if cmd in ("/balance", "/bal", "/Р±Р°Р»Р°РЅСЃ", "/РєРѕС€РµР»С‘Рє", "/РєРѕС€РµР»РµРє"):
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

    if cmd in ("/topup", "/РїРѕРїРѕР»РЅРёС‚СЊ", "/sbp", "/СЃР±Рї"):
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
            "рџ’і <b>РџРѕРїРѕР»РЅРµРЅРёРµ С‡РµСЂРµР· РЎР‘Рџ</b>\n\n"
            f"Р‘Р°Р»Р°РЅСЃ: <b>{bal.get_balance(uid)}</b> в‚Ѕ\n"
            f"РЎСѓРјРјР° РѕС‚ {bal.TOPUP_MIN} РґРѕ {bal.TOPUP_MAX} в‚Ѕ.\n\n"
            "Р’С‹Р±РµСЂРё СЃСѓРјРјСѓ РёР»Рё /topup 500",
            reply_markup=bal.topup_amounts_keyboard(),
            state=state,
            uid=uid,
            store_key="bal_ui_msg",
        )
        return True

    # owner: pending topups / manual credit
    if owner and cmd in ("/balpend", "/sbppend", "/РїРѕРїРѕР»РЅРµРЅРёСЏ"):
        pending = bal.list_pending_topups(20)
        if not pending:
            tg.send_message(cfg, chat_id, "РќРµС‚ РѕС‚РєСЂС‹С‚С‹С… Р·Р°СЏРІРѕРє РЅР° РїРѕРїРѕР»РЅРµРЅРёРµ.")
            return True
        lines = [
            "рџ’і <b>Р—Р°СЏРІРєРё РЎР‘Рџ</b>\n"
            "<i>Р’ Р±Р°РЅРєРµ РёС‰Рё СЃСѓРјРјСѓ pay_exact в†’ Р—Р°С‡РёСЃР»РёС‚СЊ</i>\n"
        ]
        for t in pending:
            un = t.get("username")
            who = f"@{un}" if un else t.get("name")
            pay = t.get("pay_exact") or t.get("amount")
            lines.append(
                f"вЂў <code>{t.get('id')}</code> В· <b>{pay}</b> в‚Ѕ В· "
                f"{bal.topup_status_label(str(t.get('status')))}\n"
                f"  {html.escape(str(who))} В· РєРѕРґ <code>{t.get('code')}</code>"
            )
        tg.send_message(cfg, chat_id, "\n".join(lines), parse_mode="HTML")
        for t in pending[:5]:
            pay = t.get("pay_exact") or t.get("amount")
            tg.send_message(
                cfg,
                chat_id,
                f"Р—Р°СЏРІРєР° <code>{html.escape(str(t.get('id')))}</code>\n"
                f"РС‰Рё РІ Р±Р°РЅРєРµ: <b>{pay}</b> в‚Ѕ В· РєРѕРґ "
                f"<code>{html.escape(str(t.get('code')))}</code>",
                parse_mode="HTML",
                reply_markup=bal.topup_owner_keyboard(str(t["id"])),
            )
        return True

    if owner and cmd in ("/baladd", "/balset"):
        # /baladd USER_ID|@username 500 [РєРѕРјРјРµРЅС‚]
        parts = arg.split()
        if len(parts) < 2:
            tg.send_message(
                cfg,
                chat_id,
                "РџСЂРёРјРµСЂ:\n<code>/baladd 123456789 500</code>\n"
                "<code>/baladd @Ibramosta 10000</code>\n"
                "<code>/balset 123456789 0</code> вЂ” РІС‹СЃС‚Р°РІРёС‚СЊ Р±Р°Р»Р°РЅСЃ\n\n"
                "Р‘Р°Р»Р°РЅСЃ РѕР±С‰РёР№: РЅР° Bothost РёРґС‘С‚ С‡РµСЂРµР· РјРѕСЃС‚ РЅР° РџРљ.",
                parse_mode="HTML",
            )
            return True
        target_raw = parts[0].strip()
        try:
            amount = int(parts[1])
        except ValueError:
            tg.send_message(cfg, chat_id, "РЎСѓРјРјР° вЂ” С‡РёСЃР»РѕРј")
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
                "РќРµ РЅР°С€С‘Р» user_id. РЈРєР°Р¶Рё С‡РёСЃР»РѕРј РёР»Рё @username РёР· СЂРѕР·С‹РіСЂС‹С€Р°/РєРѕС€РµР»СЊРєР°.",
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
                tg.send_message(cfg, chat_id, "РЎСѓРјРјР° > 0")
                return True
            new_b = bal.credit(target, amount, kind="owner_add", note=note)
        tg.send_message(
            cfg,
            chat_id,
            f"вњ… Р‘Р°Р»Р°РЅСЃ <code>{target}</code> в†’ <b>{new_b}</b> в‚Ѕ",
            parse_mode="HTML",
        )
        try:
            tg.send_message(
                cfg,
                target,
                f"рџ’° Р‘Р°Р»Р°РЅСЃ РїРѕРїРѕР»РЅРµРЅ РІР»Р°РґРµР»СЊС†РµРј: СЃРµР№С‡Р°СЃ <b>{new_b}</b> в‚Ѕ\n/balance",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return True

    if owner and cmd in ("/treasury", "/РєР°СЃСЃР°", "/cash"):
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
            return False, "РќСѓР¶РЅРѕ С„РѕС‚Рѕ"
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

    # С„РѕС‚Рѕ СЃ РїРѕРґРїРёСЃСЊСЋ /setqr вЂ” СЃСЂР°Р·Сѓ
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
                    f"вњ… QR СЃРѕС…СЂР°РЅС‘РЅ: <code>{html.escape(info)}</code>\n"
                    "РђРІС‚РѕР·Р°С‡РёСЃР»РµРЅРёРµ РЅРѕС‡СЊСЋ: <b>РІРєР»</b>.\nРўРµСЃС‚: /topup 100",
                    parse_mode="HTML",
                )
            else:
                tg.send_message(cfg, chat_id, f"вќЊ {html.escape(info)}")
            return True

    if owner and cmd in ("/setqr", "/sbp_qr", "/qr"):
        state["await_sbp_qr"] = True
        save_state(state)
        tg.send_message(
            cfg,
            chat_id,
            "рџ“· РџСЂРёС€Р»Рё <b>С„РѕС‚Рѕ QR</b> РґР»СЏ РЎР‘Рџ РѕРґРЅРёРј СЃРѕРѕР±С‰РµРЅРёРµРј.\n"
            "РР»Рё: С„РѕС‚Рѕ СЃ РїРѕРґРїРёСЃСЊСЋ <code>/setqr</code>\n"
            "РЎРѕС…СЂР°РЅСЋ в†’ РєР»РёРµРЅС‚С‹ РїРѕР»СѓС‡Р°С‚ РїСЂРё /topup.\n"
            "РћС‚РјРµРЅР°: /cancel_qr",
            parse_mode="HTML",
        )
        return True

    if owner and cmd in ("/cancel_qr",):
        state.pop("await_sbp_qr", None)
        save_state(state)
        tg.send_message(cfg, chat_id, "РћРє, Р·Р°РіСЂСѓР·РєСѓ QR РѕС‚РјРµРЅРёР».")
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
                        f"вњ… QR СЃРѕС…СЂР°РЅС‘РЅ: <code>{html.escape(info)}</code>\n"
                        "РљР»РёРµРЅС‚Р°Рј РїСЂРё /topup СѓР№РґС‘С‚ РєР°СЂС‚РёРЅРєР°.\n"
                        "вљЎ РђРІС‚РѕР·Р°С‡РёСЃР»РµРЅРёРµ 24/7: <b>РІРєР»</b> "
                        "(В«РЇ РѕРїР»Р°С‚РёР»В» в†’ СЃСЂР°Р·Сѓ Р±Р°Р»Р°РЅСЃ, СѓС‚СЂРѕРј СЃРІРµСЂРёС€СЊ Р±Р°РЅРє).\n"
                        "РўРµСЃС‚: /topup 100",
                        parse_mode="HTML",
                    )
                else:
                    tg.send_message(cfg, chat_id, f"вќЊ {html.escape(info)}")
            except Exception as e:
                tg.send_message(
                    cfg, chat_id, f"вќЊ РќРµ СЃРѕС…СЂР°РЅРёР» QR: {html.escape(str(e)[:200])}"
                )
            return True
        if text and not text.startswith("/"):
            tg.send_message(cfg, chat_id, "РќСѓР¶РЅРѕ <b>С„РѕС‚Рѕ</b> QR, РЅРµ С‚РµРєСЃС‚.", parse_mode="HTML")
            return True

    if owner and cmd in ("/cashout", "/РІС‹РІРѕРґ", "/withdraw"):
        # /cashout 5000 [Р·Р°РјРµС‚РєР°] вЂ” СѓС‡С‘С‚ РІС‹РІРѕРґР° (РґРµРЅСЊРіРё СѓР¶Рµ РЅР° С‚РІРѕРµР№ РєР°СЂС‚Рµ)
        parts = arg.split(maxsplit=1)
        if not parts:
            tg.send_message(
                cfg,
                chat_id,
                "РЈС‡С‘С‚ РІС‹РІРѕРґР° (РґРµРЅСЊРіРё СЃ РЎР‘Рџ СѓР¶Рµ Сѓ С‚РµР±СЏ РІ Р±Р°РЅРєРµ):\n"
                "<code>/cashout 5000</code>\n"
                "<code>/cashout 5000 РЅР° РєР°СЂС‚Сѓ</code>\n\n"
                "РљР°СЃСЃР°: /treasury",
                parse_mode="HTML",
            )
            return True
        try:
            amount = int("".join(c for c in parts[0] if c.isdigit()) or "0")
        except ValueError:
            amount = 0
        note = parts[1].strip() if len(parts) > 1 else "РІС‹РІРѕРґ"
        try:
            entry = bal.owner_cashout(amount, note=note)
        except ValueError as e:
            tg.send_message(cfg, chat_id, str(e))
            return True
        tg.send_message(
            cfg,
            chat_id,
            f"рџ“¤ Р—Р°РїРёСЃР°Р» РІС‹РІРѕРґ <b>{entry.get('amount')}</b> в‚Ѕ\n"
            f"Р’СЃРµРіРѕ РІС‹РІРµРґРµРЅРѕ (СѓС‡С‘С‚): <b>{entry.get('withdrawn_total')}</b> в‚Ѕ\n\n"
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
    try:
        top = bal.create_topup(
            user_id=uid, amount=amount, username=uname, name=name
        )
    except ValueError as e:
        tg.send_message(cfg, chat_id, f"вљ пёЏ {html.escape(str(e))}", parse_mode="HTML")
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
    # QR (РЅРѕРјРµСЂ РЅРµ СЃРІРµС‚РёРј)
    pay = int(top.get("pay_exact") or top.get("amount") or amount)
    qp = bal.qr_path(cfg)
    if qp:
        try:
            tg.send_photo(
                cfg,
                chat_id,
                str(qp),
                caption=(
                    f"QR РЎР‘Рџ\n"
                    f"РЎСѓРјРјР°: <b>{pay}</b> в‚Ѕ (СЂРѕРІРЅРѕ)\n"
                    f"РљРѕРґ: <code>{html.escape(str(top.get('code')))}</code>"
                ),
            )
        except Exception as e:
            print("sbp qr send", e, flush=True)
    s = bal.sbp_cfg(cfg)
    if not s.get("qr_ok"):
        notify_owner(
            cfg,
            "вљ пёЏ РљР»РёРµРЅС‚ /topup, РЅРѕ QR РµС‰С‘ РЅРµ Р·Р°РіСЂСѓР¶РµРЅ.\n"
            "РџСЂРёС€Р»Рё Р±РѕС‚Сѓ: /setqr в†’ С„РѕС‚Рѕ QR.\n"
            f"user <code>{uid}</code> В· {pay} в‚Ѕ В· "
            f"<code>{html.escape(str(top['id']))}</code>",
        )
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
        tg.answer_callback(cfg, cq["id"], "Р‘Р°Р»Р°РЅСЃ")
        show(bal.format_balance_card(uid, cfg), bal.balance_keyboard(cfg))
        return True

    if action == "topup":
        if not bal.topup_enabled(cfg):
            tg.answer_callback(cfg, cq["id"], "РџРѕРїРѕР»РЅРµРЅРёРµ РІС‹РєР»", show_alert=True)
            show(bal.topup_disabled_text(), bal.balance_keyboard(cfg))
            return True
        tg.answer_callback(cfg, cq["id"], "РЎСѓРјРјР°")
        show(
            "рџ’і <b>РџРѕРїРѕР»РЅРµРЅРёРµ РЎР‘Рџ</b>\n\n"
            f"Р‘Р°Р»Р°РЅСЃ: <b>{bal.get_balance(uid)}</b> в‚Ѕ\n"
            "Р’С‹Р±РµСЂРё СЃСѓРјРјСѓ:",
            bal.topup_amounts_keyboard(),
        )
        return True

    if action == "mytop":
        tg.answer_callback(cfg, cq["id"], "Р—Р°СЏРІРєРё")
        items = bal.list_user_topups(uid, 8)
        if not items:
            show("Р—Р°СЏРІРѕРє РїРѕРєР° РЅРµС‚.", bal.balance_keyboard(cfg))
            return True
        lines = ["рџ“њ <b>РњРѕРё РїРѕРїРѕР»РЅРµРЅРёСЏ</b>\n"]
        for t in items:
            lines.append(
                f"вЂў <code>{html.escape(str(t.get('id')))}</code> В· "
                f"{t.get('amount')} в‚Ѕ В· {bal.topup_status_label(str(t.get('status')))}\n"
                f"  РєРѕРґ <code>{html.escape(str(t.get('code')))}</code>"
            )
        show("\n".join(lines), bal.balance_keyboard(cfg))
        return True

    if action == "custom":
        if not bal.topup_enabled(cfg):
            tg.answer_callback(cfg, cq["id"], "РџРѕРїРѕР»РЅРµРЅРёРµ РІС‹РєР»", show_alert=True)
            show(bal.topup_disabled_text(), bal.balance_keyboard(cfg))
            return True
        state.setdefault("balance_await", {})[str(uid)] = {"step": "custom_amount"}
        save_state(state)
        tg.answer_callback(cfg, cq["id"], "РЎРІРѕСЏ СЃСѓРјРјР°")
        show(
            f"РќР°РїРёС€Рё СЃСѓРјРјСѓ С‡РёСЃР»РѕРј ({bal.TOPUP_MIN}вЂ“{bal.TOPUP_MAX} в‚Ѕ):",
            {"inline_keyboard": [[{"text": "в—ЂпёЏ РќР°Р·Р°Рґ", "callback_data": "bal:topup"}]]},
        )
        return True

    if action == "amt" and len(parts) >= 3:
        if not bal.topup_enabled(cfg):
            tg.answer_callback(cfg, cq["id"], "РџРѕРїРѕР»РЅРµРЅРёРµ РІС‹РєР»", show_alert=True)
            show(bal.topup_disabled_text(), bal.balance_keyboard(cfg))
            return True
        try:
            amount = int(parts[2])
        except ValueError:
            tg.answer_callback(cfg, cq["id"], "РћС€РёР±РєР°", show_alert=True)
            return True
        tg.answer_callback(cfg, cq["id"], f"{amount} в‚Ѕ")
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
        # СЂР°Р·РѕРІС‹Р№ РїРѕРєР°Р· СЂРµРєРІРёР·РёС‚РѕРІ (РЅРѕРјРµСЂ) вЂ” С‚РѕР»СЊРєРѕ РїРѕ РєРЅРѕРїРєРµ
        tid = parts[2]
        top = bal.get_topup(tid)
        if not top or int(top.get("user_id") or 0) != uid:
            tg.answer_callback(cfg, cq["id"], "Р—Р°СЏРІРєР° РЅРµ РЅР°Р№РґРµРЅР°", show_alert=True)
            return True
        s = bal.sbp_cfg(cfg)
        if not s.get("phone"):
            tg.answer_callback(cfg, cq["id"], "Р РµРєРІРёР·РёС‚С‹ РЅРµ Р·Р°РґР°РЅС‹", show_alert=True)
            return True
        tg.answer_callback(cfg, cq["id"], "Р РµРєРІРёР·РёС‚С‹")
        phone_line = f"<code>{html.escape(s['phone'])}</code>"
        extra = []
        if s.get("bank"):
            extra.append(f"рџЏ¦ {html.escape(s['bank'])}")
        if s.get("name"):
            extra.append(f"рџ‘¤ {html.escape(s['name'])}")
        tg.send_message(
            cfg,
            chat_id,
            "рџ“‹ <b>Р РµРєРІРёР·РёС‚С‹ РґР»СЏ СЌС‚РѕР№ РѕРїР»Р°С‚С‹</b>\n"
            "(РЅРµ РїРµСЂРµСЃС‹Р»Р°Р№ РІ С‡Р°С‚С‹)\n\n"
            f"рџ“± {phone_line}\n"
            + ("\n".join(extra) + "\n" if extra else "")
            + f"\nРЎСѓРјРјР°: <b>{top.get('amount')}</b> в‚Ѕ\n"
            f"РљРѕРјРјРµРЅС‚Р°СЂРёР№: <code>{html.escape(str(top.get('code')))}</code>",
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
            # 24/7: СЃСЂР°Р·Сѓ РЅР° Р±Р°Р»Р°РЅСЃ, РІР»Р°РґРµР»РµС† СЃРІРµСЂРёС‚ Р±Р°РЅРє РєРѕРіРґР° РїСЂРѕСЃРЅС‘С‚СЃСЏ
            try:
                top, new_bal = bal.confirm_topup(tid, 0)
            except ValueError as e:
                tg.answer_callback(cfg, cq["id"], str(e)[:180], show_alert=True)
                return True
            top["auto_credit"] = True
            bal.save_topup(top)
            tg.answer_callback(cfg, cq["id"], "Р—Р°С‡РёСЃР»РµРЅРѕ!")
            show(
                f"вњ… <b>Р‘Р°Р»Р°РЅСЃ РїРѕРїРѕР»РЅРµРЅ</b>\n\n"
                f"+<b>{pay}</b> в‚Ѕ (РЎР‘Рџ)\n"
                f"РЎРµР№С‡Р°СЃ: <b>{new_bal}</b> в‚Ѕ\n"
                f"Р—Р°СЏРІРєР° <code>{html.escape(tid)}</code>\n\n"
                f"/balance В· /order",
                bal.balance_keyboard(cfg),
            )
            notify_owner(
                cfg,
                "вљЎ <b>РЎР‘Рџ Р°РІС‚РѕР·Р°С‡РёСЃР»РµРЅРёРµ</b> (РєР»РёРµРЅС‚ РЅРµ Р¶РґР°Р»)\n\n"
                f"РџСЂРѕРІРµСЂСЊ Р±Р°РЅРє РєРѕРіРґР° СЃРјРѕР¶РµС€СЊ: <b>{pay}</b> в‚Ѕ\n"
                f"РєРѕРґ <code>{html.escape(str(top.get('code')))}</code>\n"
                f"Р·Р°СЏРІРєР° <code>{html.escape(tid)}</code>\n"
                f"РѕС‚ {html.escape(str(who))} В· <code>{uid}</code>\n\n"
                "РќРµС‚ РїРµСЂРµРІРѕРґР° в†’ В«РЎРїРёСЃР°С‚СЊ (С„РµР№Рє)В»\n"
                "Р•СЃС‚СЊ в†’ В«Р’ Р±Р°РЅРєРµ РѕРєВ»",
                reply_markup=bal.topup_owner_keyboard(tid, mode="review"),
            )
            return True

        tg.answer_callback(cfg, cq["id"], "РќР° РїСЂРѕРІРµСЂРєРµ")
        show(
            f"рџ”Ќ <b>РћРїР»Р°С‚Р° РЅР° РїСЂРѕРІРµСЂРєРµ</b>\n\n"
            f"Р—Р°СЏРІРєР° <code>{html.escape(tid)}</code>\n"
            f"РЎСѓРјРјР°: <b>{pay}</b> в‚Ѕ\n"
            f"РљРѕРґ: <code>{html.escape(str(top.get('code')))}</code>\n\n"
            "Р—Р°С‡РёСЃР»РёРј РїРѕСЃР»Рµ РїСЂРѕРІРµСЂРєРё.\n/balance",
            bal.balance_keyboard(cfg),
        )
        notify_owner(
            cfg,
            "рџ’і <b>РЎР‘Рџ: В«РЇ РѕРїР»Р°С‚РёР»В»</b> (СЂСѓС‡РЅР°СЏ РїСЂРѕРІРµСЂРєР°)\n\n"
            f"РС‰Рё РІ Р±Р°РЅРєРµ <b>{pay}</b> в‚Ѕ В· РєРѕРґ "
            f"<code>{html.escape(str(top.get('code')))}</code>\n"
            f"РѕС‚ {html.escape(str(who))} В· <code>{uid}</code>",
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
        tg.answer_callback(cfg, cq["id"], "РћС‚РјРµРЅРµРЅРѕ")
        show(
            f"Р—Р°СЏРІРєР° <code>{html.escape(tid)}</code> РѕС‚РјРµРЅРµРЅР°.\n\n"
            + bal.format_balance_card(uid, cfg),
            bal.balance_keyboard(cfg),
        )
        return True

    # owner: confirm / reject / review after auto / reverse
    if action in ("ok", "no", "rev", "seen") and len(parts) >= 3:
        if not is_owner(cfg, user):
            tg.answer_callback(cfg, cq["id"], "РўРѕР»СЊРєРѕ РІР»Р°РґРµР»РµС†", show_alert=True)
            return True
        tid = parts[2]
        if action == "seen":
            tg.answer_callback(cfg, cq["id"], "РћРє")
            if chat_id and mid:
                try:
                    tg.edit_message_text(
                        cfg,
                        chat_id,
                        mid,
                        f"рџ‘Ќ РџСЂРѕРІРµСЂРµРЅРѕ РІ Р±Р°РЅРєРµ В· <code>{html.escape(tid)}</code>",
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
            tg.answer_callback(cfg, cq["id"], "РЎРїРёСЃР°РЅРѕ")
            if chat_id and mid:
                try:
                    tg.edit_message_text(
                        cfg,
                        chat_id,
                        mid,
                        f"в†©пёЏ РћС‚РјРµРЅР° +{pay} в‚Ѕ СЃРЅСЏС‚Р° В· "
                        f"<code>{html.escape(tid)}</code>\n"
                        f"Р±Р°Р»Р°РЅСЃ РєР»РёРµРЅС‚Р°: {new_bal} в‚Ѕ",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            try:
                tg.send_message(
                    cfg,
                    int(top["user_id"]),
                    f"в†©пёЏ РџРѕРїРѕР»РЅРµРЅРёРµ <code>{html.escape(tid)}</code> РѕС‚РјРµРЅРµРЅРѕ "
                    f"(в€’{pay} в‚Ѕ).\n"
                    f"Р•СЃР»Рё РїРµСЂРµРІРѕРґ Р±С‹Р» вЂ” РЅР°РїРёС€Рё РІР»Р°РґРµР»СЊС†Сѓ СЃ С‡РµРєРѕРј.\n"
                    f"Р‘Р°Р»Р°РЅСЃ: <b>{new_bal}</b> в‚Ѕ В· /balance",
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
            tg.answer_callback(cfg, cq["id"], "Р—Р°С‡РёСЃР»РµРЅРѕ")
            if chat_id:
                try:
                    tg.edit_message_text(
                        cfg,
                        chat_id,
                        mid,
                        f"вњ… Р—Р°С‡РёСЃР»РµРЅРѕ <b>{pay}</b> в‚Ѕ В· "
                        f"<code>{html.escape(tid)}</code>\n"
                        f"Р±Р°Р»Р°РЅСЃ РєР»РёРµРЅС‚Р°: {new_bal} в‚Ѕ",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            try:
                tg.send_message(
                    cfg,
                    int(top["user_id"]),
                    f"вњ… <b>Р‘Р°Р»Р°РЅСЃ РїРѕРїРѕР»РЅРµРЅ</b>\n"
                    f"+{pay} в‚Ѕ (РЎР‘Рџ)\n"
                    f"РЎРµР№С‡Р°СЃ: <b>{new_bal}</b> в‚Ѕ\n\n"
                    f"/balance В· /order",
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
        tg.answer_callback(cfg, cq["id"], "РћС‚РєР»РѕРЅРµРЅРѕ")
        if chat_id:
            try:
                tg.edit_message_text(
                    cfg,
                    chat_id,
                    mid,
                    f"вќЊ РћС‚РєР»РѕРЅРµРЅРѕ <code>{html.escape(tid)}</code> В· {pay} в‚Ѕ",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        try:
            tg.send_message(
                cfg,
                int(top["user_id"]),
                f"вќЊ РџРѕРїРѕР»РЅРµРЅРёРµ <code>{html.escape(tid)}</code> РЅРµ РїРѕРґС‚РІРµСЂР¶РґРµРЅРѕ.\n"
                f"Р•СЃР»Рё РїРµСЂРµРІРѕРґРёР» вЂ” РЅР°РїРёС€Рё РІР»Р°РґРµР»СЊС†Сѓ СЃ РєРѕРґРѕРј "
                f"<code>{html.escape(str(top.get('code')))}</code>.\n"
                f"/topup вЂ” РЅРѕРІР°СЏ Р·Р°СЏРІРєР°",
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
    """РЎР»РµРґСѓСЋС‰РёР№ С€Р°Рі РѕРїСЂРѕСЃР° РёР»Рё AI-СЂРµРІСЊСЋ (РѕРґРЅРѕ РѕРєРЅРѕ)."""
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
            f"вњ… <b>{n - 1}/{total}</b> В· {html.escape(str(step.get('title')))}\n\n"
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
            bal_line = f"рџ’і Р‘Р°Р»Р°РЅСЃ: <b>{bal.get_balance(uid)}</b> в‚Ѕ В· /balance В· /topup\n\n"
        else:
            bal_line = (
                f"рџ’і Р‘Р°Р»Р°РЅСЃ: <b>{bal.get_balance(uid)}</b> в‚Ѕ В· /balance\n"
                f"<i>РџРѕРїРѕР»РЅРµРЅРёРµ вЂ” СЃРєРѕСЂРѕ (Platega)</i>\n\n"
            )
    prices_block = "\n".join(orders.price_catalog_lines())
    return (
        "рџ›  <b>Р—Р°РєР°Р·</b>\n"
        "в”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ\n\n"
        f"{bal_line}"
        f"{prices_block}\n\n"
        "1) РЈСЃР»СѓРіР° в†’ 2) 4 РєРѕСЂРѕС‚РєРёС… РѕС‚РІРµС‚Р° (РјРѕР¶РЅРѕ РїСЂРѕРїСѓСЃРє)\n"
        "3) Grok СЃРѕР±РµСЂС‘С‚ РўР— в†’ 4) РїРѕРґС‚РІРµСЂРґРё в†’ РѕРїР»Р°С‚Р°\n\n"
        "вљ пёЏ РҐРѕСЃС‚РёРЅРі РЅРµ РІ С†РµРЅРµ В· рџ›Ў РіР°СЂР°РЅС‚РёСЏ 2 СЃСѓС‚."
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
    """РџСЂРµРґРѕС†РµРЅРєР° + РєРЅРѕРїРєРё РѕС‚РїСЂР°РІРёС‚СЊ/Р·Р°РЅРѕРІРѕ (+ Р±Р»РѕРє AI-СЃРІРѕРґРєРё)."""
    est = orders.estimate(kind, brief)
    cur_bal = bal.get_balance(uid)
    price_s = f"<b>{est['price']} в‚Ѕ</b> СЃ Р±Р°Р»Р°РЅСЃР°"
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
                f"\nвљ пёЏ РќР° Р±Р°Р»Р°РЅСЃРµ <b>{cur_bal}</b> в‚Ѕ вЂ” РЅРµ С…РІР°С‚Р°РµС‚ "
                f"<b>{int(est['price']) - cur_bal}</b> в‚Ѕ. РЎРЅР°С‡Р°Р»Р° /topup."
            )
        else:
            bal_warn = (
                f"\nвљ пёЏ РќР° Р±Р°Р»Р°РЅСЃРµ <b>{cur_bal}</b> в‚Ѕ, РЅСѓР¶РЅРѕ "
                f"<b>{est['price']}</b> в‚Ѕ.\n"
                f"РџРѕРїРѕР»РЅРµРЅРёРµ СЃРєРѕСЂРѕ (Platega) В· /support вЂ” С‚РёРєРµС‚"
            )
    incl = html.escape(str(est.get("includes") or ""))
    ninc = html.escape(str(est.get("not_includes") or ""))
    rev = review or {}
    ai_block = ""
    if rev:
        risk = str(rev.get("risk") or "ok")
        risk_h = {"ok": "вњ…", "warn": "вљ пёЏ", "block": "рџљ«"}.get(risk, "вЂў")
        parts = [f"рџ§  <b>РџСЂРѕРІРµСЂРєР° РўР—</b> {risk_h}"]
        if rev.get("summary"):
            parts.append(html.escape(str(rev["summary"])[:500]))

        def _clean_client_ai_text(s: str) -> str:
            s = (s or "").strip()
            if not s:
                return ""
            # РЅРµ РїРѕРєР°Р·С‹РІР°С‚СЊ СЃС‹СЂРѕР№ JSON / dump РїРѕР»РµР№ (РєР°Рє РЅР° СЃРєСЂРёРЅРµ В«Р”РѕР±Р°РІРёР» РѕС‚ СЃРµР±СЏВ»)
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
            parts.append("<b>Р”РѕР±Р°РІРёР» РѕС‚ СЃРµР±СЏ:</b> " + html.escape(add))
        if rev.get("feasible_reason"):
            parts.append(
                "<b>Р’С‹РїРѕР»РЅРёРјРѕСЃС‚СЊ:</b> "
                + html.escape(str(rev["feasible_reason"])[:300])
            )
        if rev.get("legal_reason") and risk != "ok":
            parts.append(
                "<b>Р—Р°РєРѕРЅРЅРѕСЃС‚СЊ:</b> " + html.escape(str(rev["legal_reason"])[:250])
            )
        if rev.get("risk_delay") or rev.get("risk_scope"):
            parts.append(
                f"рџ“‰ СЂРёСЃРєРё: СЃСЂРѕРєРё <b>{html.escape(str(rev.get('risk_delay') or 'вЂ”'))}</b> В· "
                f"РѕР±СЉС‘Рј <b>{html.escape(str(rev.get('risk_scope') or 'вЂ”'))}</b>"
            )
        if rev.get("upsell"):
            up = _clean_client_ai_text(str(rev.get("upsell") or ""))
            if up:
                parts.append("рџ’Ў <b>РРјРµРµС‚ СЃРјС‹СЃР» РґРѕР±Р°РІРёС‚СЊ:</b> " + html.escape(up[:280]))
        eng = str(rev.get("engine") or "")
        if eng == "grok":
            parts.append("<i>вњ… РѕР±СЂР°Р±РѕС‚Р°РЅРѕ Grok</i>")
        elif eng in ("cloud", "groq", "gemini", "openrouter", "ai_bus"):
            parts.append("<i>вњ… РѕР±СЂР°Р±РѕС‚Р°РЅРѕ AI</i>")
        elif eng.startswith("fallback"):
            # РєР»РёРµРЅС‚Сѓ РЅРµ РѕСЂС‘Рј В«offlineВ» вЂ” РўР— СѓР¶Рµ РЅРѕСЂРјР°Р»СЊРЅРѕРµ РёР· РѕРїСЂРѕСЃР°
            parts.append("<i>РўР— СЃРѕР±СЂР°РЅРѕ РїРѕ С‚РІРѕРёРј РѕС‚РІРµС‚Р°Рј</i>")
        ai_block = "\n".join(parts) + "\n\n"
    warn_line = ""
    if str(rev.get("risk") or "") == "warn":
        warn_line = (
            "вљ пёЏ Р•СЃС‚СЊ РѕРіРѕРІРѕСЂРєРё РїРѕ РѕР±СЉС‘РјСѓ/СЏСЃРЅРѕСЃС‚Рё вЂ” РјРѕР¶РЅРѕ СЃР»Р°С‚СЊ, "
            "РЅРѕ СѓС‚РѕС‡РЅРё РґРµС‚Р°Р»Рё РІ /support РµСЃР»Рё С‡С‚Рѕ.\n\n"
        )
    need = int(est["price"])
    can_pay = cur_bal >= need
    body = (
        f"рџ“‹ <b>РџРѕРґС‚РІРµСЂР¶РґРµРЅРёРµ</b>\n\n"
        + ai_block
        + warn_line
        + f"<b>{html.escape(est['title'])}</b> вЂ” {price_s}\n"
        f"рџ’і Р±Р°Р»Р°РЅСЃ: <b>{cur_bal}</b> в‚Ѕ"
        + bal_warn
        + "\n\n"
        + (f"РІС…РѕРґРёС‚: {incl}\n" if incl else "")
        + (f"РЅРµ РІС…РѕРґРёС‚: {ninc}\n" if ninc else "")
        + "\n"
        f"<b>РС‚РѕРіРѕРІРѕРµ РўР—:</b>\n{html.escape(brief[:1400])}\n\n"
        + (
            "Р’СЃС‘ РІРµСЂРЅРѕ? В«РћС‚РїСЂР°РІРёС‚СЊВ» = Р·Р°РєР°Р· + СЃРїРёСЃР°РЅРёРµ СЃ Р±Р°Р»Р°РЅСЃР°."
            if can_pay
            else "РЎРЅР°С‡Р°Р»Р° РЅСѓР¶РµРЅ Р±Р°Р»Р°РЅСЃ в‰Ґ С†РµРЅС‹ Р·Р°РєР°Р·Р° вЂ” РёРЅР°С‡Рµ В«РћС‚РїСЂР°РІРёС‚СЊВ» РЅРµ РїСЂРѕР№РґС‘С‚."
        )
    )
    kb_rows: list = []
    if can_pay:
        kb_rows.append(
            [
                {"text": "вњ… Р’СЃС‘ РІРµСЂРЅРѕ В· РѕС‚РїСЂР°РІРёС‚СЊ", "callback_data": "ord:commit"},
                {"text": "вњЏпёЏ Р—Р°РЅРѕРІРѕ", "callback_data": "ord:restart"},
            ]
        )
    else:
        if bal.topup_enabled(cfg):
            kb_rows.append(
                [{"text": "рџ’і РџРѕРїРѕР»РЅРёС‚СЊ Р±Р°Р»Р°РЅСЃ", "callback_data": "bal:topup"}]
            )
        kb_rows.append(
            [{"text": "рџ’¬ РќР°РїРёСЃР°С‚СЊ РІ РїРѕРґРґРµСЂР¶РєСѓ", "callback_data": "sup:new"}]
        )
        kb_rows.append(
            [
                {"text": "вњЏпёЏ Р—Р°РЅРѕРІРѕ", "callback_data": "ord:restart"},
                {"text": "вќЊ РћС‚РјРµРЅР°", "callback_data": "ord:cancel"},
            ]
        )
    if can_pay:
        kb_rows.append([{"text": "вќЊ РћС‚РјРµРЅР°", "callback_data": "ord:cancel"}])
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
    Grok: СЃРѕР±СЂР°С‚СЊ РўР—, СѓС‚РѕС‡РЅРёС‚СЊ, Р·Р°РєРѕРЅРЅРѕСЃС‚СЊ + РІС‹РїРѕР»РЅРёРјРѕСЃС‚СЊ.
    """
    ui_edit_or_send(
        cfg,
        chat_id,
        "рџ§  <b>РЎРѕР±РёСЂР°СЋ РўР— Рё РїСЂРѕРІРµСЂСЏСЋвЂ¦</b>\n"
        "Р•РґРёРЅС‹Р№ Р±СЂРёС„ В· Р·Р°РєРѕРЅРЅРѕСЃС‚СЊ В· СЂРµР°Р»СЊРЅРѕ Р»Рё СЃРґРµР»Р°С‚СЊ РІ С‚Р°СЂРёС„Рµ.\n"
        "РЎРµРєСѓРЅРґСѓ.",
        reply_markup={
            "inline_keyboard": [[{"text": "вќЊ РћС‚РјРµРЅР°", "callback_data": "ord:cancel"}]]
        },
        state=state,
        uid=uid,
        store_key="order_ui_msg",
    )
    review = orders.review_tz_with_ai(
        cfg, kind, answers, extra_client_note=extra_note
    )
    # Р±Р»РѕРє РЅРµР·Р°РєРѕРЅРЅРѕРіРѕ
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
        # AI block Р±РµР· rule-hit вЂ” РјСЏРіРєРёР№ РѕС‚РєР°Р· Р±РµР· Р°РІС‚РѕР±Р°РЅР°
        state.setdefault("order_draft", {}).pop(str(uid), None)
        save_state(state)
        ui_edit_or_send(
            cfg,
            chat_id,
            "рџљ« <b>Р—Р°РєР°Р· РЅРµ РјРѕР¶РµРј РІР·СЏС‚СЊ</b>\n\n"
            f"{html.escape(str(review.get('legal_reason') or 'РќРµ РїСЂРѕС…РѕРґРёС‚ РїСЂРѕРІРµСЂРєСѓ.'))}\n\n"
            "Р•СЃР»Рё РѕС€РёР±РєР° вЂ” /support В· РІР»Р°РґРµР»РµС† СЂР°Р·Р±РµСЂС‘С‚.",
            reply_markup={
                "inline_keyboard": [
                    [{"text": "рџ›  Р”СЂСѓРіРѕР№ Р·Р°РєР°Р·", "callback_data": "ord:restart"}],
                    [{"text": "рџ’¬ РџРѕРґРґРµСЂР¶РєР°", "callback_data": "sup:new"}],
                ]
            },
            state=state,
            uid=uid,
            store_key="order_ui_msg",
        )
        notify_owner(
            cfg,
            "вљ пёЏ <b>AI РѕС‚РєР»РѕРЅРёР» РўР—</b> (Р±РµР· Р°РІС‚РѕР±Р°РЅР°)\n"
            f"user <code>{uid}</code> @{html.escape(uname)}\n"
            f"{html.escape(str(review.get('legal_reason') or '')[:300])}\n\n"
            f"{html.escape(brief_b[:800])}",
        )
        return

    # РЅРµСЂРµР°Р»РёСЃС‚РёС‡РЅРѕ Р¶С‘СЃС‚РєРѕ вЂ” РїСЂРµРґР»РѕР¶РёС‚СЊ СѓРїСЂРѕСЃС‚РёС‚СЊ
    if not review.get("feasible") and str(review.get("risk") or "") == "warn":
        pass  # РїРѕРєР°Р¶РµРј warn РІ estimate

    questions = list(review.get("questions") or [])
    brief = str(review.get("brief") or orders.build_brief_from_answers(kind, answers))
    # СЃРѕС…СЂР°РЅРёС‚СЊ Р·Р°РјРµС‚РєРё AI РІ answers
    ans2 = dict(answers or {})
    if review.get("additions"):
        ans2["ai_notes"] = str(review.get("additions"))[:500]
    if extra_note:
        ans2["ai_clarify"] = (str(ans2.get("ai_clarify") or "") + "\n" + extra_note).strip()[
            :1500
        ]

    if questions and not extra_note:
        # РµС‰С‘ РЅРµ РѕС‚РІРµС‡Р°Р»Рё РЅР° СѓС‚РѕС‡РЅРµРЅРёСЏ вЂ” СЃРїСЂРѕСЃРёС‚СЊ
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
        q_lines = "\n".join(f"вЂў {html.escape(q)}" for q in questions)
        ui_edit_or_send(
            cfg,
            chat_id,
            "рџ§  <b>РџРѕС‡С‚Рё РіРѕС‚РѕРІРѕ вЂ” СѓС‚РѕС‡РЅРё, РїРѕР¶Р°Р»СѓР№СЃС‚Р°</b>\n\n"
            f"{html.escape(str(review.get('summary') or '')[:400])}\n\n"
            f"<b>Р’РѕРїСЂРѕСЃС‹:</b>\n{q_lines}\n\n"
            "РћС‚РІРµС‚СЊ <b>РѕРґРЅРёРј СЃРѕРѕР±С‰РµРЅРёРµРј</b> (РјРѕР¶РЅРѕ СЃРїРёСЃРєРѕРј).\n"
            "РР»Рё Р¶РјРё В«РџСЂРѕРїСѓСЃС‚РёС‚СЊВ» вЂ” РѕС„РѕСЂРјРёРј РєР°Рє РµСЃС‚СЊ.",
            reply_markup={
                "inline_keyboard": [
                    [{"text": "вЏ­ РџСЂРѕРїСѓСЃС‚РёС‚СЊ СѓС‚РѕС‡РЅРµРЅРёСЏ", "callback_data": "ord:ai_skip"}],
                    [{"text": "вњЏпёЏ Р—Р°РЅРѕРІРѕ", "callback_data": "ord:restart"}],
                    [{"text": "вќЊ РћС‚РјРµРЅР°", "callback_data": "ord:cancel"}],
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
    РўРёРјР»РёРґ: РІР»Р°РґРµР»РµС† РїРёС€РµС‚ РѕР±С‹С‡РЅС‹Рј СЏР·С‹РєРѕРј
    В«РіРѕСЂСЏС‚ Р·Р°РєР°Р·С‹В», В«С„РёРЅСЂР°РґР°СЂВ», В«СЃРІРѕРґРєР°В» вЂ” РёР»Рё /tl /radar /hot.
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

    # СЏРІРЅС‹Рµ РєРѕРјР°РЅРґС‹
    force = False
    q = text
    if cmd in ("/tl", "/С‚РёРјР»РёРґ", "/team", "/lead"):
        force = True
        q = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else "СЃРІРѕРґРєР°"
    elif cmd in ("/radar", "/finance", "/С„РёРЅ", "/РєР°СЃСЃa", "/РєР°СЃСЃР°"):
        force = True
        q = "С„РёРЅР°РЅСЃРѕРІС‹Р№ СЂР°РґР°СЂ"
    elif cmd in ("/hot", "/РіРѕСЂРёС‚", "/РіРѕСЂСЏС‚"):
        force = True
        q = "РєР°РєРёРµ Р·Р°РєР°Р·С‹ РіРѕСЂСЏС‚"
    elif text.startswith("/"):
        return False
    else:
        # РµСЃС‚РµСЃС‚РІРµРЅРЅС‹Р№ СЏР·С‹Рє вЂ” С‚РѕР»СЊРєРѕ РµСЃР»Рё РїРѕС…РѕР¶Рµ РЅР° Р±РёР·РЅРµСЃ-РІРѕРїСЂРѕСЃ
        keys = (
            "Р·Р°РєР°Р·",
            "РіРѕСЂСЏС‚",
            "РіРѕСЂРёС‚",
            "СЃРІРѕРґРє",
            "РјР°СЂР¶РёРЅ",
            "С„РёРЅР°РЅСЃ",
            "РєР°СЃСЃ",
            "РґРµРЅСЊРі",
            "СЂР°РґР°СЂ",
            "РІ СЂР°Р±РѕС‚Рµ",
            "СЃРґР°С‚СЊ",
            "РґРµРґР»Р°Р№РЅ",
            "РѕС‚РєСЂС‹С‚",
            "РєС‚Рѕ ",
            "РїРѕРєР°Р¶Рё",
            "СЃРєРѕР»СЊРєРѕ",
        )
        if not any(k in lower for k in keys):
            return False
        # РЅРµ РїРµСЂРµС…РІР°С‚С‹РІР°С‚СЊ РєРѕСЂРѕС‚РєРёРµ РѕС‚РІРµС‚С‹ РІ С‡СѓР¶РёС… С„Р»РѕСѓ
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
            tg.send_message(cfg, chat_id, f"вќЊ С‚РёРјР»РёРґ: {html.escape(str(e)[:200])}")
            return True
        return False


def tick_order_reports(cfg: dict) -> None:
    """РђРІС‚Рѕ-РїСЂРѕРіСЂРµСЃСЃ РєР»РёРµРЅС‚Р°Рј РїРѕ in_progress СЂР°Р· РІ ~2.5 СЃСѓС‚РѕРє."""
    if cfg.get("paused"):
        return
    try:
        import growth_lib as growth

        due = growth.due_interim_reports()
    except Exception as e:
        print("interim due", e, flush=True)
        return
    for item in due[:3]:  # РЅРµ СЃРїР°РјРёС‚СЊ РїР°С‡РєРѕР№
        try:
            uid = int(item.get("user_id") or 0)
            if not uid:
                continue
            body = growth.build_interim_report(cfg, item)
            tg.send_message(cfg, uid, body, parse_mode="HTML", disable_preview=True)
            growth.mark_report_sent(item)
            # РєР»РёРµРЅС‚ РјРѕР¶РµС‚ РѕС‚РІРµС‚РёС‚СЊ
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
    """Р Р°Р· РІ ~СЃСѓС‚РєРё вЂ” РєРѕСЂРѕС‚РєРёР№ СЂР°РґР°СЂ РІР»Р°РґРµР»СЊС†Сѓ."""
    global _last_finance_digest
    now = time.time()
    if now - _last_finance_digest < 20 * 3600:
        return
    # С‚РѕР»СЊРєРѕ РµСЃР»Рё РµСЃС‚СЊ РѕС‚РєСЂС‹С‚С‹Рµ Р·Р°РєР°Р·С‹
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

        body = "рџ“¬ <b>Р•Р¶РµРґРЅРµРІРЅС‹Р№ СЂР°РґР°СЂ</b>\n\n" + growth.finance_radar_html()
        oid = owner_chat_id(cfg)
        if oid:
            tg.send_message(cfg, oid, body, parse_mode="HTML", disable_preview=True)
        _last_finance_digest = now
    except Exception as e:
        print("finance digest", e, flush=True)


def handle_owner_system(cfg: dict, state: dict, msg: dict) -> bool:
    """
    РЎР»СѓР¶РµР±РЅС‹Рµ РєРѕРјР°РЅРґС‹ РІР»Р°РґРµР»СЊС†Р° вЂ” РЎРђРњР«Р• РџР•Р Р’Р«Р• (РґРѕ Р·Р°РєР°Р·РѕРІ/С‚РёРєРµС‚РѕРІ),
    С‡С‚РѕР±С‹ /redeploy РЅРµ СЃСЉРµРґР°Р»СЃСЏ С‡РµСЂРЅРѕРІРёРєРѕРј РўР—.
    """
    chat = msg.get("chat") or {}
    if chat.get("type") != "private":
        return False
    user = msg.get("from") or {}
    if not is_owner(cfg, user):
        return False
    text = (msg.get("text") or "").strip()
    # С‚РёРјР»РёРґ: Рё /РєРѕРјР°РЅРґС‹, Рё РµСЃС‚РµСЃС‚РІРµРЅРЅС‹Р№ СЏР·С‹Рє
    if handle_owner_teamlead(cfg, state, msg):
        return True
    if not text.startswith("/"):
        return False
    chat_id = chat.get("id")
    lower = text.lower()
    cmd = lower.split()[0].split("@")[0]

    uid = int(user.get("id") or 0)

    if cmd in ("/ref", "/СЂРµС„", "/invite"):
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
            f"рџ”— <b>Р РµС„РµСЂР°Р»СЊРЅР°СЏ СЃСЃС‹Р»РєР°</b>\n\n"
            f"<code>{html.escape(link)}</code>\n\n"
            f"Р”СЂСѓРі Р¶РјС‘С‚ Start в†’ РµРіРѕ РїРµСЂРІС‹Р№ РѕРїР»Р°С‡РµРЅРЅС‹Р№ Р·Р°РєР°Р· "
            f"РґР°С‘С‚ С‚РµР±Рµ <b>+{growth.REF_BONUS_RUB} в‚Ѕ</b> РЅР° Р±Р°Р»Р°РЅСЃ.\n"
            f"/balance",
            parse_mode="HTML",
            disable_preview=True,
        )
        return True

    if cmd in ("/contract", "/dogovor", "/docs_order") and len(text.split()) >= 2:
        oid = text.split()[1].strip()
        item = orders.get_order(oid)
        if not item:
            tg.send_message(cfg, chat_id, "Р—Р°РєР°Р· РЅРµ РЅР°Р№РґРµРЅ")
            return True
        try:
            import docs_lib

            cpath, apath = docs_lib.write_contract_files(item)
            tg.send_document(cfg, chat_id, str(cpath), caption=f"Р”РѕРіРѕРІРѕСЂ {oid}")
            tg.send_document(cfg, chat_id, str(apath), caption=f"РђРєС‚ {oid}")
        except Exception as e:
            tg.send_message(cfg, chat_id, f"вќЊ {html.escape(str(e)[:200])}")
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
        gw_line = "рџЋЃ СЂРѕР·С‹РіСЂС‹С€: РЅРµС‚ вЂ” Р¶РјРё /gwrestore"
        if act:
            mid_ch = act.get("channel_message_id") or "вЂ”"
            gw_line = (
                f"рџЋЃ complete: <b>{gw.entry_count(act, complete_only=True)}</b> В· "
                f"РІСЃРµРіРѕ: {gw.entry_count(act, complete_only=False)}\n"
                f"id <code>{html.escape(str(act.get('id')))}</code> В· РїРѕСЃС‚ {mid_ch}"
            )
        host_mode = str(cfg.get("bot_host_mode") or "local")
        brain_line = "brain: ?"
        br_line = "bridge: off"
        try:
            from content import _bridge_url, brain_status

            bru = _bridge_url(cfg) or ""
            bst = brain_status(cfg, use_cache=False, probe_ollama=False)
            active = str(bst.get("active") or "вЂ”")
            src = str(bst.get("grok_source") or "вЂ”")
            model = str(bst.get("grok_model") or "вЂ”")
            brain_line = (
                f"brain: <b>{html.escape(active)}</b> В· src <code>{html.escape(src)}</code>\n"
                f"model: <code>{html.escape(model)}</code>"
            )
            if bst.get("hint"):
                brain_line += f"\nрџ’Ў {html.escape(str(bst.get('hint'))[:100])}"
            if bru:
                try:
                    import requests as _rq

                    sec = (cfg.get("grok_bridge_secret") or "").strip()
                    hdrs = {"X-Bridge-Secret": sec} if sec else {}
                    hr = _rq.get(f"{bru.rstrip('/')}/health", headers=hdrs, timeout=6)
                    ok = hr.ok and "ok" in (hr.text or "").lower()
                    br_line = (
                        f"bridge: {'вњ…' if ok else 'вќЊ http '+str(hr.status_code)}\n"
                        f"<code>{html.escape(bru[:56])}</code>"
                    )
                except Exception as be:
                    br_line = (
                        f"bridge: вќЊ {html.escape(str(be)[:80])}\n"
                        f"<code>{html.escape(bru[:56])}</code>\n"
                        "РџРљ: start_grok_bridge.bat"
                    )
            else:
                br_line = "bridge: off (API key / session / none)"
        except Exception as e:
            br_line = f"bridge: вќЊ {html.escape(str(e)[:100])}"

        _owner_panel(
            cfg,
            state,
            chat_id,
            None,
            uid,
            f"pong вњ… В· <b>Vaggo {html.escape(BOT_CODE_VERSION)}</b>\n"
            f"ver: <code>{html.escape(BOT_CODE_VERSION)}</code>\n"
            f"host: <code>{html.escape(host_mode)}</code>\n"
            f"BOT_ID: <code>{html.escape((_os.environ.get('BOT_ID') or 'local')[:40])}</code>\n\n"
            f"{brain_line}\n"
            f"{br_line}\n\n"
            f"{gw_line}\n\n"
            f"рџЏ  /start В· рџЋЃ /gstatus В· в™»пёЏ /gwrestore\n"
            f"рџ”„ /redeploy В· рџ“Њ /gfixkb",
            {
                "inline_keyboard": [
                    [{"text": "рџЏ  РњРµРЅСЋ", "callback_data": "menu:home"}],
                    [
                        {"text": "рџЋЃ Р РѕР·С‹РіСЂС‹С€", "callback_data": "menu:giveaway"},
                        {"text": "в™»пёЏ Restore", "callback_data": "menu:gwrestore"},
                    ],
                    [{"text": "рџ“Њ РљРЅРѕРїРєРё РЅР° РїРѕСЃС‚", "callback_data": "menu:gfixkb"}],
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
                "в™»пёЏ <b>Restore СЂРѕР·С‹РіСЂС‹С€Р°</b>\n\n"
                f"{html.escape(str(res.get('message') or res))}\n"
                f"active: <code>{html.escape(str(res.get('active_id') or 'вЂ”'))}</code>\n"
                f"complete: <b>{res.get('complete') or 0}</b>\n"
                f"started: {res.get('started') or 0}\n"
                f"prize: {html.escape(str(res.get('prize') or 'вЂ”')[:80])}\n"
            )
            mid_ch = res.get("channel_message_id")
            if mid_ch:
                body += f"\nРїРѕСЃС‚: https://t.me/Vaggo01/{mid_ch}"
            tg.send_message(cfg, chat_id, body, parse_mode="HTML", disable_preview=True)
        except Exception as e:
            tg.send_message(cfg, chat_id, f"вќЊ gwrestore: {html.escape(str(e)[:300])}")
        return True

    if cmd in ("/clean", "/clearchat", "/purge"):
        ui_delete_user_message(cfg, msg)
        # 1) РЅРѕРІРѕРµ РјРµРЅСЋ
        mid = ui_edit_or_send(
            cfg,
            chat_id,
            owner_home_html() + "\n\nрџ§№ <i>Р§РёС‰Сѓ С‡Р°С‚вЂ¦</i>",
            reply_markup=main_menu_keyboard(),
            state=state,
            uid=uid,
            store_key="owner_ui_msg",
        )
        # 2) deep purge: tracked + mid-1вЂ¦mid-120
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
            # 3) С„РёРЅР°Р»СЊРЅС‹Р№ С‚РµРєСЃС‚ РЅР° С‚РѕРј Р¶Рµ РѕРєРЅРµ
            try:
                tg.edit_message_text(
                    cfg,
                    chat_id,
                    int(mid),
                    owner_home_html()
                    + f"\n\nрџ§№ <b>Р“РѕС‚РѕРІРѕ</b> В· СѓР±СЂР°РЅРѕ ~{n} СЃРѕРѕР±С‰. Р±РѕС‚Р°\n"
                    f"<i>РЎС‚Р°СЂС€Рµ ~48С‡ Telegram РЅРµ РґР°С‘С‚ СѓРґР°Р»СЏС‚СЊ Р±РѕС‚Сѓ вЂ” СЃРјР°С…РЅРё РІСЂСѓС‡РЅСѓСЋ.</i>",
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
            "рџ—‘ Р§РµСЂРЅРѕРІРёРє Р·Р°РєР°Р·Р° СЃР±СЂРѕС€РµРЅ.",
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
            "вЏі РўСЏРЅСѓ РєРѕРґ СЃ GitHubвЂ¦",
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
                    f"вќЊ Pull: {html.escape(str(res['pull_error'])[:400])}",
                    state=state,
                    uid=uid,
                    store_key="owner_ui_msg",
                )
                return True
            ui_edit_or_send(
                cfg,
                chat_id,
                "рџљЂ <b>Redeploy</b>\n\n"
                f"ver: <code>{html.escape(BOT_CODE_VERSION)}</code>\n"
                f"sha: <code>{html.escape(str(res.get('remote_sha') or pull.get('sha') or 'вЂ”'))}</code>\n"
                f"files: {int(pull.get('count') or 0)}\n"
                f"<code>{html.escape(files[:400])}</code>\n\n"
                f"restart: {html.escape(str(rst.get('message') or rst.get('method') or rst.get('error') or rst)[:200])}\n"
                f"{'вЏі РџРµСЂРµР·Р°РїСѓСЃРєвЂ¦' if rst.get('will_exit') else ''}",
                state=state,
                uid=uid,
                store_key="owner_ui_msg",
            )
        except Exception as e:
            ui_edit_or_send(
                cfg,
                chat_id,
                f"вќЊ redeploy: {html.escape(str(e)[:400])}",
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
                "рџ“¦ <b>Deploy</b>\n\n"
                f"ver: <code>{html.escape(BOT_CODE_VERSION)}</code>\n"
                f"local: <code>{html.escape((local or 'вЂ”')[:12])}</code>\n"
                f"remote: <code>{html.escape((remote or 'вЂ”')[:12])}</code>\n"
                f"need update: <b>{'YES' if need else 'no'}</b>\n\n"
                "/redeploy В· /clean",
                reply_markup=main_menu_keyboard(),
                state=state,
                uid=uid,
                store_key="owner_ui_msg",
            )
        except Exception as e:
            ui_edit_or_send(
                cfg,
                chat_id,
                f"вќЊ {html.escape(str(e)[:300])}",
                state=state,
                uid=uid,
                store_key="owner_ui_msg",
            )
        return True

    return False


def handle_orders_private(cfg: dict, state: dict, msg: dict) -> bool:
    """Р—Р°РєР°Р·С‹: РїСЂРёС‘Рј Р·Р°СЏРІРѕРє + СЃРґР°С‡Р°. Р РµР·СѓР»СЊС‚Р°С‚ РІСЃРµРіРґР° РѕС‚РґРµР»СЊРЅРѕ РѕС‚ РєР°РЅР°Р»Р°/Р±РѕС‚Р°."""
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

    # owner: РїСЂР°РІРєР° РўР—
    if owner:
        aetz = (state.get("await_owner_edit_tz") or {}).get(str(uid))
        if aetz and text and not text.startswith("/"):
            oid = str(aetz.get("order_id") or "")
            state.setdefault("await_owner_edit_tz", {}).pop(str(uid), None)
            save_state(state)
            item = orders.get_order(oid)
            if not item:
                tg.send_message(cfg, chat_id, "Р—Р°РєР°Р· РЅРµ РЅР°Р№РґРµРЅ")
                return True
            item["brief"] = text.strip()[:2000]
            orders.save_order(item)
            try:
                orders.append_event(item, "tz_edit", "РўР— РѕР±РЅРѕРІР»РµРЅРѕ РІР»Р°РґРµР»СЊС†РµРј")
            except Exception:
                pass
            item = orders.get_order(oid) or item
            try:
                _push_client_order_card(
                    cfg,
                    item,
                    delta=f"вњЏпёЏ РўР— РїРѕ Р·Р°РєР°Р·Сѓ <code>{html.escape(oid)}</code> РѕР±РЅРѕРІР»РµРЅРѕ.",
                )
            except Exception:
                pass
            tg.send_message(
                cfg,
                chat_id,
                f"вњ… РўР— СЃРѕС…СЂР°РЅРµРЅРѕ В· <code>{html.escape(oid)}</code>",
                parse_mode="HTML",
                reply_markup=orders.owner_order_hub_keyboard(
                    oid, user_id=int(item.get("user_id") or 0) or None
                ),
            )
            return True
        if aetz and cmd in ("/cancel", "/РѕС‚РјРµРЅР°"):
            state.setdefault("await_owner_edit_tz", {}).pop(str(uid), None)
            save_state(state)
            tg.send_message(cfg, chat_id, "РџСЂР°РІРєР° РўР— РѕС‚РјРµРЅРµРЅР°.")
            return True

        # owner: СЃРѕРѕР±С‰РµРЅРёРµ РєР»РёРµРЅС‚Сѓ
        amsg = (state.get("await_owner_msg_client") or {}).get(str(uid))
        if amsg and text and not text.startswith("/"):
            oid = str(amsg.get("order_id") or "")
            cid = int(amsg.get("client_id") or 0)
            state.setdefault("await_owner_msg_client", {}).pop(str(uid), None)
            save_state(state)
            if not cid:
                tg.send_message(cfg, chat_id, "РќРµС‚ client_id")
                return True
            try:
                # force_reply вЂ” РєР»РёРµРЅС‚ Р¶РјС‘С‚ В«РѕС‚РІРµС‚РёС‚СЊВ» РЅР° СЃРѕРѕР±С‰РµРЅРёРµ
                payload = {
                    "chat_id": cid,
                    "text": (
                        f"РЎРѕРѕР±С‰РµРЅРёРµ РїРѕ Р·Р°РєР°Р·Сѓ <code>{html.escape(oid)}</code>\n\n"
                        f"{html.escape(text[:2800])}\n\n"
                        f"<i>РћС‚РІРµС‚СЊ РЅР° СЌС‚Рѕ СЃРѕРѕР±С‰РµРЅРёРµ вЂ” СЏ РїРµСЂРµРґР°Рј РёСЃРїРѕР»РЅРёС‚РµР»СЋ.</i>"
                    ),
                    "parse_mode": "HTML",
                    "reply_markup": json.dumps(
                        {
                            "force_reply": True,
                            "input_field_placeholder": "Р’Р°С€ РѕС‚РІРµС‚вЂ¦",
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
                # РєРѕРЅС‚РµРєСЃС‚: Р»СЋР±РѕР№ СЃР»РµРґСѓСЋС‰РёР№ РѕС‚РІРµС‚ РєР»РёРµРЅС‚Р° (reply РёР»Рё РїСЂРѕСЃС‚Рѕ С‚РµРєСЃС‚)
                state.setdefault("client_reply_ctx", {})[str(cid)] = {
                    "order_id": oid,
                    "owner_msg_id": int(mid_sent) if mid_sent else None,
                    "ts": int(time.time()),
                }
                save_state(state)
                # РїРѕРґС‚РІРµСЂР¶РґРµРЅРёРµ РІР»Р°РґРµР»СЊС†Сѓ вЂ” edit РіР»Р°РІРЅРѕРіРѕ РѕРєРЅР°
                ui_edit_or_send(
                    cfg,
                    chat_id,
                    f"вњ… РЈС€Р»Рѕ РєР»РёРµРЅС‚Сѓ\nР·Р°РєР°Р· <code>{html.escape(oid)}</code>\n\n"
                    f"РћРЅ РјРѕР¶РµС‚ <b>РѕС‚РІРµС‚РёС‚СЊ</b> вЂ” РїСЂРёРґС‘С‚ СЃСЋРґР°.",
                    reply_markup=orders.owner_order_hub_keyboard(oid, user_id=cid),
                    state=state,
                    uid=uid,
                    store_key="owner_ui_msg",
                )
                item = orders.get_order(oid)
                if item:
                    orders.append_event(item, "msg", "СЃРѕРѕР±С‰РµРЅРёРµ РєР»РёРµРЅС‚Сѓ")
            except Exception as e:
                tg.send_message(
                    cfg, chat_id, f"вќЊ РќРµ СЃРјРѕРі РЅР°РїРёСЃР°С‚СЊ: {html.escape(str(e)[:200])}"
                )
            return True
        if amsg and cmd in ("/cancel", "/РѕС‚РјРµРЅР°"):
            state.setdefault("await_owner_msg_client", {}).pop(str(uid), None)
            save_state(state)
            tg.send_message(cfg, chat_id, "РЎРѕРѕР±С‰РµРЅРёРµ РѕС‚РјРµРЅРµРЅРѕ.")
            return True

    # РєР»РёРµРЅС‚ РѕС‚РІРµС‡Р°РµС‚ РЅР° СЃРѕРѕР±С‰РµРЅРёРµ РІР»Р°РґРµР»СЊС†Р° (reply РёР»Рё РґРёР°Р»РѕРі)
    if not owner and text and not text.startswith("/"):
        # РЅРµ РїРµСЂРµС…РІР°С‚С‹РІР°С‚СЊ РѕРїСЂРѕСЃ РўР— / confirm
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
        # reply РІСЃРµРіРґР° РјРѕР¶РЅРѕ; free-text вЂ” С‚РѕР»СЊРєРѕ РІРЅРµ РѕС„РѕСЂРјР»РµРЅРёСЏ Р·Р°РєР°Р·Р°
        if (use_ctx and not in_order_flow) or is_reply_to_bot:
            oid = str((ctx or {}).get("order_id") or "")
            if not oid and is_reply_to_bot:
                # РїРѕРїСЂРѕР±СѓРµРј РІС‹С‚Р°С‰РёС‚СЊ id РёР· С‚РµРєСЃС‚Р°, РЅР° РєРѕС‚РѕСЂС‹Р№ РѕС‚РІРµС‚РёР»Рё
                rtxt = (rt.get("text") or rt.get("caption") or "")
                m = re.search(r"Р·Р°РєР°Р·Сѓ?\s+([a-f0-9]{8,12})", rtxt, re.I)
                if not m:
                    m = re.search(r"\b([a-f0-9]{10})\b", rtxt)
                if m:
                    oid = m.group(1)
            if not oid:
                # РѕС‚РєСЂС‹С‚С‹Р№ Р·Р°РєР°Р· РєР»РёРµРЅС‚Р°
                pend = orders.user_pending_order(uid)
                if pend:
                    oid = str(pend.get("id") or "")
            who = f"@{uname}" if uname else name
            notify_owner(
                cfg,
                f"рџ’¬ <b>РћС‚РІРµС‚ РєР»РёРµРЅС‚Р°</b>\n"
                f"{html.escape(str(who))} В· <code>{uid}</code>\n"
                f"Р·Р°РєР°Р· <code>{html.escape(oid or 'вЂ”')}</code>\n\n"
                f"{html.escape(text[:1500])}",
                reply_markup={
                    "inline_keyboard": (
                        [
                            [
                                {
                                    "text": "рџ“‚ Р—Р°РєР°Р·",
                                    "callback_data": f"ord:o:open:{oid}",
                                },
                                {
                                    "text": "рџ’¬ РћС‚РІРµС‚РёС‚СЊ",
                                    "callback_data": f"ord:o:msg:{oid}",
                                },
                            ]
                        ]
                        if oid
                        else [[{"text": "рџЏ  РџСѓР»СЊС‚", "callback_data": "menu:home"}]]
                    )
                },
            )
            # РЅРµ СЃР±СЂР°СЃС‹РІР°РµРј ctx вЂ” РјРѕР¶РЅРѕ РїРµСЂРµРїРёСЃС‹РІР°С‚СЊСЃСЏ
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
            # РєРѕСЂРѕС‚РєРѕ, Р±РµР· СЃРїР°РјР° РµСЃР»Рё СѓР¶Рµ РІ РїРµСЂРµРїРёСЃРєРµ
            last_ack = int((ctx or {}).get("last_ack") or 0)
            if int(time.time()) - last_ack > 120:
                tg.send_message(cfg, chat_id, "вњ… РџСЂРёРЅСЏР».", parse_mode="HTML")
                if ctx is not None:
                    ctx["last_ack"] = int(time.time())
                    state.setdefault("client_reply_ctx", {})[str(uid)] = ctx
                    save_state(state)
            return True

    # РІРѕРїСЂРѕСЃ РїРѕ РїСЂРѕРµРєС‚Сѓ (СЃ РєР°СЂС‚РѕС‡РєРё Р·Р°РєР°Р·Р°)
    aq = (state.get("await_order_question") or {}).get(str(uid))
    if aq and text and not text.startswith("/"):
        oid = str(aq.get("order_id") or "")
        state.setdefault("await_order_question", {}).pop(str(uid), None)
        # РѕСЃС‚Р°РІР»СЏРµРј reply-ctx РґР»СЏ РґР°Р»СЊРЅРµР№С€РµР№ РїРµСЂРµРїРёСЃРєРё
        state.setdefault("client_reply_ctx", {})[str(uid)] = {
            "order_id": oid,
            "ts": int(time.time()),
        }
        save_state(state)
        item = orders.get_order(oid) if oid else None
        tg.send_message(
            cfg,
            chat_id,
            f"вњ… РћС‚РїСЂР°РІРёР». РњРѕР¶РµС€СЊ РїРёСЃР°С‚СЊ РґР°Р»СЊС€Рµ вЂ” РѕС‚РІРµС‡Сѓ Р·РґРµСЃСЊ.",
            parse_mode="HTML",
        )
        try:
            who = f"@{uname}" if uname else name
            notify_owner(
                cfg,
                f"рџ’¬ <b>Р’РѕРїСЂРѕСЃ РїРѕ Р·Р°РєР°Р·Сѓ</b> <code>{html.escape(oid)}</code>\n"
                f"{html.escape(str(who))} В· <code>{uid}</code>\n\n"
                f"{html.escape(text[:1200])}",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {
                                "text": "рџ“‚ Р—Р°РєР°Р·",
                                "callback_data": f"ord:o:open:{oid}",
                            },
                            {
                                "text": "рџ’¬ РћС‚РІРµС‚РёС‚СЊ",
                                "callback_data": f"ord:o:msg:{oid}",
                            },
                        ]
                    ]
                },
            )
        except Exception:
            pass
        return True
    if aq and cmd in ("/cancel", "/РѕС‚РјРµРЅР°"):
        state.setdefault("await_order_question", {}).pop(str(uid), None)
        save_state(state)
        tg.send_message(cfg, chat_id, "РћРє, РѕС‚РјРµРЅРµРЅРѕ.")
        return True

    # Р»СЋР±С‹Рµ slash-РєРѕРјР°РЅРґС‹ вЂ” РЅРµ С‚СЂРѕРіР°РµРј (РёРЅР°С‡Рµ /redeploy В«РїСЂРѕРіР»Р°С‚С‹РІР°РµС‚СЃСЏВ»)
    if text.startswith("/"):
        # РєСЂРѕРјРµ order-РєРѕРјР°РЅРґ РЅРёР¶Рµ
        if cmd not in (
            "/order",
            "/Р·Р°РєР°Р·",
            "/orders",
            "/Р·Р°РєР°Р·С‹",
            "/myorders",
            "/РјРѕРё",
            "/РёСЃС‚РѕСЂРёСЏ",
            "/myorder",
            "/prices",
            "/odeliver",
        ):
            return False

    # --- owner: СЃРїРёСЃРѕРє / РІС‹РґР°С‡Р° ---
    if owner and cmd in ("/orders", "/Р·Р°РєР°Р·С‹"):
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
            tg.send_message(cfg, chat_id, "РџСЂРёРјРµСЂ: /odeliver abc123\nРџРѕС‚РѕРј С„Р°Р№Р» СЃ РїРѕРґРїРёСЃСЊСЋ /odeliver abc123")
            return True
        oid = arg.split()[0]
        item = orders.get_order(oid)
        if not item:
            tg.send_message(cfg, chat_id, "Р—Р°РєР°Р· РЅРµ РЅР°Р№РґРµРЅ")
            return True
        state["await_order_deliver"] = oid
        save_state(state)
        tg.send_message(
            cfg,
            chat_id,
            f"Р–РґСѓ С„Р°Р№Р» РґР»СЏ Р·Р°РєР°Р·Р° <code>{html.escape(oid)}</code>\n"
            f"РџСЂРёС€Р»Рё РґРѕРєСѓРјРµРЅС‚/Р°СЂС…РёРІ/С„РѕС‚Рѕ (РјРѕР¶РЅРѕ СЃ РїРѕРґРїРёСЃСЊСЋ).",
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
            cap = (msg.get("caption") or text or "Р“РѕС‚РѕРІРѕ РїРѕ Р·Р°РєР°Р·Сѓ").strip()
            try:
                client_id = int(item["user_id"])
                if kind == "photo":
                    tg.api(
                        cfg,
                        "sendPhoto",
                        data={
                            "chat_id": client_id,
                            "photo": file_id,
                            "caption": f"вњ… Р—Р°РєР°Р· <code>{oid}</code>\n{cap}"[:1024],
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
                            "caption": f"вњ… Р—Р°РєР°Р· <code>{oid}</code>\n{cap}"[:1024],
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
                            "caption": f"вњ… Р—Р°РєР°Р· <code>{oid}</code>\n{cap}"[:1024],
                            "parse_mode": "HTML",
                        },
                    )
                item["status"] = "done"
                item["result_file_id"] = file_id
                item["deliver_note"] = cap[:500]
                orders.save_order(item)
                state.pop("await_order_deliver", None)
                save_state(state)
                tg.send_message(cfg, chat_id, f"вњ… Р¤Р°Р№Р» СѓС€С‘Р» РєР»РёРµРЅС‚Сѓ В· Р·Р°РєР°Р· {oid} = done")
            except Exception as e:
                tg.send_message(cfg, chat_id, f"вќЊ РќРµ СЃРјРѕРі РѕС‚РїСЂР°РІРёС‚СЊ РєР»РёРµРЅС‚Сѓ: {html.escape(str(e)[:200])}")
            return True

    # --- user: history ---
    if cmd in ("/myorders", "/РјРѕРё", "/РёСЃС‚РѕСЂРёСЏ", "/myorder"):
        ui_edit_or_send(
            cfg,
            chat_id,
            f"рџ’° Р‘Р°Р»Р°РЅСЃ: <b>{bal.get_balance(uid)}</b> в‚Ѕ"
            + (
                " В· /topup\n\n"
                if bal.topup_enabled(cfg)
                else "\n<i>РџРѕРїРѕР»РЅРµРЅРёРµ РІСЂРµРјРµРЅРЅРѕ РІС‹РєР».</i>\n\n"
            )
            + orders.format_user_history(uid),
            reply_markup={
                "inline_keyboard": [
                    [{"text": "рџ”„ РћР±РЅРѕРІРёС‚СЊ", "callback_data": "ord:mine"}],
                    [{"text": "рџ’° Р‘Р°Р»Р°РЅСЃ", "callback_data": "bal:show"}],
                    [{"text": "рџ›  РќРѕРІС‹Р№ Р·Р°РєР°Р·", "callback_data": "ord:restart"}],
                ]
            },
            state=state,
            uid=uid,
            store_key="order_ui_msg",
        )
        save_state(state)
        return True

    # --- user: start order ---
    if cmd in ("/order", "/Р·Р°РєР°Р·") or (cmd == "/orders" and not owner):
        # СѓР±СЂР°С‚СЊ /order РёР· С‡Р°С‚Р°
        ui_delete_user_message(cfg, msg)
        _order_start_msg(cfg, chat_id, state=state, uid=uid)
        save_state(state)
        return True

    # await TZ questionnaire (or legacy confirm)
    drafts = state.setdefault("order_draft", {})
    draft = drafts.get(str(uid))
    if draft and draft.get("kind"):
        # С„РѕС‚Рѕ/РґРѕРє РєР°Рє СЂРµС„РµСЂРµРЅСЃ РЅР° С€Р°РіРµ В«РїСЂРёРјРµСЂВ»
        if (
            draft.get("tz_step") == "example"
            and not draft.get("await_confirm")
            and (msg.get("photo") or msg.get("document"))
        ):
            file_id = None
            if msg.get("photo"):
                file_id = (msg.get("photo") or [])[-1].get("file_id")
                label = "С„РѕС‚Рѕ-СЂРµС„РµСЂРµРЅСЃ"
            else:
                file_id = (msg.get("document") or {}).get("file_id")
                label = "С„Р°Р№Р»-СЂРµС„РµСЂРµРЅСЃ"
            cap = (msg.get("caption") or text or "").strip()
            ans = f"{label}" + (f": {cap}" if cap else "")
            if file_id:
                ans += f" [file_id:{file_id[:40]}вЂ¦]"
            text = ans if len(ans) >= 2 else "СЂРµС„РµСЂРµРЅСЃ РІРѕ РІР»РѕР¶РµРЅРёРё"
        elif msg.get("photo") or msg.get("document") or msg.get("video"):
            return False
        if text.startswith("/"):
            return False
        # СѓР¶Рµ Р¶РґС‘Рј С‚РѕР»СЊРєРѕ РїРѕРґС‚РІРµСЂР¶РґРµРЅРёРµ РєРЅРѕРїРєРѕР№
        if draft.get("await_confirm"):
            ui_delete_user_message(cfg, msg)
            ui_edit_or_send(
                cfg,
                chat_id,
                "Р–РјРё РєРЅРѕРїРєСѓ <b>В«Р’СЃС‘ РІРµСЂРЅРѕ В· РѕС‚РїСЂР°РІРёС‚СЊВ»</b> РёР»Рё <b>В«Р—Р°РЅРѕРІРѕВ»</b> РІ СЃРѕРѕР±С‰РµРЅРёРё РІС‹С€Рµ.",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {
                                "text": "вњ… Р’СЃС‘ РІРµСЂРЅРѕ В· РѕС‚РїСЂР°РІРёС‚СЊ",
                                "callback_data": "ord:commit",
                            },
                            {"text": "вњЏпёЏ Р—Р°РЅРѕРІРѕ", "callback_data": "ord:restart"},
                        ],
                        [{"text": "вќЊ РћС‚РјРµРЅР°", "callback_data": "ord:cancel"}],
                    ]
                },
                state=state,
                uid=uid,
                store_key="order_ui_msg",
            )
            save_state(state)
            return True
        # AI-СѓС‚РѕС‡РЅРµРЅРёСЏ РїРѕСЃР»Рµ РѕРїСЂРѕСЃР°
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
                f"вљ пёЏ {html.escape(err)}\n\n" + str(step.get("ask") or ""),
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

    # soft: keywords from non-owner вЂ” РќР• С‚СЂРѕРіР°С‚СЊ, РµСЃР»Рё СѓР¶Рµ РёРґС‘С‚ РѕРїСЂРѕСЃ/С‡РµСЂРЅРѕРІРёРє
    if not owner and text and not text.startswith("/"):
        soft = ("Р·Р°РєР°Р·", "СЃРґРµР»Р°С‚СЊ СЃР°Р№С‚", "СЃРґРµР»Р°С‚СЊ Р±РѕС‚Р°", "С…РѕС‡Сѓ СЃР°Р№С‚", "СЂР°Р·СЂР°Р±РѕС‚", "Р·Р°РєР°Р·Р°С‚СЊ")
        tl = text.lower()
        if any(s in tl for s in soft) and not drafts.get(str(uid)):
            ui_edit_or_send(
                cfg,
                chat_id,
                "РџРѕС…РѕР¶Рµ РЅР° Р·Р°РєР°Р·.\n\nР–РјРё РєРЅРѕРїРєСѓ РёР»Рё /order вЂ” РІС‹Р±РµСЂРµРј С‚РёРї Рё РѕС†РµРЅРёРј.",
                reply_markup={
                    "inline_keyboard": [
                        [{"text": "рџ›  РћС„РѕСЂРјРёС‚СЊ Р·Р°РєР°Р·", "callback_data": "ord:restart"}],
                        [{"text": "рџ“¦ РњРѕРё Р·Р°РєР°Р·С‹", "callback_data": "ord:mine"}],
                    ]
                },
                state=state,
                uid=uid,
            )
            return True

    return False


def _push_client_order_card(cfg: dict, item: dict, *, delta: str | None = None) -> None:
    """
    Р–РёРІР°СЏ РєР°СЂС‚РѕС‡РєР°: edit СЃС‚Р°СЂРѕРіРѕ СЃРѕРѕР±С‰РµРЅРёСЏ, РёРЅР°С‡Рµ send + Р·Р°РїРѕРјРЅРёС‚СЊ mid.
    РћРїС†РёРѕРЅР°Р»СЊРЅРѕ РєРѕСЂРѕС‚РєРёР№ delta-РїСѓС€.
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
    """Р’Р»Р°РґРµР»РµС†: СЃРјРµРЅРёС‚СЊ СЃС‚Р°С‚СѓСЃ + СѓРІРµРґРѕРјРёС‚СЊ РєР»РёРµРЅС‚Р°. Р’СЃРµРіРґР° load СЃРІРµР¶РёР№ state."""
    user = cq.get("from") or {}
    if not is_owner(cfg, user):
        tg.answer_callback(cfg, cq["id"], "РўРѕР»СЊРєРѕ РІР»Р°РґРµР»РµС†", show_alert=True)
        return True
    item = orders.get_order(oid)
    if not item:
        # РґРёР°РіРЅРѕСЃС‚РёРєР°
        try:
            all_ids = list((orders.load_orders().get("items") or {}).keys())
        except Exception:
            all_ids = []
        tg.answer_callback(
            cfg,
            cq["id"],
            f"РќРµ РЅР°Р№РґРµРЅ {oid[:8]}вЂ¦ (РІ Р±Р°Р·Рµ {len(all_ids)})",
            show_alert=True,
        )
        return True
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    item["status"] = status
    orders.save_order(item)
    # timeline
    ev_map = {
        "in_progress": "РІР·СЏС‚ РІ СЂР°Р±РѕС‚Сѓ",
        "done": "РіРѕС‚РѕРІ В· СЃРґР°С‡Р°",
        "cancelled": "РѕС‚РјРµРЅС‘РЅ",
        "accepted": "РїСЂРёРЅСЏС‚",
        "new": "РЅРѕРІС‹Р№",
    }
    try:
        orders.append_event(item, status, ev_map.get(status, status))
        item = orders.get_order(oid) or item
    except Exception:
        pass
    client_msgs = {
        "in_progress": (
            f"рџ›  <b>#{html.escape(oid[:8])}</b> вЂ” РІ СЂР°Р±РѕС‚Рµ.\n"
            f"РљР°СЂС‚РѕС‡РєР° РѕР±РЅРѕРІР»РµРЅР°. Р’РѕРїСЂРѕСЃС‹ вЂ” РєРЅРѕРїРєРѕР№ В«Р’РѕРїСЂРѕСЃ РїРѕ РїСЂРѕРµРєС‚СѓВ»."
        ),
        "done": (
            f"вњ”пёЏ <b>#{html.escape(oid[:8])}</b> вЂ” РіРѕС‚РѕРІРѕ.\n"
            f"Р¤Р°Р№Р»/СЂРµР·СѓР»СЊС‚Р°С‚ РїСЂРёРґС‘С‚ РѕС‚РґРµР»СЊРЅРѕ. РџСЂРёРјРё РёР»Рё РЅР°РїРёС€Рё РїСЂР°РІРєРё."
        ),
        "cancelled": (
            f"вќЊ <b>#{html.escape(oid[:8])}</b> РѕС‚РјРµРЅС‘РЅ.\n"
            f"РќРѕРІС‹Р№: /order"
        ),
    }
    if status == "in_progress":
        tg.answer_callback(cfg, cq["id"], "Р’ СЂР°Р±РѕС‚Рµ", show_alert=False)
    elif status == "done":
        tg.answer_callback(cfg, cq["id"], "Р“РѕС‚РѕРІРѕ вЂ” РїСЂРёС€Р»Рё С„Р°Р№Р»", show_alert=False)
        state["await_order_deliver"] = oid
        save_state(state)
        if chat_id:
            tg.send_message(
                cfg,
                chat_id,
                f"РџСЂРёС€Р»Рё С„Р°Р№Р» РґР»СЏ <code>{html.escape(oid)}</code>\n"
                f"РёР»Рё /odeliver {html.escape(oid)}",
                parse_mode="HTML",
            )
    elif status == "cancelled":
        tg.answer_callback(cfg, cq["id"], "РћС‚РјРµРЅС‘РЅ", show_alert=False)
    else:
        tg.answer_callback(cfg, cq["id"], status)
    # Р¶РёРІР°СЏ РєР°СЂС‚РѕС‡РєР° + РєРѕСЂРѕС‚РєРёР№ delta
    try:
        _push_client_order_card(cfg, item, delta=client_msgs.get(status))
    except Exception as e:
        print("push client card", e, flush=True)
    # РїСЂРё СЃРґР°С‡Рµ вЂ” Р°РєС‚ + С‡РµСЂРЅРѕРІРёРє РєРµР№СЃР° РІР»Р°РґРµР»СЊС†Сѓ
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
                    caption=f"рџ“‹ <b>РђРєС‚ РІС‹РїРѕР»РЅРµРЅРЅС‹С… СЂР°Р±РѕС‚</b>\nР·Р°РєР°Р· <code>{html.escape(oid)}</code>",
                )
        except Exception as e:
            print("act send", e, flush=True)
        try:
            import docs_lib

            case = docs_lib.build_case_draft(item)
            notify_owner(
                cfg,
                case
                + "\n\n<i>РћРїСѓР±Р»РёРєРѕРІР°С‚СЊ? РћС‚СЂРµРґР°РєС‚РёСЂСѓР№ Рё РєРёРЅСЊ /draft РёР»Рё РІ РєР°РЅР°Р» РІСЂСѓС‡РЅСѓСЋ.</i>",
            )
        except Exception as e:
            print("case draft", e, flush=True)
    # РІР»Р°РґРµР»СЊС†Сѓ вЂ” edit РєР°СЂС‚РѕС‡РєРё, Р±РµР· Р»РёС€РЅРµРіРѕ В«OKВ» РІ С‡Р°С‚
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
        tg.answer_callback(cfg, cq["id"], "Р’С‹Р±РµСЂРё СѓСЃР»СѓРіСѓ РЅРёР¶Рµ")
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
            tg.answer_callback(cfg, cq["id"], "Р—Р°РєР°Р·С‹")
            _panel(
                orders.format_owner_orders_list(items),
                orders.owner_orders_list_keyboard(items),
            )
            return True

        if sub == "clients":
            clients = orders.list_clients(limit=20)
            tg.answer_callback(cfg, cq["id"], "РљР»РёРµРЅС‚С‹")
            body = (
                f"рџ‘Ґ <b>РљР»РёРµРЅС‚С‹</b> В· {len(clients)}\n"
                f"{'в”Ѓ' * 16}\n"
                f"РџСЂРѕС„РёР»СЊ: Р·Р°РєР°Р·С‹ В· Р±Р°Р»Р°РЅСЃ В· РЅР°РїРёСЃР°С‚СЊ"
            )
            _panel(body, orders.owner_clients_keyboard(clients))
            return True

        if sub == "prof" and len(parts) >= 4:
            try:
                cuid = int(parts[3])
            except ValueError:
                tg.answer_callback(cfg, cq["id"], "bad id", show_alert=True)
                return True
            tg.answer_callback(cfg, cq["id"], "РџСЂРѕС„РёР»СЊ")
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
                tg.answer_callback(cfg, cq["id"], "РќРµ РЅР°Р№РґРµРЅ", show_alert=True)
                return True
            cuid = int(item.get("user_id") or 0)

            if sub == "open":
                # СЃР±СЂРѕСЃ РѕР¶РёРґР°РЅРёСЏ РїСЂР°РІРєРё/СЃРѕРѕР±С‰РµРЅРёСЏ
                state.setdefault("await_owner_edit_tz", {}).pop(str(uid), None)
                state.setdefault("await_owner_msg_client", {}).pop(str(uid), None)
                save_state(state)
                tg.answer_callback(cfg, cq["id"], "Р—Р°РєР°Р·")
                _panel(
                    orders.format_order_card(item, for_owner=True),
                    orders.owner_order_hub_keyboard(oid, user_id=cuid or None),
                )
                return True

            if sub == "tz":
                tg.answer_callback(cfg, cq["id"], "РўР—")
                _panel(
                    orders.format_tz_full(item),
                    {
                        "inline_keyboard": [
                            [
                                {
                                    "text": "вњЏпёЏ РџСЂР°РІРёС‚СЊ РўР—",
                                    "callback_data": f"ord:o:editz:{oid}",
                                }
                            ],
                            [
                                {
                                    "text": "В« Рљ Р·Р°РєР°Р·Сѓ",
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
                tg.answer_callback(cfg, cq["id"], "Р–РґСѓ РЅРѕРІРѕРµ РўР—")
                _panel(
                    f"вњЏпёЏ <b>РџСЂР°РІРєР° РўР—</b> <code>{html.escape(oid)}</code>\n\n"
                    f"РџСЂРёС€Р»Рё <b>РїРѕР»РЅС‹Рј СЃРѕРѕР±С‰РµРЅРёРµРј</b> РЅРѕРІС‹Р№ С‚РµРєСЃС‚ РўР—.\n"
                    f"/cancel вЂ” РѕС‚РјРµРЅР°\n\n"
                    f"<i>РЎРµР№С‡Р°СЃ:</i>\n"
                    f"{html.escape((item.get('brief') or '')[:900])}",
                    {
                        "inline_keyboard": [
                            [
                                {
                                    "text": "В« РћС‚РјРµРЅР°",
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
                tg.answer_callback(cfg, cq["id"], "Р–РґСѓ С‚РµРєСЃС‚")
                who = item.get("username") or item.get("name") or cuid
                _panel(
                    f"рџ’¬ <b>РЎРѕРѕР±С‰РµРЅРёРµ РєР»РёРµРЅС‚Сѓ</b>\n"
                    f"Р·Р°РєР°Р· <code>{html.escape(oid)}</code> В· {html.escape(str(who))}\n\n"
                    f"РќР°РїРёС€Рё С‚РµРєСЃС‚ вЂ” СѓР№РґС‘С‚ РєР»РёРµРЅС‚Сѓ РІ Р»РёС‡РєСѓ.\n"
                    f"/cancel вЂ” РѕС‚РјРµРЅР°",
                    {
                        "inline_keyboard": [
                            [
                                {
                                    "text": "В« РћС‚РјРµРЅР°",
                                    "callback_data": f"ord:o:open:{oid}",
                                }
                            ]
                        ]
                    },
                )
                return True

            if sub == "docs":
                tg.answer_callback(cfg, cq["id"], "Р”РѕРєСѓРјРµРЅС‚С‹")
                _panel(
                    f"рџ“„ <b>Р”РѕРєСѓРјРµРЅС‚С‹</b> В· <code>{html.escape(oid)}</code>\n\n"
                    f"Р’С‹Р±РµСЂРё СЃС‚РѕРїРєСѓ: РґРѕРіРѕРІРѕСЂ В· Р°РєС‚ В· РєРµР№СЃ В· РІСЃС‘ СЃСЂР°Р·Сѓ",
                    orders.owner_docs_keyboard(oid),
                )
                return True

            if sub in ("docc", "doca", "case", "docall"):
                tg.answer_callback(cfg, cq["id"], "РћС‚РїСЂР°РІР»СЏСЋвЂ¦")
                try:
                    import docs_lib

                    cpath, apath = docs_lib.write_contract_files(item)
                    if sub in ("docc", "docall") and chat_id:
                        tg.send_document(
                            cfg,
                            chat_id,
                            str(cpath),
                            caption=f"рџ“„ Р”РѕРіРѕРІРѕСЂ В· {oid}",
                        )
                    if sub in ("doca", "docall") and chat_id:
                        tg.send_document(
                            cfg, chat_id, str(apath), caption=f"рџ“‹ РђРєС‚ В· {oid}"
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
                            cfg, chat_id, f"вќЊ {html.escape(str(e)[:200])}"
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
    cb_mid = msg.get("message_id")  # РїСЂР°РІРёРј СЌС‚Рѕ СЃРѕРѕР±С‰РµРЅРёРµ, РЅРµ С€Р»С‘Рј РЅРѕРІРѕРµ

    if action == "mine":
        tg.answer_callback(cfg, cq["id"], "РСЃС‚РѕСЂРёСЏ")
        if chat_id:
            ui_edit_or_send(
                cfg,
                chat_id,
                orders.format_user_history(uid),
                reply_markup={
                    "inline_keyboard": [
                        [{"text": "рџ”„ РћР±РЅРѕРІРёС‚СЊ", "callback_data": "ord:mine"}],
                        [{"text": "рџ›  РќРѕРІС‹Р№ Р·Р°РєР°Р·", "callback_data": "ord:restart"}],
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
                tg.answer_callback(cfg, cq["id"], "Р—Р°РєР°Р· РЅРµ РЅР°Р№РґРµРЅ", show_alert=True)
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
        # РІРѕРїСЂРѕСЃ РїРѕ РїСЂРѕРµРєС‚Сѓ в†’ Р¶РґС‘Рј С‚РµРєСЃС‚, СѓР№РґС‘С‚ РІ support/owner
        oid = parts[2]
        item = orders.get_order(oid)
        if not item or int(item.get("user_id") or 0) != uid:
            tg.answer_callback(cfg, cq["id"], "Р—Р°РєР°Р· РЅРµ РЅР°Р№РґРµРЅ", show_alert=True)
            return True
        state.setdefault("await_order_question", {})[str(uid)] = {
            "order_id": oid,
            "ts": int(time.time()),
        }
        save_state(state)
        tg.answer_callback(cfg, cq["id"], "РџРёС€Рё РІРѕРїСЂРѕСЃ")
        if chat_id:
            ui_edit_or_send(
                cfg,
                chat_id,
                f"рџ’¬ <b>Р’РѕРїСЂРѕСЃ РїРѕ Р·Р°РєР°Р·Сѓ</b> <code>{html.escape(oid)}</code>\n\n"
                f"РќР°РїРёС€Рё РѕРґРЅРёРј СЃРѕРѕР±С‰РµРЅРёРµРј, С‡С‚Рѕ СѓС‚РѕС‡РЅРёС‚СЊ.\n"
                f"РћС‚РІРµС‚РёРј РІ Р»РёС‡РєСѓ.\n\n"
                f"<i>/cancel вЂ” РѕС‚РјРµРЅР°</i>",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {
                                "text": "В« Рљ РєР°СЂС‚РѕС‡РєРµ",
                                "callback_data": f"ord:status:{oid}",
                            }
                        ],
                        [{"text": "вќЊ РћС‚РјРµРЅР°", "callback_data": "ord:cancel"}],
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
            tg.answer_callback(cfg, cq["id"], "Р—Р°РєР°Р· РЅРµ РЅР°Р№РґРµРЅ", show_alert=True)
            return True
        tg.answer_callback(cfg, cq["id"], "Р“РѕС‚РѕРІР»СЋ РґРѕРєСѓРјРµРЅС‚С‹вЂ¦")
        try:
            import docs_lib

            cpath, apath = docs_lib.write_contract_files(item)
            if chat_id:
                tg.send_document(
                    cfg,
                    chat_id,
                    str(cpath),
                    caption=f"рџ“„ Р”РѕРіРѕРІРѕСЂ-РѕС„РµСЂС‚Р° В· Р·Р°РєР°Р· <code>{html.escape(oid)}</code>",
                )
                tg.send_document(
                    cfg,
                    chat_id,
                    str(apath),
                    caption=f"рџ“‹ РђРєС‚ В· Р·Р°РєР°Р· <code>{html.escape(oid)}</code>\n"
                    f"<i>РђРєС‚ Р°РєС‚СѓР°Р»РµРЅ РїРѕСЃР»Рµ СЃРґР°С‡Рё; РјРѕР¶РЅРѕ СЃРєР°С‡Р°С‚СЊ Р·Р°СЂР°РЅРµРµ.</i>",
                )
            try:
                orders.append_event(item, "docs", "РІС‹РґР°РЅС‹ РґРѕРіРѕРІРѕСЂ Рё Р°РєС‚")
            except Exception:
                pass
        except Exception as e:
            if chat_id:
                tg.send_message(
                    cfg, chat_id, f"вќЊ Р”РѕРєСѓРјРµРЅС‚С‹: {html.escape(str(e)[:200])}"
                )
        return True

    if action == "cancel":
        drafts.pop(str(uid), None)
        tg.answer_callback(cfg, cq["id"], "РћС‚РјРµРЅРµРЅРѕ")
        if chat_id:
            ui_edit_or_send(
                cfg,
                chat_id,
                "РћРє, РѕС‚РјРµРЅРµРЅРѕ.\n\n/order вЂ” РЅРѕРІС‹Р№ В· /myorders вЂ” РёСЃС‚РѕСЂРёСЏ",
                reply_markup={
                    "inline_keyboard": [
                        [{"text": "рџ›  РќРѕРІС‹Р№ Р·Р°РєР°Р·", "callback_data": "ord:restart"}],
                        [{"text": "рџ“¦ РњРѕРё Р·Р°РєР°Р·С‹", "callback_data": "ord:mine"}],
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
        tg.answer_callback(cfg, cq["id"], "Р—Р°РЅРѕРІРѕ")
        if chat_id:
            _order_start_msg(
                cfg, chat_id, state=state, uid=uid, message_id=cb_mid
            )
            save_state(state)
        return True

    if action == "type" and len(parts) >= 3:
        kind = parts[2]
        if kind not in orders.ORDER_TYPES:
            tg.answer_callback(cfg, cq["id"], "РќРµРёР·РІРµСЃС‚РЅС‹Р№ С‚РёРї", show_alert=True)
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
                f"РўРёРї: <b>{html.escape(meta['title'])}</b> В· "
                f"<b>{int(meta.get('price') or 0)} в‚Ѕ</b>\n"
                f"<i>{html.escape(meta['hint'])}</i>\n\n"
                "Р’СЃРµРіРѕ <b>4 РєРѕСЂРѕС‚РєРёС… С€Р°РіР°</b> (РјРѕР¶РЅРѕ В«РџСЂРѕРїСѓСЃС‚РёС‚СЊВ»).\n"
                "Grok СЃР°Рј СЃРѕР±РµСЂС‘С‚ РїРѕР»РЅРѕРµ РўР—.\n"
                "<i>РћРґРЅРѕ РѕРєРЅРѕ вЂ” РѕС‚РІРµС‚С‹ РїРѕРґС‡РёС‰Р°СЋС‚СЃСЏ.</i>\n\n"
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
        # РїСЂРѕРїСѓСЃРє С€Р°РіР° РѕРїСЂРѕСЃР° в†’ РґРµС„РѕР»С‚ РёР· step["skip"]
        draft = drafts.get(str(uid)) or {}
        if not draft.get("kind") or draft.get("await_confirm"):
            tg.answer_callback(cfg, cq["id"], "РќРµС‡РµРіРѕ РїСЂРѕРїСѓСЃРєР°С‚СЊ")
            return True
        if draft.get("tz_step") == "ai_clarify":
            tg.answer_callback(cfg, cq["id"], "РћРє")
            # treat as ai_skip
            action = "ai_skip"
        else:
            step_id = draft.get("tz_step") or "what"
            step = orders.tz_step(step_id)
            skip_txt = str(step.get("skip") or "РЅР° СѓСЃРјРѕС‚СЂРµРЅРёРµ")
            answers = dict(draft.get("answers") or {})
            answers[str(step.get("key") or step_id)] = skip_txt
            tg.answer_callback(cfg, cq["id"], "РџСЂРѕРїСѓСЃРє")
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
        # РїСЂРѕРїСѓСЃС‚РёС‚СЊ СѓС‚РѕС‡РЅРµРЅРёСЏ AI в†’ СЃСЂР°Р·Сѓ РїРѕРґС‚РІРµСЂР¶РґРµРЅРёРµ
        draft = drafts.get(str(uid)) or {}
        kind = draft.get("kind")
        answers = dict(draft.get("answers") or {})
        brief = draft.get("brief") or orders.build_brief_from_answers(str(kind), answers)
        review = draft.get("ai_review") or {}
        if not kind:
            tg.answer_callback(cfg, cq["id"], "РќРµС‚ С‡РµСЂРЅРѕРІРёРєР°", show_alert=True)
            return True
        tg.answer_callback(cfg, cq["id"], "РћРє")
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
            tg.answer_callback(cfg, cq["id"], "РЎРЅР°С‡Р°Р»Р° РѕРїРёС€Рё Р·Р°РґР°С‡Сѓ", show_alert=True)
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
            tg.answer_callback(cfg, cq["id"], "Р—Р°Р±Р»РѕРєРёСЂРѕРІР°РЅРѕ", show_alert=True)
            return True
        # РµС‰С‘ СЂР°Р· РїРѕ РїРѕР»РЅРѕРјСѓ РўР— (РЅР° СЃР»СѓС‡Р°Р№ РѕР±С…РѕРґР° РїРѕ С€Р°РіР°Рј)
        if apply_tz_moderation(
            cfg,
            state,
            uid=uid,
            uname=uname,
            name=name,
            brief=str(brief),
            chat_id=chat_id or uid,
        ):
            tg.answer_callback(cfg, cq["id"], "Р—Р°Р±Р»РѕРєРёСЂРѕРІР°РЅРѕ", show_alert=True)
            return True
        # РїР»Р°С‚РЅС‹Р№ Р·Р°РєР°Р· вЂ” С„РёРєСЃ. С†РµРЅР°, СЃРїРёСЃР°РЅРёРµ СЃ Р±Р°Р»Р°РЅСЃР°
        est = orders.estimate(kind, brief)
        need_pay = int(est["price"])
        if need_pay > 0:
            cur = bal.get_balance(uid)
            if cur < need_pay:
                tg.answer_callback(cfg, cq["id"], "РќРµ С…РІР°С‚Р°РµС‚ Р±Р°Р»Р°РЅСЃР°", show_alert=True)
                if chat_id:
                    ui_edit_or_send(
                        cfg,
                        chat_id,
                        f"рџ’° <b>РќРµРґРѕСЃС‚Р°С‚РѕС‡РЅРѕ СЃСЂРµРґСЃС‚РІ</b>\n\n"
                        f"РќСѓР¶РЅРѕ: <b>{need_pay}</b> в‚Ѕ\n"
                        f"Р‘Р°Р»Р°РЅСЃ: <b>{cur}</b> в‚Ѕ\n"
                        f"РќРµ С…РІР°С‚Р°РµС‚: <b>{need_pay - cur}</b> в‚Ѕ\n\n"
                        + (
                            "РџРѕРїРѕР»РЅРё в†’ /topup, РїРѕС‚РѕРј СЃРЅРѕРІР° В«РћС‚РїСЂР°РІРёС‚СЊ Р·Р°РєР°Р·В»."
                            if bal.topup_enabled(cfg)
                            else "РџРѕРїРѕР»РЅРµРЅРёРµ СЃРєРѕСЂРѕ (Platega). РўРёРєРµС‚: /support"
                        ),
                        reply_markup={
                            "inline_keyboard": (
                                [
                                    [
                                        {
                                            "text": "рџ’і РџРѕРїРѕР»РЅРёС‚СЊ",
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
                                        "text": "вњ… РћС‚РїСЂР°РІРёС‚СЊ Р·Р°РєР°Р·",
                                        "callback_data": "ord:commit",
                                    }
                                ],
                                [{"text": "вќЊ РћС‚РјРµРЅР°", "callback_data": "ord:cancel"}],
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
                note=f"Р·Р°РєР°Р· {item['id']}",
                ref=str(item["id"]),
            )
            if not ok:
                item["status"] = "cancelled"
                item["deliver_note"] = f"cancel: balance {err}"
                orders.save_order(item)
                tg.answer_callback(cfg, cq["id"], "РћРїР»Р°С‚Р° РЅРµ РїСЂРѕС€Р»Р°", show_alert=True)
                if chat_id:
                    ui_edit_or_send(
                        cfg,
                        chat_id,
                        f"вќЊ {html.escape(err)}\n"
                        + (
                            "РџРѕРїРѕР»РЅРё /topup"
                            if bal.topup_enabled(cfg)
                            else "РџРѕРїРѕР»РЅРµРЅРёРµ РІСЂРµРјРµРЅРЅРѕ РІС‹РєР». В· /support"
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
            orders.save_order(item)
        drafts.pop(str(uid), None)
        check = orders.get_order(item["id"])
        if not check:
            print("WARN order not on disk after create", item["id"], flush=True)
        try:
            orders.append_event(item, "new", "Р·Р°РєР°Р· СЃРѕР·РґР°РЅ")
            if item.get("paid_from_balance"):
                orders.append_event(
                    item, "paid", f"РѕРїР»Р°С‚Р° {item.get('paid_from_balance')} в‚Ѕ"
                )
            item = orders.get_order(item["id"]) or item
        except Exception:
            pass
        # СЂРµС„РµСЂР°Р»СЊРЅС‹Р№ Р±РѕРЅСѓСЃ РїСЂРёРіР»Р°СЃРёРІС€РµРјСѓ
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
        tg.answer_callback(cfg, cq["id"], "Р—Р°РєР°Р· СЃРѕР·РґР°РЅ")
        pay_note = ""
        if item.get("paid_from_balance"):
            pay_note = (
                f"\nрџ’° РЎРїРёСЃР°РЅРѕ: <b>{item.get('paid_from_balance')}</b> в‚Ѕ"
                f" В· РѕСЃС‚Р°С‚РѕРє {item.get('balance_after')} в‚Ѕ"
            )
        if chat_id:
            mid_card = ui_edit_or_send(
                cfg,
                chat_id,
                "вњ… <b>Р—Р°РєР°Р· РїСЂРёРЅСЏС‚</b>\n\n"
                + orders.format_order_card(item)
                + pay_note
                + "\n\nрџ“„ Р”РѕРіРѕРІРѕСЂ вЂ” РєРЅРѕРїРєРѕР№ РЅР° РєР°СЂС‚РѕС‡РєРµ.",
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
            # Р°РІС‚Рѕ-РІС‹РґР°С‡Р° РґРѕРіРѕРІРѕСЂР°
            try:
                import docs_lib

                cpath, apath = docs_lib.write_contract_files(item)
                tg.send_document(
                    cfg,
                    chat_id,
                    str(cpath),
                    caption=f"рџ“„ Р”РѕРіРѕРІРѕСЂ В· <code>{html.escape(str(item['id']))}</code>",
                )
                tg.send_document(
                    cfg,
                    chat_id,
                    str(apath),
                    caption=f"рџ“‹ РђРєС‚ (С€Р°Р±Р»РѕРЅ) В· <code>{html.escape(str(item['id']))}</code>",
                )
            except Exception as e:
                print("auto docs", e, flush=True)
            save_state(state)
        notify_owner(
            cfg,
            "рџ†• <b>РќРѕРІС‹Р№ Р·Р°РєР°Р·</b>\n\n"
            + orders.format_order_card(item, for_owner=True)
            + pay_note,
            reply_markup=orders.owner_order_keyboard(item["id"]),
        )
        return True

    tg.answer_callback(cfg, cq["id"], "ok")
    return True


def handle_giveaway_private(cfg: dict, state: dict, msg: dict) -> bool:
    """
    Р›РёС‡РєР°: /start gw_ / gwref_ + СЃРєСЂРёРЅ СЂРµРїРѕСЃС‚Р° РґСЂСѓРіСѓ.
    Р”РѕСЃС‚СѓРїРЅРѕ РІСЃРµРј (РЅРµ С‚РѕР»СЊРєРѕ owner).
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

    # 1) РїРµСЂРµСЃС‹Р»РєР° РїРѕСЃС‚Р° РєР°РЅР°Р»Р° Р±РѕС‚Сѓ вЂ” Р±РѕР»СЊС€Рµ РќР• Р·Р°СЃС‡РёС‚С‹РІР°РµРј
    if msg.get("forward_from_chat") or msg.get("forward_origin") or msg.get("forward_from_message_id"):
        item = gw.get_active(state)
        if item and item.get("status") == "active" and item.get("require_repost", True):
            mid = item.get("channel_message_id")
            link = f"https://t.me/Vaggo01/{mid}" if mid else "https://t.me/Vaggo01"
            tg.send_message(
                cfg,
                chat_id,
                "рџ“Ё Р РµРїРѕСЃС‚ РЅСѓР¶РЅРѕ СЃРґРµР»Р°С‚СЊ <b>РґСЂСѓРіСѓ</b>, РЅРµ Р±РѕС‚Сѓ.\n\n"
                f"1. РћС‚РєСЂРѕР№ РїРѕСЃС‚: {link}\n"
                "2. в†— в†’ РџРµСЂРµСЃР»Р°С‚СЊ в†’ РІС‹Р±РµСЂРё <b>РґСЂСѓРіР°</b>\n"
                "3. РЎРґРµР»Р°Р№ <b>СЃРєСЂРёРЅ</b> РїРµСЂРµСЃР»Р°РЅРЅРѕРіРѕ СЃРѕРѕР±С‰РµРЅРёСЏ\n"
                "4. РџСЂРёС€Р»Рё СЃРєСЂРёРЅ <b>СЃСЋРґР°</b> вЂ” Р·Р°СЃС‡РёС‚Р°СЋ",
                parse_mode="HTML",
                disable_preview=True,
            )
            return True
        if not owner:
            return True  # С‡СѓР¶РёРµ С„РѕСЂРІР°СЂРґС‹ РЅРµ РІ РєРѕРјР°РЅРґС‹

    # 2) СЃРєСЂРёРЅ СЂРµРїРѕСЃС‚Р° вЂ” С‚РѕР»СЊРєРѕ РµСЃР»Рё СѓР¶Рµ РІ РєРІРµСЃС‚Рµ (РЅР°Р¶Р°Р» В«РЈС‡Р°СЃС‚РІРѕРІР°С‚СЊВ»)
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
                    "РЎРЅР°С‡Р°Р»Р° РѕС‚РєСЂРѕР№ РїРѕСЃС‚ СЂРѕР·С‹РіСЂС‹С€Р° Рё РЅР°Р¶РјРё <b>В«РЈС‡Р°СЃС‚РІРѕРІР°С‚СЊВ»</b>.\n"
                    f"{link}\n\n"
                    "РџСЂРѕСЃС‚Рѕ РЅР°РїРёСЃР°С‚СЊ Р±РѕС‚Сѓ в‰  СѓС‡Р°СЃС‚РёРµ.",
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
                # 1) Р¶РёРІР°СЏ РїСЂРѕРІРµСЂРєР° РїРѕРґРїРёСЃРєРё
                entry, missing, _ = refresh_subs_and_enroll(
                    cfg, item, uid, username=uname, name=name
                )
                if item.get("require_sub", True) and missing:
                    send_quest_card(
                        cfg,
                        chat_id,
                        item,
                        entry,
                        notice="вќЊ <b>РЎРЅР°С‡Р°Р»Р° РїРѕРґРїРёСЃРєР°</b> вЂ” Р±РµР· РЅРµС‘ СЃРєСЂРёРЅ РЅРµ Р·Р°СЃС‡РёС‚Р°РµРј.\n"
                        "РќРµ С…РІР°С‚Р°РµС‚: " + html.escape(", ".join(missing[:5])),
                    )
                    return True
                # 2) РїСЂРѕРІРµСЂРєР° СЃРєСЂРёРЅР° вЂ” СЃС‚Р°С‚СѓСЃ РІ С‚РѕР№ Р¶Рµ РєР°СЂС‚РѕС‡РєРµ
                send_quest_card(
                    cfg, chat_id, item, entry, notice="рџ”Ќ <b>РџСЂРѕРІРµСЂСЏСЋ СЃРєСЂРёРЅвЂ¦</b>"
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
                    ok_scr, reason = False, f"РѕС€РёР±РєР° РїСЂРѕРІРµСЂРєРё: {e}"
                if not ok_scr:
                    low_r = (reason or "").lower()
                    auto_fail = any(
                        x in low_r
                        for x in (
                            "РЅРµ СѓРґР°Р»РѕСЃСЊ РїСЂРѕРІРµСЂРёС‚СЊ",
                            "РѕС€РёР±РєР° РїСЂРѕРІРµСЂРєРё",
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
                            "РЅРµРґРѕСЃС‚СѓРї",
                        )
                    )
                    # РЅР° РѕР±Р»Р°РєРµ Р±РµР· vision / РјС‘СЂС‚РІС‹Р№ bridge вЂ” СЂСѓС‡РЅР°СЏ РїСЂРѕРІРµСЂРєР°, РЅРµ РѕС‚С€РёРІР°РµРј
                    if auto_fail:
                        send_quest_card(
                            cfg,
                            chat_id,
                            item,
                            entry,
                            notice="вЏі <b>РЎРєСЂРёРЅ РЅР° СЂСѓС‡РЅРѕР№ РїСЂРѕРІРµСЂРєРµ</b>\n"
                            "РђРІС‚Рѕ-РїСЂРѕРІРµСЂРєР° СЃРµР№С‡Р°СЃ РЅРµРґРѕСЃС‚СѓРїРЅР°. Р’Р»Р°РґРµР»РµС† РіР»СЏРЅРµС‚ Рё Р·Р°С‡РёСЃР»РёС‚.\n"
                            f"<i>{html.escape(reason)[:100]}</i>",
                        )
                        try:
                            oid = (cfg.get("owner_user_ids") or [None])[0]
                            cap = (
                                f"вЏі РЎРєСЂРёРЅ Р¶РґСѓС‚ СЂСѓС‡РЅСѓСЋ РїСЂРѕРІРµСЂРєСѓ\n"
                                f"@{html.escape(uname) if uname else 'вЂ”'} В· "
                                f"{html.escape(name)}\n"
                                f"id <code>{uid}</code> В· gw <code>{item.get('id')}</code>\n"
                                f"{html.escape(reason)[:180]}"
                            )
                            kb = {
                                "inline_keyboard": [
                                    [
                                        {
                                            "text": "вњ… Р—Р°СЃС‡РёС‚Р°С‚СЊ СЂРµРїРѕСЃС‚",
                                            "callback_data": f"gw:okrep:{item.get('id')}:{uid}",
                                        },
                                        {
                                            "text": "вќЊ РћС‚РєР»РѕРЅРёС‚СЊ",
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
                        notice="вќЊ <b>РЎРєСЂРёРЅ РЅРµ РїСЂРёРЅСЏС‚</b>\n"
                        f"{html.escape(reason)[:140]}\n\n"
                        "РќСѓР¶РЅРѕ: Р»РёС‡РєР° СЃ <b>Р¶РёРІС‹Рј РґСЂСѓРіРѕРј</b> + РїРµСЂРµСЃР»Р°РЅРЅС‹Р№ РїРѕСЃС‚ @Vaggo01.\n"
                        "РќРµР»СЊР·СЏ: Р±РѕС‚, РР·Р±СЂР°РЅРЅРѕРµ, СЃРµР±Рµ, РїСЂРѕСЃС‚Рѕ РєР°РЅР°Р».",
                    )
                    try:
                        notify_owner(
                            cfg,
                            f"вќЊ РЎРєСЂРёРЅ РѕС‚РєР»РѕРЅС‘РЅ В· @{html.escape(uname) if uname else 'вЂ”'}\n"
                            f"{html.escape(reason)[:200]}\n"
                            f"id <code>{uid}</code>",
                            reply_markup={
                                "inline_keyboard": [
                                    [
                                        {
                                            "text": "вњ… Р’СЃС‘ Р¶Рµ Р·Р°СЃС‡РёС‚Р°С‚СЊ",
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
                # СЃРѕС…СЂР°РЅРёС‚СЊ quest_msg_id
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
                        f"вњ… <b>РЎРєСЂРёРЅ РѕРє</b> ({html.escape(reason)[:70]})\n"
                        "РџРѕРґРїРёСЃРєР° Рё СЂРµРїРѕСЃС‚ РїСЂРѕРІРµСЂРµРЅС‹ вЂ” <b>С‚С‹ РІ СЂРѕР·С‹РіСЂС‹С€Рµ!</b>"
                    )
                else:
                    gaps = gw.enrollment_gaps(item, entry)
                    notice = (
                        f"вњ… <b>РЎРєСЂРёРЅ РїСЂРёРЅСЏС‚</b> ({html.escape(reason)[:70]})\n"
                        "Р•С‰С‘: " + html.escape(", ".join(gaps) if gaps else "вЂ”")
                    )
                send_quest_card(cfg, chat_id, item, entry, notice=notice)
                try:
                    cap = (
                        f"вњ… РЎРєСЂРёРЅ РѕРє В· @{html.escape(uname) if uname else 'вЂ”'}\n"
                        f"{html.escape(name)} В· enrolled={entry.get('complete')}\n"
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
                        f"{'вњ… Р—Р°С‡РёСЃР»РµРЅ' if entry.get('complete') else 'вЏі РїСЂРѕРіСЂРµСЃСЃ'}: "
                        f"{html.escape(name)} (@{html.escape(uname) if uname else 'вЂ”'})\n"
                        f"complete={gw.entry_count(item, complete_only=True)}",
                    )
                return True
            entry, missing, just = refresh_subs_and_enroll(
                cfg, item, uid, username=uname, name=name
            )
            send_quest_card(
                cfg, chat_id, item, entry, notice="в„№пёЏ Р РµРїРѕСЃС‚ СѓР¶Рµ Р·Р°СЃС‡РёС‚Р°РЅ."
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
                tg.send_message(cfg, chat_id, "Р РѕР·С‹РіСЂС‹С€ РЅРµ Р°РєС‚РёРІРµРЅ РёР»Рё Р·Р°РєРѕРЅС‡РёР»СЃСЏ.", parse_mode=None)
                return True
            if gw.is_expired(item):
                tg.send_message(cfg, chat_id, "РЎСЂРѕРє СЂРѕР·С‹РіСЂС‹С€Р° РІС‹С€РµР».", parse_mode=None)
                return True
            if is_giveaway_excluded(cfg, user):
                tg.send_message(
                    cfg,
                    chat_id,
                    "РўРµСЃС‚РѕРІС‹Р№ / РІР»Р°РґРµР»РµС† вЂ” РІ СЂРѕР·С‹РіСЂС‹С€ РЅРµ Р·Р°С‡РёСЃР»СЏРµРј (РѕРє РґР»СЏ РїСЂРѕРІРµСЂРєРё РєРІРµСЃС‚Р°).",
                    parse_mode=None,
                )
                return True
            # РўРћР›Р¬РљРћ Р·РґРµСЃСЊ СЃРѕР·РґР°С‘Рј СѓС‡Р°СЃС‚РЅРёРєР° вЂ” СЏРІРЅС‹Р№ В«РЈС‡Р°СЃС‚РІРѕРІР°С‚СЊВ»
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
                    f"РџСЂРёРІРµС‚{', ' + html.escape(name) if name else ''}!\n"
                    "вќЊ РџРѕРґРїРёСЃРєР° РЅРµ РїРѕРґС‚РІРµСЂР¶РґРµРЅР°: "
                    + html.escape(", ".join(missing[:5]))
                )
            elif entry.get("complete"):
                notice = (
                    f"РџСЂРёРІРµС‚{', ' + html.escape(name) if name else ''}!\n"
                    "вњ… РўС‹ РІ СЂРѕР·С‹РіСЂС‹С€Рµ."
                )
            else:
                notice = (
                    f"РџСЂРёРІРµС‚{', ' + html.escape(name) if name else ''}!\n"
                    "РџСЂРѕС…РѕР¶Сѓ С€Р°РіРё вЂ” РІ РєРѕРЅРєСѓСЂСЃ РїРѕСЃР»Рµ РїРѕРґРїРёСЃРєРё + СЂРµРїРѕСЃС‚Р°."
                )
            send_quest_card(cfg, chat_id, item, entry, notice=notice)
            if just:
                notify_owner(
                    cfg,
                    f"вњ… Р—Р°С‡РёСЃР»РµРЅ: {html.escape(name)} "
                    f"(@{html.escape(uname) if uname else 'вЂ”'})\n"
                    f"complete={gw.entry_count(item, complete_only=True)}",
                )
            return True
        # plain /start вЂ” РќР• СѓС‡Р°СЃС‚РЅРёРє; owner РЅРµ РїРµСЂРµС…РІР°С‚С‹РІР°РµРј (РјРµРЅРµРґР¶РµСЂ)
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
            # РѕРґРЅРѕ РєРѕСЂРѕС‚РєРѕРµ СЃРѕРѕР±С‰РµРЅРёРµ; РЅРµ СЃРѕР·РґР°С‘Рј entry
            tg.send_message(
                cfg,
                chat_id,
                "Р‘РѕС‚ РєР°РЅР°Р»Р° @Vaggo01.\n\n"
                "РЈС‡Р°СЃС‚РёРµ: РїРѕСЃС‚ в†’ <b>В«РЈС‡Р°СЃС‚РІРѕРІР°С‚СЊВ»</b>\n"
                f"{link}",
                parse_mode="HTML",
                disable_preview=True,
            )
            return True

    # 4) non-owner other messages вЂ” С‚РѕР»СЊРєРѕ РєРІРµСЃС‚, Р±РµР· С‡СѓР¶РёС… РєРѕРјР°РЅРґ
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
                    "РўС‹ РЅРµ РІ РєРІРµСЃС‚Рµ. РџРѕСЃС‚ в†’ <b>В«РЈС‡Р°СЃС‚РІРѕРІР°С‚СЊВ»</b>\n" + link,
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
                    tip=f"РџРµСЂРµС€Р»Рё РїРѕСЃС‚ РґСЂСѓРіСѓ: {link}\nРџРѕС‚РѕРј СЃРєСЂРёРЅ СЃСЋРґР°.",
                )
            else:
                send_quest_card(cfg, chat_id, item, entry)
            return True
        # РЅРµС‚ СЂРѕР·С‹РіСЂС‹С€Р° вЂ” С‡СѓР¶РёРј Р·Р°РєСЂС‹С‚Рѕ
        tg.send_message(
            cfg,
            chat_id,
            "Р­С‚Рѕ Р±РѕС‚ РєР°РЅР°Р»Р° @Vaggo01. РЎРµР№С‡Р°СЃ РЅРµС‚ Р°РєС‚РёРІРЅРѕРіРѕ РєРІРµСЃС‚Р°.\n"
            "РљР°РЅР°Р»: https://t.me/Vaggo01",
            parse_mode=None,
        )
        return True

    return False


def handle_giveaway_callback(cfg: dict, state: dict, cq: dict) -> bool:
    """РљРЅРѕРїРєРё РєРІРµСЃС‚Р° + РёРЅС„Рѕ РЅР° РїРѕСЃС‚Рµ."""
    data = cq.get("data") or ""
    if not data.startswith("gw:"):
        return False
    try:
        return _handle_giveaway_callback_inner(cfg, state, cq)
    except Exception as e:
        print("gw callback error", e, flush=True)
        try:
            tg.answer_callback(cfg, cq["id"], "РћС€РёР±РєР°, РїРѕРїСЂРѕР±СѓР№ РµС‰С‘ СЂР°Р·", show_alert=True)
        except Exception:
            pass
        return True


def _handle_giveaway_callback_inner(cfg: dict, state: dict, cq: dict) -> bool:
    data = cq.get("data") or ""
    parts = data.split(":")
    if len(parts) < 3:
        tg.answer_callback(cfg, cq["id"], "РћС€РёР±РєР°")
        return True
    action, gid = parts[1], parts[2]
    # РІСЃРµРіРґР° СЃРІРµР¶РёР№ store (media/giveaways.json + state)
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
            "Р РѕР·С‹РіСЂС‹С€ РЅРµ РЅР°Р№РґРµРЅ РЅР° СЃРµСЂРІРµСЂРµ. Р’Р»Р°РґРµР»РµС†: /gstatus РёР»Рё /gpost Р·Р°РЅРѕРІРѕ.",
            show_alert=True,
        )
        return True

    if action == "ended":
        tg.answer_callback(cfg, cq["id"], "Р РѕР·С‹РіСЂС‹С€ СѓР¶Рµ Р·Р°РІРµСЂС€С‘РЅ", show_alert=True)
        return True

    # РІР»Р°РґРµР»РµС†: РІСЂСѓС‡РЅСѓСЋ Р·Р°СЃС‡РёС‚Р°С‚СЊ / РѕС‚РєР»РѕРЅРёС‚СЊ СЂРµРїРѕСЃС‚
    if action in ("okrep", "norep") and len(parts) >= 4:
        if not is_owner(cfg, user):
            tg.answer_callback(cfg, cq["id"], "РўРѕР»СЊРєРѕ РІР»Р°РґРµР»РµС†", show_alert=True)
            return True
        try:
            target_uid = int(parts[3])
        except ValueError:
            tg.answer_callback(cfg, cq["id"], "bad id", show_alert=True)
            return True
        if not item or item.get("status") != "active":
            tg.answer_callback(cfg, cq["id"], "РќРµС‚ Р°РєС‚РёРІРЅРѕРіРѕ СЂРѕР·С‹РіСЂС‹С€Р°", show_alert=True)
            return True
        if action == "norep":
            tg.answer_callback(cfg, cq["id"], "РћС‚РєР»РѕРЅРµРЅРѕ")
            try:
                tg.send_message(
                    cfg,
                    target_uid,
                    "вќЊ РЎРєСЂРёРЅ РЅРµ РїСЂРёРЅСЏС‚ РІР»Р°РґРµР»СЊС†РµРј.\n"
                    "РќСѓР¶РµРЅ СЂРµРїРѕСЃС‚ РїРѕСЃС‚Р° @Vaggo01 <b>Р¶РёРІРѕРјСѓ РґСЂСѓРіСѓ</b> + РЅРѕРІС‹Р№ СЃРєСЂРёРЅ.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
            return True
        # okrep
        entry = gw.ensure_entry(item, user_id=target_uid)
        gw.set_repost_ok(item, target_uid, True)
        e = gw.get_entry(item, target_uid) or entry
        e["repost_verify_reason"] = "Р·Р°СЃС‡РёС‚Р°РЅРѕ РІСЂСѓС‡РЅСѓСЋ РІР»Р°РґРµР»СЊС†РµРј"
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
                notice = "вњ… Р РµРїРѕСЃС‚ Р·Р°СЃС‡РёС‚Р°РЅ РІСЂСѓС‡РЅСѓСЋ вЂ” <b>С‚С‹ РІ СЂРѕР·С‹РіСЂС‹С€Рµ!</b>"
            else:
                gaps = gw.enrollment_gaps(item, entry)
                notice = (
                    "вњ… Р РµРїРѕСЃС‚ Р·Р°СЃС‡РёС‚Р°РЅ РІСЂСѓС‡РЅСѓСЋ.\nР•С‰С‘: "
                    + html.escape(", ".join(gaps) if gaps else "вЂ”")
                )
            send_quest_card(cfg, target_uid, item, entry, notice=notice)
        except Exception as e:
            print("okrep dm", e, flush=True)
        return True

    if action == "rules":
        prize = str((item or {}).get("prize") or "Google AI Pro 18 РјРµСЃ")[:50]
        inv = int((item or {}).get("require_invites") or 0)
        short = (
            f"РџСЂРёР·: {prize}\n"
            f"1) РџРѕРґРїРёСЃРєР° @Vaggo01\n"
            f"2) Р РµРїРѕСЃС‚ РґСЂСѓРіСѓ + СЃРєСЂРёРЅ (Р±РѕС‚ РїСЂРѕРІРµСЂРёС‚)\n"
        )
        if inv > 0:
            short += f"3) {inv} РґСЂСѓРі(Р°)\n"
        short += "в†’ В«РЈС‡Р°СЃС‚РІРѕРІР°С‚СЊВ»"
        tg.answer_callback(cfg, cq["id"], short[:200], show_alert=True)
        return True

    if action == "count":
        # СЃС‚Р°С‚РёСЃС‚РёРєР° С‚РѕР»СЊРєРѕ РІР»Р°РґРµР»СЊС†Сѓ
        if is_owner(cfg, user) and item:
            n = gw.entry_count(item, complete_only=True)
            t = gw.entry_count(item, complete_only=False)
            tg.answer_callback(
                cfg, cq["id"], f"(С‚РѕР»СЊРєРѕ С‚С‹) complete={n} В· РЅР°С‡Р°Р»Рё={t}", show_alert=True
            )
        else:
            tg.answer_callback(cfg, cq["id"], "Р–РјРё В«РЈС‡Р°СЃС‚РІРѕРІР°С‚СЊВ»", show_alert=True)
        return True

    if action == "join":
        tg.answer_callback(
            cfg, cq["id"], "РћС‚РєСЂРѕР№ РєРЅРѕРїРєСѓ В«РЈС‡Р°СЃС‚РІРѕРІР°С‚СЊВ» РµС‰С‘ СЂР°Р·", show_alert=True
        )
        return True

    # --- quest steps (private) ---
    if not item or item.get("status") != "active":
        tg.answer_callback(cfg, cq["id"], "Р РѕР·С‹РіСЂС‹С€ РЅРµ Р°РєС‚РёРІРµРЅ", show_alert=True)
        return True
    if gw.is_expired(item):
        tg.answer_callback(cfg, cq["id"], "РЎСЂРѕРє РІС‹С€РµР»", show_alert=True)
        return True

    # РєРІРµСЃС‚-РєРЅРѕРїРєРё С‚РѕР»СЊРєРѕ РґР»СЏ С‚РµС…, РєС‚Рѕ СѓР¶Рµ РЅР°Р¶Р°Р» В«РЈС‡Р°СЃС‚РІРѕРІР°С‚СЊВ»
    if action in ("chksub", "rephow", "inv", "prog"):
        if not gw.get_entry(item, uid):
            tg.answer_callback(
                cfg,
                cq["id"],
                "РЎРЅР°С‡Р°Р»Р° В«РЈС‡Р°СЃС‚РІРѕРІР°С‚СЊВ» РЅР° РїРѕСЃС‚Рµ СЂРѕР·С‹РіСЂС‹С€Р°",
                show_alert=True,
            )
            return True

    entry = gw.get_entry(item, uid) or {}

    if action == "chksub":
        # СЃРѕС…СЂР°РЅРёС‚СЊ id РєР°СЂС‚РѕС‡РєРё РёР· callback (С‚Рѕ СЃРѕРѕР±С‰РµРЅРёРµ, РЅР° РєРѕС‚РѕСЂРѕРј РєРЅРѕРїРєР°)
        if msg.get("message_id") and not entry.get("quest_msg_id"):
            entry["quest_msg_id"] = int(msg["message_id"])
        entry, missing, just = refresh_subs_and_enroll(
            cfg, item, uid, username=uname, name=name
        )
        if entry.get("quest_msg_id") is None and msg.get("message_id"):
            entry["quest_msg_id"] = int(msg["message_id"])
        if not missing:
            if entry.get("complete"):
                tg.answer_callback(cfg, cq["id"], "РџРѕРґРїРёСЃРєР° РѕРє В· РІ СЂРѕР·С‹РіСЂС‹С€Рµ!", show_alert=False)
                notice = "вњ… РџРѕРґРїРёСЃРєР° РѕРє вЂ” <b>С‚С‹ РІ СЂРѕР·С‹РіСЂС‹С€Рµ!</b>"
            else:
                gaps = gw.enrollment_gaps(item, entry)
                tg.answer_callback(
                    cfg, cq["id"], ("РџРѕРґРїРёСЃРєР° РѕРє. Р•С‰С‘: " + ", ".join(gaps))[:200], show_alert=False
                )
                notice = "вњ… РџРѕРґРїРёСЃРєР° РѕРє. Р•С‰С‘: " + html.escape(", ".join(gaps))
        else:
            tg.answer_callback(
                cfg, cq["id"], ("РќРµС‚ РїРѕРґРїРёСЃРєРё: " + ", ".join(missing[:5]))[:200], show_alert=False
            )
            notice = "вќЊ РќРµС‚ РїРѕРґРїРёСЃРєРё: " + html.escape(", ".join(missing[:5]))
        if chat_id:
            send_quest_card(cfg, chat_id, item, entry, notice=notice)
        if just:
            notify_owner(
                cfg,
                f"вњ… Р—Р°С‡РёСЃР»РµРЅ: {html.escape(name)}\n"
                f"complete={gw.entry_count(item, complete_only=True)}",
            )
        return True

    if action == "rephow":
        if msg.get("message_id"):
            entry["quest_msg_id"] = int(msg["message_id"])
        mid = item.get("channel_message_id")
        link = f"https://t.me/Vaggo01/{mid}" if mid else "https://t.me/Vaggo01"
        tg.answer_callback(cfg, cq["id"], "РЎРјРѕС‚СЂРё РїРѕРґСЃРєР°Р·РєСѓ РЅР° РєР°СЂС‚РѕС‡РєРµ", show_alert=False)
        if chat_id:
            send_quest_card(
                cfg,
                chat_id,
                item,
                entry,
                tip=(
                    f"рџ“Ё <b>РљР°Рє СЃРґРµР»Р°С‚СЊ СЂРµРїРѕСЃС‚</b>\n"
                    f"1. {link}\n"
                    f"2. в†— РџРµСЂРµСЃР»Р°С‚СЊ в†’ <b>Р¶РёРІРѕР№ РґСЂСѓРі</b> (С‡РµР»РѕРІРµРє)\n"
                    f"3. РЎРєСЂРёРЅ С‡Р°С‚Р° (РІРёРґРЅР° С€Р°РїРєР° + В«РїРµСЂРµСЃР»Р°РЅРѕВ») в†’ СЃСЋРґР°\n\n"
                    f"вќЊ РќРµР»СЊР·СЏ: Р±РѕС‚, РР·Р±СЂР°РЅРЅРѕРµ, СЃРµР±Рµ, СЂР°РЅРґРѕРј Р±РµР· РґСЂСѓРіР°.\n"
                    f"Р‘РѕС‚ СЃРјРѕС‚СЂРёС‚ СЃРєСЂРёРЅ СЃС‚СЂРѕРіРѕ."
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
        tg.answer_callback(cfg, cq["id"], f"Р”СЂСѓР·СЊСЏ: {have}/{need}", show_alert=False)
        if chat_id:
            send_quest_card(
                cfg,
                chat_id,
                item,
                entry,
                tip=(
                    f"рџ‘Ґ Р”СЂСѓР·СЊСЏ: <b>{have}/{need}</b>\n"
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
            tg.answer_callback(cfg, cq["id"], "Р’ СЂРѕР·С‹РіСЂС‹С€Рµ вњ…", show_alert=False)
            notice = "вњ… <b>РўС‹ РІ СЂРѕР·С‹РіСЂС‹С€Рµ</b>"
        elif missing:
            tg.answer_callback(
                cfg, cq["id"], ("РќРµС‚ РїРѕРґРїРёСЃРєРё: " + ", ".join(missing[:4]))[:200], show_alert=False
            )
            notice = "вќЊ " + html.escape(", ".join(missing[:4]))
        else:
            gaps = gw.enrollment_gaps(item, entry)
            tg.answer_callback(
                cfg,
                cq["id"],
                ("Р•С‰С‘: " + ", ".join(gaps))[:200] if gaps else "РћР±РЅРѕРІР»РµРЅРѕ",
                show_alert=False,
            )
            notice = "рџ”„ РћР±РЅРѕРІР»РµРЅРѕ" + (
                ". Р•С‰С‘: " + html.escape(", ".join(gaps)) if gaps else ""
            )
        if chat_id:
            send_quest_card(cfg, chat_id, item, entry, notice=notice)
        if just:
            notify_owner(
                cfg,
                f"вњ… Р—Р°С‡РёСЃР»РµРЅ: {html.escape(name)}\n"
                f"complete={gw.entry_count(item, complete_only=True)}",
            )
        return True

    tg.answer_callback(cfg, cq["id"], "ok")
    return True


def maybe_handle_giveaway_entry(cfg: dict, state: dict, msg: dict) -> bool:
    """Р—Р°СЃС‡РёС‚Р°С‚СЊ СѓС‡Р°СЃС‚РЅРёРєР° СЂРѕР·С‹РіСЂС‹С€Р° РёР· РєРѕРјРјРµРЅС‚Р°. True = РґР°Р»СЊС€Рµ AI-РѕС‚РІРµС‚ РЅРµ РЅСѓР¶РµРЅ."""
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
    # С‚РѕР»СЊРєРѕ С‚СЂРµРґ РїРѕСЃС‚Р° СЂРѕР·С‹РіСЂС‹С€Р° (РµСЃР»Рё РїРѕСЃС‚ РїСЂРёРІСЏР·Р°РЅ)
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
        return False  # С‚РѕР»СЊРєРѕ РєРЅРѕРїРєР°
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
    marker = item.get("marker") or "рџЋЇ"
    if reason == "no_marker":
        # РЅРµ РЅР°С€ СЂРѕР·С‹РіСЂС‹С€-РєРѕРјРјРµРЅС‚ вЂ” РїСѓСЃС‚СЊ РѕР±С‹С‡РЅС‹Р№ AI
        return False
    if reason == "too_short":
        try:
            tg.send_message(
                cfg,
                chat_id,
                f"Р§С‚РѕР±С‹ СѓС‡Р°СЃС‚РІРѕРІР°С‚СЊ: 1вЂ“2 РїСЂРµРґР»РѕР¶РµРЅРёСЏ + {marker}",
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
                tg.set_message_reaction(cfg, chat_id, int(mid), marker if marker in ("рџЋЇ", "рџ”Ґ", "вќ¤", "рџ‘Ќ") else "рџ”Ґ")
        except Exception:
            pass
        return True
    if ok:
        try:
            if mid:
                tg.set_message_reaction(cfg, chat_id, int(mid), "рџ”Ґ")
        except Exception:
            pass
        try:
            tg.send_message(
                cfg,
                chat_id,
                f"РЈС‡Р°СЃС‚РёРµ Р·Р°СЃС‡РёС‚Р°РЅРѕ вњ… ({gw.entry_count(item)} С‡РµР».)",
                reply_to=mid,
                parse_mode=None,
                message_thread_id=int(thread_id) if thread_id else None,
            )
        except Exception as e:
            print("giveaway ack fail", e, flush=True)
        notify_owner(
            cfg,
            f"рџЋџ <b>РЈС‡Р°СЃС‚РЅРёРє СЂРѕР·С‹РіСЂС‹С€Р°</b> В· {gw.entry_count(item)} РІСЃРµРіРѕ\n"
            f"РћС‚: {html.escape(name)}"
            + (f" (@{html.escape(uname)})" if uname else "")
            + f"\n<code>{uid}</code>\n"
            f"<i>{html.escape(text[:300])}</i>\n"
            f"/gentries В· /gdraw",
        )
        print(f"giveaway entry uid={uid} total={gw.entry_count(item)}", flush=True)
        return True
    return False


def maybe_moderate_discussion(cfg: dict, msg: dict) -> bool:
    """
    РњРѕРґРµСЂР°С‚РѕСЂ С‡Р°С‚Р°: РР в†’ СѓРґР°Р»РёС‚СЊ + РІР°СЂРЅ/РјСѓС‚/Р±Р°РЅ.
    True = РѕР±СЂР°Р±РѕС‚Р°РЅРѕ (РѕР±С‹С‡РЅС‹Р№ AI-РѕС‚РІРµС‚ РЅРµ С€Р»С‘Рј).
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

    # owner: РїРѕ СѓРјРѕР»С‡Р°РЅРёСЋ РўРћР–Р• РјРѕРґРµСЂРёСЂСѓРµРј (РёРЅР°С‡Рµ С‚РµСЃС‚С‹ В«СЃ СЃРµР±СЏВ» РЅРµ СЂР°Р±РѕС‚Р°СЋС‚).
    # РРјРјСѓРЅРёС‚РµС‚: chat_mod_skip_owner=true
    owner_here = is_owner(cfg, user)
    if owner_here and cfg.get("chat_mod_skip_owner", False):
        print("chatmod skip owner (config)", uid, flush=True)
        return False

    mid = msg.get("message_id")
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not text:
        return False

    # РїРµСЂРјР°С‡ вЂ” СЃРЅРµСЃС‚Рё Рё РІСЃС‘
    if chatmod.is_chat_banned(uid):
        ok_del = tg.try_delete_message(cfg, chat_id, mid) if mid else False
        print(f"chatmod banned-user del={ok_del} mid={mid}", flush=True)
        return True

    # РР-РІРµСЂРґРёРєС‚
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

    # 1) СѓРґР°Р»РёС‚СЊ РЎР РђР—РЈ
    ok_del = False
    if mid:
        ok_del = tg.try_delete_message(cfg, chat_id, mid)
        if not ok_del:
            # РїРѕРІС‚РѕСЂ С‡РµСЂРµР· raw api
            try:
                tg.delete_message(cfg, chat_id, int(mid))
                ok_del = True
            except Exception as e:
                print("chatmod DELETE FAIL", mid, e, flush=True)

    # 2) Р»РµСЃС‚РЅРёС†Р° (owner РЅРµ РјСѓС‚РёРј/Р±Р°РЅРёРј Р¶С‘СЃС‚РєРѕ вЂ” С‚РѕР»СЊРєРѕ delete+warn РІ Р»РѕРі)
    if owner_here:
        res = {
            "action": "warn",
            "warnings": "вЂ”",
            "mutes": "вЂ”",
            "week_bans": "вЂ”",
            "public_text": f"вљ пёЏ РЎРѕРѕР±С‰РµРЅРёРµ СЃРЅСЏС‚Рѕ (С‚РѕРєСЃРёРє). Owner вЂ” Р±РµР· РјСѓС‚Р°.",
            "private_text": "РўРµСЃС‚ РјРѕРґРµСЂР°С†РёРё: СЃРѕРѕР±С‰РµРЅРёРµ СѓРґР°Р»РµРЅРѕ. Р›РµСЃС‚РЅРёС†Р° РЅР° owner РЅРµ РєР°РїР°РµС‚.",
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

    # 3) РїСѓР±Р»РёС‡РЅРѕ
    thr = msg.get("message_thread_id")
    try:
        tg.send_message(
            cfg,
            chat_id,
            res.get("public_text") or "вљ пёЏ",
            parse_mode=None,
            message_thread_id=int(thr) if thr else None,
            disable_preview=True,
        )
    except Exception as e:
        print("chatmod public fail", e, flush=True)

    # 4) Р›РЎ
    try:
        tg.send_message(
            cfg,
            uid,
            res.get("private_text") or "вљ пёЏ РќР°СЂСѓС€РµРЅРёРµ РІ С‡Р°С‚Рµ Vaggo.",
            parse_mode=None,
            disable_preview=True,
        )
    except Exception:
        pass

    # 5) owner notify (РµСЃР»Рё РЅРµ СЃР°Рј owner)
    if not owner_here:
        try:
            notify_owner(
                cfg,
                f"рџ›Ў <b>РњРѕРґРµСЂР°С†РёСЏ</b> В· {html.escape(str(action))}\n"
                f"del={'ok' if ok_del else 'FAIL'}\n"
                f"РћС‚: {html.escape(name)}"
                + (f" (@{html.escape(uname)})" if uname else "")
                + f"\n<code>{uid}</code>\n"
                f"РџСЂРёС‡РёРЅР°: {html.escape(str(res.get('reason_h') or reason))}\n"
                f"РЎС‚Р°С‚СѓСЃ: вљ {res.get('warnings')}/4 В· РјСѓС‚ {res.get('mutes')}/3 В· "
                f"РЅРµРґ {res.get('week_bans')}/2\n"
                f"<i>{html.escape(text[:200])}</i>\n"
                f"/modstat {uid} В· /modpardon {uid}",
            )
        except Exception:
            pass

    print(
        f"chatmod DONE action={action} del={ok_del} uid={uid} reason={reason}",
        flush=True,
    )
    return True


def maybe_handle_discussion(cfg: dict, state: dict, msg: dict) -> None:
    """РљРѕРјРјРµРЅС‚С‹: РјРѕРґРµСЂР°С†РёСЏ в†’ СЂРѕР·С‹РіСЂС‹С€ в†’ Grok. Р’ С„РѕРЅРµ вЂ” polling РЅРµ Р±Р»РѕРєРёСЂСѓРµС‚СЃСЏ."""
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    disc = cfg.get("discussion_group_id") or 0
    if not disc or chat_id != disc:
        return
    if msg.get("is_automatic_forward"):
        return
    # СЃРѕРѕР±С‰РµРЅРёРµ РєР°РЅР°Р»Р°-РїРµСЂРµСЃР»Р°Р»РєР° Р±РµР· Р°РІС‚РѕСЂР°-С‡РµР»РѕРІРµРєР°
    if msg.get("sender_chat") and (msg.get("sender_chat") or {}).get("type") == "channel":
        if not msg.get("from"):
            return
    user = msg.get("from") or {}
    if user.get("is_bot"):
        return
    # РјРѕРґРµСЂР°С‚РѕСЂ РїРµСЂРІС‹Рј
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

    # СЂРѕР·С‹РіСЂС‹С€ вЂ” РґРѕ rate-limit AI
    try:
        if maybe_handle_giveaway_entry(cfg, load_state(), msg):
            return
    except Exception as e:
        print("giveaway entry error", e, flush=True)

    if not cfg.get("auto_reply_comments", True) and not cfg.get("auto_react_comments", True):
        return
    # РЅРµ РѕС‚РІРµС‡Р°С‚СЊ СЃР°РјРѕРјСѓ СЃРµР±Рµ / СЃР»СѓР¶РµР±РЅРѕРјСѓ
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

    # СЂРµР°РєС†РёСЏ СЃСЂР°Р·Сѓ (Р»С‘РіРєР°СЏ) вЂ” РЅРµ РІР°Р»РёРј РѕС‚РІРµС‚, РµСЃР»Рё СЂРµР°РєС†РёСЏ РЅРµРґРѕСЃС‚СѓРїРЅР°
    if cfg.get("auto_react_comments", True) and mid:
        try:
            tg.set_message_reaction(cfg, chat_id, int(mid), pick_reaction_for_text(text))
        except Exception as e:
            # С‡Р°СЃС‚Рѕ: РЅРµС‚ РїСЂР°РІ / privacy / message not found вЂ” РєРѕРјРјРµРЅС‚ РІСЃС‘ СЂР°РІРЅРѕ РѕС‚РІРµС‡Р°РµРј
            print("comment react fail", str(e)[:120], flush=True)

    # instant РїРѕ СѓРјРѕР»С‡Р°РЅРёСЋ (РµСЃР»Рё РЅРµ РІС‹РєР»СЋС‡РµРЅРѕ СЏРІРЅРѕ)
    can_reply = bool(cfg.get("auto_reply_comments", True))
    instant = can_reply and not bool(cfg.get("comment_needs_owner_ok", False))
    if not can_reply:
        return

    def work():
        try:
            thr = int(thread_id) if thread_id else None
            # В«РїРµС‡Р°С‚Р°РµС‚вЂ¦В» СЃСЂР°Р·Сѓ
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
            # РїРѕСЃС‚ / С‚СЂРµРґ РєР°Рє Р¤РћРќ: reply chain + top of thread
            post_ctx = ""
            rt = msg.get("reply_to_message") or {}
            if rt:
                post_ctx = (rt.get("text") or rt.get("caption") or "")[:700]
                if not post_ctx and rt.get("reply_to_message"):
                    rr = rt["reply_to_message"]
                    post_ctx = (rr.get("text") or rr.get("caption") or "")[:700]
                # Р°РІС‚Рѕ-С„РѕСЂРІР°СЂРґ РїРѕСЃС‚Р° РєР°РЅР°Р»Р°
                if not post_ctx and (
                    rt.get("is_automatic_forward") or rt.get("forward_from_message_id")
                ):
                    post_ctx = (rt.get("text") or rt.get("caption") or "")[:700]

            def _send_comment_reply(body: str) -> dict:
                """Reply РЅР° РєРѕРјРјРµРЅС‚ (РїРѕРґ РїРѕСЃС‚РѕРј). Р•СЃР»Рё target РїСЂРѕРїР°Р» вЂ” РІСЃС‘ СЂР°РІРЅРѕ РѕС‚РїСЂР°РІРёРј, РЅРµ РјРѕР»С‡РёРј."""
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

            # stub В«вЂ¦В» Р’Р«РљР› РїРѕ СѓРјРѕР»С‡Р°РЅРёСЋ (С‚РѕС‡РєРё Р±РµСЃРёР»Рё + Р»РѕРјР°Р»Рё reply)
            stub_mid = None
            use_stub = bool(cfg.get("comment_stub_then_edit", False)) and instant
            if use_stub:
                try:
                    from content import try_instant_comment

                    stub = try_instant_comment(text, username=str(uname))
                    if stub:
                        # РїРѕР»РЅС‹Р№ instant вЂ” РѕРґРёРЅ reply, Р±РµР· Grok
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
                    # РїР»РµР№СЃС…РѕР»РґРµСЂ С‚РѕР»СЊРєРѕ РµСЃР»Рё СЏРІРЅРѕ РІРєР»СЋС‡С‘РЅ stub
                    r0 = _send_comment_reply("вЏі")
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
                reply = "Р™Рѕ, СЏ С‚СѓС‚ рџ”Ґ"
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
                        # fallback: Р±РµР· thread, РЅРѕ Р’РЎР•Р“Р”Рђ reply_to
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
                        f"рџ’¬ <b>РћС‚РІРµС‚РёР» РІ РєРѕРјРјРµРЅС‚С‹</b>\n"
                        f"РћС‚: {html.escape(str(uname))}\n"
                        f"<i>{html.escape(text[:280])}</i>\n\n"
                        f"в†’ {html.escape(reply[:280])}",
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
                    f"рџ’¬ РљРѕРјРјРµРЅС‚ в†’ <code>{item['id']}</code>\n"
                    f"РћС‚: {html.escape(str(uname))}\n"
                    f"<i>{html.escape(text[:400])}</i>\n\n"
                    f"<b>РћС‚РІРµС‚:</b>\n{html.escape(reply)}",
                    reply_markup=comment_keyboard(item["id"]),
                )
        except Exception as e:
            print("comment work fail", e, flush=True)
            traceback.print_exc()
            try:
                notify_owner(
                    cfg,
                    f"вќЊ РќРµ СЃРјРѕРі РѕС‚РІРµС‚РёС‚СЊ РІ РєРѕРјРјРµРЅС‚С‹\n"
                    f"РћС‚: {html.escape(str(uname))}\n"
                    f"<i>{html.escape(text[:200])}</i>\n"
                    f"{html.escape(str(e)[:300])}",
                )
            except Exception:
                pass

    threading.Thread(target=work, daemon=True).start()


def run() -> None:
    # Р”РѕРјР°С€РЅРёР№ РџРљ (Windows): РЅРµ polling'РёС‚СЊ, РµСЃР»Рё РѕР±Р»Р°РєРѕ РґРѕР»Р¶РЅРѕ РєСЂСѓС‚РёС‚СЊ Р±РѕС‚Р°.
    # РќР° Bothost (Linux) СЌС‚РѕС‚ gate РќР• СЃСЂР°Р±Р°С‚С‹РІР°РµС‚ вЂ” РёРЅР°С‡Рµ Р±РѕС‚ РјРѕР»С‡РёС‚!
    try:
        import os as _os

        cfg0 = load_config()
        is_home_windows = _os.name == "nt"
        cloudish = bool(cfg0.get("local_bot_disabled")) or str(
            cfg0.get("bot_host_mode") or ""
        ).lower() in ("cloud", "bothost", "hosting")
        if is_home_windows and cloudish:
            msg = (
                "Р›РѕРєР°Р»СЊРЅС‹Р№ bot.py Р’Р«РљР›Р®Р§Р•Рќ (Windows + cloud/local_bot_disabled).\n"
                "РљСЂСѓС‚РёС‚СЃСЏ Bothost. Р›РѕРєР°Р»СЊРЅРѕ: local_bot_disabled=false, bot_host_mode=local."
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

    # Р±Р°Р»Р°РЅСЃ: seed СЃ GitHub (Bothost) + remote home-bridge РµСЃР»Рё Р¶РёРІ
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

    # РѕРґРёРЅ bot.py вЂ” РёРЅР°С‡Рµ Telegram 409 Conflict
    try:
        from single_instance import acquire_lock

        acquire_lock("bot")
    except SystemExit:
        raise
    except Exception as e:
        print("lock fail", e)

    # Р»РѕРі РІ С„Р°Р№Р» вЂ” С‡С‚РѕР±С‹ РІРёРґРµС‚СЊ, РїРѕС‡РµРјСѓ РјРѕР»С‡РёС‚
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
        print("РћРЁРР‘РљРђ: bot_token РїСѓСЃС‚РѕР№")
        sys.exit(1)

    me = tg.get_me(cfg)
    print(f"Р‘РѕС‚: @{me.get('username')} id={me.get('id')}")
    print(f"CODE_VERSION={BOT_CODE_VERSION}", flush=True)

    import os as _os

    on_bothost = bool((_os.environ.get("BOT_ID") or "").strip())

    # 3.0: Р’РЎР•Р“Р”Рђ РїРѕРґС‚СЏРЅСѓС‚СЊ СѓС‡Р°СЃС‚РЅРёРєРѕРІ РёР· giveaway_restore.json (force merge)
    try:
        res_gw = gw.apply_restore_seed(force=True)
        print("giveaway restore 3.0:", res_gw, flush=True)
    except Exception as e:
        print("giveaway restore fail", e, flush=True)

    # РїСЂРё СЃС‚Р°СЂС‚Рµ РЅР° Bothost вЂ” СЃРІРµСЂРёС‚СЊ SHA Рё РїРѕРґС‚СЏРЅСѓС‚СЊ РєРѕРґ
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

    # Р°РІС‚Рѕ-pull СЃ GitHub (Bothost Р±РµР· РєРЅРѕРїРєРё auto-deploy)
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
                            "рџ”„ <b>Auto-pull</b>\n"
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
    # РјРѕР·Рі / РјРѕСЃС‚ вЂ” СЃСЂР°Р·Сѓ РІ Р»РѕРі Bothost
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
        # РћРґРЅРѕ РјРµРЅСЋ 4.0 РїСЂРё СЃС‚Р°СЂС‚Рµ
        try:
            st_ui = load_state()
            uid_boot = int((cfg.get("owner_user_ids") or [5740061551])[0])
            mode = str(cfg.get("bot_host_mode") or "local")
            src = html.escape(str(st.get("grok_source") or "вЂ”"))
            _owner_panel(
                cfg,
                st_ui,
                uid_boot,
                None,
                uid_boot,
                owner_home_html()
                + f"\n\nрџџў <b>online {BOT_CODE_VERSION}</b> В· {html.escape(mode)}\n"
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
            "Р‘РѕС‚-Р°РґРјРёРЅ РіСЂСѓРїРїС‹ РѕР±С‹С‡РЅРѕ РІСЃС‘ СЂР°РІРЅРѕ РІРёРґРёС‚ РєРѕРјРјРµРЅС‚С‹. "
            "Р•СЃР»Рё РјРѕР»С‡РёС‚ вЂ” BotFather в†’ /setprivacy в†’ Disable",
            flush=True,
        )
    # РїСЂРѕС„РёР»СЊ Р±РѕС‚Р° (РєР°Рє Сѓ SaveMod-style: РєРѕСЂРѕС‚РєРѕ Рё СЏСЃРЅРѕ)
    try:
        tg.set_my_short_description(
            cfg,
            "Director Vaggo В· Р·Р°РєР°Р·С‹, СЂРѕР·С‹РіСЂС‹С€Рё, РР-РїРѕРјРѕС‰РЅРёРє РєР°РЅР°Р»Р° @Vaggo01",
        )
        tg.set_my_description(
            cfg,
            "Р’Р°РіРіРѕ вЂ” Р·Р°РєР°Р·С‹ (Р±РѕС‚/СЃР°Р№С‚/РґРёР·Р°Р№РЅ), СЂРѕР·С‹РіСЂС‹С€Рё, РѕС‚РІРµС‚С‹ РІ РєРѕРјРјРµРЅС‚Р°С… РєР°РЅР°Р»Р°.\n"
            "/start вЂ” РјРµРЅСЋ В· /order вЂ” Р·Р°РєР°Р· В· /support вЂ” РїРѕРґРґРµСЂР¶РєР°\n"
            "/legal вЂ” РїРѕР»РёС‚РёРєР° Рё РѕС„РµСЂС‚Р° (РІСЃРµРіРґР° РґРѕСЃС‚СѓРїРЅС‹)\n"
            "РљР°РЅР°Р»: @Vaggo01",
        )
    except Exception as e:
        print("set description fail", e, flush=True)

    public_cmds = [
        {"command": "start", "description": "рџЏ  РњРµРЅСЋ"},
        {"command": "order", "description": "рџ›  Р—Р°РєР°Р·Р°С‚СЊ"},
        {"command": "myorders", "description": "рџ“¦ РњРѕРё Р·Р°РєР°Р·С‹"},
        {"command": "balance", "description": "рџ’і Р‘Р°Р»Р°РЅСЃ"},
        {"command": "prices", "description": "рџ’° РџСЂР°Р№СЃ"},
        {"command": "legal", "description": "рџ“‹ Р”РѕРєСѓРјРµРЅС‚С‹ (РІСЃРµРіРґР°)"},
        {"command": "privacy", "description": "рџ”’ РџРѕР»РёС‚РёРєР°"},
        {"command": "offer", "description": "рџ“њ РћС„РµСЂС‚Р°"},
        {"command": "support", "description": "рџ† РџРѕРґРґРµСЂР¶РєР°"},
        {"command": "ref", "description": "рџ”— Р РµС„РµСЂР°Р»СЊРЅР°СЏ СЃСЃС‹Р»РєР°"},
    ]
    owner_cmds = [
        {"command": "start", "description": "рџЏ  РџСѓР»СЊС‚"},
        {"command": "ping", "description": "Р’РµСЂСЃРёСЏ / Grok / GW"},
        {"command": "queue", "description": "РћС‡РµСЂРµРґСЊ РїРѕСЃС‚РѕРІ"},
        {"command": "gstatus", "description": "Р РѕР·С‹РіСЂС‹С€"},
        {"command": "orders", "description": "Р—Р°РєР°Р·С‹"},
        {"command": "radar", "description": "Р¤РёРЅСЂР°РґР°СЂ"},
        {"command": "hot", "description": "Р“РѕСЂСЏС‰РёРµ Р·Р°РєР°Р·С‹"},
        {"command": "tl", "description": "РўРёРјР»РёРґ"},
        {"command": "contract", "description": "Р”РѕРіРѕРІРѕСЂ: /contract id"},
        {"command": "ref", "description": "Р РµС„РµСЂР°Р»РєР°"},
        {"command": "clean", "description": "РџРѕС‡РёСЃС‚РёС‚СЊ Р›РЎ"},
        {"command": "brains", "description": "Grok СЃС‚Р°С‚СѓСЃ"},
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
    print("PollingвЂ¦ Ctrl+C СЃС‚РѕРї")

    while True:
        try:
            cfg = load_config()
            state = load_state()
            # РћС‡РµСЂРµРґСЊ РїРѕСЃС‚РѕРІ вЂ” РІРЅСѓС‚СЂРё Р±РѕС‚Р°, РѕС‚РґРµР»СЊРЅС‹Р№ publisher РЅРµ РѕР±СЏР·Р°С‚РµР»РµРЅ
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
            updates = tg.get_updates(cfg, offset=offset, timeout=25)
            dirty = False
            for u in updates:
                offset = u["update_id"] + 1
                state["offset"] = offset
                dirty = True
                try:
                    if "callback_query" in u:
                        # РєРЅРѕРїРєРё вЂ” Р±РµР· Р»РёС€РЅРµРіРѕ load_state (Р±С‹СЃС‚СЂРµРµ)
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
                            # 0) СЃР»СѓР¶РµР±РЅРѕРµ РІР»Р°РґРµР»СЊС†Р° вЂ” Р”Рћ РІСЃРµРіРѕ (redeploy/ping)
                            if handle_owner_system(cfg, state, msg):
                                continue
                            # РІР»Р°РґРµР»РµС†: Р±Р»РѕРє/СЂР°Р·Р±Р»РѕРє
                            if handle_mod_owner_commands(cfg, state, msg):
                                continue
                            # СѓСЃР»РѕРІРёСЏ вЂ” РґРѕ РІСЃРµРіРѕ РѕСЃС‚Р°Р»СЊРЅРѕРіРѕ
                            if handle_terms_private(cfg, state, msg):
                                continue
                            # С‚РёРєРµС‚С‹: await / auto-РґРѕРїРёСЃСЊ (РґРѕ orders, С‡С‚РѕР±С‹ РЅРµ СЃСЉРµРґР°С‚СЊ РўР—)
                            if handle_support_private(cfg, state, msg):
                                continue
                            # Р°РІС‚РѕР±Р»РѕРє (РЅРµР·Р°РєРѕРЅРЅРѕРµ РўР—)
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
                    # РЅРµ СЂРѕРЅСЏРµРј РІРµСЃСЊ polling РёР·вЂ‘Р·Р° РѕРґРЅРѕР№ РєРЅРѕРїРєРё
                    print("update err", type(ue).__name__, str(ue)[:160], flush=True)
            # РѕРґРёРЅ save РЅР° РїР°С‡РєСѓ вЂ” РёРЅР°С‡Рµ РєР°Р¶РґС‹Р№ offset = С‚СЏР¶С‘Р»С‹Р№ merge+disk
            if dirty:
                save_state(state)
        except KeyboardInterrupt:
            print("\nРЎС‚РѕРї.")
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
                print("409 Conflict: Р¶РґСѓ 10СЃ", flush=True)
                time.sleep(10)
            else:
                time.sleep(2)


if __name__ == "__main__":
    run()
