"""摸鱼bot 的**管家**（三合一入口）—— 双击桌面图标后唯一跑起来的东西。

这个文件是干嘛的：把原先**三个各自为政的进程**收进一个「总开关」。

    管家（本文件，常驻）
      ├── memo_gui.py   桌面应用窗口（配置中心 + 脑内抽屉；webui 已退役）
      └── bot.py        机器人本体

为什么要它（原先的三个问题）：
  ① 进程互不知情 —— 抽屉不知道机器人在不在跑；配置中心不知道抽屉起没起。
  ② 用户得自己记「先开哪个」。
  ③ 单实例约束是散的 —— 只有机器人有 `bot.pid`，各界面能开好几个。

本文件只做**生命周期编排**，不重写任何业务逻辑。

设计要点（都在代码里标了 🔴）：

  · **子进程独立**，不用线程。`serve()` 是阻塞的；「重启机器人」本来就得先杀进程，
    业务在管家进程里会让这个语义变味。
  · **单实例锁**用 `hub.pid`（和 `bot.pid` 一个套路）。第二次双击时不再起新进程，
    改为把桌面窗口拉起来（窗口进程自己也有单实例约束）。
  · **开机自启**（botctl 的计划任务）带 `--no-browser`：登录后只起托盘不弹窗口，
    想要窗口从托盘菜单「打开摸鱼bot」点出来。

用法：
  python hub.py                     起管家（桌面窗口 + 机器人）
  python hub.py --no-browser        后台模式：只托盘 + 机器人（计划任务用）
  python hub.py --no-bot            只管桌面窗口，不动机器人

🔴 本文件**不自建图标文件**，托盘图标运行时用 PIL 画（assets/icons/ 是品牌图标唯一来源）。
"""
import argparse
import atexit
import os
import subprocess
import sys
import time

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from paths import data_path  # noqa: E402  （数据目录：pid 等可写文件落哪）

import botctl  # noqa: E402  （机器人进程控制：start/stop/restart，配置中心与管家共用）
import single_lock  # noqa: E402  （OS 级单实例锁）

LAUNCHER = os.path.join(HERE, "memo-launcher.py")
HUB_PID_FILE = data_path("hub.pid")


# ── 解释器 / 创建标志 ───────────────────────────────────────────
# 🔴 进程存活判断（`botctl._pid_alive`）与 pythonw 定位（`botctl._pythonw`）都**复用
#    botctl 的那一份**，本文件不再自带：早先 hub / botctl / bot 各存一份，改一处忘一处
#    就漂移 —— hub 这份曾长期停在「只有 tasklist 慢路径」的旧版，每次判活白等 220ms。
def _detached_flags():
    """无窗口、**脱离本进程组**的创建标志（Windows 专用）。

    子服务（桌面窗口 / 机器人）要比管家活得久，所以除了 `CREATE_NO_WINDOW`
    还得加 `DETACHED_PROCESS`。`CREATE_NO_WINDOW` 那个常量取自 botctl，不写两遍。
    """
    if os.name != "nt":
        return 0
    return botctl._no_window_flags() | 0x00000008      # DETACHED_PROCESS | CREATE_NO_WINDOW


# ── 单实例锁 ────────────────────────────────────────────────────
def hub_pid():
    """`hub.pid` 里记的进程号（0 = 没记）。"""
    try:
        with open(HUB_PID_FILE, encoding="utf-8") as f:
            return int((f.read() or "0").strip())
    except (OSError, ValueError):
        return 0


def hub_running():
    """已有管家在跑就返回它的 pid，否则 0。"""
    pid = hub_pid()
    return pid if botctl._pid_alive(pid) else 0


_HUB_LOCK = single_lock.SingleInstance("hub")


def claim_single_instance():
    """抢单实例锁 → True 表示本进程是唯一的管家；False 表示已有别的在跑。

    🔴 **判据是 OS 级命名互斥体**（见 `single_lock`）—— 「查 pid → 写 pid」
       两步法不原子，两个管家同时启动会都写、都以为自己是唯一（审查报告点出的
       「单实例锁存在并发窗口」）。互斥体由内核保证只有一个持有者。
       `hub.pid` 仍写，只当信息（谁在跑 / 排查），不再当锁。
    """
    if not _HUB_LOCK.acquire():
        return False
    try:
        with open(HUB_PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    return True


def release_single_instance():
    """退出时删掉 `hub.pid` —— 但**只删自己写的那个**（别误删别人的）。"""
    if hub_pid() == os.getpid():
        try:
            os.remove(HUB_PID_FILE)
        except OSError:
            pass


# ── 子服务 ──────────────────────────────────────────────────────
class Child:
    """一个被管家拉起来的子服务 —— 目前只有桌面窗口。

    🔴 机器人**不在**这里：它由 botctl 统一管（写自己的 `bot.pid`），
       管家只通过 `botctl.bot_running() / bot_start() / bot_stop()` 观察与编排。
    """

    def __init__(self, name, args, log_name):
        self.name = name
        self.args = args
        self.log_path = data_path(log_name)
        self.proc = None

    def start(self):
        """起子进程 → True/False。日志追加写到 data_dir 下的 log 文件。"""
        flags = _detached_flags()
        try:
            fh = open(self.log_path, "ab")
            try:
                self.proc = subprocess.Popen(
                    [botctl._pythonw(), "-u", LAUNCHER] + self.args,
                    cwd=HERE, stdin=subprocess.DEVNULL,
                    stdout=fh, stderr=subprocess.STDOUT,
                    creationflags=flags, close_fds=True,
                )
            finally:
                fh.close()
        except OSError as e:
            print(f"[管家] {self.name} 起不来：{e}", flush=True)
            return False
        print(f"[管家] {self.name} 已启动（pid {self.proc.pid}）", flush=True)
        return True

    def stop(self, timeout=5.0):
        """停子进程 —— 先温和 terminate，超时再强杀。"""
        if not self.proc or self.proc.poll() is not None:
            return
        try:
            self.proc.terminate()
        except OSError:
            pass
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                return
            time.sleep(0.2)
        try:
            self.proc.kill()
        except OSError:
            pass


# ── 机器人（批 2：管家统一管）────────────────────────────────────
# 机器人进程不在 children 列表里 —— 它由 botctl 管（写自己的 bot.pid），
# 管家只通过 botctl.bot_running() / bot_start() / bot_stop() 观察与编排。
# 这样配置中心页面上的「启动机器人」按钮（也走 botctl）和管家不会冲突：
# 两边都是「同一个 bot.pid 的读写者」，单一事实来源。


# ── 托盘（批 3：pystray）─────────────────────────────────────────
# 管家常驻后台、没有控制台窗口，用户得有个「看得见摸得着」的入口。
# 双击桌面图标 → 只起管家一个进程；托盘图标就是它留在任务栏的「总开关」。
#
# 🔴 关键约束：
#   · 图标**运行时用 PIL 画**，不依赖外部 .ico 文件 —— 这样「机器人状态」能直接用
#     颜色表达（跑着=品牌青绿，掉了=静默灰），也不用随包多带两个图标资源。
#   · pystray 的消息循环跑在**独立线程**里，主线程留给看门狗；两边的共享状态都走
#     `_tray_state` 这个字典（读多写少，菜单项用可调用标签在打开时现取现值，天然动态）。
#   · 图标颜色变更走 `icon.icon = 新图`；pystray 的 Windows 后端直接调 `Shell_NotifyIcon`
#     这个全局 API（不是往自己的消息循环塞消息），所以从看门狗线程跨线程改是安全的。
import threading

try:
    import pystray
    from PIL import Image, ImageDraw
    _HAVE_TRAY = True
except Exception:  # noqa: BLE001  打包环境里 pystray/PIL 都在；开发机缺了也只是没托盘，不报错
    pystray = None
    Image = ImageDraw = None
    _HAVE_TRAY = False

_tray = None                                  # pystray.Icon 实例（在托盘线程里赋值）
_exit_requested = threading.Event()           # 托盘「退出」点了 → 主循环跳出
_tray_state = {                               # 主循环写、托盘菜单读的共享状态
    "bot_running": False,
    "autostart": False,
}


def _make_icon(running):
    """画一个托盘图标（泡泡母题）：跑着=品牌青绿，掉了=静默灰。"""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    main = (15, 143, 133, 255) if running else (122, 138, 147, 255)   # #0f8f85 / 灰
    d.ellipse([6, 6, size - 6, size - 6], fill=main)                  # 泡泡主体
    d.ellipse([18, 18, 31, 31], fill=(255, 255, 255, 230))             # 高光小白点
    return img


def _set_bot_state(running):
    """机器人状态变了就更新共享状态 + 换图标颜色（只在变化时换，避免每 2 秒重画）。"""
    if _tray_state["bot_running"] == running:
        return
    _tray_state["bot_running"] = running
    if _tray is not None:
        try:
            _tray.icon = _make_icon(running)
        except Exception:  # noqa: BLE001
            pass


def _act_open_gui(icon, item):
    """托盘「打开摸鱼bot」→ 拉起桌面窗口（窗口进程自带单实例约束）。"""
    _launch_gui()


def _launch_gui():
    """起桌面应用窗口（独立进程，关了窗口管家和托盘都还在）。"""
    try:
        subprocess.Popen(
            [botctl._pythonw(), "-u", LAUNCHER, "--gui"],
            cwd=HERE, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=_detached_flags(), close_fds=True,
        )
    except OSError as e:
        print(f"[管家] 桌面窗口起不来：{e}", flush=True)


def _act_bot_start(icon, item):
    botctl.bot_start()
    _set_bot_state(botctl.bot_running())


def _act_bot_stop(icon, item):
    botctl.bot_stop()
    _set_bot_state(botctl.bot_running())


def _act_bot_restart(icon, item):
    botctl.bot_restart()
    _set_bot_state(botctl.bot_running())


def _act_toggle_autostart(icon, item):
    nv = not _tray_state["autostart"]
    ok, msg = botctl.autostart_set(nv)
    if ok:
        _tray_state["autostart"] = nv
    else:
        print(f"[管家] 开机自启切换失败：{msg}", flush=True)


# ── 托盘「退出」：先确认，再联动关窗口 ────────────────────────────
def _confirm_exit():
    """弹一个**原生**确认框 → True 表示用户确认退出。

    🔴 用 Win32 的 `MessageBoxW`，不碰 Tk：托盘菜单的回调跑在 pystray 的
       独立线程里，在那里建 Tk 根窗口会和桌面窗口那个进程抢单例、也容易卡。
       原生框跨线程随便用，也不需要额外依赖。
    🔴 默认按钮放在「否」上（`MB_DEFBUTTON2`）：敲回车不会误退出。
    """
    if os.name != "nt":
        return True
    try:
        import ctypes
        MB_YESNO = 0x00000004
        MB_ICONWARNING = 0x00000030
        MB_DEFBUTTON2 = 0x00000100
        MB_TOPMOST = 0x00040000
        MB_SETFOREGROUND = 0x00010000
        IDYES = 6
        text = ("退出会一并关闭摸鱼bot：桌面窗口和后台机器人都会停掉，\n"
                "退出后机器人不再收发消息（直到下次启动）。\n\n"
                "确定要退出吗？")
        r = ctypes.windll.user32.MessageBoxW(
            0, text, "摸鱼bot",
            MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2 | MB_TOPMOST | MB_SETFOREGROUND)
        return r == IDYES
    except Exception:  # noqa: BLE001  弹不出来就别拦着用户退出
        return True


def _close_gui_window():
    """把已经开着的桌面窗口关掉（发 `WM_CLOSE`，等价于点窗口右上角的 ×）。

    🔴 桌面窗口是**独立进程**（`--gui`），托盘退出不会自动带走它 ——
       用户点了托盘「退出」却发现窗口还杵在桌面上（用户 2026-10-12 报的
       「托盘退出后窗口没有自动关闭」）。
    🔴 窗口可能是**托盘菜单后来拉起来的**（`_launch_gui`），那种不在 `children`
       里、拿不到 Popen 句柄 —— 所以按窗口标题找主窗口发消息，这是唯一
       对「谁开的」都管用的办法。找不到窗口（本来就没开）就静默跳过。
    🔴 `FindWindowW.restype` 必须显式设成 `c_void_p`：默认 `c_int` 会把
       64 位窗口句柄截断，`PostMessageW` 于是发给了不存在的窗口。
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        u = ctypes.windll.user32
        u.FindWindowW.restype = ctypes.c_void_p
        hwnd = u.FindWindowW(None, "摸鱼bot")
        if hwnd:
            u.PostMessageW(ctypes.c_void_p(hwnd), 0x0010, 0, 0)     # WM_CLOSE
    except Exception:  # noqa: BLE001
        pass


def _act_exit(icon, item):
    """托盘「退出」→ 先确认，再通知主循环收尾（关窗口 + 停机器人），最后摘图标。"""
    if not _confirm_exit():
        return                       # 用户点了「否」：什么都不做，图标继续挂着
    _exit_requested.set()
    try:
        icon.stop()
    except Exception:  # noqa: BLE001
        pass


def _build_menu():
    """菜单项用可调用标签，打开时现取 `_tray_state` 现值 → 状态/开关文字自动同步。"""
    s = _tray_state
    return pystray.Menu(
        pystray.MenuItem("打开摸鱼bot", _act_open_gui, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            lambda item: "机器人：运行中" if s["bot_running"] else "机器人：已停止",
            pystray.Menu(
                pystray.MenuItem("启动", _act_bot_start,
                                enabled=lambda item: not s["bot_running"]),
                pystray.MenuItem("停止", _act_bot_stop,
                                enabled=lambda item: s["bot_running"]),
                pystray.MenuItem("重启", _act_bot_restart),
            ),
        ),
        pystray.MenuItem(
            lambda item: "开机自启：开" if s["autostart"] else "开机自启：关",
            _act_toggle_autostart,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", _act_exit),
    )


def _run_tray():
    """在独立线程里起 pystray 消息循环（阻塞到 icon.stop()）。"""
    global _tray
    icon = pystray.Icon("摸鱼bot", _make_icon(_tray_state["bot_running"]),
                        "摸鱼bot 管家", menu=_build_menu())
    _tray = icon
    icon.run()


def _start_tray():
    if not _HAVE_TRAY:
        return
    _tray_state["autostart"] = botctl.autostart_state()
    threading.Thread(target=_run_tray, daemon=True).start()


def _sleep_or_exit(sec):
    """分段睡，期间一旦点了「退出」立刻醒。"""
    step = 0.1
    while sec > 0 and not _exit_requested.is_set():
        time.sleep(step)
        sec -= step


# ── 主流程 ──────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="摸鱼bot 管家（桌面窗口 + 机器人）")
    ap.add_argument("--no-browser", action="store_true",
                    help="不起桌面窗口（开机自启用：只留托盘，要窗口从托盘菜单开）")
    ap.add_argument("--no-bot", action="store_true", help="不动机器人")
    args = ap.parse_args()

    if not claim_single_instance():
        print(f"[管家] 已经有一个在跑了（pid {hub_running()}），把桌面窗口顶出来。", flush=True)
        _launch_gui()
        return 0
    atexit.register(release_single_instance)

    # 1) 桌面窗口（webui 已退役：配置中心和脑内抽屉都在这个原生窗口里）。
    #    --no-browser = 开机自启后台模式：只留托盘，窗口从托盘菜单开。
    children = []
    if not args.no_browser:
        gui = Child("摸鱼bot 窗口", ["--gui"], "hub-gui.log")
        if gui.start():
            children.append(gui)
        else:
            print("[管家] ⚠ 桌面窗口没起来（看 hub-gui.log）；托盘菜单仍可再开。", flush=True)

    # 2) 机器人（除非 --no-bot）
    # 🔴 botctl.bot_start() 是幂等的：已经在跑就直接返回。所以「桌面窗口点了启动机器人」
    #    和「管家启动时拉起」不会重复起。机器人是管家的直接子进程（botctl 在管家进程里 Popen），
    #    退出时由 botctl.bot_stop() 统一收尾。
    bot_wanted = not args.no_bot
    bot_restarts = 0
    if bot_wanted:
        ok, msg = botctl.bot_start()
        print(f"[管家] 机器人：{msg}", flush=True)
    _tray_state["bot_running"] = bot_wanted and bool(botctl.bot_running())

    # 3) 起托盘（独立线程跑 pystray 消息循环；没装 pystray 就静默跳过）
    _start_tray()

    # 4) 常驻 —— 盯着机器人；崩了自动拉起（最多 3 次，防抖动死循环）。
    #    桌面窗口被用户关掉不算事（托盘菜单随时再开），管家不跟着退。
    print("[管家] 已就绪（托盘图标在任务栏，右键开窗口 / 管机器人）。Ctrl+C 退出。", flush=True)
    try:
        while not _exit_requested.is_set():
            _sleep_or_exit(2.0)
            if _exit_requested.is_set():
                break
            for c in children:
                if c.proc and c.proc.poll() is not None:
                    print(f"[管家] {c.name} 已关闭（用户关窗口属正常）。", flush=True)
                    c.proc = None
            if bot_wanted:
                if botctl.bot_running():
                    _set_bot_state(True)
                else:
                    _set_bot_state(False)
                    if bot_restarts < 3:
                        bot_restarts += 1
                        ok, msg = botctl.bot_start()
                        print(f"[管家] 机器人掉了，重启（第 {bot_restarts} 次）：{msg}", flush=True)
                    else:
                        print("[管家] ⚠ 机器人反复退出，不再自动重启（看 bot.out.log）", flush=True)
                        bot_wanted = False     # 停掉自动重启，避免空转
    except KeyboardInterrupt:
        print("\n[管家] 收到 Ctrl+C，正在收尾…", flush=True)
    finally:
        # 🔴 先关桌面窗口：它是独立进程，不主动关就会留在桌面上
        #    （托盘菜单后来拉起来的那个不在 `children` 里，所以统一按窗口标题关）。
        _close_gui_window()
        if bot_wanted:
            botctl.bot_stop()
        for c in children:
            c.stop()
        if _tray is not None:
            try:
                _tray.stop()
            except Exception:  # noqa: BLE001
                pass
        release_single_instance()
        print("[管家] 已退出。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
