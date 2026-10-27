// Локальный эксперимент: личный Max через Node-Max (неофициальный веб-клиент).
// Не запускает Telegram, почту, календари, Авито и WhatsApp.
// Входящие пишет в logs/max.jsonl и отдаёт Помогайке — тот же черновик, что у Telegram.
// Режим approve: черновик ждёт кнопки в Помогайке.
// Режим auto: Помогайка сразу просит этот процесс отправить ответ.
// Переключение: POST http://127.0.0.1:8793/mode  {"mode":"auto"} или {"mode":"approve"}
// Ручная отправка: POST http://127.0.0.1:8793/send
//   тело: {"to":"<chatId>","text":"проверка"}
//
// Библиотека лежит в vendor/node-max: это Node-Max, у веб-клиента убран
// обязательный lz4, он нужен только сырому сокету и на Windows не собирается.
// Запуск из этой папки: npm install && npm start
// Первый вход — QR в терминале. Его сканирует телефон:
// Профиль → Устройства → Войти по QR-коду.
// Сессия лежит в session/ и в git не входит. Второй npm start рядом не запускать.

import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { MaxClient } = require("./vendor/node-max/src/index.js");

const root = path.dirname(fileURLToPath(import.meta.url));
const sessionDir = path.join(root, "session");
const sessionFile = path.join(sessionDir, "session.json");
const logDir = path.join(root, "logs");
const logFile = path.join(logDir, "max.jsonl");
const modeFile = path.join(logDir, "mode.json");
const SEND_PORT = 8793;
const POMOGAIKA_URL = process.env.POMOGAIKA_URL || "http://127.0.0.1:8000";

fs.mkdirSync(sessionDir, { recursive: true });
fs.mkdirSync(logDir, { recursive: true });

let phase = "starting";
let me = "";
let meId = "";
let replyMode = "approve";
const seen = new Set();
const sentTexts = new Map();
const chatIds = new Map();
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

function hasToken() {
  try {
    return Boolean(JSON.parse(fs.readFileSync(sessionFile, "utf8")).token);
  } catch {
    return false;
  }
}

function stampMs(timestamp) {
  const value = Number(timestamp) || 0;
  if (!value) return 0;
  return value > 1e12 ? value : value * 1000;
}

function personName(person) {
  const name = person?.names?.[0];
  if (!name) return "";
  return name.name || [name.firstName, name.lastName].filter(Boolean).join(" ");
}

function rememberChatId(value) {
  if (value == null || value === "") return;
  chatIds.set(String(value), value);
}

function resolveChatId(value) {
  const key = String(value ?? "").trim();
  if (!key) throw new Error("пустой чат");
  if (chatIds.has(key)) return chatIds.get(key);
  if (/^-?\d+$/.test(key)) {
    const asNumber = Number(key);
    if (Number.isSafeInteger(asNumber)) return asNumber;
  }
  return key;
}

function chatKind(chatId) {
  const id = String(chatId);
  if ((client.dialogs || []).some((item) => String(item.id) === id)) return "dialog";
  if ((client.chats || []).some((item) => String(item.id) === id)) return "chat";
  if ((client.channels || []).some((item) => String(item.id) === id)) return "channel";
  return "unknown";
}

function senderLabel(sender) {
  const id = String(sender ?? "");
  const person = (client.contacts || []).find((item) => String(item.id) === id);
  return personName(person) || id;
}

function rememberSent(chatId, text) {
  const key = String(chatId);
  if (!sentTexts.has(key)) sentTexts.set(key, new Set());
  sentTexts.get(key).add(text);
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

async function onIncoming(msg) {
  const id = msg?.id == null ? "" : String(msg.id);
  const chatId = msg?.chatId;
  const at = stampMs(msg?.time);
  const sender = msg?.sender == null ? "" : String(msg.sender);
  const body = String(msg?.text || "");
  const kind = chatId == null ? "" : chatKind(chatId);
  const base = {
    id,
    chatId: chatId == null ? "" : String(chatId),
    sender,
    type: msg?.type || "",
    kind,
    body,
  };

  if (id && seen.has(id)) {
    log("duplicate", { id, chatId: base.chatId });
    return;
  }
  if (id) seen.add(id);
  rememberChatId(chatId);

  if (!sender || (meId && sender === meId)) {
    log("out_observed", base);
    return;
  }
  if (at && at < acceptAfter) {
    log("skip_history", { ...base, note: "сообщение старше запуска, модель не спрашиваю" });
    return;
  }
  if (kind === "chat" || kind === "channel") {
    log("skip_chat", { ...base, note: "группа или канал, модель не спрашиваю" });
    return;
  }
  if (!body.trim()) {
    log("skip_media", { ...base, note: "не текст, модель не спрашиваю" });
    return;
  }
  if (body.startsWith("Новый вход")) {
    log("skip_chat", { ...base, note: "служебное уведомление о входе, модель не спрашиваю" });
    return;
  }

  const text = body.trim();
  if (sentTexts.get(base.chatId)?.has(text)) {
    log("out_observed", { ...base, note: "это наш ответ, модель не спрашиваю" });
    return;
  }

  const peer = senderLabel(sender);
  log("in", { ...base, peer });
  const saved = await postPomogaika("/api/max/message", {
    chat_id: base.chatId,
    text,
    message_id: id,
    mode: replyMode,
    peer,
  });
  if (!saved.ok) {
    log("model_error", {
      id,
      chatId: base.chatId,
      error: saved.error || "Помогайка не приняла сообщение",
      draft: saved.draft || "",
    });
    return;
  }
  log("draft", {
    id,
    draft_id: saved.id,
    chatId: base.chatId,
    mode: replyMode,
    question_class: saved.question_class || "",
    needs_owner: saved.needs_owner,
    booking_id: saved.booking_id,
    draft: saved.draft,
    note: saved.delivered ? "Помогайка отправила ответ" : "ждёт разрешения в Помогайке",
  });
  if (saved.delivered) console.log(`Ответ ушёл в чат ${base.chatId}.`);
  else console.log(`Черновик для ${peer || base.chatId} в Помогайке. В Max не отправлен.`);
}

function sendText(to, text) {
  const body = String(text || "").trim();
  if (!body) throw new Error("пустой текст");
  if (phase !== "ready") throw new Error("Max ещё не подключён");
  const chatId = resolveChatId(to);
  rememberSent(chatId, body);
  log("send_request", { to: String(chatId), body });
  client.sendMessage(chatId, body);
  log("send_ok", { to: String(chatId), body, note: "библиотека не подтверждает доставку" });
  return { to: String(chatId) };
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
      const result = sendText(payload.to, payload.text);
      sendJson(res, 200, { ok: true, ...result });
    } catch (error) {
      log("send_fail", { error: error.message });
      sendJson(res, phase === "ready" ? 400 : 503, { ok: false, error: error.message });
    }
    return;
  }
  sendJson(res, 404, { ok: false, error: "нет такого пути" });
});

const client = new MaxClient({
  workDir: sessionDir,
  sessionName: "session.json",
  reconnect: false,
});
client.setLogLevel("info");

client.on("start", () => {
  phase = "ready";
  acceptAfter = Date.now() - 5_000;
  meId = client.me?.id == null ? "" : String(client.me.id);
  me = personName(client.me) || meId;
  for (const dialog of client.dialogs || []) rememberChatId(dialog.id);
  log("ready", {
    me,
    meId,
    dialogs: (client.dialogs || []).length,
    mode: replyMode,
    note: "старые чаты не разбираю",
  });
  const modeNote = replyMode === "auto" ? "модель отвечает сама" : "ответ только после разрешения";
  console.log(`Max готов, аккаунт ${me || meId}. Режим: ${replyMode} (${modeNote}).`);
  console.log(`Режим: POST http://127.0.0.1:${SEND_PORT}/mode  {"mode":"auto"} или {"mode":"approve"}`);
  console.log(`Ручная отправка: POST http://127.0.0.1:${SEND_PORT}/send  {"to":"<chatId>","text":"..."}`);
});

client.on("message", (msg) => {
  onIncoming(msg).catch((error) => log("in_error", { error: error.message }));
});

replyMode = loadMode();

server.listen(SEND_PORT, "127.0.0.1", () => {
  log("listen", { note: `http://127.0.0.1:${SEND_PORT}`, mode: replyMode });
  if (!hasToken()) {
    phase = "qr";
    console.log("Вход по QR. Телефон → Профиль → Устройства → Войти по QR-коду. Процесс не закрывать.");
  } else {
    phase = "login";
    console.log("Сессия уже есть, вхожу без QR.");
  }
  client.start().catch((error) => {
    phase = "auth_failure";
    const message = error?.message || String(error);
    log("login_error", { error: message });
    if (message.includes("token not received")) {
      console.log("Пароль не принят. Запустите npm start ещё раз: QR нужно сканировать заново, затем ввести пароль в терминале.");
    } else if (message.includes("QR") || error?.name === "QRExpiredError") {
      console.log("QR истёк. Запустите npm start ещё раз и отсканируйте новый код.");
    }
    process.exit(1);
  });
});
