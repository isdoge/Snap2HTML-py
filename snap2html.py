#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Snap2HTML 跨平台版（Python 移植）

与 Snap2HTML 2.x (C# 版) 生成完全相同的数据格式 (V2)，直接复用仓库中的
template.html，因此生成的 HTML 与 C# 版外观、搜索、链接功能完全一致。
仅依赖 Python 3.8+ 标准库，可在 Windows / Debian Linux / macOS 上运行。

命令行用法:
  python snap2html.py <目录> [更多目录...] [-o 输出.html] [选项]
  python snap2html.py --merge 已生成1.html 已生成2.html [-o 合并.html]

多目录说明: 给出多个目录时一次扫描并合并为一份多根清单（模板自动显示为
虚拟根节点下的多个目录树）；--merge 则合并已生成的 Snap2HTML V2 清单文件。

选项:
  -o, --outfile FILE   输出 HTML 路径 (默认: 当前目录下 <目录名>.html)
  -t, --title TEXT     页面标题 (默认: "<目录> 的快照")
  --link [URL]         为文件生成下载/打开链接; 不带值时自动用 file:// URI
  --hidden             包含隐藏项 (Windows: Hidden 属性; Unix: 点开头; macOS 另含 UF_HIDDEN)
  --system             包含系统项 (仅 Windows 的 System 属性有意义)
  --follow             跟随目录符号链接 (默认不跟随, 避免死循环)
  --template FILE      指定模板路径 (默认: 脚本同目录的 template.html)
  --open               生成完成后用系统默认浏览器打开
  -q, --quiet          不输出进度信息

Web 界面模式 (适合无桌面的 Ubuntu/Debian 服务器, 浏览器跨设备访问):
  python snap2html.py --serve [--host 0.0.0.0] [--port 8765] [--output-dir DIR]
  然后用浏览器打开 http://127.0.0.1:8765/ （或 http://<服务器IP>:8765/）

兼容 C# 版的旧自动化参数写法:
  python snap2html.py -path <目录> -outfile 输出.html [-hidden] [-system] [-title 标题] [-link URL]

已知限制 (与 C# 版一致):
  文件/文件夹名中含 "*" 时会破坏数据格式 (名字按 * 分隔解析)。
"""

import argparse
import html
import json
import os
import re
import stat as stat_module
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import parse as urlparse

APP_NAME = "Snap2HTML-py"
APP_VERSION = "2.52"
DATA_VERSION = "2"
BASE36_DIGITS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

UF_HIDDEN = getattr(stat_module, "UF_HIDDEN", 0x8000)  # macOS
FILE_ATTRIBUTE_HIDDEN = getattr(stat_module, "FILE_ATTRIBUTE_HIDDEN", 2)
FILE_ATTRIBUTE_SYSTEM = getattr(stat_module, "FILE_ATTRIBUTE_SYSTEM", 4)

IS_WINDOWS = sys.platform == "win32"


class GenerationError(Exception):
    """生成过程中的可预期错误（输入无效、模板缺失等）"""


def to_base36(number):
    """等价于 Utils.DecimalToArbitrarySystem(number, 36)"""
    if number == 0:
        return "0"
    negative = number < 0
    number = abs(number)
    digits = []
    while number:
        number, remainder = divmod(number, 36)
        digits.append(BASE36_DIGITS[remainder])
    return ("-" if negative else "") + "".join(reversed(digits))


def js_escape(text):
    """等价于 HttpUtility.JavaScriptStringEncode（用于安全嵌入 <script>）"""
    out = json.dumps(text, ensure_ascii=False)[1:-1]
    # JSON 不转义 U+2028/U+2029/U+0085，但它们在 <script> 中是非法/危险的
    for ch in ("\u2028", "\u2029", "\u0085"):
        out = out.replace(ch, "\\u%04x" % ord(ch))
    return out


def natural_sorted(items, key):
    """等价于 Utils.OrderByNatural: 数字段按位数补零后忽略大小写比较"""
    items = list(items)
    rx = re.compile(r"\d+")
    max_digits = max((len(m.group()) for it in items for m in rx.finditer(key(it))), default=0)

    def sort_key(item):
        return rx.sub(lambda m: m.group().rjust(max_digits, "0"), key(item)).casefold()

    return sorted(items, key=sort_key)


def bytes_to_filesize(n):
    """等价于 Utils.BytesToFilesize"""
    kb, mb, gb, tb = 1024, 1024 ** 2, 1024 ** 3, 1024 ** 4
    if 0 <= n < kb:
        return f"{n} 字节"
    if n < mb:
        return f"{round(n / kb)} KB"
    if n < gb:
        return f"{round(n / mb, 1)} MB"
    if n < tb:
        return f"{round(n / gb, 2)} GB"
    return f"{round(n / tb, 2)} TB"


def path_to_file_uri(path):
    """等价于 Utils.PathToFileUri 的常用场景"""
    return Path(path).absolute().as_uri()


class SnappedFile:
    __slots__ = ("name", "size", "mtime")

    def __init__(self, name, size, mtime):
        self.name = name
        self.size = size
        self.mtime = mtime


class SnappedFolder:
    __slots__ = ("name", "path", "fullpath", "mtime", "size", "deepsize", "files", "error")

    def __init__(self, fullpath):
        parent = os.path.dirname(fullpath)
        if parent == fullpath:  # 盘符根 "C:\" 或文件系统根 "/"
            self.name, self.path = fullpath, None
        else:
            self.name, self.path = os.path.basename(fullpath), parent
        self.fullpath = fullpath
        try:
            self.mtime = int(os.stat(fullpath).st_mtime)
        except OSError:
            self.mtime = 0
        self.size = 0        # 仅本层文件
        self.deepsize = 0    # 含所有子目录
        self.files = []
        self.error = None


def is_hidden_entry(entry, st):
    if entry.name.startswith("."):
        return True
    if sys.platform == "darwin":
        return bool(getattr(st, "st_flags", 0) & UF_HIDDEN)
    if IS_WINDOWS:
        return bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_HIDDEN)
    return False


def is_system_entry(st):
    if IS_WINDOWS:
        return bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_SYSTEM)
    return False


def scan(root, skip_hidden, skip_system, follow, progress=None):
    """等价于 frmMain_BackgroundWorker.GetContent 的 BFS 扫描。

    progress(path, dirs_done) 每扫描约 100 个目录回调一次。
    """
    folders = {}
    errors = []
    star_names = []
    queue = [root]
    visited = set()  # --follow 时的符号链接环路保护
    count = 0

    while queue:
        fullpath = queue.pop(0)
        folder = SnappedFolder(fullpath)
        count += 1
        if progress and count % 100 == 0:
            progress(fullpath, count)

        try:
            with os.scandir(fullpath) as it:
                entries = list(it)
        except OSError as ex:
            folder.error = str(ex)
            errors.append((fullpath, ex))
            folder.size = 0
        else:
            for entry in natural_sorted(entries, key=lambda e: e.name):
                try:
                    lst = entry.stat(follow_symlinks=False)
                except OSError:
                    continue  # 竞态: 扫描期间被删除
                if skip_hidden and is_hidden_entry(entry, lst):
                    continue
                if skip_system and is_system_entry(lst):
                    continue

                is_dir = entry.is_dir(follow_symlinks=False)
                if is_dir:
                    if entry.is_symlink() and not follow:
                        continue  # 不跟随符号链接目录: 仅列出本身，不递归
                    if entry.is_symlink():
                        real = os.path.realpath(entry.path)
                        if real in visited:
                            folder.error = folder.error or "符号链接环路: " + entry.name
                            continue
                        visited.add(real)
                    queue.append(os.path.abspath(entry.path))
                else:
                    try:
                        st = entry.stat(follow_symlinks=True)
                    except OSError:
                        st = lst  # 断开的符号链接等
                    if "*" in entry.name:
                        star_names.append(entry.path)
                    folder.files.append(SnappedFile(entry.name, st.st_size, int(st.st_mtime)))
            folder.size = sum(f.size for f in folder.files)

        folder.deepsize = folder.size
        folders[fullpath] = folder

        # 向所有祖先累加本层大小
        ancestor = folder.path
        while ancestor is not None and ancestor in folders:
            folders[ancestor].deepsize += folder.size
            ancestor = folders[ancestor].path

    return folders, errors, star_names


def find_template(explicit):
    if explicit:
        if not os.path.isfile(explicit):
            raise GenerationError(f"未找到模板文件: {explicit}")
        return explicit
    here = Path(__file__).resolve().parent / "template.html"
    if here.is_file():
        return str(here)
    raise GenerationError("未找到 template.html（需与 snap2html.py 同目录，或用 --template 指定）")


def build_data_lines(root, metadata_json, ordered, indexes, subdirs):
    """等价于 WriteJavascriptContentArray，数据格式 V2（见 RedeMeDev.txt）"""
    lines = []
    for folder in ordered:
        parts = []
        folder_size = -1 if folder.error else folder.deepsize
        parts.append('"%s*%s*%s"' % (
            js_escape(folder.name),
            to_base36(folder_size),
            to_base36(folder.mtime),
        ))
        parent = -1 if folder.fullpath == root else indexes[folder.path]
        parts.append(str(parent))
        parts.append('"%s"' % "*".join(str(i) for i in subdirs[folder.fullpath]))
        for file in folder.files:
            parts.append('"%s*%s*%s"' % (
                js_escape(file.name),
                to_base36(file.size),
                to_base36(file.mtime),
            ))
        if folder.fullpath == root:
            parts.append(metadata_json)
        lines.append("p([" + ",".join(parts) + "])")
    return lines


def _resolve_outfile(default_name, opts, default_multi_name="merged.html"):
    """计算输出文件路径（opts.outfile / opts.output_dir / 默认名）"""
    outfile = opts.get("outfile")
    output_dir = opts.get("output_dir")
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    if not outfile:
        outfile = default_multi_name if default_multi_name and opts.get("_multi") else default_name
    if output_dir and not os.path.isabs(outfile):
        outfile = os.path.join(output_dir, outfile)
    outfile = os.path.abspath(outfile)
    if not os.path.isdir(os.path.dirname(outfile)):
        raise GenerationError("输出路径不存在: " + os.path.dirname(outfile))
    return outfile


def _render_and_write(outfile, page_title, data_lines, total_files, total_dirs, total_bytes, template_path):
    """把数据行拼进模板占位符生成最终 HTML（单根/多根/合并共用）。

    多根时模板会自动创建虚拟根节点并按各根的 metadata 汇总统计。
    生成清单的初始主题固定 auto（跟随系统），查看者可用页内按钮切换。
    """
    with open(template_path, "r", encoding="utf-8-sig") as fh:
        template = fh.read()

    title_html = html.escape(page_title, quote=True)
    replacements = {
        "[PAGE TITLE]": title_html,
        "[PAGE TITLE JS]": js_escape(page_title),
        "[BODY TITLE]": title_html.replace("\\", "\\<wbr>"),  # <wbr> = 长路径换行提示
        "[APP LINK]": "https://github.com/isdoge/Snap2HTML-py",
        "[APP NAME]": APP_NAME,
        "[APP VER]": APP_VERSION,
        "[DATA VER]": DATA_VERSION,
        "[VIEWER THEME]": "auto",
        "[GEN TIME]": time.strftime("%H:%M:%S"),
        "[GEN DATE]": time.strftime("%Y-%m-%d"),
        "[GEN TIMESTAMP]": str(int(time.time())),
        "[NUM FILES]": str(total_files),
        "[NUM DIRS]": str(total_dirs),
        "[TOT SIZE]": bytes_to_filesize(total_bytes),
        "[TOT BYTES]": str(total_bytes),
    }
    for key, value in replacements.items():
        template = template.replace(key, value)

    marker = "[DIR DATA]"
    position = template.index(marker)
    output = template[:position] + "\n".join(data_lines) + "\n" + template[position + len(marker):]

    with open(outfile, "w", encoding="utf-8-sig", newline="\n") as fh:
        fh.write(output)
    return outfile


def generate_snapshot(opts, progress=None, phase=None):
    """执行一次快照生成，支持 1..N 个根目录（多根 = 合并为一份清单），返回结果 dict。

    opts 字段:
      roots(列表) 或 root(单个), title, outfile, hidden, system, follow, link, template, output_dir
    progress(path, dirs_done) 在扫描阶段回调; phase(str) 在阶段切换时回调。
    """
    roots = list(opts.get("roots") or [])
    if not roots and opts.get("root"):
        roots = [opts["root"]]
    if not roots:
        raise GenerationError("缺少目录参数")
    normalized = []
    for root in roots:
        root = os.path.normpath(os.path.abspath(str(root)))
        if not os.path.isdir(root):
            raise GenerationError("输入路径不存在: " + root)
        normalized.append(root)
    roots = normalized
    multi = len(roots) > 1

    default_name = re.sub(r'[<>:"/\\|?*]', "_", os.path.basename(roots[0].rstrip("\\/")) or "snapshot") + ".html"
    opts["_multi"] = multi
    outfile = _resolve_outfile(default_name, opts, "merged.html" if multi else None)
    user_title = opts.get("title")
    link_opt = opts.get("link") or ""
    template_path = find_template(opts.get("template"))

    data_lines = []
    total_files = total_bytes = total_dirs = 0
    all_errors = []
    star_names = []
    n = len(roots)
    for i, root in enumerate(roots):
        if phase:
            phase(f"扫描目录 ({i + 1}/{n})" if multi else "扫描目录")
        folders, errors, stars = scan(
            root,
            skip_hidden=not opts.get("hidden"),
            skip_system=not opts.get("system"),
            follow=opts.get("follow"),
            progress=progress,
        )
        if not folders:
            raise GenerationError("根目录无法读取: " + root)

        if phase:
            phase("排序与统计" if not multi else f"排序与统计 ({i + 1}/{n})")
        ordered = natural_sorted(folders.values(), key=lambda f: f.fullpath)
        indexes = {f.fullpath: j for j, f in enumerate(ordered)}
        subdirs = {f.fullpath: [] for f in ordered}
        for folder in ordered:
            if folder.path is not None and folder.path in subdirs:
                subdirs[folder.path].append(indexes[folder.fullpath])

        root_files = sum(len(f.files) for f in ordered)
        root_bytes = sum(f.size for f in ordered)
        total_files += root_files
        total_bytes += root_bytes
        total_dirs += len(ordered)
        all_errors.extend(errors)
        star_names.extend(stars)

        link_root = path_to_file_uri(root) if link_opt == "AUTO" else link_opt.replace("\\", "/")
        root_title = (os.path.basename(root) or root) + " 的快照" if multi else (user_title or root + " 的快照")
        metadata = json.dumps({
            "title": root_title,
            "timestamp": int(time.time()),
            "sourceDir": root,
            "linkRoot": link_root,
            "numFiles": root_files,
            "numDirs": len(ordered),
            "totBytes": root_bytes,
        }, ensure_ascii=False)

        data_lines.extend(build_data_lines(root, metadata, ordered, indexes, subdirs))

    if phase:
        phase("生成 HTML")
    page_title = user_title or (f"合并快照（{n} 个目录）" if multi else roots[0] + " 的快照")
    _render_and_write(outfile, page_title, data_lines, total_files, total_dirs, total_bytes, template_path)

    return {
        "outfile": outfile,
        "name": os.path.basename(outfile),
        "roots": n,
        "numDirs": total_dirs,
        "numFiles": total_files,
        "totBytes": total_bytes,
        "errors": [p for p, _ in all_errors[:50]],
        "errorCount": len(all_errors),
        "starNames": star_names[:10],
        "starCount": len(star_names),
    }


def merge_generated_files(source_files, opts, progress=None, phase=None):
    """合并多个 Snap2HTML（V2 数据格式）生成的 HTML 文件为一份多根清单。

    原理: 每个根目录的数据块 ID 独立从 0 开始（见 RedeMeDev.txt），
    因此把各来源的 p([...]) 行按顺序拼接即可，模板会自动处理多根展示。
    """
    if len(source_files) < 2:
        raise GenerationError("合并至少需要 2 个文件")
    template_path = find_template(opts.get("template"))
    opts["_multi"] = True
    outfile = _resolve_outfile("merged.html", opts, "merged.html")

    data_lines = []
    total_files = total_bytes = total_dirs = 0
    root_count = 0
    for i, path in enumerate(source_files):
        if phase:
            phase(f"读取来源 ({i + 1}/{len(source_files)})")
        try:
            with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
                text = fh.read()
        except OSError as ex:
            raise GenerationError(f"无法读取 {path}: {ex}")
        roots_in_file = 0
        for line in text.splitlines():
            s = line.strip()
            if not (s.startswith("p([") and s.endswith("])")):
                continue
            try:
                arr = json.loads(s[2:-1])
            except json.JSONDecodeError:
                raise GenerationError("数据行解析失败，文件可能不是 Snap2HTML V2 生成: " + path)
            if len(arr) >= 2 and arr[1] == -1:
                meta = arr[-1] if isinstance(arr[-1], dict) else None
                if not isinstance(meta, dict) or "numFiles" not in meta:
                    raise GenerationError("仅支持数据格式 V2（Snap2HTML 2.0+ 生成）: " + path)
                total_files += meta["numFiles"]
                total_dirs += meta["numDirs"]
                total_bytes += meta.get("totBytes", 0)
                root_count += 1
                roots_in_file += 1
            data_lines.append(s)
        if roots_in_file == 0:
            raise GenerationError("未在文件中找到根目录数据（非 Snap2HTML V2 文件？）: " + path)
        if progress:
            progress(path, root_count)

    if root_count < 2:
        raise GenerationError("合并结果至少需包含 2 个根目录（请检查来源文件是否有效）")
    if phase:
        phase("生成 HTML")
    title = opts.get("title") or f"合并快照（{root_count} 个目录）"
    _render_and_write(outfile, title, data_lines, total_files, total_dirs, total_bytes, template_path)

    return {
        "outfile": outfile,
        "name": os.path.basename(outfile),
        "roots": root_count,
        "sources": len(source_files),
        "numDirs": total_dirs,
        "numFiles": total_files,
        "totBytes": total_bytes,
        "errors": [],
        "errorCount": 0,
        "starNames": [],
        "starCount": 0,
    }


# ---------------------------------------------------------------------------
# Web 界面（--serve）：纯标准库实现，供无桌面服务器与多设备浏览器访问
# ---------------------------------------------------------------------------

WEB_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Snap2HTML · 快照工作台</title>
<style>
:root{
  --bg:#10151b; --panel:#161d26; --panel2:#1b232e; --line:#263140; --ink:#dbe4ee; --dim:#8494a7;
  --accent:#5aa7ff; --accent2:#2f6fd0; --ok:#3ecf8e; --warn:#ffb454; --err:#ff6b6b;
  --grid:rgba(255,255,255,.022); --glow:#1a2430; --input-bg:#0c1117; --hover:#1d2733;
  --code-ink:#a8b8ca; --mask:rgba(5,8,12,.72); --ring:rgba(90,167,255,.15);
  --mono:ui-monospace,"Cascadia Code",Consolas,"Courier New",monospace;
}
html[data-theme="light"]{
  --bg:#f3f5f8; --panel:#ffffff; --panel2:#f8fafc; --line:#d9e0e8; --ink:#1e2833; --dim:#64748b;
  --accent:#2f6fd0; --accent2:#1e57b8; --ok:#159d6b; --warn:#b97a12; --err:#d64545;
  --grid:rgba(15,23,42,.035); --glow:#e8eef6; --input-bg:#ffffff; --hover:#eef3f9;
  --code-ink:#44546a; --mask:rgba(15,23,42,.45); --ring:rgba(47,111,208,.15);
}
*{box-sizing:border-box;margin:0;padding:0}
body{
  background:
    repeating-linear-gradient(0deg,transparent 0 31px,var(--grid) 31px 32px),
    repeating-linear-gradient(90deg,transparent 0 31px,var(--grid) 31px 32px),
    radial-gradient(1200px 500px at 50% -10%,var(--glow) 0%,var(--bg) 60%);
  color:var(--ink);
  font:15px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
  min-height:100vh;
}
body,.card,input[type=text],.mrow,#modal,td,.ver,.mcrumb{
  transition:background-color .25s ease,color .25s ease,border-color .25s ease;
}
.wrap{max-width:760px;margin:0 auto;padding:40px 20px 60px}
header{display:flex;align-items:baseline;gap:12px;margin-bottom:6px}
.logo{font:700 20px/1 var(--mono);letter-spacing:.12em}
.logo b{color:var(--accent)}
.cursor{display:inline-block;width:10px;height:19px;background:var(--accent);vertical-align:-2px;animation:blink 1.1s steps(1) infinite}
@keyframes blink{50%{opacity:0}}
.ver{font:12px var(--mono);color:var(--dim);border:1px solid var(--line);padding:2px 9px;border-radius:99px}
.sub{color:var(--dim);font-size:13px;margin-bottom:26px}
.card{background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--line);
  border-radius:12px;padding:22px;margin-bottom:18px;box-shadow:0 8px 30px rgba(0,0,0,.25);
  opacity:0;animation:up .5s ease forwards}
.card:nth-of-type(1){animation-delay:.05s}.card:nth-of-type(2){animation-delay:.15s}.card:nth-of-type(3){animation-delay:.25s}
@keyframes up{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
h2{font-size:13px;font-weight:600;letter-spacing:.14em;color:var(--dim);margin-bottom:14px}
label{display:block;font-size:13px;color:var(--dim);margin:12px 0 5px}
input[type=text]{width:100%;background:var(--input-bg);border:1px solid var(--line);border-radius:8px;color:var(--ink);
  font:14px var(--mono);padding:10px 12px;outline:none;transition:border .15s, box-shadow .15s}
input[type=text]:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--ring)}
select.appselect{width:100%;background:var(--input-bg);border:1px solid var(--line);border-radius:8px;color:var(--ink);
  font:14px var(--mono);padding:10px 12px;outline:none;transition:border .15s, box-shadow .15s}
select.appselect:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--ring)}
.checks{display:flex;flex-wrap:wrap;gap:8px 18px;margin-top:14px}
.checks label{display:flex;align-items:center;gap:7px;margin:0;color:var(--ink);font-size:14px;cursor:pointer}
input[type=checkbox]{accent-color:var(--accent);width:15px;height:15px}
button{width:100%;margin-top:18px;background:linear-gradient(180deg,var(--accent),var(--accent2));color:#fff;
  border:0;border-radius:8px;padding:12px;font:600 15px/1.4 inherit;letter-spacing:.06em;cursor:pointer;
  transition:transform .12s, filter .12s}
button:hover{filter:brightness(1.08)} button:active{transform:translateY(1px)}
button:disabled{opacity:.5;cursor:not-allowed}
#log{display:none;margin-top:14px;font:13px var(--mono)}
#log .row{display:flex;align-items:center;gap:10px;padding:4px 0}
.spin{width:14px;height:14px;border:2px solid var(--line);border-top-color:var(--accent);border-radius:50%;
  animation:rot .8s linear infinite;flex:none}
@keyframes rot{to{transform:rotate(360deg)}}
#cur{color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0}
.pill{display:inline-block;font:600 12px var(--mono);padding:2px 10px;border-radius:99px;flex:none}
.pill.run{color:var(--warn);background:rgba(255,180,84,.12)}
.pill.ok{color:var(--ok);background:rgba(62,207,142,.12)}
.pill.err{color:var(--err);background:rgba(255,107,107,.12)}
#cnt{color:var(--dim)}
#msg{margin-top:10px;font:13px var(--mono);color:var(--err);word-break:break-all;display:none}
#done{margin-top:14px;display:none;font:13px var(--mono);color:var(--dim)}
#done a{color:var(--accent);text-decoration:none;font-weight:600}
#done a:hover{text-decoration:underline}
table{width:100%;border-collapse:collapse;font:13px var(--mono)}
td{padding:7px 4px;border-top:1px solid var(--line);vertical-align:bottom}
tr:first-child td{border-top:0}
td.n{word-break:break-all} td.n a{color:var(--ink);text-decoration:none} td.n a:hover{color:var(--accent)}
td.s{color:var(--dim);text-align:right;white-space:nowrap}
td.t{color:var(--dim);white-space:nowrap}
td.a{white-space:nowrap;text-align:right}
td.a a{color:var(--accent);text-decoration:none;margin-left:12px}
td.a a:hover{text-decoration:underline}
.empty{color:var(--dim);font-size:13px}
.pathrow{display:flex;gap:8px}
.pathrow input{flex:1;min-width:0}
#rows .pathrow{margin-bottom:8px}
button.wide{width:100%}
.hint{font-size:12.5px;color:var(--dim);margin-bottom:12px;line-height:1.8}
.hint code{font-family:var(--mono);color:var(--code-ink);background:var(--input-bg);padding:1px 6px;border-radius:5px;border:1px solid var(--line)}
.btnrow{display:flex;gap:8px}
.btnrow button{width:auto;flex:1;margin-top:0}
#upstat{font:12.5px var(--mono);color:var(--dim);margin-top:10px;word-break:break-all;min-height:16px}
td.c{width:26px;text-align:center}
td.c input{accent-color:var(--accent)}
#themeBtn,#langBtn,#githubBtn{width:auto;margin-top:0;align-self:center;padding:5px 12px;font-size:15px;
  line-height:1.3;background:transparent;border:1px solid var(--line);color:var(--dim);letter-spacing:0}
#themeBtn:hover,#langBtn:hover,#githubBtn:hover{color:var(--accent);border-color:var(--accent)}
#githubBtn{margin-left:auto;margin-right:8px;border-radius:50%;width:31px;height:31px;padding:0;
  display:inline-flex;align-items:center;justify-content:center;text-decoration:none}
#langBtn{margin-left:0;margin-right:8px;font:600 12px var(--mono)}
#themeBtn{margin-left:0}
button.ghost{width:auto;margin-top:0;padding:10px 14px;background:var(--input-bg);border:1px solid var(--line);
  color:var(--ink);font:13px var(--mono);letter-spacing:0}
button.ghost:hover{border-color:var(--accent);color:var(--accent)}
#mask{position:fixed;inset:0;background:var(--mask);backdrop-filter:blur(2px);z-index:50;
  display:flex;align-items:center;justify-content:center;padding:20px}
#mask[hidden]{display:none}
#modal{width:580px;max-width:100%;max-height:80vh;display:flex;flex-direction:column;background:var(--panel);
  border:1px solid var(--line);border-radius:12px;box-shadow:0 20px 60px rgba(0,0,0,.5);overflow:hidden;
  opacity:0;animation:up .25s ease forwards}
.mhead{display:flex;align-items:center;justify-content:space-between;padding:13px 16px;border-bottom:1px solid var(--line)}
.mhead b{font-size:14px;letter-spacing:.08em}
#mclose{width:auto;margin-top:0;padding:2px 11px;background:transparent;border:1px solid var(--line);color:var(--dim);font:14px var(--mono)}
#mclose:hover{color:var(--err);border-color:var(--err)}
.mcrumb{font:13px var(--mono);color:var(--accent);padding:10px 16px;background:var(--input-bg);border-bottom:1px solid var(--line);word-break:break-all;min-height:39px}
.mquick{display:flex;gap:8px;padding:10px 16px 0}
.mquick button{width:auto;margin-top:0;padding:5px 12px;font:12px var(--mono);background:transparent;border:1px solid var(--line);color:var(--dim)}
.mquick button:hover{color:var(--accent);border-color:var(--accent)}
.mlist{overflow:auto;flex:1;min-height:170px}
.mrow{display:flex;align-items:center;gap:9px;padding:8px 16px;cursor:pointer;font:13px var(--mono);border-bottom:1px solid rgba(38,49,64,.5)}
.mrow:hover{background:var(--hover);color:var(--accent)}
.mrow .ico{flex:none;opacity:.85}
.mfoot{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:12px 16px;border-top:1px solid var(--line)}
.mfoot .btns{display:flex;gap:8px}
.mfoot button{width:auto;margin-top:0;padding:9px 14px;font-size:13px}
#mpick{background:linear-gradient(180deg,var(--accent),var(--accent2))}
#mpick:disabled,#mup:disabled{opacity:.4;cursor:not-allowed}
.merr{font:12.5px var(--mono);color:var(--err);word-break:break-all}
footer{color:var(--dim);font-size:12.5px;margin-top:26px;line-height:1.9}
footer code{font-family:var(--mono);color:var(--code-ink);background:var(--input-bg);padding:1px 6px;border-radius:5px;border:1px solid var(--line)}
</style>
<script>
/* 主题早期初始化: #light/#dark 深链 > localStorage > 系统偏好（避免首帧闪烁） */
try{
  var m = location.hash.match(/^#(light|dark)$/);
  var t = m ? m[1] : (localStorage.getItem("s2h-theme")
          || (window.matchMedia && matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"));
  document.documentElement.setAttribute("data-theme", t);
}catch(e){}
try{
  var l = location.hash.match(/^#(en|zh)$/);
  var ll = l ? l[1] : localStorage.getItem("s2h-lang");
  window.__lang = (ll === "en" || ll === "zh") ? ll : "zh";
}catch(e){ window.__lang = "zh"; }
</script>
</head>
<body>
<div class="wrap">
  <header>
    <span class="logo">SNAP<b>2</b>HTML</span><span class="cursor"></span>
    <span class="ver" data-i18n="ver">跨平台版 2.52 · web</span>
    <a id="githubBtn" href="https://github.com/isdoge/Snap2HTML-py" target="_blank" rel="noopener"
       title="GitHub · isdoge/Snap2HTML-py" aria-label="GitHub">
      <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.012 8.012 0 0 0 16 8c0-4.42-3.58-8-8-8z"/></svg>
    </a>
    <button type="button" id="langBtn" title="Switch to English">EN</button>
    <button type="button" id="themeBtn" title="切换主题">☀️</button>
  </header>
  <p class="sub" data-i18n="sub">把这台机器上的任意目录变成单个可搜索的 HTML 清单——在这里发起，用任何设备的浏览器查看。</p>

  <div class="card">
    <h2 data-i18n="genTitle">生成快照</h2>
    <form id="f">
      <label data-i18n="scanLabel">扫描目录（服务器上的路径；可添加多个目录，合并为一份清单）</label>
      <div id="rows"></div>
      <button type="button" id="addrow" class="ghost wide" data-i18n="addRow">＋ 添加目录（多目录将合并生成）</button>
      <label for="title" data-i18n="titleLabel">页面标题（留空自动生成）</label>
      <input type="text" id="title" autocomplete="off">
      <div class="checks">
        <label><input type="checkbox" id="hidden"><span data-i18n="hidden">包含隐藏文件</span></label>
        <label><input type="checkbox" id="system"><span data-i18n="system">包含系统文件</span></label>
        <label><input type="checkbox" id="follow"><span data-i18n="follow">跟随符号链接</span></label>
        <label><input type="checkbox" id="link" checked><span data-i18n="link">生成文件链接</span></label>
      </div>
      <button id="go" type="submit" data-i18n="go">生成快照</button>
    </form>
  </div>

  <div class="card">
    <h2 data-i18n="statusTitle">任务状态</h2>
    <div id="log">
      <div class="row"><span class="spin"></span><span id="phase" class="pill run" data-i18n="preparing">准备</span><span id="cur"></span></div>
      <div class="row"><span id="cnt" data-i18n="cntZero">目录 0</span></div>
    </div>
    <div id="done"></div>
    <div id="msg"></div>
  </div>

  <div class="card">
    <h2 data-i18n="mergeTitle">合并已有文件</h2>
    <p class="hint" data-i18n="mergeHint">把多份 Snap2HTML（V2 格式）清单合并为一份多目录清单：在下方“输出文件”勾选 2 个以上后点“合并所选”；本机上的其他清单文件可先上传到服务器。命令行等价写法：<code>python snap2html.py --merge a.html b.html</code></p>
    <input type="file" id="upfile" multiple accept=".html" hidden>
    <div class="btnrow">
      <button type="button" id="uploadBtn" class="ghost" data-i18n="uploadBtn">⬆ 上传本机清单文件</button>
      <button type="button" id="mergeBtn" data-i18n="mergeBtn">合并所选（≥2）</button>
    </div>
    <div id="upstat"></div>
  </div>

  <div class="card">
    <h2 data-i18n="filesTitle">输出文件（勾选 ≥2 个可用于合并）</h2>
    <div id="files" class="empty">加载中…</div>
  </div>

  <footer>
    <span data-i18n="footer1">服务由 <code>python snap2html.py --serve</code> 启动，默认仅监听本机回环地址；
    加 <code>--host 0.0.0.0</code> 可让局域网设备访问。</span><br>
    <span data-i18n="footer2">⚠ 本服务无鉴权：开放端口后，同一网络内的人可扫描这台机器上的任意目录。
    “生成文件链接”指向的是<b>扫描机</b>上的本地路径，跨设备查看时请改用本页的查看/下载。</span>
  </footer>
</div>

<div id="mask" hidden>
  <div id="modal">
    <div class="mhead"><b data-i18n="modalTitle">选择服务器上的目录</b><button type="button" id="mclose">✕</button></div>
    <div class="mcrumb" id="mpath"></div>
    <div class="mquick" id="mquick"></div>
    <div class="mlist" id="mlist"></div>
    <div class="mfoot">
      <span class="merr" id="merr"></span>
      <div class="btns">
        <button type="button" id="mup" data-i18n="up">上级</button>
        <button type="button" id="mpick" data-i18n="pick">选择当前目录</button>
      </div>
    </div>
  </div>
</div>

<script>
var $ = function(s){ return document.querySelector(s); };
var timer = null;

/* --- 中英双语 --- */
var lang = window.__lang || "zh";
var I18N = {
  ver:        {zh:"跨平台版 2.52 · web", en:"Cross-platform 2.52 · web"},
  sub:        {zh:"把这台机器上的任意目录变成单个可搜索的 HTML 清单——在这里发起，用任何设备的浏览器查看。",
               en:"Turn any directory on this machine into a single searchable HTML listing — start here, view from any device's browser."},
  genTitle:   {zh:"生成快照", en:"Generate Snapshot"},
  scanLabel:  {zh:"扫描目录（服务器上的路径；可添加多个目录，合并为一份清单）",
               en:"Directories to scan (paths on the server; add multiple to merge into one listing)"},
  rowPh:      {zh:"/home/user/docs 或 D:\\data", en:"/home/user/docs or D:\\data"},
  browse:     {zh:"浏览…", en:"Browse…"},
  rmTitle:    {zh:"移除此目录", en:"Remove this directory"},
  addRow:     {zh:"＋ 添加目录（多目录将合并生成）", en:"＋ Add directory (multiple dirs will be merged)"},
  titleLabel: {zh:"页面标题（留空自动生成）", en:"Page title (auto-generated if empty)"},
  hidden:     {zh:"包含隐藏文件", en:"Include hidden files"},
  system:     {zh:"包含系统文件", en:"Include system items"},
  follow:     {zh:"跟随符号链接", en:"Follow symlinks"},
  link:       {zh:"生成文件链接", en:"Generate file links"},
  go:         {zh:"生成快照", en:"Generate Snapshot"},
  statusTitle:{zh:"任务状态", en:"Task Status"},
  preparing:  {zh:"准备", en:"Preparing"},
  cntZero:    {zh:"目录 0", en:"Dirs: 0"},
  dirsCount:  {zh:"目录 {n}", en:"Dirs: {n}"},
  phDone:     {zh:"完成", en:"Done"},
  viewResult: {zh:"查看结果", en:"View result"},
  view:       {zh:"查看", en:"View"},
  download:   {zh:"下载", en:"Download"},
  dirsFiles:  {zh:"{d} 目录 / {f} 文件 / {s}", en:"{d} dirs / {f} files / {s}"},
  mergeTitle: {zh:"合并已有文件", en:"Merge Existing Files"},
  mergeHint:  {zh:"把多份 Snap2HTML（V2 格式）清单合并为一份多目录清单：在下方“输出文件”勾选 2 个以上后点“合并所选”；本机上的其他清单文件可先上传到服务器。命令行等价写法：<code>python snap2html.py --merge a.html b.html</code>",
               en:"Merge several Snap2HTML (V2) listings into one multi-root listing: check 2+ files under \u201cOutput Files\u201d below and click \u201cMerge Selected\u201d; listings from other machines can be uploaded to the server first. CLI equivalent: <code>python snap2html.py --merge a.html b.html</code>"},
  uploadBtn:  {zh:"⬆ 上传本机清单文件", en:"⬆ Upload listings from this device"},
  mergeBtn:   {zh:"合并所选（≥2）", en:"Merge Selected (≥2)"},
  filesTitle: {zh:"输出文件（勾选 ≥2 个可用于合并）", en:"Output Files (check ≥2 to merge)"},
  filesEmpty: {zh:"还没有生成或上传任何文件", en:"No files generated or uploaded yet"},
  uploaded:   {zh:"上传", en:"Uploaded"},
  modalTitle: {zh:"选择服务器上的目录", en:"Choose a directory on the server"},
  qRoot:      {zh:"磁盘 / 根目录", en:"Drives / Root"},
  qHome:      {zh:"主目录", en:"Home"},
  up:         {zh:"上级", en:"Up"},
  pick:       {zh:"选择当前目录", en:"Select This Directory"},
  thisPc:     {zh:"此电脑（点击选择磁盘）", en:"This PC (click to choose a drive)"},
  loading:    {zh:"加载中…", en:"Loading…"},
  emptyDir:   {zh:"（空目录）", en:"(empty directory)"},
  browseErr:  {zh:"无法读取该目录", en:"Cannot read this directory"},
  reqFail:    {zh:"请求失败: ", en:"Request failed: "},
  uploading:  {zh:"上传中 ({i}/{n}): {name}", en:"Uploading ({i}/{n}): {name}"},
  upDone:     {zh:"✓ 上传完成（{n} 个文件）", en:"✓ Upload finished ({n} files)"},
  upFail:     {zh:"✗ 上传失败: ", en:"✗ Upload failed: "},
  mergeFail:  {zh:"合并失败", en:"Merge failed"},
  reqFail2:   {zh:"请求失败", en:"Request failed"},
  unknownErr: {zh:"未知错误", en:"Unknown error"},
  errNoRoot:  {zh:"✗ 请至少填写一个扫描目录", en:"✗ Enter at least one directory to scan"},
  errPick2:   {zh:"✗ 请在输出文件列表勾选至少 2 个文件再合并", en:"✗ Check at least 2 files in the output list to merge"},
  errConn:    {zh:"✗ 无法连接服务: ", en:"✗ Cannot reach the server: "},
  themeToDark:{zh:"切换到深色主题", en:"Switch to dark theme"},
  themeToLight:{zh:"切换到浅色主题", en:"Switch to light theme"},
  githubTitle:{zh:"GitHub 仓库：isdoge/Snap2HTML-py", en:"GitHub repository: isdoge/Snap2HTML-py"},
  footer1:    {zh:"服务由 <code>python snap2html.py --serve</code> 启动，默认仅监听本机回环地址；加 <code>--host 0.0.0.0</code> 可让局域网设备访问。",
               en:"The service is started with <code>python snap2html.py --serve</code> and listens on localhost only by default; add <code>--host 0.0.0.0</code> to expose it to your LAN."},
  footer2:    {zh:"⚠ 本服务无鉴权：开放端口后，同一网络内的人可扫描这台机器上的任意目录。“生成文件链接”指向的是<b>扫描机</b>上的本地路径，跨设备查看时请改用本页的查看/下载。",
               en:"⚠ This service has no authentication: once exposed, anyone on the same network can scan arbitrary directories on this machine. \u201cGenerate file links\u201d point to local paths on the <b>scanning machine</b>; use this page's View/Download when viewing from other devices."}
};
/* 服务端中文消息的英文映射（整句 + 前缀） */
var SRV_EXACT = {
  "已有任务在运行，请稍候": "A task is already running, please wait",
  "请至少选择 2 个要合并的文件": "Select at least 2 files to merge",
  "请至少填写一个扫描目录": "Enter at least one directory to scan",
  "文件不存在": "File not found",
  "仅支持上传 .html 清单文件": "Only .html listings can be uploaded",
  "文件为空或超过 512MB 上限": "File is empty or exceeds the 512MB limit",
  "上传失败": "Upload failed"
};
var SRV_PREFIX = [
  ["目录不存在: ", "Directory does not exist: "],
  ["文件不存在: ", "File not found: "],
  ["无法读取目录: ", "Cannot read directory: "],
  ["保存失败: ", "Failed to save: "],
  ["请求无效: ", "Invalid request: "]
];
function t(k){ var e = I18N[k]; return e ? e[lang] : k; }
function tr(msg){
  if(lang !== "en" || !msg) return msg;
  if(SRV_EXACT[msg]) return SRV_EXACT[msg];
  for(var i = 0; i < SRV_PREFIX.length; i++){
    if(msg.indexOf(SRV_PREFIX[i][0]) === 0) return SRV_PREFIX[i][1] + msg.substring(SRV_PREFIX[i][0].length);
  }
  return msg;
}
function tPhase(p){
  if(lang !== "en" || !p) return p;
  var m = p.match(/^扫描目录 \((\d+)\/(\d+)\)$/);
  if(m) return "Scanning (" + m[1] + "/" + m[2] + ")";
  var map = { "准备":"Preparing", "扫描目录":"Scanning", "排序与统计":"Sorting & counting",
              "生成 HTML":"Generating HTML", "完成":"Done" };
  return map[p] || p;
}
function applyLang(){
  var els = document.querySelectorAll("[data-i18n]");
  for(var i = 0; i < els.length; i++){
    var e = I18N[els[i].getAttribute("data-i18n")];
    if(e) els[i].innerHTML = e[lang];
  }
  var phs = document.querySelectorAll("[data-i18n-ph]");
  for(var j = 0; j < phs.length; j++){
    var e2 = I18N[phs[j].getAttribute("data-i18n-ph")];
    if(e2) phs[j].setAttribute("placeholder", e2[lang]);
  }
  var pins = document.querySelectorAll("#rows .pathin");
  for(var k = 0; k < pins.length; k++){ pins[k].setAttribute("placeholder", t("rowPh")); }
  syncThemeBtn();
  $("#langBtn").textContent = lang === "zh" ? "EN" : "中文";
  $("#langBtn").title = lang === "zh" ? "Switch to English" : "切换到中文";
  var gh = t("githubTitle");
  $("#githubBtn").title = gh;
  $("#githubBtn").setAttribute("aria-label", gh);
}
$("#langBtn").addEventListener("click", function(){
  lang = lang === "zh" ? "en" : "zh";
  try{ localStorage.setItem("s2h-lang", lang); }catch(e){}
  applyLang();
  loadFiles();
  fetch("/api/status").then(function(r){ return r.json(); }).then(render).catch(function(){});
});

/* --- 主题切换 --- */
function curTheme(){
  return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
}
function syncThemeBtn(){
  var light = curTheme() === "light";
  $("#themeBtn").textContent = light ? "🌙" : "☀️";
  $("#themeBtn").title = light ? t("themeToDark") : t("themeToLight");
}
$("#themeBtn").addEventListener("click", function(){
  var th = curTheme() === "light" ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", th);
  try{ localStorage.setItem("s2h-theme", th); }catch(e){}
  syncThemeBtn();
});
syncThemeBtn();
applyLang();

function esc(s){
  return String(s).replace(/[&<>"]/g, function(c){
    return {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c];
  });
}

function renderFiles(list){
  var box = $("#files");
  if(!list.length){ box.innerHTML = '<span class="empty">' + t("filesEmpty") + '</span>'; return; }
  var rows = "";
  for(var i = 0; i < list.length; i++){
    var f = list[i];
    var tag = f.dir === "uploads" ? ' <span class="pill run">' + t("uploaded") + '</span>' : '';
    rows += '<tr><td class="c"><input type="checkbox" class="pick" data-name="' + encodeURIComponent(f.name) + '"></td>' +
            '<td class="n"><a href="/view?name=' + encodeURIComponent(f.name) +
            '" target="_blank">' + esc(f.name) + '</a>' + tag + '</td><td class="s">' + esc(f.size) +
            '</td><td class="t">' + esc(f.mtime) +
            '</td><td class="a"><a href="/view?name=' + encodeURIComponent(f.name) +
            '" target="_blank">' + t("view") + '</a><a href="/download?name=' + encodeURIComponent(f.name) +
            '">' + t("download") + '</a></td></tr>';
  }
  box.innerHTML = '<table>' + rows + '</table>';
}

function loadFiles(){
  fetch("/api/files").then(function(r){ return r.json(); }).then(function(j){
    renderFiles(j.files || []);
  }).catch(function(){});
}

function stopPoll(){ if(timer){ clearInterval(timer); timer = null; } }

function render(s){
  if(!s) return;
  var running = s.status === "running";
  $("#log").style.display = running ? "block" : "none";
  $("#go").disabled = running;
  $("#mergeBtn").disabled = running;
  $("#uploadBtn").disabled = running;
  if(running){
    $("#phase").textContent = s.phase ? tPhase(s.phase) : t("preparing");
    $("#cur").textContent = s.current || "";
    $("#cnt").textContent = t("dirsCount").replace("{n}", s.dirs || 0);
  }
  if(s.status === "done" && s.last){
    stopPoll();
    var r = s.last;
    $("#done").innerHTML = '<span class="pill ok">' + t("phDone") + '</span> <a href="/view?name=' +
      encodeURIComponent(r.name) + '" target="_blank">' + t("viewResult") + '</a> · <a href="/download?name=' +
      encodeURIComponent(r.name) + '">' + t("download") + '</a> — ' +
      t("dirsFiles").replace("{d}", esc(String(r.numDirs))).replace("{f}", esc(String(r.numFiles))).replace("{s}", esc(r.totSize));
    $("#done").style.display = "block";
    loadFiles();
  }
  if(s.status === "error"){
    stopPoll();
    var m = $("#msg");
    m.textContent = "✗ " + tr(s.error || t("unknownErr"));
    m.style.display = "block";
  }
}

function poll(){
  stopPoll();
  timer = setInterval(function(){
    fetch("/api/status").then(function(r){ return r.json(); }).then(render).catch(function(){});
  }, 500);
}

$("#f").addEventListener("submit", function(e){
  e.preventDefault();
  var roots = [];
  var inputs = document.querySelectorAll("#rows .pathin");
  for(var i = 0; i < inputs.length; i++){
    var v = inputs[i].value.trim();
    if(v) roots.push(v);
  }
  if(!roots.length){
    var m0 = $("#msg");
    m0.textContent = t("errNoRoot");
    m0.style.display = "block";
    return;
  }
  var body = {
    roots: roots,
    title: $("#title").value.trim(),
    hidden: $("#hidden").checked,
    system: $("#system").checked,
    follow: $("#follow").checked,
    link: $("#link").checked
  };
  $("#msg").style.display = "none";
  $("#done").style.display = "none";
  $("#go").disabled = true;
  fetch("/api/generate", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)
  }).then(function(r){ return r.json(); }).then(function(j){
    if(j.ok){ poll(); render({status:"running", phase:"准备"}); }
    else {
      $("#go").disabled = false;
      var m = $("#msg");
      m.textContent = "✗ " + (j.error ? tr(j.error) : t("reqFail2"));
      m.style.display = "block";
    }
  }).catch(function(ex){
    $("#go").disabled = false;
    var m = $("#msg");
    m.textContent = t("errConn") + ex;
    m.style.display = "block";
  });
});

loadFiles();
fetch("/api/status").then(function(r){ return r.json(); }).then(function(s){
  render(s);
  if(s && s.status === "running") poll();
}).catch(function(){});

/* --- 目录浏览器 --- */
var mcur = "", mparent = null;
var FOLDER = "📁 ";

var browseTarget = null;
function openBrowse(start, target){
  browseTarget = target || document.querySelector("#rows .pathin");
  $("#mask").hidden = false;
  $("#mquick").innerHTML =
    '<button type="button" id="qroot">' + t("qRoot") + '</button>' +
    '<button type="button" id="qhome">' + t("qHome") + '</button>';
  $("#qroot").onclick = function(){ navBrowse(""); };
  $("#qhome").onclick = function(){ navBrowse("~"); };
  navBrowse(start || (browseTarget ? browseTarget.value.trim() : "") || "");
}

function closeBrowse(){ $("#mask").hidden = true; }

function mrowHtml(name, ico){
  return '<div class="mrow" data-name="' + esc(name) + '"><span class="ico">' + ico +
         '</span><span>' + esc(name) + '</span></div>';
}

function joinPath(base, name){
  if(!base) return name;
  var sep = base.indexOf("\\") !== -1 ? "\\" : "/";
  return base.endsWith(sep) ? base + name : base + sep + name;
}

function navBrowse(p){
  $("#mlist").innerHTML = '<span class="empty" style="display:block;padding:16px">' + t("loading") + '</span>';
  $("#merr").textContent = "";
  fetch("/api/browse?path=" + encodeURIComponent(p)).then(function(r){
    return r.json();
  }).then(function(j){
    if(!j.ok){ $("#merr").textContent = tr(j.error) || t("browseErr"); return; }
    mcur = j.path || "";
    mparent = j.parent;
    $("#mpath").textContent = j.path ? j.path : t("thisPc");
    $("#mup").disabled = !j.parent;
    $("#mpick").disabled = !j.path;
    var htmlStr = "";
    var i;
    if(j.drives){
      for(i = 0; i < j.drives.length; i++){ htmlStr += mrowHtml(j.drives[i], "💾 "); }
    }
    for(i = 0; i < (j.dirs || []).length; i++){ htmlStr += mrowHtml(j.dirs[i], FOLDER); }
    $("#mlist").innerHTML = htmlStr || '<span class="empty" style="display:block;padding:16px">' + t("emptyDir") + '</span>';
    var items = $("#mlist").querySelectorAll(".mrow");
    function bindClick(el){
      el.addEventListener("click", function(){
        navBrowse(joinPath(mcur, el.getAttribute("data-name")));
      });
    }
    for(i = 0; i < items.length; i++){ bindClick(items[i]); }
  }).catch(function(e){ $("#merr").textContent = t("reqFail") + e; });
}

$("#mclose").addEventListener("click", closeBrowse);
$("#mask").addEventListener("click", function(e){ if(e.target === this) closeBrowse(); });
document.addEventListener("keydown", function(e){
  if(e.key === "Escape" && !$("#mask").hidden) closeBrowse();
});
$("#mup").addEventListener("click", function(){ if(mparent) navBrowse(mparent); });
$("#mpick").addEventListener("click", function(){
  if(mcur && browseTarget){ browseTarget.value = mcur; closeBrowse(); }
});

/* --- 多目录行 --- */
function addRow(value){
  var div = document.createElement("div");
  div.className = "pathrow";
  div.innerHTML = '<input type="text" class="pathin" placeholder="' + t("rowPh") + '" autocomplete="off">' +
                  '<button type="button" class="ghost br">' + t("browse") + '</button>' +
                  '<button type="button" class="ghost rm" title="' + t("rmTitle") + '">✕</button>';
  div.querySelector(".pathin").value = value || "";
  $("#rows").appendChild(div);
  syncRm();
  return div;
}
function syncRm(){
  var all = document.querySelectorAll("#rows .pathrow");
  for(var i = 0; i < all.length; i++){
    all[i].querySelector(".rm").style.display = all.length > 1 ? "" : "none";
  }
}
$("#rows").addEventListener("click", function(e){
  var row = e.target.closest(".pathrow");
  if(!row) return;
  if(e.target.classList.contains("br")){
    openBrowse(row.querySelector(".pathin").value.trim(), row.querySelector(".pathin"));
  } else if(e.target.classList.contains("rm")){
    row.remove();
    syncRm();
  }
});
$("#addrow").addEventListener("click", function(){ addRow(""); });
addRow();

/* --- 合并与上传 --- */
$("#uploadBtn").addEventListener("click", function(){ $("#upfile").click(); });
$("#upfile").addEventListener("change", function(){
  var list = this.files;
  if(!list || !list.length) return;
  var stat = $("#upstat");
  var i = 0;
  function next(){
    if(i >= list.length){
      stat.textContent = t("upDone").replace("{n}", list.length);
      loadFiles();
      return;
    }
    var f = list[i];
    stat.textContent = t("uploading").replace("{i}", i + 1).replace("{n}", list.length).replace("{name}", f.name);
    var fr = new FileReader();
    fr.onload = function(){
      fetch("/api/upload?name=" + encodeURIComponent(f.name), {
        method: "POST",
        headers: {"Content-Type": "text/plain"},
        body: fr.result
      }).then(function(r){ return r.json(); }).then(function(j){
        if(!j.ok){ stat.textContent = "✗ " + tr(j.error || "上传失败"); return; }
        i++;
        next();
      }).catch(function(e){ stat.textContent = t("upFail") + e; });
    };
    fr.readAsText(f, "utf-8");
  }
  next();
  this.value = "";
});

$("#mergeBtn").addEventListener("click", function(){
  var names = [];
  var boxes = document.querySelectorAll("#files .pick:checked");
  for(var i = 0; i < boxes.length; i++){ names.push(boxes[i].getAttribute("data-name")); }
  var m = $("#msg");
  if(names.length < 2){
    m.textContent = t("errPick2");
    m.style.display = "block";
    return;
  }
  m.style.display = "none";
  $("#done").style.display = "none";
  $("#mergeBtn").disabled = true;
  fetch("/api/merge", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({files: names})
  }).then(function(r){ return r.json(); }).then(function(j){
    if(j.ok){
      poll();
      render({status: "running", phase: "准备"});
    } else {
      m.textContent = "✗ " + (j.error ? tr(j.error) : t("mergeFail"));
      m.style.display = "block";
    }
    $("#mergeBtn").disabled = false;
  }).catch(function(e){
    m.textContent = t("errConn") + e;
    m.style.display = "block";
    $("#mergeBtn").disabled = false;
  });
});

/* 深链: /#browse 或 /#browse|<起始目录>，便于收藏常用入口 */
if(location.hash.indexOf("#browse") === 0){
  var hp = location.hash.split("|")[1];
  openBrowse(hp ? decodeURIComponent(hp) : "");
}
</script>
</body>
</html>
"""


def _lan_ip():
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))  # UDP connect 不发包，仅选路由
        return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        sock.close()


def _list_drives():
    import string
    return [letter + ":\\" for letter in string.ascii_uppercase if os.path.exists(letter + ":\\")]


def serve_web(host, port, opts):
    """启动 Web 界面服务（阻塞）。opts 需要 template / output_dir。"""
    find_template(opts.get("template"))  # 启动时快速失败
    output_dir = opts["output_dir"]
    os.makedirs(output_dir, exist_ok=True)
    uploads_dir = os.path.join(output_dir, "uploads")

    state = {"status": "idle", "phase": "", "current": "", "dirs": 0, "error": "", "last": None}
    slock = threading.Lock()
    busy = threading.Event()

    def worker(kind, payload):
        def prog(path, count):
            with slock:
                state["current"], state["dirs"] = path, count

        def ph(name):
            with slock:
                state["phase"] = name

        try:
            if kind == "merge":
                result = merge_generated_files(payload["files"], payload["opts"], phase=ph)
            else:
                result = generate_snapshot(payload, progress=prog, phase=ph)
            with slock:
                state.update(
                    status="done", phase="完成", error="",
                    last={
                        "name": result["name"],
                        "numDirs": result["numDirs"],
                        "numFiles": result["numFiles"],
                        "totSize": bytes_to_filesize(result["totBytes"]),
                        "time": time.strftime("%H:%M:%S"),
                    },
                )
        except Exception as ex:
            with slock:
                state.update(status="error", phase="", error=str(ex))
        finally:
            busy.clear()

    class Handler(BaseHTTPRequestHandler):
        server_version = "Snap2HTMLWeb/" + APP_VERSION

        def _send(self, code, body, ctype="application/json; charset=utf-8", disp=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if disp:
                self.send_header("Content-Disposition", disp)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

        def _browse_dir(self, path):
            parent = os.path.dirname(path)
            dirs = []
            try:
                with os.scandir(path) as it:
                    for entry in it:
                        try:
                            if entry.is_dir():
                                dirs.append(entry.name)
                        except OSError:
                            continue
            except OSError as ex:
                self._json(200, {"ok": False, "error": f"无法读取目录: {ex}"})
                return
            self._json(200, {
                "ok": True,
                "path": path,
                "parent": None if parent == path else parent,
                "dirs": natural_sorted(dirs, key=lambda s: s),
            })

        def do_GET(self):
            parsed = urlparse.urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                self._send(200, WEB_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif parsed.path == "/api/status":
                with slock:
                    self._json(200, state)
            elif parsed.path == "/api/files":
                items = []
                for folder, tag in ((output_dir, "output"), (uploads_dir, "uploads")):
                    try:
                        for name in os.listdir(folder):
                            path = os.path.join(folder, name)
                            if name.endswith(".html") and os.path.isfile(path):
                                st = os.stat(path)
                                items.append({
                                    "name": name,
                                    "dir": tag,
                                    "size": bytes_to_filesize(st.st_size),
                                    "mtime": time.strftime("%m-%d %H:%M", time.localtime(st.st_mtime)),
                                    "_ts": st.st_mtime,
                                })
                    except OSError:
                        pass
                items.sort(key=lambda i: i["_ts"], reverse=True)
                for item in items:
                    del item["_ts"]
                self._json(200, {"files": items})
            elif parsed.path == "/api/browse":
                qs = urlparse.parse_qs(parsed.query)
                raw = (qs.get("path") or [""])[0].strip()
                if not raw:
                    if IS_WINDOWS:
                        # 空路径: 返回磁盘列表作为浏览起点
                        self._json(200, {"ok": True, "path": "", "parent": None, "dirs": [], "drives": _list_drives()})
                    else:
                        self._browse_dir("/")
                elif raw == "~":
                    self._browse_dir(os.path.expanduser("~"))
                else:
                    path = os.path.abspath(raw)
                    if not os.path.isdir(path):
                        self._json(400, {"ok": False, "error": "目录不存在: " + raw})
                    else:
                        self._browse_dir(path)
            elif parsed.path in ("/view", "/download"):
                qs = urlparse.parse_qs(parsed.query)
                name = (qs.get("name") or [""])[0]
                # 只允许 output_dir / uploads_dir 下的纯文件名，杜绝路径穿越
                safe = name == os.path.basename(name) and name.endswith(".html")
                path = ""
                if safe:
                    for folder in (output_dir, uploads_dir):
                        candidate = os.path.join(folder, name)
                        if os.path.isfile(candidate):
                            path = candidate
                            break
                if path:
                    with open(path, "rb") as fh:
                        body = fh.read()
                    disp = None
                    if parsed.path == "/download":
                        disp = "attachment; filename*=UTF-8''" + urlparse.quote(name)
                    self._send(200, body, "text/html; charset=utf-8", disp)
                else:
                    self._json(404, {"ok": False, "error": "文件不存在"})
            else:
                self._json(404, {"ok": False, "error": "not found"})

        def _acquire_busy(self):
            if busy.is_set():
                self._json(409, {"ok": False, "error": "已有任务在运行，请稍候"})
                return False
            busy.set()
            with slock:
                state.update(status="running", phase="准备", current="", dirs=0, error="", last=None)
            return True

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > 65536:
                raise ValueError("请求体过大")
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def do_POST(self):
            route = urlparse.urlparse(self.path).path
            if route == "/api/generate":
                if busy.is_set():
                    self._json(409, {"ok": False, "error": "已有任务在运行，请稍候"})
                    return
                try:
                    job = self._read_json()
                except (ValueError, json.JSONDecodeError) as ex:
                    self._json(400, {"ok": False, "error": "请求无效: " + str(ex)})
                    return

                roots = [str(r).strip() for r in (job.get("roots") or []) if str(r).strip()]
                if not roots and str(job.get("root") or "").strip():
                    roots = [str(job["root"]).strip()]  # 兼容旧客户端
                valid = []
                for root in roots:
                    if not os.path.isdir(root):
                        self._json(400, {"ok": False, "error": "目录不存在: " + root})
                        return
                    valid.append(os.path.normpath(os.path.abspath(root)))
                if not valid:
                    self._json(400, {"ok": False, "error": "请至少填写一个扫描目录"})
                    return
                if not self._acquire_busy():
                    return

                # 安全: 输出文件名由服务端生成，不接受客户端路径
                if len(valid) > 1:
                    outfile = "merged-" + time.strftime("%Y%m%d-%H%M%S") + ".html"
                else:
                    base = os.path.basename(valid[0].rstrip("\\/")) or "snapshot"
                    outfile = re.sub(r'[<>:"/\\|?*]', "_", base) + "-" + time.strftime("%Y%m%d-%H%M%S") + ".html"

                task = {
                    "roots": valid,
                    "title": str(job.get("title") or "").strip() or None,
                    "outfile": outfile,
                    "output_dir": output_dir,
                    "template": opts.get("template"),
                    "hidden": bool(job.get("hidden")),
                    "system": bool(job.get("system")),
                    "follow": bool(job.get("follow")),
                    "link": "AUTO" if job.get("link") else "",
                }
                threading.Thread(target=worker, args=("generate", task), daemon=True).start()
                self._json(200, {"ok": True})

            elif route == "/api/merge":
                if busy.is_set():
                    self._json(409, {"ok": False, "error": "已有任务在运行，请稍候"})
                    return
                try:
                    job = self._read_json()
                except (ValueError, json.JSONDecodeError) as ex:
                    self._json(400, {"ok": False, "error": "请求无效: " + str(ex)})
                    return

                files = []
                for name in (job.get("files") or []):
                    name = str(name)
                    if name != os.path.basename(name) or not name.endswith(".html"):
                        self._json(400, {"ok": False, "error": "非法文件名: " + name})
                        return
                    for folder in (output_dir, uploads_dir):
                        candidate = os.path.join(folder, name)
                        if os.path.isfile(candidate):
                            files.append(candidate)
                            break
                    else:
                        self._json(400, {"ok": False, "error": "文件不存在: " + name})
                        return
                if len(files) < 2:
                    self._json(400, {"ok": False, "error": "请至少选择 2 个要合并的文件"})
                    return
                if not self._acquire_busy():
                    return

                mopts = {
                    "title": str(job.get("title") or "").strip() or None,
                    "outfile": "merged-" + time.strftime("%Y%m%d-%H%M%S") + ".html",
                    "output_dir": output_dir,
                    "template": opts.get("template"),
                }
                threading.Thread(target=worker, args=("merge", {"files": files, "opts": mopts}), daemon=True).start()
                self._json(200, {"ok": True})

            elif route == "/api/upload":
                qs = urlparse.parse_qs(urlparse.urlparse(self.path).query)
                name = (qs.get("name") or [""])[0]
                if name != os.path.basename(name) or not name.endswith(".html"):
                    self._json(400, {"ok": False, "error": "仅支持上传 .html 清单文件"})
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                if length <= 0 or length > 512 * 1024 * 1024:
                    self._json(400, {"ok": False, "error": "文件为空或超过 512MB 上限"})
                    return
                os.makedirs(uploads_dir, exist_ok=True)
                dest = os.path.join(uploads_dir, name)
                if os.path.exists(dest):
                    stem, dot = os.path.splitext(name)
                    dest = os.path.join(uploads_dir, stem + "-" + time.strftime("%Y%m%d-%H%M%S") + dot)
                try:
                    remaining = length
                    with open(dest, "wb") as fh:
                        while remaining > 0:
                            chunk = self.rfile.read(min(1024 * 1024, remaining))
                            if not chunk:
                                break
                            fh.write(chunk)
                            remaining -= len(chunk)
                except OSError as ex:
                    self._json(500, {"ok": False, "error": "保存失败: " + str(ex)})
                    return
                self._json(200, {"ok": True, "name": os.path.basename(dest), "dir": "uploads"})

            else:
                self._json(404, {"ok": False, "error": "not found"})

        def log_message(self, fmt, *args):
            if not self.path.startswith("/api/status"):  # 轮询太吵，不记日志
                sys.stderr.write("[web] %s\n" % (fmt % args))

    server = ThreadingHTTPServer((host, port), Handler)
    print("Snap2HTML Web 界面已启动", file=sys.stderr)
    print(f"  本机访问:   http://127.0.0.1:{port}/", file=sys.stderr)
    if host == "0.0.0.0":
        print(f"  局域网访问: http://{_lan_ip()}:{port}/", file=sys.stderr)
        print("  ⚠ 服务无鉴权，局域网内任何人都可以扫描本机目录", file=sys.stderr)
    elif host not in ("127.0.0.1", "::1", "localhost"):
        print(f"  访问地址:   http://{host}:{port}/", file=sys.stderr)
    else:
        print("  （如需局域网访问，加 --host 0.0.0.0 重新启动）", file=sys.stderr)
    print(f"  输出目录:   {output_dir}", file=sys.stderr)
    print("  按 Ctrl+C 停止", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止", file=sys.stderr)
    finally:
        server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="snap2html.py",
        description="Snap2HTML 跨平台版：把目录树生成单个可搜索的 HTML 文件清单（CLI 或 Web 界面）。",
    )
    parser.add_argument("roots", nargs="*", help="要生成快照的根目录（可多个，多目录合并为一份清单）")
    parser.add_argument("-path", "--path", dest="path", help="追加一个根目录（兼容 C# 版参数）")
    parser.add_argument("-merge", "--merge", dest="merge", nargs="+", metavar="HTML",
                        help="合并模式: 合并 2 个以上已生成的 Snap2HTML（V2）清单文件")
    parser.add_argument("-o", "-outfile", "-output", "--outfile", dest="outfile", help="输出 HTML 路径")
    parser.add_argument("-t", "-title", "--title", dest="title", help="页面标题")
    parser.add_argument("-link", "--link", dest="link", nargs="?", const="AUTO", default="",
                        help="文件链接的 URL 前缀；不带值时自动使用根目录的 file:// URI")
    parser.add_argument("-hidden", "--hidden", action="store_true", help="包含隐藏项")
    parser.add_argument("-system", "--system", action="store_true", help="包含系统项")
    parser.add_argument("--follow", action="store_true", help="跟随目录符号链接")
    parser.add_argument("-template", "--template", dest="template", help="模板文件路径")
    parser.add_argument("--open", action="store_true", help="生成后用浏览器打开")
    parser.add_argument("-q", "-silent", "--quiet", dest="quiet", action="store_true", help="安静模式")
    parser.add_argument("-serve", "--serve", action="store_true",
                        help="启动 Web 界面服务（适合无桌面服务器 / 多设备访问）")
    parser.add_argument("-host", "--host", dest="host", default="127.0.0.1",
                        help="Web 服务监听地址 (默认 127.0.0.1，0.0.0.0 = 局域网可访问)")
    parser.add_argument("-port", "--port", dest="port", type=int, default=8765, help="Web 服务端口 (默认 8765)")
    parser.add_argument("-output-dir", "--output-dir", dest="output_dir",
                        help="Web 模式的输出目录 (默认: 脚本同目录下 output/)")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except Exception:
        pass

    if args.serve:
        default_out = str(Path(__file__).resolve().parent / "output")
        serve_web(args.host, args.port, {
            "template": args.template,
            "output_dir": args.output_dir or default_out,
        })
        return

    if args.merge:
        if args.roots or args.path:
            sys.exit("错误: --merge 与目录参数不能同时使用")
        opts = {
            "title": args.title,
            "outfile": args.outfile,
            "template": args.template,
            "output_dir": None,
        }
        if not args.quiet:
            print(f"合并 {len(args.merge)} 个清单文件 ...", file=sys.stderr)

        def phase(name):
            if not args.quiet and sys.stderr.isatty():
                sys.stderr.write("\r" + " " * 130 + "\r" + name + "...\n")

        try:
            result = merge_generated_files(args.merge, opts, phase=phase)
        except GenerationError as ex:
            sys.exit(f"错误: {ex}")
    else:
        roots = list(args.roots)
        if args.path and args.path not in roots:
            roots.append(args.path)
        if not roots:
            parser.error("缺少目录参数（例如: python snap2html.py /some/dir [/another/dir ...]）")
        opts = {
            "roots": roots,
            "title": args.title,
            "outfile": args.outfile,
            "hidden": args.hidden,
            "system": args.system,
            "follow": args.follow,
            "link": args.link,
            "template": args.template,
            "output_dir": None,
        }
        if not args.quiet:
            shown = roots[0] if len(roots) == 1 else f"{len(roots)} 个目录"
            print(f"扫描 {shown} ...", file=sys.stderr)

        def progress(path, count):
            if not args.quiet and sys.stderr.isatty():
                sys.stderr.write(f"\r[{count}] 扫描中: " + path[:110] + " " * 6)
                sys.stderr.flush()

        def phase(name):
            if not args.quiet and sys.stderr.isatty():
                sys.stderr.write("\r" + " " * 130 + "\r" + name + "...\n")

        try:
            result = generate_snapshot(opts, progress=progress, phase=phase)
        except GenerationError as ex:
            sys.exit(f"错误: {ex}")

    if not args.quiet and sys.stderr.isatty():
        sys.stderr.write("\r" + " " * 130 + "\r")
    if not args.quiet:
        print(
            f"完成: {result['outfile']}\n"
            f"  目录: {result['numDirs']}  文件: {result['numFiles']}  "
            f"总大小: {bytes_to_filesize(result['totBytes'])}",
            file=sys.stderr,
        )
        if result["starCount"]:
            print(f"警告: {result['starCount']} 个名称含 \"*\"，在清单中无法正确解析（与 C# 版相同）:", file=sys.stderr)
            for path in result["starNames"]:
                print("  " + path, file=sys.stderr)
        if result["errorCount"]:
            print(f"警告: {result['errorCount']} 个文件夹无法读取:", file=sys.stderr)
            for path in result["errors"]:
                print("  " + path, file=sys.stderr)
            if result["errorCount"] > len(result["errors"]):
                print(f"  ... 以及另外 {result['errorCount'] - len(result['errors'])} 个", file=sys.stderr)

    if args.open:
        webbrowser.open(Path(result["outfile"]).resolve().as_uri())


if __name__ == "__main__":
    main()
