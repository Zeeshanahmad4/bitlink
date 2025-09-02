import express from "express";
import bodyParser from "body-parser";
import axios from "axios";
import qrcode from "qrcode-terminal";
import pkg from "whatsapp-web.js";
const { Client, LocalAuth, MessageMedia } = pkg;

const GATEWAY_SECRET = process.env.GATEWAY_SECRET;
const HUB_URL = process.env.HUB_URL || "http://127.0.0.1:8000/webhook/whatsapp";
const PORT = Number(process.env.GW_PORT || 3001);

const client = new Client({
  authStrategy: new LocalAuth({ clientId: "bitlink" }),
  puppeteer: {
    headless: true,
    args: ["--no-sandbox", "--disable-setuid-sandbox"],
  },
});

client.on("qr", (qr) => {
  console.log("Scan this QR:");
  qrcode.generate(qr, { small: true });
});
client.on("ready", () => console.log("WhatsApp ready ✅"));

await client.initialize();

function toChatId(e164) {
  return `${(e164 || "").replace(/\D/g, "")}@c.us`;
}

async function postToHub(payload) {
  try {
    await axios.post(HUB_URL, payload, {
      headers: {
        "X-Shared-Secret": GATEWAY_SECRET,
        "Content-Type": "application/json",
      },
      timeout: 10000,
    });
  } catch (e) {
    console.error("Hub POST failed:", e?.response?.status || e.message);
  }
}

client.on("message", async (msg) => {
  try {
    if (msg.fromMe) return;
    const contact = await msg.getContact();
    const payload = {
      wa_message_id: msg.id?._serialized || `${msg.id?.id || ""}`,
      from_number: `+${(msg.from || "").replace(/@.*/, "")}`,
      sender_name:
        contact?.pushname || contact?.name || contact?.number || "Unknown",
      timestamp: msg.timestamp ? msg.timestamp * 1000 : Date.now(),
    };
    if (msg.hasMedia) {
      const media = await msg.downloadMedia();
      payload.has_media = true;
      payload.media = {
        data: media?.data,
        mimetype: media?.mimetype,
        filename: media?.filename || "attachment",
      };
      if (msg.body) payload.text = msg.body;
    } else {
      payload.text = msg.body || "";
    }
    await postToHub(payload);
  } catch (e) {
    console.error("Inbound WA error:", e.message);
  }
});

const app = express();
app.use(bodyParser.json({ limit: "20mb" }));
function requireSecret(req, res, next) {
  if (req.headers["x-shared-secret"] !== GATEWAY_SECRET)
    return res.sendStatus(401);
  next();
}
app.get("/health", (_, res) => res.json({ ok: true }));
app.post("/wa/sendText", requireSecret, async (req, res) => {
  const { to, text } = req.body || {};
  if (!to || !text) return res.status(400).send("to + text required");
  try {
    await client.sendMessage(toChatId(to), text);
    res.sendStatus(200);
  } catch (e) {
    console.error("sendText error:", e.message);
    res.sendStatus(500);
  }
});
app.post("/wa/sendMedia", requireSecret, async (req, res) => {
  const { to, caption, filename, mimetype, data } = req.body || {};
  if (!to || !mimetype || !data)
    return res.status(400).send("to + mimetype + data required");
  try {
    const media = new MessageMedia(mimetype, data, filename || "attachment");
    await client.sendMessage(toChatId(to), media, { caption: caption || "" });
    res.sendStatus(200);
  } catch (e) {
    console.error("sendMedia error:", e.message);
    res.sendStatus(500);
  }
});
app.listen(PORT, () => console.log(`WA Gateway on :${PORT}`));
