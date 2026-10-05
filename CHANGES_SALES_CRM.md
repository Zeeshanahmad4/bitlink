# Sales CRM & Dashboard — Change Log

**Scope of this document:** the features built on the `feature/sales-crm-dashboard` branch, on top of `main` (commit `73e1349`).

**Date:** 2026-10-01
**Branch:** `feature/sales-crm-dashboard`
**Base commit:** `73e1349` (`main` / `origin/main`)

---

## Table of contents

1. [Executive summary](#1-executive-summary)
2. [Feature 1 — Snippets](#2-feature-1--snippets)
3. [Feature 2 — Sales Leaderboard](#3-feature-2--sales-leaderboard)
4. [Feature 3 — Delivery Client Mapping](#4-feature-3--delivery-client-mapping)
5. [Feature 4 — Dashboard theme redesign](#5-feature-4--dashboard-theme-redesign)
6. [Feature 5 — Private channel auto-creation with sales pool members](#6-feature-5--private-channel-auto-creation-with-sales-pool-members)
7. [Feature 6 — Handover to Delivery](#7-feature-6--handover-to-delivery)
8. [Supporting work (built alongside)](#8-supporting-work-built-alongside)
9. [New and modified files](#9-new-and-modified-files)
10. [Verification checklist](#10-verification-checklist)

---

## 1. Executive summary

Six user-facing features were added, all reachable from the web dashboard at `templates/index.html` and served by `control_server.py`:

| # | Feature | Area | Primary file(s) |
|---|---------|------|-----------------|
| 1 | Sales Snippets — reusable reply templates | Dashboard + Slack | `db_store.py`, `control_server.py`, `bitlink_manager.py`, `templates/index.html` |
| 2 | Sales Leaderboard — rep performance ranking | Dashboard | `db_store.py`, `sales_store.py`, `templates/index.html` |
| 3 | Delivery Client Mapping — client → Slack channel registry | Dashboard | `control_server.py`, `schema.sql`, `templates/index.html` |
| 4 | Dashboard theme redesign — violet brand system | Dashboard | `templates/index.html` |
| 5 | Private channel auto-creation with sales pool members added as members | Bridge backend | `sales_provisioner.py` |
| 6 | Handover to Delivery — move a won client to delivery, provision a private channel with project managers | Dashboard + backend | `control_server.py`, `db_store.py`, `templates/index.html` |

A supporting migration moved all CRM state out of Google Sheets into **PostgreSQL** (`db.py`, `db_store.py`, `schema.sql`, `migrate_sheets_to_pg.py`), and an **AI layer** was added (`ai_context.py`, `ai_prompts.py`, `gemini_client.py`, `sales_insights.py`, `sales_ai_jobs.py`).

---

## 2. Feature 1 — Snippets

Reusable, pre-written sales reply templates. Managed from the dashboard, consumed inside Slack.

### What was built

A new `snippets` table holds titled text blocks. They are surfaced in two places:

- **Dashboard view** — a card grid with copy-to-clipboard, inline edit, and delete.
- **Slack command** — `/sales-snippet` (alias `/snippet-sales`) opens a picker that posts the selected snippet into the current channel.

### Storage

`schema.sql:78-83`

```sql
CREATE TABLE IF NOT EXISTS snippets (
    id         BIGSERIAL PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '',
    content    TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### Data access

`db_store.py:648` `get_snippets()`, plus `save_snippet`, `update_snippet`, `delete_snippet`. Exposed through the thin shim in `sales_store.py:37-49`.

### API

`control_server.py`

| Method | Path | Purpose | Line |
|--------|------|---------|------|
| GET | `/api/sales/snippets` | List all snippets | 730 |
| POST | `/api/sales/snippets` | Create | 736 |
| PUT | `/api/sales/snippets/<row_idx>` | Update | 748 |
| DELETE | `/api/sales/snippets/<row_idx>` | Delete | 758 |

### Dashboard UI

- Sidebar nav item **Snippets** — `templates/index.html:631-638`
- View container — `1459`, header `1461-1465`
- Card grid (`md:grid-cols-2 lg:grid-cols-3`) — `1467`, cards `1469`
- Card contents: title, edit/delete icons, monospace content block, copy button — `1472-1491`
- Empty state — `1495-1498`
- Add/Edit modal — `1786-1808` (title + content textarea)
- Page-level **Add Snippet** button in the top bar — `734-741`

### Slack surface

`bitlink_manager.py`

- Command routing — `247-316` (`/sales-snippet`, `/snippet-sales`). Loads snippets async (`267-269`), builds a select-menu modal (`283-312`).
- Submission handler — `469-485`, resolves the chosen snippet and posts its content.

---

## 3. Feature 2 — Sales Leaderboard

Ranks sales reps by closed-won volume, with an active-pipeline tiebreaker.

### What is measured

One aggregated SQL query over `sales_leads`, grouped by owning rep:

`db_store.py:625-641`

```sql
SELECT owner_slack_id,
       COUNT(*) FILTER (WHERE LOWER(stage) = 'won')  AS wins,
       COUNT(*) FILTER (WHERE LOWER(stage) = 'lost') AS lost,
       COUNT(*) FILTER (WHERE LOWER(stage) NOT IN ('won','lost')) AS active
FROM sales_leads
WHERE owner_slack_id <> ''
GROUP BY owner_slack_id
ORDER BY wins DESC, active DESC
```

Notes on the ranking rules:

- **Primary sort: `wins` descending.** Revenue is *not* part of the score — `deal_value` exists on `sales_leads` but is deliberately excluded from this ranking.
- **Tiebreaker: `active` descending.**
- `lost` is returned but does not affect ordering.
- Leads with an empty `owner_slack_id` (unassigned) are excluded.

Exposed as `sales_store.get_leaderboard_metrics()` (`sales_store.py:53`).

### API

`control_server.py:765` — `GET /api/sales/leaderboard`

### Dashboard UI

Two surfaces:

**Dedicated view** — `templates/index.html:1503`

- Sidebar nav item **Leaderboard** — `641-648`
- Header + refresh — `1506-1515`
- **Podium for the top 3** — `1518-1566`. Rank 0 renders as a dark gradient card with a crown; ranks 1-2 as white cards. Shows wins as the hero number and an active/inactive pill.
- **Full rankings table** — `1569-1616`. Columns: Rank, Sales Rep, Slack User ID, Wins, Status.
- Empty state — `1619-1623`

**Embedded widget** — inside the Member Pool view, `1184-1220`. A compact horizontal top-3 card strip with a gold border on first place.

**Client-side join** — `1938-1953`. The frontend merges `leaderboardMetrics` onto `salesPool` by `owner_slack_id` so reps with zero wins still appear. Fetches via `fetchLeaderboard()` at `2221-2229`.

---

## 4. Feature 3 — Delivery Client Mapping

A registry mapping each delivery client to the Slack channel that carries their conversation. This is the data model behind Feature 6.

### Data model

`schema.sql:5-19`

```sql
CREATE TABLE IF NOT EXISTS client_mappings (
    id               BIGSERIAL PRIMARY KEY,
    platform         TEXT        NOT NULL,
    client_name      TEXT        NOT NULL DEFAULT '',
    external_id      TEXT        NOT NULL DEFAULT '',
    slack_channel_id TEXT        NOT NULL DEFAULT '',
    paused           BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

Indexed on platform, channel, and external ID. `paused` is the kill control — a paused mapping is excluded from bridge routing.

`paused` rows still route nothing but remain visible in the UI with an amber **Paused** toggle.

### API

`control_server.py`

| Method | Path | Purpose | Line |
|--------|------|---------|------|
| GET | `/api/delivery/mappings` | List | 180 |
| POST | `/api/delivery/mappings` | Create (and optionally auto-provision a channel) | 199 |
| PUT | `/api/delivery/mappings/<row_idx>` | Update | 308 |
| POST | `/api/delivery/mappings/<row_idx>/pause` | Toggle/set paused | 338 |
| DELETE | `/api/delivery/mappings/<row_idx>` | Delete | 361 |

Every mutation calls `dispatch_bridge_refresh()` so running bridge processes reload the mapping without a restart.

### Dashboard UI

- Sidebar nav item **Delivery** — `570-578`
- View — `830`, header `833-836`
- Search box + platform filter (ALL / WhatsApp / Telegram / Discord) — `840-855`
- Table columns: Client, Platform, External Identity, Slack Channel, Status, Actions — `862-869`
- Loading skeleton rows — `875-891`
- Platform badges and brand icons (`fa-whatsapp`, `fa-telegram`, `fa-discord`) — `898-902`, `2264-2276`
- Pause toggle — `911-917`, handler `2459-2468`
- Edit / Delete — `920-925`, handlers `2396-2407` and `2441-2457`
- Client-side search/filter computed property — `2278-2287`

---

## 5. Feature 4 — Dashboard theme redesign

The dashboard was reskinned from a generic dark UI to a violet brand system. Tailwind is used via CDN with a fully custom theme configuration.

### Typography

`templates/index.html:10-13` — Google Fonts **Plus Jakarta Sans** (300-800) as the UI face, **Space Grotesk** (500, 700) for monospace/display.

### Colour system

Defined in the `tailwind.config` block, `16-203`.

Core tokens:

| Role | Value | Line |
|------|-------|------|
| Canvas | `#F8F7FF` | 30 |
| Card | `#FFFFFF` | 32 |
| Sidebar | `#F0EDFF` | 33 |
| Line / border | `#E5E0F5` | 34 |
| Ink (primary text) | `#24105F` | 36 |
| Muted text | `#756B92` | 37 |
| Primary | `#6D28D9` | 41 |
| Primary hover | `#5B21B6` | 42 |
| Bright accent | `#7C3AED` | 43 |
| Magenta / pink | `#EC0ACB` / `#F02BCF` | 44-45 |
| Selected nav bg | `#E9DEFF` | 46 |
| Warning | `#F59E0B` | 57 |
| Danger | `#E8455F` | 58 |
| Link | `#5B4BE7` | 50 |
| Gateway gradient stops | `#4C1D95` → `#6D28D9` → `#7C3AED` → `#EC0ACB` | 53-56 |

**Important:** the standard Tailwind scales were remapped rather than left alone (`65-137`). `slate` now renders as purple-greys, `emerald` renders as violets, and `indigo`/`sky`/`blue`/`violet` render as purple-blues. This means existing `emerald`/`slate` classes visually changed without touching individual components. `rose` (danger) and `amber` (warning) were deliberately left as red/amber.

### Depth, motion, and finish

- **Shadows** — `card`, `raised`, `lift`, `liftlg`, `glow`, `glowsm`, `glass`, `overlay`, all violet-tinted via `rgba(36,16,95,...)` — `139-151`
- **Keyframes** — `fade-up`, `fade-in`, `scale-in`, `sheen`, `shimmer`, `pulse-ring`, `float-slow` — `152-191`
- **Background textures** — `brand-sheen`, `brand-rule` — `193-196`
- **Radii** — `rounded-4xl` 2.5rem, `rounded-3xl` 1.75rem — `197-199`

### Bespoke components

| Component | What it does | Lines |
|-----------|--------------|-------|
| `.bg-gateway-soft` | Static 135° brand gradient | 221-228 |
| `.bg-gateway-gradient` | Animated drifting gradient, 18s infinite | 343-351 |
| `.btn-emerald` | Primary button (lifts on hover, presses on active) | 230-243 |
| `.glass-bar` | Frosted sticky top bar | 317-322, applied 681 |
| `.nav-rail::before` | Vertical violet→magenta rail on the active sidebar item | 390-403 |
| `.sheen-host` | Light sweep across a card on hover | 325-339, used 800 |
| `.stagger-item` | Entrance animation with per-item `--i` delay | 282-289 |
| `.row-hover` | Inset left violet rail on table row hover | 294-300 |
| `.card-lift` | Compositor-only elevation on hover | 304-313 |
| `.skeleton` | Shimmer loading placeholder | 354-363 |
| `.live-dot` | Breathing pulse indicator | 367-379, used 766, 812 |
| `.tnum` | Tabular numerals for stat alignment | 414 |
| `.overlay-in` / `.dialog-in` | Modal fade + scale entry | 417-423 |

Accessibility: a 2px violet `:focus-visible` outline on every interactive element (`274-278`), and a full `prefers-reduced-motion` override (`436-450`) that restores hidden elements' base opacity and transform.

### Layout

- **App shell** — `453`: flex, `overflow-hidden`, `bg-frame`
- **Login screen** — `458-542`: full-screen overlay, `rounded-4xl` card, `/static/login_logo.png`
- **Sidebar** — `554-673`: fixed `w-56`, `bg-sidebar`, brand block, 8-item nav, avatar/logout footer
- **Sticky top bar** — `681-743`: `.glass-bar`, page title, refresh + contextual primary action
- **Hero stat row** — `754-817`: 3 cards (Active Bridges, Pipeline, dark Gateway Relay card with animated gradient + sheen). Values count up via rAF in `animateCount()` (`1887-1921`)
- **Content well** — `749`, hosting a single white `rounded-3xl` view container at `825`

### Navigation (information architecture)

The previous horizontal pill tab bar was removed; the sidebar now carries navigation (comment at `745`). Items in order (`569-649`):

1. Delivery — `570`
2. Sales CRM — `581`
3. Contacts — `591`
4. Member Pool — `601`
5. Settings — `611`
6. Blocklist — `621`
7. **Snippets** — `631` *(new)*
8. **Leaderboard** — `641` *(new)*

### Other UI elements in the redesign

- **Auth** — login overlay with inline validation and spinner (`458-542`, state at `2000-2006`, `submitLogin` at `2056`)
- **Toasts** — fixed bottom-right stack, auto-dismiss ~3.5s (`1866-1877`, `showToast` at `2102`)
- **Modals** — Delivery Client Add/Edit (`1693`), Handoff Table (`1638`), Add Sales Rep (`1738`), Blocklist (`1762`), Snippet Add/Edit (`1786`), AI Insights (`1810`)
- **Branding assets** — `static/favicon.png`, `static/apple-touch-icon.png`, `static/login_logo.png`

---

## 6. Feature 5 — Private channel auto-creation with sales pool members

### The change

Auto-created lead channels are now **private** by default, and instead of only pushing a notification invite at the team, active sales pool members are **added as channel members**.

Both inbound paths default to private:

- `sales_provisioner.py:470-473` — `provision_whatsapp_lead(..., is_private: bool = True)`
- `sales_provisioner.py:571-574` — `provision_telegram_lead(..., is_private: bool = True)`

### Channel naming

`get_base_channel_name()` (`sales_provisioner.py:87-101`) produces:

```
sale-wa-{lead_slug}-{YYYYMMDD}     # WhatsApp
sale-tg-{lead_slug}-{YYYYMMDD}     # Telegram
```

The slug comes from `sanitize_lead_name()` (`48`), which truncates to 15 characters and falls back to the last digits of the external ID via `extract_last_digits()` (`71`) when the sender supplied no name.

### Collision handling

`create_slack_channel_with_collision_retry()` (`sales_provisioner.py:111-164`) tries three candidates in order:

1. The base name
2. Base + `-%H%M` (`get_collision_channel_name`, line `104`)
3. Base + `-%H%M%S`

If Slack returns `name_taken`, it waits 0.5s and advances. If Slack returns `invalid_name_specials`, it drops the lead name and retries name generation from the external ID only (line `155-158`). Any other API error aborts with `(None, None)`.

### Adding the sales pool

`invite_member_pool_to_channel()` (`sales_provisioner.py:314-382`)

Reads the active pool from the `sales_pool` table via `sales_store.get_active_sales_pool_user_ids()`. Behaviour:

- If the pool is empty, logs and returns `[]` — channel creation still succeeds (`324-326`).
- **Batch first**: one `conversations_invite` call with all IDs joined by comma (`356`).
- **Falls back to individual invites** if the batch is rejected (`370-374`). This exists because a single stale or non-workspace Slack ID causes Slack to reject the *entire* batch — a departed rep would otherwise block the whole team.
- `_invite_one()` (`332-350`) inspects the response body rather than relying on exceptions, because Slack returns `ok: false` (no raised error) for `already_in_channel` and `cant_invite_self`. Both are treated as success.
- Skipped IDs are logged with a pointer to the Member Pool tab (`376-380`).

The master bot (`MASTER_SLACK_BOT_ID`) is invited inside `create_slack_channel_with_collision_retry()` itself (`140-146`) so it is always present regardless of which caller provisioned the channel.

### Topic metadata

`set_channel_topic()` (`sales_provisioner.py:301-311`) writes `"{Platform} Lead: {external_id} | Auto-provisioned by Bitlink Sales"` via `conversations_setTopic`.

### Orchestration

`provision_slack_channel_with_pool()` (`sales_provisioner.py:385-406`) runs the three steps together and returns `(channel_id, channel_name, invited_ids)`.

### Guards that run *before* any of this

`evaluate_lead_guards()` (`sales_provisioner.py:177-296`) returns one of five actions (enum at `169-174`), checked in this order:

| Order | Guard | Action on trigger | Lines |
|-------|-------|-------------------|-------|
| 1 | Blocklist | `BLOCKED_BLOCKLIST` — lead dropped | 185-188 |
| 2 | Deduplication (open lead exists) | `DEDUPLICATED` — reuse existing channel, no new channel | 190-195 |
| 3 | Kill-switch | `BLOCKED_KILLSWITCH` — alert admin channel, no channel created | 200-222 |
| 4 | Manual triage | `ROUTED_TRIAGE` — post lead to triage channel instead of auto-creating | 224-292 |
| 5 | All clear | `PROCEED` | 294-296 |

Two failure modes fail loudly instead of silently dropping the lead: kill-switch with no alert channel (`205-213`) and triage mode with no triage channel (`228-238`) both log `CRITICAL`.

### DM notifications are retained

`notify_pool_members_dm()` (`sales_provisioner.py:792-864`) still DMs the **full active pool**, not just those successfully added. The reason is documented in the code: Slack suppresses Activity notifications for the first message in a brand-new channel, and a private-channel invite is easy to miss, so the DM is the reliable alert. `invited_ids` only controls the wording — "You have been added to this lead channel" vs "You could not be added to the channel automatically" (`838-846`).

### Full inbound flow

`provision_whatsapp_lead()` (`470-568`) / `provision_telegram_lead()` (`571-...`):

1. Evaluate guards
2. Assign round-robin owner — `assign_round_robin_owner()` (`411`)
3. Create private channel + add pool — `provision_slack_channel_with_pool()` (`516`)
4. Record lead in CRM — `record_new_lead_in_crm()` (`435`, called `527`)
5. Post welcome card with owner mention — `post_lead_welcome_card()` (`668`, called `538`)
6. DM the pool — `notify_pool_members_dm()` (`792`, called `550`)

Returns a status dict: `provisioned` | `deduplicated` | `blocked` | `triage` | `error`.

---

## 7. Feature 6 — Handover to Delivery

Moves a won client out of the sales pipeline and into delivery, provisioning a private Slack channel containing the project managers.

### The flow

A two-step modal, both steps driven from the Delivery view.

**Step 1 — pick the client.** The **Add Delivery Client** button in the top bar (`698-705`) opens the handoff table (`1638-1691`). It lists every CRM lead where `stage === 'won'` and not paused (`1665`), showing client name, platform, external ID, deal value, and assigned owner. Each row has a **Handover to Delivery** button (`1677-1680`) calling `startHandoff(lead)` (`2373-2387`), which prefills the form and opens step 2. Platform and client name carry over read-only.

**Step 2 — confirm routing.** The **Complete Delivery Handoff** modal (`1693-1736`) shows the prefilled client and asks for one of:

- **New Delivery Channel Name** (`1720-1722`) — leave the ID empty and the server creates the channel
- **Existing Slack Channel ID** (`1724-1727`) — link a channel that already exists instead of creating one

Submitting calls `POST /api/delivery/mappings` (`2409-2439`).

### Server-side provisioning

`control_server.py:199-306` — `POST /api/delivery/mappings`

1. Validate `client_name` and `external_id` (`210-211`); require at least one of channel ID or channel name (`213-214`, returns 400).
2. **If no channel ID was supplied, provision a private channel** (`216-266`):
   - Pick the bot token by platform — `SLACK_BOT_TOKEN` for WhatsApp, `SLACK_BOT_TOKEN_TELEGRAM` for Telegram with fallback (`222-223`)
   - Normalise the name to `[^a-z0-9-]` and lowercase (`226`). If the name field was left blank entirely, a slug is derived as `del-{client_name}-{last4_of_external_id}` (`228-233`)
   - `conversations_create(name=..., is_private=True)` (`236`) — **private, same as the sales-side channels**
   - **Build the invite list** (`241-258`):
     - the master bot (`MASTER_SLACK_BOT_ID`), then
     - **every active row from the `project_managers` table** via `db_store.get_project_managers()` (`246-248`)
     - If no PMs are found, fall back to the `PROJECT_MANAGER_SLACK_ID` env var (`253-255`) so provisioning still works before anyone populates the table
     - De-duplicate (`258`)
   - Invite them all in one `conversations_invite` call (`260-264`)
   - On any failure here, return 500 with `Failed to auto-provision channel: {e}` (`265-266`)
3. **Idempotency check** — refuse a duplicate `(external_id, slack_channel_id)` pair with HTTP 409 (`273-277`).
4. Insert the `client_mappings` row (`279-280`).
5. **Auto-pause the linked sales lead** — `db_store.update_sales_lead_by_external_id(external_id, {"paused": True})` (`283-286`). This is what stops the sales bot from continuing to answer a client who has moved to delivery. Failure here is logged but does not fail the request.
6. `dispatch_bridge_refresh()` so bridges pick up the new route (`288`).
7. Return 201.

### Orphan cleanup

If step 4 fails after step 2 succeeded, the newly created channel would have no client attached. The handler archives it on a best-effort basis and logs a warning (`293-304`), rather than leaving a stray channel in the workspace.

### Project managers table

`schema.sql:71-76`

```sql
CREATE TABLE IF NOT EXISTS project_managers (
    id            BIGSERIAL PRIMARY KEY,
    slack_user_id TEXT NOT NULL,
    name          TEXT NOT NULL DEFAULT '',
    active        BOOLEAN NOT NULL DEFAULT TRUE
);
```

Read through `db_store.get_project_managers()` (`db_store.py:283-284`), which shares the `_people()` helper (`267`) with the sales pool. Only `active = TRUE` rows are invited.

**Note:** this table is currently provisioned directly in the database. Unlike the sales pool, the Member Pool tab in the dashboard manages `sales_pool` — there is no equivalent UI for `project_managers` yet.

---

## 8. Supporting work (built alongside)

### Google Sheets → PostgreSQL

The CRM previously lived in a spreadsheet. It now lives in Postgres.

- `schema.sql` — 9 tables: `client_mappings`, `sales_leads`, `sales_pool`, `blocklist`, `project_managers`, `snippets`, `settings`, `dashboard_users`
- `db.py` — bounded connection pool (`_BoundedPool`, line `47`), `query_all` / `query_one` / `execute` / `execute_returning_id` / `execute_many` / `run_script`, driven by `DATABASE_URL`
- `db_store.py` — the data access layer. Postgres-only; the Sheets fallback was removed rather than left as a second code path
- `sales_store.py` — a thin shim kept for existing callers
- `migrate_sheets_to_pg.py` — the migration script. Handles the duplicate-keys case in the live Settings sheet explicitly with `ON CONFLICT` rather than relying on accidental last-wins behaviour (`schema.sql:85-87`)
- Surrogate keys replace spreadsheet `row_idx`; the API still returns the field named `row_idx` for compatibility, now holding the real `BIGSERIAL` id

### AI layer

| File | Role |
|------|------|
| `ai_context.py` | Builds a labelled transcript (CLIENT / SALES REP) with speaker classification and full-channel fetch |
| `ai_prompts.py` | Reply and insight prompt templates with strict output rules |
| `gemini_client.py` | Gemini API wrapper with model fallback and retry (`call_gemini_api`, line `34`) |
| `sales_insights.py` | Deterministic win/loss analytics: win rate, median and mean cycle times, value bands, per-dimension breakdowns, deal tables. Renders a facts block (`render_facts_block`, line `545`) that the model narrates over (`render_report`, line `579`) |
| `sales_ai_jobs.py` | Weekly stage inference job, scheduled Sunday 02:00 or run via `--run-now` (line `51`) |

Insights are triggered from the dashboard via `POST /api/sales/insights/generate` (`control_server.py:663`), which proxies to the bridge on port 8101. Results render in the AI Insights modal (`templates/index.html:1810-1863`) with **Re-Analyze** and **Post to Slack** actions.

**Status:** `run_weekly_stage_inference()` currently walks open leads but the actual message-fetch and model-call steps are stubbed with a comment and a 2-second delay for rate limiting (`sales_ai_jobs.py:30-36`). The job runs; it does not yet change stages.

### Ops / deployment

`bitlink.service` (systemd unit), `ecosystem.config.js` (PM2), `nginx-bitlink.conf` (reverse proxy), `start_services.py` (process launcher), `DEPLOYMENT_GUIDE.md`, `schema.sql` bootstrap.

---

## 9. New and modified files

### New

```
schema.sql                    Postgres schema (9 tables)
db.py                         Connection pool + query helpers
db_store.py                   Data access layer
sales_store.py                Thin compat shim over db_store
sales_provisioner.py          Lead intake, guards, channel provisioning
sales_insights.py             Win/loss analytics + report rendering
sales_ai_jobs.py              Scheduled AI jobs
ai_context.py                 Transcript/context builder
ai_prompts.py                 Prompt templates
gemini_client.py              Gemini API wrapper
slack_format.py               Slack message formatting helpers
migrate_sheets_to_pg.py       Google Sheets → Postgres migration
bitlink_manager.py            Slack command router (/handoff, /sales-snippet, ...)
bitlink.service               systemd unit
ecosystem.config.js           PM2 process definitions
nginx-bitlink.conf            Reverse proxy config
start_services.py             Service launcher
control_server.py             Dashboard HTTP API (Flask)
templates/index.html          Dashboard UI
static/                       favicon, apple-touch-icon, login_logo
tests/                        Test suite
test_config.py, test_fake_event.py
DEPLOYMENT_GUIDE.md           Deployment runbook
```

### Modified

```
management_server.py
main_whatsapp.py
main_telegram.py
main_upwork.py
g_sheets_client.py
gemini_enhance.py
tele_backup.py
requirements.txt
whatsapp-bot/service-baileys.js
whatsapp-bot/package-lock.json
```

---

## 10. Verification checklist

Run against a test workspace before merging.

**Snippets**
- [ ] Create a snippet in the dashboard; it appears in the grid
- [ ] Copy button puts content on the clipboard and toasts
- [ ] Edit and delete reflect immediately
- [ ] `/sales-snippet` in Slack lists snippets and posts the selected one

**Leaderboard**
- [ ] Reps with wins rank above reps without
- [ ] A rep with zero wins still appears (joined from the pool)
- [ ] The top-3 podium and the Member Pool widget agree

**Delivery mapping**
- [ ] Search and platform filter narrow the table
- [ ] Pause toggles and survives a refresh
- [ ] Edits and deletes persist

**Theme**
- [ ] No leftover hardcoded colours clash with the violet palette
- [ ] Keyboard focus is visible on every control
- [ ] `prefers-reduced-motion` suppresses animation without hiding content

**Private channels + pool**
- [ ] A new inbound lead creates a **private** channel
- [ ] Active pool members are members of it
- [ ] A stale Slack ID in the pool does not block the other invites
- [ ] An already-present member does not produce an error
- [ ] A duplicate inbound message reuses the existing channel
- [ ] A blocklisted number creates nothing
- [ ] Kill-switch and triage mode both halt creation

**Handover to delivery**
- [ ] Only `won`, non-paused leads appear in the picker
- [ ] Providing a name creates a private channel with the PMs inside it
- [ ] Providing an existing ID creates no new channel
- [ ] The originating sales lead is paused afterwards
- [ ] Submitting the same mapping twice returns 409
- [ ] A DB failure after channel creation archives the orphan channel
