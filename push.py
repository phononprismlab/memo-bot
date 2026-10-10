"""会话发现 + 播报目标（群 / 私聊统一）。

这个文件是干嘛的：机器人收到的每条消息都来自某个「会话」—— 要么是群，要么是私聊。
这里把见过的会话统一记下来，配置中心页面据此列出一张表：**每个会话一行**，
带可编辑的备注名 + 最近说过什么 + 最后说话时间。用户据此自己决定
「启动播报 / 关机播报要发到哪几个会话」，而不是被默认塞进群。

**为什么不分群 / 私聊两个列表**：页面上给用户看的是一串 openid，而
`group_openid` 与 `user_openid` 长得几乎一样，分两个列表反而更难判断
「这串 id 后面是谁」。合成一张表、用标签区分，再配上「备注名 + 最近一条消息」
才认得出来。

🔴 平台拿不到会话名字：
  - 群名接口 `GET /v2/groups/{id}/info` 需要白名单，个人开发者返回
    `11253 应用无接口访问权限 —— 该接口仅白名单机器人可用`；
  - 私聊**没有任何查用户信息的接口**。
  所以 `name` 只能靠**用户自己起**（配置中心里点着改），平台侧永远拿不到。
  能自动给的线索只有「最近说过什么」—— 从本机 `memos.db` 里取。

播报目标读 `.env` 的 `QQ_PUSH_TARGETS`，格式 `group:<openid>;c2c:<openid>`
（分号或逗号分隔）。🔴 兼容旧键：`QQ_PUSH_TARGETS` 没配时回退到
`QQ_GROUP_OPENID`（纯 openid，视作群）+ `QQ_C2C_OPENID`。

谁调用它：`bot.py`（记录 + 启动播报）、`offwork.py`（关机播报）、`config-web.py`（页面）。
"""
import json
import os
import re
import threading
import time
from datetime import datetime

from paths import data_path

# 会话类型。值同时是 `.env` 里的前缀（`group:xxx` / `c2c:xxx`），
# 与 `send_file.SCOPE_GROUP` / `SCOPE_C2C` 一致 —— 这里不 import send_file，
# 免得绕出循环依赖（send_file 会 import settings，settings 可能 import push）。
SCOPE_GROUP = "group"
SCOPE_C2C = "c2c"
SCOPES = (SCOPE_GROUP, SCOPE_C2C)

# 用户可能手写的同义前缀 → 归一化到 SCOPE_*
_SCOPE_ALIAS = {
    "group": SCOPE_GROUP, "群": SCOPE_GROUP, "群聊": SCOPE_GROUP,
    "c2c": SCOPE_C2C, "user": SCOPE_C2C, "私聊": SCOPE_C2C, "单聊": SCOPE_C2C,
}

FILE = data_path("sessions.json")
_LOCK = threading.Lock()
_RECENT = {}          # "scope:openid" -> 上次落盘时间（同一个会话别每条消息都写文件）
_NOTE_GAP = 300       # 秒

RECENT_MAX = 40       # 「最近一条消息」在页面上截断到多少字


def key(scope, openid):
    """会话唯一键 —— 与 `bot.py::chat_target()["key"]` 同一套格式。"""
    return f"{scope}:{openid}"


def _split(raw):
    """按分号 / 逗号 / 换行切，去空去重（顺序保留）。"""
    out = []
    for p in re.split(r"[;,，；\n]+", str(raw or "")):
        p = p.strip().strip('"').strip("'")
        if p and p not in out:
            out.append(p)
    return out


def parse_targets(raw):
    """把 `QQ_PUSH_TARGETS` 解析成 `[(scope, openid), ...]`。

    容忍两种写法：
      - 带前缀：`group:G123;c2c:U456`（正常写法）
      - 裸 openid：`G123`（= 群，兼容旧的 `QQ_GROUP_OPENID`）
    未知前缀不丢，按群处理 —— 宁可发错类型也不要静默吞掉用户填的目标。
    """
    out = []
    for p in _split(raw):
        scope, sep, oid = p.partition(":")
        if not sep:
            scope, oid = SCOPE_GROUP, p          # 裸 openid = 群
        else:
            scope = _SCOPE_ALIAS.get(scope.strip().lower(), SCOPE_GROUP)
            oid = oid.strip()
        if not oid:
            continue
        item = (scope, oid)
        if item not in out:
            out.append(item)
    return out


def format_targets(targets):
    """`[(scope, openid), ...]` → `"group:x;c2c:y"`（写回 `.env` 用）。"""
    return ";".join(f"{sc}:{oid}" for sc, oid in targets)


def _get(key_, env=None):
    """取配置：进程环境优先，其次调用方给的 dict（计划任务入口不一定会 load_dotenv）。"""
    v = os.getenv(key_)
    if (v is None or v == "") and env:
        v = env.get(key_)
    return v or ""


def targets(env=None):
    """播报目标 → `[(scope, openid), ...]`。

    主键 `QQ_PUSH_TARGETS`；没配就回退旧的两键（老用户升级后不丢配置）。
    """
    raw = _get("QQ_PUSH_TARGETS", env)
    if raw.strip():
        return parse_targets(raw)
    out = [(SCOPE_GROUP, o) for o in _split(_get("QQ_GROUP_OPENID", env))]
    out += [(SCOPE_C2C, o) for o in _split(_get("QQ_C2C_OPENID", env))]
    dedup = []
    for item in out:
        if item not in dedup:
            dedup.append(item)
    return dedup


# ── 本地记录（sessions.json）──────────────────────────────────────
def _load():
    if not os.path.exists(FILE):
        return {}
    try:
        with open(FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    s = data.get("sessions") if isinstance(data, dict) else None
    return s if isinstance(s, dict) else {}


def _save(sessions):
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"sessions": sessions}, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, FILE)


def _migrate_once():
    """首次运行把旧的 `groups.json` + `c2c.json` 并进来（旧文件保留不删）。

    只在 `sessions.json` 还不存在时做一次。旧文件里没有 scope 字段，
    群那份按 group 算、私聊那份按 c2c 算 —— 本来就是分开的两个文件。
    """
    if os.path.exists(FILE):
        return
    merged = {}
    for name, scope in (("groups.json", SCOPE_GROUP), ("c2c.json", SCOPE_C2C)):
        path = data_path(name)
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        # groups.json 是 {"groups": {...}}，c2c.json 直接就是 {openid: {...}}
        items = data.get("groups") if isinstance(data, dict) and "groups" in data else data
        if not isinstance(items, dict):
            continue
        for oid, v in items.items():
            oid = str(oid or "").strip()
            if not oid:
                continue
            v = v or {}
            merged[key(scope, oid)] = {
                "name": str(v.get("name") or "").strip(),
                "last": int(v.get("last") or 0),
            }
    if merged:
        try:
            _save(merged)
        except OSError:
            pass


def note(scope, openid, name=""):
    """记下「这个会话里有机器人活动」。同一个会话 5 分钟内只落盘一次。

    这是**唯一的写入点**：`bot.py` 每收到一条消息就调一次。
    """
    if scope not in SCOPES:
        scope = SCOPE_GROUP
    openid = str(openid or "").strip()
    if not openid:
        return
    k = key(scope, openid)
    now = time.time()
    with _LOCK:
        _migrate_once()
        prev = _RECENT.get(k)
        if prev and now - prev < _NOTE_GAP:
            return
        _RECENT[k] = now
        s = _load()
        cur = s.get(k) or {}
        s[k] = {"name": str(name or cur.get("name") or "").strip(),
                "last": int(now)}
        try:
            _save(s)
        except OSError:
            pass


def set_name(scope, openid, name):
    """改备注名（只存本机 `sessions.json`，已 gitignore）。空串 = 清掉备注。"""
    if scope not in SCOPES:
        return False
    openid = str(openid or "").strip()
    if not openid:
        return False
    with _LOCK:
        _migrate_once()
        s = _load()
        k = key(scope, openid)
        cur = s.get(k) or {}
        s[k] = {"name": str(name or "").strip(), "last": int(cur.get("last") or 0)}
        try:
            _save(s)
        except OSError:
            return False
    return True


# ── 从 memos.db 补「最近说过什么」──────────────────────────────────
def _db_recent():
    """每个会话的最近一条消息 → `{group_id: (content, created_at)}`。

    🔴 库里 `group_id` 的两种形态（见 `bot.py::chat_target()`）：
       群 = 裸 `group_openid`；私聊 = `c2c:<user_openid>`。
       所以 `c2c:` 前缀同时也是「这是私聊」的判据。

    ⚠️ 只读、失败静默 —— 库不在 / 表还没建 / 被占用，都不该让配置中心打不开。
    """
    try:
        import sqlite3

        from store import DB            # 懒导入：只在真要查时才碰 DB
        if not os.path.exists(DB):
            return {}
        con = sqlite3.connect(DB)
        try:
            rows = con.execute(
                "SELECT m.group_id, m.content, m.created_at FROM memos m"
                " JOIN (SELECT group_id, MAX(created_at) mx FROM memos"
                "       GROUP BY group_id) t"
                "   ON m.group_id = t.group_id AND m.created_at = t.mx"
            ).fetchall()
        finally:
            con.close()
    except Exception:                   # noqa: BLE001
        return {}
    out = {}
    for gid, content, created in rows:
        gid = str(gid or "").strip()
        if gid and gid not in out:
            out[gid] = (str(content or ""), str(created or ""))
    return out


def _db_scope_id(scope, openid):
    """会话 → 库里 `group_id` 的形态。"""
    return f"c2c:{openid}" if scope == SCOPE_C2C else openid


def _iso_ts(text):
    """ISO 时间串 → 时间戳（失败返回 0）。"""
    try:
        return int(datetime.fromisoformat(text).timestamp())
    except (ValueError, TypeError, OSError):
        return 0


def recent(scope, openid, limit=RECENT_MAX):
    """这个会话最近说过的一句 → (截断后的文本, 时间戳)。没记录过则 ("", 0)。"""
    got = _db_recent().get(_db_scope_id(scope, openid))
    if not got:
        return "", 0
    content, created = got
    content = " ".join(content.split())          # 换行 / 连续空白压成单个空格
    if len(content) > limit:
        content = content[:limit] + "…"
    return content, _iso_ts(created)


def sessions(env=None):
    """全部会话（按最近活跃倒序）→ list[dict]。

    每项：`{scope, openid, name, last, target, recent, recent_at}`

    三个来源合并：
      1. `sessions.json`（收到消息就记，带 last + 用户起的备注名）
      2. `memos.db` 历史（只读补的 —— 文件是后加的，不补的话老用户看到空列表）
      3. `.env` 里已配的推送目标（可能还没消息记录，比如手动填的 openid）
    """
    _migrate_once()
    with _LOCK:
        s = _load()
    db = _db_recent()
    tg = set(targets(env))

    rows = {}

    def _touch(scope, openid, name="", last=0):
        k = key(scope, openid)
        cur = rows.get(k)
        if cur is None:
            rows[k] = {"scope": scope, "openid": openid, "name": name, "last": last}
        else:
            if name and not cur["name"]:
                cur["name"] = name
            if last > cur["last"]:
                cur["last"] = last

    for k, v in s.items():
        scope, _, oid = str(k).partition(":")
        oid = oid.strip()
        if scope not in SCOPES or not oid:
            continue
        v = v or {}
        _touch(scope, oid, str(v.get("name") or "").strip(), int(v.get("last") or 0))

    for gid in db:
        if gid.startswith("c2c:"):
            scope, oid = SCOPE_C2C, gid[len("c2c:"):].strip()
        else:
            scope, oid = SCOPE_GROUP, gid
        if oid:
            _touch(scope, oid)

    for scope, oid in tg:                    # 配了但还没说过话的目标也要出现在表里
        _touch(scope, oid)

    out = []
    for row in rows.values():
        content, at = db.get(_db_scope_id(row["scope"], row["openid"])) or ("", "")
        # `at` 是库里的 ISO 串（`_db_recent()` 给的是原始行）—— 要转成时间戳才能排序
        if at and not row["last"]:
            row["last"] = _iso_ts(at)
        text = " ".join(str(content).split())
        if len(text) > RECENT_MAX:
            text = text[:RECENT_MAX] + "…"
        row["target"] = (row["scope"], row["openid"]) in tg
        row["recent"] = text
        row["recent_at"] = _iso_ts(at)
        out.append(row)

    # 已勾选的排前面，其次按最近活跃（`last` 兜一层 int —— 文件被人手改过也不至于炸）
    out.sort(key=lambda r: (not r["target"], -int(r["last"] or 0)))
    return out
