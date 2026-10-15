"""本机设置（键值对，JSON 落盘）—— 三个入口改的是同一份文件。

这个文件是干嘛的：把「改配置」从「开编辑器改 `.env` + 重启 bot」变成点几下。
存的是**非敏感**的行为配置（播报文案、抽屉展示范围），落 `settings.json`。

三个入口：
  - 桌面配置中心：`memo_gui.py` 的「配置中心」页签（推荐，能填能测能启停）
  - 脑内抽屉页面：顶部「⚙ 设置」（只放跟页面相关的 `bubbles_show`）
  - 机器人：`设置` / `设置 <键> <值>` / `设置 重置 <键>`

🔴 **密钥不进这里**（AppID / AppSecret / 群 openid 仍留在 `.env`）——
   这个文件的内容会经网页和群消息来回传，不放秘密。

取值优先级：`settings.json` → 同名环境变量（`KEYS` 里的 `env`）→ 内置默认值。
只把**显式改过**的键写进文件；没改过的走 env / 默认值（老配置不会失效）。
"""
import json
import os
import re
import threading

from paths import data_path

HERE = os.path.dirname(os.path.abspath(__file__))
# 🔴 settings.json 是**可写数据** —— 安装版由 MEMO_DATA_DIR 指到用户目录（见 paths.py）
FILE = data_path("settings.json")

_LOCK = threading.Lock()

# 图片收集在当前发布版本关闭。保留字段读取旧配置，但不在机器人设置指令中展示或修改。
_CHAT_HIDDEN_KEYS = {"image_collect", "image_alert_gb"}

# ── 「展示范围」共用的分类表 ──────────────────────────────────────
# 🔴 「脑内抽屉展示范围」和「回旋镖推送范围」是**同一套分类**（用户 2026-10-12
#    要求「回旋镖推送范围的设置应和脑内抽屉展示范围相同」）——
#    所以只在这里定义一次，两个键共用，免得以后改一个忘另一个。
#    `memo` / `memo_done` 是待办的两个状态，`open` / `done` 是思绪的两个状态，
#    `dropped` 两边都有（回收站）。
_SCOPE_CHOICES = ["memo", "memo_done", "open", "done", "dropped"]
_SCOPE_LABELS = {"memo": "待办", "memo_done": "已完成的待办",
                 "open": "未整理的思绪", "done": "已整理的思绪",
                 "dropped": "已丢弃"}
# 旧标签 / 简写别名：改名前用户是照着「未整理」「已整理」发指令的，
# 而且旧 settings.json 里存的虽然是 code，但群里发过的指令不能突然报「认不出」。
_SCOPE_ALIASES = {"decided": "done",          # 2026-10-12 分类合并
                  "未整理": "open", "已整理": "done",
                  "未完成": "memo", "已完成": "memo_done"}

# ── 可设置的键 ───────────────────────────────────────────────────
# kind: text（一行文本，留空 = 关掉该功能）/ area（多行文本，换行有意义）/
#       multi（多选，值是一组 code）/ int（整数）/ bool（开 / 关）
KEYS = {
    "startup_msg": {
        "label": "启动播报",
        "kind": "text",
        "default": "我上班了，开摸！",
        "env": "QQ_STARTUP_MSG",
        "help": "bot 上线时在群里说的话；留空 = 不播报",
    },
    "intro_msg": {
        "label": "功能介绍（首次上线 / 发「你好」）",
        "kind": "area",
        "default": (
            "你好，我是摸鱼bot 🐟\n"
            "\n"
            "我在群里帮你干这些活：\n"
            "· 记待办 —— 说「待办 买猫粮」，我收进脑内抽屉\n"
            "· 收灵感 —— 不带指令的普通发言，我悄悄记下来\n"
            "· 搜索灵感 —— 说「搜索灵感 设计」，按正文、标签或结论找回来\n"
            "· 标签 —— 写灵感时加 #设计，之后可以按标签搜索\n"
            "· 发文件 —— 说「发文件 设计说明」，我把文件渲染成图片发出来\n"
            "· 摸鱼 —— 说「摸鱼」，随机来一条，歇一下\n"
            "· 回旋镖 —— 把几天前记的旧账翻一条回来\n"
            "· 导出 —— 把全部待办 + 灵感导成一个文件发给你\n"
            "\n"
            "先说明白一件事：我没有语言生成能力。\n"
            "不会聊天、不会回答问题、不会替你写东西 —— 我只是个记事本加文件搬运工。\n"
            "想找 AI 聊天的话，我帮不上忙。\n"
            "\n"
            "发「帮助」看完整指令。"
        ),
        "help": "机器人第一次连上时自动发的自我介绍；之后每次上线只发「启动播报」。"
                "群里 / 私聊发「你好」也回这段。留空 = 首次上线不介绍（「你好」改用内置短文案）",
    },
    "memo_ack": {
        "label": "待办回执",
        "kind": "text",
        "default": "已经放进脑内抽屉了",
        "help": "记下一条待办后回给用户的那句话；留空 = 不回执（静默记下）",
    },
    "idea_ack": {
        "label": "灵感回执",
        "kind": "text",
        "default": "",
        "help": "把一条发言收作灵感后回的那句话。私聊里对每条都回；群里只在"
                "@ 机器人时回（群里不 @ 的普通发言一律静默收，免得刷屏）。"
                "留空 = 静默收下，不回 —— 这是默认；想让它回一句就填上，例如「💡 已收作灵感」",
    },
    "bubbles_show": {
        "label": "脑内抽屉展示范围",
        "kind": "multi",
        "default": ["memo", "open"],
        "choices": list(_SCOPE_CHOICES),
        "choice_labels": dict(_SCOPE_LABELS),
        # 🔴 「已决议」并入「已整理」（用户 2026-10-12）—— 旧配置 / 旧指令里
        #    的 decided 一律当 done 收，别让存量设置突然失效。
        "aliases": dict(_SCOPE_ALIASES),
        "help": "打开脑内抽屉时，内容流里放哪几类（默认：待办 + 未整理的思绪）",
    },
    "boomerang_days": {
        "label": "回旋镖回溯天数",
        "kind": "int",
        "default": 3,
        "min": 0,
        "max": 365,
        # 🔴 下限是 0，不是 1：填 0 = 今天记的也算，刚上手的人发一句「回旋镖」
        #    立刻就能看到效果，不会被「还没满 3 天」挡在门外。
        "help": "发「回旋镖」时，翻多少天以前的记录（默认 3 天；填 0 = 今天记的也算，"
                "刚上手就能玩）",
    },
    "boomerang_scope": {
        "label": "回旋镖推送范围",
        "kind": "multi",
        # 默认覆盖「全部待办（含已完成）+ 全部思绪（含已整理）」，不含已丢弃 ——
        # 等价于旧的 ["memo","open","done"]（那时 memo 不分完成与否）。
        "default": ["memo", "memo_done", "open", "done"],
        # 🔴 和「脑内抽屉展示范围」**同一套分类**（用户 2026-10-12）：
        #    以前这里只有 4 类、「待办」不分完成与否 —— 用户明确要求对齐，
        #    所以直接共用 `_SCOPE_CHOICES` / `_SCOPE_LABELS`。
        "choices": list(_SCOPE_CHOICES),
        "choice_labels": dict(_SCOPE_LABELS),
        # 旧配置里的 decided 当 done 收（见 bubbles_show 的说明）
        "aliases": dict(_SCOPE_ALIASES),
        "help": "发「回旋镖」时，从哪几类旧记录里挑一条推回来（分类与"
                "「脑内抽屉展示范围」一致）",
    },
    # 🔴 收图：**一个 multi 键顶三个开关**。
    #    空数组 = 关闭 ／ ["c2c"] = 仅私聊 ／ ["group"] = 仅群聊 ／ 两个都有 = 全部。
    #    所以「两个子开关都关掉 → 主开关自动关」是**天然成立**的，不需要跨键联动规则。
    #    配置中心那个「主开关 + 两个子开关」的区块是**自绘**的（不通用表单渲染器），
    #    主开关 = `bool(value)`，只是同一份数据的另一种画法。
    "image_collect": {
        "label": "收集图片",
        "kind": "multi",
        "default": [],
        "allow_empty": True,          # 🔴 别的 multi 不许空（「至少要选一个」），这个必须许
        "empty_label": "不收集",       # 空值的展示文案（默认会显示成空串）
        "choices": ["c2c", "group"],
        "choice_labels": {"c2c": "私聊", "group": "群聊"},
        "help": "把消息里的图片存到本地（脑内抽屉里显示、导出的 zip 里带上）。"
                "默认关。图片是带时效的远程链接，收到不存就再也拿不到了 —— "
                "所以这是「收到即落盘」，之后一直留着，不会自动清理。",
    },
    "ui_font_scale": {
        "label": "界面字号（%）",
        "kind": "int",
        "default": 115,
        "min": 80,
        "max": 200,
        "help": "整个桌面应用的界面字号缩放，默认 115%（比原来大一档）。"
                "100 = 原始大小，往上调字更大。改完保存，切回脑内抽屉立刻生效。",
    },
    "image_alert_gb": {
        "label": "图片占用报警阈值",
        "kind": "int",
        "default": 5,
        "min": 0,
        "max": 1024,
        # 🔴 0 = 不报警（不是「0GB 就报警」）—— 关掉报警必须有个明确的写法
        "help": "图片占用超过这个 GB 数时，往播报目标发一条提醒（默认 5）；填 0 = 不报警。"
                "占用回落到阈值以下的下一次超标才会再提醒一次，不会刷屏。",
    },
    # ── 掉线告警（v0.3.2）─────────────────────────────────────────
    # 🔴 这四条是**唯一**一组「机器人自己发不出去消息」时的设置 ——
    #    账号掉线时 QQ 通道不可用，告警只能走**带外**（托盘气泡 / webhook）。
    "alert_enabled": {
        "label": "掉线告警",
        "kind": "bool",
        "default": True,
        "help": "机器人进程还在、但账号已经掉线（收不到消息）时，用系统托盘气泡提醒你。"
                "默认开。关掉 = 完全不探活。",
    },
    "alert_probe_sec": {
        "label": "探活间隔（秒）",
        "kind": "int",
        "default": 120,
        "min": 30,
        "max": 3600,
        "help": "多久检查一次账号还在不在线（默认 120 秒）。调小 = 发现更快，"
                "但会更频繁地请求 QQ 接口；最小 30 秒。",
    },
    "alert_cooldown_min": {
        "label": "告警冷却（分钟）",
        "kind": "int",
        "default": 30,
        "min": 1,
        "max": 1440,
        "help": "同一场掉线最多隔多久提醒一次（默认 30 分钟），防止刷屏。"
                "恢复正常时会立刻发一条，不受冷却限制。",
    },
    "alert_webhook": {
        "label": "告警 webhook 地址",
        "kind": "text",
        "default": "",
        # 🔴 留空 = 只用托盘气泡（零配置可用）；填了就在发托盘气泡的同时 POST 一条 JSON。
        #    发不出去**只写日志**，绝不影响机器人本身。
        # 🔴 help 要说清「这一行是干嘛的」（用户 2026-10-16）：光写「填一个地址」
        #    用户不知道它解决什么问题 —— 掉线时 QQ 通道本身就是断的，托盘气泡
        #    又只有人坐在机器前才看得见，这一行是给「人不在电脑前」留的逃生口。
        "help": "机器人掉线时，除了弹系统托盘气泡，还可以往这个网址发一条提醒 —— 人不在"
                "电脑前也能在手机 / 群里收到（托盘气泡只有你正坐在机器前才看得到）。"
                "填一个 http(s) 开头的网址，掉线和恢复各发一条 JSON；留空 = 只用托盘气泡，"
                "什么都不用配。",
    },
}

CMD_WORDS = ("设置", "配置")
_SEP = " ：:　\t"          # 指令词后面必须跟分隔符，否则「设置好了」会被误判
_RESET_WORDS = ("重置", "恢复默认", "默认")


def file_path():
    """settings.json 的路径（可用 MEMO_SETTINGS_FILE 改）。"""
    return os.getenv("MEMO_SETTINGS_FILE") or FILE


def _dotenv():
    """读同目录 `.env`（不依赖第三方库）—— 只用于「env 回退」。"""
    out = {}
    try:
        with open(data_path(".env"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


# ── 读写 ────────────────────────────────────────────────────────
def _load():
    p = file_path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    vals = data.get("values") if isinstance(data, dict) else None
    return vals if isinstance(vals, dict) else {}


def _save(values):
    p = file_path()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"values": values}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


# ── 归一化 / 校验 ────────────────────────────────────────────────
def _to_code(key, token):
    """把「已整理」/「open」统一成 code；认不出返回 None。"""
    cfg = KEYS[key]
    t = str(token).strip()
    if t in cfg["choices"]:
        return t
    for code, label in cfg["choice_labels"].items():
        if t == label:
            return code
    # 旧值别名（decided → done）：分类合并后老配置 / 老指令照常生效
    alias = (cfg.get("aliases") or {}).get(t)
    if alias:
        return alias
    return None


# bool 键：人从群里发的、网页传的、代码里传的三种来源都认。
# 🔴 中文口语和符号都要收 —— 用户在群里发「设置 掉线告警 开」是很自然的写法；
#    认不出时**报错**而不是猜（猜错会让用户以为设置生效了）。
_BOOL_TRUE = {"1", "true", "yes", "y", "on", "开", "开启", "打开", "启用", "是", "√", "✓"}
_BOOL_FALSE = {"0", "false", "no", "n", "off", "关", "关闭", "关掉", "禁用", "否", "不", "×", "x", "✗"}


def _to_bool(raw):
    """任意来源 → (True/False, err)。"""
    if isinstance(raw, bool):
        return raw, None
    s = str(raw if raw is not None else "").strip().lower()
    if s in _BOOL_TRUE:
        return True, None
    if s in _BOOL_FALSE:
        return False, None
    return None, f"要填「开」或「关」（现在是「{raw}」）"


def normalize(key, raw):
    """把外部传进来的值转成规范形式 → (value, err)。err 非空表示不合法。"""
    if key not in KEYS:
        return None, f"没有这个设置项：{key}"
    cfg = KEYS[key]
    if cfg["kind"] in ("text", "area"):
        return str(raw if raw is not None else "").strip(), None
    if cfg["kind"] == "bool":
        return _to_bool(raw)
    if cfg["kind"] == "int":
        s = str(raw if raw is not None else "").strip()
        try:
            n = int(s)
        except ValueError:
            return None, f"要填一个整数（现在是「{s}」）"
        lo, hi = cfg.get("min"), cfg.get("max")
        if lo is not None and n < lo:
            return None, f"不能小于 {lo}"
        if hi is not None and n > hi:
            return None, f"不能大于 {hi}"
        return n, None
    # multi
    if isinstance(raw, (list, tuple)):
        parts = list(raw)
    else:
        parts = re.split(r"[,，、\s]+", str(raw or "").strip())
    out = []
    for p in parts:
        if not str(p).strip():
            continue
        code = _to_code(key, p)
        if code is None:
            opts = " / ".join(cfg["choice_labels"][c] for c in cfg["choices"])
            return None, f"认不出「{p}」（可选：{opts}）"
        if code not in out:
            out.append(code)
    if not out and not cfg.get("allow_empty"):
        opts = " / ".join(cfg["choice_labels"][c] for c in cfg["choices"])
        return None, f"至少要选一个（可选：{opts}）"
    return out, None


def _from_env(key):
    """同名环境变量 → 规范值；没配返回 None。

    先看进程环境，再看同目录 `.env` —— 这样不管调用方是 bot（已 load_dotenv）、
    网页服务还是工作区的脚本，回退行为都一致。
    """
    cfg = KEYS[key]
    name = cfg.get("env")
    if not name:
        return None
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        raw = _dotenv().get(name)
    if raw is None or not str(raw).strip():
        return None
    val, err = normalize(key, raw)
    return None if err else val


# ── 对外取值 ────────────────────────────────────────────────────
def get(key):
    """解析后的当前值（settings.json → env → 默认）。"""
    if key not in KEYS:
        raise KeyError(key)
    with _LOCK:
        saved = _load()
    if key in saved:
        return saved[key]
    e = _from_env(key)
    return KEYS[key]["default"] if e is None else e


def is_set(key):
    """用户是否显式改过这个键（用来区分「没配」和「配成空」）。"""
    with _LOCK:
        return key in _load()


def all_values():
    return {k: get(k) for k in KEYS}


def set_value(key, raw):
    """写一个键 → (ok, 值或错误信息)。"""
    val, err = normalize(key, raw)
    if err:
        return False, err
    with _LOCK:
        saved = _load()
        saved[key] = val
        _save(saved)
    return True, val


def reset(key):
    """恢复默认（从文件里删掉这个键）→ (ok, 值或错误信息)。"""
    if key not in KEYS:
        return False, f"没有这个设置项：{key}"
    with _LOCK:
        saved = _load()
        saved.pop(key, None)
        _save(saved)
    return True, get(key)


def apply_values(pairs):
    """批量写（网页「保存」用）→ (成功写入数, [错误信息])。

    先**全量校验**再一次性落盘 —— 否则改 5 项、第 3 项写错，会留个半成品。
    """
    errs = []
    staged = {}
    for key, raw in (pairs or {}).items():
        if key not in KEYS:
            errs.append(f"{key}: 没有这个设置项")
            continue
        val, err = normalize(key, raw)
        if err:
            errs.append(f"{KEYS[key]['label']}: {err}")
            continue
        staged[key] = val
    if errs:
        return 0, errs
    if staged:
        with _LOCK:
            saved = _load()
            saved.update(staged)
            _save(saved)
    return len(staged), []


# ── 展示 ────────────────────────────────────────────────────────
def display(key):
    """人看的值（multi 显示成中文标签）。"""
    cfg = KEYS[key]
    val = get(key)
    if cfg["kind"] == "multi":
        if not val:
            # 🔴 空值本来是空串（「收集图片 = 」），太像个 bug。允许为空的键自己带文案。
            return cfg.get("empty_label") or "（空）"
        return "、".join(cfg["choice_labels"].get(c, c) for c in val)
    if cfg["kind"] == "int":
        return str(val)
    return val if val else "（空 = 关掉）"


def describe():
    """给网页用的完整描述。"""
    out = []
    for key, cfg in KEYS.items():
        out.append({
            "key": key,
            "label": cfg["label"],
            "kind": cfg["kind"],
            "help": cfg.get("help", ""),
            "choices": cfg.get("choices", []),
            "choice_labels": cfg.get("choice_labels", {}),
            "allow_empty": bool(cfg.get("allow_empty")),
            "value": get(key),
            "default": cfg["default"],
            # 整数项的上下限透给界面（桌面版拿它定 Spinbox 范围，网页版定 input 的 min/max）
            "min": cfg.get("min"),
            "max": cfg.get("max"),
        })
    return out


def _find_key(name):
    n = (name or "").strip()
    if n in KEYS and n not in _CHAT_HIDDEN_KEYS:
        return n
    for k, cfg in KEYS.items():
        if k in _CHAT_HIDDEN_KEYS:
            continue
        if n == cfg["label"]:
            return k
    return None


def list_text():
    lines = ["⚙ 设置（桌面「配置中心」改的是同一份）"]
    for k, cfg in KEYS.items():
        if k in _CHAT_HIDDEN_KEYS:
            continue
        lines.append(f"{cfg['label']} [{k}] = {display(k)}")
    lines.append("改法：设置 <键> <值> ｜ 设置 重置 <键>")
    return "\n".join(lines)


# ── 机器人指令 ──────────────────────────────────────────────────
def is_command(text):
    """是不是「设置」指令。指令词后面必须有分隔符 ——
    否则「设置好了」「配置一下」这种普通聊天会被当成指令。"""
    t = (text or "").strip()
    for c in CMD_WORDS:
        if t == c:
            return True
        if t.startswith(c) and len(t) > len(c) and t[len(c)] in _SEP:
            return True
    return False


def _strip_prefix(text):
    t = (text or "").strip()
    for c in CMD_WORDS:
        if t.startswith(c):
            return t[len(c):].strip()
    return t


def handle_command(text):
    """返回要回给群/私聊的文本。"""
    rest = _strip_prefix(text)
    if not rest:
        return list_text()

    for w in _RESET_WORDS:
        if rest.startswith(w) and len(rest) > len(w):
            name = rest[len(w):].strip()
            key = _find_key(name)
            if not key:
                return f"（没有「{name}」这个设置项，发「设置」看列表）"
            ok, info = reset(key)
            if not ok:
                return f"（{info}）"
            return f"已把「{KEYS[key]['label']}」恢复默认：{display(key)}"

    parts = re.split(r"[ ：:　\t]+", rest, maxsplit=1)
    name = parts[0].strip()
    if len(parts) < 2:
        key = _find_key(name)
        if not key:
            return f"（没有「{name}」这个设置项，发「设置」看列表）"
        return f"{KEYS[key]['label']} = {display(key)}\n改法：设置 {name} <值>"

    key = _find_key(name)
    if not key:
        return f"（没有「{name}」这个设置项，发「设置」看列表）"
    ok, info = set_value(key, parts[1])
    if not ok:
        return f"（{info}）"
    return f"已设置「{KEYS[key]['label']}」= {display(key)}"
