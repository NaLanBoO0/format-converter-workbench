# -*- coding: utf-8 -*-
"""能力注册表：汇总「什么能转成什么」，并按需加载插件。

两个关键设计：

1. **懒加载 + 失败隔离**。插件只在需要时才 import；某个插件 import 失败
   （比如它依赖的库没装），只影响它自己声明的那几条能力，别的插件照常工作。
   整个程序不会因为一个插件坏掉就起不来。

2. **显式清单兜底**。PyInstaller 打包后 ``pkgutil.iter_modules`` 有时探不到
   被冻结进 exe 的包，所以除了自动扫描还留一份模块名清单。
"""
from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from pathlib import Path

from .base import Capability, Converter
from .result import Result

PKG = "conv.converters"

# 同一个格式的多种后缀写法归一到规范名。
# 没有这一层，用户拖进来的 .jpeg / .tif 会查不到任何可转目标。
SRC_ALIAS = {
    "jpeg": "jpg",
    "jpe": "jpg",
    "tif": "tiff",
}


def normalize_ext(ext: str) -> str:
    e = str(ext).lower().lstrip(".")
    return SRC_ALIAS.get(e, e)


# 兜底清单：打包后自动扫描可能失效，按这份名单补 import。
# 文件不存在的会被静默跳过，所以可以放心把将来要加的模块先写进来。
PLUGIN_MODULES = (
    "image",        # 图片互转 / 压缩 / 合成 PDF       — Pillow
    "pdf_ops",      # PDF 合并 / 拆分 / 旋转 / 加解密    — pypdf
    "pdf_render",   # PDF → 图片（逐页 / 长图）          — pypdfium2
    "pdf_text",     # PDF → Markdown / 纯文本           — pdftotext(外置) / pypdf 兜底
    "ncm",          # NCM → MP3（复用 v1.0 解密核心）
    "av",           # 音视频（v1.2，需 FFmpeg）
    "office",       # Office 文档 → PDF（v1.3，走 Office COM，LibreOffice 兜底）
)


@dataclass
class Target:
    """一个可选的转换目标，给界面用。"""

    dst: str
    cap: Capability
    converter: Converter
    ok: bool = True
    reason: str = ""

    @property
    def action(self) -> str:
        return self.cap.action

    @property
    def key(self) -> tuple[str, str]:
        return self.cap.key

    @property
    def label(self) -> str:
        return self.cap.label or self.dst.upper()

    def to_dict(self) -> dict:
        return {
            "dst": self.dst,
            "action": self.action,
            "label": self.label,
            "converter": self.converter.id,
            "multi": self.cap.multi,
            "one2many": self.cap.one2many,
            "options": [o.to_dict() for o in self.cap.options],
            "ok": self.ok,
            "reason": self.reason,
        }


class Registry:
    def __init__(self) -> None:
        self._all: list[Converter] = []
        self._errors: dict[str, str] = {}
        self._loaded = False

    # ---------------- 加载 ----------------

    def ensure(self) -> "Registry":
        if not self._loaded:
            self.reload()
        return self

    def reload(self) -> "Registry":
        self._all = []
        self._errors = {}
        seen: set[str] = set()

        for name in self._module_names():
            mod = self._import_plugin(name)
            if mod is None:
                continue
            for obj in vars(mod).values():
                if not (isinstance(obj, type) and issubclass(obj, Converter)):
                    continue
                if obj is Converter or obj.__module__ != mod.__name__:
                    continue
                if obj.id in seen:
                    continue
                try:
                    inst = obj()
                except Exception as e:                       # noqa: BLE001
                    self._errors[name] = f"实例化失败：{e}"
                    continue
                if not inst.id:
                    self._errors[name] = f"{obj.__name__} 没定义 id"
                    continue
                seen.add(inst.id)
                self._all.append(inst)

        self._all.sort(key=lambda c: (c.order, c.id))
        self._loaded = True
        return self

    def _module_names(self) -> list[str]:
        names: list[str] = []
        try:
            pkg = importlib.import_module(PKG)
            for m in pkgutil.iter_modules(pkg.__path__):
                if not m.name.startswith("_") and m.name != "base":
                    names.append(m.name)
        except Exception:                                    # noqa: BLE001
            pass
        for n in PLUGIN_MODULES:                             # 补上清单里漏掉的
            if n not in names:
                names.append(n)
        return names

    def _import_plugin(self, name: str):
        try:
            return importlib.import_module(f"{PKG}.{name}")
        except ModuleNotFoundError as e:
            # 插件文件本身不存在 → 只是还没实现，静默跳过。
            # 但它 import 的某个第三方库不存在 → 记下来，界面要能告诉用户为什么缺功能。
            missing = (e.name or "")
            if missing.endswith(name) or missing == f"{PKG}.{name}":
                return None
            self._errors[name] = f"缺少依赖 {missing}"
            return None
        except Exception as e:                               # noqa: BLE001
            self._errors[name] = str(e)
            return None

    # ---------------- 查询 ----------------

    @property
    def converters(self) -> list[Converter]:
        return list(self._all)

    @property
    def errors(self) -> dict[str, str]:
        """加载失败的插件：{模块名: 原因}。界面可以把这些展示出来。"""
        return dict(self._errors)

    def by_id(self, cid: str) -> Converter | None:
        for c in self._all:
            if c.id == cid:
                return c
        return None

    def src_exts(self) -> list[str]:
        s: set[str] = set()
        for c in self._all:
            for cap in c.capabilities():
                s.add(cap.src)
        return sorted(s)

    def dst_exts(self) -> list[str]:
        s: set[str] = set()
        for c in self._all:
            for cap in c.capabilities():
                s.add(cap.dst)
        return sorted(s)

    def targets_for(self, src_ext: str) -> list[Target]:
        """某个扩展名能转成哪些格式（同名目标只保留最好的那个）。"""
        src_ext = normalize_ext(src_ext)

        ranked: dict[tuple, tuple[tuple[int, int], Target]] = {}
        for c in self._all:
            for cap in c.capabilities():
                if cap.src != src_ext:
                    continue
                # 按「能力」粒度算可用性 —— Office 插件装了 Word 没装 Excel，
                # xlsx→pdf 要单独灰掉，而 docx→pdf 保持可用。
                avail, why = c.cap_available(cap)
                # 排前面的优先：可用的 > 引擎缺的；同档看 order
                rank = (0 if avail else 1, c.order)
                t = Target(cap.dst, cap, c, avail, "" if avail else why)
                old = ranked.get(cap.key)
                if old is None or rank < old[0]:
                    ranked[cap.key] = (rank, t)

        items = [v for _, v in ranked.values()]
        # 稳定排序：同一目标格式的多种操作保持插件里的声明顺序
        items.sort(key=lambda t: (not t.ok, t.dst))
        return items

    def overview(self) -> list[dict]:
        """按转换类别聚合成「能力总览」，给界面的功能导航板块用。

        每个板块 = 一个插件（converter），字段：
        * ``label`` 板块名（图片格式转换 / Office 文档转 PDF…）
        * ``engine`` 依赖引擎（ffmpeg / office / …），None 表示内置库
        * ``ok`` 整插件是否可用
        * ``note`` 一句话说明支持哪些格式互转（自动生成，不手写维护）

        注意：``note`` 是根据能力清单**自动推导**的，所以以后加插件、
        加格式，这个总览不用改一行 HTML 就会跟着更新。
        """
        out: list[dict] = []
        for c in self._all:
            caps = c.capabilities()
            if not caps:
                continue
            ok, why = c.available()
            reason = "" if ok else why

            # 能力带 group（音频/视频…）→ 按 group 拆成多个板块；
            # 否则整插件一个板块。
            group_names = getattr(c, "GROUP_NAMES", None) or {}
            buckets: dict[str, list[Capability]] = {}
            for cap in caps:
                buckets.setdefault(cap.group or "", []).append(cap)

            for grp, gcaps in buckets.items():
                label = group_names.get(grp, c.label) if grp else c.label
                out.append({
                    "id": c.id + (f":{grp}" if grp else ""),
                    "label": label,
                    "engine": c.engine,
                    "ok": ok,
                    "reason": reason,
                    "note": self._summarize(gcaps),
                })
        return out

    @staticmethod
    def _summarize(caps: list[Capability]) -> str:
        """把一组能力概括成一句话，让人一眼看懂「这个板块能干嘛」。

        归纳规则（按序尝试，命中即用）：

        1. **纯格式互转**：所有 src≠dst、无 action 的能力。
           - 若某格式既是源又是目标（src∩dst），说「A、B、C 之间互转」；
           - 目标里出现的"额外产物"（如把图片合成 PDF），单独说「可合成 X」。
        2. **同格式操作**（src==dst 或带 action）：像 PDF 的合并/拆分/旋转、
           音频的重编码，本质是对同一格式的加工，归成「支持 合并/拆分/…」。
        3. **一对多 / 提取类**（src 固定、dst 多个且带 action，如 PDF 转图片、
           视频抽音轨）：说「X 转 Y、Z」。
        """
        # 先按「有没有 action / src==dst」分成两类
        plain: list[Capability] = []     # 普通格式转换（src→dst）
        ops: list[Capability] = []       # 带操作标识的（合并/拆分/每页/抽音轨…）

        for cap in caps:
            if cap.action or cap.src == cap.dst:
                ops.append(cap)
            else:
                plain.append(cap)

        parts: list[str] = []

        # ---- 1) 普通格式互转 ----
        if plain:
            srcs = {c.src for c in plain}
            dsts = {c.dst for c in plain}
            mutual = sorted(srcs & dsts)           # 既是源又是目标 = 互转
            extra_dst = sorted(dsts - srcs)        # 目标里独有的
            only_src = sorted(srcs - dsts)         # 只当源、不当目标

            # 目标里独有的格式，再拆「合成类」（multi=True，多合一）和「普通转出类」
            multi_dst = sorted({
                c.dst for c in plain if c.dst in extra_dst and c.multi})
            conv_dst = sorted(set(extra_dst) - set(multi_dst))

            if len(mutual) > 1:
                parts.append("、".join(mutual) + " 之间互转")
            elif len(mutual) == 1:
                parts.append(mutual[0] + " 互转")
            # 只当源不当目标的（单向源），说「X 转出到…」；跟独有目标一起合理解释
            if only_src:
                if conv_dst:
                    parts.append("、".join(only_src) + " 转 " + "、".join(conv_dst))
                else:
                    parts.append("、".join(only_src) + " 可转出")
            elif conv_dst:
                # 没有"只当源"的格式，但目标有独有产物（通常不会发生，兜底）
                parts.append("转 " + "、".join(conv_dst))
            if multi_dst:
                parts.append("可合成 " + "、".join(multi_dst))

        # ---- 2) 同格式操作（合并/拆分/旋转/重编码）----
        # 只取「操作动词」，去掉重复的「X 转 X」前缀
        seen_op: list[str] = []
        for c in ops:
            if c.src == c.dst:
                seen_op.append(c.text)

        # ---- 3) 一对多 / 提取类（带 action 的 src→dst：抽音轨、转GIF、每页一张…）----
        # 按 action 分组（extract 和 gif 是两种不同动作，不能混）。
        # 同一 action 跨多个源时合并 src 集合；src 太多（>4）就折叠成「…等 N 种」。
        from collections import defaultdict
        by_action: dict[str, tuple[set[str], set[str]]] = {}
        action_order: list[str] = []
        for c in ops:
            if c.src == c.dst or not c.src:
                continue
            a = c.action or c.dst
            if a not in by_action:
                by_action[a] = (set(), set())
                action_order.append(a)
            by_action[a][0].add(c.src)
            by_action[a][1].add(c.dst)

        def _srcs(txt_set: set[str]) -> str:
            s = sorted(txt_set)
            if len(s) > 4:
                return s[0] + " 等 " + str(len(s)) + " 种格式"
            return "、".join(s)

        # 去重：dst 集合完全相同的条目（如「每页一张」和「拼成长图」都转 jpg+png），
        # 只保留一条 —— 总览只关心「能转成什么」，具体方式在转换时再选。
        seen_dst: set[tuple] = set()
        for a in action_order:
            srcs, dsts = by_action[a]
            dk = tuple(sorted(dsts))
            if dk in seen_dst:
                continue
            seen_dst.add(dk)
            parts.append(_srcs(srcs) + " → " + "、".join(sorted(dsts)))

        parts.extend(dict.fromkeys(seen_op))   # 同格式操作放最后，去重保序

        return "；".join(parts) if parts else "无可用转换"

    def find(self, src_ext: str, dst_ext: str, action: str | None = None
             ) -> tuple[Converter, Capability, Target] | None:
        """挑一个干这活最合适的插件。

        ``action`` 为 None 时优先选"普通转换"（action 为空的那条）；
        传了值就精确匹配 —— PDF 的合并/拆分/旋转全靠它区分。
        """
        want = normalize_ext(dst_ext)
        cands = [t for t in self.targets_for(src_ext) if t.dst == want]
        if not cands:
            return None
        if action is None:
            for t in cands:
                if not t.cap.action:
                    return t.converter, t.cap, t
            return cands[0].converter, cands[0].cap, cands[0]
        for t in cands:
            if t.cap.action == action:
                return t.converter, t.cap, t
        return None

    def run(self, srcs: list[str | Path], dst_ext: str, out_dir: str | Path,
            opts: dict | None = None, action: str | None = None) -> list[Result]:
        """执行一次转换。任何异常都被包成 Result.bad，绝不往外抛。

        **一个输入文件至少对应一条结果** —— 这件事由注册表保证，不由插件自己操心。

        除了声明了 ``multi=True`` 的能力（多合一：PDF 合并、多张图合成 PDF），
        其余都是"一对一"，这里会**逐个文件**调用插件。

        为什么这个循环必须放在注册表里：
        ``Job`` 带的是 ``srcs`` 列表，让插件各自处理的话，很容易写成只用
        ``job.first`` —— 症状是"拖 3 张图进去选 JPG，只转出 1 张"，
        而且**不报任何错**。曾经 image / pdf_ops / pdf_render 三个插件
        全都踩了同一个坑（只有 ncm 自己写了循环，所以没被发现）。
        修在注册表层：所有插件一次性受益，以后新写的插件也不可能再犯。
        """
        from .base import Job, apply_options, ensure_dir

        if not srcs:
            return [Result.bad("没有输入文件")]

        paths = [Path(s) for s in srcs]
        out_path = ensure_dir(out_dir)
        want = normalize_ext(dst_ext)

        # ---- 情况 A：多合一（所有输入 → 一个输出）----
        # 只有第一个文件命中 multi 能力才走这条；混着不同类型的多合一是没意义的。
        first_hit = self.find(normalize_ext(paths[0].suffix), want, action)
        if first_hit and first_hit[2].ok and first_hit[1].multi:
            return self._call(first_hit[0], first_hit[1], paths, out_path, opts)

        # ---- 情况 B：一对一，逐个文件 ----
        results: list[Result] = []
        for p in paths:
            ext = normalize_ext(p.suffix)
            hit = self.find(ext, want, action)
            if hit is None:
                results.append(Result.bad(
                    f"没有能把 {ext.upper()} 转成 {want.upper()} 的插件", p))
                continue
            converter, cap, target = hit
            if not target.ok:
                results.append(Result.bad(target.reason, p))
                continue
            results.extend(self._call(converter, cap, [p], out_path, opts))

        return results or [Result.bad("没有可转换的文件")]

    def _call(self, converter, cap, paths: list[Path], out_dir: Path,
              opts: dict | None) -> list[Result]:
        """真正把活交给插件。插件抛异常也在这里兜住。"""
        from .base import Job, apply_options

        # 输出扩展名用 capability 声明的规范名（用户传 jpeg 也统一产出 jpg）
        job = Job(paths, cap.dst, out_dir, apply_options(cap, opts), cap.action)
        try:
            out = converter.convert(job)
            return out or [Result.bad("插件没有返回任何结果",
                                      paths[0] if paths else None)]
        except Exception as e:                               # noqa: BLE001
            import traceback
            return [Result.bad(f"{converter.label} 出错：{e}",
                               paths[0] if paths else None,
                               extra={"trace": traceback.format_exc()[-800:]})]


_REG: Registry | None = None


def get_registry() -> Registry:
    global _REG
    if _REG is None:
        _REG = Registry().ensure()
    return _REG
