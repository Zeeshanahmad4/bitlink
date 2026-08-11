const { makeWASocket, useMultiFileAuthState, DisconnectReason, downloadContentFromMessage } = require('@whiskeysockets/baileys');
const { Boom } = require('@hapi/boom');
const express = require('express');
const bodyParser = require('body-parser');
const mime = require('mime-types');
const fs = require('fs');
const path = require('path');
const axios = require('axios');
const pino = require('pino');
const qrcode = require('qrcode-terminal');
require('dotenv').config({ path: '../.env' });

const NOTIFIED_GROUPS_FILE = path.join(__dirname, 'notified_groups.json');
const SLACK_BOT_TOKEN = process.env.SLACK_BOT_TOKEN;
const SLACK_ADMIN_CHANNEL = process.env.SLACK_ADMIN_CHANNEL_ID;
const PORT = process.env.PORT || 3101;

const app = express();
app.use(bodyParser.json({ limit: '50mb' }));

function log(...args) {
    const ts = new Date().toISOString().replace('T', ' ').replace('Z', '');
    console.log(`[${ts}]`, ...args);
}

log("🚀 Starting WhatsApp Service with Baileys...");

let isConnected = false;
let sock = null;

const messageQueue = [];
const messageStore = new Map();
const MAX_STORE_SIZE = 10000;

function storeMessage(msgId, data) {
    messageStore.set(msgId, data);
    if (messageStore.size > MAX_STORE_SIZE) {
        const firstKey = messageStore.keys().next().value;
        messageStore.delete(firstKey);
    }
}

const loadNotifiedGroups = () => {
    try {
        if (fs.existsSync(NOTIFIED_GROUPS_FILE)) {
            const data = fs.readFileSync(NOTIFIED_GROUPS_FILE, 'utf8');
            const groupIds = JSON.parse(data);
            log(`💾 Loaded ${groupIds.length} previously notified groups from file.`);
            return new Set(groupIds);
        }
    } catch (error) {
        log('❌ Error loading notified groups file. Starting fresh.', error);
    }
    log('📄 No notified groups file found. A new one will be created.');
    return new Set();
};

const saveNotifiedGroups = () => {
    try {
        const groupIdsArray = Array.from(notifiedGroups);
        const data = JSON.stringify(groupIdsArray, null, 2);
        fs.writeFileSync(NOTIFIED_GROUPS_FILE, data, 'utf8');
    } catch (error) {
        log('❌ CRITICAL: Failed to save notified groups to file!', error);
    }
};

const notifiedGroups = loadNotifiedGroups();

async function handleNewGroup(groupId, groupName) {
    try {
        if (notifiedGroups.has(groupId)) return;
        notifiedGroups.add(groupId);
        saveNotifiedGroups();

        const displayName = groupName || groupId;
        log(`🚀 New group detected: "${displayName}" (${groupId}). Sending notification.`);

        if (!SLACK_BOT_TOKEN) { log('❌ SLACK_BOT_TOKEN is not set.'); return; }
        if (!SLACK_ADMIN_CHANNEL) { log('❌ SLACK_ADMIN_CHANNEL_ID is not set.'); return; }

        const slackMessage = {
            channel: SLACK_ADMIN_CHANNEL,
            text: `BitLink Bot has a new WhatsApp group: *${displayName}*`,
            blocks: [
                { type: "section", text: { type: "mrkdwn", text: `🚀 BitLink Bot has a new WhatsApp group: *${displayName}*` } },
                {
                    type: "section", fields: [
                        { type: "mrkdwn", text: `*Group Name:*\n${displayName}` },
                        { type: "mrkdwn", text: `*Group ID (for /add-client):*\n\`${groupId}\`` }
                    ]
                }
            ]
        };
        await axios.post('https://slack.com/api/chat.postMessage', slackMessage, {
            headers: { Authorization: `Bearer ${SLACK_BOT_TOKEN}` }
        });
        log(`📬 Successfully sent notification to Slack for group "${displayName}".`);
    } catch (error) {
        log(`❌ Failed to process new group notification:`, error);
    }
}

function extractText(msg) {
    const m = msg.message || msg;
    if (!m) return '';
    return m.conversation ||
        m.extendedTextMessage?.text ||
        m.imageMessage?.caption ||
        m.videoMessage?.caption ||
        m.documentMessage?.caption ||
        m.audioMessage?.caption || '';
}

async function extractQuotedText(msg) {
    try {
        const contextInfo = msg.message?.extendedTextMessage?.contextInfo;
        if (!contextInfo?.stanzaId) return null;

        const quotedId = contextInfo.stanzaId;
        const quotedJid = contextInfo.participant || msg.key.remoteJid;

        if (sock && typeof sock.loadMessage === 'function') {
            const quoted = await sock.loadMessage(quotedJid, quotedId);
            if (quoted) return extractText(quoted);
        }
        return null;
    } catch {
        return null;
    }
}

async function extractMedia(msg) {
    const m = msg.message;
    if (!m) return null;

    const mediaTypes = [
        { key: 'imageMessage', type: 'image' },
        { key: 'videoMessage', type: 'video' },
        { key: 'documentMessage', type: 'document' },
        { key: 'audioMessage', type: 'audio' },
    ];

    for (const { key, type } of mediaTypes) {
        const content = m[key];
        if (!content) continue;
        try {
            const stream = await downloadContentFromMessage(content, type);
            let buffer = Buffer.from([]);
            for await (const chunk of stream) {
                buffer = Buffer.concat([buffer, chunk]);
            }
            return {
                mimetype: content.mimetype || 'application/octet-stream',
                filename: content.fileName || `file.${mime.extension(content.mimetype) || 'bin'}`,
                data: buffer.toString('base64')
            };
        } catch (e) {
            log(`Error downloading ${type} media:`, e.message);
            return null;
        }
    }
    return null;
}

function registerMessageHandlers() {
    sock.ev.on('messages.upsert', async ({ messages, type }) => {
        if (type !== 'notify') return;

        for (const msg of messages) {
            const msgId = msg.key.id;
            if (!msgId || msg.key.fromMe) continue;

            const chatId = msg.key.remoteJid;
            const body = extractText(msg);

            const senderName = msg.pushName ||
                msg.key.participant?.split('@')[0] || 'Unknown';

            const quotedBody = await extractQuotedText(msg);

            storeMessage(msgId, {
                remoteJid: chatId,
                id: msgId,
                fromMe: false,
                participant: msg.key.participant
            });

            const hasMedia = !!(msg.message?.imageMessage ||
                msg.message?.videoMessage ||
                msg.message?.documentMessage ||
                msg.message?.audioMessage);

            if (hasMedia) {
                const media = await extractMedia(msg);
                if (media) {
                    messageQueue.push({
                        chatId, body,
                        timestamp: msg.messageTimestamp,
                        quotedBody, messageId: msgId,
                        senderName, media
                    });
                    log(`📎 Media message received from ${chatId}: ${media.filename}`);
                }
            } else if (body) {
                messageQueue.push({
                    chatId, body,
                    timestamp: msg.messageTimestamp,
                    quotedBody, messageId: msgId,
                    senderName,
                    media: null
                });
                log(`💬 Text message received from ${chatId}: ${body.substring(0, 50)}...`);
            }

            if (chatId.endsWith('@g.us') && !notifiedGroups.has(chatId)) {
                const metadata = await sock.groupMetadata(chatId).catch(() => null);
                await handleNewGroup(chatId, metadata?.subject || chatId);
            }
        }
    });

    sock.ev.on('messages.update', async (updates) => {
        for (const { key, update } of updates) {
            if (!update || !update.message) {
                log(`[DELETE DETECTED] ${key.remoteJid}: msg ${key.id}`);
                try {
                    await axios.post('http://127.0.0.1:8101/whatsapp-delete', {
                        messageId: key.id,
                    });
                } catch (err) {
                    log('Failed to notify bridge about delete:', err.message);
                }
            } else if (update.message) {
                const newText = extractText({ message: update.message });
                if (newText && key.id) {
                    log(`[EDIT DETECTED] ${key.remoteJid}: → "${newText.substring(0, 50)}"`);
                    try {
                        await axios.post('http://127.0.0.1:8101/whatsapp-edit', {
                            messageId: key.id,
                            newText,
                        });
                    } catch (err) {
                        log('Failed to notify bridge about edit:', err.message);
                    }
                }
            }
        }
    });

    sock.ev.on('groups.upsert', async (groups) => {
        for (const group of groups) {
            await handleNewGroup(group.id, group.subject);
        }
    });

    sock.ev.on('group-participants.update', async (update) => {
        if (update.action === 'add' && sock.user?.id) {
            const botId = sock.user.id.split(':')[0] + '@s.whatsapp.net';
            if (update.participants.includes(botId)) {
                const metadata = await sock.groupMetadata(update.id).catch(() => null);
                await handleNewGroup(update.id, metadata?.subject || update.id);
            }
        }
    });
}

async function scanUnmappedGroups() {
    try {
        log('🔍 Performing initial scan for unmapped groups...');
        const groups = await sock.groupFetchAllParticipating();
        let count = 0;
        for (const [id, group] of Object.entries(groups)) {
            if (!notifiedGroups.has(id)) {
                await handleNewGroup(id, group.subject);
                count++;
            }
        }
        log(count > 0 ? `✅ Initial group scan complete. Found ${count} new groups.` : '✅ Initial group scan: all groups already mapped.');
    } catch (error) {
        log('❌ Failed to perform initial group scan:', error.message);
    }
}

async function startSock() {
    const { state, saveCreds } = await useMultiFileAuthState('auth_info');

    sock = makeWASocket({
        auth: state,
        logger: pino({ level: 'info' }),
        syncFullHistory: false,
        getMessage: async (jid, id) => {
            const stored = Array.from(messageStore.values()).find(
                m => m.remoteJid === jid && m.id === id
            );
            return stored || undefined;
        },
    });

    sock.ev.on('creds.update', saveCreds);

    sock.ev.on('connection.update', ({ qr, connection, lastDisconnect }) => {
        if (qr) {
            log('📱 QR Code received — scan with WhatsApp on your phone:');
            qrcode.generate(qr, { small: true });
        }
        if (connection === 'close') {
            isConnected = false;
            const statusCode = lastDisconnect?.error instanceof Boom
                ? lastDisconnect.error.output.statusCode : null;
            const shouldReconnect = statusCode !== DisconnectReason.loggedOut;
            log(shouldReconnect
                ? '🔌 Connection closed. Reconnecting...'
                : '🔌 Connection closed. Logged out — not reconnecting.');
            if (shouldReconnect) setTimeout(startSock, 5000);
            return;
        }

        if (connection === 'open') {
            isConnected = true;
            log('🎉 >>> WhatsApp is ready! (Baileys) <<<');
            registerMessageHandlers();
            scanUnmappedGroups();
        }
    });
}

app.get('/health', (req, res) => {
    res.json({
        status: isConnected ? 'ready' : 'not_ready',
        clients: messageStore.size,
        timestamp: new Date().toISOString()
    });
});

app.get('/get-messages', (req, res) => {
    const messages = messageQueue.splice(0, messageQueue.length);
    res.json(messages);
});

app.post('/send-message', async (req, res) => {
    const { chatId, message, media, mentions } = req.body;

    if (!isConnected) {
        return res.status(503).json({ success: false, error: `Not connected` });
    }
    if (!chatId) {
        return res.status(400).json({ success: false, error: 'chatId is required' });
    }

    try {
        let sentMessage;

        const baseOpts = {};
        if (mentions && Array.isArray(mentions) && mentions.length > 0) {
            baseOpts.mentions = mentions;
        }

        if (media?.data) {
            const buffer = Buffer.from(media.data, 'base64');
            const mimeType = media.mimetype || 'application/octet-stream';
            const filename = media.filename || `file.${mime.extension(mimeType) || 'bin'}`;

            if (mimeType.startsWith('audio/') || filename.toLowerCase().match(/\.(m4a|mp3|wav|ogg|opus)$/)) {
                sentMessage = await sock.sendMessage(chatId, {
                    audio: buffer,
                    mimetype: mimeType,
                    ...(message ? { caption: message } : {}),
                    ...baseOpts
                });
            } else if (mimeType.startsWith('image/') && !['image/svg+xml', 'image/webp'].includes(mimeType)) {
                sentMessage = await sock.sendMessage(chatId, {
                    image: buffer,
                    caption: message || '',
                    ...baseOpts
                });
            } else if (mimeType.startsWith('video/')) {
                sentMessage = await sock.sendMessage(chatId, {
                    video: buffer,
                    caption: message || '',
                    ...baseOpts
                });
            } else {
                sentMessage = await sock.sendMessage(chatId, {
                    document: buffer,
                    fileName: filename,
                    mimetype: mimeType,
                    ...(message ? { caption: message } : {}),
                    ...baseOpts
                });
            }
        } else if (message) {
            sentMessage = await sock.sendMessage(chatId, { text: message, ...baseOpts });
        } else {
            return res.status(400).json({ success: false, error: 'No message or media provided' });
        }

        const msgId = sentMessage?.key?.id;
        if (msgId) {
            storeMessage(msgId, { remoteJid: chatId, id: msgId, fromMe: true });
        }

        log(`✅ Successfully sent message to ${chatId}`);
        res.json({ success: true, messageId: msgId, timestamp: sentMessage?.messageTimestamp || Math.floor(Date.now() / 1000) });
    } catch (error) {
        log(`❌ Failed to send message to ${chatId}:`, error);
        res.status(500).json({ success: false, error: error.toString() });
    }
});

app.post('/delete-message', async (req, res) => {
    const { messageId } = req.body;

    if (!isConnected) {
        return res.status(503).json({ success: false, error: 'WhatsApp client is not ready' });
    }
    if (!messageId) {
        return res.status(400).json({ success: false, error: 'messageId is required' });
    }

    try {
        log(`🗑️  Attempting to delete message: ${messageId}`);
        const key = messageStore.get(messageId);
        if (!key) {
            log(`❌ Message ${messageId} not found in store`);
            return res.status(404).json({ success: false, error: 'Message not found or may have been already deleted' });
        }

        await sock.sendMessage(key.remoteJid, { delete: key });
        messageStore.delete(messageId);
        log(`✅ Successfully deleted message ${messageId}`);
        res.json({ success: true, message: 'Message deleted successfully' });
    } catch (error) {
        log(`❌ Failed to delete message ${messageId}:`, error);
        let errorMessage = 'Message could not be deleted';
        if (error.toString().includes('too old')) {
            errorMessage = 'Message is too old to be deleted (>7 minutes)';
        } else if (error.toString().includes('not found')) {
            errorMessage = 'Message not found or already deleted';
        }
        res.status(500).json({ success: false, error: errorMessage });
    }
});

app.post('/edit-message', async (req, res) => {
    const { messageId, newText } = req.body;

    if (!messageId || !newText) {
        return res.status(400).json({ success: false, error: 'Missing messageId or newText' });
    }

    try {
        const key = messageStore.get(messageId);
        if (!key) {
            return res.status(404).json({ success: false, error: 'Message not found' });
        }

        await sock.sendMessage(key.remoteJid, { text: newText, edit: key });
        log(`✅ Successfully edited message ${messageId}`);
        res.json({ success: true });
    } catch (err) {
        log('Error editing message:', err);
        res.status(500).json({ success: false, error: err.message });
    }
});

app.get('/chat-info/:chatId', async (req, res) => {
    const { chatId } = req.params;

    if (!isConnected) {
        return res.status(503).json({ success: false, error: 'WhatsApp client is not ready' });
    }

    try {
        let result = { id: chatId, isGroup: chatId.endsWith('@g.us'), name: chatId };

        if (chatId.endsWith('@g.us')) {
            const metadata = await sock.groupMetadata(chatId).catch(() => null);
            if (metadata) {
                result.name = metadata.subject;
                result.participantCount = metadata.participants?.length || 0;
            }
        }

        res.json({ success: true, chat: result });
    } catch (error) {
        res.status(500).json({ success: false, error: error.toString() });
    }
});

const server = app.listen(PORT, () => {
    log(`🌐 API server listening at http://localhost:${PORT}`);
    log('📋 Available endpoints:');
    log('   GET  /health - Check service status');
    log('   GET  /get-messages - Retrieve queued messages');
    log('   POST /send-message - Send a message');
    log('   POST /delete-message - Delete a message');
    log('   POST /edit-message - Edit a message');
    log('   GET  /chat-info/:chatId - Get chat information');
});

log('🔄 Initializing WhatsApp client (Baileys)...');
startSock();

async function shutdown() {
    log('\n🛑 Shutting down WhatsApp service (Baileys)...');
    if (sock) {
        try { await sock.end(new Error('shutdown')); } catch {}
        isConnected = false;
    }
    server.close();
    process.exit(0);
}

process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
