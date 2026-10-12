"""集中管理两类路径：只读的**代码资源** 与 可写的**数据目录**。

为什么要单独一个文件：打包成安装版之后，代码装在安装目录（可能只读），
而 `.env` / `memos.db` / `settings.json` 这类**要写**的东西必须落到用户可写的位置 ——
否则装到 Program Files 会因为没权限直接崩。两者必须分开。

规则：
  · HERE      = 代码所在目录。只读资源（`md2png.mjs` / `moyu-data.json` / `docs/`）仍从这里取。
  · DATA_DIR  = 可写数据目录。由启动器设环境变量 `MEMO_DATA_DIR` 指定
                （安装版 = `%LOCALAPPDATA%\\memo-bot`）。
                **没设时回退到 HERE** —— 也就是直接跑源码时，行为与以前完全一致。

谁调用它：`store.py` / `settings.py` / `bot.py` / `send_file.py` / `ignore.py` /
`moyu.py` / `config_core.py` / `memo_gui.py` / `ideas.py`。
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def data_dir():
    """可写数据目录（不存在就建）。没设 `MEMO_DATA_DIR` 时 = 代码目录。"""
    d = os.environ.get("MEMO_DATA_DIR") or HERE
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        # 极端情况（盘符不存在 / 只读）——退回代码目录，至少还能跑
        return HERE
    return d


def data_path(name):
    """可写数据目录下的某个文件（只拼路径，不创建文件）。"""
    return os.path.join(data_dir(), name)
