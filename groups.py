"""已发现的群 + 主动推送目标。

这个文件是干嘛的：机器人每收到一条群消息，就把 `group_openid` 记下来。
配置中心页面据此列出「这个机器人被拉进过哪些群」——
用户不用再手动拉群、翻日志、拿 AI 抓 openid，**在群里随便说一句话，页面里就会出现**。

主动推送的目标（启动播报 / 关机播报 / 发文件的主动发送）读 `.env` 的
`QQ_GROUP_OPENID`，**支持多个**（分号或逗号分隔）。

谁调用它：`bot.py`（记录 + 启动播报）、`offwork.py`（关机播报）、`config-web.py`（页面）。
"""
import json
import os
import re
import threading
import time

from paths import data_path

FILE = data_path("groups.json")
_LOCK = threading.Lock()
_RECENT = {}          # openid -> 上次落盘时间（同一个群别每条消息都写文件）
_NOTE_GAP = 300       # 秒


def parse_ids(raw=None):
    """把 `QQ_GROUP_OPENID` 解析成群 openid 列表（分号 / 逗号 / 换行分隔）。"""
    if raw is None:
        raw = os.getenv("QQ_GROUP_OPENID") or ""
    out = []
    for p in re.split(r"[;,，；\n]+", str(raw)):
        p = p.strip().strip('"').strip("'")
        if p and p not in out:
            out.append(p)
    return out


def _load():
    if not os.path.exists(FILE):
        return {}
    try:
        with open(FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    g = data.get("groups") if isinstance(data, dict) else None
    return g if isinstance(g, dict) else {}


def _save(groups):
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"groups": groups}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, FILE)


def note(openid, name=""):
    """记下「这个群里有机器人活动」。同一个群 5 分钟内只落盘一次。"""
    openid = str(openid or "").strip()
    if not openid:
        return
    now = time.time()
    with _LOCK:
        prev = _RECENT.get(openid)
        if prev and now - prev < _NOTE_GAP:
            return
        _RECENT[openid] = now
        g = _load()
        cur = g.get(openid) or {}
        g[openid] = {"name": str(name or cur.get("name") or "").strip(),
                     "last": int(now)}
        try:
            _save(g)
        except OSError:
            pass


def all_groups():
    """全部已发现的群（按最近活跃倒序）→ [{"openid", "name", "last"}]。"""
    with _LOCK:
        g = _load()
    out = [{"openid": k, "name": (v or {}).get("name", ""), "last": (v or {}).get("last", 0)}
           for k, v in g.items()]
    out.sort(key=lambda x: -x["last"])
    return out


def targets():
    """主动推送目标 = `.env` 里的 `QQ_GROUP_OPENID`（支持多个）。"""
    return parse_ids()
