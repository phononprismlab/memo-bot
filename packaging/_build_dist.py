"""组装 memo-bot 安装版目录（packaging/build/memo-bot）。

产物结构（会被 Inno Setup 原样装到 %LOCALAPPDATA%\\memo-bot）：
    py/     Python embeddable + site-packages   ← 免安装运行时
    app/    memo-bot 代码 + node_modules/playwright-core
    node/   node.exe                            ← 渲染链路用

跑法：python packaging/_build_dist.py

先决条件：`packaging/py/` 得先存在（就是那个已装好依赖的 embeddable Python）。
    它是**按需重建的**，不进仓库（47MB，且含二进制）。
"""
import os
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 🔴 本文件现在住在仓库里：packaging/ 的上一级就是 memo-bot/ 源码目录，
#    再上一级是仓库根。产物一律落在 packaging/build/ 下（build/ 已 gitignore）。
PKG = Path(__file__).resolve().parent      # <仓库>/packaging/
REPO = PKG.parent                          # <仓库>（= memo-bot 源码目录）
PY = PKG / "py"                            # 已装好依赖的 embeddable（本地重建，不进仓库）
NODE_EXE = Path("C:/Users/Administrator/.workbuddy-ai/binaries/node/versions/22.22.2-6/node.exe")
PWC = Path("C:/Users/Administrator/.workbuddy-ai/binaries/node/workspace/node_modules/playwright-core")
DIST = PKG / "build" / "memo-bot"

# app/ 里要拷的代码文件（不含开发用的 test/ 和打包用的脚本）
APP_FILES = [
    "bot.py", "brain-drawer.py", "config-web.py", "export.py", "ideas.py",
    "ignore.py", "md2img.py", "moyu.py", "send_file.py", "settings.py",
    "store.py", "paths.py", "memo-launcher.py", "hub.py", "media.py",
    "push.py", "version.py",
    # 桌面版这条线（原生界面 + 进程/计划任务查询 + 自定义指令管理）
    "memo_gui.py", "botctl.py", "extmgr.py",
    "md2png.mjs", "moyu-data.json", "README.md", ".env.example",
    "VERSION",
]
# app/ 里要拷的目录（extensions/local 是私有扩展，不对外发）
APP_DIRS = ["docs", "extensions"]


def size_of(p):
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def main():
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)

    # 1) Python 运行时（embeddable + 依赖）
    shutil.copytree(PY, DIST / "py")

    # 2) 代码
    app = DIST / "app"
    app.mkdir()
    missing = []
    for f in APP_FILES:
        s = REPO / f
        if s.exists():
            shutil.copy2(s, app / f)
        else:
            missing.append(f)
    for d in APP_DIRS:
        s = REPO / d
        if s.is_dir():
            shutil.copytree(s, app / d,
                            ignore=shutil.ignore_patterns("local", "__pycache__"))

    # 3) node
    (DIST / "node").mkdir()
    shutil.copy2(NODE_EXE, DIST / "node" / "node.exe")

    # 4) playwright-core —— 放 app/node_modules 下，探测逻辑（HERE/node_modules/…）自动命中
    nm = app / "node_modules"
    nm.mkdir()
    shutil.copytree(PWC, nm / "playwright-core")

    # 5) 报告
    print("组装完成：", DIST)
    if missing:
        print("  ⚠ 源里缺这些文件（已跳过）：", ", ".join(missing))
    print()
    total = 0
    for sub in ("py", "app", "node"):
        s = size_of(DIST / sub)
        total += s
        print(f"  {sub:6s} {s / 1048576:7.1f} MB")
    print(f"  {'合计':6s} {total / 1048576:7.1f} MB")
    print()
    print("  app/ 内容：")
    for f in sorted(os.listdir(app)):
        print(f"    {f}")


if __name__ == "__main__":
    main()
