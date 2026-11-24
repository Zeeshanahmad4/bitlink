# BitLink Bidirectional Threading & Native Message Replies Plan

**Document Version:** 1.0  
**Status:** Planned / Ready for Implementation  
**Target Systems:** WhatsApp (Baileys), Telegram (Telethon), Slack Workspace  

---

## 1. Overview & Goal

Enable seamless, two-way threaded communication between Slack and messaging platforms (WhatsApp & Telegram):
1. **Slack Thread ➔ Client Native Quote/Reply**: When an agent replies inside a Slack message thread, the client on WhatsApp or Telegram receives it as a native quoted reply pointing to that specific original message.
2. **Client Quoted Reply ➔ Slack Thread**: When a client on WhatsApp or Telegram long-presses/swipes to reply to a specific previous message, the incoming message is automatically posted inside the corresponding Slack thread instead of cluttering the main channel.

---

## 2. Architecture & Data Flow

```
                                  [SLACK]
                                     ▲
             (Client quotes msg)     │  (Agent replies in thread)
           Inbound with thread_ts    │  Outbound with parent thread_ts
                                     ▼
                      [PostgreSQL: message_mappings]
                         (slack_ts ↔ external_msg_id)
                                     ▲
             (Client quotes msg)     │  (Outbound with quote/reply_to)
            stanzaId / reply_to_id   │  quoted key / reply_to
                                     ▼
                       [WHATSAPP]   &   [TELEGRAM]
```

### The Two Workflows

#### A. Outbound Flow (Slack Thread ➔ Client Native Reply)
1. Team member replies to a message in Slack under a thread.
2. Slack event emits `thread_ts` (root parent message timestamp) and `ts` (reply message timestamp).
3. The bridge queries PostgreSQL `message_mappings` using `(channel_id, thread_ts)` to find the original `external_msg_id`.
4. **WhatsApp Dispatch**:
   - `wa-bridge` sends `quotedMessageId = external_msg_id` to Node Gateway (`/send-message`).
   - Baileys invokes `sock.sendMessage(chatId, { text: ... }, { quoted: { key: { remoteJid, id: quotedMessageId } } })`.
   - The customer sees a native WhatsApp quoted reply bubble.
5. **Telegram Dispatch**:
   - `tg-bridge` invokes `telethon_client.send_message(chat_id, text, reply_to=int(external_msg_id))`.
   - The customer sees a native Telegram reply bubble.

#### B. Inbound Flow (Client Quoted Message ➔ Slack Thread)
1. Customer quotes an earlier message on WhatsApp or Telegram and replies.
2. **ID Extraction**:
   - **WhatsApp**: Baileys extracts `contextInfo.stanzaId` (the message ID of the quoted item) and sends it in the webhook payload.
   - **Telegram**: Telethon reads `event.message.reply_to_msg_id`.
3. The bridge queries `message_mappings` where `external_msg_id = quoted_id`.
4. If a matching `slack_ts` is found:
   - The bridge calls `chat.postMessage(channel=channel_id, thread_ts=parent_slack_ts, text=...)`.
   - The message lands directly in the Slack thread of that parent message.
5. If no matching `slack_ts` is found (e.g. quoted message occurred before feature deployment):
   - The message posts to the main channel normally, optionally prepended with a quote preview: `> [Replying to previous message]`.

---

## 3. Database Schema Changes

A shared PostgreSQL table `message_mappings` ensures message references persist across container restarts and are accessible to all bridge instances.

### Table Definition (`schema.sql`)
```sql
CREATE TABLE IF NOT EXISTS message_mappings (
    id BIGSERIAL PRIMARY KEY,
    platform VARCHAR(16) NOT NULL,            -- 'whatsapp' or 'telegram'
    channel_id VARCHAR(64) NOT NULL,          -- Slack channel ID
    slack_ts VARCHAR(32) NOT NULL,            -- Slack message timestamp
    external_msg_id VARCHAR(128) NOT NULL,    -- WhatsApp stanzaId or Telegram message_id
    chat_id VARCHAR(128) NOT NULL,            -- WhatsApp JID or Telegram Chat ID
    direction VARCHAR(8) NOT NULL,            -- 'inbound' or 'outbound'
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Fast lookups for Slack thread -> Client quote
CREATE INDEX IF NOT EXISTS idx_msg_map_slack ON message_mappings (channel_id, slack_ts);

-- Fast lookups for Client quote -> Slack thread
CREATE INDEX IF NOT EXISTS idx_msg_map_ext ON message_mappings (platform, external_msg_id);

-- 30-Day auto-prune query (run periodically or via cron/ai-jobs)
-- DELETE FROM message_mappings WHERE created_at < NOW() - INTERVAL '30 days';
```

---

## 4. Required Code Changes

### Phase 1: Database Helper Functions (`db_store.py`)
Add helper functions to record and look up message mappings:
```python
def record_message_mapping(platform: str, channel_id: str, slack_ts: str, external_msg_id: str, chat_id: str, direction: str):
    """Saves a 1-to-1 link between a Slack message ts and an external WhatsApp/Telegram ID."""
    with db._cursor(commit=True) as cur:
        cur.execute("""
            INSERT INTO message_mappings (platform, channel_id, slack_ts, external_msg_id, chat_id, direction)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
        """, (platform, channel_id, slack_ts, str(external_msg_id), str(chat_id), direction))

def get_external_id_by_slack_ts(channel_id: str, slack_ts: str) -> Optional[str]:
    """Retrieves the WhatsApp stanzaId or Telegram message_id for a given Slack thread root."""
    row = db.query_one("""
        SELECT external_msg_id FROM message_mappings
        WHERE channel_id = %s AND slack_ts = %s
        ORDER BY id DESC LIMIT 1
    """, (channel_id, slack_ts))
    return row.get("external_msg_id") if row else None

def get_slack_ts_by_external_id(platform: str, external_msg_id: str) -> Optional[str]:
    """Retrieves the Slack timestamp for an incoming quoted message ID."""
    row = db.query_one("""
        SELECT slack_ts FROM message_mappings
        WHERE platform = %s AND external_msg_id = %s
        ORDER BY id DESC LIMIT 1
    """, (platform, str(external_msg_id)))
    return row.get("slack_ts") if row else None
```

---

### Phase 2: WhatsApp Gateway (`whatsapp-bot/service-baileys.js`)

1. **Inbound Extraction (`processIncomingMessage`)**:
   Pass `quotedMessageId` from `contextInfo.stanzaId` to the Python bridge:
   ```javascript
   const contextInfo = msg.message?.extendedTextMessage?.contextInfo;
   const quotedMessageId = contextInfo?.stanzaId || null;
   ```
2. **Outbound Quoting (`/send-message`)**:
   Support `quotedMsgId` in request payload:
   ```javascript
   if (quotedMsgId) {
       sendOptions.quoted = {
           key: {
               remoteJid: chatId,
               id: quotedMsgId
           }
       };
   }
   await sock.sendMessage(chatId, content, sendOptions);
   ```

---

### Phase 3: WhatsApp Bridge (`main_whatsapp.py`)

1. **Inbound Processing**:
   - When posting a new message to Slack:
     - Check if payload has `quotedMessageId`.
     - Call `db_store.get_slack_ts_by_external_id('whatsapp', quotedMessageId)`.
     - If found, pass `thread_ts=parent_slack_ts` to `chat_postMessage`.
   - Store every inbound message mapping:
     - `db_store.record_message_mapping('whatsapp', channel_id, posted_ts, wa_msg_id, chat_id, 'inbound')`.
2. **Outbound Processing (`process_slack_to_whatsapp`)**:
   - Check if `event.get("thread_ts")` exists and is different from `event.get("ts")`.
   - If in a thread:
     - Query `db_store.get_external_id_by_slack_ts(channel_id, thread_ts)`.
     - Pass `quotedMsgId` in payload to `POST /send-message`.
   - When message is successfully sent, store the outbound mapping.

---

### Phase 4: Telegram Bridge (`main_telegram.py`)

1. **Inbound Processing**:
   - Read `event.message.reply_to_msg_id`.
   - If present, query `db_store.get_slack_ts_by_external_id('telegram', reply_to_msg_id)`.
   - If found, post to Slack with `thread_ts=parent_slack_ts`.
   - Store mapping in `message_mappings`.
2. **Outbound Processing**:
   - Check if `event.get("thread_ts")` is set.
   - If in a thread:
     - Query `db_store.get_external_id_by_slack_ts(channel_id, thread_ts)`.
     - If found, pass `reply_to=int(parent_tg_msg_id)` to `telethon_client.send_message()`.
   - Store outbound mapping.

---

## 5. Edge Cases & Fallback Behavior

| Edge Case | Handled Behavior |
| :--- | :--- |
| **Agent replies in thread to message sent before this update** | If parent `slack_ts` is not found in `message_mappings`, the message falls back to sending as a normal non-quoted message to the client. No messages dropped. |
| **Client quotes a message deleted in Slack** | If parent `slack_ts` is invalid/deleted, posts to the main channel with fallback text: `> [Replying to previous message]`. |
| **Normal Slack messages (not in thread)** | Delivered directly as normal chat messages without quotes. |
| **Multi-level nested threads** | Slack threads always reference the root `thread_ts`. All replies in the thread anchor back to the original parent message. |

---

## 6. Verification & Testing Checklist

- [ ] **WhatsApp Outbound**: Reply inside a thread in Slack $\rightarrow$ Client receives WhatsApp message with quoted bubble.
- [ ] **WhatsApp Inbound**: Client quotes a message on WhatsApp $\rightarrow$ Message appears inside the Slack thread.
- [ ] **Telegram Outbound**: Reply inside a thread in Slack $\rightarrow$ Client receives Telegram message with reply quote.
- [ ] **Telegram Inbound**: Client quotes a message on Telegram $\rightarrow$ Message appears inside the Slack thread.
- [ ] **Direct Messages**: Normal messages outside threads continue to post to the main channel without alteration.
- [ ] **Restart Stability**: Verify mappings survive Docker container restarts via PostgreSQL storage.
