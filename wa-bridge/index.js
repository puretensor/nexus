import makeWASocket, {
  useMultiFileAuthState,
  DisconnectReason,
  Browsers,
  fetchLatestWaWebVersion,
  downloadContentFromMessage,
} from '@whiskeysockets/baileys';
import express from 'express';
import pino from 'pino';

// ── Config ──────────────────────────────────────────────────────────────────

const INSTANCE_NAME = process.env.WA_INSTANCE || 'wa-1';
const BOT_PHONE     = process.env.BOT_PHONE || '';
const HTTP_PORT     = parseInt(process.env.HTTP_PORT || '3100', 10);
const NEXUS_WEBHOOK = process.env.NEXUS_WEBHOOK || 'http://127.0.0.1:9876/wa/incoming';
const AUTH_DIR      = process.env.AUTH_DIR || './auth';

const log = pino({ level: process.env.LOG_LEVEL || 'info' });

// ── State ───────────────────────────────────────────────────────────────────

let sock = null;
let connectionStatus = 'disconnected';
let pairingCodeRequested = false;
let reconnectAttempts = 0;
const MAX_RECONNECT_ATTEMPTS = 50;

// ── Express HTTP API ────────────────────────────────────────────────────────

const app = express();
app.use(express.json({ limit: '10mb' }));

// GET /status — connection health
app.get('/status', (_req, res) => {
  res.json({
    instance: INSTANCE_NAME,
    status: connectionStatus,
    uptime: process.uptime(),
  });
});

// GET /contacts — list recent chats (if available)
app.get('/contacts', async (_req, res) => {
  if (!sock || connectionStatus !== 'open') {
    return res.status(503).json({ error: 'Not connected' });
  }
  try {
    // baileys stores contacts in sock.store if configured; return basic info
    const contacts = await sock.store?.contacts || {};
    const list = Object.entries(contacts).map(([jid, c]) => ({
      jid,
      name: c.name || c.notify || jid,
    }));
    res.json({ contacts: list });
  } catch (err) {
    res.json({ contacts: [], note: 'Contact store not available' });
  }
});

// POST /send — send a message to a JID
app.post('/send', async (req, res) => {
  if (!sock || connectionStatus !== 'open') {
    return res.status(503).json({ error: 'Not connected' });
  }

  const { jid, text, media_url, media_type, caption } = req.body;
  if (!jid) {
    return res.status(400).json({ error: 'Missing jid' });
  }

  try {
    let result;
    if (text) {
      result = await sock.sendMessage(jid, { text });
    } else if (media_url) {
      // Support sending media by URL
      const msgContent = { [media_type || 'image']: { url: media_url } };
      if (caption) msgContent.caption = caption;
      result = await sock.sendMessage(jid, msgContent);
    } else {
      return res.status(400).json({ error: 'Missing text or media_url' });
    }

    res.json({ success: true, key: result?.key });
  } catch (err) {
    log.error({ err }, 'Failed to send message');
    res.status(500).json({ error: err.message });
  }
});

// POST /send-voice — send a voice note (base64 OGG Opus)
app.post('/send-voice', async (req, res) => {
  if (!sock || connectionStatus !== 'open') {
    return res.status(503).json({ error: 'Not connected' });
  }

  const { jid, audio_b64 } = req.body;
  if (!jid || !audio_b64) {
    return res.status(400).json({ error: 'Missing jid or audio_b64' });
  }

  try {
    const audioBuffer = Buffer.from(audio_b64, 'base64');
    const result = await sock.sendMessage(jid, {
      audio: audioBuffer,
      mimetype: 'audio/ogg; codecs=opus',
      ptt: true,
    });
    res.json({ success: true, key: result?.key });
  } catch (err) {
    log.error({ err }, 'Failed to send voice');
    res.status(500).json({ error: err.message });
  }
});

app.listen(HTTP_PORT, '0.0.0.0', () => {
  log.info(`wa-bridge [${INSTANCE_NAME}] HTTP API listening on :${HTTP_PORT}`);
});

// ── Webhook to Nexus ────────────────────────────────────────────────────────

async function postToNexus(payload) {
  try {
    const resp = await fetch(NEXUS_WEBHOOK, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(10000),
    });
    if (!resp.ok) {
      log.warn(`Nexus webhook returned ${resp.status}: ${await resp.text()}`);
    }
  } catch (err) {
    log.error({ err }, 'Failed to POST to Nexus webhook');
  }
}

// ── Message unwrapping (from IS-Voice bot) ──────────────────────────────────

function unwrapMessage(m) {
  if (!m) return null;
  return (
    m?.editedMessage?.message?.protocolMessage?.editedMessage ||
    m?.ephemeralMessage?.message ||
    m?.viewOnceMessage?.message ||
    m?.documentWithCaptionMessage?.message ||
    m
  );
}

function extractText(inner) {
  return (
    inner?.conversation ||
    inner?.extendedTextMessage?.text ||
    inner?.imageMessage?.caption ||
    inner?.videoMessage?.caption ||
    inner?.documentMessage?.caption ||
    null
  );
}

function getMessageType(inner) {
  if (inner?.conversation || inner?.extendedTextMessage) return 'text';
  if (inner?.imageMessage) return 'image';
  if (inner?.videoMessage) return 'video';
  if (inner?.audioMessage) return inner.audioMessage.ptt ? 'voice' : 'audio';
  if (inner?.documentMessage) return 'document';
  if (inner?.stickerMessage) return 'sticker';
  if (inner?.contactMessage) return 'contact';
  if (inner?.locationMessage) return 'location';
  return 'unknown';
}

// ── Main bot ────────────────────────────────────────────────────────────────

async function startBot() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);

  let version;
  try {
    const vInfo = await fetchLatestWaWebVersion({});
    version = vInfo.version;
    log.info('Using WhatsApp Web version: %s', version.join('.'));
  } catch (err) {
    log.warn('Failed to fetch WA version: %s', err.message);
  }

  sock = makeWASocket({
    auth: state,
    logger: pino({ level: 'silent' }),
    connectTimeoutMs: 60000,
    defaultQueryTimeoutMs: 60000,
    browser: Browsers.ubuntu('Chrome'),
    syncFullHistory: false,
    ...(version && { version }),
  });

  // ── Connection handling ─────────────────────────────────────────────────

  sock.ev.on('connection.update', async (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (connection === 'connecting') {
      connectionStatus = 'connecting';
      log.info('Connecting to WhatsApp...');
    }

    if (qr && !pairingCodeRequested && BOT_PHONE) {
      pairingCodeRequested = true;
      try {
        const code = await sock.requestPairingCode(BOT_PHONE);
        log.info('═══════════════════════════════════════');
        log.info('  WA-BRIDGE PAIRING CODE: %s', code);
        log.info('  Instance: %s | Phone: +%s', INSTANCE_NAME, BOT_PHONE);
        log.info('  Go to: Settings > Linked Devices > Link a Device');
        log.info('  Choose "Link with phone number instead"');
        log.info('═══════════════════════════════════════');
      } catch (err) {
        log.error('Failed to request pairing code: %s', err.message);
        pairingCodeRequested = false;
      }
    }

    if (connection === 'close') {
      connectionStatus = 'disconnected';
      const statusCode = lastDisconnect?.error?.output?.statusCode;
      const shouldReconnect = statusCode !== DisconnectReason.loggedOut;

      if (shouldReconnect && reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
        reconnectAttempts++;
        const delay = Math.min(5000 * reconnectAttempts, 30000);
        log.info(
          'Connection closed (%d). Reconnecting in %ds... (attempt %d)',
          statusCode, delay / 1000, reconnectAttempts,
        );
        setTimeout(startBot, delay);
      } else if (statusCode === DisconnectReason.loggedOut) {
        log.error('Logged out. Delete %s and restart to re-authenticate.', AUTH_DIR);
        process.exit(1);
      } else {
        log.error('Max reconnection attempts reached. Exiting.');
        process.exit(1);
      }
    }

    if (connection === 'open') {
      connectionStatus = 'open';
      reconnectAttempts = 0;
      pairingCodeRequested = false;
      log.info('Connected to WhatsApp successfully! Instance: %s', INSTANCE_NAME);
    }
  });

  sock.ev.on('creds.update', saveCreds);

  // ── Message handling — normalize and forward to Nexus ───────────────────

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify') return;

    for (const msg of messages) {
      // Skip own messages and status broadcasts
      if (msg.key.fromMe) continue;
      if (msg.key.remoteJid === 'status@broadcast') continue;

      const inner = unwrapMessage(msg.message);
      if (!inner) continue;

      const isGroup = msg.key.remoteJid?.endsWith('@g.us') || false;
      const msgType = getMessageType(inner);
      const text = extractText(inner);

      // For non-text messages without text content, still notify but with type info
      const body = text || `[${msgType}]`;

      // Build normalized payload for Nexus
      const payload = {
        instance: INSTANCE_NAME,
        jid: msg.key.remoteJid,
        sender_jid: isGroup ? msg.key.participant : msg.key.remoteJid,
        push_name: msg.pushName || '',
        message_type: msgType,
        body: body,
        is_group: isGroup,
        timestamp: msg.messageTimestamp
          ? (typeof msg.messageTimestamp === 'number'
              ? msg.messageTimestamp
              : parseInt(msg.messageTimestamp.toString(), 10))
          : Math.floor(Date.now() / 1000),
        message_id: msg.key.id,
      };

      // Add group metadata if available
      if (isGroup && msg.key.participant) {
        payload.group_jid = msg.key.remoteJid;
        payload.group_participant = msg.key.participant;
      }

      log.info(
        '[%s] %s from %s: %s',
        INSTANCE_NAME,
        msgType,
        msg.pushName || payload.sender_jid,
        body.substring(0, 80),
      );

      // Fire-and-forget webhook to Nexus
      postToNexus(payload);
    }
  });
}

// ── Startup ─────────────────────────────────────────────────────────────────

log.info('wa-bridge starting...');
log.info('  Instance:  %s', INSTANCE_NAME);
log.info('  Phone:     %s', BOT_PHONE ? `+${BOT_PHONE}` : 'NOT SET');
log.info('  HTTP port: %d', HTTP_PORT);
log.info('  Webhook:   %s', NEXUS_WEBHOOK);
log.info('  Auth dir:  %s', AUTH_DIR);

startBot();
