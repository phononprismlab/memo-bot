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
 [15] launcher       启动器：源码直跑不许改数据目录
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

    # 🔴 搜索根里混进 Windows 保留设备名文件时不能崩（真机上踩过：一个 `nul`
    #    把「查运势」整条指令搞挂，用户看到的是原始 ValueError）。
    check("保留设备名识别：nul / NUL / nul.txt / com3 / lpt9.log",
          all(bot._is_reserved_name(x) for x in
              ("nul", "NUL", "nul.txt", "con", "COM3", "aux", "prn", "lpt9.log")))
    check("保留设备名识别：别误伤 null.txt / nulify.md",
          not any(bot._is_reserved_name(x) for x in
                  ("null.txt", "nulify.md", "com10", "lpt0", "console.py")))

    # 行为验证：把 os.walk 换成「目录里只有 nul 一个文件」的桩
    _real_walk = bot.os.walk
    _stub_root = tempfile.mkdtemp(prefix="reserved_")
    _orig_roots = bot.SEARCH_ROOTS
    try:
        bot.SEARCH_ROOTS = [_stub_root]
        bot.os.walk = lambda _r: iter([(_stub_root, [], ["nul", "con.md", "real.txt"])])
        got = [rel for rel, _ in bot.search_files("nul")]
        check("搜索跳过保留设备名（不崩、也不收录）", got == [], f"got={got}")
        bot.os.walk = lambda _r: iter([(_stub_root, [], ["real.txt"])])
        got2 = [rel for rel, _ in bot.search_files("real")]
        check("同目录下的正常文件照常搜到", got2 == ["real.txt"], f"got={got2}")
    finally:
        bot.os.walk = _real_walk
        bot.SEARCH_ROOTS = _orig_roots
        shutil.rmtree(_stub_root, ignore_errors=True)

    # 第二层保险：relpath 抛 ValueError 时跳过这一个文件，而不是崩掉整条指令
    _real_relpath = bot.os.path.relpath
    _orig_roots = bot.SEARCH_ROOTS
    _stub2 = tempfile.mkdtemp(prefix="relpath_")
    try:
        with open(os.path.join(_stub2, "boom.txt"), "w", encoding="utf-8") as f:
            f.write("x")
        with open(os.path.join(_stub2, "ok.txt"), "w", encoding="utf-8") as f:
            f.write("x")
        bot.SEARCH_ROOTS = [_stub2]
        bot.os.path.relpath = lambda p, s=None: (_ for _ in ()).throw(ValueError("path is on mount")) \
            if str(p).endswith("boom.txt") else _real_relpath(p, s)
        got = [rel for rel, _ in bot.search_files("txt")]
        check("relpath 抛 ValueError 时只跳过那一个文件", got == ["ok.txt"], f"got={got}")
    finally:
        bot.os.path.relpath = _real_relpath
        bot.SEARCH_ROOTS = _orig_roots
        shutil.rmtree(_stub2, ignore_errors=True)

    # 🔴 搜索可能跑几十秒（搜索根 = 整个用户目录）→ 必须丢线程，不能卡事件循环
    _src = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
    check("两条 relpath 分支共用同一个 try/except ValueError",
          "except ValueError:\n                    continue" in _src
          and "os.path.normcase(full).startswith(ws_prefix)" in _src)
    check("同目录加权用 normcase 归一比较（大小写不敏感）",
          "ws_prefix = os.path.normcase(WORKSPACE_ROOT + os.sep)" in _src)
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
              "linear-gradient(0deg,var(--footbar-1)" in T)
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
        check("长文本判据带上结论与图片",
              "estHeight(truncate(it.t), laneW, it.note, it.pics)" in T2
              and "estHeight(truncate(it.t), fullW, it.note, it.pics)" in T2)
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


# ── [22] extmgr：扩展包的管理面（导入 / 列出 / 移除）────────────────
_EXT_GOOD_SRC = (
    "def register(reg):\n"
    "    def h(text, mentioned):\n"
    "        return 'hi'\n"
    "    reg.add(['你好', 'hi'], h, help='你好 —— 打招呼')\n"
)


def _zip_bytes(entries):
    """把 [(名字, 内容)] 打成一个 zip 的 bytes（测试用）。"""
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, c in entries:
            z.writestr(n, c)
    return buf.getvalue()


def test_ext_manager():
    print("\n[22] 自定义指令（扩展包管理）")
    import extmgr
    import extensions

    # 🔴 全程在临时目录里做 —— 绝不碰真实的 extensions/local/
    tmp = tempfile.mkdtemp(prefix="extmgr_test_")
    real = (extmgr.LOCAL_DIR, extmgr.TRASH_DIR, extensions.LOCAL_DIR)
    extmgr.LOCAL_DIR = os.path.join(tmp, "local")
    extmgr.TRASH_DIR = os.path.join(tmp, ".removed")
    extensions.LOCAL_DIR = extmgr.LOCAL_DIR
    os.makedirs(extmgr.LOCAL_DIR)
    try:
        # 1) scan_dir：一级 .py + 一级含 .py 的子目录；`_` / `.` 开头都跳过
        os.makedirs(os.path.join(extmgr.LOCAL_DIR, "pack"))
        open(os.path.join(extmgr.LOCAL_DIR, "pack", "a.py"), "w", encoding="utf-8").close()
        open(os.path.join(extmgr.LOCAL_DIR, "solo.py"), "w", encoding="utf-8").close()
        open(os.path.join(extmgr.LOCAL_DIR, "_private.py"), "w", encoding="utf-8").close()
        os.makedirs(os.path.join(extmgr.LOCAL_DIR, ".hidden"))
        open(os.path.join(extmgr.LOCAL_DIR, ".hidden", "x.py"), "w", encoding="utf-8").close()
        os.makedirs(os.path.join(extmgr.LOCAL_DIR, "emptydir"))
        got = [(i["kind"], i["name"]) for i in extensions.scan_dir(extmgr.LOCAL_DIR)]
        check("scan_dir 认「单文件包 + 目录包」，跳过 _ / . 开头与空目录",
              got == [("dir", "pack"), ("file", "solo")], str(got))

        # 2) 导入单文件
        ok, msg, info = extmgr.import_bytes("greet.py", _EXT_GOOD_SRC.encode("utf-8"))
        check("导入 .py 成功且带出指令名",
              ok and info["commands"] and "你好" in info["commands"], msg)
        check("导入后落到 local/<名字>.py",
              os.path.isfile(os.path.join(extmgr.LOCAL_DIR, "greet.py")))

        # 3) 没有 register 的 .py 要拒绝（装进去也是死文件）
        ok, msg, _i = extmgr.import_bytes("noreg.py", b"x = 1\n")
        check("没有 def register() 的 .py 被拒", (not ok) and "register" in msg, msg)

        # 4) 不支持的扩展名
        ok, msg, _i = extmgr.import_bytes("a.txt", b"x")
        check("非 .py/.zip 被拒", (not ok) and ".py" in msg, msg)

        # 5) 重名冲突 → 默认不覆盖；overwrite=True 才覆盖
        ok, msg, info = extmgr.import_bytes("greet.py", _EXT_GOOD_SRC.encode("utf-8"))
        check("重名默认拒绝并标 conflict", (not ok) and info.get("conflict"), msg)
        ok, msg, _i = extmgr.import_bytes("greet.py", _EXT_GOOD_SRC.encode("utf-8"),
                                          overwrite=True)
        check("overwrite=True 能覆盖", ok, msg)

        # 6) 文件名收拾：不能跑出 local/（`..` / 路径分隔符 / 中文一律滤掉）
        ok, _m, _i = extmgr.import_bytes("../../evil.py", _EXT_GOOD_SRC.encode("utf-8"))
        check("恶意文件名被收拾在 local/ 内",
              ok and not os.path.exists(os.path.join(tmp, "evil.py"))
              and os.path.isfile(os.path.join(extmgr.LOCAL_DIR, "evil.py")))

        # 7) zip 目录包：单一顶层目录会被剥掉
        ok, msg, info = extmgr.import_bytes("mypack.zip", _zip_bytes([
            ("mypack/greet.py", _EXT_GOOD_SRC), ("mypack/README.md", "# hi")]))
        check("导入 .zip 成功", ok, msg)
        check("zip 的单一顶层目录被剥掉",
              os.path.isfile(os.path.join(extmgr.LOCAL_DIR, "mypack", "greet.py")))
        check("zip 里的附带文件一起装进来",
              os.path.isfile(os.path.join(extmgr.LOCAL_DIR, "mypack", "README.md")))

        # 8) zip 安全：路径穿越 / 绝对路径 / 盘符，一个都不能放
        for name, entries in (
                ("zip-slip（../）", [("../evil.py", _EXT_GOOD_SRC)]),
                ("绝对路径（/etc）", [("/etc/evil.py", _EXT_GOOD_SRC)]),
                ("盘符（C:）", [("C:/evil.py", _EXT_GOOD_SRC)])):
            ok, msg, _i = extmgr.import_bytes("evil.zip", _zip_bytes(entries))
            check(f"{name} 被拒", (not ok) and "不安全" in msg, msg)
        check("穿越的 zip 没在上一层留下文件",
              not os.path.exists(os.path.join(tmp, "evil.py")))

        # 9) zip 里没有 register / 空 zip / 假 zip
        ok, msg, _i = extmgr.import_bytes("x.zip", _zip_bytes([("a/x.py", "y=1\n")]))
        check("zip 里没有 register 被拒", (not ok) and "register" in msg, msg)
        ok, msg, _i = extmgr.import_bytes("x.zip", _zip_bytes([]))
        check("空 zip 被拒", (not ok) and "空" in msg, msg)
        ok, msg, _i = extmgr.import_bytes("x.zip", b"not a zip")
        check("假 zip 被拒", (not ok) and "zip" in msg.lower(), msg)

        # 10) 列表：探出指令名 + help；撞名要标出来
        items = {i["name"]: i for i in extmgr.list_items()}
        check("列表里有刚导入的两个包", {"greet", "mypack"} <= set(items), str(sorted(items)))
        check("探测出扩展注册的指令名", "你好" in items["greet"]["commands"],
              str(items["greet"]["commands"]))
        check("探测出 help 行", any("打招呼" in h for h in items["greet"]["helps"]),
              str(items["greet"]["helps"]))
        check("两个包注册同名指令时标 dup",
              "你好" in items["greet"].get("dup", []), str(items["greet"].get("dup")))
        check("列表带体积与文件清单",
              items["mypack"]["size"] > 0 and "greet.py" in items["mypack"]["files"])

        # 11) 坏扩展：探测出错误但**不抛**（界面要能把原因显示出来）
        with open(os.path.join(extmgr.LOCAL_DIR, "broken.py"), "w", encoding="utf-8") as f:
            f.write("raise RuntimeError('boom')\n")
        broken = {i["name"]: i for i in extmgr.list_items()}["broken"]
        check("坏扩展在列表里报错而不抛", bool(broken["error"]) and "boom" in broken["error"],
              broken["error"])

        # 12) 加载器端到端：目录包里的指令真的能被 bot 注册上
        reg = extensions.load(api=None)
        names = {n for c in reg.commands for n in c["names"]}
        check("extensions.load() 认目录包（zip 装的那种）", "你好" in names, str(sorted(names)))

        # 13) 移除 = 挪进 .removed（不是真删）
        ok, msg = extmgr.remove("file:greet")
        check("移除成功", ok, msg)
        check("移除后不在列表里",
              "greet" not in {i["name"] for i in extmgr.list_items()})
        check("移除是「挪走」不是「删掉」",
              os.path.isdir(extmgr.TRASH_DIR) and os.listdir(extmgr.TRASH_DIR))
        ok, msg = extmgr.remove("file:greet")
        check("移除不存在的包不抛、返回失败", not ok and bool(msg), msg)

        # 14) 配置中心接线：GROUPS / full_state / 路由 / 白名单
        cw = _load_configweb()
        csrc = open(os.path.join(ROOT, "config-web.py"), encoding="utf-8").read()
        gsrc = open(os.path.join(ROOT, "memo_gui.py"), encoding="utf-8").read()
        check("GROUPS 里有 ext 分区",
              any(k == "ext" for k, _t, _d in cw.GROUPS))
        check("full_state 带 ext（列表 + 安装目录）",
              isinstance(cw.full_state().get("ext"), dict)
              and "items" in cw.full_state()["ext"])
        check("页面有自定义指令区块（extSection + 导入按钮）",
              "extSection(g)" in cw.PAGE and "ext-import" in cw.PAGE)
        check("页面用 base64 上传（不走 multipart）",
              "/api/ext/import" in cw.PAGE and "fileToB64" in cw.PAGE)
        check("接口有 /api/ext/import 与 /api/ext/remove",
              '"/api/ext/import"' in csrc and '"/api/ext/remove"' in csrc)
        check("打开扩展目录走白名单（不是任意目录）",
              '"ext": _extmgr.LOCAL_DIR' in csrc)
        check("🖥 桌面版也有「自定义指令」页（分派 + 导入对话框）",
              'key == "ext"' in gsrc and "_render_ext" in gsrc and "导入扩展…" in gsrc)
        check("🖥 桌面版探测走后台线程（不冻界面）",
              "_ext_probe_work" in gsrc
              and "threading.Thread(target=self._ext_probe_work" in gsrc)
    finally:
        extmgr.LOCAL_DIR, extmgr.TRASH_DIR, extensions.LOCAL_DIR = real
        extmgr._PROBE_CACHE.clear()
        shutil.rmtree(tmp, ignore_errors=True)


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
              bool(m.replies) and "启动播报" in m.replies[0]
              and "脑内抽屉展示范围" in m.replies[0], f"got={m.replies}")
        check("「设置」指令不落库灵感",
              len(store.list_ideas(store.STATUS_OPEN)) == n_ideas)

        m = Msg("设置 startup_msg 收工，明天见")
        run(m)
        check("「设置 <键> <值>」落盘",
              bot._settings.get("startup_msg") == "收工，明天见",
              f"got={bot._settings.get('startup_msg')!r}")
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
        # 🔴 面板与指令列表都按 KEYS 的顺序排 —— 用户要求「启动播报」在「功能介绍」之前
        _order = [x["key"] for x in settings.describe()]
        check("设置项顺序：启动播报在功能介绍之前",
              _order.index("startup_msg") < _order.index("intro_msg"),
              f"got={_order}")
        check("展示范围排在文案之后",
              _order.index("bubbles_show") > _order.index("intro_msg"),
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
        check("三键排在启动播报之后、展示范围之前",
              _order.index("startup_msg") < _order.index("intro_msg")
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
        check("is_command「设置 x y」", settings.is_command("设置 启动播报 早") is True)
        check("is_command「设置好了」不算", settings.is_command("设置好了") is False)
        check("is_command「配置一下」不算", settings.is_command("配置一下") is False)
        check("is_command 普通聊天不算", settings.is_command("今天天气不错") is False)

        txt = settings.handle_command("设置")
        check("「设置」列出全部键",
              all(k["label"] in txt for k in settings.KEYS.values()), f"got={txt[:80]}")
        check("「设置」带改法提示", "设置 <键> <值>" in txt)

        settings.handle_command("设置 启动播报 我要下班了")
        check("用中文标签也能改", settings.get("startup_msg") == "我要下班了")
        settings.handle_command("设置 startup_msg 明天见")
        check("用键名也能改", settings.get("startup_msg") == "明天见")
        out = settings.handle_command("设置 重置 启动播报")
        check("重置后 is_set 为假",
              settings.is_set("startup_msg") is False and "恢复默认" in out, f"got={out}")
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
        check("settings 的十个键都在字段表里",
              {f["key"] for f in fields if f["target"] == "settings"}
              == {"startup_msg", "intro_msg", "memo_ack", "idea_ack",
                  "bubbles_show", "boomerang_days", "boomerang_scope",
                  "image_collect", "image_alert_gb", "ui_font_scale"})
        # 🔴 「运行方式」「摸鱼库」两块不走字段表（前者是自绘面板、后者是整块 textarea），
        #    所以字段表只覆盖 6 个分区，别把这两个算进去。（忽略表并入群内行为）
        check("字段表覆盖 6 个「有字段」的分区",
              {f["group"] for f in fields}
              == {"bot", "behavior", "msg", "files", "bubbles", "boomerang"},
              str(sorted({f["group"] for f in fields})))
        check("GROUPS 共 9 个分区，第 9 个是自定义指令",
              len(cw.GROUPS) == 9 and cw.GROUPS[-1][0] == "ext",
              str([k for k, _, _ in cw.GROUPS]))
        check("页签顺序 = GROUPS 顺序",
              [g["key"] for g in [{"key": k} for k, _, _ in cw.GROUPS]]
              == ["bot", "behavior", "msg", "files", "bubbles", "boomerang",
                  "runtime", "moyu", "ext"])

        # 5b) 「界面字号」：桌面版和网页版读的是**同一个设置键**，口径必须一致。
        fs = [f for f in fields if f["key"] == "ui_font_scale"][0]
        check("界面字号归在「⑤ 脑内抽屉」",
              fs["group"] == "bubbles" and fs["kind"] == "int")
        check("界面字号上下限透给界面（桌面 Spinbox / 网页 input 都用它）",
              fs["min"] == 80 and fs["max"] == 200)
        check("界面字号默认值 > 100（用户说字太小，默认就该大一档）",
              int(fs["value"]) > 100)
        # 网页版：整页 zoom 跟着设置走。
        # 🔴 body 的高度**不能**再除一次 zoom —— 实测 Chromium 的 zoom 不缩放
        #    body 自身的盒高，除一次会让 body 矮一截、保存条悬在半空（真机截图确认）。
        check("网页版整页跟随字号缩放（--z + body zoom）",
              "zoom:var(--z,1)" in cw.PAGE
              and "height:calc(100% / var(--z" not in cw.PAGE
              and "function applyFontScale()" in cw.PAGE
              and "applyFontScale();" in cw.PAGE)
        # 桌面版：字号助手 + 整窗重建
        _g = open(os.path.join(ROOT, "memo_gui.py"), encoding="utf-8").read()
        check("桌面版字号助手读的是同一个键",
              'get("ui_font_scale")' in _g and "def _font(" in _g)

        # 6) 目录浏览：不存在 / 不是目录都给错误，别抛
        bad = cw.browse_dirs(os.path.join(tmpdir, "nope"))
        check("browse 不存在的目录返回 ok=false", bad["ok"] is False and bad.get("error"))
        good = cw.browse_dirs(tmpdir)
        check("browse 正常目录返回 ok=true", good["ok"] is True and "dirs" in good)

        # 7) 页面护栏
        check("只监听 127.0.0.1（经严格模式包装的 Server 类）",
              'srv(("127.0.0.1"' in
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
    #    🔴 `_pid_alive` 现在**只有 botctl.py 一份实现**，bot.py 直接复用（见下面那条源码护栏）；
    #       所以行为验证只抠 botctl.py —— 抠 bot.py 会抠到空（它已经不带函数定义了）。
    _bot_src = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
    check("bot.py 复用 botctl._pid_alive（不再自带一份）",
          "from botctl import _pid_alive" in _bot_src
          and "\ndef _pid_alive(" not in _bot_src)
    for rel in ("botctl.py",):
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
                # 🔴 必须 `_fast=False` 强制走 tasklist 慢路径 —— Windows 上
                #    默认会先走 OpenProcess 快路径（拿得到句柄就直接 True，
                #    根本不会碰 subprocess），那样这两条兜底护栏就永远测不到了。
                check(f"{rel}::_pid_alive 遇 UnicodeDecodeError 不冒泡",
                      fn(os.getpid(), _fast=False) is False)
            except Exception as e:
                check(f"{rel}::_pid_alive 遇 UnicodeDecodeError 不冒泡", False,
                      f"冒泡 {type(e).__name__}")

            subprocess.run = lambda *a, **k: _NoStdout()
            try:
                check(f"{rel}::_pid_alive 遇 stdout=None 不抛 TypeError",
                      fn(os.getpid(), _fast=False) is False)
            except Exception as e:
                check(f"{rel}::_pid_alive 遇 stdout=None 不抛 TypeError", False,
                      f"抛了 {type(e).__name__}")
        finally:
            subprocess.run = orig_run


def test_gui_speed():
    """桌面版「打开要等几秒」的护栏（2026-10-11 用户反馈）。

    两个真凶都量过：
      · `task_state()` 起一次命令行查计划任务 = **1.2 秒**
      · `_pid_alive()` 起一次 tasklist           = **0.22 秒**
    这两个都被 `full_state()` 调用、`full_state()` 又被每次界面刷新调用
    ⇒ 每刷一次界面白卡 1.4 秒。修完必须是「非阻塞 + 快路径」，
    这个测试就是**把它们钉死**，防止以后又被改回同步。
    """
    print("\n[20] 桌面版启动速度（不能再回到「等几秒」）")
    import botctl

    # 1) _pid_alive 走快路径（OpenProcess）必须极快
    if os.name == "nt":
        t0 = time.perf_counter()
        alive = botctl._pid_alive(os.getpid())
        dt = (time.perf_counter() - t0) * 1000
        check("⏱ _pid_alive() 快路径 < 50ms（走 OpenProcess，不起 tasklist）",
              alive is True and dt < 50, f"{dt:.0f} ms, alive={alive}")
        t0 = time.perf_counter()
        dead = botctl._pid_alive(999999)
        dt2 = (time.perf_counter() - t0) * 1000
        check("⏱ 不存在的 PID 也走快路径返回 False（< 50ms）",
              dead is False and dt2 < 50, f"{dt2:.0f} ms, dead={dead}")
    else:
        check("⏱ _pid_alive() 快路径（跳过：非 Windows）", True, "")

    # 2) task_state() 默认**绝不能阻塞** —— 先把缓存打回「从没查过」
    botctl._TASK_CACHE["t"], botctl._TASK_CACHE["v"] = 0.0, "unknown"
    botctl._TASK_CACHE["busy"] = False
    t0 = time.perf_counter()
    v1 = botctl.task_state()
    dt = (time.perf_counter() - t0) * 1000
    check("⏱ task_state() 缓存过期也不阻塞（< 200ms，先给旧值）",
          dt < 200, f"{dt:.0f} ms, v={v1}")
    check("⏱ 而且确实把刷新丢到了后台（busy=True）",
          botctl._TASK_CACHE["busy"] is True, str(botctl._TASK_CACHE["busy"]))

    # 3) bot_running() 连续调用吃缓存（第二次不查盘）
    botctl.bot_running(force=True)          # 先填一次缓存
    t0 = time.perf_counter()
    for _ in range(20):
        botctl.bot_running()
    dt = (time.perf_counter() - t0) * 1000 / 20
    check("⏱ bot_running() 有短缓存（20 次平均 < 5ms）", dt < 5, f"{dt:.2f} ms")

    # 4) 源码护栏：别把这两个优化删了
    src = open(os.path.join(ROOT, "botctl.py"), encoding="utf-8").read()
    check("🔒 task_state 默认 block=False（非阻塞语义）",
          "def task_state(force=False, block=False)" in src)
    check("🔒 _pid_alive 有 OpenProcess 快路径 + _fast 开关",
          "OpenProcess" in src and "_fast=True" in src)
    check("🔒 快路径失败按错误码判「不存在」（87=ERROR_INVALID_PARAMETER）",
          "GetLastError() == 87" in src)
    check("🔒 bot_start 等 pid 时用 force（不吃 0.6s 缓存）",
          "bot_running(force=True)" in src)
    check("🔒 托盘读自启状态不 force（否则右键菜单卡 1.2 秒）",
          "return task_state() != \"none\"" in src)


def test_gui_update_check():
    """桌面版「检查更新」的护栏（2026-10-11 用户反馈「功能消失了」）。

    原来这块只在网页版有，桌面版漏搬 ⇒ 用户找不到检查更新。
    """
    print("\n[21] 桌面版「检查更新」")
    p = os.path.join(ROOT, "memo_gui.py")
    if not os.path.isfile(p):
        check("memo_gui.py 存在", False, "文件不在")
        return
    src = open(p, encoding="utf-8").read()
    check("🔄 运行方式页有「版本」区块 + 检查更新按钮",
          "def _render_runtime" in src and '"检查更新"' in src
          and 'text="版本"' in src)
    check("🔄 复用 version.py 的多源检查（不另写一套）",
          "_version.latest()" in src and "download_url" in src)
    check("🔄 有群文件兜底提示（国内 git 直连不稳）", "群文件" in src)
    # 🔴 后台线程不许碰 Tk —— 连 after() 都不行（实测抛
    #    RuntimeError: main thread is not in main loop，表现是「点了没反应」）。
    check("🔄 联网在后台线程、结果靠主线程轮询取（子线程不碰 Tk）",
          "threading.Thread" in src and "def _poll_update" in src
          and "_update_result = (cur, far, err)" in src)
    # 🔴 这一页会被重画（顶栏查到计划任务状态后 show_section），
    #    检查结果必须存在 _upd_view 里、重建时回填，否则刚查出来就被冲掉。
    check("🔄 检查结果存 _upd_view，页面重画后回填",
          "_upd_view" in src and 'self._upd_view["note"]' in src
          and '_v = self._upd_view' in src)

    # ── 抽屉密度（用户反馈「一屏显示的内容太少」，后又反馈「太稀疏」） ──
    # 每道卡片数 3 → 6：3 个时同泳道间距太大、整屏空。改大后必须配
    # `_lane_span` 的真实重叠判据，否则同道会叠在一起。
    check("📐 抽屉密度参数已上调（泳道更窄更多 / 每道 6 个 / 正文有截断）",
          "LANE_MIN_W = 256" in src and "PER_LANE = 6" in src
          and "TEXT_MAX" in src and "LINE_MAX" in src)
    check("📐 首屏按 PER_LANE 铺满每条泳道（不是只摆一条就靠补位）",
          "for r in bands:" in src and "bands = list(range(self.PER_LANE))" in src)
    # 行数也要截：只截字数挡不住「群里分十几行写的记录」
    check("📐 正文同时截字数与行数（多行记录也能截住）",
          "_lines[:self.LINE_MAX]" in src)

    # 🔴 别再自己插换行！实测 Tk 的 `wraplength` 对中文 / 长路径 / 超长 URL
    #    都能按像素正确折行（长路径那串没空格也照样折）。曾经误判成
    #    「Tk 只在空白处折」，加了个每 18 字硬插 \n 的函数，结果中文被断在句子中间
    #    （「我可怎／么话啊」）——用户报「异常换行」。
    check("📐 不用自己插换行（Tk 的 wraplength 会按像素折，包括中文/长路径）",
          "_soft_wrap" not in src and "soft_wrap" not in src)


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
        con.execute("INSERT INTO memos (id,group_id,content,created_at,kind,status,images)"
                    " VALUES (?,?,?,?,?,?,?)",
                    ("c" * 8, "g1", "今天刚记的", today, "idea", "open",
                     json.dumps(["images/202610/aaaaaaaaaaaaaaaa.png"])))
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

        # 5b) 「界面字号」：int + 80–200，越界要拦（拦不住的话 0 会让整页字消失）
        val, err = _settings.normalize("ui_font_scale", "115")
        check("界面字号接受 115", val == 115 and err is None)
        val, err = _settings.normalize("ui_font_scale", "abc")
        check("界面字号拒绝非整数", val is None and "整数" in err)
        val, _ = _settings.normalize("ui_font_scale", "10")
        check("界面字号拒绝过小值（下限 80）", val is None)
        val, _ = _settings.normalize("ui_font_scale", "999")
        check("界面字号拒绝过大值（上限 200）", val is None)
        check("界面字号默认 115（比原始大一档，用户说字太小）",
              _settings.KEYS["ui_font_scale"]["default"] == 115)
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
        # 🔴 图片必须写成**相对路径**的 Markdown 引用：导 zip 时图在 images/ 下，
        #    正文不引用的话解压出来就是「一堆图 + 不知道哪张配哪条」。
        check("md 导出带图片相对路径引用（导 zip 后能直接看图）",
              "](images/202610/aaaaaaaaaaaaaaaa.png)" in text)
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
        check("按钮走 /api/export", "/api/export?format=' + encodeURIComponent(f)" in T2)
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


# ── [15] 启动器：源码直跑不许改数据目录 ─────────────────────────
def test_data_dir_isolation():
    """启动器不许改源码版的数据目录。

    早先无条件设 `MEMO_DATA_DIR`，源码直跑也被挪到 `%LOCALAPPDATA%/memo-bot`，
    读不到旧库（实测生成了空 memos.db）。
    """
    print("\n[15] 启动器：源码直跑不许改数据目录")

    # 1) 启动器：用 node/ 判定安装版、只在安装版设 MEMO_DATA_DIR
    lsrc = open(os.path.join(ROOT, "memo-launcher.py"), encoding="utf-8").read()
    check("launcher 用 node/ 判定安装版",
          '_NODE_IN_PACKAGE = os.path.join(os.path.dirname(HERE), "node", "node.exe")' in lsrc)
    check("launcher 只在安装版设 MEMO_DATA_DIR",
          'if PACKAGED and not os.environ.get("MEMO_DATA_DIR")' in lsrc)

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


def test_push_targets():
    print("\n[18] 播报目标：读得到 .env、取消得掉（不回退旧键）")
    import push

    tmp = tempfile.mkdtemp(prefix="push_targets_")
    env_path = os.path.join(tmp, ".env")
    _orig = push.data_path
    push.data_path = lambda n: os.path.join(tmp, n) if n == ".env" else _orig(n)
    push._ENV_FILE["t"], push._ENV_FILE["v"] = -1.0, {}
    for k in ("QQ_PUSH_TARGETS", "QQ_GROUP_OPENID", "QQ_C2C_OPENID"):
        os.environ.pop(k, None)
    try:
        # 1) 配置中心不 load_dotenv —— push 必须自己把 .env 读进来，
        #    否则页面上勾选完一刷新就「又没勾上」。
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("QQ_GROUP_OPENID=G111\nQQ_PUSH_TARGETS=group:G111;c2c:U222\n")
        push._ENV_FILE["t"] = -1.0
        check("从 .env 读得到目标（不依赖进程环境）",
              push.targets() == [("group", "G111"), ("c2c", "U222")],
              str(push.targets()))

        # 2) 🔴 主键配成**空**时不能回退旧键 —— 否则用户在页面上取消勾选
        #    最后一个目标，会被 .env 里同时存在的 QQ_GROUP_OPENID 立刻复活。
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("QQ_GROUP_OPENID=G111\nQQ_PUSH_TARGETS=\n")
        push._ENV_FILE["t"] = -1.0
        check("🔴 主键为空 = 全部取消，不回退旧键",
              push.targets() == [], str(push.targets()))

        # 3) 主键**从没出现过**才回退旧键（老用户升级不丢配置）
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("QQ_GROUP_OPENID=G111\n")
        push._ENV_FILE["t"] = -1.0
        check("主键缺失才回退旧键（兼容老配置）",
              push.targets() == [("group", "G111")], str(push.targets()))

        # 4) 进程环境优先于 .env（机器人进程走 load_dotenv 的情况）
        os.environ["QQ_PUSH_TARGETS"] = "c2c:U999"
        check("进程环境优先于 .env",
              push.targets() == [("c2c", "U999")], str(push.targets()))
        os.environ.pop("QQ_PUSH_TARGETS", None)

        # 5) 源码护栏：_get 必须兜 .env（删掉这层就回到「勾不上」的老 bug）
        src = open(os.path.join(ROOT, "push.py"), encoding="utf-8").read()
        check("_get 里有 .env 兜底", "_env_file()" in src)
    finally:
        push.data_path = _orig
        push._ENV_FILE["t"], push._ENV_FILE["v"] = -1.0, {}
        for k in ("QQ_PUSH_TARGETS", "QQ_GROUP_OPENID", "QQ_C2C_OPENID"):
            os.environ.pop(k, None)
        shutil.rmtree(tmp, ignore_errors=True)


def test_gui_source():
    """桌面版（memo_gui.py）的源码护栏。

    起真窗口太慢，这里只锁「结构上必须成立、删了就退化成老样子」的几条。
    """
    print("\n[19] 桌面 GUI 源码护栏")
    p = os.path.join(ROOT, "memo_gui.py")
    if not os.path.isfile(p):
        check("memo_gui.py 存在", False, "文件不在")
        return
    src = open(p, encoding="utf-8").read()
    # extmgr 的源码也在这里锁一条（探针编码），省得起第二次窗口。
    _ep = os.path.join(ROOT, "extmgr.py")
    _EXTMGR_SRC = open(_ep, encoding="utf-8").read() if os.path.isfile(_ep) else ""

    # 🔴 pythonw 启动时 sys.stdout 是 None，一句 print 就会让整个窗口起不来。
    check("🖥 memo_gui 里没有 print()（pythonw 下会崩）",
          not re.search(r"(?<![\w.])print\s*\(", src))
    # 🔴 单实例锁 + 唤起旧窗口：少了会开出好几份、各改一份配置。
    check("🖥 有单实例锁（gui.pid）", "gui.pid" in src)
    check("🖥 有唤起已开窗口（FindWindowW）", "FindWindowW" in src)
    # 🔴 窗口无控制台，异常必须落日志，否则出错是「窗一关什么都没了」。
    check("🖥 有崩溃日志兜底（sys.excepthook）",
          "excepthook" in src and "gui-error.log" in src)
    # 🔴 AUMID 必须在 Window 建好之后再设 —— ttkbootstrap 建窗口时会覆盖它，
    #    设早了任务栏就退回 Tk 默认羽毛图标。
    _win_i = src.find("ttk.Window(")
    _aum_i = src.find("win_app_id()", _win_i)
    check("🖥 建窗口后重设 AUMID（否则任务栏掉成默认图标）",
          _win_i > 0 and _aum_i > _win_i and src.count("win_app_id()") >= 2,
          f"window@{_win_i} aum@{_aum_i}")

    # ── 浮层结构（用户明确要求照 web 版抽屉） ──
    check("🖥 浮层有「编辑/保存」切换（同一个按钮两副面孔）",
          'set_text("保存")' in src and 'set_text("编辑")' in src)
    check("🖥 进编辑态会收起标签/附件/动作区（一次只做一件事）",
          "grid_remove()" in src and "grid()" in src)
    check("🖥 底部动作按**整条流程**摆、当前站灰化（不是按状态隐现）",
          'st == _store.STATUS_OPEN' in src and "disabled=disabled" in src)
    check("🖥 动作按钮图标只用基础几何符号（emoji 在 Win10 会缺字形）",
          "✓ " in src and "★ " in src and "✅" not in src and "🗑" not in src,
          # 只看 memo_gui.py 自身 —— brain-drawer.py（网页版）走浏览器渲染，
          # emoji 没问题，不在这个护栏范围内。
          f"✅×{src.count('✅')} 🗑×{src.count('🗑')}")
    # 标签行：输入框和「已有」必须同一行（用户要求把「已有」挪到输入框右边）
    check("🖥 「已有」标签与输入行并排（不再单独占一行）",
          'pool.pack(side="left"' in src)

    # ── 真机反馈修的四条 ──
    # ① 禁用 / 占位文字**不能**用 border（它和底色几乎一样，等于把字擦掉）。
    #    真机踩到过：浮层里「还没打标签」和灰掉的流程按钮完全看不见。
    check("🖥 禁用/占位文字用 _dim() 而不是 border 色",
          "def _dim(" in src and "dim_ink(" in src
          and 'disabled_fg=self._c("border")' not in src
          and 'fg=self._c("border")' not in src)
    # ② 底部不放「关闭」—— 窗口自带 X，两个入口重复（用户要求去掉）。
    check("🖥 底部动作区没有「关闭」按钮（窗口 X 就够了）",
          '"关闭"' not in src)
    # ③ bubbles_show 必须真的当闸门用 —— 以前只在 chips 里生效，条目不做过滤。
    #    现在闸门收敛到 `_collect(gate_show=...)`：**轮播舞台**才套闸，
    #    **列表窗口 / 回收站**不套（否则「已丢弃」和「已隐藏分类」就翻不出来了）。
    check("🖥 展示范围（bubbles_show）真的过滤条目",
          "def _collect(self, rows, *, gate_show):" in src
          and 'if gate_show and b not in self._show:' in src
          and "def _load_items" in src and "def _list_items" in src)
    check("🖥 只有轮播舞台套 bubbles_show 闸（列表/回收站不受限）",
          "self._collect(self._rows(), gate_show=True)" in src
          and "self._collect(self._rows(), gate_show=False)" in src)
    # ④ 排布要有随机感：首屏起始高度 + 横向漂移都是随机的，
    #    泳道也要随机挑（固定取 0 号道会让三条道利用率不均，看着有规律）。
    check("🖥 气泡排布有随机感（泳道/起始高度/横向漂移）",
          "random.choice" in src and "random.shuffle" in src
          and "random.uniform" in src and "import random" in src)
    # ⑤ 手动回旋镖：复用 store 的 pick/mark，不重新实现规则。
    check("🖥 有手动回旋镖按钮且复用 store 规则",
          "def _manual_boomerang" in src and "pick_boomerang" in src
          and "mark_boomerang" in src)

    # ── 底部按钮行：思维流 + 四分类（点击开列表窗口）+ 回收站 ──
    # 已丢弃**不进轮播舞台**（_collect 里跳过），也不做分类 chip ——
    # 入口统一到底部常驻的「回收站」按钮。
    check("🖥 已丢弃不进轮播（_collect 里跳过）",
          'if b == "dropped":' in src)
    check("🖥 「全部」已改名「思维流」，且是选中态（当前视图本身）",
          '"思维流"' in src and '"全部"' not in src
          and 'self._chips[None] = view' in src)
    check("🖥 chips 不列已丢弃（入口统一到回收站按钮）",
          'for b in _BUCKET_ORDER:' in src
          and 'if b == "dropped":' in src
          and "continue" in src)
    check("🖥 分类 chip 点击开「列表窗口」（不再就地筛轮播）",
          "def _open_list" in src and "self._open_list(x)" in src)
    check("🖥 底部有常驻「回收站」按钮（不看 bubbles_show）",
          '"回收站", self._open_trash' in src)
    check("🖥 回收站是静态列表（自带滚动条，不是轮播）",
          "def _open_trash" in src and "def _dropped_items" in src
          and "def _scroll_area" in src)
    check("🖥 回收站列表区可滚（否则底部按钮被顶出屏幕）",
          'cv.configure(scrollregion=cv.bbox("all"))' in src
          and "cv.itemconfigure(wid, width=e.width)" in src)
    check("🖥 回收站与列表窗口共用同一份可滚骨架（不各写一遍）",
          "def _scroll_area(self, parent, pad):" in src
          and src.count("self._scroll_area(win, PAD)") >= 2)
    _trash_body = src.split("def _dropped_items")[1].split("def _open_trash")[0]
    check("🖥 回收站不受展示范围（bubbles_show）限制",
          "self._show" not in _trash_body)
    # 永久删除**不可逆** → 必须有二次确认（浮层里一处、回收站里一处）。
    check("🖥 永久删除有二次确认（浮层 + 回收站各一处）",
          src.count('messagebox.askyesno("永久删除"') >= 2)
    check("🖥 回收站用 store 的真删接口（不自己写 SQL）",
          "_store.delete_items(ids)" in src)

    # ── RoundedButton 自身的行为（真机踩过） ──
    cfg = src[src.find("    def configure(self, **kw):"):src.find("    # -- 内部 --")]
    # 🔴 改 state 必须立刻重绘：`__init__` 已按「正常态」画过一遍，只改字段的话
    #    按钮会一直显示成可点的样子，直到鼠标移上去触发一次重绘才突然变灰
    #    （真机表现：「鼠标扫过一次就变成灰色不可点文字」）。
    check("🖥 切 disabled 时立刻重绘（否则 hover 才变色）",
          'redraw = True' in cfg, cfg[:60].replace("\n", " "))
    # 禁用态要照 web 版 `opacity:.45` 保留形状（淡化），不能整条擦成孤立灰字。
    check("🖥 禁用态保留淡化的圆角框（照 web 版 opacity:.45）",
          "lerp_color(self._bgc, fill" in src
          and "lerp_color(self._bgc, outline" in src)

    # ── 结论 / 备注 + 导出（网页版有、桌面版一度漏掉的两个功能） ──
    # 备注：浮层里能写结论，点动作按钮时**一起提交**（不是「先改状态再补写」两步）。
    # 🔴 备忘没有「结论」这个概念，整块不给它建 —— 所以要有 `if not memo` 这道闸。
    check("🖥 浮层有结论/备注框，改状态时一起提交",
          "结论 / 备注" in src and 'note_box.get("1.0", "end")' in src
          and "def _set_status(self, ids, status, memo, note=None)" in src
          and "_store.set_idea_status(ids, status, note)" in src
          and "if not memo:" in src)
    # 气泡上也要显示结论 —— 但**有才渲染**，没写结论的条目不该平白多占一行。
    check("🖥 气泡显示结论且「有才渲染」",
          '"结论：" + it["note"]' in src and 'if it.get("note"):' in src)
    # 导出：排版复用 export.py（和机器人「导出」指令同一份），格式从扩展名反推。
    check("🖥 导出复用 export.py（不另写一套排版）",
          "def _export_all" in src and "_export.render(" in src
          and "asksaveasfilename" in src and "import export as _export" in src)
    # 库里有图时默认给 zip —— 图片只在 zip 里，默认给 md 会被当成「图没导出来」。
    check("🖥 有图时导出默认选 zip",
          "has_pics" in src and 'default_fmt = "zip" if has_pics else "md"' in src)

    # ── 真机反馈第二轮（2026-10-11）──
    # ① 圆角必须**手算圆弧顶点**。Tk 的 `create_polygon(smooth=True)` 是二次 B 样条，
    #    闭合处会往外鼓一小块 —— 真机上就是卡片左上/右下角那两个「不明像素」。
    check("🖥 圆角手算顶点（smooth 样条会在闭合处鼓出小方块）",
          "smooth=False" in src and "create_polygon(pts, smooth=True" not in src)
    # ② 气泡里的控件是直角矩形，内缩不够就会戳到圆角矩形外面。
    #    0.293r 只是数学下限，实测抗锯齿会在角上留一道细缝 → 取 0.45r。
    check("🖥 气泡内缩 ≥ 半径的 29%（直角角落藏进圆弧）",
          "INSET_RATIO = 0.45" in src
          and "self.BUBBLE_R * self.INSET_RATIO" in src and 'b["inset"]' in src)
    # ③ meta 行不许拼出「备忘 · 备忘」—— 备忘的桶名和类型名本来就是同一个词。
    check("🖥 类型/状态文案去重（不再有「备忘 · 备忘」）",
          "def _meta_text" in src and "bucket_label.startswith(kind_label)" in src)
    # ④ 图标：`iconbitmap(default=…)` / `iconphoto(True, …)` 的 **-default 变体只设
    #    「以后新建窗口」的默认值，不改当前窗口** —— 只写 default 的话当前窗口
    #    永远是 Tk 羽毛（用户报的「左上角和任务栏都不显示金鱼」）。
    check("🖥 当前窗口图标单独设一次（-default 不改当前窗口）",
          "iconphoto(False," in src and "iconbitmap(ico)" in src)
    # ⑤ 滚动范围要用**内容请求高度**。`canvas.bbox("all")` 是 canvas 反算的，
    #    切换分区换过内容后留一截过期余量 → 滚轮能滑出一大串空白。
    check("🖥 滚动范围 = 内容请求高度（不是 canvas.bbox）",
          "def _sync_scrollregion" in src and "self.scroll.winfo_reqheight()" in src
          and "self.canvas.bbox(" not in src)
    # ⑥ 滚轮是 bind_all 的（配置中心 / 脑内抽屉共用一套事件）→ 必须判可见性，
    #    否则在抽屉页滚轮会去滚配置中心。
    check("🖥 滚轮只在配置中心可见时响应",
          "def _on_wheel" in src and "self.canvas.winfo_ismapped()" in src)
    # ⑦ 顶部**不淡出**，改遮罩带 —— 长气泡的上半截不会再在还看得见的时候
    #    先变灰（用户报的「视觉缺口」）。
    check("🖥 顶部不淡出（_alpha_at 只剩底部渐入）",
          "def _alpha_at" in src and "a_out" not in src
          and 'L["h"] * 0.16' not in src)
    # 🔴 遮罩带 / 筛选条必须是**舞台的同级兄弟控件**（place + lift）。
    #    做成 canvas item 是盖不住气泡的：卡片本体是 canvas 的嵌入窗口，而嵌入
    #    窗口在 Windows 上永远画在 canvas 自绘内容之上；`tag_raise()` 对窗口 item
    #    也无效（真机像素实测：遮罩带里采到的还是卡片色）。同级兄弟才能盖住整块舞台。
    check("🖥 遮罩带常驻置顶（气泡滑进来被盖住，不是渐变消失）",
          "TOP_MASK_H" in src and "self._mask.lift()" in src
          and "self._mask.place(x=0, y=0, relwidth=1, height=self.TOP_MASK_H)" in src)
    # 底部现在是**两行**（搜索行 + 按钮行）包在一个 `foot` 里，`foot` 才是舞台的同级兄弟。
    check("🖥 遮罩带 / 底栏是舞台的同级兄弟（canvas 窗口压不住）",
          'foot.place(relx=0, rely=1, anchor="sw", relwidth=1)' in src
          and "self._foot.lift()" in src
          and "self.stage.tag_raise(self._mask)" not in src
          and "self.stage.tag_raise(self._bar)" not in src)
    # ⑧ 正文上限 1000 字（用户要求），行数兜底别让气泡高到离谱。
    check("🖥 气泡正文上限 1000 字",
          "TEXT_MAX = 1000" in src and "LINE_MAX = 45" in src)

    # ── 真机反馈第三轮（2026-10-11 夜）──
    # ⑨ 圆角矩形是**多边形**，每帧只能 `move()` 平移。
    #    `coords(bg_id, x1,y1,x2,y2)` 会把 40 个点截断成 2 个（Tk 实测），
    #    圆角整个消失、只剩一条对角线残影从卡片左上角露出来 —— 用户报的
    #    「两个角的不明像素点」；截图里数出 8 处、颜色正是 border 色 #24303f。
    check("🖥 圆角矩形用 move() 平移（coords 会把多边形截断成 2 点）",
          'self.stage.move(b["bg_id"]' in src
          and 'coords(b["bg_id"], xi, yi' not in src
          and "bx=x0, by=y0" in src)
    # ⑩ 不可见时**照样推进时间轴**，只跳过渲染。以前直接 return，
    #    最小化期间气泡不过期也不补位 → 再打开时集体过期、整屏空。
    _t = src[src.find("    def _tick(self):"):src.find("    def _toggle_pause")]
    check("🖥 抽屉不可见时仍推进时间轴（只跳过渲染）",
          "visible = bool(self.winfo_ismapped())" in _t
          and "if not visible:" in _t and "标签页不可见时不空转" not in src)
    # ⑪ 处理后**增量对齐**，不整屏清空重建 —— 那就是用户报的「抽搐」。
    check("🖥 数据变化走 _sync 增量对齐（不整屏 _clear_stage 重建）",
          "def _sync(self, items)" in src and "_sync(self._load_items())" in src
          and "def _reload(self" not in src and "def _prewarm(" not in src)
    # ⑫ 队列存 **id** 不存下标 —— 条目增删时下标会错位，轮转顺序就乱了。
    check("🖥 pick 队列存 id（下标会随增删错位）",
          "def _pick" in src and "by_id.get(i)" in src
          and "self._items[idx]" not in src)
    # ⑬ 处理完**不弹通知**：卡片从轮播里消失、浮层关掉，本身就是反馈。
    check("🖥 改状态后不弹 toast（用户明确说这条通知无用）",
          'self.app.toast(f"已更新' not in src)

    # ── 「界面字号」缩放（用户：字太小、要能自己调）──
    # 🔴 全应用字号必须都过 `_font()`。写死 `font=("Microsoft YaHei", 9)`
    #    的地方用户调多大都不会动，表现是「有的地方大了、别处还是小的」。
    check("🖥 界面字号有统一缩放助手 _font()",
          "def _font(size, weight=None)" in src and "def _reload_font_scale(" in src
          and "_FONT_FAMILY" in src)
    check("🖥 没有写死的字体元组（全部走 _font()）",
          not re.search(r'font=\("Microsoft YaHei"', src)
          and len(re.findall(r"_font\(", src)) >= 50,
          f"裸字体元组 {src.count('font=(\"Microsoft YaHei\"')} 处")
    check("🖥 字号变了整窗重建（Tk 字号建控件时定死，没法事后改）",
          "def _rebuild_for_font" in src and "_reload_font_scale()" in src
          and "self.root.winfo_children()" in src)
    check("🖥 重建前先熄灯（_alive 旗子挡住还在路上的 _tick）",
          "self._alive = False" in src and "if not self._alive:" in src)

    # ── 第三轮反馈（字被裁 / 浮层撑爆 / 提示不说人话）──
    # 🔴 正文折行宽度必须**从布局常量算**。写死 `int(width) - 48` 时它比真实
    #    可用宽度（`lane_w − 2·inset − 2·ROW_PADX − EDGE_W − EDGE_GAP` = −54）
    #    大 6px，每行最后一个字的右边缘被容器裁掉 —— 用户报的「字边缘消失」。
    check("📐 正文折行宽度由布局常量算（不再是写死的 −48）",
          "content_w = max(120, w_int - inset * 2 - self.ROW_PADX * 2" in src
          and "int(width) - 48" not in src)
    check("📐 排版常量齐全（ROW_PADX / ROW_PADY / EDGE_W / EDGE_GAP）",
          all(k in src for k in ("ROW_PADX = 14", "ROW_PADY = 10",
                                 "EDGE_W = 4", "EDGE_GAP = 10")))
    check("📐 折行宽度确实小于气泡内宽（不留溢出）",
          "wraplength=content_w" in src and "int(width) - 48" not in src)

    # 🔴 浮层：动作条必须固定常显、正文区自己滚、窗口高度封顶。
    #    以前整张卡片一起长高，上千字的记录把窗口顶穿屏幕，
    #    底部「已处理 / 已决议」按钮点不到（用户报的「无法正常更改状态」）。
    check("🪟 浮层动作条挂在固定底栏（不跟正文一起滚）",
          "bar = tk.Frame(bar_wrap, bg=light)" in src
          and 'bar_wrap.pack(side="bottom"' in src)
    check("🪟 浮层高度封顶 + 设了 scrollregion",
          "cvh = min(chc, avail)" in src
          and "scrollregion=(0, 0, cwc, chc)" in src)
    check("🪟 滚轮绑在 Toplevel（不用 bind_all，免得跟配置中心抢）",
          'win.bind("<MouseWheel>", _on_wheel)' in src
          and 'bind_all("<MouseWheel>"' not in src.split("def _open_modal")[1])
    check("🪟 编辑态收起动作条后能放回原位（before=cv）",
          "bar_wrap.pack_forget()" in src and "before=cv" in src)

    # 🔴 保存提示要说人话：以前报「.env 已写 2 项；settings.json 已写 4 项」，
    #    用户不知道那两个文件是什么、也看不出是成功还是写了一半。
    check("💬 保存提示说人话（连接设置 / 机器人设置）",
          "项连接设置" in src and "项机器人设置" in src
          and "已写 {len(keys)} 项" not in src and "已写 {n} 项" not in src)

    # ── 第十轮真机反馈（8 条 bug）──
    # ① 配置中心滚轮：`scrollregion` 比视口矮时 Tk 会把画布原点夹到
    #    `scrollY2 − 视口高`（**负数**）→ 内容整体下推。真机实测内容窗
    #    `winfo_y` 从 0 → 160，而 `yview()` 还报 `(0.0, 1.0)` 像「没滚」。
    #    修法三层：scrollregion 取 max(reqheight, 视口高) / 滚轮加闸 / 切页回顶。
    check("⚙ 滚轮：scrollregion 不小于视口（否则原点被夹成负数→下推）",
          "h = max(self.scroll.winfo_reqheight(), self.canvas.winfo_height())" in src)
    check("⚙ 滚轮：内容装得下时直接不动（不加闸会一直下推触底）",
          "if self.scroll.winfo_reqheight() <= self.canvas.winfo_height():" in src)
    check("⚙ 切分区后回到顶部（否则停在上一个分区的滚动位置）",
          "def _after_swap" in src and "self.canvas.yview_moveto(0)" in src)

    # ② 扩展「指令名撞了」根因：探针子进程 stdout 用 `locale.getpreferredencoding()`
    #    （cp936）写、父进程按 utf-8 解 → 中文指令名乱码 → **乱码后不同指令名
    #    撞成同一字符串** → 误报 dup。修法：探针自己把 stdout/stderr 拧成 utf-8。
    check("🧩 探针子进程强制 UTF-8 输出（否则中文指令名乱码→误报撞名）",
          'sys.stdout.reconfigure(encoding="utf-8"' in _EXTMGR_SRC
          and 'sys.stderr.reconfigure(encoding="utf-8"' in _EXTMGR_SRC)

    # ③ 脑内抽屉加搜索：轮播是「等它飘过来」，搜索是「直接去找」，两条路并存。
    check("🔍 底部有搜索框（回车 / 点按钮都能搜）",
          "self._q_var" in src and "def _search" in src
          and '"<Return>", lambda e: self._search()' in src)
    check("🔍 搜索命中正文 / 标签 / 结论三处",
          "x[\"content\"], x[\"note\"]" in src and "x[\"tags\"]" in src)
    check("🔍 搜索空串给提示、不静默开空窗",
          "先在搜索框里写点什么再搜" in src)

    # ④ 列表窗口：分类 chip / 搜索结果共用，行可点开浮层。
    check("🗂 列表窗口：分类与搜索共用，标题分两支",
          "def _open_list" in src
          and 'f"{_BUCKET_LABEL[bucket]} · 列表"' in src
          and 'f"搜索「{query}」"' in src)
    check("🗂 列表窗口：点行开浮层（先关列表再开卡片）",
          "def _open(it)" in src and "            _close()" in src
          and "self._open_modal(it)" in src)
    check("🗂 列表窗口也支持 Esc / 窗口 X 关闭",
          'win.bind("<Escape>"' in src
          and "WM_DELETE_WINDOW" in src)

    # ⑤ 已有状态的灵感「写决议和备注没办法保存」→ 补保存按钮，且**不动状态**。
    check("✍ 浮层有「保存备注」按钮，且保存不夹带改状态",
          "note_save" in src and "def _save_note" in src
          and '_store.set_idea_status([it["id"]], it["status"], val)' in src)
    check("✍ 备注没改动时保存按钮禁用（避免空写）",
          "def _note_changed" in src and 'state="disabled"' in src)
    check("✍ 结论框自适应高度（按 displaylines 数，不再只显示两行）",
          "def _fit_note" in src
          and 'count("1.0", "end-1c", "displaylines")' in src)

    # ⑥ 回收站勾选框：iOS 拨杆看着像开关（用户「一下子都没看懂」），改方框打勾。
    #    ⚠️ 只锁**回收站这一处** —— 配置中心那边的开关本来就是拨杆语义，是对的。
    _trash_fn = src.split("    def _open_trash(self):")[1].split("    def _resolve(")[0]
    check("☑ 回收站勾选用方框打勾（不用 iOS 拨杆 toggle）",
          'bootstyle="primary-round-toggle"' not in _trash_fn
          and 'bootstyle="primary"' in _trash_fn)

    # ⑦ 浮层标题文案：脑内抽屉 → 思维卡片。
    check("🏷 浮层标题改成「思维卡片」",
          'win.title("思维卡片")' in src and 'win.title("脑内抽屉")' not in src)

    # ⑧ 编辑后尺寸突变 + 拉到极小丢内容：设 minsize 下限 + 摆窗用 req 尺寸。
    check("📏 浮层设了尺寸下限（编辑态不塌、不丢内容）",
          "_floor" in src and "win.minsize(" in src)
    check("📏 摆窗位置用 max(req, 实际)（否则设了 minsize 会摆出屏幕）",
          "max(win.winfo_reqwidth(), win.winfo_width())" in src
          and "max(win.winfo_reqheight(), win.winfo_height())" in src)

    # ⑨ 泳道密度 3 → 6，且**必须**配真实重叠判据，否则同道会叠。
    check("📐 同道不重叠用真实位置判据（_lane_span 跟每个现有气泡比）",
          "def _lane_span" in src
          and "b[\"y_start\"] - b[\"travel\"] * (now - b[\"t0\"]) / b[\"dur\"]" in src
          and "bot + self.MIN_GAP > t and top - self.MIN_GAP < t + b[\"h\"]" in src)
    check("📐 _spawn 先量出真实高度再挑泳道（frac 那条路不预挑）",
          "if lane is None and frac is None:" in src
          and "free = [ln for ln in range(L[\"lanes\"])" in src)
    # 🔴 「起点」与「出生位置」必须自洽：y_start 永远是舞台下方，frac 只改 y0，
    #    warm 由两者之差算出。以前 y_start 直接写 y0、warm 却按「从下方走到 y0」
    #    算，真实出生位置成了 `2*y0 − 舞台高 − 8` —— 首屏各段被压到屏幕上方、
    #    密集重叠，而 `_lane_span` 判的还是 y0，拦不住（用户报「卡片重叠」）。
    check("📐 起点 y_start 与出生位置 y0 自洽（warm 由两者之差算）",
          "y_start = L[\"h\"] + 8 + random.uniform(0.0, 120.0)" in src
          and "y0 = y_start" in src
          and "travel = y_start + h + 16" in src
          and "warm = (y_start - y0) / travel" in src
          and 'warm = (L["h"] + 8 - y0) / travel if frac is not None else 0.0'
          not in src)
    check("📐 气泡字典存的是真起点（y_start=y_start，不是 y0）",
          "y_start=y_start" in src and "y_start=y0" not in src)
    check("📐 底部变两行后，气泡条高按实际底栏高算（不写死 BAR_H）",
          "def _bar_h" in src and "L[\"h\"] - self._bar_h()" in src)


def test_image_collect():
    """[23] 收图：attachments 解析 + 开关闸门。

    🔴 这组是给一个**确定性死 bug** 兜底的：`bot.image_urls()` 原来写
       `if not isinstance(a, dict): continue`，而 botpy 的 `message.attachments`
       是 `_Attachments` **对象**列表 ⇒ 每一项都被 continue 掉 ⇒ 恒返回空数组
       ⇒ 收图功能整个是死的（用户报「发了图片但抽屉里始终没有」）。
       所以这里既测行为、也钉住源码里不许再出现「只认 dict」的写法。
    """
    print("\n[23] 收图：attachments 解析 + 开关闸门")
    import bot

    class _Att:                       # 仿 botpy.message.BaseMessage._Attachments
        def __init__(self, ct=None, url=None):
            self.content_type, self.url = ct, url

    def msg(atts):
        return SimpleNamespace(attachments=atts)

    check("认 botpy 的附件**对象**（不是 dict）",
          bot.image_urls(msg([_Att("image/jpeg", "https://x/a.jpg")]))
          == ["https://x/a.jpg"])
    check("dict 形态也认（兼容）",
          bot.image_urls(msg([{"content_type": "image/png",
                               "url": "https://x/b.png"}])) == ["https://x/b.png"])
    check("非图片附件跳过",
          bot.image_urls(msg([_Att("video/mp4", "https://x/v.mp4")])) == [])
    check("没有 url 的附件跳过", bot.image_urls(msg([_Att("image/jpeg", "")])) == [])
    check("content_type 认不出也放行（QQ 偶尔报 octet-stream）",
          bot.image_urls(msg([_Att("application/octet-stream", "https://x/c")]))
          == ["https://x/c"])
    check("content_type 带参数（image/jpeg; charset=…）照样认",
          bot.image_urls(msg([_Att("image/jpeg; charset=utf-8", "https://x/d")]))
          == ["https://x/d"])
    check("没有 attachments 字段 → 空数组", bot.image_urls(SimpleNamespace()) == [])
    check("attachments 是 None → 空数组", bot.image_urls(msg(None)) == [])
    _many = [_Att("image/jpeg", f"https://x/{i}") for i in range(20)]
    check(f"一条消息最多收 {bot._media.MAX_PER_MSG} 张",
          len(bot.image_urls(msg(_many))) == bot._media.MAX_PER_MSG)

    # 源码护栏：不许再回到「只认 dict」的写法
    src = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
    check("🔒 bot.py 里没有「只认 dict」的旧写法",
          "if not isinstance(a, dict):" not in src)
    check("🔒 取值助手 _att() 同时认对象与 dict",
          "def _att(a, name):" in src and "return getattr(a, name, None)" in src)

    # 开关闸门：没开这个 scope 时**连解析都不做**（省得白下）
    _orig_get = bot._settings.get
    bot._settings.get = lambda k, *a, **kw: (
        ["c2c"] if k == "image_collect" else _orig_get(k, *a, **kw))
    try:
        check("开关没开这个 scope → 不收（group 不在 [c2c] 里）",
              bot.collect_images(msg([_Att("image/jpeg", "https://x/a.jpg")]),
                                 "group") == [])
        bot._settings.get = lambda k, *a, **kw: (
            [] if k == "image_collect" else _orig_get(k, *a, **kw))
        check("开关整体关着（空数组）→ 私聊也不收",
              bot.collect_images(msg([_Att("image/jpeg", "https://x/a.jpg")]),
                                 "c2c") == [])
    finally:
        bot._settings.get = _orig_get


# ── [24] 本地扩展：查运势（个人文件，仓库里没有 → 没有就跳过）────────
def test_local_fortune():
    print("\n[24] 本地扩展 · 查运势 的容错")
    path = os.path.join(ROOT, "extensions", "local", "fortune.py")
    if not os.path.isfile(path):
        print("  （extensions/local/fortune.py 不在 —— 个人文件不进仓库，跳过）")
        return

    src = open(path, encoding="utf-8").read()
    _find_body = src.split("def _fortune_path")[1].split("def _today_rhythm")[0]
    check("🔒 兜底搜索被 try 住（搜索根里出坏文件不该让指令报错）",
          "except Exception:" in _find_body)
    check("🔒 handler 自己兜住异常（不把原始 traceback 甩给用户）",
          "（查运势失败：" in src)

    spec = importlib.util.spec_from_file_location("_fortune", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    _orig_file = mod.FORTUNE_FILE
    _orig_cache = dict(mod._FORTUNE_CACHE)

    def _boom(*_a, **_kw):
        raise ValueError("path is on mount '\\\\.\\nul', start on mount 'c:'")

    class _Reg:
        def __init__(self, api):
            self.api = api
            self.cmds = []

        def add(self, names, fn, help=""):        # noqa: A002
            self.cmds.append((list(names), fn))

    try:
        mod.FORTUNE_FILE = ""
        mod._FORTUNE_CACHE.update(path=None, ts=0.0)
        # ① 搜索炸了 → 降级成「未配置」，不抛
        txt = mod.fortune_text(SimpleNamespace(search_files=_boom))
        check("搜索抛 ValueError 时降级成「未配置」而不是崩",
              "未配置" in txt, f"got={txt!r}")

        # ② 真正的入口（handler）也兜住
        reg = _Reg(SimpleNamespace(search_files=_boom))
        mod.register(reg)
        check("register 挂上了指令", len(reg.cmds) == 1 and "查运势" in reg.cmds[0][0])
        _handle = reg.cmds[0][1]
        mod._FORTUNE_CACHE.update(path=None, ts=0.0)
        out = _handle("查运势", False)
        check("handler 正常路径返回字符串", isinstance(out, str) and out, f"got={out!r}")

        _real_text = mod.fortune_text
        mod.fortune_text = _boom
        try:
            out2 = _handle("查运势", False)
            check("handler 内部异常 → 人话提示（含类型名）",
                  out2.startswith("（查运势失败：") and "ValueError" in out2, f"got={out2!r}")
        finally:
            mod.fortune_text = _real_text
    finally:
        mod.FORTUNE_FILE = _orig_file
        mod._FORTUNE_CACHE.update(_orig_cache)


# ── [25] 子进程不许弹控制台窗口（noWindow）─────────────────────────
#    `explorer /select,` 是**故意**要窗口的，白名单放行。
_NO_WINDOW_WHITELIST = ("explorer",)


def _subproc_calls_without_flags(path):
    """AST 扫一个文件，返回**没带 creationflags（也没有 `**kwargs`）**的子进程调用行号。

    `**kwargs` 也算合格 —— `extmgr` 那种写法是 `**_no_window()`，AST 里 keywords
    的 `arg` 是 `None`，光看名字会误报。
    """
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute)
                and f.attr in ("run", "Popen", "call", "check_output", "check_call")
                and isinstance(f.value, ast.Name) and f.value.id == "subprocess"):
            continue
        if any(k.arg == "creationflags" or k.arg is None for k in node.keywords):
            continue
        seg = ast.get_source_segment(src, node) or ""
        if any(w in seg for w in _NO_WINDOW_WHITELIST):
            continue
        bad.append(node.lineno)
    return bad


def test_no_window():
    print("\n[25] 子进程 noWindow（桌面版不该闪黑窗）")
    import botctl

    check("CREATE_NO_WINDOW 常量 = 0x08000000",
          botctl._no_window_flags() == (0x08000000 if os.name == "nt" else 0))

    targets = ["botctl.py", "hub.py", "extmgr.py", "md2img.py", "memo_gui.py", "config-web.py"]
    _local = os.path.join(ROOT, "extensions", "local")
    if os.path.isdir(_local):
        targets += [os.path.join("extensions", "local", n)
                    for n in sorted(os.listdir(_local)) if n.endswith(".py")]
    for rel in targets:
        p = os.path.join(ROOT, rel)
        if not os.path.isfile(p):
            continue
        bad = _subproc_calls_without_flags(p)
        check(f"{rel} 的子进程调用都带 creationflags / **_no_window()",
              not bad, f"缺标志的行号：{bad}")


def main():
    print("=" * 60)
    print("memo-bot 回归测试")
    print("=" * 60)
    test_moyu_parse()
    test_moyu_draw()
    test_store()
    # MEMO_SKIP_BOT_TESTS=1 是逃生口：test_bot 会 os.walk 整个工作区。
    # Windows 保留设备名文件（`nul`）以前会把这一组直接搞崩，现在搜索侧已经
    # 跳过这类名字 + 兜住 relpath 的 ValueError，正常不该再需要这个开关；
    # 留着是给「工作区里有别的工具处理不了的怪文件」兜底。
    if os.environ.get("MEMO_SKIP_BOT_TESTS") == "1":
        print("[4] bot 工具函数 —— 跳过（MEMO_SKIP_BOT_TESTS=1）")
    else:
        test_bot()
    test_send_file()
    test_md2img()
    test_ideas_bubbles()
    test_extensions()
    test_ext_manager()
    test_ignore()
    test_route()
    test_settings()
    test_config_web()
    test_console_encoding()
    test_boomerang_export()
    test_data_dir_isolation()
    test_onboarding()
    test_moyu_library()
    test_push_targets()
    test_gui_source()
    test_image_collect()
    test_local_fortune()
    test_no_window()
    test_gui_speed()
    test_gui_update_check()
    print("\n" + "=" * 60)
    if FAILS:
        print(f"❌ {len(FAILS)}/{TOTAL[0]} 项失败：")
        for f in FAILS:
            print(f"   - {f}")
        sys.exit(1)
    print(f"✅ 全部通过（{TOTAL[0]} 项）")


if __name__ == "__main__":
    main()
