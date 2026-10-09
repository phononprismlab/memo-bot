"""本机设置（键值对，JSON 落盘）—— 三个入口改的是同一份文件。

这个文件是干嘛的：把「改配置」从「开编辑器改 `.env` + 重启 bot」变成点几下。
存的是**非敏感**的行为配置（播报文案、泡泡展示范围），落 `settings.json`。

三个入口：
  - 配置中心网页：`config-web.py` 起的那一页（推荐，能填能测能启停）
  - 灵感泡泡页面：顶部「⚙ 设置」（只放跟页面相关的 `bubbles_show`）
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

HERE = os.path.dirname(os.path.abspath(__file__))
FILE = os.path.join(HERE, "settings.json")

_LOCK = threading.Lock()

# ── 可设置的键 ───────────────────────────────────────────────────
# kind: text（一行文本，留空 = 关掉该功能）/ multi（多选，值是一组 code）
KEYS = {
    "startup_msg": {
        "label": "启动播报",
        "kind": "text",
        "default": "我上班了，开摸！",
        "env": "QQ_STARTUP_MSG",
        "help": "bot 上线时在群里说的话；留空 = 不播报",
    },
    "offwork_msg": {
        "label": "关机通知文案",
        "kind": "text",
        "default": "现在开始，我要下班了！",
        "env": "OFFWORK_MSG",
        "help": "关机时自动发到群里的话；留空 = 不发",
    },
    "bubbles_show": {
        "label": "灵感泡泡展示范围",
        "kind": "multi",
        "default": ["open"],
        "choices": ["open", "done", "decided", "dropped"],
        "choice_labels": {"open": "待处理", "done": "已处理",
                          "decided": "已决议", "dropped": "已丢弃"},
        "help": "打开灵感泡泡时，内容流里放哪几类（默认只放「待处理」）",
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
        with open(os.path.join(HERE, ".env"), encoding="utf-8") as f:
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
    """把「待处理」/「open」统一成 code；认不出返回 None。"""
    cfg = KEYS[key]
    t = str(token).strip()
    if t in cfg["choices"]:
        return t
    for code, label in cfg["choice_labels"].items():
        if t == label:
            return code
    return None


def normalize(key, raw):
    """把外部传进来的值转成规范形式 → (value, err)。err 非空表示不合法。"""
    if key not in KEYS:
        return None, f"没有这个设置项：{key}"
    cfg = KEYS[key]
    if cfg["kind"] == "text":
        return str(raw if raw is not None else "").strip(), None
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
    if not out:
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
        return "、".join(cfg["choice_labels"].get(c, c) for c in val)
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
            "value": get(key),
            "default": cfg["default"],
        })
    return out


def _find_key(name):
    n = (name or "").strip()
    if n in KEYS:
        return n
    for k, cfg in KEYS.items():
        if n == cfg["label"]:
            return k
    return None


def list_text():
    lines = ["⚙ 设置（网页「配置中心」改的是同一份）"]
    for k, cfg in KEYS.items():
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
