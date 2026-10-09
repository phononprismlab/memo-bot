"""扩展加载器 —— 公开仓库的「可扩展性」入口。

设计（让公开项目保持干净，同时允许任意自定义指令）：

- 公开仓库只带 `example.py`（模板，不注册任何指令）。
- **你自己的自定义指令放 `extensions/local/`**，该目录已在 `.gitignore` 里，
  **不会进公开仓库**（本地 bot = 公开代码 + 你的私有扩展）。
- 每个扩展模块提供一个 `register(reg)` 函数，在里面调用 `reg.add(...)` 注册指令。
- 加载失败只打印 warning，**不会拖垮 bot 启动**。

指令处理器签名：`handler(text, mentioned) -> str | list[str] | None`
  - 返回 `None`  → 不处理（继续往下走）
  - 返回 `str`   → 回一条消息
  - 返回 `list`  → 依次回多条（msg_seq 递增）
  - 处理器是**同步函数**；框架用线程执行，所以里面可以做阻塞操作（读硬件 / 调外部命令）。

可用上下文：`reg.api`（默认含 `search_files(query, limit)` 与 `SEARCH_ROOT`）。
"""
import importlib.util
import os


HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_DIR = os.path.join(HERE, "local")     # 私有扩展目录（gitignore）


class Registry:
    """扩展注册表。扩展模块通过 `register(reg)` 拿到它。"""

    def __init__(self, api=None):
        self.api = api
        self.commands = []          # [{"names": set[str], "handler": fn, "help": str}]

    def add(self, names, handler, help=""):
        """注册一个**精确匹配**的指令。

        names: str 或 list[str]（指令词与别名）。
        handler: `def handler(text, mentioned) -> str | list[str] | None`。
        help: 帮助菜单里显示的一行（留空则不出现在菜单）。
        """
        if isinstance(names, str):
            names = [names]
        names = {n.strip() for n in names if n and n.strip()}
        if not names:
            raise ValueError("reg.add() 需要至少一个非空指令词")
        if not callable(handler):
            raise TypeError("reg.add() 的 handler 必须可调用")
        self.commands.append({"names": names, "handler": handler, "help": help})
        return handler

    def match(self, text):
        """精确匹配指令词 → handler；没命中返回 None。

        逆序查找：**后注册的优先**（`extensions/local/` 加载在公开目录之后，
        所以私有扩展可以覆盖公开扩展的同名指令）。
        """
        for c in reversed(self.commands):
            if text in c["names"]:
                return c["handler"]
        return None

    def help_lines(self):
        """帮助菜单里要显示的扩展指令行（按注册顺序）。"""
        return [c["help"] for c in self.commands if c["help"]]


def _iter_modules(d):
    if not os.path.isdir(d):
        return []
    return [
        os.path.join(d, n)
        for n in sorted(os.listdir(d))
        if n.endswith(".py") and not n.startswith("_")
    ]


def _register_file(path, reg):
    """把单个 .py 扩展加载并调用其 register(reg)。失败只告警。"""
    modname = "memoext_" + os.path.splitext(os.path.basename(path))[0]
    try:
        spec = importlib.util.spec_from_file_location(modname, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        print(f"[warn] 加载扩展 {os.path.basename(path)} 失败：{type(e).__name__}: {e}", flush=True)
        return
    fn = getattr(mod, "register", None)
    if not callable(fn):
        return
    try:
        fn(reg)
    except Exception as e:
        print(f"[warn] 扩展 {os.path.basename(path)} 的 register() 出错："
              f"{type(e).__name__}: {e}", flush=True)


def load(api=None):
    """加载 `extensions/*.py` 与 `extensions/local/*.py`，返回 Registry。

    顺序：先公开目录、后私有目录；精确匹配时**后注册者优先**，
    所以 `extensions/local/` 里的扩展可以覆盖公开扩展的同名指令。
    """
    reg = Registry(api=api)
    for p in _iter_modules(HERE):
        _register_file(p, reg)
    for p in _iter_modules(LOCAL_DIR):
        _register_file(p, reg)
    return reg
