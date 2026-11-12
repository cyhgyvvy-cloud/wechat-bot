# -*- mode: python ; coding: utf-8 -*-
"""控制台打包配置：单文件 exe，双击即开。"""

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
    a.binaries,
    a.datas,
    [],
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
)
