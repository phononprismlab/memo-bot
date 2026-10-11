"""摸鱼bot 本地应用界面（原生 Tkinter / ttkbootstrap）。

干嘛的：
  把原来「配置中心网页（config-web.py 起的 127.0.0.1 服务）」和「脑内抽屉」搬进一个
  本地桌面窗口。跑起来后不用再开浏览器访问 webui —— 配置、看抽屉都在这个窗口里完成。
  本文件只负责「界面 + 把用户操作翻译成对 bot 代码的调用」，所有真正的读写都复用
  config-web.py / settings.py / push.py / ignore.py / moyu.py / botctl 里的纯逻辑函数，
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

# 把脚本所在目录钉进 sys.path / 工作目录，本地模块（store / media / config-web）才能稳定导入。
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
from paths import data_path  # noqa: E402  （gui.pid 单实例锁落数据目录）

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


def load_config_web():
    """把 config-web.py（文件名带连字符，不能直接 import）当模块加载并返回。

    只认它的纯逻辑函数（full_state / write_env / bot_start …），HTTP 服务只在
    `if __name__ == "__main__"` 里起，所以这里 import 不会拉起任何服务。
    """
    spec = importlib.util.spec_from_file_location(
        "config_web", os.path.join(HERE, "config-web.py"))
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
        dark = detect_system_dark()
        self.theme = THEME_DARK if dark else THEME_LIGHT
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

    def _flat_styles(self):
        """极简白基调的全局控件样式：无边框、弱化矩形感、留白呼吸。"""
        s = self.style
        c = s.colors
        # 两个主入口 tab：加大字号、无边框、选中用品牌色。
        # 🔴 TNotebook 自己的背景必须也换成主题 bg —— 否则残留内置主题（flatly）的
        #    白色，在 tab 行和内容边缘露出一圈「白边」。
        s.configure("TNotebook", border_width=0, tabmargins=(0, 0, 0, 0),
                    background=c.bg, bordercolor=c.bg,
                    lightcolor=c.bg, darkcolor=c.bg)
        s.configure("TNotebook.Tab", padding=(22, 10),
                    font=_font(12), borderwidth=0,
                    background=c.bg, bordercolor=c.bg, lightcolor=c.bg, darkcolor=c.bg)
        s.map("TNotebook.Tab",
              background=[("selected", c.bg), ("active", c.active)],
              foreground=[("selected", c.primary)])
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
        # 切到脑内抽屉时把焦点给舞台 —— 空格暂停/继续才能生效
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
           **机器人设置**（播报文案、展示范围、收图开关这些）。
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
        try:
            ttk.ToastNotification(
                title="保存结果" if ok else "出错了",
                message=msg, duration=2500).show_toast()
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
                selected_fg=self._c("primary"), selected_fill=self._c("active"),
                parent_bg=self._c("bg"), fixed_width=174)
            b.pack(anchor="w", pady=2, fill="x")
            self._nav.append((b, g["key"]))
        if self.current is None:
            self.current = state["groups"][0]["key"]
        self.show_section(self.current)

    def _c(self, key):
        """当前主题色（ttkbootstrap Colors 的字段）。"""
        return getattr(self.app.style.colors, key)

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
        self.desc.configure(text=g["desc"])

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
                # hidden 字段（image_collect）由自绘区块画，但它必须留在字段表里，
                # 否则保存时 setDirty 认不出键会静默丢改动。
                self._render_self_drawn(f)
            else:
                self._render_one_field(f)

    def _render_one_field(self, f):
        target, key, kind = f["target"], f["key"], f["kind"]
        wrap = ttk.Frame(self.scroll, padding=(0, 4))
        wrap.pack(fill="x", pady=4)
        ttk.Label(wrap, text=f["label"], font=_font(10, "bold")).pack(anchor="w")
        if f.get("help"):
            ttk.Label(wrap, text=f["help"], bootstyle="secondary",
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
            var = tk.StringVar(value=str(f.get("value", "")))
            row = ttk.Frame(wrap)
            row.pack(fill="x")
            ttk.Entry(row, textvariable=var, width=64).pack(side="left", fill="x", expand=True)
            self._ghost_btn(row, "浏览",
                            lambda v=var: self._browse(v),
                            padx=10, pady=4).pack(side="left", padx=4)
            self._var_map[(target, key)] = var
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
        """image_collect 自绘区块：主开关（c2c/group）+ 超限报警阈值。"""
        key = f["key"]
        cur = as_set(f.get("value"))
        lf = ttk.LabelFrame(self.scroll, text=f["label"], padding=8)
        lf.pack(fill="x", pady=6)
        if f.get("help"):
            ttk.Label(lf, text=f["help"], bootstyle="secondary",
                      wraplength=720, font=_font(9)).pack(anchor="w")
        vars_ = {}
        for scope, label in (("c2c", "收私聊图片"), ("group", "收群图片")):
            v = tk.BooleanVar(value=scope in cur)
            ttk.Checkbutton(lf, text=label, variable=v,
                            bootstyle="primary-round-toggle").pack(anchor="w", pady=4)
            vars_[scope] = v
        self._multi[("settings", key)] = vars_

        alert = next((x for x in self.state["fields"] if x["key"] == "image_alert_gb"), None)
        if alert:
            av = tk.IntVar(value=int(alert.get("value") or 5))
            row = ttk.Frame(lf, padding=(0, 4))
            row.pack(fill="x")
            ttk.Label(row, text=alert["label"]).pack(side="left")
            ttk.Spinbox(row, from_=0, to=1024, textvariable=av, width=8).pack(side="left", padx=6)
            self._var_map[("settings", "image_alert_gb")] = av

    def _save_btn(self):
        self._solid_btn(self.scroll, "保存更改",
                        self._on_save).pack(anchor="e", pady=10)

    def _browse(self, var):
        import tkinter.filedialog as fd
        d = fd.askdirectory(initialdir=var.get() or os.path.expanduser("~"))
        if d:
            var.set(d.replace("\\", "/"))

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
        ok, msg = self.app.save(env_vals, set_vals)
        self.app.toast(msg, ok)

    # ---- ① 连接 QQ：凭据 ----
    def _render_bot(self):
        self._render_fields("bot")
        self._solid_btn(self.scroll, "保存凭据", self._on_save
                        ).pack(anchor="e", pady=(4, 10))

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
        av = tk.BooleanVar(value=bot.get("task") == "running")
        ttk.Checkbutton(lf2, text="开机自动启动机器人", variable=av,
                        bootstyle="primary-round-toggle",
                        command=lambda v=av: self._autostart(v.get())).pack(anchor="w", pady=4)

        lf3 = ttk.LabelFrame(self.scroll, text="依赖自检", padding=8)
        lf3.pack(fill="x", pady=6)
        for d in self.state.get("deps", []):
            mark = "✓" if d["ok"] else ("✗" if d.get("needed") else "·")
            col = "success" if d["ok"] else ("danger" if d.get("needed") else "secondary")
            ttk.Label(lf3, text=f"{mark} {d['name']}：{d['detail']}", bootstyle=col,
                      font=_font(9)).pack(anchor="w", pady=1)

        # ── 版本 / 检查更新 ────────────────────────────────────────
        # 🔴 桌面版一开始漏了这块（只在网页版有），用户找不到「检查更新」。
        #    逻辑复用 `version.py`（三源重试 + 人话报错）和网页版同一套，不另写。
        lf4 = ttk.LabelFrame(self.scroll, text="版本", padding=8)
        lf4.pack(fill="x", pady=6)
        ttk.Label(lf4, text="更新：点「下载新版」拿安装包（文件名带版本号），双击装上就行"
                            "—— 装到同一个目录，你的数据（备忘 / 灵感 / 配置）不受影响。",
                  bootstyle="secondary", wraplength=720,
                  font=_font(9)).pack(anchor="w")
        ver_row = ttk.Frame(lf4, padding=(0, 6))
        ver_row.pack(anchor="w", fill="x")
        # 🔴 这一页**会被重建**（顶栏后台查到计划任务状态后会 `show_section("runtime")`
        #    一次，好让「开机自启」勾选框跟着刷新）。所以检查更新的结果显示在
        #    `_upd_view` 里、每次重建回填 —— 否则用户刚点出来的「有新版」
        #    过一两秒就被冲回默认值（真实踩到过）。
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
        ok, msg = self.cw.autostart_set(bool(on))
        self.app.toast(msg, ok)
        self.app.refresh()

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
                ttk.Label(lf, text=c["help"], bootstyle="secondary",
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
        ttk.Label(head,
                  text="选一个 .py（单文件指令）或 .zip（指令包）。导入不会自动生效 ——"
                       " 导完到「⑦ 运行方式」点一下「重启」，指令才会注册进机器人。",
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
            ttk.Label(row,
                      text=("指令：" + cmds) if cmds else "没注册任何指令 —— 这个包装进来是死的",
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
                ttk.Label(row, text="⚠ 加载失败：" + it["error"], bootstyle="danger",
                          wraplength=700, font=_font(8)).pack(anchor="w")
            if it.get("dup"):
                ttk.Label(row, text="⚠ 指令名和别的扩展撞了：" + "、".join(it["dup"])
                                     + "（同名时按加载顺序，后面的盖住前面的）",
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
# 桶名 / 标签跟 web 版脑内抽屉（brain-drawer.py）对齐，配色用的是同一套状态色。
_BUCKET_LABEL = {
    "memo": "备忘",
    "memo_done": "备忘 · 已完成",
    "open": "待处理",
    "done": "已处理",
    "decided": "已决议",
    "dropped": "已丢弃",
}
_BUCKET_ORDER = ("memo", "memo_done", "open", "done", "decided", "dropped")


def _meta_text(it, memo):
    """气泡 / 浮层左上角那行「类型 · 状态 · 日期」。

    🔴 桶名和类型名会撞：备忘的桶就叫「备忘」（已完成时叫「备忘 · 已完成」），
       无脑拼起来就是「备忘 · 备忘 · 2026-10-08」（用户报的重复）。
       桶名已经带了类型前缀就只留桶名，否则才补类型。
    """
    kind_label = "备忘" if memo else "灵感"
    bucket_label = _BUCKET_LABEL[it["bucket"]]
    if bucket_label.startswith(kind_label):
        return f"{bucket_label} · {it['date']}"
    return f"{kind_label} · {bucket_label} · {it['date']}"
# 桶 → (浅色 hex, 深色 hex)，跟 web 抽屉的状态色一致
_BUCKET_COLOR = {
    "memo": ("#b8791a", "#f6ad55"),
    "memo_done": ("#6b7472", "#8b98a9"),
    "open": ("#0f8f85", "#4fd1c5"),
    "done": ("#3f8a4a", "#68d391"),
    "decided": ("#6b4fb8", "#b794f4"),
    "dropped": ("#6b7472", "#7c8798"),
}


def _clean_text(text):
    """和 brain-drawer.clean 一致：去 @ 占位、压掉多余空白、保留换行。"""
    t = re.sub(r"<[^<>\s]{1,40}>", " ", text or "")
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()

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
    循环轮播；点气泡弹出全文浮层并处理，处理后移出轮播；空格暂停/继续。
    Tk 控件没有透明度，渐入渐出用「颜色向底色插值」模拟。

    布局要点：
    · **泳道制**：舞台横向均分若干条泳道，一条泳道同一时刻只放一个气泡。
      这样气泡再高也不会互相压住 —— 气泡高度差异大（长文本能有半屏高），
      靠「错开起始时间」是躲不过重叠的，只有独占泳道才稳。
    · 速度一致：所有气泡按同一个 px/s 走，行程 = 舞台高 + 自身高度 + 余量，
      所以越高的气泡走得越久，但都能完整飘出顶部，不会半路被切掉。
    · 开屏预热：每条泳道先摆一个、直接放到飞行途中（首屏立刻有内容）。
    · 顶部遮罩：气泡**不淡出**，而是从下面滑进舞台顶部的遮罩带、被整块擦掉。
      以前是「气泡顶部升进舞台上方若干比例就整体插值到背景色」，长气泡的
      上半截会在下半截还很清楚的时候先变灰 —— 看着像被啃掉一块（用户报的
      「视觉缺口」）。遮罩带没有这个问题：气泡保持实色，被盖住多少就是多少。
    """

    BASE_DUR = 26.0        # 基准行程（1.26 倍舞台高）走完的秒数
    TRAVEL_VH = 1.26       # 基准行程 = 1.26 倍舞台高
    TICK_MS = 16           # 动画帧间隔（≈60fps；实测单帧只花 ~0.4ms，撑得住）
    LANE_MIN_W = 256       # 每条泳道最小宽度（决定泳道条数：1060px → 4 条）
    # 🔴 每条泳道同时最多几个气泡。3 → 6（用户：「现在看起来还是太稀疏」）。
    #    提到 6 之后同泳道间距只剩几十像素，**必须**有真实的不重叠判据兜住 ——
    #    见 `_lane_span`：光看「最低的那个」挡不住「插到两条中间压上去」。
    PER_LANE = 6
    MIN_GAP = 24           # 同泳道相邻气泡的最小纵向间隔
    TEXT_MAX = 1000        # 气泡正文最多显示多少字（超了截断 + 省略号，点开看全文）
    LINE_MAX = 45          # 气泡正文最多几行（极端多行的记录兜个底，别让气泡高到离谱）
    BAR_H = 34             # 底部筛选条高度（气泡在它下沿之下就开始渐显）
    TOP_MASK_H = 64        # 顶部遮罩带高度（气泡从下面滑进来、被它干净地擦掉）
    ALPHA_STEP = 0.05      # 渐隐渐出的量化步长（0.12 只有 8 档，肉眼是「跳」的）
    BUBBLE_R = 13          # 气泡圆角半径（背景画在舞台 Canvas 上）
    # 🔴 内容控件相对圆角矩形要内缩多少（占半径的比例）。
    #    控件本体是**直角**矩形，内缩量必须 ≥ r·(1 − 1/√2) ≈ 0.293r，
    #    否则它的直角会戳到圆弧外面露出小方块。取 0.45r 留足余量：
    #    0.293r 只有 0.3px 的富余，抗锯齿一抖就在角上留一道细缝。
    INSET_RATIO = 0.45
    # 🔴 气泡内部的排版常量 —— **正文折行宽度必须由它们算出来**，别在别处
    #    另写一个魔数。以前 wraplength 写死 `泳道宽 − 48`，而真实可用宽度是
    #    `泳道宽 − 2·inset − 2·ROW_PADX − EDGE_W − EDGE_GAP`（= −54），
    #    折行宽度比容器宽 6px ⇒ 每行最后一个字的右边缘被容器裁掉，
    #    用户看到的就是「字边缘消失」。
    ROW_PADX = 14          # 气泡内边距（左右）
    ROW_PADY = 10          # 气泡内边距（上下）
    EDGE_W = 4             # 左侧强调条宽度
    EDGE_GAP = 10          # 强调条与正文的间距

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
        self._running = True     # 空格暂停旗
        self._paused_at = None
        self._modal = None
        self._trash = None       # 回收站窗口（同一时刻只开一个）
        self._trash_pick = {}    # 回收站里 id → BooleanVar（勾选态）
        self._list = None        # 分类 / 搜索结果列表窗口（同上，只开一个）
        self._tick_job = None
        self._due = None         # 下一帧该跑的时刻（_tick 自校正用）
        self._rebuild_job = None
        self._L = dict(w=800, h=500, lanes=2, gap=24, lane_w=380, travel=630)
        # 🔴 字号改了会整窗重建（App._rebuild_for_font），这个标签页会被 destroy。
        #    `after` 定时器不随控件一起消失，`_tick` 一旦在销毁后再跑就会
        #    对着已销毁的控件调 Tk → 被全局异常钩子弹成错误框。用这个旗子兜住。
        self._alive = True
        self._build()
        self._tick()

    def destroy(self):
        """先熄灯再拆 —— 让还在路上的 `_tick` 自己停住（见 `_alive` 的说明）。"""
        self._alive = False
        super().destroy()

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
        # 🔴 空格绑定挂在标签页上、不挂舞台：遮罩带 / 筛选条是**舞台的同级兄弟**
        #    （见下），点它们再按空格，焦点不在舞台，挂舞台就收不到了。
        self.bind("<space>", lambda e: self._toggle_pause())
        self.stage.focus_set()

        # ── 顶部遮罩带 ────────────────────────────────────────────
        # 气泡从下往上滑，滑进这块纯底色带里就被干净地盖住（不是渐变消失）。
        #
        # 🔴 它必须是**舞台的同级兄弟控件**，不能是 canvas 上的矩形 item：
        #    卡片本体是 canvas 的嵌入窗口（`create_window`），而嵌入窗口在
        #    Windows 上永远画在 canvas 自绘内容（矩形/多边形）**之上** ——
        #    矩形遮罩压根盖不住它（真机像素实测：遮罩带里仍是卡片色）。
        #    而且 `tag_raise()` 对 canvas 的窗口 item **无效**（实测），
        #    没法靠提层级解决。同级兄弟用 place + lift 才能真正盖住整块舞台。
        self._mask = tk.Frame(self, bg=self._c("bg"), bd=0, highlightthickness=0)
        self._mask.place(x=0, y=0, relwidth=1, height=self.TOP_MASK_H)
        self._mask.lift()

        # 底部：**两行** —— 上面一行搜索，下面一行是原来的 chips + 工具按钮。
        # 🔴 同样做成舞台的**同级兄弟**（不是 canvas window）—— 否则后建的气泡
        #    会盖在筛选条上面（窗口 item 的层级提不上去，同上）。place 到最底、
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
        # 🔴 这行提示别写太长：筛选条是一整行 pack，字多了会把 chips 顶出窗口。
        hint = tk.Label(bar, text="空格 暂停/继续",
                        bg=self._c("bg"), fg=self._c("secondary"),
                        font=_font(8))
        hint.pack(side="left", padx=(12, 0))
        self._build_chips()

    def _build_chips(self):
        """底部那一排：**「思维流」+ 四个分类入口**。

        🔴 语义变了（用户 2026-10-11 定）—— 这一排**不再是轮播的筛选器**：
           · 「思维流」（原来的「全部」）= 当前这个视图本身，永远选中；点它只是
             把焦点还给舞台（空格暂停才收得到）。
           · 点「备忘 / 待处理 / 已处理 / 已决议」= 打开一个**列表窗口**
             （和回收站同一套骨架），在那儿按时间倒序翻 —— 不用等轮播飘过来。
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
                             selected_fill=self._c("active"),
                             parent_bg=self._c("bg"))
        view.pack(side="left", padx=(0, 3))
        view.set_selected(True)
        self._chips[None] = view
        for b in _BUCKET_ORDER:
            if b == "dropped":
                continue
            chip = RoundedButton(self._chips_box, _BUCKET_LABEL[b],
                                 lambda x=b: self._open_list(x),
                                 kind="ghost", radius=10, padx=10, pady=4,
                                 font=_font(10),
                                 fg=self._c("secondary"),
                                 hover_fill=self._c("active"),
                                 selected_fg=self._c("primary"),
                                 selected_fill=self._c("active"),
                                 parent_bg=self._c("bg"))
            chip.pack(side="left", padx=(0, 3))
            self._chips[b] = chip

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
        # 遮罩带 / 筛选条是舞台的同级兄弟，靠 place(relwidth=1) 自己跟着变宽，
        # 这里只要保证它们仍在舞台之上（重建气泡不会影响兄弟控件的层级，
        # 但重建整个标签页之后要再提一次）。
        self._mask.lift()
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
        """气泡在高度 y（顶边）时的不透明度。

        🔴 **只在底部渐入，顶部不渐出** —— 顶部交给遮罩带（见类 docstring）。
           以前顶部也按位置插值到背景色，长气泡的上半截会在下半截还很清楚的
           时候先变灰，看着像被啃掉一块（用户报的「视觉缺口」）。

        · 渐入：**顶边**从舞台底边冒上来的 60px 内淡入。
          🔴 不能用「底边越过筛选条」当判据 —— 那样高气泡（能有两百多 px）
             顶边早就进视口了，alpha 还是 0，表现是「气泡隐形上升好几秒」。
        """
        L = self._L
        return max(0.0, min(1.0, (L["h"] - y) / 60.0))

    def _lane_x(self, lane):
        """第 lane 条泳道的横向起点。"""
        L = self._L
        return L["gap"] + lane * (L["lane_w"] + L["gap"])

    def _lane_bottom(self, lane, now):
        """这条泳道上最低（y 最大）气泡的底边；空泳道返回 None。"""
        low = None
        for b in self._bubbles:
            if b["lane"] != lane:
                continue
            y = b["y_start"] - b["travel"] * (now - b["t0"]) / b["dur"]
            bottom = y + b["h"]
            if low is None or bottom > low:
                low = bottom
        return low

    def _lane_span(self, lane, now, y0, h):
        """候选气泡 [y0, y0+h] 放进这条泳道，会不会和现有的撞（含 MIN_GAP 间距）。

        🔴 必须和**每一个**现有气泡比，不能只看最低的那个 —— 首屏 spread 是往
           舞台中间几个固定高度插的，只比最低的挡不住「插到两条中间压上去」。
           PER_LANE 从 3 提到 6 之后同泳道间距只剩几十像素，这个洞会立刻暴露。
        🔴 所有气泡**同速**上升 ⇒ 相对位置恒定 ⇒ 入队时判一次就够，
           不会出现「后面追上来撞上」。
        """
        top, bot = y0, y0 + h
        for b in self._bubbles:
            if b["lane"] != lane:
                continue
            t = b["y_start"] - b["travel"] * (now - b["t0"]) / b["dur"]
            if bot + self.MIN_GAP > t and top - self.MIN_GAP < t + b["h"]:
                return False
        return True

    def _lane_ok(self, lane, now):
        """这条泳道现在能不能再放一个：新气泡从屏幕下方起步，只要求它和
        该泳道现有最低气泡之间留出 MIN_GAP 间距。

        🔴 因为所有气泡**同速**上升，间距一旦留出来就永远保持，所以只需在
        进入时刻判一次 —— 不会出现「后面追上来撞上」的情况。
        """
        low = self._lane_bottom(lane, now)
        if low is None:
            return True
        return low <= (self._L["h"] + 8) - self.MIN_GAP

    def _lane_ready(self, now):
        """随机挑一条能放的泳道；都没有返回 None。

        🔴 随机而不是「取编号最小的那条」：按编号取的话，先空出来的永远是
        0 号道，长时间跑下来三条道的利用率完全不均，看着像有规律。
        """
        free = [ln for ln in range(self._L["lanes"]) if self._lane_ok(ln, now)]
        return random.choice(free) if free else None

    def _fill(self, spread=False):
        """把空位补齐到上限。

        spread=True 用于首屏：**每条泳道按 PER_LANE 个位置散开摆**（不是只摆一个、
        剩下的全从屏幕下方往上爬 —— 那样首屏只有两三条，看着很空）。
        spread=False 只管补位。

        🔴 为什么要随机：所有气泡**同速**上升，同时出发的会永远排成一条水平线。
        首屏那几条如果按固定比例摆，看上去就是「一行一行对齐」的表格 ——
        用户明确说「太整齐了，希望更有随机感一点」。

        🔴 每摆一个都要过 `_lane_ok`（`_spawn` 内部会判）—— 长文本气泡能有三百多 px
        高，硬塞满 PER_LANE 个会撞在一起。放不下就少放几个，安全优先。
        """
        L = self._L
        cap = L["lanes"] * self.PER_LANE
        if spread:
            lanes = L["lanes"]
            # 把可见竖直区间均分成 PER_LANE 段；每段里随机取一点。
            # 分段保证同泳道两条之间的起始高度至少差一个段宽 —— 不会撞，也不齐。
            for lane in range(lanes):
                bands = list(range(self.PER_LANE))
                random.shuffle(bands)
                for r in bands:
                    if len(self._bubbles) >= cap:
                        return
                    lo = r / self.PER_LANE
                    hi = (r + 1) / self.PER_LANE
                    frac = 0.84 - 0.66 * random.uniform(lo, min(hi, 1.0))
                    self._spawn(frac=frac, lane=lane)
        while len(self._bubbles) < cap:
            if not self._spawn():
                break

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

    def _rows(self):
        """库里全部备忘 + 灵感（**原始行**，还没转成条目结构）。"""
        rows = []
        rows += _store.list_memos(500, kind=_store.KIND_MEMO)
        rows += _store.list_ideas(None, 500)
        return rows

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
            if gate_show and b not in self._show:
                continue
            content = _clean_text(r.get("content"))
            if not content:
                continue
            out.append({
                "id": r["id"], "kind": r.get("kind") or _store.KIND_MEMO,
                "status": r.get("status") or _store.STATUS_OPEN,
                "bucket": b, "content": content,
                "date": (r.get("created_at") or "")[:10],
                "tags": [t["name"] if isinstance(t, dict) else str(t)
                         for t in (r.get("tags") or [])],
                "images": r.get("images") or [],
                # 结论 / 备注（只有灵感有；备忘在库层面就没有这个概念）
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

    def _list_items(self, bucket=None, query=None):
        """列表窗口的条目：**不看展示范围**，按桶 / 关键词筛，时间倒序。

        🔴 搜索要搜「全部」而不是「轮播正在展示的那几类」—— 用户找东西的时候
           不知道、也不关心某个类别有没有被展示范围关掉。
        关键词命中范围：正文 + 标签 + 结论/备注，大小写不敏感。
        """
        out = self._collect(self._rows(), gate_show=False)
        if bucket is not None:
            out = [x for x in out if x["bucket"] == bucket]
        if query:
            q = query.strip().lower()
            if q:
                out = [x for x in out
                       if q in " ".join([x["content"], x["note"]]
                                        + x["tags"]).lower()]
        return out

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
        （从哪几类里挑）。和机器人收到「回旋镖」指令走**同一套** `store.pick_*` ——
        不重新实现一遍规则，免得两边行为不一致。

        🔴 挑中后会 `mark_boomerang()`：这就是「回旋镖」的语义 —— 翻出来给你看过了，
        下次不该再翻。所以点几次池子就少几条，推完会明确告诉你（而不是默默没反应）。
        """
        from datetime import datetime
        days = int(_settings.get("boomerang_days") or 0)
        scope = _settings.get("boomerang_scope")
        now = datetime.now()
        item = _store.pick_boomerang(days, scope, now)
        if not item:
            left_at_all = _store.count_boomerang(0, scope, now)
            if left_at_all:
                self.app.toast(f"还没有满 {days} 天的旧记录（可推范围在"
                               f"「配置中心 → 回旋镖」里改）", False)
            else:
                self.app.toast("旧记录都翻过一遍了 —— 同一条不会翻第二次", False)
            return
        _store.mark_boomerang(item["id"])
        created = (item.get("created_at") or "")[:10]
        try:
            ago = (now - datetime.fromisoformat(item["created_at"])).days
        except (ValueError, TypeError):
            ago = days
        rest = _store.count_boomerang(days, scope, now)
        self.app.toast(f"回旋镖：{'今天' if ago <= 0 else f'{ago} 天前'}"
                       f"（{created}）· 池子里还剩 {rest} 条", True)
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
        """把全部备忘 + 灵感导出成一个文件（网页版「⬇ 导出」的桌面版）。

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
                parent=self.app.root, title="导出灵感与备忘",
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
        # 🔴 从下方补位时可以先挑泳道：判据便宜，而且够用（新气泡的起点在舞台
        #    下方，只要最低那个让出 MIN_GAP 就一定不撞）。
        #    `frac` 那条路**不能**在这儿定泳道 —— 它是往舞台中间插的，得先拿到
        #    气泡真实高度才能判重叠，见下面 y0 算完之后那段。
        if lane is None and frac is None:
            lane = self._lane_ready(now)
            if lane is None:
                return False
        it = self._pick()
        if not it:
            return False
        width = L["lane_w"]
        w_int = int(width)
        inset = max(3, int(round(self.BUBBLE_R * self.INSET_RATIO)))
        # 🔴 正文折行宽度**从布局常量算**（见 ROW_PADX 那一带的说明）：
        #    气泡窗口被 canvas 钉成 `lane_w − 2·inset`，里面还要减掉 row 的
        #    左右内边距、左侧强调条、强调条与正文的间距。再留 2px 余量，
        #    防字体度量取整后仍然超出容器。
        content_w = max(120, w_int - inset * 2 - self.ROW_PADX * 2
                        - self.EDGE_W - self.EDGE_GAP - 2)

        bg, surf, border = self._c("bg"), self._c("light"), self._c("border")
        # 圆角卡片 = 舞台 Canvas 上的圆角矩形（fill/outline 随 alpha 插值）
        # + 内容控件贴在其内、四周内缩 1px 露出边框和圆角。
        # Tk 控件本体是直角矩形，任何「控件自身当卡片」的画法都不可能圆角。
        frame = tk.Frame(self.stage, bg=bg, bd=0, highlightthickness=0)
        row = tk.Frame(frame, bg=bg, padx=self.ROW_PADX, pady=self.ROW_PADY)
        row.pack(fill="both", expand=True)
        fade = [(frame, "bg", surf), (row, "bg", surf)]

        accent = _BUCKET_COLOR[it["bucket"]][
            1 if self.app.theme == THEME_DARK else 0]
        edge = tk.Frame(row, bg=bg, width=self.EDGE_W)
        edge.pack(side="left", fill="y", padx=(0, self.EDGE_GAP))
        fade.append((edge, "bg", accent))

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
        txt = tk.Label(body, text=shown, justify="left", anchor="nw",
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
            note_lbl = tk.Label(body, text="结论：" + it["note"],
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
            free = [ln for ln in range(L["lanes"])
                    if self._lane_span(ln, now, y0, h)]
            lane = random.choice(free) if free else None
        if lane is None or not self._lane_span(lane, now, y0, h):
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
        bg_id = round_rect(self.stage, x0 := self._lane_x(lane), y0,
                           x0 + w_int, y0 + h,
                           self.BUBBLE_R, fill=bg, outline=bg, width=1)
        win = self.stage.create_window(x0 + inset, y0 + inset, window=frame,
                                       anchor="nw", width=w_int - inset * 2)
        # 🔴 立刻按位置把 alpha 算出来，别留 0 等 _tick 来纠正 —— 首屏摆好的
        #    气泡要马上可见，否则会看到「一片空白、过一会儿才慢慢冒出来」。
        a0 = self._alpha_at(y0, h)
        self._apply_alpha(bubbles_bg=bg_id, fade=fade,
                          alpha=a0, bg=bg, surf=surf, border=border)
        # 🔴 这里**不用**再 tag_raise 筛选条 / 遮罩带 —— 它们是舞台的**同级兄弟**
        #    控件（place + lift），层级天然在整块舞台之上，气泡建多少个都压不下去。
        #    （以前是 canvas window + tag_raise，而 tag_raise 对窗口 item 无效，
        #     气泡其实一直盖在筛选条上面。）
        b = dict(win=win, bg_id=bg_id, frame=frame, fade=fade, item=it,
                 lane=lane, x0=x0, dx=dx, y_start=y_start, inset=inset,
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

    def _apply_alpha(self, bubbles_bg, fade, alpha, bg, surf, border):
        """把一整套控件颜色插到 alpha（1=完全显现，0=融进底色）。

        bubbles_bg 是舞台 Canvas 上气泡的圆角矩形背景 id —— fill 从底色
        插到卡片色、outline 从底色插到边框色，与内容控件同步渐隐。
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
                    outline=lerp_color(bg, border, alpha))
            except tk.TclError:
                pass

    def _drop(self, b, refill=True):
        """气泡升出屏幕 → 移除；refill=True 时在空出的泳道上补一条。

        泳道号是气泡自带的（不随列表顺序变），所以这里不用重排别人的横向位置。
        """
        try:
            self.stage.delete(b["win"])
            self.stage.delete(b["bg_id"])
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

        if not self._running or self._paused_at is not None:
            return
        # 🔴 不可见时**照样推进时间轴**，只跳过渲染。
        #    以前这里直接 `return`，最小化期间气泡既不过期、也不补位；再打开时
        #    所有气泡在同一帧集体过期被清空，补上来的新气泡还都在屏幕下方 ——
        #    表现就是「隔一会儿打开整屏没内容」（用户报的）。位置是按时间算的，
        #    所以后台跑完再切回来，画面直接就是稳态，不会跳。
        visible = bool(self.winfo_ismapped())
        bg, surf, border = self._c("bg"), self._c("light"), self._c("border")
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
            try:
                self.stage.coords(b["win"], xi + b["inset"], yi + b["inset"])
                # 🔴 圆角矩形是**多边形**，只能 `move()` 平移。
                #    `coords(bg_id, x1,y1,x2,y2)` 会把 40 个点截断成 2 个点
                #    （Tk 实测），圆角消失、只剩一条对角线残影从卡片左上角
                #    露出来 —— 用户报的「两个角的不明像素点」。
                ddx, ddy = xi - b["bx"], yi - b["by"]
                if ddx or ddy:
                    self.stage.move(b["bg_id"], ddx, ddy)
                    b["bx"], b["by"] = xi, yi
            except tk.TclError:
                self._bubbles.remove(b)
                continue
            # 渐隐渐出都按位置算（见 _alpha_at）—— 与 _spawn 共用同一套口径
            alpha = self._alpha_at(y, b["h"])
            if abs(alpha - b["alpha"]) >= self.ALPHA_STEP:
                b["alpha"] = alpha
                self._apply_alpha(b["bg_id"], b["fade"], alpha, bg, surf, border)

    def _toggle_pause(self):
        if self._paused_at is None:
            self._paused_at = time.monotonic()
        else:
            gap = time.monotonic() - self._paused_at
            for b in self._bubbles:
                b["t0"] += gap
            self._paused_at = None

    def _clear_stage(self):
        for b in list(self._bubbles):
            try:
                self.stage.delete(b["win"])
                self.stage.delete(b["bg_id"])
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
        # 标题叫「思维卡片」：这是**一条**记录的全貌，不是整个抽屉（用户改的）。
        win.title("思维卡片")
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
        bar_wrap = tk.Frame(win, bg=light)
        bar_wrap.pack(side="bottom", fill="x", padx=PAD + CARD_INSET,
                      pady=(0, PAD))
        tk.Frame(bar_wrap, bg=self._c("border"), height=1).pack(fill="x")
        cv = tk.Canvas(win, highlightthickness=0, bd=0, bg=self._c("bg"))
        cv.pack(side="top", fill="both", expand=True, padx=PAD, pady=(PAD, 4))
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

        # ── 行 2：正文（只读 Label ↔ 编辑态换成可编辑框）────────────
        body_holder = tk.Frame(card, bg=light)
        body_holder.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        txt = tk.Label(body_holder, text=it["content"],
                       justify="left", anchor="nw", bg=light, fg=self._c("fg"),
                       wraplength=520, font=_font(11))
        txt.pack(anchor="w")
        editbox = scrolledtext.ScrolledText(body_holder, height=6, wrap="word",
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
        #    备忘没有「结论」这个概念，所以整块不给它建。
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
        bar.pack(fill="x", pady=(10, 0))

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

        # 整条流程都摆出来，当前所在的那一站灰掉。
        # 灵感：待处理 ⇄ 已处理 / 已决议 / 丢弃；备忘：待处理 ⇄ 已完成 / 丢弃。
        if memo:
            # 已完成时「完成」按钮改口径成「取消完成」（再点就是退回待处理）
            done_label = ("← 取消完成" if st == _store.STATUS_DONE
                          else "✓ 完成这条备忘")
            plan = [
                (done_label, "success", "done",
                 st == _store.STATUS_DROPPED),
                ("× 丢弃", "danger", "dropped",
                 st == _store.STATUS_DROPPED),
                ("← 恢复", "secondary", "open",
                 st != _store.STATUS_DROPPED),
            ]
        else:
            plan = [
                ("✓ 已处理", "success", "done", st == _store.STATUS_DONE),
                ("★ 已决议", "info", "decided", st == _store.STATUS_DECIDED),
                ("× 丢弃", "danger", "dropped", st == _store.STATUS_DROPPED),
                ("← 恢复待处理", "secondary", "open", st == _store.STATUS_OPEN),
            ]
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
                txt.pack_forget()
                editbox.pack(fill="x")
                edit_hint.pack(anchor="w", pady=(6, 0))
                tag_wrap.grid_remove()
                if note_wrap is not None:
                    note_wrap.grid_remove()
                if imgs is not None:
                    imgs.grid_remove()
                if banner_lbl is not None:
                    banner_lbl.grid_remove()
                # 🔴 用 `before=cv` 放回原位：pack 是按调用顺序分配空间的，
                #    直接 `pack()` 会让动作条排到滚动区**后面**，抢不到位置。
                bar_wrap.pack_forget()
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
                txt.pack(anchor="w")
                tag_wrap.grid()
                if note_wrap is not None:
                    note_wrap.grid()
                if imgs is not None:
                    imgs.grid()
                if banner_lbl is not None:
                    banner_lbl.grid()
                bar_wrap.pack(side="bottom", fill="x", padx=PAD + CARD_INSET,
                              pady=(0, PAD), before=cv)
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
                    txt.configure(text=new)
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

        def _fit_card(_e=None):
            """卡片尺寸变了就重算：宽度跟着内容走，**高度封顶**、超出交给滚动。

            🔴 高度必须封顶 —— 否则长记录会把窗口顶穿屏幕（见 bar_wrap 的说明）。
            """
            cwc = card.winfo_reqwidth() + CARD_INSET * 2
            chc = card.winfo_reqheight() + CARD_INSET * 2
            bar_h = bar_wrap.winfo_reqheight()
            avail = max(240, int(win.winfo_screenheight() * 0.82)
                        - bar_h - PAD * 2 - 16)
            cvh = min(chc, avail)
            # 🔴 窗口尺寸下限 —— 用户报的两条都靠它：
            #    · 宽度：card 是直角 Frame，窗口拖得比它窄时 canvas 直接被裁掉，
            #      拖到极窄就「内容全没了」。下限 = 卡片宽 + 两侧留白。
            #    · 高度：进编辑态会 grid_remove 掉标签 / 结论 / 附件，卡片变矮、
            #      窗口跟着缩一截，看着像换了个窗口。所以下限取「只读态量到的高度」，
            #      **只抬不降**。
            if not editing["on"]:
                _floor["h"] = max(_floor["h"], cvh + bar_h + PAD * 2 + 4)
            try:
                win.minsize(cwc + PAD * 2,
                            min(_floor["h"],
                                int(win.winfo_screenheight() * 0.92)))
            except tk.TclError:
                pass
            if (_fit["w"], _fit["h"], _fit["ch"]) == (cwc, cvh, chc):
                return
            _fit.update(w=cwc, h=cvh, ch=chc)
            cv.configure(width=cwc, height=cvh,
                         scrollregion=(0, 0, cwc, chc))
            cv.delete("cardbg")
            round_rect(cv, 1, 1, cwc - 1, chc - 1, 16,
                       fill=self._c("light"), outline=self._c("border"),
                       width=1, tags="cardbg")
            cv.tag_lower("cardbg")
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

    def _delete(self, c):
        if not messagebox.askyesno("永久删除", "这条会永远消失，确定？"):
            return
        n = _store.delete_items([c["id"]])
        self.app.toast(f"已删除 {n} 条", bool(n))
        if self._modal is not None and self._modal.winfo_exists():
            self._modal.destroy()
        self.refresh(self.app.state)

    # ── 回收站（已丢弃）────────────────────────────────────────────
    def _dropped_items(self):
        """所有**已丢弃**的条目（备忘 + 灵感），最新的在前。

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
                "date": (r.get("created_at") or "")[:10],
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
        inner.bind("<Configure>",
                   lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>",
                lambda e: cv.itemconfigure(wid, width=e.width))
        parent.bind("<MouseWheel>",
                    lambda e: cv.yview_scroll(-1 if e.delta > 0 else 1, "units"))
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
        self._open_list(items=self._list_items(query=q), query=q)

    def _open_list(self, bucket=None, items=None, query=None):
        """分类 / 搜索结果的**静态列表窗口**。

        🔴 为什么要有它：轮播是「等条目飘过来」，条目一多就只能干等。列表窗口是
           「直接翻」—— 时间倒序、能滚、点一行就开那张思维卡片。
        🔴 和回收站共用 `_scroll_area` 那套骨架（顶部标题 / 中间可滚列表），
           但**不做勾选**：这里没有批量操作，点一行 = 打开卡片。
        🔴 同样**不放「关闭」**（窗口 X + Esc 就够，和浮层 / 回收站一条规矩）。
        🔴 数据侧走 `_list_items`，它**不看 `bubbles_show`** —— 用户找东西时
           不知道也不关心某类有没有被展示范围关掉。
        """
        _store.init()
        if items is None:
            items = self._list_items(bucket=bucket, query=query)
        if bucket is not None:
            title = f"{_BUCKET_LABEL[bucket]} · 列表"
            sub = "按时间倒序 · 点一行打开思维卡片"
        elif query:
            title = f"搜索「{query}」"
            sub = "命中正文 / 标签 / 结论 · 点一行打开思维卡片"
        else:
            title = "列表"
            sub = "按时间倒序 · 点一行打开思维卡片"

        if self._list is not None and self._list.winfo_exists():
            self._list.destroy()
        win = tk.Toplevel(self)
        self._list = win
        win.title(title)
        win.configure(bg=self._c("bg"))
        win.transient(self.app.root)
        W = 680
        H = max(320, min(560, int(win.winfo_screenheight() * 0.72)))
        win.geometry("%dx%d+%d+%d" % (
            W, H, max(8, self.app.root.winfo_rootx() + 150),
            max(8, self.app.root.winfo_rooty() + 110)))
        win.minsize(520, 300)

        PAD = 12
        light = self._c("light")
        # 🔴 Frame 的 `pady` 是**单个距离**，不是 pack/grid 那种 (上, 下) 元组 ——
        #    传元组会 `TclError: bad screen distance "10 4"`。
        head = tk.Frame(win, bg=self._c("bg"), padx=PAD, pady=10)
        head.pack(side="top", fill="x", pady=(0, 4))
        tk.Label(head, text=title, bg=self._c("bg"), fg=self._c("fg"),
                 font=_font(12, "bold")).pack(side="left")
        tk.Label(head, text=f"共 {len(items)} 条 · {sub}", bg=self._c("bg"),
                 fg=self._c("secondary"),
                 font=_font(9)).pack(side="left", padx=(10, 0))
        cv, inner = self._scroll_area(win, PAD)

        def _close():
            self._list = None
            try:
                win.destroy()
            except tk.TclError:
                pass

        def _open(it):
            """开卡片前先收掉列表 —— 不然卡片关掉之后底下还压着一个窗口。"""
            _close()
            self._open_modal(it)

        if not items:
            tk.Label(inner, text="这里没有条目", bg=self._c("bg"),
                     fg=self._c("secondary"),
                     font=_font(10)).pack(pady=48)
        for it in items:
            memo = it["kind"] == _store.KIND_MEMO
            row = tk.Frame(inner, bg=light, padx=12, pady=9)
            row.pack(fill="x", pady=3)
            accent = _BUCKET_COLOR[it["bucket"]][
                1 if self.app.theme == THEME_DARK else 0]
            tk.Label(row, text=_meta_text(it, memo), bg=light, fg=accent,
                     font=_font(9, "bold")).pack(anchor="w")
            summary = " ".join(it["content"].split())
            if len(summary) > 140:
                summary = summary[:140] + "…"
            tk.Label(row, text=summary, bg=light, fg=self._c("fg"),
                     justify="left", anchor="w", wraplength=580,
                     font=_font(10)).pack(anchor="w", pady=(3, 0))
            if it.get("note"):
                note = " ".join(it["note"].split())
                if len(note) > 120:
                    note = note[:120] + "…"
                tk.Label(row, text="结论：" + note, bg=light,
                         fg=self._c("secondary"), justify="left", anchor="w",
                         wraplength=580, font=_font(9)).pack(anchor="w",
                                                             pady=(3, 0))
            # 整行可点（含里面的 Label）—— 只绑 row 的话点在文字上没反应
            for w in [row] + list(row.winfo_children()):
                w.bind("<Button-1>", lambda e, x=it: _open(x))
                try:
                    w.configure(cursor="hand2")
                except tk.TclError:
                    pass

        win.protocol("WM_DELETE_WINDOW", _close)
        win.bind("<Escape>", lambda e: _close())
        cv.yview_moveto(0)

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
        x = max(8, self.app.root.winfo_rootx() + 150)
        y = max(8, self.app.root.winfo_rooty() + 110)
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
            tk.Label(mid, text=summary, bg=light, fg=self._c("fg"),
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
