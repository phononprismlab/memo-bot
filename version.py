"""版本号与「有没有新版本」的检测。

这个文件是干嘛的：版本号只写一份（`VERSION` 常量），配置中心页面显示、更新检测比较、
打包脚本都用它。**改了版本号要同步改同目录的 `VERSION` 文件** —— 远端检测读的就是那个文件，
    `packaging/memo-bot.iss` 也是直接读它（所以安装包名字里的版本号不会跟代码脱节）。

谁调用它：`memo_gui.py`（桌面版「⑩ 版本」页的版本号 + 「检查更新」按钮）、
         `config_core.py`（把版本号塞进 `full_state()`）。

🔴 远端地址走 jsdelivr，不走 raw.githubusercontent.com —— 后者在国内基本连不上（实测 curl exit 35）。

🔴 **检测更新要多个源一起看**（`latest()` 里有详述）：
  · `api.github.com/repos/<repo>/releases/latest` —— **最实时**（`max-age=60`），
    但它对空 `User-Agent` 返回 403、未认证配额 60 次/小时/IP，国内可达性也一般；
  · `data.jsdelivr.com` 的版本列表 —— HTTP 缓存 5 分钟，但它从 GitHub 同步 **tag**
    有自己的节奏（本仓库实测**索引延迟 ≈10 分钟**）；
  · `cdn.jsdelivr.net/...@main/VERSION` —— 分支引用，边缘缓存 **12 小时**，只能兜底。
  三个源都查、取**较大者**，任一挂了都不影响。

关于**下载新版**（`download_url`）：
  · 默认拼 **GitHub Releases 直链** —— 仓库是公开的，零成本，且和源码同源：
      https://github.com/<repo>/releases/download/v<版本>/memo-bot-setup-v<版本>.exe
    所以发版时必须把安装包传成 Release 资产，**tag 用 `v<版本>`、文件名带版本号**（见 asset_name）。
  · 🔴 `github.com` 的下载直链在国内经常打不开 —— 想换自己的服务器 / 对象存储，
    设 `MEMO_DOWNLOAD_BASE=https://你的域名/xxx`，程序会把同样的文件名拼在后面。换服务器不用改代码。
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

VERSION = "0.3.1"
REPO = "phononprismlab/memo-bot"
RAW_URL = "https://cdn.jsdelivr.net/gh/{repo}@main/VERSION"
# jsdelivr 的「这个仓库有哪些版本」接口。🔴 它按 **tag** 列版本，HTTP 缓存 5 分钟，
#    但「jsdelivr 什么时候知道新 tag」是另一回事（实测 ≈10 分钟）。
VERSIONS_API = "https://data.jsdelivr.com/v1/packages/gh/{repo}"
# GitHub Releases API —— 最实时的源（响应头 max-age=60）。
RELEASES_API = "https://api.github.com/repos/{repo}/releases/latest"
HOMEPAGE = "https://github.com/{repo}"

# 安装包命名（**必须**和 `packaging/memo-bot.iss` 的 OutputBaseFilename 一致，
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


# 🔴 绕开系统 / 环境变量里的代理（和 send_file.py、media.py 同一处理）：
#    机器上留个「指向本机但已关掉」的代理时，`urlopen` 会一路吃
#    `[WinError 10061] 积极拒绝`，检查更新就永远失败。更新源本来就直连。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get(url, timeout):
    """取一个 URL 的文本。失败直接抛，由调用方汇总。

    🔴 必须带 `User-Agent` —— GitHub API 对空 UA 直接返回 403。
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": "memo-bot-update-check",
        "Accept": "application/json, text/plain, */*",
    })
    with _OPENER.open(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _from_release_api(timeout):
    """从 GitHub Releases API 取最新 Release 的 tag —— **最实时的源**（缓存只有 60 秒）。

    ⚠️ 未认证配额 60 次/小时/IP：用户手动点「检查更新」绰绰有余，但**别拿它轮询**。
    """
    data = json.loads(_get(RELEASES_API.format(repo=REPO), timeout))
    tag = str(data.get("tag_name") or "")
    m = re.search(r"\d+\.\d+(?:\.\d+)?", tag)
    return m.group(0) if m else None


def _from_versions_api(timeout):
    """从 jsdelivr 的版本列表接口取**最新 tag**。

    ⚠️ 它按 tag 列，而 jsdelivr 从 GitHub 同步 tag 有延迟（本仓库实测 ≈10 分钟），
       所以发版后头十几分钟它可能还是旧值 —— 这也是要留第三个源的原因。
    返回 tag 归一化后的版本串（`v0.1.1` → `0.1.1`）；列表为空返回 None。
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


# 查版本的源，**按新鲜度排序**（先试最实时的）。
SOURCES = (
    ("GitHub Releases", _from_release_api),
    ("jsdelivr 版本列表", _from_versions_api),
    ("jsdelivr 文件", _from_raw),
)

# 每个源试几次。网络抖动（连接被重置之类）第一次失败、第二次往往就通了。
ATTEMPTS = 2
# 单次请求超时。三个源 × 两次 = 最多 6 次，别让用户对着「检查中…」干等。
PER_TIMEOUT = 5
# 总时长上限 —— 超了就不再往下试，宁可报错也别卡住页面。
DEADLINE = 15

# WinError / errno → 人话。用户看不懂 `[WinError 10054] 远程主机强迫关闭了一个现有的连接`，
# 更不知道该干什么；给一句能照着做的提示。
_NET_HINTS = {
    10054: "连接被重置（网络环境可能有限制，或代理不稳定）",
    10060: "连接超时",
    10061: "连接被拒绝（如果开了代理，确认代理正在运行）",
    11001: "域名解析失败（DNS 问题）",
    104: "连接被重置",
    110: "连接超时",
    111: "连接被拒绝",
}


def _why(e):
    """把底层网络异常翻译成一句人话。"""
    r = getattr(e, "reason", e)
    code = getattr(r, "winerror", None) or getattr(r, "errno", None)
    if code in _NET_HINTS:
        return _NET_HINTS[code]
    txt = str(r)
    if "timed out" in txt:
        return "连接超时"
    return f"{type(r).__name__}"


def latest(timeout=PER_TIMEOUT, attempts=ATTEMPTS, deadline=DEADLINE):
    """取远端最新版本号 → (版本串 或 None, 错误说明 或 None)。

    🔴 **为什么要多个源**（2026-10-10 两次真机事故）：
      1. 最早只查 `cdn.jsdelivr.net/...@main/VERSION`。它是**分支引用**，
         jsdelivr 给它的响应头是 `Cache-Control: public, max-age=604800, s-maxage=43200`
         —— 边缘缓存 **12 小时**。发版 3 小时后去取，拿到的还是 `0.1.0`（`Age: 11640`），
         于是 0.1.0 的客户端点「检查更新」被判成「已是最新」。
         🔴 **加随机查询串也躲不掉**：`?nocache=<随机数>` 命中同一份缓存，`Age` 一模一样
         —— jsdelivr 的缓存键不含 query。别再想着用 cache-busting 绕。
      2. 换成 jsdelivr 的版本列表后，又发现它**索引新 tag 有 ≈10 分钟延迟**
         （`max-age=300` 说的是 HTTP 缓存，不是「jsdelivr 什么时候知道新 tag」）。
      3. 还有一类事故跟缓存无关：**单个源 + 不重试**，网络抖一下（比如
         `URLError: [WinError 10054] 连接被重置`）整个检查就挂了，而且原始异常
         直接甩到页面上，用户看不懂。

    所以：**多个源 + 每个源重试 + 取较大者 + 人话报错**，任一源挂了都不影响。
    """
    t0 = time.monotonic()
    found, errs = [], []
    for name, fn in SOURCES:
        got, why = None, "超时跳过"
        for _ in range(attempts):
            if time.monotonic() - t0 > deadline:
                break
            try:
                got = fn(timeout)
            except (urllib.error.URLError, OSError, ValueError, KeyError,
                    AttributeError) as e:
                got, why = None, _why(e)
            else:
                why = None if got else "没解析出版本号"
                if got:
                    break
        if got:
            found.append(got)
        else:
            errs.append(f"{name}：{why}")
    if not found:
        # 三个源报的原因通常一样（同一个网络问题），去重后再说，别糊用户一屏
        reasons = []
        for e in errs:
            r = e.split("：", 1)[1] if "：" in e else e
            if r not in reasons:
                reasons.append(r)
        return None, "连不上更新服务器：" + "、".join(reasons)
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

