// Локальный эксперимент: личный WhatsApp через whatsapp-web.js.
// Не запускает Telegram, почту, календари и Авито.
// Входящие пишет в logs/whatsapp.jsonl и отдаёт Помогайке — тот же черновик, что у Telegram.
// Режим approve: черновик ждёт кнопки в Помогайке.
// Режим auto: Помогайка сразу просит этот процесс отправить ответ.
// Переключение: POST http://127.0.0.1:8792/mode  {"mode":"auto"} или {"mode":"approve"}
// Ручная отправка в обоих режимах: POST http://127.0.0.1:8792/send
//   тело: {"to":"79XXXXXXXXX","text":"проверка"}
//
// Запуск из этой папки: npm install && npm start
// QR один раз. Сессия лежит в session/ и в git не входит.

import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import qrcode from "qrcode-terminal";
import wweb from "whatsapp-web.js";

const { Client, LocalAuth } = wweb;

const root = path.dirname(fileURLToPath(import.meta.url));
const sessionDir = path.join(root, "session");
const logDir = path.join(root, "logs");
const logFile = path.join(logDir, "whatsapp.jsonl");
const modeFile = path.join(logDir, "mode.json");
const SEND_PORT = 8792;
const POMOGAIKA_URL = process.env.POMOGAIKA_URL || "http://127.0.0.1:8000";

const ACK = {
  [-1]: "error",
  0: "pending",
  1: "server",
  2: "device",
  3: "read",
  4: "played",
};

fs.mkdirSync(sessionDir, { recursive: true });
fs.mkdirSync(logDir, { recursive: true });

let phase = "starting";
let me = "";
let replyMode = "approve";
const seen = new Set();
let acceptAfter = Number.POSITIVE_INFINITY;

function normalizeMode(value) {
  const raw = String(value || "").trim().toLowerCase();
  if (raw === "auto" || raw === "авто") return "auto";
  if (raw === "approve" || raw === "manual" || raw === "ручной") return "approve";
  return "";
}

function loadMode() {
  try {
    const parsed = JSON.parse(fs.readFileSync(modeFile, "utf8"));
    return normalizeMode(parsed.mode) || "approve";
  } catch {
    return "approve";
  }
}

function setMode(value) {
  const next = normalizeMode(value);
  if (!next) throw new Error('режим: "auto" (сама) или "approve" (после разрешения)');
  replyMode = next;
  fs.writeFileSync(modeFile, `${JSON.stringify({ mode: replyMode })}\n`, "utf8");
  const note = replyMode === "auto" ? "модель отвечает сама" : "ответ только после разрешения";
  log("mode", { mode: replyMode, note });
  return replyMode;
}

function log(event, fields = {}) {
  const entry = { at: new Date().toISOString(), event, ...fields };
  fs.appendFileSync(logFile, `${JSON.stringify(entry)}\n`, "utf8");
  const detail = fields.body || fields.draft || fields.error || fields.reason || fields.note || "";
  const tail = detail ? ` — ${String(detail).replace(/\s+/g, " ").slice(0, 180)}` : "";
  console.log(`${entry.at} ${event}${tail}`);
}

function findChrome() {
  const fromEnv = process.env.CHROME_PATH;
  const candidates = [
    fromEnv,
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  ].filter(Boolean);
  return candidates.find((item) => fs.existsSync(item));
}

function stampMs(timestamp) {
  const value = Number(timestamp) || 0;
  if (!value) return 0;
  return value > 1e12 ? value : value * 1000;
}

function postPomogaika(urlPath, payload) {
  return fetch(`${POMOGAIKA_URL}${urlPath}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(60_000),
  }).then(async (res) => {
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      return { ok: false, error: data.detail || data.error || `Помогайка ответила ${res.status}` };
    }
    return data;
  }).catch((error) => ({ ok: false, error: error.message }));
}

function chatLabel(id) {
  return String(id || "").replace(/@.*/, "");
}

async function onIncoming(msg) {
  const id = msg.id?._serialized || "";
  const at = stampMs(msg.timestamp);
  const from = msg.from || "";
  const body = msg.body || "";
  const base = {
    id,
    from,
    to: msg.to || "",
    author: msg.author || "",
    type: msg.type || "",
    fromMe: Boolean(msg.fromMe),
    body,
  };

  if (id && seen.has(id)) {
    log("duplicate", { id, from });
    return;
  }
  if (id) seen.add(id);

  if (msg.fromMe) {
    log("out_observed", base);
    return;
  }
  if (at && at < acceptAfter) {
    log("skip_history", { ...base, note: "сообщение старше запуска, модель не спрашиваю" });
    return;
  }
  if (from.endsWith("@g.us") || from === "status@broadcast" || msg.isStatus) {
    log("skip_chat", { ...base, note: "группа или статус, модель не спрашиваю" });
    return;
  }
  if (msg.type !== "chat" || !body.trim()) {
    log("skip_media", { ...base, note: "не текст, модель не спрашиваю" });
    return;
  }

  log("in", base);
  const saved = await postPomogaika("/api/whatsapp/message", {
    chat_id: from,
    text: body,
    message_id: id,
    mode: replyMode,
  });
  if (!saved.ok) {
    log("model_error", { id, from, error: saved.error || "Помогайка не приняла сообщение", draft: saved.draft || "" });
    return;
  }
  log("draft", {
    id,
    draft_id: saved.id,
    from,
    mode: replyMode,
    question_class: saved.question_class || "",
    needs_owner: saved.needs_owner,
    booking_id: saved.booking_id,
    draft: saved.draft,
    note: saved.delivered ? "Помогайка отправила ответ" : "ждёт разрешения в Помогайке",
  });
  if (saved.delivered) console.log(`Ответ ушёл ${chatLabel(from)}.`);
  else console.log(`Черновик для ${chatLabel(from)} в Помогайке. В чат не отправлен.`);
}

function normalizePhone(raw) {
  let digits = String(raw).replace(/\D/g, "");
  if (digits.length === 11 && digits.startsWith("8")) digits = `7${digits.slice(1)}`;
  if (digits.length === 10) digits = `7${digits}`;
  return digits;
}

async function resolveChatId(to) {
  const value = String(to || "").trim();
  if (!value) throw new Error("пустой адресат");
  if (value.includes("@")) return value;
  const digits = normalizePhone(value);
  if (digits.length < 11) throw new Error("номер слишком короткий, нужен код страны");
  const wid = await client.getNumberId(digits);
  if (!wid) throw new Error("этот номер не зарегистрирован в WhatsApp");
  return wid._serialized;
}

async function sendText(to, text) {
  const body = String(text || "").trim();
  if (!body) throw new Error("пустой текст");
  if (phase !== "ready") throw new Error("WhatsApp ещё не подключён");
  const chatId = await resolveChatId(to);
  log("send_request", { to: chatId, body });
  const sent = await client.sendMessage(chatId, body);
  const id = sent?.id?._serialized || "";
  if (id) seen.add(id);
  log("send_ok", { id, to: chatId, body });
  return { id, to: chatId };
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on("data", (chunk) => {
      size += chunk.length;
      if (size > 64_000) {
        reject(new Error("слишком большое тело"));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(body),
  });
  res.end(body);
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url || "/", "http://127.0.0.1");
  if (req.method === "GET" && (url.pathname === "/health" || url.pathname === "/mode")) {
    sendJson(res, 200, { ok: true, phase, me, port: SEND_PORT, mode: replyMode });
    return;
  }
  if (req.method === "POST" && url.pathname === "/mode") {
    try {
      const payload = JSON.parse((await readBody(req)) || "{}");
      const mode = setMode(payload.mode);
      sendJson(res, 200, { ok: true, mode });
    } catch (error) {
      sendJson(res, 400, { ok: false, error: error.message, mode: replyMode });
    }
    return;
  }
  if (req.method === "POST" && url.pathname === "/send") {
    try {
      const payload = JSON.parse((await readBody(req)) || "{}");
      const result = await sendText(payload.to, payload.text);
      sendJson(res, 200, { ok: true, ...result });
    } catch (error) {
      log("send_fail", { error: error.message });
      sendJson(res, phase === "ready" ? 400 : 503, { ok: false, error: error.message });
    }
    return;
  }
  sendJson(res, 404, { ok: false, error: "нет такого пути" });
});

const chromePath = findChrome();
const client = new Client({
  authStrategy: new LocalAuth({ clientId: "pomogaika", dataPath: sessionDir }),
  puppeteer: {
    headless: false,
    ...(chromePath ? { executablePath: chromePath } : {}),
    args: ["--lang=ru-RU"],
  },
});

client.on("qr", (qr) => {
  phase = "qr";
  log("qr", { note: "QR в терминале и в окне Chrome. В файл его не пишу." });
  qrcode.generate(qr, { small: true });
  console.log("Телефон → Связанные устройства → Привязка устройства. Процесс не закрывать.");
});

client.on("authenticated", () => {
  phase = "authenticated";
  log("authenticated", { note: "сессия принята, жду готовности клиента" });
});

client.on("auth_failure", (message) => {
  phase = "auth_failure";
  log("auth_failure", { error: String(message || "сбой входа") });
});

client.on("ready", () => {
  phase = "ready";
  acceptAfter = Date.now() - 5_000;
  me = client.info?.wid?.user || "";
  log("ready", { me, mode: replyMode, note: "старые чаты не разбираю" });
  const modeNote = replyMode === "auto" ? "модель отвечает сама" : "ответ только после разрешения";
  console.log(`WhatsApp готов, аккаунт ${me}. Режим: ${replyMode} (${modeNote}).`);
  console.log(`Режим: POST http://127.0.0.1:${SEND_PORT}/mode  {"mode":"auto"} или {"mode":"approve"}`);
  console.log(`Ручная отправка: POST http://127.0.0.1:${SEND_PORT}/send  {"to":"79...","text":"..."}`);
});

client.on("message", (msg) => {
  onIncoming(msg).catch((error) => log("in_error", { error: error.message }));
});

client.on("message_create", (msg) => {
  if (!msg.fromMe) return;
  onIncoming(msg).catch((error) => log("out_error", { error: error.message }));
});

client.on("message_ack", (msg, ack) => {
  log("ack", {
    id: msg.id?._serialized || "",
    from: msg.from || "",
    to: msg.to || "",
    ack,
    ack_name: ACK[ack] || String(ack),
    fromMe: Boolean(msg.fromMe),
  });
});

client.on("disconnected", (reason) => {
  phase = "disconnected";
  log("disconnected", { reason: String(reason || "") });
  console.log("Сессия оборвалась. Процесс останавливаю, чтобы не открывать второй клиент.");
  process.exit(1);
});

replyMode = loadMode();

server.listen(SEND_PORT, "127.0.0.1", () => {
  log("listen", {
    note: `http://127.0.0.1:${SEND_PORT}`,
    mode: replyMode,
    chrome: chromePath || "chromium puppeteer",
  });
});

client.initialize().catch((error) => {
  log("init_error", { error: error.message });
  process.exit(1);
});
