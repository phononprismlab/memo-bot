"""把灵感库渲染成一个「思维泡泡」轮播页面，并支持在页面上直接处理灵感。

来源：灵感 `[10-08 17:25] (decided) 我想起之前对于个人网站的设想，就是一个我，
背后不断滚动？浮现那些天马行空的想法……` 的落地。

用法：
  python ideas-bubbles.py                       # 生成静态 HTML → 工作区根 灵感泡泡.html
  python ideas-bubbles.py --serve               # 起本地服务并打开浏览器（页面里点一下**直接写库**）
  python ideas-bubbles.py --apply 灵感操作.json  # 应用离线模式导出的操作
  python ideas-bubbles.py -o out.html
  python ideas-bubbles.py --open-only           # 只放「待处理」的灵感
  python ideas-bubbles.py --include-memo        # 连备忘一起放

设计：
  - 气泡从底部「思维源」升起、横向漂移、顶部淡出，循环不停。
  - 一轮内不重复；一轮放完自动重洗（所以每次打开顺序都不一样）。
  - 按灵感状态配色；长文本气泡自动缩小字号，**正文完整显示、不截断**。
  - 悬停暂停单个气泡，点击展开浮层（全文 + 处理按钮 + 结论输入）。
  - 数据在生成时**内嵌**进 HTML —— file:// 下 fetch 本地 JSON 会被 CORS 拦，内嵌最稳。

两种处理模式（页面自动探测）：
  - **在线（--serve）**：`GET /api/ping` 通 → 点按钮直接 POST 写 memos.db。
  - **离线（双击 HTML）**：操作暂存 localStorage，顶部显示待提交数，可导出 `灵感操作.json`，
    再跑 `--apply` 落地。
"""
import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import store  # noqa: E402

WORKSPACE = os.path.dirname(HERE)
DEFAULT_OUT = os.path.join(WORKSPACE, "灵感泡泡.html")

# 状态 → 展示名（颜色在 CSS 里）
STATUS_LABEL = {
    "open": "待处理",
    "done": "已处理",
    "decided": "已决议",
    "dropped": "已丢弃",
}
STATUS_ORDER = ("open", "done", "decided", "dropped")
VALID_STATUS = set(STATUS_ORDER)


def clean(text):
    """去掉 @ 占位符与多余空白（和 bot 侧 strip_at 的清洗口径一致）。"""
    t = re.sub(r"<[^<>\s]{1,40}>", " ", text or "")
    return re.sub(r"\s+", " ", t).strip()


def collect(open_only=False, include_memo=False):
    """读灵感库 → 按时间正序的条目列表（带 id，供页面回写状态）。"""
    if include_memo:
        rows = list(reversed(store.list_memos(500)))
    else:
        rows = store.list_ideas(None, 500, newest_first=False)
    items = []
    for m in rows:
        content = clean(m.get("content"))
        if not content:
            continue
        status = m.get("status") or "open"
        if open_only and status != "open":
            continue
        items.append({
            "id": m.get("id") or "",
            "t": content,
            "s": status if status in STATUS_LABEL else "open",
            "d": (m.get("created_at") or "")[:16].replace("T", " "),
            "k": m.get("kind") or "idea",
            "note": m.get("note") or "",
        })
    return items


TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<link rel="icon" href="data:,">
<title>灵感泡泡__BRAND__</title>
<style>
  :root{
    --ink:#e6edf3; --dim:#8b98a9; --line:rgba(255,255,255,.12);
    --open:#4fd1c5; --done:#68d391; --decided:#b794f4; --dropped:#7c8798;
  }
  *{box-sizing:border-box}
  html,body{height:100%;margin:0}
  body{
    overflow:hidden; color:var(--ink);
    font:15px/1.6 -apple-system,"Segoe UI","Microsoft YaHei","PingFang SC",sans-serif;
    background:
      radial-gradient(ellipse 130% 90% at 50% 118%, #17334f 0%, #0d1a2b 42%, #070a11 100%);
    -webkit-font-smoothing:antialiased;
  }
  /* 水下光斑 */
  body::before{
    content:""; position:fixed; inset:-20%; pointer-events:none; opacity:.5;
    background:
      radial-gradient(circle 22vmax at 18% 8%, rgba(79,209,197,.13), transparent 62%),
      radial-gradient(circle 18vmax at 84% 22%, rgba(183,148,244,.11), transparent 62%),
      radial-gradient(circle 26vmax at 50% 96%, rgba(56,132,255,.14), transparent 66%);
    animation:drift 26s ease-in-out infinite alternate;
  }
  @keyframes drift{from{transform:translate3d(-2%,-1%,0) scale(1)}to{transform:translate3d(2%,2%,0) scale(1.06)}}

  /* 🔴 标题栏 + 提示条包在一个带渐变底的容器里 —— 气泡飘到顶部时从它下面穿过，
        不会和标题糊在一起（z-index 22 高于气泡） */
  .topbar{
    position:fixed; top:0; left:0; right:0; z-index:22; pointer-events:none;
    padding-bottom:40px;
    background:linear-gradient(180deg,#080b14 0%,#080b14 54%,rgba(8,11,20,.82) 79%,rgba(8,11,20,0) 100%);
  }
  header{
    display:flex; align-items:center; gap:14px; flex-wrap:wrap;
    padding:16px 22px 0;
  }
  header h1{margin:0; font-size:16px; font-weight:700; letter-spacing:.4px}
  header h1 span{color:var(--dim); font-weight:400; margin-left:8px; font-size:13px}
  .legend{display:flex; gap:12px; margin-left:auto; font-size:12px; color:var(--dim)}
  .legend i{display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:5px}
  .legend .l-open i{background:var(--open)}
  .legend .l-done i{background:var(--done)}
  .legend .l-decided i{background:var(--decided)}
  .legend .l-dropped i{background:var(--dropped)}

  #stage{position:fixed; inset:0; overflow:hidden}

  .bubble{
    position:absolute; bottom:-10vh; left:0;
    padding:13px 17px; border-radius:20px;
    max-width:var(--mw,30%);
    background:rgba(255,255,255,.055);
    border:1px solid var(--line);
    box-shadow:0 10px 34px rgba(0,0,0,.4), inset 0 1px 0 rgba(255,255,255,.09);
    -webkit-backdrop-filter:blur(7px); backdrop-filter:blur(7px);
    cursor:pointer; will-change:transform,opacity;
    animation:floatUp var(--dur) cubic-bezier(.32,.06,.62,.96) forwards;
    border-left:3px solid var(--accent,var(--open));
  }
  .bubble:hover{animation-play-state:paused; background:rgba(255,255,255,.1)}
  .bubble[data-s="open"]{--accent:var(--open)}
  .bubble[data-s="done"]{--accent:var(--done)}
  .bubble[data-s="decided"]{--accent:var(--decided)}
  .bubble[data-s="dropped"]{--accent:var(--dropped)}
  .bubble .meta{
    display:block; font-size:10.5px; letter-spacing:.5px; color:var(--dim);
    margin-bottom:5px; text-transform:uppercase;
  }
  .bubble .txt{word-break:break-word; white-space:pre-wrap}
  .b-s{font-size:21px}
  .b-m{font-size:16px}
  .b-l{font-size:14px}
  .b-xl{font-size:12.5px; line-height:1.7}

  @keyframes floatUp{
    0%  {transform:translate(0,0) scale(.72); opacity:0}
    7%  {opacity:1}
    15% {transform:translate(calc(var(--dx) * .22),-16vh) scale(1)}
    72% {opacity:.94}
    100%{transform:translate(var(--dx),-116vh) scale(1.05); opacity:0}
  }

  /* 底部思维源 */
  .source{
    position:fixed; left:50%; bottom:6.5vh; transform:translateX(-50%);
    z-index:5; text-align:center; pointer-events:none;
  }
  .source .halo{
    width:74px; height:74px; margin:0 auto; border-radius:50%;
    background:radial-gradient(circle,rgba(79,209,197,.42),rgba(79,209,197,0) 68%);
    animation:pulse 3.6s ease-in-out infinite;
  }
  .source .label{font-size:11px; letter-spacing:3px; color:var(--dim); margin-top:-6px}
  @keyframes pulse{0%,100%{transform:scale(.86);opacity:.65}50%{transform:scale(1.14);opacity:1}}

  /* 全文浮层 */
  #modal{
    position:fixed; inset:0; z-index:40; display:none;
    align-items:center; justify-content:center; padding:26px;
    background:rgba(4,7,12,.74); -webkit-backdrop-filter:blur(4px); backdrop-filter:blur(4px);
  }
  #modal.on{display:flex}
  #modal .card{
    max-width:min(620px,92vw); max-height:76vh; overflow:auto;
    padding:26px 28px; border-radius:20px;
    background:rgba(20,28,42,.94); border:1px solid var(--line);
    box-shadow:0 24px 70px rgba(0,0,0,.6);
  }
  #modal .meta{font-size:11.5px; color:var(--dim); letter-spacing:.6px; margin-bottom:12px}
  #modal .body{font-size:16px; line-height:1.8; white-space:pre-wrap; word-break:break-word}
  #modal .hint{margin-top:18px; font-size:11.5px; color:var(--dim); line-height:1.7}

  /* 处理操作区 */
  #modal .note{
    width:100%; margin-top:18px; padding:10px 12px; border-radius:11px;
    background:rgba(255,255,255,.06); border:1px solid var(--line);
    color:var(--ink); font:13.5px/1.6 inherit; resize:vertical; min-height:46px;
  }
  #modal .note::placeholder{color:#66717f}
  #modal .note:focus{outline:none; border-color:rgba(79,209,197,.55)}
  #modal .actions{display:flex; gap:8px; flex-wrap:wrap; margin-top:12px}
  #modal .actions button{
    flex:1 1 auto; min-width:108px; padding:10px 14px; border-radius:11px; cursor:pointer;
    background:rgba(255,255,255,.07); border:1px solid var(--line); color:var(--ink);
    font:13.5px/1 inherit; transition:.16s;
  }
  #modal .actions button:hover{background:rgba(255,255,255,.14); transform:translateY(-1px)}
  #modal .actions button[data-act="done"]:hover{border-color:var(--done); color:var(--done)}
  #modal .actions button[data-act="decided"]:hover{border-color:var(--decided); color:var(--decided)}
  #modal .actions button[data-act="dropped"]:hover{border-color:var(--dropped); color:var(--dropped)}
  #modal .actions button[data-act="open"]:hover{border-color:var(--open); color:var(--open)}
  #modal .actions button[disabled]{opacity:.45; cursor:default; transform:none}

  /* 顶部工具条：模式提示 + 待提交 */
  .bar{
    display:flex; align-items:center; gap:9px; padding:10px 22px 0;
    font-size:11.5px; color:var(--dim); flex-wrap:wrap;
  }
  .bar .pill{
    pointer-events:auto; padding:5px 11px; border-radius:999px;
    border:1px solid var(--line); background:rgba(255,255,255,.05);
  }
  .bar .pill.live{color:#68d391; border-color:rgba(104,211,145,.42)}
  .bar .pill.offline{color:#f6ad55; border-color:rgba(246,173,85,.42)}
  .bar .pill.pending{color:#4fd1c5; border-color:rgba(79,209,197,.5); cursor:pointer}
  .bar .pill.pending:hover{background:rgba(79,209,197,.15)}

  #toast{
    position:fixed; left:50%; bottom:8vh; transform:translate(-50%,14px);
    z-index:60; max-width:82vw; padding:11px 18px; border-radius:12px; font-size:13px;
    background:rgba(26,36,52,.97); border:1px solid var(--line); color:var(--ink);
    box-shadow:0 14px 40px rgba(0,0,0,.5);
    opacity:0; pointer-events:none; transition:.22s;
  }
  #toast.on{opacity:1; transform:translate(-50%,0)}

  #empty{
    position:fixed; inset:0; z-index:8; display:none;
    align-items:center; justify-content:center; flex-direction:column; gap:12px;
    color:var(--dim); font-size:15px; pointer-events:none;
  }
  #empty.on{display:flex}
  #empty .big{font-size:38px}

  /* 🔴 页脚同 topbar：给一层向上渐隐的底色，气泡「从黑暗里升起」而不是糊在页脚字上 */
  footer{
    position:fixed; left:0; right:0; bottom:0; z-index:20;
    padding:26px 22px 10px; font-size:11.5px; color:var(--dim);
    display:flex; gap:16px; pointer-events:none; flex-wrap:wrap;
    background:linear-gradient(0deg,#080b14 0%,#080b14 56%,rgba(8,11,20,.86) 79%,rgba(8,11,20,0) 100%);
  }
  footer b{color:#aab6c4; font-weight:600}
  @media (max-width:640px){
    header{padding:12px 14px 0} .legend{display:none}
    .bar{padding:8px 14px 0}
    .topbar{padding-bottom:30px}
    .b-s{font-size:19px} .b-m{font-size:15px} .b-l{font-size:13px} .b-xl{font-size:12px}
  }
</style>
</head>
<body>
<div class="topbar">
  <header>
    <h1>💭 灵感泡泡<span>__BRAND__</span></h1>
    <div class="legend">
      <span class="l-open"><i></i>待处理</span>
      <span class="l-done"><i></i>已处理</span>
      <span class="l-decided"><i></i>已决议</span>
      <span class="l-dropped"><i></i>已丢弃</span>
    </div>
  </header>
  <div class="bar">
    <span class="pill" id="mode">检测中…</span>
    <span class="pill pending" id="pending" style="display:none"></span>
  </div>
</div>

<div id="stage"></div>
<div class="source"><div class="halo"></div><div class="label">思维源</div></div>

<div id="empty"><div class="big">🎉</div><div>灵感都处理完了</div></div>

<div id="modal"><div class="card">
  <div class="meta"></div>
  <div class="body"></div>
  <textarea class="note" id="note" rows="2" placeholder="结论 / 备注（可选）"></textarea>
  <div class="actions">
    <button data-act="done">✅ 已处理</button>
    <button data-act="decided">📌 已决议</button>
    <button data-act="dropped">🗑 丢弃</button>
    <button data-act="open">↩️ 恢复待处理</button>
  </div>
  <div class="hint">处理后会从轮播移出 · 点卡片外关闭 · 空格暂停/继续</div>
</div></div>

<div id="toast"></div>

<footer>
  <span>共 <b id="n">0</b> 条灵感</span>
  <span>生成于 <b id="gen"></b></span>
  <span id="state">空格暂停 / 继续</span>
</footer>

<script>
const IDEAS = __DATA__;
const LABEL = __LABEL__;
const GEN = __GEN__;

const stage = document.getElementById('stage');
const modal = document.getElementById('modal');
const stateEl = document.getElementById('state');
const modeEl = document.getElementById('mode');
const pendEl = document.getElementById('pending');
const toastEl = document.getElementById('toast');
const noteEl = document.getElementById('note');
const emptyEl = document.getElementById('empty');
const nEl = document.getElementById('n');

nEl.textContent = IDEAS.length;
document.getElementById('gen').textContent = GEN;

let queue = [];
let running = true;
let lane = 0;
let MODE = 'offline';
let current = null;

/* ── 处理状态 ───────────────────────────────────────────── */
const PENDING_KEY = 'bubble_pending_v1';

function lsGet(k, dflt){
  try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : dflt; }
  catch(e){ return dflt; }
}
function lsSet(k, v){
  try { localStorage.setItem(k, JSON.stringify(v)); } catch(e){}
}

let pending = lsGet(PENDING_KEY, {});

function renderPending(){
  const c = Object.keys(pending).length;
  if(c && MODE === 'offline'){
    pendEl.style.display = '';
    pendEl.textContent = '⬇ 待提交 ' + c + ' 条 · 点此导出';
  } else {
    pendEl.style.display = 'none';
  }
}

function toast(msg){
  toastEl.textContent = msg;
  toastEl.classList.add('on');
  clearTimeout(toast._t);
  toast._t = setTimeout(()=>toastEl.classList.remove('on'), 2400);
}

async function detectMode(){
  // 🔴 file:// 下直接判定离线 —— 否则 fetch('/api/ping') 会被 CORS 拦，
  //    功能上没问题（catch 住），但会在 console 里留两条红字，看着像坏了
  if(location.protocol !== 'file:'){
    try {
      const r = await fetch('/api/ping', {cache:'no-store'});
      const j = await r.json();
      if(j && j.ok) MODE = 'serve';
    } catch(e){ MODE = 'offline'; }
  }
  if(MODE === 'serve'){
    modeEl.className = 'pill live';
    modeEl.textContent = '● 直连模式 —— 处理即刻写库';
  } else {
    modeEl.className = 'pill offline';
    modeEl.textContent = '○ 离线模式 —— 操作暂存，可导出';
  }
  renderPending();
}

function dropIdea(id){
  const i = IDEAS.findIndex(x=>x.id === id);
  if(i >= 0) IDEAS.splice(i, 1);
  queue = queue.filter(x=>x.id !== id);
  nEl.textContent = IDEAS.length;
  stage.querySelectorAll('.bubble').forEach(el=>{ if(el.dataset.id === id) el.remove(); });
  if(!IDEAS.length) emptyEl.classList.add('on');
}

async function applyStatus(it, status, note){
  if(MODE === 'serve'){
    const r = await fetch('/api/status', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id: it.id, status: status, note: note || ''}),
    });
    const j = await r.json();
    if(!j.ok) throw new Error(j.error || '写入失败');
  } else {
    pending[it.id] = {s: status, note: note || '', t: Date.now()};
    lsSet(PENDING_KEY, pending);
    renderPending();
  }
  dropIdea(it.id);
  toast('已标记「' + (LABEL[status]||status) + '」'
        + (MODE === 'serve' ? '' : ' · 暂存中，记得导出'));
}

pendEl.addEventListener('click', ()=>{
  const ops = Object.entries(pending).map(([id, v])=>({id: id, status: v.s, note: v.note || ''}));
  if(!ops.length) return;
  const blob = new Blob([JSON.stringify({ops: ops}, null, 2)], {type:'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = '灵感操作.json';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(()=>URL.revokeObjectURL(a.href), 1500);
  toast('已导出 ' + ops.length + ' 条 —— 再跑：python memo-bot/ideas-bubbles.py --apply 灵感操作.json');
});

/* ── 气泡 ───────────────────────────────────────────────── */
function shuffle(a){
  for(let i=a.length-1;i>0;i--){const j=Math.floor(Math.random()*(i+1));[a[i],a[j]]=[a[j],a[i]];}
  return a;
}
function refill(){ queue = shuffle(IDEAS.slice()); }
// 🔴 从队尾取 —— 所以「放回」必须 unshift 到队首；用 push 会把它又放回队尾，
//    下一次 pick 立刻原样取回，挑 16 次等于挑 1 次（真机实测：窄屏只剩 1 个气泡）。
function pick(){ if(!queue.length) refill(); return queue.pop(); }
function unPick(it){ queue.unshift(it); }

// 布局参数：窄屏 2 泳道 + 更稀疏，宽屏 3 泳道
// 🔴 maxB 决定稳态密度（气泡寿命固定，同时最多 maxB 个 → 竖直间距 = 126vh/maxB），
//    不要靠 tick 调密度：tick 只影响「补位快慢」，调小了反而让补位气泡叠上来。
function layout(){
  const narrow = window.innerWidth < 700;
  const n = narrow ? 2 : 3;
  return {
    narrow, n, w: 100 / n,
    gap: narrow ? 3.4 : 2.4,
    maxB: narrow ? 5 : 8,
    tick: narrow ? 3000 : 2400,
  };
}

// 「长文本」判据 —— 按**估算高度**判，不按字数。
// 🔴 固定字数阈值在两种屏上都会判错：同样 100 字，1440 的泳道里 1 行、390 的泳道里 10 行；
//    而「多高算太高」取决于泳道竖直间距（= 126vh / maxB），跟字数没有固定关系。
const BASE_DUR = 24;      // 上升 126vh 用的秒数（全员一致）
const BUB_PAD = 48;       // 气泡上下 padding + meta 行，粗算
function fontSize(n){ return n<=14?21 : n<=45?16 : n<=110?14 : 12.5; }
function estHeight(n, widthPx){
  const fp = fontSize(n);
  const lines = Math.ceil(n / Math.max(6, widthPx / fp));
  return lines * fp * 1.7 + BUB_PAD;
}
function isLongText(it){
  const cfg = layout();
  const n = it.t.length;
  const laneW = (cfg.w - cfg.gap) / 100 * innerWidth - BUB_PAD;
  const fullW = (100 - 2 * cfg.gap) / 100 * innerWidth - BUB_PAD;
  const hLane = estHeight(n, laneW);
  const budget = innerHeight * 1.26 / cfg.maxB * 0.9;   // 竖直间距留 10% 余量
  // 只有「泳道里装不下」**且**「横过来能明显变矮（至少省 25%）」才跨泳道，
  // 否则为了跨而跨，反而把整条舞台的错位节奏打乱
  return hLane > budget && estHeight(n, fullW) < hLane * 0.75;
}
const hasLongOnStage = () =>
  [...stage.children].some(el => el.dataset.long === '1');

// 字号只跟字数走（跟「跨不跨泳道」是两回事）
function sizeClass(n){
  if(n<=14) return 'b-s';
  if(n<=45) return 'b-m';
  if(n<=110) return 'b-l';
  return 'b-xl';
}

// warm ∈ [0,1)：开局「预热」——用负 animation-delay 把气泡直接放到飞行途中，
// 而不是让它们都从底部一个接一个冒出来（那样前 20 秒会挤成一坨，实测重叠 3~5 处）。
// 🔴 补位必须**卡最小间隔**，不能「死一个就立刻补一个」：
//    补位节奏被 tick / 定时器抖动带偏，竖直间距就不再是 126vh/maxB，
//    长气泡（230px）会啃到邻居。速度一致 + 间隔一致 = 永久等距。
let lastSpawn = 0;
function minGapMs(cfg){ return BASE_DUR * 1000 / cfg.maxB; }

function spawn(warm){
  if(!running || !IDEAS.length) return;
  const cfg = layout();
  if(stage.childElementCount >= cfg.maxB) return;   // 防堆积
  if(!warm){                                        // 预热不受间隔限制
    const now = Date.now();
    if(now - lastSpawn < minGapMs(cfg)) return;
    lastSpawn = now;
  }

  // 🔴 窄屏上一条长文本气泡就占掉小半屏 —— 只允许同时存在一条，
  //    否则后冒出来的全被它盖住（真机截图实测）。
  //    预热阶段（warm）直接避开长文本：预热是「凭空出现在半空」，没有从底部升起的
  //    自然错位，长气泡横跨全宽，落点附近的气泡必被压住。
  let it = pick();
  const avoidLong = (cfg.narrow && hasLongOnStage()) || warm > 0;
  if(avoidLong){
    const tries = Math.min(16, IDEAS.length);
    for(let t = 0; t < tries && isLongText(it); t++){ unPick(it); it = pick(); }
    // 挑不出来、窄屏又已经有长气泡了 → 这次干脆不补位（放回队首，等它飘走再来），
    // 硬塞上去只会两条互相压住
    if(isLongText(it) && cfg.narrow && hasLongOnStage()){
      unPick(it);
      return;
    }
  }

  const n = it.t.length;
  const el = document.createElement('div');
  el.className = 'bubble ' + sizeClass(n);
  el.dataset.s = it.s;
  el.dataset.id = it.id;
  // 🔴 所有气泡**同一速度**上升。之前按字数给不同寿命（16~31s），快的会追上慢的 ——
  //    出发时留的竖直间距飞几秒就被吃光，然后两条叠一起（真机截图实测）。
  //    速度一致 ⇒ 间距恒定 = 126vh / maxB，永久不撞。
  const dur = BASE_DUR + Math.random()*0.8;   // 只留极小的抖动，避免看着像机械传送带
  el.style.setProperty('--dur', dur.toFixed(1) + 's');
  const delay = warm > 0 ? -dur * Math.min(warm, 0.97) : 0;
  if(delay) el.style.animationDelay = delay.toFixed(2) + 's';

  // 🔴 长文本跨泳道占满宽度 —— 挤在单泳道里会又高又窄，
  //    高度超过泳道的垂直间距就会和前后气泡叠在一起（截图实测过）。
  const isLong = isLongText(it);
  el.dataset.long = isLong ? '1' : '0';
  let left, mw, dir;
  if(isLong){
    mw = 100 - 2*cfg.gap;
    left = cfg.gap;
    dir = Math.random() < .5 ? -1 : 1;
  } else {
    const L = lane++ % cfg.n;
    mw = cfg.w - cfg.gap;
    left = L * cfg.w + cfg.gap/2;
    // 🔴 最左/最右泳道只向**内**漂 —— 否则横向漂移会把气泡推出屏幕被裁掉
    dir = L === 0 ? 1 : (L === cfg.n - 1 ? -1 : (Math.random() < .5 ? -1 : 1));
  }
  el.style.setProperty('--mw', mw.toFixed(1) + '%');
  el.style.left = left.toFixed(1) + '%';
  // 🔴 漂移幅度按 gap 走（vw 就是 stage 的 %，stage = 满屏）：
  //    相邻泳道只隔一个 gap，漂太多就会横向撞上隔壁泳道（窄屏 2 泳道尤其明显）。
  const amp = isLong ? 0.32 : 0.60;
  el.style.setProperty('--dx', (dir * (0.12 + Math.random() * amp) * cfg.gap).toFixed(2) + 'vw');

  const meta = document.createElement('span');
  meta.className = 'meta';
  meta.textContent = (LABEL[it.s]||'') + ' · ' + it.d;
  const txt = document.createElement('div');
  txt.className = 'txt';
  txt.textContent = it.t;              // 🔴 全文，不截断

  el.append(meta, txt);
  el.addEventListener('click', (e)=>{ e.stopPropagation(); openModal(it); });
  stage.appendChild(el);
  setTimeout(()=>el.remove(), (dur + delay)*1000 + 400);   // delay 为负 → 剩余寿命更短
}

/* ── 浮层 ───────────────────────────────────────────────── */
function openModal(it){
  current = it;
  modal.querySelector('.meta').textContent = (LABEL[it.s]||'') + ' · ' + it.d;
  modal.querySelector('.body').textContent = it.t;
  noteEl.value = it.note || '';
  modal.classList.add('on');
}

// 只有点「卡片外」才关 —— 否则点输入框也会误关
modal.addEventListener('click', (e)=>{ if(e.target === modal) modal.classList.remove('on'); });

modal.querySelectorAll('.actions button').forEach(btn=>{
  btn.addEventListener('click', async (e)=>{
    e.stopPropagation();
    if(!current) return;
    const act = btn.dataset.act;
    const note = noteEl.value.trim();
    btn.disabled = true;
    try {
      await applyStatus(current, act, note);
      modal.classList.remove('on');
      current = null;
    } catch(err){
      toast('写入失败：' + err.message);
    } finally {
      btn.disabled = false;
    }
  });
});

/* ── 键盘 / 启动 ─────────────────────────────────────────── */
// 暂停 = 不再生成新气泡（已在飘的继续飘完，避免半路卡住）
function setRunning(v){
  running = v;
  stateEl.textContent = v ? '空格暂停 / 继续' : '已暂停 —— 空格继续';
}

document.addEventListener('keydown', (e)=>{
  if(e.code === 'Space' && document.activeElement !== noteEl){
    e.preventDefault(); setRunning(!running);
  }
  if(e.code === 'Escape') modal.classList.remove('on');
});

detectMode();
refill();

// 🔴 开局一次性按「飞行进度」均匀铺满 —— 不要 setTimeout 连发，
//    连发会让所有气泡挤在屏幕底部同一段高度里（真机截图：overlaps 3~5）。
// 预热位置**等距**（i/maxB），别加随机抖动 —— 抖一下就可能让两个气泡撞在同一高度
const boot = layout();
for(let i=0;i<boot.maxB;i++) spawn(i / boot.maxB);
lastSpawn = Date.now();     // 预热铺完，下一个补位要等满一个间隔

function loop(){
  if(running) spawn();
  setTimeout(loop, layout().tick);
}
setTimeout(loop, 900);
</script>
</body>
</html>
"""


# 页面品牌名（可选，默认空）。设 MEMO_PAGE_BRAND 可换成你自己的产品名。
PAGE_BRAND = (os.getenv("MEMO_PAGE_BRAND") or "").strip()
_BRAND_SUFFIX = f" · {PAGE_BRAND}" if PAGE_BRAND else ""


def build_html(items):
    stats = {}
    for it in items:
        stats[it["s"]] = stats.get(it["s"], 0) + 1
    data = json.dumps(items, ensure_ascii=False).replace("</", "<\\/")
    return (
        TEMPLATE
        .replace("__BRAND__", _BRAND_SUFFIX if PAGE_BRAND else "")
        .replace("__DATA__", data)
        .replace("__LABEL__", json.dumps(STATUS_LABEL, ensure_ascii=False))
        .replace("__GEN__", json.dumps(time.strftime("%Y-%m-%d %H:%M"), ensure_ascii=False))
    ), stats


def apply_ops(path):
    """应用离线模式导出的 `灵感操作.json`。"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    ops = data.get("ops") if isinstance(data, dict) else data
    if not isinstance(ops, list) or not ops:
        print("（文件里没有可应用的操作）")
        return 1
    store.init()
    n = 0
    for op in ops:
        iid = (op.get("id") or "").strip()
        status = (op.get("status") or "").strip()
        if not iid or status not in VALID_STATUS:
            print(f"  跳过非法条目：{op}")
            continue
        c = store.set_idea_status([iid], status, (op.get("note") or "").strip() or None)
        n += c
        print(f"  [{STATUS_LABEL[status]}] {iid[:8]} {'OK' if c else '（未找到）'}")
    print(f"已应用 {n} 条")
    return 0


def _make_handler(doc):
    """本地服务的请求处理器：静态页 + /api/ping + /api/ideas + /api/status。"""
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        server_version = "IdeasBubbles/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass  # 静音访问日志，只留我们自己打的处理记录

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False)
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/api/ping"):
                return self._send(200, {"ok": True, "mode": "serve"})
            if self.path.startswith("/api/ideas"):
                return self._send(200, {"ok": True, "items": collect()})
            if self.path in ("/", "/index.html"):
                return self._send(200, doc, "text/html; charset=utf-8")
            return self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            if not self.path.startswith("/api/status"):
                return self._send(404, {"ok": False, "error": "not found"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, OSError) as e:
                return self._send(400, {"ok": False, "error": f"请求体解析失败：{e}"})
            iid = (payload.get("id") or "").strip()
            status = (payload.get("status") or "").strip()
            note = (payload.get("note") or "").strip()
            if not iid or status not in VALID_STATUS:
                return self._send(400, {"ok": False, "error": "id 或 status 不合法"})
            try:
                changed = store.set_idea_status([iid], status, note or None)
            except Exception as e:  # noqa: BLE001 —— 回给页面，别让服务挂掉
                return self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            if not changed:
                return self._send(404, {"ok": False, "error": "没找到这条灵感（可能已被删）"})
            print(f"  [{STATUS_LABEL[status]}] {iid[:8]}  {note[:40]}", flush=True)
            return self._send(200, {"ok": True, "changed": changed})

    return Handler


def serve(doc, port=8765, open_browser=True, tries=12):
    """起本地服务：页面上的处理按钮**直接写入** memos.db。"""
    from http.server import ThreadingHTTPServer

    handler = _make_handler(doc)
    httpd = None
    for p in range(port, port + tries):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), handler)
            port = p
            break
        except OSError:
            continue
    if httpd is None:
        print(f"（{port}~{port + tries - 1} 都没空端口，用 --port 换一个）")
        return 1

    url = f"http://127.0.0.1:{port}/"
    print(f"灵感泡泡服务已启动：{url}")
    print("  页面上的处理按钮会**直接写入** memos.db；Ctrl+C 停止。")
    if open_browser:
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


def main():
    ap = argparse.ArgumentParser(description="灵感泡泡轮播页面（生成静态页 / 起本地服务）")
    ap.add_argument("-o", "--out", default=DEFAULT_OUT, help="静态 HTML 输出路径")
    ap.add_argument("--serve", action="store_true", help="起本地服务，页面可直接处理灵感")
    ap.add_argument("--port", type=int, default=8765, help="--serve 的起始端口")
    ap.add_argument("--no-browser", action="store_true", help="--serve 时不自动打开浏览器")
    ap.add_argument("--apply", metavar="JSON", help="应用离线模式导出的操作文件")
    ap.add_argument("--open-only", action="store_true", help="只放待处理的灵感")
    ap.add_argument("--include-memo", action="store_true", help="连备忘一起放")
    args = ap.parse_args()

    if args.apply:
        sys.exit(apply_ops(args.apply))

    store.init()
    items = collect(args.open_only, args.include_memo)
    if not items:
        print("（灵感库是空的，没有可放的内容）")
        sys.exit(1)

    doc, stats = build_html(items)
    brief = f"共 {len(items)} 条 · " + " / ".join(
        f"{STATUS_LABEL[k]} {stats.get(k, 0)}" for k in STATUS_ORDER if stats.get(k)
    )

    if args.serve:
        print(brief)
        sys.exit(serve(doc, args.port, not args.no_browser))

    out = os.path.abspath(args.out)
    with open(out, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"已生成：{out}")
    print(f"  {brief}")
    print(f"  体积 {os.path.getsize(out) // 1024} KB（双击打开；想直接在页面上处理用 --serve）")


if __name__ == "__main__":
    main()
