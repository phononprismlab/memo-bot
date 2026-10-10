"""忽略表：命中规则的群 / 私聊消息一律跳过（不收灵感、不响应）。

典型用途：群里除了本 bot 还有别的机器人时，两个 bot 会把对方发的话当内容收进来，
既产生噪音、极端情况下还会互相响应形成循环。把对方的昵称 / openid 记进来即可打断。

规则语义（两条任一命中即跳过整条消息）：
  1. **子串**：消息文本（去掉 @ 标记后）包含该串 → 命中（忽略大小写）
  2. **发送者**：发送者 openid 与该串**完全相等** → 命中

规则来源（两者都生效，`忽略 列表` 会标注来源）：
  - 环境变量 `MEMO_IGNORE`（逗号分隔）—— 改 `.env` 即生效，指令改不动它
  - `ignore.json`（同目录；可用 `MEMO_IGNORE_FILE` 改路径）—— 由「忽略」指令维护

指令：
  忽略 <内容>        —— 加入忽略表
  忽略 列表          —— 查看（别名：list / 查看 / 帮助）
  忽略 删除 <内容>   —— 移出忽略表（别名：del / rm / 移除）
  忽略 清空          —— 清空 ignore.json 里的规则（清不掉 .env 里的）
"""
import json
import os
import re

from paths import data_path

HERE = os.path.dirname(os.path.abspath(__file__))

CMD_PREFIX = "忽略"
CMD_SEPS = " \t:：,，"
MIN_RULE_LEN = 2          # 太短的规则会误伤一大片，拒绝

LIST_WORDS = {"", "列表", "list", "查看", "帮助", "help", "?"}
DEL_WORDS = ("删除", "移除", "del", "rm", "remove", "取消")
CLEAR_WORDS = {"清空", "clear", "reset", "全部删除"}


# ── 读写 ────────────────────────────────────────────────────────
def file_path():
    """本地规则文件路径。

    惰性求值：`MEMO_IGNORE_FILE` 来自 .env，而 .env 是在 bot.py 导入之后才加载的，
    若在模块顶层读死会拿到空值。
    """
    return os.getenv("MEMO_IGNORE_FILE") or data_path("ignore.json")


def env_rules():
    raw = os.getenv("MEMO_IGNORE") or ""
    return [x.strip() for x in re.split(r"[,，]", raw) if x.strip()]


def file_rules():
    try:
        with open(file_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("rules", [])
    if not isinstance(data, list):
        return []
    return [str(x).strip() for x in data if str(x).strip()]


def _save(rules):
    with open(file_path(), "w", encoding="utf-8") as f:
        json.dump({"rules": rules}, f, ensure_ascii=False, indent=2)


def rules():
    """全部生效规则（env 优先，去重，保序）。"""
    out, seen = [], set()
    for r in env_rules() + file_rules():
        k = r.lower()
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def _same(a, b):
    return a.strip().lower() == b.strip().lower()


# ── 匹配 ────────────────────────────────────────────────────────
def match(text, sender_id=None):
    """命中返回规则本身，否则 None。"""
    t = (text or "").lower()
    for r in rules():
        if sender_id and r == sender_id:
            return r
        if r and r.lower() in t:
            return r
    return None


# ── 指令 ────────────────────────────────────────────────────────
def is_command(text):
    """是否是「忽略」指令。

    收紧到「忽略」+ 分隔符（或裸「忽略」）—— 否则「忽略这个 bug」这种普通聊天
    会被当成指令，把后面的内容加进忽略表。
    """
    t = (text or "").strip()
    if not t.startswith(CMD_PREFIX):
        return False
    rest = t[len(CMD_PREFIX):]
    return rest == "" or rest[0] in CMD_SEPS


def _strip_prefix(text):
    arg = (text or "").strip()[len(CMD_PREFIX):]
    return re.sub(r"^[\s:：,，]+", "", arg).strip()


def list_text():
    rs = rules()
    if not rs:
        head = "🚫 忽略表是空的（没有任何消息被跳过）。"
    else:
        head = f"🚫 忽略表 {len(rs)} 条（命中即跳过：不收灵感、不响应）："
    lines = [head]
    envs = {r.lower() for r in env_rules()}
    for i, r in enumerate(rs, 1):
        lines.append(f"{i}. {r}" + ("   ← 来自 .env（指令改不掉）" if r.lower() in envs else ""))
    lines.append("")
    lines.append("用法：忽略 <内容> ／ 忽略 删除 <内容> ／ 忽略 清空")
    return "\n".join(lines)


def add(rule):
    rule = (rule or "").strip()
    if len(rule) < MIN_RULE_LEN:
        return f"（规则太短了，至少 {MIN_RULE_LEN} 个字，否则会误伤一大片）"
    if any(_same(rule, r) for r in rules()):
        return f"（「{rule}」已经在忽略表里了）\n\n" + list_text()
    cur = file_rules()
    cur.append(rule)
    _save(cur)
    return f"已忽略「{rule}」——含它的消息不再收作灵感、也不会被响应。\n\n" + list_text()


def remove(rule):
    rule = (rule or "").strip()
    if not rule:
        return "用法：忽略 删除 <内容>"
    if any(_same(rule, r) for r in env_rules()):
        return f"（「{rule}」来自 .env 的 MEMO_IGNORE，只能改 .env 移除）"
    cur = file_rules()
    left = [r for r in cur if not _same(r, rule)]
    if len(left) == len(cur):
        return f"（忽略表里没有「{rule}」）\n\n" + list_text()
    _save(left)
    return f"已移除「{rule}」。\n\n" + list_text()


def clear():
    n = len(file_rules())
    _save([])
    return n


def handle_command(text):
    """处理一条「忽略 ...」指令，返回要回复的文本。"""
    arg = _strip_prefix(text)
    if arg in LIST_WORDS:
        return list_text()
    if arg in CLEAR_WORDS:
        n = clear()
        return f"已清空 {n} 条本地忽略规则。\n\n" + list_text()
    for w in DEL_WORDS:
        if arg == w:
            return "用法：忽略 删除 <内容>"
        if arg.startswith(w):
            rest = arg[len(w):].strip()
            if rest:
                return remove(rest)
    return add(arg)
