"""统一的启动入口 —— 安装版和源码直跑都走它。先做三件事，再把参数转交给真正的脚本。

为什么需要它：

1. **补 `sys.path`**。安装版用的是 Python embeddable，它自带的 `python313._pth` 会让
   Python 进「隔离模式」—— **脚本所在目录不会自动进 `sys.path`**，于是
   `from store import ...` 直接 `ModuleNotFoundError`。这里补上。
2. **定数据目录（只在安装版）**。代码装在安装目录（可能只读），而 `.env` / `memos.db` /
   `settings.json` 这些要写，必须落到用户可写的地方 → 设 `MEMO_DATA_DIR`（见 `paths.py`）。
   🔴 靠「安装根有没有 `node/` 子目录」判断是不是安装版 —— **源码直跑一律不设**，
   各模块回退到代码目录，行为与打包前完全一致。
   （早先无条件设，源码版数据目录被改到 `%LOCALAPPDATA%/memo-bot`，读不到旧库。）
3. **让随包的 node 能被找到**。安装版把 `node.exe` 放在 `node/` 子目录，不在 PATH 上，
   这里设 `MEMO_NODE` 指过去。（`playwright-core` 不用管 —— 它放在
   `node_modules/` 下，`md2png.mjs` 本来就会去那儿找。）

用法（`--xxx` 之后的参数原样透传给对应脚本）：

    memo-launcher.py --gui               起桌面应用（配置中心 + 脑内抽屉，一个窗口）
    memo-launcher.py --hub               起管家（桌面窗口 + 机器人；webui 已退役）
    memo-launcher.py --bot --log X       跑机器人
    memo-launcher.py --send --file X     发文件
    memo-launcher.py --ideas --export    导出灵感池
"""
import os
import runpy
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# 安装版结构：<安装根>/app/（代码）+ <安装根>/node/node.exe。源码目录没有 node/。
_NODE_IN_PACKAGE = os.path.join(os.path.dirname(HERE), "node", "node.exe")
PACKAGED = os.path.isfile(_NODE_IN_PACKAGE)

# ── 1. 数据目录（可写）—— 🔴 只有安装版才改 ─────────────────────
if PACKAGED and not os.environ.get("MEMO_DATA_DIR"):
    _base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    os.environ["MEMO_DATA_DIR"] = os.path.join(_base, "memo-bot")
if os.environ.get("MEMO_DATA_DIR"):
    try:
        os.makedirs(os.environ["MEMO_DATA_DIR"], exist_ok=True)
    except OSError:
        pass

# ── 2. sys.path（embeddable 隔离模式的解药）────────────────────
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# ── 3. 随包的 node ────────────────────────────────────────────
# 🔴 node.exe 在**安装目录根的 node/ 下**，而本文件在 app/ 里 → 要往上一层找。
if PACKAGED and not os.environ.get("MEMO_NODE"):
    os.environ["MEMO_NODE"] = _NODE_IN_PACKAGE

# ── 4. 按第一个参数分派 ───────────────────────────────────────
SCRIPTS = {
    "--hub": "hub.py",
    "--gui": "memo_gui.py",
    "--bot": "bot.py",
    "--send": "send_file.py",
    "--ideas": "ideas.py",
    "--moyu": "moyu.py",
    "--export": "export.py",
}

_argv = sys.argv[1:]
if _argv and _argv[0] in SCRIPTS:
    mode, rest = _argv[0], _argv[1:]
else:
    mode, rest = "--gui", _argv          # 什么都不带 = 起桌面应用（双击快捷方式走这条）

target = os.path.join(HERE, SCRIPTS[mode])
sys.argv = [target] + rest
runpy.run_path(target, run_name="__main__")
