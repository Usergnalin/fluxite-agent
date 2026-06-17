# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['src/main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('src/', 'src/'),
    ],
    hiddenimports=[
        # cffi / PyNaCl
        '_cffi_backend',
        'cffi',
        'cffi._cffi_include',
        'cffi.recompiler',
        'pycparser',
        # nacl bindings
        'nacl',
        'nacl.bindings',
        'nacl.signing',
        'nacl.encoding',
        # requests stack
        'certifi',
        'charset_normalizer',
        'charset_normalizer.md__mypyc',
        'idna',
        'urllib3',
        'urllib3.contrib',
        'urllib3.util',
        'PIL', 'PIL.Image', 'PIL.PngImagePlugin', 'PIL.JpegImagePlugin'
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='mcagent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,       # headless agent, keep console visible
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,          # add an .ico file path here if you have one
)
