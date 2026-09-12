# -*- coding: utf-8 -*-
"""PDF → Markdown / 纯文本（外置引擎 pdftotext 优先，pypdf 兜底）。

为什么是「文字提取」而不是「渲染」：PDF 是页面描述语言，文字层里已经
存了「哪些字形画在哪个坐标」。提取文字是读它，不需要 OCR（除非是扫描件）。

两条引擎，按优先级降级：

1. **pdftotext**（poppler 套件）—— 首选。``-layout`` 保留版面空白，
   启发式规则据此识别标题/列表层级，还原出真正的 Markdown 结构。
2. **pypdf** —— 兜底。已经打包进 exe，零成本，但 ``extract_text()``
   不保留版面空白，只能按行输出纯文本（标题识别基本靠猜）。

扫描件（图片型 PDF）两条引擎都提取不出文字，会明确告诉用户「这是扫描件，
需要 OCR」，而不是吐一个空文件假装成功 —— 这一点刻意做成了硬性判断。

踩坑记录：
* pdftotext 的 ``-layout`` 输出是「用空格撑开的对齐文本」，不是干净文本，
  不能直接写进 Markdown，必须先把每行的连续空格归一化、去掉行尾空白，
  否则标题的 ``#`` 判断和列表的 ``- `` 判断全都会失灵。
* pypdf 对中文 PDF 的提取依赖 ToUnicode 映射，个别 PDF 会返回乱码或空；
  空文本时宁可报「可能没有文字层」也不要输出空文件。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ..core import engines
from ..core.base import (Capability, Converter, Job, Option, safe_stem,
                         unique_path)
from ..core.result import Result

try:
    from pypdf import PdfReader
    _HAVE_PYPDF = True
except Exception:                                            # noqa: BLE001
    PdfReader = None                                         # type: ignore
    _HAVE_PYPDF = False


TIMEOUT = 300                # pdftotext 秒级~几十秒级，给足 5 分钟宁慢勿断
MIN_TEXT_CHARS = 8           # 提取出的文字少于这个字符数，判定为「没有文字层」


# --------------------------------------------------------------------------
# 参数
# --------------------------------------------------------------------------

MODE_OPT = Option(
    "mode", "输出格式", "choice", "md",
    choices=(("md", "Markdown（识别标题/列表结构）"),
             ("txt", "纯文本（TXT，只留文字）")),
)

TXT_HINT = "Markdown 会尝试识别标题层级；纯文本只提取文字、不带任何格式标记"


# --------------------------------------------------------------------------
# 引擎探测
# --------------------------------------------------------------------------

def _pdftotext() -> tuple[str, str]:
    """返回 (pdftotext 路径, 错误说明)。没找到时返回空串让调用方走 pypdf 兜底。"""
    eng = engines.pdftotext()
    if not eng.found:
        return "", ""
    return eng.path, ""


# --------------------------------------------------------------------------
# 文本清洗
# --------------------------------------------------------------------------

def _normalize_line(line: str) -> str:
    """把 pdftotext -layout 的「空格撑版」行洗干净。

    -layout 会用连续空格把文字撑到对齐列，直接写进 Markdown 会让标题/列表
    判断全失灵。这里做最小清洗：
    * 去掉行尾空白和换页符（\\x0c）
    * 把 3 个以上连续空格压成一个（保留 2 个，让表格的字段分隔还能看出来）
    """
    line = line.replace("\x0c", "").rstrip()
    line = re.sub(r" {3,}", "  ", line)
    return line


def _is_heading(line: str) -> int:
    """判断一行是不是标题，返回标题级别（1~6），不是则返回 0。

    纯启发式，基于常见论文/报告的标题特征：
    * 形如「1. xxx」「1.2.3 xxx」「第x章」「Chapter 1」这类编号开头
    * 全大写短句
    * 无标点结尾的短行（< 60 字符且不以句号/逗号结尾）

    只做「合理猜测」，宁可漏判也不乱判 —— 正文里偶尔一句短话被当成标题
    比标题没识别出来更糟。
    """
    s = line.strip()
    if not s or len(s) > 80:
        return 0

    # ---- 第一优先：明确的标题特征（这些几乎不可能误报）----

    # 编号标题：1.2.3 xxx / 1.2 xxx（注意：单独的「1. xxx」是列表，不算）
    if re.match(r"^\d+(\.\d+){1,3}[、.．）)]?\s+\S", s):
        return min(s.count(".") + s.count("．") + 1, 3)

    # 章节标题：第X章 / 第X节 / Chapter N / Section N / Appendix N
    if re.match(r"^第[一二三四五六七八九十百\d]+[章节部篇]", s):
        return 1
    m = re.match(r"^(Chapter|Section|Appendix)\s+[A-Za-z0-9]+", s, re.I)
    if m:
        return 1 if m.group(1).lower() == "chapter" else 2

    # ---- 第二优先：排除「像标题其实不是」的行 ----

    # 表格行/对齐文本：含 2 个以上「连续空格分隔的字段」。pdftotext -layout
    # 会把表格字段用大片空格撑开对齐（`姓名  学号  分数`），这是最可靠的
    # 表格特征。注意 _normalize_line 已经把 3+ 空格压成 2 个，所以这里判 ≥2。
    if len(re.split(r"\s{2,}", s)) >= 2:
        return 0

    # 有序列表项：`1. xxx` / `1、xxx` / `1) xxx`（单独编号开头）
    if re.match(r"^\d{1,3}[、.．)]\s+\S", s):
        return 0

    # 表格数据行（pypdf 兜底路径用）：pypdf 不保留版面空白，字段间只有单空格，
    # 上面那条「≥2 空格」拦不住。特征是「短片段 + 含纯数字」（如「Alice 95」）。
    fields = s.split()
    if 2 <= len(fields) <= 6 and any(
            f.isdigit() or re.fullmatch(r"\d+[.:%\-]?", f) for f in fields):
        return 0

    # ---- 第三优先：弱特征（可能误报，靠前面的排除兜底）----

    # 全大写短句（英文标题习惯）
    if len(s) < 40 and s.isupper() and re.search(r"[A-Z]", s):
        return 2
    # 短行、不以标点结尾、词数少
    if len(s) < 60 and not re.search(r"[。，；：,.;:、]$", s):
        words = len(s.split())
        if words <= 12:
            return 3
    return 0


def _to_markdown(lines: list[str]) -> str:
    """把已清洗的文本行组装成 Markdown。

    规则：
    * 标题行 → 加对应数量的 ``#``
    * ``• `` / ``- `` 开头的行原样保留（当作列表）
    * 空行原样保留（段落分隔）
    * 其余当正文
    """
    out: list[str] = []
    prev_blank = True
    for raw in lines:
        line = _normalize_line(raw)
        lvl = _is_heading(line) if line.strip() else 0
        if lvl:
            out.append("#" * lvl + " " + line.strip())
            prev_blank = False
            continue
        s = line.strip()
        if not s:
            # 连续空行压成一个
            if not prev_blank:
                out.append("")
            prev_blank = True
            continue
        # 列表项：• / - / * / 数字加点 开头，原样保留（去掉多余缩进）
        if re.match(r"^[•\-*·]\s+", s) or re.match(r"^\d+[、.．)]\s+", s):
            out.append(s)
        else:
            out.append(s)
        prev_blank = False
    # 去掉开头和结尾的多余空行
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out) + "\n"


def _extract_with_pdftotext(exe: str, src: Path, mode: str) -> tuple[str, str]:
    """用 pdftotext 提取。返回 (文本, 错误)，错误非空表示失败。"""
    args = [exe, "-layout", "-enc", "UTF-8", str(src), "-"]
    try:
        r = subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return "", "提取超时（超过 5 分钟）"
    except OSError as e:
        return "", f"启动 pdftotext 失败：{e}"

    if r.returncode != 0:
        err = (r.stderr or "").strip()
        tail = [l.strip() for l in err.splitlines() if l.strip()]
        return "", f"pdftotext 报错：{tail[-1][:160] if tail else '未知错误'}"

    text = r.stdout or ""
    return text, ""


def _extract_with_pypdf(src: Path) -> tuple[str, str]:
    """用 pypdf 提取（兜底）。返回 (文本, 错误)。"""
    if not _HAVE_PYPDF:
        return "", "缺少 pypdf，无法提取"
    try:
        reader = PdfReader(str(src))
    except Exception as e:                                   # noqa: BLE001
        return "", f"打不开这个 PDF：{e}"

    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:                                    # noqa: BLE001
            pass
        try:
            len(reader.pages)
        except Exception as e:                               # noqa: BLE001
            return "", "这个 PDF 有密码，无法自动提取"

    parts: list[str] = []
    for page in reader.pages:
        try:
            t = page.extract_text() or ""
        except Exception as e:                               # noqa: BLE001
            return "", f"提取第 {len(parts) + 1} 页失败：{e}"
        if t.strip():
            parts.append(t.strip())
    if not parts:
        return "", ""
    # pypdf 提取的行已经比较干净，直接拼页
    text = "\n\n".join(parts)
    return text, ""


# --------------------------------------------------------------------------
# 插件
# --------------------------------------------------------------------------

class PdfTextConverter(Converter):
    id = "pdf_text"
    label = "PDF 转文字"
    order = 26
    engine = "pdftotext"     # 主引擎；缺了自动降级到 pypdf，不置灰

    def capabilities(self) -> list[Capability]:
        return [
            Capability("pdf", "md", "转成 Markdown（识别标题/列表）",
                       options=(MODE_OPT,)),
            Capability("pdf", "txt", "转成纯文本 TXT",
                       options=(MODE_OPT,)),
        ]

    def available(self) -> tuple[bool, str]:
        # 永远可用：pdftotext 没有也能用 pypdf 兜底。
        # 只有 pypdf 也没有时才不可用（理论上打包后 pypdf 一定在）。
        if not _HAVE_PYPDF and not engines.pdftotext().found:
            return False, "需要 pypdf 或 pdftotext 才能提取 PDF 文字"
        return True, ""

    def convert(self, job: Job) -> list[Result]:
        src = job.first
        if not src.is_file():
            return [Result.bad("源文件不存在", src)]

        mode = str(job.opts.get("mode") or "md")
        if mode not in ("md", "txt"):
            mode = "md"
        dst = job.dst_ext            # md 或 txt，注册表保证二者之一

        # 1) 优先 pdftotext
        exe, _why = _pdftotext()
        text = ""
        err = ""
        if exe:
            text, err = _extract_with_pdftotext(exe, src, mode)

        # 2) pdftotext 失败或缺失 → pypdf 兜底
        if not text and err:
            # pdftotext 明确报错（比如损坏），不要掩盖，直接返回
            return [Result.bad(err, src)]
        if not text:
            text, err = _extract_with_pypdf(src)
            if err:
                return [Result.bad(err, src)]

        # 3) 空文本 = 扫描件/无文字层，如实告知
        if len(text.strip()) < MIN_TEXT_CHARS:
            return [Result.bad(
                "这个 PDF 提取不出文字。它可能是扫描件（图片型），需要 OCR 才能识别。"
                "本工具暂不支持 OCR，建议先用「PDF 转图片」导出页面再配合其他 OCR 工具。",
                src)]

        # 4) 组装输出
        if mode == "md":
            body = _to_markdown(text.splitlines())
        else:
            body = text.strip() + "\n"

        out = unique_path(job.out_dir, safe_stem(src.stem), dst)
        try:
            out.write_text(body, encoding="utf-8")
        except Exception as e:                               # noqa: BLE001
            return [Result.bad(f"写出失败：{e}", src)]

        return [Result.good(out, src=src, kind=dst,
                            chars=len(text.strip()),
                            engine="pdftotext" if exe else "pypdf")]
