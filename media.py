"""收图：把消息里的图片下载到本地、给抽屉页当普通文件引用。

这个文件是干嘛的：QQ 消息里的图片是**带时效的远程 URL**（`rkey` 会过期，官方也没写
多久），所以「收到就落盘」是唯一可靠做法。落盘之后：

  - `store.py` 只存**相对路径数组**（`images/202610/ab12cd34ef567890.jpg`），
    不存二进制、不存 base64 —— 数据库轻、导出也干净；
  - `brain-drawer.py` 用 `GET /img/<yyyyMM>/<文件名>` 把它当本地文件读出来给 `<img>`；
  - `export.py` 打包 zip 时直接按路径把文件塞进去。

落盘位置：`<数据目录>/images/<yyyyMM>/<sha16>.<ext>`
  用**内容哈希**命名 —— 同图重复发只占一份；扩展名从 `content_type` 来
  （拿不到就嗅探文件头），因为 QQ 的 URL 路径里往往没有扩展名。

谁调用它：`bot.py`（收到消息时下载）、`brain-drawer.py`（读占用 / 吐文件）、
`export.py`（打包）、`config-web.py`（显示占用）。

🔴 三条实现红线（都是实测踩出来的）：
  1. **必须绕开代理** —— 图片域 `multimedia.nt.qq.com.cn` 国内直连可达、
     也没有防盗链，但走系统代理会 **502**。所以显式 `ProxyHandler({})`。
  2. **必须写临时文件再 `os.replace`** —— 下载中断留半个文件的话，
     下次同图命中哈希会当成「已经有了」，那张图就永远是坏的。
  3. **路径必须能反查** —— 对外只吐相对路径，读的时候一律过 `resolve_path()`
     做**目录包含校验**，防 `../` 穿越（见 brain-drawer 的 `/img/` 路由）。

本模块**不读设置**（开关判断在调用方 `bot.py`），只做「给我 URL，我还你相对路径」。
"""
import hashlib
import os
import re
import urllib.error
import urllib.request
from datetime import datetime

from paths import data_dir

# 只收这几种图。别的一概跳过 —— 语音、视频、文件都不在范围内。
MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

# 文件头 → 扩展名。`content_type` 拿不到时的兜底。
_MAGIC = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
)

MAX_BYTES = 20 * 1024 * 1024     # 单张 20MB —— 超过的丢掉，不设置「大图警告」
MAX_PER_MSG = 9                  # 一条消息最多收几张（再多多半是刷屏 / 表情包大战）
TIMEOUT = 10                     # 秒。图片域很快，10s 拿不到就是拿不到了

_SUBDIR_RE = re.compile(r"^\d{6}$")                 # yyyyMM
# 🔴 后缀必须**穷举**（和 store._IMG_RE 同一份白名单），不能写成 `[a-z]{3,4}` ——
#    那样 `.svg` / `.exe` 这种也长得合规矩，等于把「白名单」写成了「不用太长就行」。
#    `/img/<yyyyMM>/<name>` 是照这个正则放行的，宽一位就多一条摸文件的路。
_NAME_RE = re.compile(r"^[0-9a-f]{16}\.(jpg|png|gif|webp)$")  # sha16 + 白名单后缀
_EXT_OK = set(MIME_EXT.values())

# 名字与目录都白名单化 —— 对外接口只认这个形状，别的直接拒。
# `bytes` / `str` 的兼容判断在 safe_name / resolve_path 里做。


def images_root():
    """图片总目录（`<数据目录>/images`）。只拼路径，不创建。"""
    return os.path.join(data_dir(), "images")


def month_dir(now=None):
    """当月目录名（`yyyyMM`）。按月分，单目录不会堆到几万个文件。"""
    return (now or datetime.now()).strftime("%Y%m")


def sniff_ext(head):
    """从文件头猜扩展名（拿不到返回空串）。"""
    for magic, ext in _MAGIC:
        if head.startswith(magic):
            return ext
    # WebP = RIFF....WEBP
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    return ""


def pick_ext(content_type, head=b"", url=""):
    """决定存成什么扩展名：`content_type` → 文件头 → URL 路径 → 默认 .jpg。

    QQ 的图片 URL 一般长 `.../xxx?rkey=...`，路径里**没有**扩展名，
    所以 URL 只当最后的兜底。
    """
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in MIME_EXT:
        return MIME_EXT[ct]
    ext = sniff_ext(head)
    if ext:
        return ext
    path = (url or "").split("?")[0].lower()
    for e in _EXT_OK:
        if path.endswith(e):
            return e
    return ".jpg"


def content_type_of(blob):
    """反查 MIME（`/img/` 路由要回 `Content-Type`）。

    🔴 按**文件头**判，不信扩展名 —— 扩展名是我们自己起的，而文件内容才是真的。
    """
    ext = sniff_ext(blob[:16]) if blob else ""
    for mt, e in MIME_EXT.items():
        if e == ext:
            return mt
    return "application/octet-stream"


def _fetch(url):
    """下载一个 URL → (bytes, content_type)。失败返回 (None, 原因)。"""
    req = urllib.request.Request(url, headers={
        # 带个 UA：有些 CDN 对空 UA 会 403
        "User-Agent": "Mozilla/5.0 (memo-bot)",
    })
    # 🔴 显式清空代理链。图片域走代理必 502（实测），直连才通。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=TIMEOUT) as resp:
        # 多读 1 字节：正好 20MB 的图不该被误杀
        blob = resp.read(MAX_BYTES + 1)
        return blob, (resp.headers.get("Content-Type") or "")


def download(url):
    """下载单张图 → (bytes, content_type)；不该收 / 收不动就 (None, 原因)。

    「原因」只给日志看，不往群里发（收图是静默行为）。
    """
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return None, "不是 http(s) 地址"
    try:
        blob, ct = _fetch(url)
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:                       # noqa: BLE001  （超时 / DNS / 断连都算）
        return None, f"{type(e).__name__}: {e}"
    if not blob:
        return None, "空响应"
    if len(blob) > MAX_BYTES:
        return None, f"超过 {MAX_BYTES // 1024 // 1024}MB"
    ct = (ct or "").split(";")[0].strip().lower()
    if ct and ct not in MIME_EXT:
        # 声明的类型不是图 —— 但**别急着扔**：QQ 偶尔把 webp 报成 octet-stream。
        # 用文件头再判一次，两个都认不出才丢。
        if not sniff_ext(blob[:16]):
            return None, f"不是图片（{ct}）"
    return blob, ct


def save_image(blob, url="", content_type="", now=None):
    """把已下载的图片落盘 → 相对路径（失败返回空串）。

    相对路径形如 `images/202610/ab12cd34ef567890.jpg` —— 存进 `memos.images` 的就是它。
    """
    if not blob:
        return ""
    dig = hashlib.sha256(blob).hexdigest()[:16]
    ext = pick_ext(content_type, blob[:16], url)
    rel = f"images/{month_dir(now)}/{dig}{ext}"
    target = os.path.join(data_dir(), rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".part"
    try:
        with open(tmp, "wb") as fh:
            fh.write(blob)
        # 🔴 临时文件 + 原子替换：半截文件被当成「已有」是收图最恶心的坑
        os.replace(tmp, target)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return ""
    return rel


def collect(urls, now=None):
    """批量收图（去重、限量）→ (相对路径数组, 失败原因数组)。

    `urls` 直接来自 `message.attachments` 里的 `url` 字段（见 bot.py）。
    """
    out, errs = [], []
    seen = set()
    for u in list(urls or [])[:MAX_PER_MSG]:
        u = (u or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        blob, info = download(u)
        if blob is None:
            errs.append(str(info))
            continue
        rel = save_image(blob, url=u, content_type=info, now=now)
        if rel:
            out.append(rel)
        else:
            errs.append("写盘失败")
    return out, errs


def list_files():
    """遍历本地已存的图 → [{rel, size, mtime}]。

    🔴 以**磁盘**为准而不是数据库：文件可能是用户手动丢进去的（或数据库删行但
    文件还在），占用统计要反映磁盘实况。
    """
    root = images_root()
    out = []
    if not os.path.isdir(root):
        return out
    for mon in sorted(os.listdir(root)):
        mdir = os.path.join(root, mon)
        if not os.path.isdir(mdir):
            continue
        for name in sorted(os.listdir(mdir)):
            fp = os.path.join(mdir, name)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            if not os.path.isfile(fp):
                continue
            out.append({
                "rel": f"images/{mon}/{name}",
                "size": st.st_size,
                "mtime": st.st_mtime,
            })
    return out


def usage():
    """占用概况 → {count, bytes, mb, gb}。配置中心那一行显示的就是它。"""
    files = list_files()
    total = sum(f["size"] for f in files)
    return {
        "count": len(files),
        "bytes": total,
        "mb": round(total / 1024 / 1024, 1),
        "gb": round(total / 1024 / 1024 / 1024, 2),
    }


# ── 路径安全 ────────────────────────────────────────────────────
def safe_name(name):
    """文件名是不是我们起的那个形状（`sha16.ext`）。"""
    return bool(isinstance(name, str) and _NAME_RE.match(name))


def resolve_path(mon, name):
    """把 `/img/<yyyyMM>/<name>` 解析成**受控范围内的绝对路径**；不合法返回空串。

    🔴 两道锁：目录名 / 文件名都必须匹配白名单正则，且解析结果（`realpath`，
    软链接也展开）必须仍在 `images/` 里 —— 光过滤 `..` 不够，
    符号链接 / Windows 的 8.3 短名都能绕出去。
    """
    if not isinstance(mon, str) or not _SUBDIR_RE.match(mon):
        return ""
    if not safe_name(name):
        return ""
    root = os.path.realpath(images_root())
    fp = os.path.realpath(os.path.join(root, mon, name))
    # 🔴 必须 `root + sep` 前缀比 —— 直接 startswith(root) 会放进 `images-evil/`
    if not fp.startswith(root + os.sep):
        return ""
    return fp if os.path.isfile(fp) else ""


def human(nbytes):
    """字节数说成人话（配置中心用）。"""
    n = float(nbytes or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"
