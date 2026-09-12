# 格式转换工作台

一个本地运行的**多格式转换工具**。把文件拖进去，自动列出它能转成的所有格式，选一个就能转换。界面 + 命令行两种用法，免费、离线、不联网。

从「NCM 转 MP3」起步，逐步长成现在的多格式工作台。

## 能做什么

| 板块 | 支持的转换 |
|---|---|
| 🎵 网易云 NCM 解密 | ncm → mp3 |
| 🖼️ 图片格式转换 | bmp / gif / ico / jpg / png / tiff / webp 之间互转；多张合成 PDF |
| 📄 PDF 页面操作 | 合并多个 PDF、按页拆分、提取指定页、旋转页面、加密码、去密码 |
| 📄 PDF 转图片 | pdf → jpg / png（逐页或长图） |
| 📝 PDF 转文字 | pdf → Markdown / 纯文本 |
| 🎧 音频转换 | aac / aiff / flac / m4a / mp3 / ogg / opus / wav / wma 之间互转 |
| 🎬 视频转换 | avi / flv / m4v / mkv / mov / mp4 / ts / webm / wmv 之间互转 |
| 📑 Office 文档转 PDF | doc / docx / ppt / pptx / xls / xlsx → pdf |

## 怎么用

**界面版**：双击 `格式转换工作台.exe`，把文件拖进窗口，选目标格式，点一下即可。输出目录默认在原文件旁边。

**命令行版**：`格式转换工作台-命令行.exe`

```bash
# 单文件转换
格式转换工作台-命令行.exe 输入.pdf --to md --out ./输出目录

# 批量转换同一类型
格式转换工作台-命令行.exe a.mp4 b.mp4 --to mp3
```

## 引擎说明

程序采用「轻量库内置 + 重量引擎按需探测」的混合策略，缺了什么会在界面里提示、引导安装，不会崩溃：

| 引擎 | 用途 | 是否内置 |
|---|---|---|
| Pillow / pypdf / pypdfium2 / python-docx | 图片、PDF、文档 | ✅ 已打包进 exe |
| FFmpeg | 音视频转码 | 需自行安装，启动时自动探测 |
| Microsoft Office（COM） | Office → PDF | 已装 Office 即可用 |
| poppler（pdftotext） | PDF → Markdown（保留版面，识别更准） | 需自行安装，缺了自动降级用内置 pypdf |

> **PDF 转文字说明**：能直接提取文字层的 PDF 效果最好；**扫描件（图片型 PDF）没有文字层，无法提取**，需要 OCR。当前版本对扫描件会明确提示「需要 OCR」，不会输出空文件假装成功。

## 运行环境

- Windows 10 / 11（64 位）
- 音视频转换需要本机安装 [FFmpeg](https://ffmpeg.org/)
- PDF 转 Markdown 建议安装 [poppler](https://github.com/oschwartz10612/Poppler)（`winget install oschwartz10612.Poppler`）

## 从源码运行

```bash
pip install pillow pypdf pypdfium2 python-docx pyinstaller
python outputs/main.py
```

打包：`python outputs/build_workbench.py`

## 开源协议

MIT License — 见 [LICENSE](LICENSE)。
