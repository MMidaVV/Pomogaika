// Локальный эксперимент: свой аккаунт Авито, свои диалоги, лог на диск.
// Запуск из этой папки: npm install && npm start
// Вход и код из SMS вводите в открывшемся окне сами.
// Сброс «уже виденного»: удалите logs/seen.json

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer-extra";
import StealthPlugin from "puppeteer-extra-plugin-stealth";

puppeteer.use(StealthPlugin());

const root = path.dirname(fileURLToPath(import.meta.url));
const profileDir = path.join(root, "profile");
const logDir = path.join(root, "logs");
const logFile = path.join(logDir, "messages.jsonl");
const seenFile = path.join(logDir, "seen.json");
const markerFile = path.join(logDir, "markers.json");

const MESSENGER_URL = "https://www.avito.ru/profile/messenger";
const POLL_MS = 15_000;
const LOGIN_WAIT_MS = 20 * 60 * 1000;

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
      const link = node.querySelector('a[href*="messenger"]')?.getAttribute("href") || "";
      const id = node.getAttribute("data-id") || link;
      const peer = textOf(node, "channels/user-title") || textOf(node, "channels/item-title") || "Собеседник";
      const item = textOf(node, "channels/item-title");
      const text = textOf(node, "channels/last-message");
      const time = textOf(node, "channels/message-time");
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
      loginForm: !!document.querySelector(
        'input[name="login"], input[type="tel"], [data-marker="login-form"], [data-marker="phone/input"]',
      ),
      dialogs,
      markers,
    };
  });
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
  await page.goto(MESSENGER_URL, { waitUntil: "domcontentloaded", timeout: 60_000 });

  const deadline = Date.now() + LOGIN_WAIT_MS;
  let ready = null;
  while (Date.now() < deadline) {
    let state;
    try {
      state = await readPage(page);
    } catch {
      await sleep(1000);
      continue;
    }
    if (state.blocked) {
      console.log("Авито показывает проверку. Скрипт её не проходит. Окно оставляю открытым, опрос не начинаю.");
      browser.disconnect();
      return;
    }
    if (state.onMessenger && !state.loginForm) {
      ready = state;
      break;
    }
    await sleep(2000);
  }

  if (!ready) {
    console.log("За 20 минут мессенджер так и не открылся. Запустите скрипт ещё раз и войдите в окне.");
    browser.disconnect();
    return;
  }

  fs.writeFileSync(markerFile, JSON.stringify(ready.markers, null, 2));
  console.log(`Мессенджер открыт. Сейчас вижу диалогов: ${ready.dialogs.length}.`);
  printDialogs(ready.dialogs);
  if (!ready.dialogs.length && !ready.markers.some((marker) => marker.includes("channel"))) {
    console.log("Подписи элементов на странице не похожи на список диалогов. Они записаны в logs/markers.json.");
  }
  console.log(`Дальше в ${path.relative(root, logFile)} попадут только новые сообщения.`);
  writeLog({ kind: "started", at: new Date().toISOString(), dialogs: ready.dialogs.length });

  let primed = seen.size > 0;
  const accept = (dialogs, markers) => {
    const listReady = dialogs.length > 0 || markers.some((marker) => marker.includes("channel"));
    if (!primed) {
      if (!listReady) return;
      for (const dialog of dialogs) seen.add(keyOf(dialog));
      saveSeen(seen);
      primed = true;
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
    }
    saveSeen(seen);
  };
  accept(ready.dialogs, ready.markers);

  while (true) {
    await sleep(POLL_MS);
    let state;
    try {
      state = await readPage(page);
    } catch (error) {
      console.log(`Страница недоступна (${error.message}). Жду следующий опрос.`);
      continue;
    }
    if (state.blocked || (state.loginForm && !state.dialogs.length && !state.onMessenger)) {
      console.log("Авито снова просит вход или проверку. Опрос останавливаю, окно не закрываю.");
      writeLog({ kind: "stopped", at: new Date().toISOString(), href: state.href });
      browser.disconnect();
      return;
    }
    accept(state.dialogs, state.markers);
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
