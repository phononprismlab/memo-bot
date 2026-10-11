"""生成 memo-bot 的图标（.ico + .png），写进仓库 `assets/icons/`。

输入（美术原图，放在 `~/Downloads`，本脚本会同时拷一份到 `assets/icons/src/` 存档）：
    設定・ひらめき・削除の金魚アイコンセット.png   三连图 → 配置中心 / 脑内抽屉 / 卸载
    怒り顔のちび koi sprite.png                   单张   → 安装包自身 / 品牌标

输出（`memo-bot/assets/icons/`，**仓库内，是图标的唯一来源**）：
    installer.ico / .png    安装包自身 + 品牌标
    config.ico / .png       配置中心
    drawer.ico / .png       脑内抽屉
    uninstall.ico / .png    卸载
    src/                    美术原图存档（重跑本脚本要用）

`memo-bot.iss` 直接引用这里的 .ico —— 别处不要再放副本，否则会漂移。
验收用的预览图写到 `_exe_poc/_preview/`，不进仓库。
"""

import os
import shutil
from collections import deque

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))          # _exe_poc
ROOT = os.path.dirname(HERE)                                # 工作区根
REPO_ICONS = os.path.join(ROOT, "memo-bot", "assets", "icons")
PREVIEW = os.path.join(HERE, "_preview")
DL = os.path.join(os.path.expanduser("~"), "Downloads")

KOI = os.path.join(DL, "怒り顔のちび koi sprite.png")
SHEET = os.path.join(DL, "設定・ひらめき・削除の金魚アイコンセット.png")

SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
PREVIEW_SIZES = (16, 32, 48)


def alpha_mask(im):
    """把 alpha 二值化 —— 抗锯齿边缘的半透明像素不算内容。"""
    return im.getchannel("A").point(lambda v: 255 if v > 8 else 0)


def square_crop(im, margin=0.05):
    """裁到内容外框，再补成正方形（四周留 margin 比例的透明边）。

    图标必须正方形，否则 Windows 会拉伸变形。
    """
    box = alpha_mask(im).getbbox()
    if box:
        im = im.crop(box)
    w, h = im.size
    side = max(w, h) + int(max(w, h) * margin) * 2
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(im, ((side - w) // 2, (side - h) // 2), im)
    return canvas


def components(im, cell=16):
    """按 alpha 网格连通块切开三连图，返回各块外框（按 上→下、左→右 排序）。"""
    a = alpha_mask(im)
    w, h = a.size
    cols, rows = w // cell, h // cell
    grid = [[False] * cols for _ in range(rows)]
    for gy in range(rows):
        for gx in range(cols):
            if a.crop((gx * cell, gy * cell, (gx + 1) * cell, (gy + 1) * cell)).getbbox():
                grid[gy][gx] = True

    seen = [[False] * cols for _ in range(rows)]
    boxes = []
    for gy in range(rows):
        for gx in range(cols):
            if not grid[gy][gx] or seen[gy][gx]:
                continue
            q = deque([(gx, gy)])
            seen[gy][gx] = True
            x0 = x1 = gx
            y0 = y1 = gy
            while q:
                cx, cy = q.popleft()
                x0, x1 = min(x0, cx), max(x1, cx)
                y0, y1 = min(y0, cy), max(y1, cy)
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        nx, ny = cx + dx, cy + dy
                        if 0 <= nx < cols and 0 <= ny < rows and grid[ny][nx] and not seen[ny][nx]:
                            seen[ny][nx] = True
                            q.append((nx, ny))
            px0, py0 = x0 * cell, y0 * cell
            px1, py1 = min(w, (x1 + 1) * cell), min(h, (y1 + 1) * cell)
            sub = a.crop((px0, py0, px1, py1)).getbbox()
            boxes.append((px0 + sub[0], py0 + sub[1], px0 + sub[2], py0 + sub[3]))

    boxes.sort(key=lambda b: (b[1] // 200, b[0]))
    return boxes


def save_icon(im, name):
    ico = os.path.join(REPO_ICONS, name + ".ico")
    im.save(ico, format="ICO", sizes=SIZES)
    im.resize((256, 256), Image.LANCZOS).save(os.path.join(REPO_ICONS, name + ".png"))
    os.makedirs(PREVIEW, exist_ok=True)
    for s in PREVIEW_SIZES:
        im.resize((s, s), Image.LANCZOS).save(
            os.path.join(PREVIEW, "{}_{}.png".format(name, s))
        )
    return ico


def archive_sources():
    src = os.path.join(REPO_ICONS, "src")
    os.makedirs(src, exist_ok=True)
    for path, alias in ((KOI, "koi.png"), (SHEET, "goldfish-icon-set.png")):
        shutil.copyfile(path, os.path.join(src, alias))
        print("  存档 %-22s %d 字节" % (alias, os.path.getsize(os.path.join(src, alias))))


def main():
    os.makedirs(REPO_ICONS, exist_ok=True)

    koi = square_crop(Image.open(KOI).convert("RGBA"))
    print("installer  裁切后:", koi.size)
    save_icon(koi, "installer")

    sheet = Image.open(SHEET).convert("RGBA")
    boxes = components(sheet)
    print("三连图切出", len(boxes), "块:")
    for b in boxes:
        print("   ", b, "->", (b[2] - b[0], b[3] - b[1]))

    for box, name in zip(boxes, ["config", "drawer", "uninstall"]):
        icon = square_crop(sheet.crop(box))
        print("  %-10s 裁切后: %s" % (name, icon.size))
        save_icon(icon, name)

    print("\n美术原图存档:")
    archive_sources()

    print("\n输出目录:", REPO_ICONS)
    for f in sorted(os.listdir(REPO_ICONS)):
        p = os.path.join(REPO_ICONS, f)
        if os.path.isfile(p):
            print("  %-16s %d 字节" % (f, os.path.getsize(p)))


if __name__ == "__main__":
    main()
