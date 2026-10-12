"""配置后端（被桌面端复用）—— 纯逻辑，不含任何 HTTP / 网页。

这个文件是干嘛的：把「配置中心」的纯后端逻辑抽出来，桌面端 memo_gui.py 直接把它当
后端模块 import 来用。原来这些逻辑和本地网页服务（config-web.py）混在一起；网页 UI
下线后，后端独立成这个文件，桌面端不再依赖任何网页模块。

包含：`.env` 按行读写（注释与顺序保留、写前备份）、搜索根目录校验与统计、依赖自检、
整份可编辑字段表（env + settings 混排）、以及整份状态快照 `full_state()`。
桌面端的配置中心 / 脑内抽屉 / 运行方式 / 版本四个页签全部走它。

谁调用它：memo_gui.py（桌面端）。原先的网页 config-web.py 已删除，本文件是其后端真源。
"""

import importlib.util
import os
import re
import shutil
import sys
import time

# 🔴 标准输出 / 错误强制 UTF-8。中文 Windows 默认 GBK，终端里编不出的字符
#    （emoji / ⏻ / ⚙ 等）会让 print 抛 UnicodeEncodeError 把服务带崩。
#    errors="replace" 兜底：编不出的打成 ?，不让进程挂掉。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


import ignore as _ignore      # noqa: E402  （忽略表：规则读写）
import settings as _settings  # noqa: E402  （本机设置：settings.json）
import push as _push            # noqa: E402  （已发现的会话 / 播报目标，群与私聊统一）
import media as _media         # noqa: E402  （收进来的图片：占用统计 / 体积格式化）
import moyu as _moyu          # noqa: E402  （摸鱼库：内置库 / 用户自己那份）
import extmgr as _extmgr      # noqa: E402  （自定义指令：扩展包的导入 / 列出 / 移除）
import version as _version    # noqa: E402  （版本号 + 「有没有新版本」检测）
from paths import data_path, data_dir   # noqa: E402  （数据目录：可写文件落哪）

# 🔴 .env / 日志 / pid 都是**可写数据** —— 安装版由 MEMO_DATA_DIR 指到用户目录（见 paths.py）
ENV_PATH = data_path(".env")
ENV_BAK = data_path(".env.bak")
BOT_LOG = data_path("bot.out.log")
PID_FILE = data_path("bot.pid")
TASK_NAME = "MoyuBot"

# 数文件时的预算：超过就停下并标注「至少这么多」，避免一个百万级目录把页面卡死
COUNT_BUDGET = 3.0


# ── .env：读 ─────────────────────────────────────────────────────
def read_env_file():
    """读 `.env` 的原始键值（只去引号，不做别的解释）。文件不存在返回 {}。"""
    out = {}
    try:
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def sync_env():
    """把 `.env` 的值灌进本进程的 `os.environ`。

    🔴 必须做，而且必须**每次写完 `.env` 都再做一次**：
       `ignore.env_rules()`、`settings._from_env()` 这些地方读的是 `os.environ`，
       不灌的话它们看到的是启动那一刻的旧值（甚至什么都没有）——
       表现就是页面刚改完忽略关键词，列表还是空的。
    """
    for k, v in read_env_file().items():
        os.environ[k] = v


# ── .env：写 ─────────────────────────────────────────────────────
def _env_literal(v):
    """把一个值写成 `.env` 里合法的字面量。

    - Windows 路径的反斜杠统一转成正斜杠：两个 `.env` 解析器（python-dotenv 与
      `settings._dotenv`）对 `\\` 转义的处理不一致，正斜杠能绕开这个坑，
      而 Python / Node 打开文件时都照样认正斜杠。
    - 空值写成 `""`；含空白或 `#` 的值加双引号；含双引号直接拒绝（避免引号打架）。
    """
    v = str(v)
    if re.match(r"^[A-Za-z]:[\\/]", v) or v.startswith("\\\\"):
        v = v.replace("\\", "/")
    if '"' in v:
        raise ValueError("值里不能包含双引号")
    if v == "":
        return '""'
    if re.search(r"[\s#]", v):
        return '"' + v + '"'
    return v


def write_env(updates):
    """把若干键写进 `.env` → (写入的键列表, 错误信息或 None)。

    🔴 按行改：**原有注释与顺序原样保留**，已有键只换值，新键追加到末尾。
       这样用户手写的注释不会丢、git diff 也干净。
    """
    if not updates:
        return [], None

    clean = {}
    errs = []
    for k, v in updates.items():
        spec = ENV_SPEC.get(k)
        if spec is None:
            errs.append(f"不允许改这个键：{k}")
            continue
        v = str(v if v is not None else "").strip()
        if spec.get("kind") == "bool":
            v = "true" if v.lower() in ("1", "true", "yes", "on") else "false"
        if spec.get("kind") == "paths":
            # 多目录：归一成「分号串 + 正斜杠」——
            # 两个 .env 解析器（python-dotenv 与 settings._dotenv）对反斜杠转义处理不一致。
            # 🔴 顺手**去重**（保序）：用户在框里粘来粘去很容易粘出重复行，
            #    留着只会让搜索白跑一遍同一个目录。归一后的结果会回填到输入框
            #    （桌面端保存后整页重画），用户能直接看到整理后的样子。
            _seen = set()
            _parts = []
            for p in split_roots(v):
                p = p.replace("\\", "/")
                if p not in _seen:
                    _seen.add(p)
                    _parts.append(p)
            v = ";".join(_parts)
        # 🔴 密钥留空 = 「不修改」。因为页面上本来就不回显它，留空不能理解成「清空」。
        if spec.get("secret") and not v:
            continue
        chk = spec.get("check")
        if chk:
            e = chk(v)
            if e:
                errs.append(f"{spec['label']}：{e}")
                continue
        clean[k] = v
    if errs:
        return [], "；".join(errs)
    if not clean:
        return [], None

    lines = []
    if os.path.exists(ENV_PATH):
        try:
            with open(ENV_PATH, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError as e:
            return [], f"读不到 .env：{e}"

    remaining = dict(clean)
    out = []
    for ln in lines:
        s = ln.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in remaining:
                try:
                    out.append(f"{k}={_env_literal(remaining.pop(k))}")
                except ValueError as e:
                    return [], f"{k}：{e}"
                continue
        out.append(ln)
    if remaining:
        if out and out[-1].strip():
            out.append("")
        out.append("# ── 由配置中心写入 ──")
        for k, v in remaining.items():
            try:
                out.append(f"{k}={_env_literal(v)}")
            except ValueError as e:
                return [], f"{k}：{e}"

    try:
        if os.path.exists(ENV_PATH):
            shutil.copy2(ENV_PATH, ENV_BAK)          # 备份上一次（覆盖式，只留最近一份）
        tmp = ENV_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(out) + "\n")
        os.replace(tmp, ENV_PATH)
    except OSError as e:
        return [], f"写 .env 失败：{e}"
    sync_env()                       # 🔴 立刻让本进程看到新值（忽略表 / 设置回退都读它）
    return list(clean), None


# ── 目录校验 / 统计 ───────────────────────────────────────────────
def split_roots(v):
    """把多目录字段拆成目录列表（分号或换行分隔，去掉引号与空项）。"""
    out = []
    for part in re.split(r"[;\n]+", str(v or "")):
        p = part.strip().strip('"').strip("'")
        if p:
            out.append(p)
    return out


def check_roots(v):
    """搜索根目录的校验：每个都要存在且是目录（**支持多个**，分号或换行分隔）。"""
    parts = split_roots(v)
    if not parts:
        return "至少填一个目录（留空的话「发文件」会去搜你的整个用户目录，很慢）"
    for p in parts:
        if not os.path.isdir(os.path.expanduser(p)):
            return f"这个目录不存在：{p}"
    return None


def count_files(root, budget=COUNT_BUDGET):
    """数这个目录下有多少个文件 → (数量, 用时秒, 是否到点提前停)。

    排除规则跟「发文件」的搜索一致（见 `bot.py` 的 `EXCLUDE_DIRS`），
    否则报出来的数字会虚高。**在子线程里调**，别卡住 HTTP 线程。
    """
    from bot import EXCLUDE_DIRS

    n = 0
    t0 = time.time()
    stopped = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        n += len(filenames)
        if time.time() - t0 > budget:
            stopped = True
            break
    return n, round(time.time() - t0, 3), stopped


def browse_dirs(path):
    """列一个目录下的子目录（配置页的「浏览」用）→ dict。只列目录，不读文件内容。"""
    p = os.path.expanduser(path or os.path.expanduser("~"))
    p = os.path.abspath(p)
    if not os.path.isdir(p):
        return {"ok": False, "path": p, "error": f"不是目录：{p}"}
    dirs = []
    try:
        with os.scandir(p) as it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        dirs.append(e.name)
                except OSError:
                    continue
    except OSError as e:
        return {"ok": False, "path": p, "error": f"读不了这个目录：{e}"}
    # 隐藏目录排后面，其余按名字（不区分大小写）排
    dirs.sort(key=lambda d: (d.startswith("."), d.lower()))
    parent = os.path.dirname(p.rstrip("/\\")) or p
    return {"ok": True, "path": p.replace("\\", "/"),
            "parent": parent.replace("\\", "/"), "dirs": dirs}


# ── 机器人进程 / 计划任务 ────────────────────────────────────────
# 🔴 这套逻辑抽到 botctl.py 了（配置中心和管家 hub.py 共用，三合一打包后不能重复两份）。
#    这里直接 import 透传，页面代码（bot_start / bot_stop / autostart_set / task_state …）
#    的调用处都不用改。`LAUNCHER / BOT_LOG / PID_FILE / TASK_NAME` 仍在本文件顶部定义，
#    因为它们还被页面响应体（状态查询 / 日志路径）用到。
from botctl import (  # noqa: E402,F401
    bot_running, bot_pid, _pid_alive,
    bot_start, bot_stop, bot_restart,
    task_state, autostart_set, autostart_state, _pythonw, _powershell,
)


# ── 依赖自检 ────────────────────────────────────────────────────
PY_DEPS = [
    ("qq-botpy", "botpy", "收 QQ 消息（必需）"),
    ("python-dotenv", "dotenv", "读 .env（必需）"),
    ("markdown", "markdown", "文本转 HTML（渲染成图片用）"),
    ("pillow", "PIL", "长图分片拼接（渲染成图片用）"),
]

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def _find_node():
    return os.getenv("MEMO_NODE") or shutil.which("node") or ""


def _find_chrome():
    p = os.getenv("MEMO_CHROME")
    if p and os.path.isfile(p):
        return p
    for c in CHROME_CANDIDATES:
        if os.path.isfile(c):
            return c
    return ""


def _find_playwright():
    p = os.getenv("MEMO_PLAYWRIGHT_CORE")
    if p and os.path.isdir(p):
        return p
    cand = os.path.join(HERE, "node_modules", "playwright-core")
    if os.path.isdir(cand):
        return cand
    for base in (os.getenv("NODE_PATH") or "").split(os.pathsep):
        c = os.path.join(base, "playwright-core")
        if base and os.path.isdir(c):
            return c
    return ""


def dep_report():
    """每个依赖一项：{name, ok, detail, needed, install}。

    🔴 `install` = 这个依赖用**哪个包管理器**能一键装上：
       · `"pip"` —— Python 包（qq-botpy / python-dotenv / markdown / pillow）
       · `"npm"` —— Node 包（playwright-core，随包的 node_modules 里装）
       · `""`    —— 装不了（node 本身、系统浏览器）——这些只能引导用户自己装
       界面据此决定「一键安装」按钮要不要显形、装什么（见 memo_gui）。
    """
    out = []
    for pkg, mod, why in PY_DEPS:
        ok = importlib.util.find_spec(mod) is not None
        out.append({"name": pkg, "ok": ok, "needed": "必需" in why,
                    "install": "pip",
                    "detail": why if ok else f"没装（{why}）"})
    node = _find_node()
    out.append({"name": "node", "ok": bool(node), "needed": False,
                "install": "",
                "detail": node or "没找到 —— 渲染成图片要用；找不到会退回文件卡片"})
    chrome = _find_chrome()
    out.append({"name": "Chrome / Edge", "ok": bool(chrome), "needed": False,
                "install": "",
                "detail": chrome or "没找到 —— 渲染成图片要用"})
    pwc = _find_playwright()
    # 🔴 playwright-core 是 **Node 包**（不是 pip 包）—— 以前标成「装不了」，
    #    结果页面上它明明缺着、却没有一键安装入口（用户 2026-10-12 报的
    #    「依赖自检依然没有一键安装的方法」）。它可以用 `npm install` 装。
    out.append({"name": "playwright-core", "ok": bool(pwc), "needed": False,
                "install": "npm",
                "detail": pwc or "没找到 —— 渲染成图片要用（下面可以一键装）"})
    return out


# ── .env 键表（白名单：页面只能改这些）────────────────────────────
ENV_SPEC = {
    "QQ_BOT_APPID": {
        "label": "AppID", "group": "bot", "required": True,
        "help": "QQ 机器人 AppID —— 在 https://q.qq.com 创建机器人后，「开发设置」里拿",
        "check": lambda v: None if re.fullmatch(r"\d{5,}", v) else "应该是一串数字（纯数字）",
    },
    "QQ_BOT_SECRET": {
        "label": "AppSecret", "group": "bot", "required": True, "secret": True,
        "help": "机器人密钥（不是 Token）。已保存后这里不再显示，要改就重新填一遍",
        "check": lambda v: None if len(v) >= 8 else "看起来太短了（应该是一长串随机字符）",
    },
    # 🔴 下面三个键都标 hidden：页面上**不再有裸 openid 输入框**。
    #    播报目标改成一个「已发现的会话」勾选列表（见 pushTargetsHTML()），
    #    勾选/取消勾选由 /api/push 直接写 .env —— 用户不该被要求手抄 openid。
    "QQ_PUSH_TARGETS": {
        "label": "播报目标（勾选列表管理）", "group": "bot", "hidden": True,
        "help": "启动播报发到哪些会话。格式 group:<openid>;c2c:<openid>。"
                "由「已发现的会话」列表勾选写入，一般不用手改。",
        "check": lambda v: None,
    },
    "QQ_GROUP_OPENID": {
        "label": "目标群 openid（旧键）", "group": "bot", "hidden": True,
        "help": "旧版遗留键。仍可识别 —— QQ_PUSH_TARGETS 没配时按群目标使用。",
        "check": lambda v: None,
    },
    "QQ_C2C_OPENID": {
        "label": "目标私聊用户 openid（旧键）", "group": "bot", "hidden": True,
        "help": "旧版遗留键。仍可识别 —— QQ_PUSH_TARGETS 没配时按私聊目标使用。",
        "check": lambda v: None,
    },
    "QQ_BOT_SANDBOX": {
        "label": "沙箱环境", "group": "bot", "kind": "bool",
        "help": "QQ 开放平台的调试开关，默认关着，别动它。"
                "开着 = 机器人只对你后台加过白名单的测试群可见；给别人用必须关掉。",
        "check": lambda v: None,
    },
    "MEMO_TRIGGER": {
        "label": "待办触发词", "group": "behavior", "default": "待办",
        "help": "消息以它开头就记为一条待办（私聊、群聊都认）",
        "check": lambda v: None if (v and " " not in v) else "不能为空、不能含空格",
    },
    "MEMO_SEARCH_ROOT": {
        "label": "「发文件」搜索根目录", "group": "files", "kind": "paths",
        "default": os.path.expanduser("~"),
        "help": "「发文件」只在这些目录下面找文件。可以填多个，一行一个（换行分隔）。"
                "目录越大搜得越慢 —— 别指向主目录（里面常有上百万个文件），"
                "指向你真正放文档的地方",
        "check": check_roots,
    },
    "MEMO_IGNORE": {
        "label": "忽略关键词（逗号分隔）", "group": "behavior", "default": "",
        "help": "消息里出现这些词就整条跳过（不收灵感、也不响应）。"
                "同群有别的机器人时，把它的名字填进来，免得俩机器人互相刷屏",
        "check": lambda v: None,
    },
}

# 分组顺序 = 页签顺序 = 页面一次只显示一块
# 🔴 分块原则：一个页签的内容要能**一屏装下**（不上下拉）。
#    所以「播报文案」与「回执文案」分开 —— 三段长文案塞一块必然超屏。
GROUPS = [
    ("bot", "① 连接 QQ", "在 https://q.qq.com 创建机器人后，把凭据填进来。必填。"),
    # 🔴 标题不叫「群内行为」—— 这一块的设置在**私聊和群聊都生效**（触发词、
    #    回执、忽略表对两种会话一视同仁），叫「群内」会让人以为私聊不管用。
    ("behavior", "② 消息行为", "待办触发词、记下待办 / 收到灵感后回的那句话，以及"
                              "忽略表（别把同群其他机器人的话当内容收）。"
                              "这些设置私聊和群聊都生效。"),
    ("msg", "③ 播报", "机器人上线时说什么、第一次连上时怎么介绍自己，"
                  "以及播报发到哪些会话。"),
    ("files", "④ 发文件", "「发文件」功能的检索范围。"),
    ("bubbles", "⑤ 脑内抽屉", "轮播页里放哪几类（待办 / 灵感）。"),
    ("boomerang", "⑥ 回旋镖", "发一条「回旋镖」，把记过的旧账翻一条推回来给你看。"
                             "下面两项是它的细节设置；推送范围跟「⑤ 脑内抽屉」的"
                             "展示范围是同一套分类。"),
    ("runtime", "⑦ 运行方式", "开机自启、机器人进程、依赖自检。"),
    ("moyu", "⑧ 摸鱼库", "「摸鱼」随机抽的那些内容，一行一条。改完保存立刻生效，不用重启。"),
    ("ext", "⑨ 自定义指令", "导入别人做好的指令包（.py / .zip），或把你自己的指令导出分享。"
                          "装完要重启机器人才生效。"),
    ("version", "⑩ 版本", "当前版本、检查更新、更新日志。装到同一目录数据不受影响。"),
]


def state_fields():
    """页面要渲染的**全部可编辑字段**（按分组顺序），env 与 settings 混在一起。

    每个字段带 `target`：'env' 表示写 `.env`，'settings' 表示写 `settings.json`。
    前端只负责画，分流逻辑在后端，页面本身不含任何键名硬编码。
    """
    envfile = read_env_file()
    out = []

    for key, spec in ENV_SPEC.items():
        if spec.get("hidden"):
            # 由自绘区块（勾选列表）管理的键，不进表单 —— 免得和勾选列表两份真源打架
            continue
        raw = envfile.get(key)
        if raw is None:
            raw = os.getenv(key)
        if raw is None:
            raw = spec.get("default", "")
        if spec.get("kind") == "bool":
            kind = "bool"
        elif spec.get("kind") == "paths":
            kind = "paths"
        elif spec.get("path"):
            kind = "path"
        else:
            kind = "text"
        if spec.get("secret"):
            kind = "secret"
        out.append({
            "target": "env", "key": key, "label": spec["label"], "help": spec.get("help", ""),
            "group": spec["group"], "kind": kind, "required": bool(spec.get("required")),
            "value": "" if spec.get("secret") else str(raw),
            "set": bool(str(raw).strip()),
            "default": spec.get("default", ""),
        })

    group_of = {"startup_msg": "msg", "intro_msg": "msg",
                "memo_ack": "behavior", "idea_ack": "behavior",
                "bubbles_show": "bubbles", "ui_font_scale": "bubbles",
                # 收图开关与报警阈值都归「② 群内行为」—— 收图是对收消息行为的开关。
                "image_alert_gb": "behavior",
                "boomerang_days": "boomerang", "boomerang_scope": "boomerang"}
    # 🔴 收图入口暂隐（用户要求）：`image_collect` / `image_alert_gb` 两个键
    #    **仍然进字段表**（否则 `S.fields` 里没有它们，`setDirty` 认不出键，
    #    保存时会静默丢掉改动），只是打 `hidden` 让所有渲染器跳过 —— 界面上
    #    再也看不到收图开关。
    #    实现代码全部保留：自绘函数 `imageCollectHTML()` 和 `bindFields()` 里的
    #    接线都还在，入口要恢复只需把 behaviorSection 里那行拼接加回来。
    # 🔴 踩过：一开始是直接 `continue` 跳过它，结果 `S.fields` 里没有这个键，
    #    `setDirty()` 第一行 `if(!f) return;` 就把每次改动吞了，保存永远说「没有改动」。
    hidden_settings = {"image_collect", "image_alert_gb"}
    for s in _settings.describe():
        out.append({
            "target": "settings", "key": s["key"], "label": s["label"], "help": s["help"],
            "group": group_of.get(s["key"], "behavior"), "kind": s["kind"],
            "required": False, "value": s["value"], "set": _settings.is_set(s["key"]),
            "default": s["default"], "choices": s["choices"],
            "choice_labels": s["choice_labels"],
            "min": s.get("min"), "max": s.get("max"),
            "hidden": s["key"] in hidden_settings,
        })
    return out


def full_state():
    pid = bot_running()
    st = task_state()
    envfile = read_env_file()
    appid = (envfile.get("QQ_BOT_APPID") or os.getenv("QQ_BOT_APPID") or "").strip()
    secret = (envfile.get("QQ_BOT_SECRET") or os.getenv("QQ_BOT_SECRET")
              or os.getenv("QQ_BOT_TOKEN") or "").strip()
    raw_root = (envfile.get("MEMO_SEARCH_ROOT") or os.getenv("MEMO_SEARCH_ROOT")
                or os.path.expanduser("~")).strip()
    roots = split_roots(raw_root)
    return {
        "ok": True,
        "fields": state_fields(),
        "groups": [{"key": k, "title": t, "desc": d} for k, t, d in GROUPS],
        # 🔴 `autostart` 是「开机自启开关」的**唯一判据**（= 计划任务存在且可用）。
        #    以前界面拿 `task == "running"` 判断 —— 自启任务注册后状态是 **ready**
        #    而不是 running，于是「明明开了却显示关闭」（用户 2026-10-12 报的）。
        "bot": {"running": bool(pid), "pid": pid, "task": st, "task_name": TASK_NAME,
                "autostart": autostart_state()},
        "deps": dep_report(),
        "ignore": {"env": _ignore.env_rules(), "file": _ignore.file_rules()},
        "moyu": {
            "source": _moyu.source(),                 # 'builtin' / 'user'
            "path": _moyu.user_path(),
            "groups": _moyu.as_texts(),               # {cat: [文本, ...]}
            "cats": [{"key": c, "name": _moyu.CAT_NAME[c], "icon": _moyu.CAT_ICON[c],
                      "help": _moyu.CAT_HELP[c]} for c in _moyu.CATS],
            "max_text_len": _moyu.MAX_TEXT_LEN,
            "max_per_cat": _moyu.MAX_PER_CAT,
            "builtin_total": len(_moyu.builtin_items()),
        },
        "paths": {"env": ENV_PATH, "settings": _settings.file_path(),
                  "search_root": roots[0] if roots else "",
                  "search_roots": roots, "here": HERE,
                  "data_dir": data_dir(), "memos_db": data_path("memos.db"),
                  "bot_log": BOT_LOG},
        "ready": bool(appid and secret),
        "version": _version.VERSION,
        "repo": _version.REPO,
        # 已发现的会话（群 + 私聊统一一张表）+ 当前播报目标
        # 🔴 目标用 `group:<id>` / `c2c:<id>` 字符串形态给前端 —— 和 .env 里存的一致，
        #    勾选判断直接 `includes()`，不用再拼。
        "sessions": _push.sessions(),
        "push_targets": [f"{sc}:{oid}" for sc, oid in _push.targets()],
        "push_env_key": "QQ_PUSH_TARGETS",
        # 自定义指令（扩展包）：已装的列表 + 安装目录。
        # 🔴 `list_items()` 会开一个**子进程**去试加载（扩展是任意代码，不能在本进程
        #    exec）—— 结果按文件 mtime 缓存，所以只有「刚导入 / 手改过」那一次慢。
        "ext": {
            "items": _extmgr.list_items(),
            "dir": _extmgr.LOCAL_DIR,
            "accept": list(_extmgr.ACCEPT_EXT),
        },
    }


