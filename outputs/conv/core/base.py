# -*- coding: utf-8 -*-
"""转换器插件基类、能力声明、任务模型。

设计要点（对应"每个转换功能代码隔离，按需调用"）：

* 每个插件是一个独立文件，放在 ``conv/converters/`` 下。
* 插件只声明「我能把 X 转成 Y」，主程序查注册表就知道该调谁。
* 加一个新格式 = 丢一个文件进去，主程序零改动。
* 插件之间不互相 import，各自只依赖自己的引擎。
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from .result import Result


# --------------------------------------------------------------------------
# 可调参数
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Option:
    """一个可调参数的声明。主程序据此自动生成控件，插件不用写界面代码。"""

    key: str
    label: str
    kind: str = "text"                  # text | int | bool | choice
    default: object = None
    choices: tuple = ()                 # kind="choice"：[("值", "显示名"), …]
    lo: int | None = None
    hi: int | None = None
    hint: str = ""

    def normalize(self, raw: object) -> object:
        """把前端传来的原始值转成本类型，非法值一律退回默认值。"""
        if raw is None or raw == "":
            return self.default

        if self.kind == "bool":
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ("1", "true", "yes", "on", "是", "开")

        if self.kind == "int":
            try:
                v = int(float(str(raw).strip()))
            except (TypeError, ValueError):
                return self.default
            if self.lo is not None:
                v = max(self.lo, v)
            if self.hi is not None:
                v = min(self.hi, v)
            return v

        if self.kind == "choice":
            # 刻意**不**做合法性替换：非法值原样透传，让插件去报明确的错。
            # 悄悄换成默认值更糟 —— 用户传了 45°，却拿到一个 90° 的文件，
            # 而他以为转成功了。宁可报错。
            return raw

        return str(raw)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "default": self.default,
            "choices": [
                {"value": c[0] if isinstance(c, (tuple, list)) else c,
                 "text": c[1] if isinstance(c, (tuple, list)) and len(c) > 1 else str(c)}
                for c in self.choices
            ],
            "lo": self.lo,
            "hi": self.hi,
            "hint": self.hint,
        }


def opts_of(cap: "Capability") -> dict:
    """把 Capability 的 Option 列表变成 {key: Option} 方便查。"""
    return {o.key: o for o in cap.options}


def apply_options(cap: "Capability", raw: dict | None) -> dict:
    """按声明把前端传来的参数清洗成安全的值。"""
    raw = raw or {}
    return {o.key: o.normalize(raw.get(o.key)) for o in cap.options}


# --------------------------------------------------------------------------
# 能力声明
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Capability:
    """一条「源格式 → 目标格式」的声明。

    ``action`` 用来区分**同一对格式的多种操作**。典型例子是 PDF：
    合并、按页拆分、提取页、旋转、加密的目标格式全都是 ``pdf``，
    光靠 (src, dst) 分不开它们。留空表示"普通格式转换"。
    """

    src: str                    # 源扩展名，小写、不带点
    dst: str                    # 目标扩展名
    label: str = ""             # 动作说明，留空则自动生成
    action: str = ""            # 同格式多操作时的区分标识；"" = 普通转换
    multi: bool = False         # 支持多文件输入（多个合成一个输出）
    one2many: bool = False      # 单个输入会产出多个文件
    options: tuple = ()         # tuple[Option, ...]
    group: str = ""             # 语义分组（音频/视频/…），供能力总览拆分板块用

    @property
    def text(self) -> str:
        return self.label or f"{self.src.upper()} → {self.dst.upper()}"

    @property
    def key(self) -> tuple[str, str]:
        return (self.dst, self.action)

    def to_dict(self) -> dict:
        return {
            "src": self.src,
            "dst": self.dst,
            "action": self.action,
            "label": self.text,
            "multi": self.multi,
            "one2many": self.one2many,
            "options": [o.to_dict() for o in self.options],
        }


# --------------------------------------------------------------------------
# 任务模型
# --------------------------------------------------------------------------

@dataclass
class Job:
    """交给插件去干的一件活。"""

    srcs: list[Path]
    dst_ext: str
    out_dir: Path
    opts: dict = field(default_factory=dict)
    action: str = ""            # 同格式多操作时用来区分（合并/拆分/旋转…）

    @property
    def first(self) -> Path:
        return self.srcs[0]

    @property
    def stem(self) -> str:
        return self.first.stem


# --------------------------------------------------------------------------
# 插件基类
# --------------------------------------------------------------------------

class Converter(ABC):
    """转换器插件。子类只需要三件事：我是谁、我能干什么、我怎么做。"""

    id: str = ""                    # 唯一标识，用于注册和日志
    label: str = ""                 # 显示名
    order: int = 100                # 界面里的排序权重，小的靠前
    engine: str | None = None       # 依赖的外部引擎 id（ffmpeg / soffice），没有则 None

    @abstractmethod
    def capabilities(self) -> list[Capability]:
        """声明我能做哪些转换。"""

    def available(self) -> tuple[bool, str]:
        """我能不能干活。返回 (可用, 不可用原因)。

        引擎缺失时返回 (False, "需要 xx"),主程序会把对应条目置灰并显示原因，
        而不是让整个程序崩掉 —— 这是"优雅降级"的关键。
        """
        return True, ""

    def cap_available(self, cap: "Capability") -> tuple[bool, str]:
        """某**一条能力**能不能用。默认回落整插件级别的 ``available()``。

        大部分插件引擎只有一个（ffmpeg），整插件可用就全可用，用不着覆写。
        但像 Office 这种一个插件管三个应用（Word / Excel / PowerPoint）的，
        可能出现"装了 Word 没装 Excel"——这时 ``xlsx→pdf`` 得单独置灰，
        ``docx→pdf`` 却要亮着。覆写这个方法按 ``cap.src`` 分别判断即可。
        """
        return self.available()

    @abstractmethod
    def convert(self, job: Job) -> list[Result]:
        """干活。

        约定：
        * 绝不抛异常给调用方，失败也要包成 ``Result.bad()`` 返回。
        * 输出目录不存在时自己创建。
        * 文件名冲突时用 ``unique_path`` 让路，不覆盖用户已有文件。
        """


# --------------------------------------------------------------------------
# 文件系统小工具
# --------------------------------------------------------------------------

_BAD_FS = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def parse_pages(spec: str, total: int) -> list[int]:
    """解析 "1-3,5,8-" 这样的页码表达式，返回去重保序的 1-based 页码。

    支持：单页 ``5`` / 区间 ``1-3`` / 到末尾 ``5-`` / 从头 ``-3``。
    中文逗号、顿号、空格都能当分隔符。非法片段直接忽略，不报错 ——
    宁可少选几页，也不该让整个操作失败。

    这是个通用工具，所以放在 core 里而不是某个插件里：
    PDF 页面操作和 PDF 渲染都要用它，插件之间不该互相 import。
    """
    out: list[int] = []
    seen: set[int] = set()
    for part in re.split(r"[,，、;；\s]+", str(spec or "").strip()):
        if not part:
            continue

        m = re.match(r"^(\d*)\s*[-~－—]+\s*(\d*)$", part)
        if m:
            a, b = m.group(1), m.group(2)
            if not a and not b:
                continue
            start = int(a) if a else 1
            end = int(b) if b else total
            if start > end:
                start, end = end, start
            nums = range(start, end + 1)
        else:
            try:
                nums = [int(part)]
            except ValueError:
                continue

        for p in nums:
            if 1 <= p <= total and p not in seen:
                seen.add(p)
                out.append(p)
    return out


def safe_stem(name: str, fallback: str = "output") -> str:
    """把任意字符串洗成安全的文件名主干。"""
    s = _BAD_FS.sub("_", str(name)).strip(" ._")
    s = re.sub(r"\s+", " ", s)
    return s[:120] or fallback


def unique_path(d: Path, stem: str, ext: str) -> Path:
    """在 d 下找一个不冲突的路径：name.ext → name (1).ext → name (2).ext …

    刻意不覆盖同名文件 —— 用户的东西比"安静的失败"重要得多。
    """
    ext = ext.lstrip(".")
    p = d / f"{stem}.{ext}"
    if not p.exists():
        return p
    for i in range(1, 10000):
        p = d / f"{stem} ({i}).{ext}"
        if not p.exists():
            return p
    raise RuntimeError(f"{d} 下同名文件太多了")


def ensure_dir(d: str | Path) -> Path:
    p = Path(d)
    p.mkdir(parents=True, exist_ok=True)
    return p
