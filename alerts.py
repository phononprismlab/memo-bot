"""账号掉线告警（v0.3.2）—— 「进程活着、账号掉线」的探活与带外提醒。

干嘛的：抓一个很阴的故障 —— **机器人进程还在跑、托盘也显示在跑，但账号其实
收不到消息了**（被风控 / 密钥被换 / 宿主网络断了）。用户侧的表现就是
「发消息机器人没反应」，而他完全看不出这是账号掉了还是代码坏了。

谁调用它：`hub.py` 的看门狗循环（那里本来就是主循环，这一层不占 bot 进程）。
对端：`bot.py` 写的链路心跳文件（见下一节）。

🔴 三条设计铁律：
  1. **告警必须带外** —— 账号掉线时 QQ 通道本身不可用，只能走系统托盘气泡。
     托盘气泡不需要任何配置，开箱即用；另可填一个 webhook 转去手机 / 群。
  2. **探活必须只读** —— 探活期间一个字都不能发给用户。账号掉线时发消息
     等于往一个报错通道里刷垃圾。
  3. **绝不误报** —— 一条假告警的代价比漏一条高得多（用户以后就不再信它了）。
     网络抖动、接口改名、进程刚起都归「不确定」，只写日志，不弹窗。

🔴 `bot.link` 的**契约定义在本模块**（写它的 bot.py 从这里 import 路径）。
   理由：文件格式的消费方在这里，判「旧成什么样」的逻辑也在这里；阈值分散两处
   早晚会漂移 —— 那种 bug 的表现是「改了告警间隔却发现根本不生效」。

配置（`settings.json`，配置中心可改）：`alert_enabled`（开关）/
`alert_probe_sec`（探活间隔）/ `alert_cooldown_min`（冷却）/
`alert_webhook`（留空 = 只用托盘）。
"""
import json
import os
import time
from urllib import request as _urlreq

from paths import data_path

import settings as _settings

# bot 侧写的链路心跳。🔴 安装版下这是用户目录里可写的文件，源码目录不带它。
LINK_FILE = data_path("bot.link")
# 告警状态：跨进程重启保留，免得管家一重启就把冷却清零、又重新刷一轮告警。
STATE_FILE = data_path("alert.state")

STATE_OK = "ok"                # 账号在线
STATE_OFFLINE = "offline"      # 进程活着但账号掉线
STATE_UNKNOWN = "unknown"      # 判不出来（进程刚起 / 网络抖动 / 接口改名）
STATE_NOBOT = "nobot"          # 机器人进程不在 —— 归管家的进程看门狗管，不告警

LINK_GRACE_SEC = 90.0          # 进程刚起来的宽限期：首次握手可能要几十秒
LINK_STALE_FACTOR = 3.0        # 「链路死了」= 超过心跳间隔的这么多倍没收到 ACK
LINK_STALE_MIN = 90.0          # 就算间隔很小，也至少给这么多秒的容忍
LINK_DEFAULT_HB = 45.0         # 从没拿到过间隔时的保守估计（实测常见值）

TITLE_OFFLINE = "摸鱼bot 掉线了"
TITLE_RECOVERED = "摸鱼bot 恢复了"
TEXT_OFFLINE = ("机器人进程还在跑，但账号已经收不到消息了（{reason}）。\n"
                "去看一下 QQ 机器人后台的设置 / 密钥，或者本机的网络。")
TEXT_RECOVERED = "账号重新在线了，可以正常收消息。"


# ── 链路心跳（读的那一半）─────────────────────────────────────────
def read_link():
    """读 bot 侧写的链路心跳 → dict / None。文件坏了当没有（不抛异常）。"""
    try:
        with open(LINK_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def link_state(link, now):
    """链路心跳旧不旧 → `(state, reason)`。纯函数，单测直接驱动。

    state ∈ `STATE_OK` / `STATE_OFFLINE`。

    🔴 为什么必须看**心跳回执**：botpy 的 `_send_heart()` 只发心跳、从不看
       ACK（op=11 在 `_is_system_event()` 里直接 `return True`）。所以 TCP
       半开时（对端没了、本地还觉得连着）心跳一直往虚空里发，botpy 永远发现
       不了、也不会重连。用户看到的就是「进程活着，但发消息机器人没反应」。
    🔴 **进程刚起有宽限期**：一上来就报警是噪声。
    """
    if not isinstance(link, dict):
        return STATE_OFFLINE, "没有链路心跳文件"
    started = link.get("started_at") or 0
    if not started:
        return STATE_OFFLINE, "链路心跳里没有启动时间"
    age = now - started
    if age < LINK_GRACE_SEC:
        return STATE_OK, f"机器人才起来 {age:.0f} 秒，宽限期内"
    # 阈值按**网关自己给的心跳间隔**算。没拿到过就按常见值估 ——
    # 宁可晚报警，也不要乱报。
    hb = float(link.get("hb_interval") or LINK_DEFAULT_HB)
    stale_after = max(hb * LINK_STALE_FACTOR, LINK_STALE_MIN)
    ack_at = link.get("ack_at") or 0
    if ack_at:
        ack_age = now - ack_at
        if ack_age > stale_after:
            return STATE_OFFLINE, f"{ack_age:.0f} 秒没收到网关心跳回执"
        return STATE_OK, "心跳正常"
    # 一个 ACK 都没收到过，但已经过了宽限期。
    if link.get("events"):
        return STATE_OK, "还没收到心跳回执，但已经有消息进来了"
    return STATE_OFFLINE, "一次网关心跳回执都没收到过"


def classify(*, now, bot_alive, link):
    """`(状态, 原因)` —— 纯函数，单测直接驱动。

    STATE_NOBOT ⇒ 交回给管家的进程看门狗（它会自动重启机器人），不重复告警。
    """
    if not bot_alive:
        return STATE_NOBOT, "机器人进程不在（管家会负责重启）"
    return link_state(link, now)


# ── 告警状态 / 冷却 ──────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state):
    """原子落盘 —— 管家随时可能被杀，不能留下半个文件当状态。"""
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass


def reset_state():
    """开机时把状态清回「在线」。

    🔴 为什么需要：`alert.state` 是跨重启保留的。管家每次启动时，机器人也是
       新起的 —— 链路心跳从上一次进程退出时就被清了，新链路还没握上手。
       这时候 `prev` 还写着上一轮的 `offline`，等它握手成功会弹出一条
       **莫名其妙**的「恢复了」。开机先清，之后的「恢复」才是真的恢复。
    """
    was = load_state()
    if was.get("state") == STATE_OK:
        return
    save_state({"state": STATE_OK, "reason": "管家重启，状态重置", "reset_at": time.time()})


def _as_float(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ── 带外通知 ─────────────────────────────────────────────────────
def notify_tray(icon, title, text):
    """托盘气泡。`icon` 是 pystray 的 Icon；为 None（没装 pystray）就只写日志。

    🔴 pystray 的 `notify` 在 Windows 上走 `Shell_NotifyIcon` 的 NIF_INFO，
       是**全局 API**，从看门狗线程跨线程调是安全的（和改图标颜色一个道理）。
    🔴 它自己会吞异常，这里再包一层是防旧版本 pystray 的签名不同。
    """
    print(f"[掉线告警] {title} {text}", flush=True)
    if icon is None:
        return False
    try:
        icon.notify(text, title)
        return True
    except Exception as e:                          # noqa: BLE001
        print(f"[掉线告警] 托盘气泡发不出去：{type(e).__name__}: {e}", flush=True)
        return False


def notify_webhook(url, payload, timeout=10):
    """往 webhook POST 一条 JSON。**失败只写日志，绝不影响机器人本身。**"""
    if not url:
        return False, "没配 webhook"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = _urlreq.Request(url, data=body, method="POST",
                          headers={"Content-Type": "application/json"})
    try:
        with _urlreq.urlopen(req, timeout=timeout) as r:     # noqa: S310
            code = getattr(r, "status", None) or r.getcode()
        return 200 <= int(code) < 300, f"HTTP {code}"
    except Exception as e:                              # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


# ── 一轮探活 ─────────────────────────────────────────────────────
def run_check(*, now=None, bot_alive=True, link=None, icon=None,
              appid=None, secret=None, sandbox=False, probe=None):
    """跑一轮探活，按需告警。返回一个 dict 供管家照着做（该弹的我已经弹了）。

    参数
    ----
    now      当前时间戳（秒），测试用
    bot_alive 机器人进程是否活着（管家给）
    link     链路心跳 dict（默认自己读；测试可注入）
    icon     pystray 实例（托盘气泡用）；None = 不弹
    appid / secret / sandbox  探活凭据（从 `.env` 拿）
    probe    注入点：`(appid, secret, sandbox) -> (state, detail)`，
             真实环境 None ⇒ 用 `send_file.probe_account`（真去请求 QQ 接口）

    🔴 **本函数不做任何 QQ 发送** —— 探活只读，告警走托盘 / webhook。
    🔴 **没有配置 appid/secret 时不报「凭据失效」**：那意味着用户还没配好，
       管家本来就会因此起不了机器人，不是「掉线」。
    """
    now = time.time() if now is None else float(now)
    if not _settings.get("alert_enabled"):
        return {"enabled": False, "state": STATE_UNKNOWN, "reason": "告警开关是关的"}
    if link is None:
        link = read_link()

    state, reason = classify(now=now, bot_alive=bot_alive, link=link)

    # 凭据层：链路正常时才有必要再问一句。链路已经断了还去问 QQ，
    # 是在拿一个注定失败的请求拖慢告警。
    cred_state, cred_detail = "unknown", "没配凭据，跳过"
    if state == STATE_OK and appid and secret:
        fn = probe or _probe_account
        cred_state, cred_detail = fn(appid, secret, sandbox)
        if cred_state == "bad":
            state, reason = STATE_OFFLINE, cred_detail

    cfg_state = load_state()
    prev = cfg_state.get("state") or STATE_OK
    cooldown = _settings.get("alert_cooldown_min") * 60.0
    last_at = _as_float(cfg_state.get("last_alert_at"), 0.0)
    kind = None

    if state == STATE_OFFLINE:
        # 🔴 冷却只压「重复提醒」；从好变坏时第一次提醒**不受冷却限制** ——
        #    否则关一次开关、间隔一次冷却，第一次告警就被吃掉了。
        first = prev != STATE_OFFLINE
        if first or (now - last_at) >= cooldown:
            kind = "offline"
            text = TEXT_OFFLINE.format(reason=reason)
            notify_tray(icon, TITLE_OFFLINE, text)
            payload = {"type": "offline", "title": TITLE_OFFLINE, "text": text,
                       "reason": reason, "at": now}
            _post_webhook(payload)
            cfg_state["last_alert_at"] = now
    elif state == STATE_OK:
        # 恢复**不受冷却限制**：好消息不怕多，而且这时候用户正在等它。
        if prev == STATE_OFFLINE:
            kind = "recovered"
            notify_tray(icon, TITLE_RECOVERED, TEXT_RECOVERED)
            _post_webhook({"type": "recovered", "title": TITLE_RECOVERED,
                           "text": TEXT_RECOVERED, "at": now})
    elif state == STATE_NOBOT and prev == STATE_OFFLINE:
        # 进程都没了，说明管家正在重启它 —— 这一轮什么都不说，等它回来。
        pass

    cfg_state["state"] = state
    cfg_state["reason"] = reason
    cfg_state["cred_state"] = cred_state
    cfg_state["cred_detail"] = cred_detail
    cfg_state["updated_at"] = now
    save_state(cfg_state)
    return {"enabled": True, "state": state, "reason": reason,
            "cred_state": cred_state, "cred_detail": cred_detail,
            "notified": kind}


def _post_webhook(payload):
    """发 webhook（配了才发）。失败只写日志 —— 绝不让它把告警流程带崩。"""
    url = (_settings.get("alert_webhook") or "").strip()
    if not url:
        return
    ok, detail = notify_webhook(url, payload)
    print(f"[掉线告警] webhook {detail}", flush=True)
