"""发文件到 QQ 群（发送端，最小可用版）

用途：由本机任意调用方（你的助手 / 定时任务 / 手动）调用本脚本，
经 QQ 官方机器人把文件（图片 / 视频 / 语音 / 任意文件）发送到指定群。

实现说明：
- 直接用 QQ 官方 REST API（stdlib urllib，零第三方依赖），不依赖 botpy。
  原因：本地文件上传方法 post_group_base64file 只在未合并的 PR 里，官方发布版没有。
- 只用 HTTP，不启动 WebSocket 网关 → 可与常驻的收备忘 bot（bot.py）同时运行。

运行方式：
  python send_file.py --self-test                本地自检（不发 QQ）
  python send_file.py --file <路径> [--caption 文本] [--group <openid>]
  python send_file.py --file <路径> --passive    强制走被动回复（默认主动推送）

默认主动推送（需群设置里开启「机器人主动在群聊内发言」）；主动推送失败会自动
降级为被动回复（需 4 分钟内群里 @ 过 bot）。
"""
import argparse
import base64
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
VIDEO_EXT = {".mp4"}
VOICE_EXT = {".silk", ".mp3", ".wav", ".ogg"}
HARD_LIMIT = 200 * 1024 * 1024  # QQ 富媒体 200MB 限制

# 纯文本类：手机端文件卡片常打不开，改为「把内容当文本消息直发」
TEXT_EXT = {
    ".md", ".markdown", ".txt", ".json", ".csv", ".tsv", ".log", ".py", ".js", ".mjs",
    ".ts", ".html", ".htm", ".css", ".yml", ".yaml", ".ini", ".cfg", ".conf", ".toml",
    ".sh", ".bat", ".cmd", ".ps1", ".xml", ".env", ".sql", ".java", ".c", ".cpp", ".go", ".rs",
}
TEXT_CHUNK = 1200        # 每段字符数（QQ 群文本消息保守上限）
TEXT_MAX_CHUNKS = 8      # 超过则不拆，退回文件卡片
# 被动回复的 msg_seq 起始值：bot 自己用 1~3，这里抬高避免撞车（40054005 去重）
CLI_SEQ_BASE = 100

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_PROD = "https://api.sgroup.qq.com"
API_SANDBOX = "https://sandbox.api.sgroup.qq.com"

LAST_MSG_FILE = Path(__file__).with_name("last_group_msg.json")
PASSIVE_MAX_AGE = 240  # 被动回复有效期（秒）；超时需重新在群里 @ 一次 bot


def load_last_msg(max_age=PASSIVE_MAX_AGE):
    """读取 bot 缓存的最近一条群消息（用于被动回复）。过期或无则返回 None。"""
    if not LAST_MSG_FILE.exists():
        return None
    try:
        d = json.loads(LAST_MSG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not d.get("msg_id") or (time.time() - float(d.get("ts", 0))) > max_age:
        return None
    return d


def load_env():
    """读取同目录 .env（不依赖第三方库）。"""
    env = {}
    p = Path(__file__).with_name(".env")
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def detect_file_type(path):
    """按扩展名推断 QQ 富媒体类型：1图片 2视频 3语音 4文件。"""
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXT:
        return 1
    if ext in VIDEO_EXT:
        return 2
    if ext in VOICE_EXT:
        return 3
    return 4  # 任意格式，以「文件卡片」发送，群里可下载


def is_text_file(path):
    """是否纯文本类文件（可改为文本消息直发，手机端可直接阅读）。"""
    return Path(path).suffix.lower() in TEXT_EXT


def read_text(path):
    return Path(path).read_text(encoding="utf-8", errors="replace")


def split_text(text, size=TEXT_CHUNK):
    """把长文本切成若干段，优先按行切，避免把一行切断。"""
    chunks, cur = [], ""
    for ln in text.splitlines(keepends=True):
        while len(ln) > size:          # 单行本身超长 → 硬切
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(ln[:size])
            ln = ln[size:]
        if len(cur) + len(ln) > size:
            chunks.append(cur)
            cur = ""
        cur += ln
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c.strip()]


def _post_json(url, payload, headers=None, timeout=60):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {e.code}: {detail}") from None
    return json.loads(raw) if raw else {}


def get_access_token(appid, secret):
    data = _post_json(TOKEN_URL, {"appId": appid, "clientSecret": secret}, timeout=20)
    if "access_token" not in data:
        raise RuntimeError(f"获取 access_token 失败（检查 AppID/AppSecret）：{data}")
    return data["access_token"]


def send_file(file_path, group_openid, caption, file_type, sandbox, appid, secret, msg_id=None,
              seq_base=1):
    token = get_access_token(appid, secret)
    base = API_SANDBOX if sandbox else API_PROD
    headers = {"Authorization": f"QQBot {token}", "X-Union-Appid": appid}

    b64 = base64.b64encode(Path(file_path).read_bytes()).decode("ascii")
    # 1) 上传本地文件，拿 file_info
    up = _post_json(
        f"{base}/v2/groups/{group_openid}/files",
        {"file_type": file_type, "file_data": b64},
        headers,
    )
    file_info = up.get("file_info") or (up.get("data") or {}).get("file_info")
    if not file_info:
        raise RuntimeError(f"上传未返回 file_info：{up}")

    seq = seq_base

    def _with_reply(payload):
        # 被动回复：带上触发消息的 msg_id 与递增 msg_seq（主动消息个人 bot 无权限）
        nonlocal seq
        if msg_id:
            payload = {**payload, "msg_id": msg_id, "msg_seq": seq}
            seq += 1
        return payload

    # 2) 可选：先发一句话
    if caption:
        _post_json(
            f"{base}/v2/groups/{group_openid}/messages",
            _with_reply({"content": caption, "msg_type": 0}),
            headers,
        )
    # 3) 发送富媒体消息（msg_type=7，media 必须是 {"file_info": ...}）
    _post_json(
        f"{base}/v2/groups/{group_openid}/messages",
        _with_reply({"msg_type": 7, "media": {"file_info": file_info}}),
        headers,
    )


def send_images(paths, group_openid, caption, sandbox, appid, secret, msg_id=None, seq_base=1):
    """把多张图片作为「图片消息」(file_type=1) 依次发出。

    与 send_file 分开实现的原因：多张图要共用一个 access_token 与一段连续的 msg_seq，
    否则每次调用都重置 seq 会撞 40054005。
    """
    token = get_access_token(appid, secret)
    base = API_SANDBOX if sandbox else API_PROD
    headers = {"Authorization": f"QQBot {token}", "X-Union-Appid": appid}
    seq = seq_base

    def _send(payload):
        nonlocal seq
        if msg_id:
            payload = {**payload, "msg_id": msg_id, "msg_seq": seq}
            seq += 1
        return _post_json(f"{base}/v2/groups/{group_openid}/messages", payload, headers)

    if caption:
        _send({"content": caption, "msg_type": 0})
    for p in paths:
        b64 = base64.b64encode(Path(p).read_bytes()).decode("ascii")
        up = _post_json(
            f"{base}/v2/groups/{group_openid}/files",
            {"file_type": 1, "file_data": b64},
            headers,
        )
        file_info = up.get("file_info") or (up.get("data") or {}).get("file_info")
        if not file_info:
            raise RuntimeError(f"上传未返回 file_info：{up}")
        _send({"msg_type": 7, "media": {"file_info": file_info}})
    return len(paths)


def send_text_message(text, group_openid, sandbox, appid, secret, msg_id=None, seq=1):
    """发送一条纯文本消息。msg_id 为空即主动推送，否则为被动回复。"""
    token = get_access_token(appid, secret)
    base = API_SANDBOX if sandbox else API_PROD
    headers = {"Authorization": f"QQBot {token}", "X-Union-Appid": appid}
    payload = {"content": text, "msg_type": 0}
    if msg_id:
        payload = {**payload, "msg_id": msg_id, "msg_seq": seq}
    return _post_json(f"{base}/v2/groups/{group_openid}/messages", payload, headers)


def self_test():
    print("=== send_file 本地自检（不发 QQ）===")
    samples = {
        "a.png": 1, "b.JPG": 1, "c.gif": 1,
        "d.mp4": 2,
        "e.mp3": 3, "f.wav": 3,
        "g.zip": 4, "h.pdf": 4, "i.md": 4,
    }
    ok = True
    print("-- 文件类型识别 --")
    for name, want in samples.items():
        got = detect_file_type(name)
        good = got == want
        ok = ok and good
        print(f"  {name:8s} -> file_type={got} (期望 {want}) {'OK' if good else 'FAIL'}")

    print("-- base64 编码往返 --")
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"fishtank-file-payload")
        tmp = f.name
    b64 = base64.b64encode(Path(tmp).read_bytes()).decode()
    rt = base64.b64decode(b64) == b"fishtank-file-payload"
    ok = ok and rt
    print(f"  base64 往返: {'OK' if rt else 'FAIL'} ({len(b64)} 字符)")
    os.unlink(tmp)

    env = {**load_env(), **os.environ}
    appid = (env.get("QQ_BOT_APPID") or "").strip()
    secret = (env.get("QQ_BOT_SECRET") or env.get("QQ_BOT_TOKEN") or "").strip()
    group = (env.get("QQ_GROUP_OPENID") or "").strip()
    sandbox = (env.get("QQ_BOT_SANDBOX") or "").strip().lower() in ("1", "true", "yes")
    print("-- 凭据/配置（来自 .env 或环境变量）--")
    print(f"  QQ_BOT_APPID    : {'已配置' if appid else '缺失'}")
    print(f"  QQ_BOT_SECRET   : {'已配置' if secret else '缺失'}")
    print(f"  QQ_GROUP_OPENID : {'已配置' if group else '缺失（发送时用 --group）'}")
    print(f"  QQ_BOT_SANDBOX  : {sandbox}")
    print("=== 自检通过 ===" if ok else "=== 自检失败 ===")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="发文件到 QQ 群")
    ap.add_argument("--file", help="要发送的本地文件路径")
    ap.add_argument("--caption", default="", help="随文件一起发送的一句话（可选）")
    ap.add_argument("--group", default=None, help="目标群 openid（默认取 .env 的 QQ_GROUP_OPENID）")
    ap.add_argument("--type", type=int, default=0, help="覆盖类型 1图片/2视频/3语音/4文件（默认按扩展名）")
    ap.add_argument("--proactive", action="store_true", help="[默认] 主动推送（保留兼容，等价于不加 --passive）")
    ap.add_argument("--passive", action="store_true", help="强制走被动回复（需 bot 已缓存最近一条群消息）")
    ap.add_argument("--as-text", action="store_true", help="把文件内容当文本消息直发（手机端可直接读）")
    ap.add_argument("--as-file", action="store_true", help="强制以文件卡片发送（默认文本类文件走文本直发）")
    ap.add_argument("--self-test", action="store_true", help="本地自检，不发 QQ")
    args = ap.parse_args()

    if args.self_test:
        sys.exit(self_test())

    env = {**load_env(), **os.environ}
    appid = (env.get("QQ_BOT_APPID") or "").strip()
    secret = (env.get("QQ_BOT_SECRET") or env.get("QQ_BOT_TOKEN") or "").strip()
    group = args.group or (env.get("QQ_GROUP_OPENID") or "").strip()
    sandbox = (env.get("QQ_BOT_SANDBOX") or "").strip().lower() in ("1", "true", "yes")

    # 默认主动推送（群内开启「机器人主动发言」后可用）；--passive 则走被动回复。
    last = load_last_msg()
    msg_id = None
    if args.passive:
        if not last:
            print("⚠️ 被动回复需要 bot 已缓存最近一条群消息：请先在群里 @摸鱼bot 发一条消息，")
            print("   然后在 4 分钟内重试（或去掉 --passive 用主动推送）。")
            sys.exit(3)
        msg_id = last["msg_id"]
        group = group or last.get("group_openid", "")

    if not args.file:
        print("缺少 --file。用法：python send_file.py --file <路径> [--caption 文本] [--group <openid>]")
        sys.exit(2)
    if not Path(args.file).exists():
        print(f"文件不存在：{args.file}")
        sys.exit(2)
    size = Path(args.file).stat().st_size
    if size > HARD_LIMIT:
        print(f"文件 {size} 字节，超过 QQ 200MB 限制，无法发送。")
        sys.exit(2)
    if not appid or not secret:
        print("缺少 QQ_BOT_APPID / QQ_BOT_SECRET，请在 .env 配置（见 .env.example）。")
        sys.exit(1)
    if not group:
        print("缺少目标群 openid：请传 --group 或在 .env 配 QQ_GROUP_OPENID。")
        sys.exit(1)

    ft = args.type or detect_file_type(args.file)
    if size > 30 * 1024 * 1024:
        print(f"提示：文件较大（{size // 1024 // 1024}MB），base64 上传会慢一些。")
    mode = "被动回复" if msg_id else "主动推送"

    # 文本类文件：默认渲染成图片（文件卡片在手机端打不开，拆文本又太碎）
    if is_text_file(args.file) and not args.as_file and not args.as_text:
        try:
            import md2img

            tmpdir = tempfile.mkdtemp(prefix="mdimg_")
            imgs = md2img.render(args.file, tmpdir)
            print(f"渲染成 {len(imgs)} 张图片（{mode}）：{args.file} -> 群 {group} ...")
            try:
                send_images(
                    imgs, group, args.caption or f"📄 {Path(args.file).name}",
                    sandbox, appid, secret, msg_id, CLI_SEQ_BASE,
                )
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
            print("发送成功 ✅")
            return
        except Exception as e:
            print(f"渲染失败（{type(e).__name__}: {e}），改用文本直发…")

    if args.as_text or is_text_file(args.file):
        chunks = split_text(read_text(args.file))
        print(f"以文本直发（{mode}，{len(chunks)} 段）：{args.file} -> 群 {group} ...")
        try:
            for i, ch in enumerate(chunks):
                send_text_message(
                    ch, group, sandbox, appid, secret, msg_id,
                    (CLI_SEQ_BASE + i) if msg_id else 1,
                )
                if i < len(chunks) - 1:
                    time.sleep(1)
        except Exception as e:
            print(f"发送失败：{type(e).__name__}: {e}")
            sys.exit(1)
        print("发送成功 ✅")
        return

    print(f"发送中（{mode}）：{args.file} ({size} 字节, file_type={ft}) -> 群 {group} ...")
    try:
        send_file(args.file, group, args.caption, ft, sandbox, appid, secret, msg_id, CLI_SEQ_BASE)
    except Exception as e:
        # 主动推送失败（例如群内未开启主动发言）→ 自动降级为被动回复
        if msg_id is None and last and "40034105" in str(e):
            print("主动推送无权限（40034105），降级为被动回复…")
            group = group or last.get("group_openid", "")
            try:
                send_file(args.file, group, args.caption, ft, sandbox, appid, secret,
                          last["msg_id"], CLI_SEQ_BASE)
            except Exception as e2:
                print(f"发送失败：{type(e2).__name__}: {e2}")
                sys.exit(1)
        else:
            print(f"发送失败：{type(e).__name__}: {e}")
            sys.exit(1)
    print("发送成功 ✅")


if __name__ == "__main__":
    main()
