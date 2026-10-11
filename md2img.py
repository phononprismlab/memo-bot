"""Markdown / 纯文本 → PNG 图片（发到 QQ 群，手机端可直接查看）。

为什么：QQ 群里的「文件卡片」在手机端常常打不开（尤其 .md 这类无默认应用的类型），
把内容拆成多条文本消息又很碎。渲染成图片则原生可看、可缩放、保留排版。

链路：Python(markdown 库) 把源文件转 HTML → 套阅读样式 → 调 Node/Playwright
用系统 Chrome 截图（超高时纵向切片）。

用法：
  python md2img.py <文件> [输出目录]
"""
import html
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from botctl import _no_window_flags

HERE = os.path.dirname(os.path.abspath(__file__))
SHOT_JS = os.path.join(HERE, "md2png.mjs")


def find_node():
    """定位 node 可执行文件：环境变量 MEMO_NODE → PATH。

    🔴 不再写死任何本机路径 —— 旧实现把 workbuddy 管理的 node 目录钉死，
    换机器/升级 Node 就整个渲染链路失效。现在只认 MEMO_NODE 与 PATH 上的 `node`。
    """
    env = os.environ.get("MEMO_NODE")
    if env and os.path.isfile(env):
        return env
    found = shutil.which("node")
    if found:
        return found
    raise RuntimeError(
        "找不到 node 可执行文件。已尝试：环境变量 MEMO_NODE / PATH 上的 `node`\n"
        "→ 装 Node.js，或设环境变量 MEMO_NODE 指向 node 可执行文件。"
    )


IMG_WIDTH = 760       # CSS 宽度（dpr=2 → 实际 1520px）
SLICE_H = 0           # 0 = 交给 md2png.mjs 用默认（8000 CSS px = 16000 设备像素）
                      # 🔴 不能贪大：Chrome 截图在设备像素 >16384 时会**回绕**（末尾重复开头）
MAX_MERGE_PX = 40000  # 拼接后总设备像素上限；超过则不拼接、直接发多片
MD_EXT = {".md", ".markdown"}

# 阅读样式：浅色、窄栏、中文优先字体
_TEMPLATE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<base href="{base}">
<title>{title}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 28px 30px 34px;
    background: #ffffff; color: #1f2328;
    font: 15px/1.75 -apple-system, "Segoe UI", "Microsoft YaHei", "PingFang SC",
          "Hiragino Sans GB", "Source Han Sans SC", sans-serif;
    -webkit-font-smoothing: antialiased;
  }}
  .doc-title {{
    font-size: 15px; color: #6b7280; letter-spacing: .3px;
    border-bottom: 1px solid #e5e7eb; padding-bottom: 10px; margin: 0 0 20px;
  }}
  h1, h2, h3, h4 {{ line-height: 1.35; margin: 26px 0 12px; font-weight: 700; }}
  h1 {{ font-size: 24px; padding-bottom: 8px; border-bottom: 2px solid #e5e7eb; }}
  h2 {{ font-size: 20px; padding-bottom: 6px; border-bottom: 1px solid #eef0f2; }}
  h3 {{ font-size: 17px; }}
  h4 {{ font-size: 15px; color: #374151; }}
  p {{ margin: 10px 0; }}
  ul, ol {{ margin: 10px 0; padding-left: 26px; }}
  li {{ margin: 4px 0; }}
  li > ul, li > ol {{ margin: 4px 0; }}
  code {{
    background: #f3f4f6; border-radius: 4px; padding: 1px 5px;
    font-family: "Cascadia Mono", Consolas, "Courier New", monospace; font-size: 13px;
    color: #b02a37; word-break: break-all;
  }}
  pre {{
    background: #f6f8fa; border: 1px solid #e5e7eb; border-radius: 8px;
    padding: 12px 14px; overflow-x: auto; margin: 12px 0;
  }}
  pre code {{ background: none; color: #24292f; padding: 0; font-size: 12.5px; line-height: 1.6; }}
  blockquote {{
    margin: 12px 0; padding: 8px 14px; color: #57606a;
    background: #f8fafc; border-left: 4px solid #cbd5e1; border-radius: 0 6px 6px 0;
  }}
  blockquote p {{ margin: 4px 0; }}
  table {{ border-collapse: collapse; width: 100%; margin: 14px 0; font-size: 13.5px; }}
  th, td {{ border: 1px solid #e5e7eb; padding: 7px 10px; text-align: left; vertical-align: top; }}
  th {{ background: #f3f4f6; font-weight: 600; }}
  tr:nth-child(even) td {{ background: #fafbfc; }}
  hr {{ border: none; border-top: 1px solid #e5e7eb; margin: 22px 0; }}
  a {{ color: #0969da; text-decoration: none; word-break: break-all; }}
  img {{ max-width: 100%; max-height: 600px; height: auto;
         border: 1px solid #e5e7eb; border-radius: 6px; margin: 6px 0; }}
  strong {{ font-weight: 700; }}
</style></head>
<body>
<div class="doc-title">{title}</div>
{body}
</body></html>
"""


def _to_html(src_path):
    """源文件 → HTML 字符串。.md 走 markdown 渲染，其它文本按等宽原样。"""
    with open(src_path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    title = html.escape(os.path.basename(src_path))
    ext = os.path.splitext(src_path)[1].lower()
    # 🔴 HTML 写在临时目录里，正文里的相对图片路径会失效 →
    #    用 <base> 指回源文件所在目录，让 ![](相对路径.png) 能正常加载。
    try:
        base = Path(src_path).resolve().parent.as_uri() + "/"
    except Exception:
        base = ""
    if ext in MD_EXT:
        import markdown

        body = markdown.markdown(text, extensions=["extra", "sane_lists"])
    else:
        body = "<pre><code>" + html.escape(text) + "</code></pre>"
    return _TEMPLATE.format(base=base, title=title, body=body)


def _merge(paths):
    """多片纵向拼回单张。

    md2png.mjs 为避开 Chrome 的 16384 回绕必须分片截图，这里再拼回去 ——
    用户看到的仍是**一张长图**，但内容完整、不会在末尾重复开头。
    只有 1 片时原样返回；总高超过 MAX_MERGE_PX 时也原样返回多片（防超大图）。
    """
    if len(paths) <= 1:
        return paths
    try:
        from PIL import Image
    except ImportError:
        return paths

    sizes, total = [], 0
    for p in paths:
        with Image.open(p) as im:
            sizes.append((im.width, im.height))
            total += im.height
    if total > MAX_MERGE_PX:
        return paths

    canvas = Image.new("RGB", (max(w for w, _ in sizes), total), "white")
    y = 0
    for p, (_, ih) in zip(paths, sizes):
        with Image.open(p) as im:
            canvas.paste(im, (0, y))
        y += ih
    out = os.path.join(os.path.dirname(paths[0]), "merged.png")
    canvas.save(out, "PNG")
    canvas.close()
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass
    return [out]


def render(src_path, out_dir=None, width=IMG_WIDTH, slice_h=SLICE_H):
    """渲染成 PNG，返回图片路径列表（通常为 1 张长图）。"""
    if out_dir is None:
        out_dir = tempfile.mkdtemp(prefix="mdimg_")
    os.makedirs(out_dir, exist_ok=True)

    html_path = os.path.join(out_dir, "_doc.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(_to_html(src_path))

    proc = subprocess.run(
        [find_node(), SHOT_JS, html_path, out_dir, str(width), str(slice_h)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        # 🔴 node 是控制台程序：机器人跑在 pythonw 下没有控制台，不带这个标志
        #    它就会自己新建一个黑窗口 —— 每次「发文件 + 渲染成图片」都闪一下。
        creationflags=_no_window_flags(),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"截图失败：{proc.stderr.strip() or proc.stdout.strip()}")
    line = (proc.stdout or "").strip().splitlines()[-1]
    return _merge(json.loads(line)["outs"])


def main():
    if len(sys.argv) < 2:
        print("用法：python md2img.py <文件> [输出目录]")
        sys.exit(2)
    src = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else None
    outs = render(src, out)
    for p in outs:
        print(f"{p}  ({os.path.getsize(p) // 1024} KB)")


if __name__ == "__main__":
    main()
