"""扩展（自定义指令）管理 —— 把扩展包**装进 / 列出 / 移出** `extensions/local/`。

谁调用它：
  - `memo_gui.py`（桌面配置中心）：「自定义指令」页签
  - `config_core.py`（配置后端）：把已装列表塞进 `full_state()`
  - 命令行自测：`python extmgr.py list`

🔴 和 `extensions/__init__.py` 的分工（别搞混）：
  · `extensions/__init__.py` = **运行时加载** —— bot 启动时把 `local/` 下的扩展
    注册成指令。判「什么算一个扩展包」的规则也写在那里（`scan_dir()`），本文件直接复用。
  · 本文件 = **管理面** —— 配置中心里点「导入 / 移除」时动的是**文件**。
  · 装完必须**重启机器人**才生效（指令是启动时注册的）。

导入支持两种包：
  · `xxx.py`  —— 单文件包，落到 `extensions/local/xxx.py`
  · `xxx.zip` —— 目录包，解到 `extensions/local/xxx/`（可带自己的数据文件）
  两者都必须有 `def register(reg):`，否则拒绝（装进去也是个死文件）。

🔴 探测「这个扩展注册了哪些指令」**必须开子进程**（`probe_items()`）：
   扩展是任意代码，直接在本进程 `exec_module` 的话，一个死循环 / 阻塞调用就会把
   配置中心界面冻死（桌面版是 Tk 主线程）。子进程带超时，坏扩展最多显示一行「加载失败」。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import extensions as _ext          # noqa: E402  —— 复用它的扫描规则 / 加载逻辑

LOCAL_DIR = _ext.LOCAL_DIR
# 移除的扩展挪到这里（不是真删）—— 同名再导入 / 想看回原文件都还有救。
# 🔴 放在 `extensions/` 下而不是 `local/` 下：`local/` 里的东西会被加载器扫，
#    `.removed` 虽然以点开头会被跳过，但放外层更省心。
TRASH_DIR = os.path.join(_ext.HERE, ".removed")

ACCEPT_EXT = (".py", ".zip")
MAX_PY_BYTES = 1 * 1024 * 1024        # 单个 .py 上限
MAX_ZIP_BYTES = 8 * 1024 * 1024       # zip 解压后总上限
MAX_ZIP_FILES = 300
PROBE_TIMEOUT = 8                     # 秒

# 「这个文件会不会注册指令」的静态判断 —— 只用来**提前拒绝**明显不合格的包，
# 真正的加载结果以子进程探测为准。
_REGISTER_RE = re.compile(r"^[ \t]*def[ \t]+register[ \t]*\(", re.M)

# 探测结果缓存：路径 -> (mtime, size, 结果)。扩展没改动就不重复开子进程。
_PROBE_CACHE = {}


# ── 探测（子进程）────────────────────────────────────────────────
# 在子进程里跑的一小段脚本：给一个路径列表，逐个试加载，把注册到的指令打回来。
#
# 🔴 第一件事必须是把 stdout 钉成 **utf-8**。中文 Windows 上子进程默认按
#    `locale.getpreferredencoding()`（cp936）写 stdout，而父进程 `probe_items()`
#    是按 utf-8 解的 —— 中文指令名全变乱码；更糟的是**不同指令名乱码之后会撞成
#    同一个字符串**，于是 `dup` 判定把每个扩展都标成「指令名和别的扩展撞了」
#    （用户报的「四个扩展为什么报错」）。真机复现：`额度`/`查余额`/`看运势`/`体检`
#    各撞一次，fortune 的 4 个指令还被去重成 3 个。
_PROBE_SRC = r'''
import json, sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError, ValueError):
    pass
root, paths = sys.argv[1], json.loads(sys.argv[2])
sys.path.insert(0, root)
import extensions
out = {}
for p in paths:
    reg = extensions.Registry(api=None)
    ok, err = extensions._register_file(p, reg)
    cmds = []
    for c in reg.commands:
        cmds.append({"names": sorted(c["names"]), "help": c["help"]})
    out[p] = {"ok": bool(ok), "error": err, "commands": cmds}
print(json.dumps(out, ensure_ascii=False))
'''


def _no_window():
    """子进程别弹控制台窗口 —— 桌面版是 pythonw 起的，没有控制台时会自己新建一个。"""
    import botctl
    return {"creationflags": botctl._no_window_flags()}


def probe_items(paths):
    """在**一个**子进程里把若干 .py 试加载一遍，返回 `{路径: {ok, error, commands}}`。

    一次调用搞定所有文件（不是每个文件开一个进程）。超时 / 起不来时，
    所有路径都记成失败 —— 界面照常显示，只是那一栏写着原因。

    🔴 编码契约：子进程**保证**按 utf-8 写 stdout（见 `_PROBE_SRC` 开头），
       所以这里固定 `decode("utf-8")`。反过来（子进程按 cp936 写、这里按
       utf-8 解）会让中文指令名变乱码，并伪造出「重名」—— 见 `_PROBE_SRC`。
    """
    paths = [p for p in (paths or []) if p]
    if not paths:
        return {}
    fail = {p: {"ok": False, "error": "", "commands": []} for p in paths}
    try:
        r = subprocess.run(
            [sys.executable, "-c", _PROBE_SRC, HERE, json.dumps(paths)],
            capture_output=True, timeout=PROBE_TIMEOUT, cwd=HERE, **_no_window())
    except subprocess.TimeoutExpired:
        for p in paths:
            fail[p]["error"] = f"加载超时（超过 {PROBE_TIMEOUT} 秒没返回）"
        return fail
    except Exception as e:                                            # noqa: BLE001
        for p in paths:
            fail[p]["error"] = f"{type(e).__name__}: {e}"
        return fail

    data = {}
    lines = r.stdout.decode("utf-8", "replace").strip().splitlines()
    if lines:
        try:
            data = json.loads(lines[-1])
        except ValueError:
            data = {}
    if not isinstance(data, dict):
        data = {}
    tail = r.stderr.decode("utf-8", "replace").strip()[-300:]
    for p in paths:
        got = data.get(p)
        if isinstance(got, dict):
            fail[p] = {"ok": bool(got.get("ok")), "error": got.get("error") or "",
                       "commands": got.get("commands") or []}
        else:
            fail[p]["error"] = tail or "探测子进程没有返回结果"
    return fail


# ── 列出现有扩展 ─────────────────────────────────────────────────
def _tree_size(item):
    """一个扩展包占多少字节（目录包把里面的文件加一起）。"""
    if item["kind"] != "dir":
        try:
            return os.path.getsize(item["path"])
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(item["path"]):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _extra_files(item):
    """目录包里**除 .py 之外**的文件（README / 数据文件…）—— 只在页面上展示用。

    🔴 别把它并进 `files`：那个列表是给 `_py_files()` 拿去加载的，混进 .md 会去 exec 它。
    """
    if item["kind"] != "dir":
        return []
    out = []
    for root, _dirs, fs in os.walk(item["path"]):
        for f in fs:
            rel = os.path.relpath(os.path.join(root, f), item["path"]).replace("\\", "/")
            if not rel.lower().endswith(".py") and "__pycache__" not in rel:
                out.append(rel)
    return sorted(out)[:12]


def _file_stat(path):
    try:
        st = os.stat(path)
        return st.st_mtime, st.st_size
    except OSError:
        return 0.0, 0


def list_items(probe=True):
    """列出 `extensions/local/` 下所有扩展包。

    `probe=True` 时顺带探出每个包注册了哪些指令（子进程 + 缓存）。
    返回 `[{id, kind, name, path, files, size, mtime, commands, helps, error}]`。
    """
    items = []
    for it in _ext.scan_dir(LOCAL_DIR):
        mtime, _ = _file_stat(it["path"])
        if it["kind"] == "dir":
            for p in it["files"]:
                m, _s = _file_stat(p)
                mtime = max(mtime, m)
        items.append({
            "id": ("file:" if it["kind"] == "file" else "dir:") + it["name"],
            "kind": it["kind"],
            "name": it["name"],
            "path": it["path"],
            "files": [os.path.basename(p) for p in it["files"]],
            "extra_files": _extra_files(it),
            "size": _tree_size(it),
            "mtime": mtime,
            "commands": [],
            "helps": [],
            "error": "",
            "dup": [],
        })
    if not probe or not items:
        return items

    # 哪些文件需要探测（改过 / 没缓存过）
    need = []
    by_path = {}
    for it in items:
        for p in _py_files(it):
            by_path[p] = it
            st = _file_stat(p)
            cached = _PROBE_CACHE.get(p)
            if cached and cached[0] == st[0] and cached[1] == st[1]:
                _merge_probe(it, p, cached[2])
            else:
                need.append(p)
    if need:
        res = probe_items(need)
        for p in need:
            info = res.get(p) or {"ok": False, "error": "没探测到", "commands": []}
            st = _file_stat(p)
            _PROBE_CACHE[p] = (st[0], st[1], info)
            if p in by_path:
                _merge_probe(by_path[p], p, info)

    # 同名指令冲突：加载器是「后注册者优先」，被盖住的那个在页面上要看得见
    seen = {}
    for it in items:
        for c in it["commands"]:
            seen.setdefault(c, []).append(it["name"])
    for it in items:
        dup = sorted({c for c in it["commands"] if len(seen.get(c, [])) > 1})
        it["dup"] = dup
    return items


def _py_files(item):
    if item["kind"] == "dir":
        return [os.path.join(item["path"], f) for f in item["files"]]
    return [item["path"]]


def _merge_probe(item, path, info):
    for c in info.get("commands") or []:
        for n in c.get("names") or []:
            if n not in item["commands"]:
                item["commands"].append(n)
        if c.get("help") and c["help"] not in item["helps"]:
            item["helps"].append(c["help"])
    if not info.get("ok") and not item["error"]:
        item["error"] = f"{os.path.basename(path)}：{info.get('error') or '加载失败'}"


def find_item(ident):
    """按 id（`file:名字` / `dir:名字`）找一个扩展包，找不到返回 None。"""
    for it in list_items(probe=False):
        if it["id"] == ident:
            return it
    return None


# ── 导入 ─────────────────────────────────────────────────────────
def _safe_stem(s):
    """把上传的文件名收拾成一个安全的模块 / 目录名（不能跑出 local/）。"""
    s = re.sub(r"[^0-9A-Za-z_\-]", "", s or "")
    s = s.strip("-_")
    if not s:
        s = "ext"
    if s[0].isdigit():
        s = "ext_" + s
    return s[:60]


def _has_register_bytes(blob):
    """字节流里有没有 `def register(`（按 UTF-8 解，编不出的字符替换掉）。"""
    try:
        text = blob.decode("utf-8", "replace")
    except Exception:                                                 # noqa: BLE001
        return False
    return bool(_REGISTER_RE.search(text))


def _zip_entries(zf):
    """校验并规整 zip 条目，返回 `[(相对路径, ZipInfo)]`。

    拒绝：绝对路径、`..`、盘符、符号链接、超量 / 超大。
    单一顶层目录会被剥掉（`我的包/xxx.py` → `xxx.py`），这样打包时多套一层也没关系。
    """
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if not infos:
        raise ValueError("压缩包是空的")
    norm = []
    for i in infos:
        raw = i.filename.replace("\\", "/")
        parts = [p for p in raw.split("/") if p not in ("", ".")]
        if raw.startswith("/") or not parts or ".." in parts or ":" in parts[0]:
            raise ValueError(f"压缩包里有个不安全的路径：{i.filename}")
        if (i.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError(f"压缩包里有个符号链接，拒绝解压：{i.filename}")
        norm.append((parts, i))
    tops = {p[0] for p, _ in norm}
    strip = len(tops) == 1 and all(len(p) > 1 for p, _ in norm)
    return [("/".join(p[1:] if strip else p), i) for p, i in norm]


def _extract_zip(zf, dest):
    """把 zip 解到 dest —— 自己逐条写文件，不用 `extractall`（彻底断掉路径穿越）。"""
    entries = _zip_entries(zf)
    if len(entries) > MAX_ZIP_FILES:
        raise ValueError(f"压缩包里文件太多（{len(entries)} 个，上限 {MAX_ZIP_FILES}）")
    total = sum(i.file_size for _, i in entries)
    if total > MAX_ZIP_BYTES:
        raise ValueError(f"解压后太大（{total // 1024 // 1024}MB，上限 "
                         f"{MAX_ZIP_BYTES // 1024 // 1024}MB）")
    root = os.path.abspath(dest)
    os.makedirs(root, exist_ok=True)
    for rel, info in entries:
        target = os.path.abspath(os.path.join(root, *rel.split("/")))
        if os.path.commonpath([root, target]) != root:
            raise ValueError(f"压缩包里有个不安全的路径：{info.filename}")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as fdst:
            shutil.copyfileobj(src, fdst)
    return entries


def import_bytes(filename, blob, overwrite=False):
    """把上传上来的扩展包内容装进 `extensions/local/`。

    `filename` 只用来定名字与判类型（`.py` / `.zip`），**不信任其中的路径**。
    返回 `(ok, msg, info)`；`info` 里带 `conflict=True` 表示「同名已存在」。
    """
    ext = os.path.splitext(filename or "")[1].lower()
    stem = _safe_stem(os.path.splitext(os.path.basename(filename or ""))[0])
    if ext not in ACCEPT_EXT:
        return False, "只支持 .py（单文件指令）或 .zip（指令包）", {}
    if not blob:
        return False, "文件是空的", {}
    os.makedirs(LOCAL_DIR, exist_ok=True)

    if ext == ".py":
        if len(blob) > MAX_PY_BYTES:
            return False, f"这个 .py 太大了（{len(blob) // 1024}KB，上限 1MB）", {}
        if not _has_register_bytes(blob):
            return False, ("这个 .py 里没有 `def register(reg):` —— "
                           "扩展必须暴露 register() 才会注册指令，装进去也用不了"), {}
        dest = os.path.join(LOCAL_DIR, stem + ".py")
        if os.path.exists(dest) and not overwrite:
            return False, f"已经有一个叫「{stem}」的扩展了", {"conflict": True, "name": stem}
        try:
            with open(dest, "wb") as f:
                f.write(blob)
        except OSError as e:
            return False, f"写入失败：{e}", {}
        return True, f"已装好「{stem}」（单文件扩展）", _item_info(dest, stem, "file")

    # .zip
    if len(blob) > MAX_ZIP_BYTES:
        return False, f"这个 zip 太大了（{len(blob) // 1024 // 1024}MB）", {}
    dest = os.path.join(LOCAL_DIR, stem)
    if os.path.exists(dest) and not overwrite:
        return False, f"已经有一个叫「{stem}」的扩展了", {"conflict": True, "name": stem}
    tmp = os.path.join(LOCAL_DIR, ".tmp-" + stem)
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        with zipfile.ZipFile(_BytesReader(blob)) as zf:
            entries = _extract_zip(zf, tmp)
            pys = [rel for rel, _i in entries if rel.lower().endswith(".py")]
            if not pys:
                raise ValueError("压缩包里没有 .py 文件")
            if not any(_has_register_bytes(zf.read(i)) for rel, i in entries
                       if rel.lower().endswith(".py")):
                raise ValueError("压缩包里的 .py 都没有 `def register(reg):` —— "
                                 "扩展必须暴露 register() 才会注册指令")
    except (zipfile.BadZipFile, ValueError, OSError) as e:
        shutil.rmtree(tmp, ignore_errors=True)
        return False, f"这个 zip 装不了：{e}", {}
    except Exception as e:                                            # noqa: BLE001
        shutil.rmtree(tmp, ignore_errors=True)
        return False, f"这个 zip 装不了：{type(e).__name__}: {e}", {}
    shutil.rmtree(dest, ignore_errors=True)      # overwrite 时先清掉旧的
    try:
        os.replace(tmp, dest)
    except OSError:
        shutil.move(tmp, dest)
    return True, f"已装好「{stem}」（{len(pys)} 个 .py 的指令包）", _item_info(dest, stem, "dir")


class _BytesReader:
    """给 `zipfile.ZipFile` 用的最小文件对象 —— 上传上来的是内存里的 bytes。

    🔴 `seekable()` / `readable()` 必须实现：zipfile 会先问一句再决定怎么读，
    少了它直接抛 `AttributeError`（踩过）。
    """

    def __init__(self, blob):
        self._blob = blob
        self._pos = 0

    def read(self, n=-1):
        if n is None or n < 0:
            out = self._blob[self._pos:]
            self._pos = len(self._blob)
            return out
        out = self._blob[self._pos:self._pos + n]
        self._pos += len(out)
        return out

    def seek(self, off, whence=0):
        if whence == 0:
            self._pos = off
        elif whence == 1:
            self._pos += off
        else:
            self._pos = len(self._blob) + off
        return self._pos

    def tell(self):
        return self._pos

    def seekable(self):
        return True

    def readable(self):
        return True

    def writable(self):
        return False


def import_file(src_path, overwrite=False):
    """从本机文件导入（桌面版「导入扩展…」走这条）。返回 `(ok, msg, info)`。"""
    src_path = os.path.abspath(src_path or "")
    if not os.path.isfile(src_path):
        return False, "找不到这个文件", {}
    try:
        with open(src_path, "rb") as f:
            blob = f.read(MAX_ZIP_BYTES + 1)
    except OSError as e:
        return False, f"读不了这个文件：{e}", {}
    return import_bytes(os.path.basename(src_path), blob, overwrite=overwrite)


def _item_info(path, name, kind):
    """导入成功后回一份「这个包现在是什么样」的信息，页面上立刻能显示。"""
    for it in list_items(probe=True):
        if it["name"] == name and it["kind"] == kind:
            return it
    return {"name": name, "kind": kind, "path": path, "commands": [], "helps": [],
            "error": "", "dup": [], "files": [], "extra_files": []}


# ── 移除 ─────────────────────────────────────────────────────────
def remove(ident):
    """把一个扩展包**挪到** `extensions/.removed/`（不是真删，还能捞回来）。

    返回 `(ok, msg)`。
    """
    it = find_item(ident)
    if not it:
        return False, "没找到这个扩展（可能已经被移走了）"
    os.makedirs(TRASH_DIR, exist_ok=True)
    suffix = ".py" if it["kind"] == "file" else ""
    dst = os.path.join(TRASH_DIR, f"{time.strftime('%Y%m%d-%H%M%S')}-{it['name']}{suffix}")
    try:
        shutil.move(it["path"], dst)
    except OSError as e:
        return False, f"移除失败：{e}"
    _PROBE_CACHE.pop(it["path"], None)
    return True, f"已移除「{it['name']}」（挪进了 extensions/.removed/，没真删）"


# ── 命令行自测 ───────────────────────────────────────────────────
def _main(argv):
    cmd = (argv[1] if len(argv) > 1 else "list").lower()
    if cmd == "list":
        for it in list_items():
            cmds = "、".join(it["commands"]) or "（没注册指令）"
            flag = f"  ⚠ 加载失败：{it['error']}" if it["error"] else ""
            print(f"[{it['kind']}] {it['name']}  →  {cmds}{flag}")
        return 0
    if cmd == "import" and len(argv) > 2:
        ok, msg, _info = import_file(argv[2], overwrite="--force" in argv)
        print(("✅ " if ok else "❌ ") + msg)
        return 0 if ok else 1
    if cmd == "remove" and len(argv) > 2:
        ok, msg = remove(argv[2])
        print(("✅ " if ok else "❌ ") + msg)
        return 0 if ok else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass
    sys.exit(_main(sys.argv))
