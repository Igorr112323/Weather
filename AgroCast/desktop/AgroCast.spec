# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent


def _data_files(source, destination):
    files = sorted(
        (path for path in source.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(source).as_posix(),
    )
    entries = []
    for path in files:
        parent = path.relative_to(source).parent.as_posix()
        target = destination if parent == "." else "%s/%s" % (destination, parent)
        entries.append((str(path), target))
    return entries


a = Analysis(
    [str(ROOT / "agrocast" / "desktop" / "app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=(
        _data_files(ROOT / "static", "static")
        + _data_files(ROOT / "world", "world")
        + _data_files(ROOT / "migrations", "migrations")
    ),
    hiddenimports=[
        "uvicorn.logging",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.lifespan.on",
        "agrocast.serve.product",
        "agrocast.desktop.app",
        "sqlalchemy.dialects.sqlite",
        "sqlalchemy.dialects.sqlite.pysqlite",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PyQt6", "psycopg", "psycopg2", "PIL"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AgroCast",
    icon=[str(ROOT / "desktop" / "agrocast.ico"), str(ROOT / "desktop" / "agrocast.icns")],
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="AgroCast")
