"""验证快捷方式实际显示的图标 —— 让 Windows 自己解析 .lnk，再把图标抓成 PNG。

只检查「文件在不在」不够：IconFilename 写错了 .lnk 照样生成，只是 Windows 回落成默认图标。
这里用 SHGetFileInfoW 取 .lnk 的图标句柄（不加 overlay，拿到的就是目标图标），
再用 _check_exe_icon.hicon_to_image 转成像素，最后拼成一张对比图。
"""

import ctypes
import os
import sys
from ctypes import wintypes

from PIL import Image

import _check_exe_icon as ci

shell32 = ctypes.windll.shell32
user32 = ctypes.windll.user32

SHGFI_ICON = 0x00000100
SHGFI_LARGEICON = 0x00000000


class SHFILEINFOW(ctypes.Structure):
    _fields_ = [
        ("hIcon", ctypes.c_void_p),
        ("iIcon", ctypes.c_int),
        ("dwAttributes", wintypes.DWORD),
        ("szDisplayName", wintypes.WCHAR * 260),
        ("szTypeName", wintypes.WCHAR * 80),
    ]


shell32.SHGetFileInfoW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(SHFILEINFOW), wintypes.UINT, wintypes.UINT
]
shell32.SHGetFileInfoW.restype = ctypes.c_void_p
user32.DestroyIcon.argtypes = [ctypes.c_void_p]
user32.DestroyIcon.restype = ctypes.c_int


def lnk_icon(path):
    info = SHFILEINFOW()
    ret = shell32.SHGetFileInfoW(path, 0, ctypes.byref(info),
                                 ctypes.sizeof(info), SHGFI_ICON | SHGFI_LARGEICON)
    if not ret or not info.hIcon:
        return None
    try:
        return ci.hicon_to_image(info.hIcon)
    finally:
        user32.DestroyIcon(info.hIcon)


def main():
    sm = os.path.join(os.environ["APPDATA"], "Microsoft", "Windows", "Start Menu", "Programs", "memo-bot")
    desktop = os.path.join(os.path.expanduser("~"), "Desktop", "配置中心.lnk")

    targets = [
        ("配置中心", os.path.join(sm, "配置中心.lnk")),
        ("脑内抽屉", os.path.join(sm, "脑内抽屉.lnk")),
        ("使用说明", os.path.join(sm, "使用说明.lnk")),
        ("卸载", os.path.join(sm, "卸载 memo-bot.lnk")),
        ("桌面·配置中心", desktop),
    ]

    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_preview")
    os.makedirs(out_dir, exist_ok=True)

    cells = []
    for name, path in targets:
        if not os.path.isfile(path):
            print("  %-14s 文件不存在: %s" % (name, path))
            continue
        img = lnk_icon(path)
        if img is None:
            print("  %-14s 取图标失败" % name)
            continue
        print("  %-14s %s  -> 图标 %s" % (name, os.path.basename(path), img.size))
        cells.append((name, img))

    if not cells:
        return 1

    S = 96
    sheet = Image.new("RGB", (S * len(cells) + 20, S + 20), (250, 250, 250))
    for i, (name, img) in enumerate(cells):
        big = img.resize((S, S), Image.LANCZOS)
        sheet.paste(big, (10 + i * S, 10), big)
    out = os.path.join(out_dir, "_shortcut_icons.png")
    sheet.save(out)
    print("对比图:", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
