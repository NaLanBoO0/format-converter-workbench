# -*- coding: utf-8 -*-
"""转换器插件集合。一个文件负责一个格式族，互相隔离。

插件契约（见 ``conv.core.base.Converter``）::

    class MyConverter(Converter):
        id = "my"
        label = "我的格式"
        order = 50              # 界面排序，越小越靠前
        engine = None           # 依赖的外部引擎 id（"ffmpeg" / "soffice"）

        def capabilities(self):
            return [Capability("xxx", "yyy", "转成 YYY", options=(...))]

        def available(self):
            # 引擎缺失时返回 (False, "原因")，界面会置灰并显示原因
            return True, ""

        def convert(self, job):
            # 绝不抛异常，失败也要 Result.bad()；输出目录已由注册表确保存在
            return [Result.good(out)]

加一个新格式 = 在这里丢一个文件。主程序零改动。
"""
