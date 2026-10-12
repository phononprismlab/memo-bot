"""会话发现 + 播报目标（群 / 私聊统一）。

这个文件是干嘛的：机器人收到的每条消息都来自某个「会话」—— 要么是群，要么是私聊。
这里把见过的会话统一记下来，配置中心页面据此列出一张表：**每个会话一行**，
带可编辑的备注名 + 最近说过什么 + 最后说话时间。用户据此自己决定
「启动播报要发到哪几个会话」，而不是被默认塞进群。

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
（分号或逗号分隔）。🔴 兼容旧键：**从没配过** `QQ_PUSH_TARGETS` 时回退到
`QQ_GROUP_OPENID`（纯 openid，视作群）+ `QQ_C2C_OPENID`；配过就以它为准，
**空值不回退**（见 `targets()` 的说明）。

谁调用它：`bot.py`（记录 + 启动播报）、`config_core.py`（桌面配置中心的「播报目标」列表）。
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


_ENV_FILE = {"t": -1.0, "v": {}}


def _env_file():
    """读同目录 `.env`（带 mtime 缓存）。

    🔴 必须兜这一层：配置中心（网页版 / 桌面版）**不调 `load_dotenv()`**，
    进程环境里根本没有这些键 —— 只认 `os.getenv` 的话，页面读到的永远是空的。
    表现就是「勾了播报目标，一刷新又变回没勾」——写了 `.env` 但读不出来。
    """
    try:
        path = data_path(".env")
        mt = os.path.getmtime(path)
    except OSError:
        return {}
    if mt == _ENV_FILE["t"]:
        return _ENV_FILE["v"]
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        return {}
    _ENV_FILE["t"], _ENV_FILE["v"] = mt, out
    return out


def _get(key_, env=None):
    """取配置：进程环境 → 调用方给的 dict → `.env` 文件。

    🔴 `.env` 放在最后但**必须有** —— 机器人进程靠 `load_dotenv()` 把值灌进
    进程环境，配置中心却不会，少了这一层两边读到的就不是同一份配置。
    """
    v = os.getenv(key_)
    if (v is None or v == "") and env:
        v = env.get(key_)
    if v is None or v == "":
        v = _env_file().get(key_)
    return v or ""


def _present(key_, env=None):
    """这个键**存在**吗（哪怕是空值）？

    🔴 用来区分「从来没配过」和「配成了空」—— 两者 `_get()` 都返回 ""，
    但含义完全相反：前者该回退旧键，后者就是用户把目标全取消了。
    """
    if key_ in os.environ:
        return True
    if env and key_ in env:
        return True
    return key_ in _env_file()


def targets(env=None):
    """播报目标 → `[(scope, openid), ...]`。

    主键 `QQ_PUSH_TARGETS`；**从没配过**这个键才回退旧的两键（老用户升级不丢配置）。

    🔴 「配成空」不回退：`.env` 里旧键（`QQ_GROUP_OPENID`）和主键往往同时存在，
       要是把空值也当「没配」，用户在页面上取消勾选最后一个目标就会立刻被旧键
       复活 —— 表现是「怎么点都取消不掉」。
    """
    raw = _get("QQ_PUSH_TARGETS", env)
    if raw.strip() or _present("QQ_PUSH_TARGETS", env):
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


# ── 图片占用报警 ────────────────────────────────────────────────
# 照片是**收到即落盘、不自动清理**，所以占用只会涨。超过用户设的阈值就往播报目标
# 发一条提醒。状态落 `image-alert.json`（本机状态，已 gitignore）。
#
# 🔴 防刷屏的关键是「**回滞**」：不能占用一直超阈值就每张图报一次。
#    规则 = 只有「上次报警后占用曾**回落**到阈值以下」才允许再报一次。
#    也就是说 `fired=True` 且占用仍超标 → 静默；占用降到阈值以下 → 把 `fired` 清掉，
#    下次再超标才会报。用户想立刻再收一条提醒，把阈值调一下即可。
_ALERT_FILE = data_path("image-alert.json")


def alert_should_fire(gb, threshold_gb, fired):
    """要不要发这条报警 → bool。纯函数，方便单测。"""
    if not threshold_gb or threshold_gb <= 0:
        return False                      # 阈值 0 = 关掉报警
    if gb < threshold_gb:
        return False
    return not fired                      # 已经在「超标且报过」的状态里就不再报


def alert_text(gb, threshold_gb, count=0):
    """报警正文（人话，带上怎么办）。"""
    head = f"图片已经存了 {gb} GB（超过你设的 {threshold_gb} GB）"
    if count:
        head += f"，共 {count} 张"
    return (
        f"{head}。\n"
        "我不会自动删图 —— 图片是带时效的远程链接，删了就真没了。\n"
        "想少占点地方，可以自己去这个目录里清：\n"
        f"  {os.path.join(os.path.dirname(_ALERT_FILE), 'images')}\n"
        "清完占用会自己降下来，下次再超过阈值时会再提醒你一次。\n"
        "（不想收这类提醒：「设置 图片占用报警阈值 0」，或在配置中心填 0）"
    )


def _alert_load():
    if not os.path.exists(_ALERT_FILE):
        return {}
    try:
        with open(_ALERT_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _alert_save(state):
    try:
        tmp = _ALERT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, _ALERT_FILE)
    except OSError:
        pass


def check_image_alert(threshold_gb, usage, send=None):
    """占用超阈值就发一条提醒 → dict（给日志 / 测试看）。

    `usage` = `media.usage()` 的返回值；`send` = `send(text)`，不传就**只更新状态不发**
    （供配置中心那种只读场景调用）。
    返回 `{"fired": bool, "state": {...}}`。
    """
    gb = float((usage or {}).get("gb") or 0)
    count = int((usage or {}).get("count") or 0)
    st = _alert_load()
    fired = bool(st.get("fired"))
    if gb < threshold_gb:
        # 🔴 回落 = 撤销「已报警」，下次超标才会再报。这行是防刷屏的全部秘密。
        if fired:
            _alert_save({"fired": False, "gb": gb})
        return {"fired": False, "state": {"fired": False, "gb": gb}}
    if not alert_should_fire(gb, threshold_gb, fired):
        return {"fired": False, "state": st}
    text = alert_text(gb, threshold_gb, count)
    if send is None:
        _alert_save({"fired": True, "gb": gb})
        return {"fired": False, "state": {"fired": True, "gb": gb}}
    try:
        send(text)
        _alert_save({"fired": True, "gb": gb})
        return {"fired": True, "state": {"fired": True, "gb": gb}}
    except Exception as e:                # noqa: BLE001  发失败不算报过 —— 下次重试
        return {"fired": False, "state": st, "error": f"{type(e).__name__}: {e}"}
