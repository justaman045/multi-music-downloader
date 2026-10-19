#!/usr/bin/env python3
"""
Build the standalone single-file YT Music Downloader.

Runs PyInstaller with packaging/ytmusic.spec, optionally pre-fetching
the Deno binary for this platform so the finished executable needs no
first-run download. Produces dist/YTMusicDownloader[.exe].

Usage:
    venv/bin/python packaging/build.py
    venv/bin/python packaging/build.py --no-deno
"""

from __future__ import annotations

import argparse
import platform
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = PROJECT_ROOT / "packaging" / ".vendor"
SPEC = PROJECT_ROOT / "packaging" / "ytmusic.spec"
DIST = PROJECT_ROOT / "dist"


def deno_asset() -> str | None:
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "linux":
        if machine in {"x86_64", "amd64"}:
            return "deno-x86_64-unknown-linux-gnu.zip"
        if machine in {"aarch64", "arm64"}:
            return "deno-aarch64-unknown-linux-gnu.zip"
    elif system == "darwin":
        if machine in {"x86_64", "amd64"}:
            return "deno-x86_64-apple-darwin.zip"
        if machine in {"arm64", "aarch64"}:
            return "deno-aarch64-apple-darwin.zip"
    elif system == "windows":
        if machine in {"amd64", "x86_64"}:
            return "deno-x86_64-pc-windows-msvc.zip"
        if machine in {"arm64", "aarch64"}:
            return "deno-aarch64-pc-windows-msvc.zip"

    return None


def _download(url: str, timeout: int, *, attempts: int = 4) -> bytes:
    """Fetch a URL with retries.

    GitHub's API rate-limits by source IP, and CI runs the four platform
    builds concurrently from the same egress, so a 403 here is expected
    rather than exceptional. Back off and retry; a 404 (asset genuinely
    missing) is not retried.
    """
    import time
    import urllib.error
    import urllib.request

    delay = 3.0
    last: Exception | None = None

    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "ytm-build",
                "Accept": "application/vnd.github+json, application/octet-stream",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise
            last = exc
            # 403/429 are the rate-limit shapes; 5xx is transient.
            retryable = exc.code in {403, 429} or 500 <= exc.code < 600
        except Exception as exc:  # timeouts, resets, DNS
            last = exc
            retryable = True

        if not retryable or attempt == attempts:
            break
        print(f"  {type(last).__name__}: retrying in {delay:.0f}s "
              f"(attempt {attempt + 1}/{attempts})")
        time.sleep(delay)
        delay *= 2

    raise last if last else RuntimeError(f"could not fetch {url}")


def vendor_deno() -> None:
    """Download this platform's Deno binary into packaging/.vendor/.

    Best-effort: a failure here must not fail the build. The app already
    knows how to fetch Deno on first run, so a missing bundle only costs
    a one-time download for the user, whereas aborting would lose the
    whole executable.
    """
    import json

    asset = deno_asset()

    if asset is None:
        print(f"  No bundled Deno for {platform.system()}/{platform.machine()}; "
              "the app will download it on first run.")
        return

    try:
        print(f"  Fetching {asset} ...")
        api = json.loads(
            _download(
                "https://api.github.com/repos/denoland/deno/releases/latest",
                timeout=60,
            ).decode("utf-8")
        )

        url = next(
            (item.get("browser_download_url")
             for item in api.get("assets", [])
             if item.get("name") == asset),
            None,
        )
        if url is None:
            print(f"  Could not find {asset} in the release; skipping.")
            return

        blob = _download(url, timeout=300)

        VENDOR_DIR.mkdir(parents=True, exist_ok=True)
        archive = VENDOR_DIR / asset
        archive.write_bytes(blob)

        with zipfile.ZipFile(archive) as zf:
            member = next(
                (n for n in zf.namelist() if Path(n).name in {"deno", "deno.exe"}),
                None,
            )
            if member is None:
                print("  Deno archive had no executable; skipping.")
                archive.unlink(missing_ok=True)
                return
            target = VENDOR_DIR / Path(member).name
            with zf.open(member) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            target.chmod(
                target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            )

        archive.unlink(missing_ok=True)
        print(f"  Vendored Deno into {VENDOR_DIR}")
    except Exception as exc:
        # Degrade gracefully: build without the bundled Deno.
        print(f"  WARNING: could not vendor Deno ({type(exc).__name__}: {exc}).")
        print("  Continuing without it — the app downloads Deno on first run.")
        for leftover in VENDOR_DIR.glob("deno-*.zip"):
            leftover.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-deno",
        action="store_true",
        help="Skip vendoring Deno (smaller build; app downloads it later).",
    )
    options = parser.parse_args()

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller is required: venv/bin/pip install -r requirements-build.txt")
        return 1

    if not options.no_deno:
        vendor_deno()

    print("  Running PyInstaller ...")
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", str(SPEC), "--distpath", str(DIST),
         "--workpath", str(PROJECT_ROOT / "build")],
        cwd=str(PROJECT_ROOT),
    )

    if result.returncode != 0:
        return result.returncode

    artifact = DIST / ("YTMusicDownloader.exe" if sys.platform == "win32" else "YTMusicDownloader")
    if artifact.exists():
        size_mb = artifact.stat().st_size / (1024 * 1024)
        print()
        print(f"  Built {artifact} ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
