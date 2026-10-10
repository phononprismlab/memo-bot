"""版本号与「有没有新版本」的检测。

这个文件是干嘛的：版本号只写一份（`VERSION` 常量），配置中心页面显示、更新检测比较、
打包脚本都用它。**改了版本号要同步改同目录的 `VERSION` 文件** —— 远端检测读的就是那个文件，
`_exe_poc/memo-bot.iss` 也是直接读它（所以安装包名字里的版本号不会跟代码脱节）。

谁调用它：`config-web.py`（页面显示 + 「检查更新」按钮）。

🔴 远端地址走 jsdelivr，不走 raw.githubusercontent.com —— 后者在国内基本连不上（实测 curl exit 35）。

🔴 **检测更新要两个源一起看**（`latest()` 里有详述）：`cdn.jsdelivr.net/...@main/VERSION`
是**分支引用**，jsdelivr 对它缓存 **12 小时**，发版后半天内老客户端查到的还是旧版本号；
`data.jsdelivr.com` 的版本列表接口只缓存 5 分钟，但它按 **tag** 列 —— 所以发版必须打 tag。

关于**下载新版**（`download_url`）：
  · 默认拼 **GitHub Releases 直链** —— 仓库是公开的，零成本，且和源码同源：
      https://github.com/<repo>/releases/download/v<版本>/memo-bot-setup-v<版本>.exe
    所以发版时必须把安装包传成 Release 资产，**tag 用 `v<版本>`、文件名带版本号**（见 asset_name）。
  · 想换成自己的服务器 / 对象存储：设 `MEMO_DOWNLOAD_BASE=https://你的域名/xxx`，
    程序会把同样的文件名拼在后面。换服务器不用改代码。
"""
import json
import os
import re
import urllib.error
import urllib.request

VERSION = "0.1.2"
REPO = "phononprismlab/memo-bot"
RAW_URL = "https://cdn.jsdelivr.net/gh/{repo}@main/VERSION"
# jsdelivr 的「这个仓库有哪些版本」接口。🔴 它按 **tag** 列版本，只缓存 5 分钟。
VERSIONS_API = "https://data.jsdelivr.com/v1/packages/gh/{repo}"
HOMEPAGE = "https://github.com/{repo}"

# 安装包命名（**必须**和 `_exe_poc/memo-bot.iss` 的 OutputBaseFilename 一致，
# 否则「检查更新 → 下载」会 404）。发版时 Release 资产的 tag 用 `v<版本>`。
ASSET_TMPL = "memo-bot-setup-v{ver}.exe"
TAG_TMPL = "v{ver}"

# 自建下载源的基址（可选）。设了就用它，不设就走 GitHub Releases。
DOWNLOAD_BASE_ENV = "MEMO_DOWNLOAD_BASE"


def parse_version(s):
    """'v0.2.1' / '0.2.1' → (0, 2, 1)；认不出返回 ()。"""
    return tuple(int(n) for n in re.findall(r"\d+", str(s or "")))


def compare_version(a, b):
    """a 相对 b：新 → 1，相同 → 0，旧 → -1。

    按**数字**比，不是按字符串 —— '0.10.0' 比 '0.9.0' 新（字符串比会得到反的）。
    缺位补 0：'0.2' 与 '0.2.0' 视为相同。
    """
    pa, pb = parse_version(a), parse_version(b)
    if not pa or not pb:
        return 0
    n = max(len(pa), len(pb))
    pa = pa + (0,) * (n - len(pa))
    pb = pb + (0,) * (n - len(pb))
    return (pa > pb) - (pa < pb)


def _get(url, timeout):
    """取一个 URL 的文本。失败直接抛，由调用方汇总。"""
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _from_versions_api(timeout):
    """从 jsdelivr 的版本列表接口取**最新 tag** —— 只缓存 5 分钟，发版后基本立刻可见。

    返回的是 tag 归一化后的版本串（`v0.1.1` → `0.1.1`）；列表为空返回 None。
    """
    data = json.loads(_get(VERSIONS_API.format(repo=REPO), timeout))
    vs = [str(v.get("version") or "") for v in (data.get("versions") or [])]
    vs = [v for v in vs if parse_version(v)]
    if not vs:
        return None
    best = vs[0]
    for v in vs[1:]:
        # 🔴 不靠接口给的顺序（它现在是新的在前，但没承诺过）—— 自己按数字比一遍。
        if compare_version(v, best) > 0:
            best = v
    return best


def _from_raw(timeout):
    """从 `@main/VERSION` 文件取版本号 —— 兜底源，但**缓存 12 小时**。"""
    text = _get(RAW_URL.format(repo=REPO), timeout).strip()
    m = re.search(r"\d+\.\d+(?:\.\d+)?", text)
    return m.group(0) if m else None


def latest(timeout=8):
    """取远端最新版本号 → (版本串 或 None, 错误说明 或 None)。

    🔴 **为什么非要两个源**：`cdn.jsdelivr.net/...@main/VERSION` 是**分支引用**，
       jsdelivr 给它的响应头是 `Cache-Control: public, max-age=604800, s-maxage=43200`
       —— 边缘节点缓存 **12 小时**。实测发版 3 小时后去取，拿到的还是 `0.1.0`
       （`Age: 11640`）。**加随机查询串也躲不掉**：`?nocache=<随机数>` 命中的是同一份
       缓存，`Age` 一模一样 —— jsdelivr 的缓存键不含 query。
       表现就是用户那句「用 0.1.0 的客户端点检查更新，说已是最新」。

       而 `data.jsdelivr.com` 的版本列表接口是 `max-age=300`（**5 分钟**），
       且它按 **tag** 列 —— 发版打了 tag 就能很快看到。

    两个源都查、取**较大者**：
      · 正常发版（先 tag 后 Release）→ 版本列表接口先到，立刻生效；
      · 万一只改了 `VERSION` 没打 tag → 兜底源迟早也会跟上（只是慢 12 小时）。
    两个都拿不到才返回错误。
    """
    found, errs = [], []
    for name, fn in (("版本列表", _from_versions_api), ("VERSION 文件", _from_raw)):
        try:
            v = fn(timeout)
        except (urllib.error.URLError, OSError, ValueError, KeyError, AttributeError) as e:
            errs.append(f"{name}: {type(e).__name__}: {e}")
            continue
        if v:
            found.append(v)
        else:
            errs.append(f"{name}: 没解析出版本号")
    if not found:
        return None, "；".join(errs)
    best = found[0]
    for v in found[1:]:
        if compare_version(v, best) > 0:
            best = v
    return best, None


# ── 下载：给用户一个能直接点的方式 ────────────────────────────────
def _norm(ver):
    """版本串归一化：'v0.2.0' / ' 0.2.0 ' → '0.2.0'（拼 URL 时别再带 v，模板里有）。"""
    return str(ver or "").strip().lstrip("vV")


def asset_name(ver):
    """安装包文件名（带版本号）。例：memo-bot-setup-v0.2.0.exe"""
    return ASSET_TMPL.format(ver=_norm(ver))


def download_base(base=None):
    """下载基址：显式传参 > 环境变量 > 空（= 走 GitHub Releases）。"""
    return (base if base is not None else os.environ.get(DOWNLOAD_BASE_ENV) or "").strip().rstrip("/")


def download_url(ver, base=None):
    """安装包**直链** —— 页面上那个「⬇ 下载新版」指的就是它。

    默认 GitHub Releases；设了 `MEMO_DOWNLOAD_BASE` 就换成自己的服务器。
    """
    name = asset_name(ver)
    b = download_base(base)
    if b:
        return f"{b}/{name}"
    return f"https://github.com/{REPO}/releases/download/{TAG_TMPL.format(ver=_norm(ver))}/{name}"


def release_url(ver):
    """这一版的 Release 页面（直链被墙 / 想先看更新说明时的兜底）。"""
    return f"https://github.com/{REPO}/releases/tag/{TAG_TMPL.format(ver=_norm(ver))}"


def releases_url():
    """全部 Releases 列表 —— 连不上直链时让用户自己翻。"""
    return f"https://github.com/{REPO}/releases"

