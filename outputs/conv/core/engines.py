# -*- coding: utf-8 -*-
"""外部引擎探测：FFmpeg / LibreOffice。

为什么要有这一层（对应已确认的「混合式」引擎策略）：

* **轻量库**（Pillow / pypdf / pypdfium2）直接打包进 exe，开箱即用，不走这里。
* **重量级引擎**不打包 —— FFmpeg 80~170MB、LibreOffice 装完约 700MB，
  硬塞进 exe 会让它膨胀到 300MB+。改成运行时探测：
  找到了就启用对应能力，找不到就把那几项置灰 + 给出安装引导。

核心原则：**缺引擎不是错误，是一种正常状态。** 没装 ffmpeg 不影响你用图片转换。
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path


def base_dir() -> Path:
    """程序所在目录。打包后是 exe 所在目录，源码运行时是 outputs/。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent


# --------------------------------------------------------------------------

@dataclass
class Engine:
    """一个外部可执行程序。"""

    id: str
    label: str
    hint: str                            # 找不到时的安装引导
    path: str = ""
    version: str = ""
    searched: list = field(default_factory=list)
    # 多可执行文件的引擎用这个（目前只有 Office：Word / Excel / PowerPoint 三个）
    apps: dict = field(default_factory=dict)

    @property
    def found(self) -> bool:
        return bool(self.path)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "found": self.found,
            "path": self.path,
            "version": self.version,
            "hint": self.hint,
        }


# --------------------------------------------------------------------------
# 候选路径
# --------------------------------------------------------------------------

def _portable_dirs(name: str) -> list[Path]:
    """程序目录下的便携版引擎位置。

    ``engines/ffmpeg/bin/ffmpeg.exe`` 或 ``ffmpeg/bin/ffmpeg.exe``
    —— 以后要支持「一键下载引擎到程序目录」时，放这儿就行。
    """
    root = base_dir()
    return [
        root / "engines" / name / "bin",
        root / "engines" / name,
        root / name / "bin",
        root / name,
    ]


def _user_dirs(name: str) -> list[Path]:
    """用户级安装位置 + winget 的可移植包目录。

    winget 装可移植包时会解压到
    ``%LOCALAPPDATA%\\Microsoft\\WinGet\\Packages\\<包名>\\<子目录>\\bin\\``，
    然后在 ``WinGet\\Links`` 里建一个符号链接。**但建链接这一步在很多机器上会失败**
    （需要管理员权限或开发者模式），winget 会报
    ``create_symlink: The requested lookup key was not found...``
    并留下一个 **0 字节的坏链接**。

    所以我们两条路都找：既看用户级 Programs 目录（我们推荐的稳定位置），
    也直接**通配扫 winget 的 Packages 目录**（绕过那条坏链接）。
    """
    out = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / name / "bin",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / name,
        Path(os.environ.get("APPDATA", "")) / "Programs" / name / "bin",
    ]

    pkgs = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    if pkgs.is_dir():
        try:
            for pkg in sorted(pkgs.iterdir()):
                if not pkg.is_dir():
                    continue
                # 包名里带引擎名字的才看（Gyan.FFmpeg_... / TheDocumentFoundation.LibreOffice_...）
                # pdftotext 的引擎 id 和包名不同（包叫 Poppler），单独认一下
                pkg_low = pkg.name.lower()
                name_hit = name.lower() in pkg_low
                if not name_hit and not (
                        name == "pdftotext" and "poppler" in pkg_low):
                    continue
                # <包>/<版本子目录>/bin/  或  <包>/<版本子目录>/ 或 <版本子目录>/Library/bin/
                # （poppler 的结构就是 <版本>/Library/bin，比通用的多一层 Library/）
                for sub in sorted(pkg.iterdir()):
                    if sub.is_dir():
                        out.append(sub / "Library" / "bin")
                        out.append(sub / "bin")
                        out.append(sub)
        except OSError:
            pass
    return out


def _env_override(name: str) -> str:
    for key in (f"NCM_{name.upper()}_PATH", f"CONV_{name.upper()}_PATH"):
        v = os.environ.get(key, "").strip()
        if v and Path(v).exists():
            return v
    return ""


def _usable(p: Path) -> bool:
    """这个东西"看起来"能不能执行。

    关键是**排除 0 字节文件** —— winget 建符号链接失败时会留下一个 0 字节的
    同名文件，`shutil.which()` 和 `is_file()` 都会认为它存在。
    如果那个目录恰好在 PATH 上，程序就会选中一个"存在但跑不起来"的 exe，
    表现是转换全部失败且报错莫名其妙。
    """
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def _candidates(name: str, exe_names: tuple[str, ...],
                extra_dirs: list[Path] | None = None) -> tuple[list[str], list[str]]:
    """按优先级收集所有候选可执行文件路径。

    返回 (候选列表, 找过的位置)。候选已去重、已剔除 0 字节的坏文件。
    """
    cands: list[str] = []
    tried: list[str] = []

    def add(p: Path | str) -> None:
        s = str(p)
        if s and s not in cands:
            cands.append(s)

    # 1) 环境变量指定（最优先，方便用户手工指路）
    v = _env_override(name)
    if v:
        add(v)

    # 2) PATH
    tried.append(f"PATH 中的 {exe_names[0]}")
    for e in exe_names:
        p = shutil.which(e)
        if p:
            add(p)

    # 3) 程序目录旁的便携版 + 4) 用户级/winget 位置 + 5) 常见安装位置
    dirs = _portable_dirs(name) + _user_dirs(name) + list(extra_dirs or [])
    for d in dirs:
        tried.append(str(d))
        for e in exe_names:
            f = d / e
            if f.is_file():
                add(f)

    # 剔除 0 字节的坏文件（坏符号链接的痕迹）
    keep = [c for c in cands if _usable(Path(c))]
    dropped = [c for c in cands if c not in keep]
    for d in dropped:
        tried.append(f"（跳过 0 字节的坏文件）{d}")

    return keep, tried


def _version_of(exe: str, args: tuple[str, ...] = ("-version",)) -> str:
    """取版本号。失败就返回空 —— 空也意味着"这个 exe 跑不起来"。"""
    import subprocess
    try:
        r = subprocess.run(
            [exe, *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = (r.stdout or r.stderr or "").strip()
        return out.splitlines()[0][:80] if out else ""
    except Exception:                                    # noqa: BLE001
        return ""


# --------------------------------------------------------------------------
# 具体引擎
# --------------------------------------------------------------------------

_CACHE: dict[str, Engine] = {}

_FFMPEG_HINT = ("没找到 FFmpeg。装一个就能解锁音视频转换：\n"
                "命令行执行 winget install Gyan.FFmpeg ，装完重启本程序。\n"
                "（或者把便携版解压到本程序目录下的 engines\\ffmpeg\\bin\\）")

_SOFFICE_HINT = ("没找到 LibreOffice。装上就能解锁 Office 文档互转（docx/xlsx/pptx ↔ PDF）：\n"
                 "命令行执行 winget install TheDocumentFoundation.LibreOffice\n"
                 "（约 700MB，装完重启本程序）")

_BROKEN_HINT = ("找到了 {path}\n但它跑不起来（可能是失败的符号链接或不完整的安装）。\n"
                "重新装一次，或把便携版解压到本程序目录下的 engines\\{name}\\bin\\")


def _resolve(name: str, exe_names: tuple[str, ...],
             version_args: tuple[str, ...],
             extra_dirs: list[Path]) -> tuple[str, str, list[str], str]:
    """找出一个**真的能跑起来**的引擎。

    返回 (路径, 版本, 找过的位置, 失败原因)。

    "文件存在" 不等于 "能执行" —— winget 建符号链接失败会留下 0 字节的坏文件，
    断网/权限问题也可能留下残缺的安装。所以这里逐个试跑，取第一个能报出版本的。
    """
    cands, tried = _candidates(name, exe_names, extra_dirs)
    for c in cands:
        ver = _version_of(c, version_args)
        if ver:
            return c, ver, tried, ""
    if cands:
        # 找到了但全都跑不起来 —— 要如实说，别让用户以为"没装"
        return "", "", tried, _BROKEN_HINT.format(path=cands[0], name=name)
    return "", "", tried, ""


def ffmpeg(refresh: bool = False) -> Engine:
    if not refresh and "ffmpeg" in _CACHE:
        return _CACHE["ffmpeg"]

    extra = [
        Path(r"C:\ffmpeg\bin"),
        Path(r"C:\Program Files\ffmpeg\bin"),
        Path(r"C:\tools\ffmpeg\bin"),
    ]
    path, ver, tried, broken = _resolve("ffmpeg", ("ffmpeg.exe", "ffmpeg"),
                                        ("-version",), extra)
    hint = broken or _FFMPEG_HINT
    eng = Engine("ffmpeg", "FFmpeg（音视频）", hint, path, ver, tried)
    _CACHE["ffmpeg"] = eng
    return eng


def soffice(refresh: bool = False) -> Engine:
    if not refresh and "soffice" in _CACHE:
        return _CACHE["soffice"]

    extra = [
        Path(r"C:\Program Files\LibreOffice\program"),
        Path(r"C:\Program Files (x86)\LibreOffice\program"),
    ]
    path, ver, tried, broken = _resolve("soffice",
                                        ("soffice.exe", "soffice", "libreoffice"),
                                        ("--version",), extra)
    hint = broken or _SOFFICE_HINT
    eng = Engine("soffice", "LibreOffice（办公文档）", hint, path, ver, tried)
    _CACHE["soffice"] = eng
    return eng


ALL_IDS = ("ffmpeg", "office", "soffice", "pdftotext")


# --------------------------------------------------------------------------
# Office（Microsoft Office / WPS 的 COM 自动化）
# --------------------------------------------------------------------------

_OFFICE_HINT = ("没找到 Microsoft Office / WPS，也没有 LibreOffice。\n"
                "装上任意一个就能解锁 Office 文档转 PDF（docx / xlsx / pptx）：\n"
                "  · 有 Office：什么都不用做，装上就自动认出来\n"
                "  · 没有 Office：命令行执行 winget install "
                "TheDocumentFoundation.LibreOffice （约 700MB）")

# 能用的三个 Office 组件 → 对应的可执行文件名
_OFFICE_APPS = {
    "word": ("WINWORD.EXE", "Word（文字文档）"),
    "excel": ("EXCEL.EXE", "Excel（表格）"),
    "powerpoint": ("POWERPNT.EXE", "PowerPoint（演示文稿）"),
}

# COM ProgID：找不到 exe 路径时也能用 COM 启动（Office 装在哪儿都行）
OFFICE_PROGID = {
    "word": "Word.Application",
    "excel": "Excel.Application",
    "powerpoint": "PowerPoint.Application",
}


def _app_paths_from_registry(exe_name: str) -> str:
    """从注册表 App Paths 里取 Office 组件的真实路径。

    Office 的安装目录带版本号（root\\Office16、root\\Office15…），
    硬编码路径迟早失效。App Paths 是 Windows 官方登记的"这个 exe 在哪"，
    比猜目录可靠得多。
    """
    import winreg                                    # 只存在于 Windows
    if os.name != "nt":
        return ""
    sub = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(root, sub) as k:
                v, _ = winreg.QueryValueEx(k, "")
            if v and Path(v).is_file():
                return str(Path(v))
        except OSError:
            continue
    return ""


def _off_dirs() -> list[Path]:
    pf = [os.environ.get("ProgramFiles", r"C:\Program Files"),
          os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")]
    out: list[Path] = []
    for base in pf:
        if not base:
            continue
        for rel in ("Microsoft Office/root/Office16", "Microsoft Office/Office16",
                    "Microsoft Office/root/Office15", "Microsoft Office/Office15",
                    # WPS 也注册同样的 COM ProgID，顺手认一下
                    "WPS Office", "Kingsoft/WPS Office"):
            out.append(Path(base) / rel)
    return out


_PDTXT_HINT = ("没找到 pdftotext（poppler 套件）。没有它也能转 PDF→Markdown，\n"
               "但标题结构识别会弱一些（改用内置 pypdf 兜底）。\n"
               "想要更好的效果：命令行执行 winget install OSGeo.poppler，装完重启本程序。")


def pdftotext(refresh: bool = False) -> Engine:
    """探测 poppler 套件里的 pdftotext 命令行工具。

    这是 PDF→Markdown/纯文本的**首选引擎**（见 pdf_text 插件）。为什么选它：
      * 它专门做「提取文字 + 保留版面」，``-layout`` 输出的是带坐标的空格对齐文本，
        再经过启发式标题识别，能还原出「标题 / 正文 / 列表」的层级结构；
      * 体积小（poppler 整个十几 MB），符合「重量级引擎外置探测」的策略，
        不像 marker 那样要拖几个 GB 的 PyTorch。
    找不到时 pdf_text 插件会用 pypdf 兜底，功能照样在，只是标题识别弱一点。
    """
    if not refresh and "pdftotext" in _CACHE:
        return _CACHE["pdftotext"]

    extra = [
        Path(r"C:\Program Files\poppler\Library\bin"),
        Path(r"C:\Program Files (x86)\poppler\Library\bin"),
        Path(r"C:\Program Files\poppler\bin"),
    ]
    path, ver, tried, broken = _resolve("pdftotext", ("pdftotext.exe", "pdftotext"),
                                        ("-v",), extra)
    hint = broken or _PDTXT_HINT
    eng = Engine("pdftotext", "poppler/pdftotext（PDF 文字提取）", hint,
                 path, ver, tried)
    _CACHE["pdftotext"] = eng
    return eng


def office(refresh: bool = False) -> Engine:
    """探测 Microsoft Office / WPS。

    为什么优先用 Office 而不是 LibreOffice：
      * 本机往往已经装了，**零下载**；
      * 转换用的是 Word/Excel/PPT 自己的渲染引擎，保真度是 Windows 上最高的
        （毕竟 PDF 就是它们导出的）；
      * 不用多占 700MB。
    LibreOffice 作为备用（见 soffice()）。
    """
    if not refresh and "office" in _CACHE:
        return _CACHE["office"]

    apps: dict[str, str] = {}
    tried: list[str] = []
    for key, (exe_name, _label) in _OFFICE_APPS.items():
        p = _app_paths_from_registry(exe_name)
        if p:
            apps[key] = p
            continue
        tried.append(f"注册表 App Paths\\{exe_name}")
        for d in _off_dirs():
            f = d / exe_name
            if f.is_file():
                apps[key] = str(f)
                break
        else:
            tried.append(f"常见安装目录下的 {exe_name}")

    eng = Engine("office", "Microsoft Office", _OFFICE_HINT, "", "", tried)
    if apps:
        eng.apps = apps
        # path 用 Word 的路径当代表（它最常用），found 就靠它
        eng.path = apps.get("word") or next(iter(apps.values()))
        names = [v for k, v in (("word", "Word"), ("excel", "Excel"),
                                ("powerpoint", "PowerPoint")) if k in apps]
        eng.version = "已找到：" + " / ".join(names)
    _CACHE["office"] = eng
    return eng


def get(engine_id: str, refresh: bool = False) -> Engine:
    if engine_id == "ffmpeg":
        return ffmpeg(refresh)
    if engine_id == "soffice":
        return soffice(refresh)
    if engine_id == "office":
        return office(refresh)
    if engine_id == "pdftotext":
        return pdftotext(refresh)
    # 未知引擎：当成永远可用，免得插件因为拼错 id 就永久置灰
    return Engine(engine_id, engine_id, "", "", "", [])


def status(refresh: bool = False) -> dict:
    """给界面用的一份引擎状态快照。"""
    return {i: get(i, refresh).to_dict() for i in ALL_IDS}
