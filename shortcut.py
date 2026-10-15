"""开始菜单快捷方式：任务栏图标按 AUMID 认，这里把「同一个 AUMID + 金鱼图标」
写进快捷方式，Windows 就会从它取任务栏 / 开始菜单 / 固定按钮的图标。

干嘛的：进程里 `iconphoto` / `iconbitmap` 只作用于窗口标题栏，落不到任务栏 ——
        任务栏按钮的图标是 shell 按「进程 AppUserModelID」找的：开始菜单里有
        带**同一个 AUMID** 的快捷方式时，图标以它为准（金鱼）；一个都没有时
        退回宿主 exe 的默认图标（Tk 蓝羽毛）。真机实验结论：AUMID 在这台机器
        上被缓存成别的图标时，这条快捷方式的路子能把图标盖回去。
谁调用它：`hub.py` 管家启动时（常驻进程，幂等，每次启动核一次）；
        安装版另外由安装脚本 `packaging/memo-bot.iss` 的 [Icons] AppUserModelID
        写入同一份定义 —— 两处字符串必须一致（`test/run-tests.py` 有护栏）。

🔴 AUMID 常量只在**本文件**定义一次，`memo_gui.win_app_id()` 从这里 import ——
    两处各写一份必然漂移（改了一处另一处没改 = 任务栏图标又找不着北）。
🔴 快捷方式指向 `memo-launcher.py --hub`（和安装版 [Icons] 同一个入口）：
    开始菜单点它 = 起管家（托盘），桌面窗口从托盘菜单开。
"""
import logging
import os
import sys

log = logging.getLogger(__name__)

# 🔴 与包装脚本 packaging/memo-bot.iss 的 [Icons] AppUserModelID 保持一致
APP_USER_MODEL_ID = "PhononPrismLab.MoyuBot.Desktop"

LNK_NAME = "摸鱼bot.lnk"


def _app_dir():
    return os.path.dirname(os.path.abspath(__file__))


def pythonw():
    """跑管家用的 pythonw.exe。

    安装版在**上一层目录的 py/**（`<安装根>/py/pythonw.exe`，memo-launcher.py
    在 `app/` 里，判断方式和 launcher 的 PACKAGED 检测同源）；源码直跑用仓库
    自带的 packaging/py。
    """
    here = _app_dir()
    packaged = os.path.join(os.path.dirname(here), "py", "pythonw.exe")
    if os.path.isfile(packaged):
        return packaged
    local = os.path.join(here, "packaging", "py", "pythonw.exe")
    if os.path.isfile(local):
        return local
    # 都没有（比如换了结构）→ 退回当前解释器旁边的，别返回 None 让调用方炸
    return os.path.join(os.path.dirname(sys.executable), "pythonw.exe")


def launcher():
    """管家入口脚本（和本模块同目录，源码版 / 安装版布局都一样）。"""
    return os.path.join(_app_dir(), "memo-launcher.py")


def icon():
    """品牌金鱼图标（assets/icons 是全仓图标唯一来源）。"""
    return os.path.join(_app_dir(), "assets", "icons", "installer.ico")


def work_dir():
    """快捷方式的工作目录：app 目录（launcher 靠它定位随包资源）。"""
    return _app_dir()


def start_menu_dir():
    """当前用户的「开始菜单 → 程序」目录。"""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Microsoft", "Windows", "Start Menu",
                        "Programs")


def lnk_path():
    return os.path.join(start_menu_dir(), LNK_NAME)


def _build_lnk():
    """按既定规格拼一个 .lnk（pylnk3 是随包的纯 Python 模块，无第三方依赖）。

    AUMID 写进 PropertyStoreDataBlock：FMTID {9F4C2855-9F79-4B39-A8D0-
    E1D42DE1D5F9}（PKEY_AppUserModel_ID）的属性 5，VT_LPWSTR。
    """
    import pylnk3

    lnk = pylnk3.for_file(
        pythonw(),                      # 目标（.exe）
        arguments='"%s" --hub' % launcher(),
        description="常驻后台，托盘里开配置中心 / 脑内抽屉 / 机器人",
        icon_file=icon(),
        work_dir=work_dir(),
    )
    value = pylnk3.TypedPropertyValue()
    value.set_string(APP_USER_MODEL_ID)     # VT_LPWSTR
    store = pylnk3.PropertyStore(
        properties=[(5, value)],
        format_id=pylnk3.bytes_from_guid(
            "{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F9}"),
    )
    block = pylnk3.ExtraData_PropertyStoreDataBlock(stores=[store])
    lnk.extra_data = pylnk3.ExtraData(blocks=[block])
    return lnk


def ensure():
    """幂等创建 / 刷新开始菜单快捷方式。失败只记日志，绝不影响管家启动。

    干嘛的：管家每次启动核一次。已经存在也照样重写一遍（目标路径会随安装
                位置变，重写保证永远指向当前这一份）。
    返回快捷方式路径；环境不支持（非 Windows / 没 pylnk3 / 目录不可写）返回 None。
    """
    if os.name != "nt":
        return None
    path = lnk_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _build_lnk().save(path)
        log.info("开始菜单快捷方式已就绪：%s", path)
        return path
    except Exception as e:                          # noqa: BLE001
        # 任务栏图标退回宿主默认（羽毛）而已，不值得让管家起不来
        log.warning("开始菜单快捷方式创建失败（任务栏图标将退化为默认）：%s", e)
        return None
