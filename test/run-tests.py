"""memo-bot 回归测试（纯 stdlib，不依赖 pytest）。

用法（在 memo-bot 目录下）：
  python test/run-tests.py

覆盖：
  [1] moyu.parse     指令解析三态（识别 / 参数不认识 / 非指令）
  [2] moyu.draw      抽签、默认池、分类池、防重复、上限
  [3] store          状态机、统计、前缀查找、老库迁移
  [4] bot            文本清洗、搜索打分、动态帮助、日志轮转、PID 判活、群/私聊会话标识
  [5] send_file      文件类型识别、文本类判定、群/单聊目标路径、last_msg 兼容
  [6] md2img         依赖定位（node / 浏览器候选探测）
  [7] brain-drawer  页面生成、--serve API、--apply、防重叠源码护栏
  [8] extensions     扩展注册表、加载容错、example.py 模板不注册指令
  [9] ignore         忽略表：env/file 规则、match 双语义、四种指令、来源保护
 [10] route_message  真实路由：忽略闸门（群/私聊）、指令放行、指令分发、灵感兜底
 [11] settings       本机设置：默认值/env 回退/校验归一化/批量回滚/中文标签/指令识别
[12] config-web     配置中心：.env 按行写入（注释与顺序保留/备份）、白名单校验、
                    secret 不回显、字段表分组、页签与未配置门禁、摸鱼库页签、
                    页面护栏、文档同步
 [13] console        GBK 环境下不许把进程带崩（stdout 重定向 / 日志编码）
 [14] boomerang      回旋镖取数范围 + 导出打包
 [15] offwork        关机播报触发可靠性 + 数据目录解耦
  [16] onboarding     新手引导：「你好」自我介绍（写明无语言生成能力）、首次上线发介绍、
                     之后只发启动播报、备忘 / 灵感回执可配
  [17] moyu.library   摸鱼库可配置：内置只读种子 + 数据目录用户库、保存 / 校验 / 恢复默认、
                     读坏回退内置、改库清空「抽过哪些」

🔴 所有涉及落盘的用例都改写到临时目录，**不碰真实的 memos.db / moyu-recent.json / .env**。
"""
import ast
import importlib.util
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

# 🔴 标准输出 / 错误强制 UTF-8。中文 Windows 默认 GBK，测试输出里的 ✅ / ⚙ 等
#    字符在 GBK 下编不出来 → print 抛 UnicodeEncodeError 把整轮测试带崩。
#    errors="replace" 兜底，不让进程挂掉。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# 🔴 测试不许碰真实数据目录。`sessions.json` 是「消息路由」写的 —— route 测试用的是
#    假 openid，曾真的落进仓库里的 groups.json / c2c.json（踩过两次）。
#    这里把 `MEMO_DATA_DIR` 指到临时目录，所有走 `paths.data_path()` 的**模块级路径**
#    （push.FILE / store.DB / settings / .env …）就都落在临时目录里了。
#    ⚠️ 顺带切断了 push.py 的旧文件迁移 —— 它读 `data_path("groups.json")`，
#       不设的话测试会去读仓库里的真实 groups.json，把真实 openid 迁进断言。
_TMP_DATA = tempfile.mkdtemp(prefix="memo_test_data_")
os.environ["MEMO_DATA_DIR"] = _TMP_DATA

# 🔴 关掉「首次建库塞示例」（store._seed）。测试的临时库每个都是全新的，
#    不关的话每段用例都会多出 4 条示例，把一堆「共 N 条」的断言搅乱。
#    播种行为本身由 [3] store 段单独开一个库测（那里会临时摘掉这个变量）。
os.environ["MEMO_NO_SEED"] = "1"

FAILS = []
TOTAL = [0]


def check(name, cond, detail=""):
    TOTAL[0] += 1
    if cond:
        print(f"  \u2705 {name}")
    else:
        print(f"  \u274c {name}" + (f"   [{detail}]" if detail else ""))
        FAILS.append(name)


# ── [1] moyu.parse ──────────────────────────────────────────────
def test_moyu_parse():
    print("\n[1] moyu.parse 指令解析三态")
    import moyu

    # 旧实现只认 5/14 种自然写法（识别率 36%）；这些现在必须全部命中
    must_hit = [
        "摸鱼", "摸鱼！", "摸鱼!", "摸摸鱼", "摸鱼~", "摸鱼。", "摸鱼 。",
        "摸个鱼", "想摸鱼", "摸鱼吗", "来点摸鱼", "摸鱼吧", "摸鱼一下",
        "歇会儿", "歇会", "休息一下", "放松",
    ]
    for t in must_hit:
        got = moyu.parse(t)
        check(f"parse({t!r}) 命中", got not in (None, "badarg"), f"got={got}")

    exact = {
        "摸鱼 3": (3, None),
        "摸鱼3": (3, None),
        "摸鱼 3 条": (3, None),
        "摸鱼3条": (3, None),
        "摸鱼 冷知识": (1, ("trivia",)),
        "摸鱼！冷知识": (1, ("trivia",)),
        "摸鱼 5 冷知识": (5, ("trivia",)),
        "摸鱼 全部": (1, moyu.ALL_CATS),
        "摸鱼 放松": (1, ("relax",)),
    }
    for t, want in exact.items():
        got = moyu.parse(t)
        check(f"parse({t!r}) == {want}", got == want, f"got={got}")

    check("parse('摸鱼真爽') == badarg", moyu.parse("摸鱼真爽") == "badarg")
    check("parse('摸鱼bot真棒') == badarg", moyu.parse("摸鱼bot真棒") == "badarg")
    check("parse('今天不想摸鱼了') is None", moyu.parse("今天不想摸鱼了") is None)
    check("parse('备忘 摸鱼') is None", moyu.parse("备忘 摸鱼") is None)
    check("parse('') is None", moyu.parse("") is None)


# ── [2] moyu.draw ───────────────────────────────────────────────
def test_moyu_draw():
    print("\n[2] moyu.draw 抽签与防重复")
    import moyu

    orig_recent = moyu.RECENT
    tmpdir = tempfile.mkdtemp(prefix="moyu_test_")
    moyu.RECENT = os.path.join(tmpdir, "recent.json")
    try:
        items = moyu.load_items()
        check("内容库非空", len(items) > 0, f"n={len(items)}")
        ids = [it["id"] for it in items]
        check("id 无重复", len(ids) == len(set(ids)))

        cats = {it["cat"] for _ in range(200) for it in moyu.draw(1)}
        check("默认池只含 relax/slack（不含冷知识）",
              cats <= {"relax", "slack"}, f"got={cats}")

        for c in ("relax", "slack", "trivia"):
            got = {it["cat"] for it in moyu.draw(5, (c,))}
            check(f"分类池 {c} 不越界", got == {c}, f"got={got}")

        if os.path.exists(moyu.RECENT):
            os.remove(moyu.RECENT)
        seen = [moyu.draw(1)[0]["id"] for _ in range(12)]
        check("连抽 12 次无重复", len(set(seen)) == 12, f"unique={len(set(seen))}")

        check("MAX_DRAW 上限生效", len(moyu.draw(99)) == moyu.MAX_DRAW)
        check("recent 窗口不超过 RECENT_KEEP",
              len(moyu._load_recent()) <= moyu.RECENT_KEEP)

        # 池耗尽后应自动重置，而不是返回空
        pool = [it for it in items if it["cat"] in moyu.DEFAULT_CATS]
        for _ in range(len(pool) + 5):
            got = moyu.draw(1)
            if not got:
                break
        check("池耗尽后仍能抽出（自动重置）", bool(got))
    finally:
        moyu.RECENT = orig_recent
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── [3] store ───────────────────────────────────────────────────
def test_store():
    print("\n[3] store 状态机与迁移")
    import store

    orig_db = store.DB
    tmpdir = tempfile.mkdtemp(prefix="store_test_")
    try:
        store.DB = os.path.join(tmpdir, "t.db")
        store.init()

        a = store.add_memo("g", "gn", "s", "sn", "备忘一", kind=store.KIND_MEMO)
        b = store.add_memo("g", "gn", "s", "sn", "灵感一", kind=store.KIND_IDEA)
        check("add_memo 带 kind", a["kind"] == "memo" and b["kind"] == "idea")
        check("list_memos(memo) 只 1 条",
              len(store.list_memos(10, store.KIND_MEMO)) == 1)
        check("list_ideas(open) 只 1 条",
              len(store.list_ideas(store.STATUS_OPEN)) == 1)
        check("count_memos(idea) == 1", store.count_memos(store.KIND_IDEA) == 1)

        st = store.idea_stats()
        check("idea_stats 初始",
              st == {"open": 1, "done": 0, "decided": 0, "dropped": 0, "total": 1},
              f"got={st}")

        n = store.set_idea_status([b["id"]], store.STATUS_DECIDED, note="结论")
        check("set_idea_status 改 1 条", n == 1, f"got={n}")
        st = store.idea_stats()
        check("decided 计数 +1 / open -1",
              st["decided"] == 1 and st["open"] == 0, f"got={st}")

        got = store.find_idea(b["id"][:8])
        check("find_idea 前缀命中", bool(got) and got["id"] == b["id"])
        check("find_idea 不存在返回 None", store.find_idea("zzzzzzzz") is None)
        check("set_idea_status 空列表返回 0", store.set_idea_status([], "done") == 0)

        # 老库迁移：只有旧列
        old = os.path.join(tmpdir, "old.db")
        con = sqlite3.connect(old)
        con.execute(
            "CREATE TABLE memos (id TEXT PRIMARY KEY, group_id TEXT, content TEXT, created_at TEXT)"
        )
        con.execute("INSERT INTO memos VALUES ('x','g','旧数据','2026-01-01T00:00:00')")
        con.commit()
        con.close()
        store.DB = old
        store.init()
        rows = store.list_memos(10)
        check("迁移后旧数据保留且 kind=memo",
              len(rows) == 1 and rows[0]["kind"] == "memo", f"got={rows}")
        cols = [r[1] for r in sqlite3.connect(old).execute("PRAGMA table_info(memos)")]
        check("迁移补齐 4 个新列",
              all(c in cols for c in ("kind", "status", "note", "processed_at")),
              f"got={cols}")
        check("🔴 老库不播种（表已存在 → fresh=False，一个字节都不动）", len(rows) == 1)

        # ── 首次建库播种示例（2026-10-10）──────────────────────────
        # 测试环境默认关着（见文件顶部的 MEMO_NO_SEED）—— 这里临时摘掉，单开一个库测
        _no_seed = os.environ.pop("MEMO_NO_SEED", None)
        try:
            store.DB = os.path.join(tmpdir, "fresh.db")
            store.init()
            seeded = store.list_memos(50)
            check("🔴 全新库会塞示例（3 条灵感 + 1 条备忘）",
                  len(seeded) == 4
                  and len([r for r in seeded if r["kind"] == "idea"]) == 3
                  and len([r for r in seeded if r["kind"] == "memo"]) == 1,
                  f"got={[(r['kind'], r['content']) for r in seeded]}")
            check("示例都是待处理（打开抽屉就能看到）",
                  all(r["status"] == "open" for r in seeded))
            _all_txt = "".join(r["content"] for r in seeded)
            check("示例覆盖了「灵感怎么来 / 怎么处理 / 回旋镖 / 摸鱼」四件事",
                  "不用 @ 我" in _all_txt and "回旋镖" in _all_txt
                  and "点一下" in _all_txt and "摸鱼" in _all_txt)
            check("示例时间各错开一秒（同一秒排序不稳定）",
                  len({r["created_at"] for r in seeded}) == 4)
            check("示例标了来源（sender_name=摸鱼bot）",
                  all(r["sender_name"] == "摸鱼bot" for r in seeded))

            store.init()
            check("🔴 二次 init 不重复播种", len(store.list_memos(50)) == 4)

            store.delete_items([r["id"] for r in seeded])
            store.init()
            check("🔴 示例被删光后也不重新塞（只看「表是不是新建的」）",
                  len(store.list_memos(50)) == 0)

            os.environ["MEMO_NO_SEED"] = "1"
            store.DB = os.path.join(tmpdir, "noseed.db")
            store.init()
            check("MEMO_NO_SEED=1 关掉播种（测试 / 想要空库的人）",
                  len(store.list_memos(50)) == 0)
        finally:
            if _no_seed is not None:
                os.environ["MEMO_NO_SEED"] = _no_seed
    finally:
        store.DB = orig_db
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── [4] bot ─────────────────────────────────────────────────────
def test_bot():
    print("\n[4] bot 工具函数")
    import bot

    check("strip_at 剥 <C9> 占位符", bot.strip_at("<C9> 摸鱼") == "摸鱼",
          f"got={bot.strip_at('<C9> 摸鱼')!r}")
    check("strip_at 剥 @昵称", bot.strip_at("@摸鱼bot 摸鱼") == "摸鱼",
          f"got={bot.strip_at('@摸鱼bot 摸鱼')!r}")

    check("search_files 空查询返回 []", bot.search_files("") == [])
    # 搜索根默认是用户主目录（慢且不确定）→ 测试里临时指到仓库目录
    _orig_roots = bot.SEARCH_ROOTS
    bot.SEARCH_ROOTS = [ROOT]
    try:
        hits = bot.search_files("moyu-data")
        check("search_files 命中 moyu-data.json",
              any("moyu-data.json" in rel for rel, _ in hits),
              f"got={[r for r, _ in hits[:3]]}")
    finally:
        bot.SEARCH_ROOTS = _orig_roots

    # 🔴 排序：匹配分降序 → 同分时**修改时间倒序（最新在前）** → 再同分时路径短的在前
    _srt = tempfile.mkdtemp(prefix="sort_test_")
    _orig_roots = bot.SEARCH_ROOTS
    try:
        now = time.time()
        # 三个同分（文件名都含 hit）但 mtime 不同的文件
        for name, age in (("hit-old.txt", 3000), ("hit-new.txt", 10), ("hit-mid.txt", 600)):
            p = os.path.join(_srt, name)
            with open(p, "w", encoding="utf-8") as f:
                f.write("x")
            os.utime(p, (now - age, now - age))
        # 一个分更低（只有路径含 hit）的文件，但 mtime 最新
        os.makedirs(os.path.join(_srt, "hit-dir"))
        p = os.path.join(_srt, "hit-dir", "zzz.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("x")
        os.utime(p, (now, now))
        bot.SEARCH_ROOTS = [_srt]
        got = [rel for rel, _ in bot.search_files("hit")]
        check("同分时按 mtime 倒序（最新在前）",
              got[:3] == ["hit-new.txt", "hit-mid.txt", "hit-old.txt"], f"got={got}")
        check("时间不越权：低分文件排在所有高分之后",
              got[-1].endswith("zzz.txt"), f"got={got}")

        # 同 mtime 时用路径长度兜底（保证结果稳定、可复现）
        for name in ("hit-bbbbbbbbbb.txt", "hit-a.txt"):
            p = os.path.join(_srt, name)
            with open(p, "w", encoding="utf-8") as f:
                f.write("x")
            os.utime(p, (now, now))
        got2 = [rel for rel, _ in bot.search_files("hit")]
        check("同分同时间时路径短的在前",
              got2.index("hit-a.txt") < got2.index("hit-bbbbbbbbbb.txt"), f"got={got2}")
    finally:
        bot.SEARCH_ROOTS = _orig_roots
        shutil.rmtree(_srt, ignore_errors=True)

    # 🔴 多搜索目录（MEMO_SEARCH_ROOT 支持分号分隔的多个根）
    _r1 = tempfile.mkdtemp(prefix="multi_a_")
    _r2 = tempfile.mkdtemp(prefix="multi_b_")
    _orig_roots = bot.SEARCH_ROOTS
    try:
        with open(os.path.join(_r1, "alpha-needle.txt"), "w", encoding="utf-8") as f:
            f.write("x")
        os.makedirs(os.path.join(_r2, "sub"))
        with open(os.path.join(_r2, "sub", "beta-needle.txt"), "w", encoding="utf-8") as f:
            f.write("x")
        bot.SEARCH_ROOTS = [_r1, _r2]
        rels = [rel for rel, _ in bot.search_files("needle")]
        check("多个搜索根都生效", len(rels) == 2, f"got={rels}")
        # 根重叠（第二个是第一个的子目录）→ 不重复收
        bot.SEARCH_ROOTS = [_r2, os.path.join(_r2, "sub")]
        rels2 = [rel for rel, _ in bot.search_files("beta-needle")]
        check("重叠的搜索根不重复收", len(rels2) == 1, f"got={rels2}")
    finally:
        bot.SEARCH_ROOTS = _orig_roots
        shutil.rmtree(_r1, ignore_errors=True)
        shutil.rmtree(_r2, ignore_errors=True)

    check("parse_search_roots 分号分隔", bot.parse_search_roots("a;b") == ["a", "b"])
    check("parse_search_roots 换行分隔", bot.parse_search_roots("a\nb\n") == ["a", "b"])
    check("parse_search_roots 去重", bot.parse_search_roots("a;a") == ["a"])
    check("parse_search_roots 空 → 用户主目录",
          bot.parse_search_roots("") == [os.path.expanduser("~")])

    # 🔴 搜索可能跑几十秒（搜索根 = 整个用户目录）→ 必须丢线程，不能卡事件循环
    _src = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
    check("search_files 走 asyncio.to_thread（不卡事件循环）",
          "await asyncio.to_thread(search_files, q)" in _src)
    check("没有残留的同步 search_files 调用",
          "\n        matches = search_files(q)" not in _src)

    check("help_text 含内置指令", "备忘" in bot.help_text())
    check("help_text 追加了扩展帮助行",
          all(h in bot.help_text() for h in bot.EXT.help_lines()))

    tmpdir = tempfile.mkdtemp(prefix="bot_test_")
    try:
        p = os.path.join(tmpdir, "a.log")
        with open(p, "w", encoding="utf-8") as f:
            f.write("x" * 100)
        check("rotate_log 未超限不轮转", bot.rotate_log(p, max_bytes=1000) is False)
        check("rotate_log 超限则轮转", bot.rotate_log(p, max_bytes=10) is True)
        check("轮转产物存在", os.path.isfile(p + ".1"))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    check("_pid_alive(0) == False", bot._pid_alive(0) is False)
    check("_pid_alive(本进程) == True", bot._pid_alive(os.getpid()) is True)
    check("PID_FILE 在仓库目录下",
          os.path.dirname(bot.PID_FILE) == os.path.dirname(bot.LAST_MSG_FILE))

    # 群 / 单聊统一会话标识
    grp = SimpleNamespace(group_openid="G1", author=SimpleNamespace(member_openid="M1"))
    c2c = SimpleNamespace(author=SimpleNamespace(user_openid="U1"))
    gt = bot.chat_target(grp, bot.SCOPE_GROUP)
    check("chat_target 群：key=group:<id>", gt["key"] == "group:G1", f"got={gt}")
    check("chat_target 群：落库 id/发送目标/发送者",
          gt["store_id"] == "G1" and gt["openid"] == "G1" and gt["sender_id"] == "M1",
          f"got={gt}")
    ct = bot.chat_target(c2c, bot.SCOPE_C2C)
    check("chat_target 私聊：key=c2c:<id>", ct["key"] == "c2c:U1", f"got={ct}")
    check("chat_target 私聊：落库 group_id/name 与发送目标",
          ct["store_id"] == "c2c:U1" and ct["store_name"] == "私聊"
          and ct["openid"] == "U1" and ct["sender_id"] == "U1",
          f"got={ct}")
    check("chat_target 群/私聊 key 不撞车", gt["key"] != ct["key"])

    # save_last_msg 带 scope（写临时文件，不碰真实缓存）
    _orig_last = bot.LAST_MSG_FILE
    _tmpd = tempfile.mkdtemp(prefix="bot_lastmsg_")
    try:
        bot.LAST_MSG_FILE = os.path.join(_tmpd, "last.json")
        bot.save_last_msg("U9", "MSG9", scope="c2c")
        with open(bot.LAST_MSG_FILE, encoding="utf-8") as f:
            d = json.load(f)
        check("save_last_msg 记 scope/openid/msg_id",
              d["scope"] == "c2c" and d["openid"] == "U9" and d["msg_id"] == "MSG9",
              f"got={d}")
        check("save_last_msg 兼容 group_openid 旧读法", d.get("group_openid") == "U9")
        bot.save_last_msg("", "MSG", scope="c2c")
        check("save_last_msg 空 openid 不写", json.load(open(bot.LAST_MSG_FILE, encoding="utf-8"))["msg_id"] == "MSG9")
    finally:
        bot.LAST_MSG_FILE = _orig_last
        shutil.rmtree(_tmpd, ignore_errors=True)

    # 私聊入口的源码级护栏
    bot_src = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
    check("bot.py 注册了 on_c2c_message_create", "on_c2c_message_create" in bot_src)
    check("bot.py 私聊走 SCOPE_C2C", "scope=SCOPE_C2C" in bot_src)
    check("self_test 落盘走临时库（不污染真实 memos.db）",
          "selftest.db" in bot_src and "_store.DB = orig_db" in bot_src)

    # 私聊落库字段（handle_event 是模块级函数，可直接端到端验证）
    import store as _store
    _orig_db = _store.DB
    _tmpd2 = tempfile.mkdtemp(prefix="bot_c2c_")
    try:
        _store.DB = os.path.join(_tmpd2, "t.db")
        _store.init()
        ct = bot.chat_target(c2c, bot.SCOPE_C2C)
        r = bot.handle_event(ct["store_id"], ct["store_name"], ct["sender_id"], "",
                             "备忘 私聊测试")
        check("私聊「备忘」落库 kind=memo", bool(r) and r["kind"] == "memo", f"got={r}")
        rows = _store.list_memos(10)
        check("私聊记录 group_id=c2c:<openid> / group_name=私聊",
              rows and rows[0]["group_id"] == "c2c:U1" and rows[0]["group_name"] == "私聊",
              f"got={rows[:1]}")
        idea = _store.add_memo(ct["store_id"], ct["store_name"], ct["sender_id"], "",
                               "私聊随手一句", kind=_store.KIND_IDEA)
        check("私聊灵感 kind=idea / status=open",
              idea["kind"] == "idea"
              and _store.list_ideas(_store.STATUS_OPEN)[0]["group_id"] == "c2c:U1")
    finally:
        _store.DB = _orig_db
        shutil.rmtree(_tmpd2, ignore_errors=True)


# ── [5] send_file ───────────────────────────────────────────────
def test_send_file():
    print("\n[5] send_file 类型识别")
    import send_file as sf

    cases = {
        "a.png": 1, "b.JPG": 1, "c.gif": 1,
        "d.mp4": 2, "e.mp3": 3, "f.wav": 3,
        "g.zip": 4, "h.pdf": 4, "i.md": 4,
    }
    for fn, want in cases.items():
        got = sf.detect_file_type(fn)
        check(f"detect_file_type({fn}) == {want}", got == want, f"got={got}")

    check("is_text_file(.md) 真", sf.is_text_file("a.md") is True)
    check("is_text_file(.py) 真", sf.is_text_file("a.py") is True)
    check("is_text_file(.png) 假", not sf.is_text_file("a.png"))
    check("CLI_SEQ_BASE 避开 bot 的 1~3", sf.CLI_SEQ_BASE > 3,
          f"got={sf.CLI_SEQ_BASE}")

    # 群 / 单聊目标路径
    check("target_seg 群 → groups/<id>", sf.target_seg("group", "G1") == "groups/G1",
          f"got={sf.target_seg('group', 'G1')}")
    check("target_seg 单聊 → users/<id>", sf.target_seg("c2c", "U1") == "users/U1",
          f"got={sf.target_seg('c2c', 'U1')}")

    sf_src = open(os.path.join(ROOT, "send_file.py"), encoding="utf-8").read()
    check("send_file.py 不再写死 /v2/groups/", "/v2/groups/" not in sf_src)

    # load_last_msg：旧格式（只有 group_openid）按群处理；新格式认 scope
    _orig = sf.LAST_MSG_FILE
    _d = tempfile.mkdtemp(prefix="sf_last_")
    try:
        p = Path(_d) / "last.json"
        sf.LAST_MSG_FILE = p
        now = time.time()
        p.write_text(json.dumps({"group_openid": "G1", "msg_id": "M1", "ts": now}),
                     encoding="utf-8")
        d = sf.load_last_msg()
        check("load_last_msg 旧格式 → scope=group/openid=G1",
              d and d["scope"] == "group" and d["openid"] == "G1", f"got={d}")
        p.write_text(json.dumps({"scope": "c2c", "openid": "U1", "msg_id": "M2", "ts": now}),
                     encoding="utf-8")
        d = sf.load_last_msg()
        check("load_last_msg 新格式 → scope=c2c/openid=U1",
              d and d["scope"] == "c2c" and d["openid"] == "U1", f"got={d}")
        p.write_text(json.dumps({"scope": "c2c", "openid": "U1", "msg_id": "M2", "ts": 0}),
                     encoding="utf-8")
        check("load_last_msg 过期返回 None", sf.load_last_msg() is None)
    finally:
        sf.LAST_MSG_FILE = _orig
        shutil.rmtree(_d, ignore_errors=True)


# ── [6] md2img ──────────────────────────────────────────────────
def test_md2img():
    print("\n[6] md2img 依赖定位")
    import md2img

    node = md2img.find_node()
    check("find_node 返回存在的文件", os.path.isfile(node), f"got={node}")

    py_src = open(os.path.join(ROOT, "md2img.py"), encoding="utf-8").read()
    check("md2img.py 不再把 node 路径写成模块级常量",
          not re.search(r'^NODE\s*=\s*r?["\']C:', py_src, re.M))
    check("md2img.py 支持 MEMO_NODE", "MEMO_NODE" in py_src)

    mjs_src = open(os.path.join(ROOT, "md2png.mjs"), encoding="utf-8").read()
    py_src2 = open(os.path.join(ROOT, "md2img.py"), encoding="utf-8").read()
    # 🔴 不允许任何写死的用户主目录路径（Windows C:/Users/xxx、macOS /Users/xxx、Linux /home/xxx）
    _HOME_RE = re.compile(r'([A-Za-z]:[\\/]Users[\\/]|[\\/](?:Users|home)[\\/][^\\/]+[\\/])')
    check("md2png.mjs 不写死用户目录", not _HOME_RE.search(mjs_src))
    check("md2img.py 不写死用户目录", not _HOME_RE.search(py_src2))
    check("md2png.mjs 支持 MEMO_CHROME", "MEMO_CHROME" in mjs_src)
    check("md2png.mjs 支持 MEMO_PLAYWRIGHT_CORE", "MEMO_PLAYWRIGHT_CORE" in mjs_src)


# ── [7] brain-drawer ───────────────────────────────────────────
def _load_bubbles():
    spec = importlib.util.spec_from_file_location(
        "ib", os.path.join(ROOT, "brain-drawer.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_ideas_bubbles():
    print("\n[7] brain-drawer 页面生成与页内处理")
    import settings
    import store

    orig_db = store.DB
    _orig_set = os.environ.get("MEMO_SETTINGS_FILE")
    tmpdir = tempfile.mkdtemp(prefix="ib_test_")
    try:
        store.DB = os.path.join(tmpdir, "t.db")
        # 🔴 设置也要指到临时文件，否则会写真实的 memo-bot/settings.json
        os.environ["MEMO_SETTINGS_FILE"] = os.path.join(tmpdir, "settings.json")
        store.init()
        store.add_memo("g", "", "s", "", "测试灵感一", kind=store.KIND_IDEA)
        b = store.add_memo("g", "", "s", "", "测试灵感二", kind=store.KIND_IDEA)

        ib = _load_bubbles()

        items = ib.collect()
        check("collect 每条都带 id", all(i.get("id") for i in items), f"got={items}")
        check("collect 条数正确", len(items) == 2, f"got={len(items)}")
        check("collect 按时间正序", items[0]["t"] == "测试灵感一", f"got={items[0]['t']}")

        doc, stats = ib.build_html(items)
        check("build_html 无占位符残留",
              all(p not in doc for p in ("__DATA__", "__LABEL__", "__GEN__", "__PREVIEW__")))
        check("build_html 数据已内嵌", "测试灵感一" in doc)
        check("页面不截断正文（源码里无 PREVIEW 截断）", "PREVIEW" not in doc)
        check("stats 统计正确", stats.get("open") == 2, f"got={stats}")
        check("HTML 带 favicon 占位（避免 404 噪音）", 'rel="icon"' in doc)

        # ── 布局防重叠的源码级护栏（这些是真机踩过的坑，回归必查）
        T = ib.TEMPLATE
        py_src = open(os.path.join(ROOT, "brain-drawer.py"), encoding="utf-8").read()
        _store_src = open(os.path.join(ROOT, "store.py"), encoding="utf-8").read()
        check("所有气泡同一下降速度（BASE_DUR，避免追尾）",
              "const BASE_DUR" in T and "const dur = BASE_DUR" in T)
        check("有最小间隔门 minGapMs（保证竖直等距）", "function minGapMs" in T)
        check("挑长文本放回队首用 unshift（否则原地打转）",
              "function unPick" in T and "queue.unshift(it)" in T)
        check("长文本判据按估算高度、非固定字数阈值（已删 LONG_CHARS 常量）",
              "function estHeight" in T
              and not re.search(r"LONG_CHARS\s*=", T)
              and not re.search(r"LONG_CHARS\s*=", py_src))
        check("离线模式靠 file.protocol 前置判定（避免 CORS 红字）",
              "location.protocol !== 'file:'" in T)
        check("页脚渐隐底色（气泡从黑暗升起，不糊页脚字）",
              "linear-gradient(0deg,#080b14" in T)
        check("预热等距（按实际条目数均分），无随机抖动",
              "spawn((i + 1) / (cnt + 1))" in T and "const cnt = Math.min(cfg.maxB" in T)
        # 🔴 回归护栏：预热进度**不能从 0 开始**（`i/cnt` 的老写法）。
        #    warm=0 → animationDelay=0 → 气泡从 bottom:-10vh 往上爬，
        #    而 floatUp 前 ~9% 全在视口外 → 实测 2.3s 才露头；
        #    条目只有 1 条时就是「打开抽屉整屏空白 2.3 秒」。
        check("🔴 预热进度不从 0 开始（否则首屏要等 2.3s 才看到气泡）",
              "spawn(i / cnt)" not in T)

        # --apply：离线导出的操作
        ops_path = os.path.join(tmpdir, "ops.json")
        with open(ops_path, "w", encoding="utf-8") as f:
            json.dump({"ops": [
                {"id": b["id"], "status": "decided", "note": "测试结论"},
                {"id": "nonexistent", "status": "done"},
            ]}, f, ensure_ascii=False)
        check("apply_ops 正常返回 0", ib.apply_ops(ops_path) == 0)
        got = store.find_idea(b["id"])
        check("apply_ops 写入状态与结论",
              got["status"] == "decided" and got["note"] == "测试结论",
              f"got={got['status']}/{got['note']}")

        bad = os.path.join(tmpdir, "bad.json")
        with open(bad, "w", encoding="utf-8") as f:
            json.dump({"ops": [{"id": b["id"], "status": "bogus"}]}, f)
        ib.apply_ops(bad)
        check("apply_ops 跳过非法 status，不污染数据",
              store.find_idea(b["id"])["status"] == "decided")

        # --serve 的 HTTP API
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), ib._make_handler(doc))
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}"
        try:
            with urllib.request.urlopen(base + "/api/ping") as r:
                check("GET /api/ping ok", json.load(r).get("ok") is True)
            with urllib.request.urlopen(base + "/api/ideas") as r:
                check("GET /api/ideas 条数", len(json.load(r)["items"]) == 2)
            with urllib.request.urlopen(base + "/") as r:
                check("GET / 返回页面", len(r.read()) > 1000)

            req = urllib.request.Request(
                base + "/api/status",
                data=json.dumps({"id": b["id"], "status": "done", "note": "api"}).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req) as r:
                check("POST /api/status 写入 1 条", json.load(r).get("changed") == 1)
            check("API 写入已落库", store.find_idea(b["id"])["status"] == "done")

            for label, payload, want in [
                ("不存在的 id", {"id": "deadbeef", "status": "done"}, 404),
                ("非法 status", {"id": b["id"], "status": "bogus"}, 400),
            ]:
                try:
                    urllib.request.urlopen(urllib.request.Request(
                        base + "/api/status",
                        data=json.dumps(payload).encode(),
                        headers={"Content-Type": "application/json"}))
                    check(f"{label} 应被拒绝", False)
                except urllib.error.HTTPError as e:
                    check(f"{label} → HTTP {want}", e.code == want, f"got={e.code}")

            # 设置接口（网页「⚙ 设置」用；和机器人「设置」指令写同一份）
            with urllib.request.urlopen(base + "/api/settings") as r:
                sj = json.load(r)
            check("GET /api/settings 返回全部键",
                  sj.get("ok") is True and {k["key"] for k in sj["keys"]} == set(settings.KEYS),
                  f"got={sj}")
            sreq = urllib.request.Request(
                base + "/api/settings",
                data=json.dumps({"values": {"bubbles_show": ["open", "done"]}}).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(sreq) as r:
                check("POST /api/settings 返回 ok", json.load(r).get("ok") is True)
            check("POST /api/settings 已落库",
                  settings.get("bubbles_show") == ["open", "done"],
                  f"got={settings.get('bubbles_show')}")
            try:
                urllib.request.urlopen(urllib.request.Request(
                    base + "/api/settings",
                    data=json.dumps({"values": {"bubbles_show": ["乱写"]}}).encode(),
                    headers={"Content-Type": "application/json"}))
                check("非法设置值应被拒绝", False)
            except urllib.error.HTTPError as e:
                check("非法设置值 → HTTP 400", e.code == 400, f"got={e.code}")

            # 停止服务（页面顶部「⏻ 停止服务」用的就是这个）
            q = urllib.request.Request(base + "/api/quit", data=b"{}",
                                       headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(q) as r:
                check("POST /api/quit 返回 ok", json.load(r).get("ok") is True)
        finally:
            httpd.shutdown()
            httpd.server_close()

        # ── 视图：内容流只放「待处理」，其余进归档列表 ──────────
        d = store.add_memo("g", "", "s", "", "已丢弃的一条", kind=store.KIND_IDEA)
        store.set_idea_status([d["id"]], "dropped", "验收")
        items2 = ib.collect()
        _, stats2 = ib.build_html(items2)
        check("collect 带上了归档条目", len(items2) == 3, f"got={len(items2)}")
        check("stats 统计含 dropped", stats2.get("dropped") == 1, f"got={stats2}")
        only = ib.collect(open_only=True)
        check("collect(open_only=True) 只返回待处理",
              bool(only) and all(x["s"] == "open" for x in only), f"got={only}")

        T2 = ib.TEMPLATE
        check("内容流不再硬编码只取 open",
              "x.s === 'open'" not in T2)
        check("轮播队列只装待处理", "shuffle(streamItems().slice())" in T2)
        check("归档四个桶（含已完成的备忘）",
              "const ARCHIVE = ['memo_done', 'done', 'decided', 'dropped'];" in T2)
        check("垃圾桶文案", "🗑 垃圾桶" in T2)
        check("已处理 / 已决议 各有列表入口",
              "✅ 已处理" in T2 and "📌 已决议" in T2)
        check("列表浮层 DOM（#list / #lbody / #ltitle）",
              'id="list"' in T2 and 'id="lbody"' in T2 and 'id="ltitle"' in T2)
        check("列表行带恢复按钮", "← 恢复" in T2 and "'open'" in T2)
        check("改状态不删记录（moveItem；dropIdea 已删除）",
              "function moveItem" in T2 and "function dropIdea" not in T2)
        check("serve 模式定时拉最新（bot 新收的灵感自动进流）",
              "setInterval(refreshFromServer" in T2 and "async function refreshFromServer" in T2)
        check("停止服务入口在页面上", "/api/quit" in T2 and "btn-quit" in T2)
        check("浮层打开时不刷新（避免读的时候列表跳）",
              "modal.classList.contains('on') || listEl.classList.contains('on')" in T2)
        check("页脚区分待处理与归档", 'id="n-arch"' in T2)

        # 🔴 空库护栏（2026-10-09 修）：--serve 不能因为「没有可放的内容」就退出，
        #    否则库一空服务就静默起不来，页面都打不开（也就看不到归档）。
        empty_doc, empty_stats = ib.build_html([])
        check("空库也能生成页面", isinstance(empty_doc, str) and "streamItems" in empty_doc)
        check("空库 stats 全 0", all(v == 0 for v in empty_stats.values()), f"got={empty_stats}")
        check("--serve 下空库不硬退出", "if not items and not args.serve:" in py_src)
        check("起服务失败时报真实错误（不吞 OSError）",
              "last_err = e" in py_src and "都起不来：" in py_src)

        # ── 设置：展示范围 + 网页设置入口（2026-10-09）────────────
        check("思维源占位已删除", "思维源" not in T2 and 'class="source"' not in T2)
        check(".source / pulse 相关 CSS 已删",
              ".source{" not in T2 and "@keyframes pulse" not in T2)
        check("顶部有「⚙ 设置」入口", 'id="btn-settings"' in T2 and "⚙ 设置" in T2)
        check("设置浮层 DOM",
              'id="settings"' in T2 and 'id="sbody"' in T2 and 'id="ssave"' in T2)
        check("设置已内嵌进页面（占位符被替换）",
              "__SETTINGS__" not in doc and "let SETTINGS = [{" in doc)
        check("内容流按设置过滤", "IDEAS.filter(x => SHOW.includes(x.s))" in T2)
        check("展示范围默认「备忘 + 待处理」",
              "SHOW = settingValue('bubbles_show') || ['memo', 'open']" in T2)
        check("页脚改为 显示 / 共", '显示 <b id="n">' in T2 and '共 <b id="n-arch">' in T2)
        check("保存后重建内容流",
              "function rebuildStream" in T2 and "rebuildStream();" in T2)
        check("离线模式不允许存设置", "离线模式存不了" in T2)
        check("Esc 也关设置浮层", "settingsEl.classList.remove('on')" in T2)
        check("轮询避开设置浮层", "settingsEl.classList.contains('on')" in T2)
        # 🔴 内嵌的 __SETTINGS__ 是「渲染那一刻」的快照，服务进程却是常驻的 ——
        #    启动/刷新必须重新拉一次，否则别人（或「设置」指令）改过范围后刷新看不到变化
        check("启动时从服务端拉设置", "async function refreshSettings" in T2)
        check("serve 启动会等设置拉回来",
              "await refreshSettings()" in T2 and "await detectMode()" in T2)
        check("轮询里也同步设置", "await refreshSettings();" in T2)
        # 🔴 首屏气泡**不等网络**：__DATA__ / __SETTINGS__ 本来就内嵌在页面里，
        #    detectMode() 那两跳只是用来确认「是不是 serve 模式 / 设置有没有被改过」。
        #    放在 await 后面的话，任何一跳慢或挂住 = 开屏一个气泡都不铺
        #    （真机复现：把 /api/ping 延迟 3s → 整整 3 秒气泡总数为 0）。
        _boot = T2.index("(async ()=>{")
        _dm = T2.index("await detectMode()", _boot)
        check("首屏气泡不等网络（先铺后连）",
              T2.index("refill();", _boot) < _dm
              and T2.index("prewarm();", _boot) < _dm)
        check("设置真被改过才重建（没变就别白闪一下）",
              "JSON.stringify(SHOW) !== showBefore" in T2[_dm:_dm + 400])
        # 🔴 fetch 默认没有超时 → 服务卡住 = 无限期空白。只读 GET 统一走 apiGet 兜底
        check("只读接口有超时兜底", "function apiGet" in T2 and "AbortSignal.timeout" in T2)
        check("启动那两跳走 apiGet",
              "apiGet('/api/ping')" in T2 and "apiGet('/api/settings')" in T2)
        check("写操作不走 apiGet（中途 abort 说不清）",
              "apiGet('/api/delete')" not in T2 and "apiGet('/api/edit')" not in T2)
        # 🔴 同一条灵感不能同时出现两个气泡（用户截图：同一条 19:10 出现两次）——
        #    条目数少于 maxB 时队列会被 refill 循环复用，必须排除「已经在屏幕上的」
        check("取条目时排除在屏条目", "function onStageIds" in T2 and "on.has(it.id)" in T2)
        check("全在屏时返回 null 而不是复用", "return null;" in T2)
        check("spawn 拿到 null 就不补位", "if(!it) return;" in T2)
        check("unPick 容忍 null", "function unPick(it){ if(it)" in T2)
        check("预热按实际条目数均分（不按 maxB）",
              "Math.min(cfg.maxB, streamItems().length)" in T2)
        check("只有一处铺气泡入口（prewarm）",
              T2.count("prewarm();") == 2 and "spawn(i / boot.maxB)" not in T2)
        check("设置只提交改动过的键",
              "JSON.stringify(v) !== JSON.stringify(s.value)" in T2)
        check("没改动就不发请求", "没有改动" in T2)
        # 🔴 有结论的条目要把结论一起显示（已处理/已决议/已丢弃 常带结论）；
        #    没结论就不渲染那个节点 —— 与以前一致
        check("气泡渲染结论节点", "nt.className = 'note'" in T2 and "'结论：' + it.note" in T2)
        check("没结论就不渲染（条件判断）", "if(it.note){" in T2)
        check("结论样式已定义", ".bubble .note{" in T2)
        check("结论高度算进布局估算",
              "function noteHeight" in T2 and "noteHeight(note, widthPx)" in T2)
        check("长文本判据带上结论",
              "estHeight(it.t, laneW, it.note)" in T2
              and "estHeight(it.t, fullW, it.note)" in T2)
        check("保存设置发的是 {values:...} 契约",
              "JSON.stringify({values: vals})" in T2)

        # 🔴 换行必须保留（2026-10-09 用户发现：群里分几行写的记录，页面上全挤成一行）
        check("clean 保留换行",
              ib.clean("第一行\n第二行\n第三行") == "第一行\n第二行\n第三行",
              repr(ib.clean("第一行\n第二行\n第三行")))
        check("clean 只压空格 / 制表符，不压换行",
              ib.clean("a  \t b") == "a b" and ib.clean("a\nb") == "a\nb",
              repr(ib.clean("a  \t b")))
        check("clean 统一 CRLF / CR 换行符",
              ib.clean("a\r\nb\rc") == "a\nb\nc", repr(ib.clean("a\r\nb\rc")))
        check("clean 去掉行首尾多余空格",
              ib.clean("a   \n   b") == "a\nb", repr(ib.clean("a   \n   b")))
        check("clean 连续空行压成一个",
              ib.clean("a\n\n\n\nb") == "a\n\nb", repr(ib.clean("a\n\n\n\nb")))
        check("clean 仍会剥掉 @ 占位符",
              ib.clean("<C9> 你好") == "你好", repr(ib.clean("<C9> 你好")))
        check("clean 不再用 \\s+ 压掉换行", 're.sub(r"\\s+", " ", t)' not in T2)
        check("折行按显式换行分段算（wrappedLines）",
              "function wrappedLines" in T2 and ".split('\\n')" in T2)
        check("正文与结论都走 wrappedLines",
              "wrappedLines(text, widthPx / fp)" in T2
              and "wrappedLines(note, widthPx / NOTE_FONT)" in T2)

        # ── 备忘也上这一页（2026-10-09）───────────────────────────
        # 🔴 备忘 = 我要做，有「没完成 / 已完成 / 已丢弃」三态；灵感 = 我的想法，有四态。
        #    两条流混排在同一条内容流里，靠「桶」分流；dropped 是两类共用的桶。
        m1 = store.add_memo("g", "", "s", "", "买猫粮", kind=store.KIND_MEMO)
        store.add_memo("g", "", "s", "", "换水", kind=store.KIND_MEMO)
        memo_items = [x for x in ib.collect() if x["k"] == "memo"]
        check("备忘进了同一条流", len(memo_items) == 2, f"got={len(memo_items)}")
        check("没完成的备忘桶 = memo",
              all(x["s"] == "memo" for x in memo_items), f"got={memo_items}")
        check("备忘也会被 --open-only 带上",
              any(x["k"] == "memo" for x in ib.collect(open_only=True)))
        check("--no-memo 时流里没有备忘",
              not any(x["k"] == "memo" for x in ib.collect(include_memo=False)))
        check("bucket_of：备忘按 status 分桶、灵感按四态、乱值回落 open",
              ib.bucket_of({"kind": "memo", "status": "open"}) == "memo"
              and ib.bucket_of({"kind": "memo", "status": "done"}) == "memo_done"
              and ib.bucket_of({"kind": "idea", "status": "decided"}) == "decided"
              and ib.bucket_of({"kind": "idea", "status": "乱写"}) == "open")
        check("🔴 已丢弃的备忘进 dropped 桶（与灵感共用）",
              ib.bucket_of({"kind": "memo", "status": "dropped"}) == "dropped"
              and ib.bucket_of({"kind": "idea", "status": "dropped"}) == "dropped"
              and ib.memo_bucket("dropped") == "dropped")
        check("BUCKET_ORDER / BUCKET_LABEL 覆盖六个桶",
              set(ib.BUCKET_ORDER)
              == {"memo", "memo_done", "open", "done", "decided", "dropped"}
              and set(ib.BUCKET_LABEL) == set(ib.BUCKET_ORDER))
        check("备忘「已完成」标签（不再叫「已勾掉」）",
              ib.BUCKET_LABEL["memo_done"] == "备忘 · 已完成"
              and "勾掉" not in "".join(ib.BUCKET_LABEL.values()))
        check("备忘没有 decided 态（store.MEMO_STATUSES）",
              store.MEMO_STATUSES == ("open", "done", "dropped"))

        check("完成备忘 → 改动 1 条", store.set_memo_status([m1["id"]], "done") == 1)
        check("完成后桶变 memo_done",
              any(x["id"] == m1["id"] and x["s"] == "memo_done" for x in ib.collect()))
        check("取消完成 → 回到 memo 桶",
              store.set_memo_status([m1["id"]], "open") == 1
              and any(x["id"] == m1["id"] and x["s"] == "memo" for x in ib.collect()))
        check("丢弃备忘 → 进 dropped 桶",
              store.set_memo_status([m1["id"]], "dropped") == 1
              and any(x["id"] == m1["id"] and x["s"] == "dropped" for x in ib.collect()))
        check("已丢弃的备忘默认不进内容流",
              not any(x["id"] == m1["id"] for x in ib.collect(open_only=True)))
        check("非法 status 返回 0 不炸", store.set_memo_status([m1["id"]], "decided") == 0)
        check("改不存在的 id 返回 0 不炸", store.set_memo_status(["nope"], "done") == 0)
        check("备忘改状态碰不到灵感（kind 过滤）",
              store.set_memo_status([b["id"]], "done") == 0
              and store.set_memo_status([b["id"]], "dropped") == 0)
        check("老的 set_memo_done 仍然可用（薄包装）",
              store.set_memo_done([m1["id"]], True) == 1
              and store.find_idea(b["id"]) is not None
              and store.set_memo_done([m1["id"]], False) == 1)

        # 离线导出的操作文件：备忘条目走 kind='memo'（新写法 status，老写法 done 也认）
        ops2 = os.path.join(tmpdir, "ops2.json")
        with open(ops2, "w", encoding="utf-8") as f:
            json.dump({"ops": [{"id": m1["id"], "kind": "memo", "status": "dropped"},
                               {"id": b["id"], "status": "open", "note": "回到待处理"}]}, f,
                      ensure_ascii=False)
        check("apply_ops 支持备忘新写法（status）", ib.apply_ops(ops2) == 0)
        check("apply_ops 把备忘丢进了 dropped",
              any(x["id"] == m1["id"] and x["s"] == "dropped" for x in ib.collect()))
        check("apply_ops 同时处理灵感状态", store.find_idea(b["id"])["status"] == "open")
        ops2b = os.path.join(tmpdir, "ops2b.json")
        with open(ops2b, "w", encoding="utf-8") as f:
            json.dump({"ops": [{"id": m1["id"], "kind": "memo", "done": True}]}, f,
                      ensure_ascii=False)
        check("apply_ops 仍认老写法（done）", ib.apply_ops(ops2b) == 0
              and any(x["id"] == m1["id"] and x["s"] == "memo_done" for x in ib.collect()))
        ops2c = os.path.join(tmpdir, "ops2c.json")
        with open(ops2c, "w", encoding="utf-8") as f:
            json.dump({"ops": [{"id": m1["id"], "kind": "memo", "status": "乱写"},
                               {"id": m1["id"], "kind": "memo", "status": "decided"}]}, f,
                      ensure_ascii=False)
        check("apply_ops 拒绝备忘的非法 status", ib.apply_ops(ops2c) == 0)

        # ✏️ 离线链路也能改正文（可逆，所以不像删除那样被排除在 --apply 之外）
        _m3 = store.add_memo("g", "", "s", "", "离线编辑前", kind=store.KIND_MEMO)
        ops3 = os.path.join(tmpdir, "ops3.json")
        with open(ops3, "w", encoding="utf-8") as f:
            json.dump({"ops": [{"id": _m3["id"], "content": "离线编辑后"}]}, f,
                      ensure_ascii=False)
        check("apply_ops 支持只改正文（不带动状态）", ib.apply_ops(ops3) == 0)
        _r3 = next(x for x in ib.collect() if x["id"] == _m3["id"])
        check("离线改正文生效、桶没变（还是没完成的备忘）",
              _r3["t"] == "离线编辑后" and _r3["s"] == "memo")
        # 🔴 纯编辑（只有 content、没有 done/status）**不能**顺手把已完成的备忘改回 open
        _m4 = store.add_memo("g", "", "s", "", "已完成的备忘", kind=store.KIND_MEMO)
        store.set_memo_status([_m4["id"]], "done")
        ops3b = os.path.join(tmpdir, "ops3b.json")
        with open(ops3b, "w", encoding="utf-8") as f:
            json.dump({"ops": [{"id": _m4["id"], "kind": "memo", "content": "只改正文"}]}, f,
                      ensure_ascii=False)
        ib.apply_ops(ops3b)
        check("🔴 纯编辑不会把已完成的备忘改回 open",
              any(x["id"] == _m4["id"] and x["s"] == "memo_done" and x["t"] == "只改正文"
                  for x in ib.collect()))
        # 编辑 + 状态叠在一条 op 上：两个都要生效
        ops3c = os.path.join(tmpdir, "ops3c.json")
        with open(ops3c, "w", encoding="utf-8") as f:
            json.dump({"ops": [{"id": _m3["id"], "kind": "memo", "content": "叠着改",
                                "status": "dropped"}]}, f, ensure_ascii=False)
        ib.apply_ops(ops3c)
        check("编辑 + 改状态能叠在一条 op 上",
              any(x["id"] == _m3["id"] and x["t"] == "叠着改" and x["s"] == "dropped"
                  for x in ib.collect()))
        ops3d = os.path.join(tmpdir, "ops3d.json")
        with open(ops3d, "w", encoding="utf-8") as f:
            json.dump({"ops": [{"id": _m3["id"], "content": "   "}]}, f, ensure_ascii=False)
        check("apply_ops 空正文不炸（跳过）", ib.apply_ops(ops3d) == 0)

        # /api/memo —— 页面上的勾选框 / 浮层走这条
        httpd2 = ThreadingHTTPServer(("127.0.0.1", 0), ib._make_handler(doc))
        port2 = httpd2.server_address[1]
        threading.Thread(target=httpd2.serve_forever, daemon=True).start()
        base2 = f"http://127.0.0.1:{port2}"

        def _post(path, payload, want=200):
            """打一发 POST，返回 (状态码, json)。"""
            try:
                with urllib.request.urlopen(urllib.request.Request(
                        base2 + path, data=json.dumps(payload).encode(),
                        headers={"Content-Type": "application/json"})) as r:
                    return r.status, json.load(r)
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read() or b"{}")

        try:
            with urllib.request.urlopen(base2 + "/api/items") as r:
                check("GET /api/items 与旧 /api/ideas 同源",
                      len(json.load(r)["items"]) == len(ib.collect()))

            code, j = _post("/api/memo", {"id": m1["id"], "status": "open"})
            check("POST /api/memo 取消完成（status）", code == 200 and j.get("changed") == 1)
            check("取消完成已落库",
                  any(x["id"] == m1["id"] and x["s"] == "memo" for x in ib.collect()))

            code, j = _post("/api/memo", {"id": m1["id"], "done": True})
            check("POST /api/memo 仍认老的 done 写法",
                  code == 200 and j.get("changed") == 1
                  and any(x["id"] == m1["id"] and x["s"] == "memo_done" for x in ib.collect()))

            code, j = _post("/api/memo", {"id": m1["id"], "status": "dropped"})
            check("POST /api/memo 丢弃 → dropped 桶",
                  code == 200 and j.get("changed") == 1
                  and any(x["id"] == m1["id"] and x["s"] == "dropped" for x in ib.collect()))

            code, _ = _post("/api/memo", {"id": b["id"], "status": "dropped"})
            check("/api/memo 碰不到灵感（kind 过滤）", code == 404)

            for label, payload, want in [
                ("不存在的 id", {"id": "deadbeef", "done": True}, 404),
                ("空 id", {"id": "", "done": True}, 400),
                ("非法 status", {"id": m1["id"], "status": "decided"}, 400),
            ]:
                code, _ = _post("/api/memo", payload)
                check(f"/api/memo {label} → HTTP {want}", code == want, f"got={code}")

            # ── ✏️ 再次编辑（走 /api/edit）─────────────────────────
            medit2 = store.add_memo("g", "", "s", "", "HTTP 编辑前", kind=store.KIND_MEMO)
            d_before = next(x["d"] for x in ib.collect() if x["id"] == medit2["id"])
            code, j = _post("/api/edit", {"id": medit2["id"], "content": "HTTP 编辑后"})
            check("POST /api/edit 改正文", code == 200 and j.get("changed") == 1)
            _row = next(x for x in ib.collect() if x["id"] == medit2["id"])
            check("🔴 改完正文变了、创建时间（显示的时间）没变",
                  _row["t"] == "HTTP 编辑后" and _row["d"] == d_before)
            for label, payload, want in [
                ("空 id", {"id": "", "content": "x"}, 400),
                ("空正文", {"id": medit2["id"], "content": "   "}, 400),
                ("不存在的 id", {"id": "deadbeef", "content": "x"}, 404),
            ]:
                code, _ = _post("/api/edit", payload)
                check(f"/api/edit {label} → HTTP {want}", code == want, f"got={code}")

            # ── 永久删除（2026-10-10）──────────────────────────────
            mdel = store.add_memo("g", "", "s", "", "这条要被永久删掉",
                                  kind=store.KIND_MEMO)
            store.set_memo_status([mdel["id"]], "dropped")
            code, _ = _post("/api/delete", {"id": "deadbeef"})
            check("/api/delete 不存在的 id → 404", code == 404, f"got={code}")
            code, _ = _post("/api/delete", {"id": ""})
            check("/api/delete 空 id → 400", code == 400, f"got={code}")
            code, j = _post("/api/delete", {"id": mdel["id"]})
            check("/api/delete 真删成功", code == 200 and j.get("deleted") == 1)
            check("🔴 删完真的从库里没了",
                  not any(x["id"] == mdel["id"] for x in ib.collect()))
            code, _ = _post("/api/delete", {"id": mdel["id"]})
            check("再删同一条 → 404（幂等不炸）", code == 404, f"got={code}")
            check("delete_items 不吃空列表", store.delete_items([]) == 0
                  and store.delete_items(None) == 0)
        finally:
            httpd2.shutdown()
            httpd2.server_close()

        # store.delete_items 直接测一遍（两种 kind 都能删、返回条数）
        _dm = store.add_memo("g", "", "s", "", "备忘删我", kind=store.KIND_MEMO)
        _di = store.add_memo("g", "", "s", "", "灵感删我", kind=store.KIND_IDEA)
        check("delete_items 一次删多条（含灵感）",
              store.delete_items([_dm["id"], _di["id"]]) == 2
              and not any(x["id"] in (_dm["id"], _di["id"]) for x in ib.collect()))
        check("delete_items 不存在的 id 不计数", store.delete_items(["nope"]) == 0)

        # ── ✏️ 再次编辑正文（2026-10-10）────────────────────────────
        # 🔴 这条需求的核心是「**不改变创建时间戳**」—— 只动 content，created_at 一个字节不碰
        medit = store.add_memo("g", "", "s", "", "写错了的正文", kind=store.KIND_MEMO)
        _created = next(x["created_at"] for x in store.list_memos(500, store.KIND_MEMO)
                        if x["id"] == medit["id"])
        check("update_content 改正文 → 改动 1 条",
              store.update_content(medit["id"], "改好的正文") == 1)
        _after = next(x for x in store.list_memos(500, store.KIND_MEMO) if x["id"] == medit["id"])
        check("正文真的改了", _after["content"] == "改好的正文")
        check("🔴 创建时间不变（这条需求的核心）", _after["created_at"] == _created)
        check("update_content 不存在的 id → 0", store.update_content("nope", "x") == 0)
        check("update_content 空 id / None → 0",
              store.update_content("", "x") == 0 and store.update_content(medit["id"], None) == 0)
        check("update_content 不按 kind 过滤（灵感也能改）",
              store.update_content(b["id"], "灵感也改过了") == 1
              and store.find_idea(b["id"])["content"] == "灵感也改过了")
        _uc = _store_src.split("def update_content")[1].split("\n# ──")[0]
        _uc_body = _uc.split('"""')[-1]      # 掐掉 docstring，只看真代码
        check("🔴 update_content 的 SQL 里没有 created_at",
              "SET content=?" in _uc_body and "created_at" not in _uc_body)

        # 源码护栏：备忘在页面上是「一张带勾选框的卡片」
        check("标题已换成「脑内抽屉」",
              "🗂 脑内抽屉" in T2 and "灵感泡泡" not in T2)
        check("<title> 同步改名", "<title>脑内抽屉" in T2)
        check("备忘卡片挂勾选框",
              "function makeChk" in T2
              and "if(it.k === 'memo') hd.append(makeChk(it))" in T2)
        check("勾选框用 CSS 画（方框 + ::after 对勾），不依赖字符字形",
              ".bubble .chk{" in T2 and ".bubble .chk.on::after{" in T2)
        check("完成走 /api/memo", "fetch('/api/memo'" in T2)
        check("已完成的备忘有列表入口",
              "✓ 已完成" in T2 and 'id="btn-memo_done"' in T2)
        check("列表行也显示完成状态", "row.append(makeChk(it, renderList))" in T2)
        check("浮层对备忘换一套动作（完成 / 丢弃 / 恢复）",
              "modal.querySelectorAll('.ideaact button')" in T2
              and "modal.querySelectorAll('.memoact button')" in T2
              and 'data-memo="done"' in T2 and 'data-memo="drop"' in T2
              and 'data-memo="open"' in T2 and "function syncMemoActions" in T2)
        check("离线暂存区分备忘与灵感",
              "v.k === 'memo'" in T2 and "op.kind = 'memo'" in T2)
        check("旧导出文件名已改（抽屉操作.json）",
              "抽屉操作.json" in T2 and "灵感操作.json" not in T2)
        check("导出文件名/入口脚本名同步",
              'os.path.join(WORKSPACE, "脑内抽屉.html")' in py_src)
        check("🔴 页面上不该再出现「勾掉」",
              "勾掉" not in T2 and "勾掉" not in py_src)

        # ── 永久删除：确认弹窗 + 「以后不再提示」（2026-10-10）─────
        check("永久删除走 /api/delete",
              "fetch('/api/delete'" in T2 and 'path.startswith("/api/delete")' in py_src)
        check("确认弹窗是自定义浮层（要放勾选框，window.confirm 做不到）",
              'id="confirm"' in T2 and "function askDelete" in T2)
        check("弹窗里写明找不回来",
              "找不回来" in T2 and 'id="cquote"' in T2)
        check("弹窗带「以后不再提示」勾选框",
              'id="cnoask"' in T2 and "noAskDelete" in T2)
        check("「不再提示」存浏览器本地（不进 settings.json）",
              "bubble_del_noask_v1" in T2 and "lsSet(NOASK_KEY" in T2)
        check("弹窗默认不勾（每次都从「没勾」开始）",
              "cnoaskEl.checked = false;" in T2)
        check("设置浮层能把它勾回来",
              "function deleteConfirmItem" in T2 and "永久删除前再确认" in T2)
        check("🔴 离线模式禁用永久删除",
              "离线模式删不了" in T2 and "del.disabled = true" in T2)
        check("🔴 --apply 不接受删除操作（不可恢复的不进那条链路）",
              "不含永久删除" in py_src and '"/api/delete"' not in py_src.split("def apply_ops")[1]
              .split("def _make_handler")[0])
        check("永久删除按钮有独立的危险样式",
              "button.danger" in T2 and "永久删除" in T2)

        # ── ✏️ 再次编辑：页面入口 + 创建时间不变（2026-10-10）──────
        check("编辑走 /api/edit",
              "fetch('/api/edit'" in T2 and 'path.startswith("/api/edit")' in py_src)
        check("浮层里有「✏️ 编辑」入口", 'id="btn-edit"' in T2 and "✏️ 编辑" in T2)
        check("编辑态是独立 textarea + 保存 / 取消",
              'id="editbox"' in T2 and 'id="esave"' in T2 and 'id="ecancel"' in T2)
        check("🔴 页面写明了「创建时间不变」", "创建时间不变" in T2)
        check("归档列表行也有编辑入口", "ed.textContent = '✏️ 编辑'" in T2)
        check("🔴 编辑时空格是打字、不是暂停轮播",
              "document.activeElement !== editboxEl" in T2)
        check("Escape 先退出编辑态（再关浮层）",
              "else if(editing) setEditMode(false)" in T2)
        check("离线编辑也进 --apply（可逆，不受删除那条限制）",
              "store.update_content" in py_src.split("def apply_ops")[1]
              .split("def _make_handler")[0])
    finally:
        store.DB = orig_db
        if _orig_set is None:
            os.environ.pop("MEMO_SETTINGS_FILE", None)
        else:
            os.environ["MEMO_SETTINGS_FILE"] = _orig_set
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── [8] extensions ──────────────────────────────────────────────
def test_extensions():
    print("\n[8] 扩展机制")
    import extensions

    # 注册表：add / match / help_lines
    reg = extensions.Registry(api=SimpleNamespace(search_files=lambda q, limit=5: []))

    def h(text, mentioned):
        return "ok"

    reg.add(["测试指令", "别名"], h, help="测试指令 —— 说明")
    check("match 命中指令", reg.match("测试指令") is h)
    check("match 命中别名", reg.match("别名") is h)
    check("match 未命中返回 None", reg.match("不存在") is None)
    check("help_lines 含 help 行", "测试指令 —— 说明" in reg.help_lines())
    check("add 支持 str 名", reg.add("单名", h) is h)
    check("add 空名报错", _raises(lambda: reg.add("", h)))
    check("add 非 callable 报错", _raises(lambda: reg.add("x", 123)))
    # 后注册优先（私有扩展可覆盖公开扩展）
    reg.add(["测试指令"], lambda t, m: "v2", help="")
    check("后注册者优先", reg.match("测试指令")("", False) == "v2")

    # 加载器：正常模块注册成功；坏模块只告警不抛
    tmp = tempfile.mkdtemp(prefix="ext_test_")
    try:
        good = os.path.join(tmp, "myext.py")
        with open(good, "w", encoding="utf-8") as f:
            f.write("def register(reg):\n"
                    "    reg.add(['你好'], lambda t, m: 'hi', help='你好 —— 打招呼')\n")
        reg2 = extensions.Registry(api=None)
        extensions._register_file(good, reg2)
        check("_register_file 注册指令", reg2.match("你好") is not None)

        bad = os.path.join(tmp, "bad.py")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("raise RuntimeError('boom')\n")
        reg3 = extensions.Registry(api=None)
        extensions._register_file(bad, reg3)   # 只打印 warning，不应抛
        check("坏扩展不抛异常", reg3.commands == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 公开模板 example.py：不注册任何指令
    reg4 = extensions.Registry(api=None)
    extensions._register_file(os.path.join(ROOT, "extensions", "example.py"), reg4)
    check("example.py 模板不注册指令", reg4.commands == [])

    # load() 返回 Registry（本机可能有私有扩展，只断言类型）
    check("load() 返回 Registry",
          isinstance(extensions.load(api=None), extensions.Registry))


def _raises(fn):
    try:
        fn()
        return False
    except Exception:
        return True


# ── [9] ignore（忽略表）─────────────────────────────────────────
def test_ignore():
    print("\n[9] 忽略表")
    import ignore

    tmp = tempfile.mkdtemp(prefix="ignore_test_")
    _orig_file = os.environ.get("MEMO_IGNORE_FILE")
    _orig_env = os.environ.get("MEMO_IGNORE")
    os.environ["MEMO_IGNORE_FILE"] = os.path.join(tmp, "ignore.json")
    os.environ["MEMO_IGNORE"] = "测试机器人， 别的bot ,"
    try:
        # env 解析：中英文逗号、去空白、丢空项
        check("env_rules 解析中英逗号并去空",
              ignore.env_rules() == ["测试机器人", "别的bot"], f"got={ignore.env_rules()}")

        check("file_rules 文件不存在返回 []", ignore.file_rules() == [])

        p = ignore.file_path()
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"rules": ["A", " B ", ""]}, f, ensure_ascii=False)
        check('file_rules 认 {"rules": [...]} 并去空白',
              ignore.file_rules() == ["A", "B"], f"got={ignore.file_rules()}")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(["X", "Y"], f, ensure_ascii=False)
        check("file_rules 认裸 list",
              ignore.file_rules() == ["X", "Y"], f"got={ignore.file_rules()}")
        with open(p, "w", encoding="utf-8") as f:
            f.write("{ 坏 json")
        check("file_rules 坏文件不抛、返回 []", ignore.file_rules() == [])

        # rules()：env 优先 + 忽略大小写去重 + 保序
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"rules": ["别的BOT", "新增"]}, f, ensure_ascii=False)
        check("rules env 优先 + 忽略大小写去重",
              ignore.rules() == ["测试机器人", "别的bot", "新增"], f"got={ignore.rules()}")

        # match：子串（忽略大小写）/ 发送者 id / 未命中
        check("match 子串命中",
              ignore.match("【测试机器人】这个文件我没能收下") == "测试机器人")
        check("match 子串忽略大小写（规则里是大写）",
              ignore.match("【别的BOT】上线了") == "别的bot")
        check("match 发送者 id 完全相等",
              ignore.match("随便说点什么", sender_id="别的bot") == "别的bot")
        check("match 未命中返回 None", ignore.match("今天天气不错") is None)

        # is_command：收紧，避免「忽略这个 bug」被当成指令
        check("is_command 裸「忽略」", ignore.is_command("忽略") is True)
        check("is_command「忽略 空格」", ignore.is_command("忽略 测试机器人") is True)
        check("is_command「忽略：冒号」", ignore.is_command("忽略：xx") is True)
        check("is_command「忽略这个bug」不算", ignore.is_command("忽略这个bug") is False)
        check("is_command 普通指令不算", ignore.is_command("备忘 xx") is False)

        # 指令：列表 / 新增 / 删除 / 清空
        check("handle 裸「忽略」→ 列表", "忽略表" in ignore.handle_command("忽略"))
        check("handle「忽略 列表」→ 列表", "忽略表" in ignore.handle_command("忽略 列表"))
        r = ignore.handle_command("忽略 循环机器人")
        check("handle 新增写盘", "循环机器人" in ignore.file_rules(),
              f"got={ignore.file_rules()}")
        check("handle 新增回执含规则名", "循环机器人" in r)
        check("新增后 match 生效", ignore.match("循环机器人说了一句话") == "循环机器人")
        r = ignore.handle_command("忽略 删除 循环机器人")
        check("handle 删除移除规则", "循环机器人" not in ignore.file_rules())
        check("删除回执", "已移除" in r)

        r = ignore.handle_command("忽略 a")
        check("过短规则被拒", "太短" in r and "a" not in ignore.file_rules(), f"got={r!r}")

        r = ignore.handle_command("忽略 删除 测试机器人")
        check("env 规则指令删不掉",
              "MEMO_IGNORE" in r and "测试机器人" in ignore.rules())

        ignore.handle_command("忽略 加一条")
        ignore.handle_command("忽略 清空")
        check("清空只清 ignore.json，env 仍在",
              ignore.file_rules() == [] and ignore.rules() == ["测试机器人", "别的bot"],
              f"got={ignore.rules()}")
    finally:
        if _orig_file is None:
            os.environ.pop("MEMO_IGNORE_FILE", None)
        else:
            os.environ["MEMO_IGNORE_FILE"] = _orig_file
        if _orig_env is None:
            os.environ.pop("MEMO_IGNORE", None)
        else:
            os.environ["MEMO_IGNORE"] = _orig_env
        shutil.rmtree(tmp, ignore_errors=True)

    # bot.py 接线护栏
    import bot
    bot_src = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
    check("bot.py 接入了忽略闸门", "_ignore.match(" in bot_src)
    check("bot.py 放行「忽略」指令", "_ignore.is_command(" in bot_src)
    check("帮助菜单含「忽略」", "忽略" in bot.help_text())


# ── [10] route_message（真实路由）───────────────────────────────
def test_route():
    print("\n[10] route_message 路由（忽略闸门 / 指令分发端到端）")
    import asyncio
    import bot
    import ignore
    import store

    class Msg:
        """假消息：字段名与 botpy 的 GroupMessage / C2CMessage 对齐。"""

        def __init__(self, content, gid="G1", mid="MSG1", sid="M1", uid=None):
            self.content = content
            self.id = mid
            self.group_openid = gid
            self.author = SimpleNamespace(member_openid=sid, user_openid=uid)
            self.replies = []

        async def reply(self, content=None, msg_seq=1):
            self.replies.append(content)

    class Client:
        async def send_file_to_chat(self, *a, **k):
            raise AssertionError("本用例不应走到发文件分支")

    tmp = tempfile.mkdtemp(prefix="route_test_")
    _env = {k: os.environ.get(k)
            for k in ("MEMO_IGNORE", "MEMO_IGNORE_FILE", "MEMO_SETTINGS_FILE")}
    _orig_db, _orig_last = store.DB, bot.LAST_MSG_FILE
    os.environ["MEMO_IGNORE"] = "测试机器人"
    os.environ["MEMO_IGNORE_FILE"] = os.path.join(tmp, "ignore.json")
    os.environ["MEMO_SETTINGS_FILE"] = os.path.join(tmp, "settings.json")
    store.DB = os.path.join(tmp, "t.db")
    bot.LAST_MSG_FILE = os.path.join(tmp, "last.json")
    try:
        store.init()
        cli = Client()

        def run(msg, mentioned=True, scope=bot.SCOPE_GROUP):
            asyncio.run(bot.route_message(cli, msg, mentioned, scope))

        # 1) 命中忽略规则 → 静默跳过（不收灵感、不回复）
        m = Msg("【测试机器人】这个文件我没能收下")
        run(m)
        check("命中忽略：不回复", m.replies == [], f"got={m.replies}")
        check("命中忽略：不收灵感", store.list_ideas(store.STATUS_OPEN) == [])

        # 2) 「忽略」指令本身必须放行（否则会被自己的规则吃掉）
        m = Msg("忽略 列表")
        run(m)
        check("「忽略 列表」有回复", bool(m.replies) and "忽略表" in m.replies[0],
              f"got={m.replies}")
        check("「忽略」指令不落库", store.list_ideas(store.STATUS_OPEN) == [])

        # 3) 指令新增规则 → 立刻生效
        m = Msg("忽略 循环机器人")
        run(m)
        check("指令新增规则写盘", "循环机器人" in ignore.file_rules(),
              f"got={ignore.file_rules()}")
        m = Msg("循环机器人说：大家好")
        run(m, mentioned=False)
        check("新规则立即拦下消息",
              m.replies == [] and store.list_ideas(store.STATUS_OPEN) == [],
              f"replies={m.replies}")

        # 4) 非 @ 的普通群消息 → 静默收作灵感
        m = Msg("下周三要交个东西")
        run(m, mentioned=False)
        ideas = store.list_ideas(store.STATUS_OPEN)
        check("非 @ 普通消息静默收作灵感",
              len(ideas) == 1 and ideas[0]["kind"] == "idea", f"got={ideas}")
        check("非 @ 普通消息不回复", m.replies == [], f"got={m.replies}")

        # 5) 「备忘」→ 落库 + 回执
        m = Msg("备忘 明天确认最小实例数")
        run(m)
        memos = store.list_memos(10, store.KIND_MEMO)
        check("「备忘」落库 kind=memo",
              bool(memos) and memos[0]["kind"] == "memo", f"got={memos[:1]}")
        check("「备忘」有回执（文案来自设置项 memo_ack）",
              bool(m.replies) and m.replies[0] == bot._settings.get("memo_ack")
              and m.replies[0] == "已经放进脑内抽屉了", f"got={m.replies}")

        # 6) @ 场景未识别 → 明确提示
        m = Msg("这是一句没头没脑的话")
        run(m)
        check("@ 未识别给提示", bool(m.replies) and "没认出来" in m.replies[0],
              f"got={m.replies}")

        # 7) 私聊命中忽略 → 同样静默，不收灵感
        n_before = len(store.list_ideas(store.STATUS_OPEN))
        m = Msg("【测试机器人】私聊里也一样", uid="U1")
        run(m, mentioned=True, scope=bot.SCOPE_C2C)
        check("私聊命中忽略：静默", m.replies == [], f"got={m.replies}")
        check("私聊命中忽略：不收灵感",
              len(store.list_ideas(store.STATUS_OPEN)) == n_before)

        # 8) 「设置」指令走通真实路由（与网页「⚙ 设置」写同一份文件）
        n_ideas = len(store.list_ideas(store.STATUS_OPEN))
        m = Msg("设置")
        run(m)
        check("「设置」列全部键",
              bool(m.replies) and "关机通知文案" in m.replies[0]
              and "脑内抽屉展示范围" in m.replies[0], f"got={m.replies}")
        check("「设置」指令不落库灵感",
              len(store.list_ideas(store.STATUS_OPEN)) == n_ideas)

        m = Msg("设置 关机通知文案 收工，明天见")
        run(m)
        check("「设置 <键> <值>」落盘",
              bot._settings.get("offwork_msg") == "收工，明天见",
              f"got={bot._settings.get('offwork_msg')!r}")
        check("改完有回执", bool(m.replies) and "收工，明天见" in m.replies[0],
              f"got={m.replies}")

        m = Msg("设置 bubbles_show 待处理,已处理")
        run(m)
        check("multi 键认中文标签",
              bot._settings.get("bubbles_show") == ["open", "done"],
              f"got={bot._settings.get('bubbles_show')}")

        m = Msg("设置 bubbles_show 乱写")
        run(m)
        check("非法值被拒且给提示",
              bot._settings.get("bubbles_show") == ["open", "done"]
              and bool(m.replies) and "认不出" in m.replies[0], f"got={m.replies}")

        # 9) 不是指令的普通话（「设置好了」）不能被吃掉
        m = Msg("设置好了")
        run(m, mentioned=False)
        ideas2 = store.list_ideas(store.STATUS_OPEN)
        check("「设置好了」当普通灵感收下",
              len(ideas2) == n_ideas + 1 and m.replies == [], f"got={m.replies}")

        # 10) 🔴 慢搜索不能卡事件循环 —— 搜索根是百万级目录时一次要几十秒，
        #     同步跑会把 bot 冻住（收不到消息、心跳停摆）。用慢速假搜索驱动真实路由，
        #     期间起一个 50ms 的 ticker，看它还能不能跑。
        _real_search = bot.search_files
        try:
            def _slow_search(q, limit=bot.SEARCH_LIMIT):
                time.sleep(0.6)
                return [("a.md", "C:/x/a.md")]
            bot.search_files = _slow_search

            class _OkClient:
                sent = []

                async def send_file_to_chat(self, *a, **k):
                    _OkClient.sent.append(a)

            async def _probe():
                ticks = 0

                async def ticker():
                    nonlocal ticks
                    while True:
                        await asyncio.sleep(0.05)
                        ticks += 1

                t = asyncio.create_task(ticker())
                mm = Msg("发文件 测试")
                await bot.route_message(_OkClient(), mm, True, bot.SCOPE_GROUP)
                t.cancel()
                return ticks, mm.replies

            ticks, replies = asyncio.run(_probe())
            check("慢搜索期间事件循环仍可调度（不卡心跳）", ticks >= 5,
                  f"0.6s 内只跑了 {ticks} 次 ticker")
            check("慢搜索后唯一命中仍直接发出（不回列表）",
                  len(_OkClient.sent) == 1 and replies == [],
                  f"sent={len(_OkClient.sent)} replies={replies}")
        finally:
            bot.search_files = _real_search
    finally:
        store.DB, bot.LAST_MSG_FILE = _orig_db, _orig_last
        for k, v in _env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)


# ── [11] settings（本机设置：网页 + 机器人指令共用）──────────────
def test_settings():
    print("\n[11] settings 本机设置（网页「⚙ 设置」+ 机器人「设置」指令）")
    import settings

    tmpdir = tempfile.mkdtemp(prefix="settings_test_")
    orig = os.environ.get("MEMO_SETTINGS_FILE")
    os.environ["MEMO_SETTINGS_FILE"] = os.path.join(tmpdir, "settings.json")
    try:
        check("默认值：三个键都在",
              set(settings.all_values()) == set(settings.KEYS), f"got={settings.all_values()}")
        # 🔴 面板与指令列表都按 KEYS 的顺序排 —— 用户要求「启动播报」在「关机通知文案」之前
        _order = [x["key"] for x in settings.describe()]
        check("设置项顺序：启动播报在关机通知文案之前",
              _order.index("startup_msg") < _order.index("offwork_msg"),
              f"got={_order}")
        check("展示范围排在两个文案之后",
              _order.index("bubbles_show") > _order.index("offwork_msg"),
              f"got={_order}")
        # 🔴 三键新增：功能介绍 / 备忘回执 / 灵感回执（都是「回复文案可配」的一部分）
        check("新增三键都在",
              {"intro_msg", "memo_ack", "idea_ack"} <= set(settings.KEYS))
        check("功能介绍是多行文本（kind=area）",
              settings.KEYS["intro_msg"]["kind"] == "area")
        check("功能介绍默认值写明「没有语言生成能力」",
              "没有语言生成能力" in settings.get("intro_msg"))
        check("备忘回执默认「已经放进脑内抽屉了」",
              settings.get("memo_ack") == "已经放进脑内抽屉了")
        check("灵感回执默认留空（= 静默收下，保持原有行为）",
              settings.get("idea_ack") == "", f"got={settings.get('idea_ack')!r}")
        check("新增三键都还没显式写过",
              all(settings.is_set(k) is False
                  for k in ("intro_msg", "memo_ack", "idea_ack")))
        check("三键排在关机通知文案之后、展示范围之前",
              _order.index("offwork_msg") < _order.index("intro_msg")
              < _order.index("memo_ack") < _order.index("idea_ack")
              < _order.index("bubbles_show"), f"got={_order}")
        check("默认展示范围是「备忘 + 待处理」", settings.get("bubbles_show") == ["memo", "open"])
        check("展示范围标签：memo_done 叫「已完成」（不再叫「已勾掉」）",
              settings.KEYS["bubbles_show"]["choice_labels"]["memo_done"] == "已完成")
        check("没写过文件时 is_set 为假", settings.is_set("startup_msg") is False)
        check("文件还不存在", not os.path.exists(os.environ["MEMO_SETTINGS_FILE"]))

        ok, info = settings.set_value("startup_msg", "  起来干活  ")
        check("set text 会去空白", ok and info == "起来干活", f"got={info!r}")
        check("写入后 is_set 为真", settings.is_set("startup_msg") is True)
        check("文件已落盘", os.path.exists(os.environ["MEMO_SETTINGS_FILE"]))

        # area（多行）与可清空的回执
        ok, info = settings.set_value("intro_msg", "第一行\n第二行\n")
        check("area 保留内部换行、只去首尾空白",
              ok and info == "第一行\n第二行", f"got={info!r}")
        ok, info = settings.set_value("memo_ack", "  收好了  ")
        check("备忘回执可改（去空白）", ok and info == "收好了", f"got={info!r}")
        ok, info = settings.set_value("memo_ack", "")
        check("备忘回执可以清空（= 静默记下）", ok and info == "", f"got={info!r}")

        ok, info = settings.set_value("bubbles_show", "待处理, done")
        check("multi 认中文标签 + code", ok and info == ["open", "done"], f"got={info}")
        ok, info = settings.set_value("bubbles_show", ["open", "open", "decided"])
        check("multi 去重", ok and info == ["open", "decided"], f"got={info}")

        ok, info = settings.set_value("bubbles_show", ["乱写"])
        check("multi 非法值被拒", ok is False and "认不出" in info, f"got={info}")
        ok, info = settings.set_value("bubbles_show", "")
        check("multi 空值被拒", ok is False, f"got={info}")
        ok, info = settings.set_value("不存在的键", "x")
        check("未知键被拒", ok is False and "没有这个设置项" in info, f"got={info}")
        check("被拒后值没变", settings.get("bubbles_show") == ["open", "decided"])

        # 批量写：先全量校验，再一次性落盘（一个坏 → 整批不写）
        n, errs = settings.apply_values(
            {"startup_msg": "AAA", "bubbles_show": ["bogus"]})
        check("批量写：有错就整批不写",
              n == 0 and bool(errs) and settings.get("startup_msg") == "起来干活", f"got={errs}")
        n, errs = settings.apply_values(
            {"startup_msg": "BBB", "bubbles_show": ["open", "dropped"]})
        check("批量写：全对才落盘", n == 2 and not errs, f"got={n},{errs}")

        ok, info = settings.reset("startup_msg")
        check("reset 后 is_set 为假", ok and settings.is_set("startup_msg") is False)
        check("reset 后取值等于回退值", info == settings.get("startup_msg") and bool(info))

        check("is_command 裸「设置」", settings.is_command("设置") is True)
        check("is_command「配置」也算", settings.is_command("配置") is True)
        check("is_command「设置 x y」", settings.is_command("设置 关机通知文案 早") is True)
        check("is_command「设置好了」不算", settings.is_command("设置好了") is False)
        check("is_command「配置一下」不算", settings.is_command("配置一下") is False)
        check("is_command 普通聊天不算", settings.is_command("今天天气不错") is False)

        txt = settings.handle_command("设置")
        check("「设置」列出全部键",
              all(k["label"] in txt for k in settings.KEYS.values()), f"got={txt[:80]}")
        check("「设置」带改法提示", "设置 <键> <值>" in txt)

        settings.handle_command("设置 关机通知文案 我要下班了")
        check("用中文标签也能改", settings.get("offwork_msg") == "我要下班了")
        settings.handle_command("设置 offwork_msg 明天见")
        check("用键名也能改", settings.get("offwork_msg") == "明天见")
        out = settings.handle_command("设置 重置 关机通知文案")
        check("重置后 is_set 为假",
              settings.is_set("offwork_msg") is False and "恢复默认" in out, f"got={out}")
        out = settings.handle_command("设置 不存在的项 x")
        check("未知键给提示", "这个设置项" in out and "看列表" in out, f"got={out}")
        settings.handle_command("设置 bubbles_show 待处理、已处理")
        check("multi 也能用指令改",
              settings.get("bubbles_show") == ["open", "done"])

        d = {x["key"]: x for x in settings.describe()}
        check("describe 含 label/kind/value/help",
              all(k in d["bubbles_show"] for k in ("label", "kind", "value", "help")))
        check("describe 带 choices 与中文标签",
              d["bubbles_show"]["choices"]
              == ["memo", "memo_done", "open", "done", "decided", "dropped"]
              and d["bubbles_show"]["choice_labels"]["memo"] == "备忘"
              and d["bubbles_show"]["choice_labels"]["open"] == "待处理")
    finally:
        if orig is None:
            os.environ.pop("MEMO_SETTINGS_FILE", None)
        else:
            os.environ["MEMO_SETTINGS_FILE"] = orig
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── [12] config-web（配置中心）──────────────────────────────────
def _load_configweb():
    spec = importlib.util.spec_from_file_location(
        "cweb", os.path.join(ROOT, "config-web.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _raises(fn, *a):
    try:
        fn(*a)
        return False
    except Exception:  # noqa: BLE001 —— 只关心「有没有抛」
        return True


def test_config_web():
    print("\n[12] config-web 配置中心（.env 写入 / 白名单 / 字段表 / 页面护栏）")
    cw = _load_configweb()

    tmpdir = tempfile.mkdtemp(prefix="cweb_test_")
    orig_env, orig_bak = cw.ENV_PATH, cw.ENV_BAK
    try:
        cw.ENV_PATH = os.path.join(tmpdir, ".env")
        cw.ENV_BAK = os.path.join(tmpdir, ".env.bak")

        # 1) 值字面量：空 / 含空格 / 裸值 / Windows 路径 / 双引号
        check("空值写成两个引号", cw._env_literal("") == '""')
        check("含空格的值加双引号", cw._env_literal("a b") == '"a b"')
        check("普通值不加引号", cw._env_literal("abc") == "abc")
        check("Windows 路径反斜杠转正斜杠",
              cw._env_literal(r"C:\Users\x") == "C:/Users/x",
              cw._env_literal(r"C:\Users\x"))
        check("值里含双引号直接拒绝", _raises(cw._env_literal, 'a"b'))

        # 2) 按行改 .env：注释与顺序保留、已有键只换值、新键追加
        SRC = ("# 我的注释\nQQ_BOT_APPID=123456\n\n# 备忘触发词\n"
               "MEMO_TRIGGER=备忘\nMEMO_SEARCH_ROOT=\"C:/tmp\"\n")
        with open(cw.ENV_PATH, "w", encoding="utf-8", newline="\n") as f:
            f.write(SRC)
        keys, err = cw.write_env({"MEMO_TRIGGER": "记一下", "QQ_BOT_SANDBOX": "true"})
        after = open(cw.ENV_PATH, encoding="utf-8").read()
        check("写 .env 无错误", err is None, err)
        check("注释原样保留", "# 我的注释" in after and "# 备忘触发词" in after)
        check("已有键只换值", "MEMO_TRIGGER=记一下" in after)
        check("新键追加到末尾", after.rstrip().endswith("QQ_BOT_SANDBOX=true"))
        check("键顺序未乱",
              after.index("QQ_BOT_APPID") < after.index("MEMO_TRIGGER")
              < after.index("MEMO_SEARCH_ROOT"))
        check("写前备份 .env.bak", os.path.exists(cw.ENV_BAK))
        check("备份 = 写入前内容",
              open(cw.ENV_BAK, encoding="utf-8").read() == SRC)
        check("返回值列出写入的键",
              sorted(keys) == ["MEMO_TRIGGER", "QQ_BOT_SANDBOX"], keys)

        # 再写一次：替换而不是重复追加
        cw.write_env({"MEMO_TRIGGER": "备忘"})
        again = open(cw.ENV_PATH, encoding="utf-8").read()
        check("重复写不追加新行", again.count("MEMO_TRIGGER=") == 1)
        check("第二次备份 = 第一次写入后的内容",
              open(cw.ENV_BAK, encoding="utf-8").read() == after)

        # 3) 白名单与校验
        k, e = cw.write_env({"NOT_ALLOWED_KEY": "x"})
        check("非白名单键被拒", k == [] and "不允许" in (e or ""), e)
        k, e = cw.write_env({"QQ_BOT_APPID": "abc"})
        check("AppID 非数字被拒", k == [] and "数字" in (e or ""), e)
        k, e = cw.write_env({"MEMO_TRIGGER": "有 空格"})
        check("触发词含空格被拒", k == [] and "空格" in (e or ""), e)
        k, e = cw.write_env({"MEMO_SEARCH_ROOT": os.path.join(tmpdir, "no-such-dir")})
        check("不存在的目录被拒", k == [] and "不存在" in (e or ""), e)
        check("被拒时整批不写（AppID 没被改成 abc）",
              cw.read_env_file()["QQ_BOT_APPID"] == "123456")
        k, e = cw.write_env({"QQ_BOT_SANDBOX": "yes"})
        check("bool 归一化成 true", cw.read_env_file()["QQ_BOT_SANDBOX"] == "true")

        # 3b) 搜索根支持**多个目录**（分号 / 换行分隔）
        _ra = tempfile.mkdtemp(prefix="cw_root_a_")
        _rb = tempfile.mkdtemp(prefix="cw_root_b_")
        try:
            check("多目录字段拆成列表", cw.split_roots("a;b\nc") == ["a", "b", "c"])
            check("多目录校验：都存在则通过", cw.check_roots(f"{_ra};{_rb}") is None)
            check("多目录校验：有一个不存在就报错",
                  bool(cw.check_roots(f"{_ra};{os.path.join(_rb, 'nope')}")))
            check("多目录校验：空值报错", bool(cw.check_roots("")))
            check("MEMO_SEARCH_ROOT 是多目录字段",
                  cw.ENV_SPEC["MEMO_SEARCH_ROOT"].get("kind") == "paths")
            k, e = cw.write_env({"MEMO_SEARCH_ROOT": f"{_ra}\n{_rb}"})
            check("多目录写 .env 成功", k == ["MEMO_SEARCH_ROOT"] and e is None, e)
            stored = cw.read_env_file()["MEMO_SEARCH_ROOT"]
            check("多目录在 .env 里是分号串（换行已归一）", ";" in stored and "\n" not in stored, stored)
            check("多目录写 .env 反斜杠转正斜杠", "\\" not in stored, stored)
        finally:
            shutil.rmtree(_ra, ignore_errors=True)
            shutil.rmtree(_rb, ignore_errors=True)
            cw.write_env({"MEMO_SEARCH_ROOT": tmpdir})     # 还原，别影响后面的用例

        # 4) 密钥留空 = 不改（页面不回显它，留空不能理解成清空）
        cw.write_env({"QQ_BOT_SECRET": "SECRET-VALUE-123456"})
        before_secret = cw.read_env_file()["QQ_BOT_SECRET"]
        k, e = cw.write_env({"QQ_BOT_SECRET": ""})
        check("密钥留空不写", k == [] and e is None, (k, e))
        check("密钥保持原值", cw.read_env_file()["QQ_BOT_SECRET"] == before_secret)

        # 5) 字段表：secret 不回显、分组与 target 齐全
        fields = cw.state_fields()
        sec = [f for f in fields if f["key"] == "QQ_BOT_SECRET"][0]
        check("AppSecret 不回显（value 为空）", sec["value"] == "" and sec["set"] is True)
        check("字段带 target（env / settings 分流）",
              {f["target"] for f in fields} == {"env", "settings"})
        check("settings 的八个键都在字段表里",
              {f["key"] for f in fields if f["target"] == "settings"}
              == {"startup_msg", "offwork_msg", "intro_msg", "memo_ack", "idea_ack",
                  "bubbles_show", "boomerang_days", "boomerang_scope"})
        # 🔴 「运行方式」「摸鱼库」两块不走字段表（前者是自绘面板、后者是整块 textarea），
        #    所以字段表只覆盖 7 个分区，别把这两个算进去。
        check("字段表覆盖 7 个「有字段」的分区",
              {f["group"] for f in fields}
              == {"bot", "behavior", "msg", "files", "bubbles", "boomerang", "ignore"},
              str(sorted({f["group"] for f in fields})))
        check("GROUPS 共 9 个分区，第 9 个是摸鱼库",
              len(cw.GROUPS) == 9 and cw.GROUPS[-1][0] == "moyu",
              str([k for k, _, _ in cw.GROUPS]))
        check("页签顺序 = GROUPS 顺序",
              [g["key"] for g in [{"key": k} for k, _, _ in cw.GROUPS]]
              == ["bot", "behavior", "msg", "files", "bubbles", "boomerang",
                  "runtime", "ignore", "moyu"])

        # 6) 目录浏览：不存在 / 不是目录都给错误，别抛
        bad = cw.browse_dirs(os.path.join(tmpdir, "nope"))
        check("browse 不存在的目录返回 ok=false", bad["ok"] is False and bad.get("error"))
        good = cw.browse_dirs(tmpdir)
        check("browse 正常目录返回 ok=true", good["ok"] is True and "dirs" in good)

        # 7) 页面护栏
        check("只监听 127.0.0.1",
              'ThreadingHTTPServer(("127.0.0.1"' in
              open(os.path.join(ROOT, "config-web.py"), encoding="utf-8").read())
        check("写操作要求自定义请求头（CSRF 防护）",
              cw.HEADER_FLAG == "X-Memo-Config" and cw.HEADER_FLAG in cw.PAGE)
        check("页面有「保存并重启机器人」", "保存并重启机器人" in cw.PAGE)
        check("页面有目录浏览器", "浏览…" in cw.PAGE)
        check("页面有搜索目录实测按钮", "check-root" in cw.PAGE)
        check("页面支持多目录字段（每行一个）", "f.kind === 'paths'" in cw.PAGE)

        # 8) 数据文件位置：字段齐全 + 页面展示 + 打开文件夹走白名单
        _st = cw.full_state()
        for _k in ("data_dir", "memos_db", "env", "settings"):
            check(f"paths 暴露 {_k}", bool(_st["paths"].get(_k)), str(_st["paths"].get(_k)))
        check("页面有数据文件位置区块", "数据文件位置" in cw.PAGE)
        check("页面有打开文件夹按钮", "data-open" in cw.PAGE)
        _csrc = open(os.path.join(ROOT, "config-web.py"), encoding="utf-8").read()
        check("打开文件夹是白名单（不吃任意路径）",
              'targets = {"data": data_dir()' in _csrc and 'payload.get("what")' in _csrc)
        check("页面有开机自启开关", "autostart" in cw.PAGE)
        check("页面有依赖自检", "依赖自检" in cw.PAGE)

        # 9) 页签 + 未配置门禁（一屏一块，不用上下拉）
        check("页面有页签条容器", 'id="tabs"' in cw.PAGE)
        check("页签按后端分组渲染（页面不硬编码分组名）",
              "function renderTabs" in cw.PAGE and "function visibleGroups" in cw.PAGE
              and "const gs = visibleGroups()" in cw.PAGE)
        check("一次只渲染当前页签那一块", "gs.find(x => x.key === ACTIVE_TAB)" in cw.PAGE)
        check("未配凭据时只留「① 连接 QQ」",
              "S.groups.filter(g => g.key === 'bot')" in cw.PAGE)
        check("未配凭据时页签条隐藏",
              "classList.add('hide')" in cw.PAGE and ".tabs.hide{display:none}" in cw.PAGE)
        check("页面骨架是一屏一块（只有内容区滚动）",
              "overflow:hidden" in cw.PAGE and "overflow-y:auto" in cw.PAGE)
        check("多行文案用 textarea 渲染（kind=area）", "f.kind === 'area'" in cw.PAGE)

        # 9b) 摸鱼库页签：页面护栏 + 端点 + 打开库文件走白名单
        _csrc = open(os.path.join(ROOT, "config-web.py"), encoding="utf-8").read()
        check("页面有摸鱼库页签的渲染函数", "function moyuSection" in cw.PAGE)
        check("摸鱼库是整库提交（改一类不丢另外两类）",
              "function moyuPayload" in cw.PAGE and "function moyuValue" in cw.PAGE)
        check("摸鱼库有分类切换按钮", "data-moyucat" in cw.PAGE)
        check("摸鱼库有恢复内置按钮", 'id="moyu-reset"' in cw.PAGE)
        check("摸鱼库有「打开库文件位置」", 'id="moyu-open"' in cw.PAGE)
        check("摸鱼库保存走 /api/moyu", "api('/api/moyu', {groups: moyu})" in cw.PAGE)
        check("摸鱼库恢复走 /api/moyu {reset:true}", "api('/api/moyu', {reset: true})" in cw.PAGE)
        check("后端有 /api/moyu 端点", 'path == "/api/moyu"' in _csrc)
        check("打开库文件在白名单里（不吃任意路径）", '"moyu": _moyu.user_path()' in _csrc)
        check("页签尺寸锁在「九个能排一行」的档位（改标题要重算宽度）",
              "font:12px/1.2 inherit" in cw.PAGE and "max-width:880px" in cw.PAGE)

        # 9c) 「去 QQ 里开两个开关」提示块 —— 用户不看安装指南就会漏掉这两步，
        #     不开的话机器人收不到普通发言 / 发不出播报，看起来就像「坏了」。
        check("页面有「去 QQ 开两个开关」提示块", "function groupSwitchesHTML" in cw.PAGE)
        check("提示块挂在「① 连接 QQ」分区上（不是别处）",
              "groupSwitchesHTML()" in
              cw.PAGE.split("function botSection")[1].split("function ")[0])
        check("写清了第一条开关：群聊消息范围 = 获取群内全部消息",
              "机器人可获取的群聊消息范围" in cw.PAGE and "获取群内全部消息" in cw.PAGE)
        check("写清了第二条开关：机器人主动在群聊内发言",
              "机器人主动在群聊内发言" in cw.PAGE)
        check("两条都写了「不开会怎样」", cw.PAGE.count("不开：") >= 2)
        check("提示块是 HTML 不是裸 markdown（help 走 esc 纯文本）",
              "**" not in cw.PAGE.split("function groupSwitchesHTML")[1].split("\nfunction ")[0])

        # 9d) 提示块里的 QQ 设置页截图 —— 走白名单静态路由，按文件头发类型
        check("提示块里插了 QQ 设置页截图",
              'class="swshot"' in cw.PAGE and 'src="/docs/qq-group-switches.png"' in cw.PAGE)
        check("截图路由是白名单（不吃任意路径）",
              "DOC_IMAGES = {" in _csrc and "if path in DOC_IMAGES" in _csrc
              and "DOC_IMAGES[path]" in _csrc)
        check("image_mime 按文件头认类型，不看扩展名",
              cw.image_mime(b"\x89PNG\r\n\x1a\n" + b"x" * 20) == "image/png"
              and cw.image_mime(b"\xff\xd8\xff\xe0" + b"x" * 20) == "image/jpeg"
              and cw.image_mime(b"nope") == "application/octet-stream")
        check("那张截图确实在仓库里",
              os.path.isfile(os.path.join(ROOT, "docs", "qq-group-switches.png")))

        # 8) sync_env：写完 .env 立刻让本进程看到新值
        cw.sync_env()
        check("sync_env 把 .env 灌进 os.environ",
              os.environ.get("MEMO_TRIGGER") == "备忘",
              os.environ.get("MEMO_TRIGGER"))

        # 9) 文档同步：每个可配置键都要在 .env.example 里出现过
        example = open(os.path.join(ROOT, ".env.example"), encoding="utf-8").read()
        missing = [k for k in cw.ENV_SPEC if k not in example]
        check(".env.example 覆盖全部可配置键", not missing, f"缺={missing}")

        # 10) 泡泡页只管展示范围（启动播报 / 关机文案搬去配置中心了）
        bubbles = open(os.path.join(ROOT, "brain-drawer.py"), encoding="utf-8").read()
        check("泡泡页设置浮层只渲染 bubbles_show",
              "const PAGE_KEYS = ['bubbles_show'];" in bubbles)
        check("泡泡页不再渲染通知文案项",
              "startup_msg" not in bubbles.split("const PAGE_KEYS")[1].split("</script>")[0])
    finally:
        cw.ENV_PATH, cw.ENV_BAK = orig_env, orig_bak
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── [13] 控制台编码：GBK 环境下不许把进程带崩 ────────────────────
def _free_port():
    """要一个当前空闲的本地端口（起测试服务用）。"""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _extract_pid_alive(relpath):
    """用 AST 从源文件里单独抠出 `_pid_alive`，返回 **(函数对象, 函数源码段)**；找不到返回 `(None, "")`。

    🔴 不能直接 `import bot` —— `bot.py` 是**模块级执行**，import 就会把机器人启动起来
    （它会读 .env、连 QQ、抢 pid 锁）。测试要验的只是这一个纯函数，用 AST 精确抠出来最干净。

    🔴 返回源码段是给源码护栏用的：断言必须**只匹配这个函数体**。曾踩到 —— 断言整个文件里
    有没有 `ValueError`，而 `bot_stop()` 里恰好也有一处同名兜底 → `_pid_alive` 的兜底被删掉
    后护栏依然全绿（假绿）。
    """
    path = os.path.join(ROOT, relpath)
    if not os.path.isfile(path):
        return None, ""
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    node = next((n for n in tree.body
                 if isinstance(n, ast.FunctionDef) and n.name == "_pid_alive"), None)
    if node is None:
        return None, ""
    seg = ast.get_source_segment(src, node)
    ns = {"os": os, "subprocess": subprocess}
    exec(seg, ns)
    return ns["_pid_alive"], seg


class _NoStdout:
    """假的 subprocess 结果：stdout 是 None（受限环境里真会出现）。"""
    stdout = None


def test_console_encoding():
    """🔴 中文 Windows 上 stdout 重定向到文件时按系统默认编码（GBK）写 —— 编不出的
    字符会让 print 抛 UnicodeEncodeError，把**整个进程**带崩。

    真凶案例（2026-10-09）：用户双击「脑内抽屉.bat」页面打不开。open_drawer.py 用
    pythonw 后台拉起 brain-drawer.py，子进程 stdout 重定向到日志文件，而启动提示里的
    ⏻(U+23FB) 在 GBK 里没有字形 → 异常在 serve_forever() 之前冒出来，服务根本没起来。
    开发机终端是 UTF-8（PYTHONIOENCODING=utf-8），源码文本护栏**完全抓不到**这种崩溃，
    所以这里必须真起一次子进程、把环境强制成 GBK 来验。
    """
    print("\n[13] 控制台编码（GBK 环境下不许把进程带崩）")

    # 1) 源码护栏：每个可执行入口都要把 stdout / stderr 切成 UTF-8
    entries = ["bot.py", "config-web.py", "brain-drawer.py",
               "send_file.py", "ideas.py", os.path.join("test", "run-tests.py")]
    for name in entries:
        path = os.path.join(ROOT, name)
        src = open(path, encoding="utf-8").read() if os.path.isfile(path) else ""
        check(f"{name} 强制 UTF-8 输出", 'reconfigure(encoding="utf-8"' in src)

    # 2) 工作区入口脚本给子进程也设好编码环境变量（双保险）。它不在仓库里，有才查。
    ob_path = os.path.join(os.path.dirname(ROOT), "open_drawer.py")
    if os.path.isfile(ob_path):
        ob = open(ob_path, encoding="utf-8").read()
        check("open_drawer.py 给子进程设 PYTHONIOENCODING", "PYTHONIOENCODING" in ob)
    else:
        check("open_drawer.py 存在（工作区入口脚本）", True, "跳过：不在仓库内")

    # 3) 行为验证：强制 GBK 起一次 --serve，必须真能起来
    port = _free_port()
    env = dict(os.environ)
    for k in ("PYTHONUTF8", "PYTHONIOENCODING", "LANG", "LC_ALL", "LC_CTYPE"):
        env.pop(k, None)
    env["PYTHONIOENCODING"] = "gbk"          # 模拟中文 Windows 双击环境
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "brain-drawer.py"),
         "--serve", "--no-browser", "--port", str(port)],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    alive = False
    try:
        for _ in range(24):                  # 最多等 6 秒
            time.sleep(0.25)
            if proc.poll() is not None:
                break                        # 崩了 —— 就是我们修的那个 bug
            try:
                with opener.open(f"http://127.0.0.1:{port}/api/ping", timeout=0.5) as r:
                    alive = bool(json.load(r).get("ok"))
                    if alive:
                        break
            except (OSError, ValueError, urllib.error.URLError):
                pass
        detail = ""
        if not alive:
            try:
                out, _ = proc.communicate(timeout=2)
                detail = (out or b"").decode("utf-8", "replace")[-400:]
            except subprocess.TimeoutExpired:
                pass
        check("GBK 环境下 --serve 能起来（不被 ⏻ 编码问题带崩）", alive, detail)
    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        try:
            proc.stdout.close()
        except (OSError, ValueError):
            pass

    # 4) 🔴 行为护栏：`_pid_alive` 不许被「解码异常 / stdout 为 None」打断（2026-10-09 真凶）。
    #    重启机器人时崩在这里 —— bot_stop 成功 → bot_start 崩 → **机器人被停掉却起不来**：
    #      · tasklist 输出是 GBK，被 UTF-8 解码 → UnicodeDecodeError
    #        （它继承 **ValueError**，不是 OSError → 只写 `except (OSError, ...)` 兜不住）
    #      · 受限环境里 stdout 是 None → `str(pid) in None` 抛 TypeError
    #    异常在 subprocess 内部产生，源码文本护栏**抓不到** → 必须真调一次。
    #    bot.py 是模块级执行（import 就会启动机器人），所以用 AST 抠出函数单独跑。
    for rel in ("bot.py", "config-web.py"):
        fn, seg = _extract_pid_alive(rel)
        check(f"{rel}::_pid_alive 源码带 encoding + ValueError 兜底",
              'encoding="utf-8", errors="replace"' in seg
              and "subprocess.SubprocessError, ValueError" in seg)
        if fn is None or os.name != "nt":
            why = "没抽出函数定义" if fn is None else "跳过：非 Windows"
            check(f"{rel}::_pid_alive 遇 UnicodeDecodeError 不冒泡", True, why)
            check(f"{rel}::_pid_alive 遇 stdout=None 不抛 TypeError", True, why)
            continue
        orig_run = subprocess.run
        try:
            def _raise_decode(*a, **k):
                raise UnicodeDecodeError("utf-8", b"\xd0\xcf", 0, 1, "invalid continuation byte")

            subprocess.run = _raise_decode
            try:
                check(f"{rel}::_pid_alive 遇 UnicodeDecodeError 不冒泡", fn(os.getpid()) is False)
            except Exception as e:
                check(f"{rel}::_pid_alive 遇 UnicodeDecodeError 不冒泡", False,
                      f"冒泡 {type(e).__name__}")

            subprocess.run = lambda *a, **k: _NoStdout()
            try:
                check(f"{rel}::_pid_alive 遇 stdout=None 不抛 TypeError", fn(os.getpid()) is False)
            except Exception as e:
                check(f"{rel}::_pid_alive 遇 stdout=None 不抛 TypeError", False,
                      f"抛了 {type(e).__name__}")
        finally:
            subprocess.run = orig_run


# ── [14] 回旋镖 + 导出 ──────────────────────────────────────────
def test_boomerang_export():
    """回旋镖：把「N 天以前」的旧记录翻一条推回给用户，推过的不重复推。
    导出：把全部备忘 + 灵感导成 md / txt / json，群指令和泡泡页按钮共用同一套排版。
    """
    print("\n[14] 回旋镖（翻旧账）+ 导出（备忘/灵感 → 文件）")
    import bot as _bot
    import export as _export
    import settings as _settings
    import store

    tmp = tempfile.mkdtemp(prefix="bm_export_test_")
    orig_db = store.DB
    orig_set = os.environ.get("MEMO_SETTINGS_FILE")
    store.DB = os.path.join(tmp, "t.db")
    os.environ["MEMO_SETTINGS_FILE"] = os.path.join(tmp, "settings.json")
    store.init()
    try:
        # 造数据：5 天前 1 备忘 + 1 灵感（带结论），今天 1 灵感
        con = sqlite3.connect(store.DB)
        old = (datetime.now() - timedelta(days=5)).isoformat(timespec="seconds")
        today = datetime.now().isoformat(timespec="seconds")
        con.execute("INSERT INTO memos (id,group_id,content,created_at,kind,status)"
                    " VALUES (?,?,?,?,?,?)", ("a" * 8, "g1", "五天前的备忘", old, "memo", "open"))
        con.execute("INSERT INTO memos (id,group_id,content,created_at,kind,status,note)"
                    " VALUES (?,?,?,?,?,?,?)",
                    ("b" * 8, "g1", "五天前的灵感", old, "idea", "decided", "当时的结论"))
        con.execute("INSERT INTO memos (id,group_id,content,created_at,kind,status)"
                    " VALUES (?,?,?,?,?,?)", ("c" * 8, "g1", "今天刚记的", today, "idea", "open"))
        con.commit()
        con.close()

        # 1) 指令解析：只认完整写法，别把「回旋镖xx」当指令
        check("parse_boomerang 裸指令", _bot.parse_boomerang("回旋镖") == (True, None))
        check("parse_boomerang 带天数", _bot.parse_boomerang("回旋镖 7") == (True, 7))
        check("🔴 parse_boomerang 认 0 天（今天记的也翻）",
              _bot.parse_boomerang("回旋镖 0") == (True, 0)
              and _bot.parse_boomerang("回旋镖0") == (True, 0))
        check("parse_boomerang 负数当非指令（落回普通流程，不误判）",
              _bot.parse_boomerang("回旋镖 -1") == (False, None))
        check("parse_boomerang 别名", _bot.parse_boomerang("回旋标") == (True, None))
        check("parse_boomerang 非指令不误判", _bot.parse_boomerang("回旋镖xx") == (False, None))
        check("parse_boomerang 普通聊天不误判", _bot.parse_boomerang("今天摸鱼了") == (False, None))

        # 2) 只翻「够天数」的：今天那条不算
        scope = _settings.get("boomerang_scope")
        check("未满天数的记录不进池子", store.count_boomerang(3, scope) == 2)
        check("把回溯天数调到 0，今天那条才进池子", store.count_boomerang(0, scope) == 3)

        # 3) 推一条 → 打标记 → 不重复推
        #    先把范围锁死成一类，结果才是确定的（不然随机挑到备忘就没有「结论」了）
        _settings.set_value("boomerang_scope", ["decided"])
        first = _bot.boomerang_text()
        check("回旋镖文案带头 🪃", "🪃" in first)
        check("回旋镖带上当时的结论", "当时的结论" in first)
        check("推过一条后池子少一条", store.count_boomerang(3, ["decided"]) == 0)
        _settings.set_value("boomerang_scope", ["memo"])
        second = _bot.boomerang_text()
        check("换一类还能再推一条", "五天前的备忘" in second)
        check("第二次推的不是同一条", second != first)
        _settings.set_value("boomerang_scope", scope)
        third = _bot.boomerang_text()
        check("推完只剩没满天数的 → 提示再等等", "再等等" in third)

        # 4) 范围配置真的生效（先把上一步打的标记清掉再按范围数一遍）
        con = sqlite3.connect(store.DB)
        con.execute("UPDATE memos SET boomerang_at = NULL")
        con.commit()
        con.close()
        saved = _settings.get("boomerang_scope")
        _settings.set_value("boomerang_scope", ["memo"])
        check("范围只剩「备忘」→ 只数得到备忘",
              store.count_boomerang(3, ["memo"]) == 1
              and store.count_boomerang(3, ["decided"]) == 1)
        check("范围里没有「已丢弃」→ 那条灵感不算",
              store.count_boomerang(3, ["dropped"]) == 0)
        _settings.set_value("boomerang_scope", saved)
        check("范围恢复后两条都回来", store.count_boomerang(3, saved) == 2)

        # 4.5) 🔴 回溯天数 = 0：今天刚记的也能被翻出来（新用户第一发就有反应）
        con = sqlite3.connect(store.DB)
        con.execute("UPDATE memos SET boomerang_at = NULL")
        con.commit()
        con.close()
        _settings.set_value("boomerang_days", 0)
        check("🔴 天数 0 → 三条全进池子（含今天那条）", store.count_boomerang(0, saved) == 3)
        _settings.set_value("boomerang_scope", ["open"])   # 只留「今天刚记的」那条灵感
        got0 = _bot.boomerang_text()
        check("🔴 天数 0 真能翻到今天的记录", "今天刚记的" in got0, got0[:60])
        check("文案说「你今天」而不是「0 天前」",
              "你今天" in got0 and "0 天前" not in got0)
        # 再重置一次：验「显式传 0」不会被 falsy 判断（`if not days`）吃掉
        con = sqlite3.connect(store.DB)
        con.execute("UPDATE memos SET boomerang_at = NULL")
        con.commit()
        con.close()
        check("🔴 显式传 days=0 不会被当成「没传」（falsy 陷阱）",
              "今天刚记的" in _bot.boomerang_text(0))
        _settings.set_value("boomerang_scope", saved)
        _settings.set_value("boomerang_days", 3)

        # 5) settings：新加的 int 类型要能校验
        val, err = _settings.normalize("boomerang_days", "abc")
        check("回溯天数拒绝非整数", val is None and "整数" in err)
        val, err = _settings.normalize("boomerang_days", "0")
        check("🔴 回溯天数下限是 0（今天记的也翻得到，不把新用户挡在门外）",
              val == 0 and err is None)
        val, _ = _settings.normalize("boomerang_days", "-1")
        check("回溯天数拒绝负数", val is None)
        val, _ = _settings.normalize("boomerang_days", "9999")
        check("回溯天数上限是 365", val is None)
        val, err = _settings.normalize("boomerang_days", "7")
        check("回溯天数接受 7", val == 7 and err is None)
        check("🔴 设置项声明里 min 就是 0（别被人改回 1）",
              _settings.KEYS["boomerang_days"]["min"] == 0)
        check("help 文案说明「填 0 就能翻今天记的」",
              "0" in _settings.KEYS["boomerang_days"]["help"])
        check("help 文案里没有裸的 markdown 星号（页面按纯文本渲染）",
              "**" not in _settings.KEYS["boomerang_days"]["help"])
        val, err = _settings.normalize("boomerang_scope", "备忘、待处理灵感")
        check("推送范围认中文标签", val == ["memo", "open"] and err is None)
        check("推送范围默认含备忘", "memo" in _settings.get("boomerang_scope"))
        check("回溯天数默认 3", _settings.get("boomerang_days") == 3)

        # 6) 导出：三种格式 + 格式名容错
        check("normalize_format 认 markdown", _export.normalize_format("markdown") == "md")
        check("normalize_format 认 .TXT", _export.normalize_format(".TXT") == "txt")
        check("normalize_format 认不出回默认", _export.normalize_format("pdf") == "md")
        fname, data = _export.render("md")
        text = data.decode("utf-8")
        check("导出文件名带时间戳", fname.startswith("摸鱼bot导出_") and fname.endswith(".md"))
        check("md 导出有总标题与备忘段", "# 摸鱼bot 导出" in text and "## 备忘" in text)
        check("md 导出有灵感分组", "## 灵感" in text and "已决议" in text)
        check("md 导出带上结论", "当时的结论" in text)
        check("md 导出把正文放进引用块（保留换行）", "> 五天前的备忘" in text)
        _, jdata = _export.render("json")
        payload = json.loads(jdata.decode("utf-8"))
        check("json 导出结构完整",
              set(payload) >= {"exported_at", "counts", "memos", "ideas"})
        check("json 导出计数正确", payload["counts"] == {"memo": 1, "idea": 2})
        _, tdata = _export.render("txt")
        check("txt 导出有分组标题", "── 备忘" in tdata.decode("utf-8"))

        # 7) 导出指令解析
        check("parse_export 裸指令", _bot.parse_export("导出") == (True, None))
        check("parse_export 带格式", _bot.parse_export("导出 json") == (True, "json"))
        check("parse_export 非指令不误判", _bot.parse_export("导出xx") == (False, None))
        check("parse_export 普通聊天不误判", _bot.parse_export("导出来看看") == (False, None))

        # 8) 源码护栏：两个入口都接上了，且中文文件名不会乱码
        T2 = open(os.path.join(ROOT, "brain-drawer.py"), encoding="utf-8").read()
        check("泡泡页有「⬇ 导出」按钮", 'id="btn-export"' in T2)
        check("导出按钮只在线模式显示", "btn-export').style.display = ''" in T2)
        check("按钮走 /api/export", "/api/export?format=md" in T2)
        check("服务端有 /api/export 路由", '"/api/export"' in T2)
        check("导出响应带 filename*（中文名不乱码）", "filename*=UTF-8''" in T2)
        CW = open(os.path.join(ROOT, "config-web.py"), encoding="utf-8").read()
        # 🔴 不写死圈号（①~⑧ 会随分组增删变）—— 只认 group key
        check("配置中心有回旋镖分区", '("boomerang", "' in CW and "回旋镖" in CW)
        check("回旋镖两个键都归到该分区",
              '"boomerang_days": "boomerang"' in CW
              and '"boomerang_scope": "boomerang"' in CW)
        B2 = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
        check("导出强制走文件卡片（不渲染成图）", "as_file=True" in B2)
        check("帮助菜单提到回旋镖与导出",
              "回旋镖 [天数]" in B2 and "导出 [格式]" in B2)
    finally:
        store.DB = orig_db
        if orig_set is None:
            os.environ.pop("MEMO_SETTINGS_FILE", None)
        else:
            os.environ["MEMO_SETTINGS_FILE"] = orig_set
        shutil.rmtree(tmp, ignore_errors=True)


# ── [15] 关机播报（触发可靠性）+ 数据目录解耦 ────────────────────
def test_offwork_paths():
    """🔴 两条硬约束：

    1. **关机播报不能再依赖计划任务**。实测事件 1074 触发的那次：任务启动了
       （LastRunTime 有值）但返回码 0xC000026B、脚本一行日志都没写 —— 关机流程
       没给 Python 启动的机会。主力改成常驻进程监听 `WM_QUERYENDSESSION`。
    2. **启动器不许改源码版的数据目录**。早先无条件设 `MEMO_DATA_DIR`，
       源码直跑也被挪到 `%LOCALAPPDATA%/memo-bot`，读不到旧库（实测生成了空 memos.db）。
    """
    print("\n[15] 关机播报（触发可靠性）+ 数据目录解耦")

    workspace = os.path.dirname(ROOT)

    # 1) 模块在位
    for name in ("offwork.py", "shutdown_hook.py", "memo-launcher.py", "paths.py"):
        check(f"{name} 在位", os.path.isfile(os.path.join(ROOT, name)))

    # 2) offwork：dry-run 不联网、返回码合法
    import offwork

    cfg = offwork.read_config()
    check("read_config 返回 5 元组（群 / 私聊目标合成一个列表）",
          isinstance(cfg, tuple) and len(cfg) == 5)
    check("read_config 的目标是 (scope, openid) 列表",
          isinstance(cfg[2], list)
          and all(isinstance(t, tuple) and len(t) == 2 for t in cfg[2]))
    code, text = offwork.send_offwork(dry_run=True)
    check("dry-run 返回 0/2 且不抛", code in (0, 2), text)
    check("dry-run 不取 access_token（不打网络）", "access_token" not in text)

    # 3) shutdown_hook：真建顶层窗口，PostMessage 能触发且去重
    import shutdown_hook

    check("shutdown_hook.available 与平台一致",
          shutdown_hook.available() == (os.name == "nt"))
    if shutdown_hook.available():
        import ctypes

        called = []
        shutdown_hook.install(lambda: called.append(1))
        hwnd = None
        for _ in range(100):
            hwnd = shutdown_hook._HWND
            if hwnd:
                break
            time.sleep(0.05)
        check("关机监听建出顶层窗口", bool(hwnd))
        if hwnd:
            u = ctypes.windll.user32
            u.PostMessageW(hwnd, shutdown_hook.WM_QUERYENDSESSION, 0, 0)
            time.sleep(0.4)
            u.PostMessageW(hwnd, shutdown_hook.WM_QUERYENDSESSION, 0, 0)
            time.sleep(0.2)
            check("收到关机通知只回调一次（去重）", called == [1], str(called))

    # 4) 计划任务入口改为转调 offwork（不再自带一份发送逻辑）
    notify = os.path.join(workspace, "offwork_notify.py")
    nsrc = open(notify, encoding="utf-8").read() if os.path.isfile(notify) else ""
    check("offwork_notify.py 转调 offwork 模块", "offwork.send_offwork" in nsrc)
    check("offwork_notify.py 不再自己取 token", "get_access_token" not in nsrc)

    # 5) 启动器：源码直跑不许改数据目录
    lsrc = open(os.path.join(ROOT, "memo-launcher.py"), encoding="utf-8").read()
    check("launcher 用 node/ 判定安装版",
          '_NODE_IN_PACKAGE = os.path.join(os.path.dirname(HERE), "node", "node.exe")' in lsrc)
    check("launcher 只在安装版设 MEMO_DATA_DIR",
          'if PACKAGED and not os.environ.get("MEMO_DATA_DIR")' in lsrc)

    # 6) paths：没设 MEMO_DATA_DIR 时回退代码目录（源码直跑行为不变）
    probe = ("import os,sys;"
             f"sys.path.insert(0, r'{ROOT}');"
             "import paths;"
             "print(paths.data_dir())")
    env = dict(os.environ)
    env["MEMO_DATA_DIR"] = ""
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                         text=True, encoding="utf-8", errors="replace", env=env)
    check("未设 MEMO_DATA_DIR 时回退代码目录",
          out.stdout.strip() == ROOT,
          out.stdout.strip() or out.stderr.strip()[:120])

    # 7) 版本号 + 更新检测（纯函数，不联网）
    import version as _ver

    check("version.py 有 VERSION", bool(_ver.VERSION))
    check("VERSION 文件与常量一致",
          open(os.path.join(ROOT, "VERSION"), encoding="utf-8").read().strip() == _ver.VERSION)
    check("parse_version 认 v 前缀", _ver.parse_version("v1.2.3") == (1, 2, 3))
    check("parse_version 认不出返回空", _ver.parse_version("abc") == ())
    check("compare 新版在前 → 1", _ver.compare_version("0.2.0", "0.1.0") == 1)
    check("compare 同版 → 0", _ver.compare_version("0.1.0", "0.1.0") == 0)
    check("compare 旧版 → -1", _ver.compare_version("0.1.0", "0.2.0") == -1)
    check("compare 按数字比（0.10.0 > 0.9.0）", _ver.compare_version("0.10.0", "0.9.0") == 1)
    check("compare 缺位补 0（0.2 == 0.2.0）", _ver.compare_version("0.2", "0.2.0") == 0)
    check("远端地址走 jsdelivr（raw 连不上）", "jsdelivr" in _ver.RAW_URL)

    # 7.1) 下载：给用户一个能直接点的链接（GitHub Releases 直链 / 可换成自建源）
    check("安装包名带版本号", _ver.asset_name("0.2.0") == "memo-bot-setup-v0.2.0.exe")
    check("版本串带 v 前缀也能归一化",
          _ver.asset_name("v0.2.0") == "memo-bot-setup-v0.2.0.exe"
          and _ver.asset_name(" 0.2.0 ") == "memo-bot-setup-v0.2.0.exe")
    _dl = _ver.download_url("0.2.0")
    check("默认下载直链 = GitHub Releases 且带版本号",
          _dl == "https://github.com/phononprismlab/memo-bot/releases/download/v0.2.0/"
                 "memo-bot-setup-v0.2.0.exe", _dl)
    check("Release 页兜底链接", _ver.release_url("0.2.0").endswith("/releases/tag/v0.2.0"))
    check("全部 Releases 列表链接", _ver.releases_url().endswith("/releases"))
    _orig_base = os.environ.get("MEMO_DOWNLOAD_BASE")
    try:
        os.environ["MEMO_DOWNLOAD_BASE"] = "https://example.com/dl/"
        check("🔴 MEMO_DOWNLOAD_BASE 能换成自建源（去尾斜杠）",
              _ver.download_url("0.2.0") == "https://example.com/dl/memo-bot-setup-v0.2.0.exe")
        check("显式传 base 优先于环境变量",
              _ver.download_url("0.2.0", "https://cdn.example.com")
              == "https://cdn.example.com/memo-bot-setup-v0.2.0.exe")
    finally:
        if _orig_base is None:
            os.environ.pop("MEMO_DOWNLOAD_BASE", None)
        else:
            os.environ["MEMO_DOWNLOAD_BASE"] = _orig_base
    check("没设基址时回到 GitHub", "github.com" in _ver.download_url("0.2.0"))

    _csrc2 = open(os.path.join(ROOT, "config-web.py"), encoding="utf-8").read()
    check("页面有「检查更新」按钮", "btn-update" in _csrc2)
    check("配置中心暴露 version", '"version": _version.VERSION' in _csrc2)
    check("更新接口回「直接下载」的链接（不只是仓库主页）",
          '"download": _version.download_url(far)' in _csrc2)
    check("页面有可见的「⬇ 下载新版」入口",
          'id="ver-dl"' in _csrc2 and "a.dlbtn" in _csrc2)
    check("直链打不开时给 Releases 兜底入口", "Releases 页面" in _csrc2)

    # 🔴 安装包文件名两边必须一致：.iss 的 OutputBaseFilename ↔ version.py 的 ASSET_TMPL。
    #    不一致 = 用户点「⬇ 下载新版」直接 404。_exe_poc 只在开发机上，没有就跳过。
    _iss_path = os.path.join(os.path.dirname(ROOT), "_exe_poc", "memo-bot.iss")
    if os.path.exists(_iss_path):
        _iss = open(_iss_path, encoding="utf-8-sig").read()
        check("🔴 .iss 产物名带版本号，且与 version.py 的模板对得上",
              "OutputBaseFilename=memo-bot-setup-v{#MyAppVersion}" in _iss
              and _ver.ASSET_TMPL == "memo-bot-setup-v{ver}.exe")
        check("🔴 .iss 的版本号从 VERSION 文件读（不再手写一份）",
              'Trim(FileRead(FileOpen(RepoDir + "\\VERSION")))' in _iss
              and '#define MyAppVersion "' not in _iss)

    # 8) 会话发现 + 播报目标（push.py）—— 群和私聊**合成一张表**。
    #    动机：页面上给用户看的是一串 openid，`group_openid` 和 `user_openid` 长得
    #    几乎一样，分「已发现的群 / 已发现的私聊」两个列表反而认不出「这串 id 后面是谁」。
    #    合成一张表 + 可改的备注名 + 最近一条消息，才认得出、才选得对。
    import push as _push

    check("push.py 在位", os.path.isfile(os.path.join(ROOT, "push.py")))
    # 🔴 护栏：route 测试会用假 openid 调 push.note()，不隔离就写进仓库里的真实文件。
    check("🔴 测试期间 push.FILE 已指向临时目录（不碰真实 sessions.json）",
          _TMP_DATA in _push.FILE)
    check("🔴 旧模块 groups.py / c2c.py 已删（统一到 push.py）",
          not os.path.isfile(os.path.join(ROOT, "groups.py"))
          and not os.path.isfile(os.path.join(ROOT, "c2c.py")))

    # 8a) 目标解析：新键 `group:<id>;c2c:<id>` + 旧键兼容
    check("parse_targets 带前缀", _push.parse_targets("group:G1;c2c:U2")
          == [("group", "G1"), ("c2c", "U2")])
    check("parse_targets 裸 openid 当群（兼容旧 QQ_GROUP_OPENID）",
          _push.parse_targets("G1") == [("group", "G1")])
    check("parse_targets 中文前缀 + 去重",
          _push.parse_targets("群:G1；私聊:U2, group:G1")
          == [("group", "G1"), ("c2c", "U2")])
    check("parse_targets 丢掉空 openid",
          _push.parse_targets("group:;c2c:U9") == [("c2c", "U9")])
    check("format_targets 往返",
          _push.format_targets(_push.parse_targets("c2c:U2;group:G1")) == "c2c:U2;group:G1")

    _saved_keys = ("QQ_PUSH_TARGETS", "QQ_GROUP_OPENID", "QQ_C2C_OPENID")
    _saved_env = {k: os.environ.get(k) for k in _saved_keys}
    try:
        for _k in _saved_keys:
            os.environ.pop(_k, None)
        os.environ["QQ_PUSH_TARGETS"] = "c2c:U2;group:G1"
        os.environ["QQ_GROUP_OPENID"] = "OLD"
        check("targets 配了新键就忽略旧键",
              _push.targets() == [("c2c", "U2"), ("group", "G1")], str(_push.targets()))
        os.environ["QQ_PUSH_TARGETS"] = ""
        os.environ["QQ_GROUP_OPENID"] = "G1;G2"
        os.environ["QQ_C2C_OPENID"] = "U2"
        check("🔴 targets 没配新键时回退旧两键（老用户升级不丢配置）",
              _push.targets() == [("group", "G1"), ("group", "G2"), ("c2c", "U2")],
              str(_push.targets()))
        check("targets 支持 env dict 兜底（计划任务入口不一定 load_dotenv）",
              _push.targets({"QQ_PUSH_TARGETS": "group:G9"}) == [("group", "G9")])
    finally:
        for _k, _v in _saved_env.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v

    # 8b) 记录 / 备注名 / 勾选状态
    import store as _store

    _ptmp = tempfile.mkdtemp(prefix="push_")
    _orig_pfile, _orig_pdb = _push.FILE, _store.DB
    _push.FILE = os.path.join(_ptmp, "sessions.json")
    _store.DB = os.path.join(_ptmp, "none.db")     # 隔离真实库，否则历史会话混进断言
    _push._RECENT.clear()
    try:
        check("sessions 空 → []", _push.sessions() == [])
        _push.note("group", "G1")
        _push.note("c2c", "U1", name="张三")
        _push.note("c2c", "")                      # 空 openid 直接忽略
        got = [(r["scope"], r["openid"]) for r in _push.sessions()]
        check("note 记下群和私聊（空 openid 忽略）",
              sorted(got) == [("c2c", "U1"), ("group", "G1")], str(got))
        _push.note("group", "G1")                  # 节流窗口内重复 → 不重复记
        check("note 重复记录不重复", len(_push.sessions()) == 2)
        check("note 带上的备注名保留",
              [r["name"] for r in _push.sessions() if r["openid"] == "U1"] == ["张三"])
        check("set_name 改备注名",
              _push.set_name("c2c", "U1", "李四")
              and [r["name"] for r in _push.sessions() if r["openid"] == "U1"] == ["李四"])
        os.environ["QQ_PUSH_TARGETS"] = "group:G1"
        try:
            row = [r for r in _push.sessions() if r["openid"] == "G1"][0]
            check("sessions 标出哪些已在播报目标里", row["target"] is True)
            check("sessions 已勾选的排在前面",
                  _push.sessions()[0]["openid"] == "G1")
        finally:
            os.environ.pop("QQ_PUSH_TARGETS", None)
    finally:
        _push.FILE, _store.DB = _orig_pfile, _orig_pdb
        _push._RECENT.clear()
        shutil.rmtree(_ptmp, ignore_errors=True)

    # 8c) DB 补齐：`sessions.json` 是后加的，不补的话老用户打开页面看到的是空列表；
    #     「最近说过什么」也来自这里 —— 平台不给会话名，这是唯一能自动拿到的身份线索。
    _dtmp = tempfile.mkdtemp(prefix="push_db_")
    _orig_pfile2, _orig_db2 = _push.FILE, _store.DB
    _push.FILE = os.path.join(_dtmp, "sessions.json")
    _store.DB = os.path.join(_dtmp, "memos.db")
    _push._RECENT.clear()
    try:
        _con = sqlite3.connect(_store.DB)
        _con.execute("CREATE TABLE memos (group_id TEXT, content TEXT, created_at TEXT)")
        _con.executemany("INSERT INTO memos VALUES (?,?,?)", [
            ("c2c:HIST-1", "第一条", "2026-10-01T10:00:00"),
            ("c2c:HIST-1", "第二条", "2026-10-02T10:00:00"),
            ("c2c:HIST-2", "你好", "2026-10-03T10:00:00"),
            ("GROUPX", "群里的备忘", "2026-10-04T10:00:00"),
            ("", "无主", "2026-10-05T10:00:00"),
        ])
        _con.commit()
        _con.close()
        _ss = _push.sessions()
        got = [(r["scope"], r["openid"]) for r in _ss]
        check("sessions 从 DB 补出历史会话（群 + 私聊、去重、丢掉空 id）",
              sorted(got) == [("c2c", "HIST-1"), ("c2c", "HIST-2"), ("group", "GROUPX")],
              str(got))
        check("sessions 补齐是只读的（查完不写 sessions.json）",
              not os.path.exists(_push.FILE))
        _row = [r for r in _ss if r["openid"] == "HIST-1"][0]
        check("recent 取的是最近一条（不是最老一条）", _row["recent"] == "第二条",
              _row["recent"])
        check("recent_at 是时间戳", _row["recent_at"] > 0, str(_row["recent_at"]))
        check("群那条也认得出来",
              [r["recent"] for r in _ss if r["openid"] == "GROUPX"] == ["群里的备忘"])
    finally:
        _push.FILE, _store.DB = _orig_pfile2, _orig_db2
        _push._RECENT.clear()
        shutil.rmtree(_dtmp, ignore_errors=True)

    # 8d) 迁移：首次把旧的 groups.json + c2c.json 并进 sessions.json（旧文件保留不删）
    _mtmp = tempfile.mkdtemp(prefix="push_mig_")
    _orig_pfile3, _orig_mdata = _push.FILE, os.environ.get("MEMO_DATA_DIR")
    _push.FILE = os.path.join(_mtmp, "sessions.json")
    os.environ["MEMO_DATA_DIR"] = _mtmp            # 让 data_path("groups.json") 指到这里
    _push._RECENT.clear()
    try:
        with open(os.path.join(_mtmp, "groups.json"), "w", encoding="utf-8") as _f:
            json.dump({"groups": {"G1": {"name": "长风号", "last": 111}}}, _f)
        with open(os.path.join(_mtmp, "c2c.json"), "w", encoding="utf-8") as _f:
            json.dump({"U2": {"name": "", "last": 222}}, _f)
        _push.note("group", "G3")                  # 触发迁移
        got = {(r["scope"], r["openid"]): r["name"] for r in _push.sessions()}
        check("迁移：旧 groups.json → group:，旧 c2c.json → c2c:，备注名保留",
              got.get(("group", "G1")) == "长风号" and ("c2c", "U2") in got,
              str(got))
        check("🔴 迁移后旧文件保留不删",
              os.path.exists(os.path.join(_mtmp, "groups.json"))
              and os.path.exists(os.path.join(_mtmp, "c2c.json")))
        _push.note("c2c", "U2", name="老王")
        check("迁移只做一次（不覆盖后来改的备注名）",
              [r["name"] for r in _push.sessions() if r["openid"] == "U2"] == ["老王"])
    finally:
        _push.FILE = _orig_pfile3
        if _orig_mdata is None:
            os.environ.pop("MEMO_DATA_DIR", None)
        else:
            os.environ["MEMO_DATA_DIR"] = _orig_mdata
        _push._RECENT.clear()
        shutil.rmtree(_mtmp, ignore_errors=True)

    # 8e) 调用方与页面
    _csrc3 = open(os.path.join(ROOT, "config-web.py"), encoding="utf-8").read()
    check("页面有「播报目标」区块", "播报目标" in _csrc3)
    check("页面有勾选框（data-pushtoggle）", "data-pushtoggle" in _csrc3)
    check("页面有「改名」入口（data-rename）", "data-rename" in _csrc3)
    check("配置中心有 /api/push 路由", '"/api/push"' in _csrc3)
    check("配置中心有 /api/session 路由（改备注名）", '"/api/session"' in _csrc3)
    check("配置中心把会话列表塞进页面状态",
          '"sessions": _push.sessions()' in _csrc3 and '"push_targets"' in _csrc3)
    check("🔴 页面上不再有裸 openid 输入框（两个旧键标 hidden）",
          _csrc3.count('"hidden": True') >= 3)
    _bsrc = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
    check("bot 收到消息会记录会话", "_push.note(scope, t[\"openid\"])" in _bsrc)
    check("🔴 bot 里不再有取群名的死代码（botpy 的 GroupMessage 没这字段）",
          'getattr(message, "group_name"' not in _bsrc)
    check("启动播报读 push.targets()",
          "_push.targets()" in _bsrc and "for sc, oid in targets" in _bsrc)
    _osrc = open(os.path.join(ROOT, "offwork.py"), encoding="utf-8").read()
    check("关机播报读 push.targets()",
          "_push.targets(env)" in _osrc and "for sc, oid in targets" in _osrc)
    check("🔴 sessions.json 进了 .gitignore（含真实 openid + 备注名）",
          "sessions.json" in open(os.path.join(ROOT, ".gitignore"),
                                  encoding="utf-8").read())
    check("🔴 单聊也能主动推送（不再强制被动）",
          "scope == SCOPE_C2C or not group" not in
          open(os.path.join(ROOT, "send_file.py"), encoding="utf-8").read())


# ── [16] 新手引导：「你好」/ 首次功能介绍 / 可配回执 ──────────────
def test_onboarding():
    print("\n[16] 新手引导（首次功能介绍 /「你好」/ 可配回执）")
    import asyncio
    import bot
    import settings
    import store

    class Msg:
        """假消息：字段名与 botpy 的 GroupMessage / C2CMessage 对齐。"""

        def __init__(self, content, gid="G1", mid="MSG1", sid="M1", uid=None):
            self.content = content
            self.id = mid
            self.group_openid = gid
            self.author = SimpleNamespace(member_openid=sid, user_openid=uid)
            self.replies = []

        async def reply(self, content=None, msg_seq=1):
            self.replies.append(content)

    class Client:
        async def send_file_to_chat(self, *a, **k):
            raise AssertionError("本用例不应走到发文件分支")

    tmp = tempfile.mkdtemp(prefix="onboard_test_")
    _env = {k: os.environ.get(k) for k in
            ("MEMO_SETTINGS_FILE", "MEMO_DATA_DIR", "MEMO_IGNORE",
             "MEMO_IGNORE_FILE", "MEMO_TRIGGER")}
    _orig_db, _orig_last, _orig_trigger = store.DB, bot.LAST_MSG_FILE, bot.TRIGGER
    os.environ["MEMO_SETTINGS_FILE"] = os.path.join(tmp, "settings.json")
    os.environ["MEMO_DATA_DIR"] = tmp          # onboarded.json 落到临时目录
    os.environ["MEMO_IGNORE"] = ""
    os.environ["MEMO_IGNORE_FILE"] = os.path.join(tmp, "ignore.json")
    os.environ["MEMO_TRIGGER"] = "备忘"
    store.DB = os.path.join(tmp, "t.db")
    bot.LAST_MSG_FILE = os.path.join(tmp, "last.json")
    bot.TRIGGER = "备忘"
    try:
        store.init()
        cli = Client()

        def run(msg, mentioned=True, scope=bot.SCOPE_GROUP):
            asyncio.run(bot.route_message(cli, msg, mentioned, scope))

        # 1) 「你好」= 自我介绍，且明说没有语言生成能力
        check("HELLO_CMDS 认「你好」", "你好" in bot.HELLO_CMDS)
        check("内置兜底文案写明「没有语言生成能力」",
              "没有语言生成能力" in bot.HELLO_FALLBACK)
        check("intro_text 默认取设置里的「功能介绍」",
              bot.intro_text() == settings.get("intro_msg")
              and "没有语言生成能力" in bot.intro_text())
        check("功能介绍默认是多行的", "\n" in bot.intro_text())

        m = Msg("你好")
        run(m)
        check("「你好」回自我介绍", len(m.replies) == 1
              and "没有语言生成能力" in m.replies[0], f"got={m.replies}")
        check("「你好」不落库",
              store.list_memos(10, store.KIND_IDEA) == []
              and store.list_memos(10, store.KIND_MEMO) == [])

        # 2) 设置里清空功能介绍 → 「你好」回退内置短文案（永远有话说）
        settings.set_value("intro_msg", "")
        m = Msg("你好")
        run(m)
        check("清空功能介绍后回退内置文案",
              m.replies == [bot.HELLO_FALLBACK], f"got={m.replies}")
        settings.reset("intro_msg")

        # 3) 备忘回执可配
        check("备忘回执默认值来自设置",
              settings.get("memo_ack") == "已经放进脑内抽屉了")
        m = Msg("备忘 买猫粮")
        run(m)
        check("默认备忘回执", m.replies == ["已经放进脑内抽屉了"], f"got={m.replies}")
        settings.set_value("memo_ack", "收好了")
        m = Msg("备忘 买狗粮")
        run(m)
        check("改设置后备忘回执跟着变", m.replies == ["收好了"], f"got={m.replies}")
        settings.set_value("memo_ack", "")
        m = Msg("备忘 买鱼粮")
        run(m)
        check("回执留空 = 静默记下（不回复）", m.replies == [], f"got={m.replies}")
        check("静默时仍然落库", len(store.list_memos(10, store.KIND_MEMO)) == 3,
              f"got={len(store.list_memos(10, store.KIND_MEMO))}")
        settings.reset("memo_ack")

        # 4) 灵感回执可配（私聊兜底分支）
        check("灵感回执默认留空（保持原有静默行为）", settings.get("idea_ack") == "")
        m = Msg("突然想到一个点子", uid="U1")
        run(m, mentioned=True, scope=bot.SCOPE_C2C)
        check("默认私聊静默收灵感", m.replies == [], f"got={m.replies}")
        settings.set_value("idea_ack", "💡 已收作灵感")
        m = Msg("又一个点子", uid="U1")
        run(m, mentioned=True, scope=bot.SCOPE_C2C)
        check("填了灵感回执就回那一句", m.replies == ["💡 已收作灵感"], f"got={m.replies}")
        check("回执改了不影响落库",
              len(store.list_ideas(store.STATUS_OPEN)) == 2,
              f"got={len(store.list_ideas(store.STATUS_OPEN))}")
        settings.reset("idea_ack")

        # 5) 首次引导标记（写在数据目录：源码版 / 安装版各记各的，卸载不动数据）
        _flag = os.path.join(tmp, "onboarded.json")
        check("标记落在数据目录",
              bot.data_path(bot._ONBOARD_FILE) == _flag,
              f"got={bot.data_path(bot._ONBOARD_FILE)}")
        check("初始：没发过功能介绍", bot.onboarded_done() is False)
        bot.mark_onboarded()
        check("打标记后：发过了", bot.onboarded_done() is True)
        check("标记文件真的落盘", os.path.exists(_flag))
        with open(_flag, "w", encoding="utf-8") as fh:
            fh.write("{坏掉的 json")
        check("标记读坏了当没发过（下次上线重试）", bot.onboarded_done() is False)

        # 6) 上线播报：首次发介绍、之后只发启动播报（源码护栏）
        _src = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
        check("上线时按「有没有发过介绍」二选一",
              "first = not onboarded_done()" in _src
              and '"intro_msg" if first else "startup_msg"' in _src)
        check("介绍被清空则退回普通播报", "first = False" in _src)
        check("介绍发成功才打标记（一个群都没发成 → 下次重试）",
              "if first and sent:" in _src and "mark_onboarded()" in _src)
        check("写死的 C2C_ACK_IDEA 已换成设置项",
              "C2C_ACK_IDEA" not in _src and '_settings.get("idea_ack")' in _src)
    finally:
        store.DB, bot.LAST_MSG_FILE, bot.TRIGGER = _orig_db, _orig_last, _orig_trigger
        for k, v in _env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)


# ── [17] 摸鱼库可配置（内置种子 + 用户库）───────────────────────
def test_moyu_library():
    print("\n[17] 摸鱼库可配置（内置种子 / 用户库 / 校验 / 恢复默认）")
    import moyu

    tmp = tempfile.mkdtemp(prefix="moyu_lib_")
    _orig_up, _orig_recent = moyu.user_path, moyu.RECENT
    # 🔴 和 groups.json 一样：user_path() 默认落代码目录，测试必须指到临时目录，
    #    否则「保存摸鱼库」的用例会真的往仓库里写 moyu-user.json。
    moyu.user_path = lambda: os.path.join(tmp, "moyu-user.json")
    moyu.RECENT = os.path.join(tmp, "moyu-recent.json")
    up = moyu.user_path()
    try:
        base = moyu.builtin_items()

        # 1) 还没用户库 → 用内置的
        check("初始 source()=builtin", moyu.source() == "builtin")
        check("初始 load_items() 就是内置库",
              moyu.load_items() == base and len(base) > 0, f"got={len(moyu.load_items())}")
        check("as_texts() 三类齐全",
              set(moyu.as_texts()) == set(moyu.CATS)
              and all(len(v) > 0 for v in moyu.as_texts().values()))
        check("内置库 id 唯一", len({it["id"] for it in base}) == len(base))
        check("内置库每条 cat 合法、text 是字符串",
              all(it.get("cat") in moyu.CATS and isinstance(it.get("text"), str)
                  for it in base))

        # 2) 保存 → 落盘 + 立刻生效
        ok, n = moyu.save_items({"relax": ["去阳台站两分钟", "   ", "去阳台站两分钟"],
                                 "slack": ["泡杯茶"], "trivia": []})
        check("保存成功", ok is True, n)
        check("只算有效条数（丢空行 + 同类去重）", n == 2, f"got={n}")
        check("保存后 source()=user", moyu.source() == "user")
        check("保存后 load_items() = 用户库", len(moyu.load_items()) == 2)
        check("落盘文件真的在", os.path.exists(up))
        doc = json.load(open(up, encoding="utf-8"))
        check("落盘结构 {version, items}",
              doc.get("version") == 1 and isinstance(doc.get("items"), list))
        check("id 是「分类-序号」",
              [it["id"] for it in doc["items"]] == ["relax-01", "slack-01"])
        check("原子写：不留 .tmp 残留", not os.path.exists(up + ".tmp"))

        # 3) 也吃整段文本（配置中心传的是数组，但手改 / 别的调用方可能传字符串）
        ok, n = moyu.save_items({"relax": "甲\n\n乙\n甲", "slack": [], "trivia": []})
        check("传整段文本也能存（按行拆 / 丢空行 / 去重）", ok is True and n == 2, n)

        # 4) 改了库 → 之前「抽过哪些」作废
        moyu._save_recent(["relax-01"])
        moyu.save_items({"relax": ["换个内容"], "slack": [], "trivia": []})
        check("改库后 recent 清空", moyu._load_recent() == [])

        # 5) 校验：全空 / 单条超长 / 单类超条数
        ok, err = moyu.save_items({"relax": [], "slack": [], "trivia": []})
        check("三类全空被拒", ok is False and "至少留一条" in (err or ""), err)
        ok, err = moyu.save_items({"relax": ["x" * (moyu.MAX_TEXT_LEN + 1)]})
        check("单条超长被拒", ok is False and "太长" in (err or ""), err)
        ok, err = moyu.save_items(
            {"relax": [f"第{i}条" for i in range(moyu.MAX_PER_CAT + 1)]})
        check("单类超条数被拒", ok is False and "超过上限" in (err or ""), err)
        check("被拒时不写文件（还是上一次的内容）",
              [it["text"] for it in moyu.load_items()] == ["换个内容"])

        # 6) 读坏 / 空库 → 悄悄退回内置，别让「摸鱼」整个哑掉
        with open(up, "w", encoding="utf-8") as fh:
            fh.write("{坏掉的 json")
        check("用户库读坏 → 退回内置",
              moyu.load_items() == base and moyu.source() == "builtin")
        with open(up, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "items": []}, fh)
        check("用户库是空的 → 退回内置", len(moyu.load_items()) == len(base))
        with open(up, "w", encoding="utf-8") as fh:
            json.dump({"version": 1,
                       "items": [{"id": "a", "cat": "relax", "text": "x"}]}, fh)
        check("用户库正常 → 就用它", moyu.source() == "user")

        # 7) 恢复内置默认
        ok, msg = moyu.reset_items()
        check("恢复默认成功", ok is True and "已恢复" in msg, msg)
        check("恢复后用户库文件没了", not os.path.exists(up))
        check("恢复后 source()=builtin", moyu.source() == "builtin")
        ok, msg = moyu.reset_items()
        check("本来就用内置时再点一次：说人话、不当成错误",
              ok is False and msg == "本来用的就是内置库", msg)

        # 8) 空库文案指路 + stats
        check("空库文案指向配置中心页签", "配置中心" in moyu.format_moyu([]))
        _st = moyu.stats()
        check("stats() 带 source / path",
              _st["source"] == "builtin" and _st["path"] == up, str(_st))

        # 9) 源码护栏
        _msrc = open(os.path.join(ROOT, "moyu.py"), encoding="utf-8").read()
        check("内置库只当种子（程序没有往 DATA 写的地方）",
              'open(DATA' in _msrc
              and not re.search(r'open\(\s*DATA\s*,\s*["\']w', _msrc))
        check("用户库落数据目录而不是代码目录",
              'USER_FILE = "moyu-user.json"' in _msrc and "data_path(USER_FILE)" in _msrc)
        check("用户库进了 .gitignore",
              "moyu-user.json" in open(os.path.join(ROOT, ".gitignore"),
                                       encoding="utf-8").read())
    finally:
        moyu.user_path, moyu.RECENT = _orig_up, _orig_recent
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    print("=" * 60)
    print("memo-bot 回归测试")
    print("=" * 60)
    test_moyu_parse()
    test_moyu_draw()
    test_store()
    test_bot()
    test_send_file()
    test_md2img()
    test_ideas_bubbles()
    test_extensions()
    test_ignore()
    test_route()
    test_settings()
    test_config_web()
    test_console_encoding()
    test_boomerang_export()
    test_offwork_paths()
    test_onboarding()
    test_moyu_library()
    print("\n" + "=" * 60)
    if FAILS:
        print(f"❌ {len(FAILS)}/{TOTAL[0]} 项失败：")
        for f in FAILS:
            print(f"   - {f}")
        sys.exit(1)
    print(f"✅ 全部通过（{TOTAL[0]} 项）")


if __name__ == "__main__":
    main()
