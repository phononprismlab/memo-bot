"""摸鱼bot 的**收端**（常驻进程）：监听 QQ 群 / 私聊消息 → 识别指令 → 落库 → 回执。

这个文件是干嘛的：
  - 常驻跑着（由计划任务 `MoyuBot` 托管），用 QQ 官方机器人 WebSocket 长连接收消息；
  - 群消息与私聊消息都进 `route_message()`，依次过：
    忽略表 → 「设置」指令 → 扩展指令 → 内置指令（待办 / 查待办 / 查灵感 / 你好 /
    发文件 / 摸鱼 / 回旋镖 / 导出 / 帮助）→ 都没命中就兜底**静默收作灵感**；
  - 数据落 `store.py`（SQLite）；发文件走 `send_file.py`（纯 HTTP，不占长连接）；
  - 配置读两处：`settings.py`（本机设置，可网页改）+ `.env`（凭据 / 路径）。
  - 上线播报：**第一次**连上发「功能介绍」（`intro_msg`），之后每次只发「启动播报」
    （`startup_msg`）；发过没发过看数据目录的 `onboarded.json`。
    目标 = 用户在配置中心勾选的会话（`.env` 的 `QQ_PUSH_TARGETS`，
    `group:<id>;c2c:<id>`，群和私聊都可多个、平级，见 `push.py`）。

跑起来的入口：`python bot.py`（真实启动，需 `.env` 里的 QQ 凭据）。

本地运行，走 QQ 官方机器人 WebSocket 模式（无需公网 IP）。

三种运行方式：
  python bot.py --self-test   本地模拟群消息，验证识别+落库逻辑（无需 QQ 凭据）
  python bot.py --list        打印最近记录的待办（本地读取用）
  python bot.py              真实启动 bot（需配置 .env 里的 QQ 凭据）

三条入口（群与私聊共用同一套指令与识别逻辑）：
  - on_group_at_message_create 群内 @机器人（宽松指令，含「发」「发送」等别名）
  - on_group_message_create    群内全量消息（需群设置「消息范围=全部消息」；只认明确指令）
    注意：botpy 1.2.1 未注册该事件，由 install_group_message_parser() 运行时补上。
  - on_c2c_message_create      私聊消息（QQ 开放平台需开启「单聊」；无需补解析器，
    botpy 1.2.1 已注册 C2C_MESSAGE_CREATE）
"""
import asyncio
import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from types import SimpleNamespace

# 🔴 标准输出 / 错误强制 UTF-8。中文 Windows 上默认是 GBK，一旦日志或回执文案里
#    出现 GBK 编不出的字符（emoji / ⏻ / ⚙ 等），print 会抛 UnicodeEncodeError。
#    本进程是常驻的（计划任务 + pythonw 无窗口），崩了用户完全看不到 —— 必须防住。
#    errors="replace" 兜底：编不出的字符打成 ?，不让进程挂掉。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

from store import (
    init,
    add_memo,
    list_memos,
    list_ideas,
    idea_stats,
    KIND_MEMO,
    KIND_IDEA,
    STATUS_OPEN,
    STATUS_DONE,
    STATUS_DECIDED,
    pick_boomerang,
    count_boomerang,
)
import moyu
import extensions as _ext
import ignore as _ignore
import settings as _settings
import export as _export
import push as _push
import media as _media

# 🔴 进程存活判断**复用 botctl 的那一份**，本文件不再自带：早先 bot.py / botctl.py 各存
#    一份，两边的「显式 encoding + ValueError 兜底」得手工同步，改一处忘一处就会在
#    重启流程里崩（bot_stop 成功 → bot_start 崩 → 机器人被停掉却起不来）。
from botctl import _pid_alive  # noqa: E402

import single_lock as _single_lock  # noqa: E402  （OS 级单实例锁）

from paths import data_path

HERE = os.path.dirname(os.path.abspath(__file__))
# 🔴 这两个是**可写数据** —— 安装版由 MEMO_DATA_DIR 指到用户目录（见 paths.py）
LAST_MSG_FILE = data_path("last_group_msg.json")
PID_FILE = data_path("bot.pid")


_INSTANCE_LOCK = _single_lock.SingleInstance("bot")


def acquire_pid_lock():
    """单实例锁：已有活着的实例就返回 False。

    🔴 为什么需要：计划任务只保证「同一个任务不并发」，手动执行
    `python bot.py` 会起第二个进程 → 两条 WS 连接抢事件、消息随机丢。
    🔴 **判据是 OS 级命名互斥体**（`single_lock`），不是「读 pid → 判活 → 写 pid」
       那三步 —— 那三步不是原子的，两个进程同时启动会**都通过检查**，
       结果跑起两个（审查报告点出的「单实例锁存在并发窗口」）。
       pid 文件仍写，但它现在只当**信息**（`botctl` 用它找到进程去停 / 判活），
       不再承担「锁」的职责。
    """
    if not _INSTANCE_LOCK.acquire():
        old = bot_pid()
        print(f"[拒绝启动] 已有实例在跑"
              f"{f'（PID {old}）' if old else ''} —— 同一时刻只能跑一个 bot。",
              flush=True)
        return False
    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError as e:
        print(f"[warn] 写 PID 文件失败（不阻塞启动）: {e}", flush=True)
    return True


def bot_pid():
    """`bot.pid` 里记的进程号（0 = 没记）。仅供展示 / 排查，不参与判活。"""
    try:
        with open(PID_FILE, encoding="utf-8") as f:
            return int((f.read() or "0").strip() or 0)
    except (OSError, ValueError):
        return 0


def release_pid_lock():
    try:
        with open(PID_FILE, encoding="utf-8") as f:
            if int((f.read() or "0").strip() or 0) == os.getpid():
                os.remove(PID_FILE)
    except (OSError, ValueError):
        pass



def save_last_msg(openid, msg_id, scope="group"):
    """记录最近一条消息（群 / 单聊），供「被动回复」兜底用。

    `group_openid` 键保留：send_file.py 的旧读法仍可用。
    """
    if not openid or not msg_id:
        return
    try:
        with open(LAST_MSG_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "scope": scope,
                "openid": openid,
                "group_openid": openid,
                "msg_id": msg_id,
                "ts": time.time(),
            }, f)
    except Exception as e:
        print("save_last_msg failed:", e, flush=True)

# 加载同目录 .env（缺失或未装 python-dotenv 时静默跳过）
try:
    from dotenv import load_dotenv

    load_dotenv(data_path(".env"))
except Exception:
    pass

TRIGGER = os.getenv("MEMO_TRIGGER", "待办").strip()


def extract_memo(text):
    """从一条群消息里抽取待办内容。支持前缀触发词，且前面可带 @机器人。"""
    if not text:
        return None
    t = strip_at(text)
    if TRIGGER and t.startswith(TRIGGER):
        content = re.sub(r"^[\s:：]+", "", t[len(TRIGGER):].strip())
        if content:
            return content
    return None


def handle_event(group_id, group_name, sender_id, sender_name, message):
    content = extract_memo(message)
    if not content:
        return None
    return add_memo(group_id, group_name, sender_id, sender_name, content)


# ── 「#」标签：写灵感时 # 后面的内容自动变成标签 ────────────────────
# 约定（用户 2026-10-12）：
#   · `#` 落在**词边界**时开启一段「标签区」，直到下一个 `#` 或结尾；
#   · 标签区内部再按「标点 / 空格 / 换行」切成**多个**标签名；
#   · 标签区从正文里摘掉，只留正文落库。
# 🔴 词边界 = 空白 / 标点 / **汉字**。汉字算边界是因为中文里「配色#设计」
#    的 # 就是标签起手（没有空格也认）；而拉丁字母 / 数字后面**不算** ——
#    「C#」「issue#42」这种 # 是词的一部分，不能剪（真机上两种都出现过）。
_TAG_OPEN = "#＃"
_BOUNDARY = set(" \t\r\n,，。;；:：、.．!！?？~～…·/\\|｜+＋()（）[]【】{}「」『』\"'“”‘’<>《》")
# 标签区内部的分隔符 = 边界字符 + # 自己（「#设计#UI」连写也要切开）
_TAG_SEP = _BOUNDARY | set(_TAG_OPEN)
TAG_MAX_PER_ITEM = 10      # 一条最多切几个标签，防「整段话全是 #」的 abusive 输入


def _is_tag_boundary(ch):
    """# 能不能在这个字符后面开标签区（空白 / 标点 / 汉字算，字母数字不算）。"""
    if ch in _BOUNDARY:
        return True
    o = ord(ch)
    # 常用汉字区（基本区 + 扩展 A + 兼容表 + 扩展 B 起头）—— 标签名本身
    # 可以含汉字，这里只看「# 前面那个字符」是不是汉字。
    return (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF
            or 0xF900 <= o <= 0xFAFF or 0x20000 <= o <= 0x2A6DF)


def split_tags(text):
    """把 `#标签` 片段从正文里摘出来 → `(正文, [标签名...])`。

    干嘛的：聊天里写「参考这个配色 #设计」时，# 后面是用户的分类意图，
            不该混在正文里。标签落库后，搜索灵感 / 脑内抽屉都能用。
    谁调用它：bot.py 收灵感的两条入库路径（_store_media / @ 带图兜底）。

    例：
        "参考这个配色 #设计 UI"   → ("参考这个配色", ["设计", "UI"])
        "参考配色#设计"           → ("参考配色", ["设计"])       # 汉字后直接跟 #
        "C# 和 Python 都学"       → ("C# 和 Python 都学", [])   # 字母后的 # 不动
        "issue#42 已修"           → ("issue#42 已修", [])       # 同理
        "#设计 #UI 参考"          → 原样返回（整条都是标签、没正文）
        "没有标签"                → ("没有标签", [])
    🔴 全是标签、没正文时**原样返回**（不切）—— 空正文的条目在抽屉里
       就是一张空白卡片，不如把原文留着。
    """
    body = text or ""
    if not any(ch in _TAG_OPEN for ch in body):
        return body, []
    tags, keep = [], []
    i, n, last = 0, len(body), 0
    while i < n:
        if body[i] in _TAG_OPEN and (i == 0 or _is_tag_boundary(body[i - 1])):
            j = i + 1
            while j < n and body[j] not in _TAG_OPEN:   # 标签区到下一个 # 或结尾
                j += 1
            keep.append(body[last:i])
            # 区内按「标点 / 空格 / 换行 / #」切成多个名字
            part = []
            for ch in body[i + 1:j]:
                if ch in _TAG_SEP:
                    if part:
                        nm = "".join(part)
                        if nm not in tags and len(tags) < TAG_MAX_PER_ITEM:
                            tags.append(nm)
                        part = []
                else:
                    part.append(ch)
            if part and len(tags) < TAG_MAX_PER_ITEM:
                nm = "".join(part)
                if nm not in tags:
                    tags.append(nm)
            last = j
            i = j
            continue
        i += 1
    keep.append(body[last:])
    content = re.sub(r"[ \t]+", " ", "".join(keep)).strip()
    if not content:                      # 全是标签、没正文 → 不切，原文照存
        return body, []
    return content, tags


# ── 收图：把消息里的图片落盘 ──────────────────────────────────────
# 🔴 **只在「这条消息确实会入库」时才下载** —— 被忽略表拦掉的、被指令吃掉的、
#    纯图片但收图开关关着的，一律不下载。否则光是别人在群里发图，硬盘就悄悄满了。
#
# 开关 = `settings.image_collect`（一个 multi 键顶三个开关，见 settings.py）：
#   []              关闭
#   ["c2c"]         只收私聊
#   ["group"]       只收群聊
#   ["c2c","group"] 都收

# 纯图片消息（没配文字）的占位正文 —— 抽屉里得有个看得见的东西。
# 🔴 不要用 emoji 当占位：QQ 消息里发的图可能一张都没有，而 Windows 上有的
#    emoji 编不出来（🫧 U+1FAE7 那次教训）。用纯文本方括号，任何字体都有。
_IMAGE_ONLY_TEXT = "[图片]"


def _att(a, name):
    """附件字段取值 —— botpy 给的是 `_Attachments` **对象**，别的地方可能是 dict。

    🔴 这里踩过一个大坑：原实现是「附件不是 dict 就跳过」，而
       `botpy.message.BaseMessage.__init__` 把 attachments 解析成
       `[self._Attachments(items) for items in data.get("attachments", {})]`
       —— **全是对象、一个 dict 都没有**，于是每个附件都被那一步跳掉，
       `image_urls()` 恒返回空数组。表现就是：收图开关开了、图也确实发到了，
       但抽屉里永远没有图（用户报「我向摸鱼bot发送了图片，但始终无法出现在抽屉里」）。
       所以取值必须对象 / dict 两种都认。
    """
    if isinstance(a, dict):
        return a.get(name)
    return getattr(a, name, None)


def image_urls(message):
    """从消息里挑出图片 URL（只认 QQ 官方的 `attachments` 结构）。

    🔴 图片走 `attachments`（不是 `message_type` / `msg_elements` —— 那两个在
       botpy 1.2.1 全源码 0 命中）。每项是 `botpy.message.BaseMessage._Attachments`，
       带 `content_type` / `filename` / `width` / `height` / `size` / `url` 属性。
    🔴 只读 `content_type` 与 `url`，**不碰别的字段** —— 平台上还有别的附件形态，
       多读一处就多一处将来会变的东西。
    """
    out = []
    for a in getattr(message, "attachments", None) or []:
        ct = str(_att(a, "content_type") or "").split(";")[0].strip().lower()
        url = str(_att(a, "url") or "").strip()
        if not url:
            continue
        # 🔴 content_type 认不出、或报成 `application/octet-stream` 时**也放行**
        #    —— QQ 偶尔把 webp 报成 octet-stream（media.download 里也记着这条）。
        #    真正的把关在 `media.download()`：它按**文件头**再判一次，
        #    两个都认不出才丢，并且只留 ≤ 20MB 的。
        if ct and not ct.startswith("image/") and ct != "application/octet-stream":
            continue
        out.append(url)
    return out[: _media.MAX_PER_MSG]


async def collect_images_async(message, scope):
    """`collect_images` 的**异步**版本 —— 给 `route_message` 用。

    🔴 为什么必须异步：收图是**阻塞式网络下载**（单张超时 10s、每条消息最多 9 张
       ⇒ 极端情况能占住事件循环约 90s）。`route_message` 是 async 路由，
       在里面同步调它 = 整个 bot 卡死 90 秒 —— WebSocket 心跳发不出去、
       别的消息全排队（审查报告点出的「事件循环阻塞」）。丢进线程池，
       事件循环照常转。
    🔴 逻辑**只有一份**：`collect_images` 仍是纯同步函数（测试直接调它），
       这里只是 `to_thread` 包一层，不复制任何判断。
    """
    return await asyncio.to_thread(collect_images, message, scope)


def collect_images(message, scope):
    """当前发布版本关闭收图；保留函数供后续版本恢复与既有调用兼容。

    配置入口虽隐藏，但旧 settings.json 或机器人设置指令仍可能保留 / 写入开关；
    因此发布开关必须在实际下载边界生效，不能只依赖 UI 隐藏。
    """
    return []
def check_image_alert():
    """占用超阈值就往播报目标发提醒（防刷屏靠 push 里的回滞状态）。"""
    try:
        threshold = int(_settings.get("image_alert_gb") or 0)
        if threshold <= 0:
            return
        targets = _push.targets()
        if not targets:
            return                       # 没配播报目标就没处发，静默跳过

        def _send(text):
            import send_file as sf

            appid = os.getenv("QQ_BOT_APPID")
            secret = os.getenv("QQ_BOT_SECRET") or os.getenv("QQ_BOT_TOKEN")
            sandbox = (os.getenv("QQ_BOT_SANDBOX") or "").strip().lower() in ("1", "true", "yes")
            for sc, oid in targets:
                sf.send_text_message(text, oid, sandbox, appid, secret, None, 1, sc)

        r = _push.check_image_alert(threshold, _media.usage(), _send)
        if r.get("fired"):
            print("[收图·报警] 已发出占用提醒", flush=True)
    except Exception as e:               # noqa: BLE001  报警失败绝不能影响收图
        print(f"[收图·报警失败] {type(e).__name__}: {e}", flush=True)


# ── 群指令 ──────────────────────────────────────────────────────
# 内置指令的菜单；扩展指令的帮助行由 help_text() 追加（见文件后半的扩展加载）。
HELP_BASE = (
    "🐟 摸鱼bot 指令：\n"
    "待办 <内容> —— 记一条待办\n"
    "查待办 —— 看最近 10 条待办\n"
    "查灵感 —— 看还没处理的灵感\n"
    "搜索灵感 <关键词> —— 在灵感里搜（正文 / 标签 / 结论都算）\n"
    "发文件 <关键词> —— 在配置中心「④ 发文件」设的目录里搜文件、渲染成图片发出\n"
    "摸鱼 [1~5|放松|冷知识|全部] —— 随机来一条，歇一下\n"
    "回旋镖 [天数] —— 把 N 天前（默认 3 天）记过的旧账翻一条回来\n"
    "导出 [格式] —— 把全部待办 + 灵感导成一个文件发给你（格式 md / txt / json / zip）\n"
    "            zip 连收进来的图片原图一起打包（太大发不出来时会告诉你去哪拿）\n"
    "忽略 <内容> —— 含该内容的消息一律跳过（不收灵感、不响应）；\n"
    "            「忽略 列表 / 忽略 删除 X / 忽略 清空」管理\n"
    "设置 —— 看/改本机设置（桌面「配置中心」改的是同一份）；\n"
    "            「设置 <键> <值> / 设置 重置 <键>」\n"
    "你好 —— 看我都能干什么（说明里写清了：我没有语言生成能力）\n"
    "帮助 —— 显示本菜单\n"
    "群 / 私聊都可用；不带 @ 的普通发言（含私聊）会自动收作「灵感」，不打扰\n"
    "写灵感带 # 号：# 后面的内容自动存成标签（空格 / 标点分隔多个）"
)
MEMO_LIST_CMDS = {"查待办", "查看待办", "待办列表", "列待办"}
IDEA_LIST_CMDS = {"查灵感", "查看灵感", "灵感列表", "列灵感"}
HELP_CMDS = {"帮助", "菜单", "指令", "help"}
# 「你好」= 自我介绍。回复内容 = 设置里的「功能介绍」（首次上线发的也是它），
# 所以用户在配置中心改一次，两处一起变。
HELLO_CMDS = {"你好", "您好", "hi", "hello", "嗨"}
# 设置里「功能介绍」被清空时的兜底 —— 保证「你好」永远有话说。
HELLO_FALLBACK = (
    "你好，我是摸鱼bot 🐟\n"
    "我只会记事和搬文件：记待办、收灵感、搜索灵感、发文件、摸鱼、回旋镖、导出。\n"
    "写灵感时加 #标签，之后可以按标签找回来。\n"
    "我没有语言生成能力，不会聊天、也不会回答问题。\n"
    "发「帮助」看完整指令。"
)

# ── 首次上线的「功能介绍」────────────────────────────────────────
# 只发一次：**第一次**连上发功能介绍，之后每次上线只发「启动播报」。
# 🔴 标记写在**数据目录**（安装版 = %LOCALAPPDATA%\memo-bot），不写代码目录 ——
#    源码版和安装版各记各的，互不干扰；卸载也不动数据。
_ONBOARD_FILE = "onboarded.json"


def intro_text():
    """「你好」的回复 / 首次上线的自我介绍 —— 两处取的是同一份设置。"""
    return (_settings.get("intro_msg") or "").strip() or HELLO_FALLBACK


def onboarded_done():
    """第一次连上时发过「功能介绍」没有。文件缺失 / 读坏了都当没发过。"""
    try:
        with open(data_path(_ONBOARD_FILE), encoding="utf-8") as fh:
            return bool(json.load(fh).get("done"))
    except (OSError, ValueError):
        return False


def mark_onboarded():
    """记下「功能介绍已发」。**发成功才调** —— 发失败下次上线还能重试。"""
    try:
        with open(data_path(_ONBOARD_FILE), "w", encoding="utf-8") as fh:
            json.dump({"done": True,
                       "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                      fh, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"[功能介绍·标记写入失败] {type(e).__name__}: {e}", flush=True)


# ── 回旋镖：把 N 天以前的旧记录翻一条推回给用户 ───────────────────
# 指令词只认完整写法，不用「镖」「回旋」这种短词 —— 否则「飞镖」「回旋踢」会被误判。
BOOMERANG_CMDS = ("回旋镖", "回旋标")
# ── 导出：把全部待办 + 灵感导成一个文件发回来 ─────────────────────
EXPORT_CMDS = ("导出",)
# ── 「摸鱼」：随机抽一条放松/摸鱼/冷知识 ───────────────────────────
# 指令识别在 moyu.parse() 里（归一化 + 前缀匹配），这里不重复写正则。
MOYU_BADARG_HINT = "（摸鱼后面可以写：条数 1~5 / 放松 / 冷知识 / 全部）"


# QQ 群 @机器人 的两种形态：
#   1) 真机实测：content 里是 `<C9>` 这样的尖括号占位符（不是「@昵称」）
#   2) 部分客户端/自测：`@昵称`
# 都要剥掉，否则 @ 场景下所有指令都匹配不上。
_MENTION_ANGLE = re.compile(r"<[^\s<>]{1,40}>")
_MENTION_AT_PREFIX = re.compile(r"^(?:\s*@[^\s@]{1,30})+\s*")


def strip_at(text):
    """去掉 @机器人 标记（尖括号占位符任意位置；@昵称 只在开头）。"""
    t = _MENTION_ANGLE.sub(" ", text or "")
    t = _MENTION_AT_PREFIX.sub("", t)
    return re.sub(r"[ \t]+", " ", t).strip()


# ── 会话标识：群 / 单聊统一 ──────────────────────────────────────
SCOPE_GROUP = "group"
SCOPE_C2C = "c2c"
C2C_GROUP_NAME = "私聊"
# 私聊里「非指令文本」收作灵感后回不回执 —— 由设置项 `idea_ack` 决定：
# 留空 = 静默收（默认，跟群内非 @ 一个语义）；填一句就回那一句。
# 🔴 别再往这里加写死的开关 —— 配置中心里能改，改完重启即可。


def chat_target(message, scope):
    """统一群 / 单聊的会话标识，返回 dict：

      key        —— 会话唯一键（PENDING 用；群 `group:<id>` / 单聊 `c2c:<id>`）
      store_id   —— 落库的 group_id（群 = group_openid；单聊 = `c2c:<user_openid>`）
      store_name —— 落库的 group_name（群为空；单聊固定「私聊」）
      openid     —— 发送目标 openid（群 group_openid / 单聊 user_openid）
      sender_id  —— 发送者 id（群 member_openid / 单聊 user_openid）
    """
    if scope == SCOPE_C2C:
        author = getattr(message, "author", None)
        openid = (getattr(author, "user_openid", "") or "") if author else ""
        return {"key": f"c2c:{openid}", "store_id": f"c2c:{openid}",
                "store_name": C2C_GROUP_NAME, "openid": openid, "sender_id": openid}
    openid = getattr(message, "group_openid", "") or ""
    author = getattr(message, "author", None)
    sid = (getattr(author, "member_openid", "") or "") if author else ""
    return {"key": f"group:{openid}", "store_id": openid, "store_name": "",
            "openid": openid, "sender_id": sid}


# ── 回旋镖：把旧记录翻一条推回来 ──────────────────────────────────
def parse_boomerang(text):
    """`回旋镖` / `回旋镖 7` → (True, None) / (True, 7)；不是这个指令 → (False, None)。

    天数可以写在指令后面，临时改这一次的回溯范围（不用去配置中心改设置）。
    🔴 下限是 **0**（不是 1）：`回旋镖 0` = 今天记的也翻得到，刚上手就能试。
    """
    t = (text or "").strip()
    for c in BOOMERANG_CMDS:
        if not t.startswith(c):
            continue
        rest = t[len(c):]
        if not rest.strip():
            return True, None
        m = re.match(r"^[ ：:　\t]*(\d{1,4})", rest)
        if m:
            return True, max(0, min(3650, int(m.group(1))))
        return False, None          # 「回旋镖xx」这种不是指令，落回普通流程
    return False, None


def boomerang_text(days=None, now=None):
    """回旋镖的回复文案：从「N 天以前」的旧记录里随机挑一条。

    🔴 **哪发的指令就推回哪** —— 这里只生成文本，具体发到群还是私聊由
    route_message 用当前这条 message 决定。

    🔴 **抽出来的卡片放回池子**（用户 2026-10-12）：挑中的那条不打标记、池子不变，
    同一条可以被反复抽到（连着两次抽到同一张也算数）。
    """
    now = now or datetime.now()
    if days is None:
        days = int(_settings.get("boomerang_days"))
    scope = _settings.get("boomerang_scope")

    item = pick_boomerang(days, scope, now)
    if not item:
        # 只有两种「挑不到」：① 范围内有记录、但都还没满天数；② 范围内压根没记录。
        # 🔴 不会再有「都推过一遍了」—— 池子不再减少（同一条能反复抽到）。
        if count_boomerang(0, scope, now):
            return (f"🪃 还没有满 {days} 天的旧记录，再等等。\n"
                    f"（可推范围在「配置中心 → 回旋镖」里改；回溯天数填 0 就能翻今天记的）")
        return ("🪃 这个范围里还没有记录可翻。\n"
                "（先去记两条；也可以到「配置中心 → 回旋镖」把推送范围放宽）")

    created = item.get("created_at") or ""
    try:
        ago = (now - datetime.fromisoformat(created)).days
    except ValueError:
        ago = days
    verb = "记过一条待办" if item.get("kind") == KIND_MEMO else "冒出过一个想法"
    note = (item.get("note") or "").strip()
    tail = f"\n\n【当时的结论】{note}" if note else ""
    # 回溯天数可以是 0（今天记的也翻得到）—— 那时候别写「0 天前」，说「今天」
    head = (f"你今天（{created[:10]}）{verb}："
            if ago <= 0 else f"你 {ago} 天前（{created[:10]}）{verb}：")
    return (f"🪃 回旋镖！\n"
            f"{head}\n"
            f"{(item.get('content') or '').strip()}{tail}\n\n"
            f"…还有后续吗？")


# ── 导出：把全部待办 + 灵感导成一个文件发回来 ─────────────────────
def parse_export(text):
    """`导出` / `导出 md` / `导出 json` / `导出 zip` → (True, 格式或 None)；不是指令 → (False, None)。"""
    t = (text or "").strip()
    for c in EXPORT_CMDS:
        if not t.startswith(c):
            continue
        rest = t[len(c):].strip(" ：:　\t")
        if not rest:
            return True, None
        if rest.lower().lstrip(".") in ("md", "markdown", "txt", "text", "json", "zip"):
            return True, rest
        return False, None
    return False, None


def memo_list_text(limit=10, kind=KIND_MEMO):
    is_memo = kind == KIND_MEMO
    if is_memo:
        rows = list_memos(limit, kind)
        label, icon = "待办", "📝"
        head = f"{icon} 最近 {len(rows)} 条{label}："
    else:
        # 灵感只看「待处理」——处理过的在电脑上用脑内抽屉看
        rows = list_ideas(STATUS_OPEN, limit, newest_first=True)
        label, icon = "灵感", "💡"
        st = idea_stats()
        # 🔴 「已处理 / 已决议」合并报「已整理」（用户 2026-10-12：两种烧脑分法
        #    简化成一种；库里 status 不动，decided 照常算进已整理）
        head = (
            f"{icon} 待处理灵感 {st['open']} 条"
            f"（已整理 {st['done'] + st['decided']} / 已丢弃 {st['dropped']}）："
        )
    if not rows:
        return f"（暂无{label}）"
    lines = [head]
    for m in rows:
        ts = (m.get("created_at") or "")[5:16].replace("T", " ")
        lines.append(f"[{ts}] {strip_at(m['content'])}")
    return "\n".join(lines)


# ── 「搜索灵感 <关键词>」：在灵感里按正文 / 标签 / 结论搜 ────────────
# 🔴 @ 与非 @ 都认：非 @ 群消息也认，不然「搜索灵感 设计」会被当成普通
#    发言收成灵感，越收越乱（和查待办 / 查灵感 同一处理）。
# 🔴 「已处理 / 已决议」对外一律报「已整理」（用户 2026-10-12 简化）；
#    库里的 status 不动，decided 照常算进已整理。
IDEA_SEARCH_RE = re.compile(r"^(?:搜索灵感|搜灵感|灵感搜索|搜索)\s*(.+)$", re.S)
IDEA_SEARCH_HINT = "告诉我搜什么，例如「搜索灵感 设计」"
IDEA_SEARCH_SCAN = 500        # 只扫最近 500 条灵感（再老的让抽屉去翻）


def idea_search_text(q, limit=10):
    """按「正文 / 标签 / 结论」搜灵感，命中按时间倒序回前 limit 条。

    干嘛的：灵感一多，轮播等不到、列表翻不完 —— 「搜索灵感 设计」直接
            按关键词找（用户 2026-10-12 的要求）。写灵感时 # 出来的标签
            也参与匹配：打了「#设计」的条目，搜「设计」照样命中。
    谁调用它：消息处理器里「搜索灵感」指令分支。
    """
    q = " ".join((q or "").split())          # 多行 / 连续空格折成一个
    if not q:
        return IDEA_SEARCH_HINT
    key = q.lower()
    hits = []
    for it in list_ideas(None, IDEA_SEARCH_SCAN, newest_first=True):
        tags = [str(t) for t in (it.get("tags") or [])]
        hay = "\n".join([str(it.get("content") or ""),
                         str(it.get("note") or "")] + tags).lower()
        if key in hay:
            hits.append((it, tags))
    if not hits:
        return (f"（没有含「{q}」的灵感 —— 换个词试试；"
                f"发「查灵感」可以看全部待处理）")
    lines = [f"💡 搜「{q}」命中 {len(hits)} 条灵感"
             f"（列出前 {min(limit, len(hits))} 条，按时间倒序）："]
    for it, tags in hits[:limit]:
        ts = (it.get("created_at") or "")[5:16].replace("T", " ")
        st = it.get("status")
        if st == STATUS_OPEN:
            mark = ""
        elif st in (STATUS_DONE, STATUS_DECIDED):
            mark = "（已整理）"
        else:
            mark = "（已丢弃）"
        tag_s = (" · " + " ".join("#" + t for t in tags)) if tags else ""
        lines.append(f"[{ts}] {strip_at(it['content'])}{tag_s}{mark}")
    return "\n".join(lines)


# ── 「发文件」：搜索文件根目录 + 交互式选择 ────────────────────────
# 🔴 搜索根默认是你的用户主目录（~）；用环境变量 MEMO_SEARCH_ROOT 改成任意目录
#    （比如只搜某个项目文件夹）。**支持多个目录**（分号或换行分隔），配置中心
#    「④ 发文件」里一行一个。注意：这是「发文件」功能的检索范围，
#    不要指向含凭据 / 私密文件的目录。
# 🔴 同目录文件在搜索里加权（同分时优先）；用 MEMO_WORKSPACE_ROOT 覆盖。
WORKSPACE_ROOT = os.getenv("MEMO_WORKSPACE_ROOT") or HERE


def parse_search_roots(raw=None):
    """把 `MEMO_SEARCH_ROOT` 解析成目录列表（分号或换行分隔；去重、展开 `~`）。

    空 → 回退到用户主目录（跟以前一样）。**配置中心里是每行一个目录。**
    """
    text = os.getenv("MEMO_SEARCH_ROOT") if raw is None else raw
    out = []
    for part in re.split(r"[;\n]+", text or ""):
        p = part.strip().strip('"').strip("'")
        if not p:
            continue
        p = os.path.expanduser(p)
        if p not in out:
            out.append(p)
    return out or [os.path.expanduser("~")]


def _prune_roots(roots):
    """去掉被别的根包含的子目录 —— 否则同一棵树会被 `os.walk` 走两遍。"""
    norm = sorted({os.path.normcase(os.path.abspath(r)) for r in roots}, key=len)
    out = []
    for r in norm:
        if any(r == o or r.startswith(o + os.sep) for o in out):
            continue
        out.append(r)
    return out


SEARCH_ROOTS = parse_search_roots()
SEARCH_ROOT = SEARCH_ROOTS[0]      # 兼容：扩展 API / 老代码只认单个根


def search_scope_text(limit=120):
    """「没找到文件」回执里附的搜索范围说明（多个根用顿号连；过长截断）。

    默认根是用户主目录也照样显示 —— 用户得知道「刚才到底搜了哪儿」，
    才好判断是该换个关键词，还是去配置中心「④ 发文件」加目录。
    """
    s = "、".join(_prune_roots(SEARCH_ROOTS))
    return s if len(s) <= limit else s[: limit - 1] + "…"
EXCLUDE_DIRS = {
    "node_modules", ".git", "__pycache__", "envs", ".venv",
    "binaries", ".cache", "dist", "build", ".next",
    # 隐私保护：这些目录里的文件永远不会被「发文件」检索到
    ".workbuddy-ai", ".ssh", ".aws", ".config", ".npm", ".cargo",
}

# 🔴 Windows 保留设备名。误写 `>nul` 这类重定向时，磁盘上会真的多出一个叫 `nul`
#    的文件（要 `\\?\` 前缀才建得出来）。这种名字在 Win32 路径层会被解析成**设备**
#    （`nul` → `\\.\nul`），于是 `os.path.relpath` 认为它「在另一个盘上」直接拒绝，
#    `os.stat` / `os.remove` 之类也全都拿它没办法 —— **一个这样的文件就能把整条
#    搜索指令搞崩**。搜索时直接跳过，别让用户去处理这种系统级垃圾。
_RESERVED_NAMES = ({"con", "prn", "aux", "nul"}
                   | {f"com{i}" for i in range(1, 10)}
                   | {f"lpt{i}" for i in range(1, 10)})


def _is_reserved_name(fn):
    """文件名是不是 Windows 保留设备名（`nul`、`NUL`、`nul.txt`、`com3.log` 都算）。

    判据是「第一个点之前的部分」—— Win32 对 `nul.txt` 同样按设备解析。
    """
    return fn.split(".")[0].lower() in _RESERVED_NAMES


SEARCH_LIMIT = 20

# @ 场景：宽松（「发」「发送」等口语别名）
FILE_SEND_RE = re.compile(r"^(?:发文件|找文件|发送文件|发送|给我发|帮我发|发)\s*(.+)$")
# 非 @ 场景：只认明确写法，避免「发工资了吗」这类误触
EXPLICIT_FILE_RE = re.compile(r"^(?:发文件|找文件|发送文件)\s*(.+)$")
BARE_FILE_CMDS = {"发文件", "找文件", "发送文件", "发送"}
PENDING = {}          # group_openid -> {"matches": [(rel, full)], "ts": float}
PENDING_TTL = 300     # 选择态有效期（秒）
MAX_SEND = 200 * 1024 * 1024
RENDER_MAX = 150 * 1024   # 文本类文件「渲染成图片」的体积上限（字节）


def _is_subsequence(q, s):
    """q 的字符是否按顺序出现在 s 中（如「设计说明」→「设计风格参考说明」）。"""
    it = iter(s)
    return all(ch in it for ch in q)


def search_files(query, limit=SEARCH_LIMIT):
    """按文件名/路径模糊搜**配置的根目录**（默认你的用户主目录，可用 MEMO_SEARCH_ROOT 改，
    **支持多个目录** —— 分号分隔，配置中心里是每行一个）。

    打分（全部**忽略大小写**）：
      3.0 文件名含该子串（最准，如 readme → README.md）
      2.5 相对路径含该子串
      2.0 文件名是其**子序列**（如 设计说明 → 设计风格参考说明-2026-10-08.md）
      1.5 相对路径是其**子序列**（如 工作日志10-08 → .../每日工作日志/2026-10-08.md）
      当前工作区内的文件额外 +0.1（同分时优先本地）

    返回顺序（🔴 见下面 sort）：
      **匹配分降序 → 同分时修改时间倒序（最新在前）→ 再同分时路径短的在前**

    ⚠️ 1.5 这级只在查询长度 >=3 时启用 —— 子序列太宽松，短查询会命中一大片。
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    out = []
    seen = set()
    # 🔴 前缀比较统一 `normcase` 再比：`_prune_roots` 已经把搜索根小写化，而
    #    `WORKSPACE_ROOT` 的原始大小写取决于进程是怎么被拉起来的（pythonw 传的
    #    路径大小写不固定）。不归一的话，「同目录加权」会时灵时不灵，更糟的是
    #    —— 见下面那支没有兜底的 relpath —— 会直接把指令搞崩。
    ws_prefix = os.path.normcase(WORKSPACE_ROOT + os.sep)
    for root in _prune_roots(SEARCH_ROOTS):
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
            for fn in filenames:
                if _is_reserved_name(fn):
                    continue
                low = fn.lower()
                full = os.path.join(dirpath, fn)
                key = os.path.normcase(full)
                if key in seen:              # 多个根有重叠时别重复收
                    continue
                seen.add(key)
                # 🔴 两支的 relpath 都要兜 `ValueError`。Windows 上出现保留设备名
                #    文件（`nul` → `\\.\nul`）时 relpath 会认为它「在另一个盘上」
                #    而直接拒绝；上面 `_is_reserved_name` 已经挡掉了常见情况，
                #    这里是第二层保险 —— 任何一个文件都不该崩掉整条指令。
                try:
                    if os.path.normcase(full).startswith(ws_prefix):
                        rel = os.path.relpath(full, WORKSPACE_ROOT).replace("\\", "/")
                        local = True
                    else:
                        rel = os.path.relpath(full, root).replace("\\", "/")
                        local = False
                except ValueError:
                    continue
                if q in low:
                    score = 3.0
                elif q in rel.lower():
                    score = 2.5
                elif _is_subsequence(q, low):
                    score = 2.0
                elif len(q) >= 3 and _is_subsequence(q, rel.lower()):
                    score = 1.5
                else:
                    continue
                if local:
                    score += 0.1
                # mtime 只对**命中的**文件取（命中数很少，开销可忽略；不是对全树 stat）
                try:
                    mt = os.path.getmtime(full)
                except OSError:
                    mt = 0.0
                out.append((score, mt, rel, full))
    # 🔴 匹配分降序 → **修改时间倒序（最新在前）** → 路径短的在前（同秒时保证结果稳定）。
    #    以前同分是按路径长度升序排，那个顺序对用户没有意义（用户 2026-10-09 明确要求改成时间优先）。
    out.sort(key=lambda x: (-x[0], -x[1], len(x[2])))
    return [(rel, full) for _, _, rel, full in out[:limit]]


def resolve_choice(text, matches):
    """把用户的回复解析成 matches 里的一项（支持序号或文件名片段）。"""
    t = (text or "").strip()
    if t.isdigit():
        i = int(t)
        return matches[i - 1] if 1 <= i <= len(matches) else None
    hits = [m for m in matches if t and t in m[0]]
    return hits[0] if len(hits) == 1 else None


def file_list_text(matches, truncated=False):
    lines = [f"📎 找到 {len(matches)} 个文件，回复序号选一个："]
    for i, (rel, _) in enumerate(matches, 1):
        lines.append(f"{i}. {rel}")
    if truncated:
        lines.append(f"（只列前 {SEARCH_LIMIT} 个 —— 加更精确的关键词能缩小范围）")
    return "\n".join(lines)


# ── 本地扩展（自定义指令）─────────────────────────────────────────
# 公开仓库只带 extensions/example.py 模板；你的私有指令放 extensions/local/（已 gitignore）。
# 契约见 extensions/__init__.py 与 README 的「可扩展性」章节。
EXT = _ext.load(api=SimpleNamespace(search_files=search_files, SEARCH_ROOT=SEARCH_ROOT))


def help_text():
    """帮助菜单 = 内置指令 + 已注册的扩展指令。"""
    lines = [HELP_BASE]
    ext_help = EXT.help_lines()
    if ext_help:
        lines.append("")
        lines.extend(ext_help)
    return "\n".join(lines)


# ── 补 botpy 缺失的「群全量消息」解析器 ────────────────────────────
def install_group_message_parser():
    """botpy 1.2.1 只注册了 group_at_message_create，全量群消息会被网关丢弃
    （日志报 `_parser unknown event group_message_create`）。
    这里给 ConnectionState 补一个 parse_group_message_create，使
    GROUP_MESSAGE_CREATE 能分发到 on_group_message_create。
    必须在 Client 建立连接前调用（解析器在 ConnectionState.__init__ 里快照）。
    """
    try:
        from botpy.connection import ConnectionState
        from botpy.message import GroupMessage
    except Exception as e:
        print("[warn] 补全量群消息解析器失败（import）:", e, flush=True)
        return False
    if hasattr(ConnectionState, "parse_group_message_create"):
        return True

    def parse_group_message_create(self, payload):
        _message = GroupMessage(self.api, payload.get("id", None), payload.get("d", {}))
        self._dispatch("group_message_create", _message)

    ConnectionState.parse_group_message_create = parse_group_message_create
    print("[init] 已补上 group_message_create 解析器", flush=True)
    return True


# ── 本地自测：不连 QQ，模拟几条群消息验证逻辑 ──────────────────────
class _FakeEv:
    def __init__(self, gid, gname, sid, sname, msg):
        self.group_id, self.group_name = gid, gname
        self.sender_id, self.sender_name, self.message = sid, sname, msg


def self_test():
    """本地模拟群消息，验证识别 + 落库逻辑。

    🔴 落盘走**临时库**，不碰真实 `memos.db`（否则每跑一次就污染真实待办）。
    """
    import store as _store

    orig_db = _store.DB
    tmpdir = tempfile.mkdtemp(prefix="memo_selftest_")
    _store.DB = os.path.join(tmpdir, "selftest.db")
    try:
        init()
        samples = [
            _FakeEv("123", "示例开发群", "dominik", "dominik", "待办 明天确认云托管最小实例数"),
            _FakeEv("123", "示例开发群", "dominik", "dominik", "@机器人 待办 备案变更还差营业执照"),
            _FakeEv("123", "示例开发群", "dominik", "dominik", "这只是普通聊天不是待办"),
            _FakeEv("123", "示例开发群", "dominik", "dominik", "待办：把事件系统文档补完"),
        ]
        print("=== 模拟群消息识别 ===")
        for s in samples:
            r = handle_event(s.group_id, s.group_name, s.sender_id, s.sender_name, s.message)
            print(f"  {s.message!r}\n    -> {('已记录: ' + r['content']) if r else '忽略（非待办）'}")
        print("\n=== 「#」标签切分（split_tags）===")
        for s in ("参考这个配色 #设计 UI",
                  "参考配色#设计",
                  "C# 和 Python 都学",
                  "issue#42 已修",
                  "#设计 #UI 参考",
                  "设计、UI、参考 三个词 #设计,UI,参考",
                  "没有标签"):
            c, tg = split_tags(s)
            print(f"  {s!r}\n    -> 正文 {c!r} / 标签 {tg}")
        print("\n=== 本次模拟落库结果（临时库，已丢弃）===")
        for m in list_memos():
            print(f"  [{m['created_at']}] {m['group_name']}/{m['sender_name']}: {m['content']}")
    finally:
        _store.DB = orig_db
        shutil.rmtree(tmpdir, ignore_errors=True)


def list_mode():
    init()
    rows = list_memos()
    if not rows:
        print("（暂无待办 / 灵感）")
        return
    for m in rows:
        tag = "待办" if m.get("kind") == KIND_MEMO else "灵感"
        print(f"[{m['created_at']}] ({tag}) {m['group_name']}/{m['sender_name']}: {m['content']}")


def _arg_value(flag):
    """取命令行 --flag value 的值。"""
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


LOG_MAX_BYTES = 5 * 1024 * 1024  # 超过就轮转（bot.out.log → .1 → .2 → .3）
LOG_KEEP = 3


def rotate_log(path, max_bytes=LOG_MAX_BYTES, keep=LOG_KEEP):
    """日志超限则轮转：path → path.1 → … → path.{keep}。失败不阻塞启动。"""
    try:
        if not os.path.isfile(path) or os.path.getsize(path) < max_bytes:
            return False
    except OSError:
        return False
    for i in range(keep, 1, -1):
        src, dst = f"{path}.{i - 1}", f"{path}.{i}"
        if os.path.isfile(src):
            try:
                os.replace(src, dst)
            except OSError:
                pass
    try:
        os.replace(path, f"{path}.1")
        return True
    except OSError:
        return False


def setup_file_logging(path):
    """把 stdout/stderr 重定向到日志文件（供 pythonw 无控制台常驻运行）。

    必须在 import botpy 之前调用 —— botpy 在导入时执行 logging.basicConfig，
    StreamHandler 会绑定当时的 sys.stderr。
    """
    rotated = rotate_log(path)
    f = open(path, "a", encoding="utf-8", buffering=1)
    sys.stdout = f
    sys.stderr = f
    # 先占住 root handler（绑定到文件、去掉 ANSI 色码），
    # 这样 botpy 导入时的 logging.basicConfig 不会再挂一个带颜色的 stderr handler。
    import logging

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(f)
    handler.setFormatter(
        logging.Formatter("%(asctime)s\t[%(levelname)s]\t%(filename)s:%(lineno)s\t%(message)s")
    )
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    print(f"\n===== 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} =====", flush=True)
    if rotated:
        print(f"[init] 日志超过 {LOG_MAX_BYTES // 1024 // 1024}MB，已轮转为 {path}.1", flush=True)
    return f


# ── 路由：@ / 全量 / 私聊 共用 ────────────────────────────────────
async def route_message(client, message, mentioned, scope=SCOPE_GROUP):
    """@ / 全量 / 私聊 共用路由。mentioned=False 时只认明确指令，避免误触。

    抽成模块级函数（而非 MemoClient 的方法）是为了能被回归测试直接驱动：
    `client` 只需提供 send_file_to_chat(message, scope, openid, full, rel, mid)。
    """
    t = chat_target(message, scope)
    gid, mid = t["key"], (getattr(message, "id", "") or "")
    # 记下这个会话（群 / 私聊都记）—— 配置中心「① 连接 QQ」据此列出
    # 「机器人在哪些会话里活动过」，用户点着勾选就决定播报发到哪（见 push.py）。
    # 🔴 不传名字：平台侧根本拿不到 —— 群名接口要白名单，私聊没有查用户信息的接口。
    #    `botpy.message.GroupMessage.__slots__` 里连 `group_name` 都没有，
    #    以前那句 `getattr(…, "group_name", "")` 是永远取不到值的死代码。
    #    名字靠用户自己在配置中心起（`push.set_name`）。
    try:
        _push.note(scope, t["openid"])
    except Exception:
        pass
    save_last_msg(t["openid"], mid, scope)
    raw = getattr(message, "content", "") or ""
    text = strip_at(raw)
    if mentioned:
        # 留原始内容便于排查 @ 占位符的真实编码
        print(f"[@消息] scope={scope} raw={raw!r} -> text={text!r}", flush=True)

    # 0) 忽略表：先放行「忽略」指令本身（否则指令会被自己的规则吃掉），
    #    再对普通消息做闸门 —— 命中即整条跳过（不收灵感、不响应、**也不收图**）。
    #    同群有别的机器人时，靠这个避免互相收集 / 循环响应。
    #
    # 🔴 忽略表必须在**收图之前**：收图是有副作用的（下载 + 写盘 + 可能触发报警），
    #    要它先过闸门，不然「忽略」两个字就白写了。
    # ⚠️ 但纯图片消息 `text` 是空的，而忽略表按文本匹配 —— 空文本匹配不到任何规则，
    #    所以这里先判「有没有图」，有图就继续往下走（不能因为没文本就 return）。
    urls = image_urls(message)
    if not text and not urls:
        return

    if text and _ignore.is_command(text):
        await message.reply(content=_ignore.handle_command(text), msg_seq=1)
        return
    hit = _ignore.match(text, t["sender_id"]) if text else None
    if hit:
        print(f"[忽略] scope={scope} hit={hit!r} text={text!r}", flush=True)
        return

    # 0.6) 设置：改的是 `memo-bot/settings.json` —— 和网页「⚙ 设置」同一份。
    #      「设置」后面**必须有分隔符**，否则「设置好了」「配置一下」这种普通聊天
    #      会被当成指令（同忽略表的处理）。
    if text and _settings.is_command(text):
        await message.reply(content=_settings.handle_command(text), msg_seq=1)
        return

    # 0.5) 有待选文件时，优先解析「选哪个」（序号 / 文件名片段）
    pend = PENDING.get(gid)
    if pend and text and time.time() - pend["ts"] <= PENDING_TTL:
        choice = resolve_choice(text, pend["matches"])
        if choice:
            PENDING.pop(gid, None)
            await client.send_file_to_chat(
                message, scope, t["openid"], choice[1], choice[0], mid
            )
            return

    # 🔴 从这里往下全是**看文本**的分支。纯图片消息（text 为空）要直接跳到兜底收图，
    #    否则 `FILE_SEND_RE.match("")` 这类空串匹配会莫名其妙命中。
    if not text:
        return await _store_media(message, t, scope, urls, "", mentioned)

    # 1) 发文件：搜工作区 → 唯一直接发 / 多个列出来问
    bare_cmds = BARE_FILE_CMDS if mentioned else {"发文件", "找文件", "发送文件"}
    if text in bare_cmds:
        await message.reply(content="告诉我要发什么，例如「发文件 设计说明」", msg_seq=1)
        return
    fm = (FILE_SEND_RE if mentioned else EXPLICIT_FILE_RE).match(text)
    if fm:
        q = fm.group(1).strip()
        # 🔴 必须丢线程：搜索根可能是整个用户目录（百万级文件），同步跑会把事件循环
        #    卡死几十秒 —— 期间收不到任何消息、心跳也停摆。
        matches = await asyncio.to_thread(search_files, q)
        if not matches:
            # 把实际搜索范围回给用户：根可能是好几个（配置中心「④ 发文件」），
            # 不说清楚「搜了哪儿」，用户只会反复换关键词干着急。
            await message.reply(
                content=f"（没找到含「{q}」的文件 —— 只搜这些目录："
                        f"{search_scope_text()}）", msg_seq=1
            )
            return
        if len(matches) == 1:
            await client.send_file_to_chat(
                message, scope, t["openid"], matches[0][1], matches[0][0], mid
            )
            return
        PENDING[gid] = {"matches": matches, "ts": time.time()}
        await message.reply(
            content=file_list_text(matches, len(matches) >= SEARCH_LIMIT), msg_seq=1
        )
        return

    # 1.5) 本地扩展指令（extensions/ 下；自定义指令不进公开仓库）
    #      处理器同步执行（放线程，阻塞操作不卡心跳）；可返回多段消息
    ext_handler = EXT.match(text)
    if ext_handler:
        try:
            out = await asyncio.to_thread(ext_handler, text, mentioned)
        except Exception as e:
            out = f"（扩展指令执行失败：{type(e).__name__}: {e}）"
        if out:
            parts = out if isinstance(out, list) else [out]
            for i, part in enumerate(parts, start=1):
                await message.reply(content=part, msg_seq=i)
            return

    # 1.7) 摸鱼：随机来一条放松/摸鱼点子（`摸鱼 3` 多条、`摸鱼 冷知识` 换池）
    my = moyu.parse(text)
    if my is not None:
        if my == "badarg":
            # @ 场景：明确在跟机器人说话 → 给参数提示；
            # 非 @ 场景：可能只是聊天（「摸鱼真爽」），不打扰 → 落回原流程
            if mentioned:
                await message.reply(content=MOYU_BADARG_HINT, msg_seq=1)
                return
        else:
            n, cats = my
            reply = moyu.format_moyu(moyu.draw(n, cats))
            print(f"[摸鱼] {gid} text={text!r} n={n} cats={cats} -> {reply[:36]!r}", flush=True)
            await message.reply(content=reply, msg_seq=1)
            return

    # 1.8) 回旋镖：把 N 天以前的旧记录翻一条推回**当前这个会话**
    #      （哪发的指令推哪：群里发就回群里，私聊发就回私聊）
    is_bm, bm_days = parse_boomerang(text)
    if is_bm:
        reply = boomerang_text(bm_days)
        print(f"[回旋镖] {gid} days={bm_days} -> {reply[:40]!r}", flush=True)
        await message.reply(content=reply, msg_seq=1)
        return

    # 1.9) 导出：把全部待办 + 灵感导成一个文件发回来（群 / 私聊都可用）
    is_ex, ex_fmt = parse_export(text)
    if is_ex:
        fname, data = _export.render(ex_fmt)
        # 🔴 平台发文件上限 ~10MB，zip 带原图很容易超（手机照片 2–5MB/张）。
        #    超了不发半截包 —— 直接降级成「文本 + 文件在哪」，让用户自己去拿。
        #    半截 zip 更糟：用户以为下载成功了，解压才发现是坏的。
        if _export.too_big_for_qq(data):
            size = _export.human_size(len(data))
            await message.reply(
                content=(f"📦 打包好了，但整包 {size} 超过了机器人发文件的上限（10MB），发不出来。\n"
                         f"包已经放在本机：{_export.render_to_data(ex_fmt)[1]}\n"
                         f"直接在电脑上打开这个文件夹拿就行。"),
                msg_seq=1)
            print(f"[导出] {gid} {fname} 太大（{len(data)} bytes）→ 降级为提示", flush=True)
            return
        tmpdir = tempfile.mkdtemp(prefix="memo_export_")
        try:
            path = os.path.join(tmpdir, fname)
            with open(path, "wb") as fh:
                fh.write(data)
            # as_file=True：导出要的是文件本身，别被当成文本渲染成图片
            await client.send_file_to_chat(
                message, scope, t["openid"], path, fname, mid, as_file=True
            )
            print(f"[导出] {gid} {fname}（{len(data)} bytes）", flush=True)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        return

    # 2) 查待办 / 查灵感 / 你好 / 帮助
    if text in MEMO_LIST_CMDS:
        await message.reply(content=memo_list_text(10, KIND_MEMO), msg_seq=1)
        return
    if text in IDEA_LIST_CMDS:
        await message.reply(content=memo_list_text(10, KIND_IDEA), msg_seq=1)
        return
    # 搜索灵感：按正文 / 标签 / 结论搜（@ 与非 @ 都认）
    sm = IDEA_SEARCH_RE.match(text)
    if sm:
        q = sm.group(1).strip()
        if not q:
            await message.reply(content=IDEA_SEARCH_HINT, msg_seq=1)
            return
        reply = idea_search_text(q)
        print(f"[搜索灵感] {gid} q={q!r} -> {reply.splitlines()[0][:40]!r}",
              flush=True)
        await message.reply(content=reply, msg_seq=1)
        return
    if text in HELLO_CMDS:
        await message.reply(content=intro_text(), msg_seq=1)
        return
    if text in HELP_CMDS:
        await message.reply(content=help_text(), msg_seq=1)
        return

    # 4) 记待办（@ 或不 @ 都认「待办」触发词）
    result = handle_event(t["store_id"], t["store_name"], t["sender_id"], "", raw)
    if result:
        # 🔴 待办是「明确要记」的 —— 这时才把图和它绑在一起
        pics = await collect_images_async(message, scope)
        if pics:
            _attach_images(result["id"], pics)
        print(f"[待办] {gid} -> {result['content']}"
              f"{f'（+{len(pics)} 图）' if pics else ''}", flush=True)
        ack = (_settings.get("memo_ack") or "").strip()   # 留空 = 静默记下，不回执
        if ack:
            try:
                await message.reply(content=ack, msg_seq=1)
            except Exception as e:  # 回执失败不影响已记录
                print("reply failed:", e, flush=True)
        return

    # 4.5) 兜底
    #   私聊：一切非指令文本都收作灵感（私聊没有「打扰群」的顾虑）
    #   @ 群消息：也收作灵感 —— @ 就是在**直接跟机器人说话**，按「灵感回执」回一句；
    #             没配回执就回「没认出来」，免得「@ 了却没反应」分不清是没识别还是挂了
    #   非 @ 群消息：静默落灵感，不把群聊变成机器人频道
    if scope == SCOPE_C2C:
        await _store_media(message, t, scope, urls, text.strip(), mentioned)
        return
    if mentioned:
        # 🔴 以前这里**只收图、正文直接丢**，回一句「没认出来」—— 于是「灵感回执」
        #    在群里**永远不生效**（用户 2026-10-12 报的「配了灵感回执也不生效」：
        #    私聊那条路会回执，群里这条路根本不走 idea_ack）。
        #    现在统一走 `_store_media`（正文 + 图一起收，并按需回执），
        #    没配回执时再补一句「没认出来」，行为与以前一致、但不再丢正文。
        await _store_media(message, t, scope, urls, text.strip(), mentioned)
        if not (_settings.get("idea_ack") or "").strip():
            print(f"[未识别] @消息 text={text!r}", flush=True)
            await message.reply(
                content="（没认出来这条指令，发「帮助」看菜单）", msg_seq=1
            )
        return

    # 5) 非 @ 的普通文本 → 自动收作「灵感」（静默，不打扰群）
    await _store_media(message, t, scope, urls, text.strip(), mentioned)


async def _store_media(message, t, scope, urls, text, mentioned):
    """兜底入库：把这条消息（文本 + 图）收作灵感。

    🔴 纯图片消息的 `content` 是空的 —— 但**不能丢**（用户发图就是想存），
       给一个占位文本 `[图片]` / `[图片 ×3]`，否则抽屉里是一条空白卡片。
    """
    pics = await collect_images_async(message, scope)
    body = text
    if not body:
        if not pics:
            return                                  # 没文本也没图 → 确实没东西可存
        body = f"[图片 ×{len(pics)}]" if len(pics) > 1 else "[图片]"
    if not text and not pics:
        return
    # 「# 标签」：正文按约定切给标签列（词边界之外的 # 不当标签，见 split_tags）
    body, tags = split_tags(body)
    idea = add_memo(t["store_id"], t["store_name"], t["sender_id"], "",
                    body, kind=KIND_IDEA, images=pics, tags=tags or None)
    tag = "灵感·私聊" if scope == SCOPE_C2C else "灵感"
    print(f"[{tag}] {t['key']} -> {idea['content']}"
          f"{f'（+{len(pics)} 图）' if pics else ''}", flush=True)
    if scope == SCOPE_C2C or mentioned:
        idea_ack = (_settings.get("idea_ack") or "").strip()   # 留空 = 静默收下
        if idea_ack:
            await message.reply(content=idea_ack, msg_seq=1)


def _attach_images(item_id, paths):
    """把刚落库的条目补上图片路径（东西已经存了，只补一个列）。"""
    try:
        import store as _store

        _store.set_item_images(item_id, paths)
    except Exception as e:                # noqa: BLE001  补不上也不影响正文
        print(f"[收图·挂到条目上失败] {type(e).__name__}: {e}", flush=True)



def main():
    init()
    if not acquire_pid_lock():
        sys.exit(3)
    atexit.register(release_pid_lock)
    try:
        from botpy import Client, Intents
        from botpy.message import GroupMessage
    except ImportError:
        print("未安装 botpy，无法启动真实 bot。请先：pip install -r requirements.txt")
        sys.exit(1)

    appid = os.getenv("QQ_BOT_APPID")
    # botpy 的 Client.start(appid, secret) 用的是「机器人密钥(AppSecret)」，不是 Token。
    # 兼容旧变量名 QQ_BOT_TOKEN。
    secret = os.getenv("QQ_BOT_SECRET") or os.getenv("QQ_BOT_TOKEN")
    sandbox = (os.getenv("QQ_BOT_SANDBOX") or "").strip().lower() in ("1", "true", "yes")
    if not appid or not secret:
        print("缺少 QQ_BOT_APPID / QQ_BOT_SECRET，请在 .env 中配置（见 .env.example）。")
        sys.exit(1)

    install_group_message_parser()

    class MemoClient(Client):
        async def on_ready(self):
            print(f"[botpy] 已上线：{self.robot.name}", flush=True)
            # 播报（主动推送：群内需开启「机器人主动发言」；私聊无需开关）
            # 🔴 目标由用户自己在配置中心勾选（`.env` 的 `QQ_PUSH_TARGETS`，
            #    格式 `group:<id>;c2c:<id>`，见 push.py）—— 群和私聊平级，
            #    **不默认发群**：有人就是完全不想把机器人拉进群。
            # 🔴 **第一次**连上发「功能介绍」（新手引导），之后每次上线只发「启动播报」。
            first = not onboarded_done()
            msg = (_settings.get("intro_msg" if first else "startup_msg") or "").strip()
            if first and not msg:
                # 「功能介绍」被清空 = 不做引导，退回普通播报
                first = False
                msg = (_settings.get("startup_msg") or "").strip()
            tag = "功能介绍" if first else "启动播报"
            targets = _push.targets()
            sent = 0
            for sc, oid in targets:
                if not msg:
                    break
                try:
                    import send_file as sf

                    await asyncio.to_thread(
                        sf.send_text_message, msg, oid, sandbox, appid, secret, None, 1, sc
                    )
                    print(f"[{tag}] {sc}:{oid}: {msg}", flush=True)
                    sent += 1
                except Exception as e:
                    print(f"[{tag}失败] {sc}:{oid}: {type(e).__name__}: {e}", flush=True)
            # 至少发成功一个目标才算引导完成 —— 一个都没发成，下次上线重试
            if first and sent:
                mark_onboarded()

        async def send_file_to_chat(self, message, scope, openid, full, rel, mid,
                                    caption=None, as_file=False):
            """发送一个文件到群 / 私聊。文本类渲染成图片（手机可直接看）；其余走文件卡片。

            `as_file=True` 跳过图片渲染、强制走文件卡片 —— 「导出」要的是**文件本身**
            （用户拿去存起来 / 导进别处），渲染成图就没法用了。
            """
            import send_file as sf

            size = os.path.getsize(full)
            if size > MAX_SEND:
                await message.reply(content=f"（{rel} 超过 200MB，发不了）", msg_seq=1)
                return

            # 私聊一律**被动回复**（带触发消息的 msg_id）：用户刚发来一条指令，
            # 回给「那条消息」语义更准，也不占主动消息额度（主动推送单聊是能用的，
            # 见 push.py —— 播报那条路走的就是主动推送）。
            # 群优先主动推送，失败再降级为被动回复。
            if scope == SCOPE_C2C:
                attempts = [(mid, sf.CLI_SEQ_BASE)]
            else:
                attempts = [(None, 1), (mid, sf.CLI_SEQ_BASE)]

            # 文本类：渲染成图片发送（文件卡片在手机端打不开，拆文本又太碎）
            # as_file=True（导出）例外：要的就是文件本身。
            if not as_file and sf.is_text_file(full) and size <= RENDER_MAX:
                tmpdir = tempfile.mkdtemp(prefix="memoimg_")
                try:
                    import md2img

                    imgs = await asyncio.to_thread(md2img.render, full, tmpdir)
                    cap = (caption or f"📄 {rel}") + (
                        f"（共 {len(imgs)} 张）" if len(imgs) > 1 else ""
                    )
                    last_err = None
                    for msg_id, seq in attempts:
                        try:
                            await asyncio.to_thread(
                                sf.send_images, imgs, openid, cap, sandbox, appid, secret,
                                msg_id, seq, scope,
                            )
                            print(f"[发图] {rel} -> {scope}:{openid}（{len(imgs)} 张）", flush=True)
                            return
                        except Exception as e:
                            last_err = e
                    raise last_err
                except Exception as e:
                    print(f"[发图·失败，退回文件卡片] {type(e).__name__}: {e}", flush=True)
                finally:
                    shutil.rmtree(tmpdir, ignore_errors=True)

            ft = sf.detect_file_type(full)
            last_err = None
            for msg_id, seq in attempts:
                try:
                    await asyncio.to_thread(
                        sf.send_file, full, openid, caption or f"📎 {rel}", ft,
                        sandbox, appid, secret, msg_id, seq, scope,
                    )
                    print(f"[发文件] {rel} -> {scope}:{openid}", flush=True)
                    return
                except Exception as e:
                    last_err = e
            await message.reply(
                content=f"（发送失败：{type(last_err).__name__}: {last_err}）", msg_seq=1
            )

        async def route(self, message, mentioned, scope=SCOPE_GROUP):
            # 实现在模块级 route_message()（便于回归测试直接驱动）
            await route_message(self, message, mentioned, scope)

        async def on_group_at_message_create(self, message: GroupMessage):
            # 群消息的 openid 字段名是 group_openid；author 只有 member_openid。
            await self.route(message, mentioned=True)

        async def on_group_message_create(self, message: GroupMessage):
            # 群内全量消息（需群设置「消息范围=全部消息」）
            await self.route(message, mentioned=False)

        async def on_c2c_message_create(self, message):
            # 私聊消息（QQ 开放平台需开启「单聊」）。私聊里没有 @ 概念，
            # 一律按「用户在直接跟机器人说话」处理（mentioned=True）。
            await self.route(message, mentioned=True, scope=SCOPE_C2C)

    # 群聊 + 单聊消息都对应 Intents.public_messages（不是 group_messages）
    intents = Intents(public_messages=True)
    MemoClient(intents=intents).run(appid=appid, secret=secret)


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    elif "--list" in sys.argv:
        list_mode()
    else:
        _log = _arg_value("--log")
        if _log:
            setup_file_logging(_log)
        main()
