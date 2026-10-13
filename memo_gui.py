"""摸鱼bot 本地应用界面（原生 Tkinter / ttkbootstrap）。

干嘛的：
  把原来「配置中心网页」和「脑内抽屉」搬进一个
  本地桌面窗口。跑起来后不用再开浏览器访问 webui —— 配置、看抽屉都在这个窗口里完成。
  本文件只负责「界面 + 把用户操作翻译成对 bot 代码的调用」，所有真正的读写都复用
  config_core.py / settings.py / push.py / ignore.py / moyu.py / botctl 里的纯逻辑函数，
  不重复造轮子，也不起任何 HTTP 服务。

谁调用它：
  - 打包后的桌面程序入口（memo-launcher.py 在合适的时候拉起它）；
  - 开发期也可直接 `python memo-gui.py` 跑起来看效果。

设计基线：
  - 配置中心内部 9 个分区用「左侧分区列表」导航；
  - 默认主题跟随系统（Windows 亮/暗），品牌色用工作室统一基调（蓝绿 #0f8f85 / #4fd1c5）。
"""

import importlib.util
import math
import os
import random
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.filedialog as filedialog
import tkinter.font as tkfont
import tkinter.messagebox as messagebox
import tkinter.scrolledtext as scrolledtext
import webbrowser
import ttkbootstrap as ttk
from ttkbootstrap.constants import *

# 把脚本所在目录钉进 sys.path / 工作目录，本地模块（store / media / config_core）才能稳定导入。
HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# 数据层：复用 bot 自己的 store / media（纯逻辑，不起任何服务）。
import store as _store
import media as _media
import export as _export       # 导出：与机器人「导出」指令、网页版共用同一套排版
import settings as _settings   # 设置项：脑内抽屉的展示范围 / 回旋镖规则都在这
import extmgr as _extmgr       # 自定义指令：扩展包的导入 / 列出 / 移除
import botctl as _botctl       # 进程 / 计划任务查询（首屏靠它的非阻塞接口提速）
import diag as _diag           # 诊断包：环境 + 配置摘要 + 日志尾巴打成一个 zip
from paths import data_path, data_dir  # noqa: E402  （gui.pid 单实例锁 / 主题偏好落数据目录）

# 单实例锁（和 hub.pid / bot.pid 一个套路）：第二次启动顶到前面，不各开一份
_GUI_PID_FILE = data_path("gui.pid")


# ── 界面字号缩放 ─────────────────────────────────────────────────
# 🔴 所有字号都必须走 `_font()`，别再写 `font=_font(9)` 这种裸字号 ——
#    裸字号不受「配置中心 → 脑内抽屉 → 界面字号」控制，用户调大了它也不动，
#    表现就是「改了个别地方字大了、别处还是小的」。
_FONT_FAMILY = "Microsoft YaHei"
_FONT_SCALE = 100          # 当前缩放百分比，由 _reload_font_scale() 刷新
_FONT_MIN, _FONT_MAX = 80, 200
_FONT_FLOOR = 8            # 再小也不能小于 8pt，否则中文糊成一团


def _reload_font_scale():
    """从设置里读「界面字号（%）」并缓存，返回**是否变了**。

    干嘛的：字号是全应用统一的一个缩放系数，改完要重建界面才看得到效果。
    谁调用它：`App.refresh()`（每次保存 / 切页签都会走到）。
    """
    global _FONT_SCALE
    try:
        v = int(_settings.get("ui_font_scale") or 100)
    except (TypeError, ValueError):
        v = 100
    v = max(_FONT_MIN, min(_FONT_MAX, v))
    changed = v != _FONT_SCALE
    _FONT_SCALE = v
    return changed


def _font(size, weight=None):
    """按当前缩放算出一个字体元组（给 tk / ttk 的 `font=` 用）。

    干嘛的：全应用的界面字号统一过这里，用户才能在设置里整体调大调小。
    谁调用它：本文件里所有建控件、配样式的地方。
    """
    n = max(_FONT_FLOOR, int(round(size * _FONT_SCALE / 100.0)))
    return (_FONT_FAMILY, n, weight) if weight else (_FONT_FAMILY, n)


def _raise_widget(w):
    """把控件提到同级控件的栈顶 —— **对 Canvas 控件必须走这里**。

    干嘛的：需要「盖在舞台上面」的同级兄弟控件（例如底部的筛选条）建好后置顶。
    谁调用它：目前没有调用点（顶部遮罩带已弃用，见 BrainDrawerTab 的说明）；
            留着是给以后新增浮层用的工具。
    🔴 本机打包的 tkinter 把 `Canvas.lift = tkraise = tag_raise`（画布**条目**
        升降，要求 tagOrId）—— 对 Canvas 控件调 `.lift()` / `.tkraise()` 会抛
        `wrong # args: should be … raise tagOrId ?aboveThis?`，抽屉一 build 就
        炸（真机探针实测）。全局 `raise` 对任何控件类型都对。
    """
    w.tk.call("raise", str(w))


def _clamp_win(win, x, y, w, h, margin=8):
    """把顶级窗口的 x/y 夹取进屏幕，返回夹好后的 `(x, y)`。

    干嘛的：主窗口贴在屏幕右下角时，「主窗坐标 + 120/+150」那套偏移会把
            列表窗 / 回收站整个推出屏幕 —— 用户眼里只剩一条窗边，就是反馈
            单里的「只显示一条」（真机：主窗最大化或靠右下时必现）。
    谁调用它：_open_list / _open_trash 摆窗口位置时。
    🔴 多屏按**虚拟屏**算（winfo_screenwidth/height 就是整个虚拟桌面的尺寸），
       负坐标屏也覆盖；任务栏高度不可知，只按屏幕边界夹、不猜任务栏。
    """
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    return (min(max(margin, int(x)), max(margin, sw - w - margin)),
            min(max(margin, int(y)), max(margin, sh - h - margin)))


def load_config_web():
    """把 config_core.py（配置后端）当模块加载并返回。

    桌面端的配置中心 / 脑内抽屉 / 运行方式 / 版本四个页签全部走它提供的纯逻辑函数
    （full_state / write_env / bot_start …）。原先的网页后端已抽成 config_core.py，
    后端逻辑独立成 config_core.py，这里 import 不会拉起任何服务。
    """
    spec = importlib.util.spec_from_file_location(
        "config_web", os.path.join(HERE, "config_core.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── 系统主题检测 ───────────────────────────────────────────────────
def detect_system_dark():
    """返回 True 表示 Windows 当前是暗色模式（读注册表 AppsUseLightTheme）。

    读不到（非 Windows / 权限）时默认亮色，避免一上来就黑。
    """
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return val == 0
    except Exception:
        return False


# ── 主题偏好持久化（浅色 / 深色 / 跟随系统）───────────────────────
# 单独落一个小文件进数据目录，不碰 .env / settings.json（那是连接与机器人设置）。
_THEME_FILE = "ui-theme.txt"


def load_ui_theme():
    """读主题偏好：'light' / 'dark' / 'system'，读不到或非法默认 'system'。"""
    try:
        with open(os.path.join(data_dir(), _THEME_FILE), encoding="utf-8") as f:
            v = (f.read() or "").strip()
        if v in ("light", "dark", "system"):
            return v
    except Exception:
        pass
    return "system"


def save_ui_theme(mode):
    """把主题偏好写进数据目录（失败静默，不影响切换）。"""
    try:
        with open(os.path.join(data_dir(), _THEME_FILE), "w",
                  encoding="utf-8") as f:
            f.write(mode)
    except Exception:
        pass


def win_app_id(appid="PhononPrismLab.MoyuBot.Desktop"):
    """Windows：给当前进程登记一个独立的 AppUserModelID。

    🔴 不登记的话任务栏会按宿主进程（pythonw.exe）分组，并显示 Tk 自带的
    羽毛图标 —— `iconbitmap` / `iconphoto` 只在窗口标题栏生效，落不到任务栏。
    必须在**创建任何窗口之前**调用。
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(appid)
    except Exception:
        pass


# ── 品牌色主题（极简白基调，蓝绿只做点缀）────────────────────────
# 浅色：近白底 #f7f9fa / 纯白卡片 / 极浅灰线 / 深灰字，brand #0f8f85 只用于选中态与主按钮
# 深色：同结构的深色变体，跟随系统暗色时使用
BRAND_LIGHT = {
    "primary": "#0f8f85", "secondary": "#8a94a3", "success": "#1a9e77",
    "info": "#0ea5b7", "warning": "#d9933b", "danger": "#e05656",
    "light": "#ffffff", "dark": "#1c2530", "bg": "#f7f9fa", "fg": "#1c2530",
    "selectbg": "#0f8f85", "selectfg": "#ffffff", "border": "#e6eaee",
    "inputfg": "#1c2530", "inputbg": "#ffffff", "active": "#eef4f3",
}
BRAND_DARK = {
    "primary": "#4fd1c5", "secondary": "#8b98a9", "success": "#34d399",
    "info": "#38bdf8", "warning": "#f6ad55", "danger": "#fc8181",
    "light": "#182130", "dark": "#0b1017", "bg": "#10161f", "fg": "#e8eef4",
    "selectbg": "#4fd1c5", "selectfg": "#0b1017", "border": "#24303f",
    "inputfg": "#e8eef4", "inputbg": "#182130", "active": "#1d2836",
}

THEME_LIGHT = "brand-light"
THEME_DARK = "brand-dark"

# ── 泳道气泡卡片配色（独立于浮层卡片，让气泡「浮起来」）──────────────
# 浅色：纯白面 + 淡青描边 + 青灰投影；深色：抬升的深面 + 更深的描边 + 黑投影。
# 用一套专属值，不蹭 modal 的 light/border —— 气泡要的是「比背景亮一档且有影」，
# 而 modal 是贴在窗口上的实卡，两者观感诉求不同。
BUBBLE_SURF_LIGHT = "#ffffff"
BUBBLE_SURF_DARK = "#1b2735"
BUBBLE_BORDER_LIGHT = "#cfe3ee"
BUBBLE_BORDER_DARK = "#2c3a4d"
BUBBLE_SHADOW_LIGHT = "#a9c2cf"
BUBBLE_SHADOW_DARK = "#000000"
BUBBLE_SHADOW_OFS = (3, 5)   # 投影相对卡片的偏移（右下），营造悬浮感
BUBBLE_BORDER_W = 2          # 气泡描边粗细（状态色那道边），用户 2026-10-12 要求加粗


def bubble_palette(theme):
    """按当前主题返回 (surf, border, shadow) 三个 hex。"""
    dark = theme == THEME_DARK
    return (BUBBLE_SURF_DARK if dark else BUBBLE_SURF_LIGHT,
            BUBBLE_BORDER_DARK if dark else BUBBLE_BORDER_LIGHT,
            BUBBLE_SHADOW_DARK if dark else BUBBLE_SHADOW_LIGHT)


def register_brand_themes(style):
    """把两套品牌主题注册进 ttkbootstrap 的 Style 单例里。"""
    from ttkbootstrap.style.theme import ThemeDefinition
    style.register_theme(ThemeDefinition(THEME_LIGHT, dict(BRAND_LIGHT), mode="light"))
    style.register_theme(ThemeDefinition(THEME_DARK, dict(BRAND_DARK), mode="dark"))


# ── 纯数据层（不依赖 Tk，可在无显示环境单测）───────────────────────
def section_fields(state, group_key):
    """从 full_state() 里挑出属于某个分区的「可编辑字段」列表。

    返回 [{target,key,label,help,kind,value,set,default,choices,choice_labels,hidden}, ...]。
    self-drawn（hidden）字段也包含在内，调用方决定是否自绘。
    """
    return [f for f in state["fields"] if f.get("group") == group_key]


def primary_button_state(bot):
    """根据 bot 状态算「启动/停止/重启」三个按钮哪个可用。"""
    running = bool(bot.get("running"))
    return {"running": running,
            "start": not running, "stop": running, "restart": running}


def as_set(value):
    """把 settings 的多选值（可能是 list 或 'a,b' 字符串）归一成字符串集合。"""
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        return set(str(x) for x in value)
    return set(x for x in str(value).replace(" ", "").split(",") if x)


def lerp_color(c1, c2, t):
    """两个 #rrggbb 颜色按 t∈[0,1] 插值（泡泡渐入动画用）。"""
    def h2r(h):
        h = h.lstrip("#")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    a, b = h2r(c1), h2r(c2)
    return "#%02x%02x%02x" % tuple(
        max(0, min(255, round(a[i] + (b[i] - a[i]) * t))) for i in range(3))


def dim_ink(fg, bg, t=0.55):
    """「禁用 / 占位」用的灰：比正文淡，但**看得见**。

    🔴 别拿 `border` 当这个灰 —— 它是**边框色**，和底色几乎一样（深色主题下
    边框 #24303f、底 #10161f），拿它写文字等于把字擦掉。真机踩过：浮层里
    「还没打标签」和灰掉的流程按钮**完全看不见**。
    """
    return lerp_color(fg, bg, t)


def round_rect(cv, x1, y1, x2, y2, r=12, **kw):
    """在 Canvas 上画一个圆角矩形，返回 item id。

    干嘛的：Tk 原生控件全是直角矩形，卡片 / 按钮的圆角只能自己在 Canvas 上画。
    谁调用它：`RoundedButton._draw`、`BrainDrawerTab` 的气泡底、浮层的卡片底。

    🔴 **别用 `create_polygon(smooth=True)`**：Tk 的平滑是**二次 B 样条**，
       曲线不过控制点，而且闭合处（起点 ↔ 终点）会往外鼓出一小块 ——
       真机上就是卡片**左上角 / 右下角那两个不明像素方块**（用户报的）。
       这里改成**手算圆弧顶点 + smooth=False**：每个角 steps 段，
       点位完全可控，画出来是真正的圆角，边缘也不会有过冲。
    r 会自动夹到矩形短边的一半以内，小尺寸也不画崩。
    """
    r = max(0, min(int(r), int((x2 - x1) // 2), int((y2 - y1) // 2)))
    if r <= 0:
        return cv.create_rectangle(x1, y1, x2, y2, **kw)
    # 半径越小用越少的段数就够了（小按钮 36 个顶点纯属浪费）
    steps = max(3, min(10, r // 2 + 3))
    pts = []
    # 四个角的圆心 + 起始角（Tk 的 y 轴朝下，所以角度顺时针递增）
    for cx, cy, a0 in ((x2 - r, y1 + r, -90.0),     # 右上
                       (x2 - r, y2 - r, 0.0),       # 右下
                       (x1 + r, y2 - r, 90.0),      # 左下
                       (x1 + r, y1 + r, 180.0)):    # 左上
        for i in range(steps + 1):
            a = math.radians(a0 + 90.0 * i / steps)
            pts += [cx + r * math.cos(a), cy + r * math.sin(a)]
    return cv.create_polygon(pts, smooth=False, **kw)


def round_rect_bottom(cv, x1, y1, x2, y2, r=12, **kw):
    """圆角矩形，只圆**底边**两角：顶边是直线（贴着上面的卡片底，填住它的凹角），
    左右下角圆弧。`round_rect` 的对偶 —— 浮层里上下两半拼成一个完整圆角卡用。
    """
    r = max(0, min(int(r), int((x2 - x1) // 2), int((y2 - y1) // 2)))
    if r <= 0:
        return cv.create_rectangle(x1, y1, x2, y2, **kw)
    steps = max(3, min(10, r // 2 + 3))
    pts = [x1, y1, x2, y1]          # 顺时针：左上 → 右上（顶边直线）
    for i in range(steps + 1):      # 右下弧：0° → 90°
        a = math.radians(90.0 * i / steps)
        pts += [x2 - r + r * math.cos(a), y2 - r + r * math.sin(a)]
    for i in range(steps + 1):      # 左下弧：90° → 180°
        a = math.radians(90.0 + 90.0 * i / steps)
        pts += [x1 + r + r * math.cos(a), y2 - r + r * math.sin(a)]
    pts += [x1, y1]
    return cv.create_polygon(pts, smooth=False, **kw)


def round_rect_top(cv, x1, y1, x2, y2, r=12, **kw):
    """圆角矩形，只圆**顶边**两角：底边直线，左右上角圆弧。`round_rect_bottom`
    的对偶 —— 浮层里上下两半拼成一个完整圆角卡用（上卡圆顶、下条圆底，中间
    那段接缝是直线，看着像同一张卡）。"""
    r = max(0, min(int(r), int((x2 - x1) // 2), int((y2 - y1) // 2)))
    if r <= 0:
        return cv.create_rectangle(x1, y1, x2, y2, **kw)
    steps = max(3, min(10, r // 2 + 3))
    pts = [x1, y2, x2, y2, x2, y1 + r]   # 左下 → 右下 → 右上（底边 + 右边直线）
    for i in range(steps + 1):           # 右上弧：0° → -90°
        a = math.radians(-90.0 * i / steps)
        pts += [x2 - r + r * math.cos(a), y1 + r + r * math.sin(a)]
    pts += [x1 + r, y1]                  # 顶边到左上弧起点
    for i in range(steps + 1):           # 左上弧：-90° → -180°
        a = math.radians(-90.0 - 90.0 * i / steps)
        pts += [x1 + r + r * math.cos(a), y1 + r + r * math.sin(a)]
    pts += [x1, y2]
    return cv.create_polygon(pts, smooth=False, **kw)


class RoundedButton(tk.Canvas):
    """圆角扁平按钮：Tk 原生按钮画不出圆角，统一用 Canvas 自绘。

    干嘛的：GUI 里所有可点的按钮（nav 分区 / 筛选 chips / 保存 / 启停 /
    浮层操作），替代 tk.Button 与 ttk.Button。
    谁调用它：ConfigCenter、BrainDrawerTab。

    形态（kind）：
    · ghost：平时透明只显文字，hover 显淡底（导航、chips、次级操作）；
      `set_selected(True)` 后换成选中底色 + 粗体。
    · solid：常显圆角实心底（主操作），hover 微调底色，
      支持 `configure(state="disabled")` 灰化。
    · 传 `outline` 就是**描边样式**（浅底 + 细边框），hover 时边框与文字
      一起换成 `hover_outline` / `hover_fg` —— 浮层底部那排流程按钮用它，
      和 web 版抽屉的 `.actions button:hover{border-color:…;color:…}` 一致。
    """

    def __init__(self, master, text, command=None, *, kind="ghost",
                 font=_font(10), radius=11, padx=14, pady=6,
                 fg="#5b7b90", fill=None, hover_fill=None,
                 outline=None, hover_outline=None, hover_fg=None,
                 selected_fg=None, selected_fill=None, disabled_fg=None,
                 bold_selected=True, anchor="center", fixed_width=None,
                 parent_bg=None):
        self._text, self._command, self._kind = text, command, kind
        self._font, self._radius = font, radius
        self._padx, self._pady, self._anchor = padx, pady, anchor
        self._fg, self._fill, self._hover = fg, fill, hover_fill
        self._outline, self._hover_outline = outline, hover_outline
        self._hover_fg = hover_fg
        self._sel_fg, self._sel_fill = selected_fg, selected_fill
        self._dis_fg, self._bold_sel = disabled_fg, bold_selected
        self._selected = False
        self._hover_on = False
        self._state = "normal"
        if parent_bg is None:
            try:
                parent_bg = master["bg"]
            except (tk.TclError, KeyError):
                parent_bg = fill or fg
        self._bgc = parent_bg
        f = tkfont.Font(font=font)
        self._tw = f.measure(text)
        w = fixed_width or (self._tw + padx * 2)
        h = f.metrics("linespace") + pady * 2
        self._fixed_w = fixed_width
        super().__init__(master, width=w, height=h, highlightthickness=0,
                         bd=0, bg=parent_bg, cursor="hand2")
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self._draw()

    # -- 状态 --
    def set_selected(self, on):
        if self._selected != bool(on):
            self._selected = bool(on)
            self._draw()

    def set_text(self, text):
        if text != self._text:
            f = tkfont.Font(font=self._font)
            self._text = text
            self._tw = f.measure(text)
            if not self._fixed_w:
                self.configure(width=self._tw + self._padx * 2)
            self._draw()

    def configure(self, **kw):                     # noqa: A003（与 tk 习惯一致）
        redraw = False
        if "state" in kw:
            st = kw.pop("state")
            self._state = st
            # 🔴 必须重绘！`__init__` 里已经按「正常态」画过一遍了，改 state
            #    只动内部字段的话，按钮会**一直显示成可点的样子**，直到别的事件
            #    （最典型就是鼠标移上去触发 _set_hover）碰巧把它重画一次才突然变灰。
            #    真机就是这样：鼠标扫过一次，按钮「啪」地变成一段灰色文字。
            redraw = True
            super().configure(cursor="hand2" if st == "normal" else "arrow")
        if "text" in kw:
            self.set_text(kw.pop("text"))
        for k, attr in (("fg", "_fg"), ("fill", "_fill"),
                        ("hover_fill", "_hover"), ("hover_fg", "_hover_fg"),
                        ("outline", "_outline"),
                        ("hover_outline", "_hover_outline"),
                        ("selected_fg", "_sel_fg"),
                        ("selected_fill", "_sel_fill")):
            if k in kw:
                setattr(self, attr, kw.pop(k))
                redraw = True
        if kw:
            super().configure(**kw)
        if redraw:
            self._draw()

    # -- 内部 --
    def _on_click(self, _e):
        if self._state == "normal" and self._command:
            self._command()

    def _set_hover(self, on):
        self._hover_on = on
        self._draw()

    def _draw(self):
        w, h = int(self["width"]), int(self["height"])
        self.delete("all")
        fg, fill, outline, bold = self._fg, self._fill, self._outline, False
        if self._state == "disabled":
            # 禁用态照 web 版的 `opacity:.45` 做：**保留形状**（底色 + 边框），
            # 整体往背景色插值变淡 —— 一眼仍认得出「这是流程里的一站，你现在在这」。
            # 🔴 别把边框和底色整条去掉 —— 那样它就成了一行孤立灰字，不像按钮、
            #    也丢掉了「过程控制」的观感（用户报过）。
            # 🔴 两个系数不一样：底色退得更多（往里收），边框只淡一点（留住轮廓）。
            #    边框本身就和卡片底色很近（#24303f vs #182130），跟着一样退就看不见了。
            fg = self._dis_fg or dim_ink(self._fg, self._bgc)
            if fill:
                fill = lerp_color(self._bgc, fill, 0.30)
            if outline:
                outline = lerp_color(self._bgc, outline, 0.75)
        elif self._selected and self._sel_fg is not None:
            fg, fill, outline, bold = (self._sel_fg, self._sel_fill,
                                       self._sel_fill, self._bold_sel)
        elif self._hover_on:
            fill = self._hover
            outline = self._hover_outline or outline
            fg = self._hover_fg or fg
        if fill or outline:
            round_rect(self, 1, 1, w - 1, h - 1, self._radius,
                       fill=fill or "", outline=outline or fill, width=1)
        font = self._font[:2] + ("bold",) if bold else self._font
        tx = self._padx if self._anchor == "w" else w // 2
        self.create_text(tx, h // 2, text=self._text, fill=fg, font=font,
                         anchor="w" if self._anchor == "w" else "center")


# ── 主窗口 ────────────────────────────────────────────────────────
class App:
    def __init__(self):
        # 🔴 必须在建窗口之前：任务栏图标认的是进程 AUMID，晚一步就没用了
        win_app_id()
        self._poll_job = None       # 顶栏「计划任务」后台轮询的定时器 id
        self.cw = load_config_web()
        # 主题偏好：'light' / 'dark' / 'system'（默认跟随系统）。
        # 读不到就退化为跟随系统，别因为偏好文件坏了就黑屏。
        self.theme_mode = load_ui_theme()
        self.theme = self._resolve_theme()
        # 🔴 ttkbootstrap 强制单根：先建唯一的 Window（它内部会建 root + style），
        # 再从 Window 上拿 style 注册品牌主题，绝不能在建 Window 之前另建 Style（会多一个 root）。
        self.root = ttk.Window(
            title="摸鱼bot", themename="flatly", size=(1080, 720))
        # 🔴 ttkbootstrap 的 Window 构造里会把进程 AUMID 改成它自己的
        #    "ttkbootstrap.app"，覆盖掉最前面设的值 —— 任务栏于是显示 Tk 默认的
        #    羽毛图标、窗口图标落不到任务栏。必须在 Window 建好之后再钉一次。
        win_app_id()
        self.root.minsize(960, 640)
        self.style = self.root.style
        register_brand_themes(self.style)
        self.style.theme_use(self.theme)
        _reload_font_scale()      # 先读一次用户设的界面字号，首屏就按它建
        self._flat_styles()
        self._sync_system_poll()  # 跟随系统模式下开始盯注册表

    def _flat_styles(self):
        """极简白基调的全局控件样式：无边框、弱化矩形感、留白呼吸。"""
        s = self.style
        c = s.colors
        # 两个主入口 tab（配置中心 / 脑内抽屉）：加大字号、选中要**一眼看得出来**。
        # 🔴 TNotebook 自己的背景必须也换成主题 bg —— 否则残留内置主题（flatly）的
        #    白色，在 tab 行和内容边缘露出一圈「白边」。
        # 🔴 选中态以前几乎看不出来（用户 2026-10-12 报的「页签切换不够明显」）：
        #    主题里 `active`（浅色 #eef4f3）跟 `bg`（#f7f9fa）差不到一个色阶，
        #    选中 tab 只有字色从灰变品牌色 —— 扫一眼根本分不清在哪个页。
        #    现在选中 = 品牌淡底 + 品牌色**粗体** + 品牌色描边，hover 才用 active。
        sel_bg = lerp_color(c.primary, c.bg, 0.86)
        s.configure("TNotebook", border_width=0, tabmargins=(0, 0, 0, 0),
                    background=c.bg, bordercolor=c.bg,
                    lightcolor=c.bg, darkcolor=c.bg)
        s.configure("TNotebook.Tab", padding=(24, 11),
                    font=_font(12), borderwidth=1, focuscolor=c.bg,
                    background=c.bg, bordercolor=c.bg, lightcolor=c.bg, darkcolor=c.bg)
        s.map("TNotebook.Tab",
              background=[("selected", sel_bg), ("active", c.active)],
              foreground=[("selected", c.primary), ("active", c.fg)],
              bordercolor=[("selected", c.primary)],
              lightcolor=[("selected", c.primary)],
              darkcolor=[("selected", c.primary)],
              font=[("selected", _font(12, "bold"))])
        # 按钮一律去边框
        s.configure("TButton", borderwidth=0, focuscolor=c.bg)
        # 🔴 Frame / Label 也必须显式钉住背景色 —— ttk 这两类默认不跟随品牌主题，
        #    会露出内置主题（flatly）的浅色，形成一块块「白边」。
        s.configure("TFrame", background=c.bg, borderwidth=0)
        s.configure("TLabel", background=c.bg, foreground=c.fg)
        # LabelFrame 去边框线：只留标题和留白（弱化「方框感」）
        s.configure("TLabelframe", borderwidth=0, relief="flat",
                    background=c.bg, bordercolor=c.bg)
        s.configure("TLabelframe.Label", font=_font(10, "bold"),
                    foreground=c.fg, background=c.bg)
        # 输入框细边框
        s.configure("TEntry", borderwidth=1, bordercolor=c.border,
                    lightcolor=c.border, darkcolor=c.border,
                    fieldbackground=c.light, foreground=c.fg,
                    insertcolor=c.fg)
        s.configure("TCombobox", borderwidth=1, bordercolor=c.border,
                    lightcolor=c.border, darkcolor=c.border,
                    arrowcolor=c.secondary, fieldbackground=c.light,
                    background=c.light, foreground=c.fg)
        # 滚动条弱化成细线感
        s.configure("Vertical.TScrollbar", troughcolor=c.bg,
                    background=c.border, bordercolor=c.bg,
                    lightcolor=c.border, darkcolor=c.border, arrowsize=10)

        self.state = None
        self._build()
        self.refresh()

    def _build(self):
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=0)
        root.rowconfigure(1, weight=1)

        top = ttk.Frame(root, padding=10)
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text="摸鱼bot · 本地应用",
                  font=_font(14, "bold")).pack(side="left")
        self.bot_status = ttk.Label(top, text="机器人：…", bootstyle="secondary")
        self.bot_status.pack(side="left", padx=12)
        self.ver_label = ttk.Label(top, text="", bootstyle="secondary")
        self.ver_label.pack(side="right")

        nb = ttk.Notebook(root)
        nb.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        self.notebook = nb
        # 窗口 + 任务栏图标用品牌金鱼（assets/icons 是图标的唯一来源）。
        # 🔴 **`-default` 变体只设「以后新建窗口」的默认图标，不改当前窗口** ——
        #    以前只写 `iconbitmap(default=…)` / `iconphoto(True, …)`，当前窗口
        #    一直是 Tk 自带的羽毛（用户报的「左上角和任务栏都不显示金鱼」）。
        #    所以「当前窗口」和「默认」必须各设一次。
        # 顺序：PNG 先兜底（不吃 ico 的环境），.ico 最后压轴 —— Windows 上
        # 它是多尺寸的（含任务栏用的大图），系统自己挑，比 PNG 清楚。
        ico = os.path.join(HERE, "assets", "icons", "installer.ico")
        png = os.path.join(HERE, "assets", "icons", "installer.png")
        if os.path.isfile(png):
            try:
                from PIL import Image, ImageTk
                im = Image.open(png)
                im.thumbnail((64, 64))      # 32 的任务栏大图标会糊
                self._icon_photo = ImageTk.PhotoImage(im)
                self.root.iconphoto(False, self._icon_photo)   # 当前窗口
                self.root.iconphoto(True, self._icon_photo)    # 以后的 Toplevel
            except Exception:
                pass
        if os.path.isfile(ico):
            for _set in (lambda: self.root.iconbitmap(ico),
                         lambda: self.root.iconbitmap(default=ico)):
                try:
                    _set()
                except Exception:
                    pass
        self.config_center = ConfigCenter(nb, self)
        self.brain_drawer = BrainDrawerTab(nb, self)
        nb.add(self.config_center, text="配置中心")
        nb.add(self.brain_drawer, text="脑内抽屉")
        # 切到脑内抽屉时把焦点给舞台（点气泡 / 拖选区都从它出发，行为统一）
        nb.bind("<<NotebookTabChanged>>",
                lambda e: e.widget.nametowidget(
                    e.widget.select()).focus_set()
                if e.widget.select() is not None else None)

        # 关窗口时走自己的处理（现在只是 destroy，但留一个明确的落点）
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        """关窗口。

        🔴 **不手动 `after_cancel` 任何定时器** —— 试过，会炸：
           `after_cancel` 末尾的 `deletecommand(id)` 对「已经跑过 / 已经取消过」
           的 id 会抛 TclError，而它抛之前已经把 Tcl 侧取消掉了，于是
           Tkinter 的 `_tclCommands` 里留下一条悬空记录，`destroy()` 再遍历时
           就报 `can't delete Tcl command`。
        🔴 也不需要取消：`mainloop()` 一退出，队列里那些 `after` 就不会再执行了
           （用户关窗走的正是这条路）。
        """
        self.root.destroy()

    def refresh(self):
        """刷新整个界面。

        🔴 这里**必须快**（用户开抽屉 / 切页签都会走到）。两个曾经的瓶颈都
           已经挪走：查计划任务改成后台（1.2 秒）、验进程改成 OpenProcess（0.01ms）。
           副作用是「计划任务」那一栏第一帧还是旧值 —— 用 `_poll_bot_status()`
           等后台查完补上，而不是为了一个数字把界面卡住。
        """
        if _reload_font_scale():
            # 用户刚改了「界面字号」。Tk 的字号是**建控件时定死**的，没有全局
            # 字号可事后改 —— 只能整窗重建。`_flat_styles()` 末尾会自己再
            # `refresh()` 一次（那时字号已是最新，不会再进这个分支），所以这里
            # 直接返回，别重复跑下面的刷新。
            self._rebuild_for_font()
            return
        self.state = self.cw.full_state()
        self._update_bot_status()
        self.ver_label.configure(text=f"v{self.state['version']}")
        self.config_center.refresh(self.state)
        self.brain_drawer.refresh(self.state)
        self._poll_bot_status()

    def _rebuild_for_font(self):
        """字号变了 → 把整个窗口拆了重建（并停在用户原来那个分区上）。"""
        keep = getattr(getattr(self, "config_center", None), "current", None)
        for w in self.root.winfo_children():
            w.destroy()
        self._flat_styles()            # 末尾会 _build() + refresh()
        if keep:
            try:
                self.config_center.show_section(keep)
            except (tk.TclError, StopIteration):
                pass

    # ── 浅色 / 深色 / 跟随系统 ────────────────────────────────────
    def _resolve_theme(self):
        """按当前偏好算出要用的 ttkbootstrap 主题名。"""
        if self.theme_mode == "dark":
            return THEME_DARK
        if self.theme_mode == "light":
            return THEME_LIGHT
        return THEME_DARK if detect_system_dark() else THEME_LIGHT

    def set_theme_mode(self, mode):
        """切换浅色 / 深色 / 跟随系统，并把偏好落盘。

        🔴 必须整窗重建：Tk 的字号/主题都是**建控件时定死**的，没有事后改主题的
           统一入口；重建是唯一干净的做法（和字号变更走同一条 `_rebuild_window`）。
            主题没变就不重建 —— 点同一个按钮只是把偏好再写一遍，不闪屏。
        """
        if mode not in ("light", "dark", "system"):
            return
        save_ui_theme(mode)
        self.theme_mode = mode
        target = self._resolve_theme()
        if target == self.theme:
            self._sync_system_poll()   # 偏好变了，把轮询状态对齐一下
            return
        self.theme = target
        self._rebuild_window()

    def _rebuild_window(self):
        """拆掉所有子控件、换主题、按当前分区重建（字号变更也复用这条）。"""
        keep = getattr(getattr(self, "config_center", None), "current", None)
        for w in self.root.winfo_children():
            w.destroy()
        self.style.theme_use(self.theme)
        self._flat_styles()            # 末尾会 _build() + refresh()
        if keep:
            try:
                self.config_center.show_section(keep)
            except (tk.TclError, StopIteration):
                pass
        self._sync_system_poll()

    def _sync_system_poll(self):
        """跟随系统模式下，周期性读注册表盯系统主题切换；其余模式关掉轮询。"""
        if getattr(self, "theme_mode", "system") == "system":
            if getattr(self, "_sys_job", None) is None:
                self._sys_job = self.root.after(1500, self._poll_system)
        else:
            job = getattr(self, "_sys_job", None)
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
                self._sys_job = None

    def _poll_system(self):
        """注册表里的系统主题变了就跟着换；没变就继续轮询。"""
        self._sys_job = None
        if getattr(self, "theme_mode", "system") != "system":
            return
        target = THEME_DARK if detect_system_dark() else THEME_LIGHT
        if target != self.theme:
            self.theme = target
            self._rebuild_window()
            return
        self._sys_job = self.root.after(1500, self._poll_system)

    def _update_bot_status(self):
        try:
            bot = self.state["bot"]
            st = primary_button_state(bot)
            self.bot_status.configure(
                text=f"机器人：{'运行中' if st['running'] else '已停止'}"
                     f"（计划任务 {bot.get('task', '?')}）")
        except tk.TclError:
            pass                     # 窗口已经关了，别去碰已销毁的控件

    def _poll_bot_status(self, tries=0):
        """等后台把计划任务状态查完，再把那一栏补上。

        🔴 整个函数包在 try 里：这是 `after` 回调，**窗口可能已经关了** ——
           那时候任何 configure 都会抛 TclError，而我们的全局异常钩子会把它
           弹成错误框（用户明明只是关了个窗口）。静默收掉。
        """
        try:
            if not self.root.winfo_exists():
                return
            if _botctl.task_state_fresh():
                task = _botctl.task_state()
                self.state["bot"]["task"] = task
                # 自启勾选框的判据也一起补上（首帧可能还是 unknown）
                self.state["bot"]["autostart"] = task in ("ready", "running")
                self._update_bot_status()
                # 「运行方式」页上也有这个状态和自启勾选框 —— 正开着就重画一下
                if self.config_center.current == "runtime":
                    self.config_center.show_section("runtime")
                return
            if tries < 25:                   # 最多等 ~7.5 秒（正常 1.3 秒内就回来了）
                self._poll_job = self.root.after(
                    300, lambda: self._poll_bot_status(tries + 1))
        except tk.TclError:
            pass                             # 窗口没了 —— 到此为止

    def save(self, env_vals, set_vals):
        """把 env / settings 两类改动落盘，返回 (ok, msg)。

        🔴 提示要**说人话**：以前是「.env 已写 2 项；settings.json 已写 4 项」，
           用户根本不知道 `.env` / `settings.json` 是什么、更不知道「已写」是
           成功了还是写了一半（用户明确反馈「这里的提示太不说人话了，看不懂」）。
           现在按用户能理解的两类来报：**连接设置**（凭据 / 搜索目录）与
           **机器人设置**（播报文案、展示范围这些）。
        """
        parts = []
        if env_vals:
            keys, err = self.cw.write_env(env_vals)
            if err:
                return False, f"没保存成功：{err}"
            parts.append(f"{len(keys)} 项连接设置")
        if set_vals:
            n, errs = self.cw._settings.apply_values(set_vals)
            if errs:
                return False, f"没保存成功：{';'.join(errs)}"
            parts.append(f"{n} 项机器人设置")
        self.refresh()
        if not parts:
            return True, "没有改动"
        return True, "已保存：" + "、".join(parts)

    def toast(self, msg, ok=True):
        """轻提示：主窗口底部居中的一条应用内文字，2.5s 自己消失。

        🔴 用户 2026-10-12：去掉所有右下角通知。ttkbootstrap 自带的
           系统级弹窗默认 position=(5, 50, 'se') —— 屏幕右下角的
           弹窗，挡视野、和别的软件弹窗叠在一起，还容易直接被
           Windows 通知设置吞掉。改成应用内的一条：不抢焦点、不挡操作。
        🔴 失败信息也只做轻提示，不弹模态框 —— 保存失败时用户往往正盯着
           表单，一个 messagebox 会把视线整个抢走。
        """
        try:
            lbl = getattr(self, "_toast_lbl", None)
            if lbl is None or not lbl.winfo_exists():
                lbl = tk.Label(self.root, text="", anchor="center",
                               padx=16, pady=7, font=_font(9))
                self._toast_lbl = lbl
            dark = self.theme == THEME_DARK
            # 色板跟着主题走：成功用品牌色、出错用 danger；字色取底色系的前景。
            conf = dict(bg=self.style.colors.primary if ok
                        else self.style.colors.danger,
                        fg="#0b2a3d" if dark else "#ffffff")
            lbl.configure(text=msg, **conf)
            # 底部居中、压在底栏上方一点（别盖住状态栏 / 任务栏）
            lbl.place(relx=0.5, rely=1.0, anchor="s", y=-16)
            lbl.lift()
            job = getattr(self, "_toast_job", None)
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except (tk.TclError, ValueError):
                    pass

            def _hide():
                # 🔴 整窗重建（切主题 / 改字号）后控件已经没了 —— winfo_exists()
                #    不过关就别碰，否则异常会一路冒到全局钩子弹错误框。
                try:
                    if lbl.winfo_exists():
                        lbl.place_forget()
                except tk.TclError:
                    pass
            self._toast_job = self.root.after(2500, _hide)
        except Exception:
            pass


# ── 配置中心标签页 ────────────────────────────────────────────────
class ConfigCenter(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.cw = app.cw
        self.state = None
        self.current = None
        self._nav = []            # [(button, key)]
        self._var_map = {}        # (target, key) -> tk variable
        self._update_result = None  # 检查更新的后台结果（主线程轮询取用）
        # 检查更新区的**显示状态**：这一页会被重建（见 _render_runtime 的说明），
        # 重建后要按它回填，否则用户刚查出来的结果会被冲掉。
        self._upd_view = {"note": "", "dl_url": "", "dl_text": ""}
        # 开机自启开关：乐观更新用（见 _autostart）——`_autostart_var` 指向当前
        # 那个 Checkbutton 的变量（这一页会重画，重画时重新赋），忙 / 待补意图
        # 用来兜住连点。
        self._autostart_var = None
        self._autostart_busy = False
        self._autostart_pending = None
        # 导出诊断包：打包在后台线程跑，忙的时候忽略再次点击（见 _export_diag）
        self._diag_busy = False
        self._multi = {}          # (target, key) -> {choice: BooleanVar}
        self._push = {}           # (scope, oid) -> BooleanVar
        self._moyu_texts = {}
        # 自定义指令页：后台探测的结果（主线程轮询取用）+ 列表容器 + 提示行
        self._ext_result = None
        self._ext_box = None
        self._ext_count = None
        self._ext_note = None
        self._build()

    def _build(self):
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        nav = ttk.Frame(self, width=190, padding=(6, 8))
        nav.grid(row=0, column=0, sticky="ns")
        nav.pack_propagate(False)
        self.nav = nav
        ttk.Label(nav, text="分区", font=_font(10, "bold"),
                  bootstyle="secondary").pack(anchor="w", pady=(0, 6))

        body = ttk.Frame(self, padding=12)
        body.grid(row=0, column=1, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)

        self.title = ttk.Label(body, text="", font=_font(13, "bold"))
        self.title.grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.desc = ttk.Label(body, text="", bootstyle="secondary", wraplength=720,
                              font=_font(9))
        self.desc.grid(row=1, column=0, sticky="w", pady=(0, 10))

        self.canvas = tk.Canvas(body, highlightthickness=0, bd=0,
                                bg=self._c("bg"))
        # 不装竖直滚动条：ttk 滚动条会画出一条贯穿到底的槽（深色底下就是一道
        # 刺眼的亮线，用户报的「不明白边」）。设置页不长，鼠标滚轮足够。
        # 🔴 必须用 tk.Frame + 显式 bg：ttk.Frame 在部分主题下不跟随全局背景，
        #    而 tk.Canvas 自己不设 bg 就是系统灰白 —— 内容滚到底后下方的
        #    空白区域会露出一大块浅色（用户截图里的「白边」）。
        self.scroll = tk.Frame(self.canvas, bg=self._c("bg"))
        self.scroll.bind("<Configure>", lambda e: self._sync_scrollregion())
        self._canvas_win = self.canvas.create_window((0, 0), window=self.scroll, anchor="nw")
        self.canvas.grid(row=2, column=0, sticky="nsew")
        # 内容宽度跟随 canvas 拉伸（否则窗口放大时右侧留一条白边）
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        # 鼠标滚轮滚动
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _sync_scrollregion(self, _e=None):
        """滚动范围 = **内容自己的请求高度**，但**不小于视口**。

        🔴 别用 `canvas.bbox("all")` —— 那个框是 canvas 反算出来的，切换分区
           换过内容之后会留一截过期的余量，表现就是「滚轮上滑还能滚出一大串
           空白」（用户报的）。

        🔴 高度必须 `max(内容高, 视口高)`。内容比视口矮时如果只写内容高，
           Tk 会把画布原点夹到 `scrollY2 - 视口高`（**负数**），滚轮一滚内容
           就被整体**下推**，上面留一大片空白；而 `yview()` 报数时又把原点夹回
           0，看着像「没滚动但内容跑了」（用户报的「滚到最下端触底」）。
           真机实测（`_probe/scroll_probe.py`）：内容 357 / 视口 517 时，
           内容窗的 y 从 0 被推到 160。
        """
        try:
            h = max(self.scroll.winfo_reqheight(), self.canvas.winfo_height())
            self.canvas.configure(
                scrollregion=(0, 0, self.canvas.winfo_width(), h))
        except tk.TclError:
            pass

    def _on_canvas_resize(self, e):
        self.canvas.itemconfigure(self._canvas_win, width=e.width)
        self._sync_scrollregion()

    def _on_wheel(self, e):
        """只有本页**可见、且真的滚得动**时才滚。

        🔴 滚轮是 `bind_all` 的（子控件上的滚轮才收得到），而配置中心和脑内
           抽屉共用同一个 Tk 事件 —— 不判可见性的话，在抽屉页滚轮会去滚
           配置中心，切回来发现内容位置全乱了。
        🔴 装得下就直接吞掉：`scrollregion` 比视口矮时 Tk **照样**接受
           `yview_scroll`，只是把原点推成负数 —— 内容被整体下推（见
           `_sync_scrollregion`）。这里先拦一道，别给它机会。
        """
        if not self.canvas.winfo_ismapped():
            return
        if self.scroll.winfo_reqheight() <= self.canvas.winfo_height():
            return
        self.canvas.yview_scroll(-1 * (e.delta // 120), "units")

    def refresh(self, state):
        self.state = state
        for b, _ in self._nav:
            b.destroy()
        self._nav.clear()
        for g in state["groups"]:
            b = RoundedButton(
                self.nav, g["title"],
                lambda k=g["key"]: self.show_section(k),
                kind="ghost", anchor="w", padx=12, pady=6, radius=10,
                fg=self._c("secondary"), hover_fill=self._c("active"),
                selected_fg=self._c("primary"), selected_fill=lerp_color(self._c("primary"), self._c("bg"), 0.86),
                parent_bg=self._c("bg"), fixed_width=174)
            b.pack(anchor="w", pady=2, fill="x")
            self._nav.append((b, g["key"]))
        # 主题切换常驻在导航底部（浅色 / 跟随系统 / 深色）—— 不进任何分区，
        # 三个功能里「切换深浅色」的入口就在这。
        self._build_theme_switch()
        if self.current is None:
            self.current = state["groups"][0]["key"]
        self.show_section(self.current)

    def _c(self, key):
        """当前主题色（ttkbootstrap Colors 的字段）。"""
        return getattr(self.app.style.colors, key)

    def _build_theme_switch(self):
        """导航底部常驻「跟随系统 / 浅色 / 深色」三态切换。

        偏好落在数据目录 ui-theme.txt（见 load/save_ui_theme），选中即整窗重建换肤。
        三个按钮竖排、样式同分区导航，避免 RoundedButton 不支持 fill 拉伸导致
        并排时圆角矩形只画在左半边。
        🔴 顺序固定「跟随系统 / 浅色 / 深色」（用户 2026-10-12）——
           「跟随系统」是默认值，排第一；别再把它夹在中间。
        """
        for w in getattr(self, "_theme_widgets", []):
            try:
                w.destroy()
            except tk.TclError:
                pass
        self._theme_widgets = []
        nav = self.nav
        sep = tk.Frame(nav, bg=self._c("border"), height=1)
        sep.pack(fill="x", padx=8, pady=(8, 6))
        self._theme_widgets.append(sep)
        lbl = tk.Label(nav, text="外观", bg=self._c("bg"),
                       fg=self._c("secondary"), font=_font(10, "bold"))
        lbl.pack(anchor="w", padx=12, pady=(0, 4))
        self._theme_widgets.append(lbl)
        cur = getattr(self.app, "theme_mode", "system")
        for text, mode in (("跟随系统", "system"),
                           ("浅色", "light"),
                           ("深色", "dark")):
            b = RoundedButton(nav, text, lambda m=mode: self.app.set_theme_mode(m),
                              kind="ghost", anchor="w", padx=12, pady=5, radius=10,
                              font=_font(9),
                              fg=self._c("secondary"),
                              hover_fill=self._c("active"),
                              selected_fg=self._c("primary"),
                              selected_fill=lerp_color(self._c("primary"), self._c("bg"), 0.86),
                              parent_bg=self._c("bg"), fixed_width=174)
            b.pack(anchor="w", pady=2, fill="x")
            if mode == cur:
                b.set_selected(True)
            self._theme_widgets.append(b)

    def _dim(self):
        """禁用 / 占位文字色：比正文淡但仍看得见（别用 border，那是边框色）。"""
        return dim_ink(self._c("fg"), self._c("bg"))

    def _solid_btn(self, parent, text, cmd, *, padx=16, pady=7):
        """主操作圆角按钮（品牌色实心）。"""
        dark = self.app.theme == THEME_DARK
        return RoundedButton(parent, text, cmd, kind="solid",
                             fg="#0b2a3d" if dark else "#ffffff",
                             fill=self._c("primary"),
                             hover_fill=lerp_color(
                                 self._c("primary"),
                                 "#ffffff" if dark else "#000000", 0.10),
                             disabled_fg=self._dim(),
                             parent_bg=self._c("bg"), padx=padx, pady=pady)

    def _ghost_btn(self, parent, text, cmd, *, color="secondary",
                   padx=14, pady=6):
        """次级圆角按钮（平时只显文字，hover 显淡底）。"""
        return RoundedButton(parent, text, cmd, kind="ghost",
                             fg=self._c(color), hover_fill=self._c("active"),
                             disabled_fg=self._dim(),
                             parent_bg=self._c("bg"), padx=padx, pady=pady)

    def show_section(self, key):
        self.current = key
        # 左侧分区导航：选中 = 品牌色文字 + 圆角淡底；未选中 = 灰字
        for b, k in self._nav:
            b.set_selected(k == key)
        for w in list(self.scroll.children.values()):
            w.destroy()
        self._var_map.clear()
        self._multi.clear()
        self._push.clear()
        self._moyu_texts.clear()

        g = next(x for x in self.state["groups"] if x["key"] == key)
        self.title.configure(text=g["title"])
        # 分区描述也过 `_soft_wrap`（同上：中文长句 Tk 会留大片行尾空白）
        self.desc.configure(text=_soft_wrap(g["desc"], 720, 9))

        if key == "bot":
            self._render_bot()
        elif key == "msg":
            self._render_msg()
        elif key == "runtime":
            self._render_runtime()
        elif key == "moyu":
            self._render_moyu()
        elif key == "ext":
            self._render_ext()
        elif key == "version":
            self._render_version()
        else:
            self._render_fields(key)
            self._save_btn()
        # 内容整批换过了 → 滚动范围要跟着重算，否则留着上一页的余量
        # （用户报的「滚轮上滑还能滚出一大串空白」就是这么来的）。
        # 🔴 还要**回到顶部**：上一页滚到一半的位置留着的话，短页面一打开
        #    就停在半路（原点还是负的，上面一片空）。
        def _after_swap():
            self._sync_scrollregion()
            try:
                self.canvas.yview_moveto(0)
            except tk.TclError:
                pass
        self.after_idle(_after_swap)

    # ---- 通用字段渲染 ----
    def _render_fields(self, group_key):
        for f in section_fields(self.state, group_key):
            if f.get("hidden"):
                # 收图入口暂隐（用户要求）：hidden 字段（image_collect /
                # image_alert_gb）一个都不画了。它们仍留在字段表里，否则保存时
                # 认不出键会静默丢改动 —— 见 config_core.state_fields 的说明。
                # 自绘实现 `_render_self_drawn` 原样保留，入口恢复只需删掉这个分支。
                continue
            self._render_one_field(f)

    def _render_one_field(self, f):
        target, key, kind = f["target"], f["key"], f["kind"]
        wrap = ttk.Frame(self.scroll, padding=(0, 4))
        wrap.pack(fill="x", pady=4)
        ttk.Label(wrap, text=f["label"], font=_font(10, "bold")).pack(anchor="w")
        if f.get("help"):
            # 🔴 help 也过 `_soft_wrap`：Tk 的 wraplength 只按空格折行，中文长句
            #    会在行尾留一大截空白（用户报的「右边较大空隙 / 符号后异常换行」）。
            ttk.Label(wrap, text=_soft_wrap(f["help"], 720, 9),
                      bootstyle="secondary",
                      wraplength=720, font=_font(9)).pack(anchor="w", pady=(0, 4))

        if kind == "bool":
            var = tk.BooleanVar(value=str(f.get("value")) not in ("", "false", "False", "0"))
            ttk.Checkbutton(wrap, text="启用" if var.get() else "未启用", variable=var,
                            bootstyle="primary-round-toggle").pack(anchor="w", pady=(2, 6))
            self._var_map[(target, key)] = var
        elif kind == "secret":
            var = tk.StringVar()
            ent = ttk.Entry(wrap, textvariable=var, show="•", width=48)
            ent.pack(fill="x")
            ttk.Label(wrap, text="（留空 = 不修改已保存的值）", bootstyle="secondary",
                      font=_font(8)).pack(anchor="w")
            self._var_map[(target, key)] = var
        elif kind == "paths":
            # 多个搜索根（「发文件」用）：一行一个目录，和网页版 textarea 同规格。
            # 存进来的值可能是分号串（write_env 归一后的形态）或换行串（手改的），
            # 一律拆开展示；保存时原样交出去，write_env 会再归一分号串。
            _dirs = [p.strip() for p in
                     re.split(r"[;\n]", str(f.get("value", ""))) if p.strip()]
            txt = scrolledtext.ScrolledText(wrap, height=max(3, min(8, len(_dirs) + 1)),
                                            width=64, wrap="none")
            txt.insert("1.0", "\n".join(_dirs))
            txt.pack(fill="x")
            row = ttk.Frame(wrap)
            row.pack(fill="x", pady=(4, 0))
            self._ghost_btn(row, "浏览…",
                            lambda t=txt: self._browse_into(t),
                            padx=10, pady=4).pack(side="left")
            ttk.Label(row, text="一行一个目录 —— 「发文件」只在这些目录下面找；"
                                "目录越大搜得越慢。",
                      bootstyle="secondary", font=_font(8)).pack(side="left", padx=8)
            self._var_map[(target, key)] = txt
        elif kind == "area":
            txt = scrolledtext.ScrolledText(wrap, height=5, width=80, wrap="word")
            txt.insert("1.0", str(f.get("value", "")))
            txt.pack(fill="x")
            self._var_map[(target, key)] = txt
        elif kind == "int":
            # 上下限跟着设置定义走（后端 normalize 也会拦，这里让 Spinbox 直接框住）
            lo, hi = f.get("min"), f.get("max")
            var = tk.IntVar(value=int(f.get("value") or 0))
            ttk.Spinbox(wrap, from_=0 if lo is None else lo,
                        to=1024 if hi is None else hi,
                        textvariable=var, width=10).pack(anchor="w")
            self._var_map[(target, key)] = var
        elif kind == "multi":
            self._render_multi(f)
        else:  # text
            var = tk.StringVar(value=str(f.get("value", "")))
            ttk.Entry(wrap, textvariable=var, width=64).pack(fill="x")
            self._var_map[(target, key)] = var

    def _render_multi(self, f):
        target, key = f["target"], f["key"]
        cur = as_set(f.get("value"))
        choices = f.get("choices") or []
        labels = f.get("choice_labels") or {}
        vars_ = {}
        for c in choices:
            v = tk.BooleanVar(value=c in cur)
            ttk.Checkbutton(self.scroll, text=labels.get(c, c), variable=v,
                            bootstyle="primary-round-toggle").pack(anchor="w", pady=4)
            vars_[c] = v
        self._multi[(target, key)] = vars_

    def _render_self_drawn(self, f):
        """image_collect 自绘区块：主开关（c2c/group）+ 超限报警阈值。

        🔴 收图入口暂隐（用户要求）—— `_render_fields` 不再调它，函数体保留：
            入口要恢复只需把 `_render_fields` 里 hidden 分支的 `continue` 改回来。
        """
        key = f["key"]
        cur = as_set(f.get("value"))
        lf = ttk.LabelFrame(self.scroll, text=f["label"], padding=8)
        lf.pack(fill="x", pady=6)
        if f.get("help"):
            ttk.Label(lf, text=_soft_wrap(f["help"], 720, 9),
                      bootstyle="secondary",
                      wraplength=720, font=_font(9)).pack(anchor="w")
        vars_ = {}
        for scope, label in (("c2c", "收私聊图片"), ("group", "收群图片")):
            v = tk.BooleanVar(value=scope in cur)
            ttk.Checkbutton(lf, text=label, variable=v,
                            bootstyle="primary-round-toggle").pack(anchor="w", pady=4)
            vars_[scope] = v
        self._multi[("settings", key)] = vars_

    def _save_btn(self):
        self._solid_btn(self.scroll, "保存更改",
                        self._on_save).pack(anchor="e", pady=10)

    def _browse(self, var):
        import tkinter.filedialog as fd
        d = fd.askdirectory(initialdir=var.get() or os.path.expanduser("~"))
        if d:
            var.set(d.replace("\\", "/"))

    def _browse_into(self, txt):
        """「浏览…」把选中的目录**追加**进行数目录框（一行一个，不盖掉已填的）。

        干嘛的：「发文件」的搜索根是 paths 字段（多个目录），浏览选一个就追加
        一行；初始目录取当前最后一行，连续挑同一个目录下的子目录时不用每次重找。
        谁调用它：`_render_one_field` 的 paths 分支（桌面配置中心「④ 发文件」）。
        """
        import tkinter.filedialog as fd
        cur = txt.get("1.0", "end").strip()
        last = cur.splitlines()[-1].strip() if cur else ""
        d = fd.askdirectory(initialdir=last or os.path.expanduser("~"))
        if d:
            txt.insert("end", ("" if not cur else "\n") + d.replace("\\", "/"))

    def _bad_path_lines(self, env_vals):
        """检查多目录字段：把**不存在**的目录挑出来（逐行给名字）。

        🔴 框里只要有一条不存在的目录，后端会整批拒写 —— 但报错只说「这个目录
           不存在：X」不好定位是哪一行。这里先逐个点名，让用户能对症删行 / 改行，
           而不是面对一句笼统的失败提示（用户 2026-10-12 提的「框里有条不存在的
           文件夹怎么办」）。
        """
        bad = []
        for f in self.state.get("fields", []):
            if f.get("target") != "env" or f.get("kind") != "paths":
                continue
            raw = env_vals.get(f["key"])
            if raw is None:
                continue
            for p in self.cw.split_roots(str(raw)):
                if not os.path.isdir(os.path.expanduser(p)):
                    bad.append(p)
        return bad

    def _on_save(self):
        env_vals, set_vals = {}, {}
        for (target, key), var in self._var_map.items():
            if isinstance(var, tk.BooleanVar):
                v = "true" if var.get() else "false"
            elif isinstance(var, scrolledtext.ScrolledText):
                v = var.get("1.0", "end").strip()
            else:
                v = var.get()
            (env_vals if target == "env" else set_vals)[key] = v
        for (target, key), vars_ in self._multi.items():
            checked = [c for c, v in vars_.items() if v.get()]
            (env_vals if target == "env" else set_vals)[key] = ",".join(checked)
        # 多目录先点名：有不存在的一律拦下，指出具体是哪几行。
        bad = self._bad_path_lines(env_vals)
        if bad:
            shown = "、".join(bad[:3]) + ("…" if len(bad) > 3 else "")
            self.app.toast(f"这几行目录不存在，删掉或改对再保存：{shown}", False)
            return
        ok, msg = self.app.save(env_vals, set_vals)
        self.app.toast(msg, ok)

    # ---- ① 连接 QQ：凭据 ----
    def _render_bot(self):
        self._render_fields("bot")
        self._group_switches_box()
        self._solid_btn(self.scroll, "保存凭据", self._on_save
                        ).pack(anchor="e", pady=(4, 10))

    def _group_switches_box(self):
        """「去 QQ 里开两个开关」提示块（内容与网页版配置中心一致）。

        干嘛的：这两个开关在 QQ 那边管，配置中心碰不到它们；不开的话机器人
        收不到普通发言、也发不出播报，用户会以为「机器人坏了」。十个人有九个
        不看说明书，所以直接摆在「① 连接 QQ」页上，别指望人家去翻安装指南。
        谁调用它：`_render_bot()`（桌面配置中心「① 连接 QQ」页）。
        """
        lf = ttk.LabelFrame(
            self.scroll,
            text="要在群聊中使用机器人，你需要打开的开关：",
            padding=8)
        lf.pack(fill="x", pady=8)
        # 🔴 关键限制要先讲清楚（用户 2026-10-12 定稿）：机器人**分不清是谁说的**，
        #    多人同时聊会串；按用户分别收集还在开发。不先说明白，用户在多人群里
        #    用一次就会觉得「这机器人记的东西全乱了」。
        _limit = ("注意：机器人现在分不清一句话是谁说的，只适合单人活跃的群；"
                  "多人同时聊会串。按用户分别收集正在开发中。")
        ttk.Label(lf, text=_soft_wrap(_limit, 720, 9),
                  bootstyle="warning", wraplength=720,
                  font=_font(9)).pack(anchor="w", pady=(0, 8))
        _intro = ("把机器人拉进群之后，用手机点群里的机器人头像 → 右上角"
                  "「设置」→ 打开下面这两项。这两项在 QQ 那边管，"
                  "配置中心管不到它们。")
        ttk.Label(lf, text=_soft_wrap(_intro, 720, 9),
                  bootstyle="secondary", wraplength=720,
                  font=_font(9)).pack(anchor="w", pady=(0, 6))
        for title, action, note in (
            ("机器人可获取的群聊消息范围", "选「获取群内全部消息」",
             "不开：只有 @ 它才理你，群里的普通发言它收不到"),
            ("机器人主动在群聊内发言", "打开",
             "不开：它没法主动发东西 —— 上线播报发不出来"),
        ):
            row = ttk.Frame(lf)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=f"{title} → {action}",
                      font=_font(10, "bold")).pack(anchor="w")
            ttk.Label(row, text=note, bootstyle="secondary",
                      font=_font(9)).pack(anchor="w")
        _done = "开完在群里发一句「待办 测试」，能收到回执就说明配好了。"
        ttk.Label(lf, text=_soft_wrap(_done, 720, 9),
                  bootstyle="secondary", wraplength=720,
                  font=_font(9)).pack(anchor="w", pady=(6, 0))

    # ---- ③ 播报：文案 + 播报目标勾选 ----
    def _render_msg(self):
        self._render_fields("msg")
        sess = self.state.get("sessions", [])
        targets = set(self.state.get("push_targets", []))
        if sess:
            lf = ttk.LabelFrame(self.scroll, text="播报目标（勾选 = 启动播报发到这些会话）",
                                padding=8)
            lf.pack(fill="x", pady=8)
            for s in sess:
                scope, oid = s.get("scope"), s.get("openid")
                tag = f"{scope}:{oid}"
                name = s.get("name") or s.get("id") or tag
                last = str(s.get("recent") or "")
                v = tk.BooleanVar(value=tag in targets)
                row = ttk.Frame(lf, padding=(0, 4))
                row.pack(fill="x")
                cb = ttk.Checkbutton(row, text=name, variable=v,
                                     bootstyle="primary-round-toggle")
                cb.pack(side="left")
                ttk.Label(row, text=last[:40], bootstyle="secondary",
                          font=_font(8)).pack(side="left", padx=8)
                cb.configure(command=lambda sc=scope, o=oid, v=v: self._toggle_push(sc, o, v))
                self._push[(scope, oid)] = v
        self._save_btn()

    def _toggle_push(self, scope, oid, var):
        """勾 / 取消一个播报目标 → 立刻写 `.env`。

        🔴 这里**不能**调 `self.app.refresh()`：那会① 重新查计划任务（起一次
        PowerShell，几百毫秒的卡顿）② 把整页控件重建 —— 包括用户正在点的这个
        复选框，等于「点下去控件没了」。写完直接提示就行，复选框自己的状态
        已经是新的了。
        """
        cur = set(self.cw._push.targets())
        item = (scope, oid)
        if var.get():
            cur.add(item)
        else:
            cur.discard(item)
        keys, err = self.cw.write_env(
            {"QQ_PUSH_TARGETS": self.cw._push.format_targets(list(cur))})
        if err:
            self.app.toast(f"保存失败：{err}", False)
            var.set(not var.get())          # 回滚复选框，别骗用户
            return
        # 🔴 同步内存里的 state：切走再切回这一页时，复选框是按 `app.state` 重建的。
        #    只写 `.env` 不更新它的话，重新进来又会按旧值渲染 —— 用户看到的是
        #    「取消勾选 → 切一下页 → 又勾上了」。
        if isinstance(self.app.state, dict):
            self.app.state["push_targets"] = [
                f"{sc}:{oid}" for sc, oid in sorted(cur)]
        self.app.toast("播报目标已更新", True)

    # ---- ⑦ 运行方式 ----
    def _render_runtime(self):
        bot = self.state["bot"]
        st = primary_button_state(bot)
        lf = ttk.LabelFrame(self.scroll, text="机器人进程", padding=8)
        lf.pack(fill="x", pady=6)
        ttk.Label(lf, text=f"状态：{'运行中' if st['running'] else '已停止'}（PID {bot.get('pid') or '-'}）",
                  font=_font(10)).pack(anchor="w", pady=4)
        bar = ttk.Frame(lf)
        bar.pack(anchor="w")
        # 启停按钮带禁用态：不可用的那个灰化成圆角描边样式
        for label, action, allowed in (("启动", "start", st["start"]),
                                       ("停止", "stop", st["stop"]),
                                       ("重启", "restart", st["restart"])):
            b = self._ghost_btn(bar, label,
                                lambda a=action: self._bot(a), padx=18, pady=5)
            if not allowed:
                b.configure(state="disabled")
            b.pack(side="left", padx=4)

        lf2 = ttk.LabelFrame(self.scroll, text="开机自启（Windows 计划任务 MoyuBot）", padding=8)
        lf2.pack(fill="x", pady=6)
        # 🔴 勾选态必须读 `autostart`（= 计划任务存在且可用），**不能**拿
        #    `task == "running"` —— 自启任务注册后是 ready 不是 running，
        #    老写法导致「明明开了却显示关闭」（用户 2026-10-12 报的）。
        #    计划任务状态是后台查的（1.2 秒），查完 `_poll_bot_status` 会重画这一栏。
        av = tk.BooleanVar(value=bool(bot.get("autostart")))
        # 🔴 记到实例上：`_autostart` 要先认下点击、再后台确认，失败还得把开关
        #    拨回来 —— 没有这个引用就够不着已经建好的那个 Checkbutton。
        self._autostart_var = av
        ttk.Checkbutton(lf2, text="开机自动启动机器人", variable=av,
                        bootstyle="primary-round-toggle",
                        command=lambda v=av: self._autostart(v.get())).pack(anchor="w", pady=4)
        ttk.Label(lf2, text="开启后，登录 Windows 会自动把管家跑起来（含机器人，后台无窗口）。",
                  bootstyle="secondary", font=_font(8)).pack(anchor="w")

        lf3 = ttk.LabelFrame(self.scroll, text="依赖自检", padding=8)
        lf3.pack(fill="x", pady=6)
        for d in self.state.get("deps", []):
            mark = "✓" if d["ok"] else ("✗" if d.get("needed") else "·")
            col = "success" if d["ok"] else ("danger" if d.get("needed") else "secondary")
            ttk.Label(lf3, text=f"{mark} {d['name']}：{d['detail']}", bootstyle=col,
                      font=_font(9)).pack(anchor="w", pady=1)

        # 🔴「一键安装缺失依赖」：缺**任何可自动装的东西**（pip 包 / npm 包）就显形。
        #    以前只数 pip 包 —— playwright-core（Node 包）缺着时按钮根本不出现，
        #    用户报的「依赖自检依然没有一键安装的方法」就是这个（2026-10-12）。
        #    装不上的（node 本身、系统浏览器）不在这里，只能给出引导文字。
        _need_pip = [d for d in self.state.get("deps", [])
                     if not d["ok"] and d.get("install") == "pip"]
        _need_npm = [d for d in self.state.get("deps", [])
                     if not d["ok"] and d.get("install") == "npm"]
        if _need_pip or _need_npm:
            _row = ttk.Frame(lf3, padding=(0, 6))
            _row.pack(anchor="w", fill="x")
            self._install_btn = self._solid_btn(
                _row, "一键安装缺失依赖", self._install_deps, padx=14, pady=5)
            self._install_btn.pack(side="left")
            parts = []
            if _need_pip:
                parts.append(f"{len(_need_pip)} 个 Python 包")
            if _need_npm:
                parts.append(f"{len(_need_npm)} 个 Node 包")
            self._install_note = ttk.Label(
                _row, text=f"缺 {' + '.join(parts)} —— 点左边按钮自动装好"
                           f"（走国内镜像，不用自己敲命令）",
                bootstyle="secondary", font=_font(8))
            self._install_note.pack(side="left", padx=(10, 0))
        else:
            self._install_btn = None
            self._install_note = None
        # 装不了的那些（node / 浏览器）单独给一句引导，别让用户干瞪眼
        _manual = [d for d in self.state.get("deps", [])
                   if not d["ok"] and not d.get("install")]
        if _manual:
            _m = "、".join(d["name"] for d in _manual)
            ttk.Label(lf3, text=_soft_wrap(
                f"另外还缺 {_m} —— 这两个得你自己装（装了才能把文件渲染成图片；"
                f"不装也能用，只是发文件时给的是文件卡片）。", 720, 8),
                bootstyle="secondary", wraplength=720,
                font=_font(8)).pack(anchor="w", pady=(4, 0))

    def _render_version(self):
        """「⑩ 版本」独立页签（item19 2026-10-12）：当前版本、检查更新、下载新版。

        干嘛的：把原来塞在「⑦ 运行方式」里的版本块单独拆成一页，和网页版
                config_core 的「⑩ 版本」页签对齐。
        谁调用它：show_section 在 key == "version" 时调它，不再走通用表单
                （version 没有可编辑字段，走通用表单会成一页空壳还多个没用的保存按钮）。
        🔴 检查更新的结果存在 `_upd_view` 里、每次重建回填 —— 换肤会整窗
           重建这一页，不然用户刚点出来的「有新版」一秒后被冲回默认。
        """
        # 🔴 逻辑复用 `version.py`（三源重试 + 人话报错），和网页版同一套，不另写。
        lf4 = ttk.LabelFrame(self.scroll, text="版本", padding=8)
        lf4.pack(fill="x", pady=6)
        _upd = ("更新：点「下载新版」拿安装包（文件名带版本号），双击装上就行"
                "—— 装到同一个目录，你的数据（待办 / 思绪 / 配置）不受影响。")
        ttk.Label(lf4, text=_soft_wrap(_upd, 720, 9),
                  bootstyle="secondary", wraplength=720,
                  font=_font(9)).pack(anchor="w")
        ver_row = ttk.Frame(lf4, padding=(0, 6))
        ver_row.pack(anchor="w", fill="x")
        _v = self._upd_view
        self._ver_note = ttk.Label(
            ver_row, text=_v["note"] or f"v{self.state.get('version', '?')}",
            font=_font(10, "bold"))
        self._ver_note.pack(side="left")
        # 「下载新版」平时藏着，查出有新版本才显形
        self._dl_btn = self._solid_btn(
            ver_row, _v["dl_text"] or "下载新版",
            self._open_download, padx=14, pady=5)
        self._ver_dl_url = _v["dl_url"]
        if _v["dl_url"]:
            self._dl_btn.pack(side="left", padx=(10, 0))
        self._ghost_btn(ver_row, "检查更新",
                        self._check_update, padx=14, pady=5).pack(side="left", padx=(8, 0))
        ttk.Label(lf4, text="国内网络 git 直连不稳定，可在你下载摸鱼bot的群内寻找最新群文件。",
                  bootstyle="secondary", wraplength=720,
                  font=_font(8)).pack(anchor="w", pady=(6, 0))

        # ── 遇到问题：一键导出诊断包（用户 2026-10-13）──────────────
        # 🔴 为什么放在「版本」这一区：用户遇到 bug 时最后一定是来这儿找
        #    「我这版是不是旧的」，顺手就能看到「出问题该导什么」。
        ttk.Label(lf4, text=_soft_wrap(
            "遇到问题：点下面这个按钮，会在桌面生成一个「诊断包」—— 里面是版本、"
            "运行环境和最近一段日志，不含你的待办 / 思绪 / 密钥。把它发给群主，"
            "对方就能定位问题。", 720, 9),
            bootstyle="secondary", wraplength=720,
            font=_font(9)).pack(anchor="w", pady=(12, 0))
        _diag_row = ttk.Frame(lf4, padding=(0, 6))
        _diag_row.pack(anchor="w", fill="x")
        self._ghost_btn(_diag_row, "导出诊断包",
                        self._export_diag, padx=14, pady=5).pack(side="left")
        self._diag_note = ttk.Label(_diag_row, text="", bootstyle="secondary",
                                    font=_font(9), wraplength=520)
        self._diag_note.pack(side="left", padx=(10, 0))

    def _export_diag(self):
        """导出诊断包（打包内容与隐私红线见 `diag.py`）。

        🔴 打包要读日志尾部（单文件可能几十 MB），必须丢后台线程 —— 在按钮
           回调里同步跑会把界面冻住好几秒；完成后 `after(0)` 回主线程改文案，
           后台线程绝不碰 Tk。
        🔴 连点守卫：打包期间再点直接忽略，免得同时往桌面写两个包。
        """
        if getattr(self, "_diag_busy", False):
            return
        self._diag_busy = True
        self._set_diag_note("正在打包…")
        state = self.state                        # 界面现成的那份，别再取一次

        def work():
            try:
                ok, msg, path = _diag.export_bundle(state)
            except Exception as e:               # noqa: BLE001
                ok, msg, path = False, f"导出失败：{e}", None
            try:
                self.app.root.after(0, lambda: self._diag_done(ok, msg, path))
            except tk.TclError:
                pass                             # 窗口已经关了，别再碰 Tk

        threading.Thread(target=work, daemon=True).start()

    def _set_diag_note(self, text):
        """改「导出诊断包」右边那行小字（控件可能已随整页重画销毁）。"""
        lbl = getattr(self, "_diag_note", None)
        if lbl is None:
            return
        try:
            # 🔴 长路径里没有空格，Tk 的 wraplength 断不开 → 过一遍像素预折行
            lbl.configure(text=_soft_wrap(text, 520, 9))
        except tk.TclError:
            pass

    def _diag_done(self, ok, msg, path):
        """后台打包回来了（`after` 送回主线程，这里可以安全碰 Tk）。"""
        self._diag_busy = False
        if ok and path:
            self._set_diag_note(f"已生成 {os.path.basename(path)}，就在桌面 —— "
                                "把它发给群主即可")
        else:
            self._set_diag_note(msg)
        self.app.toast(msg, ok)

    def _check_update(self):
        """检查更新：查远端最新版本。

        🔴 联网在后台线程做（要好几秒），但**后台线程绝不能碰 Tk** ——
           连 `root.after()` 都不行，Tkinter 不是线程安全的（实测从子线程调
           `after` 会抛 `RuntimeError: main thread is not in main loop`，
           结果就是「点了没反应」）。
           所以走后台上写变量 + 主线程轮询的套路（和 `task_state` 同一个模式）。
        """
        cur = self.state.get("version", "?")
        self._set_ver_note(f"v{cur}（检查中…）")
        self._hide_download()
        self._update_result = None

        def work():
            try:
                far, err = self.cw._version.latest()
            except Exception as e:                                   # noqa: BLE001
                far, err = None, f"{type(e).__name__}: {e}"
            self._update_result = (cur, far, err)      # 只写变量，不碰任何控件

        threading.Thread(target=work, daemon=True).start()
        self._poll_update(cur, 0)

    def _poll_update(self, cur, tries):
        """主线程轮询：后台查完就画，没查完就再等一轮。"""
        try:
            if self._update_result is not None:
                c, far, err = self._update_result
                self._update_result = None
                self._on_update_result(c, far, err)
                return
            if tries < 120:                  # 最多等 ~12 秒
                self._poll_job = self.app.root.after(
                    100, lambda: self._poll_update(cur, tries + 1))
        except tk.TclError:
            pass                             # 窗口没了

    def _set_ver_note(self, text):
        """改「版本」那行文案 —— **同时存进 `_upd_view`**，重画这一页也不丢。"""
        self._upd_view["note"] = text
        try:
            self._ver_note.configure(text=text)
        except tk.TclError:
            pass

    def _on_update_result(self, cur, far, err):
        """把检查结果画到界面上（已回到主线程）。"""
        try:
            if not self._ver_note.winfo_exists():
                return                               # 用户已经切走了
            if not far:
                # 服务端给的已经是人话，别再套一层括号（网页版那条注释同样适用）
                self._set_ver_note(f"v{cur}　检查失败：{err or ''}")
                return
            if self.cw._version.compare_version(far, cur) > 0:
                self._set_ver_note(f"有新版 v{far}（当前 v{cur}）")
                self._show_download(far)
                self.app.toast(f"有新版本 v{far} —— 点「下载 v{far}」", True)
            else:
                self._set_ver_note(f"v{cur}（已是最新）")
                self.app.toast(f"已经是最新版 v{cur}", True)
        except (tk.TclError, AttributeError):
            pass             # 界面已经被拆了 / 版本不可比 —— 静默收掉，别弹错误框

    def _show_download(self, far):
        """显示「下载 vX.Y.Z」按钮并记下直链。"""
        self._ver_dl_url = self.cw._version.download_url(far)
        self._upd_view["dl_url"] = self._ver_dl_url
        self._upd_view["dl_text"] = f"下载 v{far}"
        try:
            self._dl_btn.set_text(f"下载 v{far}")
            self._dl_btn.pack(side="left", padx=(10, 0))
        except tk.TclError:
            pass

    def _hide_download(self):
        """收起下载按钮（连 `_upd_view` 一起清，重画后也不会冒出来）。"""
        self._upd_view["dl_url"] = ""
        self._upd_view["dl_text"] = ""
        try:
            self._dl_btn.pack_forget()
        except tk.TclError:
            pass

    # ── 一键安装缺失依赖（item17 2026-10-12）──
    def _install_deps(self):
        """后台装齐缺失依赖（pip 包 + npm 包）；子线程不碰 Tk，结果回主线程再画。

        干嘛的：让用户不用自己敲命令。用**当前这个 Python**（sys.executable）
        跑 pip、用**随包的 node** 跑 npm，保证装到 bot 真正用到的地方。

        🔴 **走国内镜像**（用户问「国内网络直连那些依赖的速度」）：PyPI 和
           npm registry 官方源在国内经常慢到超时（实测几十 KB/s 甚至直接断），
           所以默认挂清华 PyPI 镜像 + npmmirror。想用官方源就设环境变量
           `MEMO_NO_MIRROR=1`。
        谁调用它：_render_runtime 里「依赖自检」块的「一键安装缺失依赖」按钮。
        """
        if self._install_btn is None:
            return
        need_pip = any(not d["ok"] and d.get("install") == "pip"
                       for d in self.state.get("deps", []))
        need_npm = any(not d["ok"] and d.get("install") == "npm"
                       for d in self.state.get("deps", []))
        if not need_pip and not need_npm:
            self.app.toast("可自动安装的依赖已经齐全", True)
            return
        self._install_btn.set_text("安装中…")
        self._install_btn.configure(state="disabled")
        if getattr(self, "_install_note", None) is not None:
            self._install_note.configure(text="正在安装，别关窗口…")
        self._install_result = None

        def _pip_args(here):
            """pip 安装参数 —— 官方源 + **国内镜像做备用索引**。

            🔴 实测（2026-10-12 本机）：
               · 官方 pypi.org 能连、7MB 的 wheel 约 4 秒；
               · 清华镜像单独用时 pip 反而报「找不到包」。
               所以**不把镜像当唯一源**：官方为主索引、清华挂 `--extra-index-url`
               —— pip 会把两个索引的候选合并，哪个有、哪个快就用哪个。
               国内连不上官方时镜像顶上，镜像缺包时官方兜底。
               `MEMO_NO_MIRROR=1` 可强制只用官方。
            """
            args = ["install"]
            for _r in (os.path.join(here, "requirements.txt"),
                       os.path.join(here, "extensions", "local", "requirements.txt")):
                if os.path.isfile(_r):
                    args += ["-r", _r]
            if not os.environ.get("MEMO_NO_MIRROR"):
                args += ["--extra-index-url",
                         "https://pypi.tuna.tsinghua.edu.cn/simple"]
            return args

        def _npm_args(here):
            """npm 安装 playwright-core 的参数 —— 默认走 npmmirror。

            🔴 实测（2026-10-12 本机）：**官方 registry.npmjs.org 根本连不上**
               （curl 返回 000、11 秒无响应），npmmirror 正常（15MB / 8.6 秒）。
               所以 npm 这边**必须**默认挂 npmmirror，不像 pip 那样能用官方兜底。
               `MEMO_NO_MIRROR=1` 时退回官方（给能直连官方的网络用）。
            """
            args = ["install", "--no-audit", "--no-fund"]
            if not os.environ.get("MEMO_NO_MIRROR"):
                args += ["--registry=https://registry.npmmirror.com"]
            return args

        def work():
            py = sys.executable
            here = os.path.dirname(os.path.abspath(__file__))
            _cf = 0x08000000 if os.name == "nt" else 0
            logs, ok_all = [], True
            if need_pip:
                try:
                    proc = subprocess.run([py, "-m", "pip"] + _pip_args(here),
                                          capture_output=True, text=True,
                                          creationflags=_cf, timeout=600)
                    ok_all &= proc.returncode == 0
                    logs.append("=== pip ===\n" + (proc.stdout or "")
                                + "\n" + (proc.stderr or ""))
                except Exception as e:                   # noqa: BLE001
                    ok_all = False
                    logs.append(f"=== pip ===\n{type(e).__name__}: {e}")
            if need_npm:
                node = self.cw._find_node()
                npm = os.path.join(os.path.dirname(node), "npm.cmd") \
                    if node and os.name == "nt" else "npm"
                if not node or (os.name == "nt" and not os.path.isfile(npm)):
                    ok_all = False
                    logs.append("=== npm ===\n没找到 node/npm，装不了 playwright-core")
                else:
                    try:
                        proc = subprocess.run([npm] + _npm_args(here), cwd=here,
                                              capture_output=True, text=True,
                                              creationflags=_cf, timeout=600)
                        ok_all &= proc.returncode == 0
                        logs.append("=== npm ===\n" + (proc.stdout or "")
                                    + "\n" + (proc.stderr or ""))
                    except Exception as e:               # noqa: BLE001
                        ok_all = False
                        logs.append(f"=== npm ===\n{type(e).__name__}: {e}")
            self._install_result = (ok_all, "\n".join(logs))

        threading.Thread(target=work, daemon=True).start()
        self._poll_install(0)

    def _poll_install(self, tries):
        """主线程轮询：后台装完就画，没装完就再等一轮。"""
        try:
            if self._install_result is not None:
                ok, log = self._install_result
                self._install_result = None
                self._on_install_result(ok, log)
                return
            if tries < 600:                  # 最多等 ~60 秒（pip 快的几秒，慢的一两分钟）
                self._poll_job = self.app.root.after(
                    100, lambda: self._poll_install(tries + 1))
        except tk.TclError:
            pass

    def _on_install_result(self, ok, log):
        try:
            if self._install_btn is not None and self._install_btn.winfo_exists():
                self._install_btn.set_text("一键安装缺失依赖")
                self._install_btn.configure(state="normal")
            if getattr(self, "_install_note", None) is not None:
                self._install_note.configure(
                    text="装好了 ✓" if ok else "安装失败，看日志文件")
        except tk.TclError:
            pass
        if not ok:
            # 🔴 桌面端无控制台（pythonw），失败日志落地成文件，不往 stdout 打。
            try:
                _logp = os.path.join(data_dir(), "install-deps.log")
                with open(_logp, "w", encoding="utf-8") as _f:
                    _f.write(log)
                self.app.toast(f"依赖安装失败，日志在 {_logp}", False)
            except Exception:                                # noqa: BLE001
                self.app.toast("依赖安装失败", False)
            return
        # 🔴 装完重抓 state（dep_report 会重算）整页重画 —— 让「依赖自检」立刻反映新状态
        try:
            self.refresh(self.cw.full_state())
        except Exception:                                    # noqa: BLE001
            pass
        self.app.toast("依赖已装好", True)

    def _open_download(self):
        """用系统浏览器打开安装包直链（直链打不开时页面上还有 Releases 兜底）。"""
        url = self._ver_dl_url or self.cw._version.releases_url()
        try:
            webbrowser.open(url)
        except Exception as e:                                       # noqa: BLE001
            self.app.toast(f"打不开浏览器：{e}", False)

    def _bot(self, action):
        fn = {"start": self.cw.bot_start, "stop": self.cw.bot_stop,
              "restart": self.cw.bot_restart}.get(action)
        if fn:
            ok, msg = fn()
            self.app.toast(msg or action, ok)
            self.app.refresh()

    def _autostart(self, on):
        """开机自启开关：**先认点击、再后台确认**，真失败了才回拨 + 提示。

        🔴 用户 2026-10-13 报「按钮有点卡顿」。以前是同步等 `autostart_set`
           把 PowerShell 跑完（注册 / 删计划任务，1~3 秒），紧接着
           `self.app.refresh()` 整页重画；重画时读的还是**旧** state，开关先
           弹回去、等确认完再翻过来 —— 一来一回就是那个卡顿。
        现在分三步：
          ① 点下去立刻认（只拨这一个开关，不重画整页）；
          ② 真正干活丢后台线程，界面不冻；
          ③ 只有失败才回拨，并 toast 说清原因。
        """
        on = bool(on)
        self._set_autostart_ui(on)               # ① 立刻认下这次点击
        if self._autostart_busy:                 # 上一轮还没回来：记住最后意图
            self._autostart_pending = on
            return
        self._autostart_busy = True

        def work():
            try:
                ok, msg = self.cw.autostart_set(on)
            except Exception as e:               # noqa: BLE001
                ok, msg = False, f"设置开机自启失败：{e}"
            try:
                self.app.root.after(0, lambda: self._autostart_done(on, ok, msg))
            except tk.TclError:
                pass                             # 窗口已经关了，别再碰 Tk

        threading.Thread(target=work, daemon=True).start()

    def _set_autostart_ui(self, on):
        """把开关拨到 `on`（同时改 state —— 之后整页重画也会是这个值）。"""
        try:
            self.state["bot"]["autostart"] = bool(on)
        except (KeyError, TypeError):
            pass
        v = getattr(self, "_autostart_var", None)
        if v is not None:
            try:
                v.set(bool(on))
            except tk.TclError:
                pass                             # 控件跟着重画销毁了，正常

    def _autostart_done(self, want, ok, msg):
        """后台确认回来了（`after` 送回主线程，这里可以安全碰 Tk）。"""
        self._autostart_busy = False
        pending = self._autostart_pending
        self._autostart_pending = None

        if ok:
            self.app.toast(msg, True)
            # 任务刚变过（`autostart_set` 已把状态缓存清掉），让那一栏自己补齐，
            # 顺带把「计划任务 xxx」那行文字刷新 —— 非阻塞接口，不会卡。
            self.app._poll_bot_status()
        else:
            # 🔴 失败：回拨 + 说清为什么，别把开关停在一个不成立的位置上。
            self._set_autostart_ui(not want)
            self.app.toast(msg or "开机自启没设置成功", False)

        # 忙的时候用户又点过 → 以那一次为准，补跑。
        # 🔴 判据要跟**后台真实做到的状态**比，不能跟 state 比 —— state 早被
        #    第二次点击乐观更新过了，拿它比会误判成「已经一致」而漏掉补跑，
        #    结果真实状态和界面显示分家。
        if pending is not None and pending != (want if ok else (not want)):
            self._autostart(pending)

    # ---- ⑧ 摸鱼库 ----
    def _render_moyu(self):
        m = self.state["moyu"]
        ttk.Label(self.scroll,
                  text=f"内置 {m['builtin_total']} 条；当前来源：{m['source']}（改完点保存立刻生效）",
                  bootstyle="secondary", font=_font(9)).pack(anchor="w", pady=(0, 6))
        for c in m["cats"]:
            lf = ttk.LabelFrame(self.scroll, text=f"{c['icon']} {c['name']}", padding=6)
            lf.pack(fill="x", pady=4)
            if c.get("help"):
                ttk.Label(lf, text=_soft_wrap(c["help"], 700, 8),
                          bootstyle="secondary",
                          font=_font(8), wraplength=700).pack(anchor="w")
            txt = scrolledtext.ScrolledText(lf, height=6, width=80, wrap="word")
            txt.insert("1.0", "\n".join(m["groups"].get(c["key"], [])))
            txt.pack(fill="x")
            self._moyu_texts[c["key"]] = txt
        bar = ttk.Frame(self.scroll, padding=(0, 6))
        bar.pack(anchor="e")
        self._solid_btn(bar, "保存摸鱼库",
                        self._moyu_save).pack(side="left", padx=4)
        self._ghost_btn(bar, "恢复内置",
                        self._moyu_reset).pack(side="left", padx=4)

    def _moyu_save(self):
        groups = {k: [ln.strip() for ln in t.get("1.0", "end").splitlines() if ln.strip()]
                  for k, t in self._moyu_texts.items()}
        ok, info = self.cw._moyu.save_items(groups)
        self.app.toast(info if ok else "保存失败", ok)
        self.app.refresh()

    def _moyu_reset(self):
        ok, msg = self.cw._moyu.reset_items()
        self.app.toast(msg or "已恢复", ok)
        self.app.refresh()

    # ---- ⑨ 自定义指令 ----
    def _render_ext(self):
        """「⑨ 自定义指令」页：导入 / 列出 / 移除扩展包。

        🔴 探测「每个包装了哪些指令」要**开子进程**（扩展是任意代码，在本进程
           `exec_module` 的话一个死循环就把 Tk 主线程冻住了），所以走
           「后台线程写变量 + 主线程轮询」这套（和检查更新同一个模式）。
           先用 `probe=False` 把文件名 / 体积这层立刻画出来，探到了再补指令那行。
        """
        self._ext_result = None
        self._ext_box = None
        self._ext_count = None

        head = ttk.LabelFrame(self.scroll, text="导入一个指令包", padding=8)
        head.pack(fill="x", pady=6)
        _ext_help = ("选一个 .py（单文件指令）或 .zip（指令包）。导入不会自动生效 ——"
                     " 导完到「⑦ 运行方式」点一下「重启」，指令才会注册进机器人。")
        ttk.Label(head, text=_soft_wrap(_ext_help, 720, 9),
                  bootstyle="secondary", wraplength=720,
                  font=_font(9)).pack(anchor="w")
        bar = ttk.Frame(head, padding=(0, 8))
        bar.pack(anchor="w")
        self._solid_btn(bar, "导入扩展…", self._ext_pick,
                        padx=16, pady=5).pack(side="left", padx=(0, 8))
        self._ghost_btn(bar, "打开安装目录", self._ext_open_folder,
                        padx=14, pady=5).pack(side="left")
        self._ext_note = ttk.Label(head, text="", wraplength=720,
                                   font=_font(9))
        self._ext_note.pack(anchor="w")

        lf = ttk.LabelFrame(self.scroll, text="已装的扩展", padding=8)
        lf.pack(fill="x", pady=6)
        self._ext_count = ttk.Label(lf, text="", bootstyle="secondary",
                                    font=_font(9))
        self._ext_count.pack(anchor="w", pady=(0, 4))
        self._ext_box = ttk.Frame(lf)
        self._ext_box.pack(fill="x")
        self._draw_ext_items(_extmgr.list_items(probe=False))
        threading.Thread(target=self._ext_probe_work, daemon=True).start()
        self._poll_ext(0)

    def _ext_probe_work(self):
        """后台线程：只写变量，**绝不碰任何 Tk 控件**（Tkinter 不是线程安全的）。"""
        try:
            self._ext_result = _extmgr.list_items(probe=True)
        except Exception as e:                                        # noqa: BLE001
            self._ext_result = e

    def _poll_ext(self, tries):
        """主线程轮询：探测回来就重画列表，没回来就再等一轮。"""
        try:
            if not self.winfo_exists():
                return
            if self._ext_result is not None:
                res, self._ext_result = self._ext_result, None
                if isinstance(res, Exception):
                    self._set_ext_note(f"列不出来：{type(res).__name__}: {res}", bad=True)
                else:
                    self._draw_ext_items(res)
                return
            if tries < 80:                      # 最多等 ~8 秒（正常 1 秒内回来）
                self._poll_job = self.after(100, lambda: self._poll_ext(tries + 1))
        except tk.TclError:
            pass                                 # 界面已经被拆了

    def _set_ext_note(self, text, bad=False):
        try:
            self._ext_note.configure(text=text, bootstyle="danger" if bad else "secondary")
        except (tk.TclError, AttributeError):
            pass

    def _draw_ext_items(self, items):
        box = self._ext_box
        if box is None or not box.winfo_exists():
            return
        for w in box.winfo_children():
            w.destroy()
        try:
            self._ext_count.configure(
                text=f"共 {len(items)} 个；每个扩展的 help= 那行会自动进「帮助」菜单")
        except (tk.TclError, AttributeError):
            pass
        if not items:
            ttk.Label(box, text="还没装任何自定义指令。", bootstyle="secondary",
                      font=_font(9)).pack(anchor="w")
            return
        for it in items:
            row = ttk.Frame(box, padding=(0, 6))
            row.pack(fill="x")
            top = ttk.Frame(row)
            top.pack(fill="x")
            ttk.Label(top, text=it["name"], font=_font(10, "bold")).pack(side="left")
            ttk.Label(top, text="指令包" if it["kind"] == "dir" else "单文件",
                      bootstyle="secondary",
                      font=_font(8)).pack(side="left", padx=8)
            self._ghost_btn(top, "移除", lambda i=it["id"]: self._ext_remove(i),
                            color="danger", padx=10, pady=3).pack(side="right")
            cmds = "、".join(it["commands"]) if it["commands"] else ""
            _cline = ("指令：" + cmds) if cmds else "没注册任何指令 —— 这个包装进来是死的"
            ttk.Label(row, text=_soft_wrap(_cline, 700, 9),
                      bootstyle="secondary" if cmds else "danger",
                      wraplength=700, font=_font(9)).pack(anchor="w")
            ttk.Label(row, text=f"{'、'.join(it['files'])} · {_human_size(it['size'])}",
                      bootstyle="secondary",
                      font=_font(8)).pack(anchor="w")
            if it.get("extra_files"):
                ttk.Label(row, text="包里还带了：" + "、".join(it["extra_files"]),
                          bootstyle="secondary",
                          font=_font(8)).pack(anchor="w")
            if it.get("error"):
                ttk.Label(row, text=_soft_wrap("⚠ 加载失败：" + it["error"], 700, 8),
                          bootstyle="danger",
                          wraplength=700, font=_font(8)).pack(anchor="w")
            if it.get("dup"):
                _dup = ("⚠ 指令名和别的扩展撞了：" + "、".join(it["dup"])
                        + "（同名时按加载顺序，后面的盖住前面的）")
                ttk.Label(row, text=_soft_wrap(_dup, 700, 8),
                          bootstyle="danger", wraplength=700,
                          font=_font(8)).pack(anchor="w")
            ttk.Separator(box).pack(fill="x")

    def _ext_pick(self):
        """「导入扩展…」—— 本机选文件，装进 extensions/local/。"""
        path = filedialog.askopenfilename(
            title="选一个扩展包",
            filetypes=[("扩展包（.py / .zip）", "*.py *.zip"),
                       ("单文件指令", "*.py"), ("指令包", "*.zip"), ("所有文件", "*.*")])
        if not path:
            return
        ok, msg, info = _extmgr.import_file(path)
        if not ok and info.get("conflict"):
            if messagebox.askyesno("已经装过了", f"{msg}。\n\n覆盖它吗？"):
                ok, msg, _info = _extmgr.import_file(path, overwrite=True)
            else:
                self._set_ext_note("已取消（没有覆盖）", bad=True)
                return
        if ok:
            self._set_ext_note(f"{msg} —— 到「⑦ 运行方式」点「重启」才生效")
        else:
            self._set_ext_note(msg, bad=True)
        self.app.toast(msg, ok)
        self._refresh_ext_list()

    def _ext_remove(self, ident):
        if not messagebox.askyesno(
                "移除扩展",
                "移除这个扩展？文件会挪进 extensions/.removed/（不是真删，还能捞回来）。"):
            return
        ok, msg = _extmgr.remove(ident)
        self.app.toast(msg, ok)
        if ok:
            self._set_ext_note(msg + " —— 重启机器人后才不再响应它的指令")
        else:
            self._set_ext_note(msg, bad=True)
        self._refresh_ext_list()

    def _refresh_ext_list(self):
        """导入 / 移除之后重画列表（探测结果有 mtime 缓存，这次基本是秒回）。"""
        try:
            self._draw_ext_items(_extmgr.list_items(probe=True))
        except Exception as e:                                        # noqa: BLE001
            self._set_ext_note(f"列不出来：{type(e).__name__}: {e}", bad=True)

    def _ext_open_folder(self):
        try:
            os.makedirs(_extmgr.LOCAL_DIR, exist_ok=True)
            opener = getattr(os, "startfile", None)
            if opener is None:
                self._set_ext_note("这个系统不支持打开文件夹", bad=True)
                return
            opener(_extmgr.LOCAL_DIR)
        except OSError as e:
            self._set_ext_note(f"打不开：{e}", bad=True)


def _human_size(n):
    """字节数 → 「1.2 KB」这种给人看的写法（配置中心的扩展列表用）。"""
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


# ── 脑内抽屉标签页（Phase 2 原生渲染）────────────────────────────
# 桶名 / 标签沿用原来的抽屉口径（待办 / 思绪两大类），配色用的是同一套状态色。
# 🔴 分类大改版（用户 2026-10-12 定）：展示层只剩「待办」和「思绪」两大类 ——
#    · 待办 = 待办（未完成 / 已完成），灵感 = 思绪（未整理 / 已整理）；
#    · 「已处理 / 已决议」两种烧脑分法**合并**为「已整理」（库里 status 不动，
#      存量 decided 照常读，只是不再单独展示）。
_BUCKET_LABEL = {
    "memo": "待办 · 未完成",
    "memo_done": "待办 · 已完成",
    "open": "思绪 · 未整理",
    "done": "思绪 · 已整理",
    "decided": "思绪 · 已整理",
    "dropped": "已丢弃",
}
_BUCKET_ORDER = ("memo", "memo_done", "open", "done", "decided", "dropped")

# 底部 chips 的两大类入口（点开是列表窗口，不是就地筛轮播）。
_LIST_GROUPS = ("todo", "thought")
_GROUP_LABEL = {"todo": "待办", "thought": "思绪"}
_GROUP_BUCKETS = {"todo": ("memo", "memo_done"),
                   "thought": ("open", "done", "decided")}
# 列表窗口里的子筛选：待办 → 未完成 / 已完成；思绪 → 未整理 / 已整理。
_SUB_LABEL = {"memo": "未完成", "memo_done": "已完成",
              "open": "未整理", "done": "已整理", "decided": "已整理"}
# 子筛选项的「码」用的是桶名；「已整理」同时收纳库里 done / decided 两种状态。
_SUB_MATCH = {"done": ("done", "decided")}


def _fmt_time(s):
    """把库里的 `created_at`（ISO，形如 `2026-10-12T18:42:43`）显示成**完整时间**。

    🔴 以前只截前 10 位（到「日」）—— 同一天记了好几条就分不清先后
       （用户 2026-10-12：「卡片时间戳完全显示（目前只显示到日）」）。
       这里原样显示到秒，只把 ISO 的 `T` 换成空格；解析不出来就原样返回。
    """
    t = str(s or "").strip()
    if not t:
        return ""
    return t.replace("T", " ")[:19]


def _meta_text(it, memo):
    """气泡 / 浮层左上角那行「类型 · 状态 · 日期」。

    桶名已经带了类型前缀（「待办 · 未完成」）就只留桶名，否则才补类型。
    """
    kind_label = "待办" if memo else "思绪"
    bucket_label = _BUCKET_LABEL[it["bucket"]]
    if bucket_label.startswith(kind_label):
        return f"{bucket_label} · {it['date']}"
    return f"{kind_label} · {bucket_label} · {it['date']}"


def _flow_plan(memo, st):
    """浮层 / 列表右侧那排流程按钮：(文案, 颜色, 目标状态, 是否禁用)。

    🔴 「完成」与「取消完成」、「整理」与「取消整理」是**同一个按钮的两种状态**
       （用户 2026-10-12）—— 按当前状态给文案，并把 target 指到**相反**的状态。
    🔴 以前「取消完成」的 target 也写死成 "done"（等于再点一次「完成」），
       所以点了没反应（用户报的「待办列表点击取消完成没反应」）。这里 target
       跟着状态走：已完成 → open，未完成 → done。
    🔴 已丢弃不在这两个按钮可及的范围内（它在回收站处理），所以丢了禁用。
    """
    dropped = st == _store.STATUS_DROPPED
    if memo:
        toggle = (("← 取消完成", "open") if st == _store.STATUS_DONE
                  else ("✓ 完成", "done"))
    else:
        toggle = (("← 取消整理", "open")
                  if st in (_store.STATUS_DONE, _store.STATUS_DECIDED)
                  else ("✓ 整理", "done"))
    return [
        (toggle[0], "success", toggle[1], dropped),
        ("× 丢弃", "danger", "dropped", dropped),
    ]


# 桶 → (浅色 hex, 深色 hex)，跟 web 抽屉的状态色一致
# 🔴 「已完成」（memo_done）/「已整理」（done, decided）都是**终态**，边框统一用
#    灰色（用户 2026-10-12：「把已整理和已完成的内容边框颜色改成灰的」）——
#    不再用绿色，绿色会和「未完成」的橙 / 「未整理」的青抢注意力。
#    decided 与 done 展示上合并，所以**同色**（否则两条都写「已整理」却两个色）。
_TERMINAL_GRAY = ("#6b7472", "#8b98a9")
_BUCKET_COLOR = {
    "memo": ("#b8791a", "#f6ad55"),
    "memo_done": _TERMINAL_GRAY,
    "open": ("#0f8f85", "#4fd1c5"),
    "done": _TERMINAL_GRAY,
    "decided": _TERMINAL_GRAY,
    "dropped": ("#6b7472", "#7c8798"),
}


def _clean_text(text):
    """清洗正文：去 @ 占位、压掉多余空白、**保留换行**。"""
    t = re.sub(r"<[^<>\s]{1,40}>", " ", text or "")
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()

# ── 展示层折行（英文 / 符号串不再被硬砍）────────────────────────────
# 🔴 症状（用户报的「出现英文与符号就会异常换行」）：气泡 / 卡片里的 URL、路径
#    这种**没有空格的长串**，Tk 的 `wraplength` 只能从中间硬砍
#    （`Administrator/Desk` → `.../D` + `esktop`），而它前一行的短文本又因为整串
#    塞不下被迫单独占一行（「看看这个」后面一大截空白）。
# 🔴 Tk 的折行**只认 ASCII 空格**（实测：细空格 U+2009 / 发丝空格 U+200A /
#    零宽空格 U+200B / 全角空格 U+3000 一律不当断行点）—— 所以既不能用零宽
#    空格「骗」它，也不能靠改字体。
# 🔴 于是自己按**像素**贪心折行：中文/全角**逐字**可断（Tk 本来也这么做），
#    英文串在「`/ ? & = . -`」这些分隔符**之后**可断；单块仍超宽才硬切。
#    换行是**纯展示**的 `\n`，库里正文一个字都不改 —— 卡片编辑框 / 导出 /
#    搜索命中的都还是原文。
# 🔴 历史教训：早期那个「每 18 字硬插 \n」的函数把中文断在句子中间
#    （「我可怎／么话啊」）。这里的规矩是**绝不**在中文之间插断点，
#    且只在「整块放不下」时才折 —— 不会再犯。
_BREAK_AFTER = set("/?&=.:,;!@#%+~|\\-_")
_WRAP_FONTS = {}


def _wrap_font(size, weight=None):
    """按字号取（并缓存）一个 Tk 字体对象，用来量像素宽。"""
    key = (size, weight)
    f = _WRAP_FONTS.get(key)
    if f is None:
        f = tkfont.Font(font=_font(size, weight))
        _WRAP_FONTS[key] = f
    return f


def _is_cjk(ch):
    """中日韩字符 / 全角标点：**逐字**可断行。"""
    o = ord(ch)
    return (0x2E80 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF
            or 0xFE30 <= o <= 0xFE4F or 0xFF00 <= o <= 0xFFEF
            or 0x3040 <= o <= 0x30FF)


def _wrap_chunks(par):
    """把一段文本拆成「可在其后断行」的最小块。"""
    out, buf = [], ""

    def flush(breakable):
        nonlocal buf
        if buf:
            out.append((buf, breakable))
            buf = ""

    for ch in par:
        if ch in " \t":
            flush(True)
            out.append((" ", True))
        elif _is_cjk(ch):
            flush(True)
            out.append((ch, True))
        else:
            buf += ch
            if ch in _BREAK_AFTER:
                flush(True)
    flush(False)
    return out


def _soft_wrap(text, width, size, weight=None):
    """按像素把 text 折成多行（纯展示）→ 保证每行都放得下、长串不留大片空白。

    `width` = 可用像素宽（调用方给的是 wraplength 同级的可用宽）。留 2px 余量，
    免得字体度量取整后仍比 Tk 的边界略宽、被 Tk 再折一次。
    """
    if not text:
        return text
    font = _wrap_font(size, weight)
    avail = max(40, int(width) - 2)
    lines = []
    for par in text.split("\n"):
        if not par:
            lines.append("")
            continue
        line = ""
        for seg, _brk in _wrap_chunks(par):
            if seg == " " and not line:
                continue                      # 行首空格丢掉
            if font.measure(line + seg) <= avail:
                line += seg
                continue
            if font.measure(seg) > avail:     # 单块自己就超宽 → 硬切
                if line:
                    lines.append(line.rstrip())
                    line = ""
                piece = ""
                for ch in seg:
                    if piece and font.measure(piece + ch) > avail:
                        lines.append(piece)
                        piece = ""
                    piece += ch
                line = piece
            else:
                lines.append(line.rstrip())
                line = seg.lstrip()
        lines.append(line.rstrip())
    # 去掉末尾可能多出来的空行
    while len(lines) > 1 and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _bucket_of(row):
    """一条库记录 → 它在这个抽屉里的桶名。"""
    kind = row.get("kind") or _store.KIND_MEMO
    status = row.get("status") or _store.STATUS_OPEN
    if kind == _store.KIND_MEMO:
        if status == _store.STATUS_DROPPED:
            return "dropped"
        return "memo_done" if status == _store.STATUS_DONE else "memo"
    return status if status in _BUCKET_LABEL else "open"


class BrainDrawerTab(ttk.Frame):
    """脑内抽屉：泡泡上浮轮播舞台（产品核心特色）。

    形态与 web 抽屉一致——条目像泡泡从窗口底部缓缓上浮、到顶部渐隐消失、
    循环轮播；点气泡弹出全文浮层并处理，处理后移出轮播。
    Tk 控件没有透明度，渐入渐出用「颜色向底色插值」模拟。

    布局要点：
    · **泳道制**：舞台横向均分若干条泳道，一条泳道同一时刻只放一个气泡。
      这样气泡再高也不会互相压住 —— 气泡高度差异大（长文本能有半屏高），
      靠「错开起始时间」是躲不过重叠的，只有独占泳道才稳。
    · 速度一致：所有气泡按同一个 px/s 走，行程 = 舞台高 + 自身高度 + 余量，
      所以越高的气泡走得越久，但都能完整飘出顶部，不会半路被切掉。
    · 开屏预热：每条泳道先摆一个、直接放到飞行途中（首屏立刻有内容）。
    · 顶部遮罩：气泡**不用整体渐色**，而是从下面滑进舞台顶部的渐变带、
      被逐层擦掉（用户 2026-10-12 把原来的 64px 实色带改成「不透明度 0」的渐变）。
      早期用「整块插值到背景色」的做法是全局 alpha：长气泡的上半截会在
      下半截还很清楚的时候先变灰 —— 看着像被啃掉一块（用户报的「视觉缺口」）。
      渐变带没有这个问题：每层只盖住自己那一层，盖住多少就是多少。
    """

    BASE_DUR = 26.0        # 基准行程（1.26 倍舞台高）走完的秒数
    TRAVEL_VH = 1.26       # 基准行程 = 1.26 倍舞台高
    TICK_MS = 16           # 动画帧间隔（≈60fps；实测单帧只花 ~0.4ms，撑得住）
    # 🔴 前台自动刷新间隔。用户 2026-10-12 报「发送灵感后不点刷新看不到」——
    #    机器人是**另一个进程**，它往 memos.db 写完这边收不到任何通知，以前
    #    只能手动点「刷新」。现在窗口在前台时按这个间隔看一眼库变没变。
    AUTO_REFRESH_MS = 5000
    LANE_MIN_W = 256       # 每条泳道最小宽度（决定泳道条数：1060px → 4 条）
    # 🔴 每条泳道同时最多几个气泡。3 → 6（用户：「现在看起来还是太稀疏」）。
    #    提到 6 之后同泳道间距只剩几十像素，**必须**有真实的不重叠判据兜住 ——
    #    见 `_lane_span`：光看「最低的那个」挡不住「插到两条中间压上去」。
    PER_LANE = 6
    MIN_GAP = 24           # 同泳道相邻气泡的最小纵向间隔
    TEXT_MAX = 1000        # 气泡正文最多显示多少字（超了截断 + 省略号，点开看全文）
    LINE_MAX = 45          # 气泡正文最多几行（极端多行的记录兜个底，别让气泡高到离谱）
    # 🔴 超过这个字数的条目用 **2 条泳道宽**的大卡（用户 2026-10-12）：
    #    长记录挤在单泳道里又高又窄（三四百 px 高、两百多 px 宽），一屏放不下
    #    几条；给它两条泳道的宽度，高度立刻塌下来，观感从「一根柱子」变「一张卡」。
    #    大卡占 2 条泳道，泳道判据（_lane_span / _lane_bottom）都按 span 算。
    WIDE_CHARS = 300
    WIDE_SPAN = 2
    BAR_H = 34             # 底部筛选条高度（气泡在它下沿之下就开始渐显）
    # 🔴 顶部渐隐区高度（气泡升到这里就开始化掉）。**不再用实体遮罩带** ——
    #    用户 2026-10-12：「遮罩太硬…不能做渐变就去掉黄框内容，思绪卡片直接被
    #    页签栏切断」。Tk 没有真正的 alpha，实色遮罩带就是一条硬边（还比底深，
    #    看着像一道缝），所以改成**整卡按位置均匀渐隐**（见 `_alpha_at`）：
    #    整张卡一起化掉，不会出现「上半截先变灰」的缺口（那是按位置分片渐变的锅）。
    TOP_FADE_H = 84
    ALPHA_STEP = 0.05      # 渐隐渐出的量化步长（0.12 只有 8 档，肉眼是「跳」的）
    BUBBLE_R = 13          # 气泡圆角半径（背景画在舞台 Canvas 上）
    # 🔴 内容控件相对圆角矩形要内缩多少（占半径的比例）。
    #    控件本体是**直角**矩形，内缩量必须 ≥ r·(1 − 1/√2) ≈ 0.293r，
    #    否则它的直角会戳到圆弧外面露出小方块。取 0.45r 留足余量：
    #    0.293r 只有 0.3px 的富余，抗锯齿一抖就在角上留一道细缝。
    INSET_RATIO = 0.45
    # 🔴 气泡内部的排版常量 —— **正文折行宽度必须由它们算出来**，别在别处
    #    另写一个魔数。以前 wraplength 写死 `泳道宽 − 48`，而真实可用宽度是
    #    `泳道宽 − 2·inset − 2·ROW_PADX`（= −32），折行宽度比容器宽会
    #    让每行最后一个字的右边缘被容器裁掉，用户看到的就是「字边缘消失」。
    # 🔴 状态色只画在圆角矩形的**边框**上（accent）：去掉了左侧强调条，
    #    那根直角边块和圆角矩形的「内角不一致」是用户报丑的根源。
    ROW_PADX = 14          # 气泡内边距（左右）
    ROW_PADY = 10          # 气泡内边距（上下）

    def __init__(self, master, app):
        super().__init__(master, padding=0)
        self.app = app
        # 🔴 没有「当前桶」这个概念了：底部那一排不再是筛选器，点分类是**开列表
        #    窗口**（见 `_build_chips`），轮播舞台永远展示展示范围内的全部条目。
        self._tag = None         # None = 不限
        self._show = list(_settings.get("bubbles_show") or [])   # 设置里允许展示的桶
        self._tags = []
        self._thumbs = []        # 气泡里的 PhotoImage 引用防 GC
        self._modal_thumbs = []  # 浮层里的 PhotoImage（与气泡分开，别被轮播清掉）
        self._items = []         # 当前筛选下的轮播条目
        self._queue = []         # pick 队列（**存 id**，轮转；见 _sync 的说明）
        self._bubbles = []       # 在屏气泡记录
        self._modal = None
        self._trash = None       # 回收站窗口（同一时刻只开一个）
        self._trash_pick = {}    # 回收站里 id → BooleanVar（勾选态）
        self._list = None        # 分类 / 搜索结果列表窗口（同上，只开一个）
        # 🔴 当前列表窗对应哪个入口（("group", "todo") / ("query", "xx") …）+
        #    重建回调。用户重复点同一个入口时**置顶**而不是重开（见 _open_list）；
        #    卡片里改了状态后要靠回调把列表刷新（见 _set_status）。
        self._list_key = None
        self._list_rebuild = None
        self._tick_job = None
        self._due = None         # 下一帧该跑的时刻（_tick 自校正用）
        self._rebuild_job = None
        self._L = dict(w=800, h=500, lanes=2, gap=24, lane_w=380, travel=630)
        # 前台自动刷新：定时器 id + 「上次看到的库文件版本」（见 _auto_tick）
        self._auto_job = None
        self._db_stamp = None
        # 🔴 字号改了会整窗重建（App._rebuild_for_font），这个标签页会被 destroy。
        #    `after` 定时器不随控件一起消失，`_tick` 一旦在销毁后再跑就会
        #    对着已销毁的控件调 Tk → 被全局异常钩子弹成错误框。用这个旗子兜住。
        self._alive = True
        self._build()
        self._tick()
        self._db_stamp = self._db_mtime()   # 基线 = 建窗这一刻的库版本
        # 切回前台立刻查一次（不然要等满一拍 AUTO_REFRESH_MS 才看得到新条目）
        self.app.root.bind("<FocusIn>", self._on_focus_in, add="+")
        self._schedule_auto()

    def destroy(self):
        """先熄灯再拆 —— 让还在路上的 `_tick` / `_auto_tick` 自己停住（见 `_alive`）。"""
        self._alive = False
        super().destroy()

    # ── 前台自动刷新 ─────────────────────────────────────────────
    def _schedule_auto(self):
        """排下一次自动刷新（`_alive` 是唯一开关，见 `destroy`）。"""
        if self._alive:
            self._auto_job = self.after(self.AUTO_REFRESH_MS, self._auto_tick)

    def _db_mtime(self):
        """memos.db（含 WAL 副档）的最新 mtime，单位 ns；变了 = 别的进程写过库。

        🔴 **不看 `-shm`**：WAL 模式下它是共享内存索引，光是打开一个连接就会动，
           拿它当判据会「自己触发自己」变成无限刷新。只看主库 + `-wal`。
        """
        best = 0
        for suf in ("", "-wal"):
            try:
                best = max(best, os.stat(_store.DB + suf).st_mtime_ns)
            except OSError:
                pass
        return best

    def _front_and_visible(self):
        """窗口此刻是不是「在前台看得见」—— 最小化 / 被藏起来 / 当前不是本页签都不算。

        🔴 `winfo_ismapped()` 一个就够了：Tk 的 Notebook 会把**非当前页签**
           unmap 掉，所以它同时覆盖了「当前页是脑内抽屉」和「窗口没被藏起来」。
        """
        try:
            root = self.app.root
            if root.state() == "iconic":        # 最小化到任务栏
                return False
            return bool(self.winfo_ismapped() and root.winfo_viewable())
        except tk.TclError:
            return False

    def _auto_tick(self):
        """前台自动刷新：机器人写进来的新条目，不点「刷新」也能自己冒出来。

        🔴 **用库文件 mtime 当闸门，而不是无脑 `refresh()`**：`refresh()` 里有
           `_store.init()`，它一上来就 `BEGIN IMMEDIATE` 抢写锁；要是正好撞上
           bot 在写，就得等满 busy timeout（默认 5 秒）—— 界面直接卡住。
           加了闸门就只有「真有新数据」时才碰库，频率低、也不会白抢锁。
        🔴 窗口不在前台时不查（最小化 / 在后台），省掉无谓的 stat 和读库。
        """
        self._auto_job = None
        if not self._alive:
            return
        if self._front_and_visible():
            self._check_db_changed()
        self._schedule_auto()

    def _check_db_changed(self):
        """库文件变了就重查一遍（闸门共用，`_auto_tick` 与切回前台都走它）。"""
        if self._db_mtime() != self._db_stamp:
            self.refresh(self.app.state)

    def _on_focus_in(self, event):
        """窗口刚被切回前台 → 立刻看一眼库，别让用户干等满一拍 AUTO_REFRESH_MS。

        🔴 必须判 `event.widget is root`：Tk 的 bindtags 会把**窗口内所有控件**的
           FocusIn 冒泡到这个 toplevel 的绑定上（点一下搜索框都会进来），
           不判的话焦点每动一下就查一次库。
        """
        if event.widget is not self.app.root or not self._alive:
            return
        self._check_db_changed()

    # ── 基础 ─────────────────────────────────────────────────────
    def _c(self, key):
        """当前主题色（ttkbootstrap Colors 的字段）。"""
        return getattr(self.app.style.colors, key)

    def _dim(self):
        """禁用 / 占位文字色：比正文淡但仍看得见（别用 border，那是边框色）。"""
        return dim_ink(self._c("fg"), self._c("bg"))

    def _bar_h(self):
        """底部工具条此刻占多高（两行：搜索行 + 按钮行）。

        🔴 别再用写死的 `BAR_H` —— 首屏 spread 要拿它算「气泡最矮能摆到哪」，
           写死的话加了搜索行之后气泡会被摆到搜索框底下（开场就是透明的）。
        """
        try:
            return max(self.BAR_H, self._foot.winfo_reqheight())
        except (tk.TclError, AttributeError):
            return self.BAR_H

    def _build(self):
        # 舞台铺满整个标签页
        self.stage = tk.Canvas(self, highlightthickness=0, bd=0,
                               bg=self._c("bg"))
        self.stage.pack(fill="both", expand=True)
        self.stage.bind("<Configure>", lambda e: self._relayout())
        # 焦点给舞台（点气泡 / 拖选区都从它出发，行为统一）
        self.stage.focus_set()

        # 🔴 顶部**不再有实色遮罩带**（用户 2026-10-12：「遮罩太硬」）。
        #    气泡升到顶端的化掉是靠 `_alpha_at` 的**顶部渐隐**做的：
        #    整张卡一起淡出，不是被一条实色墙齐刷刷切掉。
        #    （Tk 没有真正的 alpha，实色遮罩带必然是硬边，而且是**比底更深**的
        #     一条 —— 真机像素采样实测：遮罩带里 16,22,31，带外也 16,22,31，
        #     看着就是一道缝。0.25px 的抖动渐变也盖不住这层观感，所以直接弃用。）

        # 底部：**两行** —— 上面一行搜索，下面一行是原来的 chips + 工具按钮。
        # 🔴 做成舞台的**同级兄弟**（不是 canvas window）—— 否则后建的气泡
        #    会盖在筛选条上面（窗口 item 的层级提不上去）。place 到最底、
        #    铺满全宽，气泡永远从它下面过。
        foot = tk.Frame(self, bg=self._c("bg"))
        self._foot = foot
        foot.place(relx=0, rely=1, anchor="sw", relwidth=1)
        foot.lift()

        # ── 搜索行 ────────────────────────────────────────────────
        # 加它的理由（用户原话）：「灵感多起来等轮播根本找不到」。轮播是「等它
        # 飘过来」，搜索是「直接去找」—— 两条路并存，谁都不替代谁。
        # 结果**不进舞台**，开一个列表窗口（和回收站同一套骨架）按时间倒序看。
        sbar = tk.Frame(foot, bg=self._c("bg"), padx=14, pady=2)
        sbar.pack(side="top", fill="x")
        tk.Label(sbar, text="搜索", bg=self._c("bg"), fg=self._c("secondary"),
                 font=_font(9)).pack(side="left")
        self._q_var = tk.StringVar(value="")
        qent = tk.Entry(sbar, textvariable=self._q_var, width=26, font=_font(9),
                        bd=1, relief="solid", bg=self._c("bg"),
                        fg=self._c("fg"), insertbackground=self._c("fg"),
                        highlightthickness=0)
        qent.pack(side="left", padx=(6, 0), ipady=2)
        qent.bind("<Return>", lambda e: self._search())
        qent.bind("<Escape>", lambda e: (self._q_var.set(""),
                                         self.stage.focus_set()))
        RoundedButton(sbar, "搜一下", self._search,
                      kind="ghost", radius=10, padx=10, pady=3,
                      font=_font(9),
                      fg=self._c("primary"), fill=self._c("bg"),
                      outline=self._c("border"),
                      hover_fill=self._c("active"),
                      hover_outline=self._c("primary"),
                      hover_fg=self._c("primary"),
                      parent_bg=self._c("bg")).pack(side="left", padx=(6, 0))
        tk.Label(sbar, text="回车搜索 · 正文 / 标签 / 结论 都搜 · 结果在列表窗口里看",
                 bg=self._c("bg"), fg=self._c("secondary"),
                 font=_font(8)).pack(side="left", padx=(10, 0))

        bar = tk.Frame(foot, bg=self._c("bg"), padx=14, pady=7)
        self._bar = bar
        bar.pack(side="top", fill="x")
        # chips 放在自己的容器里 —— 设置改了（展示范围变了）要整排重建，
        # 直接往 bar 里 pack 的话没法只清掉 chips。
        self._chips_box = tk.Frame(bar, bg=self._c("bg"))
        self._chips_box.pack(side="left")
        self._chips = {}
        tk.Label(bar, text="标签", bg=self._c("bg"), fg=self._c("secondary"),
                 font=_font(9)).pack(side="left", padx=(10, 4))
        self._tag_var = tk.StringVar(value="")
        self._tag_cb = ttk.Combobox(bar, textvariable=self._tag_var, width=12,
                                    state="readonly")
        self._tag_cb.pack(side="left", padx=2)
        self._tag_cb.bind("<<ComboboxSelected>>", lambda e: self._set_tag())
        RoundedButton(bar, "刷新", lambda: self.refresh(self.app.state),
                      kind="ghost", radius=10, padx=8, pady=3,
                      font=_font(9),
                      fg=self._c("secondary"), hover_fill=self._c("active"),
                      parent_bg=self._c("bg")).pack(side="left", padx=(8, 0))
        # 手动回旋镖：按「配置中心 → 回旋镖」的规则翻一条旧记录出来
        RoundedButton(bar, "回旋镖", self._manual_boomerang,
                      kind="ghost", radius=10, padx=10, pady=3,
                      font=_font(9),
                      fg=self._c("primary"), fill=self._c("bg"),
                      outline=self._c("border"),
                      hover_fill=self._c("active"),
                      hover_outline=self._c("primary"),
                      hover_fg=self._c("primary"),
                      parent_bg=self._c("bg")).pack(side="left", padx=(6, 0))
        # 导出：和机器人「导出」指令、网页版「⬇ 导出」共用 export.py 的排版。
        RoundedButton(bar, "导出", self._export_all,
                      kind="ghost", radius=10, padx=10, pady=3,
                      font=_font(9),
                      fg=self._c("secondary"), fill=self._c("bg"),
                      outline=self._c("border"),
                      hover_fill=self._c("active"),
                      hover_outline=self._c("primary"),
                      hover_fg=self._c("primary"),
                      parent_bg=self._c("bg")).pack(side="left", padx=(6, 0))
        # 回收站：已丢弃的条目**只在这里出现**（不进轮播舞台），点开是静态列表。
        # 🔴 常驻显示、**不看 `bubbles_show`** —— 那个设置管的是「轮播舞台上展示
        #    哪几类」，把清理入口也挡掉的话用户就找不到「已丢弃」了（用户报过）。
        RoundedButton(bar, "回收站", self._open_trash,
                      kind="ghost", radius=10, padx=10, pady=3,
                      font=_font(9),
                      fg=self._c("secondary"), fill=self._c("bg"),
                      outline=self._c("border"),
                      hover_fill=self._c("active"),
                      hover_outline=self._c("danger"),
                      hover_fg=self._c("danger"),
                      parent_bg=self._c("bg")).pack(side="left", padx=(6, 0))
        self._build_chips()

    def _build_chips(self):
        """底部那一排：**「思维流」+ 「待办 / 思绪」两个分类入口**。

        🔴 语义变了（用户 2026-10-12 定）—— 这一排**不再是轮播的筛选器**：
           · 「思维流」（原来的「全部」）= 当前这个视图本身，永远选中；点它只是
             把焦点还给舞台。
           · 点「待办 / 思绪」= 打开一个**列表窗口**（和回收站同一套骨架），
             在那儿按时间倒序翻，并再按【未完成/已完成】/【未整理/已整理】子筛 ——
             不用等轮播飘过来。
           轮播舞台永远展示 `bubbles_show` 范围内的全部条目。
        🔴 分类入口**不看 `bubbles_show`**：那个设置管的是「轮播舞台上展示哪几类」，
           而列表窗口是「回头找东西」，再拿它挡一道就本末倒置了。
        🔴 `dropped` 不列：它归底部常驻的「回收站」按钮。
        """
        for w in self._chips_box.winfo_children():
            w.destroy()
        self._chips = {}
        view = RoundedButton(self._chips_box, "思维流",
                             lambda: self.stage.focus_set(),
                             kind="ghost", radius=10, padx=10, pady=4,
                             font=_font(10),
                             fg=self._c("secondary"),
                             hover_fill=self._c("active"),
                             selected_fg=self._c("primary"),
                             selected_fill=lerp_color(self._c("primary"), self._c("bg"), 0.86),
                             parent_bg=self._c("bg"))
        view.pack(side="left", padx=(0, 3))
        view.set_selected(True)
        self._chips[None] = view
        for g in _LIST_GROUPS:
            chip = RoundedButton(self._chips_box, _GROUP_LABEL[g],
                                 lambda x=g: self._open_list(group=x),
                                 kind="ghost", radius=10, padx=10, pady=4,
                                 font=_font(10),
                                 fg=self._c("secondary"),
                                 hover_fill=self._c("active"),
                                 selected_fg=self._c("primary"),
                                 selected_fill=lerp_color(self._c("primary"), self._c("bg"), 0.86),
                                 parent_bg=self._c("bg"))
            chip.pack(side="left", padx=(0, 3))
            self._chips[g] = chip

    def _relayout(self):
        """舞台尺寸变化 → 重算泳道；泳道宽变了就重摆整屏。"""
        w = max(self.stage.winfo_width(), 600)
        h = max(self.stage.winfo_height(), 400)
        lanes = max(1, int(w // self.LANE_MIN_W))
        gap = 24
        lane_w = (w - gap * (lanes + 1)) / lanes
        old = self._L
        self._L = dict(w=w, h=h, lanes=lanes, gap=gap, lane_w=lane_w,
                       travel=h * self.TRAVEL_VH)
        # 筛选条是舞台的同级兄弟，靠 place(relwidth=1) 自己跟着变宽，
        # 这里只要保证它仍在舞台之上（重建气泡不会影响兄弟控件的层级，
        # 但重建整个标签页之后要再提一次）。
        self._foot.lift()
        # 🔴 气泡**宽度**是创建时按当时的泳道宽定死的，只挪 x0 不够 ——
        #    窗口一变宽，老气泡还按旧宽度铺，会横着压进隔壁泳道（实测重叠 26px）。
        #    泳道宽/条数变了就整体重摆；140ms 防抖，拖窗口时不会一路闪。
        if old["lanes"] != lanes or abs(old["lane_w"] - lane_w) > 0.5:
            self._schedule_rebuild()
            return
        for b in self._bubbles:
            b["x0"] = self._lane_x(b["lane"])
        # 泳道规格没变、只是尺寸微调：不够就按常规补位（不重排，避免闪）
        self._fill(spread=False)

    def _schedule_rebuild(self):
        """防抖：拖窗口过程中只重摆一次。"""
        if self._rebuild_job is not None:
            self.after_cancel(self._rebuild_job)
        self._rebuild_job = self.after(140, self._rebuild)

    def _rebuild(self):
        """按当前泳道尺寸重摆整屏气泡（尺寸变了必须重建，宽度改不了）。"""
        self._rebuild_job = None
        self._clear_stage()
        self._fill(spread=True)

    def _alpha_at(self, y, h):
        """气泡在高度 y（顶边）时的不透明度 —— 底部渐入 + 顶部渐隐。

        · **渐入**：顶边从舞台底边冒上来的 60px 内淡入。
          🔴 不能用「底边越过筛选条」当判据 —— 那样高气泡（能有两百多 px）
             顶边早就进视口了，alpha 还是 0，表现是「气泡隐形上升好几秒」。
        · **渐隐（顶部）**：顶边升进顶端 TOP_FADE_H 之内开始淡出，到顶边贴住
             舞台上沿时几乎透明 —— 于是气泡是「化掉」的，不是被页签栏硬切。
          🔴 判据用**顶边 y**（不是底边）：整张卡的 alpha 是同一个数，一起化。
             按位置分片渐变会让长卡「上半截先变灰」，那才是以前的「视觉缺口」。
          🔴 渐隐终点**不用到 0**：留一个极小值（0.04）让它完全贴边时基本看不见，
             但不会因为「alpha 必须正好 0」而多算一轮 —— 反正下一帧 p≥1 就摘掉了。
        """
        L = self._L
        fade_in = max(0.0, min(1.0, (L["h"] - y) / 60.0))
        fade_out = max(0.0, min(1.0, (y + 8.0) / self.TOP_FADE_H))
        return max(0.0, min(1.0, min(fade_in, fade_out)))

    def _lane_x(self, lane):
        """第 lane 条泳道的横向起点。"""
        L = self._L
        return L["gap"] + lane * (L["lane_w"] + L["gap"])

    def _lane_bottom(self, lane, now, span=1):
        """这 span 条泳道上最低（y 最大）气泡的底边；空泳道返回 None。

        span > 1 时（大卡）把跨这几条泳道的现有气泡都算进来。
        """
        low = None
        for b in self._bubbles:
            bs = b.get("span", 1)
            if b["lane"] + bs <= lane or b["lane"] >= lane + span:
                continue
            y = b["y_start"] - b["travel"] * (now - b["t0"]) / b["dur"]
            bottom = y + b["h"]
            if low is None or bottom > low:
                low = bottom
        return low

    def _lane_span(self, lane, now, y0, h, span=1):
        """候选气泡 [y0, y0+h] 放进这 span 条泳道，会不会和现有的撞（含 MIN_GAP 间距）。

        🔴 必须和**每一个**现有气泡比，不能只看最低的那个 —— 首屏 spread 是往
           舞台中间几个固定高度插的，只比最低的挡不住「插到两条中间压上去」。
           PER_LANE 从 3 提到 6 之后同泳道间距只剩几十像素，这个洞会立刻暴露。
        🔴 所有气泡**同速**上升 ⇒ 相对位置恒定 ⇒ 入队时判一次就够，
           不会出现「后面追上来撞上」。
        """
        top, bot = y0, y0 + h
        for b in self._bubbles:
            bs = b.get("span", 1)
            if b["lane"] + bs <= lane or b["lane"] >= lane + span:
                continue
            t = b["y_start"] - b["travel"] * (now - b["t0"]) / b["dur"]
            if bot + self.MIN_GAP > t and top - self.MIN_GAP < t + b["h"]:
                return False
        return True

    def _lane_ok(self, lane, now, span=1):
        """这条泳道现在能不能再放一个：新气泡从屏幕下方起步，只要求它和
        该泳道现有最低气泡之间留出 MIN_GAP 间距。

        🔴 因为所有气泡**同速**上升，间距一旦留出来就永远保持，所以只需在
        进入时刻判一次 —— 不会出现「后面追上来撞上」的情况。
        """
        low = self._lane_bottom(lane, now, span)
        if low is None:
            return True
        return low <= (self._L["h"] + 8) - self.MIN_GAP

    def _lane_ready(self, now, span=1):
        """随机挑一条能放的泳道；都没有返回 None。

        🔴 随机而不是「取编号最小的那条」：按编号取的话，先空出来的永远是
           0 号道，长时间跑下来三条道的利用率完全不均，看着像有规律。
        🔴 span=2（大卡）要保证 lane+span 不越界 —— 最后一条泳道放不下宽卡。
        """
        free = [ln for ln in range(self._L["lanes"] - span + 1)
                if self._lane_ok(ln, now, span)]
        return random.choice(free) if free else None

    # 🔴 一帧最多新建几个气泡窗口。全屏时舞台从 4 条泳道变 9 条、上限
    #    9×PER_LANE = 54 个 —— 一口气建 54 个子窗口会给窗口系统一次猛冲，
    #    实测在那一下能看到卡片文字「花」掉（用户报的「全屏后卡片出现乱码」）。
    #    分批建（每帧 6 个）既保住首屏观感，又不给系统那一下子。
    SPAWN_PER_FRAME = 6

    def _fill(self, spread=False):
        """把空位补齐到上限（**分批**：每帧最多 `SPAWN_PER_FRAME` 个）。

        spread=True 用于首屏：**每条泳道按 PER_LANE 个位置散开摆**（不是只摆一个、
        剩下的全从屏幕下方往上爬 —— 那样首屏只有两三条，看着很空）。
        spread=False 只管补位。

        🔴 为什么要随机：所有气泡**同速**上升，同时出发的会永远排成一条水平线。
        首屏那几条如果按固定比例摆，看上去就是「一行一行对齐」的表格 ——
        用户明确说「太整齐了，希望更有随机感一点」。

        🔴 每摆一个都要过 `_lane_ok`（`_spawn` 内部会判）—— 长文本气泡能有三百多 px
        高，硬塞满 PER_LANE 个会撞在一起。放不下就少放几个，安全优先。

        🔴 **分批建**：先把这一轮要摆的位置列出来（`_fill_plan`），再每帧建几个；
        没建完的排到下一帧（`_drain_fill`）。`spread` 的计划只算一次，
        后续帧只按计划继续建 —— 否则每帧重算会打乱随机、也可能重复摆同一段。
        """
        L = self._L
        cap = L["lanes"] * self.PER_LANE
        if spread:
            plan = []
            lanes = L["lanes"]
            # 把可见竖直区间均分成 PER_LANE 段；每段里随机取一点。
            # 分段保证同泳道两条之间的起始高度至少差一个段宽 —— 不会撞，也不齐。
            for lane in range(lanes):
                bands = list(range(self.PER_LANE))
                random.shuffle(bands)
                for r in bands:
                    lo = r / self.PER_LANE
                    hi = (r + 1) / self.PER_LANE
                    frac = 0.84 - 0.66 * random.uniform(lo, min(hi, 1.0))
                    plan.append((frac, lane))
            random.shuffle(plan)      # 打散顺序：先建哪几个不该有规律
            self._fill_plan = plan
            self._fill_cap = cap
        self._drain_fill(spread)

    def _drain_fill(self, spread):
        """按计划建若干个（最多一帧的量）；没建完排下一帧。"""
        L = self._L
        cap = getattr(self, "_fill_cap", L["lanes"] * self.PER_LANE)
        n = 0
        if spread and getattr(self, "_fill_plan", None):
            plan = self._fill_plan
            while plan and n < self.SPAWN_PER_FRAME:
                if len(self._bubbles) >= cap:
                    break
                frac, lane = plan.pop(0)
                self._spawn(frac=frac, lane=lane)
                n += 1
        elif not spread:
            while len(self._bubbles) < cap and n < self.SPAWN_PER_FRAME:
                if not self._spawn():
                    break
                n += 1
        # 还有得建 → 下一帧接着建（1ms 后；别用 0，免得饿死别的事件）
        if (spread and getattr(self, "_fill_plan", None)
                and len(self._bubbles) < cap):
            if self._alive:
                self.after(1, lambda: self._drain_fill(True))

    # ── 数据 / 筛选 ─────────────────────────────────────────────
    def refresh(self, state):
        self.app.state = state
        _store.init()  # 独立开 GUI 也不依赖 bot 先起
        # 🔴 展示范围是**设置项**，每次 refresh 都重读 —— 用户在配置中心里改完
        #    「不显示已丢弃」，切回抽屉就该立刻生效，不能等重启。
        show = list(_settings.get("bubbles_show") or [])
        if show != self._show:
            self._show = show
            self._build_chips()
        self._tags = _store.tags_all()
        names = [t["name"] for t in self._tags]
        self._tag_cb["values"] = [""] + names
        if self._tag not in names:
            self._tag = None
            self._tag_var.set("")
        # 数据变了就**增量对齐**，不整屏重建。
        # 🔴 以前这里比完 (id, status) 不一致就走 `_reload()`（`_clear_stage()` +
        #    `_prewarm()`）：用户点一下「已决议」，整屏气泡同时消失、再同时从
        #    下面冒出来 —— 就是那一「抽搐」（用户报的）。现在只摘掉真变了的那一个。
        self._sync(self._load_items())
        # 🔴 记下「我看到的这一版」：界面自己刚写完库（改状态 / 手动回旋镖 / 标签）
        #    时也让闸门对齐，免得下一拍 `_auto_tick` 又白刷一次。
        self._db_stamp = self._db_mtime()

    # 轮播舞台一次最多铺多少个 —— 它不是「翻资料」的地方，够铺满屏幕就行。
    # 🔴 列表窗口 / 搜索**不走这个上限**（见 `_list_query`）：那里是用户真去找东西
    #    的地方，必须有办法翻到最早的那条（审查报告点出的「500 条限制隐藏旧数据」）。
    STAGE_ROWS = 200

    def _rows(self):
        """轮播舞台的候选行（**原始行**，有上限，够铺满舞台即可）。"""
        return _store.query_items(limit=self.STAGE_ROWS)

    @staticmethod
    def _list_filter(group, sub, bucket):
        """列表窗口的大类 / 子筛 → SQL 的 (kinds, statuses) 过滤条件。

        🔴 把过滤**下推到 SQL**（而不是先捞固定窗口再在内存里筛）——
           这样记录再多也能翻到最早那条（审查报告点出的「500 条隐藏旧数据」）。
        「已整理」= 库里 done / decided 两种状态（见 `_SUB_MATCH`）。
        """
        kinds, statuses = None, None
        if group == "todo":
            kinds = [_store.KIND_MEMO]
        elif group == "thought":
            kinds = [_store.KIND_IDEA]
        if sub:
            kinds = ([_store.KIND_MEMO] if sub.startswith("memo")
                     else [_store.KIND_IDEA])
            statuses = list(_SUB_MATCH.get(sub, (sub,)))
        elif bucket is not None:
            kinds = ([_store.KIND_MEMO] if bucket.startswith("memo")
                     else [_store.KIND_IDEA])
            statuses = list(_SUB_MATCH.get(bucket, (bucket,)))
        return kinds, statuses

    def _collect(self, rows, *, gate_show):
        """库记录 → 轮播 / 列表通用的条目结构。

        `gate_show=True` 时套 `bubbles_show` 那道闸（轮播舞台用），
        `False` 时不套（列表窗口用 —— 见 `_build_chips` 的说明）。
        两者都**永远跳过已丢弃**：它归「回收站」。
        """
        out = []
        for r in rows:
            b = _bucket_of(r)
            # 🔴 已丢弃**不进轮播舞台、也不进分类列表**：它归底部常驻的
            #    「回收站」（静态列表）。轮播是「过一遍、顺手处理」，
            #    回收站是「回头清理」，两种心智分开。
            if b == "dropped":
                continue
            # 🔴 「设置里允许展示哪几类」只对**轮播舞台**生效。以前它完全没被读，
            #    用户在配置中心关掉「已丢弃」，抽屉里照样满屏丢弃条目。
            # 🔴 decided 与 done 展示层已合并（用户 2026-10-12）→ 过闸时按
            #    「已整理」一档判：勾了 done 两种都显示，都不勾两种都不显示。
            if gate_show:
                show_b = "done" if b == "decided" else b
                if show_b not in self._show:
                    continue
            content = _clean_text(r.get("content"))
            if not content:
                continue
            out.append({
                "id": r["id"], "kind": r.get("kind") or _store.KIND_MEMO,
                "status": r.get("status") or _store.STATUS_OPEN,
                "bucket": b, "content": content,
                "date": _fmt_time(r.get("created_at")),
                "tags": [t["name"] if isinstance(t, dict) else str(t)
                         for t in (r.get("tags") or [])],
                "images": r.get("images") or [],
                # 结论 / 备注（只有灵感有；待办在库层面就没有这个概念）
                "note": r.get("note") or "",
            })
        out.sort(key=lambda x: x["date"], reverse=True)
        return out

    def _load_items(self):
        """轮播舞台的条目池：展示范围之内的全部（再叠标签筛选）。"""
        out = self._collect(self._rows(), gate_show=True)
        if self._tag:
            out = [x for x in out if self._tag in x["tags"]]
        return out

    # 列表窗口一页取多少条（「加载更多」再取一页）。
    LIST_PAGE = 200

    def _list_items(self, group=None, sub=None, bucket=None, query=None,
                    limit=None, offset=0):
        """列表窗口的条目：**不看展示范围**，按大类 / 子筛 / 关键词筛，时间倒序。

        `group` = 列表窗口顶上的两大类（待办 / 思绪，见 _GROUP_BUCKETS）；
        `sub` = 打开后再按的子筛（未完成 / 已完成 / 未整理 / 已整理，码用桶名；
        「已整理」同时收纳库里 done / decided 两种状态，见 _SUB_MATCH）。
        🔴 搜索要搜「全部」而不是「轮播正在展示的那几类」—— 用户找东西的时候
           不知道、也不关心某个类别有没有被展示范围关掉。
        🔴 **筛选下推到 SQL + offset 分页**（`store.query_items`）：以前是
           「先捞 500 条固定窗口再在界面里筛」，超过 500 条后更早的数据
           在列表 / 搜索里永远翻不到（审查报告点出的「500 条隐藏旧数据」）。
        关键词命中范围：正文 + 标签 + 结论/备注 —— 由 SQL 的 LIKE 负责。
        """
        kinds, statuses = self._list_filter(group, sub, bucket)
        rows = _store.query_items(kinds=kinds, statuses=statuses, text=query,
                                 limit=limit or self.LIST_PAGE, offset=offset)
        out = self._collect(rows, gate_show=False)
        if group is not None and not sub:
            # 大类没有子筛时，SQL 只按 kind 粗筛；这里再按大类桶精确收一遍
            out = [x for x in out if x["bucket"] in _GROUP_BUCKETS[group]]
        return out

    def _list_total(self, group=None, sub=None, bucket=None, query=None):
        """同条件下的**总条数**（列表窗口用它显示「共 N 条 / 还能加载更多」）。"""
        kinds, statuses = self._list_filter(group, sub, bucket)
        return _store.count_query_items(kinds=kinds, statuses=statuses, text=query)

    def _set_tag(self):
        v = self._tag_var.get()
        self._tag = v if v else None
        self.refresh(self.app.state)

    def _sync(self, items):
        """把在屏气泡与最新数据对齐 —— 只动**真变了的那几个**。

        🔴 别退回「整屏清空重建」：用户每处理一条就看到整屏闪一下（抽搐）。
        规则：
        · 条目没了（被筛掉 / 删掉），或状态 / 正文 / 结论变了 → 摘掉它那一个气泡；
        · 其余气泡**原样留着**，位置和动画都不打断；
        · 最后 `_fill()` 补位。首屏（一个气泡都没有）走 spread 铺开，
          否则新气泡都在屏幕下方，要等十几秒才飘上来。
        """
        by_id = {x["id"]: x for x in items}
        for b in list(self._bubbles):
            old = b["item"]
            cur = by_id.get(old["id"])
            if (cur is None or cur["status"] != old["status"]
                    or cur["content"] != old["content"]
                    or cur.get("note") != old.get("note")):
                self._drop(b, refill=False)
        self._items = items
        # 🔴 队列存 **id** 不是下标：条目增删时下标会整体错位，轮转顺序就乱了
        #    （表现是「刚看过的那条又连着冒出来」）。已存在的 id 保持原顺序，
        #    新来的排到队尾。
        keep = [i for i in self._queue if i in by_id]
        seen = set(keep)
        for x in items:
            if x["id"] not in seen:
                keep.append(x["id"])
                seen.add(x["id"])
        self._queue = keep
        self._fill(spread=not self._bubbles)

    # ── 手动回旋镖 ──────────────────────────────────────────────
    def _manual_boomerang(self):
        """按「配置中心 → 回旋镖」的规则翻一条旧记录出来，弹出它的全文。

        规则全在 settings 里：`boomerang_days`（翻多少天以前）+ `boomerang_scope`
        （从哪几类里挑，分类与「⑤ 脑内抽屉」的展示范围一致）。和机器人收到
        「回旋镖」指令走**同一套** `store.pick_*` —— 不重新实现一遍规则。

        🔴 **抽出来的卡片会放回池子**（用户 2026-10-12）：不再 `mark_boomerang()`，
        池子恒定，同一条能被反复抽到（连着两次抽到同一张也算数）。
        🔴 抽中后**不再弹底部提示** —— 那条提示正好盖在「回旋镖」按钮上，
           用户点完想再点就点不着了（用户 2026-10-12 报的「通知遮盖了按钮」）。
           浮层标题栏本来就写着「几天前翻出来的旧记录」，信息没丢。
        """
        from datetime import datetime
        days = int(_settings.get("boomerang_days") or 0)
        scope = _settings.get("boomerang_scope")
        now = datetime.now()
        item = _store.pick_boomerang(days, scope, now)
        if not item:
            # 没翻到才提示（此时没有卡片浮层，用户也需要知道为什么没反应）
            left_at_all = _store.count_boomerang(0, scope, now)
            if left_at_all:
                self.app.toast(f"还没有满 {days} 天的旧记录（可推范围在"
                               f"「配置中心 → 回旋镖」里改）", False)
            else:
                self.app.toast("这个范围里还没有能翻的记录（可推范围在"
                               "「配置中心 → 回旋镖」里改）", False)
            return
        created = _fmt_time(item.get("created_at"))
        try:
            ago = (now - datetime.fromisoformat(item["created_at"])).days
        except (ValueError, TypeError):
            ago = days
        # 转成轮播用的条目结构，直接开浮层看全文
        tags = [t["name"] if isinstance(t, dict) else str(t)
                for t in (item.get("tags") or [])]
        self._open_modal({
            "id": item["id"], "kind": item.get("kind") or _store.KIND_MEMO,
            "status": item.get("status") or _store.STATUS_OPEN,
            "bucket": _bucket_of(item),
            "content": _clean_text(item.get("content")),
            "date": created, "tags": tags,
            "images": item.get("images") or [],
            "note": item.get("note") or "",
        }, banner=f"回旋镖 · {'今天' if ago <= 0 else f'{ago} 天前'}翻出来的旧记录")

    # ── 导出 ────────────────────────────────────────────────────
    def _export_all(self):
        """把全部待办 + 灵感导出成一个文件（网页版「⬇ 导出」的桌面版）。

        🔴 排版复用 `export.py` —— 和机器人「导出」指令、网页版导出是**同一份
           定义**，三处导出来的文件长得一模一样，不另写一套。
        🔴 格式从**文件对话框里选的扩展名**反推，不再先弹一次「要哪种格式」——
           桌面上的习惯就是「另存为时选类型」，一步到位。
        🔴 本地存盘没有 QQ 那 10MB 的限制，所以 zip 带原图也不拦。
        """
        fmts = [("Markdown", "*.md"), ("纯文本", "*.txt"),
                ("JSON", "*.json"), ("ZIP（含图片原图）", "*.zip")]
        # 🔴 库里有图时**默认就给 zip** —— 图片只存在 zip 里（md/txt 只是引用
        #    相对路径），默认给 md 的话用户会以为「图片没导出来」。
        has_pics = bool(_media.list_files())
        if has_pics:
            fmts = [fmts[3]] + fmts[:3]
        default_fmt = "zip" if has_pics else "md"
        try:
            fp = filedialog.asksaveasfilename(
                parent=self.app.root, title="导出思绪与待办",
                defaultextension="." + default_fmt, filetypes=fmts,
                initialfile=_export.filename(default_fmt))
        except Exception:                                   # noqa: BLE001
            return
        if not fp:                                          # 用户取消
            return
        fmt = os.path.splitext(fp)[1].lstrip(".").lower()
        try:
            _fname, data = _export.render(fmt)
            with open(fp, "wb") as f:
                f.write(data)
        except Exception as e:                              # noqa: BLE001
            messagebox.showerror("导出失败", f"{type(e).__name__}: {e}")
            return
        self.app.toast(f"已导出 {_export.human_size(len(data))} → {fp}", True)

    # ── 轮播引擎 ────────────────────────────────────────────────
    def _pick(self):
        """挑一条不在屏上的条目（队列轮转保证条条轮得到）。

        队列里存的是 **id**（见 `_sync`）—— 条目增删时下标会错位，
        用下标轮转会出现「刚看过的又连着冒出来」。
        """
        n = len(self._queue)
        if not n:
            return None
        by_id = {x["id"]: x for x in self._items}
        onscreen = {b["item"]["id"] for b in self._bubbles}
        for _ in range(n):
            i = self._queue.pop(0)
            self._queue.append(i)
            it = by_id.get(i)
            if it is not None and it["id"] not in onscreen:
                return it
        return None

    def _spawn(self, frac=None, lane=None):
        """在指定/可用泳道上放一个气泡。

        frac 给了就按舞台高度的比例摆（首屏铺开用）；否则从舞台下方起步。
        放不下、或没有条目可放时返回 False。
        """
        L = self._L
        now = time.monotonic()
        # 🔴 先挑条目：宽卡（span=2）占几条泳道由**这条有多长**决定，
        #    所以只能拿到条目之后再定泳道。
        it = self._pick()
        if not it:
            return False
        # 长条目给两条泳道的宽度（见 WIDE_CHARS）—— 高度立刻塌下来，
        # 观感从「一根又高又窄的柱子」变成「一张宽卡」。
        span = self.WIDE_SPAN if len(it["content"]) > self.WIDE_CHARS else 1
        # 🔴 从下方补位时可以先挑泳道：判据便宜，而且够用（新气泡的起点在舞台
        #    下方，只要最低那个让出 MIN_GAP 就一定不撞）。
        #    `frac` 那条路**不能**在这儿定泳道 —— 它是往舞台中间插的，得先拿到
        #    气泡真实高度才能判重叠，见下面 y0 算完之后那段。
        if lane is None and frac is None:
            lane = self._lane_ready(now, span)
            if lane is None:
                return False
        # 宽卡不能跨出舞台右缘（最后一条泳道放不下 2 条宽）
        if lane is not None and lane + span > L["lanes"]:
            return False
        width = L["lane_w"] * span + L["gap"] * (span - 1)
        w_int = int(width)
        inset = max(3, int(round(self.BUBBLE_R * self.INSET_RATIO)))
        # 🔴 正文折行宽度**从布局常量算**（见 ROW_PADX 那一带的说明）：
        #    气泡窗口被 canvas 钉成 `lane_w − 2·inset`，里面还要减掉 row 的
        #    左右内边距、左侧强调条、强调条与正文的间距。再留 2px 余量，
        #    防字体度量取整后仍然超出容器。
        content_w = max(120, w_int - inset * 2 - self.ROW_PADX * 2 - 2)

        # 气泡卡片用专属配色（见 bubble_palette）—— 比背景亮一档的纯白/抬升深面，
        # 配状态色描边（accent，按条目桶色）+ 右下投影，让卡片「浮」在背景上。
        bg = self._c("bg")
        surf, _, shadow = bubble_palette(self.app.theme)
        # 圆角卡片 = 舞台 Canvas 上的圆角矩形（fill/outline 随 alpha 插值）
        # + 内容控件贴在其内、四周内缩 1px 露出边框和圆角。
        # Tk 控件本体是直角矩形，任何「控件自身当卡片」的画法都不可能圆角。
        frame = tk.Frame(self.stage, bg=bg, bd=0, highlightthickness=0)
        row = tk.Frame(frame, bg=bg, padx=self.ROW_PADX, pady=self.ROW_PADY)
        row.pack(fill="both", expand=True)
        fade = [(frame, "bg", surf), (row, "bg", surf)]

        # 状态色只画在圆角矩形的**边框**上（accent），不再用左侧强调条——
        # 那根直角边块和圆角矩形「内角不一致」是用户报丑的根源。
        accent = _BUCKET_COLOR[it["bucket"]][
            1 if self.app.theme == THEME_DARK else 0]

        body = tk.Frame(row, bg=bg)
        body.pack(side="left", fill="both", expand=True)

        meta = tk.Label(body, text=_meta_text(it, it["kind"] == _store.KIND_MEMO),
                        bg=bg, fg=bg, font=_font(8))
        meta.pack(anchor="w")
        fade.append((meta, "fg", self._c("secondary")))

        # 🔴 每条只显示前 TEXT_MAX 个字、最多 LINE_MAX 行：气泡越矮，一屏能放下的
        #    条数越多（用户反馈「一屏显示的内容太少」）。全文点一下就能看。
        #    行数也要截 —— 群里分十几行写的记录，只截字数是不够的。
        _full = it["content"]
        _cut = _full[:self.TEXT_MAX]
        _lines = _cut.split("\n")
        if len(_lines) > self.LINE_MAX:
            _cut = "\n".join(_lines[:self.LINE_MAX])
        shown = _cut + ("…" if len(_cut) < len(_full) else "")
        # 🔴 直接交给 Tk 的 wraplength 折行，**别自己插换行** ——
        #    实测 Tk 对中文 / 长路径 / 超长 URL 都能按像素正确折行
        #    （长路径那串没有空格也照样折）。早期误判成「Tk 只在空白处折」，
        #    加了个每 18 字硬插 \n 的函数，结果中文被断在句子中间
        #    （「我可怎／么话啊」），用户报「异常换行」。
        txt = tk.Label(body, text=_soft_wrap(shown, content_w, 10),
                       justify="left", anchor="nw",
                       bg=bg, fg=bg,
                       wraplength=content_w,
                       font=_font(10))
        txt.pack(anchor="w", pady=(4, 0))
        fade.append((txt, "fg", self._c("fg")))

        if it["images"]:
            imgs = tk.Frame(body, bg=bg)
            imgs.pack(anchor="w", pady=(6, 0))
            fade.append((imgs, "bg", surf))
            for rel in it["images"][:2]:
                fp = self._resolve(rel)
                if not fp:
                    continue
                try:
                    from PIL import Image, ImageTk
                    im = Image.open(fp)
                    im.thumbnail((88, 88))
                    ph = ImageTk.PhotoImage(im)
                except Exception:
                    continue
                lbl = tk.Label(imgs, image=ph, bg=bg)
                lbl.image = ph
                self._thumbs.append(ph)
                fade.append((lbl, "bg", surf))
                lbl.pack(side="left", padx=(0, 4))

        # 结论 / 备注：**有才渲染**（已处理 / 已决议 / 已丢弃 常见），照 web 版。
        # 没有就不建这个控件 —— 气泡高度是 winfo_reqheight() 实测的，
        # 少一行自然就矮一行，不会白占位置。
        note_lbl = None
        if it.get("note"):
            note_lbl = tk.Label(body,
                                text=_soft_wrap("结论：" + it["note"],
                                                content_w, 9),
                                justify="left", anchor="nw", bg=bg, fg=bg,
                                wraplength=content_w,
                                font=_font(9))
            note_lbl.pack(anchor="w", pady=(5, 0))
            fade.append((note_lbl, "fg", self._c("secondary")))

        self.stage.update_idletasks()
        # 🔴 内缩量必须 ≥ r·(1 − 1/√2) ≈ 0.293r：控件本身是**直角**矩形，
        #    内缩不够的话它的四个直角会戳到圆角矩形的圆弧**外面**，
        #    在角落露出小方块。取 0.45r（见 INSET_RATIO 的说明）。
        #    （`inset` / `w_int` 已在函数开头算好 —— 正文折行宽度要用到它们。）
        h = max(frame.winfo_reqheight(), 60) + inset * 2

        # 行程覆盖「起点 → 整块完全升出顶部」，速度大家都一样，
        # 所以高气泡走得更久 —— 这样任何高度的气泡都能完整飘完，不会半路消失。
        #
        # 🔴 从下方补位时要**再随机往下退一点**（入场延迟）。全体气泡同速上升，
        #    同一时刻出发的会永远排成一条水平线 —— 上一版补位全从 h+8 起步，
        #    结果就是底下一整排齐刷刷对齐（用户说「太整齐了」）。
        #    往下退只会让间距更大，不会撞到同泳道上面那个。
        # 🔴 气泡的「起点」永远在舞台下方；`frac` 只是把它**出生那一刻的位置**
        #    抬到半空（见下面的 warm）。
        #    以前 `y_start` 直接写 y0、`warm` 却按「从下方一路走到 y0」算 ——
        #    两者不自洽，真实出生位置变成 `2*y0 − 舞台高 − 8`：首屏各段被整体
        #    压到屏幕上方（低几段直接出生在屏幕外、当场被丢），而 `_lane_span`
        #    判的还是 y0，于是「判着不撞、实际叠在一起」。
        #    统一到 `y_start` 之后：判据 = 真实位置，速度仍全体一致。
        y_start = L["h"] + 8 + random.uniform(0.0, 120.0)
        if frac is None:
            y0 = y_start
        else:
            # 摆到指定高度，并保证**整个气泡**落在筛选条之上（否则它一开场就是
            # 透明的 —— 底边还在栏底下，按位置算出来的 alpha 是 0）
            bar_top = L["h"] - self._bar_h()
            y0 = max(6.0, min(L["h"] * frac, bar_top - h - 12))

        # 🔴 泳道要**拿到真实高度之后**才能定 / 才能判重叠 —— 见函数开头。
        #    放不下就把刚建好的控件拆掉（否则 canvas 上会留一堆看不见的窗口）。
        if lane is None:
            free = [ln for ln in range(L["lanes"] - span + 1)
                    if self._lane_span(ln, now, y0, h, span)]
            lane = random.choice(free) if free else None
        if lane is None or not self._lane_span(lane, now, y0, h, span):
            try:
                frame.destroy()
            except tk.TclError:
                pass
            return False

        travel = y_start + h + 16          # 起点 → 整块升出顶部
        # 🔴 速度 = travel/dur = 舞台高×TRAVEL_VH / BASE_DUR —— 与 travel 无关，
        #    所以全体气泡同速。这条性质是 `_lane_span`「入队判一次就够」的前提，
        #    改 travel/dur 的口径时**必须**保住它。
        dur = self.BASE_DUR * (travel / (L["h"] * self.TRAVEL_VH))
        # 起始时刻 = 现在往前推「已经走完的那一段」，于是气泡一放出来就在 y0 处。
        # frac 那条路用它把气泡直接摆到半空；补位那条路 warm=0，从起点往上爬。
        warm = (y_start - y0) / travel
        warm = max(0.0, min(0.98, warm))
        # 横向漂移：大了才有「飘」的随机感，但要小于泳道间隙，
        # 否则气泡会横着探进隔壁泳道（实测会视觉重叠）。
        dx = random.uniform(-1.0, 1.0) * min(L["gap"] * 0.45, 11.0)
        # 投影：先画、落在卡片圆角矩形**之下**（canvas 后建的盖在先建的）。
        # 偏移 BUBBLE_SHADOW_OFS，渐入时和卡片同步从底色插到投影色。
        # 🔴 描边统一加粗到 BUBBLE_BORDER_W（用户 2026-10-12：「颜色边框加粗」）——
        #    1px 在浅色壁纸上几乎看不见，气泡像贴着一条淡色边。投影保持 1px
        #    （它是「一道边」，不是「一个框」，粗了会发脏）。
        sx, sy = BUBBLE_SHADOW_OFS
        shadow_id = round_rect(self.stage, (x0 := self._lane_x(lane)) + sx, y0 + sy,
                              x0 + w_int + sx, y0 + h + sy,
                              self.BUBBLE_R, fill=bg, outline=bg, width=1)
        bg_id = round_rect(self.stage, x0, y0,
                           x0 + w_int, y0 + h,
                           self.BUBBLE_R, fill=bg, outline=accent,
                           width=BUBBLE_BORDER_W)
        win = self.stage.create_window(x0 + inset, y0 + inset, window=frame,
                                       anchor="nw", width=w_int - inset * 2)
        # 🔴 立刻按位置把 alpha 算出来，别留 0 等 _tick 来纠正 —— 首屏摆好的
        #    气泡要马上可见，否则会看到「一片空白、过一会儿才慢慢冒出来」。
        a0 = self._alpha_at(y0, h)
        self._apply_alpha(bubbles_bg=bg_id, fade=fade,
                          alpha=a0, bg=bg, surf=surf, accent=accent,
                          shadow_id=shadow_id, shadow=shadow)
        # 🔴 这里**不用**再 tag_raise 筛选条 / 遮罩带 —— 它们是舞台的**同级兄弟**
        #    控件（place + lift），层级天然在整块舞台之上，气泡建多少个都压不下去。
        #    （以前是 canvas window + tag_raise，而 tag_raise 对窗口 item 无效，
        #     气泡其实一直盖在筛选条上面。）
        b = dict(win=win, bg_id=bg_id, frame=frame, fade=fade, item=it,
                 lane=lane, span=span, x0=x0, dx=dx, y_start=y_start, inset=inset,
                 surf=surf, accent=accent, shadow=shadow, shadow_id=shadow_id,
                 # 🔴 圆角矩形是**多边形**，每帧只能用 move() 平移。
                 #    用 coords(bg_id, x1,y1,x2,y2) 会把它截断成 2 个点
                 #    （Tk 实测：40 点 → 2 点），圆角整个消失、只剩一条对角线
                 #    残影从卡片左上角露出来（用户报的「两个角的不明像素点」）。
                 bx=x0, by=y0,
                 t0=time.monotonic() - warm * dur,
                 dur=dur, travel=travel, alpha=a0, w=w_int, h=h)
        self._bubbles.append(b)
        clickable = [frame, row, body, txt, meta]
        if note_lbl is not None:
            clickable.append(note_lbl)
        for wgt in clickable:
            wgt.bind("<Button-1>", lambda e, x=it: self._open_modal(x))
        self.stage.tag_bind(bg_id, "<Button-1>",
                            lambda e, x=it: self._open_modal(x))
        return True

    def _apply_alpha(self, bubbles_bg, fade, alpha, bg, surf, accent,
                     shadow_id=None, shadow=None):
        """把一整套控件颜色插到 alpha（1=完全显现，0=融进底色）。

        bubbles_bg 是舞台 Canvas 上气泡的圆角矩形背景 id —— fill 从底色
        插到卡片色、outline 从底色插到**状态色**（accent，按条目桶色），
        与内容控件同步渐隐。
        shadow_id 是卡片下方那块投影 —— 和卡片同步从底色插到投影色。
        """
        for w, prop, target in fade:
            try:
                w.configure({prop: lerp_color(bg, target, alpha)})
            except tk.TclError:
                pass
        if bubbles_bg is not None:
            try:
                self.stage.itemconfigure(
                    bubbles_bg, fill=lerp_color(bg, surf, alpha),
                    outline=lerp_color(bg, accent, alpha))
            except tk.TclError:
                pass
        if shadow_id is not None and shadow is not None:
            try:
                c = lerp_color(bg, shadow, alpha)
                self.stage.itemconfigure(shadow_id, fill=c, outline=c)
            except tk.TclError:
                pass

    def _drop(self, b, refill=True):
        """气泡升出屏幕 → 移除；refill=True 时在空出的泳道上补一条。

        泳道号是气泡自带的（不随列表顺序变），所以这里不用重排别人的横向位置。
        """
        try:
            self.stage.delete(b["win"])
            self.stage.delete(b["bg_id"])
            self.stage.delete(b["shadow_id"])
            b["frame"].destroy()
        except tk.TclError:
            pass
        if b in self._bubbles:
            self._bubbles.remove(b)
        if refill:
            self._spawn()

    def _tick(self):
        """推进一帧动画，并把下一帧排好。

        🔴 排下一次用的是**「下次该跑的时刻」**，不是固定 `after(40)` ——
        固定延迟会把 Tk 的调度误差一帧帧累起来（实测 40ms 漂成 45~55ms），
        动画看着就是「一卡一卡」。落后太多（刚从后台切回来）就重新对齐，
        免得一秒钟补几十帧。
        """
        if not self._alive:
            return                      # 控件已销毁（整窗重建），别再排下一帧
        step = self.TICK_MS / 1000.0
        now = time.monotonic()
        if self._due is None or not (now - 0.25 < self._due < now + 0.5):
            self._due = now + step
        else:
            self._due += step
        delay = int(round(max(0.001, self._due - now) * 1000))
        self._tick_job = self.after(max(1, delay), self._tick)

        # 🔴 不可见时**照样推进时间轴**，只跳过渲染。
        #    以前这里直接 `return`，最小化期间气泡既不过期、也不补位；再打开时
        #    所有气泡在同一帧集体过期被清空，补上来的新气泡还都在屏幕下方 ——
        #    表现就是「隔一会儿打开整屏没内容」（用户报的）。位置是按时间算的，
        #    所以后台跑完再切回来，画面直接就是稳态，不会跳。
        visible = bool(self.winfo_ismapped())
        bg, surf = self._c("bg"), self._c("light")
        for b in list(self._bubbles):
            p = (now - b["t0"]) / b["dur"]
            if p >= 1.0:
                self._drop(b)
                continue
            if not visible:
                continue
            y = b["y_start"] - b["travel"] * p
            x = b["x0"] + b["dx"] * p
            # 🔴 位置**取整**，而且背景和内容用同一个整数坐标：
            #    canvas 里的嵌入窗口在 Windows 上只能落在整数像素，
            #    背景矩形却能画在小数上 —— 两者不一致时每帧会错开 1px，
            #    看着像卡片里的字在抖。对齐了就没有这个抖动。
            xi, yi = int(round(x)), int(round(y))
            # 🔴 位置没变就**整帧跳过**（不 coords、不 move、不 repaint）：
            #    60fps 下气泡常常还卡在同一整数像素上，每帧照搬一遍是无意义的
            #    GDI 调用 —— 一屏十几个窗口、每个每帧一次 setwindowpos，
            #    既是纯开销，也让子窗口在移动中更容易被 DWM 捕捉到「半合成」的
            #    中间态（快速移动的文本窗口偶发花屏 / 彩边就是这么来的）。
            if xi == b["bx"] and yi == b["by"]:
                # alpha 仍按位置算（位置没变 → alpha 也没变，跳过即可）
                continue
            try:
                self.stage.coords(b["win"], xi + b["inset"], yi + b["inset"])
                # 🔴 圆角矩形是**多边形**，只能 `move()` 平移。
                #    `coords(bg_id, x1,y1,x2,y2)` 会把 40 个点截断成 2 个点
                #    （Tk 实测），圆角消失、只剩一条对角线残影从卡片左上角
                #    露出来 —— 用户报的「两个角的不明像素点」。
                ddx, ddy = xi - b["bx"], yi - b["by"]
                self.stage.move(b["bg_id"], ddx, ddy)
                self.stage.move(b["shadow_id"], ddx, ddy)
                b["bx"], b["by"] = xi, yi
            except tk.TclError:
                self._bubbles.remove(b)
                continue
            # 渐隐渐出都按位置算（见 _alpha_at）—— 与 _spawn 共用同一套口径
            alpha = self._alpha_at(y, b["h"])
            if abs(alpha - b["alpha"]) >= self.ALPHA_STEP:
                b["alpha"] = alpha
                self._apply_alpha(b["bg_id"], b["fade"], alpha, bg, surf, b["accent"],
                                  shadow_id=b["shadow_id"], shadow=b["shadow"])

    def _clear_stage(self):
        for b in list(self._bubbles):
            try:
                self.stage.delete(b["win"])
                self.stage.delete(b["bg_id"])
                self.stage.delete(b["shadow_id"])
                b["frame"].destroy()
            except tk.TclError:
                pass
        self._bubbles.clear()
        self._thumbs.clear()

    # ── 全文浮层 + 处理 ────────────────────────────────────────
    def _build_tag_editor(self, parent, it):
        """浮层里的「标签」编辑区 —— 打标签的唯一入口就在这里。

        干嘛的：一条记录当前的标签以 chip 列出（点它就摘掉）；下面一行左边是
        「输入框 + 加上」，**同一行的右边**列出库里已经用过的标签，点一下直接打上
        —— 建议和输入并排，不再单独占一行。
        谁调用它：`_open_modal`。返回最外层 Frame —— 编辑态下由调用方
        `pack_forget()` 收起来（和 web 版一样：一次只做一件事）。

        标签是**数据不是设置** —— 落 `store.set_item_tags()`，不进 settings.json；
        所以这里改完要 `refresh()` 让轮播里的筛选下拉、卡片上的标签跟着更新。
        """
        light = self._c("light")
        wrap = tk.Frame(parent, bg=light)
        # 🔴 这里**不 pack** —— 由调用方 grid 定位（编辑态要 grid_remove 收起来）

        state = {"tags": list(it.get("tags") or [])}

        head = tk.Frame(wrap, bg=light)
        head.pack(anchor="w", fill="x")
        tk.Label(head, text="标签", bg=light, fg=self._c("secondary"),
                 font=_font(9)).pack(side="left", padx=(0, 8))
        chips = tk.Frame(head, bg=light)
        chips.pack(side="left")

        add = tk.Frame(wrap, bg=light)
        add.pack(anchor="w", fill="x", pady=(6, 0))
        entry = tk.Entry(add, width=14, font=_font(9), bd=1,
                         relief="solid", bg=light, fg=self._c("fg"),
                         insertbackground=self._c("fg"),
                         highlightthickness=0)
        entry.pack(side="left", ipady=2)
        RoundedButton(add, "加上", lambda: add_name(entry.get()),
                      kind="ghost", radius=9, padx=10, pady=3,
                      font=_font(9),
                      fg=self._c("primary"), hover_fill=self._c("active"),
                      parent_bg=light).pack(side="left", padx=(6, 0))

        # 「已有」紧跟在输入行右侧：用过的标签在这儿等着被点，不用再往下找一行
        pool = tk.Frame(add, bg=light)
        pool.pack(side="left", padx=(14, 0))
        pool_head = tk.Label(pool, text="已有", bg=light, fg=self._c("secondary"),
                             font=_font(9))
        pool_head.pack(side="left", padx=(0, 6))
        pool_chips = tk.Frame(pool, bg=light)
        pool_chips.pack(side="left")

        def commit(names):
            names = [n.strip() for n in names if n and n.strip()]
            seen, out = set(), []
            for n in names:
                if n not in seen:
                    seen.add(n)
                    out.append(n)
            _store.set_item_tags(it["id"], out)
            state["tags"] = out
            it["tags"] = out
            redraw()
            self.refresh(self.app.state)      # 卡片 / 筛选下拉跟着更新

        def add_name(name):
            if name and name.strip():
                commit(state["tags"] + [name.strip()])
            entry.delete(0, "end")

        def remove(name):
            commit([t for t in state["tags"] if t != name])

        def redraw():
            for w in chips.winfo_children():
                w.destroy()
            if not state["tags"]:
                tk.Label(chips, text="还没打标签", bg=light,
                         fg=self._dim(),
                         font=_font(9)).pack(side="left")
            for t in state["tags"]:
                RoundedButton(chips, f"{t} ×", lambda x=t: remove(x),
                              kind="ghost", radius=9, padx=8, pady=3,
                              font=_font(9),
                              fg=self._c("primary"),
                              hover_fill=self._c("active"),
                              parent_bg=light).pack(side="left", padx=(0, 4))
            for w in pool_chips.winfo_children():
                w.destroy()
            rest = [x["name"] for x in _store.tags_all()
                    if x["name"] not in state["tags"]]
            if not rest:
                pool.pack_forget()
            else:
                pool.pack(side="left", padx=(14, 0))
                for t in rest[:12]:
                    RoundedButton(pool_chips, t, lambda x=t: commit(state["tags"] + [x]),
                                  kind="ghost", radius=9, padx=8, pady=3,
                                  font=_font(9),
                                  fg=self._c("secondary"),
                                  hover_fill=self._c("active"),
                                  parent_bg=light).pack(side="left", padx=(0, 4))

        entry.bind("<Return>", lambda e: add_name(entry.get()))
        redraw()
        return wrap

    def _open_modal(self, it, banner=None):
        """点气泡弹出的全文浮层：看全文、打标签、改正文、推进流程。

        banner 不为空时，在标题行下面多显示一行强调说明（手动回旋镖用它
        告诉用户「这条是翻出来的旧记录」）。

        🔴 布局照 web 版抽屉的浮层来（用户明确要求「参考 webui」）：
          · 右上角一个「编辑 / 保存」切换按钮 —— 它作用的是**正文**，就该挨着正文；
          · 底部动作按钮**整条流程都常显**，当前所在的那一站灰掉 ——
            用户一眼看到「我在哪、还能去哪」，而不是按钮忽隐忽现；
          · 进编辑态就把标签行 / 附件 / 动作区收起来，一次只做一件事。
        """
        if self._modal is not None and self._modal.winfo_exists():
            self._modal.destroy()
        self._modal_thumbs.clear()
        win = tk.Toplevel(self)
        self._modal = win
        # 标题叫「思绪卡片」：这是**一条**记录的全貌，不是整个抽屉（用户改的）。
        win.title("思绪卡片")
        win.configure(bg=self._c("bg"))
        win.transient(self.app.root)
        win.geometry("+%d+%d" % (self.app.root.winfo_rootx() + 120,
                                 self.app.root.winfo_rooty() + 90))
        # 圆角卡片：**先**建 Canvas，**再**把 card 建在 Canvas 里（card 必须是
        # canvas 的子孙，`create_window` 才能显示它 —— 建在 win 下当兄弟是不显示的，
        # 表现就是「点开一片空白」）。
        light = self._c("light")
        # 🔴 浮层分两层：**上面是可滚动的正文区，下面是固定常显的动作条**。
        #    以前整张卡片一起长高，长记录（合并转发那种上千字）会把卡片撑得比
        #    屏幕还高，底部那排「已处理 / 已决议 / 丢弃」被直接顶出屏幕 ——
        #    用户根本点不到，等于没法改状态（用户报的「过长文字不压缩导致无法
        #    正常更改状态」）。
        #    pack 顺序有讲究：**先** pack 底部条、**再** pack 滚动区，
        #    后者才会拿到剩下的空间。
        PAD = 10               # 浮层四周留白
        CARD_INSET = 6         # 圆角底衬的内缩（r=16 → 至少 4.7px，见下）
        BAR_R = 14             # 底部圆角面板的圆角半径
        # 浮层上下两半拼成**一张**圆角卡：
        #   · 上面 cv 的卡底只圆「顶边」两角（round_rect_top），底边是直线；
        #   · 下面 bar_cv 的卡底只圆「底边」两角（round_rect_bottom），顶边是直线；
        #   中间那段接缝是直线 → 看着就是同一张卡，不再是「上圆下直」。
        #   两者宽度都 = cwc（在 _fit_card 里对齐），左右边缘自然齐平。
        bar_cv = tk.Canvas(win, highlightthickness=0, bd=0, bg=self._c("bg"))
        bar_cv.pack(side="bottom", fill="x", padx=PAD, pady=(0, PAD))
        # 动作条内容住在 bar_wrap 里，bar_cv 只负责画底色 + 下圆角。
        # 🔴 bar_wrap 是 Canvas 的子 Frame，必须用 create_window 挂进去才显示
        #    —— 直接 pack 只排布它内部、它自己仍不可见（表现就是「按钮全没了」）。
        bar_wrap = tk.Frame(bar_cv, bg=light, padx=16)
        # 分隔线放进 bar_wrap，跟按钮共用同一套 padding：分隔线 pady=(0,10)、
        # 按钮条 pady=(0,10) → 按钮上下间距对称，各 10px。
        tk.Frame(bar_wrap, bg=self._c("border"), height=1).pack(
            fill="x", pady=(0, 10))
        # 先把窗口挂上（位置 / 尺寸留给 _fit_card 用 cwc 算），x 内缩 CARD_INSET
        # 让按钮跟上面的卡片正文左对齐。
        bar_win = bar_cv.create_window(CARD_INSET, 0, window=bar_wrap,
                                       anchor="nw")
        cv = tk.Canvas(win, highlightthickness=0, bd=0, bg=self._c("bg"))
        cv.pack(side="top", fill="both", expand=True, padx=PAD, pady=(PAD, 0))
        card = tk.Frame(cv, bg=light, padx=22, pady=18)
        # 🔴 card 内部用 grid 不用 pack：编辑态要把中间的几块收起来，
        #    而 `grid_remove()` 会**记住位置**、`grid()` 一调就回原处；
        #    pack 的 `pack_forget()` 再 pack 会跑到末尾，顺序全乱。
        card.columnconfigure(0, weight=1)

        kind, st = it["kind"], it["status"]
        memo = kind == _store.KIND_MEMO
        accent = _BUCKET_COLOR[it["bucket"]][
            1 if self.app.theme == THEME_DARK else 0]

        # ── 行 0：类型 · 状态 · 日期 ………………………… 编辑 / 保存 ──
        meta_row = tk.Frame(card, bg=light)
        meta_row.grid(row=0, column=0, sticky="ew")
        tk.Label(meta_row, text=_meta_text(it, memo), bg=light, fg=accent,
                 font=_font(9, "bold")).pack(side="left")
        banner_lbl = None
        if banner:
            banner_lbl = tk.Label(card, text=banner, bg=light,
                                  fg=self._c("primary"),
                                  font=_font(9))
            banner_lbl.grid(row=1, column=0, sticky="w", pady=(6, 0))

        # ── 行 2：正文（只读 Text：能拖选 / Ctrl+C ↔ 编辑态换成编辑框）────
        body_holder = tk.Frame(card, bg=light)
        body_holder.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        # 🔴 正文用**只读 Text** 而不是 Label（用户 2026-10-13：卡片上的字选不中、
        #    复制不了，不符合记笔记的习惯）。Label 天生不支持选择；Text 天然支持
        #    拖选 + Ctrl+C，而 `state="disabled"` 只挡编辑、**不挡选择与复制**。
        # 🔴 Text 的 `width` / `height` 单位是**字符 / 行**，量不出像素宽 ——
        #    所以外面套一个固定像素尺寸的盒子（`pack_propagate(False)`），尺寸由
        #    `_body_fit` 按折行结果算。这样「卡片宽度跟着正文走」的行为不变，
        #    Text 只管显示和选择。
        body_box = tk.Frame(body_holder, bg=light)
        body_box.pack(anchor="nw")
        body_box.pack_propagate(False)
        txt = tk.Text(body_box, wrap="none", bd=0, highlightthickness=0,
                      padx=0, pady=0, spacing1=0, spacing2=0, spacing3=0,
                      bg=light, fg=self._c("fg"), font=_font(11),
                      cursor="xterm", takefocus=0,
                      selectbackground=self._c("primary"),
                      selectforeground=self._c("light"))
        # 🔴 存原始正文：正文宽是**渲染后**由 `_fit_card` 按卡片实宽定的，比这里
        #    的 520 宽不少。不重折的话文字只铺到 520px、右边留一大截空白
        #    （用户报的「思绪卡片右边较大空隙」）。`_fit_card` 会拿这个原文重折。
        txt._orig_text = it["content"]
        txt.insert("1.0", _soft_wrap(it["content"], 520, 11))
        txt.configure(state="disabled")
        txt.pack(fill="both", expand=True)
        # 编辑框拉高（用户 2026-10-12：编辑灵感时文本框太小）—— 6 行只够
        # 瞥见两句话，长一点的灵感得反复在里面翻。12 行 ≈ 小半屏，
        # 浮层本身高度封顶 + 正文区滚动，框大了也顶不穿屏幕。
        editbox = scrolledtext.ScrolledText(body_holder, height=12, wrap="word",
                                           font=_font(11),
                                           bd=0, highlightthickness=1,
                                           highlightbackground=self._c("primary"),
                                           highlightcolor=self._c("primary"),
                                           bg=self._c("bg"), fg=self._c("fg"),
                                           insertbackground=self._c("fg"))
        # 编辑态才显示的提示行（照 web 版：只改正文 · 创建时间不变）
        edit_hint = tk.Label(body_holder, text="只改正文 · 创建时间不变 · Esc 取消",
                             bg=light, fg=self._c("secondary"),
                             font=_font(8))
        # 先都不 pack —— 进编辑态才换上

        # ── 行 3：标签 ─────────────────────────────────────────────
        tag_wrap = self._build_tag_editor(card, it)
        tag_wrap.grid(row=3, column=0, sticky="ew", pady=(12, 0))

        # ── 行 4：结论 / 备注（**只有灵感有**）──────────────────────
        # 照 web 版：打开时把当前结论预填进框，点下面的动作按钮时把框里的
        # 内容一起提交（改状态 = 顺手写结论，一步完成）。
        # 🔴 框里显示什么就提交什么 —— 清空框 = 抹掉结论，这是用户主动行为。
        #    待办没有「结论」这个概念，所以整块不给它建。
        note_wrap, note_box, note_save = None, None, None
        if not memo:
            note_wrap = tk.Frame(card, bg=light)
            note_wrap.grid(row=4, column=0, sticky="ew", pady=(12, 0))
            note_wrap.columnconfigure(0, weight=1)
            # 标题行：左边「结论 / 备注」，右边一个「保存备注」。
            # 🔴 这个按钮不是多余的：底部那排动作按钮会**顺手把结论一起提交**，
            #    而「当前所在的那一站」是灰的 —— 所以「已经是已决议、只想再补一句
            #    结论」这条路以前是堵死的（用户报的「已有状态的灵感写决议和备注
            #    没办法保存」）。单独给一个只改结论、不动状态的入口。
            nhead = tk.Frame(note_wrap, bg=light)
            nhead.grid(row=0, column=0, sticky="ew")
            tk.Label(nhead, text="结论 / 备注（可选）", bg=light,
                     fg=self._c("secondary"),
                     font=_font(9)).pack(side="left")
            note_box = tk.Text(note_wrap, height=2, wrap="word",
                               font=_font(10),
                               bd=0, highlightthickness=1,
                               highlightbackground=self._c("border"),
                               highlightcolor=self._c("primary"),
                               bg=self._c("bg"), fg=self._c("fg"),
                               insertbackground=self._c("fg"))
            note_box.grid(row=1, column=0, sticky="ew", pady=(4, 0))
            if it.get("note"):
                note_box.insert("1.0", it["note"])

            def _note_now():
                return note_box.get("1.0", "end").strip()

            def _sync_note_btn():
                """没改动就灰着 —— 免得用户点了以为「没反应」。"""
                try:
                    note_save.configure(
                        state="normal" if _note_now() != (it.get("note") or "")
                        else "disabled")
                except tk.TclError:
                    pass

            def _save_note():
                """只保存结论，**不动状态**（见上面那段说明）。"""
                val = _note_now()
                _store.set_idea_status([it["id"]], it["status"], val)
                it["note"] = val
                self.refresh(self.app.state)
                self.app.toast("备注已保存", True)
                _sync_note_btn()

            def _fit_note():
                """结论框高度跟着内容长。

                🔴 以前写死 `height=2`：长结论只看得到头两行，后面全被截掉
                   （用户报的）。这里用 Tk 自己的 `count(..., "displaylines")`
                   数**折行之后**的行数 —— 比按字数估准（中文 / 长 URL 都会折）。
                   不封顶：卡片本来就带滚动，宁可长也不要藏。
                """
                try:
                    note_box.update_idletasks()
                    n = note_box.count("1.0", "end-1c", "displaylines")
                except (tk.TclError, TypeError, ValueError):
                    return
                if isinstance(n, (tuple, list)):
                    n = n[0] if n else 1
                n = max(2, int(n or 1))
                try:
                    if int(note_box.cget("height")) != n:
                        note_box.configure(height=n)
                        win.after(20, _fit_card)
                except (tk.TclError, ValueError):
                    pass

            def _note_changed(_e=None):
                _fit_note()
                _sync_note_btn()

            note_save = RoundedButton(nhead, "保存备注", _save_note,
                                      kind="ghost", radius=8, padx=10, pady=3,
                                      font=_font(9),
                                      fg=self._c("primary"),
                                      hover_fill=self._c("active"),
                                      disabled_fg=self._dim(),
                                      parent_bg=light)
            note_save.pack(side="right")
            note_box.bind("<KeyRelease>", _note_changed)
            note_box.bind("<<Paste>>", lambda e: win.after(20, _note_changed))
            win.after(20, _note_changed)

        # ── 行 5：附件图片 ─────────────────────────────────────────
        imgs = None
        if it["images"]:
            imgs = tk.Frame(card, bg=light)
            imgs.grid(row=5, column=0, sticky="ew", pady=8)
            for rel in it["images"][:6]:
                fp = self._resolve(rel)
                if not fp:
                    continue
                try:
                    from PIL import Image, ImageTk
                    im = Image.open(fp)
                    im.thumbnail((120, 120))
                    ph = ImageTk.PhotoImage(im)
                except Exception:
                    continue
                lbl = tk.Label(imgs, image=ph, bg=light, cursor="hand2")
                lbl.image = ph
                self._modal_thumbs.append(ph)
                lbl.pack(side="left", padx=4)
                lbl.bind("<Button-1>", lambda e, p=fp: self._open_image(p))

        # ── 动作按钮（固定在窗口底部，不跟着正文滚 —— 见 bar_wrap 的说明）──
        bar = tk.Frame(bar_wrap, bg=light)
        bar.pack(fill="x", pady=(0, 10))

        # 图标只用基础几何符号：对勾 / 星 / 叉 / 左箭头在 Microsoft YaHei 里都有字形。
        # 🔴 同理别用彩色 emoji 当按钮字样 —— Win10 上可能根本画不出（当初 🫧 踩过）。
        def _act_btn(parent, label, color, cmd, *, disabled=False):
            b = RoundedButton(parent, label, cmd, kind="ghost", radius=11,
                              padx=16, pady=9, font=_font(10),
                              fg=self._c(color),
                              fill=self._c("bg"), outline=self._c("border"),
                              hover_fill=self._c("active"),
                              hover_outline=self._c(color),
                              hover_fg=self._c(color),
                              disabled_fg=self._dim(),
                              parent_bg=light)
            if disabled:
                b.configure(state="disabled")
            return b

        # 流程按钮：单按钮两态（完成 ⇄ 取消完成 / 整理 ⇄ 取消整理）+ 丢弃。
        # 🔴 口径与列表窗口完全一致（同一份 `_flow_plan`）—— 待办（待办）与
        #    思绪（灵感）共用，避免两处规则漂移。
        plan = _flow_plan(memo, st)

        def _apply(target):
            """动作按钮 = 「改状态」+「顺手把结论写进去」，照 web 版一次提交。"""
            note = None
            if note_box is not None:
                note = note_box.get("1.0", "end").strip()
            self._set_status([it["id"]], target, memo=memo, note=note)

        for label, color, target, disabled in plan:
            _act_btn(bar, label, color,
                     lambda t=target: _apply(t),
                     disabled=disabled).pack(side="left", padx=(0, 8))

        # 右端：辅助操作（不是流程的一部分，单独放右边）。
        # 🔴 这里**不放「关闭」** —— 窗口右上角自带 X，两个关闭入口是重复的
        #    （用户明确要求去掉）。想用键盘就按 Esc（见下面的绑定）。
        RoundedButton(bar, "永久删除", lambda: self._delete(it), kind="ghost",
                      radius=11, padx=14, pady=9, font=_font(10),
                      fg=self._c("danger"), hover_fill=self._c("active"),
                      parent_bg=light).pack(side="right")

        # ── 编辑态 ─────────────────────────────────────────────────
        editing = {"on": False}

        def set_edit_mode(on):
            """进 / 出编辑态：正文换成编辑框，其余区块收起，按钮字样跟着换。"""
            editing["on"] = on
            if on:
                editbox.delete("1.0", "end")
                editbox.insert("1.0", it["content"])
                body_box.pack_forget()      # 只读正文整盒收起（Text 是它的子控件）
                editbox.pack(fill="x")
                edit_hint.pack(anchor="w", pady=(6, 0))
                tag_wrap.grid_remove()
                if note_wrap is not None:
                    note_wrap.grid_remove()
                if imgs is not None:
                    imgs.grid_remove()
                if banner_lbl is not None:
                    banner_lbl.grid_remove()
                # 编辑态收起整条动作面板（bar_wrap 是 bar_cv 的子控件，
                # 直接 hide 父级 bar_cv 即可；bar_wrap 自己没有独立 pack 位）。
                bar_cv.pack_forget()
                # 编辑态：按钮从「编辑」变「保存」，同时换成品牌色强调
                edit_btn.set_text("保存")
                edit_btn.configure(fg=self._c("primary"),
                                   fill=self._c("active"),
                                   outline=self._c("primary"),
                                   hover_fill=self._c("active"),
                                   hover_outline=self._c("primary"),
                                   hover_fg=self._c("primary"))
            else:
                editbox.pack_forget()
                edit_hint.pack_forget()
                body_box.pack(anchor="nw")
                tag_wrap.grid()
                if note_wrap is not None:
                    note_wrap.grid()
                if imgs is not None:
                    imgs.grid()
                if banner_lbl is not None:
                    banner_lbl.grid()
                bar_cv.pack(side="bottom", fill="x", padx=PAD, pady=(0, PAD))
                edit_btn.set_text("编辑")
                edit_btn.configure(fg=self._c("secondary"),
                                   fill=self._c("bg"),
                                   outline=self._c("border"),
                                   hover_fill=self._c("active"),
                                   hover_outline=self._c("primary"),
                                   hover_fg=self._c("primary"))
            win.after(20, _fit_card)

        def _toggle_edit():
            """同一个按钮两副面孔：不在编辑态就是「编辑」，在编辑态就是「保存」。"""
            if editing["on"]:
                new = editbox.get("1.0", "end").strip()
                if new and new != it["content"]:
                    _store.update_content(it["id"], new)
                    it["content"] = new
                    txt._orig_text = new
                    # 🔴 立刻按当前宽度重折：`_fit_card` 有缓存键，改完字如果卡片
                    #    尺寸没变（比如只改了个错别字），它会直接 return，正文就
                    #    还是旧的。
                    _body_fit(new, _fit.get("wl", 520))
                    self.refresh(self.app.state)
                    self.app.toast("已保存", True)
                set_edit_mode(False)
            else:
                set_edit_mode(True)
                editbox.focus_set()
                editbox.mark_set("insert", "end")

        edit_btn = RoundedButton(meta_row, "编辑", _toggle_edit, kind="ghost",
                                 radius=8, padx=10, pady=4,
                                 font=_font(9),
                                 fg=self._c("secondary"),
                                 fill=self._c("bg"),
                                 outline=self._c("border"),
                                 hover_fill=self._c("active"),
                                 hover_outline=self._c("primary"),
                                 hover_fg=self._c("primary"),
                                 parent_bg=light)
        edit_btn.pack(side="right")

        def _on_escape(_e=None):
            """Esc：编辑态先退回只读（照 web 版语义），否则关掉浮层。

            底部那个「关闭」按钮去掉之后，Esc 就是不开鼠标也能关窗的入口。
            """
            if editing["on"]:
                set_edit_mode(False)
            else:
                win.destroy()

        win.bind("<Escape>", _on_escape)
        # 点卡片外的空白也关（照 web 版：点外面 = 关闭；点里面不关）
        cv.bind("<Button-1>", lambda e: win.destroy()
                if e.widget is cv else None)

        # 内容挂进 canvas，**再**在底下垫一层圆角矩形当卡片底（Tk 控件画不出圆角）。
        # 🔴 不能反过来「先量 reqwidth 再画」：那时控件还没 map，量到的是初始值，
        #    画出的框跟内容对不上。
        # 🔴 内缩 ≥ r·(1−1/√2)：card 是直角 Frame，贴着圆角矩形的话四个角会戳到
        #    圆弧外面露出小方块（和气泡那个是同一个毛病）。这里 r=16 → 至少 4.7px。
        cv.create_window(CARD_INSET, CARD_INSET, window=card, anchor="nw")

        _fit = {"w": 0, "h": 0, "ch": 0}
        _floor = {"h": 0}      # 窗口高度下限（只读态量到的），见 _fit_card
        _wfont = _wrap_font(11)     # 正文的字体度量（量宽 / 行高都靠它）

        def _body_fit(orig, avail):
            """按可用像素宽重折正文 → 定 `body_box` 的尺寸 + Text 的显示行数。

            🔴 必须**按新宽度重折**（`_soft_wrap`）—— 只改控件宽度的话，文字
               仍是按 520 折好的，Tk 不会重排既有的 `\\n`，右边会留一大截空白
               （用户报的「思绪卡片右边较大空隙」）。
            🔴 Text 的 `width` / `height` 是字符 / 行单位、量不出像素 ⇒ 量宽交给
               外层 `body_box`（固定像素 + `pack_propagate(False)`），Text 的
               `height` 只负责「显示几行」。这样卡片宽度仍然跟着正文走。
            🔴 写 Text 前要临时 `state="normal"`（disabled 下 `delete` / `insert`
               会被忽略），写完立刻切回 —— 只读态才是用户看到的那一态。
            """
            new = _soft_wrap(orig, avail, 11)
            if new != txt.get("1.0", "end-1c"):
                txt.configure(state="normal")
                txt.delete("1.0", "end")
                txt.insert("1.0", new)
                txt.configure(state="disabled")
            lines = new.split("\n") if new else [""]
            w_max = max((_wfont.measure(l) for l in lines), default=1)
            body_box.configure(width=max(1, w_max),
                               height=_wfont.metrics("linespace") * len(lines))
            txt.configure(height=len(lines))

        def _fit_card(_e=None):
            """卡片尺寸变了就重算：宽度跟着内容走，**高度封顶**、超出交给滚动。

            🔴 高度必须封顶 —— 否则长记录会把窗口顶穿屏幕（见 bar_wrap 的说明）。
            🔴 宽度要**同时看正文和动作条**（用户 2026-10-12：短文本按钮展示不全）——
               正文短时 card 很窄，但「✓ 已整理」这类按钮比它宽，卡片会被
               按钮顶宽、圆角和内容对不上。取两者的较大值，谁宽谁说话。
            """
            # 编辑态整条动作面板被收起（bar_cv 已 forget），对它不占高度。
            bar_h = 0 if editing["on"] else bar_wrap.winfo_reqheight()
            bar_w = 0 if editing["on"] else bar_wrap.winfo_reqwidth()
            cwc = max(card.winfo_reqwidth(), bar_w) + CARD_INSET * 2
            chc = card.winfo_reqheight() + CARD_INSET * 2
            # 底部面板比内容多留 BAR_R：那截是圆角收尾区（画的是 barbg 自己，
            # 没内容），这样 bar_wrap 的直角底边落在圆角弧线以上，不会戳出卡外。
            bar_total = bar_h + BAR_R
            avail = max(240, int(win.winfo_screenheight() * 0.82)
                        - bar_total - PAD * 2 - 16)
            cvh = min(chc, avail)
            # 🔴 窗口尺寸下限 —— 用户报的两条都靠它：
            #    · 宽度：card 是直角 Frame，窗口拖得比它窄时 canvas 直接被裁掉，
            #      拖到极窄就「内容全没了」。下限 = 卡片宽 + 两侧留白。
            #    · 高度：进编辑态会 grid_remove 掉标签 / 结论 / 附件，卡片变矮、
            #      窗口跟着缩一截，看着像换了个窗口。所以下限取「只读态量到的高度」，
            #      **只抬不降**。
            if not editing["on"]:
                _floor["h"] = max(_floor["h"], cvh + bar_total + PAD * 2 + 4)
            try:
                win.minsize(cwc + PAD * 2,
                            min(_floor["h"],
                                int(win.winfo_screenheight() * 0.92)))
            except tk.TclError:
                pass
            # 🔴 缓存键带上 bar_total：编辑态切换时 cwc/cvh/chc 可能都没变，
            #    但 bar 占不占高度变了，不带上会跳过重算 → 动作条不回来。
            if (_fit["w"], _fit["h"], _fit["ch"], _fit.get("bar", -1)) \
                    == (cwc, cvh, chc, bar_total):
                return
            _fit.update(w=cwc, h=cvh, ch=chc, bar=bar_total)
            cv.configure(width=cwc, height=cvh,
                         scrollregion=(0, 0, cwc, chc))
            cv.delete("cardbg")
            # 上卡只圆顶边两角，底边直线 —— 跟下面 bar_cv 的圆底边拼成一张卡。
            round_rect_top(cv, 1, 1, cwc - 1, chc - 1, 16,
                           fill=self._c("light"), outline=self._c("border"),
                           width=1, tags="cardbg")
            cv.tag_lower("cardbg")
            # 底部动作面板：画下圆角底，宽度与上面卡片同宽（左右齐平）。
            bar_cv.configure(width=cwc, height=bar_total)
            bar_cv.delete("barbg")
            round_rect_bottom(bar_cv, 1, 1, cwc - 1, bar_total - 1, BAR_R,
                              fill=self._c("light"), outline=self._c("border"),
                              width=1, tags="barbg")
            bar_cv.tag_lower("barbg")
            # bar_wrap 放进 bar_cv 的窗口：宽 = 卡宽 - 两侧内缩，高 = 内容高。
            bar_cv.itemconfigure(bar_win, width=cwc - 2 * CARD_INSET,
                                 height=bar_h)
            # 🔴 正文 wraplength 跟着卡宽走（不再是写死的 520）：减去卡片
            #    内缩 + 卡片自身左右 padding，文字正好铺满、贴到右边。
            # 🔴 还要**按新宽度重折**（`_soft_wrap`）—— 只改 wraplength 的话，
            #    文字仍是按 520 折好的，Tk 不会重排既有 `\n`，右边留一大截空白
            #    （用户报的「思绪卡片右边较大空隙」）。
            _wl = max(200, cwc - CARD_INSET * 2 - 44)
            _fit["wl"] = _wl        # 记下当前宽度：保存正文后按同一个宽度重折
            _body_fit(getattr(txt, "_orig_text", ""), _wl)
            cv.yview_moveto(0)          # 每次重算都回到顶部，别停在半路
            win.after_idle(_place_window)

        def _place_window():
            """摆到主窗口右下方，但**不许跑出屏幕**（变高了也不会顶出去）。"""
            try:
                win.update_idletasks()
                sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
                # 🔴 取「请求尺寸」和「实际尺寸」的较大者：`wm minsize` 把窗口
                #    撑大时 `winfo_reqwidth/height` 还是内容算出来的那个小值，
                #    只按它算会把窗口摆到屏幕外面去。
                w = max(win.winfo_reqwidth(), win.winfo_width())
                h = max(win.winfo_reqheight(), win.winfo_height())
                x = self.app.root.winfo_rootx() + 120
                y = self.app.root.winfo_rooty() + 90
                x = max(8, min(x, sw - w - 16))
                y = max(8, min(y, sh - h - 48))       # 48 = 给任务栏留位置
                win.geometry("+%d+%d" % (x, y))
            except tk.TclError:
                pass

        def _on_wheel(e):
            """滚轮滚正文区。

            🔴 绑在 **Toplevel** 上就够了 —— Tk 的 bindtags 链是
               `控件 → 控件类 → 顶层窗口 → all`，子控件上的滚轮事件会走到这里，
               不用给每个 Label 单独绑（也不该用 `bind_all`，那会跟配置中心的
               滚轮抢）。编辑态的 Text 自己会 `break`，两边互不打扰。
            """
            if _fit["ch"] > cv.winfo_height():
                cv.yview_scroll(-1 if e.delta > 0 else 1, "units")

        card.bind("<Configure>", _fit_card)
        win.bind("<MouseWheel>", _on_wheel)
        # 🔴 只读 Text 会把滚轮事件吃掉（Tk 给 Text 类绑了滚轮处理），鼠标停在
        #    正文上时整张卡就滚不动了。这里显式接管、并 `break` 掉类绑定。
        def _wheel_on_body(e):
            _on_wheel(e)
            return "break"
        txt.bind("<MouseWheel>", _wheel_on_body)
        win.after(20, _fit_card)

    def _set_status(self, ids, status, memo, note=None):
        """改状态。灵感可以**顺手把结论一起写进去**（note=None 表示不动结论）。

        🔴 传 `note=""` 是「清空结论」而不是「不改」—— 沿用 web 版的语义：
           浮层里那个框显示什么就提交什么，清空是用户的主动操作。
        """
        if memo:
            _store.set_memo_status(ids, status)
        else:
            _store.set_idea_status(ids, status, note)
        # 🔴 这里**不弹通知**：卡片从轮播里消失、浮层关掉，本身就是反馈；
        #    每处理一条右下角跳一个「已更新 N 条」纯属噪音（用户明确要去掉）。
        if self._modal is not None and self._modal.winfo_exists():
            self._modal.destroy()
        self.refresh(self.app.state)
        self._refresh_open_list()

    def _delete(self, c):
        if not messagebox.askyesno("永久删除", "这条会永远消失，确定？"):
            return
        n = _store.delete_items([c["id"]])
        self.app.toast(f"已删除 {n} 条", bool(n))
        if self._modal is not None and self._modal.winfo_exists():
            self._modal.destroy()
        self.refresh(self.app.state)
        self._refresh_open_list()

    def _refresh_open_list(self):
        """列表窗口还开着的话就地重建 —— 卡片里改了状态 / 删了条目后保持一致。

        🔴 从列表点开卡片**不再关列表**（用户 2026-10-12），所以卡片里的改动
           必须回灌到列表，否则列表还停在旧数据上（点「取消完成」看着像没反应）。
        """
        if self._list is not None and self._list.winfo_exists() \
                and self._list_rebuild is not None:
            try:
                self._list_rebuild()
            except tk.TclError:
                pass

    # ── 回收站（已丢弃）────────────────────────────────────────────
    def _dropped_items(self):
        """所有**已丢弃**的条目（待办 + 灵感），最新的在前。

        🔴 这里**不看 `bubbles_show`** —— 那个设置管的是「轮播舞台上展示哪几类」，
           回收站是清理入口，被展示范围挡住就没人清得掉垃圾了。
        """
        rows = []
        for r in (_store.list_memos(500, kind=_store.KIND_MEMO)
                  + _store.list_ideas(None, 500)):
            if _bucket_of(r) != "dropped":
                continue
            content = _clean_text(r.get("content"))
            if not content:
                continue
            rows.append({
                "id": r["id"],
                "kind": r.get("kind") or _store.KIND_MEMO,
                "status": r.get("status") or _store.STATUS_OPEN,
                "bucket": "dropped",
                "content": content,
                "date": _fmt_time(r.get("created_at")),
                "tags": [t["name"] if isinstance(t, dict) else str(t)
                         for t in (r.get("tags") or [])],
                "images": r.get("images") or [],
                "note": r.get("note") or "",
            })
        rows.sort(key=lambda x: x["date"], reverse=True)
        return rows

    def _scroll_area(self, parent, pad):
        """可滚列表骨架：返回 `(cv, inner)`。顶部标题 / 底部动作条由调用方自己摆。

        回收站和分类 / 搜索结果列表**共用这一份**（两处的行为差别只在行内容和
        底部动作条，骨架一模一样，没必要各写一遍）。

        🔴 列表区**必须能滚**：几百条记录直接把窗口撑出屏幕的话，底部那排按钮
           就点不到了（回收站踩过）。
        🔴 滚轮绑 **Toplevel**（bindtags 会从子控件冒上来），别用 `bind_all` ——
           那会跟配置中心的滚轮抢。
        🔴 **视口钳制**（用户 2026-10-12：列表「上滚空白、只显示一条」）：
          内容比视口矮时，滚动区钳成视口本身 —— 否则 bbox 比视口高出一截，
          滚轮能滚进一片啥都没有的空白，剩下的真内容被挤成「只显示一条」。
          bbox 取不到（空 canvas）时不动滚动区，别把 None 传给 configure。
        🔴 **滚轮双闸**：方向上已经没得滚了就 `break` 拦下来（yview 到头还继续
          调 `yview_scroll` 会让位置漂），并且消费掉事件、不往父窗口窜。
        """
        body_wrap = tk.Frame(parent, bg=self._c("bg"))
        body_wrap.pack(side="top", fill="both", expand=True, padx=pad)
        cv = tk.Canvas(body_wrap, highlightthickness=0, bd=0, bg=self._c("bg"))
        sb = ttk.Scrollbar(body_wrap, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        cv.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(cv, bg=self._c("bg"))
        wid = cv.create_window(0, 0, window=inner, anchor="nw")

        def _sync(_e=None):
            """内容 / 视口任一变化 → 重算条目宽 + 滚动区（钳到视口）。"""
            cv.itemconfigure(wid, width=cv.winfo_width())
            bb = cv.bbox("all")
            if not bb:
                return
            top, bot = bb[1], bb[3]
            vh = cv.winfo_height()
            if bot - top <= vh:          # 内容不比视口高 → 不留可滚空白
                cv.configure(scrollregion=(bb[0], 0, bb[2], max(bot, vh)))
            else:
                cv.configure(scrollregion=bb)

        def _wheel(e):
            """滚轮双闸：没得滚的方向直接拦 + break，不往父窗口窜。"""
            top, bot = cv.yview()
            if e.delta > 0 and top <= 0.0:
                return "break"
            if e.delta < 0 and bot >= 1.0:
                return "break"
            cv.yview_scroll(-1 if e.delta > 0 else 1, "units")
            return "break"

        inner.bind("<Configure>", _sync)
        cv.bind("<Configure>", _sync)
        parent.bind("<MouseWheel>", _wheel)
        return cv, inner

    # ── 搜索 / 分类列表 ──────────────────────────────────────────
    def _search(self):
        """底部搜索框回车 / 点「搜一下」→ 开一个结果列表窗口。

        🔴 结果**不进轮播舞台**：轮播是「等它飘过来」，搜索是「直接去找」。
           两条路并存 —— 这正是用户要的（「灵感多起来等轮播根本找不到」）。
        """
        q = self._q_var.get().strip()
        if not q:
            self.app.toast("先在搜索框里写点什么再搜", False)
            return
        self._open_list(query=q)

    def _open_list(self, group=None, bucket=None, query=None):
        """分类 / 搜索结果的**静态列表窗口**。

        🔴 为什么要有它：轮播是「等条目飘过来」，条目一多就只能干等。列表窗口是
           「直接翻」—— 时间倒序、能滚、点一行就开那张思绪卡片。
        🔴 `group`（待办 / 思绪）打开后，顶上还有一行**子筛选**（未完成/已完成、
           未整理/已整理）—— 用户 2026-10-12：「打开后再按…分类，列表就清楚很多」。
        🔴 和回收站共用 `_scroll_area` 那套骨架（顶部标题 / 中间可滚列表），
           但**不做勾选**：这里没有批量操作，点一行 = 打开卡片。
        🔴 同样**不放「关闭」**（窗口 X + Esc 就够，和浮层 / 回收站一条规矩）。
        🔴 数据侧走 `_list_items`，它**不看 `bubbles_show`** —— 用户找东西时
           不知道也不关心某类有没有被展示范围关掉。
        """
        _store.init()
        if group is not None:
            title = f"{_GROUP_LABEL[group]} · 列表"
            sub = "按时间倒序 · 点一行打开思绪卡片"
        elif query:
            title = f"搜索「{query}」"
            sub = "命中正文 / 标签 / 结论 · 点一行打开思绪卡片"
        else:
            title = "列表"
            sub = "按时间倒序 · 点一行打开思绪卡片"

        # 🔴 同一个入口重复点 → **把已开的窗置顶**，不重开一个（用户 2026-10-12：
        #    「打开待办页面后再点待办，不应该重新弹出，而是把当前待办窗口置顶」）。
        #    换了入口（待办 → 思绪 / 另一次搜索）才重开。
        key = (("group", group) if group is not None
               else ("query", query) if query else ("bucket", bucket))
        if self._list is not None and self._list.winfo_exists():
            if self._list_key == key:
                try:
                    self._list.deiconify()
                    self._list.lift()
                    self._list.focus_force()
                except tk.TclError:
                    pass
                return
            self._list.destroy()
        win = tk.Toplevel(self)
        self._list = win
        self._list_key = key
        win.title(title)
        win.configure(bg=self._c("bg"))
        win.transient(self.app.root)
        W = 680
        H = max(320, min(560, int(win.winfo_screenheight() * 0.72)))
        # 🔴 位置必须过屏幕夹取：主窗贴右下角时 +150/+110 会把列表窗整个推出
        #    屏幕，用户眼里只剩一条边（反馈单「只显示一条」）。
        _x, _y = _clamp_win(win, self.app.root.winfo_rootx() + 150,
                            self.app.root.winfo_rooty() + 110, W, H)
        win.geometry("%dx%d+%d+%d" % (W, H, _x, _y))
        win.minsize(520, 300)

        PAD = 12
        light = self._c("light")
        # 🔴 Frame 的 `pady` 是**单个距离**，不是 pack/grid 那种 (上, 下) 元组 ——
        #    传元组会 `TclError: bad screen distance "10 4"`。
        head = tk.Frame(win, bg=self._c("bg"), padx=PAD, pady=10)
        head.pack(side="top", fill="x", pady=(0, 4))
        tk.Label(head, text=title, bg=self._c("bg"), fg=self._c("fg"),
                 font=_font(12, "bold")).pack(side="left")
        # 条数用 StringVar —— 点状态按钮重建列表时它要跟着变（见 _rebuild）。
        count_var = tk.StringVar()
        tk.Label(head, textvariable=count_var, bg=self._c("bg"),
                 fg=self._c("secondary"),
                 font=_font(9)).pack(side="left", padx=(10, 0))

        # 子筛选行：大类（待办/思绪）打开时才有。[全部] + 该大类的两个子类。
        # 🔴 sub_state 无条件定义 —— `_rebuild` 在搜索 / 兜底路径也要读它。
        sub_state = {"v": None, "btns": []}
        if group is not None:
            # thought 组里 done/decided 展示同为「已整理」，子筛只留一个入口
            if group == "thought":
                subs = [("", "全部"), ("open", "未整理"), ("done", "已整理")]
            else:
                subs = [("", "全部"), ("memo", "未完成"), ("memo_done", "已完成")]
            srow = tk.Frame(win, bg=self._c("bg"), padx=PAD)
            srow.pack(side="top", fill="x", pady=(0, 4))

            def _pick(v):
                """点子筛：换筛选项 + 把「选中」框搬到点下的那个上。"""
                sub_state["v"] = v or None
                for code, sb in sub_state["btns"]:
                    sb.set_selected((code or None) == sub_state["v"])
                _rebuild()

            for code, text in subs:
                sb = RoundedButton(srow, text, lambda c=code: _pick(c),
                                   kind="ghost", radius=9, padx=10, pady=4,
                                   font=_font(9),
                                   fg=self._c("secondary"),
                                   fill=self._c("bg"), outline=self._c("border"),
                                   hover_fill=self._c("active"),
                                   hover_outline=self._c("primary"),
                                   hover_fg=self._c("primary"),
                                   selected_fg=self._c("primary"),
                                   selected_fill=lerp_color(self._c("primary"), self._c("bg"), 0.86),
                                   parent_bg=self._c("bg"))
                sb.pack(side="left", padx=(0, 4))
                sb.set_selected(not code)
                sub_state["btns"].append((code, sb))

        cv, inner = self._scroll_area(win, PAD)

        def _close():
            self._list = None
            self._list_key = None
            self._list_rebuild = None
            try:
                win.destroy()
            except tk.TclError:
                pass

        def _open(it):
            """开卡片 —— **列表留着**（用户 2026-10-12：在列表里打开卡片不该关列表）。

            卡片改完状态后靠 `self._list_rebuild` 把列表刷新，所以关不关都不影响
            数据一致性；留着反而能接着翻下一条。
            """
            self._open_modal(it)

        def _row(it):
            memo = it["kind"] == _store.KIND_MEMO
            st = it["status"]
            row = tk.Frame(inner, bg=light, padx=12, pady=9)
            row.pack(fill="x", pady=3)

            # 左：文字（点开卡片）
            left = tk.Frame(row, bg=light)
            left.pack(side="left", fill="both", expand=True)
            accent = _BUCKET_COLOR[it["bucket"]][
                1 if self.app.theme == THEME_DARK else 0]
            meta = tk.Label(left, text=_meta_text(it, memo), bg=light, fg=accent,
                            font=_font(9, "bold"))
            meta.pack(anchor="w")
            summary = " ".join(it["content"].split())
            if len(summary) > 140:
                summary = summary[:140] + "…"
            body = tk.Label(left, text=_soft_wrap(summary, 440, 10), bg=light,
                            fg=self._c("fg"),
                            justify="left", anchor="w", wraplength=440,
                            font=_font(10))
            body.pack(anchor="w", pady=(3, 0))
            note_lbl = None
            if it.get("note"):
                n = " ".join(it["note"].split())
                if len(n) > 120:
                    n = n[:120] + "…"
                note_lbl = tk.Label(left, text=_soft_wrap("结论：" + n, 440, 9),
                                    bg=light,
                                    fg=self._c("secondary"), justify="left",
                                    anchor="w", wraplength=440, font=_font(9))
                note_lbl.pack(anchor="w", pady=(3, 0))
            for w in [left, meta, body] + ([note_lbl] if note_lbl else []):
                w.bind("<Button-1>", lambda e, x=it: _open(x))
                try:
                    w.configure(cursor="hand2")
                except tk.TclError:
                    pass

            # 右：状态操作按钮（待办：完成 ⇄ 取消完成 + 丢弃；
            #     思绪：整理 ⇄ 取消整理 + 丢弃）。同一套口径见 `_flow_plan`。
            right = tk.Frame(row, bg=light)
            right.pack(side="right", fill="y", padx=(10, 0))
            plan = _flow_plan(memo, st)

            def _act(target, iid=it["id"], mm=memo):
                self._set_status([iid], target, memo=mm)
                # 改完状态后重建本列表：当前站按钮变灰、或条目移出本桶而消失。
                _rebuild()

            for label, color, target, disabled in plan:
                sb = RoundedButton(right, label, lambda t=target: _act(t),
                                   kind="ghost", radius=9, padx=10, pady=4,
                                   font=_font(9), fg=self._c(color),
                                   fill=self._c("bg"), outline=self._c("border"),
                                   hover_fill=self._c("active"),
                                   hover_outline=self._c(color),
                                   hover_fg=self._c(color),
                                   disabled_fg=self._dim(),
                                   parent_bg=light)
                if disabled:
                    sb.configure(state="disabled")
                sb.pack(anchor="e", pady=(0, 3))

        # 🔴 分页：一次取一页（LIST_PAGE 条），底部给「加载更多」。
        #    记录再多也翻得到最早那条（审查报告点出的「500 条隐藏旧数据」）。
        page = {"n": self.LIST_PAGE}

        def _rebuild():
            """重画列表 —— 筛选项 / 状态变了就回到第一页、并滚回顶部。"""
            page["n"] = self.LIST_PAGE
            _render(at_top=True)

        def _render(at_top=False):
            for w in inner.winfo_children():
                w.destroy()
            rows = self._list_items(group=group, sub=sub_state["v"],
                                    bucket=bucket, query=query,
                                    limit=page["n"])
            for it in rows:
                _row(it)
            total = self._list_total(group=group, sub=sub_state["v"],
                                     bucket=bucket, query=query)
            shown = len(rows)
            more = max(0, total - shown)
            tail = f" · 还有 {more} 条" if more else ""
            count_var.set(f"共 {total} 条 · 显示 {shown}{tail} · {sub}")
            if not inner.winfo_children():
                tk.Label(inner, text="这里没有条目", bg=self._c("bg"),
                         fg=self._c("secondary"),
                         font=_font(10)).pack(pady=48)
            if more:
                def _more():
                    page["n"] += self.LIST_PAGE
                    _render(at_top=False)      # 🔴 「加载更多」别把视图弹回顶部
                self._ghost_btn(inner, f"加载更多（还有 {more} 条）", _more,
                                padx=14, pady=6).pack(pady=12)
            if at_top:
                cv.yview_moveto(0)

        # 登记重建回调：卡片里改了状态后 `_set_status` 会调它把本列表刷新
        # （列表不再随着开卡片而关闭，见 `_open`）。
        self._list_rebuild = _rebuild
        _rebuild()

        win.protocol("WM_DELETE_WINDOW", _close)
        win.bind("<Escape>", lambda e: _close())

    def _open_trash(self):
        """回收站：已丢弃条目的**静态列表**，勾出要永久删除的。

        🔴 刻意**不做成轮播**（用户明确要求「不用以泳道形式轮播」）：轮播是
           「过一遍、顺手处理」，回收站是「回头清理」，两种心智不该混在一个界面里。
           所以已丢弃既不进轮播舞台、也没有筛选 chip，只有这一个入口。

        🔴 和浮层一样，列表区**必须能滚**：几百条丢弃记录直接把窗口撑出屏幕的话，
           底部的「永久删除选中」就点不到了。
        """
        if self._trash is not None and self._trash.winfo_exists():
            self._trash.lift()
            self._trash.focus_set()
            return
        _store.init()

        win = tk.Toplevel(self)
        self._trash = win
        win.title("回收站 · 已丢弃")
        win.configure(bg=self._c("bg"))
        win.transient(self.app.root)
        W = 680
        H = max(320, min(560, int(win.winfo_screenheight() * 0.72)))
        x, y = _clamp_win(win, self.app.root.winfo_rootx() + 150,
                          self.app.root.winfo_rooty() + 110, W, H)
        win.geometry("%dx%d+%d+%d" % (W, H, x, y))
        win.minsize(520, 300)

        PAD = 12
        light = self._c("light")
        sel = {}                      # id → BooleanVar
        self._trash_pick = sel
        count_var = tk.StringVar()

        # 顶部说明 + 底部动作条都**固定**，只有中间那块列表滚（同上）。
        # 🔴 Frame 的 `pady` 是**单个距离**，不是 pack/grid 那种 (上, 下) 元组 ——
        #    传元组会 `TclError: bad screen distance "10 4"`。上下间距用 pack 的 pady 调。
        head = tk.Frame(win, bg=self._c("bg"), padx=PAD, pady=10)
        head.pack(side="top", fill="x", pady=(0, 4))
        tk.Label(head, text="回收站", bg=self._c("bg"), fg=self._c("fg"),
                 font=_font(12, "bold")).pack(side="left")
        tk.Label(head, textvariable=count_var, bg=self._c("bg"),
                 fg=self._c("secondary"), font=_font(9)).pack(side="left",
                                                             padx=(10, 0))
        foot = tk.Frame(win, bg=self._c("bg"), padx=PAD, pady=10)
        foot.pack(side="bottom", fill="x")
        cv, inner = self._scroll_area(win, PAD)

        def _refresh_count():
            n = sum(1 for v in sel.values() if v.get())
            count_var.set(f"共 {len(sel)} 条 · 已选 {n} 条")
            del_btn.set_text(f"永久删除选中（{n}）")
            del_btn.configure(state="normal" if n else "disabled")

        def _toggle_all(on):
            for v in sel.values():
                v.set(on)
            _refresh_count()

        def _purge(ids):
            """真删。**不可逆** → 必须二次确认，且把条数写在提示里。"""
            if not ids:
                return
            msg = ("这条会永远消失，确定？" if len(ids) == 1
                   else f"这 {len(ids)} 条会永远消失，确定？")
            if not messagebox.askyesno("永久删除", msg, parent=win):
                return
            got = _store.delete_items(ids)
            self.app.toast(f"已删除 {got} 条", bool(got))
            _rebuild()
            self.refresh(self.app.state)

        def _row(it):
            memo = it["kind"] == _store.KIND_MEMO
            row = tk.Frame(inner, bg=light, padx=12, pady=9)
            row.pack(fill="x", pady=3)
            var = tk.BooleanVar(value=False)
            sel[it["id"]] = var
            # 🔴 这里要的是**勾选**，不是开关。`*-round-toggle` 画出来是 iOS 那种
            #    拨杆，拿它做多选没人看得懂（用户原话「哪有用这个开关做选择的」）。
            #    `bootstyle="primary"` 走的是 checkbutton 那套资源 —— 方框 + 对勾。
            ttk.Checkbutton(row, variable=var,
                            bootstyle="primary",
                            command=_refresh_count).pack(side="left", anchor="n")
            # pack 顺序：左右两侧先各占一边，中间那块再吃掉剩下的宽度
            RoundedButton(row, "删除", lambda i=it["id"]: _purge([i]),
                          kind="ghost", radius=9, padx=10, pady=4,
                          font=_font(9), fg=self._c("danger"),
                          hover_fill=self._c("active"),
                          parent_bg=light).pack(side="right", anchor="n",
                                                padx=(8, 0))
            mid = tk.Frame(row, bg=light)
            mid.pack(side="left", fill="x", expand=True, padx=(8, 0))
            accent = _BUCKET_COLOR["dropped"][
                1 if self.app.theme == THEME_DARK else 0]
            tk.Label(mid, text=_meta_text(it, memo), bg=light, fg=accent,
                     font=_font(9, "bold")).pack(anchor="w")
            summary = " ".join(it["content"].split())
            if len(summary) > 110:
                summary = summary[:110] + "…"
            tk.Label(mid, text=_soft_wrap(summary, 440, 10), bg=light,
                     fg=self._c("fg"),
                     justify="left", anchor="w", wraplength=440,
                     font=_font(10)).pack(anchor="w", pady=(3, 0))

        def _rebuild():
            for w in inner.winfo_children():
                w.destroy()
            sel.clear()
            data = self._dropped_items()
            if not data:
                tk.Label(inner,
                         text="回收站是空的 —— 在气泡上点「× 丢弃」的条目会出现在这里",
                         bg=self._c("bg"), fg=self._c("secondary"),
                         font=_font(10)).pack(pady=48)
            for it in data:
                _row(it)
            _refresh_count()
            cv.yview_moveto(0)

        RoundedButton(foot, "全选", lambda: _toggle_all(True), kind="ghost",
                      radius=10, padx=12, pady=5, font=_font(9),
                      fg=self._c("secondary"), fill=self._c("bg"),
                      outline=self._c("border"), hover_fill=self._c("active"),
                      hover_outline=self._c("primary"),
                      hover_fg=self._c("primary"),
                      parent_bg=self._c("bg")).pack(side="left")
        RoundedButton(foot, "清空选择", lambda: _toggle_all(False), kind="ghost",
                      radius=10, padx=12, pady=5, font=_font(9),
                      fg=self._c("secondary"), fill=self._c("bg"),
                      outline=self._c("border"), hover_fill=self._c("active"),
                      hover_outline=self._c("primary"),
                      hover_fg=self._c("primary"),
                      parent_bg=self._c("bg")).pack(side="left", padx=(6, 0))
        del_btn = RoundedButton(foot, "永久删除选中（0）",
                                lambda: _purge([i for i, v in sel.items() if v.get()]),
                                kind="ghost", radius=10, padx=14, pady=5,
                                font=_font(9, "bold"),
                                fg=self._c("danger"), fill=self._c("bg"),
                                outline=self._c("border"),
                                hover_fill=self._c("active"),
                                hover_outline=self._c("danger"),
                                hover_fg=self._c("danger"),
                                disabled_fg=self._dim(),
                                parent_bg=self._c("bg"))
        del_btn.pack(side="left", padx=(14, 0))
        # 🔴 这里**不放「关闭」** —— 窗口右上角自带 X，Esc 也关得掉，两个入口重复
        #    （和浮层同一条规矩，用户明确要求过）。

        def _close():
            self._trash = None
            self._trash_pick = {}
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", _close)
        win.bind("<Escape>", lambda e: _close())
        _rebuild()

    def _resolve(self, rel):
        """把 `images/<yyyyMM>/<name>` 安全地解析成本机绝对路径（防穿越）。"""
        parts = rel.replace("\\", "/").split("/")
        if len(parts) != 3 or parts[0] != "images":
            return ""
        try:
            return _media.resolve_path(parts[1], parts[2])
        except Exception:
            return ""

    def _open_image(self, fp):
        try:
            os.startfile(fp)
        except Exception:
            pass

    # ── 列表行状态按钮 ─────────────────────────────────────────────
    # 见 `_open_list` / `_open_trash`：每行右侧直接摆「已处理/已决议/丢弃/恢复」
    # 这类状态操作按钮，点一下即改状态并重建本列表 —— 不再提供「打开/复制/打开
    # 文件夹」那套外部处理按钮（用户明确要求去掉；要转外部看就点开思绪卡片）。

def _gui_pid():
    """`gui.pid` 里记的进程号（0 = 没记）。"""
    try:
        with open(_GUI_PID_FILE, encoding="utf-8") as f:
            return int((f.read() or "0").strip())
    except (OSError, ValueError):
        return 0


def _gui_running():
    pid = _gui_pid()
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True,
                                 encoding="utf-8", errors="replace",
                                 timeout=10,
                                 creationflags=_botctl._no_window_flags()).stdout or ""
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def _raise_existing_window():
    """把已经开着的「摸鱼bot」窗口顶到前台（Win32 FindWindow，找不到就算了）。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        hwnd = ctypes.windll.user32.FindWindowW(None, "摸鱼bot")
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 9)      # SW_RESTORE
            ctypes.windll.user32.SetForegroundWindow(hwnd)
    except Exception:  # noqa: BLE001
        pass


def _install_crash_log():
    """把未捕获异常写进 `gui-error.log`（数据目录）。

    窗口是无控制台的（pythonw 启动），异常默认**无声无息**地吞掉 ——
    界面直接消失、用户完全不知道出了什么事。这里落一份带 traceback 的日志，
    下次出问题至少有的查。
    """
    import traceback

    def _hook(exc_type, exc, tb):
        try:
            with open(data_path("gui-error.log"), "a", encoding="utf-8") as f:
                f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
                traceback.print_exception(exc_type, exc, tb, file=f)
        except OSError:
            pass
        try:
            messagebox.showerror("摸鱼bot 出错了",
                                 f"{exc_type.__name__}: {exc}\n\n"
                                 f"详情见 gui-error.log")
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = _hook


def main():
    _install_crash_log()
    # 单实例：已经开着就把旧窗口顶到前面，不再开第二个（多窗口会各改一份配置）。
    if _gui_running():
        _raise_existing_window()
        return 0
    try:
        with open(_GUI_PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    try:
        App().root.mainloop()
    finally:
        if _gui_pid() == os.getpid():
            try:
                os.remove(_GUI_PID_FILE)
            except OSError:
                pass


if __name__ == "__main__":
    main()
