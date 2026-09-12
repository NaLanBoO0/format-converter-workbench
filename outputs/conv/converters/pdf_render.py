# -*- coding: utf-8 -*-
"""PDF → 图片 — 基于 pypdfium2。

为什么用 pypdfium2 而不是 PyMuPDF：它是 Google PDFium 的绑定，Apache/BSD
许可（PyMuPDF 的 AGPL 在分发时是个麻烦），体积也更小。

为什么必须"渲染"而不能"提取图片"：PDF 里根本没有独立的图片对象，
它存的是绘制指令（在某个坐标画什么字形/图形）。想得到图就必须按 DPI 画一遍。

两种模式：
* 逐页：每页产出一个 png / jpg
* 长图：选中的页垂直拼成一张（手机上连着看文档很方便）
"""
from __future__ import annotations

from pathlib import Path

from ..core.base import (Capability, Converter, Job, Option, parse_pages,
                         safe_stem, unique_path)
from ..core.result import Result

try:
    import pypdfium2 as pdfium
    _HAVE_PDFIUM = True
except Exception:                                            # noqa: BLE001
    pdfium = None                                            # type: ignore
    _HAVE_PDFIUM = False

try:
    from PIL import Image
    _HAVE_PIL = True
except Exception:                                            # noqa: BLE001
    Image = None                                             # type: ignore
    _HAVE_PIL = False


MAX_SPLIT_FILES = 500          # 逐页导出的单次上限
MAX_LONG_PIXELS = 120_000_000  # 长图总像素上限（约 360MB RGB），超了就劝退
JPEG_MAX_SIDE = 65500          # JPEG 编码器的硬上限


PAGES_OPT = Option("pages", "页码范围", "text", "",
                   hint='如 1-3,5,8- ；留空 = 全部')

DPI_OPT = Option("dpi", "分辨率", "int", 150, lo=36, hi=600,
                 hint="DPI。越大越清晰、文件越大；150 适合屏幕看，300 适合打印")

QUALITY_OPT = Option("quality", "JPG 质量", "int", 90, lo=1, hi=100)

RENDER_OPTS = (PAGES_OPT, DPI_OPT)


def _render(path: Path, indices: list[int], dpi: int):
    """逐页渲染成 PIL 图。用完必须把 bitmap / page 关掉，否则 PDF 句柄泄漏。"""
    pdf = pdfium.PdfDocument(str(path))
    try:
        scale = max(0.05, float(dpi) / 72.0)
        for idx in indices:
            page = pdf[idx]
            try:
                bitmap = page.render(scale=scale)
                try:
                    yield idx, bitmap.to_pil()
                finally:
                    bitmap.close()
            finally:
                page.close()
    finally:
        pdf.close()


class PdfRenderConverter(Converter):
    id = "pdf_render"
    label = "PDF 转图片"
    order = 25

    def capabilities(self) -> list[Capability]:
        caps: list[Capability] = []
        for d in ("png", "jpg"):
            caps.append(Capability(
                "pdf", d, f"每页一张 {d.upper()}", action="page",
                one2many=True, options=RENDER_OPTS + (QUALITY_OPT,)))
            caps.append(Capability(
                "pdf", d, f"拼成长图 {d.upper()}", action="long",
                options=RENDER_OPTS + (QUALITY_OPT,)))
        return caps

    def available(self) -> tuple[bool, str]:
        if not _HAVE_PDFIUM:
            return False, "需要 pypdfium2（pip install pypdfium2）"
        if not _HAVE_PIL:
            return False, "需要 Pillow（pip install pillow）"
        return True, ""

    def convert(self, job: Job) -> list[Result]:
        if not _HAVE_PDFIUM or not _HAVE_PIL:
            return [Result.bad("缺少 pypdfium2 或 Pillow，PDF 转图片不可用")]
        return self._long(job) if job.action == "long" else self._page(job)

    # ---------------- 公共：确定要渲染哪些页 ----------------

    def _pick_pages(self, job: Job) -> tuple[list[int], int] | list[Result]:
        src = job.first
        try:
            pdf = pdfium.PdfDocument(str(src))
        except Exception as e:                               # noqa: BLE001
            return [Result.bad(f"打不开这个 PDF：{e}", src)]
        try:
            total = len(pdf)
        finally:
            pdf.close()
        if total == 0:
            return [Result.bad("这个 PDF 一页都没有", src)]

        spec = str(job.opts.get("pages") or "").strip()
        # 逐页模式下留空就是全部页
        want = parse_pages(spec, total) or list(range(1, total + 1))
        return want, total

    # ---------------- 逐页导出 ----------------

    def _page(self, job: Job) -> list[Result]:
        picked = self._pick_pages(job)
        if isinstance(picked, list):
            return picked
        want, _total = picked

        if len(want) > MAX_SPLIT_FILES:
            return [Result.bad(
                f"选中了 {len(want)} 页，超过单次上限 {MAX_SPLIT_FILES} 页。"
                f"请用「页码范围」分批导出")]

        src = job.first
        dpi = job.opts.get("dpi") or 150
        ext = job.dst_ext
        fmt = "JPEG" if ext == "jpg" else "PNG"
        q = job.opts.get("quality")
        stem = safe_stem(src.stem)
        width = len(str(max(want)))

        results: list[Result] = []
        try:
            for idx, img in _render(src, [p - 1 for p in want], dpi):
                page_no = idx + 1
                out = unique_path(
                    job.out_dir, f"{stem}_第{str(page_no).zfill(width)}页", ext)
                try:
                    rgb = img.convert("RGB") if fmt == "JPEG" else img
                    kw = {"quality": q} if (fmt == "JPEG" and isinstance(q, int)) else {}
                    rgb.save(out, fmt, **kw)
                    results.append(Result.good(out, src=src, page=page_no,
                                               width=img.width, height=img.height))
                except Exception as e:                       # noqa: BLE001
                    results.append(Result.bad(f"第 {page_no} 页写出失败：{e}", src))
                finally:
                    img.close()
        except Exception as e:                               # noqa: BLE001
            results.append(Result.bad(f"渲染中断：{e}", src))
        return results or [Result.bad("没有渲染出任何页面", src)]

    # ---------------- 拼接长图 ----------------

    def _long(self, job: Job) -> list[Result]:
        picked = self._pick_pages(job)
        if isinstance(picked, list):
            return picked
        want, total = picked

        if len(want) > MAX_SPLIT_FILES:
            return [Result.bad(f"选中了 {len(want)} 页，太多了，请缩小范围")]

        src = job.first
        dpi = job.opts.get("dpi") or 150

        imgs = []
        total_px = 0
        try:
            for _idx, img in _render(src, [p - 1 for p in want], dpi):
                total_px += img.width * img.height
                if total_px > MAX_LONG_PIXELS:
                    img.close()
                    return [Result.bad(
                        f"拼起来太大了（约 {total_px / 1e6:.0f} 百万像素）。"
                        f"请把分辨率调低一点，或者减少页数")]
                imgs.append(img)
        except Exception as e:                               # noqa: BLE001
            for i in imgs:
                i.close()
            return [Result.bad(f"渲染失败：{e}", src)]

        if not imgs:
            return [Result.bad("没有渲染出任何页面", src)]

        w = max(i.width for i in imgs)
        h = sum(i.height for i in imgs)

        if h > JPEG_MAX_SIDE and job.dst_ext == "jpg":
            for i in imgs:
                i.close()
            return [Result.bad(
                f"长图高度 {h} 像素，超过 JPG 格式上限 {JPEG_MAX_SIDE}。"
                f"请降低分辨率、减少页数，或者改用 PNG")]

        canvas = Image.new("RGB", (w, h), (255, 255, 255))
        y = 0
        for im in imgs:
            canvas.paste(im, (0, y))
            y += im.height
            im.close()

        out = unique_path(job.out_dir, f"{safe_stem(src.stem)}_长图{len(want)}页",
                          job.dst_ext)
        try:
            if job.dst_ext == "jpg":
                q = job.opts.get("quality")
                canvas.save(out, "JPEG",
                            quality=q if isinstance(q, int) else 90,
                            optimize=True)
            else:
                canvas.save(out, "PNG", optimize=True)
        except Exception as e:                               # noqa: BLE001
            canvas.close()
            return [Result.bad(f"写出失败：{e}", src)]
        canvas.close()

        return [Result.good(out, src=src, pages=len(want), from_total=total,
                            width=w, height=h)]
