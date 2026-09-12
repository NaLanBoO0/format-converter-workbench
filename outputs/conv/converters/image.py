# -*- coding: utf-8 -*-
"""图片格式转换 — 基于 Pillow。

覆盖：JPG / PNG / WEBP / BMP / TIFF / GIF / ICO 互转，长边缩放、
      质量调节、EXIF 保留、多帧动画保留、多张合成 PDF。

几个踩过的坑写在实现里：
* JPEG / BMP 不存 alpha 通道 —— 带透明的图不铺白底会变黑，必须先 flatten。
* ICO 要按多尺寸保存，不然 Windows 只会用到很小的那个。
* EXIF 跟目标格式不兼容时会抛异常 —— 不能因为元数据丢掉整张图，要降级重试。
"""
from __future__ import annotations

from pathlib import Path

from ..core.base import (Capability, Converter, Job, Option, safe_stem,
                         unique_path)
from ..core.result import Result

try:
    from PIL import Image
    _HAVE_PIL = True
except Exception:                                            # noqa: BLE001
    Image = None                                             # type: ignore
    _HAVE_PIL = False


EXT_TO_PIL = {
    "jpg": "JPEG", "jpeg": "JPEG",
    "png": "PNG",
    "webp": "WEBP",
    "bmp": "BMP",
    "tiff": "TIFF", "tif": "TIFF",
    "gif": "GIF",
    "ico": "ICO",
}

# 这些格式没有 alpha 通道，透明区域必须先铺白底
OPAQUE_ONLY = {"JPEG", "BMP"}

# 能存 EXIF 的格式
EXIF_OK = {"JPEG", "WEBP", "TIFF", "PNG"}

IMG_EXTS = ("jpg", "png", "webp", "bmp", "tiff", "gif", "ico")

IMG_OPTS = (
    Option("quality", "质量", "int", 92, lo=1, hi=100,
           hint="只对 JPG / WEBP 生效。越高越清晰、文件越大"),
    Option("max_side", "长边限制", "int", 0, lo=0, hi=20000,
           hint="像素。0 = 保持原始尺寸，超出时等比缩小"),
    Option("keep_exif", "保留 EXIF", "bool", True,
           hint="拍摄参数、旋转方向等元信息"),
)

PDF_OPTS = (
    Option("page", "纸张尺寸", "choice", "auto",
           choices=(("auto", "跟随图片尺寸"), ("a4", "A4"), ("letter", "Letter"))),
    Option("margin", "页边距", "int", 0, lo=0, hi=300, hint="像素"),
    Option("quality", "JPEG 质量", "int", 85, lo=1, hi=100),
)

_PDF_DPI = 150
_PAGE_SIZES = {
    "a4": (int(8.27 * _PDF_DPI), int(11.69 * _PDF_DPI)),      # 210 × 297 mm
    "letter": (int(8.5 * _PDF_DPI), int(11 * _PDF_DPI)),
}


# --------------------------------------------------------------------------
# 图像处理小工具
# --------------------------------------------------------------------------

def _load_frames(src: Path):
    """读出所有帧。静态图是 1 帧，GIF / 动画 WEBP 会有多帧。

    必须 copy() 出来再关原文件 —— 否则 with 块结束后副本也失效。
    """
    with Image.open(src) as im:
        info = dict(im.info)
        count = getattr(im, "n_frames", 1)
        frames = []
        for i in range(count):
            try:
                im.seek(i)
            except EOFError:
                break
            im.load()
            frames.append(im.copy())
    return frames, info


def _flatten(im):
    """给透明图铺白底。

    JPEG / BMP 不存 alpha 通道，直接把 RGBA 存成 JPEG 的话透明区会变黑，
    这是最容易踩的一个坑。
    """
    transparent = im.mode in ("RGBA", "LA", "PA") or (
        im.mode == "P" and "transparency" in im.info
    )
    if transparent:
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    if im.mode != "RGB":
        return im.convert("RGB")
    return im


def _prepare(im, fmt: str):
    """把图像调成目标格式能接受的样子。"""
    if fmt in OPAQUE_ONLY:
        return _flatten(im)
    if im.mode == "P":
        # 调色板图：带透明信息就转 RGBA 保住它，否则转 RGB
        return im.convert("RGBA" if "transparency" in im.info else "RGB")
    if im.mode == "LA":
        return im.convert("RGBA")
    if im.mode in ("RGB", "RGBA", "L"):
        return im
    try:
        return im.convert("RGBA")
    except Exception:                                        # noqa: BLE001
        return im.convert("RGB")


def _resize(im, max_side: int):
    """长边超过 max_side 时等比缩小。"""
    if not max_side or max_side <= 0:
        return im
    w, h = im.size
    longest = max(w, h)
    if longest <= max_side:
        return im
    scale = max_side / longest
    return im.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                     Image.LANCZOS)


def _save(frames, out: Path, fmt: str, info: dict, opts: dict) -> None:
    kw: dict = {}

    q = opts.get("quality")
    if fmt in ("JPEG", "WEBP") and isinstance(q, int) and 1 <= q <= 100:
        kw["quality"] = q
    if fmt == "JPEG":
        kw["optimize"] = True
        kw["progressive"] = True
    if fmt == "ICO":
        # 一次生成全尺寸，Windows 会根据显示场景自己挑
        kw["sizes"] = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]

    exif = info.get("exif")
    if exif and opts.get("keep_exif", True) and fmt in EXIF_OK:
        kw["exif"] = exif

    if len(frames) > 1 and fmt in ("GIF", "WEBP"):
        frames[0].save(out, format=fmt, save_all=True, append_images=frames[1:],
                       duration=info.get("duration", 100),
                       loop=info.get("loop", 0), **kw)
        return

    try:
        frames[0].save(out, format=fmt, **kw)
    except Exception:                                        # noqa: BLE001
        # EXIF 跟目标格式不兼容会炸。不能因为元数据丢掉整张图 —— 去掉再试。
        kw.pop("exif", None)
        frames[0].save(out, format=fmt, **kw)


def _fit_page(im, size: tuple[int, int], margin: int):
    """把图等比缩放后居中放到指定纸张的白底页上。"""
    pw, ph = size
    avail_w = max(1, pw - margin * 2)
    avail_h = max(1, ph - margin * 2)
    w, h = im.size
    scale = min(avail_w / w, avail_h / h)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    page = Image.new("RGB", (pw, ph), (255, 255, 255))
    page.paste(im.resize((nw, nh), Image.LANCZOS), ((pw - nw) // 2, (ph - nh) // 2))
    return page


# --------------------------------------------------------------------------
# 插件
# --------------------------------------------------------------------------

class ImageConverter(Converter):
    id = "image"
    label = "图片格式转换"
    order = 10

    def capabilities(self) -> list[Capability]:
        caps: list[Capability] = []
        for s in IMG_EXTS:
            for d in IMG_EXTS:
                if d == s:
                    continue
                caps.append(Capability(s, d, f"转成 {d.upper()}", options=IMG_OPTS))
            caps.append(Capability(s, "pdf", "合成 PDF", multi=True,
                                   options=PDF_OPTS))
        return caps

    def available(self) -> tuple[bool, str]:
        if not _HAVE_PIL:
            return False, "需要 Pillow（pip install pillow）"
        return True, ""

    def convert(self, job: Job) -> list[Result]:
        if not _HAVE_PIL:
            return [Result.bad("缺少 Pillow，图片功能不可用")]
        if job.dst_ext == "pdf":
            return self._to_pdf(job)
        return self._to_image(job)

    # ---------------- 图片 → 图片 ----------------

    def _to_image(self, job: Job) -> list[Result]:
        src = job.first
        fmt = EXT_TO_PIL.get(job.dst_ext)
        if fmt is None:
            return [Result.bad(f"不支持输出 .{job.dst_ext}", src)]

        try:
            frames, info = _load_frames(src)
        except Exception as e:                               # noqa: BLE001
            return [Result.bad(f"读不了这张图：{e}", src)]
        if not frames:
            return [Result.bad("这张图里没有可用的画面", src)]

        max_side = job.opts.get("max_side") or 0
        prepared = [_resize(_prepare(f, fmt), max_side) for f in frames]

        out = unique_path(job.out_dir, safe_stem(src.stem), job.dst_ext)
        try:
            _save(prepared, out, fmt, info, job.opts)
        except Exception as e:                               # noqa: BLE001
            return [Result.bad(f"写出失败：{e}", src)]

        w, h = prepared[0].size
        return [Result.good(out, src=src, width=w, height=h,
                            frames=len(prepared))]

    # ---------------- 图片 → PDF ----------------

    def _to_pdf(self, job: Job) -> list[Result]:
        pages = []
        try:
            for p in job.srcs:
                frames, _info = _load_frames(p)
                if frames:
                    pages.append(_prepare(frames[0], "JPEG"))
        except Exception as e:                               # noqa: BLE001
            return [Result.bad(f"读取图片失败：{e}")]
        if not pages:
            return [Result.bad("没有可用的图片")]

        opts = job.opts
        q = opts.get("quality")
        q = q if isinstance(q, int) and 1 <= q <= 100 else 85
        page = opts.get("page", "auto")
        margin = opts.get("margin") or 0

        if len(job.srcs) == 1:
            name = safe_stem(job.srcs[0].stem)
        else:
            name = safe_stem(f"{job.srcs[0].stem} 等{len(job.srcs)}张")
        out = unique_path(job.out_dir, name, "pdf")

        try:
            if page in _PAGE_SIZES:
                pages = [_fit_page(im, _PAGE_SIZES[page], margin) for im in pages]
                pages[0].save(out, "PDF", save_all=True, append_images=pages[1:],
                              resolution=_PDF_DPI)
            else:
                pages[0].save(out, "PDF", save_all=True, append_images=pages[1:],
                              resolution=96, quality=q)
        except Exception as e:                               # noqa: BLE001
            return [Result.bad(f"生成 PDF 失败：{e}")]

        return [Result.good(out, src=job.first, pages=len(pages))]
