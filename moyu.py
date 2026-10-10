"""摸鱼库：群指令「摸鱼」随机抽一条，帮人放松。

**生效的库 = 数据目录里的 `moyu-user.json`**；这个文件不存在时用随代码走的内置库
`moyu-data.json`（65 条）。改法有三个，改的都是同一个文件：

  - 配置中心网页「⑨ 摸鱼库」页签（推荐：分类切着看、有计数、能一键恢复内置）
  - 直接编辑数据目录里的 `moyu-user.json`（手改也行）
  - 删掉 `moyu-user.json` = 恢复内置库

🔴 **内置的 `moyu-data.json` 只当种子，程序永远不写它** —— 源码版的数据目录就是代码
   目录，如果直接往那儿写，会把仓库里跟着版本走的库覆盖掉。

- 三类内容：relax（放松动作）/ slack（摸鱼点子）/ trivia（冷知识）。
- 防重复：把最近抽过的 id 记在 `moyu-recent.json`，抽的时候排除；
  快抽完一轮（剩余不足 n 条）就重置，从头再来。改库时这份记录会一并清空。

🔴 指令识别走「归一化 + 前缀匹配」而不是精确正则 —— 旧实现用
   `^(?:摸鱼|…)$` 锚定，导致 `摸鱼！`/`摸摸鱼`/`想摸鱼` 这类自然写法
   全部落空（实测 14 种写法只认 5 种，识别率 36%），而且失败后静默
   落进灵感库。现在统一先归一化再匹配。

用法：
  python moyu.py            抽 1 条（默认池：放松 + 摸鱼）
  python moyu.py 3          抽 3 条
  python moyu.py --cat all  三类混抽
  python moyu.py --cat trivia
  python moyu.py --stats    看库大小、用的是内置库还是自己那份
"""
import json
import os
import random
import re
import sys

from paths import data_path

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "moyu-data.json")   # 只读：内置库（种子），随代码走
USER_FILE = "moyu-user.json"                  # 可写：用户自己那份，落在数据目录
RECENT = data_path("moyu-recent.json")        # 可写：最近抽过的（见 paths.py）

RECENT_KEEP = 20  # 记住最近抽过的多少条（不重复窗口）
MAX_DRAW = 5      # 一次最多抽几条
MAX_PER_CAT = 200   # 每类最多多少条（防手滑粘进来一整本书）
MAX_TEXT_LEN = 120  # 单条最长多少字（群消息一条太长也没人看）

# 分类；顺序 = 配置中心页面上「分哪三类」的排列顺序
CATS = ("relax", "slack", "trivia")

# 分类 → 展示用图标 / 名称
CAT_ICON = {"relax": "🧘", "slack": "🐟", "trivia": "💡"}
CAT_NAME = {"relax": "放松", "slack": "摸鱼", "trivia": "冷知识"}
CAT_HELP = {
    "relax": "真能让人从椅子上站起来歇一下的动作。发「摸鱼」默认抽这类。",
    "slack": "纯摸鱼点子、划水建议。发「摸鱼」默认也抽这类。",
    "trivia": "冷知识。默认池不含它，要发「摸鱼 冷知识」才抽。",
}

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


def user_path():
    """用户自己那份摸鱼库的路径（文件不存在 = 用内置的）。"""
    return data_path(USER_FILE)


def builtin_items():
    """随代码走的内置库（只读种子）。"""
    with open(DATA, encoding="utf-8") as f:
        return json.load(f)["items"]


def _read_user():
    """读用户库 → items；文件不在 / 读坏了 / 是空的 → None。"""
    p = user_path()
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            items = json.load(f).get("items")
    except (OSError, ValueError):
        return None
    return items if isinstance(items, list) and items else None


def load_items():
    """**生效的**摸鱼库：数据目录里有用户库就用它，否则用内置库。

    🔴 用户库读坏了 / 空了 → 悄悄退回内置库，别让「摸鱼」整个哑掉。
    """
    return _read_user() or builtin_items()


def source():
    """现在用的是哪份：'user' = 用户自己改过的；'builtin' = 内置库。"""
    return "user" if _read_user() else "builtin"


def as_texts():
    """→ {cat: [文本, ...]}（配置中心页面拿去填输入框）。"""
    out = {c: [] for c in CATS}
    for it in load_items():
        c = it.get("cat")
        if c in out and isinstance(it.get("text"), str):
            out[c].append(it["text"])
    return out


def _clean(groups):
    """{cat: 文本或文本列表} → ({cat: [干净文本]}, 错误信息)。

    规则：按行拆 → 去首尾空白 → 丢空行 → 同类去重；三类不能全空；
    单条长度与每类条数有上限（防手滑粘进来一整本书）。
    """
    out = {}
    for c in CATS:
        raw = (groups or {}).get(c)
        if raw is None:
            raw = []
        lines = raw if isinstance(raw, (list, tuple)) else str(raw).splitlines()
        seen, keep = set(), []
        for ln in lines:
            t = str(ln).strip()
            if not t:
                continue
            if len(t) > MAX_TEXT_LEN:
                return None, (f"「{CAT_NAME[c]}」里有 {len(t)} 个字的一条，太长了"
                              f"（上限 {MAX_TEXT_LEN} 字）：{t[:20]}…")
            if t in seen:
                continue
            seen.add(t)
            keep.append(t)
        if len(keep) > MAX_PER_CAT:
            return None, f"「{CAT_NAME[c]}」有 {len(keep)} 条，超过上限 {MAX_PER_CAT} 条"
        out[c] = keep
    if not any(out.values()):
        return None, "三类都是空的，「摸鱼」就没东西可抽了 —— 至少留一条"
    return out, None


def save_items(groups):
    """把 {cat: 文本 / [文本]} 落成用户库 → (ok, 条数 或 错误信息)。"""
    clean, err = _clean(groups)
    if err:
        return False, err
    items = []
    for c in CATS:
        for i, t in enumerate(clean[c], 1):
            items.append({"id": f"{c}-{i:02d}", "cat": c, "text": t})
    doc = {
        "version": 1,
        "note": ("摸鱼库（你自己这份）。也可以在配置中心「⑨ 摸鱼库」页签里改。"
                 "cat: relax=放松 / slack=摸鱼 / trivia=冷知识"),
        "items": items,
    }
    p = user_path()
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except OSError as e:
        return False, f"写不进去：{type(e).__name__}: {e}"
    # 🔴 库换了 → 之前记的「抽过哪些」全部作废（id 是位置编号，换内容后含义就变了）
    _save_recent([])
    return True, len(items)


def reset_items():
    """删掉用户库 → 回到内置库 → (ok, 消息)。"""
    p = user_path()
    if not os.path.exists(p):
        return False, "本来用的就是内置库"
    try:
        os.remove(p)
    except OSError as e:
        return False, f"删不掉：{type(e).__name__}: {e}"
    _save_recent([])
    return True, "已恢复内置摸鱼库"


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
        return "（摸鱼库是空的 —— 到配置中心「摸鱼库」页签里加几条）"
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
    return {"total": len(items), "by_cat": by_cat, "recent": len(_load_recent()),
            "source": source(), "path": user_path()}


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
