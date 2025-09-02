// service.js - FINAL, STABLE, & CORRECT VERSION

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

console.log("Starting WhatsApp Service with Standard Stealth Mode...");

const client = new Client({
    authStrategy: new LocalAuth(),
    puppeteer: {
        headless: true,
        args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-gpu', '--disable-dev-shm-usage'],
    }
});

let isReady = false;

client.on('qr', (qr) => {
    console.log('QR code received, please scan in your terminal:');
    qrcode.generate(qr, { small: true });
});

client.on('authenticated', () => {
    console.log('Authentication successful! Initializing client...');
});

client.on('ready', () => {
    isReady = true;
    console.log('>>> WhatsApp is ready! <<<');
});

client.on('auth_failure', msg => { console.error('AUTHENTICATION FAILURE', msg); isReady = false; });
client.on('disconnected', (reason) => { console.log('Client was logged out', reason); isReady = false; });

// --- API and Message Handling ---
const messageQueue = [];

// --- THIS IS THE UPDATED, ROBUST LOGIC ---

client.on('message', async (msg) => {
    let quotedBody = null;
    // First, check if the message is a reply
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

    if (msg.hasMedia) {
        msg.downloadMedia().then(media => {
            if (media) {
                const extension = mime.extension(media.mimetype);
                const filename = media.filename || `file.${extension}` || 'file.bin';

                messageQueue.push({
                    chatId: msg.from, 
                    body: msg.body, 
                    timestamp: msg.timestamp,
                    quotedBody: quotedBody, // Add the quoted text
                    media: { 
                        mimetype: media.mimetype, 
                        filename: filename,
                        data: media.data 
                    }
                });
            }
        });
    } else {
        messageQueue.push({ 
            chatId: msg.from, 
            body: msg.body, 
            timestamp: msg.timestamp, 
            quotedBody: quotedBody, // Add the quoted text
            media: null 
        });
    }
});

app.get('/get-messages', (req, res) => res.json(messageQueue.splice(0, messageQueue.length)));
app.post('/send-message', async (req, res) => {
    const { chatId, message, media } = req.body;
    if (!isReady) return res.status(503).json({ success: false, error: 'WhatsApp client is not ready' });
    try {
        if (media && media.data) {
            const mediaFile = new MessageMedia(media.mimetype, media.data, media.filename);
            await client.sendMessage(chatId, mediaFile, { caption: message });
        } else {
            await client.sendMessage(chatId, message);
        }
        res.status(200).json({ success: true });
    } catch (error) {
        res.status(500).json({ success: false, error: error.toString() });
    }
});

const PORT = 3000;
app.listen(PORT, () => { console.log(`API server listening at http://localhost:${PORT}`); });

client.initialize();