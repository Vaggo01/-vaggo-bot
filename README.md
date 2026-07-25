# Director Vaggo Bot **4.6.0**

Telegram-бот канала @Vaggo01: заказы, розыгрыши, очередь постов, Grok.

## Bothost (рекомендуется 24/7)

### 1. Env в панели Bothost

**Обязательно:**
- `BOT_TOKEN` — токен @DirectorVaggobot
- `OWNER_USER_IDS` — `5740061551`
- `CHANNEL_ID` — `@Vaggo01`

**Мозг (нужен один из вариантов):**

| Вариант | Env | Когда |
|--------|-----|--------|
| **A. API key** (проще) | `XAI_API_KEY=xai-...` | Комп не нужен |
| **B. Bridge** | `GROK_BRIDGE_URL` + `GROK_BRIDGE_SECRET` | Super-сессия на ПК + tunnel |

Опционально:
- `BOT_HOST_MODE=cloud` (ставится само, если есть `BOT_ID`)
- `BRAIN=auto`

### 2. Код
Репозиторий: `github.com/vladislavbondarev230-cloud/-vaggo-bot`  
После `push_bothost.ps1` — Redeploy / `/redeploy` / авто-pull.

### 3. Перед стартом Bothost
**Стоп локального** `bot.py` (иначе 409 Conflict).

### 4. Проверка
`/ping` → `ver: 4.6.0`, host `cloud`, brain `grok · api_key` (или bridge)

## Локалка

`bot_host_mode=local`, `use_grok_session=true`, session из `grok login`.  
Bothost при этом **STOP**.

## Розыгрыш

- Итог при **min_complete=10** complete
- Срок вышел и людей меньше — **+24ч**
- `/gstatus` · `/gwrestore` · `/gfixkb`
