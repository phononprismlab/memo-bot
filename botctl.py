"""机器人进程控制 —— 配置中心和管家（hub）共用的一份逻辑。

这个文件是干嘛的：把机器人（bot.py）的**启 / 停 / 重启 / 开机自启**收一处，
    配置中心页面上的按钮、管家（hub.py）的编排，都调这里，不各写一遍。

为什么独立成文件（而不是塞在配置中心里）：
    三合一打包后，管家也要管机器人。把这套逻辑放进配置后端，
    管家就得 import 一整套配置逻辑（很长一坨），太重。
    抽出来，两边各 import 几个函数就行。

🔴 关键约束（和单机版一致，但打包后更敏感）：
    · 机器人**同一时刻只能跑一个** —— `bot.pid` 是唯一判据。
    · 开机自启 = 注册 Windows 计划任务 `MoyuBot`，但任务指向的是**管家**
      （`--hub --no-browser`），不是直接指向机器人。理由：机器人是管家的子服务，
      让管家在登录时统一拉起来，才不会「计划任务拉一个、管家又拉一个」打架。
    · 桌面配置中心的「启动机器人」按钮，走的是**直接 Popen**（不是 Start-ScheduledTask），
      因为计划任务现在代表「启动管家」，按钮不该去启动管家本体。

调用方：config_core.py（桌面配置中心按钮 / 状态查询）、hub.py（管家编排）。
"""
import os
import subprocess
import sys
import threading
import time

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from paths import data_path, data_dir  # noqa: E402

import single_lock as _single_lock  # noqa: E402  （OS 级跨进程锁）

LAUNCHER = os.path.join(HERE, "memo-launcher.py")   # 统一启动器（补 sys.path + 定数据目录）
PID_FILE = data_path("bot.pid")

# 启动动作的**跨进程**串行锁（见 bot_start 的说明）—— 只护住「检查 + Popen」
# 那一小段，不是机器人运行期的锁（那个归 bot.py 自己的命名互斥体）。
_START_LOCK = _single_lock.SingleInstance("botstart")
_START_THREAD_LOCK = threading.Lock()
BOT_LOG = data_path("bot.out.log")
TASK_NAME = "MoyuBot"


def _pid_alive(pid, _fast=True):
    """进程 pid 还活着吗？

    🔴 Windows 先走 **OpenProcess 快路径**（~0.01ms）。以前只有 tasklist 一条路，
       实测 **221ms** —— 而它被 `bot_running()` 调用、`bot_running()` 又被
       配置中心的 `full_state()` 调用，等于**每次刷新页面都白等 0.2 秒**
       （桌面版切页签、保存设置都会撞上）。
    ⚠️ `_fast=False` 强制走 tasklist：回归测试要靠它验证慢路径的兜底（别删）。
    """
    if pid <= 0:
        return False
    if _fast and os.name == "nt":
        # 🔴 **拿得到句柄 ≠ 进程在跑** —— 这是本函数踩过的最阴的坑（2026-10-12 实测）：
        #    子进程退出后，只要**别人还持有它的句柄**（Popen 就持有子进程句柄，
        #    管家 hub 因此一直握着 bot 的句柄），它就变成「僵尸」留在进程表里 ——
        #    `OpenProcess` 照样**成功**，可进程早没了。
        #    真机现象：bot.pid 记着 20016、`bot_running()` 报「在跑」，
        #    但 tasklist 里根本没有 20016（GetExitCodeProcess 回来是 1 = 已退出）
        #    ⇒ 桌面版「启动」按钮死活点不动（它以为已经在跑）。
        #    修法：拿到句柄后**再问一次退出码**，只有 STILL_ACTIVE(259) 才算在跑。
        verdict = None          # None = 快路径没定论 → 落到下面的 tasklist 兜底
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            # 🔴 restype 必须显式设成 c_void_p：默认 c_int 会把 64 位句柄截断，
            #    高位句柄可能被截成 0（假死）或截成别的非零值（假活）。
            k32.OpenProcess.restype = ctypes.c_void_p
            h = k32.OpenProcess(0x1000, False, int(pid))
            if not h:
                # 87 = ERROR_INVALID_PARAMETER = 压根没这个 PID → 直接判「不在」，
                # 不用再去开 tasklist；其它错误码（多半是权限）交给 tasklist 兜底。
                if k32.GetLastError() == 87:
                    verdict = False
            else:
                try:
                    code = ctypes.c_ulong(0)
                    if k32.GetExitCodeProcess(ctypes.c_void_p(h),
                                              ctypes.byref(code)):
                        verdict = (code.value == 259)     # STILL_ACTIVE
                finally:
                    k32.CloseHandle(ctypes.c_void_p(h))
        except Exception:                                            # noqa: BLE001
            verdict = None
        if verdict is not None:
            return verdict
    try:
        if os.name == "nt":
            # 🔴 两处都要兜底，否则**整个启停流程会被打断**
            #    （实测踩到：bot_stop 成功 → bot_start 在 _pid_alive 崩 → bot 被停掉却没起来）：
            #    1) 显式 encoding + errors="replace" —— 中文 Windows 的 tasklist 输出是 GBK，
            #       而设了 PYTHONUTF8=1 的环境会拿 UTF-8 去解码 → UnicodeDecodeError 直接冒泡。
            #       表头乱码无所谓，PID 是 ASCII，不影响匹配。
            #    2) stdout 兜底成 "" —— 受限环境里它可能是 None，`in None` 会抛 TypeError。
            #    3) CREATE_NO_WINDOW —— tasklist 是控制台程序，父进程（桌面应用）没有
            #       控制台时它会自己新建一个，用户看到黑窗口一闪。常量写字面量而不是
            #       调 helper：回归测试会用 AST 把本函数单独抠出来跑，那时候模块里的
            #       helper 不在作用域，写成名字会 NameError。
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=10,
                                 creationflags=0x08000000).stdout or ""
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


_BOT_CACHE = {"t": 0.0, "v": 0}
_BOT_LOCK = threading.Lock()
_BOT_TTL = 0.6          # 秒：这一小段内的重复查询直接吃缓存


def bot_running(force=False):
    """机器人正在跑就返回它的 pid，否则 0。

    🔴 做个短缓存（0.6 秒）：`full_state()` 一次刷新里可能问好几遍，
       而每次都要读 pid 文件 + 验进程。**刚启停完要拿新状态就传 `force=True`**
       （`bot_start` 的等待循环就是这么用的）。
    """
    now = time.time()
    if not force:
        with _BOT_LOCK:
            if now - _BOT_CACHE["t"] < _BOT_TTL:
                return _BOT_CACHE["v"]
    pid = bot_pid()
    v = pid if _pid_alive(pid) else 0
    with _BOT_LOCK:
        _BOT_CACHE["t"], _BOT_CACHE["v"] = time.time(), v
    return v


def _no_window_flags():
    """子进程别弹控制台窗口（Windows）。

    🔴 powershell / tasklist 都是**控制台程序**：父进程（桌面应用，pythonw 起的）
    没有控制台时，它们会自己新建一个 → 用户看到黑窗口一闪。凡是可能从
    GUI 进程里拉起来的控制台命令，都要带上这个标志。
    """
    if os.name == "nt":
        return 0x08000000      # CREATE_NO_WINDOW
    return 0


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
            timeout=timeout, creationflags=_no_window_flags(),
        )
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except FileNotFoundError:
        return 127, "找不到 powershell（开机自启开关只在 Windows 上支持）"
    except subprocess.SubprocessError as e:
        return 1, f"{type(e).__name__}: {e}"


_TASK_CACHE = {"t": 0.0, "v": "unknown", "busy": False}
_TASK_LOCK = threading.Lock()
_TASK_TTL = 2.0         # 秒：这么久内认为是新鲜的


def _query_task_state():
    """真去查一次（慢：起一次 PowerShell，实测 **1.2 秒**）。"""
    rc, out = _powershell(
        f"$t=Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue;"
        "if($null -eq $t){'NONE'}else{$t.State}", timeout=30)
    if rc != 0:
        v = "unknown"
    else:
        s = (out or "").strip().splitlines()[-1].strip().lower() if out.strip() else ""
        v = {"none": "none", "ready": "ready", "running": "running",
             "disabled": "disabled"}.get(s, "unknown")
    with _TASK_LOCK:
        _TASK_CACHE["t"], _TASK_CACHE["v"] = time.time(), v
    return v


def _task_state_worker():
    """后台线程：查完把 busy 放掉（查不着也不许把 busy 卡住）。"""
    try:
        _query_task_state()
    except Exception:                                                # noqa: BLE001
        pass
    finally:
        with _TASK_LOCK:
            _TASK_CACHE["busy"] = False


def task_state(force=False, block=False):
    """计划任务 `MoyuBot` 的状态：'none' / 'ready' / 'running' / 'disabled' / 'unknown'。

    🔴 **默认永不阻塞**（`block=False`）：缓存过期也先把**旧值**返回，刷新丢给
    后台线程。理由：查一次要起 PowerShell、实测 **1.2 秒**，而它被
    `full_state()` 调用、`full_state()` 又被配置中心每次刷新调用 ——
    同步查等于**每刷新一次界面就白卡 1.2 秒**（桌面版「打开要等几秒」的主因）。

    · `force=True`  → 同步查一次（用户刚开关自启，必须马上拿到准的）
    · `block=True`  → 缓存过期就同步查（老语义，需要立刻准的场合）
    """
    now = time.time()
    with _TASK_LOCK:
        if not force and (now - _TASK_CACHE["t"]) < _TASK_TTL:
            return _TASK_CACHE["v"]          # 新鲜：直接给
        if not force and not block:
            if not _TASK_CACHE["busy"]:      # 过期了：丢给后台，先给旧值
                _TASK_CACHE["busy"] = True
                threading.Thread(target=_task_state_worker, daemon=True).start()
            return _TASK_CACHE["v"]
    return _query_task_state()


def task_state_fresh():
    """缓存里的计划任务状态是不是新鲜的（界面据此决定还要不要再轮询）。"""
    with _TASK_LOCK:
        return bool(_TASK_CACHE["t"]) and \
            (time.time() - _TASK_CACHE["t"]) < _TASK_TTL


def _pythonw():
    """跟当前解释器同目录的 pythonw.exe（无控制台窗口），找不到就用当前解释器。"""
    exe = sys.executable or "python"
    if os.name == "nt" and os.path.basename(exe).lower() == "python.exe":
        cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.isfile(cand):
            return cand
    return exe


def autostart_set(on):
    """开 / 关开机自启（注册 / 删除计划任务）→ (ok, 人话说明)。

    🔴 任务指向**管家**（`--hub --no-browser`），不是直接指向机器人。
       机器人是管家的子服务 —— 让管家在登录时统一拉起来，
       才不会出现「计划任务拉一个、管家再拉一个」两个机器人打架。
       `--no-browser`：登录时别弹浏览器窗口（配置中心 / 抽屉常驻后台即可）。
    """
    if os.name != "nt":
        return False, "开机自启只在 Windows 上支持（靠计划任务实现）"

    def _q(p):
        return p.replace("'", "''")          # PowerShell 单引号串里，单引号要写两遍

    if on:
        script = (
            f"$act=New-ScheduledTaskAction -Execute '{_q(_pythonw())}' "
            f"-WorkingDirectory '{_q(data_dir())}' "
            f"-Argument '\"{_q(LAUNCHER)}\" --hub --no-browser';"
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
        return True, "已开启开机自启 —— 下次登录 Windows 会自动把管家跑起来（含机器人，后台无窗口）"

    script = (f"$t=Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue;"
              f"if($null -ne $t){{Unregister-ScheduledTask -TaskName '{TASK_NAME}' "
              "-Confirm:$false};'OK'")
    rc, out = _powershell(script)
    _TASK_CACHE["t"] = 0.0
    if rc != 0 or "OK" not in out:
        return False, out or "删除计划任务失败"
    # 🔴 这行以前被缩进在 if 里面（永远执行不到）→ 成功关闭时函数**返回 None**，
    #    调用方 `ok, msg = autostart_set(False)` 直接 `TypeError` 崩掉 ——
    #    表现就是「开机自启打开了还关不掉」（用户 2026-10-12 报的）。
    return True, "已关闭开机自启 —— 下次登录不再自动启动（正在跑的机器人不受影响）"


def autostart_state():
    """开机自启是否已开 —— 计划任务**存在且可用**（ready / running）才算开。

    托盘菜单的「开机自启：开/关」和配置中心「⑦ 运行方式」的自启勾选框
    都读它来显示当前状态、再调 `autostart_set` 翻转。

    🔴 **不能 `force=True`** —— 那会同步起一次 PowerShell（1.2 秒），
       而它是在**右键点托盘那一刻**被调用的，用户会看到菜单卡住不弹。
       这里只吃缓存（顺便让后台去刷），拿不到就按「没开」显示 ——
       反正真点了菜单项会走到 `autostart_set()`，那里是同步查的，不会误操作。
    🔴 「unknown」（还没查到）也算没开：查完后台会刷成准的
       （配置中心靠 `_poll_bot_status` 重画那一栏）。
       「disabled」不算开 —— 任务被手动禁用了，登录时并不会跑。
    """
    if os.name != "nt":
        return False
    return task_state() in ("ready", "running")


def bot_stop():
    """停掉机器人 → (ok, 说明)。"""
    pid = bot_running()
    if not pid:
        return True, "机器人本来就没在跑"
    try:
        # 和 _pid_alive 同一个坑：中文 Windows 的 taskkill 输出是 GBK，
        # 这里只取返回码不看文本，但解码仍会在 subprocess 内部发生 → 必须显式指定。
        # taskkill 也是控制台程序 → 不带 CREATE_NO_WINDOW 就会从桌面应用里弹出黑窗口。
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=20,
                       creationflags=_no_window_flags())
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return False, f"停不掉：{type(e).__name__}: {e}"
    for _ in range(24):
        if not _pid_alive(pid):
            return True, f"已停止（pid {pid}）"
        time.sleep(0.25)
    return False, f"发了停止信号，但进程 {pid} 还活着 —— 手动看下任务管理器"


def bot_start():
    """启动机器人 → (ok, 说明)。

    🔴 **直接 Popen**，不走 `Start-ScheduledTask`：计划任务现在指向**管家**，
       按钮去调它反而会启一个多余的管家。机器人是管家的子服务，
       但配置中心页面上「启动机器人」这个动作由管家统一负责也不是不行 ——
       这里保持「按钮 = 直接起机器人进程」，简单且和单机版语义一致。

    🔴 「检查在不在跑 → Popen」两步**不是原子的**：两个启动请求同时到达时，
       都能在对方写出 `bot.pid` 之前通过检查，于是各起一个进程
       （审查报告点出的并发窗口）。所以整段拿一把**启动锁**串起来 ——
       后到的那个进来时，前一个已经把机器人的 `bot.pid` 写好了，
       `bot_running()` 立刻为真，直接返回「已经在跑了」。
       ⚠️ 机器人的**运行期**唯一性由 `bot.py` 自己的 OS 级命名互斥体兜底
       （`single_lock`）：就算这里真漏起了一个，第二个进程也会自己退出。
        锁必须**在检查之前**拿、在拿到之后才检查，顺序不能反。
    """
    if not _START_THREAD_LOCK.acquire(blocking=False):
        return False, "另一个启动操作正在进行，请稍后再试"
    try:
        if not _START_LOCK.acquire():
            return False, "另一个启动操作正在进行，请稍后再试"
        try:
            return _bot_start_locked()
        finally:
            _START_LOCK.release()
    finally:
        _START_THREAD_LOCK.release()


def _bot_start_locked():
    """`bot_start` 的临界区（已持启动锁）—— 见 `bot_start` 的说明。"""
    if bot_running():
        return True, "机器人已经在跑了"
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
    for _ in range(60):                       # 最多等 ~15 秒
        time.sleep(0.25)
        pid = bot_running(force=True)         # 🔴 force：别吃 0.6s 缓存，否则白等
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
