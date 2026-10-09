"""群备忘 + 发文件机器人（最小可用版）
本地运行，走 QQ 官方机器人 WebSocket 模式（无需公网 IP）。

三种运行方式：
  python bot.py --self-test   本地模拟群消息，验证识别+落库逻辑（无需 QQ 凭据）
  python bot.py --list        打印最近记录的备忘（本地读取用）
  python bot.py              真实启动 bot（需配置 .env 里的 QQ 凭据）

三条入口（群与私聊共用同一套指令与识别逻辑）：
  - on_group_at_message_create 群内 @机器人（宽松指令，含「发」「发送」等别名）
  - on_group_message_create    群内全量消息（需群设置「消息范围=全部消息」；只认明确指令）
    注意：botpy 1.2.1 未注册该事件，由 install_group_message_parser() 运行时补上。
  - on_c2c_message_create      私聊消息（QQ 开放平台需开启「单聊」；无需补解析器，
    botpy 1.2.1 已注册 C2C_MESSAGE_CREATE）
"""
import asyncio
import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

from store import (
    init,
    add_memo,
    list_memos,
    list_ideas,
    idea_stats,
    KIND_MEMO,
    KIND_IDEA,
    STATUS_OPEN,
)
import moyu
import extensions as _ext
import ignore as _ignore

HERE = os.path.dirname(os.path.abspath(__file__))
LAST_MSG_FILE = os.path.join(HERE, "last_group_msg.json")
PID_FILE = os.path.join(HERE, "bot.pid")


def _pid_alive(pid):
    """进程 pid 是否还活着。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            return str(pid) in out
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_pid_lock():
    """单实例锁：已有活着的实例就返回 False。

    🔴 为什么需要：计划任务只保证「同一个任务不并发」，手动执行
    `python bot.py` 会起第二个进程 → 两条 WS 连接抢事件、消息随机丢。
    """
    old = 0
    try:
        with open(PID_FILE, encoding="utf-8") as f:
            old = int((f.read() or "0").strip() or 0)
    except (OSError, ValueError):
        old = 0
    if old and old != os.getpid() and _pid_alive(old):
        print(f"[拒绝启动] 已有实例在跑（PID {old}）—— 同一时刻只能跑一个 bot。", flush=True)
        return False
    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError as e:
        print(f"[warn] 写 PID 文件失败（不阻塞启动）: {e}", flush=True)
        return True
    return True


def release_pid_lock():
    try:
        with open(PID_FILE, encoding="utf-8") as f:
            if int((f.read() or "0").strip() or 0) == os.getpid():
                os.remove(PID_FILE)
    except (OSError, ValueError):
        pass



def save_last_msg(openid, msg_id, scope="group"):
    """记录最近一条消息（群 / 单聊），供「被动回复」兜底用。

    `group_openid` 键保留：send_file.py 的旧读法仍可用。
    """
    if not openid or not msg_id:
        return
    try:
        with open(LAST_MSG_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "scope": scope,
                "openid": openid,
                "group_openid": openid,
                "msg_id": msg_id,
                "ts": time.time(),
            }, f)
    except Exception as e:
        print("save_last_msg failed:", e, flush=True)

# 加载同目录 .env（缺失或未装 python-dotenv 时静默跳过）
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass

TRIGGER = os.getenv("MEMO_TRIGGER", "备忘").strip()


def extract_memo(text):
    """从一条群消息里抽取备忘内容。支持前缀触发词，且前面可带 @机器人。"""
    if not text:
        return None
    t = strip_at(text)
    if TRIGGER and t.startswith(TRIGGER):
        content = re.sub(r"^[\s:：]+", "", t[len(TRIGGER):].strip())
        if content:
            return content
    return None


def handle_event(group_id, group_name, sender_id, sender_name, message):
    content = extract_memo(message)
    if not content:
        return None
    return add_memo(group_id, group_name, sender_id, sender_name, content)


# ── 群指令 ──────────────────────────────────────────────────────
# 内置指令的菜单；扩展指令的帮助行由 help_text() 追加（见文件后半的扩展加载）。
HELP_BASE = (
    "🐟 摸鱼bot 指令：\n"
    "备忘 <内容> —— 记一条备忘\n"
    "查备忘 —— 看最近 10 条备忘\n"
    "查灵感 —— 看还没处理的灵感\n"
    "发文件 <关键词> —— 搜文件并渲染成图片发出\n"
    "摸鱼 [1~5|放松|冷知识|全部] —— 随机来一条，歇一下\n"
    "忽略 <内容> —— 含该内容的消息一律跳过（不收灵感、不响应）；\n"
    "            「忽略 列表 / 忽略 删除 X / 忽略 清空」管理\n"
    "帮助 —— 显示本菜单\n"
    "群 / 私聊都可用；不带 @ 的普通发言（含私聊）会自动收作「灵感」，不打扰"
)
MEMO_LIST_CMDS = {"查备忘", "查看备忘", "备忘列表", "列备忘"}
IDEA_LIST_CMDS = {"查灵感", "查看灵感", "灵感列表", "列灵感"}
HELP_CMDS = {"帮助", "菜单", "指令", "help"}
# ── 「摸鱼」：随机抽一条放松/摸鱼/冷知识 ───────────────────────────
# 指令识别在 moyu.parse() 里（归一化 + 前缀匹配），这里不重复写正则。
MOYU_BADARG_HINT = "（摸鱼后面可以写：条数 1~5 / 放松 / 冷知识 / 全部）"


# QQ 群 @机器人 的两种形态：
#   1) 真机实测：content 里是 `<C9>` 这样的尖括号占位符（不是「@昵称」）
#   2) 部分客户端/自测：`@昵称`
# 都要剥掉，否则 @ 场景下所有指令都匹配不上。
_MENTION_ANGLE = re.compile(r"<[^\s<>]{1,40}>")
_MENTION_AT_PREFIX = re.compile(r"^(?:\s*@[^\s@]{1,30})+\s*")


def strip_at(text):
    """去掉 @机器人 标记（尖括号占位符任意位置；@昵称 只在开头）。"""
    t = _MENTION_ANGLE.sub(" ", text or "")
    t = _MENTION_AT_PREFIX.sub("", t)
    return re.sub(r"[ \t]+", " ", t).strip()


# ── 会话标识：群 / 单聊统一 ──────────────────────────────────────
SCOPE_GROUP = "group"
SCOPE_C2C = "c2c"
C2C_GROUP_NAME = "私聊"
# 私聊里「非指令文本」的处理：True = 回一句「已收作灵感」；False = 静默收（同群内非 @ 语义）
C2C_ACK_IDEA = False


def chat_target(message, scope):
    """统一群 / 单聊的会话标识，返回 dict：

      key        —— 会话唯一键（PENDING 用；群 `group:<id>` / 单聊 `c2c:<id>`）
      store_id   —— 落库的 group_id（群 = group_openid；单聊 = `c2c:<user_openid>`）
      store_name —— 落库的 group_name（群为空；单聊固定「私聊」）
      openid     —— 发送目标 openid（群 group_openid / 单聊 user_openid）
      sender_id  —— 发送者 id（群 member_openid / 单聊 user_openid）
    """
    if scope == SCOPE_C2C:
        author = getattr(message, "author", None)
        openid = (getattr(author, "user_openid", "") or "") if author else ""
        return {"key": f"c2c:{openid}", "store_id": f"c2c:{openid}",
                "store_name": C2C_GROUP_NAME, "openid": openid, "sender_id": openid}
    openid = getattr(message, "group_openid", "") or ""
    author = getattr(message, "author", None)
    sid = (getattr(author, "member_openid", "") or "") if author else ""
    return {"key": f"group:{openid}", "store_id": openid, "store_name": "",
            "openid": openid, "sender_id": sid}


def memo_list_text(limit=10, kind=KIND_MEMO):
    is_memo = kind == KIND_MEMO
    if is_memo:
        rows = list_memos(limit, kind)
        label, icon = "备忘", "📝"
        head = f"{icon} 最近 {len(rows)} 条{label}："
    else:
        # 灵感只看「待处理」——处理过的在电脑上用灵感池.md 看
        rows = list_ideas(STATUS_OPEN, limit, newest_first=True)
        label, icon = "灵感", "💡"
        st = idea_stats()
        head = (
            f"{icon} 待处理灵感 {st['open']} 条"
            f"（已处理 {st['done']} / 已决议 {st['decided']} / 已丢弃 {st['dropped']}）："
        )
    if not rows:
        return f"（暂无{label}）"
    lines = [head]
    for m in rows:
        ts = (m.get("created_at") or "")[5:16].replace("T", " ")
        lines.append(f"[{ts}] {strip_at(m['content'])}")
    return "\n".join(lines)


# ── 「发文件」：搜索文件根目录 + 交互式选择 ────────────────────────
# 🔴 搜索根默认是你的用户主目录（~）；用环境变量 MEMO_SEARCH_ROOT 改成任意目录
#    （比如只搜某个项目文件夹）。注意：这是「发文件」功能的检索范围，
#    不要指向含凭据 / 私密文件的目录。
# 🔴 同目录文件在搜索里加权（同分时优先）；用 MEMO_WORKSPACE_ROOT 覆盖。
WORKSPACE_ROOT = os.getenv("MEMO_WORKSPACE_ROOT") or HERE
SEARCH_ROOT = os.getenv("MEMO_SEARCH_ROOT") or os.path.expanduser("~")
EXCLUDE_DIRS = {
    "node_modules", ".git", "__pycache__", "envs", ".venv",
    "binaries", ".cache", "dist", "build", ".next",
    # 隐私保护：这些目录里的文件永远不会被「发文件」检索到
    ".workbuddy-ai", ".ssh", ".aws", ".config", ".npm", ".cargo",
}
SEARCH_LIMIT = 20

# @ 场景：宽松（「发」「发送」等口语别名）
FILE_SEND_RE = re.compile(r"^(?:发文件|找文件|发送文件|发送|给我发|帮我发|发)\s*(.+)$")
# 非 @ 场景：只认明确写法，避免「发工资了吗」这类误触
EXPLICIT_FILE_RE = re.compile(r"^(?:发文件|找文件|发送文件)\s*(.+)$")
BARE_FILE_CMDS = {"发文件", "找文件", "发送文件", "发送"}
PENDING = {}          # group_openid -> {"matches": [(rel, full)], "ts": float}
PENDING_TTL = 300     # 选择态有效期（秒）
MAX_SEND = 200 * 1024 * 1024
RENDER_MAX = 150 * 1024   # 文本类文件「渲染成图片」的体积上限（字节）


def _is_subsequence(q, s):
    """q 的字符是否按顺序出现在 s 中（如「设计说明」→「设计风格参考说明」）。"""
    it = iter(s)
    return all(ch in it for ch in q)


def search_files(query, limit=SEARCH_LIMIT):
    """按文件名/路径模糊搜**配置的根目录**（默认你的用户主目录，可用 MEMO_SEARCH_ROOT 改）。

    打分（全部**忽略大小写**）：
      3.0 文件名含该子串（最准，如 readme → README.md）
      2.5 相对路径含该子串
      2.0 文件名是其**子序列**（如 设计说明 → 设计风格参考说明-2026-10-08.md）
      1.5 相对路径是其**子序列**（如 工作日志10-08 → .../每日工作日志/2026-10-08.md）
      当前工作区内的文件额外 +0.1（同分时优先本地）

    ⚠️ 1.5 这级只在查询长度 ≥3 时启用 —— 子序列太宽松，短查询会命中一大片。
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    out = []
    for dirpath, dirnames, filenames in os.walk(SEARCH_ROOT):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            low = fn.lower()
            full = os.path.join(dirpath, fn)
            if full.startswith(WORKSPACE_ROOT + os.sep):
                rel = os.path.relpath(full, WORKSPACE_ROOT).replace("\\", "/")
                local = True
            else:
                rel = os.path.relpath(full, SEARCH_ROOT).replace("\\", "/")
                local = False
            if q in low:
                score = 3.0
            elif q in rel.lower():
                score = 2.5
            elif _is_subsequence(q, low):
                score = 2.0
            elif len(q) >= 3 and _is_subsequence(q, rel.lower()):
                score = 1.5
            else:
                continue
            if local:
                score += 0.1
            out.append((score, rel, full))
    out.sort(key=lambda x: (-x[0], len(x[1])))
    return [(rel, full) for _, rel, full in out[:limit]]


def resolve_choice(text, matches):
    """把用户的回复解析成 matches 里的一项（支持序号或文件名片段）。"""
    t = (text or "").strip()
    if t.isdigit():
        i = int(t)
        return matches[i - 1] if 1 <= i <= len(matches) else None
    hits = [m for m in matches if t and t in m[0]]
    return hits[0] if len(hits) == 1 else None


def file_list_text(matches, truncated=False):
    lines = [f"📎 找到 {len(matches)} 个文件，回复序号选一个："]
    for i, (rel, _) in enumerate(matches, 1):
        lines.append(f"{i}. {rel}")
    if truncated:
        lines.append(f"（只列前 {SEARCH_LIMIT} 个 —— 加更精确的关键词能缩小范围）")
    return "\n".join(lines)


# ── 本地扩展（自定义指令）─────────────────────────────────────────
# 公开仓库只带 extensions/example.py 模板；你的私有指令放 extensions/local/（已 gitignore）。
# 契约见 extensions/__init__.py 与 README 的「可扩展性」章节。
EXT = _ext.load(api=SimpleNamespace(search_files=search_files, SEARCH_ROOT=SEARCH_ROOT))


def help_text():
    """帮助菜单 = 内置指令 + 已注册的扩展指令。"""
    lines = [HELP_BASE]
    ext_help = EXT.help_lines()
    if ext_help:
        lines.append("")
        lines.extend(ext_help)
    return "\n".join(lines)


# ── 补 botpy 缺失的「群全量消息」解析器 ────────────────────────────
def install_group_message_parser():
    """botpy 1.2.1 只注册了 group_at_message_create，全量群消息会被网关丢弃
    （日志报 `_parser unknown event group_message_create`）。
    这里给 ConnectionState 补一个 parse_group_message_create，使
    GROUP_MESSAGE_CREATE 能分发到 on_group_message_create。
    必须在 Client 建立连接前调用（解析器在 ConnectionState.__init__ 里快照）。
    """
    try:
        from botpy.connection import ConnectionState
        from botpy.message import GroupMessage
    except Exception as e:
        print("[warn] 补全量群消息解析器失败（import）:", e, flush=True)
        return False
    if hasattr(ConnectionState, "parse_group_message_create"):
        return True

    def parse_group_message_create(self, payload):
        _message = GroupMessage(self.api, payload.get("id", None), payload.get("d", {}))
        self._dispatch("group_message_create", _message)

    ConnectionState.parse_group_message_create = parse_group_message_create
    print("[init] 已补上 group_message_create 解析器", flush=True)
    return True


# ── 本地自测：不连 QQ，模拟几条群消息验证逻辑 ──────────────────────
class _FakeEv:
    def __init__(self, gid, gname, sid, sname, msg):
        self.group_id, self.group_name = gid, gname
        self.sender_id, self.sender_name, self.message = sid, sname, msg


def self_test():
    """本地模拟群消息，验证识别 + 落库逻辑。

    🔴 落盘走**临时库**，不碰真实 `memos.db`（否则每跑一次就污染真实备忘）。
    """
    import store as _store

    orig_db = _store.DB
    tmpdir = tempfile.mkdtemp(prefix="memo_selftest_")
    _store.DB = os.path.join(tmpdir, "selftest.db")
    try:
        init()
        samples = [
            _FakeEv("123", "示例开发群", "dominik", "dominik", "备忘 明天确认云托管最小实例数"),
            _FakeEv("123", "示例开发群", "dominik", "dominik", "@机器人 备忘 备案变更还差营业执照"),
            _FakeEv("123", "示例开发群", "dominik", "dominik", "这只是普通聊天不是备忘"),
            _FakeEv("123", "示例开发群", "dominik", "dominik", "备忘：把事件系统文档补完"),
        ]
        print("=== 模拟群消息识别 ===")
        for s in samples:
            r = handle_event(s.group_id, s.group_name, s.sender_id, s.sender_name, s.message)
            print(f"  {s.message!r}\n    -> {('已记录: ' + r['content']) if r else '忽略（非备忘）'}")
        print("\n=== 本次模拟落库结果（临时库，已丢弃）===")
        for m in list_memos():
            print(f"  [{m['created_at']}] {m['group_name']}/{m['sender_name']}: {m['content']}")
    finally:
        _store.DB = orig_db
        shutil.rmtree(tmpdir, ignore_errors=True)


def list_mode():
    init()
    rows = list_memos()
    if not rows:
        print("（暂无备忘 / 灵感）")
        return
    for m in rows:
        tag = "备忘" if m.get("kind") == KIND_MEMO else "灵感"
        print(f"[{m['created_at']}] ({tag}) {m['group_name']}/{m['sender_name']}: {m['content']}")


def _arg_value(flag):
    """取命令行 --flag value 的值。"""
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


LOG_MAX_BYTES = 5 * 1024 * 1024  # 超过就轮转（bot.out.log → .1 → .2 → .3）
LOG_KEEP = 3


def rotate_log(path, max_bytes=LOG_MAX_BYTES, keep=LOG_KEEP):
    """日志超限则轮转：path → path.1 → … → path.{keep}。失败不阻塞启动。"""
    try:
        if not os.path.isfile(path) or os.path.getsize(path) < max_bytes:
            return False
    except OSError:
        return False
    for i in range(keep, 1, -1):
        src, dst = f"{path}.{i - 1}", f"{path}.{i}"
        if os.path.isfile(src):
            try:
                os.replace(src, dst)
            except OSError:
                pass
    try:
        os.replace(path, f"{path}.1")
        return True
    except OSError:
        return False


def setup_file_logging(path):
    """把 stdout/stderr 重定向到日志文件（供 pythonw 无控制台常驻运行）。

    必须在 import botpy 之前调用 —— botpy 在导入时执行 logging.basicConfig，
    StreamHandler 会绑定当时的 sys.stderr。
    """
    rotated = rotate_log(path)
    f = open(path, "a", encoding="utf-8", buffering=1)
    sys.stdout = f
    sys.stderr = f
    # 先占住 root handler（绑定到文件、去掉 ANSI 色码），
    # 这样 botpy 导入时的 logging.basicConfig 不会再挂一个带颜色的 stderr handler。
    import logging

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(f)
    handler.setFormatter(
        logging.Formatter("%(asctime)s\t[%(levelname)s]\t%(filename)s:%(lineno)s\t%(message)s")
    )
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    print(f"\n===== 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} =====", flush=True)
    if rotated:
        print(f"[init] 日志超过 {LOG_MAX_BYTES // 1024 // 1024}MB，已轮转为 {path}.1", flush=True)
    return f


# ── 路由：@ / 全量 / 私聊 共用 ────────────────────────────────────
async def route_message(client, message, mentioned, scope=SCOPE_GROUP):
    """@ / 全量 / 私聊 共用路由。mentioned=False 时只认明确指令，避免误触。

    抽成模块级函数（而非 MemoClient 的方法）是为了能被回归测试直接驱动：
    `client` 只需提供 send_file_to_chat(message, scope, openid, full, rel, mid)。
    """
    t = chat_target(message, scope)
    gid, mid = t["key"], (getattr(message, "id", "") or "")
    save_last_msg(t["openid"], mid, scope)
    raw = getattr(message, "content", "") or ""
    text = strip_at(raw)
    if mentioned:
        # 留原始内容便于排查 @ 占位符的真实编码
        print(f"[@消息] scope={scope} raw={raw!r} -> text={text!r}", flush=True)
    if not text:
        return

    # 0) 忽略表：先放行「忽略」指令本身（否则指令会被自己的规则吃掉），
    #    再对普通消息做闸门 —— 命中即整条跳过（不收灵感、不响应）。
    #    抄海豹骰子 `.botlist`：两个 bot 同群时靠这个避免互相收集 / 循环响应。
    if _ignore.is_command(text):
        await message.reply(content=_ignore.handle_command(text), msg_seq=1)
        return
    hit = _ignore.match(text, t["sender_id"])
    if hit:
        print(f"[忽略] scope={scope} hit={hit!r} text={text!r}", flush=True)
        return

    # 0.5) 有待选文件时，优先解析「选哪个」（序号 / 文件名片段）
    pend = PENDING.get(gid)
    if pend and time.time() - pend["ts"] <= PENDING_TTL:
        choice = resolve_choice(text, pend["matches"])
        if choice:
            PENDING.pop(gid, None)
            await client.send_file_to_chat(
                message, scope, t["openid"], choice[1], choice[0], mid
            )
            return

    # 1) 发文件：搜工作区 → 唯一直接发 / 多个列出来问
    bare_cmds = BARE_FILE_CMDS if mentioned else {"发文件", "找文件", "发送文件"}
    if text in bare_cmds:
        await message.reply(content="告诉我要发什么，例如「发文件 设计说明」", msg_seq=1)
        return
    fm = (FILE_SEND_RE if mentioned else EXPLICIT_FILE_RE).match(text)
    if fm:
        q = fm.group(1).strip()
        matches = search_files(q)
        if not matches:
            await message.reply(
                content=f"（没找到含「{q}」的文件，已搜配置的根目录）", msg_seq=1
            )
            return
        if len(matches) == 1:
            await client.send_file_to_chat(
                message, scope, t["openid"], matches[0][1], matches[0][0], mid
            )
            return
        PENDING[gid] = {"matches": matches, "ts": time.time()}
        await message.reply(
            content=file_list_text(matches, len(matches) >= SEARCH_LIMIT), msg_seq=1
        )
        return

    # 1.5) 本地扩展指令（extensions/ 下；自定义指令不进公开仓库）
    #      处理器同步执行（放线程，阻塞操作不卡心跳）；可返回多段消息
    ext_handler = EXT.match(text)
    if ext_handler:
        try:
            out = await asyncio.to_thread(ext_handler, text, mentioned)
        except Exception as e:
            out = f"（扩展指令执行失败：{type(e).__name__}: {e}）"
        if out:
            parts = out if isinstance(out, list) else [out]
            for i, part in enumerate(parts, start=1):
                await message.reply(content=part, msg_seq=i)
            return

    # 1.7) 摸鱼：随机来一条放松/摸鱼点子（`摸鱼 3` 多条、`摸鱼 冷知识` 换池）
    my = moyu.parse(text)
    if my is not None:
        if my == "badarg":
            # @ 场景：明确在跟机器人说话 → 给参数提示；
            # 非 @ 场景：可能只是聊天（「摸鱼真爽」），不打扰 → 落回原流程
            if mentioned:
                await message.reply(content=MOYU_BADARG_HINT, msg_seq=1)
                return
        else:
            n, cats = my
            reply = moyu.format_moyu(moyu.draw(n, cats))
            print(f"[摸鱼] {gid} text={text!r} n={n} cats={cats} -> {reply[:36]!r}", flush=True)
            await message.reply(content=reply, msg_seq=1)
            return

    # 2) 查备忘 / 查灵感 / 帮助
    if text in MEMO_LIST_CMDS:
        await message.reply(content=memo_list_text(10, KIND_MEMO), msg_seq=1)
        return
    if text in IDEA_LIST_CMDS:
        await message.reply(content=memo_list_text(10, KIND_IDEA), msg_seq=1)
        return
    if text in HELP_CMDS:
        await message.reply(content=help_text(), msg_seq=1)
        return

    # 4) 记备忘（@ 或不 @ 都认「备忘」触发词）
    result = handle_event(t["store_id"], t["store_name"], t["sender_id"], "", raw)
    if result:
        print(f"[备忘] {gid} -> {result['content']}", flush=True)
        try:
            await message.reply(content="已记到秘书会话 ✅", msg_seq=1)
        except Exception as e:  # 回执失败不影响已记录
            print("reply failed:", e, flush=True)
        return

    # 4.5) 兜底
    #   私聊：一切非指令文本都收作灵感（私聊没有「打扰群」的顾虑）
    #   @ 群消息：明确回一句，避免「@ 了却没反应」分不清是没识别还是挂了
    #   非 @ 群消息：静默落灵感，不把群聊变成机器人频道
    if scope == SCOPE_C2C:
        idea = add_memo(t["store_id"], t["store_name"], t["sender_id"], "",
                        text.strip(), kind=KIND_IDEA)
        print(f"[灵感·私聊] {gid} -> {idea['content']}", flush=True)
        if C2C_ACK_IDEA:
            await message.reply(content="💡 已收作灵感", msg_seq=1)
        return
    if mentioned:
        print(f"[未识别] @消息 text={text!r}", flush=True)
        await message.reply(
            content="（没认出来这条指令，发「帮助」看菜单）", msg_seq=1
        )
        return

    # 5) 非 @ 的普通文本 → 自动收作「灵感」（静默，不打扰群）
    if text.strip():
        idea = add_memo(t["store_id"], t["store_name"], t["sender_id"], "",
                        text.strip(), kind=KIND_IDEA)
        print(f"[灵感] {gid} -> {idea['content']}", flush=True)



def main():
    init()
    if not acquire_pid_lock():
        sys.exit(3)
    atexit.register(release_pid_lock)
    try:
        from botpy import Client, Intents
        from botpy.message import GroupMessage
    except ImportError:
        print("未安装 botpy，无法启动真实 bot。请先：pip install -r requirements.txt")
        sys.exit(1)

    appid = os.getenv("QQ_BOT_APPID")
    # botpy 的 Client.start(appid, secret) 用的是「机器人密钥(AppSecret)」，不是 Token。
    # 兼容旧变量名 QQ_BOT_TOKEN。
    secret = os.getenv("QQ_BOT_SECRET") or os.getenv("QQ_BOT_TOKEN")
    sandbox = (os.getenv("QQ_BOT_SANDBOX") or "").strip().lower() in ("1", "true", "yes")
    if not appid or not secret:
        print("缺少 QQ_BOT_APPID / QQ_BOT_SECRET，请在 .env 中配置（见 .env.example）。")
        sys.exit(1)

    install_group_message_parser()

    class MemoClient(Client):
        async def on_ready(self):
            print(f"[botpy] 已上线：{self.robot.name}", flush=True)
            # 启动播报（主动推送，群内需开启「机器人主动发言」）
            gid = (os.getenv("QQ_GROUP_OPENID") or "").strip()
            msg = (os.getenv("QQ_STARTUP_MSG") or "我上班了，开摸！").strip()
            if gid and msg:
                try:
                    import send_file as sf

                    await asyncio.to_thread(
                        sf.send_text_message, msg, gid, sandbox, appid, secret, None, 1
                    )
                    print(f"[启动播报] {gid}: {msg}", flush=True)
                except Exception as e:
                    print(f"[启动播报失败] {type(e).__name__}: {e}", flush=True)

        async def send_file_to_chat(self, message, scope, openid, full, rel, mid, caption=None):
            """发送一个文件到群 / 私聊。文本类渲染成图片（手机可直接看）；其余走文件卡片。"""
            import send_file as sf

            size = os.path.getsize(full)
            if size > MAX_SEND:
                await message.reply(content=f"（{rel} 超过 200MB，发不了）", msg_seq=1)
                return

            # 私聊没有「主动发言」权限 → 一律被动回复（带触发消息的 msg_id）；
            # 群优先主动推送，失败再降级为被动回复。
            if scope == SCOPE_C2C:
                attempts = [(mid, sf.CLI_SEQ_BASE)]
            else:
                attempts = [(None, 1), (mid, sf.CLI_SEQ_BASE)]

            # 文本类：渲染成图片发送（文件卡片在手机端打不开，拆文本又太碎）
            if sf.is_text_file(full) and size <= RENDER_MAX:
                tmpdir = tempfile.mkdtemp(prefix="memoimg_")
                try:
                    import md2img

                    imgs = await asyncio.to_thread(md2img.render, full, tmpdir)
                    cap = (caption or f"📄 {rel}") + (
                        f"（共 {len(imgs)} 张）" if len(imgs) > 1 else ""
                    )
                    last_err = None
                    for msg_id, seq in attempts:
                        try:
                            await asyncio.to_thread(
                                sf.send_images, imgs, openid, cap, sandbox, appid, secret,
                                msg_id, seq, scope,
                            )
                            print(f"[发图] {rel} -> {scope}:{openid}（{len(imgs)} 张）", flush=True)
                            return
                        except Exception as e:
                            last_err = e
                    raise last_err
                except Exception as e:
                    print(f"[发图·失败，退回文件卡片] {type(e).__name__}: {e}", flush=True)
                finally:
                    shutil.rmtree(tmpdir, ignore_errors=True)

            ft = sf.detect_file_type(full)
            last_err = None
            for msg_id, seq in attempts:
                try:
                    await asyncio.to_thread(
                        sf.send_file, full, openid, caption or f"📎 {rel}", ft,
                        sandbox, appid, secret, msg_id, seq, scope,
                    )
                    print(f"[发文件] {rel} -> {scope}:{openid}", flush=True)
                    return
                except Exception as e:
                    last_err = e
            await message.reply(
                content=f"（发送失败：{type(last_err).__name__}: {last_err}）", msg_seq=1
            )

        async def route(self, message, mentioned, scope=SCOPE_GROUP):
            # 实现在模块级 route_message()（便于回归测试直接驱动）
            await route_message(self, message, mentioned, scope)

        async def on_group_at_message_create(self, message: GroupMessage):
            # 群消息的 openid 字段名是 group_openid；author 只有 member_openid。
            await self.route(message, mentioned=True)

        async def on_group_message_create(self, message: GroupMessage):
            # 群内全量消息（需群设置「消息范围=全部消息」）
            await self.route(message, mentioned=False)

        async def on_c2c_message_create(self, message):
            # 私聊消息（QQ 开放平台需开启「单聊」）。私聊里没有 @ 概念，
            # 一律按「用户在直接跟机器人说话」处理（mentioned=True）。
            await self.route(message, mentioned=True, scope=SCOPE_C2C)

    # 群聊 + 单聊消息都对应 Intents.public_messages（不是 group_messages）
    intents = Intents(public_messages=True)
    MemoClient(intents=intents).run(appid=appid, secret=secret)


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    elif "--list" in sys.argv:
        list_mode()
    else:
        _log = _arg_value("--log")
        if _log:
            setup_file_logging(_log)
        main()
