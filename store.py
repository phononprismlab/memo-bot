"""本地存储层：群备忘 / 灵感落 SQLite（单文件，本机任何程序可直接读）。

两条流：
  kind='memo' —— 明确标记的备忘（@bot 发「备忘 xxx」，或「备忘」前缀）
  kind='idea' —— 群里非 @ 的普通文本，自动收集为灵感

🔴 库**第一次建出来**时会塞几条示例（`SEED_ITEMS`）—— 三条灵感介绍页面能干什么、
   一条备忘介绍怎么用摸鱼bot，免得新用户打开脑内抽屉看到一片空白。
   老库不塞；`MEMO_NO_SEED=1` 可关。

生产替换：把这里换成 CloudBase PostgreSQL 的 bot_memos 表即可，接口不变。
"""
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta
from uuid import uuid4

from paths import data_path

# 🔴 memos.db 是**可写数据** —— 安装版由 MEMO_DATA_DIR 指到用户目录（见 paths.py）
DB = data_path("memos.db")

KIND_MEMO = "memo"
KIND_IDEA = "idea"

# 记录状态。灵感（kind='idea'）四态都用；备忘（kind='memo'）只用 open / done / dropped
# （没有「已决议」—— 备忘是「我要做」，要么做完，要么不做了）。
STATUS_OPEN = "open"          # 待处理 / 没完成
STATUS_DONE = "done"          # 已处理 / 已完成
STATUS_DECIDED = "decided"    # 已决议（有结论，但不产生行动）—— 只有灵感用
STATUS_DROPPED = "dropped"    # 已丢弃（不成立 / 无效 / 不做了）

# ── 标签 ──────────────────────────────────────────────────────
# 卡片自己的分类。字典表 `tags` 存「有哪些标签 + 各是什么颜色」，
# 每条记录的标签名数组存在 `memos.tags`（JSON 列）。
# 🔴 不建关联表：单机 SQLite、量级小，沿用逐列 ALTER 的迁移风格就够。
TAG_NAME_MAX = 20             # 标签名 trim 后最长几个字（1 是下限：空的不许打）
DEFAULT_TAG_COLOR = "#4fd1c5"

# ── 首次使用：塞几条示例，别让新用户打开抽屉看到一片空白 ──────────────
# 三条灵感介绍页面能干什么，一条备忘介绍怎么用摸鱼bot。
# 🔴 **只在 `memos` 表第一次建出来的时候塞**（判据是建表前它还不存在）——
#    老库绝不塞：用户可能已经把示例处理掉了，不能又给他塞回来。
# 🔴 设 `MEMO_NO_SEED=1` 可以关掉（测试、以及想要空库的人）。
# 这些就是普通记录：能编辑、能完成、能丢弃、能永久删除，和用户自己记的没区别。
SEED_ITEMS = (
    (KIND_IDEA, "群里随手发一句，我就悄悄收成灵感 —— 不用 @ 我"),
    (KIND_IDEA, "看到卡片点一下就能处理：已处理 / 已决议 / 丢弃，网页上直接生效"),
    (KIND_IDEA, "发「回旋镖」能把很久以前记的东西翻回来，别让它们烂在库里"),
    (KIND_MEMO, "在群里发「摸鱼」，随机回你一条摸鱼文案"),
)


def _seed(con):
    """把示例内容写进刚建好的空库。返回塞了几条。"""
    base = datetime.now()
    n = len(SEED_ITEMS)
    for idx, (kind, content) in enumerate(SEED_ITEMS):
        # 时间各错开一秒 —— 列表和导出里有个自然先后（同一秒排序不稳定）
        created = (base - timedelta(seconds=n - idx)).isoformat(timespec="seconds")
        con.execute(
            "INSERT INTO memos (id,group_id,group_name,sender_id,sender_name,content,created_at,kind)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (str(uuid4()), "", "示例", "", "摸鱼bot", content, created, kind),
        )
    return n


def init():
    con = sqlite3.connect(DB)
    # 🔴 建表**之前**先看表在不在 —— 这就是「是不是第一次用」的唯一判据。
    #    用「表是空的」当判据不行：用户把示例删光了，再启动就又给他塞回来。
    fresh = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memos'"
    ).fetchone() is None
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
    # 标签字典：有哪些标签、各是什么颜色。条目侧的标签名数组在 memos.tags 里。
    con.execute(
        """CREATE TABLE IF NOT EXISTS tags (
            name       TEXT PRIMARY KEY,
            color      TEXT,
            created_at TEXT
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
    if "boomerang_at" not in cols:
        # 回旋镖推过这条没有（非空 = 推过）—— 避免同一个用户反复收到同一条旧记录
        con.execute("ALTER TABLE memos ADD COLUMN boomerang_at TEXT")
    if "tags" not in cols:
        # 标签名数组的 JSON。🔴 老行是 NULL —— 读的时候一律当空数组（见 _row_tags）
        con.execute("ALTER TABLE memos ADD COLUMN tags TEXT")
    if "images" not in cols:
        # 本地图片相对路径数组的 JSON（`images/202610/<sha16>.jpg`）。
        # 🔴 只存**路径**不存二进制 —— 库一直很轻，导出也干净。
        #    老行是 NULL，读的时候一律当空数组（见 _row_images）。
        con.execute("ALTER TABLE memos ADD COLUMN images TEXT")
    # 全新的库才塞示例（老库 fresh=False，一个字节都不动）
    if fresh and not os.environ.get("MEMO_NO_SEED"):
        _seed(con)
    con.commit()
    con.close()


def add_memo(group_id, group_name, sender_id, sender_name, content, kind=KIND_MEMO, tags=None,
             images=None):
    memo_id = str(uuid4())
    created = datetime.now().isoformat(timespec="seconds")
    names = _clean_tags(tags)
    pics = _clean_images(images)
    con = sqlite3.connect(DB)
    con.execute(
        "INSERT INTO memos (id,group_id,group_name,sender_id,sender_name,content,created_at,kind,tags,images)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            memo_id, group_id, group_name, sender_id, sender_name, content, created,
            kind, json.dumps(names, ensure_ascii=False) if names else None,
            json.dumps(pics, ensure_ascii=False) if pics else None,
        ),
    )
    con.commit()
    con.close()
    # 🔴 顺手把这些标签补进字典表 —— 否则打了一个新标签，配置页的标签条里却看不到
    if names:
        tags_ensure(names)
    return {"id": memo_id, "content": content, "created_at": created, "kind": kind,
            "tags": names, "images": pics}


# ── 图片：只存相对路径 ───────────────────────────────────────────
# 二进制在 `media.py` 落盘，这里只记「这条挂了哪几张」。
# 🔴 路径写法固定成 `images/<yyyyMM>/<sha16>.<ext>` —— 认不出这个形状的一律丢弃，
#    否则脏数据会顺着 `/img/` 路由去摸文件（穿越风险）。

_IMG_RE = re.compile(r"^images/\d{6}/[0-9a-f]{16}\.(jpg|png|gif|webp)$")


def _clean_images(images):
    """过滤出合法形状的相对路径，保序去重。不合规的**静默丢弃**（同标签的处理）。"""
    out, seen = [], set()
    for p in images or []:
        s = str(p or "").strip().replace("\\", "/")
        if not s or s in seen or not _IMG_RE.match(s):
            continue
        seen.add(s)
        out.append(s)
    return out


def _row_images(v):
    """把 memos.images 列读成数组。🔴 NULL / 坏 JSON / 不是数组 → 一律当空数组。"""
    if not v:
        return []
    try:
        arr = json.loads(v)
    except (ValueError, TypeError):
        return []
    return _clean_images(arr) if isinstance(arr, list) else []


# ── 标签：字典 + 条目侧 ──────────────────────────────────────────

def _clean_tags(tags):
    """把乱七八糟的输入收成一份干净的标签名数组。顺序保留，重复去掉。

    🔴 规则：trim 后非空、长度 ≤ TAG_NAME_MAX、同名只留一个。
       不合规的**直接丢弃**（不报错）—— 条目侧的标签是「顺手打一下」，
       为了一个打错的名字把整次操作失败掉太粗暴。
    """
    out, seen = [], set()
    for t in tags or []:
        name = str(t).strip()
        if not name or len(name) > TAG_NAME_MAX or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _row_tags(v):
    """把 memos.tags 列读成数组。🔴 NULL / 坏 JSON / 不是数组 → 一律当空数组。"""
    if not v:
        return []
    try:
        arr = json.loads(v)
    except (ValueError, TypeError):
        return []
    return [str(x) for x in arr] if isinstance(arr, list) else []


def _with_lists(row):
    """给查出来的行补上解析好的 `tags` / `images` 数组（JSON 字符串 → list）。"""
    row["tags"] = _row_tags(row.get("tags"))
    row["images"] = _row_images(row.get("images"))
    return row


def tags_all():
    """标签字典 + 每个标签挂了几条。按名字排，返回 [{name,color,count}]。

    count 只数**还没被删的**行 —— 字典里的标签可能一条都没挂（用户刚建还没用），
    这时 count=0，UI 上仍然要显示，方便他继续用。
    """
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    dict_rows = con.execute("SELECT name, color, created_at FROM tags ORDER BY name").fetchall()
    out = []
    for r in dict_rows:
        name = r["name"]
        # 🔴 用 LIKE 粗筛再精筛：tags 是 JSON 数组，'%"名字"%' 可能有子串误命中
        #    （"生活" vs "生活用品"），所以捞出来在 Python 侧按数组精确比对。
        cand = con.execute(
            "SELECT tags FROM memos WHERE tags LIKE ?", ('%"' + name + '"%',)
        ).fetchall()
        n = sum(1 for c in cand if name in _row_tags(c["tags"]))
        out.append({"name": name, "color": r["color"] or DEFAULT_TAG_COLOR, "count": n})
    con.close()
    return out


def tags_ensure(names):
    """把还没进字典的标签名补进去（用默认色）。返回新增了几个。"""
    names = _clean_tags(names)
    if not names:
        return 0
    now = datetime.now().isoformat(timespec="seconds")
    con = sqlite3.connect(DB)
    n = 0
    for name in names:
        cur = con.execute(
            "INSERT OR IGNORE INTO tags (name,color,created_at) VALUES (?,?,?)",
            (name, DEFAULT_TAG_COLOR, now),
        )
        n += cur.rowcount
    con.commit()
    con.close()
    return n


def set_item_tags(item_id, names):
    """**覆盖**某条记录的标签。页面上打标签 / 去掉标签都走这里。返回改动条数。

    🔴 顺带把新标签补进字典表；但**不删**字典里已经没人用的标签
       —— 用户可能是先把标签从条目上摘下来，待会儿还要打回去。
       真要清字典走 tags_delete()。
    """
    if not item_id:
        return 0
    clean = _clean_tags(names)
    con = sqlite3.connect(DB)
    cur = con.execute(
        "UPDATE memos SET tags=? WHERE id=?",
        (json.dumps(clean, ensure_ascii=False) if clean else None, item_id),
    )
    n = cur.rowcount
    con.commit()
    con.close()
    if clean:
        tags_ensure(clean)
    return n


def set_item_images(item_id, paths):
    """**覆盖**某条记录的图片路径。返回改动条数。

    为什么要「先入库再补」：正文和图片是两步（图要下载，慢），
    但正文不能等下载完才落库 —— 万一下载卡住，用户那条备忘就丢了。
    所以先写正文、拿到 id，再把图挂上去。
    """
    if not item_id:
        return 0
    clean = _clean_images(paths)
    con = sqlite3.connect(DB)
    cur = con.execute(
        "UPDATE memos SET images=? WHERE id=?",
        (json.dumps(clean, ensure_ascii=False) if clean else None, item_id),
    )
    n = cur.rowcount
    con.commit()
    con.close()
    return n


def tags_rename(old, new):
    """标签改名。返回 (改动条数, 错误消息)。错误消息为 None 表示成功。

    🔴 「全表替换」在**数组维度**做 —— 逐行读出来、map 一遍、写回去。
       不能直接 SQL 字符串替换：`["生活"]` 换成 `["日常"]` 时字符串替换也行，
       但 `["生活","生活用品"]` 换 `生活` 就会把 `生活用品` 也改坏。
    🔴 目标名已存在时**合并**：把 old 从数组里摘掉，new 本来就在的话不重复加。
    """
    old = str(old or "").strip()
    new = str(new or "").strip()
    if not old or not new:
        return 0, "标签名不能为空"
    if len(new) > TAG_NAME_MAX:
        return 0, f"标签名最多 {TAG_NAME_MAX} 个字"
    if old == new:
        return 0, None
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    exists = con.execute("SELECT 1 FROM tags WHERE name=?", (old,)).fetchone()
    if not exists:
        con.close()
        return 0, f"没有「{old}」这个标签"
    n = 0
    rows = con.execute("SELECT id, tags FROM memos WHERE tags LIKE ?", ('%"' + old + '"%',)).fetchall()
    for r in rows:
        cur_tags = _row_tags(r["tags"])
        if old not in cur_tags:
            continue
        mapped = []
        for t in cur_tags:
            v = new if t == old else t
            if v not in mapped:
                mapped.append(v)
        con.execute(
            "UPDATE memos SET tags=? WHERE id=?",
            (json.dumps(mapped, ensure_ascii=False) if mapped else None, r["id"]),
        )
        n += 1
    # 字典：old 摘掉，new 补上（保留 old 原来的配色）
    color = None
    crow = con.execute("SELECT color FROM tags WHERE name=?", (old,)).fetchone()
    if crow:
        color = crow["color"]
    con.execute("DELETE FROM tags WHERE name=?", (old,))
    con.execute(
        "INSERT OR IGNORE INTO tags (name,color,created_at) VALUES (?,?,?)",
        (new, color or DEFAULT_TAG_COLOR, datetime.now().isoformat(timespec="seconds")),
    )
    con.commit()
    con.close()
    return n, None


def tags_delete(name):
    """删标签：字典里摘掉 + 所有记录上摘掉。返回摘了几条记录。"""
    name = str(name or "").strip()
    if not name:
        return 0
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    n = 0
    rows = con.execute("SELECT id, tags FROM memos WHERE tags LIKE ?", ('%"' + name + '"%',)).fetchall()
    for r in rows:
        cur_tags = _row_tags(r["tags"])
        if name not in cur_tags:
            continue
        left = [t for t in cur_tags if t != name]
        con.execute(
            "UPDATE memos SET tags=? WHERE id=?",
            (json.dumps(left, ensure_ascii=False) if left else None, r["id"]),
        )
        n += 1
    con.execute("DELETE FROM tags WHERE name=?", (name,))
    con.commit()
    con.close()
    return n


def tags_set_color(name, color):
    """改标签配色。返回改动条数（0 = 没这个标签）。"""
    name = str(name or "").strip()
    color = str(color or "").strip()
    # 只收 #rgb / #rrggbb —— 这串颜色最后会塞进 style，不给任意字符串留口子
    if not name or not _is_hex_color(color):
        return 0
    con = sqlite3.connect(DB)
    cur = con.execute("UPDATE tags SET color=? WHERE name=?", (color, name))
    n = cur.rowcount
    con.commit()
    con.close()
    return n


def _is_hex_color(s):
    """#abc / #aabbcc 才算合法颜色。"""
    if not isinstance(s, str) or not s.startswith("#") or len(s) not in (4, 7):
        return False
    return all(c in "0123456789abcdefABCDEF" for c in s[1:])


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
    return [_with_lists(dict(r)) for r in rows]


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
    return [_with_lists(dict(r)) for r in rows]


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
    return _with_lists(dict(row)) if row else None


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


# ── 备忘：没完成 / 已完成 / 已丢弃 三态 ──────────────────────────
# 备忘 = 我要做（kind='memo'），复用 status 列：
#   open = 没完成，done = 已完成，dropped = 已丢弃（不做了 / 作废）。
# 🔴 「已完成」和「已丢弃」都**不是删除**：行还在表里，随时能取消 / 恢复。
#    真要抹掉得走 delete_items()（页面上「已丢弃」列表里的「永久删除」）。
# processed_at 记的是最后一次改状态的时间。

MEMO_STATUSES = (STATUS_OPEN, STATUS_DONE, STATUS_DROPPED)


def set_memo_status(ids, status):
    """批量改备忘状态（open / done / dropped）。返回实际改动条数。

    🔴 `kind='memo'` 过滤在 SQL 里 —— 传进来灵感 id 也不会被误改。
    """
    if not ids or status not in MEMO_STATUSES:
        return 0
    now = datetime.now().isoformat(timespec="seconds")
    con = sqlite3.connect(DB)
    n = 0
    for i in ids:
        cur = con.execute(
            "UPDATE memos SET status=?, processed_at=? WHERE id=? AND kind=?",
            (status, now, i, KIND_MEMO),
        )
        n += cur.rowcount
    con.commit()
    con.close()
    return n


def set_memo_done(ids, done=True):
    """批量完成 / 取消完成备忘。返回实际改动条数。

    保留这个签名是因为老调用方（离线操作文件 `抽屉操作.json` 里的 `{"done": true}`、
    老测试）还在用 —— 内部就是 `set_memo_status`。
    """
    return set_memo_status(ids, STATUS_DONE if done else STATUS_OPEN)


def delete_items(ids):
    """**永久删除**这些记录（真删行，不可恢复）。备忘和灵感都能删。返回删除条数。

    🔴 只给「已丢弃」列表的「永久删除」用。页面侧必须先弹确认框 ——
       这一层不做任何状态检查，调用方负责确认。
    """
    ids = [i for i in (ids or []) if i]
    if not ids:
        return 0
    con = sqlite3.connect(DB)
    marks = ",".join("?" * len(ids))
    cur = con.execute(f"DELETE FROM memos WHERE id IN ({marks})", ids)
    n = cur.rowcount
    con.commit()
    con.close()
    return n


# ── ✏️ 再次编辑正文 ────────────────────────────────────────────
# 回顾的时候发现写错了字、或者对某件事有了新想法 —— 改正文就行。
# 🔴 这是用户明确要求：**创建时间戳（created_at）保持不变**，时间轴不会乱。

def update_content(item_id, content):
    """**只改正文**，不碰创建时间。页面上「✏️ 编辑」走这里。

    🔴 备忘和灵感都能改（不按 kind 过滤）。返回实际改动条数（id 不存在 → 0）。
    🔴 SQL 里**没有 created_at** —— 这是这个函数存在的全部意义。
    """
    if not item_id or content is None:
        return 0
    con = sqlite3.connect(DB)
    cur = con.execute("UPDATE memos SET content=? WHERE id=?", (content, item_id))
    n = cur.rowcount
    con.commit()
    con.close()
    return n


# ── 回旋镖：把「N 天以前」的旧记录翻出来推回给用户 ────────────────
# 一次只推一条，推过的记上时间戳（boomerang_at），下次不再推同一条。
# 可推范围由 settings 的 `boomerang_scope` 决定（备忘 + 灵感各状态），
# 这里是纯数据层：只认 code，不读设置。

_SCOPE_TO_STATUS = {
    "open": STATUS_OPEN,
    "done": STATUS_DONE,
    "decided": STATUS_DECIDED,
    "dropped": STATUS_DROPPED,
}


def _boomerang_where(days, scope, now=None):
    """拼出回旋镖的 WHERE 子句 → (sql 片段, 参数列表)。没有可推范围时返回 (None, [])。

    🔴 `days=0` 是合法的：cutoff 就是 now，**今天刚记的也进池子** ——
       刚试用的用户发一句「回旋镖」立刻能看到效果，不该被「还没满 N 天」挡在门外。
    """
    want_memo = "memo" in (scope or [])
    statuses = [_SCOPE_TO_STATUS[s] for s in (scope or []) if s in _SCOPE_TO_STATUS]
    if not want_memo and not statuses:
        return None, []
    cutoff = ((now or datetime.now()) - timedelta(days=int(days))).isoformat(timespec="seconds")
    where = ["created_at <= ?", "boomerang_at IS NULL"]
    args = [cutoff]
    branches = []
    if want_memo:
        branches.append("kind = ?")
        args.append(KIND_MEMO)
    if statuses:
        marks = ",".join("?" * len(statuses))
        branches.append(f"(kind = ? AND status IN ({marks}))")
        args.extend([KIND_IDEA, *statuses])
    where.append("(" + " OR ".join(branches) + ")")
    return " AND ".join(where), args


def pick_boomerang(days, scope, now=None):
    """随机挑一条「days 天以前、且还没推过」的记录；没有候选返回 None。"""
    clause, args = _boomerang_where(days, scope, now)
    if clause is None:
        return None
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    row = con.execute(
        f"SELECT * FROM memos WHERE {clause} ORDER BY RANDOM() LIMIT 1", args
    ).fetchone()
    con.close()
    return _with_lists(dict(row)) if row else None


def count_boomerang(days, scope, now=None):
    """还剩几条可推 —— 用来给「没候选」时一句更准的提示。"""
    clause, args = _boomerang_where(days, scope, now)
    if clause is None:
        return 0
    con = sqlite3.connect(DB)
    n = con.execute(f"SELECT COUNT(*) FROM memos WHERE {clause}", args).fetchone()[0]
    con.close()
    return n


def mark_boomerang(memo_id):
    """记下「这条已经回旋镖过了」，下次不再推它。"""
    now = datetime.now().isoformat(timespec="seconds")
    con = sqlite3.connect(DB)
    con.execute("UPDATE memos SET boomerang_at=? WHERE id=?", (now, memo_id))
    con.commit()
    con.close()
