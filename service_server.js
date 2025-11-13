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
const SLACK_WEBHOOK_URL = process.env.SLACK_WEBHOOK_URL;
const app = express();
app.use(bodyParser.json({ limit: '50mb' }));

console.log("🚀 Starting WhatsApp Service with Standard Stealth Mode...");

const client = new Client({
    authStrategy: new LocalAuth(),
    puppeteer: {
        headless: true,
        args: [
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-gpu',
            '--disable-dev-shm-usage',
            '--disable-web-security',
            '--disable-features=VizDisplayCompositor'
        ],
    }
});

let isReady = false;

// --- WhatsApp Client Event Handlers ---
client.on('qr', (qr) => {
    console.log('📱 QR code received, please scan in your terminal:');
    qrcode.generate(qr, { small: true });
});

client.on('authenticated', () => {
    console.log('✅ Authentication successful! Initializing client...');
});

// service.js - ADD THIS ENTIRE BLOCK

// --- Proactive Group Discovery on Startup ---
client.on('ready', async () => {
    isReady = true;
    console.log('🎉 >>> WhatsApp is ready! <<<');

    try {
        console.log('🔍 Performing initial scan for unmapped groups...');
        const allChats = await client.getChats();
        
        for (const chat of allChats) {
            if (chat.isGroup) {
                // We pass every group to our intelligent handler.
                // It will use its 'notifiedGroups' memory to ensure
                // it only sends a notification for truly new groups.
                await handleNewGroup(chat);
            }
        }
        console.log('✅ Initial group scan complete.');
    } catch (error) {
        console.error('❌ Failed to perform initial group scan:', error);
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
            console.log(`💾 Loaded ${groupIds.length} previously notified groups from file.`);
            return new Set(groupIds);
        }
    } catch (error) {
        console.error('❌ Error loading notified groups file. Starting fresh.', error);
    }
    console.log('📄 No notified groups file found. A new one will be created.');
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
        console.error('❌ CRITICAL: Failed to save notified groups to file!', error);
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
        console.log(`🚀 New group detected: "${groupName}" (${groupId}). Sending notification.`);

        // 3. Send the notification to Slack.
        if (!SLACK_WEBHOOK_URL) {
            console.error('❌ SLACK_WEBHOOK_URL is not set.');
            return;
        }

        const slackMessage = {
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
        await axios.post(SLACK_WEBHOOK_URL, slackMessage);
        console.log(`📬 Successfully sent notification to Slack for group "${groupName}".`);

    } catch (error) {
        console.error(`❌ Failed to process new group notification for chat "${chat.name}":`, error);
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

    // --- Original Message Processing Logic (UNCHANGED) ---
    let quotedBody = null;
    let senderName = null;

    if (msg.hasQuotedMsg) {
        try {
            const quotedMsg = await msg.getQuotedMessage();
            if (quotedMsg && quotedMsg.body) { quotedBody = quotedMsg.body; }
        } catch (error) { console.error("Could not get quoted message:", error); }
    }

    if (msg.from.endsWith('@g.us')) {
        try {
            const contact = await msg.getContact();
            senderName = contact.name || contact.pushname || contact.number;
        } catch (error) {
            console.error("Could not get sender contact from group message:", error);
        }
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
                console.log(`📎 Media message received from ${msg.from}: ${filename}`);
            }
        } catch (error) {
            console.error("Error downloading media:", error);
            messageData.media = null;
            messageQueue.push(messageData);
        }
    } else {
        messageData.media = null;
        messageQueue.push(messageData);
    const ts = new Date().toISOString();
    console.log(`[${ts}] 💬 Text message received from ${msg.from}: ${msg.body.substring(0, 50)}...`);
    }
});
client.on('message_edit', async (msg, newBody, prevBody) => {
    console.log(`[EDIT DETECTED] ${msg.from}: "${prevBody}" → "${newBody}"`);
  
    try {
      // Call back to your Python bridge to notify it of the edit
      await fetch('http://127.0.0.1:8001/whatsapp-edit', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messageId: msg.id.id,   // WhatsApp message ID
          newText: newBody,
        }),
      });
    } catch (err) {
      console.error('Failed to notify bridge about edit:', err.message);
    }
  });
  
client.on('auth_failure', msg => {
    console.error('❌ AUTHENTICATION FAILURE', msg);
    isReady = false;
});

client.on('disconnected', (reason) => {
    console.log('🔌 Client was logged out:', reason);
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
    const { chatId, message, media } = req.body;

    const currentState = await client.getState();
    if (currentState !== 'CONNECTED') {
        const errorMsg = `WhatsApp client is not ready. Current state: ${currentState}`;
        console.error(`❌ Blocked send request: ${errorMsg}`);
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
                console.log(`🎵 Sending audio file as document: ${media.filename}`);
                const mediaFile = new MessageMedia('application/octet-stream', media.data, media.filename);
                sentMessage = await chat.sendMessage(mediaFile, { caption: message || `🎵 Audio file: ${media.filename}` });
            } else {
                console.log(`📎 Sending regular media file: ${media.filename}`);
                const mediaFile = new MessageMedia(media.mimetype, media.data, media.filename);
                sentMessage = await chat.sendMessage(mediaFile, { caption: message });
            }
            
            console.log(`✅ Successfully sent media message to ${chatId}`);
        } else {
            sentMessage = await chat.sendMessage(message);
            console.log(`💬 Successfully sent text message to ${chatId}`);
        }

        // ✅✅✅ THIS IS THE CRITICAL MISSING PART ✅✅✅
        res.status(200).json({
            success: true,
            messageId: sentMessage.id._serialized,
            timestamp: sentMessage.timestamp
        });

    } catch (error) {
        console.error(`❌ Failed to send message to ${chatId}:`, error);
        res.status(500).json({
            success: false,
            error: error.toString()
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
      console.error("Error editing message:", err);
      res.status(500).json({ success: false, error: err.message });
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
        console.log(`🗑️  Attempting to delete message: ${messageId}`);

        // Get the message by ID
        const message = await client.getMessageById(messageId);

        if (!message) {
            console.log(`❌ Message ${messageId} not found`);
            return res.status(404).json({
                success: false,
                error: 'Message not found or may have been already deleted'
            });
        }

        // Delete for everyone (true parameter)
        await message.delete(true);

        console.log(`✅ Successfully deleted message ${messageId}`);
        res.status(200).json({
            success: true,
            message: 'Message deleted successfully'
        });

    } catch (error) {
        console.error(`❌ Failed to delete message ${messageId}:`, error);

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
const PORT = process.env.PORT || 3001;
app.listen(PORT, () => {
    console.log(`🌐 API server listening at http://localhost:${PORT}`);
    console.log('📋 Available endpoints:');
    console.log('   GET  /health - Check service status');
    console.log('   GET  /get-messages - Retrieve queued messages');
    console.log('   POST /send-message - Send a message');
    console.log('   POST /delete-message - Delete a message');
    console.log('   GET  /chat-info/:chatId - Get chat information');
});

// --- Initialize WhatsApp Client ---
console.log('🔄 Initializing WhatsApp client...');
client.initialize();

// --- Graceful shutdown ---
process.on('SIGINT', async () => {
    console.log('\n🛑 Shutting down WhatsApp service...');
    if (isReady) {
        await client.destroy();
    }
    process.exit(0);
});

process.on('SIGTERM', async () => {
    console.log('\n🛑 Shutting down WhatsApp service...');
    if (isReady) {
        await client.destroy();
    }
    process.exit(0);
});