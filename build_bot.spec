# -*- mode: python ; coding: utf-8 -*-
"""机器人本体打包配置（PyInstaller）。

产物：dist/微信机器人/微信机器人.exe —— 目录模式（onedir），
启动快、DLL 加载稳，便于排查；配置与日志写在 exe 同级目录。
"""
from PyInstaller.utils.hooks import (collect_data_files, collect_submodules,
                                     collect_dynamic_libs)

# wechatauto 自带朋友圈按钮模板 png，必须随包
datas = collect_data_files('wechatauto')
# uiautomation 的两个 VC140 客户端 DLL 在 bin/ 下，运行时按相对路径加载
binaries = collect_dynamic_libs('uiautomation')

hidden = [
    '_miniaudio',            # miniaudio 的 cffi 扩展，延迟 import
    '_cffi_backend',
    'win32timezone',         # pywin32 常用漏项
    'win32com.client',
    'win32api', 'win32con', 'win32gui', 'win32process',
]
hidden += collect_submodules('wechatauto')

a = Analysis(
    ['bot.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', 'cv2', 'matplotlib', 'scipy', 'pandas',
        'IPython', 'jupyter', 'pytest', 'setuptools', 'pip',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='微信机器人',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 无黑窗口；输出由 bot.py 内部重定向到 bot.log
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='微信机器人',
)
