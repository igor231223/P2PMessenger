# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['ui.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=[
        'peer',
        'cryptography.hazmat.primitives.asymmetric.x25519',
        'cryptography.hazmat.primitives.serialization',
        'cryptography.hazmat.primitives.ciphers.aead',
        'cryptography.hazmat.bindings._rust',
        'cryptography.hazmat.bindings._rust.x509',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['flask', 'miniupnpc', 'aiohttp'],
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
    name='P2PMessenger',
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
)
