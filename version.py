"""版本号与「有没有新版本」的检测。

这个文件是干嘛的：版本号只写一份（`VERSION` 常量），配置中心页面显示、更新检测比较、
打包脚本都用它。**改了版本号要同步改同目录的 `VERSION` 文件** —— 远端检测读的就是那个文件，
`_exe_poc/memo-bot.iss` 也是直接读它（所以安装包名字里的版本号不会跟代码脱节）。

谁调用它：`config-web.py`（页面显示 + 「检查更新」按钮）。

🔴 远端地址走 jsdelivr，不走 raw.githubusercontent.com —— 后者在国内基本连不上（实测 curl exit 35）。

关于**下载新版**（`download_url`）：
  · 默认拼 **GitHub Releases 直链** —— 仓库是公开的，零成本，且和源码同源：
      https://github.com/<repo>/releases/download/v<版本>/memo-bot-setup-v<版本>.exe
    所以发版时必须把安装包传成 Release 资产，**tag 用 `v<版本>`、文件名带版本号**（见 asset_name）。
  · 想换成自己的服务器 / 对象存储：设 `MEMO_DOWNLOAD_BASE=https://你的域名/xxx`，
    程序会把同样的文件名拼在后面。换服务器不用改代码。
"""
import os
import re
import urllib.error
import urllib.request

VERSION = "0.1.0"
REPO = "phononprismlab/memo-bot"
RAW_URL = "https://cdn.jsdelivr.net/gh/{repo}@main/VERSION"
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


def latest(timeout=8):
    """取远端最新版本号 → (版本串 或 None, 错误说明 或 None)。"""
    url = RAW_URL.format(repo=REPO)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            text = r.read().decode("utf-8", "replace").strip()
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, f"{type(e).__name__}: {e}"
    m = re.search(r"\d+\.\d+(?:\.\d+)?", text)
    if not m:
        return None, "远端内容里没找到版本号"
    return m.group(0), None


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

