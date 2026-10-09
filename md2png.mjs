// md2png.mjs — 用系统 Chrome 把本地 HTML 渲染成 PNG（分片截图，供上层拼接）
// 用法：node md2png.mjs <html路径> <输出目录> [宽度] [每片CSS高度]
// 输出：stdout 一行 JSON { full, slices, outs:[...] }
//
// 🔴 为什么必须分片：Chrome 的 Page.captureScreenshot 在**设备像素高度超过 16384** 时
//    会**回绕**（wrap）—— 超出部分重复顶部内容，表现为「长图末尾出现开头一小段」。
//    实测：1520×18196 的图，底部内容最匹配的位置是 y≈1212，
//    正好等于 18196 − 16384。→ 单片设备像素高度必须 ≤ 16000（留余量）。
//
// 实现要点：
// 1) clip 是相对「视口」的 → 每片先把视口高度设为该片高度、再滚动到片起点，clip 用 y=0。
// 2) 分片边界取「最近的 DOM 元素边界」，避免从文字行中间横切（不拼接时也好看）。
//
// 🔴 外部依赖不写死本机绝对路径，走「环境变量 → 候选路径探测 → 明确报错」：
//    MEMO_PLAYWRIGHT_CORE  指向 playwright-core 包目录
//    MEMO_CHROME           指向浏览器可执行文件
import { createRequire } from "module";
import { fileURLToPath, pathToFileURL } from "url";
import path from "path";
import fs from "fs";

const require = createRequire(import.meta.url);
const HERE = path.dirname(fileURLToPath(import.meta.url));

// ── 定位 playwright-core ────────────────────────────────────────
const PW_CANDIDATES = [
  process.env.MEMO_PLAYWRIGHT_CORE,
  path.join(HERE, "node_modules", "playwright-core"),
  ...(process.env.NODE_PATH || "").split(path.delimiter).filter(Boolean)
    .map((p) => path.join(p, "playwright-core")),
].filter(Boolean);

function loadPlaywright() {
  const tried = [];
  for (const p of PW_CANDIDATES) {
    try {
      return require(p);
    } catch (e) {
      tried.push(`  ${p}  →  ${e.code || e.message}`);
    }
  }
  try {
    return require("playwright-core");
  } catch (e) {
    tried.push(`  (裸 require)  →  ${e.code || e.message}`);
  }
  throw new Error(
    "找不到 playwright-core。已尝试：\n" + tried.join("\n") +
    "\n→ 在本目录执行 npm i，或设环境变量 MEMO_PLAYWRIGHT_CORE 指向包目录。"
  );
}

// ── 定位浏览器 ──────────────────────────────────────────────────
const BROWSER_CANDIDATES = [
  process.env.MEMO_CHROME,
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
  path.join(process.env.LOCALAPPDATA || "", "Google", "Chrome", "Application", "chrome.exe"),
  "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
  "/usr/bin/chromium-browser",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
].filter(Boolean);

function findBrowser() {
  for (const p of BROWSER_CANDIDATES) {
    try {
      if (fs.existsSync(p)) return p;
    } catch { /* 无权限等，继续试下一个 */ }
  }
  throw new Error(
    "找不到浏览器可执行文件。已尝试：\n" +
    BROWSER_CANDIDATES.map((p) => `  ${p}`).join("\n") +
    "\n→ 设环境变量 MEMO_CHROME 指向 Chrome / Edge 的 exe。"
  );
}

const { chromium } = loadPlaywright();
const EXE = findBrowser();

const htmlPath = process.argv[2];
const outDir = process.argv[3];
const width = Number(process.argv[4] || 760);
const sliceArg = Number(process.argv[5] || 0);

const DPR = 2;
const MAX_DEVICE_PX = 16000; // Chrome 16384 回绕阈值，留 384 余量
const SLICE = sliceArg > 0 ? sliceArg : Math.floor(MAX_DEVICE_PX / DPR); // 默认 8000 CSS px
const HARD_MAX = 200000; // 极端文档保护

const browser = await chromium.launch({ executablePath: EXE, headless: true });
try {
  const page = await browser.newPage({
    // 初始视口刻意设小：短文档的 scrollHeight 才等于内容真实高度，
    // 否则会被撑到视口高度、底部留一片空白。
    viewport: { width, height: 300 },
    deviceScaleFactor: DPR,
  });
  await page.goto(pathToFileURL(htmlPath).href, { waitUntil: "load" });
  await page.waitForTimeout(300);

  const full = await page.evaluate(() => document.documentElement.scrollHeight);
  if (full > HARD_MAX) {
    throw new Error(`文档过高（${full}px > ${HARD_MAX}px），无法渲染`);
  }

  // 在 scrollTop=0 时收集元素边界（此时 getBoundingClientRect 即文档坐标）
  const marks = await page.evaluate(() => {
    const ys = [0];
    document.querySelectorAll("body *").forEach((el) => {
      const r = el.getBoundingClientRect();
      if (r.height > 0) {
        ys.push(Math.round(r.top));
        ys.push(Math.round(r.bottom));
      }
    });
    return ys;
  });
  const uniq = [...new Set(marks)].filter((y) => y >= 0 && y <= full).sort((a, b) => a - b);

  // 分片边界：尽量接近 cur+SLICE，但落在元素边界上（且不小于半个 SLICE）
  const bounds = [0];
  let cur = 0;
  while (cur < full) {
    const target = cur + SLICE;
    if (target >= full) {
      bounds.push(full);
      break;
    }
    let pick = null;
    for (const m of uniq) {
      if (m > target) break;
      if (m > cur + SLICE * 0.5) pick = m;
    }
    if (pick === null) pick = target;
    bounds.push(pick);
    cur = pick;
  }

  const outs = [];
  for (let i = 0; i < bounds.length - 1; i++) {
    const y0 = bounds[i];
    const h = bounds[i + 1] - y0;
    if (h <= 0) continue;
    await page.setViewportSize({ width, height: h });
    await page.evaluate((y) => window.scrollTo(0, y), y0);
    await page.waitForTimeout(120);
    const out = path.join(outDir, `page-${outs.length + 1}.png`);
    await page.screenshot({ path: out, clip: { x: 0, y: 0, width, height: h } });
    outs.push(out);
  }
  console.log(JSON.stringify({ full, slices: outs.length, outs }));
} finally {
  await browser.close();
}
