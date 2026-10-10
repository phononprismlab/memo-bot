"""Windows 关机通知的常驻监听 —— 关机播报的可靠触发。

这个文件是干嘛的：Windows 计划任务用「系统事件 1074」触发关机脚本**不可靠**。
实测（2026-10-10 02:25:31 那次关机）：任务确实启动了（LastRunTime 有值），
但返回码 0xC000026B、脚本一行日志都没写 —— 关机流程没给 Python 启动的机会。

改用**常驻进程**监听 `WM_QUERYENDSESSION`：用户点关机时，Windows 会向所有顶层窗口
广播这条消息，**并等窗口回应**（`WaitToKillAppTimeout`，默认 5 秒）。这是能拿到的
最早信号，不依赖任务计划服务在关机阶段的行为，也不需要管理员权限 ——
所以安装版也能用（常驻 bot 本来就开着）。

🔴 必须是**顶层窗口**才收得到：message-only 窗口（`HWND_MESSAGE`）收不到广播。
   这里建一个永不显示（不带 `WS_VISIBLE`）的顶层窗口，只用来接消息。

谁调用它：`bot.py` 启动时 `install(callback)` 起一个后台守护线程。
"""
import ctypes
import os
import threading

WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
WM_DESTROY = 0x0002

_HWND = None          # 建好的窗口句柄（给自检 / 测试用）
_WNDPROC = None       # 回调引用，防止被 GC 掉


def available():
    """当前平台能不能装这个监听。"""
    return os.name == "nt"


def install(callback, on_error=None):
    """起后台线程监听关机消息；收到就调 `callback()`（**只调一次**，同步执行）。

    返回 True 表示已装上（不代表系统一定会广播，那要真关机才知道）。
    """
    if os.name != "nt":
        return False

    def _worker():
        try:
            _loop(callback)
        except Exception as exc:                      # noqa: BLE001
            if on_error:
                try:
                    on_error("关机监听线程退出：{}: {}".format(type(exc).__name__, exc))
                except Exception:
                    pass

    threading.Thread(target=_worker, name="shutdown-hook", daemon=True).start()
    return True


def _loop(callback):
    """建顶层窗口 + 跑消息循环（阻塞，直到窗口销毁）。"""
    global _HWND, _WNDPROC

    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    wndproc_t = ctypes.WINFUNCTYPE(
        ctypes.c_longlong, wintypes.HWND, wintypes.UINT,
        wintypes.WPARAM, wintypes.LPARAM,
    )

    fired = {"done": False}

    def _fire():
        if fired["done"]:
            return
        fired["done"] = True
        try:
            callback()
        except Exception:
            pass

    def _proc(hwnd, msg, wparam, lparam):
        if msg == WM_QUERYENDSESSION:
            _fire()
            return 1                      # 回应「可以关机」，别拖住系统
        if msg == WM_ENDSESSION:
            _fire()
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    proc = wndproc_t(_proc)

    # 🔴 64 位下必须显式声明 argtypes/restype：ctypes 默认按 c_int 处理，
    #    句柄会被截断，LPARAM 里的大值会直接 OverflowError（DefWindowProcW 会炸）。
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    user32.RegisterClassW.argtypes = [ctypes.c_void_p]
    user32.RegisterClassW.restype = ctypes.c_ushort
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE, ctypes.c_void_p,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.DefWindowProcW.restype = ctypes.c_longlong
    user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
    ]
    user32.GetMessageW.restype = ctypes.c_int

    class _WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", wndproc_t),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HANDLE),
            ("hIcon", wintypes.HANDLE),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HANDLE),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    hinst = kernel32.GetModuleHandleW(None)
    cls = _WNDCLASSW()
    cls.lpfnWndProc = proc
    cls.hInstance = hinst
    cls.lpszClassName = "MemoBotShutdownHook"
    if not user32.RegisterClassW(ctypes.byref(cls)):
        raise ctypes.WinError(ctypes.get_last_error())

    hwnd = user32.CreateWindowExW(
        0, cls.lpszClassName, "MemoBotShutdownHook", 0,
        0, 0, 0, 0, None, None, hinst, None,
    )
    if not hwnd:
        raise ctypes.WinError(ctypes.get_last_error())

    _HWND = hwnd
    _WNDPROC = proc                       # 保持引用

    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
