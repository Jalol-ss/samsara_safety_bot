# Samsara Safety Bot

Pushes Samsara safety events (Harsh Brake, Rolling Stop, Following Distance,
Crash, Near-Collision, Severe Speeding, Pedestrian Collision Warning, …) into
a Telegram supergroup, routed into one **Topic** per event type, with the
dashcam video clip attached.

Looks like your reference screenshots from the previous company.

---

## 1. What you need

| Item | Where to get it |
|---|---|
| **Telegram bot token** | Message `@BotFather` → `/newbot` → copy token into `BOT_TOKEN_SAFETY` |
| **Supergroup with Topics ON** | In the Telegram group → *Edit* → *Topics* → enable |
| **Group chat id** | Add the bot to the group as admin, send `/chat_id`, copy the negative id into `SAFETY_GROUP_CHAT_ID` |
| **Samsara API token** | Samsara dashboard → *Settings* → *API Tokens* → *Create*. Scopes required: **Read Safety Events & Scores** and **Read + Write Webhooks**. Optional: **Read Vehicle Stats** for nicer labels. |
| **Public URL** | Railway gives you one after first deploy (`https://xxx.up.railway.app`). Put it in `PUBLIC_BASE_URL`. |

The bot the admin must have permission to **Manage Topics** (otherwise
`/setup_topics` can't create them).

---

## 2. Quick start (local)

```bash
cd samsara_safety_bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # fill BOT_TOKEN_SAFETY, SAMSARA_API_TOKEN, SAFETY_GROUP_CHAT_ID
python probe.py        # confirm Samsara scopes
python bot.py          # starts polling + webhook server on $PORT (default 8080)
```

If `probe.py` prints all ✅, you're good.

---

## 3. Deploy to Railway

1. Push this folder to a GitHub repo (or use Railway's "Deploy from local").
2. In Railway → *New Project* → *Deploy from GitHub Repo* → pick the repo.
3. In *Variables* tab, add every row from `.env.example` that is marked
   required. **Do not** set `PORT` — Railway provides it.
4. First deploy will succeed but the bot will log a warning that
   `PUBLIC_BASE_URL` is empty. Railway now shows you the public URL — copy
   it (no trailing slash) into `PUBLIC_BASE_URL` and redeploy.
5. In Telegram, DM the bot (or add to the group) and run `/ping`. You
   should see "Samsara reachable — N webhook(s) registered."
6. Run `/setup_topics` **inside the group**. The bot creates one topic per
   event type.
7. Run `/register_webhook`. The bot POSTs the webhook to Samsara.
8. In **Samsara dashboard** → *Alerts* → *New Alert Configuration*:
   - **Conditions**: pick the safety events you want (Harsh Brake, Rolling
     Stop, etc.). One alert config can cover many.
   - **Scope**: all vehicles (or a vehicle tag).
   - **Actions**: *Send to Webhook* → pick "Safety Bot" (the one you just
     registered).
   - Save. Events start flowing within minutes.

### Health check

`GET /health` returns `{"status":"ok"}`. Railway's healthcheck is already
wired in `railway.json`.

---

## 4. Operator commands

| Command | What it does |
|---|---|
| `/ping` | Confirms Samsara + Telegram wiring |
| `/chat_id` | Prints the chat and thread id (useful during setup) |
| `/setup_topics` | Creates one Topic per event type (idempotent) |
| `/topics` | Lists cached topic ids |
| `/recent [hours] [limit]` | Manually pulls recent safety events from Samsara and posts any that weren't seen yet. Default: last 2 h, cap 20. Dedup is automatic. |
| `/list_webhooks` | Lists webhooks registered on Samsara |
| `/register_webhook` | Registers `PUBLIC_BASE_URL/webhook/samsara` on Samsara |
| `/unregister_webhook <id>` | Deletes a webhook |

---

## 5. Env vars reference

See `.env.example` for the full list with inline comments. The ones you
care about:

- `BOT_TOKEN_SAFETY` — Telegram bot token (required)
- `SAFETY_GROUP_CHAT_ID` — negative integer, e.g. `-1001234567890`
- `SAMSARA_API_TOKEN` — Samsara token with safety + webhook scopes
- `PUBLIC_BASE_URL` — Railway public URL, no trailing slash
- `SAMSARA_WEBHOOK_SECRET` — any random string, 32+ chars. Shared with
  Samsara when `/register_webhook` runs; used to verify every POST.
- `SAFETY_EVENT_TYPES` — comma list of event types to forward. Empty = all.
- `ATTACH_DASHCAM_VIDEO` — `1` (default) attaches the dashcam clip; `0`
  posts text only (faster).
- `VIDEO_LOOKBACK_SECONDS` / `VIDEO_LOOKAHEAD_SECONDS` — window around the
  event timestamp when requesting the clip. Defaults 15 / 15.
- `ENABLE_SAFETY_POLLER` — set `1` if your Samsara plan can't push
  webhooks, or as a backstop. Dedup makes it safe to run with webhooks on.

---

## 6. How events are routed

1. Samsara POSTs to `/webhook/samsara` (HMAC-SHA256 signed).
2. `webhook_server.py` verifies signature, parses JSON, hands each event
   to `dispatcher.py`.
3. `dispatcher.py` → normalizes the event, dedupes by id, maps the
   event-type string to a Topic display name via `EVENT_TOPIC_NAMES`,
   ensures the Topic exists, requests the dashcam clip via
   `/cameras/media/retrieval` (lookback 15 s → lookahead 15 s), and sends
   it to Telegram with caption + inline buttons (Open in Samsara, Open in
   Google Maps).
4. If the clip isn't ready after `VIDEO_POLL_SECONDS` of polling, the bot
   falls back to a text-only message with a link to the clip in Samsara.

---

## 7. Troubleshooting

**`probe.py` fails on Safety Events with 403**
Token is missing the *Read Safety Events & Scores* scope. Edit the token
in Samsara → re-run.

**Topics aren't created**
Bot isn't an admin, or the admin role doesn't include *Manage Topics*.
Also: supergroup must have *Topics* enabled.

**Events dedup forever**
Delete `state.json` — rolling cache of last 5000 event ids is kept there.

**Dashcam video is missing**
Not every Samsara plan includes dashcam retrieval. Set
`ATTACH_DASHCAM_VIDEO=0` and you'll still get text alerts with links.

**Webhook returns 401**
`SAMSARA_WEBHOOK_SECRET` in `.env` doesn't match what you passed to
Samsara. Easiest: run `/unregister_webhook <id>` then `/register_webhook`
again — the current secret is pushed up fresh.

---

## 8. Files

```
samsara_safety_bot/
├── bot.py                      # entrypoint — dispatcher + webhook server + poller
├── config.py                   # env-var loader + EVENT_TOPIC_NAMES mapping
├── probe.py                    # Samsara scope check
├── Procfile / railway.json     # Railway deploy
├── requirements.txt
├── .env.example
├── engine/
│   ├── normalize.py            # raw Samsara → internal event shape
│   ├── dispatcher.py           # dedup + topic routing + media fetch + send
│   ├── webhook_server.py       # aiohttp /webhook/samsara + /health
│   └── poller.py               # optional fallback pull loop
├── services/
│   ├── samsara.py              # thin Samsara REST client
│   ├── state.py                # JSON file: topic ids + seen-event cache
│   └── topics.py               # createForumTopic wrapper (deterministic colors)
└── handlers/
    └── commands.py             # /ping /setup_topics /recent /register_webhook …
```
