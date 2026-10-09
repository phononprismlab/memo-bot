"""本地存储层：群备忘 / 灵感落 SQLite（单文件，本机任何程序可直接读）。

两条流：
  kind='memo' —— 明确标记的备忘（@bot 发「备忘 xxx」，或「备忘」前缀）
  kind='idea' —— 群里非 @ 的普通文本，自动收集为灵感

生产替换：把这里换成 CloudBase PostgreSQL 的 bot_memos 表即可，接口不变。
"""
import os
import sqlite3
from datetime import datetime
from uuid import uuid4

DB = os.path.join(os.path.dirname(__file__), "memos.db")

KIND_MEMO = "memo"
KIND_IDEA = "idea"

# 灵感处理状态（只有 kind='idea' 会用到）
STATUS_OPEN = "open"          # 待处理
STATUS_DONE = "done"          # 已处理（产生了行动并完成）
STATUS_DECIDED = "decided"    # 已决议（有结论，但不产生行动）
STATUS_DROPPED = "dropped"    # 已丢弃（不成立/无效）


def init():
    con = sqlite3.connect(DB)
    con.execute(
        """CREATE TABLE IF NOT EXISTS memos (
            id           TEXT PRIMARY KEY,
            group_id     TEXT,
            group_name   TEXT,
            sender_id    TEXT,
            sender_name  TEXT,
            content      TEXT,
            created_at   TEXT,
            kind         TEXT DEFAULT 'memo',
            status       TEXT DEFAULT 'open',
            note         TEXT,
            processed_at TEXT
        )"""
    )
    # 老库迁移：逐列补齐
    cols = [r[1] for r in con.execute("PRAGMA table_info(memos)").fetchall()]
    if "kind" not in cols:
        con.execute("ALTER TABLE memos ADD COLUMN kind TEXT DEFAULT 'memo'")
        con.execute("UPDATE memos SET kind='memo' WHERE kind IS NULL")
    if "status" not in cols:
        con.execute("ALTER TABLE memos ADD COLUMN status TEXT DEFAULT 'open'")
        con.execute("UPDATE memos SET status='open' WHERE status IS NULL")
    if "note" not in cols:
        con.execute("ALTER TABLE memos ADD COLUMN note TEXT")
    if "processed_at" not in cols:
        con.execute("ALTER TABLE memos ADD COLUMN processed_at TEXT")
    con.commit()
    con.close()


def add_memo(group_id, group_name, sender_id, sender_name, content, kind=KIND_MEMO):
    memo_id = str(uuid4())
    created = datetime.now().isoformat(timespec="seconds")
    con = sqlite3.connect(DB)
    con.execute(
        "INSERT INTO memos (id,group_id,group_name,sender_id,sender_name,content,created_at,kind)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (memo_id, group_id, group_name, sender_id, sender_name, content, created, kind),
    )
    con.commit()
    con.close()
    return {"id": memo_id, "content": content, "created_at": created, "kind": kind}


def list_memos(limit=50, kind=None):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    if kind:
        rows = con.execute(
            "SELECT * FROM memos WHERE kind=? ORDER BY created_at DESC LIMIT ?",
            (kind, limit),
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT * FROM memos ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def count_memos(kind=None):
    con = sqlite3.connect(DB)
    if kind:
        n = con.execute("SELECT COUNT(*) FROM memos WHERE kind=?", (kind,)).fetchone()[0]
    else:
        n = con.execute("SELECT COUNT(*) FROM memos").fetchone()[0]
    con.close()
    return n


# ── 灵感池（供「处理灵感」的会话使用）─────────────────────────────

def list_ideas(status=None, limit=500, newest_first=False):
    """列出灵感。status 为 None 时列出全部。

    默认按时间**正序**（先记的先处理，供导出灵感池）；`newest_first=True` 取最近的。
    """
    order = "DESC" if newest_first else "ASC"
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    if status:
        rows = con.execute(
            f"SELECT * FROM memos WHERE kind=? AND status=? ORDER BY created_at {order} LIMIT ?",
            (KIND_IDEA, status, limit),
        ).fetchall()
    else:
        rows = con.execute(
            f"SELECT * FROM memos WHERE kind=? ORDER BY created_at {order} LIMIT ?",
            (KIND_IDEA, limit),
        ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def idea_stats():
    """灵感计数：{'open','done','decided','dropped','total'}"""
    con = sqlite3.connect(DB)
    out = {}
    for s in (STATUS_OPEN, STATUS_DONE, STATUS_DECIDED, STATUS_DROPPED):
        out[s] = con.execute(
            "SELECT COUNT(*) FROM memos WHERE kind=? AND status=?", (KIND_IDEA, s)
        ).fetchone()[0]
    con.close()
    out["total"] = sum(out.values())
    return out


def find_idea(id_or_prefix):
    """按完整 id 或 id 前缀找一条灵感（前缀唯一时命中）。"""
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT * FROM memos WHERE id=? AND kind=?", (id_or_prefix, KIND_IDEA)
    ).fetchone()
    if row is None:
        rows = con.execute(
            "SELECT * FROM memos WHERE id LIKE ? AND kind=? LIMIT 2",
            (id_or_prefix + "%", KIND_IDEA),
        ).fetchall()
        row = rows[0] if len(rows) == 1 else None
    con.close()
    return dict(row) if row else None


def set_idea_status(ids, status, note=None):
    """批量改灵感状态。ids 为完整 id 列表。返回实际改动条数。"""
    if not ids:
        return 0
    now = datetime.now().isoformat(timespec="seconds")
    con = sqlite3.connect(DB)
    n = 0
    for i in ids:
        if note is None:
            cur = con.execute(
                "UPDATE memos SET status=?, processed_at=? WHERE id=? AND kind=?",
                (status, now, i, KIND_IDEA),
            )
        else:
            cur = con.execute(
                "UPDATE memos SET status=?, note=?, processed_at=? WHERE id=? AND kind=?",
                (status, note, now, i, KIND_IDEA),
            )
        n += cur.rowcount
    con.commit()
    con.close()
    return n
