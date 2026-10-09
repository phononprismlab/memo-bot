"""灵感池入口：把群里自动收集的灵感导出 / 列出 / 标记处理结果。

给「专门处理灵感的会话」用。典型流程：
  1. 新会话开场先跑  python memo-bot/ideas.py --export
     → 在工作区生成 灵感池.md（只含「待处理」的，逐条编号）
  2. 会话里逐条讨论 / 决定做还是不做
  3. 处理完回写     python memo-bot/ideas.py --done 1,3 --note "已做成 X"
                    python memo-bot/ideas.py --decide 2 --note "结论：定位如此，不做 X"
                    python memo-bot/ideas.py --drop 4 --note "无效/重复"

四种状态：
  open     待处理
  done     已处理（产生了行动并完成）
  decided  已决议（有结论，但不产生行动）
  dropped  已丢弃（不成立 / 无效）

序号（1,2,3…）是相对「当前待处理列表」的 1-based 位置，每次 --export 后固定。
也支持传 id 前缀（前 8 位即可）。

命令：
  --export [路径]   导出待处理灵感为 Markdown（默认 工作区/灵感池.md）
  --list            终端列出（默认只看待处理；--all 看全部）
  --done IDS        标记为已处理
  --decide IDS      标记为已决议（有结论、无行动）
  --drop IDS        标记为已丢弃
  --note TEXT       给本次标记附一条结论
  --stats           只看计数
"""
import os
import re
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import store  # noqa: E402

DEFAULT_EXPORT = os.path.join(os.path.dirname(HERE), "灵感池.md")
# 群里 @机器人 的占位符（<C9> 之类）落库时会带进来，导出时清掉
_NOISE = re.compile(r"<[^\s<>]{1,40}>")


def clean(text):
    return re.sub(r"[ \t]+", " ", _NOISE.sub(" ", text or "")).strip()


def fmt_ts(s):
    return (s or "").replace("T", " ")


def parse_ids(arg):
    """把 '1,3' 或 'ab12cd34' 解析成完整 id 列表（数字走序号，其余走 id 前缀）。"""
    opens = store.list_ideas(store.STATUS_OPEN)
    out, bad = [], []
    for tok in [t.strip() for t in (arg or "").split(",") if t.strip()]:
        if tok.isdigit():
            i = int(tok)
            if 1 <= i <= len(opens):
                out.append(opens[i - 1]["id"])
            else:
                bad.append(tok)
        else:
            hit = store.find_idea(tok)
            if hit:
                out.append(hit["id"])
            else:
                bad.append(tok)
    return out, bad


def do_export(path):
    opens = store.list_ideas(store.STATUS_OPEN)
    done = store.list_ideas(store.STATUS_DONE)
    decided = store.list_ideas(store.STATUS_DECIDED)
    dropped = store.list_ideas(store.STATUS_DROPPED)
    st = store.idea_stats()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    L = [
        "# 灵感池",
        "",
        f"> 群聊自动收集 · 导出 {now}",
        f"> 待处理 **{st['open']}** / 已处理 {st['done']} / 已决议 {st['decided']} / 已丢弃 {st['dropped']}",
        ">",
        "> 回写：`--done 序号 --note \"已做\"` / `--decide 序号 --note \"结论\"` / `--drop 序号`",
        "",
        f"## 待处理（{len(opens)}）",
        "",
    ]
    if not opens:
        L += ["（没有待处理的灵感）", ""]
    for i, m in enumerate(opens, 1):
        L += [
            f"### {i} · {fmt_ts(m['created_at'])}",
            "",
            clean(m["content"]),
            "",
        ]

    if done or decided or dropped:
        L += ["---", "", "## 已归档", ""]
        for m in done:
            note = f" → {clean(m['note'])}" if m.get("note") else ""
            L.append(f"- [x] {fmt_ts(m['created_at'])} · {clean(m['content'])}{note}")
        for m in decided:
            note = f" → {clean(m['note'])}" if m.get("note") else ""
            L.append(f"- [~] {fmt_ts(m['created_at'])} · {clean(m['content'])}{note}")
        for m in dropped:
            note = f" → {clean(m['note'])}" if m.get("note") else ""
            L.append(f"- [-] {fmt_ts(m['created_at'])} · {clean(m['content'])}{note}")
        L.append("")
        L += [
            "> 标记说明：`[x]` 已处理（有行动）· `[~]` 已决议（有结论、无行动）· `[-]` 已丢弃",
            "",
        ]

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    return path, len(opens)


def main():
    args = sys.argv[1:]
    store.init()

    if not args or "--help" in args or "-h" in args:
        print(__doc__)
        return

    if "--stats" in args:
        st = store.idea_stats()
        print(
            f"待处理 {st['open']} / 已处理 {st['done']} / 已决议 {st['decided']}"
            f" / 已丢弃 {st['dropped']} / 共 {st['total']}"
        )
        return

    if "--list" in args:
        status = None if "--all" in args else store.STATUS_OPEN
        rows = store.list_ideas(status)
        label = "全部" if status is None else "待处理"
        print(f"=== 灵感（{label}，{len(rows)} 条）===")
        for i, m in enumerate(rows, 1):
            mark = {"open": " ", "done": "x", "decided": "~", "dropped": "-"}.get(
                m.get("status"), " "
            )
            print(f"[{mark}] {i:>2}. {fmt_ts(m['created_at'])}  {clean(m['content'])[:70]}")
            if m.get("note"):
                print(f"           └ {clean(m['note'])}")
        return

    for key, status in (
        ("--done", store.STATUS_DONE),
        ("--decide", store.STATUS_DECIDED),
        ("--drop", store.STATUS_DROPPED),
    ):
        if key not in args:
            continue
        idx = args.index(key)
        arg = args[idx + 1] if idx + 1 < len(args) else ""
        note = args[args.index("--note") + 1] if "--note" in args else None
        ids, bad = parse_ids(arg)
        n = store.set_idea_status(ids, status, note)
        print(f"已标记 {n} 条为 {status}" + (f"（无法解析：{bad}）" if bad else ""))
        st = store.idea_stats()
        print(
            f"当前：待处理 {st['open']} / 已处理 {st['done']} / 已决议 {st['decided']}"
            f" / 已丢弃 {st['dropped']}"
        )
        return

    if "--export" in args:
        idx = args.index("--export")
        path = args[idx + 1] if idx + 1 < len(args) and not args[idx + 1].startswith("-") else DEFAULT_EXPORT
        p, n = do_export(path)
        print(f"已导出 {n} 条待处理灵感 → {p}")
        return

    print(f"未知参数：{args}\n")
    print(__doc__)


if __name__ == "__main__":
    main()
