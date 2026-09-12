# -*- coding: utf-8 -*-
"""格式转换工作台。

把「NCM 转 MP3」单功能工具，扩展成多格式转换工作台。

三层结构：
    conv.core.registry   能力注册表 —— 汇总「什么能转成什么」
    conv.converters.*    转换器插件 —— 每个格式一族一个文件，互相隔离
    conv.core.engines    引擎探测 —— ffmpeg / LibreOffice 在不在

主程序见 outputs/main.py。
"""

__version__ = "1.1.0"
