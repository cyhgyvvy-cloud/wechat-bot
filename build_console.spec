# -*- mode: python ; coding: utf-8 -*-
"""控制台打包配置（PyInstaller）。

目录模式而不是单文件：单文件每次启动都要把 10MB 解压到 %TEMP%，
实测窗口出现要 5 秒；目录模式约 0.6 秒。运行库放在 _console 子目录，
不和机器人本体的 _internal 抢名字。
"""

a = Analysis(
    ['console.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['psutil'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['numpy', 'cv2', 'matplotlib', 'scipy', 'pandas', 'wechatauto'],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='机器人控制台',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
    contents_directory='_console',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='机器人控制台',
)
