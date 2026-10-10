"""配置中心（本地网页）—— 傻瓜化改配置：不用碰 `.env`，不用记命令。

这个文件是干嘛的：起一个**只监听 127.0.0.1** 的小服务，把一个配置页面摆出来。
页面上能做的事：
  ① 填 QQ 机器人凭据（AppID / AppSecret / 目标群 openid / 沙箱）  → 写 `.env`
  ② 改备忘触发词、启动播报、关机通知文案                        → 写 `.env` / `settings.json`
  ③ 选「发文件」的搜索根目录（带目录浏览器 + 实测：多少文件、搜一次多久）
  ④ 勾脑内抽屉的展示范围                                        → 写 `settings.json`
  ⑤ 一键开关「开机自启」（注册 / 删除 Windows 计划任务 `MoyuBot`）
  ⑥ 启动 / 停止 / 重启机器人，并自检依赖是否装齐
  ⑦ 管理忽略表（别把同群其他机器人当内容收）

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

import ignore as _ignore      # noqa: E402  （忽略表：规则读写）
import settings as _settings  # noqa: E402  （本机设置：settings.json）
import groups as _groups      # noqa: E402  （已发现的群 / 主动推送目标）
import version as _version    # noqa: E402  （版本号 + 「有没有新版本」检测）
from paths import data_path, data_dir   # noqa: E402  （数据目录：可写文件落哪）

# 🔴 .env / 日志 / pid 都是**可写数据** —— 安装版由 MEMO_DATA_DIR 指到用户目录（见 paths.py）
ENV_PATH = data_path(".env")
ENV_BAK = data_path(".env.bak")
BOT_PY = os.path.join(HERE, "bot.py")              # 代码：随安装目录走
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
def _pid_alive(pid):
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            # 🔴 两处都要兜底，否则**整个启停流程会被打断**
            #    （实测踩到：bot_stop 成功 → bot_start 在 _pid_alive 崩 → bot 被停掉却没起来）：
            #    1) 显式 encoding + errors="replace" —— 中文 Windows 的 tasklist 输出是 GBK，
            #       而设了 PYTHONUTF8=1 的环境会拿 UTF-8 去解码 → UnicodeDecodeError 直接冒泡。
            #       表头乱码无所谓，PID 是 ASCII，不影响匹配。
            #    2) stdout 兜底成 "" —— 受限环境里它可能是 None，`in None` 会抛 TypeError。
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=10).stdout or ""
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except (OSError, subprocess.SubprocessError, ValueError):
        # ValueError 不是凑数：UnicodeDecodeError 继承自 ValueError，不继承 OSError。
        # 上面的 encoding 兜底只是把「正常路径」护住了，真出现解码异常时仍会走到这里。
        return False


def bot_pid():
    """`bot.pid` 里记的进程号（0 = 没记）。"""
    try:
        with open(PID_FILE, encoding="utf-8") as f:
            return int((f.read() or "0").strip())
    except (OSError, ValueError):
        return 0


def bot_running():
    """机器人正在跑就返回它的 pid，否则 0。"""
    pid = bot_pid()
    return pid if _pid_alive(pid) else 0


def _powershell(script, timeout=90):
    """跑一段 PowerShell，返回 (returncode, stdout+stderr)。

    🔴 强制 UTF-8 输出 —— 中文系统的 PowerShell 默认按 GBK 输出，中文错误信息会变乱码。
    🔴 `$ErrorActionPreference='Stop'` —— 否则 `Register-ScheduledTask` 这类失败是
       「非终止错误」，退出码仍是 0，我们就会误报成功。要容忍「找不到」的脚本
       自己在 cmdlet 上写 `-ErrorAction SilentlyContinue`。
    """
    pre = ("$OutputEncoding=[Text.Encoding]::UTF8;"
           "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
           "$ErrorActionPreference='Stop';")
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", pre + script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except FileNotFoundError:
        return 127, "找不到 powershell（开机自启开关只在 Windows 上支持）"
    except subprocess.SubprocessError as e:
        return 1, f"{type(e).__name__}: {e}"


_TASK_CACHE = {"t": 0.0, "v": "unknown"}


def task_state(force=False):
    """计划任务 `MoyuBot` 的状态：'none' / 'ready' / 'running' / 'disabled' / 'unknown'。

    🔴 起一次 PowerShell 要几百毫秒 —— 缓存 2 秒，否则每保存一次页面都要等它。
    """
    now = time.time()
    if not force and now - _TASK_CACHE["t"] < 2.0:
        return _TASK_CACHE["v"]
    rc, out = _powershell(
        f"$t=Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue;"
        "if($null -eq $t){'NONE'}else{$t.State}", timeout=30)
    if rc != 0:
        v = "unknown"
    else:
        s = (out or "").strip().splitlines()[-1].strip().lower() if out.strip() else ""
        v = {"none": "none", "ready": "ready", "running": "running",
             "disabled": "disabled"}.get(s, "unknown")
    _TASK_CACHE["t"], _TASK_CACHE["v"] = now, v
    return v


def _pythonw():
    """跟当前解释器同目录的 pythonw.exe（无控制台窗口），找不到就用当前解释器。"""
    exe = sys.executable or "python"
    if os.name == "nt" and os.path.basename(exe).lower() == "python.exe":
        cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.isfile(cand):
            return cand
    return exe


def autostart_set(on):
    """开 / 关开机自启（注册 / 删除计划任务）→ (ok, 人话说明)。"""
    if os.name != "nt":
        return False, "开机自启只在 Windows 上支持（靠计划任务实现）"

    def _q(p):
        return p.replace("'", "''")          # PowerShell 单引号串里，单引号要写两遍

    if on:
        script = (
            f"$act=New-ScheduledTaskAction -Execute '{_q(_pythonw())}' "
            f"-WorkingDirectory '{_q(data_dir())}' "
            f"-Argument '\"{_q(LAUNCHER)}\" --bot --log \"{_q(BOT_LOG)}\"';"
            "$trg=New-ScheduledTaskTrigger -AtLogOn;"
            "$set=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
            "-DontStopIfGoingOnBatteries -StartWhenAvailable "
            "-ExecutionTimeLimit ([TimeSpan]::Zero) "
            "-RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) "
            "-MultipleInstances IgnoreNew;"
            f"Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $act -Trigger $trg "
            "-Settings $set -Force | Out-Null;'OK'"
        )
        rc, out = _powershell(script)
        _TASK_CACHE["t"] = 0.0
        if rc != 0 or "OK" not in out:
            return False, out or "注册计划任务失败（可能需要管理员权限）"
        return True, "已开启开机自启 —— 下次登录 Windows 会自动把机器人跑起来（后台无窗口）"

    script = (f"$t=Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue;"
              f"if($null -ne $t){{Unregister-ScheduledTask -TaskName '{TASK_NAME}' "
              "-Confirm:$false};'OK'")
    rc, out = _powershell(script)
    _TASK_CACHE["t"] = 0.0
    if rc != 0 or "OK" not in out:
        return False, out or "删除计划任务失败"
    return True, "已关闭开机自启 —— 下次登录不再自动启动（正在跑的机器人不受影响）"


def bot_stop():
    """停掉机器人 → (ok, 说明)。"""
    pid = bot_running()
    if not pid:
        return True, "机器人本来就没在跑"
    try:
        # 和 _pid_alive 同一个坑：中文 Windows 的 taskkill 输出是 GBK，
        # 这里只取返回码不看文本，但解码仍会在 subprocess 内部发生 → 必须显式指定。
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=20)
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return False, f"停不掉：{type(e).__name__}: {e}"
    for _ in range(24):
        if not _pid_alive(pid):
            return True, f"已停止（pid {pid}）"
        time.sleep(0.25)
    return False, f"发了停止信号，但进程 {pid} 还活着 —— 手动看下任务管理器"


def bot_start():
    """启动机器人 → (ok, 说明)。"""
    if bot_running():
        return True, "机器人已经在跑了"
    if task_state() in ("ready", "running"):
        rc, out = _powershell(f"Start-ScheduledTask -TaskName '{TASK_NAME}';'OK'")
        if rc != 0 or "OK" not in out:
            return False, out or "启动计划任务失败"
    else:
        # 没注册计划任务（或查不到）→ 直接后台起一个，和桌面入口一个套路：无窗口
        flags = 0
        if os.name == "nt":
            flags = 0x00000008 | 0x08000000      # DETACHED_PROCESS | CREATE_NO_WINDOW
        try:
            fh = open(BOT_LOG, "ab")
            try:
                subprocess.Popen([_pythonw(), "-u", LAUNCHER, "--bot", "--log", BOT_LOG],
                                 cwd=data_dir(), stdin=subprocess.DEVNULL,
                                 stdout=fh, stderr=subprocess.STDOUT,
                                 creationflags=flags, close_fds=True)
            finally:
                fh.close()
        except OSError as e:
            return False, f"起不来：{e}"
    for _ in range(60):                           # 最多等 ~15 秒
        time.sleep(0.25)
        pid = bot_running()
        if pid:
            return True, f"已启动（pid {pid}）"
    return False, "进程起了但没等到 bot.pid —— 打开 bot.out.log 看下报了什么错"


def bot_restart():
    ok, msg = bot_stop()
    if not ok:
        return False, msg
    time.sleep(1.5)
    ok2, msg2 = bot_start()
    return ok2, f"{msg} → {msg2}"


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
    "QQ_GROUP_OPENID": {
        "label": "目标群 openid", "group": "bot",
        "help": "启动播报 / 关机播报发到哪些群。可以填多个，用分号隔开。"
                "不用自己去翻日志找 —— 把机器人拉进群、在群里随便说句话，"
                "下面「已发现的群」里就会出现，点「加入推送」自动填进来。",
        "check": lambda v: None,
    },
    "QQ_BOT_SANDBOX": {
        "label": "沙箱环境", "group": "bot", "kind": "bool",
        "help": "QQ 开放平台的调试开关，默认关着，别动它。"
                "① 什么时候开：只有你在自己调试、机器人还没提交审核时，才需要打开。"
                "② 开了会怎样：机器人进入「沙箱环境」，只对你在 QQ 后台「沙箱配置」里"
                "加过白名单的测试群 / 测试用户可见 —— 别人把机器人拉进正式群，收不到任何回复。"
                "③ 给朋友用：必须关掉。关掉才是正式环境，所有群正常收发。",
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
        "label": "忽略关键词（逗号分隔）", "group": "ignore", "default": "",
        "help": "消息里出现这些词就整条跳过（不收灵感、也不响应）。"
                "同群有别的机器人时，把它的名字填进来，免得俩机器人互相刷屏",
        "check": lambda v: None,
    },
}

# 分组顺序 = 页面从上到下的顺序
GROUPS = [
    ("bot", "① 连接 QQ", "在 https://q.qq.com 创建机器人后，把凭据填进来。必填。"),
    ("behavior", "② 群内行为", "备忘触发词与两句播报文案。"),
    ("files", "③ 发文件", "「发文件」功能的检索范围。"),
    ("bubbles", "④ 脑内抽屉", "轮播页里放哪几类（备忘 / 灵感）。"),
    ("boomerang", "⑤ 回旋镖", "把 N 天前的旧记录翻出来推回给用户（发「回旋镖」触发）。"),
    ("runtime", "⑥ 运行方式", "开机自启、机器人进程、依赖自检。"),
    ("ignore", "⑦ 忽略表", "别把同群里其他机器人的话当内容收进来。"),
]


def state_fields():
    """页面要渲染的**全部可编辑字段**（按分组顺序），env 与 settings 混在一起。

    每个字段带 `target`：'env' 表示写 `.env`，'settings' 表示写 `settings.json`。
    前端只负责画，分流逻辑在后端，页面本身不含任何键名硬编码。
    """
    envfile = read_env_file()
    out = []

    for key, spec in ENV_SPEC.items():
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

    group_of = {"startup_msg": "behavior", "offwork_msg": "behavior", "bubbles_show": "bubbles",
                "boomerang_days": "boomerang", "boomerang_scope": "boomerang"}
    for s in _settings.describe():
        out.append({
            "target": "settings", "key": s["key"], "label": s["label"], "help": s["help"],
            "group": group_of.get(s["key"], "behavior"), "kind": s["kind"],
            "required": False, "value": s["value"], "set": _settings.is_set(s["key"]),
            "default": s["default"], "choices": s["choices"],
            "choice_labels": s["choice_labels"],
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
        "paths": {"env": ENV_PATH, "settings": _settings.file_path(),
                  "search_root": roots[0] if roots else "",
                  "search_roots": roots, "here": HERE,
                  "data_dir": data_dir(), "memos_db": data_path("memos.db"),
                  "bot_log": BOT_LOG},
        "ready": bool(appid and secret),
        "version": _version.VERSION,
        "repo": _version.REPO,
        "groups_found": _groups.all_groups(),
        "group_targets": _groups.targets(),
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
            if path == "/api/browse":
                return self._send(200, browse_dirs((qs.get("path") or [""])[0]))
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
                        "url": _version.HOMEPAGE.format(repo=_version.REPO),
                    })

                if path == "/api/groups":
                    # 把「已发现的群」里的一个 openid 追加到推送列表（写 .env 的 QQ_GROUP_OPENID）
                    oid = str(payload.get("add") or "").strip()
                    if not oid:
                        return self._send(400, {"ok": False, "error": "缺少 openid"})
                    cur = _groups.parse_ids()
                    if oid not in cur:
                        cur.append(oid)
                    _keys, err = write_env({"QQ_GROUP_OPENID": ";".join(cur)})
                    if err:
                        return self._send(400, {"ok": False, "error": err})
                    return self._send(200, {"ok": True,
                                            "message": "已加入推送列表（重启机器人后生效）",
                                            "state": full_state()})

                if path == "/api/open-folder":
                    # 🔴 只允许打开白名单里的这几个路径 —— 别让页面变成「打开任意目录」的口子
                    what = str(payload.get("what") or "")
                    targets = {"data": data_dir(), "db": data_path("memos.db"),
                               "env": ENV_PATH, "settings": _settings.file_path(),
                               "log": BOT_LOG}
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
  body{
    margin:0; padding:0 0 120px; color:var(--ink);
    font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei","PingFang SC",sans-serif;
    background:radial-gradient(ellipse 120% 80% at 50% -10%, #17334f 0%, #0d1a2b 45%, #070a11 100%);
    background-attachment:fixed; -webkit-font-smoothing:antialiased;
  }
  .wrap{max-width:820px; margin:0 auto; padding:0 18px}
  header{padding:26px 0 18px}
  header h1{margin:0; font-size:19px; font-weight:700; letter-spacing:.3px}
  header h1 span{color:var(--dim); font-weight:400; font-size:12.5px; margin-left:8px}
  .statusline{display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-top:12px}
  .dot{width:9px; height:9px; border-radius:50%; background:var(--dim); flex:0 0 auto}
  .dot.on{background:var(--ok); box-shadow:0 0 10px rgba(79,209,197,.75)}
  .dot.off{background:var(--bad)}
  .hint{
    margin-top:14px; padding:11px 14px; border-radius:12px; font-size:13px;
    background:rgba(246,173,85,.1); border:1px solid rgba(246,173,85,.4); color:#f6c98a;
  }
  .hint.good{background:rgba(79,209,197,.1); border-color:rgba(79,209,197,.38); color:#8fe6dc}

  section{
    margin:14px 0; border-radius:16px; overflow:hidden;
    background:rgba(20,28,42,.82); border:1px solid var(--line);
  }
  section > h2{
    margin:0; padding:14px 18px 0; font-size:14.5px; font-weight:700;
  }
  section > .desc{padding:3px 18px 0; font-size:12px; color:var(--dim)}
  section > .body{padding:6px 18px 16px}
  .f{padding:13px 0; border-bottom:1px solid rgba(255,255,255,.07)}
  .f:last-child{border-bottom:none}
  .f .lb{font-size:13.5px; font-weight:600; display:flex; align-items:center; gap:7px}
  .f .lb .req{font-size:10.5px; color:var(--warn); border:1px solid rgba(246,173,85,.5);
    border-radius:5px; padding:0 5px; font-weight:400}
  .f .lb .tag{font-size:10.5px; color:var(--dim); border:1px solid var(--line);
    border-radius:5px; padding:0 5px; font-weight:400}
  .f .hp{font-size:11.5px; color:var(--dim); margin:3px 0 8px; line-height:1.65}
  .row{display:flex; gap:8px; align-items:center; flex-wrap:wrap}
  input[type=text],input[type=password]{
    flex:1 1 260px; min-width:0; padding:9px 11px; border-radius:10px;
    background:rgba(255,255,255,.06); border:1px solid var(--line); color:var(--ink);
    font:13.5px/1.4 inherit;
  }
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
  .chks{display:flex; gap:16px; flex-wrap:wrap}
  .chks label{display:flex; align-items:center; gap:6px; font-size:13px; cursor:pointer}
  .chks input{accent-color:#4fd1c5; width:15px; height:15px}
  .sw{display:flex; align-items:center; gap:10px}
  .sw input{width:44px; height:24px; accent-color:#4fd1c5; cursor:pointer}
  .note{font-size:12px; color:var(--dim); margin-top:7px}
  .note.ok{color:#8fe6dc}
  .note.bad{color:#fc8181}
  .deps{display:flex; flex-direction:column; gap:5px; margin-top:4px}
  .deps .dep{align-items:flex-start}
  .pth{font:11.5px/1.55 ui-monospace,Consolas,"Courier New",monospace; color:var(--dim);
       word-break:break-all; flex:1 1 auto; min-width:0}
  .deps .dep button{flex:0 0 auto}
  .dep{display:flex; align-items:baseline; gap:8px; font-size:12.5px}
  .dep b{font-weight:600; min-width:130px}
  .dep .m{color:var(--dim); font-size:11.5px; word-break:break-all}
  .dep .g{color:var(--ok)} .dep .b{color:var(--bad)}
  .rules{display:flex; flex-direction:column; gap:6px; margin-top:8px}
  .rule{display:flex; align-items:center; gap:8px; font-size:12.5px;
    padding:6px 10px; border-radius:9px; background:rgba(255,255,255,.05)}
  .rule .src{margin-left:auto; font-size:11px; color:var(--dim)}
  .rule button{padding:4px 9px; font-size:11.5px}

  .savebar{
    position:fixed; left:0; right:0; bottom:0; z-index:20;
    display:flex; align-items:center; gap:10px; padding:13px 18px;
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

/* ── 渲染 ─────────────────────────────────────────────────── */
function render(){
  renderStatus();
  renderSections();
  renderBanner();
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
  if(S.ready){ el.innerHTML = '<div class="hint good">✅ QQ 凭据已就绪，机器人可以启动。</div>'; return; }
  el.innerHTML = '<div class="hint">⚠️ 还没配好 QQ 凭据 —— 填好下面「① 连接 QQ」里的 '
    + 'AppID 和 AppSecret 再保存，机器人才能启动。<br>'
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
  let html = '';
  for(const g of S.groups){
    if(g.key === 'runtime'){ html += runtimeSection(g); continue; }
    if(g.key === 'ignore'){ html += ignoreSection(g); continue; }
    if(g.key === 'bot'){ html += botSection(g); continue; }
    const fs = S.fields.filter(f => f.group === g.key);
    if(!fs.length) continue;
    html += '<section><h2>' + esc(g.title) + '</h2><div class="desc">' + esc(g.desc)
      + '</div><div class="body">' + fs.map(fieldHTML).join('') + '</div></section>';
  }
  $('sections').innerHTML = html;
  bindFields();
}

function botSection(g){
  const fs = S.fields.filter(f => f.group === g.key);
  return '<section><h2>' + esc(g.title) + '</h2><div class="desc">' + esc(g.desc)
    + '</div><div class="body">' + fs.map(fieldHTML).join('')
    + discoveredGroupsHTML() + '</div></section>';
}

function discoveredGroupsHTML(){
  const gs = S.groups_found || [];
  const tg = S.group_targets || [];
  const rows = gs.length ? gs.map(g => {
    const on = tg.includes(g.openid);
    return '<div class="dep"><b>' + esc(g.name || '（还没拿到群名）') + '</b>'
      + '<span class="m pth" title="' + esc(g.openid) + '">' + esc(g.openid) + '</span>'
      + (on ? '<span class="note ok">已在推送列表</span>'
            : '<button data-addgroup="' + esc(g.openid) + '">＋ 加入推送</button>')
      + '</div>';
  }).join('') : '<div class="note">还没发现群 —— 把机器人拉进一个群，'
    + '在群里随便说句话（@ 它，或者发一条备忘），这里就会出现。</div>';
  return '<div class="f"><div class="lb">已发现的群</div>'
    + '<div class="hp">机器人每收到一条群消息就会记下这个群。'
    + '点「加入推送」＝把它的 openid 追加到上面的「目标群 openid」，'
    + '启动播报 / 关机播报就会发过去。不用自己去翻日志找 openid。</div>'
    + '<div class="deps">' + rows + '</div></div>';
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
    + '<span class="m">' + esc(d.detail) + '</span></div>').join('');
  return '<section><h2>' + esc(g.title) + '</h2><div class="desc">' + esc(g.desc)
    + '</div><div class="body">'
    + '<div class="f"><div class="lb">开机自启</div>'
    + '<div class="hp">开启后：登录 Windows 就自动把机器人跑起来（后台无窗口）。</div>'
    + '<div class="sw"><input type="checkbox" id="autostart"'
    + (S.bot.task === 'none' || S.bot.task === 'disabled' ? '' : ' checked') + '>'
    + '<span class="note">当前：' + esc(taskText(S.bot.task)) + '（计划任务 '
    + esc(S.bot.task_name) + '）</span></div></div>'
    + '<div class="f"><div class="lb">依赖自检</div>'
    + '<div class="hp">缺「必需」的装不上就启动不了；带「渲染成图片用」的缺了只是发文件时降级成文件卡片。</div>'
    + '<div class="deps">' + deps + '</div></div>'
    + '<div class="f"><div class="lb">数据文件位置</div>'
    + '<div class="hp">备忘和灵感都在本地数据库里，配置在下面这几个文件里。'
    + '点「打开」会在资源管理器里定位过去 —— 想拿数据自己处理（或者丢给 AI 分析）时从这里取。</div>'
    + '<div class="deps">' + pathRows() + '</div></div>'
    + '<div class="f"><div class="lb">版本</div>'
    + '<div class="hp">更新方式：拿新版的文件覆盖过去就行，数据不受影响'
    + '（数据在「数据文件位置」那几个地方）。</div>'
    + '<div class="row"><span class="note" id="ver-note">v' + esc(S.version || '?') + '</span>'
    + '<button id="btn-update">检查更新</button></div></div>'
    + '</div></section>';
}

async function checkUpdate(auto){
  const note = $('ver-note');
  const cur = (S && S.version) || '?';
  if(note) note.textContent = 'v' + cur + '（检查中…）';
  let j;
  try{ j = await api('/api/update-check', {}); }
  catch(e){
    if(note) note.textContent = 'v' + cur + '（检查失败：连不上网）';
    return;
  }
  if(note){
    if(!j.ok) note.textContent = 'v' + (j.current || cur) + '（检查失败：' + (j.error || '') + '）';
    else if(j.has_update) note.textContent = '有新版 v' + j.latest + '（当前 v' + j.current + '）';
    else note.textContent = 'v' + j.current + '（已是最新）';
  }
  if(j.ok && j.has_update) toast('有新版本 v' + j.latest + ' —— ' + (j.url || ''));
}

function ignoreSection(g){
  const fs = S.fields.filter(f => f.group === g.key);
  const envs = S.ignore.env || [], files = S.ignore.file || [];
  const rows = []
    .concat(envs.map(r => ({r:r, src:'.env 关键词（在上面的输入框里删）', env:true})))
    .concat(files.map(r => ({r:r, src:'本地规则', env:false})))
    .map(x => '<div class="rule"><span>' + esc(x.r) + '</span>'
      + '<span class="src">' + esc(x.src) + '</span>'
      + (x.env ? '' : '<button data-delrule="' + esc(x.r) + '">删除</button>') + '</div>')
    .join('') || '<div class="note">列表是空的 —— 所有消息都会正常收。</div>';
  return '<section><h2>' + esc(g.title) + '</h2><div class="desc">' + esc(g.desc)
    + '</div><div class="body">'
    + fs.map(fieldHTML).join('')
    + '<div class="f"><div class="lb">再加一条</div>'
    + '<div class="hp">填昵称或 openid。命中的消息整条跳过（不收灵感、也不回应）。</div>'
    + '<div class="row"><input type="text" id="rule-add" placeholder="例如：某某机器人">'
    + '<button id="rule-add-btn">加入</button>'
    + '<button class="danger" id="rule-clear">清空本地规则</button></div></div>'
    + '<div class="f"><div class="lb">当前生效 ' + (envs.length + files.length) + ' 条</div>'
    + '<div class="rules">' + rows + '</div></div>'
    + '</div></section>';
}

/* ── 输入绑定 ─────────────────────────────────────────────── */
function setDirty(k, v){
  const f = S.fields.find(x => x.key === k);
  if(!f) return;
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
  document.querySelectorAll('[data-browse]').forEach(b =>
    b.addEventListener('click', () => openPick(b.dataset.browse)));
  document.querySelectorAll('[data-test]').forEach(b =>
    b.addEventListener('click', () => testRoot(b.dataset.test)));
  document.querySelectorAll('[data-open]').forEach(b =>
    b.addEventListener('click', async () => {
      const j = await api('/api/open-folder', {what: b.dataset.open});
      if(!j.ok) toast('打不开：' + (j.error || ''));
    }));

  const ub = $('btn-update');
  if(ub) ub.addEventListener('click', () => checkUpdate(false));

  document.querySelectorAll('[data-addgroup]').forEach(b =>
    b.addEventListener('click', async () => {
      const j = await api('/api/groups', {add: b.dataset.addgroup});
      if(j.state) S = j.state;
      toast(j.ok ? j.message : ('失败：' + (j.error || '')));
      render();
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

async function save(){
  const {env, st} = splitDirty();
  if(!Object.keys(env).length && !Object.keys(st).length){
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
  DIRTY = {};
  $('savemsg').textContent = '已保存 ✓（改凭据 / 触发词 / 搜索根要重启机器人才生效）';
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


def serve(port=DEFAULT_PORT, open_browser=True, tries=PORT_TRIES):
    """起本地服务（只监听 127.0.0.1）。"""
    sync_env()                       # 先把 .env 灌进 os.environ，跟 bot 的视角对齐
    handler = make_handler(PAGE)
    httpd = None
    last_err = None
    for p in range(port, port + tries):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), handler)
            port = p
            break
        except OSError as e:
            last_err = e
            continue
    if httpd is None:
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
    args = ap.parse_args()
    return serve(port=args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    sys.exit(main())
