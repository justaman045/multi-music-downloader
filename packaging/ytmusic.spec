# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the standalone YT Music Downloader.

Builds a single self-contained executable per platform:
onefile, console enabled (the TUI needs one), the web SPA bundled as
data, and an optional Deno binary bundled so a cold first run needs no
network beyond YouTube itself.

yt-dlp is deliberately NOT bundled as a Python module: the app
provisions the official standalone yt-dlp binary into the user's data
directory on first run instead, which keeps yt-dlp's own --update
working (the in-app updater depends on it).

Usage:
    packaging/build.py            # recommended wrapper
    pyinstaller packaging/ytmusic.spec
"""

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(SPECPATH).resolve().parent

datas = [
    (str(PROJECT_ROOT / "web" / "static"), "web/static"),
]

# ytmusicapi needs its gettext .mo files at runtime — YTMusic.__init__
# does gettext.translation("base", localedir=<pkg>/locales) and fails
# with "No translation file found for domain: 'base'" when they are
# missing. PyInstaller bundles Python modules only, so collect them.
try:
    import ytmusicapi

    datas.append(
        (
            str(Path(ytmusicapi.__file__).parent / "locales"),
            "ytmusicapi/locales",
        )
    )
except ImportError:  # pragma: no cover - build env always has it
    pass

# Bundle Deno when the builder has provisioned one into
# packaging/.vendor/ (build.py downloads it per-platform). Otherwise
# the app downloads Deno on first run.
vendor_deno = PROJECT_ROOT / "packaging" / ".vendor"
if vendor_deno.exists():
    for entry in vendor_deno.iterdir():
        if entry.name.startswith("deno"):
            datas.append((str(entry), "bin"))

a = Analysis(
    [str(PROJECT_ROOT / "ytm-ll-dl-modern.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        # The web package is imported lazily (serve mode only).
        "web",
        "web.server",
        "web.bus",
        "web.downloads",
        "web.refresh",
        "web.scheduler",
        "web.playlists",
        "web.plays",
        "web.metadata",
        # Account sign-in: browser-cookie import (lazy function-level
        # imports; cryptography decrypts Chromium cookie values).
        "browsercookies",
        "cryptography",
        # Linux keyring lookup for Chromium v11 cookies (skip on
        # Windows/macOS builds where the requirement is not installed).
        *(["secretstorage", "jeepney"] if sys.platform.startswith("linux") else []),
        # Imported lazily inside functions; PyInstaller cannot see them.
        "mutagen",
        "mutagen.flac",
        "mutagen.id3",
        "mutagen.mp3",
        "mutagen.mp4",
        "ytmusicapi",
        "certifi",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # yt-dlp is provisioned as an external binary, not a module.
        "yt_dlp",
        "tkinter",
        "unittest",
        "pydoc",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="YTMusicDownloader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # the TUI needs a console; also shows first-run progress
    disable_windowed_traceback=False,
    argv_emulation=False,  # macOS: must stay off, we parse argv ourselves
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
