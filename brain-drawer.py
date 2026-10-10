"""把「我要做」和「我的想法」渲染成一个不停往上飘的卡片轮播页，并支持在页面上直接处理。

这个文件是干嘛的：读 `memos.db` 里的**备忘 + 灵感** → 生成一个不停往上飘的气泡轮播页，
点卡片就能处理（备忘完成 / 灵感改状态），**在页面上直接写库**，不用导出任何文件。

🔴 两类内容的分工（这是这个页面存在的理由）：
  - **备忘（`kind='memo'`）= 我要做**：没完成 / 已完成 / 已丢弃 三种，卡片左上角一个勾选框，
    点一下就完成；完成的进「✓ 已完成」列表，随时能取消。也可以直接丢弃。
  - **灵感（`kind='idea'`）= 我的想法**：有 待处理 / 已处理 / 已决议 / 已丢弃 四个状态。
  备忘以前只有群指令、没有界面，处理链路是断的 —— 这个页面把它补上。

用法（推荐 --serve：页面直连库，点一下即刻写库，不需要导出任何文件）：
  python brain-drawer.py --serve               # 起本地服务 + 打开浏览器（桌面入口走的就是这条）
  python brain-drawer.py                       # 只生成静态 HTML → 工作区根 脑内抽屉.html
  python brain-drawer.py --apply 抽屉操作.json  # 应用离线模式导出的操作
  python brain-drawer.py -o out.html
  python brain-drawer.py --open-only           # 不加载归档，只带「没完成的备忘 + 待处理灵感」
  python brain-drawer.py --no-memo             # 不带备忘，只看灵感

视图：
  - **内容流放哪几类由设置决定**（`bubbles_show`，默认「备忘 + 待处理」）。
    顶部「⚙ 设置」里勾选，改的是 `memo-bot/settings.json` ——
    和配置中心网页、机器人「设置」指令写的是同一份。
  - 顶部四个按钮「✓ 已完成 / ✅ 已处理 / 📌 已决议 / 🗑 垃圾桶」，点开在列表里看，
    列表里可以「← 恢复」把某条放回内容流。
  - **有结论的条目会把结论一起显示**（气泡里在正文下方、虚线隔开一段「结论：…」）。
    没有结论就不显示 —— 和以前一样。
  - **每条都能改正文**：浮层里点「✏️ 编辑」、或归档列表行里的「✏️ 编辑」，
    改完**创建时间不变**（回顾时发现写错字 / 有了新想法，随时改）。
  - 完成 ≠ 删除、丢弃 ≠ 删除：记录还在库里，只是换个桶，随时可恢复。
    🔴 唯一会真删的是「已丢弃」列表里的「永久删除」—— 点它会先弹确认框
       （带「以后不再提示」勾选框），确认后**从库里抹掉，找不回来**。

设计：
  - 气泡从底部升起、横向漂移、顶部淡出，循环不停。
  - 一轮内不重复；一轮放完自动重洗（所以每次打开顺序都不一样）。
  - 按桶配色；长文本气泡自动缩小字号，**正文完整显示、不截断**（结论同样不截断）；
    **换行原样保留**（群里分几行写的，气泡与详情里就是几行）。
  - 悬停暂停单个气泡，点击展开浮层（全文 + 处理按钮 + 结论输入）。
  - 数据在生成时**内嵌**进 HTML —— file:// 下 fetch 本地 JSON 会被 CORS 拦，内嵌最稳。

两种处理模式（页面自动探测）：
  - **在线（--serve，默认用法）**：`GET /api/ping` 通 → 点按钮直接 POST 写 memos.db，
    并每 20s 拉一次 `/api/items`（bot 新收的灵感会自动进流）。顶部「⏻ 停止服务」可停。
    设置读写走 `GET|POST /api/settings`；顶部「⬇ 导出」走 `GET /api/export`（浏览器另存为）。
  - **离线（file:// 直接双击 HTML）**：操作暂存 localStorage，顶部显示待提交数，
    可导出 `抽屉操作.json`，再跑 `--apply` 落地 —— 仅作降级（设置也只能看、不能存）。
"""
import argparse
import json
import os
import re
import sys
import threading
import time
from urllib.parse import parse_qs, quote, urlparse

# 🔴 把标准输出 / 错误强制切成 UTF-8 —— 这是「双击打不开页面」的真凶（2026-10-09）。
#    场景：用户双击「脑内抽屉.bat」→ open_drawer.py 用 pythonw 后台拉起本脚本，
#    子进程的 stdout 是**重定向到文件**的。这时 Python 按系统默认编码写
#    （中文 Windows = GBK），而下面 serve() 的启动提示里有 ⏻(U+23FB) ——
#    GBK 没有这个字形 → print 直接抛 UnicodeEncodeError → 异常在
#    serve_forever() **之前**冒出来，服务根本没起来，页面自然打不开。
#    开发机上一直测不出来，因为终端环境是 UTF-8（PYTHONIOENCODING=utf-8）。
#    errors="replace" 兜底：万一还有编不出的字符，打成 ? 而不是让进程挂掉。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import export  # noqa: E402  （导出：和机器人「导出」指令共用同一套排版）
import settings  # noqa: E402  （设置：网页「⚙ 设置」与机器人「设置」指令共用）
import store  # noqa: E402
from paths import data_dir  # noqa: E402  （数据目录：输出产物落哪）

# 🔴 输出的是「用户可见产物」→ 落数据目录（安装版 = 用户目录，见 paths.py）
WORKSPACE = data_dir()
DEFAULT_OUT = os.path.join(WORKSPACE, "脑内抽屉.html")

# ── 桶（bucket）= 页面上的一类卡片 ─────────────────────────────
# 灵感（kind='idea'）的四个处理状态
STATUS_LABEL = {
    "open": "待处理",
    "done": "已处理",
    "decided": "已决议",
    "dropped": "已丢弃",
}
STATUS_ORDER = ("open", "done", "decided", "dropped")
VALID_STATUS = set(STATUS_ORDER)

# 🔴 备忘（kind='memo'）= 我要做，有「没完成 / 已完成 / 已丢弃」三种；
#    灵感 = 我的想法，有上面那四个状态。两条流共用一个页面，所以统一用「桶」来分流：
#    桶名既是 `bubbles_show` 设置里的取值，也直接当卡片的 `data-s`（配色 / 文案都按它查）。
# 🔴 「已丢弃」是两类**共用**的桶 —— 丢弃的备忘和丢弃的灵感都进 `dropped`。
BUCKET_MEMO = "memo"              # 备忘 · 没完成
BUCKET_MEMO_DONE = "memo_done"    # 备忘 · 已完成
BUCKET_LABEL = {
    BUCKET_MEMO: "备忘",
    BUCKET_MEMO_DONE: "备忘 · 已完成",
    "open": "待处理",
    "done": "已处理",
    "decided": "已决议",
    "dropped": "已丢弃",
}
BUCKET_ORDER = (BUCKET_MEMO, BUCKET_MEMO_DONE, "open", "done", "decided", "dropped")


def memo_bucket(status):
    """备忘的状态 → 它的桶名。`dropped` 与灵感共用同一个桶。"""
    if status == store.STATUS_DROPPED:
        return store.STATUS_DROPPED
    return BUCKET_MEMO_DONE if status == store.STATUS_DONE else BUCKET_MEMO


def bucket_of(row):
    """一条库里的记录 → 它在页面上的桶名。"""
    if (row.get("kind") or store.KIND_MEMO) == store.KIND_MEMO:
        return memo_bucket(row.get("status") or store.STATUS_OPEN)
    st = row.get("status") or store.STATUS_OPEN
    return st if st in STATUS_LABEL else store.STATUS_OPEN


def clean(text):
    """清洗一条灵感 / 备忘的文本：去掉 @ 占位符、规整空白，**但保留换行**。

    🔴 以前这里是 `re.sub(r"\\s+", " ", t)` —— `\\s` 连换行一起压成空格，
       于是群里分几行写的内容，到了页面上全挤成一行（2026-10-09 用户发现）。
       `bot.py` 的 `strip_at()` 只压 `[ \\t]+`，本来就保留换行 —— 两边口径要一致。

    现在做四件事（顺序有讲究）：
      1. 统一换行符（`\\r\\n` / `\\r` → `\\n`）；
      2. 只把**空格 / 制表符**压成一个；
      3. 去掉每行首尾多余的空格；
      4. 连续 3 个以上换行压成 1 个空行（群里手抖多敲的空行不至于把气泡撑爆）。
    """
    t = re.sub(r"<[^<>\s]{1,40}>", " ", text or "")
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def collect(open_only=False, include_memo=True):
    """读库 → 按时间正序的条目列表（**备忘 + 灵感**，带 id 供页面回写）。

    🔴 备忘和灵感在这一条流里混排：备忘是「我要做」，灵感是「我的想法」。
       每条带一个桶名 `s`（见 `BUCKET_LABEL`），页面按它分流 / 配色 / 计数。
    """
    rows = []
    if include_memo:
        rows += list(reversed(store.list_memos(500, kind=store.KIND_MEMO)))
    rows += store.list_ideas(None, 500, newest_first=False)
    rows.sort(key=lambda m: (m.get("created_at") or ""))
    items = []
    for m in rows:
        content = clean(m.get("content"))
        if not content:
            continue
        b = bucket_of(m)
        if open_only and b not in (BUCKET_MEMO, store.STATUS_OPEN):
            continue
        items.append({
            "id": m.get("id") or "",
            "t": content,
            "s": b,
            "d": (m.get("created_at") or "")[:16].replace("T", " "),
            "k": m.get("kind") or store.KIND_IDEA,
            "note": m.get("note") or "",
        })
    return items


TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<link rel="icon" href="data:,">
<title>脑内抽屉__BRAND__</title>
<style>
  :root{
    --ink:#e6edf3; --dim:#8b98a9; --line:rgba(255,255,255,.12);
    --open:#4fd1c5; --done:#68d391; --decided:#b794f4; --dropped:#7c8798;
    --memo:#f6ad55; --memo-done:#8b98a9;
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
  .legend .l-memo i{background:var(--memo)}
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
  .bubble[data-s="memo"]{--accent:var(--memo)}
  .bubble[data-s="memo_done"]{--accent:var(--memo-done)}
  .bubble[data-s="open"]{--accent:var(--open)}
  .bubble[data-s="done"]{--accent:var(--done)}
  .bubble[data-s="decided"]{--accent:var(--decided)}
  .bubble[data-s="dropped"]{--accent:var(--dropped)}
  /* 卡头一行：备忘的勾选框 + 状态/时间 */
  .bubble .hd{display:flex; align-items:center; gap:8px; margin-bottom:5px}
  .bubble .meta{
    display:block; font-size:10.5px; letter-spacing:.5px; color:var(--dim);
    margin-bottom:0; text-transform:uppercase;
  }
  /* 🔴 勾选框用 CSS 画，不用 ☐ / ☑ 字符 —— 字符字形在部分系统上会缺（踩过 🫧 的坑）。 */
  .bubble .chk{
    flex:0 0 auto; width:15px; height:15px; border-radius:4px; position:relative;
    border:1.5px solid rgba(255,255,255,.34); background:rgba(255,255,255,.06);
    cursor:pointer; transition:.15s;
  }
  .bubble .chk:hover{border-color:var(--memo); background:rgba(246,173,85,.18)}
  .bubble .chk.on{background:var(--done); border-color:var(--done)}
  .bubble .chk.on::after{
    content:""; position:absolute; left:4px; top:1px; width:4px; height:8px;
    border:solid #0d1a2b; border-width:0 2px 2px 0; transform:rotate(45deg);
  }
  .bubble .txt{word-break:break-word; white-space:pre-wrap}
  /* 结论 / 备注：只有这条**有**结论时才渲染（已处理 / 已决议 / 已丢弃 常见）。
     没有就不渲染，和以前完全一样 —— 气泡高度会因此变高，估算里已算进去（见 noteHeight）。 */
  .bubble .note{
    margin-top:8px; padding-top:7px; border-top:1px dashed var(--line);
    font-size:12px; line-height:1.6; color:#8fa3b8;
    word-break:break-word; white-space:pre-wrap;
  }
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
  #modal .meta{
    display:flex; align-items:center; gap:10px;
    font-size:11.5px; color:var(--dim); letter-spacing:.6px; margin-bottom:12px;
  }
  #modal .meta .mtxt{flex:1 1 auto; min-width:0}
  /* 「✏️ 编辑」入口 —— 改正文用；创建时间不变（这是用户明确要求） */
  #modal .meta .editbtn{
    flex:0 0 auto; padding:4px 10px; border-radius:8px; cursor:pointer;
    background:rgba(255,255,255,.06); border:1px solid var(--line); color:var(--dim);
    font:11.5px/1 inherit; letter-spacing:0; transition:.15s;
  }
  #modal .meta .editbtn:hover{
    color:var(--open); border-color:rgba(79,209,197,.5); background:rgba(79,209,197,.12);
  }
  #modal .body{font-size:16px; line-height:1.8; white-space:pre-wrap; word-break:break-word}
  /* 编辑态：正文换成这个 textarea（.body 同时隐藏） */
  #modal .editbox{
    width:100%; padding:12px 13px; border-radius:12px; min-height:130px; resize:vertical;
    background:rgba(255,255,255,.06); border:1px solid rgba(79,209,197,.45);
    color:var(--ink); font:15px/1.8 inherit; white-space:pre-wrap;
  }
  #modal .editbox:focus{outline:none; border-color:rgba(79,209,197,.72)}
  #modal .editrow{display:flex; align-items:center; gap:9px; margin-top:11px}
  #modal .editrow .ehint{margin-right:auto; font-size:11.5px; color:var(--dim)}
  #modal .editrow .ehint b{color:#68d391; font-weight:600}
  #modal .editrow button{
    padding:9px 15px; border-radius:10px; cursor:pointer; transition:.16s;
    background:rgba(255,255,255,.07); border:1px solid var(--line); color:var(--ink);
    font:13.5px/1 inherit;
  }
  #modal .editrow button:hover{background:rgba(255,255,255,.14)}
  #modal .editrow button.primary{
    background:rgba(79,209,197,.16); border-color:rgba(79,209,197,.5); color:#4fd1c5;
  }
  #modal .editrow button.primary:hover{background:rgba(79,209,197,.28)}
  #modal .editrow button[disabled]{opacity:.5; cursor:default; transform:none}
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
  #modal .actions button[data-memo="drop"]:hover{border-color:var(--dropped); color:var(--dropped)}
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
  /* 归档入口：已处理 / 已决议 / 垃圾桶 */
  .bar .pill.act{cursor:pointer; transition:.16s}
  .bar .pill.act b{font-weight:600; margin-left:5px; color:#cbd5e1}
  .bar .pill.act:hover{background:rgba(255,255,255,.13); color:var(--ink)}
  .bar .pill.act[data-s="done"]:hover{border-color:var(--done); color:var(--done)}
  .bar .pill.act[data-s="decided"]:hover{border-color:var(--decided); color:var(--decided)}
  .bar .pill.act[data-s="dropped"]:hover{border-color:var(--dropped); color:var(--ink)}
  .bar .pill.act[data-s="memo_done"]:hover{border-color:var(--memo); color:var(--memo)}
  .bar .pill.act.zero{opacity:.42}
  .bar .pill.quit{cursor:pointer}
  .bar .pill.quit:hover{color:#fc8181; border-color:rgba(252,129,129,.5)}

  /* 归档列表浮层（已处理 / 已决议 / 垃圾桶） */
  #list{
    position:fixed; inset:0; z-index:45; display:none;
    align-items:center; justify-content:center; padding:26px;
    background:rgba(4,7,12,.74); -webkit-backdrop-filter:blur(4px); backdrop-filter:blur(4px);
  }
  #list.on{display:flex}
  #list .panel{
    width:min(780px,94vw); max-height:80vh; display:flex; flex-direction:column;
    border-radius:20px; overflow:hidden;
    background:rgba(20,28,42,.96); border:1px solid var(--line);
    box-shadow:0 24px 70px rgba(0,0,0,.6);
  }
  #list .lhead{
    display:flex; align-items:center; gap:10px; flex:0 0 auto;
    padding:15px 20px; border-bottom:1px solid var(--line);
  }
  #list .lhead h2{margin:0; font-size:14.5px; font-weight:700}
  #list .lhead .cnt{color:var(--dim); font-size:12px}
  #list .lhead button{
    margin-left:auto; padding:6px 12px; border-radius:9px; cursor:pointer;
    background:rgba(255,255,255,.06); border:1px solid var(--line); color:var(--ink);
    font:13px/1 inherit;
  }
  #list .lhead button:hover{background:rgba(255,255,255,.14)}
  #list .lbody{overflow:auto; padding:10px 12px 14px}
  #list .row{
    display:flex; gap:11px; align-items:flex-start; cursor:pointer;
    padding:11px 13px; border-radius:13px; margin-bottom:6px;
    background:rgba(255,255,255,.035); border:1px solid transparent;
    border-left:3px solid var(--accent,var(--dim));
  }
  #list .row:hover{background:rgba(255,255,255,.08); border-color:var(--line); border-left-color:var(--accent)}
  #list .row[data-s="open"]{--accent:var(--open)}
  #list .row[data-s="done"]{--accent:var(--done)}
  #list .row[data-s="decided"]{--accent:var(--decided)}
  #list .row[data-s="dropped"]{--accent:var(--dropped)}
  #list .row[data-s="memo"]{--accent:var(--memo)}
  #list .row[data-s="memo_done"]{--accent:var(--memo-done)}
  #list .row .chk{
    flex:0 0 auto; margin-top:2px; width:15px; height:15px; border-radius:4px; position:relative;
    border:1.5px solid rgba(255,255,255,.34); background:rgba(255,255,255,.06);
    cursor:pointer; transition:.15s;
  }
  #list .row .chk:hover{border-color:var(--memo); background:rgba(246,173,85,.18)}
  #list .row .chk.on{background:var(--done); border-color:var(--done)}
  #list .row .chk.on::after{
    content:""; position:absolute; left:4px; top:1px; width:4px; height:8px;
    border:solid #0d1a2b; border-width:0 2px 2px 0; transform:rotate(45deg);
  }
  #list .row .c{flex:1 1 auto; min-width:0}
  #list .row .m{font-size:10.5px; color:var(--dim); letter-spacing:.5px; margin-bottom:4px}
  #list .row .x{font-size:13.5px; line-height:1.65; white-space:pre-wrap; word-break:break-word}
  #list .row .n{font-size:12px; color:#8fa3b8; margin-top:6px; white-space:pre-wrap}
  #list .row .ops{
    flex:0 0 auto; display:flex; gap:6px; flex-wrap:wrap; justify-content:flex-end;
  }
  #list .row .ops button{
    padding:6px 10px; border-radius:8px; cursor:pointer; white-space:nowrap;
    background:rgba(255,255,255,.06); border:1px solid var(--line); color:var(--dim);
    font:11.5px/1 inherit; transition:.15s;
  }
  #list .row .ops button:hover{color:var(--open); border-color:rgba(79,209,197,.5); background:rgba(79,209,197,.12)}
  #list .row .ops button[disabled]{opacity:.5; cursor:default}
  /* 「已丢弃」列表里的永久删除 —— 唯一会真删数据的按钮，红色单独标出来 */
  #list .row .ops button.danger{color:#fc8181; border-color:rgba(252,129,129,.4)}
  #list .row .ops button.danger:hover{color:#fc8181; border-color:rgba(252,129,129,.6); background:rgba(252,129,129,.14)}
  #list .row .ops button.danger[disabled]{color:var(--dim); border-color:var(--line)}
  #list .empty{padding:36px 10px; text-align:center; color:var(--dim); font-size:13.5px}

  /* 永久删除确认浮层 —— z-index 必须高过 #list(45) / #settings(46)，它是从列表里弹出来的 */
  #confirm{
    position:fixed; inset:0; z-index:48; display:none;
    align-items:center; justify-content:center; padding:26px;
    background:rgba(4,7,12,.76); -webkit-backdrop-filter:blur(4px); backdrop-filter:blur(4px);
  }
  #confirm.on{display:flex}
  #confirm .panel{
    width:min(460px,94vw); border-radius:18px; overflow:hidden;
    background:rgba(20,28,42,.97); border:1px solid var(--line);
    box-shadow:0 24px 70px rgba(0,0,0,.6);
  }
  #confirm .chead{padding:16px 20px 0}
  #confirm .chead h2{margin:0; font-size:15px; font-weight:700; color:#fc8181}
  #confirm .cbody{padding:9px 20px 0; color:var(--dim); font-size:13px; line-height:1.7}
  #confirm .cbody .q{
    color:var(--ink); font-size:13px; line-height:1.65; margin-top:9px; padding:9px 11px;
    max-height:170px; overflow:auto; border-radius:10px;
    background:rgba(255,255,255,.05); border:1px solid var(--line);
    white-space:pre-wrap; word-break:break-word;
  }
  #confirm .noask{
    display:flex; align-items:center; gap:8px; margin:14px 20px 0;
    font-size:12.5px; color:var(--dim); cursor:pointer; user-select:none;
  }
  #confirm .noask input{width:14px; height:14px; margin:0; cursor:pointer; accent-color:#fc8181}
  #confirm .noask:hover{color:var(--ink)}
  #confirm .cfoot{display:flex; gap:9px; justify-content:flex-end; padding:16px 20px 18px}
  #confirm .cfoot button{
    padding:9px 16px; border-radius:10px; cursor:pointer; transition:.16s;
    background:rgba(255,255,255,.07); border:1px solid var(--line); color:var(--ink);
    font:13.5px/1 inherit;
  }
  #confirm .cfoot button:hover{background:rgba(255,255,255,.14)}
  #confirm .cfoot button.danger{
    background:rgba(252,129,129,.16); border-color:rgba(252,129,129,.5); color:#fc8181;
  }
  #confirm .cfoot button.danger:hover{background:rgba(252,129,129,.27)}

  /* 设置浮层 —— 改的是 memo-bot/settings.json，与机器人「设置」指令同一份 */
  #settings{
    position:fixed; inset:0; z-index:46; display:none;
    align-items:center; justify-content:center; padding:26px;
    background:rgba(4,7,12,.74); -webkit-backdrop-filter:blur(4px); backdrop-filter:blur(4px);
  }
  #settings.on{display:flex}
  #settings .panel{
    width:min(640px,94vw); max-height:82vh; display:flex; flex-direction:column;
    border-radius:20px; overflow:hidden;
    background:rgba(20,28,42,.96); border:1px solid var(--line);
    box-shadow:0 24px 70px rgba(0,0,0,.6);
  }
  #settings .shead{
    display:flex; align-items:center; gap:10px; flex:0 0 auto;
    padding:15px 20px; border-bottom:1px solid var(--line);
  }
  #settings .shead h2{margin:0; font-size:14.5px; font-weight:700}
  #settings .shead .sub{color:var(--dim); font-size:11.5px}
  #settings .shead button{
    margin-left:auto; padding:6px 12px; border-radius:9px; cursor:pointer;
    background:rgba(255,255,255,.06); border:1px solid var(--line); color:var(--ink);
    font:13px/1 inherit;
  }
  #settings .shead button:hover{background:rgba(255,255,255,.14)}
  #settings .sbody{overflow:auto; padding:6px 20px 10px}
  #settings .shint{
    flex:0 0 auto; padding:10px 20px; font-size:11.5px; color:var(--dim); line-height:1.65;
    border-top:1px solid var(--line); background:rgba(255,255,255,.03);
  }
  #settings .shint code{
    background:rgba(255,255,255,.1); padding:1px 5px; border-radius:5px; font-size:11px;
  }
  #settings .item{padding:14px 0; border-bottom:1px solid rgba(255,255,255,.07)}
  #settings .item:last-child{border-bottom:none}
  #settings .item .lb{font-size:13.5px; font-weight:600; margin-bottom:3px}
  #settings .item .hp{font-size:11.5px; color:var(--dim); margin-bottom:9px}
  #settings .item input[type=text]{
    width:100%; box-sizing:border-box; padding:9px 11px; border-radius:10px;
    background:rgba(255,255,255,.06); border:1px solid var(--line); color:var(--ink);
    font:13.5px/1.4 inherit;
  }
  #settings .item input[type=text]:focus{outline:none; border-color:rgba(79,209,197,.55)}
  #settings .item .chks{display:flex; gap:16px; flex-wrap:wrap}
  #settings .item .chks label{display:flex; align-items:center; gap:6px; font-size:13px; cursor:pointer}
  #settings .item .chks input{accent-color:#4fd1c5; width:15px; height:15px}
  #settings .item .rst{
    margin-top:9px; padding:5px 10px; border-radius:8px; cursor:pointer;
    background:transparent; border:1px solid var(--line); color:var(--dim); font:11.5px/1 inherit;
  }
  #settings .item .rst:hover{color:var(--ink); background:rgba(255,255,255,.08)}
  #settings .sfoot{
    display:flex; align-items:center; gap:10px; flex:0 0 auto;
    padding:13px 20px; border-top:1px solid var(--line);
  }
  #settings .sfoot .msg{margin-left:auto; font-size:12px; color:var(--dim)}
  #settings .sfoot button{
    padding:8px 18px; border-radius:10px; cursor:pointer; font:13px/1 inherit;
    background:rgba(79,209,197,.16); border:1px solid rgba(79,209,197,.5); color:#4fd1c5;
  }
  #settings .sfoot button:hover{background:rgba(79,209,197,.28)}

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
    <h1>🗂 脑内抽屉<span>__BRAND__</span></h1>
    <div class="legend">
      <span class="l-memo"><i></i>备忘</span>
      <span class="l-open"><i></i>待处理</span>
      <span class="l-done"><i></i>已处理</span>
      <span class="l-decided"><i></i>已决议</span>
      <span class="l-dropped"><i></i>已丢弃</span>
    </div>
  </header>
  <div class="bar">
    <span class="pill" id="mode">检测中…</span>
    <span class="pill pending" id="pending" style="display:none"></span>
    <span class="pill act" id="btn-memo_done" data-s="memo_done">✓ 已完成<b>0</b></span>
    <span class="pill act" id="btn-done" data-s="done">✅ 已处理<b>0</b></span>
    <span class="pill act" id="btn-decided" data-s="decided">📌 已决议<b>0</b></span>
    <span class="pill act" id="btn-dropped" data-s="dropped">🗑 垃圾桶<b>0</b></span>
    <span class="pill act" id="btn-settings" title="内容流放哪几类">⚙ 设置</span>
    <span class="pill act" id="btn-export" style="display:none"
          title="把全部备忘 + 灵感导出成一个文件；想导 json / txt 就在群里发「导出 json」">⬇ 导出</span>
    <span class="pill quit" id="btn-quit" style="display:none">⏻ 停止服务</span>
  </div>
</div>

<div id="stage"></div>

<div id="empty"><div class="big">🎉</div><div>抽屉里没有要处理的了</div></div>

<div id="modal"><div class="card">
  <div class="meta"><span class="mtxt"></span><button class="editbtn" id="btn-edit"
    title="改正文；创建时间不变">✏️ 编辑</button></div>
  <div class="body"></div>
  <textarea class="editbox" id="editbox" rows="6" style="display:none"
    placeholder="改这一条的正文…"></textarea>
  <div class="editrow" id="editrow" style="display:none">
    <span class="ehint">只改正文 · <b>创建时间不变</b> · Esc 取消</span>
    <button id="ecancel">取消</button>
    <button id="esave" class="primary">保存</button>
  </div>
  <textarea class="note" id="note" rows="2" placeholder="结论 / 备注（可选）"></textarea>
  <div class="actions ideaact">
    <button data-act="done">✅ 已处理</button>
    <button data-act="decided">📌 已决议</button>
    <button data-act="dropped">🗑 丢弃</button>
    <button data-act="open">← 恢复待处理</button>
  </div>
  <div class="actions memoact" style="display:none">
    <button data-memo="done">✓ 完成这条备忘</button>
    <button data-memo="drop">🗑 丢弃</button>
    <button data-memo="open">← 恢复</button>
  </div>
  <div class="hint">处理后会从轮播移出 · 点卡片外关闭 · 空格暂停/继续</div>
</div></div>

<div id="list"><div class="panel">
  <div class="lhead">
    <h2 id="ltitle"></h2><span class="cnt" id="lcnt"></span>
    <button id="lclose">✕ 关闭</button>
  </div>
  <div class="lbody" id="lbody"></div>
</div></div>

<div id="confirm"><div class="panel">
  <div class="chead"><h2 id="ctitle">永久删除？</h2></div>
  <div class="cbody">
    删掉之后<b>从库里抹掉，找不回来</b> —— 不弹提示、不建回收站，也没法恢复。
    <div class="q" id="cquote"></div>
  </div>
  <label class="noask"><input type="checkbox" id="cnoask">以后不再提示（下次点「永久删除」直接删）</label>
  <div class="cfoot">
    <button id="ccancel">取消</button>
    <button id="cok" class="danger">永久删除</button>
  </div>
</div></div>

<div id="settings"><div class="panel">
  <div class="shead">
    <h2>⚙ 设置</h2><span class="sub">与配置中心、机器人「设置」指令改的是同一份</span>
    <button id="sclose">✕ 关闭</button>
  </div>
  <div class="sbody" id="sbody"></div>
  <div class="shint">凭据 / 触发词 / 搜索目录 / 开机自启 / 播报文案 → 在「配置中心」里改
    （<code>python config-web.py</code>）</div>
  <div class="sfoot">
    <button id="ssave">保存</button>
    <span class="msg" id="smsg"></span>
  </div>
</div></div>

<div id="toast"></div>

<footer>
  <span>显示 <b id="n">0</b> 条 · 共 <b id="n-arch">0</b> 条</span>
  <span>生成于 <b id="gen"></b></span>
  <span id="state">空格暂停 / 继续</span>
</footer>

<script>
let IDEAS = __DATA__;          // 全量（含归档）；轮播只取 open
const LABEL = __LABEL__;
const GEN = __GEN__;

const stage = document.getElementById('stage');
const modal = document.getElementById('modal');
const listEl = document.getElementById('list');
const stateEl = document.getElementById('state');
const modeEl = document.getElementById('mode');
const pendEl = document.getElementById('pending');
const toastEl = document.getElementById('toast');
const noteEl = document.getElementById('note');
const editboxEl = document.getElementById('editbox');
const editrowEl = document.getElementById('editrow');
const btnEdit = document.getElementById('btn-edit');
const emptyEl = document.getElementById('empty');
const nEl = document.getElementById('n');
const nArchEl = document.getElementById('n-arch');

document.getElementById('gen').textContent = GEN;

/* ── 分流 ────────────────────────────────────────────────────
   🔴 两类内容：备忘（`k='memo'`，我要做，没完成/已完成/已丢弃）和灵感（`k='idea'`，我的想法）。
      每条带一个**桶名** `s`，桶名进 `bubbles_show` 设置、也当卡片的 `data-s`。
      内容流放哪几个桶由**设置**决定（默认「备忘 + 待处理」）；
      其余桶各有归档列表（点顶部按钮看）。
      🔴 `dropped` 是两类**共用**的桶 —— 丢弃的备忘和丢弃的灵感都进这里。
      展示范围在页面上改：顶部「⚙ 设置」，和机器人「设置」指令是同一份。 */
const ARCHIVE = ['memo_done', 'done', 'decided', 'dropped'];
const ARCHIVE_TITLE = {
  memo_done: '✓ 已完成的备忘', done: '✅ 已处理',
  decided: '📌 已决议', dropped: '🗑 垃圾桶',
};
const ARCHIVE_EMPTY = {
  memo_done: '还没有完成过备忘', done: '还没有处理完的灵感',
  decided: '还没有下过结论的灵感', dropped: '垃圾桶是空的',
};

let SETTINGS = __SETTINGS__;
function settingValue(key){
  const s = SETTINGS.find(x => x.key === key);
  return s ? s.value : null;
}
let SHOW = settingValue('bubbles_show') || ['memo', 'open'];

function streamItems(){ return IDEAS.filter(x => SHOW.includes(x.s)); }
function countBy(s){ return IDEAS.filter(x => x.s === s).length; }

function updateCounts(){
  nEl.textContent = streamItems().length;
  nArchEl.textContent = IDEAS.length;
  for(const s of ARCHIVE){
    const el = document.getElementById('btn-' + s);
    if(!el) continue;
    const n = countBy(s);
    el.querySelector('b').textContent = n;
    el.classList.toggle('zero', n === 0);
  }
}
updateCounts();

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

/* 只读接口统一走这里 —— 🔴 `fetch` 默认**没有超时**。
   本地服务还没起完 / 端口被别的进程占着 / 杀软拦连接时，Promise 会永不 settle，
   页面就一直等下去。首屏那两跳尤其致命：等不到 = 一个气泡都不铺
   （真机复现：把 `/api/ping` 延迟 3s → 整整 3 秒气泡总数为 0）。
   🔴 只给**只读 GET** 加超时；写操作（POST）不加 —— 中途 abort 掉的话，
      服务端可能照样写进去了，状态反而说不清。 */
const API_TIMEOUT_MS = 4000;
function apiGet(url){
  const opt = {cache:'no-store'};
  if(typeof AbortSignal !== 'undefined' && AbortSignal.timeout){
    opt.signal = AbortSignal.timeout(API_TIMEOUT_MS);
  } else if(typeof AbortController !== 'undefined'){
    const ac = new AbortController();
    setTimeout(()=>ac.abort(), API_TIMEOUT_MS);
    opt.signal = ac.signal;
  }
  return fetch(url, opt);
}

async function detectMode(){
  // 🔴 file:// 下直接判定离线 —— 否则 apiGet('/api/ping') 会被 CORS 拦，
  //    功能上没问题（catch 住），但会在 console 里留两条红字，看着像坏了
  if(location.protocol !== 'file:'){
    try {
      const r = await apiGet('/api/ping');
      const j = await r.json();
      if(j && j.ok) MODE = 'serve';
    } catch(e){ MODE = 'offline'; }
  }
  if(MODE === 'serve'){
    modeEl.className = 'pill live';
    modeEl.textContent = '● 已连本地库 · 处理即刻写库';
    document.getElementById('btn-quit').style.display = '';
    document.getElementById('btn-export').style.display = '';
    await refreshSettings();                    // 展示范围以服务端为准（可能被指令改过）
    updateCounts();
    setInterval(refreshFromServer, 20000);      // 实时：bot 新收的灵感自动进流
  } else {
    modeEl.className = 'pill offline';
    modeEl.textContent = '○ 离线模式 · 操作暂存，可导出';
  }
  renderPending();
}

/* 把库里的最新状态拉回来（新增的补进来，已删的移除）。
   🔴 浮层开着时不刷新 —— 否则用户正在读/点的时候列表会跳。 */
async function refreshFromServer(){
  if(MODE !== 'serve') return;
  if(modal.classList.contains('on') || listEl.classList.contains('on')
     || settingsEl.classList.contains('on')) return;
  try{
    const r = await apiGet('/api/items');
    const j = await r.json();
    if(!j || !j.ok) return;
    const incoming = j.items || [];
    const byId = new Map(incoming.map(x => [x.id, x]));
    for(const it of IDEAS){
      const s = byId.get(it.id);
      // `t` 也要跟 —— 别的窗口（或别的机器）编辑过正文，这里得看到新文本
      if(s){ it.s = s.s; if(s.note) it.note = s.note; if(s.t) it.t = s.t; }
    }
    const known = new Set(IDEAS.map(x => x.id));
    for(const x of incoming){ if(!known.has(x.id)) IDEAS.push(x); }
    IDEAS = IDEAS.filter(x => byId.has(x.id));
    await refreshSettings();       // 群里「设置」指令改了展示范围，页面也跟上
    updateCounts();
  }catch(e){}
}

// 🔴 改状态 ≠ 删记录：只把它从内容流挪到对应归档列表（丢弃也一样，进垃圾桶而不是消失）
function moveItem(id, bucket){
  const it = IDEAS.find(x=>x.id === id);
  if(it) it.s = bucket;
  queue = queue.filter(x=>x.id !== id);
  stage.querySelectorAll('.bubble').forEach(el=>{ if(el.dataset.id === id) el.remove(); });
  updateCounts();
  emptyEl.classList.toggle('on', streamItems().length === 0);
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
    // 🔴 用 Object.assign 保留已有字段 —— 同一条可能先编辑过正文（`e`），再改状态
    pending[it.id] = Object.assign({}, pending[it.id], {s: status, note: note || '', t: Date.now()});
    lsSet(PENDING_KEY, pending);
    renderPending();
  }
  if(note) it.note = note;
  moveItem(it.id, status);
  toast('已标记「' + (LABEL[status]||status) + '」'
        + (MODE === 'serve' ? '' : ' · 暂存中，记得导出'));
}

/* ── 备忘：没完成 / 已完成 / 已丢弃 三态 ────────────────────
   备忘 = 我要做，不需要「已决议」那一套；完成就进「✓ 已完成」列表，不做就丢进垃圾桶。
   🔴 完成 ≠ 删除、丢弃 ≠ 删除：记录还在库里（status='done' / 'dropped'），随时能取消 / 恢复。
   🔴 桶名映射走 memoBucket()：done → 'memo_done'，open → 'memo'，dropped → 'dropped'（与灵感共用）。 */
function memoBucket(status){
  if(status === 'dropped') return 'dropped';
  return status === 'done' ? 'memo_done' : 'memo';
}

async function applyMemoStatus(it, status){
  if(MODE === 'serve'){
    const r = await fetch('/api/memo', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id: it.id, status: status}),
    });
    const j = await r.json();
    if(!j.ok) throw new Error(j.error || '写入失败');
  } else {
    // 🔴 同上：保留 `e`（编辑过的正文），别一改状态就把编辑丢了
    pending[it.id] = Object.assign({}, pending[it.id], {k:'memo', status: status, t: Date.now()});
    lsSet(PENDING_KEY, pending);
    renderPending();
  }
  moveItem(it.id, memoBucket(status));
  const said = {done:'已完成 ✓', open:'已放回备忘', dropped:'已丢弃 🗑'}[status] || status;
  toast(said + (MODE === 'serve' ? '' : ' · 暂存中，记得导出'));
}

// 勾选框那一条路走的就是「完成 / 取消完成」
async function applyMemoDone(it, done){
  return applyMemoStatus(it, done ? 'done' : 'open');
}

/* 卡片上的勾选框（CSS 画的方框，勾上用对勾伪元素）。
   `after` 是勾完之后要做的事 —— 气泡上不用传，列表里要重渲染一遍。 */
function makeChk(it, after){
  const cb = document.createElement('span');
  cb.className = 'chk' + (it.s === 'memo_done' ? ' on' : '');
  cb.title = it.s === 'memo_done' ? '已完成 —— 点一下取消' : '点一下标记完成';
  cb.addEventListener('click', async (e)=>{
    e.stopPropagation();                 // 别顺手把详情浮层也打开了
    cb.style.pointerEvents = 'none';
    try { await applyMemoDone(it, it.s !== 'memo_done'); if(after) after(); }
    catch(err){ toast('写入失败：' + err.message); }
    finally { cb.style.pointerEvents = ''; }
  });
  return cb;
}

pendEl.addEventListener('click', ()=>{
  // 备忘的暂存记的是「状态（open/done/dropped）」，灵感的记的是「状态 + 结论」，
  // ✏️ 编辑单独记在 `e` 上 —— 三者可以叠在同一条上，导出时合成一个 op。
  const ops = Object.entries(pending).map(([id, v]) => {
    const op = {id: id};
    if(v.k === 'memo'){ op.kind = 'memo'; op.status = v.status; }
    else if(v.s){ op.status = v.s; op.note = v.note || ''; }
    if(v.e != null) op.content = v.e;      // 编辑过的正文
    return op;
  });
  if(!ops.length) return;
  const blob = new Blob([JSON.stringify({ops: ops}, null, 2)], {type:'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = '抽屉操作.json';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(()=>URL.revokeObjectURL(a.href), 1500);
  toast('已导出 ' + ops.length + ' 条 —— 再跑：python memo-bot/brain-drawer.py --apply 抽屉操作.json');
});

/* ── 气泡 ───────────────────────────────────────────────── */
function shuffle(a){
  for(let i=a.length-1;i>0;i--){const j=Math.floor(Math.random()*(i+1));[a[i],a[j]]=[a[j],a[i]];}
  return a;
}
function refill(){ queue = shuffle(streamItems().slice()); }
// 🔴 从队尾取 —— 所以「放回」必须 unshift 到队首；用 push 会把它又放回队尾，
//    下一次 pick 立刻原样取回，挑 16 次等于挑 1 次（真机实测：窄屏只剩 1 个气泡）。
// 🔴 **必须跳过「已经在屏幕上的」条目**：条目数少于 maxB 时（灵感库常常就几条），
//    队列会被 refill 循环复用，同一条灵感同时出现 2~3 个气泡（用户截图：同一条 19:10 出现两次）。
//    全部条目都在屏幕上时返回 null，让调用方这次不补位。
function onStageIds(){
  const s = new Set();
  for(const el of stage.children) s.add(el.dataset.id);
  return s;
}
function pick(){
  if(!queue.length) refill();
  const on = onStageIds();
  while(queue.length){
    const it = queue.pop();
    if(!on.has(it.id)) return it;
  }
  return null;
}
function unPick(it){ if(it) queue.unshift(it); }

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
const NOTE_FONT = 12;     // 结论的字号（跟 .bubble .note 保持一致）
const NOTE_BOX = 22;      // 分隔线 + margin/padding，粗算
function fontSize(n){ return n<=14?21 : n<=45?16 : n<=110?14 : 12.5; }
// 文本按宽度折算成**行数**。
// 🔴 必须逐段算：内容里的显式换行（`\n`）各自成行，每行再按宽度折行 ——
//    否则「5 行短句」会被当成「5 个字」，气泡高度估矮、长文本判据也判错。
function wrappedLines(text, charsPerLine){
  const per = Math.max(6, charsPerLine);
  let n = 0;
  for(const seg of String(text == null ? '' : text).split('\n')){
    n += Math.max(1, Math.ceil(seg.length / per));
  }
  return n;
}
// 结论占的高度 —— 没有结论就是 0（行为与以前完全一致）
function noteHeight(note, widthPx){
  if(!note) return 0;
  return NOTE_BOX + wrappedLines(note, widthPx / NOTE_FONT) * NOTE_FONT * 1.6;
}
function estHeight(text, widthPx, note){
  const fp = fontSize(String(text == null ? '' : text).length);
  const lines = wrappedLines(text, widthPx / fp);
  return lines * fp * 1.7 + BUB_PAD + noteHeight(note, widthPx);
}
function isLongText(it){
  const cfg = layout();
  const laneW = (cfg.w - cfg.gap) / 100 * innerWidth - BUB_PAD;
  const fullW = (100 - 2 * cfg.gap) / 100 * innerWidth - BUB_PAD;
  const hLane = estHeight(it.t, laneW, it.note);
  const budget = innerHeight * 1.26 / cfg.maxB * 0.9;   // 竖直间距留 10% 余量
  // 只有「泳道里装不下」**且**「横过来能明显变矮（至少省 25%）」才跨泳道，
  // 否则为了跨而跨，反而把整条舞台的错位节奏打乱
  return hLane > budget && estHeight(it.t, fullW, it.note) < hLane * 0.75;
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

// 开局铺满：按「实际能铺几条」均分飞行进度。
// 🔴 不能按 maxB 分 —— 灵感库常常只有 3~5 条，条目比 maxB 少时
//    前几条会全挤在底部一小段里（i/maxB 只有 0、0.125、0.25）。
// 🔴 进度**不能从 0 开始**（`i/cnt` 的老写法）：`warm=0` → `animationDelay=0`
//    → 气泡老老实实从 `bottom:-10vh` 往上爬。而 `floatUp` 的前 ~9% 全在视口外
//    （起点在屏下 + 缓动前段慢），实测**要 2.3 秒才露头**。
//    条目只有 1 条时（cnt=1）就是「打开抽屉 → 整屏空白 2.3 秒」。
//    改成 `(i+1)/(cnt+1)`：第一条也落在飞行途中，cnt=1 时正好在行程中点（屏幕中央）。
//    ⇒ 开屏**立刻**看得到气泡，也才符合本函数「把气泡直接放到飞行途中」的原意。
function prewarm(){
  const cfg = layout();
  const cnt = Math.min(cfg.maxB, streamItems().length);
  for(let i = 0; i < cnt; i++) spawn((i + 1) / (cnt + 1));
  lastSpawn = Date.now();
}

function spawn(warm){
  if(!running || !streamItems().length) return;
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
  if(!it) return;                                   // 该显示的条目全在屏幕上了 → 不补位
  const avoidLong = (cfg.narrow && hasLongOnStage()) || warm > 0;
  if(avoidLong){
    const tries = Math.min(16, streamItems().length);
    for(let t = 0; t < tries && isLongText(it); t++){
      unPick(it); it = pick();
      if(!it) return;                               // 剩下的也都在屏幕上
    }
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

  const hd = document.createElement('div');
  hd.className = 'hd';
  if(it.k === 'memo') hd.append(makeChk(it));   // 🔴 备忘卡片左上角一个勾选框（完成没完成一眼看到）
  const meta = document.createElement('span');
  meta.className = 'meta';
  meta.textContent = (LABEL[it.s]||'') + ' · ' + it.d;
  hd.append(meta);
  const txt = document.createElement('div');
  txt.className = 'txt';
  txt.textContent = it.t;              // 🔴 全文不截断，且原样保留换行（容器是 pre-wrap）
  el.append(hd, txt);
  // 🔴 有结论就把结论一起显示（已处理 / 已决议 / 已丢弃 常见）；
  //    没有结论**不渲染**这个节点 —— 与以前完全一致。
  if(it.note){
    const nt = document.createElement('div');
    nt.className = 'note';
    nt.textContent = '结论：' + it.note;
    el.append(nt);
  }
  el.addEventListener('click', (e)=>{ e.stopPropagation(); openModal(it); });
  stage.appendChild(el);
  setTimeout(()=>el.remove(), (dur + delay)*1000 + 400);   // delay 为负 → 剩余寿命更短
}

/* ── 浮层 ───────────────────────────────────────────────── */
function openModal(it){
  current = it;
  modal.querySelector('.meta .mtxt').textContent = (LABEL[it.s]||'') + ' · ' + it.d;
  modal.querySelector('.body').textContent = it.t;
  const isMemo = it.k === 'memo';
  // 备忘没有「结论」这个概念 —— 换一套操作区（完成 / 丢弃 / 恢复）
  noteEl.value = it.note || '';
  setEditMode(false);            // 复位编辑态（正文 / 结论框 / 两个操作区的显隐都在里面）
  if(isMemo) syncMemoActions(it);
  modal.classList.add('on');
}

/* 备忘浮层的三个按钮按当前状态显隐：
   没完成 → 「完成」+「丢弃」；已完成 → 「取消完成」+「丢弃」；已丢弃 → 「恢复」。 */
function syncMemoActions(it){
  const doneB = modal.querySelector('.memoact [data-memo="done"]');
  const dropB = modal.querySelector('.memoact [data-memo="drop"]');
  const openB = modal.querySelector('.memoact [data-memo="open"]');
  const dropped = it.s === 'dropped';
  doneB.style.display = dropped ? 'none' : '';
  doneB.textContent = it.s === 'memo_done' ? '← 取消完成' : '✓ 完成这条备忘';
  dropB.style.display = dropped ? 'none' : '';
  openB.style.display = dropped ? '' : 'none';
}

// 只有点「卡片外」才关 —— 否则点输入框也会误关
modal.addEventListener('click', (e)=>{ if(e.target === modal) modal.classList.remove('on'); });

modal.querySelectorAll('.ideaact button').forEach(btn=>{
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

// 备忘：浮层里那三个按钮（完成 / 取消完成 / 丢弃 / 恢复）
modal.querySelectorAll('.memoact button').forEach(btn=>{
  btn.addEventListener('click', async (e)=>{
    e.stopPropagation();
    if(!current) return;
    const act = btn.dataset.memo;                    // done / drop / open
    // 「完成」按钮是切换语义：已完成时它显示「← 取消完成」，点它就是放回备忘
    const status = act === 'done' ? (current.s === 'memo_done' ? 'open' : 'done')
                 : act === 'drop' ? 'dropped'
                 : 'open';
    btn.disabled = true;
    try {
      await applyMemoStatus(current, status);
      modal.classList.remove('on');
      current = null;
    } catch(err){
      toast('写入失败：' + err.message);
    } finally {
      btn.disabled = false;
    }
  });
});

/* ── ✏️ 再次编辑正文 ───────────────────────────────────────
   回顾的时候发现写错了字、或者对某件事有了新想法 —— 改正文就行。
   🔴 **创建时间不变**（用户明确要求）：改的是 `content`，`created_at` 一个字节都不动，
      所以时间轴不会乱、排序也不会跳。备忘和灵感都能改。
   入口有两个：浮层里 .meta 右边那个「✏️ 编辑」，以及归档列表行里的「✏️ 编辑」。 */
let editing = false;

/* 进 / 出编辑态。编辑态下：正文换成 textarea，结论框和两个操作区都收起来
   （一次只做一件事，免得「改到一半顺手点了完成」）。 */
function setEditMode(on){
  editing = on;
  const it = current;
  if(!it) return;
  const isMemo = it.k === 'memo';
  modal.querySelector('.body').style.display = on ? 'none' : '';
  editboxEl.style.display = on ? '' : 'none';
  editrowEl.style.display = on ? '' : 'none';
  btnEdit.style.display = on ? 'none' : '';
  noteEl.style.display = (on || isMemo) ? 'none' : '';
  modal.querySelector('.ideaact').style.display = (on || isMemo) ? 'none' : '';
  modal.querySelector('.memoact').style.display = (on || !isMemo) ? 'none' : '';
  // 底部提示在编辑态下是错的（「空格暂停/继续」这时候是打字）—— 直接收起来
  modal.querySelector('.hint').style.display = on ? 'none' : '';
  if(on){
    editboxEl.value = it.t;
    editboxEl.focus();
    editboxEl.setSelectionRange(editboxEl.value.length, editboxEl.value.length);
  }
}

btnEdit.addEventListener('click', ()=> setEditMode(true));
document.getElementById('ecancel').addEventListener('click', ()=> setEditMode(false));
document.getElementById('esave').addEventListener('click', saveEdit);

async function saveEdit(){
  if(!current) return;
  const text = editboxEl.value.trim();
  if(!text){ toast('正文不能改成空的'); return; }
  if(text === current.t){ toast('没有改动'); setEditMode(false); return; }
  const btn = document.getElementById('esave');
  btn.disabled = true;
  try {
    await applyEdit(current, text);
    setEditMode(false);
  } catch(err){
    toast('保存失败：' + err.message);
  } finally {
    btn.disabled = false;
  }
}

/* 写库 / 暂存。和改状态一样分在线、离线两条路 —— 但**编辑是可反复执行的**，
   所以离线那条也进 `--apply`（改错了再改回来就行，不像永久删除那样不可逆）。 */
async function applyEdit(it, text){
  if(MODE === 'serve'){
    const r = await fetch('/api/edit', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id: it.id, content: text}),
    });
    const j = await r.json();
    if(!j.ok) throw new Error(j.error || '保存失败');
  } else {
    // 🔴 只在已有暂存上**加字段**，别整个覆盖 —— 同一条可能刚被标过完成还没导出
    pending[it.id] = pending[it.id] || {t: Date.now()};
    pending[it.id].e = text;
    pending[it.id].t = Date.now();
    lsSet(PENDING_KEY, pending);
    renderPending();
  }
  it.t = text;
  paintText(it.id, text);
  if(listEl.classList.contains('on')) renderList();
  modal.querySelector('.body').textContent = text;
  toast('正文已更新 · 创建时间没变' + (MODE === 'serve' ? '' : '（暂存中，记得导出）'));
}

/* 舞台上那条气泡就地换正文（字号档位跟着重算）。
   气泡是生成时就定死的 DOM 快照 —— 不就地改的话得等它飘走再重来，
   用户会以为「改了没生效」。 */
function paintText(id, text){
  for(const el of stage.children){
    if(el.dataset.id !== id) continue;
    const txt = el.querySelector('.txt');
    if(txt) txt.textContent = text;
    el.className = 'bubble ' + sizeClass(text.length);
  }
}

/* ── 永久删除：确认浮层 + 「以后不再提示」 ─────────────────
   🔴 这是页面上**唯一会真删数据**的操作。默认每次都确认；勾了「以后不再提示」
      之后就直接删。这个偏好存在**浏览器本地**（localStorage），不进 settings.json ——
      它是「这台机器上这个浏览器」的习惯，不是机器人的配置。
      想让它重新弹出来：顶部「⚙ 设置」里的「永久删除前再确认」勾回去。 */
const NOASK_KEY = 'bubble_del_noask_v1';
let noAskDelete = lsGet(NOASK_KEY, false);

const confirmEl = document.getElementById('confirm');
const cquoteEl = document.getElementById('cquote');
const cnoaskEl = document.getElementById('cnoask');
let confirmResolve = null;

function closeConfirm(ok){
  confirmEl.classList.remove('on');
  const r = confirmResolve;
  confirmResolve = null;
  if(r) r(ok);
}

function askDelete(text){
  if(noAskDelete) return Promise.resolve(true);
  cquoteEl.textContent = text;
  cnoaskEl.checked = false;             // 每次都从「没勾」开始，免得手滑一路删下去
  confirmEl.classList.add('on');
  return new Promise(resolve => { confirmResolve = resolve; });
}

document.getElementById('cok').addEventListener('click', ()=>{
  if(cnoaskEl.checked){ noAskDelete = true; lsSet(NOASK_KEY, true); }
  closeConfirm(true);
});
document.getElementById('ccancel').addEventListener('click', ()=> closeConfirm(false));
confirmEl.addEventListener('click', (e)=>{ if(e.target === confirmEl) closeConfirm(false); });

/* 永久删除一条 —— 只给「已丢弃」列表里的按钮用。
   🔴 离线模式（file://）不给删：这一下**不可恢复**，不该跟着「导出操作文件 → --apply」
      那条链路走（文件可以反复应用，误删没法挽回）。 */
async function deleteItem(it){
  if(MODE !== 'serve'){
    toast('离线模式删不了 —— 用桌面「脑内抽屉」打开再删');
    return;
  }
  if(!await askDelete(it.t)) return;
  const r = await fetch('/api/delete', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({id: it.id}),
  });
  const j = await r.json();
  if(!j.ok) throw new Error(j.error || '删除失败');
  IDEAS = IDEAS.filter(x => x.id !== it.id);
  updateCounts();
  renderList();
  toast('已永久删除');
}

/* ── 归档列表：已完成的备忘 / 已处理 / 已决议 / 垃圾桶 ────── */
let listStatus = null;

function makeRow(it){
  const row = document.createElement('div');
  row.className = 'row';
  row.dataset.s = it.s;
  const isMemo = it.k === 'memo';
  const dropped = it.s === 'dropped';
  // 列表里也直接显示完成状态。🔴 已丢弃的不给勾选框 —— 先「恢复」再决定
  if(isMemo && !dropped) row.append(makeChk(it, renderList));
  const c = document.createElement('div');
  c.className = 'c';
  const m = document.createElement('div');
  m.className = 'm';
  m.textContent = (LABEL[it.s]||'') + ' · ' + it.d;
  const x = document.createElement('div');
  x.className = 'x';
  x.textContent = it.t;
  c.append(m, x);
  if(it.note){
    const n = document.createElement('div');
    n.className = 'n';
    n.textContent = '结论：' + it.note;
    c.append(n);
  }
  const ops = document.createElement('div');
  ops.className = 'ops';
  if(dropped){
    // 已丢弃（备忘 / 灵感都在这个桶）→ 恢复 + 永久删除
    const back = document.createElement('button');
    back.textContent = '← 恢复';
    back.title = '放回内容流（备忘变回没完成，灵感变回待处理）';
    back.addEventListener('click', async (e)=>{
      e.stopPropagation();
      back.disabled = true;
      try {
        if(isMemo) await applyMemoStatus(it, 'open');
        else await applyStatus(it, 'open', it.note || '');
        renderList();
      } catch(err){ toast('写入失败：' + err.message); back.disabled = false; }
    });
    const del = document.createElement('button');
    del.className = 'danger';
    del.textContent = '🗑 永久删除';
    del.title = '从库里删掉，找不回来';
    if(MODE !== 'serve'){
      del.disabled = true;
      del.title = '离线模式删不了 —— 用桌面「脑内抽屉」打开再删';
    }
    del.addEventListener('click', async (e)=>{
      e.stopPropagation();
      del.disabled = true;
      try { await deleteItem(it); }
      catch(err){ toast('删除失败：' + err.message); del.disabled = false; }
    });
    ops.append(back, del);
  } else if(isMemo){
    const want = it.s !== 'memo_done';
    const b = document.createElement('button');
    b.textContent = want ? '✓ 完成' : '← 取消完成';
    b.title = want ? '标记这条备忘已完成' : '取消完成，放回内容流';
    b.addEventListener('click', async (e)=>{
      e.stopPropagation();
      b.disabled = true;
      try { await applyMemoDone(it, want); renderList(); }
      catch(err){ toast('写入失败：' + err.message); b.disabled = false; }
    });
    ops.append(b);
  } else {
    const b = document.createElement('button');
    b.textContent = '← 恢复';
    b.title = '放回内容流（变回待处理）';
    b.addEventListener('click', async (e)=>{
      e.stopPropagation();
      b.disabled = true;
      try { await applyStatus(it, 'open', it.note || ''); renderList(); }
      catch(err){ toast('写入失败：' + err.message); b.disabled = false; }
    });
    ops.append(b);
  }
  // ✏️ 编辑：每条都有，不分状态（已丢弃的也能改）。
  // 🔴 列表浮层 z-index 45 盖在详情浮层 40 上面 —— 所以先关列表、再开详情并直接进编辑态。
  const ed = document.createElement('button');
  ed.className = 'edit';
  ed.textContent = '✏️ 编辑';
  ed.title = '改正文；创建时间不变';
  ed.addEventListener('click', (e)=>{
    e.stopPropagation();
    listEl.classList.remove('on');
    openModal(it);
    setEditMode(true);
  });
  ops.append(ed);
  row.append(c, ops);
  row.addEventListener('click', ()=> openModal(it));
  return row;
}

function renderList(){
  document.getElementById('ltitle').textContent = ARCHIVE_TITLE[listStatus] || '';
  const items = IDEAS.filter(x=>x.s === listStatus)
                     .slice()
                     .sort((a,b)=> (b.d||'').localeCompare(a.d||''));
  document.getElementById('lcnt').textContent = items.length + ' 条';
  const body = document.getElementById('lbody');
  body.innerHTML = '';
  if(!items.length){
    const d = document.createElement('div');
    d.className = 'empty';
    d.textContent = ARCHIVE_EMPTY[listStatus] || '这里是空的';
    body.append(d);
    return;
  }
  for(const it of items) body.append(makeRow(it));
}

async function openList(status){
  listStatus = status;
  // 列表开着时轮询是停的 —— 打开前手动同步一次，保证看到的是库里的最新状态
  if(MODE === 'serve'){
    try{
      const r = await apiGet('/api/items');
      const j = await r.json();
      if(j && j.ok){
        const incoming = j.items || [];
        const byId = new Map(incoming.map(x=>[x.id, x]));
        for(const it of IDEAS){
          const s = byId.get(it.id);
          if(s){ it.s = s.s; if(s.note) it.note = s.note; if(s.t) it.t = s.t; }
        }
        const known = new Set(IDEAS.map(x=>x.id));
        for(const x of incoming){ if(!known.has(x.id)) IDEAS.push(x); }
        IDEAS = IDEAS.filter(x=>byId.has(x.id));
        updateCounts();
      }
    }catch(e){}
  }
  renderList();
  listEl.classList.add('on');
}

for(const s of ARCHIVE){
  const btn = document.getElementById('btn-' + s);
  if(btn) btn.addEventListener('click', ()=> openList(s));
}
document.getElementById('lclose').addEventListener('click', ()=> listEl.classList.remove('on'));
// 只有点浮层空白处才关（点某一行不关）
listEl.addEventListener('click', (e)=>{ if(e.target === listEl) listEl.classList.remove('on'); });

// 停止本地服务 —— 服务是后台常驻的，得留一个不用开任务管理器的出口
document.getElementById('btn-quit').addEventListener('click', async ()=>{
  if(!window.confirm('停止本地服务？页面会退回离线模式（操作需导出后手动应用）。')) return;
  try { await fetch('/api/quit', {method:'POST'}); }catch(e){}
  toast('服务已停止 —— 关掉本页即可');
});

/* ── 设置浮层 ─────────────────────────────────────────────
   这里**只放跟本页相关的那一项**（内容流放哪几类）。
   凭据 / 触发词 / 搜索目录 / 开机自启 / 播报文案 → 都在配置中心（config-web.py）里改。
   写的是 memo-bot/settings.json —— 和配置中心、机器人「设置」指令是同一份。
   🔴 离线模式（file://）下没有服务，只能看不能存。 */
const PAGE_KEYS = ['bubbles_show'];     // 本页负责的设置项（其余归配置中心）
const settingsEl = document.getElementById('settings');
const sbodyEl = document.getElementById('sbody');
const smsgEl = document.getElementById('smsg');

function pageSettings(){
  return SETTINGS.filter(s => PAGE_KEYS.includes(s.key));
}

function renderSettings(){
  sbodyEl.innerHTML = '';
  for(const s of pageSettings()){
    const item = document.createElement('div');
    item.className = 'item';
    const lb = document.createElement('div');
    lb.className = 'lb'; lb.textContent = s.label;
    const hp = document.createElement('div');
    hp.className = 'hp'; hp.textContent = s.help || '';
    item.append(lb, hp);

    if(s.kind === 'multi'){
      const box = document.createElement('div');
      box.className = 'chks';
      for(const c of (s.choices || [])){
        const l = document.createElement('label');
        const i = document.createElement('input');
        i.type = 'checkbox'; i.value = c; i.dataset.k = s.key;
        i.checked = (s.value || []).includes(c);
        l.append(i, document.createTextNode((s.choice_labels || {})[c] || c));
        box.append(l);
      }
      item.append(box);
    } else {
      const i = document.createElement('input');
      i.type = 'text'; i.dataset.k = s.key; i.value = s.value || '';
      item.append(i);
    }

    const rst = document.createElement('button');
    rst.className = 'rst'; rst.textContent = '用默认值';
    rst.addEventListener('click', ()=>{
      if(s.kind === 'multi'){
        sbodyEl.querySelectorAll('input[type=checkbox][data-k="' + s.key + '"]').forEach(i=>{
          i.checked = (s.default || []).includes(i.value);
        });
      } else {
        const el = sbodyEl.querySelector('input[type=text][data-k="' + s.key + '"]');
        if(el) el.value = s.default || '';
      }
    });
    item.append(rst);
    sbodyEl.append(item);
  }
  sbodyEl.append(deleteConfirmItem());
}

/* 「永久删除前再确认」—— 本页自己的一个开关，**不进 settings.json**。
   它只是这台浏览器上的习惯（localStorage），所以单独渲染、不走 collectSettings()。
   关掉之后，「已丢弃」列表里点「永久删除」就直接删 —— 想让它回来，在这里勾回去。 */
function deleteConfirmItem(){
  const item = document.createElement('div');
  item.className = 'item';
  const lb = document.createElement('div');
  lb.className = 'lb'; lb.textContent = '永久删除前再确认';
  const hp = document.createElement('div');
  hp.className = 'hp';
  hp.textContent = '「已丢弃」列表里点「永久删除」时弹不弹确认框。'
                 + '关掉就直接删，不再问 —— 删掉找不回来，建议留着。';
  const box = document.createElement('div');
  box.className = 'chks';
  const l = document.createElement('label');
  const i = document.createElement('input');
  i.type = 'checkbox'; i.id = 'del-confirm';
  i.checked = !noAskDelete;
  i.addEventListener('change', ()=>{
    noAskDelete = !i.checked;
    lsSet(NOASK_KEY, noAskDelete);
    toast(noAskDelete ? '以后不再弹确认框' : '删除前会再确认一次');
  });
  l.append(i, document.createTextNode('弹确认框（关掉 = 以后不再提示）'));
  box.append(l);
  item.append(lb, hp, box);
  return item;
}

function collectSettings(){
  const out = {};
  for(const s of pageSettings()){
    let v;
    if(s.kind === 'multi'){
      v = [...sbodyEl.querySelectorAll(
        'input[type=checkbox][data-k="' + s.key + '"]:checked')].map(i=>i.value);
    } else {
      const el = sbodyEl.querySelector('input[type=text][data-k="' + s.key + '"]');
      v = el ? el.value : '';
    }
    // 🔴 只提交**改动过的**键。否则每点一次「保存」就把全部键写进 settings.json，
    //    同名 `.env` 变量从此失效 —— 和「没改过的走 env / 默认值」的承诺冲突。
    if(JSON.stringify(v) !== JSON.stringify(s.value)) out[s.key] = v;
  }
  return out;
}

function applySettingsPayload(keys){
  if(Array.isArray(keys) && keys.length){
    SETTINGS = keys;
    SHOW = settingValue('bubbles_show') || ['memo', 'open'];
  }
}

// 保存后重建内容流：清掉舞台上的气泡 + 队列，按新范围重新预热铺满
function rebuildStream(){
  queue = [];
  stage.innerHTML = '';
  lastSpawn = 0;
  updateCounts();
  emptyEl.classList.toggle('on', streamItems().length === 0);
  prewarm();
}

/* 从服务端把设置拉回来（仅 serve）。
   🔴 页面内嵌的 __SETTINGS__ 是**页面被渲染那一刻**的快照 —— 服务进程是常驻的，
      别人（或群里「设置」指令）改过之后，刷新页面必须重新拉，
      否则展示范围还停在旧值，看着像「改了没用」。 */
async function refreshSettings(){
  if(MODE !== 'serve') return;
  try{
    const r = await apiGet('/api/settings');
    const j = await r.json();
    if(j && j.ok) applySettingsPayload(j.keys);
  }catch(e){}
}

async function openSettings(){
  smsgEl.textContent = '';
  if(MODE === 'serve'){
    await refreshSettings();
  } else {
    smsgEl.textContent = '离线模式：只能看，改动要用桌面「脑内抽屉」打开';
  }
  renderSettings();
  settingsEl.classList.add('on');
}

async function saveSettings(){
  if(MODE !== 'serve'){
    smsgEl.textContent = '离线模式存不了 —— 用桌面「脑内抽屉」打开再改';
    return;
  }
  try{
    const vals = collectSettings();
    if(!Object.keys(vals).length){
      smsgEl.textContent = '没有改动';
      return;
    }
    const r = await fetch('/api/settings', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({values: vals}),
    });
    const j = await r.json();
    if(!j || !j.ok){
      smsgEl.textContent = (j && j.error) || '保存失败';
      return;
    }
    applySettingsPayload(j.keys);
    renderSettings();
    rebuildStream();
    smsgEl.textContent = '已保存 ✓';
    toast('设置已保存 · 展示范围已生效');
  }catch(e){
    smsgEl.textContent = '保存失败：' + e;
  }
}

document.getElementById('btn-settings').addEventListener('click', openSettings);

/* 「⬇ 导出」：让浏览器直接「另存为」。
   走服务端 /api/export —— 和机器人「导出」指令共用 export.py 里同一套排版，
   两边导出来的文件长得一模一样。想导 json / txt 就在群里发「导出 json」。 */
document.getElementById('btn-export').addEventListener('click', ()=>{
  location.href = '/api/export?format=md';
});
document.getElementById('sclose').addEventListener('click', ()=> settingsEl.classList.remove('on'));
settingsEl.addEventListener('click', (e)=>{ if(e.target === settingsEl) settingsEl.classList.remove('on'); });
document.getElementById('ssave').addEventListener('click', saveSettings);

/* ── 键盘 / 启动 ─────────────────────────────────────────── */
// 暂停 = 不再生成新气泡（已在飘的继续飘完，避免半路卡住）
function setRunning(v){
  running = v;
  stateEl.textContent = v ? '空格暂停 / 继续' : '已暂停 —— 空格继续';
}

document.addEventListener('keydown', (e)=>{
  // 🔴 编辑框里敲空格是**打字**，不是暂停轮播 —— 必须排掉，否则正文里的空格全被吃掉
  if(e.code === 'Space' && document.activeElement !== noteEl && document.activeElement !== editboxEl){
    e.preventDefault(); setRunning(!running);
  }
  if(e.code === 'Escape'){
    // 确认框在最上层 —— 先关它（关掉 = 取消删除）；编辑态则先退出编辑（丢弃改动）
    if(confirmEl.classList.contains('on')) closeConfirm(false);
    else if(editing) setEditMode(false);
    else { modal.classList.remove('on'); listEl.classList.remove('on'); settingsEl.classList.remove('on'); }
  }
});

// 🔴 启动**先铺气泡、再去连服务**。
//    页面里的 __DATA__ / __SETTINGS__ 本来就是「渲染那一刻」的快照，够铺第一屏了；
//    detectMode() 那两跳（/api/ping、/api/settings）只是用来确认「是不是 serve 模式 /
//    设置有没有被别人改过」。放在 await 后面的话，任何一跳慢或挂住 = 开屏一个气泡都没有
//    （真机复现：ping 延迟 3s → 3s 空屏）。
(async ()=>{
  refill();

  // 🔴 开局一次性按「飞行进度」均匀铺满 —— 不要 setTimeout 连发，
  //    连发会让所有气泡挤在屏幕底部同一段高度里（真机截图：overlaps 3~5）。
  // 预热位置**等距**（i/cnt），别加随机抖动 —— 抖一下就可能让两个气泡撞在同一高度
  prewarm();

  // 设置真被改过才重建 —— 内嵌快照本来就由同一个服务生成，绝大多数情况一模一样；
  // 无条件重建会让第一屏白闪一下。
  const showBefore = JSON.stringify(SHOW);
  await detectMode();
  if(JSON.stringify(SHOW) !== showBefore) rebuildStream();

  function loop(){
    if(running) spawn();
    setTimeout(loop, layout().tick);
  }
  loop();
})();
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
    cfg = json.dumps(settings.describe(), ensure_ascii=False).replace("</", "<\\/")
    return (
        TEMPLATE
        .replace("__BRAND__", _BRAND_SUFFIX if PAGE_BRAND else "")
        .replace("__SETTINGS__", cfg)
        .replace("__DATA__", data)
        .replace("__LABEL__", json.dumps(BUCKET_LABEL, ensure_ascii=False))
        .replace("__GEN__", json.dumps(time.strftime("%Y-%m-%d %H:%M"), ensure_ascii=False))
    ), stats


def apply_ops(path):
    """应用离线模式导出的 `抽屉操作.json`。

    条目可以叠着写（一条同时改正文 + 改状态也行）：
      - 灵感改状态：`{"id":…, "status":"done|decided|dropped|open", "note":…}`
      - 备忘改状态：`{"id":…, "kind":"memo", "status":"open|done|dropped"}`
                    （老写法 `{"kind":"memo", "done":true|false}` 仍然认）
      - ✏️ 改正文：`{"id":…, "content":"新的正文"}` —— 可单用，也可和上面叠。
        🔴 只改 `content`，**创建时间不变**（`store.update_content` 里 SQL 就没带 created_at）。

    🔴 **不含永久删除** —— 那是不可恢复的操作，只能在页面上确认着点，
       不让它跟着「导出 → --apply」这条可以反复执行的链路走。
       （编辑不受这条限制：改错了再改回来就行，可逆。）
    """
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
        if not iid:
            print(f"  跳过非法条目：{op}")
            continue

        # ① 先处理正文编辑（如果有）
        content = op.get("content")
        if content is not None:
            text = clean(str(content))
            if text:
                c = store.update_content(iid, text)
                n += c
                print(f"  [编辑] {iid[:8]} {'OK' if c else '（未找到）'}")
            else:
                print(f"  跳过空正文：{iid[:8]}")

        # ② 再处理状态变更（如果有）
        status = (op.get("status") or "").strip()
        is_memo = op.get("kind") == "memo" or "done" in op
        if is_memo:
            # 🔴 `done` 键存在才算「要改状态」—— 只有 `kind` 没 `done`/`status` 的条目
            #    是「纯编辑备忘」，不能顺手把它改成 open（那会悄悄回退已完成的条目）
            if "done" in op:
                st = store.STATUS_DONE if op.get("done") else store.STATUS_OPEN
            else:
                st = status
            if st:
                if st not in store.MEMO_STATUSES:
                    print(f"  跳过非法条目：{op}")
                    continue
                c = store.set_memo_status([iid], st)
                n += c
                print(f"  [{BUCKET_LABEL[memo_bucket(st)]}] {iid[:8]} {'OK' if c else '（未找到）'}")
            elif content is None:
                print(f"  跳过非法条目：{op}")
        elif status:
            if status not in VALID_STATUS:
                print(f"  跳过非法条目：{op}")
                continue
            c = store.set_idea_status([iid], status, (op.get("note") or "").strip() or None)
            n += c
            print(f"  [{STATUS_LABEL[status]}] {iid[:8]} {'OK' if c else '（未找到）'}")
        elif content is None:
            print(f"  跳过非法条目：{op}")
    print(f"已应用 {n} 条")
    return 0


def _make_handler(doc):
    """本地服务的请求处理器。

    静态页 + `GET /api/ping` / `GET /api/items`（备忘 + 灵感，`/api/ideas` 是旧别名）
    + `GET /api/export` / `GET|POST /api/settings`
    + `POST /api/status`（灵感改状态）/ `POST /api/memo`（备忘改状态）
    + `POST /api/edit`（**改正文，创建时间不变**）
    + `POST /api/delete`（**永久删除**一条，不可恢复）/ `POST /api/quit`。
    """
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

        def _read_json(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def _send_file(self, fname, data, ctype):
            """给浏览器一个「另存为」响应（「⬇ 导出」用）。"""
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            # 文件名是中文 → 必须用 RFC 5987 的 filename*，否则浏览器拿到的是乱码
            self.send_header("Content-Disposition",
                             f"attachment; filename*=UTF-8''{quote(fname)}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.startswith("/api/ping"):
                return self._send(200, {"ok": True, "mode": "serve"})
            if self.path.startswith("/api/export"):
                q = parse_qs(urlparse(self.path).query)
                fmt = (q.get("format") or ["md"])[0]
                try:
                    fname, data = export.render(fmt)
                except Exception as e:  # noqa: BLE001 —— 回给页面，别让服务挂掉
                    return self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
                return self._send_file(fname, data, export.MIME[export.normalize_format(fmt)])
            if self.path.startswith("/api/items") or self.path.startswith("/api/ideas"):
                # `/api/ideas` 是旧路径，保留兼容（老的静态页/脚本还在用它）
                return self._send(200, {"ok": True, "items": collect()})
            if self.path.startswith("/api/settings"):
                return self._send(200, {"ok": True, "keys": settings.describe()})
            if self.path in ("/", "/index.html"):
                return self._send(200, doc, "text/html; charset=utf-8")
            return self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            if self.path.startswith("/api/quit"):
                # 🔴 shutdown() 必须在**另一个线程**里调 —— 在请求处理线程里直接调会死锁
                self._send(200, {"ok": True})
                print("  收到停止请求，服务退出。", flush=True)
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if self.path.startswith("/api/settings"):
                # 网页「⚙ 设置」保存 —— 和机器人「设置」指令写的是同一份 settings.json
                try:
                    payload = self._read_json()
                except (ValueError, OSError) as e:
                    return self._send(400, {"ok": False, "error": f"请求体解析失败：{e}"})
                vals = payload.get("values")
                if not isinstance(vals, dict):
                    return self._send(400, {"ok": False, "error": "缺少 values 对象"})
                try:
                    n_ok, errs = settings.apply_values(vals)
                except Exception as e:  # noqa: BLE001 —— 回给页面，别让服务挂掉
                    return self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
                if errs:
                    return self._send(400, {"ok": False, "error": "；".join(errs)})
                print(f"  [设置] 已更新 {n_ok} 项", flush=True)
                return self._send(200, {"ok": True, "keys": settings.describe()})
            if self.path.startswith("/api/memo"):
                # 备忘改状态：完成 / 取消完成 / 丢弃 —— 备忘没有「已决议」那一套
                try:
                    payload = self._read_json()
                except (ValueError, OSError) as e:
                    return self._send(400, {"ok": False, "error": f"请求体解析失败：{e}"})
                iid = (payload.get("id") or "").strip()
                # 🔴 兼容老的 `{done: true/false}` 写法（离线操作文件里长这样）
                status = (payload.get("status") or "").strip()
                if not status:
                    status = store.STATUS_DONE if payload.get("done") else store.STATUS_OPEN
                if not iid or status not in store.MEMO_STATUSES:
                    return self._send(400, {"ok": False, "error": "id 或 status 不合法"})
                try:
                    changed = store.set_memo_status([iid], status)
                except Exception as e:  # noqa: BLE001 —— 回给页面，别让服务挂掉
                    return self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
                if not changed:
                    return self._send(404, {"ok": False, "error": "没找到这条备忘（可能已被删）"})
                print(f"  [{BUCKET_LABEL[memo_bucket(status)]}] {iid[:8]}", flush=True)
                return self._send(200, {"ok": True, "changed": changed})
            if self.path.startswith("/api/delete"):
                # 🔴 永久删除（真删行，不可恢复）。页面侧已经弹过确认框了，这里不再问。
                try:
                    payload = self._read_json()
                except (ValueError, OSError) as e:
                    return self._send(400, {"ok": False, "error": f"请求体解析失败：{e}"})
                iid = (payload.get("id") or "").strip()
                if not iid:
                    return self._send(400, {"ok": False, "error": "id 不合法"})
                try:
                    deleted = store.delete_items([iid])
                except Exception as e:  # noqa: BLE001 —— 回给页面，别让服务挂掉
                    return self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
                if not deleted:
                    return self._send(404, {"ok": False, "error": "没找到这条（可能已经被删了）"})
                print(f"  [永久删除] {iid[:8]}", flush=True)
                return self._send(200, {"ok": True, "deleted": deleted})
            if self.path.startswith("/api/edit"):
                # ✏️ 再次编辑正文。🔴 **只动 content，创建时间一个字节都不碰**（用户明确要求）。
                try:
                    payload = self._read_json()
                except (ValueError, OSError) as e:
                    return self._send(400, {"ok": False, "error": f"请求体解析失败：{e}"})
                iid = (payload.get("id") or "").strip()
                # 走和收消息时同一个 clean() —— 两边口径一致（规整空白、保留换行）
                text = clean(str(payload.get("content") or ""))
                if not iid or not text:
                    return self._send(400, {"ok": False, "error": "id 或正文不合法（正文不能是空的）"})
                try:
                    changed = store.update_content(iid, text)
                except Exception as e:  # noqa: BLE001 —— 回给页面，别让服务挂掉
                    return self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
                if not changed:
                    return self._send(404, {"ok": False, "error": "没找到这条（可能已被删）"})
                print(f"  [编辑] {iid[:8]}  {text[:40]}", flush=True)
                return self._send(200, {"ok": True, "changed": changed, "content": text})
            if not self.path.startswith("/api/status"):
                return self._send(404, {"ok": False, "error": "not found"})
            try:
                payload = self._read_json()
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
    last_err = None
    for p in range(port, port + tries):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), handler)
            port = p
            break
        except OSError as e:      # 🔴 别静默吞掉：只报「没空端口」会误导（可能是别的错）
            last_err = e
            continue
    if httpd is None:
        print(f"（{port}~{port + tries - 1} 都起不来：{type(last_err).__name__}: {last_err}）")
        return 1

    url = f"http://127.0.0.1:{port}/"
    # 🔴 flush=True：stdout 被重定向到文件时是块缓冲的，不 flush 的话这几行会一直
    #    卡在缓冲区里 —— 服务被 Ctrl+C 停掉时整段丢失，open_drawer_child.log 里
    #    只剩一个「服务已退出」的空白，排错时看不到任何线索。
    print(f"脑内抽屉服务已启动：{url}", flush=True)
    print("  处理按钮**直接写入** memos.db；页面顶部「⏻ 停止服务」可停，Ctrl+C 也行。", flush=True)
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
    ap = argparse.ArgumentParser(description="脑内抽屉轮播页面（备忘 + 灵感；生成静态页 / 起本地服务）")
    ap.add_argument("-o", "--out", default=DEFAULT_OUT, help="静态 HTML 输出路径")
    ap.add_argument("--serve", action="store_true", help="起本地服务，页面可直接处理备忘 / 灵感")
    ap.add_argument("--port", type=int, default=8765, help="--serve 的起始端口")
    ap.add_argument("--no-browser", action="store_true", help="--serve 时不自动打开浏览器")
    ap.add_argument("--apply", metavar="JSON", help="应用离线模式导出的操作文件")
    ap.add_argument("--open-only", action="store_true", help="只放「没完成的备忘 + 待处理灵感」")
    ap.add_argument("--no-memo", action="store_true", help="不带备忘，只看灵感")
    args = ap.parse_args()

    if args.apply:
        sys.exit(apply_ops(args.apply))

    store.init()
    items = collect(args.open_only, not args.no_memo)
    if not items and not args.serve:
        # 静态快照模式：空库确实没东西可放。
        # 🔴 但 --serve 不能退 —— 页面还得能打开去看归档（已完成/已处理/已决议/垃圾桶），
        #    否则「库一空，服务就起不来」= 一个完全静默的失败（2026-10-09 修）。
        print("（库里是空的，没有可放的内容）")
        sys.exit(1)

    doc, stats = build_html(items)
    brief = f"共 {len(items)} 条 · " + " / ".join(
        f"{BUCKET_LABEL[k]} {stats.get(k, 0)}" for k in BUCKET_ORDER if stats.get(k)
    )

    if args.serve:
        print(brief)
        sys.exit(serve(doc, args.port, not args.no_browser))

    out = os.path.abspath(args.out)
    with open(out, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"已生成：{out}")
    print(f"  {brief}")
    print(f"  体积 {os.path.getsize(out) // 1024} KB")
    print("  ⚠️ 静态页是**快照**：处理只能暂存后导出。要实时处理请用 --serve。")


if __name__ == "__main__":
    main()
