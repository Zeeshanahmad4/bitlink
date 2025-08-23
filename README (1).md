# Bitlink — Multi‑Platform Client Communication Hub

> Centralize client conversations from **WhatsApp, Telegram, Discord** (and more) directly **inside Slack** — without asking clients to switch apps.

[![Star](https://img.shields.io/github/stars/bitbash-marketing/bitlink?style=social)](#)
[![Slack App](https://img.shields.io/badge/Slack-App-4A154B?logo=slack&logoColor=white)](#)
[![WhatsApp Cloud API](https://img.shields.io/badge/WhatsApp-Cloud%20API-25D366?logo=whatsapp&logoColor=white)](#)
[![Telegram Bot API](https://img.shields.io/badge/Telegram-Bot%20API-26A5E4?logo=telegram&logoColor=white)](#)
[![Discord Bot](https://img.shields.io/badge/Discord-Bot-5865F2?logo=discord&logoColor=white)](#)

---

## TL;DR
**Bitlink mirrors every client chat into Slack** in real time and lets your team reply from Slack back to the client’s original app. You keep your internal workflow; clients keep theirs. Result: *one place to see, search, and act on everything*.

---

## Problem Statement (Bitbash story)

At **Bitbash**, we work with clients who *live* on different apps — some on WhatsApp, others on Telegram or Discord. We tried moving everyone into a single system (Slack), but most clients prefer staying where they are. This led to:

- Scattered chats across months and apps, hidden from the rest of the team
- Missed context, repeated questions, inconsistent follow‑ups
- No single source of truth for PMs and developers

**Bitlink** solves this by bridging those apps *into Slack* — so the team gets full visibility without forcing clients to move.

### Outcomes
- **One inbox for the team**: every client conversation is visible in Slack
- **No app switching** for clients; **no blind spots** for the team
- **Searchable history** with metadata for audits and onboarding
- **Faster response times** and tighter project coordination

---

## What Bitlink Does

- **Two‑way sync** between Slack and client platforms (WhatsApp, Telegram, Discord; extensible).
- **Consistent channel mapping**: `#client-<company>-dm` per client/company.
- **Message capture & dedupe** to prevent loops and double‑posts.
- **Searchable history** in Slack with message metadata stored in the DB.
- **Role‑based isolation** so each developer only sees their own clients.

> Optional add‑ons: tagging, auto‑assignment, SLA timers, sentiment/NLP labels, CRM sync (Airtable/HubSpot), basic analytics.

---

## Core Flow (All Platforms)

**Inbound (Client → Slack)**
1. Listen for new messages via each platform’s official API webhook.
2. Capture **sender_id**, **message_id**, **platform**, **content**, **timestamp**.
3. **Deduplicate** (DB check by `(platform, message_id)`); drop if already seen.
4. Resolve Slack channel via mapping table; post to the matching `#client-<company>-dm`.

**Outbound (Slack → Client)**
1. Listen for new messages in a mapped Slack channel (bot events/Socket Mode).
2. Identify target **platform + client_id** from the channel binding.
3. Send the message through the platform’s send API (DM or chat); persist message & status.

---

## Features

- **Real‑time mirroring** of messages & attachments
- **Replies route back** to the correct app & user, automatically
- **Message dedupe** to avoid loops/echoes
- **Channel naming convention** for consistency: `#client-<company>-dm`
- **Per‑developer isolation** (multi‑tenant projects)
- **Audit trail & searchable metadata** (message ids, sender ids, timestamps)
- **Extensible platform adapters**: add more channels later without touching core logic

---

## Architecture (High‑Level)

```
+-----------+     +-----------------+     +----------------+     +----------------+
| WhatsApp  | --> | Ingestor (webhook) --> | Normalizer     | --> | Dedupe+Store  |
| Telegram  | --> | Ingestor (webhook) --> | (unify schema) | --> | (Postgres)    |
| Discord   | --> | Ingestor (gateway) --> |                | --> | + Redis cache |
+-----------+     +-----------------+     +----------------+     +----------------+
                                                             |
                                                             v
                                               +-------------------------+
                                               | Slack Notifier (Bolt)  |
                                               | Post to #client-*      |
                                               +-------------------------+
                                                             |
                                                             v
                                               +-------------------------+
                                               | Dispatcher              |
                                               | Slack → Platform Send   |
                                               +-------------------------+
```

**Components**
- **Adapters**: WhatsApp Cloud API, Telegram Bot API, Discord bot (official SDKs/wrappers)
- **Core service**: TypeScript/Node (NestJS/Express) with a clean adapter interface
- **DB**: Postgres (Prisma), **Cache/Queue**: Redis
- **Slack app**: Bolt (Events API/Socket Mode), channel mapping
- **Infra**: Docker Compose; deploy anywhere (Render/EC2/Fly/Heroku)

---

## Data Model (Minimal)

- `platforms` — id, name, settings  
- `contacts` — id, platform, external_user_id, display_name  
- `channels_map` — contact_id ↔ slack_channel_id, project_id/owner_id  
- `messages` — id, platform, message_id, contact_id, direction(in|out), content, timestamps, status  
- `attachments` — message_id, type, url/meta  

Deduplication key: **`(platform, message_id)`**. Outbound stores provider response id for traceability.

---

## Slack Conventions

- Default channel naming: **`#client-<company>-dm`**
- Bot posts include **origin tag** (`[WA]`, `[TG]`, `[DC]`) and sender display
- Replying in that channel sends back to the mapped client
- Use threads for sub‑topics; top‑level mirrors the latest messages

---

## Quick Start (Self‑Hosted Template)

> Use in two modes: **(A) hire us to deploy & manage for you**, or **(B) self‑host** using the template below.

### 1) Clone & configure
```bash
# replace with your repo URL
git clone https://github.com/bitbash-marketing/bitlink.git
cd bitlink
cp .env.example .env
```

**`.env.example`** (add real secrets)
```env
# General
APP_BASE_URL=https://your-domain.com
NODE_ENV=production

# Database
DATABASE_URL=postgresql://bitlink:bitlink@db:5432/bitlink

# Redis
REDIS_URL=redis://redis:6379

# Slack
SLACK_SIGNING_SECRET=xxx
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_LEVEL_TOKEN=xapp-...

# WhatsApp Cloud API (Meta)
WA_VERIFY_TOKEN=choose-a-verify-token
WA_ACCESS_TOKEN=EAA...
WA_PHONE_NUMBER_ID=123456789

# Telegram
TELEGRAM_BOT_TOKEN=12345:abcdef

# Discord
DISCORD_BOT_TOKEN=...
DISCORD_APP_ID=...
```

### 2) Run with Docker
```bash
docker compose up -d --build
# then run database migrations inside the api container
docker compose exec api npx prisma migrate deploy
```

### 3) Set webhooks
- **WhatsApp Cloud**: `POST {APP_BASE_URL}/webhooks/whatsapp`
- **Telegram**: `https://api.telegram.org/bot<token>/setWebhook?url={APP_BASE_URL}/webhooks/telegram`
- **Discord**: Bot online via gateway; set intents & add to server.
- **Slack**: Events URL → `{APP_BASE_URL}/webhooks/slack/events` (or use Socket Mode).

---

## Security & Compliance

- Official APIs only; **no scraping or ToS violations**
- Verify webhook signatures (Slack), app secrets (Meta), and bot tokens
- Principle of least privilege OAuth scopes
- Per‑tenant separation; audit logs for all outbound sends
- GDPR‑aware: easy export/delete by contact id

---

## Limits & Caveats

- **WhatsApp**: business messaging policies & rate limits apply; templates required for outbound initiations
- **Telegram/Discord**: respect rate limits; handle attachment size caps
- **Slack**: large files stored via links; use threads to reduce channel noise

---

## Roadmap

- Email (IMAP/SMTP) bridge
- Auto‑tagging & sentiment labels
- SLA timers + escalations
- Command palette (e.g., `/bitlink assign @alice`) for ops shortcuts
- Insights dashboard: response times, message volume, breached SLAs

---

## Who Uses Bitlink

- **Agencies** managing dozens of clients across apps
- **Product teams** offering support via WhatsApp/Telegram but coordinating in Slack
- **Consultants** who want a clean archive of client decisions inside Slack

> Internal to Bitbash, Bitlink eliminates DM blind spots and cuts context‑switching. Your team can get the same outcome in days, not months.

---

## Demo

Add a short Loom or GIF showing: a client DM → mirrored in Slack → reply from Slack → delivered back to client.

```
[![Watch a 1‑min demo](./docs/demo-thumb.png)](YOUR_LOOM_LINK)
```

---

## Tech Stack

TypeScript · Node.js (NestJS/Express) · Slack Bolt · WhatsApp Cloud API · Telegram Bot API · Discord.js · Postgres (Prisma) · Redis · Docker

---

## Work With Us / Contact

Want the **managed** version (we host, monitor, and support), or help standing this up for your team?

- Email: **hello@bitbash.agency** *(replace with your preferred inbox)*
- Slack Connect: *(share an invite link)*
- Book a call: *(insert your Calendly/Cal.com link)*

> Agencies: ask about **white‑label** deployment.

---

## Contributing

PRs & issues welcome. Please open an issue to discuss new adapters or integrations.

---

## License

© Bitbash. All rights reserved.  
Commercial license available for managed deployments. For self‑hosted usage, please check the `LICENSE` file when added.

---

## Appendix — Implementation Notes

- **Outbound loop protection**: mark Slack‑originated messages with internal headers/metadata so inbound filters ignore them on the return path.
- **Message identity**: store provider message IDs and Slack `ts` values for cross‑references.
- **Retries**: exponential backoff on provider 429/5xx; dead‑letter queue on persistent failures.
- **Attachments**: store metadata; for large files, post links in Slack rather than re‑uploading.
- **Observability**: per‑adapter health checks; message latency histogram; error budget SLOs.

---

> ⭐ If this is useful, **star the repo** and share feedback — it helps others find Bitlink and keeps development moving.
