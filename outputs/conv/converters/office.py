# -*- coding: utf-8 -*-
"""Office 文档 → PDF：docx / xlsx / pptx（外置引擎：Microsoft Office / WPS）。

设计（对应 GitHub 调研结论，见 `GitHub调研-格式转换方案对比.md` 3.5 节）：

* **为什么用 Office COM 而不是纯 Python 库**：
  开源库里 docx→pdf 的保真度都很差（`docx2pdf` 自己就是调 Word COM；
  Java 的 `docs-to-pdf-converter` 把 PPT 导出成"每页一张 PNG 塞进 PDF"，
  README 都承认"只能凑合打印"）。而 Word/Excel/PPT 自己导出的 PDF，
  是 Windows 上保真度最高的 —— 因为 PDF 就是它自己渲染的。
  本机往往已装 Office，**零下载**、**零新依赖**。

* **为什么不引 pywin32**：``docx2pdf`` 用 ``win32com`` 调 Word COM，
  但那要额外装 pywin32（含 C 扩展，打包进 exe 麻烦）。项目里 `dialogs.py`
  已经在用 PowerShell 调系统 COM（`-NoProfile -STA` + Base64 传路径），
  这里复用同一套路：**零新依赖、打包零风险**。

* **一个插件管三个应用**：Word / Excel / PowerPoint 分别对应 docx / xlsx / pptx。
  ``cap_available()`` 按 ``cap.src`` 分别判断 —— 装了 Word 没装 Excel 时，
  ``xlsx→pdf`` 置灰，``docx→pdf`` 照常可用。

* **LibreOffice 兜底**：如果 Office/WPS 都没有，但装了 LibreOffice，
  也能用它的命令行 ``--convert-to pdf`` 转（保真度略低，但可用）。

踩过的坑都标在实现里。
"""
from __future__ import annotations

import base64
import subprocess
from pathlib import Path

from ..core import engines
from ..core.base import (Capability, Converter, Job, safe_stem, unique_path)
from ..core.result import Result

# --------------------------------------------------------------------------
# 格式 → 应用映射
# --------------------------------------------------------------------------
# 每种源格式该用哪个 Office 应用去转。
SRC_APPS = {
    "docx": "word",
    "doc": "word",      # 老格式 Word 也能开，顺带支持
    "xlsx": "excel",
    "xls": "excel",     # 老格式
    "pptx": "powerpoint",
    "ppt": "powerpoint",
}

# 每种应用对应的 COM ProgID 和格式过滤器 id（见 convert 里的说明）
_APP_PROGID = {
    "word": "Word.Application",
    "excel": "Excel.Application",
    "powerpoint": "PowerPoint.Application",
}

# SaveAs 的 FileFormat 常量（Word/Excel/PPT 都是 17 = PDF）
# 这些是 COM 接口里写死的值，跨版本稳定。
_FMT_PDF = 17

# Office 启动 + 转一个文档是秒级~十几秒级的活；给足 5 分钟，宁慢勿断。
TIMEOUT = 300


def _app_available(app: str) -> tuple[bool, str]:
    """某个应用（word/excel/powerpoint）能不能用。

    优先 Office/WPS（engines.office() 的 apps 字典里有对应键），
    没有再看 LibreOffice 兜底（soffice 能转全部三种）。
    """
    off = engines.office()
    if off.apps.get(app):
        return True, ""
    so = engines.soffice()
    if so.found:
        return True, ""
    return False, "需要 Microsoft Office（或 LibreOffice）才能把这种文档转成 PDF"


# --------------------------------------------------------------------------
# 把 COM 报错翻译成人话
# --------------------------------------------------------------------------
_FRIENDLY = (
    ("0x800A", "文档可能损坏，或被设置了密码保护"),
    ("is already open", "这个文档已经在 Office 里打开了，先关掉再试"),
    ("busy", "Office 正在忙（可能有弹窗没关），等它一下或重启本程序"),
    ("permission", "没有权限访问这个文件或输出目录"),
    ("not a valid", "这不是有效的 Office 文档，或者扩展名和内容对不上"),
    ("corrupted", "文件已损坏"),
    ("password", "文档受密码保护，无法自动转换"),
    ("0x800706BE", "Office 组件调用失败，重试一次或重启本程序"),
)


def _friendly(err: str) -> str:
    low = err.lower()
    for pat, msg in _FRIENDLY:
        if pat.lower() in low:
            return msg
    tail = [l.strip() for l in err.splitlines() if l.strip()]
    return tail[-1][:180] if tail else "Office 没有给出错误信息"


# --------------------------------------------------------------------------
# PowerShell COM 转换
# --------------------------------------------------------------------------

def _ps_b64(s: str) -> str:
    """路径 → Base64。中文/引号/反斜杠一锅端，绝不触发 PowerShell 转义问题。"""
    return base64.b64encode(str(s).encode("utf-8")).decode("ascii")


def _word_ps(src_b64: str, dst_b64: str) -> str:
    return (
        "$ErrorActionPreference='Stop';"
        f"$src=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{src_b64}'));"
        f"$dst=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{dst_b64}'));"
        "$w=New-Object -ComObject Word.Application;"
        "$w.Visible=$false; $w.DisplayAlerts=0;"
        "try {"
        "  $d=$w.Documents.Open($src,$false,$true);"
        "  try { $d.SaveAs([ref]$dst,[ref]17) } finally { $d.Close($false);"
        "    [Runtime.InteropServices.Marshal]::ReleaseComObject($d)|Out-Null }"
        "} finally { $w.Quit();"
        "  [Runtime.InteropServices.Marshal]::ReleaseComObject($w)|Out-Null }"
    )


def _excel_ps(src_b64: str, dst_b64: str) -> str:
    # Excel 的 ExportAsFixedFormat 比 SaveAs 更稳（不会把打印区域外的内容切掉）
    return (
        "$ErrorActionPreference='Stop';"
        f"$src=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{src_b64}'));"
        f"$dst=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{dst_b64}'));"
        "$e=New-Object -ComObject Excel.Application;"
        "$e.Visible=$false; $e.DisplayAlerts=$false;"
        "try {"
        "  $wb=$e.Workbooks.Open($src,$false,$true);"
        "  try { $wb.ExportAsFixedFormat(0,$dst) } finally { $wb.Close($false);"
        "    [Runtime.InteropServices.Marshal]::ReleaseComObject($wb)|Out-Null }"
        "} finally { $e.Quit();"
        "  [Runtime.InteropServices.Marshal]::ReleaseComObject($e)|Out-Null }"
    )


def _ppt_ps(src_b64: str, dst_b64: str) -> str:
    # PowerPoint 的 SaveAs 用 ppSaveAsPDF = 32。
    # Presentation 对象也要显式 ReleaseComObject，否则 POWERPNT.EXE 会延迟退出
    # （首次冷启动尤其明显），连续转多个 PPT 会累积僵尸进程。
    return (
        "$ErrorActionPreference='Stop';"
        f"$src=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{src_b64}'));"
        f"$dst=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{dst_b64}'));"
        "$p=New-Object -ComObject PowerPoint.Application;"
        "try {"
        "  $pres=$p.Presentations.Open($src,$true,$false,$false);"
        "  try { $pres.SaveAs($dst,32) } finally { $pres.Close();"
        "    [Runtime.InteropServices.Marshal]::ReleaseComObject($pres)|Out-Null }"
        "} finally { $p.Quit();"
        "  [Runtime.InteropServices.Marshal]::ReleaseComObject($p)|Out-Null }"
    )


_PS_BUILDERS = {
    "word": _word_ps,
    "excel": _excel_ps,
    "powerpoint": _ppt_ps,
}


def _run_com(app: str, src: Path, dst: Path) -> tuple[int, str]:
    """跑一次 Office COM 转换。返回 (返回码, 人话错误)。

    **必须 resolve 成绝对路径**：PowerShell 子进程的当前目录未必和 Python 一致，
    传相对路径给 ``Documents.Open`` 会解析错（实测相对路径必失败、绝对路径成功）。
    """
    src = src.resolve()
    dst = dst.resolve()
    ps = _PS_BUILDERS[app](_ps_b64(src), _ps_b64(dst))
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", ps],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return 1, "转换超时（超过 5 分钟）。文档可能太复杂，或 Office 卡住了"
    except OSError as e:
        return 1, f"调起 Office 失败：{e}"

    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        return r.returncode, _friendly(err)
    return 0, ""


def _run_soffice(src: Path, out_dir: Path) -> tuple[int, str]:
    """LibreOffice 兜底：soffice --headless --convert-to pdf。"""
    src = src.resolve()
    out_dir = out_dir.resolve()
    so = engines.soffice()
    if not so.found:
        return 1, "没有 Office 也没有 LibreOffice"
    try:
        r = subprocess.run(
            [so.path, "--headless", "--convert-to", "pdf",
             "--outdir", str(out_dir), str(src)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return 1, "LibreOffice 转换超时"
    except OSError as e:
        return 1, f"启动 LibreOffice 失败：{e}"
    if r.returncode != 0:
        return r.returncode, _friendly((r.stderr or r.stdout or "").strip())
    return 0, ""


class OfficeConverter(Converter):
    id = "office"
    label = "Office 文档转 PDF"
    order = 40
    engine = "office"      # 主引擎是 Office；soffice 作为兜底在内部判断

    def capabilities(self) -> list[Capability]:
        caps: list[Capability] = []
        # doc/docx → pdf、xls/xlsx → pdf、ppt/pptx → pdf
        for src, app in SRC_APPS.items():
            caps.append(Capability(
                src, "pdf", f"转成 PDF（{ {'word':'Word','excel':'Excel','powerpoint':'PowerPoint'}[app] }）"))
        return caps

    def available(self) -> tuple[bool, str]:
        # 整插件级别：三个应用里有任意一个可用，就算插件"可用"。
        # 具体到某一条能力，靠 cap_available() 细分。
        off = engines.office()
        if off.apps:
            return True, ""
        so = engines.soffice()
        if so.found:
            return True, ""
        return False, engines.office().hint

    def cap_available(self, cap: Capability) -> tuple[bool, str]:
        app = SRC_APPS.get(cap.src)
        if app is None:
            return False, "不支持的文档格式"
        return _app_available(app)

    def convert(self, job: Job) -> list[Result]:
        src = job.first
        app = SRC_APPS.get(src.suffix.lower().lstrip("."))
        if app is None:
            return [Result.bad("不支持的文档格式", src)]

        if not src.is_file():
            return [Result.bad("源文件不存在", src)]

        out = unique_path(job.out_dir, safe_stem(src.stem), "pdf")

        # 优先 Office COM，失败/缺失再 LibreOffice 兜底
        off = engines.office()
        if off.apps.get(app):
            code, err = _run_com(app, src, out)
            if code == 0 and out.is_file() and out.stat().st_size > 0:
                return [Result.good(out, src=src, kind="pdf")]
            # COM 没成功，落到 soffice（如果它有）
            if err and not engines.soffice().found:
                return [Result.bad(f"转 PDF 失败：{err}", src)]
        else:
            so = engines.soffice()
            if not so.found:
                return [Result.bad(
                    f"需要 Microsoft Office 才能把 {app} 文档转成 PDF", src)]

        # LibreOffice 兜底路径
        code, err = _run_soffice(src, job.out_dir)
        if code != 0:
            return [Result.bad(f"转 PDF 失败：{err}", src)]
        # soffice 输出的文件名 = 源主干 + .pdf
        produced = job.out_dir / f"{safe_stem(src.stem)}.pdf"
        if produced.is_file() and produced.stat().st_size > 0:
            if produced != out:
                # 统一到 unique_path 定的名字，避免和已有文件撞名
                if out.exists():
                    out = unique_path(job.out_dir, safe_stem(src.stem), "pdf")
                produced.replace(out)
            return [Result.good(out, src=src, kind="pdf")]
        return [Result.bad("转 PDF 失败：没有产出文件", src)]
