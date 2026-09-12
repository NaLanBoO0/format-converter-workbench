#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""版本备份 / 回滚工具。

每次迭代**之前**先跑一次，把当前可运行版本完整快照到 versions/ 下，
出问题可以随时切回来。零依赖，纯标准库。

用法：
    python tools/backup_version.py v1.1-image-pdf      # 备份当前状态
    python tools/backup_version.py --list              # 列出所有快照
    python tools/backup_version.py --restore v1.0-...  # 回滚到某个快照
    python tools/backup_version.py --verify            # 校验所有快照是否完好
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / "outputs"
VERSIONS = ROOT / "versions"
DIST = OUTPUTS / "dist"

# 需要进快照的：源码 / 打包脚本 / 图标 / 已编译的 exe
SRC_FILES = [
    "ncm2mp3.py",          # v1.0 的解密核心（NCM 插件复用它）
    "main.py",             # v1.1 主程序
    "build_exe.py",        # v1.0 打包脚本
    "build_workbench.py",  # v1.1 打包脚本
    "ncm2mp3.ico",
]
# 整个目录树都要进快照的（v1.1 起转换插件是分文件的，漏一个就回滚不完整）
SRC_TREES = ["conv"]
SKIP_DIRS = {"__pycache__", ".pytest_cache", "_build", "_out"}


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def collect() -> list[tuple[Path, Path]]:
    """返回 [(源文件, 快照内的相对路径), ...]"""
    items: list[tuple[Path, Path]] = []

    for name in SRC_FILES:
        p = OUTPUTS / name
        if p.is_file():
            items.append((p, Path(name)))

    # 插件树整棵带上（conv/core/*.py + conv/converters/*.py + conv/ui/*.py）
    for tree in SRC_TREES:
        base = OUTPUTS / tree
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(OUTPUTS)
            if any(part in SKIP_DIRS for part in rel.parts):
                continue
            if p.suffix.lower() in (".pyc", ".pyo"):
                continue
            items.append((p, rel))

    # dist 下的 exe 全部带上
    if DIST.is_dir():
        for p in sorted(DIST.glob("*.exe")):
            items.append((p, Path("dist") / p.name))

    return items


def write_manifest(dest: Path, tag: str, items: list[tuple[Path, Path]]) -> Path:
    # 用 "|" 分隔。Windows 文件名里不允许 "|"，所以切分绝对安全；
    # 靠空白切分会踩坑 —— "77.9 KB" 这种带空格的字段会把路径切歪。
    lines = [
        f"版本标签 : {tag}",
        f"备份时间 : {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"源码位置 : {OUTPUTS}",
        "",
        f"{'sha256(前16)':<18}|{'大小':>10}|文件",
        "-" * 78,
    ]
    for src, rel in items:
        d = dest / rel
        lines.append(f"{sha256(d)[:16]:<18}|{human(d.stat().st_size):>10}|{rel}")

    # 顺便记一下源码行数，方便对比各版本膨胀情况
    for label, rel in (("ncm2mp3.py", "ncm2mp3.py"), ("main.py", "main.py")):
        f = OUTPUTS / rel
        if f.is_file():
            n = sum(1 for _ in open(f, encoding="utf-8", errors="replace"))
            lines.append(f"{label} 行数 : {n}")

    conv = OUTPUTS / "conv"
    if conv.is_dir():
        n_files = n_lines = 0
        for p in sorted(conv.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            n_files += 1
            n_lines += sum(1 for _ in open(p, encoding="utf-8", errors="replace"))
        lines.append(f"conv/ 插件树 : {n_files} 个 .py，共 {n_lines} 行")

    mp = dest / "MANIFEST.txt"
    mp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return mp


def cmd_backup(tag: str) -> int:
    if not tag:
        print("请给一个版本标签，例如：python tools/backup_version.py v1.1-image-pdf")
        return 2

    items = collect()
    if not items:
        print(f"没找到可备份的文件（检查 {OUTPUTS}）")
        return 2

    stamp = time.strftime("%Y%m%d-%H%M")
    dest = VERSIONS / f"{tag}-{stamp}"
    if dest.exists():
        print(f"快照已存在：{dest}")
        return 2

    (dest / "dist").mkdir(parents=True, exist_ok=True)
    for src, rel in items:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)

    mp = write_manifest(dest, tag, items)
    total = sum((dest / rel).stat().st_size for _, rel in items)

    print(f"已备份 → {dest.relative_to(ROOT)}")
    print(f"   {len(items)} 个文件，共 {human(total)}")
    for _, rel in items:
        print(f"   · {rel}")
    print(f"   清单：{mp.relative_to(ROOT)}")
    return 0


def list_snapshots() -> list[Path]:
    if not VERSIONS.is_dir():
        return []
    return sorted([p for p in VERSIONS.iterdir() if p.is_dir()], reverse=True)


def cmd_list() -> int:
    snaps = list_snapshots()
    if not snaps:
        print("还没有任何快照。用 python tools/backup_version.py <标签> 创建。")
        return 0
    print(f"共 {len(snaps)} 个快照（新 → 旧）：\n")
    for s in snaps:
        exes = sorted((s / "dist").glob("*.exe")) if (s / "dist").is_dir() else []
        size = sum(f.stat().st_size for f in s.rglob("*") if f.is_file())
        print(f"  {s.name}")
        print(f"      {len(exes)} 个 exe，{human(size)}")
        for e in exes:
            print(f"      · {e.name}  {human(e.stat().st_size)}")
    return 0


def cmd_verify() -> int:
    """校验快照有没有被动过。

    查两件事：

    1. **清单里的文件**：缺了 / 内容变了。
    2. **清单之外的文件**：快照本该是"只读存档"，混进新东西说明它被
       编辑器或手动拷贝污染过（本机就真发生过 —— 用 IDE 打开某个快照目录后，
       IDE 会建 `.idea/`，甚至按自己的习惯改写文件里的换行符和几行文字）。
       这种情况不查出来，回滚时会拿到一版"既不是当时状态、也不是当前状态"的代码。

    「内容变了」和「损坏」是两回事，所以措辞分开说 ——
    前者通常无害但要知道，后者必须处理。
    """
    snaps = list_snapshots()
    if not snaps:
        print("没有快照可校验。")
        return 0
    bad = 0
    for s in snaps:
        mp = s / "MANIFEST.txt"
        if not mp.is_file():
            print(f"  [缺清单] {s.name}")
            bad += 1
            continue
        problems = []
        listed: set[str] = set()
        for line in mp.read_text(encoding="utf-8").splitlines():
            parts = line.split("|", 2)
            if len(parts) != 3:
                continue
            h = parts[0].strip()          # 左对齐会补尾随空格，必须 strip
            if len(h) != 16 or not all(c in "0123456789abcdef" for c in h):
                continue
            _hash, _size, rel = parts
            rel = rel.strip()
            listed.add(rel)
            f = s / rel
            if not f.is_file():
                problems.append(f"{rel} 丢失")
            elif sha256(f)[:16] != h:
                problems.append(f"{rel} 内容与清单不符（快照被改过？）")

        # 反向查：快照里多出来的东西
        extra = []
        for f in sorted(s.rglob("*")):
            if not f.is_file():
                continue
            rel = str(f.relative_to(s))
            if rel == "MANIFEST.txt" or rel in listed:
                continue
            extra.append(rel)
        if extra:
            head = "、".join(extra[:4])
            more = f" 等 {len(extra)} 个" if len(extra) > 4 else ""
            problems.append(f"多出清单外的文件：{head}{more}")

        if problems:
            bad += 1
            print(f"  [异常] {s.name}")
            for p in problems:
                print(f"        {p}")
        else:
            print(f"  [完好] {s.name}")
    print(f"\n{len(snaps) - bad}/{len(snaps)} 个快照完好。")
    if bad:
        print("提示：[异常] 只说明快照被动过，不代表不能用。"
              "想确认它能否回滚，看缺没缺文件；只是内容不符的话可以拿 --list 对比大小。")
    return 1 if bad else 0


def cmd_restore(name: str) -> int:
    snaps = list_snapshots()
    hit = None
    for s in snaps:
        if s.name == name:
            hit = s
            break
    if hit is None:
        for s in snaps:
            if name and name in s.name:
                hit = s
                break
    if hit is None:
        print(f"没找到匹配的快照：{name}")
        print("可用：")
        for s in snaps:
            print(f"  {s.name}")
        return 2

    # 先给"当前状态"自动打一个保险快照，避免回滚把未备份的改动冲掉
    stamp = time.strftime("%Y%m%d-%H%M")
    rescue = VERSIONS / f"auto-before-restore-{stamp}"
    if not rescue.exists():
        rescue.mkdir(parents=True, exist_ok=True)
        for src, rel in collect():
            t = rescue / rel
            t.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, t)
        print(f"（已先把当前状态存到 {rescue.name} 以防万一）")

    n = 0
    for f in hit.rglob("*"):
        if not f.is_file() or f.name == "MANIFEST.txt":
            continue
        rel = f.relative_to(hit)
        t = (OUTPUTS / rel) if not str(rel).startswith("dist") else (DIST / f.name)
        t.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, t)
        n += 1
    print(f"已从 {hit.name} 回滚 {n} 个文件到 outputs/。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="版本备份 / 回滚工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("tag", nargs="?", default="", help="版本标签，如 v1.1-image-pdf")
    ap.add_argument("--list", action="store_true", help="列出所有快照")
    ap.add_argument("--verify", action="store_true", help="校验快照完整性")
    ap.add_argument("--restore", metavar="NAME", help="回滚到指定快照（可只写片段）")
    args = ap.parse_args()

    if args.list:
        return cmd_list()
    if args.verify:
        return cmd_verify()
    if args.restore:
        return cmd_restore(args.restore)
    return cmd_backup(args.tag)


if __name__ == "__main__":
    sys.exit(main())
