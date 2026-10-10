"""关机播报：往群 / 私聊发一条「下班了」。

这个文件是干嘛的：把「读配置 + 发送」从**触发方式**里拆出来，让两个触发口共用同一份逻辑。

触发口有两个（都调 `send_offwork()`）：
  1. 常驻 bot 收到 Windows 关机通知（`shutdown_hook.py`）—— **主力**。
     实测计划任务那条路会被关机流程掐掉（见下），所以由常驻进程兜住。
  2. 计划任务 `OffWorkNotify`（监听系统事件 1074）—— 兜底。
     🔴 实测：任务确实启动了（LastRunTime 有值），但返回码 0xC000026B、
     脚本一行日志都没写 —— 关机流程没给 Python 启动的机会。

谁调用它：`bot.py`（启动时装 hook）、工作区根 `offwork_notify.py`（计划任务入口）。

文案来源：`settings` 的 `offwork_msg`（网页「配置中心」和机器人「设置」改的是同一份）；
留空 = 不发。settings 内部会回退到 `.env` 的 `OFFWORK_MSG`，再回退到内置默认值。
"""
import datetime as _dt
import time

ATTEMPTS = 2          # 默认外层尝试次数（send_file 内部还有瞬时错误重试）
RETRY_WAIT = 1.0


def read_config():
    """从 .env + settings 读出 (appid, secret, 播报目标, sandbox, msg)。

    🔴 播报目标 = `[(scope, openid), ...]`，**群和私聊平级、都可多个** ——
       用户在配置中心自己勾（`.env` 的 `QQ_PUSH_TARGETS`，见 `push.py`）。
    """
    import push as _push
    import send_file
    import settings

    env = dict(send_file.load_env())
    appid = (env.get("QQ_BOT_APPID") or "").strip()
    secret = (env.get("QQ_BOT_SECRET") or "").strip()
    targets = _push.targets(env)
    sandbox = (env.get("QQ_BOT_SANDBOX") or "").strip().lower() in ("1", "true", "yes")
    msg = (settings.get("offwork_msg") or "").strip()
    return appid, secret, targets, sandbox, msg


def send_offwork(log=None, dry_run=False, attempts=ATTEMPTS, retry_wait=RETRY_WAIT):
    """发一条下班消息（**每个配了 openid 的群 / 私聊用户都发**）→ (code, 说明文本)。

    code: 0 = 全部成功 / 主动跳过；2 = 配置缺失；1 = 有目标发失败。

    关机场景请传 `attempts=1` —— 系统等窗口就那么几秒，重试反而来不及。
    """
    def _log(t):
        if log:
            try:
                log(t)
            except Exception:
                pass

    appid, secret, targets, sandbox, msg = read_config()
    missing = [n for n, v in (("QQ_BOT_APPID", appid), ("QQ_BOT_SECRET", secret)) if not v]
    if not targets:
        # 🔴 **一个目标都没勾**才算缺 —— 只勾了私聊也该能发
        #    （有用户就是完全不用群，见 push.py）
        missing.append("QQ_PUSH_TARGETS")
    if missing:
        t = "配置缺失：" + ", ".join(missing) + "（检查 .env）"
        _log(t)
        return 2, t

    if not msg:
        t = "文案是空的（设置里关掉了）→ 不发"
        _log(t)
        return 0, t

    if dry_run:
        t = "dry-run：将发送 {} -> {}".format(
            repr(msg), ", ".join(f"{sc}:{oid}" for sc, oid in targets) or "（无）")
        _log(t)
        return 0, t

    import send_file

    _log("发送 {} -> {} 个目标".format(repr(msg), len(targets)))
    failed = 0
    for sc, oid in targets:
        last = None
        sent = False
        for i in range(1, attempts + 1):
            try:
                send_file.send_text_message(
                    msg, oid, sandbox, appid, secret,
                    msg_id=None, seq=1, scope=sc,
                )
                _log("发送成功 ✅ {}:{}（第 {} 次尝试）".format(sc, oid, i))
                sent = True
                break
            except Exception as exc:                  # noqa: BLE001
                last = exc
                _log("{}:{} 第 {} 次失败：{}: {}".format(
                    sc, oid, i, type(exc).__name__, exc))
                if i < attempts:
                    time.sleep(retry_wait)
        if not sent:
            failed += 1
            _log("{}:{} 全部失败，放弃。最后错误：{}: {}".format(
                sc, oid, type(last).__name__, last))
    if failed:
        return 1, "{} 个目标发送失败".format(failed)
    return 0, "全部发送成功"


def file_log(text):
    """把一行追加到 `offwork.log`（数据目录下），同时打到 stdout。

    常驻 bot 用这个当 `send_offwork(log=…)` —— 事后能确认关机时到底发没发出去。
    """
    from paths import data_path

    line = "[{}] {}".format(_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), text)
    try:
        print(line, flush=True)               # pythonw 下 stdout 可能是 None
    except Exception:
        pass
    try:
        with open(data_path("offwork.log"), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
