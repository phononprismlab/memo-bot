"""从 PE 文件里抽出关联图标并存成 PNG —— 用来验证 SetupIconFile 真的生效了。

为什么不用 PowerShell：本机 Add-Type 被安全策略拦（禁止运行时编译 .NET）。
为什么不用 PIL：Pillow 读不了 exe 的图标资源。

做法：shell32.ExtractIconExW 拿 HICON → GetIconInfo 拿 HBITMAP →
       GetDIBits 取 BGRA 像素 → 交给 Pillow 存 PNG。

用法：python _check_exe_icon.py <exe路径> <输出png路径>
"""

import ctypes
import sys
from ctypes import wintypes

from PIL import Image

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
shell32 = ctypes.windll.shell32

# 🔴 64 位必须显式声明，否则句柄会被按 c_int 截断（同 shutdown_hook.py 的坑）
shell32.ExtractIconExW.argtypes = [
    wintypes.LPCWSTR,
    ctypes.c_int,
    ctypes.POINTER(wintypes.HICON),
    ctypes.POINTER(wintypes.HICON),
    wintypes.UINT,
]
shell32.ExtractIconExW.restype = wintypes.UINT


class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HBITMAP),
        ("hbmColor", wintypes.HBITMAP),
    ]


class BITMAP(ctypes.Structure):
    _fields_ = [
        ("bmType", wintypes.LONG),
        ("bmWidth", wintypes.LONG),
        ("bmHeight", wintypes.LONG),
        ("bmWidthBytes", wintypes.LONG),
        ("bmPlanes", wintypes.WORD),
        ("bmBitsPixel", wintypes.WORD),
        ("bmBits", ctypes.c_void_p),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


user32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.POINTER(ICONINFO)]
user32.GetIconInfo.restype = wintypes.BOOL
gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
gdi32.GetObjectW.restype = ctypes.c_int

# 🔴 句柄一律声明成 c_void_p：默认按 c_int 处理会在 64 位上溢出
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
gdi32.DeleteObject.restype = ctypes.c_int
gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
gdi32.DeleteDC.restype = ctypes.c_int
gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
gdi32.GetDIBits.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT, wintypes.UINT,
    ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
]
gdi32.GetDIBits.restype = ctypes.c_int
user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.ReleaseDC.restype = ctypes.c_int
user32.GetDC.argtypes = [ctypes.c_void_p]
user32.GetDC.restype = ctypes.c_void_p
user32.DestroyIcon.argtypes = [ctypes.c_void_p]
user32.DestroyIcon.restype = ctypes.c_int


def hicon_to_image(hicon):
    ii = ICONINFO()
    if not user32.GetIconInfo(hicon, ctypes.byref(ii)):
        raise OSError("GetIconInfo 失败")

    bm = BITMAP()
    if not gdi32.GetObjectW(ii.hbmColor, ctypes.sizeof(BITMAP), ctypes.byref(bm)):
        raise OSError("GetObject 失败")
    w, h = bm.bmWidth, bm.bmHeight

    hdc = user32.GetDC(0)
    memdc = gdi32.CreateCompatibleDC(hdc)
    try:
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h  # 负数 = 自上而下
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0  # BI_RGB

        buf = ctypes.create_string_buffer(w * h * 4)
        if not gdi32.GetDIBits(memdc, ii.hbmColor, 0, h, buf, ctypes.byref(bmi), 0):
            raise OSError("GetDIBits 失败")

        img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1)
        return img.copy()
    finally:
        gdi32.DeleteDC(memdc)
        user32.ReleaseDC(0, hdc)
        gdi32.DeleteObject(ii.hbmColor)
        gdi32.DeleteObject(ii.hbmMask)


def main():
    exe, out = sys.argv[1], sys.argv[2]
    large = (wintypes.HICON * 1)()
    small = (wintypes.HICON * 1)()
    n = shell32.ExtractIconExW(exe, 0, large, small, 1)
    print("ExtractIconExW 返回:", n)
    if n < 1 or not large[0]:
        print("没抽到图标")
        return 1

    img = hicon_to_image(large[0])
    img.save(out)
    print("尺寸:", img.size, "->", out)
    user32.DestroyIcon(large[0])
    if small[0]:
        user32.DestroyIcon(small[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
