// Проверка отрисовки мини-приложения без браузера.
//
// Зачем: правка в app.html однажды снесла пять функций разом, и шахматка исчезла
// у собственника на экране — синтаксис при этом был валидный, node --check молчал.
// Здесь скрипт подсовывает минимальный DOM, зовёт renderGrid/renderBook/renderDrawer
// на настоящих данных и говорит, падают ли они и сколько всего нарисовалось.
//
//   node tools/render_check.js src/pomogaika/web/static/app.html payload.json
//
// payload.json — ответ /api/miniapp. Снять его можно так:
//   curl -b "pomogaika_session=<cookie>" "https://pomogaika.vddapp.ru/api/miniapp?year=2026&month=10" > payload.json

const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(process.argv[2], "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/g).pop().replace(/<\/?script>/g, "");
const data = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));

const nodes = {};
function makeNode(id) {
  return {
    id,
    innerHTML: "",
    textContent: "",
    value: "",
    dataset: {},
    style: { setProperty() {}, removeProperty() {} },
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    querySelectorAll: () => [],
    querySelector: () => null,
    appendChild() {},
    addEventListener() {},
    scrollLeft: 0,
    scrollWidth: 800,
    clientWidth: 400,
    offsetWidth: 400,
    getBoundingClientRect: () => ({ width: 400, height: 300, top: 0, left: 0 }),
  };
}
const proxyDoc = {
  getElementById: (id) => (nodes[id] = nodes[id] || makeNode(id)),
  querySelectorAll: () => [],
  querySelector: () => null,
  createElement: () => makeNode("tmp"),
  documentElement: makeNode("root"),
  addEventListener() {},
  body: makeNode("body"),
};

global.document = proxyDoc;
global.window = { Telegram: null, innerHeight: 900, innerWidth: 420, addEventListener() {}, location: { search: "" } };
global.location = { search: "", origin: "https://pomogaika.vddapp.ru", href: "" };
global.navigator = { userAgent: "node" };
global.fetch = async () => ({ ok: true, status: 200, json: async () => data });
global.setTimeout = (fn) => 0;
global.clearTimeout = () => {};

const sandbox = { module: { exports: {} } };
const wrapped = script + "\n;module.exports = { renderGrid, renderBook, renderDrawer, bookingCard, setData: (d) => { D = d; } };";

let api;
try {
  api = eval(`(function(){ ${wrapped}; return module.exports; })()`);
} catch (e) {
  console.log("ПАДЕНИЕ при загрузке скрипта:", e.message);
  process.exit(1);
}

api.setData(data);
const checks = [
  ["renderGrid", () => api.renderGrid()],
  ["renderBook", () => api.renderBook()],
  ["renderDrawer", () => api.renderDrawer()],
  ["bookingCard", () => api.bookingCard(data.bookings[0])],
];
let failed = 0;
for (const [name, fn] of checks) {
  try {
    fn();
    console.log(`  ${name}: ок`);
  } catch (e) {
    failed++;
    console.log(`  ${name}: ПАДАЕТ — ${e.message}`);
  }
}
const tl = nodes["tl"];
console.log("");
console.log("шахматка отрисована символов:", tl ? tl.innerHTML.length : 0);
console.log("полос в шахматке:", tl ? (tl.innerHTML.match(/data-book=/g) || []).length : 0);
const book = nodes["s-book"];
console.log("карточек в списке броней:", book ? (book.innerHTML.match(/data-card=/g) || []).length : 0);
process.exit(failed ? 1 : 0);
