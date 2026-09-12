# -*- coding: utf-8 -*-
"""音视频格式转换 — 基于 FFmpeg（外置引擎）。

覆盖四类活：

1. **音频 ↔ 音频**：mp3 / wav / flac / m4a / aac / ogg / opus / wma / aiff
2. **视频 ↔ 视频**：mp4 / mkv / avi / mov / webm / flv / wmv / ts / m4v
3. **视频 → 音频**（抽音轨）：录屏转 mp3，最常见的一类需求
4. **视频 → GIF**：带调色板优化，不做的话画面会脏得没法看

设计原则（沿用「优雅降级」）：

* FFmpeg 没装时 ``available()`` 返回 False，界面上这几项置灰 + 给安装引导，
  **不影响图片 / PDF / NCM 那些功能**。
* 转换失败一律包成 ``Result.bad()``，绝不把异常抛给调用方 ——
  上层是 HTTP 服务，抛出去就是 500，用户只看到"请求失败"四个字。
* 失败时要把 **ffmpeg 的最后几行 stderr 带回去**。它报错很实在
  （"Unknown encoder 'libx264'"、"Invalid argument"），比"转换失败"有用一百倍。

踩过的坑都标在实现里。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from ..core import engines
from ..core.base import (Capability, Converter, Job, Option, safe_stem,
                         unique_path)
from ..core.result import Result

# --------------------------------------------------------------------------
# 格式表
# --------------------------------------------------------------------------

AUDIO_EXTS = ("mp3", "wav", "flac", "m4a", "aac", "ogg", "opus", "wma", "aiff")
VIDEO_EXTS = ("mp4", "mkv", "avi", "mov", "webm", "flv", "wmv", "ts", "m4v")

# 目标格式 → (音频编码器, 额外参数)
# 用 tuple 而不是 dict 的 value 是 str，方便以后加参数
AUDIO_CODEC = {
    "mp3": ("libmp3lame", ["-q:a", "2"]),
    "wav": ("pcm_s16le", []),
    "flac": ("flac", ["-compression_level", "5"]),
    "m4a": ("aac", []),
    "aac": ("aac", []),
    "ogg": ("libvorbis", ["-q:a", "5"]),
    "opus": ("libopus", []),
    "wma": ("wmav2", []),
    "aiff": ("pcm_s16be", []),
}

# 无损格式：码率参数对它们没意义，硬塞会让 ffmpeg 报错
LOSSLESS_AUDIO = ("wav", "flac", "aiff")

# 目标格式 → 视频编码器
VIDEO_CODEC = {
    "mp4": "libx264",
    "mkv": "libx264",
    "mov": "libx264",
    "flv": "libx264",
    "ts": "libx264",
    "m4v": "libx264",
    "webm": "libvpx-vp9",
    "avi": "mpeg4",          # x264 塞进 AVI 兼容性很差，mpeg4 稳
    "wmv": "wmv2",
}

# 目标格式 → 配套音频编码器（视频文件里的音轨）
VIDEO_AUDIO = {
    "mp4": "aac", "mkv": "aac", "mov": "aac", "m4v": "aac",
    "flv": "aac", "ts": "aac", "avi": "libmp3lame",
    "webm": "libopus", "wmv": "wmav2",
}

# 音频码率可选值；"keep" = 不指定，让 ffmpeg 沿用源质量
BR = "keep"

# --------------------------------------------------------------------------
# 参数声明
# --------------------------------------------------------------------------

_BITRATE = Option(
    "bitrate", "音频码率", "choice", BR,
    choices=((BR, "保持源质量"), ("320k", "320 kbps（最高）"),
             ("256k", "256 kbps"), ("192k", "192 kbps（推荐）"),
             ("128k", "128 kbps"), ("96k", "96 kbps（语音够用）")),
    hint="只对 mp3 / m4a / aac / opus / wma 这类有损格式生效",
)

AUDIO_OPTS = (
    _BITRATE,
    Option("max_rate_hz", "采样率上限", "choice", "keep",
           choices=(("keep", "保持原样"), ("48000", "48000 Hz"),
                    ("44100", "44100 Hz（CD）"), ("22050", "22050 Hz")),
           hint="降采样能明显减小文件。一般不用动"),
)

VIDEO_OPTS = (
    Option("crf", "画质", "int", 23, lo=0, hi=51,
           hint="越小越清晰、文件越大。18≈视觉无损，23 是默认，28 就比较糊了"),
    Option("preset", "编码速度", "choice", "medium",
           choices=(("ultrafast", "最快（文件最大）"), ("fast", "快"),
                    ("medium", "中等（推荐）"), ("slow", "慢（文件最小）")),
           hint="只影响编码耗时和文件大小，不影响画质"),
    Option("max_height", "分辨率上限", "choice", "0",
           choices=(("0", "保持原分辨率"), ("2160", "2160p（4K）"),
                    ("1440", "1440p（2K）"), ("1080", "1080p（全高清）"),
                    ("720", "720p（高清）"), ("480", "480p（小）")),
           hint="超出时才等比缩小。发微信、传作业建议 720p"),
)

EXTRACT_OPTS = (_BITRATE,)

GIF_OPTS = (
    Option("fps", "帧率", "int", 12, lo=1, hi=50,
           hint="10~15 帧既有动感又不会太大。超过 20 文件会暴涨"),
    Option("width", "宽度", "int", 480, lo=80, hi=1920,
           hint="像素，高度自动等比。480 适合发聊天窗口"),
    Option("start", "起点", "text", "",
           hint="从第几秒开始，如 00:01:30 或 90。留空 = 从头"),
    Option("seconds", "截取时长", "int", 0, lo=0, hi=3600,
           hint="秒。0 = 一直到结尾。做表情包一般取 3~6 秒"),
)

# --------------------------------------------------------------------------
# 超时
# --------------------------------------------------------------------------
# 视频转码是分钟级的活（4K 长视频尤其慢）。给足时间，宁慢勿断。
# 真卡住了用户可以关窗口 —— 服务端的 busy 计数会保证转换不被强杀。
TIMEOUT = 6 * 3600


def _ffmpeg() -> tuple[str, str]:
    """返回 (ffmpeg 路径, 错误说明)。"""
    eng = engines.ffmpeg()
    if not eng.found:
        return "", "没找到 FFmpeg。装一个就能解锁音视频转换：winget install Gyan.FFmpeg"
    return eng.path, ""


def _audio_quality_args(opts: dict, dst: str, extra: list[str]) -> list[str]:
    """音频质量参数。

    **这里有个必踩的坑**：用户明确指定了码率时，必须把编码器自带的默认
    VBR 参数（``-q:a``）**丢掉**。两个同时给的话，ffmpeg 用的是靠后的那个，
    ``-q:a 2`` 会把前面的 ``-b:a 320k`` 完全吃掉 ——
    现象是"设了 320k，文件大小和 96k 一模一样"，而且**不报任何错**。
    （实测 3 秒正弦波：96k 和 320k 都产出 ~15KB，就是这个问题。）
    """
    if dst in LOSSLESS_AUDIO:
        return list(extra)                 # 无损格式不要码率参数
    br = str(opts.get("bitrate") or BR)
    if br and br != BR:
        return ["-b:a", br]                # 用户指定了码率 → 用 ABR/CBR
    return list(extra)                     # 没指定 → 用编码器自己的默认质量


def _sample_args(opts: dict) -> list[str]:
    sr = str(opts.get("max_rate_hz") or "keep")
    if sr != "keep" and sr.isdigit():
        return ["-ar", sr]
    return []


def _scale_args(opts: dict) -> list[str]:
    """分辨率上限 —— **只缩不放**。

    坑：直接写 ``scale=-2:1080`` 会**把小视频放大**到 1080（源只有 240 高也照放），
    既没意义又让文件暴涨。选项名字叫"上限"，行为就必须是上限。

    用 ``min(H,ih)`` 把输入高度也算进去。逗号在 filtergraph 里是分隔符，
    所以整个表达式要用**单引号**包起来（``scale=-2:'min(1080,ih)'``）。
    实测这三种写法：``min(1080\\,ih)`` 和 ``'min(1080,ih)'`` 都正确保持 320x240，
    而 ``force_original_aspect_ratio=decrease`` **照样放大**到 1440x1080 —— 别用它。

    宽度用 ``-2`` 而不是 ``-1``：x264 要求宽高都是偶数。
    """
    h = str(opts.get("max_height") or "0")
    if h == "0" or not h.isdigit():
        return []
    return ["-vf", f"scale=-2:'min({h},ih)':flags=lanczos"]


# --------------------------------------------------------------------------
# 把 ffmpeg 的报错翻译成人话
# --------------------------------------------------------------------------
# ffmpeg 的 stderr 很实在，但全是英文技术细节，直接甩给用户等于没说。
# 这里只翻译**常见的、能指导下一步动作的**那几种；翻不出来就原样返回，
# 至少不丢信息。
_NO_STREAM = "源文件里没有可用的{noun}，这个转换做不了"

_FRIENDLY = (
    ("Output file does not contain any stream", _NO_STREAM),
    ("does not contain any stream", _NO_STREAM),
    ("Unknown encoder",
     "这个 FFmpeg 版本不支持需要的编码器，换个目标格式试试"),
    ("Decoder (codec", "这个 FFmpeg 版本没有能解这个格式的解码器"),
    ("Invalid data found when processing input",
     "这不是有效的音视频文件，或者它的格式 FFmpeg 认不出来"),
    ("moov atom not found", "文件不完整 —— 下载或复制中断过"),
    ("No such file or directory", "找不到输入文件"),
    ("Permission denied", "文件被别的程序占用了（比如播放器），先关掉再试"),
    ("Invalid argument", "参数组合不被支持（可能是分辨率或编码器限制）"),
    ("Unrecognized option", "这个 FFmpeg 版本不认某个参数"),
    ("Conversion failed", "转换没有完成"),
)


def _friendly(err: str, kind: str = "") -> str:
    """把 ffmpeg 的英文技术报错翻译成人话。

    ``kind`` 用来把"没有流"说得更具体 —— 抽音轨时说"没有音轨"，
    转视频时说"没有画面"，比笼统的"没有音轨/画面"有用。
    """
    noun = {"audio": "音轨", "video": "画面", "gif": "画面"}.get(kind, "音轨/画面")
    low = err.lower()
    for pat, msg in _FRIENDLY:
        if pat.lower() in low:
            return msg.format(noun=noun) if "{noun}" in msg else msg
    # 翻不出来就把 ffmpeg 的原话带上 —— 宁可丑，也别丢信息
    tail = [l.strip() for l in err.splitlines() if l.strip()]
    return tail[-1][:180] if tail else "FFmpeg 没有给出错误信息"


def _run(ff: str, args: list[str], kind: str = "") -> tuple[int, str]:
    """跑一次 ffmpeg。返回 (返回码, 人话错误)。"""
    cmd = [ff, "-hide_banner", "-nostdin", "-y", *args]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return 1, f"超过 {TIMEOUT // 3600} 小时还没转完，已中断"
    except OSError as e:
        return 1, f"启动 FFmpeg 失败：{e}"

    if r.returncode != 0:
        return r.returncode, _friendly((r.stderr or "").strip(), kind)
    return 0, ""

class AvConverter(Converter):
    id = "av"
    label = "音视频转换"
    order = 30
    engine = "ffmpeg"

    # 语义分组名，供能力总览按「音频 / 视频」拆板块用
    GROUP_NAMES = {"audio": "音频转换", "video": "视频转换"}

    def capabilities(self) -> list[Capability]:
        caps: list[Capability] = []

        # 1) 音频 → 音频
        for s in AUDIO_EXTS:
            for d in AUDIO_EXTS:
                if d != s:
                    caps.append(Capability(s, d, f"转成 {d.upper()}",
                                           options=AUDIO_OPTS, group="audio"))
            # 同格式重新编码 —— "把这首歌压小一点" 是很常见的需求。
            # action 必须区分开：跨格式那条 action=""，这条 action="reencode"，
            # 否则 (dst, action) 相同会在注册表里互相覆盖。
            caps.append(Capability(s, s, "重新编码（改码率 / 采样率）",
                                   action="reencode", options=AUDIO_OPTS,
                                   group="audio"))

        # 2) 视频 → 视频
        for s in VIDEO_EXTS:
            for d in VIDEO_EXTS:
                if d != s:
                    caps.append(Capability(s, d, f"转成 {d.upper()}",
                                           options=VIDEO_OPTS, group="video"))
            # 同格式重新编码 —— 最实用的一条：**把视频压小**（降分辨率 / 提 CRF）。
            # 传作业、发微信经常卡在"文件太大"，这是刚需。
            caps.append(Capability(s, s, "压缩 / 重新编码（改分辨率、画质）",
                                   action="reencode", options=VIDEO_OPTS,
                                   group="video"))

        # 3) 视频 → 音频（抽音轨）
        for s in VIDEO_EXTS:
            for d in AUDIO_EXTS:
                caps.append(Capability(s, d, f"提取音频为 {d.upper()}",
                                       action="extract", options=EXTRACT_OPTS,
                                       group="video"))

        # 4) 视频 → GIF（单独走调色板滤镜，所以 action 要区分开）
        for s in VIDEO_EXTS:
            caps.append(Capability(s, "gif", "转成 GIF 动图",
                                   action="gif", options=GIF_OPTS,
                                   group="video"))

        # 5) 音频 → 视频？不做 —— 学生场景用不上（静态封面拼视频请用剪辑软件）
        return caps

    def available(self) -> tuple[bool, str]:
        _path, why = _ffmpeg()
        return (False, why) if why else (True, "")

    def convert(self, job: Job) -> list[Result]:
        ff, why = _ffmpeg()
        if why:
            return [Result.bad(why, job.first)]

        dst = job.dst_ext
        src = job.first

        if not src.is_file():
            return [Result.bad("源文件不存在", src)]

        if dst == "gif":
            return [self._to_gif(ff, job)]
        if dst in AUDIO_EXTS:
            return [self._to_audio(ff, job)]
        if dst in VIDEO_EXTS:
            return [self._to_video(ff, job)]
        return [Result.bad(f"音视频插件不支持输出 .{dst}", src)]

    # ---------------- 视频 → GIF ----------------

    def _to_gif(self, ff: str, job: Job) -> Result:
        src = job.first
        opts = job.opts

        fps = opts.get("fps") or 12
        width = opts.get("width") or 480
        start = str(opts.get("start") or "").strip()
        seconds = opts.get("seconds") or 0

        # 调色板两遍法：直接输出 gif 会被压到 256 色且抖动很脏，
        # palettegen + paletteuse 能显著改善 —— 这是 ffmpeg 做 GIF 的标准姿势。
        vf = (f"fps={fps},scale={width}:-2:flags=lanczos,"
              f"split[s0][s1];[s0]palettegen=max_colors=256[p];[s1][p]paletteuse=dither=bayer")

        args: list[str] = []
        if start:
            args += ["-ss", start]
        args += ["-i", str(src), "-an", "-vf", vf, "-loop", "0"]
        if seconds and int(seconds) > 0:
            args += ["-t", str(int(seconds))]

        out = unique_path(job.out_dir, safe_stem(src.stem), "gif")
        args.append(str(out))

        code, err = _run(ff, args, "gif")
        if code != 0:
            return Result.bad(f"转 GIF 失败：{err}", src)
        if not out.is_file() or out.stat().st_size == 0:
            return Result.bad("转 GIF 失败：没有产出文件（源视频可能没有画面流）", src)
        return Result.good(out, src=src, kind="gif",
                           width=width, fps=fps)

    # ---------------- → 音频 ----------------

    def _to_audio(self, ff: str, job: Job) -> Result:
        src = job.first
        dst = job.dst_ext
        opts = job.opts

        codec, extra = AUDIO_CODEC.get(dst, (None, []))
        if codec is None:
            return Result.bad(f"不支持输出 .{dst}", src)

        args = [
            "-i", str(src),
            # 只取第一条音频流；没有音轨时不要报错（? 让它可选）
            "-map", "0:a:0?",
            "-vn", "-sn", "-dn",
            "-c:a", codec,
            *_audio_quality_args(opts, dst, extra),
            *_sample_args(opts),
        ]

        out = unique_path(job.out_dir, safe_stem(src.stem), dst)
        args.append(str(out))

        code, err = _run(ff, args, "audio")
        if code != 0:
            return Result.bad(f"转 {dst.upper()} 失败：{err}", src)
        if not out.is_file() or out.stat().st_size == 0:
            return Result.bad(
                f"转 {dst.upper()} 失败：没有产出音频。源文件里可能没有音轨", src)
        return Result.good(out, src=src, kind="audio")

    # ---------------- → 视频 ----------------

    def _to_video(self, ff: str, job: Job) -> Result:
        src = job.first
        dst = job.dst_ext
        opts = job.opts

        vcodec = VIDEO_CODEC.get(dst)
        acodec = VIDEO_AUDIO.get(dst)
        if vcodec is None:
            return Result.bad(f"不支持输出 .{dst}", src)

        crf = opts.get("crf")
        crf = crf if isinstance(crf, int) and 0 <= crf <= 51 else 23
        preset = str(opts.get("preset") or "medium")
        if preset not in ("ultrafast", "superfast", "veryfast", "faster",
                          "fast", "medium", "slow", "slower", "veryslow"):
            preset = "medium"

        args = [
            "-i", str(src),
            "-map", "0:v:0",          # 只取第一条画面流
            "-map", "0:a:0?",         # 音轨可选，静音视频不报错
            "-sn", "-dn",
            "-c:v", vcodec,
        ]

        if vcodec == "libvpx-vp9":
            # VP9 用 -crf + -b:v 0 才是恒定质量模式，直接给 -crf 会被忽略
            args += ["-crf", str(crf), "-b:v", "0", "-row-mt", "1"]
        elif vcodec in ("libx264", "libx265"):
            args += ["-crf", str(crf), "-preset", preset,
                     "-pix_fmt", "yuv420p"]      # 保证 Windows/手机上都能播
        # mpeg4 / wmv2 是老编码器，不认 -crf / -preset，走默认码率即可

        args += _scale_args(opts)

        if acodec:
            args += ["-c:a", acodec]

        out = unique_path(job.out_dir, safe_stem(src.stem), dst)
        args.append(str(out))

        code, err = _run(ff, args, "video")
        if code != 0:
            return Result.bad(f"转 {dst.upper()} 失败：{err}", src)
        if not out.is_file() or out.stat().st_size == 0:
            return Result.bad(
                f"转 {dst.upper()} 失败：没有产出视频。源文件里可能没有画面流", src)
        return Result.good(out, src=src, kind="video")
