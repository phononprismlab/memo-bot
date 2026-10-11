"""配置中心（本地网页）—— 傻瓜化改配置：不用碰 `.env`，不用记命令。

这个文件是干嘛的：起一个**只监听 127.0.0.1** 的小服务，把一个配置页面摆出来。
页面上能做的事：
  ① 填 QQ 机器人凭据（AppID / AppSecret / 目标群 openid / 沙箱）  → 写 `.env`
  ② 改备忘触发词、启动播报文案                                  → 写 `.env` / `settings.json`
  ③ 选「发文件」的搜索根目录（带目录浏览器 + 实测：多少文件、搜一次多久）
  ④ 勾脑内抽屉的展示范围                                        → 写 `settings.json`
  ⑤ 一键开关「开机自启」（注册 / 删除 Windows 计划任务 `MoyuBot`）
  ⑥ 启动 / 停止 / 重启机器人，并自检依赖是否装齐
  ⑦ 管理忽略表（别把同群其他机器人当内容收）
  ⑧ 改「摸鱼」随机抽的内容库（一行一条；改完立刻生效，不用重启）
  ⑨ 导入 / 列出 / 移除自定义指令（扩展包 `.py` / `.zip`；装完重启才生效）

🔴 只监听 127.0.0.1 —— 这个页面能读写 `.env`（含密钥），绝不对外暴露。
🔴 AppSecret 读回来只显示「已设置」，**不回显明文**；要改必须重新输入一遍。
🔴 写 `.env` 是**按行改**：原有注释与顺序原样保留，已有键只换值，新键追加到末尾。
   写前自动备份 `.env.bak`（已 gitignore）。
🔴 所有写操作都要求自定义请求头 `X-Memo-Config: 1` —— 别的网页跨域打过来会先触发
   preflight，而我们不回任何 CORS 头，浏览器直接拒发，等于天然 CSRF 防护。

用法：
  python config-web.py               # 起服务并打开浏览器（桌面「配置中心.bat」走这条）
  python config-web.py --no-browser
  python config-web.py --port 8770
"""
import argparse
import base64
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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

# 页面要显示的本地图片：URL 路径 → 仓库里的相对路径。
# 🔴 **白名单**（不吃任意路径）—— 新增图片必须显式加到这里，别写成通配。
DOC_IMAGES = {
    # QQ 群里那两个开关的位置截图（用在「① 连接 QQ」的提示块里）
    "/docs/qq-group-switches.png": os.path.join("docs", "qq-group-switches.png"),
}


def image_mime(blob):
    """按**文件头**判图片类型，不看扩展名。

    🔴 现有那张 `qq-group-switches.png` 其实是 JPEG（扩展名和内容对不上）。
       照扩展名发 `image/png` 浏览器多半也能显示（会嗅探），但既然要发就得发对。
    """
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if blob[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if blob[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


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
LAUNCHER = os.path.join(HERE, "memo-launcher.py")  # 统一启动器（补 sys.path + 定数据目录）
BOT_LOG = data_path("bot.out.log")
PID_FILE = data_path("bot.pid")
TASK_NAME = "MoyuBot"
DEFAULT_PORT = 8770
PORT_TRIES = 12
HEADER_FLAG = "X-Memo-Config"

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
            v = ";".join(p.replace("\\", "/") for p in split_roots(v))
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
    task_state, autostart_set, _pythonw, _powershell,
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
    """每个依赖一项：{name, ok, detail, needed}。"""
    out = []
    for pkg, mod, why in PY_DEPS:
        ok = importlib.util.find_spec(mod) is not None
        out.append({"name": pkg, "ok": ok, "needed": "必需" in why,
                    "detail": why if ok else f"没装 —— pip install -r requirements.txt（{why}）"})
    node = _find_node()
    out.append({"name": "node", "ok": bool(node), "needed": False,
                "detail": node or "没找到 —— 渲染成图片要用；找不到会退回文件卡片"})
    chrome = _find_chrome()
    out.append({"name": "Chrome / Edge", "ok": bool(chrome), "needed": False,
                "detail": chrome or "没找到 —— 渲染成图片要用"})
    pwc = _find_playwright()
    out.append({"name": "playwright-core", "ok": bool(pwc), "needed": False,
                "detail": pwc or "没找到 —— 在 memo-bot 下跑一次 npm install 即可"})
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
        "label": "备忘触发词", "group": "behavior", "default": "备忘",
        "help": "群消息以它开头就记为一条备忘",
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
    ("behavior", "② 群内行为", "备忘触发词、记下备忘 / 收到灵感后回的那句话，以及"
                              "忽略表（别把同群其他机器人的话当内容收）。"),
    ("msg", "③ 播报", "机器人上线时说什么、第一次连上时怎么介绍自己，"
                  "以及播报发到哪些会话。"),
    ("files", "④ 发文件", "「发文件」功能的检索范围。"),
    ("bubbles", "⑤ 脑内抽屉", "轮播页里放哪几类（备忘 / 灵感）。"),
    ("boomerang", "⑥ 回旋镖", "把 N 天前的旧记录翻出来推回给用户（发「回旋镖」触发）。"
                              "回溯天数填 0 就翻今天记的 —— 刚上手也能玩。"),
    ("runtime", "⑦ 运行方式", "开机自启、机器人进程、依赖自检。"),
    ("moyu", "⑧ 摸鱼库", "「摸鱼」随机抽的那些内容，一行一条。改完保存立刻生效，不用重启。"),
    ("ext", "⑨ 自定义指令", "导入别人做好的指令包（.py / .zip），或把你自己的指令导出分享。"
                          "装完要重启机器人才生效。"),
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
                # 🔴 `image_collect` 本身**不进表单**：它是一个 multi 键顶三个开关，
                #    由自绘区块（主开关 + 两个子开关）画，和「① 连接 QQ」的勾选列表一个套路。
                "image_alert_gb": "behavior",
                "boomerang_days": "boomerang", "boomerang_scope": "boomerang"}
    # 自绘区块接管的 settings 键：**仍然进字段表**（否则页面拿不到它的值、
    # `setDirty` 也认不出这个键 —— 保存时会静默丢掉），但打上 `hidden` 让通用渲染器跳过。
    # 🔴 踩过：一开始是直接 `continue` 跳过它，结果 `S.fields` 里没有这个键，
    #    `setDirty()` 第一行 `if(!f) return;` 就把每次改动吞了，保存永远说「没有改动」。
    self_drawn = {"image_collect"}
    for s in _settings.describe():
        out.append({
            "target": "settings", "key": s["key"], "label": s["label"], "help": s["help"],
            "group": group_of.get(s["key"], "behavior"), "kind": s["kind"],
            "required": False, "value": s["value"], "set": _settings.is_set(s["key"]),
            "default": s["default"], "choices": s["choices"],
            "choice_labels": s["choice_labels"],
            "min": s.get("min"), "max": s.get("max"),
            "hidden": s["key"] in self_drawn,
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
        "bot": {"running": bool(pid), "pid": pid, "task": st, "task_name": TASK_NAME},
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


# ── HTTP 服务 ───────────────────────────────────────────────────
def make_handler(doc):
    """请求处理器：静态页 + 一组 `/api/*`。

    🔴 所有写操作都要求自定义头 `X-Memo-Config`。别的网页跨域打过来时，
       带自定义头会先触发 CORS preflight，而我们不回任何 CORS 头 → 浏览器拒发。
    """
    class Handler(BaseHTTPRequestHandler):
        server_version = "MemoConfig/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False)
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _guard(self):
            if self.headers.get(HEADER_FLAG) == "1":
                return True
            self._send(403, {"ok": False, "error": "缺少配置页请求头（可能是别的网页在尝试跨站调用）"})
            return False

        def _json_body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        # ── GET ──────────────────────────────────────────────────
        def do_GET(self):
            from urllib.parse import parse_qs, urlparse

            u = urlparse(self.path)
            path, qs = u.path, parse_qs(u.query)
            if path in ("/", "/index.html"):
                return self._send(200, doc, "text/html; charset=utf-8")
            if path == "/api/state":
                return self._send(200, full_state())
            if path == "/api/images-usage":
                # 「② 群内行为」那块自绘收图区的只读统计行。
                # 🔴 只读、不走 POST 守卫（守卫是防**改配置**的跨站写入，读个数字没风险）。
                try:
                    u = _media.usage()
                    u["alert_gb"] = _settings.get("image_alert_gb")
                    return self._send(200, {"ok": True, "usage": u,
                                            "human": _media.human(u["bytes"])})
                except Exception as e:  # noqa: BLE001 —— 回给页面，别让服务挂掉
                    return self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            if path == "/api/browse":
                return self._send(200, browse_dirs((qs.get("path") or [""])[0]))
            if path in DOC_IMAGES:
                # 页面插图（白名单里的那几张）—— 按文件头发正确的 Content-Type
                try:
                    with open(os.path.join(HERE, DOC_IMAGES[path]), "rb") as f:
                        blob = f.read()
                except OSError:
                    return self._send(404, {"ok": False, "error": "not found"})
                return self._send(200, blob, image_mime(blob))
            return self._send(404, {"ok": False, "error": "not found"})

        # ── POST ─────────────────────────────────────────────────
        def do_POST(self):
            from urllib.parse import urlparse

            path = urlparse(self.path).path
            if path == "/api/quit":
                if not self._guard():
                    return
                self._send(200, {"ok": True})
                print("  收到停止请求，配置中心退出。", flush=True)
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if not self._guard():
                return
            try:
                payload = self._json_body()
            except (ValueError, OSError) as e:
                return self._send(400, {"ok": False, "error": f"请求体解析失败：{e}"})

            try:
                if path == "/api/env":
                    vals = payload.get("values")
                    if not isinstance(vals, dict):
                        return self._send(400, {"ok": False, "error": "缺少 values 对象"})
                    keys, err = write_env(vals)
                    if err:
                        return self._send(400, {"ok": False, "error": err})
                    print(f"  [配置] 已写 .env：{', '.join(keys) or '（无改动）'}", flush=True)
                    return self._send(200, {"ok": True, "keys": keys, "state": full_state()})

                if path == "/api/settings":
                    vals = payload.get("values")
                    if not isinstance(vals, dict):
                        return self._send(400, {"ok": False, "error": "缺少 values 对象"})
                    n, errs = _settings.apply_values(vals)
                    if errs:
                        return self._send(400, {"ok": False, "error": "；".join(errs)})
                    print(f"  [配置] 已写 settings.json：{n} 项", flush=True)
                    return self._send(200, {"ok": True, "keys": n, "state": full_state()})

                if path == "/api/autostart":
                    ok, msg = autostart_set(bool(payload.get("on")))
                    print(f"  [配置] 开机自启 -> {payload.get('on')}：{msg}", flush=True)
                    code = 200 if ok else 500
                    return self._send(code, {"ok": ok, "message": msg, "state": full_state()})

                if path == "/api/bot":
                    act = (payload.get("action") or "").strip()
                    fn = {"start": bot_start, "stop": bot_stop, "restart": bot_restart}.get(act)
                    if fn is None:
                        return self._send(400, {"ok": False, "error": f"不认识的动作：{act}"})
                    ok, msg = fn()
                    print(f"  [配置] 机器人 {act}：{msg}", flush=True)
                    return self._send(200 if ok else 500,
                                      {"ok": ok, "message": msg, "state": full_state()})

                if path == "/api/check-root":
                    roots = split_roots(payload.get("path"))
                    if not roots:
                        return self._send(400, {"ok": False, "error": "还没填目录"})
                    total, secs, stopped = 0, 0.0, False
                    for r in roots:
                        rp = os.path.expanduser(r)
                        if not os.path.isdir(rp):
                            return self._send(400, {"ok": False, "error": f"这个目录不存在：{r}"})
                        n, sec, st = count_files(rp)
                        total += n
                        secs += sec
                        stopped = stopped or st
                    print(f"  [配置] 统计 {len(roots)} 个目录：{total} 个文件 / {round(secs, 3)}s",
                          flush=True)
                    return self._send(200, {"ok": True, "count": total,
                                            "seconds": round(secs, 3), "truncated": stopped})

                if path == "/api/update-check":
                    cur = _version.VERSION
                    far, err = _version.latest()
                    if not far:
                        return self._send(200, {"ok": False, "current": cur, "error": err})
                    return self._send(200, {
                        "ok": True, "current": cur, "latest": far,
                        "has_update": _version.compare_version(far, cur) > 0,
                        # ⬇ 直接下载安装包（GitHub Releases 直链；设了 MEMO_DOWNLOAD_BASE 就走自建源）
                        "download": _version.download_url(far),
                        "asset": _version.asset_name(far),
                        # 直链打不开时的兜底：这一版的 Release 页 / 全部 Releases
                        "release": _version.release_url(far),
                        "releases": _version.releases_url(),
                        "url": _version.HOMEPAGE.format(repo=_version.REPO),
                    })

                if path == "/api/push":
                    # 勾选 / 取消勾选一个播报目标（写 .env 的 QQ_PUSH_TARGETS）。
                    # 🔴 群和私聊**同一个键**：`group:<id>;c2c:<id>`。
                    #    第一次勾选时 `_push.targets()` 会把旧键（QQ_GROUP_OPENID /
                    #    QQ_C2C_OPENID）的目标一起读进来 → 老用户的配置不会丢。
                    sc = str(payload.get("scope") or "").strip()
                    oid = str(payload.get("openid") or "").strip()
                    if sc not in _push.SCOPES or not oid:
                        return self._send(400, {"ok": False, "error": "缺少 scope / openid"})
                    on = bool(payload.get("on", True))
                    cur = _push.targets()
                    item = (sc, oid)
                    if on and item not in cur:
                        cur.append(item)
                    elif not on:
                        cur = [t for t in cur if t != item]
                    _keys, err = write_env({"QQ_PUSH_TARGETS": _push.format_targets(cur)})
                    if err:
                        return self._send(400, {"ok": False, "error": err})
                    print(f"  [配置] 播报目标 {'＋' if on else '－'} {sc}:{oid}"
                          f"（共 {len(cur)} 个）", flush=True)
                    return self._send(200, {
                        "ok": True,
                        "message": ("已加入播报目标（重启机器人后生效）" if on
                                    else "已移出播报目标"),
                        "state": full_state()})

                if path == "/api/session":
                    # 改某个会话的备注名（只存本机 sessions.json，平台拿不到名字）
                    sc = str(payload.get("scope") or "").strip()
                    oid = str(payload.get("openid") or "").strip()
                    if sc not in _push.SCOPES or not oid:
                        return self._send(400, {"ok": False, "error": "缺少 scope / openid"})
                    name = str(payload.get("name") or "").strip()
                    if not _push.set_name(sc, oid, name):
                        return self._send(400, {"ok": False, "error": "备注名保存失败"})
                    print(f"  [配置] 备注名 {sc}:{oid} -> {name or '（清空）'}", flush=True)
                    return self._send(200, {"ok": True, "message": "备注名已保存",
                                            "state": full_state()})

                if path == "/api/moyu":
                    if payload.get("reset"):
                        ok, msg = _moyu.reset_items()
                        return self._send(200 if ok else 400,
                                          {"ok": ok, "message": msg if ok else None,
                                           "error": None if ok else msg,
                                           "state": full_state()})
                    groups = payload.get("groups")
                    if not isinstance(groups, dict):
                        return self._send(400, {"ok": False, "error": "要指定 groups 或 reset"})
                    ok, info = _moyu.save_items(groups)
                    if not ok:
                        return self._send(400, {"ok": False, "error": info})
                    return self._send(200, {"ok": True,
                                            "message": f"摸鱼库已保存（{info} 条），下次发「摸鱼」立刻生效",
                                            "state": full_state()})

                if path == "/api/ext/import":
                    # 上传导入：浏览器把文件读成 base64 一起 POST 过来（不走 multipart，
                    # 省掉一个解析器）。体积上限与 extmgr 里一致。
                    name = str(payload.get("name") or "")
                    raw = payload.get("data") or ""
                    try:
                        blob = base64.b64decode(raw, validate=False) if raw else b""
                    except Exception:                                 # noqa: BLE001
                        return self._send(400, {"ok": False, "error": "文件内容解不开（base64 坏了）"})
                    if len(blob) > _extmgr.MAX_ZIP_BYTES:
                        return self._send(400, {
                            "ok": False,
                            "error": f"文件太大了（{len(blob) // 1024 // 1024}MB，"
                                     f"上限 {_extmgr.MAX_ZIP_BYTES // 1024 // 1024}MB）"})
                    ok, msg, info = _extmgr.import_bytes(
                        name, blob, overwrite=bool(payload.get("overwrite")))
                    print(f"  [扩展] 导入 {name}：{msg}", flush=True)
                    code = 200 if ok else (409 if info.get("conflict") else 400)
                    return self._send(code, {"ok": ok, "message": msg if ok else None,
                                             "error": None if ok else msg,
                                             "conflict": bool(info.get("conflict")),
                                             "item": info,
                                             "state": full_state()})

                if path == "/api/ext/remove":
                    ident = str(payload.get("id") or "")
                    ok, msg = _extmgr.remove(ident)
                    print(f"  [扩展] 移除 {ident}：{msg}", flush=True)
                    return self._send(200 if ok else 400,
                                      {"ok": ok, "message": msg if ok else None,
                                       "error": None if ok else msg,
                                       "state": full_state()})

                if path == "/api/open-folder":
                    # 🔴 只允许打开白名单里的这几个路径 —— 别让页面变成「打开任意目录」的口子
                    what = str(payload.get("what") or "")
                    targets = {"data": data_dir(), "db": data_path("memos.db"),
                               "env": ENV_PATH, "settings": _settings.file_path(),
                               "log": BOT_LOG, "moyu": _moyu.user_path(),
                               "ext": _extmgr.LOCAL_DIR}
                    t = targets.get(what)
                    if not t:
                        return self._send(400, {"ok": False, "error": "不认识的路径"})
                    try:
                        t = os.path.abspath(t)
                        if os.path.isfile(t):
                            # 选中文件本身（资源管理器 /select 不接受长路径时退回打开父目录）
                            subprocess.Popen(["explorer", "/select," + os.path.normpath(t)])
                        else:
                            d = t if os.path.isdir(t) else os.path.dirname(t)
                            if not os.path.isdir(d):
                                return self._send(404, {"ok": False, "error": "路径不存在：" + d})
                            opener = getattr(os, "startfile", None)
                            if opener is None:
                                return self._send(500, {"ok": False, "error": "这个系统不支持"})
                            opener(d)
                        return self._send(200, {"ok": True, "path": t})
                    except OSError as e:
                        return self._send(500, {"ok": False, "error": str(e)})

                if path == "/api/ignore":
                    if payload.get("clear"):
                        n = _ignore.clear()
                        return self._send(200, {"ok": True, "message": f"已清空 {n} 条",
                                                "state": full_state()})
                    rule = str(payload.get("add") or "").strip()
                    if rule:
                        if len(rule) < _ignore.MIN_RULE_LEN:
                            return self._send(400, {"ok": False,
                                                    "error": f"至少 {_ignore.MIN_RULE_LEN} 个字，太短会误伤一大片"})
                        cur = _ignore.file_rules()
                        if any(r.lower() == rule.lower() for r in cur + _ignore.env_rules()):
                            return self._send(400, {"ok": False, "error": f"「{rule}」已经在列表里了"})
                        cur.append(rule)
                        _ignore._save(cur)
                        return self._send(200, {"ok": True, "message": f"已加入「{rule}」",
                                                "state": full_state()})
                    rule = str(payload.get("remove") or "").strip()
                    if rule:
                        if any(r.lower() == rule.lower() for r in _ignore.env_rules()):
                            return self._send(400, {"ok": False,
                                                    "error": f"「{rule}」来自 .env 的忽略关键词，"
                                                             "要去上面那一栏删掉它"})
                        cur = _ignore.file_rules()
                        left = [r for r in cur if r.lower() != rule.lower()]
                        _ignore._save(left)
                        return self._send(200, {"ok": True, "message": f"已移除「{rule}」",
                                                "state": full_state()})
                    return self._send(400, {"ok": False, "error": "要指定 add / remove / clear"})
            except Exception as e:  # noqa: BLE001 —— 回给页面，别让服务挂掉
                return self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            return self._send(404, {"ok": False, "error": "not found"})

    return Handler


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:,">
<title>摸鱼bot · 配置中心</title>
<style>
  :root{
    --ink:#e6edf3; --dim:#8b98a9; --line:rgba(255,255,255,.12);
    --ok:#4fd1c5; --warn:#f6ad55; --bad:#fc8181;
  }
  *{box-sizing:border-box}
  html,body{height:100%}
  body{
    margin:0; padding:0; color:var(--ink); overflow:hidden;
    display:flex; flex-direction:column;
    font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei","PingFang SC",sans-serif;
    background:radial-gradient(ellipse 120% 80% at 50% -10%, #17334f 0%, #0d1a2b 45%, #070a11 100%);
    background-attachment:fixed; -webkit-font-smoothing:antialiased;
    /* 🔴 「界面字号」缩放：整页 zoom。桌面版（memo_gui.py）读同一个设置键。
       高度**不要**再除一次 zoom —— 实测 Chromium 的 zoom 只缩放内容，
       不缩放 body 自身那个盒子的高度；再除一次的话 body 会矮一截，
       底部保存条会悬在半空（真机截图确认过）。 */
    zoom:var(--z,1);
  }
  /* 🔴 一屏一块的骨架：头 / 页签 / 底部按钮条都固定，只有中间内容区滚动。
     这样切页签时页面不会跳，也不会出现「一路往下拉看不到头」的长页面。
     🔴 宽度定 880 是为了让九个页签排得下一行（见下面 .tab 的说明）。 */
  .wrap{flex:1 1 auto; min-height:0; display:flex; flex-direction:column;
        width:100%; max-width:880px; margin:0 auto; padding:0 18px}
  header{flex:0 0 auto; padding:14px 0 8px}
  header h1{margin:0; font-size:18px; font-weight:700; letter-spacing:.3px}
  header h1 span{color:var(--dim); font-weight:400; font-size:12.5px; margin-left:8px}
  .statusline{display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-top:10px}
  .dot{width:9px; height:9px; border-radius:50%; background:var(--dim); flex:0 0 auto}
  .dot.on{background:var(--ok); box-shadow:0 0 10px rgba(79,209,197,.75)}
  .dot.off{background:var(--bad)}
  .hint{
    margin-top:10px; padding:10px 14px; border-radius:12px; font-size:12.5px;
    background:rgba(246,173,85,.1); border:1px solid rgba(246,173,85,.4); color:#f6c98a;
  }
  .hint.good{background:rgba(79,209,197,.1); border-color:rgba(79,209,197,.38); color:#8fe6dc}

  /* 页签条：一次只显示一块，避免长页面上下拉。
     🔴 未配置 QQ 凭据时整条隐藏（只留「① 连接 QQ」）。 */
  .tabs{
    flex:0 0 auto; display:flex; gap:5px; flex-wrap:wrap; padding:2px 0 8px; margin:0;
  }
  .tabs.hide{display:none}
  /* 分区内部的次级切换（摸鱼库的「放松 / 摸鱼 / 冷知识」） */
  .tabs.mini{padding:0 0 8px}
  /* 🔴 九个分区必须排成一行。排不下就会折成两行、白白吃掉 40px 高度，
     把「① 连接 QQ」「⑦ 运行方式」挤到一屏之外。所以这里刻意压紧：
     字号 12px、横向内边距 10px —— 改标题时留意总量别超过内容宽（约 784px）。 */
  .tab{
    padding:5px 10px; border-radius:9px; cursor:pointer; white-space:nowrap;
    font:12px/1.2 inherit; color:var(--dim); transition:.15s;
    background:rgba(255,255,255,.05); border:1px solid var(--line);
  }
  .tab:hover{background:rgba(255,255,255,.13); color:var(--ink)}
  .tab.on{background:rgba(79,209,197,.16); border-color:rgba(79,209,197,.5);
    color:var(--ok); font-weight:600}

  /* 内容区：唯一会滚的地方（而且只在某个分区真的装不下时才滚） */
  #sections{flex:1 1 auto; min-height:0; overflow-y:auto; padding-bottom:6px}

  section{
    margin:0 0 12px; border-radius:16px; overflow:hidden;
    background:rgba(20,28,42,.82); border:1px solid var(--line);
  }
  section > h2{
    margin:0; padding:12px 18px 0; font-size:14.5px; font-weight:700;
  }
  section > .desc{padding:3px 18px 0; font-size:12px; color:var(--dim)}
  section > .body{padding:4px 18px 12px}
  .f{padding:10px 0; border-bottom:1px solid rgba(255,255,255,.07)}
  .f:last-child{border-bottom:none}
  .f .lb{font-size:13px; font-weight:600; display:flex; align-items:center; gap:7px}
  .f .lb .req{font-size:10.5px; color:var(--warn); border:1px solid rgba(246,173,85,.5);
    border-radius:5px; padding:0 5px; font-weight:400}
  .f .lb .tag{font-size:10.5px; color:var(--dim); border:1px solid var(--line);
    border-radius:5px; padding:0 5px; font-weight:400}
  .f .hp{font-size:11.5px; color:var(--dim); margin:2px 0 7px; line-height:1.55}
  .row{display:flex; gap:8px; align-items:center; flex-wrap:wrap}
  input[type=text],input[type=password]{
    flex:1 1 260px; min-width:0; padding:9px 11px; border-radius:10px;
    background:rgba(255,255,255,.06); border:1px solid var(--line); color:var(--ink);
    font:13.5px/1.4 inherit;
  }
  /* 单行文本框直接挂在 .f 下面（不在 .row 里），flex 不生效 → 会缩成默认宽度。
     这里补一条让它铺满，跟 textarea 对齐。 */
  .f > input[type=text]{width:100%}
  textarea{
    width:100%; box-sizing:border-box; padding:9px 11px; border-radius:10px;
    background:rgba(255,255,255,.06); border:1px solid var(--line); color:var(--ink);
    font:12.5px/1.7 ui-monospace,Consolas,"Courier New",monospace;
    resize:vertical; min-height:56px;
  }
  textarea:focus{outline:none; border-color:rgba(79,209,197,.55)}
  textarea::placeholder{color:#5b6675}
  input:focus{outline:none; border-color:rgba(79,209,197,.55)}
  input::placeholder{color:#5b6675}
  button{
    padding:8px 14px; border-radius:10px; cursor:pointer; white-space:nowrap;
    background:rgba(255,255,255,.07); border:1px solid var(--line); color:var(--ink);
    font:13px/1 inherit; transition:.15s;
  }
  button:hover{background:rgba(255,255,255,.14)}
  button.pri{background:rgba(79,209,197,.16); border-color:rgba(79,209,197,.5); color:var(--ok)}
  button.pri:hover{background:rgba(79,209,197,.3)}
  button.danger{border-color:rgba(252,129,129,.45); color:#fc8181}
  button.danger:hover{background:rgba(252,129,129,.14)}
  button:disabled{opacity:.45; cursor:default}
  /* 「⬇ 下载新版」是个 <a>（要能右键复制链接 / 新窗口打开），外观跟主按钮一致 */
  a.dlbtn{
    display:inline-block; padding:8px 14px; border-radius:10px; text-decoration:none;
    background:rgba(79,209,197,.16); border:1px solid rgba(79,209,197,.5); color:var(--ok);
    font:13px/1 inherit; transition:.15s;
  }
  a.dlbtn:hover{background:rgba(79,209,197,.3)}
  a.plain{color:var(--ok); text-decoration:none; border-bottom:1px dashed rgba(79,209,197,.5)}
  .chks{display:flex; gap:16px; flex-wrap:wrap}
  .chks label{display:flex; align-items:center; gap:6px; font-size:13px; cursor:pointer}
  .chks input{accent-color:#4fd1c5; width:15px; height:15px}
  .sw{display:flex; align-items:center; gap:10px}
  .sw input{width:44px; height:24px; accent-color:#4fd1c5; cursor:pointer}
  .note{font-size:12px; color:var(--dim); margin-top:7px}
  .note.ok{color:#8fe6dc}
  .note.bad{color:#fc8181}
  /* 「去 QQ 里开两个开关」提示块 —— 这两项在 QQ 那边管，配置中心碰不到它们；
     但十个用户九个不看说明书，不开机器人就「装死」，所以直接摆在连接页上。 */
  .f.warnbox{background:rgba(246,173,85,.07); border:1px solid rgba(246,173,85,.32);
             border-radius:8px; padding:11px 13px; margin:10px 0}
  .f.warnbox .lb{color:var(--warn)}
  .swlist{margin:7px 0 0 17px; padding:0; font-size:12.5px; line-height:1.5}
  .swlist li{margin-bottom:8px}
  .swlist li b{color:var(--ink)}
  .swlist .note{display:block; margin-top:2px}
  /* 收图区块（「② 群内行为」里自绘的那块）—— 主开关 + 两个子开关 + 占用行。
     主开关关掉时整块子开关收起来（.hide），免得用户以为「关了还在收」。 */
  .icsubs{display:flex; align-items:center; gap:16px; flex-wrap:wrap; margin-top:9px;
          padding-left:2px}
  .icsubs.hide{display:none}
  .icsub{display:flex; align-items:center; gap:6px; font-size:13px; cursor:pointer}
  .icsub input{accent-color:#4fd1c5; width:15px; height:15px}
  .imgusage{margin-top:9px; padding-top:8px; border-top:1px solid var(--line)}
  .imgusage .note{margin-top:0}
  /* QQ 设置页截图：原图 1179×463（约 2.55:1），按容器宽度等比缩放，不放大 */
  .swshot{display:block; width:100%; max-width:560px; height:auto; margin:9px 0 0;
          border:1px solid var(--line); border-radius:7px; background:rgba(0,0,0,.2)}
  /* 清单类（依赖自检 / 数据文件位置 / 播报目标）：单行 + 省略号，
     路径悬停看全文 —— 这样「⑦ 运行方式」才能一屏装下。 */
  .deps{display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr));
        gap:4px 18px; margin-top:4px}
  .dep{display:flex; align-items:baseline; gap:8px; font-size:12.5px; min-width:0}
  .dep b{font-weight:600; min-width:118px; flex:0 0 auto}
  .dep .m{color:var(--dim); font-size:11.5px; flex:1 1 auto; min-width:0;
          white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
  .pth{font:11.5px/1.5 ui-monospace,Consolas,"Courier New",monospace; color:var(--dim)}
  /* 「⑨ 自定义指令」里每个扩展注册了哪些指令词 —— 一个个小药丸，扫一眼就知道占没占名字 */
  .f code{font:11.5px/1.6 ui-monospace,Consolas,"Courier New",monospace;
    color:var(--ok); background:rgba(79,209,197,.10); border:1px solid rgba(79,209,197,.22);
    border-radius:5px; padding:1px 6px; margin:0 4px 3px 0; display:inline-block}
  input[type=file]{font-size:12px; color:var(--dim); max-width:280px}
  .deps .dep button{flex:0 0 auto}
  .dep > .g, .dep > .b{flex:0 0 auto}
  .dep .g{color:var(--ok)} .dep .b{color:var(--bad)}
  /* 播报目标列表：一行一个会话 —— 一行要放 勾选框 + 备注名 + 群/私聊标签 +
     id + 最近一条 + 改名按钮，挤成两列会糊成一团。 */
  .deps.one{grid-template-columns:1fr}
  .deps.one .dep{padding:6px 0; border-bottom:1px solid rgba(255,255,255,.06)}
  .deps.one .dep:last-child{border-bottom:none}
  .dep input[type=checkbox]{flex:0 0 auto; align-self:center; width:15px; height:15px;
                            accent-color:#4fd1c5; cursor:pointer}
  .dep .nm{min-width:96px; max-width:170px; overflow:hidden; text-overflow:ellipsis;
           white-space:nowrap}
  .dep .tag{flex:0 0 auto; font-size:10.5px; color:var(--dim);
            border:1px solid var(--line); border-radius:5px; padding:0 5px}
  .dep .note{margin:0; flex:1 1 auto; min-width:0; white-space:nowrap;
             overflow:hidden; text-overflow:ellipsis}
  /* 🔴 路径必须**允许收缩**（flex-shrink:1），否则长路径会顶穿网格格子、压到右边那一列上
     —— 实测 `C:/Users/Administrator/AppData/Local/memo-bot\settings.json`（59 字符）
     能顶出去 133px，连「打开」按钮一起挤出行外。
     用 0 1 auto 而不是 1 1 auto：短路径时按钮仍紧跟路径，外观不变。 */
  .dep .m.pth{flex:0 1 auto}
  .dep input.rename{flex:1 1 auto; min-width:0; padding:3px 7px; border-radius:7px;
                    background:rgba(255,255,255,.06); border:1px solid var(--line);
                    color:var(--ink); font:12.5px/1.4 inherit}
  .rules{display:flex; flex-direction:column; gap:6px; margin-top:8px}
  .rule{display:flex; align-items:center; gap:8px; font-size:12.5px;
    padding:6px 10px; border-radius:9px; background:rgba(255,255,255,.05)}
  .rule .src{margin-left:auto; font-size:11px; color:var(--dim)}
  .rule button{padding:4px 9px; font-size:11.5px}

  .savebar{
    flex:0 0 auto; z-index:20;
    display:flex; align-items:center; gap:10px; padding:11px 18px;
    background:rgba(10,16,26,.94); border-top:1px solid var(--line);
    -webkit-backdrop-filter:blur(8px); backdrop-filter:blur(8px);
  }
  .savebar .inner{max-width:820px; margin:0 auto; width:100%;
    display:flex; align-items:center; gap:10px; flex-wrap:wrap}
  .savebar .msg{font-size:12.5px; color:var(--dim); margin-left:auto}

  #pick{position:fixed; inset:0; z-index:40; display:none; align-items:center;
    justify-content:center; padding:24px; background:rgba(4,7,12,.74)}
  #pick.on{display:flex}
  #pick .panel{width:min(560px,94vw); max-height:76vh; display:flex; flex-direction:column;
    border-radius:18px; overflow:hidden; background:rgba(20,28,42,.98);
    border:1px solid var(--line); box-shadow:0 24px 70px rgba(0,0,0,.6)}
  #pick .ph{display:flex; align-items:center; gap:10px; padding:14px 18px;
    border-bottom:1px solid var(--line)}
  #pick .ph h3{margin:0; font-size:14px}
  #pick .ph .p{font-size:11.5px; color:var(--dim); word-break:break-all; flex:1}
  #pick .pl{overflow:auto; padding:8px 10px}
  #pick .pl .d{padding:8px 12px; border-radius:9px; cursor:pointer; font-size:13px}
  #pick .pl .d:hover{background:rgba(255,255,255,.08)}
  #pick .pf{display:flex; gap:8px; padding:12px 18px; border-top:1px solid var(--line)}
  #pick .pf .sp{margin-left:auto}
  #toast{position:fixed; left:50%; bottom:96px; transform:translate(-50%,14px); z-index:60;
    max-width:80vw; padding:11px 18px; border-radius:12px; font-size:13px;
    background:rgba(26,36,52,.97); border:1px solid var(--line); color:var(--ink);
    box-shadow:0 14px 40px rgba(0,0,0,.5); opacity:0; pointer-events:none; transition:.22s}
  #toast.on{opacity:1; transform:translate(-50%,0)}
</style></head><body>
<div class="wrap">
  <header>
    <h1>🐟 摸鱼bot 配置中心<span>改完点最下面保存</span></h1>
    <div class="statusline" id="statusline"></div>
    <div id="banner"></div>
  </header>
  <div id="tabs" class="tabs hide"></div>
  <div id="sections"></div>
</div>

<div class="savebar"><div class="inner">
  <button class="pri" id="btn-save">保存</button>
  <button id="btn-save-restart">保存并重启机器人</button>
  <button class="danger" id="btn-quit">关闭配置中心</button>
  <span class="msg" id="savemsg"></span>
</div></div>

<div id="pick"><div class="panel">
  <div class="ph"><h3>选目录</h3><span class="p" id="pick-path"></span>
    <button id="pick-close">✕</button></div>
  <div class="pl" id="pick-list"></div>
  <div class="pf">
    <button id="pick-up">↑ 上一级</button>
    <button class="pri sp" id="pick-use">就用这个目录</button>
  </div>
</div></div>

<div id="toast"></div>

<script>
const FLAG = {"X-Memo-Config": "1"};
let S = null;          // /api/state
let DIRTY = {};        // key -> 新值（只放改动过的）
let PICK_TARGET = null;
let ACTIVE_TAB = null; // 当前显示的页签 key；未配置凭据时恒为 'bot'
let MOYU_CAT = null;   // 摸鱼库页签里当前看的是哪一类（relax / slack / trivia）
let MOYU_DIRTY = {};   // 摸鱼库：cat -> 输入框原文（只放改动过的）

const $ = id => document.getElementById(id);
function esc(s){ return String(s == null ? '' : s).replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

async function api(path, body){
  const opt = body === undefined ? {} :
    {method:'POST', headers:Object.assign({'Content-Type':'application/json'}, FLAG),
     body: JSON.stringify(body)};
  const r = await fetch(path, opt);
  return await r.json();
}
function toast(t){ const el = $('toast'); el.textContent = t; el.classList.add('on');
  clearTimeout(el._t); el._t = setTimeout(()=>el.classList.remove('on'), 2600); }

/* 「界面字号」：把设置里的百分比写进 CSS 变量 --z，整页跟着缩放（见 body 的 zoom）。
   桌面版 memo_gui.py 读的是同一个设置键，两边字号口径一致。
   🔴 只认 80–200，别的值一律按 100 —— 手改 settings.json 写坏也不至于把页面撑爆。 */
function applyFontScale(){
  const f = (S && S.fields || []).find(x => x.key === 'ui_font_scale');
  let v = parseInt(f ? f.value : 100, 10);
  if(!(v >= 80 && v <= 200)) v = 100;
  document.documentElement.style.setProperty('--z', String(v / 100));
}

/* ── 渲染 ─────────────────────────────────────────────────── */
function render(){
  applyFontScale();       // 保存后 render() 会重跑，字号改完立刻生效
  renderStatus();
  renderTabs();
  renderSections();
  renderBanner();
}

/* 现在能看到的页签。🔴 没配好 QQ 凭据时只放「① 连接 QQ」——
   后面那些设置没有凭据也用不上，先不给，免得一上来就是一片字。 */
function visibleGroups(){
  if(!S.ready) return S.groups.filter(g => g.key === 'bot');
  return S.groups;
}

function renderTabs(){
  const bar = $('tabs');
  if(!S.ready){ bar.classList.add('hide'); bar.innerHTML = ''; ACTIVE_TAB = 'bot'; return; }
  const gs = visibleGroups();
  if(!ACTIVE_TAB || !gs.some(g => g.key === ACTIVE_TAB)) ACTIVE_TAB = gs[0].key;
  bar.classList.remove('hide');
  bar.innerHTML = gs.map(g =>
    '<button class="tab' + (g.key === ACTIVE_TAB ? ' on' : '') + '" data-tab="' + esc(g.key) + '">'
    + esc(g.title) + '</button>').join('');
  bar.querySelectorAll('[data-tab]').forEach(b =>
    b.addEventListener('click', () => { ACTIVE_TAB = b.dataset.tab; render(); }));
}

function renderStatus(){
  const b = S.bot;
  $('statusline').innerHTML =
    '<span class="dot ' + (b.running ? 'on' : 'off') + '"></span>' +
    '<b>' + (b.running ? '机器人运行中' : '机器人已停止') + '</b>' +
    (b.running ? '<span class="note">pid ' + b.pid + '</span>' : '') +
    '<span class="note">· 开机自启：' + taskText(b.task) + '</span>' +
    '<span style="margin-left:auto"></span>' +
    '<button id="btn-start"' + (b.running ? ' disabled' : '') + '>启动</button>' +
    '<button id="btn-stop"' + (b.running ? '' : ' disabled') + '>停止</button>' +
    '<button id="btn-restart">重启</button>';
  $('btn-start').onclick = () => botAct('start');
  $('btn-stop').onclick = () => botAct('stop');
  $('btn-restart').onclick = () => botAct('restart');
}

function taskText(t){
  return {'none':'未开启','ready':'已开启','running':'已开启（运行中）',
          'disabled':'已禁用','unknown':'查不到'}[t] || t;
}

function renderBanner(){
  const el = $('banner');
  // 凭据齐了就不占这一条 —— 状态行已经写着「机器人运行中」，重复提示只是白占一屏高度
  if(S.ready){ el.innerHTML = ''; return; }
  // 后面还有哪些页签，直接从后端给的 GROUPS 里取，不手写一份（免得改了分组这里忘了改）
  const rest = S.groups.filter(g => g.key !== 'bot')
    .map(g => g.title.replace(/^[①②③④⑤⑥⑦⑧⑨⑩]\s*/, '')).join(' / ') || '其它设置';
  el.innerHTML = '<div class="hint">⚠️ 还没配好 QQ 凭据 —— 先把下面「① 连接 QQ」里的 '
    + 'AppID 和 AppSecret 填上并保存。<br>'
    + '保存之后，其余设置（' + esc(rest) + '）才会以页签的形式出现。<br>'
    + '凭据在 <a href="https://q.qq.com" target="_blank" rel="noopener" '
    + 'style="color:#f6c98a">q.qq.com</a> 创建机器人后拿。</div>';
}

/* 取一个字段的当前值（优先取页面上还没保存的改动）。
   🔴 参数是**键名字符串** —— 早先写成传字段对象，`f.key` 是 undefined，
      于是「浏览…」「测试」永远读到空串。 */
function fieldValue(key){
  if(key in DIRTY) return DIRTY[key];
  const f = S.fields.find(x => x.key === key);
  return f ? f.value : '';
}

function fieldHTML(f){
  const v = fieldValue(f.key);
  let ctl = '';
  if(f.kind === 'multi'){
    ctl = '<div class="chks">' + (f.choices || []).map(c => {
      const on = (v || []).includes(c);
      return '<label><input type="checkbox" data-k="' + f.key + '" value="' + esc(c) + '"'
        + (on ? ' checked' : '') + '>' + esc((f.choice_labels || {})[c] || c) + '</label>';
    }).join('') + '</div>';
  } else if(f.kind === 'bool'){
    const on = String(v).toLowerCase() === 'true' || String(v) === '1';
    ctl = '<div class="sw"><input type="checkbox" data-k="' + f.key + '"'
      + (on ? ' checked' : '') + '><span class="note">' + (on ? '已打开' : '已关闭') + '</span></div>';
  } else if(f.kind === 'secret'){
    ctl = '<div class="row"><input type="password" data-k="' + f.key + '" '
      + 'placeholder="' + (f.set ? '已设置 ✓（留空 = 不改；要换就重新填一遍）' : '还没填')
      + '" autocomplete="off">'
      + '<span class="note ' + (f.set ? 'ok' : 'bad') + '">'
      + (f.set ? '已设置' : '未设置') + '</span></div>';
  } else if(f.kind === 'path'){
    ctl = '<div class="row"><input type="text" data-k="' + f.key + '" value="' + esc(v) + '">'
      + '<button data-browse="' + f.key + '">浏览…</button>'
      + '<button data-test="' + f.key + '">测试</button></div>'
      + '<div class="note" data-note="' + f.key + '"></div>';
  } else if(f.kind === 'paths'){
    const lines = String(v || '').split(';').map(s => s.trim()).filter(Boolean);
    ctl = '<textarea data-k="' + f.key + '" rows="' + Math.max(2, lines.length) + '" '
      + 'spellcheck="false" placeholder="每行一个目录，例如 D:/文档">'
      + esc(lines.join('\n')) + '</textarea>'
      + '<div class="row"><button data-browse="' + f.key + '">＋ 加一个目录</button>'
      + '<button data-test="' + f.key + '">测试</button></div>'
      + '<div class="note" data-note="' + f.key + '"></div>';
  } else if(f.kind === 'area'){
    ctl = '<textarea data-k="' + f.key + '" rows="5" spellcheck="false">'
      + esc(v) + '</textarea>';
  } else if(f.kind === 'int'){
    // 数字框带上下限（后端 normalize 也会拦，这里是让用户根本填不进去越界值）
    ctl = '<input type="number" data-k="' + f.key + '" value="' + esc(v) + '"'
      + (f.min != null ? ' min="' + f.min + '"' : '')
      + (f.max != null ? ' max="' + f.max + '"' : '')
      + ' style="max-width:160px">';
  } else {
    ctl = '<input type="text" data-k="' + f.key + '" value="' + esc(v) + '">';
  }
  return '<div class="f">'
    + '<div class="lb">' + esc(f.label)
    + (f.required ? '<span class="req">必填</span>' : '')
    + (f.target === 'settings' ? '<span class="tag">设置文件</span>' : '')
    + '</div>'
    + '<div class="hp">' + esc(f.help) + '</div>' + ctl + '</div>';
}

function groupOf(key){
  return S.fields.filter(f => f.key === key && f.target === 'env').length ? 'env' : 'settings';
}

function renderSections(){
  const gs = visibleGroups();
  const g = gs.find(x => x.key === ACTIVE_TAB) || gs[0];
  let html = '';
  if(g.key === 'runtime') html = runtimeSection(g);
  else if(g.key === 'moyu') html = moyuSection(g);
  else if(g.key === 'ext') html = extSection(g);
  else if(g.key === 'bot') html = botSection(g);
  else if(g.key === 'msg') html = msgSection(g);
  else if(g.key === 'behavior') html = behaviorSection(g);
  else {
    // `hidden` 的字段不进通用表单（由自绘区块画，见 behaviorSection）——
    // 🔴 但它**留在 S.fields 里**，因为 setDirty / splitDirty 要靠它认键。
    const fs = S.fields.filter(f => f.group === g.key && !f.hidden);
    html = '<section><h2>' + esc(g.title) + '</h2><div class="desc">' + esc(g.desc)
      + '</div><div class="body">' + fs.map(fieldHTML).join('') + '</div></section>';
  }
  $('sections').innerHTML = html;
  bindFields();
  // 「② 群内行为」里的占用行是异步取的，画完再去问（不阻塞页面刷新）
  if(g.key === 'behavior') loadImageUsage(false);
}

// ⚠️ 「去 QQ 里开两个开关」—— 这两项在 QQ 那边管，配置中心碰不到它们。
//    但十个人有九个不看说明书；不开的话机器人收不到普通发言、也发不出播报，
//    用户会以为「机器人坏了」。所以直接摆在「① 连接 QQ」页上，别指望人家去翻安装指南。
function groupSwitchesHTML(){
  return '<div class="f warnbox">'
    + '<div class="lb">⚠️ 还差一步：去 QQ 里开两个开关</div>'
    + '<div class="hp">把机器人拉进群之后，用<b>手机</b>点群里的机器人头像 → 右上角「设置」'
    + '→ 打开下面这两项。这两项在 QQ 那边管，配置中心管不到它们。</div>'
    + '<ol class="swlist">'
    + '<li><b>机器人可获取的群聊消息范围</b> → 选「获取群内全部消息」'
    + '<span class="note">不开：只有 @ 它才理你，群里的普通发言它收不到</span></li>'
    + '<li><b>机器人主动在群聊内发言</b> → 打开'
    + '<span class="note">不开：它没法主动发东西 —— 上线播报发不出来</span></li>'
    + '</ol>'
    + '<img class="swshot" src="/docs/qq-group-switches.png"'
    + ' alt="QQ 群设置里这两个开关的位置：机器人可获取的群聊消息范围、机器人主动在群聊内发言">'
    + '<div class="note">开完在群里发一句「备忘 测试」，能收到回执就说明配好了。</div>'
    + '</div>';
}

function botSection(g){
  const fs = S.fields.filter(f => f.group === g.key);
  return '<section><h2>' + esc(g.title) + '</h2><div class="desc">' + esc(g.desc)
    + '</div><div class="body">' + fs.map(fieldHTML).join('')
    + groupSwitchesHTML() + '</div></section>';
}

// 「③ 播报」= 通用字段（启动播报 / 功能介绍）+ 自绘的播报目标勾选列表。
// 凭据还没齐时不显示「播报目标」—— 那时机器人根本没连上，列表必然是空的。
function msgSection(g){
  const fs = S.fields.filter(f => f.group === g.key && !f.hidden);
  return '<section><h2>' + esc(g.title) + '</h2><div class="desc">' + esc(g.desc)
    + '</div><div class="body">' + fs.map(fieldHTML).join('')
    + (S.ready ? pushTargetsHTML() : '') + '</div></section>';
}

// 收图区块挂载点（挂在「② 群内行为」）。
// 🔴 为什么收图要自绘、不走通用表单：它是一个 multi 键（`image_collect`）顶三个开关。
//    通用表单会把它画成一排两个并排的复选框，看不出「这俩关了就全关」这层关系；
//    而「主开关 + 展开两个子开关」这个形状，跟「① 连接 QQ」的勾选列表是一个套路，
//    用户看一眼就懂。所以 `image_collect` 被排除在通用字段外（见 state_fields 的 self_drawn）。
// 「② 群内行为」= 通用字段 + 自绘的收图区块（image_collect 归这组，跟消息接收行为放一起）。
function behaviorSection(g){
  const fs = S.fields.filter(f => f.group === g.key && !f.hidden);
  return '<section><h2>' + esc(g.title) + '</h2><div class="desc">' + esc(g.desc)
    + '</div><div class="body">' + fs.map(fieldHTML).join('')
    + imageCollectHTML() + '</div></section>';
}

// 自绘的收图区块。数据真源是 `image_collect` 这一个 multi 键：
//   []               = 关
//   ["c2c"]          = 仅私聊
//   ["group"]        = 仅群聊
//   ["c2c","group"]  = 都收
// 主开关 = 里面有没有东西（不是第三个键）；两个子开关都关掉 → 数组空 → 主开关天然关。
function imageCollectHTML(){
  const f = S.fields.find(x => x.key === 'image_collect');
  const v = (fieldValue('image_collect') || []);
  const on = v.length > 0;
  const sub = (k, lb) =>
    '<label class="icsub"><input type="checkbox" data-imgsub="' + k + '"'
    + (v.includes(k) ? ' checked' : '') + '>' + lb + '</label>';
  return '<div class="f">'
    + '<div class="lb">' + esc(f ? f.label : '收集图片') + '<span class="tag">设置文件</span></div>'
    + '<div class="hp">' + esc(f ? f.help : '') + '</div>'
    + '<div class="sw"><input type="checkbox" id="img-master"' + (on ? ' checked' : '') + '>'
    + '<span class="note">' + (on ? '已打开' : '已关闭') + '</span></div>'
    + '<div class="icsubs' + (on ? '' : ' hide') + '">' + sub('c2c', '私聊') + sub('group', '群聊')
    + '<span class="note">收哪边？只勾一个也行 —— 两个都取消就等于关掉。</span></div>'
    + '<div class="imgusage" id="img-usage"><span class="note">正在统计…</span></div>'
    + '</div>';
}

// 收图占用那一行。只读、不参与保存；60 秒内不重复请求（切页签会重画）。
// 🔴 单独一个请求而不是塞进 /api/state：/api/state 是保存后要回读的，
//    把「磁盘占用」这种随时在变的数字混进去，会让「有没有改动」的比对变脏。
let IMG_USAGE = null, IMG_USAGE_AT = 0;
async function loadImageUsage(force){
  const box = $('img-usage');
  if(!box) return;
  const now = Date.now();
  if(!force && IMG_USAGE && now - IMG_USAGE_AT < 60000){
    box.innerHTML = IMG_USAGE; return;
  }
  let j;
  try{ j = await api('/api/images-usage'); }
  catch(e){ box.innerHTML = '<span class="note bad">统计不到：' + esc(e) + '</span>'; return; }
  if(!box.isConnected) return;            // 切页签已经把它换掉了，别再往里写
  if(!j.ok){ box.innerHTML = '<span class="note bad">统计不到：' + esc(j.error || '') + '</span>'; return; }
  const u = j.usage || {};
  const alertGb = Number(u.alert_gb) || 0;
  if(!u.count){
    IMG_USAGE = '<span class="note">还没收过图片。打开上面这个开关之后，'
      + '收到的图会存到数据目录的 <b>images/</b> 里，打开脑内抽屉就能看到。</span>';
  } else {
    const over = alertGb > 0 && Number(u.gb) >= alertGb;
    IMG_USAGE = '<span class="note">已存 <b>' + u.count + '</b> 张　·　共 <b>'
      + esc(j.human || mediaHuman(u.bytes)) + '</b>　·　'
      + (alertGb > 0 ? '超过 ' + alertGb + ' GB 会提醒你' : '未设报警阈值')
      + '</span>'
      + (over ? '<span class="note bad">⚠️ 已经超过报警阈值了</span>' : '');
  }
  IMG_USAGE_AT = now;
  box.innerHTML = IMG_USAGE;
}

// 体积格式化。服务端 media.human() 已经有同样一份 —— 这里只是页面侧的兜底，
// 正常情况下展示的是接口返回的 `human` 字段（两边口径必须一致，见 test）。
function mediaHuman(n){
  n = Number(n) || 0;
  if(n < 1024) return n + ' B';
  if(n < 1048576) return (n / 1024).toFixed(1) + ' KB';
  if(n < 1073741824) return (n / 1048576).toFixed(1) + ' MB';
  return (n / 1073741824).toFixed(1) + ' GB';
}

function ago(ts){
  if(!ts) return '';
  const d = Math.max(0, Math.floor(Date.now() / 1000) - ts);
  if(d < 60) return '刚刚';
  if(d < 3600) return Math.floor(d / 60) + ' 分钟前';
  if(d < 86400) return Math.floor(d / 3600) + ' 小时前';
  if(d < 86400 * 7) return Math.floor(d / 86400) + ' 天前';
  const t = new Date(ts * 1000);
  return (t.getMonth() + 1) + ' 月 ' + t.getDate() + ' 日';
}

// 播报目标：一张表列**全部会话**（群 + 私聊平级），勾选决定播报发到哪。
// 🔴 为什么合成一张表、不分「已发现的群 / 已发现的私聊」两个列表：
//    两种 openid 长得一模一样，分开放反而认不出「这串 id 后面是谁」。
// 🔴 平台拿不到会话名 —— 群名接口要白名单（个人开发者 11253 无权限），
//    私聊更是没有任何查用户信息的接口。所以名字只能**用户自己起**；
//    能自动给的线索只有本机库里的「最近说过什么」（见 push.py）。
function pushTargetsHTML(){
  const ss = S.sessions || [];
  const tg = S.push_targets || [];
  if(!ss.length){
    return '<div class="f"><div class="lb">播报目标</div>'
      + '<div class="note">还没发现任何会话 —— 把机器人拉进群、在群里说句话，'
      + '或者直接在 QQ 里私聊它一句「你好」，这里就会出现。</div></div>';
  }
  const rows = ss.map(s => {
    const k = s.scope + ':' + s.openid;
    const on = tg.includes(k);
    const sc = s.scope === 'c2c' ? '私聊' : '群';
    const nm = s.name || (s.scope === 'c2c' ? '（未命名私聊）' : '（未命名群）');
    const clue = s.recent
      ? '<span class="note">最近：' + esc(s.recent)
        + (s.recent_at ? '（' + esc(ago(s.recent_at)) + '）' : '') + '</span>'
      : '<span class="note">（还没说过话）</span>';
    return '<div class="dep" data-scope="' + esc(s.scope) + '"'
      + ' data-openid="' + esc(s.openid) + '" data-name="' + esc(s.name || '') + '">'
      + '<input type="checkbox" data-pushtoggle="' + esc(k) + '"' + (on ? ' checked' : '') + '>'
      + '<b class="nm">' + esc(nm) + '</b>'
      + '<span class="tag">' + sc + '</span>'
      + '<span class="m pth" title="' + esc(s.openid) + '">'
      + esc(s.openid.slice(0, 10)) + '…</span>'
      + clue
      + '<button data-rename="1">改名</button>'
      + '</div>';
  }).join('');
  return '<div class="f"><div class="lb">播报目标'
    + '<span class="tag">启动播报发到这里</span></div>'
    + '<div class="hp">机器人见过的会话都列在下面。勾上 = 播报也发到那里；'
    + '群和私聊平级 —— 只想收私聊，把群取消勾选就行。'
    + '名字点「改名」自己起（QQ 不给机器人读会话名，只能你来）。</div>'
    + '<div class="deps one">' + rows + '</div></div>';
}

function pathRows(){
  const P = S.paths || {};
  const rows = [
    ['数据目录', P.data_dir, 'data'],
    ['备忘 / 灵感库（memos.db）', P.memos_db, 'db'],
    ['机器人凭据（.env）', P.env, 'env'],
    ['本机设置（settings.json）', P.settings, 'settings'],
  ];
  return rows.filter(r => r[1]).map(r =>
    '<div class="dep"><b>' + esc(r[0]) + '</b>'
    + '<span class="m pth" title="' + esc(r[1]) + '">' + esc(r[1]) + '</span>'
    + '<button data-open="' + r[2] + '">打开</button></div>').join('');
}

function runtimeSection(g){
  const deps = S.deps.map(d =>
    '<div class="dep"><b>' + esc(d.name) + '</b><span class="'
    + (d.ok ? 'g' : 'b') + '">' + (d.ok ? '✓' : '✗') + '</span>'
    + '<span class="m" title="' + esc(d.detail) + '">' + esc(d.detail) + '</span></div>').join('');
  return '<section><h2>' + esc(g.title) + '</h2><div class="desc">' + esc(g.desc)
    + '</div><div class="body">'
    + '<div class="f"><div class="lb">开机自启</div>'
    + '<div class="hp">开启后：登录 Windows 就自动把机器人跑起来（后台无窗口）。</div>'
    + '<div class="sw"><input type="checkbox" id="autostart"'
    + (S.bot.task === 'none' || S.bot.task === 'disabled' ? '' : ' checked') + '>'
    + '<span class="note">当前：' + esc(taskText(S.bot.task)) + '（计划任务 '
    + esc(S.bot.task_name) + '）</span></div></div>'
    + '<div class="f"><div class="lb">依赖自检</div>'
    + '<div class="hp">缺「必需」的启动不了；带「渲染成图片用」的缺了只是发文件时降级成文件卡片。</div>'
    + '<div class="deps">' + deps + '</div></div>'
    + '<div class="f"><div class="lb">数据文件位置</div>'
    + '<div class="hp">备忘和灵感在本地数据库里，配置在这几个文件里。'
    + '点「打开」定位过去 —— 想自己处理数据（或丢给 AI 分析）时从这里取。</div>'
    + '<div class="deps">' + pathRows() + '</div></div>'
    + '<div class="f"><div class="lb">版本</div>'
    + '<div class="hp">更新：点「⬇ 下载新版」拿安装包（文件名带版本号），双击装上就行 —— '
    + '装到同一个目录，你的数据（备忘 / 灵感 / 配置）不受影响。</div>'
    + '<div class="row"><span class="note" id="ver-note">v' + esc(S.version || '?') + '</span>'
    + '<button id="btn-update">检查更新</button>'
    + '<a id="ver-dl" class="dlbtn" style="display:none" target="_blank" rel="noopener">⬇ 下载新版</a></div>'
    + '<div class="note" id="ver-file" style="display:none"></div>'
    // 🔴 国内直连 github 不稳定（jsdelivr / Releases 都可能连不上），所以除了「⬇ 下载新版」
    //    再给一条不依赖网络的兜底路：去拿安装包的那个群里翻群文件。
    //    这行小字**常驻**显示 —— 等检查更新失败才提示就晚了，用户那会儿已经在找下载入口了。
    // ⚠️ 这段代码整个住在 config-web.py 里那个 PAGE 大字符串中，是 JS ——
    //    注释只能写 //；写 # 会把整页脚本打崩（浏览器报 Invalid or unexpected token，
    //    而且单测抓不到）。
    + '<div class="note" id="ver-hint">国内网络git直连不稳定，可在你下载摸鱼bot的群内寻找最新群文件。</div>'
    + '</div></div>';
}

async function checkUpdate(auto){
  const note = $('ver-note');
  const dl = $('ver-dl');
  const fileNote = $('ver-file');
  const cur = (S && S.version) || '?';
  if(note) note.textContent = 'v' + cur + '（检查中…）';
  if(dl) dl.style.display = 'none';
  if(fileNote) fileNote.style.display = 'none';
  let j;
  try{ j = await api('/api/update-check', {}); }
  catch(e){
    if(note) note.textContent = 'v' + cur + '（检查失败：连不上网）';
    return;
  }
  if(note){
    // 🔴 别写成「（检查失败：xxx）—— 服务端给的已经是人话，再套一层括号会变成四层括号
    if(!j.ok) note.textContent = 'v' + (j.current || cur) + '　检查失败：' + (j.error || '');
    else if(j.has_update) note.textContent = '有新版 v' + j.latest + '（当前 v' + j.current + '）';
    else note.textContent = 'v' + j.current + '（已是最新）';
  }
  if(j.ok && j.has_update && dl){
    // 🔴 优先给**直接下载**的链接；直链打不开时页面上还有 Releases 兜底入口
    dl.href = j.download || j.release || j.releases || j.url || '#';
    dl.textContent = '⬇ 下载 v' + j.latest;
    dl.style.display = '';
    if(fileNote && j.asset){
      fileNote.innerHTML = '安装包：' + esc(j.asset)
        + '　·　打不开直链？去 <a class="plain" href="' + esc(j.release || j.releases || '#')
        + '" target="_blank" rel="noopener">Releases 页面</a> 手动下';
      fileNote.style.display = '';
    }
    toast('有新版本 v' + j.latest + ' —— 点「⬇ 下载 v' + j.latest + '」');
  } else if(j.ok && auto === false){
    toast('已经是最新版 v' + (j.current || cur));
  }
}


/* ── 摸鱼库 ───────────────────────────────────────────────── */
/* 三类各是一个大输入框，三个并排必然超一屏 → 上面一排小按钮切着看。
   值的来源：改过的在 MOYU_DIRTY，没改过的取 S.moyu.groups。 */
function moyuLines(v){
  const out = [], seen = new Set();
  String(v == null ? '' : v).split('\n').forEach(s => {
    s = s.trim();
    if(s && !seen.has(s)){ seen.add(s); out.push(s); }
  });
  return out;   // 去空行 + 去重 —— 跟后端保存时的处理一致，页面上显示的条数才对得上
}

function moyuValue(cat){
  if(cat in MOYU_DIRTY) return MOYU_DIRTY[cat];
  return (((S.moyu || {}).groups || {})[cat] || []).join('\n');
}

function moyuSection(g){
  const M = S.moyu || {};
  const cats = M.cats || [];
  if(!cats.length){
    return '<section><h2>' + esc(g.title) + '</h2><div class="desc">' + esc(g.desc)
      + '</div><div class="body"><div class="note bad">'
      + '摸鱼库读不出来 —— 发「摸鱼」会没反应。</div></div></section>';
  }
  if(!MOYU_CAT || !cats.some(c => c.key === MOYU_CAT)) MOYU_CAT = cats[0].key;
  const cur = cats.find(c => c.key === MOYU_CAT);
  const total = cats.reduce((n, c) => n + moyuLines(moyuValue(c.key)).length, 0);
  const picker = cats.map(c =>
    '<button class="tab' + (c.key === cur.key ? ' on' : '') + '" data-moyucat="' + esc(c.key) + '">'
    + esc(c.icon + ' ' + c.name + ' ' + moyuLines(moyuValue(c.key)).length) + '</button>').join('');
  const used = M.source === 'user'
    ? ('现在用的是你自己这份库（三类共 ' + total + ' 条）')
    : ('现在用的是内置库（三类共 ' + total + ' 条，还没改过）');
  return '<section><h2>' + esc(g.title) + '</h2><div class="desc">' + esc(g.desc)
    + '</div><div class="body">'
    + '<div class="f"><div class="lb">分哪三类</div>'
    + '<div class="hp">' + esc(used + '。一类一个框，上面切着看。'
        + '改完点最下面「保存」—— 下次发「摸鱼」立刻生效，不用重启。') + '</div>'
    + '<div class="tabs mini">' + picker + '</div>'
    + '<div class="hp">' + esc(cur.help) + '</div>'
    + '<textarea data-moyu="' + esc(cur.key) + '" rows="12" spellcheck="false" '
    + 'placeholder="一行一条，例如：站起来走 30 秒">' + esc(moyuValue(cur.key)) + '</textarea>'
    + '<div class="note">当前这一类 <b class="cnt">' + moyuLines(moyuValue(cur.key)).length
    + '</b>' + esc(' 条 · 一行一条 · 空行忽略 · 同类重复的自动去掉 · 单条最长 '
        + M.max_text_len + ' 字 · 每类最多 ' + M.max_per_cat + ' 条') + '</div>'
    + '</div>'
    + '<div class="f"><div class="lb">想改回来</div>'
    + '<div class="hp">' + esc('丢掉你改过的这份，回到随程序走的内置库（'
        + M.builtin_total + ' 条）。内置库一直都在，随时能回来。') + '</div>'
    + '<div class="row"><button class="danger" id="moyu-reset">恢复内置默认</button>'
    + '<button id="moyu-open">打开库文件位置</button>'
    + '<span class="note pth" title="' + esc(M.path || '') + '">' + esc(M.path || '')
    + '</span></div></div>'
    + '</div></section>';
}

/* ── 自定义指令（扩展包）─────────────────────────────────────── */
/* 装的是 `extensions/local/` 下的扩展包：一个 .py，或一个含 .py 的目录。
   🔴 页面只负责**文件的进出**；把指令注册进机器人是 `extensions/__init__.py`
      在 bot 启动时干的活 —— 所以这里永远要提醒「重启才生效」。
   🔴 「加载失败 / 撞名」两行必须显示：扩展是任意代码，装进去不一定跑得起来
      （缺依赖、写错、和别的扩展抢同一个指令词），不说用户就只能干瞪眼。 */
function extSize(n){
  n = n || 0;
  if(n < 1024) return n + ' B';
  if(n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1024 / 1024).toFixed(1) + ' MB';
}

function extItemHTML(it){
  const cmds = (it.commands || []).length
    ? (it.commands || []).map(c => '<code>' + esc(c) + '</code>').join('')
    : '<span class="note bad">没注册任何指令 —— 这个包装进来是死的</span>';
  let extra = '';
  if(it.error) extra += '<div class="note bad">⚠ 加载失败：' + esc(it.error) + '</div>';
  if((it.dup || []).length) extra += '<div class="note bad">⚠ 指令名和别的扩展撞了：'
    + esc(it.dup.join('、')) + '（同名的按加载顺序，后面的盖住前面的）</div>';
  if((it.helps || []).length) extra += '<div class="note">帮助菜单里会多出：'
    + esc(it.helps.join(' / ')) + '</div>';
  const extras = it.extra_files || [];
  if(extras.length) extra += '<div class="note">包里还带了：' + esc(extras.join('、')) + '</div>';
  return '<div class="f"><div class="lb">' + esc(it.name)
    + '<span class="tag">' + esc(it.kind === 'dir' ? '指令包' : '单文件') + '</span></div>'
    + '<div class="hp">' + cmds + '</div>'
    + '<div class="note">' + esc((it.files || []).join('、') || '')
    + ' · ' + esc(extSize(it.size)) + '</div>'
    + extra
    + '<div class="row"><button class="danger" data-extrm="' + esc(it.id)
    + '">移除</button></div></div>';
}

function extSection(g){
  const E = S.ext || {};
  const items = E.items || [];
  const list = items.length ? items.map(extItemHTML).join('')
    : '<div class="f"><div class="note">还没装任何自定义指令。导入一个 .py 或 .zip 就能用。</div></div>';
  return '<section><h2>' + esc(g.title) + '</h2><div class="desc">' + esc(g.desc)
    + '</div><div class="body">'
    + '<div class="f"><div class="lb">导入一个指令包</div>'
    + '<div class="hp">' + esc('选一个 .py（单文件指令）或 .zip（指令包）。'
        + '导入不会自动生效 —— 导完点最下面「保存并重启机器人」（或上面状态行的「重启」）。') + '</div>'
    + '<div class="row"><input type="file" id="ext-file" accept=".py,.zip">'
    + '<button class="pri" id="ext-import">导入</button>'
    + '<button data-open="ext">打开安装目录</button></div>'
    + '<div class="note" id="ext-msg"></div>'
    + '<div class="note pth" title="' + esc(E.dir || '') + '">' + esc(E.dir || '') + '</div>'
    + '</div>'
    + '<div class="f"><div class="lb">' + esc('已装 ' + items.length + ' 个') + '</div>'
    + '<div class="hp">' + esc('每个扩展的 help= 那一行会自动进「帮助」菜单。'
        + '移除只是把文件挪进 extensions/.removed/，不是真删，想找回还在。') + '</div></div>'
    + list
    + '</div></section>';
}

/* 文件 → base64。分块 fromCharCode：一次 apply 太多参数会爆栈。 */
function fileToB64(file){
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onerror = () => reject(new Error('读不了这个文件'));
    fr.onload = () => {
      const bytes = new Uint8Array(fr.result);
      let bin = '';
      for(let i = 0; i < bytes.length; i += 8192)
        bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 8192));
      resolve(btoa(bin));
    };
    fr.readAsArrayBuffer(file);
  });
}

async function extImport(overwrite){
  const ef = $('ext-file'), msg = $('ext-msg');
  const f = ef && ef.files && ef.files[0];
  const say = (t, bad) => { if(msg){ msg.textContent = t; msg.className = 'note' + (bad ? ' bad' : ' ok'); } };
  if(!f){ say('先选一个 .py 或 .zip 文件', true); return; }
  if(f.size > 8 * 1024 * 1024){ say('这个文件太大了（上限 8MB）', true); return; }
  say('正在导入…');
  let b64;
  try { b64 = await fileToB64(f); } catch(e){ say(String(e.message || e), true); return; }
  const j = await api('/api/ext/import', {name: f.name, data: b64, overwrite: !!overwrite});
  if(j.conflict){
    if(confirm('已经有一个叫「' + (j.item && j.item.name || '') + '」的扩展了，覆盖它吗？'))
      return extImport(true);
    say('已取消（没有覆盖）', true);
    return;
  }
  if(j.ok){
    if(j.state) S = j.state;
    toast(j.message || '已导入');
    render();
    const m2 = $('ext-msg');
    if(m2){ m2.textContent = (j.message || '已导入') + ' —— 重启机器人才生效'; m2.className = 'note ok'; }
  } else {
    say(j.error || '导入失败', true);
  }
}

/* ── 输入绑定 ─────────────────────────────────────────────── */
function setDirty(k, v){
  const f = S.fields.find(x => x.key === k);
  // 🔴 认不出键时**必须吭声**：以前是 `if(!f) return;` 静默吃掉，
  //    结果是「点了开关 → 看着变了 → 保存说没有改动」这种查半天查不出的鬼故事
  //    （自绘区块的键一度不在 S.fields 里，就是这么中招的）。
  if(!f){ console.warn('setDirty: 字段表里没有这个键 ' + k + '，改动会被丢弃'); return; }
  if(JSON.stringify(v) === JSON.stringify(f.value)) delete DIRTY[k];
  else DIRTY[k] = v;
}

function bindFields(){
  document.querySelectorAll('[data-k]').forEach(el => {
    const k = el.dataset.k;
    el.addEventListener(el.type === 'checkbox' ? 'change' : 'input', () => {
      const f = S.fields.find(x => x.key === k);
      if(!f) return;
      if(f.kind === 'multi'){
        const vals = [...document.querySelectorAll('input[type=checkbox][data-k="' + k + '"]:checked')]
          .map(i => i.value);
        setDirty(k, vals);
      } else if(f.kind === 'bool'){
        setDirty(k, el.checked ? 'true' : 'false');
      } else {
        setDirty(k, el.value);
      }
    });
  });

  // 收图区块：主开关 + 两个子开关，最终都折算回 `image_collect` 这一个 multi 键。
  // 🔴 主开关不是第三个数据键 —— 它只是「数组非空」的另一种画法。
  //    子开关全取消 → 数组空 → 主开关自动回落到「已关闭」（下面的 render() 负责重画）。
  const im = $('img-master');
  if(im) im.addEventListener('change', () => {
    const cur = fieldValue('image_collect') || [];
    // 打开时给个合理默认：两边都收（用户真想要单边，再取消其一）
    setDirty('image_collect', im.checked ? (cur.length ? cur : ['c2c', 'group']) : []);
    render();
  });
  document.querySelectorAll('[data-imgsub]').forEach(cb =>
    cb.addEventListener('change', () => {
      const vals = [...document.querySelectorAll('[data-imgsub]:checked')].map(i => i.dataset.imgsub);
      setDirty('image_collect', vals);
      render();
    }));

  document.querySelectorAll('[data-browse]').forEach(b =>
    b.addEventListener('click', () => openPick(b.dataset.browse)));
  document.querySelectorAll('[data-test]').forEach(b =>
    b.addEventListener('click', () => testRoot(b.dataset.test)));
  document.querySelectorAll('[data-open]').forEach(b =>
    b.addEventListener('click', async () => {
      const j = await api('/api/open-folder', {what: b.dataset.open});
      if(!j.ok) toast('打不开：' + (j.error || ''));
    }));

  // 自定义指令：导入 / 移除。导入走「读成 base64 → 一个 JSON POST」，
  // 不走 multipart（省掉一个解析器，也就不用碰 Content-Type 那堆坑）。
  const ei = $('ext-import');
  if(ei) ei.addEventListener('click', () => extImport(false));
  const ef = $('ext-file');
  if(ef) ef.addEventListener('change', () => {
    const m = $('ext-msg');
    if(m && ef.files[0]){ m.textContent = '选好了：' + ef.files[0].name; m.className = 'note'; }
  });
  document.querySelectorAll('[data-extrm]').forEach(b =>
    b.addEventListener('click', async () => {
      if(!confirm('移除这个扩展？文件会挪进 extensions/.removed/（不是真删，还能捞回来）。')) return;
      const j = await api('/api/ext/remove', {id: b.dataset.extrm});
      if(j.state) S = j.state;
      toast(j.ok ? (j.message || '已移除') : ('移除失败：' + (j.error || '')));
      render();
    }));

  const ub = $('btn-update');
  if(ub) ub.addEventListener('click', () => checkUpdate(false));

  // 播报目标：勾选 / 取消勾选。群和私聊共用同一个键（见 push.py）
  document.querySelectorAll('[data-pushtoggle]').forEach(cb =>
    cb.addEventListener('change', async () => {
      const k = cb.dataset.pushtoggle;
      const i = k.indexOf(':');
      cb.disabled = true;
      const j = await api('/api/push', {scope: k.slice(0, i), openid: k.slice(i + 1),
                                        on: cb.checked});
      if(j.state) S = j.state;
      toast(j.ok ? j.message : ('失败：' + (j.error || '')));
      render();
    }));

  // 改备注名：点「改名」→ 原地换成输入框，回车 / 失焦保存，Esc 取消。
  // 🔴 用行内输入而不是 window.prompt —— 风格统一，也不会被浏览器拦。
  document.querySelectorAll('[data-rename]').forEach(b =>
    b.addEventListener('click', () => {
      const row = b.closest('.dep');
      const nm = row.querySelector('.nm');
      // 🔴 `.nm` 可能已经被换成输入框了（上一次点「改名」留下的）——
      //    那时按钮还在，再点一下不能抛异常，直接忽略。
      if(!nm) return;
      b.disabled = true;                     // 输入框在的时候按钮就没意义了（render 后自然恢复）
      const inp = document.createElement('input');
      inp.type = 'text'; inp.className = 'rename'; inp.value = row.dataset.name || '';
      inp.placeholder = '起个名字（留空 = 显示 id）';
      nm.replaceWith(inp);
      inp.focus(); inp.select();
      let done = false;
      const finish = async (commit) => {
        if(done) return;
        done = true;
        if(commit){
          const j = await api('/api/session', {scope: row.dataset.scope,
                                              openid: row.dataset.openid, name: inp.value});
          if(j.state) S = j.state;
          toast(j.ok ? j.message : ('失败：' + (j.error || '')));
        }
        render();
      };
      inp.addEventListener('keydown', e => {
        if(e.key === 'Enter'){ e.preventDefault(); finish(true); }
        else if(e.key === 'Escape'){ finish(false); }
      });
      inp.addEventListener('blur', () => finish(true));
    }));

  const as = $('autostart');
  if(as) as.addEventListener('change', async () => {
    as.disabled = true;
    const j = await api('/api/autostart', {on: as.checked});
    as.disabled = false;
    if(j.state) S = j.state;
    toast(j.ok ? j.message : ('失败：' + (j.error || j.message)));
    render();
  });
  const ab = $('rule-add-btn');
  if(ab) ab.addEventListener('click', async () => {
    const v = ($('rule-add').value || '').trim();
    if(!v) return;
    const j = await api('/api/ignore', {add: v});
    if(j.state) S = j.state;
    toast(j.ok ? j.message : ('失败：' + j.error));
    render();
  });
  const rc = $('rule-clear');
  if(rc) rc.addEventListener('click', async () => {
    if(!confirm('清空本地忽略规则？（.env 里的关键词不受影响）')) return;
    const j = await api('/api/ignore', {clear: true});
    if(j.state) S = j.state;
    toast(j.ok ? j.message : ('失败：' + j.error));
    render();
  });
  document.querySelectorAll('[data-delrule]').forEach(b =>
    b.addEventListener('click', async () => {
      const j = await api('/api/ignore', {remove: b.dataset.delrule});
      if(j.state) S = j.state;
      toast(j.ok ? j.message : ('失败：' + j.error));
      render();
    }));

  // 摸鱼库：输入即记进 MOYU_DIRTY（切页签 / 切分类都不丢），保存时才提交
  document.querySelectorAll('[data-moyu]').forEach(t =>
    t.addEventListener('input', () => {
      MOYU_DIRTY[t.dataset.moyu] = t.value;
      const c = t.parentNode.querySelector('.cnt');
      if(c) c.textContent = moyuLines(t.value).length;
    }));
  document.querySelectorAll('[data-moyucat]').forEach(b =>
    b.addEventListener('click', () => {
      const t = document.querySelector('[data-moyu]');
      if(t) MOYU_DIRTY[t.dataset.moyu] = t.value;   // 先把当前这类的改动存住再切
      MOYU_CAT = b.dataset.moyucat;
      renderSections();
    }));
  const mrs = $('moyu-reset');
  if(mrs) mrs.addEventListener('click', async () => {
    const n = (S.moyu || {}).builtin_total || '';
    if(!confirm('丢掉你改过的这份摸鱼库，恢复内置的 ' + n + ' 条？')) return;
    const j = await api('/api/moyu', {reset: true});
    if(j.state) S = j.state;
    MOYU_DIRTY = {};
    // 后端两种情况都给了人话（「已恢复内置摸鱼库」/「本来用的就是内置库」），直接用
    const msg = j.message || j.error || '已恢复内置摸鱼库';
    $('savemsg').textContent = msg;
    toast(msg);
    render();
  });
  const mop = $('moyu-open');
  if(mop) mop.addEventListener('click', async () => {
    const j = await api('/api/open-folder', {what: 'moyu'});
    if(!j.ok) toast('打不开：' + (j.error || ''));
  });
}

/* ── 保存 ─────────────────────────────────────────────────── */
function splitDirty(){
  const env = {}, st = {};
  for(const k of Object.keys(DIRTY)){
    const f = S.fields.find(x => x.key === k);
    if(!f) continue;
    if(f.target === 'env'){
      if(f.kind === 'secret' && !String(DIRTY[k]).trim()) continue;  // 留空 = 不改
      let val = DIRTY[k];
      if(f.kind === 'paths'){          // 多行 → 分号串（.env 里就存 `a;b`）
        val = String(val).split(/[\n;]+/).map(s => s.trim()).filter(Boolean).join(';');
      }
      env[k] = val;
    } else {
      st[k] = DIRTY[k];
    }
  }
  return {env: env, st: st};
}

/* 摸鱼库提交的是**整库**：改过的那几类用输入框里的，没改过的照原样带上。
   这样只动一类也不会把另外两类弄丢。 */
function moyuPayload(){
  const out = {};
  for(const c of ((S.moyu || {}).cats || [])) out[c.key] = moyuLines(moyuValue(c.key));
  return out;
}

async function save(){
  const {env, st} = splitDirty();
  const moyu = Object.keys(MOYU_DIRTY).length ? moyuPayload() : null;
  if(!Object.keys(env).length && !Object.keys(st).length && !moyu){
    $('savemsg').textContent = '没有改动';
    return true;
  }
  if(Object.keys(env).length){
    const j = await api('/api/env', {values: env});
    if(!j.ok){ $('savemsg').textContent = '保存失败：' + j.error; toast('保存失败：' + j.error); return false; }
    if(j.state) S = j.state;
  }
  if(Object.keys(st).length){
    const j = await api('/api/settings', {values: st});
    if(!j.ok){ $('savemsg').textContent = '保存失败：' + j.error; toast('保存失败：' + j.error); return false; }
    if(j.state) S = j.state;
  }
  if(moyu){
    const j = await api('/api/moyu', {groups: moyu});
    if(!j.ok){ $('savemsg').textContent = '保存失败：' + j.error; toast('保存失败：' + j.error); return false; }
    if(j.state) S = j.state;
    MOYU_DIRTY = {};
  }
  DIRTY = {};
  $('savemsg').textContent = moyu
    ? '已保存 ✓（摸鱼库立刻生效；改凭据 / 触发词 / 搜索根要重启机器人才生效）'
    : '已保存 ✓（改凭据 / 触发词 / 搜索根要重启机器人才生效）';
  toast('已保存 ✓');
  render();
  return true;
}

async function botAct(action){
  const btns = ['btn-start','btn-stop','btn-restart'].map($).filter(Boolean);
  btns.forEach(b => b.disabled = true);
  $('savemsg').textContent = action === 'restart' ? '正在重启…（约 5 秒）' : '正在' + action + '…';
  const j = await api('/api/bot', {action: action});
  if(j.state) S = j.state;
  $('savemsg').textContent = j.message || (j.ok ? '完成' : '失败');
  toast(j.message || (j.ok ? '完成' : ('失败：' + j.error)));
  render();
}

/* ── 目录浏览器 ───────────────────────────────────────────── */
async function openPick(key){
  PICK_TARGET = key;
  const raw = String(fieldValue(key) || '');
  const cur = raw.split(/[\n;]+/).map(s => s.trim()).filter(Boolean)[0] || '';
  await loadDir(cur);
  $('pick').classList.add('on');
}
async function loadDir(p){
  const j = await api('/api/browse?path=' + encodeURIComponent(p || ''));
  if(!j.ok){
    $('pick-path').textContent = j.error || '打不开';
    $('pick-list').innerHTML = '';
    $('pick')._path = p;
    return;
  }
  $('pick')._path = j.path;
  $('pick')._parent = j.parent;
  $('pick-path').textContent = j.path;
  $('pick-list').innerHTML = j.dirs.length
    ? j.dirs.map(d => '<div class="d" data-d="' + esc(d) + '">📁 ' + esc(d) + '</div>').join('')
    : '<div class="note" style="padding:10px">（这个目录下没有子目录）</div>';
  document.querySelectorAll('#pick-list .d').forEach(el =>
    el.addEventListener('click', () => loadDir(($('pick')._path + '/' + el.dataset.d))));
}
async function testRoot(key){
  const note = document.querySelector('[data-note="' + key + '"]');
  const p = fieldValue(key) || '';
  if(note) note.textContent = '统计中…（最多 3 秒）';
  const j = await api('/api/check-root', {path: p});
  if(!note) return;
  if(!j.ok){ note.className = 'note bad'; note.textContent = j.error; return; }
  const n = j.count.toLocaleString('en-US');
  note.className = 'note ok';
  note.textContent = j.truncated
    ? ('这个目录至少有 ' + n + ' 个文件（扫了 ' + j.seconds + ' 秒还没数完，太大了 —— 建议换成更小的目录）')
    : (n + ' 个文件 · 全量扫一遍 ' + j.seconds + ' 秒');
}

/* ── 启动 ─────────────────────────────────────────────────── */
$('btn-save').onclick = save;
$('btn-save-restart').onclick = async () => {
  if(!(await save())) return;
  await botAct('restart');
};
$('btn-quit').onclick = async () => {
  if(!confirm('关闭配置中心？配置已经保存的不会丢。')) return;
  try{ await api('/api/quit', {}); }catch(e){}
  document.body.innerHTML = '<div style="padding:60px;text-align:center;color:#8b98a9;'
    + 'font:15px sans-serif">配置中心已关闭，这个页面可以关掉了。</div>';
};
$('pick-close').onclick = () => $('pick').classList.remove('on');
$('pick-up').onclick = () => loadDir($('pick')._parent || '');
$('pick-use').onclick = () => {
  const p = $('pick')._path;
  if(PICK_TARGET && p){
    const el = document.querySelector('[data-k="' + PICK_TARGET + '"]');
    if(el && el.tagName === 'TEXTAREA'){      // 多目录：追加一行，不覆盖已有的
      const cur = el.value.split(/[\n;]+/).map(s => s.trim()).filter(Boolean);
      const np = p.replace(/\\/g, '/');
      if(!cur.includes(np)) cur.push(np);
      el.value = cur.join('\n');
      setDirty(PICK_TARGET, el.value);
    } else if(el){
      el.value = p; setDirty(PICK_TARGET, p);
    }
  }
  $('pick').classList.remove('on');
};
$('pick').addEventListener('click', e => { if(e.target === $('pick')) $('pick').classList.remove('on'); });

(async () => {
  try{
    S = await api('/api/state');
    render();
    // 顺手查一次有没有新版（不阻塞页面，失败静默）
    setTimeout(() => { checkUpdate(true).catch(() => {}); }, 800);
  }catch(e){
    document.body.innerHTML = '<div style="padding:60px;text-align:center;color:#fc8181;'
      + 'font:15px sans-serif">连不上配置中心服务：' + esc(e) + '</div>';
  }
})();
</script></body></html>
"""


def serve(port=DEFAULT_PORT, open_browser=True, tries=PORT_TRIES, strict=False):
    """起本地服务（只监听 127.0.0.1）。

    strict=True（管家传了确切端口时）：**只绑这一个端口**，占不到就报错退出。
    🔴 为什么要这个开关：`ThreadingHTTPServer` 默认 `allow_reuse_address = 1`，
       Windows 上这个标志允许 bind**别人正在 LISTENING** 的端口（Linux 只管 TIME_WAIT）。
       于是「绑上了但收不到请求」—— 页面打不开，日志里却一切正常。
       管家已经先探明端口，这里就不该再顺延（顺延会让管家记的端口和实际不符）。
    """
    sync_env()                       # 先把 .env 灌进 os.environ，跟 bot 的视角对齐
    handler = make_handler(PAGE)

    # 🔴 严格模式：关掉 allow_reuse_address —— 否则占用检测是假的（见 docstring）
    class _StrictServer(ThreadingHTTPServer):
        allow_reuse_address = False

    srv = _StrictServer if strict else ThreadingHTTPServer
    httpd = None
    last_err = None
    for p in (range(port, port + tries) if not strict else [port]):
        try:
            httpd = srv(("127.0.0.1", p), handler)
            port = p
            break
        except OSError as e:
            last_err = e
            continue
    if httpd is None:
        if strict:
            print(f"（端口 {port} 被占用，起不来：{type(last_err).__name__}: {last_err}）",
                  flush=True)
        else:
            print(f"（{port}~{port + tries - 1} 都起不来：{type(last_err).__name__}: {last_err}）")
        return 1

    url = f"http://127.0.0.1:{port}/"
    # 🔴 flush=True：stdout 被重定向到文件 / 管道时是块缓冲的，不 flush 会一直卡在
    #    缓冲区里 —— 用户双击 .bat 时看不到「已启动」提示，排错时也没有任何线索。
    print(f"配置中心已启动：{url}", flush=True)
    print("  改完点页面底部「保存」；凭据写 .env（自动备份 .env.bak），"
          "文案与展示范围写 settings.json。", flush=True)
    print("  页面底部「关闭配置中心」或 Ctrl+C 可停。", flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


def main():
    ap = argparse.ArgumentParser(description="摸鱼bot 配置中心（本地网页）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="起始端口（默认 8770）")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--strict-port", action="store_true",
                    help="只用 --port 指定的那一个端口，占了就报错（管家用）")
    args = ap.parse_args()
    return serve(port=args.port, open_browser=not args.no_browser,
                 strict=args.strict_port)


if __name__ == "__main__":
    sys.exit(main())
