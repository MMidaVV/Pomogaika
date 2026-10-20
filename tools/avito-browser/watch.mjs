// Локальный эксперимент: свой аккаунт Авито, свои диалоги, лог на диск.
// Запуск из этой папки: npm install && npm start
// Вход и код из SMS вводите в открывшемся окне сами.
// Сброс «уже виденного»: удалите logs/seen.json

import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer-extra";
import StealthPlugin from "puppeteer-extra-plugin-stealth";

const stealth = StealthPlugin();
// Этот приём добавляет флаг --disable-blink-features=AutomationControlled.
// Chrome из-за него показывает плашку «неподдерживаемый флаг командной строки».
// Признак webdriver убираем скриптом на странице, без флага. Остальной stealth остаётся.
stealth.enabledEvasions.delete("navigator.webdriver");
puppeteer.use(stealth);

const root = path.dirname(fileURLToPath(import.meta.url));
const profileDir = path.join(root, "profile");
const logDir = path.join(root, "logs");
const logFile = path.join(logDir, "messages.jsonl");
const seenFile = path.join(logDir, "seen.json");
const markerFile = path.join(logDir, "markers.json");

const MESSENGER_URL = "https://www.avito.ru/profile/messenger";
const POMOGAIKA_URL = process.env.POMOGAIKA_URL || "http://127.0.0.1:8000";
const SEND_PORT = 8791;
const POLL_MS = 15_000;

fs.mkdirSync(profileDir, { recursive: true });
fs.mkdirSync(logDir, { recursive: true });

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function loadSeen() {
  try {
    const parsed = JSON.parse(fs.readFileSync(seenFile, "utf8"));
    return new Set(Array.isArray(parsed) ? parsed : []);
  } catch {
    return new Set();
  }
}

function saveSeen(seen) {
  fs.writeFileSync(seenFile, JSON.stringify([...seen], null, 2));
}

function writeLog(entry) {
  fs.appendFileSync(logFile, `${JSON.stringify(entry)}\n`, "utf8");
}

function keyOf(dialog) {
  return [dialog.id || dialog.raw, dialog.text, dialog.time].join("\n");
}

async function launchBrowser() {
  const options = {
    headless: false,
    userDataDir: profileDir,
    defaultViewport: null,
    // Плашку «браузером управляет автоматизированное ПО» рисует сам Chrome
    // из-за этого флага. Stealth его не убирает: он прячет признаки внутри страницы.
    ignoreDefaultArgs: ["--enable-automation"],
    args: ["--start-maximized", "--lang=ru-RU"],
  };
  try {
    return await puppeteer.launch({ ...options, channel: "chrome" });
  } catch (error) {
    console.log(`Системный Chrome не открылся (${error.message}). Беру Chrome, который скачал Puppeteer.`);
    return puppeteer.launch(options);
  }
}

async function readPage(page) {
  return page.evaluate(() => {
    const textOf = (node, marker) =>
      node.querySelector(`[data-marker="${marker}"]`)?.innerText?.replace(/\s+/g, " ").trim() || "";

    const cards = [...document.querySelectorAll('[data-marker="channels/channel"]')];
    let dialogs = cards.map((node) => {
      const link = node.querySelector('[data-marker="channels/channelLink"]')?.getAttribute("href")
        || node.querySelector('a[href*="messenger"]')?.getAttribute("href")
        || "";
      const id = link || node.getAttribute("data-id") || "";
      const peer = textOf(node, "channels/user-title") || textOf(node, "channels/item-title") || "Собеседник";
      const item = textOf(node, "channels/item-title");
      const text = textOf(node, "channels/last-message");
      const time = textOf(node, "channels/channel-datetime") || textOf(node, "channels/message-time");
      const raw = (node.innerText || "").replace(/\s+/g, " ").trim();
      return { id, peer, item, text: text || raw, time, raw };
    }).filter((dialog) => dialog.text);

    if (!dialogs.length) {
      const loose = [...document.querySelectorAll('[data-marker*="channel"]')].filter((node) => {
        const nested = node.querySelector('[data-marker*="channel"]');
        const raw = (node.innerText || "").replace(/\s+/g, " ").trim();
        return nested === null && raw.length > 8 && raw.length < 500;
      });
      dialogs = loose.map((node) => {
        const raw = (node.innerText || "").replace(/\s+/g, " ").trim();
        const link = node.querySelector("a")?.getAttribute("href") || node.getAttribute("data-id") || raw;
        return { id: link, peer: "Собеседник", item: "", text: raw, time: "", raw };
      });
    }

    const markers = [...new Set(
      [...document.querySelectorAll("[data-marker]")]
        .map((node) => node.getAttribute("data-marker"))
        .filter(Boolean),
    )];
    const body = (document.body?.innerText || "").slice(0, 400);
    return {
      href: location.href,
      blocked: /доступ ограничен|подтвердите, что запросы отправляли вы/i.test(body),
      loggedIn: !!document.querySelector(
        '[data-marker="header/username"], [data-marker="header/username-button"], [data-marker="header/menu-button"]',
      ),
      onMessenger: location.pathname.includes("/profile/messenger"),
      loginForm: !!document.querySelector('[data-marker="login-form"]'),
      dialogs,
      markers,
    };
  });
}

function isLoginWall(state) {
  const href = state.href || "";
  return state.blocked || href.includes("#login") || href.includes("/login");
}

function isMessenger(state) {
  if (!state || isLoginWall(state)) return false;
  try {
    return new URL(state.href).pathname.includes("/profile/messenger");
  } catch {
    return false;
  }
}

const sentTexts = new Set();

async function publishMessage(dialog) {
  if (!dialog.id || !dialog.text) return;
  if (sentTexts.has(dialog.text)) return;
  const response = await fetch(`${POMOGAIKA_URL}/api/avito/message`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      channel_id: dialog.id,
      peer: dialog.peer,
      item: dialog.item,
      text: dialog.text,
    }),
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail.slice(0, 300));
  }
  const body = await response.json();
  if (body.created) {
    console.log(`В Помогайке карточка для ${dialog.peer}. В Авито ничего не отправлено.`);
  }
}

function readBody(request) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    request.on("data", (chunk) => chunks.push(chunk));
    request.on("end", () => {
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}"));
      } catch (error) {
        reject(error);
      }
    });
    request.on("error", reject);
  });
}

function channelUrl(channelId) {
  if (!channelId) return MESSENGER_URL;
  if (channelId.startsWith("http")) return channelId;
  const path = channelId.startsWith("/") ? channelId : `/profile/messenger/channel/${channelId}`;
  return `https://www.avito.ru${path}`;
}

async function openChannel(page, channelId) {
  const clicked = await page.evaluate((channelId) => {
    const links = [...document.querySelectorAll('[data-marker="channels/channelLink"], a[href*="/messenger/channel/"]')];
    const link = links.find((node) => {
      const href = node.getAttribute("href") || "";
      const id = node.closest("[data-id]")?.getAttribute("data-id") || "";
      return href === channelId || href.includes(channelId) || (href && channelId.includes(href)) || id === channelId;
    });
    if (!link) return "";
    link.click();
    return link.getAttribute("href") || channelId;
  }, channelId);
  if (!clicked) {
    await page.goto(channelUrl(channelId), { waitUntil: "domcontentloaded", timeout: 30000 });
  }
  await page.waitForSelector('[data-marker="reply/input"], textarea', { timeout: 15000 });
  await page.waitForFunction(() => {
    const node = document.querySelector('[data-marker="messagesHistory"]');
    return Boolean(node && (node.innerText || "").trim().length > 20);
  }, { timeout: 8000 }).catch(() => {});
}

async function collectThread(page) {
  return page.evaluate(() => {
    const skip = /^(сегодня|вчера)$/i;
    const list = document.querySelector('[data-marker="messagesHistory/list"]');
    const nodes = [...document.querySelectorAll('[data-marker="message"]')];
    const messages = nodes.map((node) => {
      const textNode = node.querySelector('[data-marker="messageText"]');
      const text = ((textNode || node).innerText || "").replace(/\s+/g, " ").trim();
      const cls = String(node.className || "");
      const mine = /right-/.test(cls) || !/left-/.test(cls);
      return { text, mine };
    }).filter((row) => row.text && !skip.test(row.text));
    const history = document.querySelector('[data-marker="messagesHistory"]');
    const markers = [...new Set([...document.querySelectorAll("[data-marker]")]
      .map((node) => node.getAttribute("data-marker"))
      .filter((marker) => marker && /message|reply|text/i.test(marker)))];
    const structure = {
      childCount: list ? list.childElementCount : -1,
      historyText: history ? (history.innerText || "").replace(/\s+/g, " ").trim().slice(0, 300) : "",
      html: (list || history) ? (list || history).innerHTML.slice(0, 1200) : "",
    };
    const chains = nodes.slice(0, 3).map((node) => {
      const chain = [];
      let el = node;
      for (let depth = 0; depth < 6 && el; depth += 1) {
        chain.push(el.getAttribute("data-marker") || String(el.className || el.tagName).slice(0, 80));
        el = el.parentElement;
      }
      return chain;
    });
    return { messages, markers, chains, structure };
  });
}

async function readOpenThread(page) {
  const payload = await collectThread(page);
  fs.writeFileSync(path.join(logDir, "thread-markers.json"), JSON.stringify({
    markers: payload.markers,
    chains: payload.chains,
    roles: payload.messages.map((row) => row.mine),
    structure: payload.structure,
  }, null, 2));
  return payload.messages;
}

async function sendInAvito(page, channelId, text) {
  await openChannel(page, channelId);
  const typed = await page.evaluate((message) => {
    const root = document.querySelector('[data-marker="reply/input"]') || document.querySelector("textarea");
    if (!root) return false;
    const input = root.matches("textarea, input") ? root : (root.querySelector("textarea, input") || root);
    input.focus();
    if (input.tagName === "TEXTAREA" || input.tagName === "INPUT") {
      const proto = input.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value").set.call(input, message);
      input.dispatchEvent(new Event("input", { bubbles: true }));
      return true;
    }
    input.textContent = message;
    input.dispatchEvent(new InputEvent("input", { bubbles: true, data: message, inputType: "insertText" }));
    return true;
  }, text);
  if (!typed) throw new Error("не нашёл поле ответа");
  const button = await page.$('[data-marker="reply/send"]');
  if (!button) throw new Error("не нашёл кнопку отправки");
  await button.click();
  sentTexts.add(text);
  await sleep(1200);
  return readOpenThread(page);
}

function startSendServer(page) {
  const server = http.createServer(async (request, response) => {
    if (request.method !== "POST" || (request.url !== "/send" && request.url !== "/thread")) {
      response.writeHead(404);
      response.end();
      return;
    }
    try {
      const body = await readBody(request);
      const channelId = String(body.channel_id || "");
      if (request.url === "/thread") {
        await openChannel(page, channelId);
        const messages = await readOpenThread(page);
        response.writeHead(200, { "content-type": "application/json" });
        response.end(JSON.stringify({ ok: true, messages }));
        return;
      }
      const messages = await sendInAvito(page, channelId, String(body.text || ""));
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({ ok: true, messages: messages || [] }));
    } catch (error) {
      response.writeHead(502, { "content-type": "application/json" });
      response.end(JSON.stringify({ ok: false, error: error.message }));
    }
  });
  server.listen(SEND_PORT, "127.0.0.1", () => {
    console.log(`Кнопка «Отправить в Авито» ходит сюда: 127.0.0.1:${SEND_PORT}`);
  });
  return server;
}

function printDialogs(dialogs) {
  if (!dialogs.length) {
    console.log("Список диалогов пуст.");
    return;
  }
  for (const dialog of dialogs) {
    const title = dialog.item ? `${dialog.peer} · ${dialog.item}` : dialog.peer;
    console.log(`- ${title}: ${dialog.text}`);
  }
}

async function main() {
  const seen = loadSeen();
  const browser = await launchBrowser();
  const page = (await browser.pages())[0] || (await browser.newPage());
  startSendServer(page);
  await page.evaluateOnNewDocument(() => {
    try {
      Object.defineProperty(Navigator.prototype, "webdriver", {
        get: () => false,
        configurable: true,
      });
    } catch {
      // На части версий Chrome свойство нельзя подменить. Плашку из-за этого не показываем.
    }
  });
  let stopReason = "";

  const shutdown = async () => {
    if (stopReason) console.log(stopReason);
    await browser.close().catch(() => {});
  };
  process.on("SIGINT", () => {
    stopReason = "Останавливаюсь.";
    shutdown().finally(() => process.exit(0));
  });
  browser.on("disconnected", () => process.exit(0));

  console.log("Открываю Авито. Войдите в свой аккаунт в этом окне. Код из SMS вводите сами.");
  console.log("Окно само не закроется. Остановить — закройте его или нажмите Ctrl+C.");
  await page.goto(MESSENGER_URL, { waitUntil: "domcontentloaded", timeout: 60_000 });

  let ready = null;
  let toldToLogin = false;
  while (!ready) {
    let state;
    try {
      state = await readPage(page);
    } catch {
      await sleep(1000);
      continue;
    }
    if (!isMessenger(state)) {
      if (!toldToLogin) {
        console.log("Жду вход. Страница входа — это нормально, браузер из-за неё не закроется.");
        toldToLogin = true;
      }
      await sleep(2000);
      continue;
    }
    // Авито на секунду открывает адрес мессенджера и тут же уводит на #login.
    // Считаем вход завершённым, только если адрес устоялся.
    await sleep(4000);
    let again;
    try {
      again = await readPage(page);
    } catch {
      continue;
    }
    if (!isMessenger(again)) continue;
    ready = again;
  }

  fs.writeFileSync(markerFile, JSON.stringify(ready.markers, null, 2));
  console.log(`Мессенджер открыт. Сейчас вижу диалогов: ${ready.dialogs.length}.`);
  printDialogs(ready.dialogs);
  if (!ready.dialogs.length && !ready.markers.some((marker) => marker.includes("channel"))) {
    console.log("Подписи элементов на странице не похожи на список диалогов. Они записаны в logs/markers.json.");
  }
  console.log(`Дальше в ${path.relative(root, logFile)} попадут только новые сообщения.`);
  console.log("Ответ модели появится в Помогайке. В Авито он уйдёт только после кнопки «Отправить в Авито».");
  writeLog({ kind: "started", at: new Date().toISOString(), dialogs: ready.dialogs.length });

  let primed = false;
  const accept = async (dialogs, markers) => {
    const listReady = dialogs.length > 0 || markers.some((marker) => marker.includes("channel"));
    if (!primed) {
      if (!listReady) return;
      for (const dialog of dialogs) seen.add(keyOf(dialog));
      saveSeen(seen);
      primed = true;
      console.log("Уже открытые диалоги в Помогайку не переношу. Карточка появится на следующее новое сообщение.");
      return;
    }
    for (const dialog of dialogs) {
      const key = keyOf(dialog);
      if (seen.has(key)) continue;
      seen.add(key);
      writeLog({
        kind: "message",
        at: new Date().toISOString(),
        peer: dialog.peer,
        item: dialog.item,
        text: dialog.text,
        time: dialog.time,
        channelId: dialog.id,
      });
      console.log(`Новое: ${dialog.peer}: ${dialog.text}`);
      try {
        await publishMessage(dialog);
      } catch (error) {
        console.log(`В Помогайку не записалось (${error.message}). Повторю на следующем опросе.`);
        seen.delete(key);
      }
    }
    saveSeen(seen);
  };
  await accept(ready.dialogs, ready.markers);

  let away = false;
  while (true) {
    await sleep(POLL_MS);
    let state;
    try {
      state = await readPage(page);
    } catch (error) {
      console.log(`Страница недоступна (${error.message}). Жду следующий опрос.`);
      continue;
    }
    if (!isMessenger(state)) {
      if (!away) {
        console.log("Снова страница входа или проверки. Жду, окно не закрываю.");
        writeLog({ kind: "waiting", at: new Date().toISOString(), href: state.href });
        away = true;
      }
      continue;
    }
    if (away) {
      console.log("Мессенджер снова открыт.");
      away = false;
    }
    await accept(state.dialogs, state.markers);
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
