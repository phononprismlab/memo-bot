"""诊断包：把「环境 + 配置摘要 + 日志尾部」打成一个 zip，供用户发给开发者。

干嘛的：用户遇到恶性 bug 时，日志散在数据目录里（安装版是
        `%LOCALAPPDATA%\\memo-bot`）—— 普通用户既找不到、也不知道该发哪一个。
        这里一键生成一个**脱敏**的诊断包放到桌面，用户直接转发即可。
谁调用它：`memo_gui.py` 配置中心「版本与诊断」区的「导出诊断包」按钮；
        也可以直接命令行跑（`python diag.py`）给自己排障。

🔴 **绝不打包用户数据**：`memos.db`（待办 / 灵感）、`.env` 的**值**（只列键名）、
   `moyu-user.json`、`extensions/local/` 一律不进包。这是本模块的第一原则，
   改这里的代码前先想一遍「会不会把用户的东西带出去」。
"""
import datetime
import glob
import os
import platform
import sys
import zipfile

from paths import data_dir

import config_core
import settings as _settings

LOG_TAIL = 200 * 1024        # 每个日志文件最多带末尾 200KB（够定位，又不会太大）

# .env 里这些键的**值**要打码（凭据 / 会话 ID 类）
_SECRET_HINTS = ("SECRET", "TOKEN", "KEY", "APPID", "PASSWORD",
                 "OPENID", "TARGETS")

# 要收进包的日志（数据目录下的文件名，存在才带）
LOG_NAMES = ("bot.out.log", "bot.out.log.1", "botpy.log",
             "hub-gui.log", "install-deps.log", "gui-error.log")


def _desktop_dir():
    """桌面目录 —— 用系统 API 拿，处理 OneDrive 重定向与中文「桌面」。"""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        # CSIDL_DESKTOPDIRECTORY = 0x0010
        ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf)
        if buf.value:
            return buf.value
    except Exception:                                            # noqa: BLE001
        pass
    return os.path.join(os.path.expanduser("~"), "Desktop")


def _tail(path, limit=LOG_TAIL):
    """取文件末尾 `limit` 字节（截断时在开头标一行说明）。

    🔴 按**字节**读末尾、再按 UTF-8 解码并丢弃可能被切一半的首字符 ——
       直接 `readlines()` 对大日志会把整个文件读进内存。
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    try:
        with open(path, "rb") as f:
            if size > limit:
                f.seek(size - limit)
            raw = f.read()
    except OSError:
        return None
    text = raw.decode("utf-8", errors="replace")
    if size > limit:
        text = (f"（日志共 {size // 1024} KB，这里只带末尾 {limit // 1024} KB）\n"
                + text.lstrip("\ufffd"))
    return text


def _env_report():
    """.env 的**键名**清单 —— 值一律不原样带出去。"""
    env = config_core.read_env_file()
    if not env:
        return "（.env 不存在，或里面没有任何键）\n"
    out = ["下面只列「设了哪些键」，凭据与会话 ID 的值一律打码。\n"]
    for k in sorted(env):
        v = env.get(k) or ""
        if not v.strip():
            shown = "<空>"
        elif any(h in k.upper() for h in _SECRET_HINTS):
            shown = f"<已打码，{len(v)} 字符>"
        elif len(v) > 80:
            shown = v[:77] + "…"
        else:
            shown = v
        out.append(f"{k:26} {shown}")
    return "\n".join(out) + "\n"


def _settings_report():
    """settings.json 原文 —— 里面是本机行为配置（不含凭据）。"""
    p = _settings.file_path()
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except OSError as e:
        return f"（读不到 settings.json：{e}）\n"
    if not raw.strip():
        return "（settings.json 是空的 —— 全部走默认值）\n"
    return ("下面是本机设置原文（启动播报、回执文案这些是用户自己写的）。\n"
            "觉得哪一行不方便外发，删掉那行再发也行。\n\n" + raw)


def _env_info(state):
    """环境信息：版本 / 系统 / Python / 进程 / 依赖自检。"""
    bot = state.get("bot", {})
    deps = state.get("deps", [])
    lines = [
        "── 程序 ──────────────────────────────────────",
        f"版本            {state.get('version', '?')}",
        f"仓库            {state.get('repo', '')}",
        f"数据目录        {state.get('paths', {}).get('data_dir', data_dir())}",
        f"代码目录        {state.get('paths', {}).get('here', '')}",
        f"凭据是否填全    {state.get('ready', False)}",
        "",
        "── 机器人进程 ────────────────────────────────",
        f"是否在跑        {bot.get('running', False)}",
        f"PID             {bot.get('pid') or '-'}",
        f"计划任务        {bot.get('task_name', '')} = {bot.get('task', '?')}",
        f"开机自启        {bot.get('autostart', False)}",
        "",
        "── 运行环境 ──────────────────────────────────",
        f"操作系统        {platform.platform()}",
        f"Python          {sys.version.splitlines()[0]}",
        f"解释器          {sys.executable}",
        f"工作目录        {os.getcwd()}",
        "",
        "── 依赖自检 ──────────────────────────────────",
    ]
    if not deps:
        lines.append("（没有依赖信息）")
    for d in deps:
        mark = "OK  " if d.get("ok") else "缺失"
        lines.append(f"[{mark}] {d.get('name', '?')}：{d.get('detail', '')}")
    return "\n".join(lines) + "\n"


def _readme_text(stamp):
    return (
        "摸鱼bot 诊断包\n"
        "==============\n\n"
        f"生成时间：{stamp}\n\n"
        "这个包里有什么：\n"
        "  · 环境.txt    —— 版本、系统、Python、进程状态、依赖自检结果\n"
        "  · 配置摘要.txt —— 设了哪些配置项（凭据 / 会话 ID 已打码）+ 本机设置原文\n"
        "  · 日志/       —— 最近一段运行日志（每个文件只带末尾 200KB）\n\n"
        "这个包里没有什么（放心转发）：\n"
        "  · 你的待办和思绪（memos.db）\n"
        "  · 你的 AppSecret / AppID 等凭据\n"
        "  · 你导入的自定义指令\n\n"
        "怎么用：把整个 zip 发给群主 / 发到 QQ 群，或者邮件附上即可。\n"
    )


def export_bundle(state=None, out_dir=None):
    """生成诊断包 → `(ok, 说明文案, zip 路径或 None)`。

    `state` 传 `config_core.full_state()` 的结果（界面里现成的那个）；
    不传就自己取一份（命令行跑用）。
    """
    if state is None:
        try:
            state = config_core.full_state()
        except Exception as e:                                   # noqa: BLE001
            return False, f"取运行状态失败：{e}", None

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    title = f"摸鱼bot-诊断-{stamp}"
    dest_dir = out_dir or _desktop_dir()
    dest = os.path.join(dest_dir, title + ".zip")

    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as e:
        return False, f"打不开保存位置：{e}", None

    try:
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(f"{title}/说明.txt",
                       _readme_text(datetime.datetime.now()
                                    .strftime("%Y-%m-%d %H:%M:%S")))
            z.writestr(f"{title}/环境.txt", _env_info(state))
            z.writestr(f"{title}/配置摘要.txt",
                       "=== 连接设置（.env）===\n" + _env_report()
                       + "\n=== 机器人设置（settings.json）===\n" + _settings_report())
            for name in LOG_NAMES:
                p = os.path.join(data_dir(), name)
                if not os.path.isfile(p):
                    continue
                body = _tail(p)
                if body is None:
                    continue
                z.writestr(f"{title}/日志/{name}", body)
    except Exception as e:                                       # noqa: BLE001
        # 🔴 兜住一切：这是「用户已经遇到 bug 了」才点的按钮，绝不能因为
        #    某个日志文件读不动 / 编码怪就把整个导出搞崩。
        return False, f"写诊断包失败：{e}", None

    return True, f"诊断包已放到桌面：{os.path.basename(dest)}", dest


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ok, msg, path = export_bundle()
    print(msg if ok else f"失败：{msg}")
    if path:
        print(path)
    sys.exit(0 if ok else 1)
