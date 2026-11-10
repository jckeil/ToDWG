# -*- mode: python ; coding: utf-8 -*-

# ToDXF v1.0 — один exe, без внешних файлов рядом.
# Иконка вшивается в ресурсы и извлекается через sys._MEIPASS.

a = Analysis(
    ['ToDXF_v1.py'],
    pathex=[],
    binaries=[],
    datas=[('Logo_toDXF.ico', '.')],
    hiddenimports=[
        'ctypes.wintypes',
        'keyboard',
        'pyperclip',
        'win32timezone',
        'win32gui',
        'win32con',
        'win32api',
        'win32com',
        'win32com.client',
        'pythoncom',
        'ezdxf',
        'ezdxf.xref',
        'ezdxf.bbox',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='ToDXF_v1',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['Logo_toDXF.ico'],
)
