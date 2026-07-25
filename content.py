"""Стиль канала Vaggo + мозги: Grok (xAI API) → Ollama → шаблон."""
from __future__ import annotations

import os
import re
import time
from typing import Any

import requests

from state import load_config

# кэш, чтобы UI не ждал сеть
_ollama_cache: dict[str, Any] = {"t": 0.0, "ok": False}
_brain_cache: dict[str, Any] = {"t": 0.0, "data": None}


# Общий «мозг» Вагго — умнее, честнее, без заглушек
SYSTEM_BRAIN = """Ты — Вагго: умный, прямой, с характером. Канал @Vaggo01 (ИИ, стек, дисциплина, лайфхаки).

УМЕНИЕ:
- Думаешь на 1 шаг глубже обычного «ответа нейросети»: сравнение, trade-off, «когда ДА / когда НЕТ».
- Факты (цены, даты, релизы, бенчмарки, «кто лучше сейчас») — только из live web/X search или честно «на практике / без точной цифры».
- Не выдумывай топ-рейтинги, цены и «исследования». Неуверен — скажи прямо.
- Конкретика > общие слова. Лучше 1 схема, чем 5 пустых абзацев.
- Не токсичь, не кликбейт «шок», не корпоративный FAQ, не «как языковая модель…».

ЗАПРЕТ ЗАГЛУШЕК (всегда):
«Слышу», «кинь мысль яснее», «продолжим как нормальный разговор», «на связи»,
«отличный вопрос», «спасибо за коммент», «чем могу помочь», «напиши подробнее» без ответа по сути.
"""

SYSTEM_CHANNEL = SYSTEM_BRAIN + """
РОЛЬ: главный автор постов Telegram-канала «Вагго».

ГЛАВНОЕ В ПОСТЕ:
- Чтобы ХОТЕЛОСЬ дочитать и сохранить.
- Крючок с первой строки (сцена, удар, вопрос, парадокс) — не «Друзья, сегодня поговорим».
- Читатель уносит: правило, схему, выбор «когда что», мини-эксперимент.
- Живой ритм: короткие абзацы, воздух, 1–2 сильных формулировки.
- Лёгкая ирония ок. Канцелярит и вода — нет.
- Для новостей/релизов ИИ: live search обязателен; цифры и названия — точные.

ГОЛОС:
- «Друзья» уместно, но не в каждой фразе;
- эмодзи умеренно (заголовок + акценты);
- HTML только: <b> <i> <code> — НЕ markdown **;
- длина обычно 1000–1800 знаков (если не просили гайд).

РУБРИКИ:
- Вечерний Вагго — мысль + 1 действие;
- Битва нейросетей — ИИ честно, practically, «когда брать»;
- Прокачка — тело, дисциплина;
- Кибер-Лайфхак — 1 фишка, сразу;
- Проект — LAB/OS/бот, закулисье без нытья.

СТРУКТУРА:
1) крючок
2) мясо (история / сравнение / шаги)
3) вывод + цепкий вопрос в комменты
4) в конце: 👉 <a href=\"https://t.me/Vaggo01\">t.me/Vaggo01</a> если уместно

Ответ = ТОЛЬКО готовый пост, без преамбулы."""


SYSTEM_COMMENT = SYSTEM_BRAIN + """
РОЛЬ: живой собеседник в комментариях @Vaggo01 (не саппорт-скрипт).

Live web/X search — если вопрос про даты, новости, «кто/сколько сейчас», цены, релизы.
Пользуйся search; не выдумывай.

РЕЖИМЫ (смотри user):
1) seed — первый коммент под постом: по теме поста, цепляешь диалог.
2) ответ подписчику — якорь = ЕГО сообщение; пост = фон.

КАК ОТВЕЧАТЬ:
- сначала смысл его реплики (вопрос / шутка / мнение / спор);
- короткий вопрос → чёткий ответ + 1 цепляющая мысль или вопрос;
- «кто лучше / что взять» → критерий + когда A / когда B, без фанатизма;
- промпт/код/стек → конкретный совет, можно мини-шаблон;
- привет / «огонь» — коротко, по-человечески, без эссе;
- пост подтягивай, если реплика про пост или без него пусто;
- ссылки — если просят «где открыть» или это реально помогает;
- 1–4 предложения обычно; на сложный/практический вопрос — можно развёрнутее (рецепт, шаги, схема);
- тема ЛЮБАЯ: ИИ, еда, спорт, быт — отвечай полноценно по запросу, не отшивай «это не про канал»;
- пост канала — только фон, если уместно; не своди всё к рекламе @Vaggo01;
- plain text, без HTML/markdown **.

Ответ = только реплика, без кавычек и «Вот мой ответ:»."""
SYSTEM_SEED = SYSTEM_BRAIN + """
РОЛЬ: ПЕРВЫЙ комментарий под свежим постом @Vaggo01.

Задача:
- 1–3 живых предложения СТРОГО по теме поста (не «классный пост» в пустоту);
- добавь угол, который пост не разжевал, или острый вопрос подписчикам;
- тон Вагго: прямой, умный, без канцелярита;
- без markdown, без HTML, без «как ИИ…».

Ответ = только текст комментария."""

SYSTEM_GUIDE = SYSTEM_BRAIN + """
РОЛЬ: автор длинного ПОЛЕЗНОГО гайда для @Vaggo01.

Формат HTML: <b> <i> <code> — без markdown.
Объём 2500–3800 символов (лимит ~4096).
Структура: рубрика → крючок → разбор (сила / когда брать / слабее или шаги) → чеклист → вопрос в комменты → ссылка t.me/Vaggo01.
Факты — search или осторожно. Без воды и кликбейта.
Ответ = только гайд."""


def _xai_key(cfg: dict) -> str:
    """Явный API-ключ console.x.ai (xai-...)."""
    return (
        (cfg.get("xai_api_key") or "").strip()
        or (cfg.get("grok_api_key") or "").strip()
        or (os.environ.get("XAI_API_KEY") or "").strip()
        or (os.environ.get("GROK_API_KEY") or "").strip()
    )


# Маяк URL моста (ПК пишет при старте туннеля). Several mirrors — raw.github кэширует.
_BRIDGE_DISCOVERY_URLS = (
    "https://cdn.jsdelivr.net/gh/vladislavbondarev230-cloud/-vaggo-bot@main/bridge_endpoint.json",
    "https://raw.githubusercontent.com/vladislavbondarev230-cloud/%2Dvaggo-bot/main/bridge_endpoint.json",
    "https://cdn.jsdelivr.net/gh/vladislavbondarev230-cloud/-vaggo-bot@master/bridge_endpoint.json",
)
_bridge_discovery_cache: dict[str, Any] = {"t": 0.0, "url": ""}


def _probe_bridge_url(url: str, *, secret: str = "", timeout: float = 4.0) -> bool:
    """Быстрый /health — отсекает протухший trycloudflare URL."""
    if not url or not str(url).startswith("http"):
        return False
    if "your-cloudflared" in url or "XXXX" in url or "example" in url:
        return False
    try:
        headers = {"User-Agent": "VaggoBot-BridgeProbe/1.0"}
        if secret:
            headers["X-Bridge-Secret"] = secret
        r = requests.get(f"{url.rstrip('/')}/health", headers=headers, timeout=timeout)
        if not r.ok:
            return False
        try:
            data = r.json() if r.content else {}
            if isinstance(data, dict) and data.get("ok") is False:
                return False
        except Exception:
            pass
        return True
    except Exception:
        return False


def _discover_bridge_url() -> str:
    """Свежий URL с GitHub/jsDelivr (ПК пишет bridge_endpoint.json)."""
    now = time.time()
    if _bridge_discovery_cache["url"] and (now - float(_bridge_discovery_cache["t"])) < 30:
        return str(_bridge_discovery_cache["url"] or "")

    urls: list[str] = []
    urls.extend(_BRIDGE_DISCOVERY_URLS)
    # cache-bust: jsDelivr иногда держит старый endpoint
    bust = str(int(now) // 60)
    urls = [f"{u}?t={bust}" if "?" not in u else u for u in urls]

    import json as _json

    for disc in urls:
        try:
            r = requests.get(
                disc,
                timeout=8,
                headers={
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                    "User-Agent": "VaggoBot-BridgeDiscovery/1.2",
                    "Accept": "application/json,text/plain,*/*",
                },
            )
            if not r.ok or not r.content:
                print(f"bridge discovery http {r.status_code} {disc[:50]}", flush=True)
                continue
            text = r.content.decode("utf-8-sig", errors="replace")
            data = _json.loads(text) if text.strip() else {}
            u = str((data or {}).get("url") or "").strip().rstrip("/")
            if u.startswith("http") and "your-cloudflared" not in u and "XXXX" not in u:
                _bridge_discovery_cache["url"] = u
                _bridge_discovery_cache["t"] = now
                print(f"bridge discovery ok: {u}", flush=True)
                return u
            if u:
                print(f"bridge discovery skip placeholder: {u[:48]}", flush=True)
        except Exception as e:
            print("bridge discovery fail", disc[:40], e, flush=True)
    return str(_bridge_discovery_cache["url"] or "")


def _bridge_url(cfg: dict) -> str:
    """
    URL моста на домашний ПК.
    Env/config может быть СТАРЫМ (quick-tunnel меняет URL) — если /health мёртв,
    берём свежий URL из GitHub discovery.
    """
    # на самом мосту discovery/loop запрещён
    if cfg.get("grok_bridge_disable") or (os.environ.get("GROK_BRIDGE_DISABLE") or "").strip() in (
        "1",
        "true",
        "yes",
    ):
        return ""
    secret = _bridge_secret(cfg)
    direct = (
        (cfg.get("grok_bridge_url") or "").strip().rstrip("/")
        or (os.environ.get("GROK_BRIDGE_URL") or "").strip().rstrip("/")
    )
    if direct:
        # живой — ок; мёртвый — не залипаем, ищем новый
        if _probe_bridge_url(direct, secret=secret):
            return direct
        print(f"bridge direct dead, rediscover: {direct[:48]}", flush=True)
        # сбросить кэш discovery
        _bridge_discovery_cache["t"] = 0.0
        _bridge_discovery_cache["url"] = ""

    custom = (
        (cfg.get("grok_bridge_discovery") or "").strip()
        or (os.environ.get("GROK_BRIDGE_DISCOVERY") or "").strip()
    )
    if custom:
        # one-shot custom first
        try:
            r = requests.get(custom, timeout=8, headers={"Cache-Control": "no-cache"})
            if r.ok and r.content:
                import json as _json

                data = _json.loads(r.content.decode("utf-8-sig", errors="replace"))
                u = str((data or {}).get("url") or "").strip().rstrip("/")
                if u.startswith("http") and _probe_bridge_url(u, secret=secret):
                    return u
        except Exception as e:
            print("custom discovery fail", e, flush=True)

    found = _discover_bridge_url()
    if found and _probe_bridge_url(found, secret=secret):
        return found
    # даже без health (краткий DNS lag) — вернём discovered, chat сам ретрайнет
    return found or direct or ""


# секрет по умолчанию (тот же, что на домашнем мосту) — Bothost без env всё равно ходит
DEFAULT_BRIDGE_SECRET = "ftW0PH-ZJQOaeXvFuL2mu0lEFIPsremU"


def _bridge_secret(cfg: dict) -> str:
    return (
        (cfg.get("grok_bridge_secret") or "").strip()
        or (os.environ.get("GROK_BRIDGE_SECRET") or "").strip()
        or DEFAULT_BRIDGE_SECRET
    )


def _bridge_chat(
    cfg: dict,
    system: str,
    user: str,
    *,
    model: str | None = None,
    temperature: float = 0.55,
    tools: bool | None = None,
    max_tokens: int | None = None,
) -> str:
    url = _bridge_url(cfg)
    if not url:
        raise RuntimeError("no bridge")
    headers = {"Content-Type": "application/json"}
    sec = _bridge_secret(cfg)
    if sec:
        headers["X-Bridge-Secret"] = sec
    body: dict[str, Any] = {
        "system": system,
        "user": user,
        "model": model,
        "temperature": temperature,
    }
    if tools is not None:
        body["tools"] = bool(tools)
    if max_tokens is not None:
        body["max_tokens"] = int(max_tokens)
    # быстрый путь без tools — короткий timeout
    to = 45 if tools is False else 200
    resp = requests.post(
        f"{url}/chat",
        headers=headers,
        json=body,
        timeout=to,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"bridge chat {resp.status_code}: {resp.text[:200]}")
    data = resp.json() if resp.content else {}
    if not data.get("ok"):
        raise RuntimeError(f"bridge chat fail: {data.get('error') or data}")
    return str(data.get("text") or "").strip()


def _bridge_vision(
    cfg: dict,
    system: str,
    user: str,
    image_path: str,
    *,
    model: str | None = None,
    temperature: float = 0.1,
) -> str:
    import base64
    from pathlib import Path

    url = _bridge_url(cfg)
    if not url:
        raise RuntimeError("no bridge")
    raw = Path(image_path).read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    headers = {"Content-Type": "application/json"}
    sec = _bridge_secret(cfg)
    if sec:
        headers["X-Bridge-Secret"] = sec
    resp = requests.post(
        f"{url}/vision",
        headers=headers,
        json={
            "system": system,
            "user": user,
            "image_b64": b64,
            "suffix": Path(image_path).suffix or ".jpg",
            "model": model,
            "temperature": temperature,
        },
        timeout=150,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"bridge vision {resp.status_code}: {resp.text[:200]}")
    data = resp.json() if resp.content else {}
    if not data.get("ok"):
        raise RuntimeError(f"bridge vision fail: {data.get('error') or data}")
    return str(data.get("text") or "").strip()


def _is_cloud_host(cfg: dict) -> bool:
    """Bothost / cloud: сессии ~/.grok на сервере обычно нет."""
    import os

    mode = str(cfg.get("bot_host_mode") or "").lower().strip()
    if mode in ("cloud", "bothost", "hosting", "remote"):
        return True
    if (os.environ.get("BOT_ID") or "").strip():
        return True
    if (os.environ.get("BOTHOST") or os.environ.get("BOTHHOST") or "").strip():
        return True
    return False


def _grok_bearer(cfg: dict) -> tuple[str, str]:
    """
    Bearer для api.x.ai.
    source = bridge | session | api_key | ''

    Локалка: bridge → Super-session → xai_api_key
    Cloud/Bothost: bridge → xai_api_key → session (если вдруг есть)
    """
    if _bridge_url(cfg):
        return "bridge", "bridge"

    cloud = _is_cloud_host(cfg)
    key = _xai_key(cfg)
    sess = ""
    if cfg.get("use_grok_session", True):
        try:
            from grok_auth import session_token

            sess = (session_token() or "").strip()
        except Exception:
            sess = ""

    if cloud:
        if key:
            return key, "api_key"
        if sess:
            return sess, "session"
        return "", ""

    # local: session (Super) preferred over paid API credits
    if sess:
        return sess, "session"
    if key:
        return key, "api_key"
    return "", ""


_bridge_health_cache: dict[str, Any] = {"t": 0.0, "ok": False, "url": ""}


def _bridge_healthy(cfg: dict, *, force: bool = False) -> bool:
    """Реальный /health моста (кэш 25с), чтобы cloud не думал «grok ok» в пустоту."""
    url = _bridge_url(cfg)
    if not url:
        return False
    now = time.time()
    if (
        not force
        and _bridge_health_cache.get("url") == url
        and (now - float(_bridge_health_cache.get("t") or 0)) < 25
    ):
        return bool(_bridge_health_cache.get("ok"))
    ok = False
    try:
        headers = {}
        sec = _bridge_secret(cfg)
        if sec:
            headers["X-Bridge-Secret"] = sec
        r = requests.get(f"{url}/health", headers=headers, timeout=4)
        ok = bool(r.ok) and ("ok" in (r.text or "").lower() or True)
        if r.ok:
            try:
                data = r.json() if r.content else {}
                if isinstance(data, dict) and data.get("ok") is False:
                    ok = False
                elif isinstance(data, dict) and "grok" in data and not data.get("grok"):
                    ok = False
            except Exception:
                ok = r.ok
    except Exception:
        ok = False
    _bridge_health_cache["t"] = now
    _bridge_health_cache["ok"] = ok
    _bridge_health_cache["url"] = url
    return ok


def grok_ok(cfg: dict) -> bool:
    tok, src = _grok_bearer(cfg)
    if src == "bridge":
        return _bridge_healthy(cfg)
    return bool(tok)


def ollama_ok(cfg: dict, *, force: bool = False, timeout: float = 0.35) -> bool:
    """Быстрый/кэшированный пинг Ollama. UI всегда с кэшем, без force."""
    now = time.time()
    if not force and (now - float(_ollama_cache["t"])) < 45:
        return bool(_ollama_cache["ok"])
    try:
        base = (cfg.get("ollama_url") or "http://127.0.0.1:11434").rstrip("/")
        r = requests.get(f"{base}/api/tags", timeout=timeout)
        ok = bool(r.ok)
    except Exception:
        ok = False
    _ollama_cache["ok"] = ok
    _ollama_cache["t"] = now
    return ok


def brain_status(
    cfg: dict | None = None,
    *,
    probe_ollama: bool = False,
    use_cache: bool = True,
) -> dict[str, Any]:
    """
    probe_ollama=False — не трогать сеть (для UI).
    use_cache=True — вернуть кэш < 20 сек.
    """
    cfg = cfg or load_config()
    now = time.time()
    if use_cache and _brain_cache["data"] and (now - float(_brain_cache["t"])) < 20:
        return dict(_brain_cache["data"])

    mode = (cfg.get("brain") or "auto").lower().strip()
    g = grok_ok(cfg)
    _tok, gsrc = _grok_bearer(cfg)
    if probe_ollama:
        o = ollama_ok(cfg, force=True, timeout=0.35)
    else:
        # только последнее известное, без ожидания
        o = bool(_ollama_cache["ok"]) if _ollama_cache["t"] else False

    if mode == "grok":
        active = "grok" if g else "none"
    elif mode == "ollama":
        active = "ollama" if o else "none"
    elif mode == "template":
        active = "template"
    else:
        if g:
            active = "grok"
        elif o:
            active = "ollama"
        else:
            active = "template"

    hint = ""
    if active in ("none", "template") or not g:
        if _is_cloud_host(cfg):
            if not _bridge_url(cfg) and not _xai_key(cfg):
                hint = "Bothost: задай XAI_API_KEY или GROK_BRIDGE_URL+SECRET"
            elif _bridge_url(cfg) and not _bridge_healthy(cfg):
                hint = "bridge URL есть, но /health мёртв — включи ПК + tunnel"
            else:
                hint = "Grok offline — проверь ключ / bridge"
        else:
            hint = "локально: grok login  или xai_api_key"
    sess = {}
    try:
        from grok_auth import session_info

        sess = session_info()
    except Exception:
        sess = {"ok": False}
    data = {
        "mode": mode,
        "active": active,
        "grok": g,
        "grok_source": gsrc if g else (gsrc or "none"),
        "ollama": o,
        "grok_model": cfg.get("grok_model") or cfg.get("grok_full_model") or "grok-4.5",
        "ollama_model": cfg.get("ollama_model") or "qwen2.5:7b",
        "grok_tools": bool(cfg.get("grok_tools", True)),
        "grok_web_search": bool(cfg.get("grok_web_search", True)),
        "grok_x_search": bool(cfg.get("grok_x_search", True)),
        "session": sess,
        "hint": hint,
        "cloud": _is_cloud_host(cfg),
        "host_mode": str(cfg.get("bot_host_mode") or ("cloud" if _is_cloud_host(cfg) else "local")),
    }
    _brain_cache["data"] = data
    _brain_cache["t"] = now
    return dict(data)


def _grok_tools_enabled(cfg: dict, tools: bool | None) -> bool:
    if tools is not None:
        return bool(tools)
    return bool(cfg.get("grok_tools", True))


def _parse_responses_text(data: dict) -> str:
    """Достаёт финальный текст из /v1/responses (с tool calls)."""
    texts: list[str] = []
    for o in data.get("output") or []:
        if not isinstance(o, dict):
            continue
        if o.get("type") != "message":
            continue
        for c in o.get("content") or []:
            if not isinstance(c, dict):
                continue
            t = (c.get("text") or "").strip()
            if t:
                texts.append(t)
    if texts:
        # последний message обычно финальный ответ (после search)
        return texts[-1]
    # fallbacks
    for key in ("output_text", "text"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict) and (v.get("text") or "").strip():
            return str(v.get("text")).strip()
    return ""


def _grok_tools_list(cfg: dict) -> list[dict]:
    """Built-in agent tools xAI (web + X + code по конфигу)."""
    tools: list[dict] = []
    if cfg.get("grok_web_search", True):
        tools.append({"type": "web_search"})
    if cfg.get("grok_x_search", True):
        tools.append({"type": "x_search"})
    if cfg.get("grok_code_interpreter", False):
        tools.append({"type": "code_interpreter"})
    return tools


def grok_chat(
    cfg: dict,
    system: str,
    user: str,
    *,
    model: str | None = None,
    temperature: float = 0.55,
    tools: bool | None = None,
    max_tokens: int | None = None,
) -> str:
    """xAI: bridge (ПК Super) → Responses+tools → chat/completions."""
    model = (
        model
        or cfg.get("grok_model")
        or cfg.get("grok_full_model")
        or "grok-4.5"
    )
    use_tools = _grok_tools_enabled(cfg, tools)
    # 1) домашний мост (Bothost → твой ПК)
    if _bridge_url(cfg):
        try:
            return _bridge_chat(
                cfg,
                system,
                user,
                model=model,
                temperature=temperature,
                tools=use_tools,
                max_tokens=max_tokens,
            )
        except Exception as e:
            print("bridge chat fail → fallback api/session", e, flush=True)
            # fall through to api key / session (ignore dead bridge URL)
    token, source = _grok_bearer(cfg)
    if source == "bridge":
        # URL есть, но мост уже упал выше — пробуем ключ/сессию напрямую
        key = _xai_key(cfg)
        if key:
            token, source = key, "api_key"
        else:
            sess = ""
            if cfg.get("use_grok_session", True):
                try:
                    from grok_auth import session_token

                    sess = (session_token() or "").strip()
                except Exception:
                    sess = ""
            if sess:
                token, source = sess, "session"
            else:
                raise RuntimeError(
                    f"Grok bridge недоступен и нет api_key/session: {_bridge_url(cfg)}"
                )
    if not token:
        raise RuntimeError(
            "Нет доступа к Grok: grok login / XAI_API_KEY / GROK_BRIDGE_URL"
        )
    base = (cfg.get("xai_base_url") or "https://api.x.ai/v1").rstrip("/")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # 2) Responses API + built-in tools (live web / X) — флагманский режим
    if use_tools:
        tool_list = _grok_tools_list(cfg)
        if tool_list:
            inp: list[dict] = []
            if (system or "").strip():
                inp.append({"role": "system", "content": system})
            inp.append({"role": "user", "content": user})
            payload_r: dict[str, Any] = {
                "model": model,
                "input": inp,
                "tools": tool_list,
                "temperature": temperature,
            }
            try:
                resp = requests.post(
                    f"{base}/responses",
                    headers=headers,
                    json=payload_r,
                    timeout=180,
                )
                if resp.status_code in (401, 403):
                    raise RuntimeError(
                        f"Grok auth {resp.status_code} (source={source}). "
                        "Перелогинься: grok login  или обнови xai_api_key / credits"
                    )
                if resp.ok:
                    data = resp.json() if resp.content else {}
                    text = _parse_responses_text(data)
                    if text:
                        print(
                            f"grok responses ok model={data.get('model') or model} "
                            f"tools={','.join(t.get('type','') for t in tool_list)}",
                            flush=True,
                        )
                        return text
                    print("grok responses empty text, fallback chat", flush=True)
                else:
                    print(
                        f"grok responses {resp.status_code}: {resp.text[:180]}",
                        flush=True,
                    )
            except RuntimeError:
                raise
            except Exception as e:
                print("grok responses fail", e, flush=True)

    # 3) классический chat/completions (без live tools) — быстрый путь
    payload: dict[str, Any] = {
        "model": model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if max_tokens is not None:
        payload["max_tokens"] = int(max_tokens)
    resp = requests.post(
        f"{base}/chat/completions",
        headers=headers,
        json=payload,
        timeout=60 if max_tokens and int(max_tokens) <= 400 else 120,
    )
    if resp.status_code in (401, 403):
        raise RuntimeError(
            f"Grok auth {resp.status_code} (source={source}). "
            "Перелогинься: grok login  или обнови xai_api_key / credits"
        )
    resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"Grok пустой ответ: {data}")
    return ((choices[0].get("message") or {}).get("content") or "").strip()


def grok_vision(
    cfg: dict,
    system: str,
    user: str,
    image_path: str,
    *,
    model: str | None = None,
    temperature: float = 0.1,
) -> str:
    """Grok с картинкой (vision). image_path — локальный файл."""
    import base64
    from pathlib import Path

    model = (
        model
        or cfg.get("grok_vision_model")
        or cfg.get("grok_full_model")
        or cfg.get("grok_post_model")
        or "grok-4.5"
    )
    if _bridge_url(cfg):
        try:
            return _bridge_vision(
                cfg,
                system,
                user,
                image_path,
                model=model,
                temperature=temperature,
            )
        except Exception as e:
            print("bridge vision fail", e, flush=True)

    token, source = _grok_bearer(cfg)
    if source == "bridge" or not token:
        raise RuntimeError("Нет Grok для проверки скрина (bridge/session/api)")
    path = Path(image_path)
    raw = path.read_bytes()
    if len(raw) > 12_000_000:
        raise RuntimeError("Файл слишком большой")
    b64 = base64.b64encode(raw).decode("ascii")
    suf = path.suffix.lower()
    mime = "image/jpeg"
    if suf == ".png":
        mime = "image/png"
    elif suf == ".webp":
        mime = "image/webp"
    base = (cfg.get("xai_base_url") or "https://api.x.ai/v1").rstrip("/")
    payload = {
        "model": model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    },
                ],
            },
        ],
    }
    resp = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=90,
    )
    if resp.status_code in (401, 403):
        raise RuntimeError(f"Grok vision auth {resp.status_code} (source={source})")
    if resp.status_code >= 400:
        raise RuntimeError(f"Grok vision {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"Grok vision пустой: {data}")
    return ((choices[0].get("message") or {}).get("content") or "").strip()


def verify_giveaway_repost_screenshot(
    cfg: dict,
    image_path: str,
    *,
    channel_username: str = "Vaggo01",
    prize_hint: str = "",
) -> tuple[bool, str]:
    """
    Жёсткая проверка скрина: пересланный пост розыгрыша ЖИВОМУ человеку в Telegram.
    Отклонять: Избранное, боты, свой канал, рандомные чаты без диалога с человеком.
    Returns (ok, reason_ru).
    """
    import json

    uname = (channel_username or "Vaggo01").lstrip("@")
    system = f"""Ты строгий модератор розыгрыша Telegram. Смотришь ОДИН скриншот.
Ответь ТОЛЬКО JSON (без markdown, без текста вокруг):
{{"ok": true/false, "chat_kind": "person|bot|saved|channel|group|unknown", "has_forward": true/false, "from_vaggo": true/false, "reason": "кратко по-русски"}}

ok=true ТОЛЬКО если ВСЕ пункты верны:
1) Это скрин интерфейса Telegram (шапка чата, пузыри, UI), не просто картинка поста.
2) Видно ПЕРЕСЛАННОЕ сообщение (forward) — «Переслано из …» / Forwarded from … / имя канала.
3) Переслано из канала @{uname} / Vaggo / Vaggo01 / розыгрыш Google AI Pro / Gemini Pro (наш пост).
4) Чат — ЛИЧКА с ЖИВЫМ ЧЕЛОВЕКОМ (имя/аватар человека в шапке, не бот).
5) В чате видно, что сообщение ушло СОБЕСЕДНИКУ (диалог 1-на-1).

ok=false ОБЯЗАТЕЛЬНО, если хоть что-то из:
- «Избранное» / Saved Messages / «Saved» / «Избранные» / заметки себе
- чат с БОТОМ (в имени bot, Bot, «бот», синяя галочка бота, @…bot)
- переслано @DirectorVaggobot или любому другому боту
- только открыт канал @{uname} / лента канала без пересылки другу
- переслано в свой канал / в админку / в «Comments»
- групповой чат без явного личного диалога (если сомнение — false)
- скрин размыт, обрезан, не видно шапку чата
- нет признаков forward из нашего канала
- мем, коллаж, фото экрана без UI Telegram
- не уверен — ok=false (лучше отказать)

Будь параноидален: при сомнении ok=false."""
    user = (
        f"Канал розыгрыша: @{uname}. Тема: {prize_hint or 'Google AI Pro / Gemini 18 мес'}.\n"
        "Вопрос: человек переслал пост розыгрыша именно ДРУГУ (живому), "
        "а не боту, не в Избранное и не «куда попало»? Разбери скрин."
    )
    try:
        raw = grok_vision(cfg, system, user, image_path, temperature=0.0)
    except Exception as e:
        return False, f"не удалось проверить автоматически: {e}"
    text = (raw or "").strip()
    data: dict = {}
    m = re.search(r"\{[^{}]*\}", text, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
        except Exception:
            data = {}
    reason = str(data.get("reason") or text or "не похоже на репост другу")[:200]
    ok = bool(data.get("ok")) if data else False
    chat_kind = str(data.get("chat_kind") or "unknown").lower()
    has_fwd = data.get("has_forward")
    from_v = data.get("from_vaggo")

    # жёсткие эвристики поверх ответа модели
    reject_kw = (
        "избранн",
        "saved message",
        "saved messages",
        "заметк",
        "бот",
        " bot",
        "@director",
        "directorvaggo",
        "самому себе",
        "себе в",
        "канал открыт",
        "лента канала",
        "не уверен",
        "сомнен",
    )
    low = (reason + " " + text).lower()
    for kw in reject_kw:
        if kw in low and ("не " + kw) not in low:
            # если модель сама пишет «не бот» — не рубим
            if kw in ("бот", " bot") and any(
                x in low for x in ("не бот", "не bot", "живой", "человек", "друг")
            ):
                continue
            if kw in ("избранн", "saved") and "не избран" in low:
                continue
            ok = False
            if "избран" in kw or "saved" in kw:
                reason = "похоже на Избранное / себе — нужен репост другу"
            elif "бот" in kw or "bot" in kw or "director" in kw:
                reason = "похоже на чат с ботом — нужен живой человек"
            break

    if chat_kind in ("bot", "saved", "channel"):
        ok = False
        reason = {
            "bot": "чат с ботом — не засчитываем",
            "saved": "Избранное / себе — не засчитываем",
            "channel": "это канал, не личка с другом",
        }.get(chat_kind, reason)
    if has_fwd is False:
        ok = False
        reason = reason if "пересл" in reason.lower() else "не видно пересланного поста"
    if from_v is False:
        ok = False
        reason = reason if "канал" in reason.lower() or "vaggo" in low else "не наш пост / не @Vaggo01"

    if not data:
        # без JSON — не доверяем
        if '"ok": true' in low or '"ok":true' in low:
            # всё равно требуем не bot/saved
            if any(x in low for x in ("избран", "saved", "бот", "bot")):
                return False, "сомнительный скрин (бот/Избранное?) — пришли другой"
            return True, (reason[:120] or "принято")
        return False, (reason[:200] or "не похоже на репост другу")

    if ok:
        return True, reason[:200] or "репост другу ок"
    return False, reason[:200] or "отклонено"


def ollama_chat(cfg: dict, system: str, user: str, *, model: str | None = None, temperature: float = 0.55) -> str:
    base = (cfg.get("ollama_url") or "http://127.0.0.1:11434").rstrip("/")
    model = model or cfg.get("ollama_model") or "qwen2.5:7b"
    payload = {
        "model": model,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": 1800},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    resp = requests.post(f"{base}/api/chat", json=payload, timeout=180)
    resp.raise_for_status()
    data = resp.json()
    text = (data.get("message") or {}).get("content") or ""
    return text.strip()


def llm_chat(
    cfg: dict,
    system: str,
    user: str,
    *,
    temperature: float = 0.55,
    prefer_fast: bool = False,
    max_tokens: int | None = None,
    tools: bool | None = None,
) -> tuple[str, str]:
    """
    Возвращает (text, engine) где engine = grok|ollama|template.
    Приоритет auto: Grok API → Ollama → template error (caller fallback).
    """
    st = brain_status(cfg)
    active = st["active"]
    mode = st["mode"]

    # explicit force
    if mode == "grok" or active == "grok":
        if grok_ok(cfg):
            model = (
                cfg.get("grok_model")
                or cfg.get("grok_full_model")
                or "grok-4.5"
            )
            if prefer_fast:
                model = (
                    cfg.get("grok_fast_model")
                    or cfg.get("grok_model")
                    or model
                )
            # prefer_fast → быстрая модель + БЕЗ live search
            if tools is None:
                use_tools = not prefer_fast and bool(cfg.get("grok_tools", True))
            else:
                use_tools = bool(tools)
            mt = max_tokens
            if mt is None and prefer_fast:
                mt = int(cfg.get("comment_max_tokens") or 220)
            return (
                grok_chat(
                    cfg,
                    system,
                    user,
                    model=model,
                    temperature=temperature,
                    tools=use_tools,
                    max_tokens=mt,
                ).strip(),
                "grok",
            )
        if mode == "grok":
            raise RuntimeError("brain=grok, но нет API-ключа xAI")

    if mode == "ollama" or active == "ollama" or (mode == "auto" and ollama_ok(cfg, force=True)):
        if ollama_ok(cfg, force=False):
            model = cfg.get("fast_model") if prefer_fast else cfg.get("ollama_model")
            return ollama_chat(cfg, system, user, model=model, temperature=temperature).strip(), "ollama"
        if mode == "ollama":
            raise RuntimeError("brain=ollama, но Ollama не запущена")

    raise RuntimeError("Нет доступного мозга (ни Grok API, ни Ollama)")


def format_html_light(text: str) -> str:
    """Грубый перевод **bold** и *italic* в HTML, если модель вернула markdown."""
    t = text.strip()
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", t)
    return t


def pick_reaction_for_text(text: str) -> str:
    """
    Реакция Telegram под смысл поста/коммента.
    Только эмодзи из ALLOWED_REACTIONS бота.
    """
    t = (text or "").lower()
    # порядок: от более специфичного к общему
    rules: list[tuple[tuple[str, ...], str]] = (
        (("😂", "🤣", "хаха", "ахах", "лол", "кек", "смеш", "ору", "угар", "прикол"), "😁"),
        (("🤔", "хм", "сомнев", "не уверен", "странн"), "🤔"),
        (("?", "как ", "почему", "зачем", "что если", "подскаж", "помоги", "вопрос"), "🤔"),
        (("🔥", "огонь", "имба", "пушка", "жестк", "топ", "круто", "класс", "вау", "бомб"), "🔥"),
        (("❤", "❤️", "люблю", "обожаю", "мил", "нрав", "сердц", "обним"), "❤"),
        (("спасибо", "благодар", "респект", "красава", "молодец"), "🙏"),
        (("👏", "браво", "аплод", "уважуха"), "👏"),
        (("🎉", "праздн", "др ", "др,", "с днём", "поздра", "ура"), "🎉"),
        (("⚡", "быстр", "молни", "энерг", "разгон"), "⚡"),
        (("спорт", "трен", "качал", "прокач", "жим", "бег", "зал"), "⚡"),
        (("🤯", "шок", "офиге", "жесть", "не верю", "вау"), "🤯"),
        (("😢", "груст", "жаль", "обид", "слёз", "печал"), "😢"),
        (("🤮", "бее", "фуу", "отврат", "тошн", "фу,"), "🤮"),
        (("💩", "говно", "херн", "отстой", "фигня"), "💩"),
        (("🤬", "злюсь", "бесит", "ненавиж"), "🤬"),
        (("😴", "спать", "устал", "лень", "засыпа"), "😴"),
        (("🤓", "научн", "исследован", "исследова"), "🤓"),
        (("👀", "интересн", "глянь", "посмотри"), "👀"),
        (("вау", "нереально", "офигенн"), "🤩"),
        (("нейросет", "chatgpt", "claude", "grok", "gemini", "промпт", " llm"), "🔥"),
        (("python", "баг ", "github", "cursor", "код "), "👨‍💻"),
        (("деньг", "заработ", "бизнес", "монет"), "💯"),
        (("привет", "здаров", "hello", "здарова"), "❤"),
        (("согласен", "точно", "+1", "плюсую"), "👍"),
        (("не согласен", "спорно"), "🤔"),
    )
    for words, emoji in rules:
        if any(w in t for w in words):
            return emoji
    # дефолт по длине/типу
    if "?" in (text or ""):
        return "🤔"
    if len(t) < 12:
        return "👍"
    if any(w in t for w in ("нейро", "ии", "ai", "бот", "канал")):
        return "🔥"
    return "❤"


def generate_post(topic: str, *, rubric: str = "", full_brain: bool = True) -> str:
    cfg = load_config()
    rubrics = "\n".join(f"- {r}" for r in (cfg.get("style") or {}).get("rubrics") or [])
    user = (
        f"Рубрика: {rubric or 'подбери сам по теме'}\n"
        f"Тема / бриф: {topic}\n\n"
        f"Рубрики канала:\n{rubrics}\n\n"
        f"Напиши пост 1100–1800 знаков: умный, цепкий, с реальной пользой.\n"
        f"Если тема про новости/релизы/цены ИИ — сделай live search и опирайся на свежие факты.\n"
        f"Дай читателю: 1) крючок 2) мясо (схема/сравнение/шаги) 3) правило «когда что» "
        f"4) вопрос в комменты.\n"
        f"Запрещено: вода, «сегодня поговорим», пустой список, кликбейт, выдуманные цифры."
    )
    try:
        # full_brain: сильнее модель + tools + живая temperature
        if full_brain:
            model = (
                cfg.get("grok_post_model")
                or cfg.get("grok_full_model")
                or "grok-4.5"
            )
            raw = grok_chat(
                cfg,
                SYSTEM_CHANNEL,
                user,
                model=model,
                temperature=float(cfg.get("post_temperature") or 0.68),
                tools=True,
                max_tokens=int(cfg.get("post_max_tokens") or 2200),
            )
            text = format_html_light(raw.strip())
            return text
        raw, engine = llm_chat(
            cfg,
            SYSTEM_CHANNEL,
            user,
            temperature=0.65,
            tools=True,
            prefer_fast=False,
            max_tokens=int(cfg.get("post_max_tokens") or 2200),
        )
        text = format_html_light(raw)
        return text
    except Exception:
        title = rubric or "Вагго"
        body = topic.strip()
        return (
            f"👋 <b>Друзья!</b>\n\n"
            f"<b>{html_escape_title(title)}</b>\n\n"
            f"{body}\n\n"
            f"<i>Черновик-заглушка: нет Grok API и Ollama. "
            f"Вставь xai_api_key или запусти Ollama — либо попроси Grok в чате написать пост.</i>\n\n"
            f"👇 Что думаешь?\n— Вагго"
        )


def generate_guide(topic: str, *, rubric: str = "Битва нейросетей") -> str:
    """Длинный полезный гайд 2500–3800 символов."""
    cfg = load_config()
    user = (
        f"Рубрика: {rubric}\n"
        f"Тема гайда: {topic}\n\n"
        f"Сделай развёрнутый практический гайд 2500–3800 символов.\n"
        f"Live search если нужны свежие факты/модели/цены.\n"
        f"Списки, сравнения, «когда брать / когда нет», чеклист, без воды."
    )
    try:
        model = (
            cfg.get("grok_post_model")
            or cfg.get("grok_full_model")
            or "grok-4.5"
        )
        if grok_ok(cfg):
            raw = grok_chat(
                cfg,
                SYSTEM_GUIDE,
                user,
                model=model,
                temperature=0.5,
                tools=True,
                max_tokens=4000,
            )
        else:
            raw, _ = llm_chat(
                cfg, SYSTEM_GUIDE, user, temperature=0.5, tools=True, prefer_fast=False
            )
        text = format_html_light(raw)
        if len(text) > 4090:
            text = text[:4085].rsplit("\n", 1)[0] + "…"
        return text
    except Exception as e:
        return (
            f"📋 <b>{html_escape_title(rubric)}</b>\n"
            f"<i>{html_escape_title(topic)}</i>\n\n"
            f"Не смог сгенерировать гайд: {html_escape_title(str(e)[:120])}\n"
            f"Проверь /brains или grok login."
        )


def html_escape_title(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _clean_comment_text(text: str, max_len: int) -> str:
    text = (text or "").strip().strip('"').strip("'")
    for bad in (
        "Конечно! ",
        "Конечно, ",
        "Отличный вопрос! ",
        "Отличный вопрос. ",
        "Спасибо за комментарий! ",
        "Спасибо за комментарий. ",
        "Как ИИ ",
        "Как языковая модель ",
    ):
        if text.startswith(bad):
            text = text[len(bad) :]
    # citations / markdown → telegram-friendly
    text = re.sub(r"\[\[(\d+)\]\]\((https?://[^)]+)\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1", text)
    # голые длинные wiki/url в скобках — убрать шум из комментов
    text = re.sub(r"\((https?://[^)]{40,})\)", "", text)
    text = re.sub(r"https?://(?:en|ru)\.wikipedia\.org\S+", "", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > max_len:
        cut = text[: max_len - 1]
        n = cut.rfind("\n")
        if n > max_len // 2:
            cut = cut[:n]
        else:
            n = cut.rfind(". ")
            if n > max_len // 2:
                cut = cut[: n + 1]
        text = cut.rstrip() + ("…" if not cut.endswith((".", "!", "?", "…")) else "")
    return text


def generate_seed_comment(post_text: str) -> str:
    """Первый коммент под новым постом — по теме поста."""
    cfg = load_config()
    post = (post_text or "").strip()
    if not post:
        return "Ну что, друзья — кто уже в теме? Кидайте мысли 🔥"
    user = (
        "РЕЖИМ: seed (первый комментарий под постом).\n"
        "1–3 предложения по теме поста: умный угол + крючок в диалог.\n"
        "Не пересказывай пост. Не хвали пост в пустоту.\n\n"
        f"Текст поста:\n{post[:1800]}\n"
    )
    try:
        model = (
            cfg.get("grok_post_model")
            or cfg.get("grok_full_model")
            or cfg.get("grok_model")
            or "grok-4.5"
        )
        if grok_ok(cfg):
            raw = grok_chat(
                cfg,
                SYSTEM_SEED,
                user,
                model=model,
                temperature=0.72,
                tools=False,
                max_tokens=280,
            )
        else:
            raw, _ = llm_chat(
                cfg, SYSTEM_SEED, user, temperature=0.72, prefer_fast=False, tools=False
            )
        return _clean_comment_text(raw, 420) or "Ок, пост вышел — что цепляет сильнее всего? 🔥"
    except Exception:
        line = post.split("\n")[0][:80].replace("<b>", "").replace("</b>", "")
        return f"Поехали 🔥 {line}… кто что думает?"


def _comment_needs_live_facts(text: str) -> bool:
    """Нужен live search — факты, новости, «кто/сколько сейчас»."""
    low = (text or "").lower()
    keys = (
        "когда ",
        "какого числа",
        "дата ",
        "сколько сейчас",
        "курс ",
        "цена ",
        "стоит ",
        "новост",
        "финал чм",
        "кто выиграл",
        "актуальн",
        "на данный момент",
        "сейчас какая",
        "сейчас какой",
        "вышла ли",
        "вышел ли",
        "релиз",
        "обновлен",
        "gpt-5",
        "gpt5",
        "gemini 3",
        "claude 4",
        "кто лучше",
        "что лучше",
        "сравни",
        "vs ",
        " против ",
    )
    if any(k in low for k in keys):
        return True
    if low.startswith(("когда", "сколько", "где проходит", "где будет", "кто ", "что лучше")):
        return True
    return False


def _comment_is_trivial(text: str) -> bool:
    """Супер-короткая реплика без смысла — можно instant/fast."""
    t = (text or "").strip()
    if not t:
        return True
    if len(t) <= 12 and any(c in t for c in ("🔥", "👍", "❤", "❤️", "💪", "😂", "🤣", "👏", "👀")):
        return True
    low = t.lower()
    if len(t) <= 18 and any(
        w in low for w in ("ок", "окей", "ага", "угу", "норм", "понял", "ясно", "ладно")
    ):
        return True
    if len(t) <= 24 and any(
        w in low for w in ("спасибо", "благодар", "thx", "thanks", "пасиб")
    ):
        return True
    if len(t) <= 28 and any(
        w in low for w in ("привет", "здаров", "здарова", "хай", "hello", "йо", "доброе", "добрый")
    ):
        return True
    if len(t) <= 16 and any(
        w in low for w in ("крут", "топ", "класс", "огонь", "имба", "пушка", "+1", "согласен")
    ):
        return True
    return False


def try_instant_comment(
    comment_text: str,
    *,
    username: str = "",
) -> str | None:
    """
    Мгновенный ответ без Grok только для совсем пустых реплик.
    Всё содержательное → LLM (умный Grok).
    """
    t = (comment_text or "").strip()
    if not t:
        return None
    # если не тривиально — всегда мозг
    if not _comment_is_trivial(t):
        return None
    low = t.lower()
    hi = ""
    if any(w in low for w in ("привет", "здаров", "здарова", "хай", "hello", "йо", "доброе", "добрый")):
        return f"{hi}йо 🔥 что на уме — кидай, разберём"
    if any(w in low for w in ("спасибо", "благодар", "thx", "thanks", "пасиб")):
        return f"{hi}всегда 🔥 заходи ещё"
    if any(c in t for c in ("🔥", "👍", "❤️", "❤", "💪", "😂", "🤣", "👏")):
        return f"{hi}зашло 🔥 а ты сам с какой стороны смотришь?"
    if any(w in low for w in ("ок", "окей", "ладно", "понял", "ясно", "норм", "ага", "угу")):
        return f"{hi}ок 👍 если копнуть глубже — пиши"
    if any(w in low for w in ("крут", "топ", "класс", "огонь", "имба", "пушка", "+1", "согласен")):
        return f"{hi}огонь 🔥 кинь свой опыт — с чем сравниваешь?"
    return None


SYSTEM_COMMENT_FAST = SYSTEM_BRAIN + """
РОЛЬ: ответ в комментах @Vaggo01.
По делу, с характером. На короткий вопрос — коротко; на «дай рецепт/разбор» — полно по шагам.
Тема любая, не отшивай. Без HTML/markdown **. Только текст реплики."""


def generate_comment_reply(
    comment_text: str,
    *,
    post_context: str = "",
    username: str = "",
) -> str:
    """
    Умный ответ подписчику.
    trivial → instant; факты → full + search; остальное → full model, без тупых заглушек.
    """
    cfg = load_config()
    # 0) instant только для пустяков
    if cfg.get("comment_instant_simple", True):
        inst = try_instant_comment(comment_text, username=username)
        if inst:
            print("comment instant (no LLM)", flush=True)
            return inst

    free = bool(cfg.get("comment_free_chat", True))
    # полный ответ даже на оффтоп (рецепт, спорт…) — не режем в 3 фразы
    max_len = int(cfg.get("comment_max_chars") or (1200 if free else 700))
    temp = float(cfg.get("comment_temperature") or 0.62)
    # search: по умолчанию ВКЛ для факт-вопросов (можно вырубить comment_search=false)
    allow_search = cfg.get("comment_search", True)
    if cfg.get("comment_always_search"):
        allow_search = True
    needs_facts = bool(allow_search) and _comment_needs_live_facts(comment_text)
    trivial = _comment_is_trivial(comment_text)
    # умный путь по умолчанию; fast только если comment_prefer_fast и trivial
    force_fast = bool(cfg.get("comment_prefer_fast", False)) and trivial and not needs_facts
    prefer_fast = force_fast

    user = (
        f"Собеседник: {username or 'человек'}\n"
        f"Написал: {(comment_text or '').strip()}\n"
        "Ответь УМНО и ПОЛНО по его реплике — даже если тема не про канал/ИИ.\n"
        "Смысл → нормальный ответ (шаги/рецепт/схема если просят) → (опц.) короткий вопрос.\n"
        "Без заглушек, без «отличный вопрос», без «это не тема канала».\n"
    )
    if needs_facts:
        user += "Нужны свежие факты — используй live search, не выдумывай.\n"
    if post_context:
        user += f"Фон поста (не пересказывай, якорь если уместно):\n{post_context[:700]}\n"
    try:
        print(
            f"comment path fast={prefer_fast} facts={needs_facts} "
            f"text={(comment_text or '')[:40]!r}",
            flush=True,
        )
        sys_p = SYSTEM_COMMENT_FAST if prefer_fast else SYSTEM_COMMENT
        # полный мозг для нормальных комментов
        mt = int(
            cfg.get("comment_max_tokens")
            or (200 if prefer_fast else (700 if needs_facts else 550))
        )
        if not prefer_fast and grok_ok(cfg):
            model = (
                cfg.get("grok_full_model")
                or cfg.get("grok_model")
                or "grok-4.5"
            )
            raw = grok_chat(
                cfg,
                sys_p,
                user,
                model=model,
                temperature=temp,
                tools=bool(needs_facts or cfg.get("comment_always_search")),
                max_tokens=mt,
            )
            eng = "grok"
        else:
            raw, eng = llm_chat(
                cfg,
                sys_p,
                user,
                temperature=temp,
                prefer_fast=prefer_fast,
                max_tokens=mt,
                tools=bool(needs_facts),
            )
        out = _clean_comment_text(raw, max_len)
        bad_markers = (
            "кинь мысль",
            "чуть яснее",
            "нормальный разговор",
            "напиши мысль яснее",
            "продолжим как нормальный",
            "чем могу помочь",
            "отличный вопрос",
            "спасибо за комментарий",
            "спасибо за коммент",
            "слышу",
            "уточни одну деталь",
            "давай разберём",
            "без воды",
            "напиши яснее",
            "на связи. что",
        )
        low_out = (out or "").lower()
        weak = (not out) or len(out.strip()) < 8 or any(m in low_out for m in bad_markers)
        if out and not weak:
            print(f"comment llm ok engine={eng} len={len(out)}", flush=True)
            return out
        if out:
            print(f"comment llm weak engine={eng}: {out[:80]!r}", flush=True)
        # всегда retry на полном мозге, если отписка/заглушка
        if weak and grok_ok(cfg):
            raw2 = grok_chat(
                cfg,
                SYSTEM_COMMENT,
                user
                + "\nЗАПРЕТ: не пиши «Слышу», «уточни», «кинь яснее», «давай разберём».\n"
                "Дай конкретный ответ по смыслу сообщения. Если вопрос — ответь. "
                "Если привет — поздоровайся по-человечески и спроси тему.\n",
                model=cfg.get("grok_full_model") or cfg.get("grok_model") or "grok-4.5",
                temperature=0.5,
                tools=bool(needs_facts),
                max_tokens=360,
            )
            out2 = _clean_comment_text(raw2, max_len)
            low2 = (out2 or "").lower()
            if (
                out2
                and len(out2) >= 8
                and not any(m in low2 for m in bad_markers)
            ):
                print(f"comment retry ok len={len(out2)}", flush=True)
                return out2
    except Exception as e:
        print("comment llm fail", type(e).__name__, str(e)[:160], flush=True)

    print("comment fallback (no LLM)", flush=True)
    return _comment_fallback(comment_text, post_context=post_context, username=username)


def _comment_fallback(
    comment_text: str,
    *,
    post_context: str = "",
    username: str = "",
) -> str:
    """Живой запасной ответ, если мозг недоступен (Bothost без ключа и т.п.)."""
    t = (comment_text or "").strip()
    low = t.lower()
    name = (username or "").strip()
    hi = f"{name}, " if name and not name.startswith("?") else ""

    # темы ИИ
    if any(w in low for w in ("chatgpt", "чатгпт", "gpt-4", "gpt4", " gpt")):
        return f"{hi}ChatGPT удобен на быстрый текст/код/план: https://chatgpt.com — что именно пробуешь?"
    if "claude" in low or "клод" in low:
        return f"{hi}Claude часто лучше на длинные тексты: https://claude.ai — сравнишь с тем, что в посте?"
    if "grok" in low or "грок" in low:
        return f"{hi}Grok — https://grok.x.ai · прямой тон и идеи. Чем пользуешься чаще?"
    if "gemini" in low or "джемини" in low or "google ai" in low or "google one" in low:
        return (
            f"{hi}Gemini / Google AI Pro — https://gemini.google.com · "
            "если про розыгрыш — условия в посте и кнопка «Участвовать»."
        )
    if "perplex" in low or "перплекс" in low:
        return f"{hi}Perplexity — https://www.perplexity.ai · факты со ссылками. Что ищешь?"
    if any(w in low for w in ("розыгрыш", "gemini pro", "участв", "приз", "скрины", "скрин")):
        return (
            f"{hi}по розыгрышу: подписка @Vaggo01 + репост другу → скрин в @DirectorVaggobot. "
            "Если бот ругается — кинь скрин ещё раз или напиши владельцу."
        )
    if any(w in low for w in ("заказ", "бот сделай", "сайт", "сколько стоит", "прайс", "цен")):
        return (
            f"{hi}заказы — в @DirectorVaggobot → «Заказать», прайс /prices. "
            "Хостинг/домен отдельно, не в цене."
        )
    if any(w in low for w in ("ссылк", "где открыть", "сайт ", "зайти", "как зайти")):
        return (
            f"{hi}база: chatgpt.com · claude.ai · grok.x.ai · gemini.google.com · "
            "perplexity.ai — что именно нужно?"
        )
    if any(w in low for w in ("как дела", "как ты", "как сам", "как жизнь", "how are you")):
        return f"{hi}нормально 🔥 кручу канал и стек. Ты как — что сейчас в приоритете?"
    if any(w in low for w in ("привет", "здаров", "здарова", "хай", "ку ", "hello", "йо ")):
        return f"{hi}йо 🔥 на связи. Пиши по делу или в тему поста — разберём."
    if any(w in low for w in ("крут", "огонь", "топ", "класс", "люблю", "👍", "🔥")):
        return f"{hi}зашло 🔥 а сам чем из стека чаще всего пользуешься?"
    # спорт / чм
    if any(w in low for w in ("финал", "чм", "чемпионат мира", "world cup", "месси", "аргентин")):
        return (
            f"{hi}если про футбол ЧМ — дату лучше сверить на fifa.com/sports (год от года плывёт). "
            "За кого болеешь — Аргентина/Месси или свой вариант?"
        )
    if any(w in low for w in ("точк", "•••", "...", "мат ", "бан")):
        return f"{hi}ага, точки вместо мата — чтобы не словить бан. Норм тактика 😄"
    if "?" in t or any(
        w in low for w in ("как ", "что ", "почему", "зачем", "какой", "можно ли", "когда ")
    ):
        clip = t.replace("\n", " ").strip()
        if len(clip) > 90:
            clip = clip[:87] + "…"
        return (
            f"{hi}по «{clip}»: без Grok сейчас отвечу коротко — кинь цель "
            "(что нужно на выходе), разложу по шагам."
        )
    clip = t.replace("\n", " ").strip()
    if len(clip) > 70:
        clip = clip[:67] + "…"
    if clip:
        return f"{hi}понял: «{clip}». Согласен / свой опыт / вопрос — что из трёх?"
    if post_context:
        return f"{hi}интересный угол. А ты с постом согласен или есть другой взгляд?"
    return f"{hi}кинь вопрос или мысль по теме — отвечу по делу."


def rewrite_post(text: str, *, note: str = "") -> str:
    """Переписать готовый пост в стиле канала."""
    cfg = load_config()
    user = f"Перепиши пост в стиле Вагго, сохрани смысл.\n"
    if note:
        user += f"Пожелание: {note}\n"
    user += f"\nИсходник:\n{text}"
    try:
        raw, _ = llm_chat(cfg, SYSTEM_CHANNEL, user, temperature=0.5)
        return format_html_light(raw)
    except Exception:
        return format_html_light(text)


def generate_ideas(count: int = 7, *, rubric: str = "") -> str:
    cfg = load_config()
    count = max(3, min(count, 12))
    user = (
        f"Придумай {count} идей постов для канала Вагго.\n"
        f"Рубрика-фокус: {rubric or 'все рубрики'}.\n"
        "Формат каждой строки:\n"
        "N. [Рубрика] Короткий заголовок — 1 фраза о чём пост\n"
        "Только список, без вступления."
    )
    try:
        raw, _ = llm_chat(cfg, SYSTEM_CHANNEL, user, temperature=0.7, prefer_fast=True)
        return raw
    except Exception:
        base = [
            "🌌 [Вечерний Вагго] Почему скролл убивает глубокие мысли",
            "🤖 [Битва нейросетей] 4 бесплатных ИИ на сегодня — честный тест",
            "💪 [Прокачка] 12 минут дома без зала — схема",
            "⚡️ [Кибер-Лайфхак] Windows: ускорить ПК за 5 кликов",
            "🛠️ [Проект] Как мы пилим своего бота без бюджета",
            "🤖 [Битва нейросетей] Промпт, который пишет посты как человек",
            "🌌 [Вечерний Вагго] Цифровой примитив — короткая вечерняя заметка",
        ]
        return "\n".join(base[:count])


def series_topics() -> list[tuple[str, str]]:
    """Готовый набор (рубрика, тема) на «старт недели»."""
    return [
        ("Вечерний Вагго", "Концентрация в эпоху коротких роликов"),
        ("Битва нейросетей", "Сравнение 3 бесплатных нейросетей для текста"),
        ("Кибер-Лайфхак", "Полезные жесты и скрытые фичи Telegram"),
        ("Прокачка", "Утренняя разминка 10 минут без инвентаря"),
        ("Новости проекта", "Что умеет менеджер канала и как им пользоваться"),
    ]


def week_plan() -> str:
    cfg = load_config()
    brand = (cfg.get("style") or {}).get("brand") or "Вагго"
    lines = [
        f"📅 <b>План канала {brand}</b> (@{cfg.get('channel_username') or 'Vaggo01'})",
        "",
        "По календарю из канала:",
        "• 🌌 <b>Вечерний Вагго</b> — каждый вечер (философия/мистика)",
        "• 🤖 <b>Битва нейросетей</b> — 3× в неделю (тесты + промпты)",
        "• 💪 <b>Прокачка</b> — текст каждые 2 дня, видео 2× в неделю",
        "• ⚡️ <b>Кибер-Лайфхак</b> — часто, короткие фишки",
        "• 🛠️ <b>Проект</b> — бот / Zverki / закулисье по мере готовности",
        "",
        "Команды: /draft · /ideas · /series · /check · /post",
    ]
    return "\n".join(lines)


def status_text(cfg: dict, state: dict) -> str:
    drafts = [d for d in (state.get("drafts") or []) if d.get("status") == "draft"]
    pending = [c for c in (state.get("pending_comments") or []) if c.get("status") == "pending"]
    published = state.get("published") or []
    paused = bool(cfg.get("paused"))
    me_token = "есть" if (cfg.get("bot_token") or "").strip() else "НЕТ — вставь в config.json"
    disc = cfg.get("discussion_group_id") or 0
    need_ok = cfg.get("comment_needs_owner_ok", True)
    auto = bool(cfg.get("auto_reply_comments"))
    auto_mode = "выкл"
    if auto and not need_ok:
        auto_mode = "сразу в комменты"
    elif auto and need_ok:
        auto_mode = "черновик тебе на ок"
    return (
        f"📊 <b>Менеджер Вагго — статус</b>\n\n"
        f"Канал: {cfg.get('channel_id')}\n"
        f"Токен: {me_token}\n"
        f"Группа комментов: {disc or 'не задана'}\n"
        f"Пауза: {'да ⏸' if paused else 'нет ✅'}\n"
        f"Черновиков: {len(drafts)}\n"
        f"Комментов в очереди: {len(pending)}\n"
        f"Опубликовано из пульта/бота: {len(published)}\n"
        f"Режим комментов: {auto_mode}\n"
        f"Мозг: {st_line(cfg)}\n"
    )


def st_line(cfg: dict) -> str:
    st = brain_status(cfg)
    names = {"grok": "Grok API 🚀", "ollama": "Ollama (локально)", "template": "шаблоны ⚠", "none": "нет ⚠"}
    return f"{names.get(st['active'], st['active'])} (mode={st['mode']})"
