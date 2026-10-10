"""摸鱼库：群指令「摸鱼」随机抽一条，帮人放松。

- 数据在 `moyu-data.json`（手改即可增删，**不用动代码**）。
- 三类内容：relax（放松动作）/ slack（摸鱼点子）/ trivia（冷知识）。
- 防重复：把最近抽过的 id 记在 `moyu-recent.json`，抽的时候排除；
  快抽完一轮（剩余不足 n 条）就重置，从头再来。

🔴 指令识别走「归一化 + 前缀匹配」而不是精确正则 —— 旧实现用
   `^(?:摸鱼|…)$` 锚定，导致 `摸鱼！`/`摸摸鱼`/`想摸鱼` 这类自然写法
   全部落空（实测 14 种写法只认 5 种，识别率 36%），而且失败后静默
   落进灵感库。现在统一先归一化再匹配。

用法：
  python moyu.py            抽 1 条（默认池：放松 + 摸鱼）
  python moyu.py 3          抽 3 条
  python moyu.py --cat all  三类混抽
  python moyu.py --cat trivia
  python moyu.py --stats    看库大小与最近抽过多少
"""
import json
import os
import random
import re
import sys

from paths import data_path

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "moyu-data.json")   # 只读：库内容随代码走
RECENT = data_path("moyu-recent.json")        # 可写：安装版落用户目录（见 paths.py）

RECENT_KEEP = 20  # 记住最近抽过的多少条（不重复窗口）
MAX_DRAW = 5      # 一次最多抽几条

# 分类 → 展示用图标 / 名称
CAT_ICON = {"relax": "🧘", "slack": "🐟", "trivia": "💡"}
CAT_NAME = {"relax": "放松", "slack": "摸鱼", "trivia": "冷知识"}

# 默认池：只放「真的能让人歇一下」的两类；冷知识要单独点
DEFAULT_CATS = ("relax", "slack")
ALL_CATS = ("relax", "slack", "trivia")

# 群里能敲的分类别名 → 分类
CAT_ALIASES = {
    "放松": ("relax",),
    "歇会": ("relax",),
    "歇会儿": ("relax",),
    "摸鱼": ("slack",),
    "划水": ("slack",),
    "冷知识": ("trivia",),
    "冷的": ("trivia",),
    "冷": ("trivia",),
    "全部": ALL_CATS,
    "混合": ALL_CATS,
    "都来": ALL_CATS,
    "随便": ALL_CATS,
    "all": ALL_CATS,
    "mix": ALL_CATS,
}

# ── 指令词（**长的必须排在短的之前**，否则「歇会」会抢掉「歇会儿」）──
HEADS = ("休息一下", "歇会儿", "歇会", "摸个鱼", "摸鱼", "放松", "划水", "休息")

# 归一化用：标点 / 空白 / 全角空格
_PUNCT = re.compile(r"[\s\u3000，。！!~～、？?…·．.,;；:：\"'“”‘’（）()\[\]【】《》<>]+")
# 前置语气词（想摸鱼 / 来点摸鱼 / 该摸鱼了）
_PREFIX = re.compile(r"^(?:我)?(?:好想|超想|想|要|来点|来个|来|去|再|该|整点|整)")
# 重复字压缩（摸摸鱼 → 摸鱼）
_DUP = re.compile(r"(.)\1+")
# 后置语气词 / 动量词（摸鱼吗 / 摸鱼吧 / 摸鱼一下 / 摸鱼一会儿）
_TAIL = re.compile(r"(?:吗|嘛|吧|呢|啊|呀|啦|了|哦|喔|哟|哈|先|下|一下|一会儿|会)$")
# 条数 + 可选量词
_NUM = re.compile(r"^(\d+)(?:条|个|次|下|只|发)?$")
_NUM_CAT = re.compile(r"^(\d+)(?:条|个|次|下|只|发)?(.+)$")


def normalize(text):
    """群消息 → 归一化指令串：去标点空白、压缩重复字、剥前置语气词。"""
    t = _PUNCT.sub("", text or "")
    t = _DUP.sub(r"\1", t)
    return _PREFIX.sub("", t, count=1)


def parse_arg(rest):
    """参数部分 → (n, cats)；无法解析返回 None。"""
    if not rest:
        return (1, None)
    m = _NUM.match(rest)
    if m:
        return (int(m.group(1)), None)
    cats = CAT_ALIASES.get(rest)
    if cats:
        return (1, cats)
    m = _NUM_CAT.match(rest)
    if m:
        c = CAT_ALIASES.get(m.group(2))
        if c:
            return (int(m.group(1)), c)
    return None


def parse(text):
    """群消息 → 摸鱼指令解析结果（三态）。

      (n, cats)  —— 识别成功
      "badarg"   —— 确实像摸鱼指令，但参数不认识（由调用方决定要不要提示）
      None       —— 不是摸鱼指令

    归一化后示例：
      摸鱼！→ 摸鱼        | 摸摸鱼 → 摸鱼      | 想摸鱼 → 摸鱼
      摸鱼吗 → 摸鱼        | 摸鱼 3 条 → 摸鱼3条 | 摸鱼！冷知识 → 摸鱼冷知识
    """
    t = normalize(text)
    if not t:
        return None
    for head in HEADS:
        if t.startswith(head):
            rest = _TAIL.sub("", t[len(head):], count=1)
            r = parse_arg(rest)
            return r if r else "badarg"
    return None


def load_items():
    with open(DATA, encoding="utf-8") as f:
        return json.load(f)["items"]


def _load_recent():
    try:
        with open(RECENT, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_recent(ids):
    try:
        with open(RECENT, "w", encoding="utf-8") as f:
            json.dump(ids, f, ensure_ascii=False)
    except OSError:
        pass


def draw(n=1, cats=None):
    """随机抽 n 条（尽量不与最近抽过的重复）→ [item, ...]

    cats=None 走默认池（放松 + 摸鱼，**不含冷知识**）；
    传 ("trivia",) 只抽冷知识，传 ALL_CATS 三类混抽。
    """
    items = load_items()
    if not items:
        return []
    cats = tuple(cats) if cats else DEFAULT_CATS
    items = [it for it in items if it.get("cat") in cats]
    if not items:
        return []
    n = max(1, min(MAX_DRAW, int(n)))
    recent = _load_recent()
    pool = [it for it in items if it["id"] not in recent]
    if len(pool) < n:  # 这一池快抽完了 → 重置窗口，从头再来
        pool = items
        recent = []
    picked = random.sample(pool, min(n, len(pool)))
    _save_recent((recent + [it["id"] for it in picked])[-RECENT_KEEP:])
    return picked


def parse_cats(word):
    """群里敲的分类词 → 分类元组；不认识返回 None（调用方当条数或报错处理）。"""
    return CAT_ALIASES.get((word or "").strip())


def format_moyu(items):
    """抽到的条目 → 群消息文本。"""
    if not items:
        return "（摸鱼库是空的）"
    if len(items) == 1:
        it = items[0]
        return f"🐟 摸鱼一下：\n{CAT_ICON.get(it.get('cat'), '·')} {it['text']}"
    lines = ["🐟 摸鱼一下："]
    for it in items:
        lines.append(f"{CAT_ICON.get(it.get('cat'), '·')} {it['text']}")
    return "\n".join(lines)


def stats():
    items = load_items()
    by_cat = {}
    for it in items:
        by_cat[it.get("cat", "?")] = by_cat.get(it.get("cat", "?"), 0) + 1
    return {"total": len(items), "by_cat": by_cat, "recent": len(_load_recent())}


def main():
    args = list(sys.argv[1:])
    if "--stats" in args:
        print(stats())
        return
    if "--parse" in args:  # 调试用：看一条群消息会被解析成什么
        i = args.index("--parse")
        print(parse(args[i + 1]) if i + 1 < len(args) else parse(""))
        return
    cats = None
    if "--cat" in args:
        i = args.index("--cat")
        if i + 1 < len(args):
            cats = CAT_ALIASES.get(args[i + 1].strip())
            del args[i:i + 2]
    n = int(args[0]) if args and args[0].isdigit() else 1
    print(format_moyu(draw(n, cats)))


if __name__ == "__main__":
    main()
