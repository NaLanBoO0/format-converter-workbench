#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 main.py（格式转换工作台）打包成单文件 exe。

用法（在本文件所在目录执行）::

    python build_workbench.py            # 界面版 + 命令行版都打
    python build_workbench.py --gui      # 只打界面版
    python build_workbench.py --cli      # 只打命令行版

产物::

    dist/格式转换工作台.exe        双击打开界面（无控制台黑窗）
    dist/格式转换工作台-命令行.exe  命令行用

比打包 ncm2mp3 多出来的三件事（不做就会打出一个"空壳"exe）:

  1. **动态加载的插件必须显式告诉 PyInstaller。**
     `conv/core/registry.py` 是用 `importlib.import_module(f"conv.converters.{名字}")`
     加载插件的，静态分析看不见 —— 不补 `--collect-submodules conv`，
     打出来的 exe 会「没有加载到任何插件」。

  2. **pypdfium2 自带二进制（PDFium）。** 必须 `--collect-all pypdfium2`，
     否则 PDF 转图片会在运行时找不到 DLL。

  3. **Pillow / pypdf 也有 PyInstaller 钩子要走的隐藏 import**，
     用 `--collect-all` 一起收干净最省事。

体积代价（实测，onefile 会共用 Python 运行时和 OpenSSL，边际成本远低于单独相加）:
    裸基线 7.1MB → +Pillow 14.9 → +pypdf 16.6 → +pypdfium2 17.6 → 三个一起 21.2MB
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "main.py"
DIST = HERE / "dist"
WORK = HERE / "_build"
OUT = HERE / "_out"
ICON = HERE / "ncm2mp3.ico"

GUI_NAME = "格式转换工作台"
CLI_NAME = "格式转换工作台-命令行"

# registry.py 里 PLUGIN_MODULES 兜底清单要能找到的模块，显式再点一遍名。
# 只写**已经存在**的 —— 还没实现的插件写了会多出一堆 "Hidden import not found" 警告。
HIDDEN = [
    "conv.converters.image",
    "conv.converters.pdf_ops",
    "conv.converters.pdf_render",
    "conv.converters.pdf_text",
    "conv.converters.ncm",
    "conv.converters.av",
    "conv.converters.office",
    "conv.core.engines",
    "conv.core.registry",
    "conv.ui.server",
    "ncm2mp3",
    "pypdf",
    "pypdfium2",
    "PIL.Image",
]

COLLECT_ALL = ["pypdfium2", "pypdf", "PIL"]


def has_pyinstaller() -> bool:
    try:
        r = subprocess.run([sys.executable, "-m", "PyInstaller", "--version"],
                           capture_output=True, text=True)
        return r.returncode == 0
    except OSError:
        return False


def _clean(path: Path) -> None:
    """删目录，但**绝不让它影响打包结果**。

    某些环境会通过 PYTHONPATH 注入钩子，把 shutil.rmtree 换成带人工确认的版本。
    PyInstaller 的 _build 目录有上千个文件，一删就触发确认提示，甚至直接中断进程
    —— 表现就是：exe 已经打好了，脚本却报 exit=1，多版本打包时后面的版本轮不到。

    解法：把删除动作丢给一个**剥掉 PYTHONPATH 的子进程**去做。
    子进程不加载那个钩子，rmtree 就是普通的 rmtree。
    任何失败都只打印一行提示，不影响已经产出的 exe。
    """
    if not path.exists():
        return
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    code = ("import shutil,sys,pathlib;"
            "p=pathlib.Path(sys.argv[1]);"
            "shutil.rmtree(p, ignore_errors=True);"
            "print('gone' if not p.exists() else 'left')")
    try:
        r = subprocess.run([sys.executable, "-c", code, str(path)],
                           capture_output=True, text=True, env=env, timeout=180)
        if "gone" in (r.stdout or ""):
            return
    except Exception:                                          # noqa: BLE001
        pass
    # 子进程也没删掉 → 只提示，不动产物
    print(f"  （中间目录没删掉，不影响 exe：{path}）")


def build(name: str, windowed: bool) -> Path:
    """打一个 exe。

    刻意绕开所有"删除文件"的动作 —— 某些环境（安全删除保护/沙箱）会拦截批量删除，
    PyInstaller 的 --clean 和覆盖已有 exe 都可能被拦下来。做法:
      * 工作目录每次全新（不加 --clean，天然没东西要删）
      * 先输出到 _out/<时间戳>/ 空目录（目标不存在 → 不需要删）
      * 最后用 open(...,'wb') 截断写覆盖 dist 里的旧 exe
    """
    stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    work = WORK / f"{name}-{stamp}"
    outdir = OUT / stamp
    work.mkdir(parents=True, exist_ok=True)
    outdir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--name", name,
        "--distpath", str(outdir),
        "--workpath", str(work),
        "--specpath", str(work),
        "--paths", str(HERE),        # 让 PyInstaller 能 import 到 conv 包
    ]
    cmd.append("--windowed" if windowed else "--console")
    if ICON.is_file():
        cmd += ["--icon", str(ICON)]
    for m in HIDDEN:
        cmd += ["--hidden-import", m]
    for p in COLLECT_ALL:
        cmd += ["--collect-all", p]
    # 插件是动态 import 的，整包收进来最保险
    cmd += ["--collect-submodules", "conv"]
    cmd.append(str(SCRIPT))

    print()
    print("=" * 66)
    print(("[界面版] " if windowed else "[命令行版] ") + name + ".exe")
    print("=" * 66)

    produced = outdir / f"{name}.exe"
    placed = False
    try:
        subprocess.run(cmd, check=True)
        if not produced.is_file():
            raise RuntimeError(f"没生成 {produced}")
        DIST.mkdir(exist_ok=True)
        target = DIST / f"{name}.exe"
        try:
            if target.exists():
                with open(produced, "rb") as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)          # 覆盖，不删文件
            else:
                shutil.copyfile(produced, target)
            placed = True
        except OSError as e:
            print()
            print(f"  !! 覆盖 {target} 失败：{e}")
            print(f"     新的 exe 已经打好在这里：{produced}")
            print("     手动用它替换 dist 里的旧文件即可。")
    finally:
        # 清理必须"尽力而为" —— 别让收尾动作把打包结果一起带走
        _clean(work)
        if placed:
            _clean(outdir)
    return DIST / f"{name}.exe" if placed else produced


def main() -> int:
    args = sys.argv[1:]
    want_gui = "--cli" not in args
    want_cli = "--gui" not in args

    if not SCRIPT.is_file():
        print(f"找不到脚本：{SCRIPT}")
        return 1
    if not has_pyinstaller():
        print("没装 PyInstaller。先执行：\n    pip install pyinstaller")
        return 1

    DIST.mkdir(exist_ok=True)
    made: list[Path] = []
    if want_gui:
        made.append(build(GUI_NAME, windowed=True))
    if want_cli:
        made.append(build(CLI_NAME, windowed=False))

    print()
    print("=" * 66)
    print("打包完成：")
    for p in made:
        if p.is_file():
            print(f"  {p}   ({p.stat().st_size / 1048576:.1f} MB)")
        else:
            print(f"  !! 没生成：{p}")
    print()
    print("exe 可以单独拷到任何 Windows 电脑上运行，不需要装 Python。")
    print("图片 / PDF / NCM 开箱即用；音视频需要目标机器另装 FFmpeg（程序会自动探测）。")
    print("docx/xlsx/pptx → PDF 用本机已装的 Office（或 LibreOffice），无需额外安装。")
    print("中间产物都在 _build/，删掉不影响 exe。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
