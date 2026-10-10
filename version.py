"""版本号与「有没有新版本」的检测。

这个文件是干嘛的：版本号只写一份（`VERSION` 常量），配置中心页面显示、更新检测比较、
打包脚本都用它。**改了版本号要同步改同目录的 `VERSION` 文件** —— 远端检测读的就是那个文件。

谁调用它：`config-web.py`（页面显示 + 「检查更新」按钮）。

🔴 远端地址走 jsdelivr，不走 raw.githubusercontent.com —— 后者在国内基本连不上（实测 curl exit 35）。
"""
import re
import urllib.error
import urllib.request

VERSION = "0.1.0"
REPO = "phononprismlab/memo-bot"
RAW_URL = "https://cdn.jsdelivr.net/gh/{repo}@main/VERSION"
HOMEPAGE = "https://github.com/{repo}"


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
