"""跨进程单实例锁 —— 走**操作系统级**互斥体，不用「读 pid → 判活 → 写 pid」。

这个文件是干嘛的：给「同一时刻只能跑一个 bot / 一个管家 / 一次启动动作」提供
一把**真正原子**的锁。

为什么不用 pid 文件三步法：**它不原子**。两个进程同时启动时，都能在对方写
pid 之前通过「有没有活着的实例」这一关，于是都认为自己是唯一实例，
最终跑起两个（重复 WebSocket 连接、消息随机丢）。审查报告点出的
「单实例锁存在并发窗口」就是它。命名互斥体由内核保证「同时只有一个持有者」，
不存在这个窗口。

实现：
  · Windows：`CreateMutexW` 命名互斥体；`GetLastError()==ERROR_ALREADY_EXISTS`
    就说明别人已经拿着了。句柄是**进程级**的 —— 持锁进程一退出，
    内核自动释放，不留残留（不会像 pid 文件那样留下一个「僵尸锁」）。
  · 非 Windows：对锁文件做 `fcntl.flock(LOCK_EX|LOCK_NB)`，拿不到就是别人持有。

🔴 锁名带**数据目录**的指纹：同一台机器上「源码版」（数据在代码目录）和
   「安装版」（数据在 %LOCALAPPDATA%）各自的数据目录不同，本来就互不干扰；
   而**同一个数据目录**下只允许一个实例 —— 这正是我们要的语义。

谁调用它：`bot.py`（acquire_pid_lock）、`hub.py`（claim_single_instance）、
        `botctl.py`（启动动作串行化）。
"""
import ctypes
import hashlib
import os

_ERROR_ALREADY_EXISTS = 183

# 持有中的锁 → 免得被 GC 提前关掉句柄（进程活着期间必须一直持有）
_HELD = {}


def _lock_name(scope):
    """把「作用域 + 数据目录」拼成一个全局唯一的锁名。

    🔴 用数据目录指纹而不是写死的名字：同机双份（源码版 / 安装版）数据目录不同，
       不该互相挡；同一个数据目录才该互斥。
    """
    from paths import data_dir
    fp = hashlib.sha1(
        os.path.abspath(data_dir()).encode("utf-8", "replace")).hexdigest()[:16]
    return f"Global\\MoyuBot.{scope}.{fp}"


class SingleInstance:
    """一把命名互斥锁。`acquire()` 成功即持有，直到 `release()` 或进程退出。"""

    def __init__(self, scope):
        self._scope = scope
        self._name = _lock_name(scope)
        self._handle = None
        self._fd = None

    # -- 获取 / 释放 -------------------------------------------------
    def acquire(self):
        """抢锁 → True 表示本进程拿到（是唯一实例）；False 表示别人持有。"""
        if self._handle is not None or self._fd is not None:
            return True                      # 本进程已经拿着了
        return self._acquire_win() if os.name == "nt" else self._acquire_posix()

    def release(self):
        """放开锁（进程退出时系统也会自动放，这里主要用于测试与显式收尾）。"""
        if self._handle is not None:
            try:
                ctypes.windll.kernel32.ReleaseMutex(self._handle)
                ctypes.windll.kernel32.CloseHandle(self._handle)
            except Exception:                                # noqa: BLE001
                pass
            _HELD.pop(self._name, None)
            self._handle = None
        if self._fd is not None:
            try:
                import fcntl
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
            except Exception:                                # noqa: BLE001
                pass
            self._fd = None
        return True

    # -- Windows ----------------------------------------------------
    def _acquire_win(self):
        k32 = ctypes.windll.kernel32
        # bInitialOwner=False：不在创建时立刻持有，改用「创建出来 + 看错误码」
        # 判断 —— 这样 `ERROR_ALREADY_EXISTS` 才是可靠的「别人已持有」信号。
        handle = k32.CreateMutexW(None, False, self._name)
        if not handle:
            return False                     # 建都建不出来：宁可不启动，也别起两个
        if k32.GetLastError() == _ERROR_ALREADY_EXISTS:
            k32.CloseHandle(handle)          # 别人已持有 → 关掉自己这个引用
            return False
        self._handle = handle
        _HELD[self._name] = handle           # 防 GC 关句柄
        return True

    # -- POSIX ------------------------------------------------------
    def _acquire_posix(self):
        import fcntl
        from paths import data_dir
        path = os.path.join(data_dir(), f".{self._scope}.lock")
        fd = None
        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if fd is not None:               # 只在 fd 真开出来了才关（open 自己也会抛）
                try:
                    os.close(fd)
                except OSError:
                    pass
            return False
        self._fd = fd
        return True
