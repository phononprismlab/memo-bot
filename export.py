"""把待办 / 灵感导出成一个文件（Markdown / 纯文本 / JSON / zip）。

这个文件是干嘛的：给「导出」指令和脑内抽屉的「导出」按钮**共用**，
   排版与格式只有这一处定义 —— 两边调的是同一个 `render()`。
  - `bot.py` 的「导出」指令：`render()` 落临时文件 → `send_file` 发回群里 / 私聊
  - `memo_gui.py`（脑内抽屉底部「导出」按钮）：`render()` 落盘后打开文件 / 所在目录

导出内容 = **全部待办**（kind=memo）+ **全部灵感**（kind=idea，含状态与结论），
按时间**正序**（先记的在前），换行原样保留。

🔴 `zip` 格式会**连图片原图一起打包**（`导出.md` + `导出.json` + `images/`）。
   机器人发文件有 ~10MB 上限，超了由调用方降级（见 `too_big_for_qq()`）；
   桌面抽屉导出到本地文件，不受这个限制。

对外只有四个函数：`render()` / `filename()` / `normalize_format()` / `too_big_for_qq()`。
"""
import io
import json
import os
import zipfile
from datetime import datetime

import media
import store

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_FORMAT = "md"
MIME = {
    "md": "text/markdown; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
    "json": "application/json; charset=utf-8",
    "zip": "application/zip",
}

# 🔴 机器人发文件上限约 10MB（平台侧限制，不是我们能调的）。
#    手机照片 2–5MB/张，十几张就超 —— zip 里带原图很容易踩到。
QQ_FILE_LIMIT = 10 * 1024 * 1024

STATUS_LABEL = {
    store.STATUS_OPEN: "未整理",
    store.STATUS_DONE: "已整理",
    # 🔴 decided 与 done 在导出里合并成同一节「已整理」（跟桌面 / 网页同一套
    #    口径，用户 2026-10-12：两种烧脑分法简化成一种；库里 status 不动）
    store.STATUS_DECIDED: "已整理",
    store.STATUS_DROPPED: "已丢弃",
}
STATUS_ORDER = ("未整理", "已整理", "已丢弃")
_STATUS_TO_LABEL = {
    store.STATUS_OPEN: "未整理", store.STATUS_DONE: "已整理",
    store.STATUS_DECIDED: "已整理", store.STATUS_DROPPED: "已丢弃",
}


def normalize_format(fmt):
    """认 md / markdown / txt / text / json / zip（大小写、前导点都无所谓），认不出回默认。"""
    f = (fmt or "").strip().lower().lstrip(".")
    if f in ("md", "markdown"):
        return "md"
    if f in ("txt", "text"):
        return "txt"
    if f == "json":
        return "json"
    if f == "zip":
        return "zip"
    return DEFAULT_FORMAT


def too_big_for_qq(data):
    """这段 bytes 是不是大到机器人发不出去（> 10MB）。

    🔴 抽成函数是为了让调用方**能测**：降级分支不能只靠「真发一次看看」来验。
    """
    return len(data or b"") > QQ_FILE_LIMIT


def human_size(nbytes):
    """体积给人看（跟 media.human 一个口径，这里单独一份免得导出链上多挂一个依赖）。"""
    n = float(nbytes or 0)
    if n < 1024:
        return f"{int(n)} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024 ** 3:.1f} GB"


# 导出包落到本机的哪个目录（发不出去时把那句「文件在哪」告诉用户）。
EXPORT_DIR_NAME = "exports"


def render_to_data(fmt="md", now=None):
    """渲染并**落盘**到数据目录 → (文件名, 绝对路径)。

    给「太大了发不出去」那条降级路径用：包总得有个地方让用户自己去拿。
    """
    from paths import data_dir
    fname, data = render(fmt, now)
    out = os.path.join(data_dir(), EXPORT_DIR_NAME)
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, fname)
    with open(path, "wb") as fh:
        fh.write(data)
    return fname, path


def filename(fmt="md", now=None):
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    return f"摸鱼bot导出_{stamp}.{normalize_format(fmt)}"


def collect():
    """取全部待办 + 灵感，各自按时间**正序**（老的在前）。

    `list_memos` 是倒序、`list_ideas` 是正序 —— 导出统一成正序，读起来才顺。
    """
    memos = list(reversed(store.list_memos(limit=100000, kind=store.KIND_MEMO)))
    ideas = store.list_ideas(limit=100000)
    return memos, ideas


# ── 小工具 ──────────────────────────────────────────────────────
def _when(value):
    """'2026-10-09T20:28:03' → '2026-10-09 20:28'（取不到就原样）。"""
    s = str(value or "")
    return s[:16].replace("T", " ") if len(s) >= 16 else s


def _one_line(text):
    """把多行压成一行（列表项里用），换行换成 ' / '。"""
    parts = [p.strip() for p in str(text or "").splitlines()]
    return " / ".join(p for p in parts if p)


def _quote(text):
    """多行文本 → Markdown 引用块（保留换行）。"""
    body = str(text or "").strip()
    if not body:
        return "> _（空）_"
    return "\n".join("> " + ln if ln.strip() else ">" for ln in body.splitlines())


def _pic_rels(row):
    """条目挂的图片相对路径列表（`images/<yyyyMM>/<name>`）。

    🔴 必须是**相对路径**，不能换成 URL —— zip 解压出来 Markdown 要能直接把图
       显示出来，正文里的引用和包里的文件得指向同一个位置（见 `_render_zip`）。
    """
    out = []
    for rel in (row.get("images") or []):
        if isinstance(rel, str) and rel:
            out.append(rel)
    return out


def _pic_name(rel):
    """`images/202610/ab12.jpg` → `ab12.jpg`（Markdown 的 alt 文本用）。"""
    return rel.rsplit("/", 1)[-1]


# ── 三种格式的渲染 ──────────────────────────────────────────────
def _render_md(memos, ideas, now):
    L = ["# 摸鱼bot 导出", "",
         f"- 导出时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
         f"- 待办 {len(memos)} 条 · 灵感 {len(ideas)} 条", ""]

    L += [f"## 待办（{len(memos)} 条）", ""]
    if not memos:
        L += ["_（还没有待办）_", ""]
    for m in memos:
        L += [f"### {_when(m.get('created_at'))}", "", _quote(m.get("content"))]
        if m.get("note"):
            L += ["", f"> **结论**：{m['note']}"]
        # 图片：写**相对路径**的 Markdown 引用 —— 导 zip 解压后能直接看到图
        for rel in _pic_rels(m):
            L += ["", f"![{_pic_name(rel)}]({rel})"]
        L.append("")

    L += [f"## 灵感（{len(ideas)} 条）", ""]
    if not ideas:
        L += ["_（还没有灵感）_", ""]
    for lab in STATUS_ORDER:
        group = [i for i in ideas
                 if _STATUS_TO_LABEL.get(i.get("status")) == lab]
        if not group:
            continue
        L += [f"### {lab}（{len(group)} 条）", ""]
        for i in group:
            L.append(f"- **{_when(i.get('created_at'))}** {_one_line(i.get('content'))}")
            if i.get("note"):
                L.append(f"  - 结论：{_one_line(i['note'])}")
            for rel in _pic_rels(i):
                L.append(f"  - ![{_pic_name(rel)}]({rel})")
        L.append("")
    return "\n".join(L)


def _render_txt(memos, ideas, now):
    L = ["摸鱼bot 导出", "=" * 40,
         f"导出时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
         f"待办 {len(memos)} 条 · 灵感 {len(ideas)} 条", ""]

    L += [f"── 待办（{len(memos)} 条）" + "─" * 20, ""]
    if not memos:
        L += ["（还没有待办）", ""]
    for m in memos:
        L += [f"[{_when(m.get('created_at'))}]", str(m.get("content") or "").strip()]
        if m.get("note"):
            L.append(f"（结论：{m['note']}）")
        for rel in _pic_rels(m):
            L.append(f"（图片：{rel}）")
        L.append("")

    L += [f"── 灵感（{len(ideas)} 条）" + "─" * 20, ""]
    if not ideas:
        L += ["（还没有灵感）", ""]
    for lab in STATUS_ORDER:
        group = [i for i in ideas
                 if _STATUS_TO_LABEL.get(i.get("status")) == lab]
        if not group:
            continue
        L += [f"· {lab}（{len(group)} 条）", ""]
        for i in group:
            L.append(f"  [{_when(i.get('created_at'))}] {str(i.get('content') or '').strip()}")
            if i.get("note"):
                L.append(f"     结论：{i['note']}")
            for rel in _pic_rels(i):
                L.append(f"     图片：{rel}")
            L.append("")
    return "\n".join(L)


def _render_json(memos, ideas, now):
    def clean(row):
        return {
            "id": row.get("id"),
            "kind": row.get("kind"),
            "content": row.get("content"),
            "status": row.get("status"),
            "note": row.get("note"),
            "tags": row.get("tags") or [],
            # 🔴 相对路径 `images/<yyyyMM>/<name>` —— 跟 zip 里的目录结构一一对应，
            #    解压之后照着这个就能把正文和图片对起来。
            "images": row.get("images") or [],
            "created_at": row.get("created_at"),
            "processed_at": row.get("processed_at"),
            "group_name": row.get("group_name"),
        }

    payload = {
        "exported_at": now.isoformat(timespec="seconds"),
        "counts": {"memo": len(memos), "idea": len(ideas)},
        "memos": [clean(r) for r in memos],
        "ideas": [clean(r) for r in ideas],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ── zip：文本 + 原图一起打包 ─────────────────────────────────────
# 🔴 图片放 `images/<yyyyMM>/<name>`（跟磁盘上一样），md / json 里记的也是这个
#    相对路径 —— 解压出来 Markdown 里的引用能直接对上，不用改链接。
def _render_zip(memos, ideas, now):
    """→ bytes。标准库 `zipfile`，不引新依赖。

    压缩用 `ZIP_DEFLATED`：图片（jpg/png）本身压不动多少，但 md/json 能压，
    而且**不压缩的包会白白多占几 MB** —— 那几 MB 在 10MB 上限面前很要命。
    """
    md = _render_md(memos, ideas, now).encode("utf-8")
    js = _render_json(memos, ideas, now).encode("utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("导出.md", md)
        zf.writestr("导出.json", js)
        # 收进来的原图。以**磁盘**为准（数据库里可能删了行但文件还在，
        # 也可能库坏了但图还在 —— 导出要给用户的是「我到底有哪些图」）。
        for item in media.list_files():
            rel = item.get("rel") or ""
            parts = rel.split("/")
            if len(parts) != 3 or parts[0] != "images":
                continue
            mon, name = parts[1], parts[2]
            fp = media.resolve_path(mon, name)
            if not fp:
                continue
            try:
                with open(fp, "rb") as fh:
                    zf.write(fp, arcname=rel)
            except OSError:
                continue        # 单张读不到不该让整包导出失败
        # 收图关着的用户没有 images/，给个说明文件免得解压出来一头雾水
        if not media.list_files():
            zf.writestr("images/说明.txt", "这次导出没有图片。\n")
    return buf.getvalue()


# ── 对外入口 ────────────────────────────────────────────────────
def render(fmt="md", now=None):
    """→ (文件名, bytes)。两种触发方式都走这里，格式只有这一份定义。"""
    fmt = normalize_format(fmt)
    now = now or datetime.now()
    memos, ideas = collect()
    if fmt == "json":
        text = _render_json(memos, ideas, now)
        data = text.encode("utf-8")
    elif fmt == "txt":
        text = _render_txt(memos, ideas, now)
        data = text.encode("utf-8")
    elif fmt == "zip":
        data = _render_zip(memos, ideas, now)
    else:
        text = _render_md(memos, ideas, now)
        data = text.encode("utf-8")
    return filename(fmt, now), data
