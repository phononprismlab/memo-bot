"""把灵感库渲染成一个「思维泡泡」轮播页面，并支持在页面上直接处理灵感。

这个文件是干嘛的：读 `memos.db` 里的灵感 → 生成一个不停往上飘的气泡轮播页，
点气泡就能改状态（已处理 / 已决议 / 丢弃），**在页面上直接写库**，不用导出任何文件。

来源：灵感 `[10-08 17:25] (decided) 我想起之前对于个人网站的设想，就是一个我，
背后不断滚动？浮现那些天马行空的想法……` 的落地。

用法（推荐 --serve：页面直连灵感库，点一下即刻写库，不需要导出任何文件）：
  python ideas-bubbles.py --serve               # 起本地服务 + 打开浏览器（桌面入口走的就是这条）
  python ideas-bubbles.py                       # 只生成静态 HTML → 工作区根 灵感泡泡.html
  python ideas-bubbles.py --apply 灵感操作.json  # 应用离线模式导出的操作
  python ideas-bubbles.py -o out.html
  python ideas-bubbles.py --open-only           # 不加载归档，只带「待处理」
  python ideas-bubbles.py --include-memo        # 连备忘一起放

视图：
  - **内容流放哪几类由设置决定**（`bubbles_show`，默认只放「待处理」）。
    顶部「⚙ 设置」里勾选，改的是 `memo-bot/settings.json` ——
    和配置中心网页、机器人「设置」指令写的是同一份。
  - 顶部三个按钮「✅ 已处理 / 📌 已决议 / 🗑 垃圾桶」，点开在列表里看，
    列表里可以「← 恢复」把某条放回内容流。
  - **有结论的条目会把结论一起显示**（气泡里在正文下方、虚线隔开一段「结论：…」）。
    没有结论就不显示 —— 和以前一样。已处理 / 已决议 / 已丢弃 通常带结论。
  - 丢弃 ≠ 删除：记录还在库里，只是进垃圾桶（status=dropped）。

设计：
  - 气泡从底部升起、横向漂移、顶部淡出，循环不停。
  - 一轮内不重复；一轮放完自动重洗（所以每次打开顺序都不一样）。
  - 按灵感状态配色；长文本气泡自动缩小字号，**正文完整显示、不截断**（结论同样不截断）；
    **换行原样保留**（群里分几行写的，气泡与详情里就是几行）。
  - 悬停暂停单个气泡，点击展开浮层（全文 + 处理按钮 + 结论输入）。
  - 数据在生成时**内嵌**进 HTML —— file:// 下 fetch 本地 JSON 会被 CORS 拦，内嵌最稳。

两种处理模式（页面自动探测）：
  - **在线（--serve，默认用法）**：`GET /api/ping` 通 → 点按钮直接 POST 写 memos.db，
    并每 20s 拉一次 `/api/ideas`（bot 新收的灵感会自动进流）。顶部「⏻ 停止服务」可停。
    设置读写走 `GET|POST /api/settings`。
  - **离线（file:// 直接双击 HTML）**：操作暂存 localStorage，顶部显示待提交数，
    可导出 `灵感操作.json`，再跑 `--apply` 落地 —— 仅作降级（设置也只能看、不能存）。
"""
import argparse
import json
import os
import re
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import settings  # noqa: E402  （设置：网页「⚙ 设置」与机器人「设置」指令共用）
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
  /* 归档入口：已处理 / 已决议 / 垃圾桶 */
  .bar .pill.act{cursor:pointer; transition:.16s}
  .bar .pill.act b{font-weight:600; margin-left:5px; color:#cbd5e1}
  .bar .pill.act:hover{background:rgba(255,255,255,.13); color:var(--ink)}
  .bar .pill.act[data-s="done"]:hover{border-color:var(--done); color:var(--done)}
  .bar .pill.act[data-s="decided"]:hover{border-color:var(--decided); color:var(--decided)}
  .bar .pill.act[data-s="dropped"]:hover{border-color:var(--dropped); color:var(--ink)}
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
  #list .row[data-s="done"]{--accent:var(--done)}
  #list .row[data-s="decided"]{--accent:var(--decided)}
  #list .row[data-s="dropped"]{--accent:var(--dropped)}
  #list .row[data-s="open"]{--accent:var(--open)}
  #list .row .c{flex:1 1 auto; min-width:0}
  #list .row .m{font-size:10.5px; color:var(--dim); letter-spacing:.5px; margin-bottom:4px}
  #list .row .x{font-size:13.5px; line-height:1.65; white-space:pre-wrap; word-break:break-word}
  #list .row .n{font-size:12px; color:#8fa3b8; margin-top:6px; white-space:pre-wrap}
  #list .row .ops{flex:0 0 auto}
  #list .row .ops button{
    padding:6px 10px; border-radius:8px; cursor:pointer; white-space:nowrap;
    background:rgba(255,255,255,.06); border:1px solid var(--line); color:var(--dim);
    font:11.5px/1 inherit; transition:.15s;
  }
  #list .row .ops button:hover{color:var(--open); border-color:rgba(79,209,197,.5); background:rgba(79,209,197,.12)}
  #list .row .ops button[disabled]{opacity:.5; cursor:default}
  #list .empty{padding:36px 10px; text-align:center; color:var(--dim); font-size:13.5px}

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
    <span class="pill act" id="btn-done" data-s="done">✅ 已处理<b>0</b></span>
    <span class="pill act" id="btn-decided" data-s="decided">📌 已决议<b>0</b></span>
    <span class="pill act" id="btn-dropped" data-s="dropped">🗑 垃圾桶<b>0</b></span>
    <span class="pill act" id="btn-settings" title="内容流放哪几类">⚙ 设置</span>
    <span class="pill quit" id="btn-quit" style="display:none">⏻ 停止服务</span>
  </div>
</div>

<div id="stage"></div>

<div id="empty"><div class="big">🎉</div><div>灵感都处理完了</div></div>

<div id="modal"><div class="card">
  <div class="meta"></div>
  <div class="body"></div>
  <textarea class="note" id="note" rows="2" placeholder="结论 / 备注（可选）"></textarea>
  <div class="actions">
    <button data-act="done">✅ 已处理</button>
    <button data-act="decided">📌 已决议</button>
    <button data-act="dropped">🗑 丢弃</button>
    <button data-act="open">← 恢复待处理</button>
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
const emptyEl = document.getElementById('empty');
const nEl = document.getElementById('n');
const nArchEl = document.getElementById('n-arch');

document.getElementById('gen').textContent = GEN;

/* ── 分流 ────────────────────────────────────────────────────
   🔴 内容流放哪几类由**设置**决定（`bubbles_show`，默认只放「待处理」）；
      已处理 / 已决议 / 已丢弃 另有归档列表（点顶部按钮看）。
      展示范围在页面上改：顶部「⚙ 设置」，和机器人「设置」指令是同一份。 */
const ARCHIVE = ['done', 'decided', 'dropped'];
const ARCHIVE_TITLE = { done: '✅ 已处理', decided: '📌 已决议', dropped: '🗑 垃圾桶' };
const ARCHIVE_EMPTY = { done: '还没有处理完的灵感', decided: '还没有下过结论的灵感', dropped: '垃圾桶是空的' };

let SETTINGS = __SETTINGS__;
function settingValue(key){
  const s = SETTINGS.find(x => x.key === key);
  return s ? s.value : null;
}
let SHOW = settingValue('bubbles_show') || ['open'];

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
    modeEl.textContent = '● 已连灵感库 · 处理即刻写库';
    document.getElementById('btn-quit').style.display = '';
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
    const r = await fetch('/api/ideas', {cache:'no-store'});
    const j = await r.json();
    if(!j || !j.ok) return;
    const incoming = j.items || [];
    const byId = new Map(incoming.map(x => [x.id, x]));
    for(const it of IDEAS){
      const s = byId.get(it.id);
      if(s){ it.s = s.s; if(s.note) it.note = s.note; }
    }
    const known = new Set(IDEAS.map(x => x.id));
    for(const x of incoming){ if(!known.has(x.id)) IDEAS.push(x); }
    IDEAS = IDEAS.filter(x => byId.has(x.id));
    await refreshSettings();       // 群里「设置」指令改了展示范围，页面也跟上
    updateCounts();
  }catch(e){}
}

// 🔴 改状态 ≠ 删记录：只把它从内容流挪到对应归档列表（丢弃也一样，进垃圾桶而不是消失）
function moveIdea(id, status){
  const it = IDEAS.find(x=>x.id === id);
  if(it) it.s = status;
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
    pending[it.id] = {s: status, note: note || '', t: Date.now()};
    lsSet(PENDING_KEY, pending);
    renderPending();
  }
  if(note) it.note = note;
  moveIdea(it.id, status);
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
function prewarm(){
  const cfg = layout();
  const cnt = Math.min(cfg.maxB, streamItems().length);
  for(let i = 0; i < cnt; i++) spawn(i / cnt);
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

  const meta = document.createElement('span');
  meta.className = 'meta';
  meta.textContent = (LABEL[it.s]||'') + ' · ' + it.d;
  const txt = document.createElement('div');
  txt.className = 'txt';
  txt.textContent = it.t;              // 🔴 全文不截断，且原样保留换行（容器是 pre-wrap）
  el.append(meta, txt);
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

/* ── 归档列表：已处理 / 已决议 / 垃圾桶 ──────────────────── */
let listStatus = null;

function makeRow(it){
  const row = document.createElement('div');
  row.className = 'row';
  row.dataset.s = it.s;
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
      const r = await fetch('/api/ideas', {cache:'no-store'});
      const j = await r.json();
      if(j && j.ok){
        const incoming = j.items || [];
        const byId = new Map(incoming.map(x=>[x.id, x]));
        for(const it of IDEAS){
          const s = byId.get(it.id);
          if(s){ it.s = s.s; if(s.note) it.note = s.note; }
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
    SHOW = settingValue('bubbles_show') || ['open'];
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
    const r = await fetch('/api/settings', {cache:'no-store'});
    const j = await r.json();
    if(j && j.ok) applySettingsPayload(j.keys);
  }catch(e){}
}

async function openSettings(){
  smsgEl.textContent = '';
  if(MODE === 'serve'){
    await refreshSettings();
  } else {
    smsgEl.textContent = '离线模式：只能看，改动要用桌面「灵感泡泡」打开';
  }
  renderSettings();
  settingsEl.classList.add('on');
}

async function saveSettings(){
  if(MODE !== 'serve'){
    smsgEl.textContent = '离线模式存不了 —— 用桌面「灵感泡泡」打开再改';
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
  if(e.code === 'Space' && document.activeElement !== noteEl){
    e.preventDefault(); setRunning(!running);
  }
  if(e.code === 'Escape'){
    modal.classList.remove('on'); listEl.classList.remove('on'); settingsEl.classList.remove('on');
  }
});

// 🔴 启动必须**等设置拉回来再铺气泡** —— 否则第一屏按内嵌的旧范围铺，
//    服务端已经改过的话会出现「显示了不该显示的类别」。
(async ()=>{
  await detectMode();
  refill();

  // 🔴 开局一次性按「飞行进度」均匀铺满 —— 不要 setTimeout 连发，
  //    连发会让所有气泡挤在屏幕底部同一段高度里（真机截图：overlaps 3~5）。
  // 预热位置**等距**（i/cnt），别加随机抖动 —— 抖一下就可能让两个气泡撞在同一高度
  prewarm();

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
    """本地服务的请求处理器。

    静态页 + `GET /api/ping` / `GET /api/ideas` / `GET|POST /api/settings`
    + `POST /api/status` / `POST /api/quit`。
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

        def do_GET(self):
            if self.path.startswith("/api/ping"):
                return self._send(200, {"ok": True, "mode": "serve"})
            if self.path.startswith("/api/ideas"):
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
    print(f"灵感泡泡服务已启动：{url}")
    print("  处理按钮**直接写入** memos.db；页面顶部「⏻ 停止服务」可停，Ctrl+C 也行。")
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
    if not items and not args.serve:
        # 静态快照模式：空库确实没东西可放。
        # 🔴 但 --serve 不能退 —— 页面还得能打开去看归档（已处理/已决议/垃圾桶），
        #    否则「库一空，服务就起不来」= 一个完全静默的失败（2026-10-09 修）。
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
    print(f"  体积 {os.path.getsize(out) // 1024} KB")
    print("  ⚠️ 静态页是**快照**：处理只能暂存后导出。要实时处理请用 --serve。")


if __name__ == "__main__":
    main()
