# -*- coding: utf-8 -*-
"""NCM → MP3 — 复用 v1.0 已经验证过的解密核心。

**刻意不重写。** ncm2mp3.py 里那套 AES-128 实现和 NCM 容器解析是踩过坑才调通的，
最典型的一条是「AES 解出来必须剥 PKCS7 填充」—— 不剥的话真实文件全部转化失败，
而基于自己构造的测试夹具却照样全绿。算法只复用、不复制，是这个项目的基本原则。

ncm2mp3.py 保持原样不动，老的命令行用法和既有的 200+ 项测试都还能跑。
这里只是一个薄薄的适配层：把 v1.0 的函数包装成统一的 Converter 接口。
"""
from __future__ import annotations

from pathlib import Path

from ..core import engines
from ..core.base import Capability, Converter, Job, Option
from ..core.result import Result

try:
    # ncm2mp3.py 与 conv/ 同级（都在 outputs/ 下），直接 import 即可。
    # 打包进 exe 后同样可用 —— PyInstaller 会把它一起收进去。
    from ncm2mp3 import convert_one as _convert_one
    _HAVE_NCM = True
except Exception:                                            # noqa: BLE001
    _convert_one = None                                      # type: ignore
    _HAVE_NCM = False


NCM_OPTS = (
    Option("cover", "同时导出封面", "bool", False,
           hint="把专辑封面存成同名的 jpg"),
    Option("to_mp3", "强制转成 MP3", "bool", False,
           hint="源是无损格式（flac）时用它转成 mp3。需要 FFmpeg"),
)


class NcmConverter(Converter):
    id = "ncm"
    label = "网易云 NCM 解密"
    order = 5                       # 老本行，排最前面

    def capabilities(self) -> list[Capability]:
        return [Capability("ncm", "mp3", "解密成 MP3", options=NCM_OPTS)]

    def available(self) -> tuple[bool, str]:
        if not _HAVE_NCM:
            return False, "找不到 ncm2mp3.py（解密核心）"
        return True, ""

    def convert(self, job: Job) -> list[Result]:
        if not _HAVE_NCM:
            return [Result.bad("缺少 NCM 解密核心")]

        cover = bool(job.opts.get("cover"))
        to_mp3 = bool(job.opts.get("to_mp3"))
        ff = engines.ffmpeg().path or None

        if to_mp3 and not ff:
            return [Result.bad(
                "勾了「强制转成 MP3」但没找到 FFmpeg。"
                "装一个再试：winget install Gyan.FFmpeg")]

        results: list[Result] = []
        for src in job.srcs:
            try:
                okk, msg, outp = _convert_one(
                    str(src), str(job.out_dir), cover, to_mp3, ff)
            except Exception as e:                           # noqa: BLE001
                results.append(Result.bad(f"解密出错：{e}", src))
                continue

            if okk and outp is not None:
                # 注意：源是无损时实际产出是 .flac 而不是 .mp3，
                # 这里如实回报真实文件名，不假装它是 mp3。
                results.append(Result.good(outp, msg=msg, src=src))
            else:
                results.append(Result.bad(msg or "解密失败", src))
        return results or [Result.bad("没有输入文件")]
