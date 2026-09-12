#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""格式转换工作台 —— 主程序。

用法::

    python main.py                      # 打开界面（拖文件进去就能用）
    python main.py --list               # 看支持哪些转换
    python main.py a.png --to jpg       # 命令行转换
    python main.py a.pdf --to png --action page --opt dpi=300
    python main.py 一批文件/ --to pdf   # 传目录也行，会递归找

界面在 127.0.0.1 上跑，带一次性访问令牌，不对外网开放。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 让 `python main.py` 无论从哪个工作目录启动都能 import 到 conv 和 ncm2mp3
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def _ensure_std_streams() -> None:
    """--windowed 打包后 stdout/stderr 可能是 None，任何 print 都会直接崩。"""
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except OSError:
                pass
    if getattr(sys, "stdin", None) is None:
        try:
            sys.stdin = open(os.devnull, "r", encoding="utf-8")
        except OSError:
            pass


def collect(paths: list[str]) -> list[str]:
    """把目录展开成文件列表（递归），保留显式给出的文件。"""
    out: list[str] = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            for f in sorted(path.rglob("*")):
                if f.is_file() and not f.name.startswith("."):
                    out.append(str(f))
        elif path.is_file():
            out.append(str(path))
        else:
            out.append(str(path))        # 交给下游报"文件不存在"，错误信息更准确
    return out


def print_capabilities() -> int:
    from conv.core.registry import get_registry

    reg = get_registry()
    print("格式转换工作台 · 支持的能力")
    print("=" * 62)

    by_src: dict[str, list] = {}
    for c in reg.converters:
        ok, why = c.available()
        for cap in c.capabilities():
            by_src.setdefault(cap.src, []).append((cap, c, ok, why))

    if not by_src:
        print("（没有加载到任何插件）")
        return 1

    for src in sorted(by_src):
        rows = by_src[src]
        print(f"\n  .{src.upper()}")
        for cap, c, ok, why in rows:
            mark = "  " if ok else "× "
            note = "" if ok else f"   ← {why}"
            print(f"    {mark}{cap.text}{note}")

    print()
    print("=" * 62)
    print(f"插件：{', '.join(c.id for c in reg.converters)}")
    if reg.errors:
        print(f"加载失败的插件：{reg.errors}")
    return 0


def run_cli(paths: list[str], dst: str, out: str | None,
            action: str | None, opts: dict) -> int:
    from conv.core.registry import get_registry
    from conv.core.result import summary

    reg = get_registry()
    files = collect(paths)
    if not files:
        print("没有找到要转换的文件")
        return 2

    out_dir = out or str(Path(files[0]).resolve().parent)
    results = reg.run(files, dst, out_dir, opts, action)

    for r in results:
        if r.ok:
            print(f"  [完成] {r.src.name if r.src else ''} → {r.name}"
                  f"  ({r.size / 1048576:.1f}MB)")
        else:
            print(f"  [失败] {r.src.name if r.src else ''}  {r.msg}")

    print()
    print(summary(results))
    print(f"输出目录：{out_dir}")
    return 0 if all(r.ok for r in results) else 1


def main(argv: list[str] | None = None) -> int:
    _ensure_std_streams()

    ap = argparse.ArgumentParser(
        prog="格式转换工作台",
        description="图片 / PDF / NCM 格式互转。不带参数就打开界面。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("paths", nargs="*", help="要转换的文件或目录（不填则打开界面）")
    ap.add_argument("-t", "--to", help="目标格式，如 jpg / png / pdf")
    ap.add_argument("-o", "--out", help="输出目录（默认与源文件同目录）")
    ap.add_argument("--action", help="同格式多操作时指定：merge / split / extract / "
                                     "rotate / encrypt / decrypt / page / long")
    ap.add_argument("--opt", action="append", metavar="K=V",
                    help="选项，可重复。如 --opt dpi=300 --opt pages=1-3")
    ap.add_argument("--list", action="store_true", help="列出所有支持的能力")
    ap.add_argument("--port", type=int, default=0, help="界面端口（0 = 自动）")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开界面")
    ap.add_argument("--no-app", action="store_true",
                    help="用普通浏览器标签打开，不用独立窗口")
    ap.add_argument("--out-dir", help="界面里「输出到」的默认目录")
    args = ap.parse_args(argv)

    if args.list:
        return print_capabilities()

    opts: dict = {}
    for kv in (args.opt or []):
        k, _, v = kv.partition("=")
        if k.strip():
            opts[k.strip()] = v.strip()

    if args.paths and args.to:
        return run_cli(args.paths, args.to, args.out, args.action, opts)

    if args.paths and not args.to:
        print("给了文件但没给目标格式。用 --to jpg 指定，或干脆不带参数打开界面。")
        print("想看看能转成什么：python main.py --list")
        return 2

    from conv.ui.server import serve
    return serve(port=args.port,
                 open_browser=not args.no_browser,
                 out_hint=args.out_dir,
                 app_mode=not args.no_app)


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()       # 打包成 exe 后必须，否则子进程会递归开自己
    sys.exit(main())
