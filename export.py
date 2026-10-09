"""把备忘 / 灵感导出成一个文件（Markdown / 纯文本 / JSON）。

这个文件是干嘛的：给「导出」指令和脑内抽屉页的「⬇ 导出」按钮**共用**，
   排版与格式只有这一处定义 —— 两边调的是同一个 `render()`。
  - `bot.py` 的「导出」指令：`render()` 落临时文件 → `send_file` 发回群里 / 私聊
  - `brain-drawer.py` 的 `GET /api/export`：`render()` 直接当下载流吐给浏览器

导出内容 = **全部备忘**（kind=memo）+ **全部灵感**（kind=idea，含状态与结论），
按时间**正序**（先记的在前），换行原样保留。

对外只有三个函数：`render()` / `filename()` / `normalize_format()`。
"""
import json
import os
from datetime import datetime

import store

HERE = os.path.dirname(os.path.abspath(__file__))

FORMATS = ("md", "txt", "json")
DEFAULT_FORMAT = "md"
MIME = {
    "md": "text/markdown; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
    "json": "application/json; charset=utf-8",
}

STATUS_LABEL = {
    store.STATUS_OPEN: "待处理",
    store.STATUS_DONE: "已处理",
    store.STATUS_DECIDED: "已决议",
    store.STATUS_DROPPED: "已丢弃",
}
STATUS_ORDER = (store.STATUS_OPEN, store.STATUS_DONE,
                store.STATUS_DECIDED, store.STATUS_DROPPED)


def normalize_format(fmt):
    """认 md / markdown / txt / text / json（大小写、前导点都无所谓），认不出回默认。"""
    f = (fmt or "").strip().lower().lstrip(".")
    if f in ("md", "markdown"):
        return "md"
    if f in ("txt", "text"):
        return "txt"
    if f == "json":
        return "json"
    return DEFAULT_FORMAT


def filename(fmt="md", now=None):
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    return f"摸鱼bot导出_{stamp}.{normalize_format(fmt)}"


def collect():
    """取全部备忘 + 灵感，各自按时间**正序**（老的在前）。

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


# ── 三种格式的渲染 ──────────────────────────────────────────────
def _render_md(memos, ideas, now):
    L = ["# 摸鱼bot 导出", "",
         f"- 导出时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
         f"- 备忘 {len(memos)} 条 · 灵感 {len(ideas)} 条", ""]

    L += [f"## 备忘（{len(memos)} 条）", ""]
    if not memos:
        L += ["_（还没有备忘）_", ""]
    for m in memos:
        L += [f"### {_when(m.get('created_at'))}", "", _quote(m.get("content"))]
        if m.get("note"):
            L += ["", f"> **结论**：{m['note']}"]
        L.append("")

    L += [f"## 灵感（{len(ideas)} 条）", ""]
    if not ideas:
        L += ["_（还没有灵感）_", ""]
    for st in STATUS_ORDER:
        group = [i for i in ideas if i.get("status") == st]
        if not group:
            continue
        L += [f"### {STATUS_LABEL.get(st, st)}（{len(group)} 条）", ""]
        for i in group:
            L.append(f"- **{_when(i.get('created_at'))}** {_one_line(i.get('content'))}")
            if i.get("note"):
                L.append(f"  - 结论：{_one_line(i['note'])}")
        L.append("")
    return "\n".join(L)


def _render_txt(memos, ideas, now):
    L = ["摸鱼bot 导出", "=" * 40,
         f"导出时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
         f"备忘 {len(memos)} 条 · 灵感 {len(ideas)} 条", ""]

    L += [f"── 备忘（{len(memos)} 条）" + "─" * 20, ""]
    if not memos:
        L += ["（还没有备忘）", ""]
    for m in memos:
        L += [f"[{_when(m.get('created_at'))}]", str(m.get("content") or "").strip()]
        if m.get("note"):
            L.append(f"（结论：{m['note']}）")
        L.append("")

    L += [f"── 灵感（{len(ideas)} 条）" + "─" * 20, ""]
    if not ideas:
        L += ["（还没有灵感）", ""]
    for st in STATUS_ORDER:
        group = [i for i in ideas if i.get("status") == st]
        if not group:
            continue
        L += [f"· {STATUS_LABEL.get(st, st)}（{len(group)} 条）", ""]
        for i in group:
            L.append(f"  [{_when(i.get('created_at'))}] {str(i.get('content') or '').strip()}")
            if i.get("note"):
                L.append(f"     结论：{i['note']}")
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


# ── 对外入口 ────────────────────────────────────────────────────
def render(fmt="md", now=None):
    """→ (文件名, bytes)。两种触发方式都走这里，格式只有这一份定义。"""
    fmt = normalize_format(fmt)
    now = now or datetime.now()
    memos, ideas = collect()
    if fmt == "json":
        text = _render_json(memos, ideas, now)
    elif fmt == "txt":
        text = _render_txt(memos, ideas, now)
    else:
        text = _render_md(memos, ideas, now)
    return filename(fmt, now), text.encode("utf-8")
