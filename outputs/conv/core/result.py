# -*- coding: utf-8 -*-
"""统一的转换结果模型。

所有转换器，不管内部干什么，对外都返回 ``list[Result]``。
这样主程序不需要知道任何插件的细节 —— 平铺展示就好。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Result:
    """一次转换的产出，或者一次失败的原因。

    为什么是个 list 而不是单个：一次调用可能产出多个文件。
    比如「PDF 按页拆分」是 1 入 N 出，「PDF 转图片」也是如此，
    而「多张图片合成 PDF」「多个 PDF 合并」是 N 入 1 出。
    统一返回 list，前端平铺展示即可，不用为每种情况写分支。
    """

    ok: bool
    msg: str = ""
    out: Path | None = None          # 产出的文件（失败时为 None）
    src: Path | None = None          # 对应输入（多对一时可能为 None）
    extra: dict = field(default_factory=dict)   # 附加信息：页数、尺寸、份数…

    @classmethod
    def good(cls, out: str | Path | None, msg: str = "",
             src: str | Path | None = None, **extra) -> "Result":
        p = Path(out) if out is not None else None
        if not msg:
            msg = p.name if p is not None else "完成"
        return cls(True, msg, p, Path(src) if src is not None else None, extra)

    @classmethod
    def bad(cls, msg: str, src: str | Path | None = None, **extra) -> "Result":
        return cls(False, msg, None, Path(src) if src is not None else None, extra)

    @property
    def name(self) -> str:
        return self.out.name if self.out is not None else ""

    @property
    def size(self) -> int:
        try:
            return self.out.stat().st_size if self.out is not None else 0
        except OSError:
            return 0

    def to_dict(self) -> dict:
        """给前端用的 JSON 安全形式。"""
        return {
            "ok": self.ok,
            "msg": self.msg,
            "name": self.name,
            "path": str(self.out) if self.out is not None else "",
            "size": self.size,
            "src": self.src.name if self.src is not None else "",
            **{k: v for k, v in self.extra.items() if _json_safe(v)},
        }


def _json_safe(v: object) -> bool:
    return isinstance(v, (str, int, float, bool, type(None)))


def ok_count(results: list[Result]) -> int:
    return sum(1 for r in results if r.ok)


def summary(results: list[Result]) -> str:
    """把一批结果压成一句话，用于状态栏。"""
    total = len(results)
    good = ok_count(results)
    if total == 1:
        return results[0].msg
    if good == total:
        return f"全部完成：{good} 项"
    return f"完成 {good} / {total} 项，{total - good} 项失败"
