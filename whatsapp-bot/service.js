// service.js - FINAL, PRODUCTION-READY VERSION with Deletion Support - UPDATED



const { Client, LocalAuth, MessageMedia } = require('whatsapp-web.js');
const express = require('express');
const qrcode = require('qrcode-terminal');
const bodyParser = require('body-parser');
const mime = require('mime-types');
const puppeteer = require('puppeteer-extra');
const StealthPlugin = require('puppeteer-extra-plugin-stealth');
const fs = require('fs'); // <-- ADD THIS
const path = require('path'); // <-- ADD THIS
puppeteer.use(StealthPlugin());
const axios = require('axios');
require('dotenv').config({ path: '../.env' });
const NOTIFIED_GROUPS_FILE = path.join(__dirname, 'notified_groups.json'); // <-- ADD THIS
const SLACK_BOT_TOKEN = process.env.SLACK_BOT_TOKEN;
const SLACK_ADMIN_CHANNEL = process.env.SLACK_ADMIN_CHANNEL_ID;
const app = express();
app.use(bodyParser.json({ limit: '50mb' }));

function log(...args) {
    const ts = new Date().toISOString().replace('T', ' ').replace('Z', '');
    console.log(`[${ts}]`, ...args);
}

log("🚀 Starting WhatsApp Service with Standard Stealth Mode...");

const client = new Client({
    authStrategy: new LocalAuth({
        clientId: "bitlink",
        dataPath: "./sessions"
    }),
    puppeteer: {
        headless: 'new',
        args: [
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-gpu',
            '--disable-dev-shm-usage',
            '--disable-web-security',
            '--disable-features=VizDisplayCompositor',
            '--disable-extensions'
        ],
    }
});

let isReady = false;

// --- WhatsApp Client Event Handlers ---
client.on('qr', (qr) => {
    log('📱 QR code received, please scan in your terminal:');
    qrcode.generate(qr, { small: true });
});

client.on('authenticated', () => {
    log('✅ Authentication successful! Initializing client...');
});

// service.js - ADD THIS ENTIRE BLOCK

// --- Proactive Group Discovery on Startup ---
client.on('ready', async () => {
    isReady = true;
    log('🎉 >>> WhatsApp is ready! <<<');

    try {
        log('🔍 Performing initial scan for unmapped groups...');
        const allChats = await client.getChats();
        
        for (const chat of allChats) {
            if (chat.isGroup) {
                // We pass every group to our intelligent handler.
                // It will use its 'notifiedGroups' memory to ensure
                // it only sends a notification for truly new groups.
                await handleNewGroup(chat);
            }
        }
        log('✅ Initial group scan complete.');
    } catch (error) {
        log('❌ Failed to perform initial group scan:', error);
    }
});
// service.js - NEW, UNIFIED CODE BLOCK TO PASTE

// --- Group Notification Logic ---

// service.js - PASTE THIS ENTIRE BLOCK TO REPLACE ALL GROUP/MESSAGE HANDLING LOGIC

// --- UNIFIED NEW GROUP & MESSAGE HANDLING ---
/**
 * Loads the list of already notified group IDs from a JSON file.
 * @returns {Set<string>} A set containing the group IDs.
 */
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

/**
 * Saves the current set of notified group IDs to the JSON file.
 */
const saveNotifiedGroups = () => {
    try {
        const groupIdsArray = Array.from(notifiedGroups);
        const data = JSON.stringify(groupIdsArray, null, 2); // Pretty-prints the JSON
        fs.writeFileSync(NOTIFIED_GROUPS_FILE, data, 'utf8');
    } catch (error) {
        log('❌ CRITICAL: Failed to save notified groups to file!', error);
    }
};
// This Set acts as our memory. It is defined here in the global scope.
const notifiedGroups = loadNotifiedGroups();

/**
 * The single, intelligent function to handle any new group.
 * It will only process a given group ID once.
 * @param {object} chat - The whatsapp-web.js Chat object for the group.
 */
const handleNewGroup = async (chat) => {
    try {
        const groupId = chat.id._serialized;
        const groupName = chat.name;

        // 1. Check our memory. If we've already handled this group, do nothing.
        if (notifiedGroups.has(groupId)) {
            return; // Already processed.
        }

        // 2. Add to memory IMMEDIATELY to prevent duplicate notifications.
        notifiedGroups.add(groupId);
        saveNotifiedGroups(); 
        log(`🚀 New group detected: "${groupName}" (${groupId}). Sending notification.`);

        // 3. Send the notification to Slack.
        if (!SLACK_BOT_TOKEN) {
            log('❌ SLACK_BOT_TOKEN is not set.');
            return;
        }
        if (!SLACK_ADMIN_CHANNEL) {
            log('❌ SLACK_ADMIN_CHANNEL_ID is not set.');
            return;
        }

        const slackMessage = {
            channel: SLACK_ADMIN_CHANNEL,
            text: `BitLink Bot has a new WhatsApp group: *${groupName}*`,
            blocks: [
                { "type": "section", "text": { "type": "mrkdwn", "text": `🚀 BitLink Bot has a new WhatsApp group: *${groupName}*` } },
                {
                    "type": "section", "fields": [
                        { "type": "mrkdwn", "text": `*Group Name:*\n${groupName}` },
                        { "type": "mrkdwn", "text": `*Group ID (for /add-client):*\n\`${groupId}\`` }
                    ]
                }
            ]
        };
        await axios.post('https://slack.com/api/chat.postMessage', slackMessage, {
            headers: { Authorization: `Bearer ${SLACK_BOT_TOKEN}` }
        });
        log(`📬 Successfully sent notification to Slack for group "${groupName}".`);

    } catch (error) {
        log(`❌ Failed to process new group notification for chat "${chat.name}":`, error);
    }
};

// --- Event Listeners ---

// Case 1: The bot is ADDED to a group by someone else.
client.on('group_join', async (notification) => {
    const chat = await notification.getChat();
    handleNewGroup(chat);
});

// Case 2: The bot CREATES a group programmatically.
client.on('group_create', async (chat) => {
    handleNewGroup(chat);
});

// Case 3 & Normal Message Handling: A message arrives.
// This single handler now performs BOTH discovery and regular message processing.
client.on('message', async (msg) => {
    // --- Discovery Logic for Manual Creation ---
    if (msg.from.endsWith('@g.us')) {
        // This check is safe because notifiedGroups is defined in the outer scope.
        if (!notifiedGroups.has(msg.from)) {
            const chat = await msg.getChat();
            await handleNewGroup(chat);
        }
    }

    // --- Original Message Processing Logic ---
    let quotedBody = null;
    let senderName = null;

    if (msg.hasQuotedMsg) {
        try {
            const quotedMsg = await msg.getQuotedMessage();
            if (quotedMsg && quotedMsg.body) { quotedBody = quotedMsg.body; }
        } catch (error) { log("Could not get quoted message:", error); }
    }

    try {
        const contact = await msg.getContact();
        senderName = contact?.pushname || contact?.name || contact?.number || 'Unknown';
    } catch (error) {
        log("Could not get sender contact:", error);
    }

    const messageData = {
        chatId: msg.from,
        body: msg.body,
        timestamp: msg.timestamp,
        quotedBody: quotedBody,
        messageId: msg.id._serialized,
        senderName: senderName
    };

    if (msg.hasMedia) {
        try {
            const media = await msg.downloadMedia();
            if (media) {
                const extension = mime.extension(media.mimetype);
                const filename = media.filename || `file.${extension}` || 'file.bin';
                messageData.media = {
                    mimetype: media.mimetype,
                    filename: filename,
                    data: media.data
                };
                messageQueue.push(messageData);
                log(`📎 Media message received from ${msg.from}: ${filename}`);
            }
        } catch (error) {
            log("Error downloading media:", error);
            messageData.media = null;
            messageQueue.push(messageData);
        }
    } else {
        messageData.media = null;
        messageQueue.push(messageData);
    const ts = new Date().toISOString();
    log(`[${ts}] 💬 Text message received from ${msg.from}: ${msg.body.substring(0, 50)}...`);
    }
});
client.on('auth_failure', msg => {
    log('❌ AUTHENTICATION FAILURE', msg);
    isReady = false;
});

client.on('disconnected', (reason) => {
    log('🔌 Client was logged out:', reason);
    isReady = false;
});

// --- Message Queue for API ---
const messageQueue = [];

// --- Enhanced Message Handler with messageId for deletion tracking ---
// In service.js, replace your entire old 'message' handler with this one.

// In service.js, replace your message handler with this corrected version



// Health check endpoint
// In service.js, replace your old '/health' endpoint with this one.

app.get('/health', async (req, res) => {
    try {
        // <<< THIS IS THE KEY CHANGE >>>
        // Instead of using our own variable, we ask the client for its real-time state.
        const state = await client.getState();

        // The state will be 'CONNECTED' when it's fully ready.
        const isClientReady = (state === 'CONNECTED');

        res.json({
            status: isClientReady ? 'ready' : 'not_ready',
            state: state, // Also return the raw state for better debugging
            timestamp: new Date().toISOString()
        });
    } catch (error) {
        // If getState() fails, it means the client is definitely not ready.
        res.status(500).json({
            status: 'not_ready',
            error: 'Client is in an error state.',
            timestamp: new Date().toISOString()
        });
    }
});

// Get queued messages
app.get('/get-messages', (req, res) => {
    const messages = messageQueue.splice(0, messageQueue.length);
    res.json(messages);
});

// Send message endpoint with enhanced response
// The simplified, stable version for service.js

app.post('/send-message', async (req, res) => {
    const { chatId, message, media, mentions } = req.body;

    const currentState = await client.getState();
    if (currentState !== 'CONNECTED') {
        const errorMsg = `WhatsApp client is not ready. Current state: ${currentState}`;
        log(`❌ Blocked send request: ${errorMsg}`);
        return res.status(503).json({ success: false, error: errorMsg });
    }

    if (!chatId) {
        return res.status(400).json({ success: false, error: 'chatId is required' });
    }

    try {
        const chat = await client.getChatById(chatId);
        if (!chat) {
            throw new Error(`Chat not found for ID: ${chatId}`);
        }

        let mentionContacts = [];
        if (mentions && Array.isArray(mentions) && chat.isGroup) {
            log("🔍 Mentions received from Python:", mentions);
            const participants = chat.participants || [];
            log("🔍 Group participants:", participants.map(p => p.id._serialized));
            if (participants.length > 0) {
                log("🔍 DEBUG - First participant structure:", JSON.stringify(participants[0], null, 2));
            }
            // Extract phone numbers from mention IDs
            const mentionNumbers = mentions.map(m => m.split('@')[0]);
            mentionContacts = participants
                .filter(p => p && p.id && p.id._serialized)
                .filter(p => {
                    const participantNumber = p.id._serialized.split('@')[0];
                    return mentionNumbers.includes(participantNumber);
                })
                .map(p => {
                    // Safely get the serialized ID
                    if (p && p.id && p.id._serialized) {
                        return p.id._serialized;
                    }
                    // If structure is different, try alternative access
                    if (p && p._serialized) {
                        return p._serialized;
                    }
                    if (p && typeof p === 'string') {
                        return p;
                    }
                    console.warn("⚠️ Could not extract ID from participant:", p);
                    return null;
                })
                .filter(id => id !== null); // Remove any null entries
        }

        // DEBUG: Log what we're sending
        log(`💬 Sending text to ${chatId}: "${message?.substring(0, 50)}${message?.length > 50 ? '...' : ''}"`);
        log(`💬 With ${mentionContacts.length} mentions:`, mentionContacts);

        let sentMessage;

        if (media && media.data) {
            // Check if it's an audio file that might cause issues
            const isProblematicAudio = media.mimetype && (
                media.mimetype.includes('audio') ||
                media.filename.toLowerCase().includes('.m4a') ||
                media.filename.toLowerCase().includes('.mp3') ||
                media.filename.toLowerCase().includes('.wav') ||
                media.filename.toLowerCase().includes('.ogg')
            );

            if (isProblematicAudio) {
                log(`🎵 Sending audio file as document: ${media.filename}`);
                const mediaFile = new MessageMedia('application/octet-stream', media.data, media.filename);
                sentMessage = await chat.sendMessage(mediaFile, { caption: message || `🎵 Audio file: ${media.filename}`, mentions: mentionContacts });
            } else {
                log(`📎 Sending regular media file: ${media.filename}`);
                const mediaFile = new MessageMedia(media.mimetype, media.data, media.filename);
                sentMessage = await chat.sendMessage(mediaFile, { caption: message, mentions: mentionContacts });
            }
            
            log(`✅ Successfully sent media message to ${chatId}`);
        } else {
            sentMessage = await chat.sendMessage(message, {
                mentions: mentionContacts
            });
            log(`💬 Successfully sent text message to ${chatId}`);
        }
    // --- END MENTION PATCH ---
    // Only one success log is needed, already logged above

    // ✅✅✅ THIS IS THE CRITICAL MISSING PART ✅✅✅
    res.status(200).json({
        success: true,
        messageId: sentMessage.id._serialized,
        timestamp: sentMessage.timestamp
    });

  } catch (error) {
    log(`❌ Failed to send message to ${chatId}:`, error);
    res.status(500).json({
      success: false,
      error: error.toString()
    });
  }
});
// Delete message endpoint
app.post('/delete-message', async (req, res) => {
    const { messageId } = req.body;

    if (!isReady) {
        return res.status(503).json({
            success: false,
            error: 'WhatsApp client is not ready'
        });
    }

    if (!messageId) {
        return res.status(400).json({
            success: false,
            error: 'messageId is required'
        });
    }

    try {
        log(`🗑️  Attempting to delete message: ${messageId}`);

        // Get the message by ID
        const message = await client.getMessageById(messageId);

        if (!message) {
            log(`❌ Message ${messageId} not found`);
            return res.status(404).json({
                success: false,
                error: 'Message not found or may have been already deleted'
            });
        }

        // Delete for everyone (true parameter)
        await message.delete(true);

        log(`✅ Successfully deleted message ${messageId}`);
        res.status(200).json({
            success: true,
            message: 'Message deleted successfully'
        });

    } catch (error) {
        log(`❌ Failed to delete message ${messageId}:`, error);

        // Provide more specific error messages
        let errorMessage = 'Message could not be deleted';
        if (error.toString().includes('too old')) {
            errorMessage = 'Message is too old to be deleted (>7 minutes)';
        } else if (error.toString().includes('not found')) {
            errorMessage = 'Message not found or already deleted';
        }

        res.status(500).json({
            success: false,
            error: errorMessage
        });
    }
});

// --- Edit a WhatsApp message ---
app.post("/edit-message", async (req, res) => {
    const { messageId, newText } = req.body;

    try {
        if (!messageId || !newText) {
            return res.status(400).json({ success: false, error: "Missing messageId or newText" });
        }

        const message = await client.getMessageById(messageId);
        if (!message) {
            return res.status(404).json({ success: false, error: "Message not found" });
        }

        await message.edit(newText);
        res.json({ success: true });
    } catch (err) {
        log("Error editing message:", err);
        res.status(500).json({ success: false, error: err.message });
    }
});

// Get chat info endpoint (useful for debugging)
app.get('/chat-info/:chatId', async (req, res) => {
    const { chatId } = req.params;

    if (!isReady) {
        return res.status(503).json({
            success: false,
            error: 'WhatsApp client is not ready'
        });
    }

    try {
        const chat = await client.getChatById(chatId);
        res.json({
            success: true,
            chat: {
                id: chat.id._serialized,
                name: chat.name,
                isGroup: chat.isGroup,
                participantCount: chat.participants ? chat.participants.length : null
            }
        });
    } catch (error) {
        res.status(500).json({
            success: false,
            error: error.toString()
        });
    }
});

// --- Start the server ---
const PORT = process.env.PORT || 3101;
app.listen(PORT, () => {
    log(`🌐 API server listening at http://localhost:${PORT}`);
    log('📋 Available endpoints:');
    log('   GET  /health - Check service status');
    log('   GET  /get-messages - Retrieve queued messages');
    log('   POST /send-message - Send a message');
    log('   POST /delete-message - Delete a message');
    log('   POST /edit-message - Edit a message');
    log('   GET  /chat-info/:chatId - Get chat information');
});

// --- Initialize WhatsApp Client ---
log('🔄 Initializing WhatsApp client...');
client.initialize();

// --- Graceful shutdown ---
process.on('SIGINT', async () => {
    log('\n🛑 Shutting down WhatsApp service...');
    if (isReady) {
        await client.destroy();
    }
    process.exit(0);
});

process.on('SIGTERM', async () => {
    log('\n🛑 Shutting down WhatsApp service...');
    if (isReady) {
        await client.destroy();
    }
    process.exit(0);
});

// --- WhatsApp message edit event: notify Python bridge ---
client.on('message_edit', async (msg, newBody, prevBody) => {
    log(`[EDIT DETECTED] ${msg.from}: "${prevBody}" → "${newBody}"`);
    try {
        // Call back to your Python bridge to notify it of the edit
        await axios.post('http://127.0.0.1:8101/whatsapp-edit', {
            messageId: msg.id._serialized, // WhatsApp message ID
            newText: newBody,
        });
    } catch (err) {
        log('Failed to notify bridge about edit:', err.message);
    }
});