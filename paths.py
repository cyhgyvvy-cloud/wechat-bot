# -*- coding: utf-8 -*-
"""统一的目录解析，让同一份代码既能直接跑、也能打包成 exe 跑。

源码运行：配置 / 日志 / 缓存在 .py 旁边
打包成 exe：同样在 exe 旁边，而不是 PyInstaller 的临时解压目录
（否则每次启动都是一份全新的空配置，日志也找不到）
"""
import sys
from pathlib import Path


def app_dir() -> Path:
    """可写的「程序目录」：exe 所在目录，或源码目录。"""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def bundle_dir() -> Path:
    """只读的「随程序打包的资源」目录。

    源码运行时与 app_dir 相同；onefile exe 里是临时解压目录。
    只用来读内置模板，不往里写东西。
    """
    return Path(getattr(sys, '_MEIPASS', app_dir()))


ROOT = app_dir()
BUNDLE = bundle_dir()
