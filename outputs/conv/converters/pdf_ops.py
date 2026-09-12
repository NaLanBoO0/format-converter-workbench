# -*- coding: utf-8 -*-
"""PDF 页面操作 — 基于 pypdf。

覆盖：合并、按页拆分、提取指定页、旋转、加密、解密。

注意这些操作的目标格式**全是 pdf**，所以能力声明里用 ``action`` 区分
（见 conv.core.base.Capability）。没有 action 这个维度，"合并"和"拆分"
在注册表里会互相覆盖。

顺带记一条 PDF 的常识，免得以后又想不通：
PDF 是页面描述语言，存的是「在哪个坐标画哪些字形」，**没有段落/表格语义**。
所以"PDF 拆页"很可靠（只是挑页面），而"PDF 转 Word"天生不可能完美。
"""
from __future__ import annotations

from pathlib import Path

from ..core.base import (Capability, Converter, Job, Option, parse_pages,
                         safe_stem, unique_path)
from ..core.result import Result

try:
    from pypdf import PdfReader, PdfWriter
    _HAVE = True
except Exception:                                            # noqa: BLE001
    PdfReader = PdfWriter = None                             # type: ignore
    _HAVE = False


# 一次拆分最多产出多少个文件 —— 防止有人拖进来一本 2000 页的书，
# 瞬间在桌面刷出 2000 个文件
MAX_SPLIT = 500

_OK: tuple = ()

PAGES_OPT = Option(
    "pages", "页码范围", "text", "",
    hint='如 1-3,5,8- ；留空 = 全部。从 1 开始数',
)

ROTATE_OPTS = (
    Option("angle", "旋转角度", "choice", 90,
           choices=((90, "顺时针 90°"), (180, "180°"), (270, "顺时针 270°"))),
)

PWD_OPT = Option("password", "密码", "text", "",
                 hint="加密时是设置的新密码；解密时是原密码")


def _open(path: Path, password: str = ""):
    """打开 PDF，必要时解密。失败抛 ValueError，带上人话原因。"""
    try:
        r = PdfReader(str(path))
    except Exception as e:                                   # noqa: BLE001
        raise ValueError(f"打不开（可能不是有效 PDF）：{e}") from e

    if r.is_encrypted:
        try:
            r.decrypt(password or "")
        except Exception:                                    # noqa: BLE001
            pass
        try:
            len(r.pages)
        except Exception as e:                               # noqa: BLE001
            raise ValueError("这个 PDF 有密码，请填对密码再试") from e
    return r


def _write(writer, out: Path) -> None:
    with open(out, "wb") as f:
        writer.write(f)


class PdfOpsConverter(Converter):
    id = "pdf_ops"
    label = "PDF 页面操作"
    order = 20

    def capabilities(self) -> list[Capability]:
        return [
            Capability("pdf", "pdf", "合并成一个 PDF", action="merge",
                       multi=True),
            Capability("pdf", "pdf", "按页拆成多个 PDF", action="split",
                       one2many=True, options=(PAGES_OPT,)),
            Capability("pdf", "pdf", "提取指定页", action="extract",
                       options=(PAGES_OPT,)),
            Capability("pdf", "pdf", "旋转页面", action="rotate",
                       options=ROTATE_OPTS),
            Capability("pdf", "pdf", "加密码保护", action="encrypt",
                       options=(PWD_OPT,)),
            Capability("pdf", "pdf", "去掉密码", action="decrypt",
                       options=(PWD_OPT,)),
        ]

    def available(self) -> tuple[bool, str]:
        if not _HAVE:
            return False, "需要 pypdf（pip install pypdf）"
        return True, ""

    def convert(self, job: Job) -> list[Result]:
        if not _HAVE:
            return [Result.bad("缺少 pypdf，PDF 功能不可用")]
        return self._dispatch(job, job.action)

    def _dispatch(self, job: Job, action: str) -> list[Result]:
        if action == "merge":
            return self._merge(job)
        if action == "split":
            return self._split(job)
        if action == "extract":
            return self._extract(job)
        if action == "rotate":
            return self._rotate(job)
        if action == "encrypt":
            return self._encrypt(job)
        if action == "decrypt":
            return self._decrypt(job)
        return [Result.bad(f"不认识的操作：{action or '(空)'}")]

    # ---------------- 合并 ----------------

    def _merge(self, job: Job) -> list[Result]:
        pwd = str(job.opts.get("password") or "")
        w = PdfWriter()
        used = 0
        failed: list[str] = []
        for p in job.srcs:
            try:
                r = _open(p, pwd)
                w.append(r)
                used += 1
            except Exception as e:                           # noqa: BLE001
                failed.append(f"{p.name}: {e}")

        if not used:
            return [Result.bad("没有任何一个 PDF 能读进来：" + "；".join(failed[:3]))]

        name = (job.srcs[0].stem if used == 1
                else f"{job.srcs[0].stem} 等{used}个合并")
        out = unique_path(job.out_dir, safe_stem(name), "pdf")
        try:
            _write(w, out)
        except Exception as e:                               # noqa: BLE001
            return [Result.bad(f"写出失败：{e}")]

        r0 = Result.good(out, src=job.first, merged=used, pages=len(w.pages))
        if failed:
            r0.extra["note"] = f"{len(failed)} 个文件跳过（需密码或损坏）"
        return [r0]

    # ---------------- 拆分 ----------------

    def _split(self, job: Job) -> list[Result]:
        src = job.first
        pwd = str(job.opts.get("password") or "")
        try:
            r = _open(src, pwd)
        except ValueError as e:
            return [Result.bad(str(e), src)]

        total = len(r.pages)
        want = parse_pages(str(job.opts.get("pages") or ""), total) or list(
            range(1, total + 1))
        if len(want) > MAX_SPLIT:
            return [Result.bad(
                f"选中了 {len(want)} 页，超过单次上限 {MAX_SPLIT} 页。"
                f"请用「页码范围」分批处理")]

        results: list[Result] = []
        stem = safe_stem(src.stem)
        width = len(str(max(want))) if want else 1
        for p in want:
            out = unique_path(job.out_dir, f"{stem}_第{str(p).zfill(width)}页", "pdf")
            try:
                w = PdfWriter()
                w.add_page(r.pages[p - 1])
                _write(w, out)
                results.append(Result.good(out, src=src, page=p))
            except Exception as e:                           # noqa: BLE001
                results.append(Result.bad(f"第 {p} 页失败：{e}", src))
        return results

    # ---------------- 提取 ----------------

    def _extract(self, job: Job) -> list[Result]:
        src = job.first
        pwd = str(job.opts.get("password") or "")
        try:
            r = _open(src, pwd)
        except ValueError as e:
            return [Result.bad(str(e), src)]

        total = len(r.pages)
        spec = str(job.opts.get("pages") or "").strip()
        # 区分两种"空"：没填 = 全部；填了但解析不出有效页码 = 明确报错。
        # 混在一起的话，用户写了 "99"（越界）会被当成"全部"静默处理掉。
        if not spec:
            want = list(range(1, total + 1))
        else:
            want = parse_pages(spec, total)
            if not want:
                return [Result.bad(
                    f"没解析出有效页码（共 {total} 页）。"
                    f"请填「1-3,5」这样的格式，或留空表示全部", src)]

        w = PdfWriter()
        for p in want:
            w.add_page(r.pages[p - 1])

        tag = "提取" if len(want) != total else "全部"
        out = unique_path(job.out_dir, f"{safe_stem(src.stem)}_{tag}{len(want)}页", "pdf")
        try:
            _write(w, out)
        except Exception as e:                               # noqa: BLE001
            return [Result.bad(f"写出失败：{e}", src)]
        return [Result.good(out, src=src, pages=len(want), from_total=total)]

    # ---------------- 旋转 ----------------

    def _rotate(self, job: Job) -> list[Result]:
        src = job.first
        pwd = str(job.opts.get("password") or "")
        try:
            angle = int(job.opts.get("angle") or 90)
        except (TypeError, ValueError):
            angle = 90
        angle = angle % 360
        if angle not in (90, 180, 270):
            return [Result.bad("旋转角度只能是 90 / 180 / 270", src)]

        try:
            r = _open(src, pwd)
        except ValueError as e:
            return [Result.bad(str(e), src)]

        w = PdfWriter()
        try:
            for page in r.pages:
                w.add_page(page.rotate(angle))       # pypdf 的 rotate 返回新对象
        except Exception as e:                               # noqa: BLE001
            return [Result.bad(f"旋转失败：{e}", src)]

        out = unique_path(job.out_dir, f"{safe_stem(src.stem)}_转{angle}度", "pdf")
        try:
            _write(w, out)
        except Exception as e:                               # noqa: BLE001
            return [Result.bad(f"写出失败：{e}", src)]
        return [Result.good(out, src=src, angle=angle, pages=len(w.pages))]

    # ---------------- 加密 / 解密 ----------------

    def _encrypt(self, job: Job) -> list[Result]:
        src = job.first
        new_pwd = str(job.opts.get("password") or "").strip()
        if not new_pwd:
            return [Result.bad("请填写要设置的密码", src)]

        try:
            r = _open(src, "")
        except ValueError as e:
            return [Result.bad(str(e) + "（已加密的 PDF 请先用「去掉密码」）", src)]

        w = PdfWriter()
        try:
            w.append(r)
            w.encrypt(new_pwd)
        except Exception as e:                               # noqa: BLE001
            return [Result.bad(f"加密失败：{e}", src)]

        out = unique_path(job.out_dir, f"{safe_stem(src.stem)}_已加密", "pdf")
        try:
            _write(w, out)
        except Exception as e:                               # noqa: BLE001
            return [Result.bad(f"写出失败：{e}", src)]
        return [Result.good(out, src=src, pages=len(w.pages))]

    def _decrypt(self, job: Job) -> list[Result]:
        src = job.first
        pwd = str(job.opts.get("password") or "")

        try:
            r = _open(src, pwd)
        except ValueError as e:
            return [Result.bad(str(e), src)]

        if not PdfReader(str(src)).is_encrypted:
            return [Result.bad("这个 PDF 本来就没有密码", src)]

        w = PdfWriter()
        try:
            w.append(r)
        except Exception as e:                               # noqa: BLE001
            return [Result.bad(f"解密失败：{e}", src)]

        out = unique_path(job.out_dir, f"{safe_stem(src.stem)}_已解密", "pdf")
        try:
            _write(w, out)
        except Exception as e:                               # noqa: BLE001
            return [Result.bad(f"写出失败：{e}", src)]
        return [Result.good(out, src=src, pages=len(w.pages))]
