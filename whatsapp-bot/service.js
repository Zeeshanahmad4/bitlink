// service.js - FINAL, PRODUCTION-READY VERSION with Deletion Support - UPDATED

const { Client, LocalAuth, MessageMedia } = require('whatsapp-web.js'); 
const express = require('express');
const qrcode = require('qrcode-terminal');
const bodyParser = require('body-parser');
const mime = require('mime-types'); 
const puppeteer = require('puppeteer-extra');
const StealthPlugin = require('puppeteer-extra-plugin-stealth');

puppeteer.use(StealthPlugin());

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

client.on('ready', () => {
    isReady = true;
    console.log('🎉 >>> WhatsApp is ready! <<<');
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
client.on('message', async (msg) => {
    let quotedBody = null;
    
    // Handle quoted messages
    if (msg.hasQuotedMsg) {
        try {
            const quotedMsg = await msg.getQuotedMessage();
            if (quotedMsg && quotedMsg.body) { 
                quotedBody = quotedMsg.body; 
            }
        } catch (error) {
            console.error("Could not get quoted message:", error);
        }
    }

    const messageData = {
        chatId: msg.from,
        body: msg.body,
        timestamp: msg.timestamp,
        quotedBody: quotedBody,
        // The unique ID needed for future deletions
        messageId: msg.id._serialized 
    };

    // Handle media attachments
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
            // Still add the message without media if download fails
            messageData.media = null;
            messageQueue.push(messageData);
        }
    } else {
        messageData.media = null;
        messageQueue.push(messageData);
        console.log(`💬 Text message received from ${msg.from}: ${msg.body.substring(0, 50)}...`);
    }
});

// --- API Endpoints ---

// Health check endpoint
app.get('/health', (req, res) => {
    res.json({ 
        status: isReady ? 'ready' : 'not_ready', 
        timestamp: new Date().toISOString() 
    });
});

// Get queued messages
app.get('/get-messages', (req, res) => {
    const messages = messageQueue.splice(0, messageQueue.length);
    res.json(messages);
});

// Send message endpoint with enhanced response
app.post('/send-message', async (req, res) => {
    const { chatId, message, media } = req.body;
    
    if (!isReady) {
        return res.status(503).json({ 
            success: false, 
            error: 'WhatsApp client is not ready' 
        });
    }
    
    if (!chatId) {
        return res.status(400).json({ 
            success: false, 
            error: 'chatId is required' 
        });
    }

    try {
        let sentMessage;
        
        if (media && media.data) {
            // Send message with media
            const mediaFile = new MessageMedia(media.mimetype, media.data, media.filename);
            sentMessage = await client.sendMessage(chatId, mediaFile, { caption: message });
            console.log(`📎 Successfully sent media message to ${chatId}`);
        } else {
            // Send text message
            sentMessage = await client.sendMessage(chatId, message);
            console.log(`💬 Successfully sent text message to ${chatId}`);
        }
        
        // Return the ID of the new message so Python can store it for deletion
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
const PORT = process.env.PORT || 3000;
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