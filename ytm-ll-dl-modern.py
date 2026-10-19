#!/usr/bin/env python3
"""
YT Music Downloader
===================

A self-contained, multi-account YouTube Music liked-music synchronizer.

The application is designed to run only from inside a Python
virtualenv. Set it up with:

    ./install.sh
    ./run.sh

Or manually:
    python3 -m venv venv
    venv/bin/pip install -r requirements.txt
    venv/bin/python ytm-ll-dl-modern.py

The virtualenv (./venv) holds all Python dependencies plus a
project-local Deno binary used by yt-dlp for YouTube challenges.

Features:
- Multiple YouTube Music accounts
- TUI
- Create ytmusicapi authentication files from pasted browser request headers
- Fetch the COMPLETE Liked Music playlist
- Quality selected before every download session
- Session quality/policy is immutable once downloading starts
- Best source / MP3 320 / V0 / 256 / 192 / 128 / FLAC / M4A
- Skip or replace existing files at the selected quality
- Persistent per-track inventory
- Detect missing files that were previously downloaded
- Detect liked tracks that have never been downloaded
- Detect downloaded tracks that are no longer liked
- Detect failed and skipped tracks with reasons
- Persistent JSON reports
- Cross-account duplicate detection with one-click removal
- Project-local yt-dlp, Deno and FFmpeg
"""

from __future__ import annotations

# ============================================================================
# Bootstrap
# ============================================================================

import importlib
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

APP_VERSION = "6.6.9"
APP_BUILD = "2026-10-19-expanded-player-double-tap-seek"


PROJECT_DIR = Path(__file__).resolve().parent
VENV_DIR = PROJECT_DIR / "venv"
VENV_BIN = VENV_DIR / "bin"
VENV_PYTHON = VENV_BIN / "python"

# PyInstaller (and similar freeze tools) set sys.frozen. A frozen build
# has no project-local ./venv and its own __file__ points into a
# temporary extraction dir, so external binaries (yt-dlp, Deno) and the
# user's data must live elsewhere.
IS_FROZEN = bool(getattr(sys, "frozen", False))


def resolve_data_dir() -> Path:
    """
    Where user data (accounts/, files/, config/) and runtime binaries
    (yt-dlp, Deno) live.

    - Source runs keep the historical project-local layout.
    - Frozen (standalone executable) runs use a per-user home-directory
      location so the exe works from anywhere (Downloads, USB stick).
    - YTM_DATA_DIR overrides everything, in both modes.
    """
    override = os.environ.get("YTM_DATA_DIR", "").strip()

    if override:
        return Path(override).expanduser().resolve()

    if not IS_FROZEN:
        return PROJECT_DIR

    home = Path.home()

    if sys.platform == "win32":
        base = Path(
            os.environ.get("APPDATA") or home / "AppData" / "Roaming"
        )
        return base / "YTMusicDownloader"

    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "YTMusicDownloader"

    base = Path(
        os.environ.get("XDG_DATA_HOME") or home / ".local" / "share"
    )
    return base / "yt-music-downloader"


DATA_DIR = resolve_data_dir()


def runtime_bin_dir() -> Path:
    """
    Directory holding the external yt-dlp and Deno binaries: the
    project-local venv in source runs, DATA_DIR/bin in frozen builds.
    """
    if IS_FROZEN:
        return DATA_DIR / "bin"

    return VENV_BIN


def yt_dlp_binary() -> Path:
    """
    Path of the external yt-dlp binary (with the Windows extension
    when required).
    """
    name = "yt-dlp.exe" if os.name == "nt" else "yt-dlp"

    return runtime_bin_dir() / name


def deno_binary() -> Path:
    """
    Path of the external Deno binary (with the Windows extension when
    required).
    """
    name = "deno.exe" if os.name == "nt" else "deno"

    return runtime_bin_dir() / name


def require_virtualenv() -> bool:
    """
    This application only runs from inside a virtualenv. Returns
    True when a virtualenv is active, otherwise prints setup
    instructions and returns False.

    Frozen (standalone executable) builds bundle their own runtime and
    have no ./venv, so the guard is bypassed there.
    """
    if IS_FROZEN:
        return True

    if Path(sys.prefix) == VENV_DIR:
        return True

    if sys.prefix != sys.base_prefix:
        # Running inside a different virtualenv: keep a single
        # canonical layout (./venv) so the project-local yt-dlp
        # and Deno binaries are found in a known location.
        print("=" * 72)
        print("YT MUSIC DOWNLOADER - WRONG VIRTUALENV")
        print("=" * 72)
        print()
        print("This project expects to run from its own ./venv.")
        print()
        print("    ./run.sh")
        print()
        print(f"Expected virtualenv: {VENV_DIR}")
        print(f"Active prefix:       {sys.prefix}")
        print()
        return False

    print("=" * 72)
    print("YT MUSIC DOWNLOADER")
    print("=" * 72)
    print()
    print("This application must run from inside a virtualenv.")
    print()
    print("Quick start:")
    print(f"    ./install.sh")
    print(f"    ./run.sh")
    print()
    print("Or manually:")
    print("    python3 -m venv venv")
    print("    venv/bin/pip install -r requirements.txt")
    print("    venv/bin/python ytm-ll-dl-modern.py")
    print()
    print(f"Project: {PROJECT_DIR}")
    print()
    return False


def ensure_environment() -> None:
    """
    Verify the runtime: required Python packages must be importable and
    the external Deno (and, in frozen builds, yt-dlp) binaries must
    exist. Both binaries are provisioned automatically when missing;
    missing Python packages produce clear install instructions instead
    of an ImportError.

    Frozen builds bundle their Python packages, so only the external
    binaries are provisioned there.
    """
    required = (
        "textual",
        "ytmusicapi",
        "yt_dlp",
        "imageio_ffmpeg",
    )

    if not IS_FROZEN:
        missing = [
            module
            for module in required
            if importlib.util.find_spec(module) is None
        ]

        if missing:
            print("=" * 72)
            print("YT MUSIC DOWNLOADER - MISSING DEPENDENCIES")
            print("=" * 72)
            print()
            print("The virtualenv is missing required packages:")
            for module in missing:
                print(f"    - {module}")
            print()
            print("Install them with:")
            print("    venv/bin/pip install -r requirements.txt")
            print()
            print("Then run the app again.")
            sys.exit(1)

        install_deno(VENV_BIN)
        return

    # Frozen build: provision external binaries into DATA_DIR/bin.
    binary_dir = runtime_bin_dir()
    binary_dir.mkdir(parents=True, exist_ok=True)

    install_bundled_deno(binary_dir)
    install_deno(binary_dir)
    install_yt_dlp(binary_dir)


def bundled_asset(name: str) -> Path | None:
    """
    Path of a data file bundled into a frozen build (PyInstaller puts
    datas under sys._MEIPASS), or None when running from source.
    """
    if not IS_FROZEN:
        return None

    base = getattr(sys, "_MEIPASS", None)

    if not base:
        return None

    candidate = Path(base) / name

    if candidate.exists():
        return candidate

    return None


def install_bundled_deno(bin_dir: Path) -> None:
    """
    Copy the Deno binary bundled inside the frozen executable into
    bin_dir on first run, so a cold start needs no network beyond
    YouTube itself. Errors fall through to the download path in
    install_deno().
    """
    name = "deno.exe" if os.name == "nt" else "deno"
    source = bundled_asset(f"bin/{name}")

    if source is None:
        return

    target = bin_dir / name

    if target.exists():
        return

    try:
        shutil.copy2(source, target)
        target.chmod(
            target.stat().st_mode
            | stat.S_IXUSR
            | stat.S_IXGRP
            | stat.S_IXOTH
        )
        print(f"      Installed bundled Deno to {target}")
    except Exception as error:
        print(f"      Could not install bundled Deno: {error}")


def install_yt_dlp(bin_dir: Path) -> None:
    """
    Provision the standalone yt-dlp binary for frozen builds. The
    official standalone binaries run without a Python interpreter and
    support yt-dlp's own --update, which the in-app updater relies on.
    Downloaded on first run into DATA_DIR/bin.
    """
    if os.name == "nt":
        name = "yt-dlp.exe"
        asset = "yt-dlp.exe"
    elif sys.platform == "darwin":
        name = "yt-dlp"
        asset = "yt-dlp_macos"
    else:
        name = "yt-dlp"
        asset = "yt-dlp_linux"

    target = bin_dir / name

    if target.exists():
        try:
            subprocess.run(
                [str(target), "--version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )
            return
        except Exception:
            target.unlink(missing_ok=True)

    try:
        api = github_json(
            "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
        )
    except Exception as error:
        print(f"      Could not reach GitHub for yt-dlp: {error}")
        return

    url = None

    for item in api.get("assets", []):
        if item.get("name") == asset:
            url = item.get("browser_download_url")
            break

    if not url:
        print(f"      Could not find yt-dlp asset '{asset}' in the latest release.")
        return

    print(f"      Downloading {asset}...")
    download_file(url, target)

    try:
        target.chmod(
            target.stat().st_mode
            | stat.S_IXUSR
            | stat.S_IXGRP
            | stat.S_IXOTH
        )
    except Exception:
        pass


def github_json(url: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ytm-ll-dl-modern",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(
        request,
        timeout=30,
    ) as response:
        return json.loads(
            response.read().decode("utf-8")
        )


def download_file(url: str, destination: Path) -> None:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ytm-ll-dl-modern",
        },
    )

    with urllib.request.urlopen(
        request,
        timeout=120,
    ) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)


def install_deno(bin_dir: Path) -> None:
    # Filename comes from deno_binary() so Windows gets the .exe suffix
    # everywhere (the launch path and this install path must agree).
    deno_path = bin_dir / deno_binary().name

    if deno_path.exists():
        try:
            subprocess.run(
                [
                    str(deno_path),
                    "--version",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )
            print("      Project Deno already works.")
            return
        except Exception:
            deno_path.unlink(missing_ok=True)

    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "linux":
        if machine in {"x86_64", "amd64"}:
            asset = "deno-x86_64-unknown-linux-gnu.zip"
        elif machine in {"aarch64", "arm64"}:
            asset = "deno-aarch64-unknown-linux-gnu.zip"
        elif machine in {"armv7l", "armv7"}:
            asset = "deno-armv7-unknown-linux-gnueabihf.zip"
        else:
            raise RuntimeError(
                f"Unsupported Linux architecture for automatic Deno setup: "
                f"{machine}"
            )
    elif system == "darwin":
        if machine in {"x86_64", "amd64"}:
            asset = "deno-x86_64-apple-darwin.zip"
        elif machine in {"arm64", "aarch64"}:
            asset = "deno-aarch64-apple-darwin.zip"
        else:
            raise RuntimeError(
                f"Unsupported macOS architecture for automatic Deno setup: "
                f"{machine}"
            )
    elif system == "windows":
        if machine in {"amd64", "x86_64"}:
            asset = "deno-x86_64-pc-windows-msvc.zip"
        elif machine in {"arm64", "aarch64"}:
            asset = "deno-aarch64-pc-windows-msvc.zip"
        else:
            raise RuntimeError(
                f"Unsupported Windows architecture for automatic Deno setup: "
                f"{machine}"
            )
    else:
        raise RuntimeError(
            f"Unsupported operating system for automatic Deno setup: {system}"
        )

    api = github_json(
        "https://api.github.com/repos/denoland/deno/releases/latest"
    )

    download_url = None

    for item in api.get("assets", []):
        if item.get("name") == asset:
            download_url = item.get("browser_download_url")
            break

    if not download_url:
        raise RuntimeError(
            f"Could not find Deno asset '{asset}' in the latest release."
        )

    # Extract next to the project (source runs) or inside the user's
    # data dir (frozen builds, where PROJECT_DIR is an ephemeral
    # extraction directory).
    temporary_home = str(DATA_DIR if IS_FROZEN else PROJECT_DIR)

    with tempfile.TemporaryDirectory(
        dir=temporary_home
    ) as temporary:
        temporary_dir = Path(temporary)
        archive = temporary_dir / asset

        print(f"      Downloading {asset}...")
        download_file(
            download_url,
            archive,
        )

        if asset.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                member = next(
                    (
                        name
                        for name in zf.namelist()
                        if Path(name).name
                        in {"deno", "deno.exe"}
                    ),
                    None,
                )

                if not member:
                    raise RuntimeError(
                        "Deno archive did not contain a deno executable."
                    )

                extracted = temporary_dir / Path(member).name
                with zf.open(member) as source, extracted.open("wb") as target:
                    shutil.copyfileobj(source, target)

        elif asset.endswith((".tar.gz", ".tgz")):
            with tarfile.open(archive, "r:gz") as tf:
                member = next(
                    (
                        m
                        for m in tf.getmembers()
                        if Path(m.name).name == "deno"
                    ),
                    None,
                )

                if not member:
                    raise RuntimeError(
                        "Deno archive did not contain a deno executable."
                    )

                tf.extract(member, temporary_dir)
                extracted = temporary_dir / member.name
        else:
            raise RuntimeError("Unsupported Deno archive.")

        deno_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        shutil.copy2(
            extracted,
            deno_path,
        )

    try:
        deno_path.chmod(
            deno_path.stat().st_mode
            | stat.S_IXUSR
            | stat.S_IXGRP
            | stat.S_IXOTH
        )
    except OSError:
        pass

    subprocess.run(
        [
            str(deno_path),
            "--version",
        ],
        check=True,
    )


if not require_virtualenv():
    sys.exit(1)

ensure_environment()

# ============================================================================
# Imports after bootstrap
# ============================================================================

import threading
import webbrowser
from dataclasses import dataclass

import imageio_ffmpeg
import ytmusicapi
from textual.app import App, ComposeResult
from textual.containers import (
    Container,
    Horizontal,
    Vertical,
    VerticalScroll,
)
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    ProgressBar,
    RadioButton,
    RadioSet,
    Select,
    Static,
    TextArea,
)

# ============================================================================
# Paths and configuration
# ============================================================================

# Source runs keep data project-local; frozen builds keep it in the
# user's home directory (see resolve_data_dir). YTM_DATA_DIR overrides
# both.
ACCOUNTS_DIR = DATA_DIR / "accounts"
FILES_DIR = DATA_DIR / "files"
CONFIG_DIR = DATA_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "settings.json"

DEFAULT_SETTINGS = {
    "default_quality": "best",
    "default_existing_policy": "skip",
    "default_session": "loop",
    "default_limit": 25,
    "retry_attempts": 3,
    "retry_delay": 3,
    "auto_refresh_interval_hours": 0,
    "auto_download_new_likes": False,
    "premium_retry_interval_days": 7,
    "max_parallel_downloads": 1,
    "download_rate_limit": "",
    "backup_interval_hours": 0,
    "notifications_enabled": True,
    "lastfm_enabled": False,
    "lastfm_api_key": "",
    "lastfm_api_secret": "",
    "lastfm_username": "",
    "lastfm_password_md5": "",
}


INVENTORY_LOCKS: dict[str, threading.Lock] = {}


def account_inventory_lock(
    slug: str,
) -> threading.Lock:
    return INVENTORY_LOCKS.setdefault(
        slug,
        threading.Lock(),
    )


QUALITY_OPTIONS = [
    (
        "Best available source audio",
        "best",
    ),
    (
        "MP3 — 320 kbps",
        "mp3_320",
    ),
    (
        "MP3 — Best VBR (V0)",
        "mp3_v0",
    ),
    (
        "MP3 — 256 kbps",
        "mp3_256",
    ),
    (
        "MP3 — 192 kbps",
        "mp3_192",
    ),
    (
        "MP3 — 128 kbps",
        "mp3_128",
    ),
    (
        "FLAC — lossless container of decoded source",
        "flac",
    ),
    (
        "M4A/AAC — 256 kbps",
        "m4a_256",
    ),
]


def quality_label(quality: str) -> str:
    for label, value in QUALITY_OPTIONS:
        if value == quality:
            return label
    return quality


def quality_short_label(quality: str) -> str:
    return {
        "best": "Best",
        "mp3_320": "MP3 320",
        "mp3_v0": "MP3 V0",
        "mp3_256": "MP3 256",
        "mp3_192": "MP3 192",
        "mp3_128": "MP3 128",
        "flac": "FLAC",
        "m4a_256": "M4A 256",
    }.get(
        quality,
        quality,
    )


def session_mode_label(session_mode: str) -> str:
    return {
        "once": "Run once",
        "loop": "Loop until stopped",
        "skip": "Download missing across all qualities",
    }.get(
        session_mode,
        session_mode,
    )


def checked_radio_value(
    widget: Widget,
    name: str,
) -> str | None:
    for radio_set in widget.query(
        RadioSet
    ):
        if radio_set.name != name:
            continue

        for button in radio_set.query(
            RadioButton
        ):
            if button.value:
                return button.id

    return None


def build_sessions(
    session: DownloadSession,
) -> list[DownloadSession]:
    if session.session == "skip":
        return [
            DownloadSession(
                quality=quality_value,
                existing_policy="skip",
                session="once",
            )
            for quality_value, _label in QUALITY_OPTIONS
        ]

    return [
        session
    ]


def quality_directory(
    account: dict[str, Any],
    quality: str,
) -> Path:
    directory = (
        account["output"]
        / names_for_quality(quality)
    )

    directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    return directory


def ensure_directories() -> None:
    for directory in (
        ACCOUNTS_DIR,
        FILES_DIR,
        CONFIG_DIR,
    ):
        directory.mkdir(
            parents=True,
            exist_ok=True,
        )


def load_settings() -> dict[str, Any]:
    ensure_directories()

    if not CONFIG_FILE.exists():
        save_settings(DEFAULT_SETTINGS.copy())
        return DEFAULT_SETTINGS.copy()

    try:
        with CONFIG_FILE.open(
            "r",
            encoding="utf-8",
        ) as fh:
            data = json.load(fh)

        settings = DEFAULT_SETTINGS.copy()

        if isinstance(data, dict):
            settings.update(data)

        return settings
    except Exception:
        return DEFAULT_SETTINGS.copy()


def save_settings(
    settings: dict[str, Any],
) -> None:
    CONFIG_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary = CONFIG_FILE.with_suffix(".tmp")

    with temporary.open(
        "w",
        encoding="utf-8",
    ) as fh:
        json.dump(
            settings,
            fh,
            indent=2,
            ensure_ascii=False,
        )

    temporary.replace(CONFIG_FILE)


def slugify(value: str) -> str:
    value = str(value).strip()

    value = re.sub(
        r'[<>:"/\\|?*\x00-\x1f]',
        "_",
        value,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip(" .") or "account"


def account_slug(value: str) -> str:
    value = slugify(value).lower()

    value = re.sub(
        r"[^a-z0-9._-]+",
        "_",
        value,
    )

    value = re.sub(
        r"_+",
        "_",
        value,
    )

    return value.strip("_") or "account"


def safe_filename(value: str) -> str:
    value = str(value)

    value = re.sub(
        r'[<>:"/\\|?*\x00-\x1f]',
        "_",
        value,
    )

    return value.strip(" .") or "unknown"


# ============================================================================
# Account management
# ============================================================================

def account_output_directory(
    name: str,
) -> Path:
    return FILES_DIR / account_slug(name)


def get_accounts() -> list[dict[str, Any]]:
    ensure_directories()

    result = []

    for directory in sorted(
        ACCOUNTS_DIR.iterdir(),
        key=lambda path: path.name.lower(),
    ):
        if not directory.is_dir():
            continue

        auth = directory / ".headers_auth.json"

        if not auth.exists():
            continue

        metadata_file = directory / "account.json"

        name = directory.name.replace(
            "_",
            " ",
        ).title()

        if metadata_file.exists():
            try:
                with metadata_file.open(
                    "r",
                    encoding="utf-8",
                ) as fh:
                    metadata = json.load(fh)

                name = metadata.get(
                    "name",
                    name,
                )
            except Exception:
                pass

        result.append(
            {
                "name": name,
                "slug": directory.name,
                "directory": directory,
                "auth": auth,
                "output": account_output_directory(name),
                "track_count": (
                    metadata.get("track_count", 0)
                    if isinstance(metadata, dict)
                    else 0
                ),
            }
        )

    return result


def account_creation_error(
    error: Exception,
    source: str | None = None,
) -> ValueError:
    """
    Turn a failed account-creation attempt into an actionable message.

    A session that is not signed in makes YouTube return a sign-in
    prompt page instead of the liked-songs playlist; that surfaces as
    a navigation KeyError whose raw text is a wall of renderer JSON.
    Users must always see the real reason, so detect that shape and
    say exactly what to do about it.
    """
    message = str(error)
    if (
        "twoColumnBrowseResultsRenderer" in message
        or "singleColumnBrowseResultsRenderer" in message
    ):
        text = (
            "YouTube returned a sign-in prompt instead of your library — "
            "this session is not signed in to YouTube Music. Note that "
            "playing music works while signed out: open "
            "music.youtube.com, check the avatar at the top right, sign "
            "in there (or pick a different profile), then retry."
        )
    else:
        text = f"Failed to create account: {message[:400]}"
    if source:
        text += f" [source: {source}]"
    return ValueError(text)


def create_account(
    name: str,
    headers_raw: str,
    source: str | None = None,
) -> dict[str, Any]:
    name = name.strip()
    headers_raw = headers_raw.strip()

    if not name:
        raise ValueError(
            "Account name is required."
        )

    if not headers_raw:
        raise ValueError(
            "Request headers are required."
        )

    slug = account_slug(name)
    directory = ACCOUNTS_DIR / slug

    if directory.exists():
        raise ValueError(
            f"Account '{name}' already exists."
        )

    directory.mkdir(
        parents=True,
        exist_ok=False,
    )

    auth_file = directory / ".headers_auth.json"

    try:
        ytmusicapi.setup(
            filepath=str(auth_file),
            headers_raw=headers_raw,
        )

        if not auth_file.exists():
            raise RuntimeError(
                "Authentication file was not created."
            )

        try:
            auth_file.chmod(
                stat.S_IRUSR
                | stat.S_IWUSR
            )
        except OSError:
            pass

        from ytmusicapi import YTMusic

        ytm = build_ytmusic_client(auth_file)

        data = ytm.get_liked_songs(10000)

        tracks = deduplicate_tracks(
            data.get("tracks", [])
        )

        output = account_output_directory(
            name
        )

        output.mkdir(
            parents=True,
            exist_ok=True,
        )

        with (
            directory / "account.json"
        ).open(
            "w",
            encoding="utf-8",
        ) as fh:
            json.dump(
                {
                    "name": name,
                    "slug": slug,
                    "created_at": int(time.time()),
                },
                fh,
                indent=2,
                ensure_ascii=False,
            )

        return {
            "name": name,
            "slug": slug,
            "directory": directory,
            "auth": auth_file,
            "output": output,
            "track_count": len(tracks),
        }

    except Exception as error:
        shutil.rmtree(
            directory,
            ignore_errors=True,
        )
        raise account_creation_error(
            error,
            source,
        ) from error



def build_ytmusic_client(auth_path: Any) -> Any:
    """
    Construct a YTMusic client from an account auth file.

    Accounts store browser request headers — either pasted manually
    from DevTools or imported automatically from the user's browser
    cookie store. Both load as plain header-based auth.
    """
    from ytmusicapi import YTMusic

    return YTMusic(
        str(
            Path(auth_path)
        )
    )


def list_importable_browsers() -> list[dict[str, Any]]:
    """
    Detect which installed browsers can automatically supply their
    YouTube sign-in cookies for account creation.
    """
    import browsercookies

    return browsercookies.list_browsers()


def import_account_from_browser(
    name: str,
    browser: str,
    profile: str | None = None,
) -> dict[str, Any]:
    """
    Create an account by importing the signed-in youtube.com cookies
    from the user's own browser — the automatic equivalent of the
    manual "copy request headers" paste.

    This replaces Google OAuth sign-in: Google blocks third-party
    OAuth apps from reading a YouTube Music library (youtubei rejects
    every third-party OAuth token, and the official Data API needs a
    user-owned cloud project), so browser cookies are the reliable
    automatic path.
    """
    import browsercookies

    # Resolve auto-detection here (not inside header building) so the
    # account's error messages can say which profile was actually
    # used. Validation against YouTube happens inside auto_profile().
    resolved = (profile or "").strip() or None
    if resolved is None and browser != "firefox":
        resolved = browsercookies.auto_profile(browser)

    headers_raw = browsercookies.headers_raw_for(
        browser,
        resolved,
    )

    source = str(browser)
    if resolved:
        source += f" profile {resolved!r}"

    # Presence check before committing to the import: an expired or
    # signed-out session makes YouTube serve a sign-in prompt, and the
    # failure is easy to misread because YouTube Music playback works
    # while signed out. header_is_live() returning None (offline, no
    # marker) never blocks — create_account reports real reasons.
    cookie_line = next(
        (
            line[len("cookie: ") :]
            for line in headers_raw.splitlines()
            if line.lower().startswith("cookie: ")
        ),
        "",
    )
    if cookie_line and browsercookies.header_is_live(cookie_line) is False:
        raise ValueError(
            f"{source} has valid cookie data, but YouTube reports that "
            "Google session as signed out. Playing music on YouTube "
            "Music works while signed out, so open music.youtube.com "
            "in that browser, confirm the avatar at the top right "
            "(sign in if prompted), then retry."
        )

    return create_account(
        name,
        headers_raw,
        source=source,
    )



def save_account_metadata(
    account: dict[str, Any],
    track_count: int | None = None,
) -> None:
    """
    Persist account metadata locally inside the project.

    Authentication remains in accounts/<slug>/.headers_auth.json.
    This only updates the small account.json metadata file.
    """
    metadata_file = Path(account["directory"]) / "account.json"

    metadata = {
        "name": account["name"],
        "slug": account["slug"],
    }

    if metadata_file.exists():
        try:
            with metadata_file.open(
                "r",
                encoding="utf-8",
            ) as fh:
                existing = json.load(fh)

            if isinstance(existing, dict):
                metadata.update(existing)
        except Exception:
            pass

    metadata["name"] = account["name"]
    metadata["slug"] = account["slug"]

    if track_count is not None:
        metadata["track_count"] = int(track_count)

    with metadata_file.open(
        "w",
        encoding="utf-8",
    ) as fh:
        json.dump(
            metadata,
            fh,
            indent=2,
            ensure_ascii=False,
        )


def delete_account(
    account: dict[str, Any],
) -> bool:
    directory = account["directory"]

    if not directory.exists():
        return False

    shutil.rmtree(
        directory,
        ignore_errors=True,
    )

    return True


# ============================================================================
# YouTube Music
# ============================================================================

def deduplicate_tracks(
    tracks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result = []
    seen = set()

    for track in tracks:
        video_id = track.get("videoId")

        if not video_id:
            continue

        if video_id in seen:
            continue

        seen.add(video_id)
        result.append(track)

    return result


def get_artist(
    track: dict[str, Any],
) -> str:
    artists = track.get("artists") or []

    names = []

    for artist in artists:
        name = artist.get("name")

        if name and name not in names:
            names.append(name)

    return (
        ", ".join(names)
        if names
        else "Unknown Artist"
    )


def get_album(
    track: dict[str, Any],
) -> str | None:
    album = track.get("album")

    if isinstance(album, dict):
        return album.get("name")

    return None


def track_title(
    track: dict[str, Any],
) -> str:
    return (
        track.get("title")
        or "Unknown Title"
    )


def track_filename(
    track: dict[str, Any],
    quality: str,
    extension: str | None = None,
) -> str:
    artist = safe_filename(
        get_artist(track)
    )

    title = safe_filename(
        track_title(track)
    )

    if extension is None:
        if quality == "best":
            # Best-source files can be Opus/WebM/M4A depending on what
            # YouTube provides. Inventory will use the actual extension
            # once the source has been downloaded.
            extension = "m4a"
        elif quality == "flac":
            extension = "flac"
        elif quality == "m4a_256":
            extension = "m4a"
        else:
            extension = "mp3"

    extension = extension.lstrip(".")

    return (
        f"{artist}, {title}.{extension}"
    )


def find_existing_quality_file(
    track: dict[str, Any],
    quality_dir: Path,
    quality: str,
) -> Path | None:
    if quality == "best":
        prefix = (
            f"{safe_filename(get_artist(track))}, "
            f"{safe_filename(track_title(track))}."
        )

        for candidate in quality_dir.iterdir():
            if candidate.is_file() and candidate.name.startswith(prefix):
                return candidate

        return None

    candidate = quality_dir / track_filename(
        track,
        quality,
    )

    return candidate if candidate.exists() else None


def fetch_all_liked_tracks(
    account: dict[str, Any],
) -> list[dict[str, Any]]:
    from ytmusicapi import YTMusic

    ytm = build_ytmusic_client(account["auth"])

    # Request a deliberately large limit. ytmusicapi returns the
    # complete available liked-music library rather than restricting
    # the application's library to a small UI/download limit.
    data = ytm.get_liked_songs(
        10000
    )

    return deduplicate_tracks(
        data.get("tracks", [])
    )


def fetch_most_played(
    account: dict[str, Any],
) -> tuple[
    list[dict[str, Any]],
    dict[str, int],
]:
    """
    Rank the account's watch history by play frequency. History
    items already have the videoId/title/artists/album shape the
    rest of the app consumes, so they can be fed straight into a
    DownloadWorker. Returns (ranked tracks, plays per video id).
    """
    from ytmusicapi import YTMusic

    ytm = build_ytmusic_client(account["auth"])

    history = ytm.get_history()

    plays: dict[str, int] = {}
    metadata: dict[str, dict[str, Any]] = {}
    first_position: dict[str, int] = {}

    for position, item in enumerate(
        history
    ):
        video_id = item.get("videoId")

        if not video_id:
            continue

        if video_id not in plays:
            plays[video_id] = 0
            metadata[video_id] = item
            first_position[video_id] = position

        plays[video_id] += 1

    ranked_ids = sorted(
        plays,
        key=lambda video_id: (
            -plays[video_id],
            first_position[video_id],
        ),
    )

    return (
        [
            metadata[video_id]
            for video_id in ranked_ids
        ],
        plays,
    )


def save_most_played_list(
    account: dict[str, Any],
    tracks: list[dict[str, Any]],
    plays: dict[str, int],
) -> None:
    output = (
        account["output"]
        / "Most_Played"
    )

    output.mkdir(
        parents=True,
        exist_ok=True,
    )

    file = output / ".most_played.json"
    temporary = file.with_suffix(".tmp")

    entries = []

    for track in tracks:
        video_id = track.get(
            "videoId"
        )

        entries.append(
            {
                "video_id": video_id,
                "plays": plays.get(
                    video_id,
                    0,
                ),
                "title": track_title(
                    track
                ),
                "artist": get_artist(
                    track
                ),
            }
        )

    with temporary.open(
        "w",
        encoding="utf-8",
    ) as fh:
        json.dump(
            {
                "version": 1,
                "timestamp": int(time.time()),
                "tracks": entries,
            },
            fh,
            indent=2,
            ensure_ascii=False,
        )

    temporary.replace(file)


# ============================================================================
# Search / playlists / auth health
# ============================================================================

def search_tracks(
    query: str,
    limit: int = 10,
    account: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Search YouTube Music for tracks. When an account is supplied the
    search runs authenticated (matching the liked-library shapes);
    otherwise it runs anonymously with a default browser session.
    """
    from ytmusicapi import YTMusic

    if account is not None:
        ytm = build_ytmusic_client(account["auth"])
    else:
        ytm = YTMusic()

    try:
        results = ytm.search(
            query,
            filter="songs",
            limit=int(limit),
        )
    except Exception:
        results = ytm.search(
            query,
            limit=int(limit),
        )

    tracks = []
    for item in results:
        if not isinstance(item, dict):
            continue
        video_id = item.get("videoId")
        if not video_id:
            continue
        if "lyrics" in video_id:
            continue
        tracks.append(item)

    return tracks


def fetch_library_playlists(
    account: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Fetch the account's YouTube Music playlists (library scope).
    Returns lightweight summaries; tracks are fetched separately.
    """
    from ytmusicapi import YTMusic

    ytm = build_ytmusic_client(account["auth"])

    playlists = ytm.get_library_playlists(
        100
    )

    result = []

    for playlist in playlists:
        if not isinstance(playlist, dict):
            continue

        playlist_id = playlist.get(
            "playlistId"
        )

        if not playlist_id:
            continue

        result.append(
            {
                "id": playlist_id,
                "title": playlist.get(
                    "title",
                    "Untitled playlist",
                ),
                "description": playlist.get(
                    "description",
                    "",
                ),
                "count": int(
                    playlist.get(
                        "count",
                        0,
                    ) or 0
                ),
                "year": playlist.get(
                    "year",
                ),
                "author": playlist.get(
                    "author",
                ),
            }
        )

    return result


def fetch_playlist_tracks(
    account: dict[str, Any],
    playlist_id: str,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """
    Fetch all tracks in a YouTube Music playlist. Paginates until the
    playlist is exhausted or `limit` is reached.
    """
    from ytmusicapi import YTMusic

    ytm = build_ytmusic_client(account["auth"])

    tracks = []
    continuation = None

    while True:
        try:
            page = ytm.get_playlist(
                playlist_id,
                limit=min(
                    100,
                    max(1, limit - len(tracks)),
                ),
                related=(
                    False
                    if continuation is None
                    else continuation
                ),
            )
        except TypeError:
            page = ytm.get_playlist(
                playlist_id,
                limit=min(
                    100,
                    max(1, limit - len(tracks)),
                ),
            )

        page_tracks = page.get(
            "tracks",
            [],
        )

        tracks.extend(page_tracks)

        continuation = page.get(
            "continuation"
        )

        if not continuation:
            break

        if len(tracks) >= limit:
            break

    return deduplicate_tracks(
        tracks
    )


def sync_library_playlists(
    account: dict[str, Any],
) -> dict[str, Any]:
    """
    Fetch + cache an account's YouTube Music playlists and their
    tracks into files/<slug>/.playlists.json. On failure the cached
    copy is returned with online=False so the UI can hide it offline.
    """
    file = (
        Path(account["output"])
        / ".playlists.json"
    )

    snapshot = {
        "version": 1,
        "synced_at": int(time.time()),
        "online": True,
        "playlists": [],
    }

    try:
        playlists = fetch_library_playlists(
            account
        )

        cached = {
            playlist["id"]: playlist
            for playlist in snapshot["playlists"]
        }

        for playlist in playlists:
            playlist_id = playlist["id"]
            if playlist_id in cached:
                playlist["tracks"] = cached[
                    playlist_id
                ].get("tracks", [])
            else:
                try:
                    playlist["tracks"] = (
                        fetch_playlist_tracks(
                            account,
                            playlist_id,
                        )
                    )
                except Exception:
                    playlist["tracks"] = []

        snapshot["playlists"] = playlists
    except Exception as error:
        snapshot["online"] = False
        snapshot["error"] = str(error)
        if file.exists():
            try:
                return json.loads(
                    file.read_text(encoding="utf-8")
                )
            except Exception:
                pass

    save_playlist_snapshot(
        account,
        snapshot,
    )

    return snapshot


def save_playlist_snapshot(
    account: dict[str, Any],
    snapshot: dict[str, Any],
) -> None:
    file = (
        Path(account["output"])
        / ".playlists.json"
    )

    temporary = file.with_suffix(".tmp")

    with temporary.open(
        "w",
        encoding="utf-8",
    ) as fh:
        json.dump(
            snapshot,
            fh,
            indent=2,
            ensure_ascii=False,
        )

    temporary.replace(file)


def load_playlist_snapshot(
    account: dict[str, Any],
) -> dict[str, Any] | None:
    file = (
        Path(account["output"])
        / ".playlists.json"
    )

    if not file.exists():
        return None

    try:
        return json.loads(
            file.read_text(encoding="utf-8")
        )
    except Exception:
        return None


def probe_auth(
    account: dict[str, Any],
) -> dict[str, Any]:
    """
    Check whether the account's stored request headers still work by
    making a tiny authenticated call. Persists the result so the UI
    can warn about expired cookies before a real download fails.
    """
    try:
        from ytmusicapi import YTMusic

        ytm = build_ytmusic_client(account["auth"])
        ytm.get_library_playlists(
            1
        )
        health = {
            "healthy": True,
            "checked_at": int(time.time()),
            "error": None,
        }
    except Exception as error:
        health = {
            "healthy": False,
            "checked_at": int(time.time()),
            "error": str(error),
        }

    save_auth_health(
        account,
        health,
    )

    return health


def save_auth_health(
    account: dict[str, Any],
    health: dict[str, Any],
) -> None:
    metadata_file = (
        Path(account["directory"])
        / "account.json"
    )

    try:
        data = {}
        if metadata_file.exists():
            data = json.loads(
                metadata_file.read_text(
                    encoding="utf-8"
                )
            )

        data["auth_health"] = health

        with metadata_file.open(
            "w",
            encoding="utf-8",
        ) as fh:
            json.dump(
                data,
                fh,
                indent=2,
                ensure_ascii=False,
            )
    except Exception:
        pass


def load_auth_health(
    account: dict[str, Any],
) -> dict[str, Any]:
    metadata_file = (
        Path(account["directory"])
        / "account.json"
    )

    try:
        data = json.loads(
            metadata_file.read_text(
                encoding="utf-8"
            )
        )
        return data.get(
            "auth_health",
            {
                "healthy": None,
                "checked_at": None,
                "error": None,
            },
        )
    except Exception:
        return {
            "healthy": None,
            "checked_at": None,
            "error": None,
        }


# ============================================================================
# Library export
# ============================================================================

def export_library() -> Path:
    """
    Export settings plus every account's inventory and playlists into
    a single timestamped JSON file in config/exports.
    """
    from web import playlists as playlists_module

    snapshot = {
        "app_version": APP_VERSION,
        "app_build": APP_BUILD,
        "created_at": int(time.time()),
        "settings": load_settings(),
        "accounts": [],
        "playlists": playlists_module.load_custom_playlists(
            sys.modules[__name__]
        ),
    }

    for account in get_accounts():
        with account_inventory_lock(
            account["slug"]
        ):
            snapshot["accounts"].append(
                {
                    "slug": account["slug"],
                    "name": account["name"],
                    "inventory": load_inventory(
                        account
                    ),
                    "playlists": load_playlist_snapshot(
                        account
                    ),
                    "auth_health": load_auth_health(
                        account
                    ),
                }
            )

    return _export_json(
        snapshot,
        "library",
    )


# ============================================================================
# Persistent inventory
# ============================================================================

def inventory_file(
    account: dict[str, Any],
) -> Path:
    return (
        account["output"]
        / ".inventory.json"
    )


def load_inventory(
    account: dict[str, Any],
) -> dict[str, Any]:
    file = inventory_file(account)

    if not file.exists():
        return {
            "version": 2,
            "tracks": {},
        }

    try:
        with file.open(
            "r",
            encoding="utf-8",
        ) as fh:
            data = json.load(fh)

        if not isinstance(data, dict):
            raise ValueError()

        data.setdefault(
            "version",
            2,
        )
        data.setdefault(
            "tracks",
            {},
        )

        return data

    except Exception:
        return {
            "version": 2,
            "tracks": {},
        }


def save_inventory(
    account: dict[str, Any],
    inventory: dict[str, Any],
) -> None:
    account["output"].mkdir(
        parents=True,
        exist_ok=True,
    )

    file = inventory_file(account)
    temporary = file.with_suffix(".tmp")

    with temporary.open(
        "w",
        encoding="utf-8",
    ) as fh:
        json.dump(
            inventory,
            fh,
            indent=2,
            ensure_ascii=False,
        )

    temporary.replace(file)


def inventory_current_tracks(
    account: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Build track-like dicts from inventory records whose last
    refresh marked them as currently liked. Lets Library Status
    run fully from disk when no in-memory refresh happened yet.
    """
    inventory = load_inventory(
        account
    )

    tracks = []

    for video_id, record in inventory[
        "tracks"
    ].items():
        if not record.get(
            "currently_liked"
        ):
            continue

        album = record.get(
            "album"
        )

        tracks.append(
            {
                "videoId": video_id,
                "title": record.get(
                    "title"
                )
                or "Unknown Title",
                "artists": [
                    {
                        "name": record.get(
                            "artist"
                        )
                        or "Unknown Artist",
                    }
                ],
                "album": (
                    {"name": album}
                    if album
                    else None
                ),
            }
        )

    return tracks


def repair_pending_tracks(
    account: dict[str, Any],
    quality: str,
) -> list[dict[str, Any]]:
    """
    Currently-liked tracks with no healthy file at the given
    quality (never downloaded, failed, missing, or unavailable).
    Used to rebuild a quality tier without re-downloading files
    that are already present and playable.
    """
    with account_inventory_lock(
        account["slug"]
    ):
        inventory = load_inventory(
            account
        )

    quality_dir = quality_directory(
        account,
        quality,
    )

    pending = []

    for video_id, record in (
        inventory["tracks"].items()
    ):
        if not record.get(
            "currently_liked"
        ):
            continue

        # Premium-locked videos can never be repaired with the
        # current account; don't burn a retry on them.
        if is_premium_blocked(record):
            continue

        album = record.get("album")

        track = {
            "videoId": video_id,
            "title": record.get(
                "title",
                "Unknown Title",
            ),
            "artists": [
                {
                    "name": record.get(
                        "artist",
                        "Unknown Artist",
                    )
                }
            ],
            "album": (
                {"name": album}
                if album
                else None
            ),
        }

        expected = find_existing_quality_file(
            track,
            quality_dir,
            quality,
        )

        recorded_file = (
            record.get("qualities") or {}
        ).get(quality, {}).get(
            "file"
        )

        if expected is None and recorded_file:
            recorded_path = Path(
                recorded_file
            )

            if recorded_path.exists():
                expected = recorded_path

        if (
            expected is not None
            and expected.exists()
            and expected.stat().st_size > 0
        ):
            continue

        pending.append(track)

    return pending


def verify_account_files(
    account: dict[str, Any],
) -> dict[str, Any]:
    """
    Re-check every downloaded file against the inventory.

    Returns per-track results (`ok`, `missing`, `corrupt`, `size_zero`)
    keyed by video_id. When a stored sha256 exists the file is hashed
    and compared; otherwise only existence / size is checked so files
    downloaded before hashing was introduced still get a verdict.
    """
    with account_inventory_lock(
        account["slug"]
    ):
        inventory = load_inventory(
            account
        )

    results: dict[str, Any] = {}

    for video_id, record in (
        inventory["tracks"].items()
    ):
        for quality, entry in (
            record.get("qualities") or {}
        ).items():
            if entry.get("status") != "downloaded":
                continue

            path_value = entry.get("file")
            if not path_value:
                results[video_id] = {
                    "video_id": video_id,
                    "title": record.get("title"),
                    "artist": record.get("artist"),
                    "quality": quality,
                    "status": "missing",
                    "reason": "No file recorded in inventory",
                }
                continue

            path = Path(path_value)

            if not path.exists():
                results[video_id] = {
                    "video_id": video_id,
                    "title": record.get("title"),
                    "artist": record.get("artist"),
                    "quality": quality,
                    "status": "missing",
                    "reason": "File no longer exists",
                }
                continue

            size = path.stat().st_size

            if size <= 0:
                results[video_id] = {
                    "video_id": video_id,
                    "title": record.get("title"),
                    "artist": record.get("artist"),
                    "quality": quality,
                    "status": "size_zero",
                    "reason": "File is empty",
                }
                continue

            expected = entry.get("sha256")

            if expected:
                try:
                    actual = file_sha256(path)
                except Exception as error:
                    results[video_id] = {
                        "video_id": video_id,
                        "title": record.get("title"),
                        "artist": record.get("artist"),
                        "quality": quality,
                        "status": "corrupt",
                        "reason": f"Could not hash file: {error}",
                    }
                    continue

                if actual != expected:
                    results[video_id] = {
                        "video_id": video_id,
                        "title": record.get("title"),
                        "artist": record.get("artist"),
                        "quality": quality,
                        "status": "corrupt",
                        "reason": "SHA-256 mismatch — file was modified or truncated",
                    }
                    continue

            results[video_id] = {
                "video_id": video_id,
                "title": record.get("title"),
                "artist": record.get("artist"),
                "quality": quality,
                "status": "ok",
                "reason": None,
                "sha256": entry.get("sha256"),
                "size_bytes": size,
            }

    summary = {
        "ok": sum(
            1
            for entry in results.values()
            if entry["status"] == "ok"
        ),
        "missing": sum(
            1
            for entry in results.values()
            if entry["status"] == "missing"
        ),
        "corrupt": sum(
            1
            for entry in results.values()
            if entry["status"] == "corrupt"
        ),
        "size_zero": sum(
            1
            for entry in results.values()
            if entry["status"] == "size_zero"
        ),
    }

    return {
        "account": account["slug"],
        "total": len(results),
        "summary": summary,
        "results": results,
    }


def status_file(
    account: dict[str, Any],
) -> Path:
    return (
        account["output"]
        / ".status.json"
    )


def save_status_snapshot(
    account: dict[str, Any],
    quality: str,
    report: dict[str, Any],
) -> None:
    """
    Persist a trimmed snapshot of the last refresh scan so the
    missing / not-downloaded lists survive across sessions.
    """
    def entry(
        item: dict[str, Any],
    ) -> dict[str, Any]:
        track = item.get(
            "track",
            {},
        )

        return {
            "video_id": item.get(
                "video_id"
            ),
            "title": track_title(
                track
            ),
            "artist": get_artist(
                track
            ),
            "reason": item.get(
                "reason"
            ),
            "file": item.get(
                "file"
            ),
        }

    snapshot = {
        "version": 1,
        "quality": quality,
        "timestamp": int(time.time()),
        "counts": {
            "liked": report.get(
                "liked_total"
            ),
            "downloaded": report.get(
                "downloaded_total"
            ),
            "not_downloaded": report.get(
                "not_downloaded_total"
            ),
            "missing": report.get(
                "missing_total"
            ),
            "no_longer_liked": report.get(
                "no_longer_liked_total"
            ),
            "failed": report.get(
                "failed_total"
            ),
            "skipped": report.get(
                "skipped_total"
            ),
            "premium": report.get(
                "premium_total"
            ),
        },
        "missing": [
            entry(item)
            for item in report.get(
                "missing_files",
                [],
            )
        ],
        "not_downloaded": [
            entry(item)
            for item in report.get(
                "not_downloaded",
                [],
            )
        ],
        "premium": [
            entry(item)
            for item in report.get(
                "premium",
                [],
            )
        ],
    }

    account["output"].mkdir(
        parents=True,
        exist_ok=True,
    )

    file = status_file(account)
    temporary = file.with_suffix(".tmp")

    with temporary.open(
        "w",
        encoding="utf-8",
    ) as fh:
        json.dump(
            snapshot,
            fh,
            indent=2,
            ensure_ascii=False,
        )

    temporary.replace(file)


def format_size(
    size_bytes: int,
) -> str:
    value = float(size_bytes)

    for unit in (
        "B",
        "KB",
        "MB",
        "GB",
        "TB",
    ):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return (
                f"{value:.2f} {unit}"
            )
        value /= 1024

    return f"{size_bytes} B"


def format_duration(
    total_seconds: float,
) -> str:
    total_seconds = int(
        round(
            total_seconds
        )
    )

    hours = (
        total_seconds
        // 3600
    )
    minutes = (
        total_seconds
        % 3600
        // 60
    )

    days = hours // 24
    hours %= 24

    if days:
        return (
            f"{days}d {hours}h {minutes}m"
        )

    if hours:
        return (
            f"{hours}h {minutes}m"
        )

    return f"{minutes}m"


def aggregate_quality_stats(
    account: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Scan every quality directory and report, per file type:
    number of files, total bytes, and total playable seconds.
    """
    from mutagen import File as MutagenFile

    quality_dirs = []

    for label, key in QUALITY_OPTIONS:
        directory = (
            account["output"]
            / names_for_quality(key)
        )

        if not directory.exists():
            continue

        files = [
            path
            for path in directory.iterdir()
            if (
                path.is_file()
                and not path.name.startswith(".ytm_")
                and not path.name.endswith(".part")
            )
        ]

        count = len(files)
        total_bytes = 0
        total_seconds = 0.0

        for path in files:
            try:
                total_bytes += (
                    path.stat().st_size
                )
            except OSError:
                continue

            try:
                audio = MutagenFile(
                    path
                )

                if (
                    audio is not None
                    and audio.info is not None
                ):
                    total_seconds += (
                        audio.info.length
                    )
            except Exception:
                pass

        quality_dirs.append(
            {
                "key": key,
                "label": label,
                "count": count,
                "bytes": total_bytes,
                "seconds": total_seconds,
            }
        )

    return quality_dirs


def account_health_summary(
    account: dict[str, Any],
) -> dict[str, Any]:
    """
    Aggregate inventory statuses across all qualities and flag
    problems worth acting on: liked tracks with no downloaded
    file in any quality, and failed downloads on liked tracks.
    """
    with account_inventory_lock(
        account["slug"]
    ):
        inventory = load_inventory(account)

    records = inventory["tracks"]

    status_counts: dict[str, int] = {}
    liked_total = 0
    liked_without_download = []
    failed_on_liked = []

    for video_id, record in records.items():
        qualities = record.get(
            "qualities",
            {},
        )
        liked = (
            record.get("currently_liked")
            is True
        )

        if liked:
            liked_total += 1

        if not qualities:
            continue

        has_download = any(
            quality_record.get("status")
            == "downloaded"
            for quality_record in (
                qualities.values()
            )
        )

        if liked and not has_download:
            if not is_premium_blocked(record):
                liked_without_download.append(
                    {
                        "video_id": video_id,
                        "title": record.get(
                            "title",
                            "Unknown Title",
                        ),
                        "artist": record.get(
                            "artist",
                            "Unknown Artist",
                        ),
                    }
                )

        for quality, quality_record in (
            qualities.items()
        ):
            status = quality_record.get(
                "status"
            )
            status_counts[status] = (
                status_counts.get(
                    status,
                    0,
                )
                + 1
            )

            if (
                status == "failed"
                and liked
                and not is_premium_blocked(record)
            ):
                failed_on_liked.append(
                    {
                        "video_id": video_id,
                        "title": record.get(
                            "title",
                            "Unknown Title",
                        ),
                        "artist": record.get(
                            "artist",
                            "Unknown Artist",
                        ),
                        "quality": quality,
                        "reason": quality_record.get(
                            "reason",
                            "",
                        ),
                    }
                )

    return {
        "status_counts": status_counts,
        "liked_total": liked_total,
        "liked_without_download": liked_without_download,
        "failed_on_liked": failed_on_liked,
    }


def names_for_quality(
    quality: str,
) -> str:
    names = {
        "best": "Best_Source",
        "mp3_320": "MP3_320kbps",
        "mp3_v0": "MP3_V0",
        "mp3_256": "MP3_256kbps",
        "mp3_192": "MP3_192kbps",
        "mp3_128": "MP3_128kbps",
        "flac": "FLAC",
        "m4a_256": "M4A_AAC_256kbps",
    }

    return names.get(
        quality,
        quality,
    )


def update_inventory_from_library(
    account: dict[str, Any],
    tracks: list[dict[str, Any]],
    mark_liked: bool = True,
    source: str = "library",
) -> dict[str, Any]:
    inventory = load_inventory(account)
    records = inventory["tracks"]

    now = int(time.time())

    current_ids = set()

    for track in tracks:
        video_id = track.get("videoId")

        if not video_id:
            continue

        current_ids.add(video_id)

        record = records.setdefault(
            video_id,
            {
                "video_id": video_id,
                "qualities": {},
            },
        )

        record["title"] = track_title(track)
        record["artist"] = get_artist(track)
        record["album"] = get_album(track)
        record["last_seen_liked"] = now
        record["currently_liked"] = bool(
            mark_liked
        )
        record["source"] = (
            source
            if not mark_liked
            else record.get("source", "library")
        )

    if mark_liked:
        for video_id, record in records.items():
            if video_id not in current_ids:
                record["currently_liked"] = False

        inventory["last_library_sync"] = now
        inventory["liked_count"] = len(
            current_ids
        )

    save_inventory(
        account,
        inventory,
    )

    return inventory


def scan_inventory(
    account: dict[str, Any],
    current_tracks: list[dict[str, Any]],
    quality: str,
) -> dict[str, Any]:
    """
    Build a live view of:
      - liked + downloaded
      - liked + not downloaded
      - previously downloaded + now missing
      - downloaded + no longer liked
      - failed
      - skipped
    """
    inventory = update_inventory_from_library(
        account,
        current_tracks,
    )

    records = inventory["tracks"]
    current_map = {
        track["videoId"]: track
        for track in current_tracks
        if track.get("videoId")
    }

    quality_dir = quality_directory(
        account,
        quality,
    )

    liked_downloaded = []
    not_downloaded = []
    missing_files = []
    no_longer_liked = []
    failed = []
    skipped = []
    unavailable = []
    premium = []

    # Current liked tracks.
    for video_id, track in current_map.items():
        record = records.get(
            video_id,
            {},
        )

        qualities = record.get(
            "qualities",
            {},
        )

        quality_record = qualities.get(
            quality,
            {},
        )

        expected = find_existing_quality_file(
            track,
            quality_dir,
            quality,
        )

        # Best-source files can have different extensions. If inventory
        # already knows the exact file, prefer that path.
        recorded_file = quality_record.get("file")
        if expected is None and recorded_file:
            recorded_path = Path(recorded_file)
            if recorded_path.exists():
                expected = recorded_path

        status = quality_record.get(
            "status"
        )

        if (
            expected is not None
            and expected.exists()
            and expected.stat().st_size > 0
        ):
            liked_downloaded.append(
                {
                    "video_id": video_id,
                    "track": track,
                    "file": str(expected),
                }
            )

            # Repair inventory if the file exists but
            # the inventory was stale.
            quality_record.update(
                {
                    "status": "downloaded",
                    "file": str(expected),
                    "last_verified": int(time.time()),
                }
            )
            qualities[quality] = quality_record
            record["qualities"] = qualities
            records[video_id] = record

        elif status == "downloaded":
            missing_path = recorded_file or str(
                quality_dir / track_filename(track, quality)
            )
            missing_files.append(
                {
                    "video_id": video_id,
                    "track": track,
                    "file": missing_path,
                    "reason": (
                        "Inventory says it was downloaded, "
                        "but the file is missing."
                    ),
                }
            )

        elif status == "failed":
            reason = quality_record.get(
                "reason",
                "Previous download failed.",
            )
            # Premium-restricted videos are not a retryable failure;
            # surface them separately so the UI/TUI stop offering them.
            if is_premium_failure(
                reason
            ) or is_premium_blocked(record):
                premium.append(
                    {
                        "video_id": video_id,
                        "track": track,
                        "reason": reason,
                    }
                )
            else:
                failed.append(
                    {
                        "video_id": video_id,
                        "track": track,
                        "reason": reason,
                    }
                )

        elif status == "skipped":
            skipped.append(
                {
                    "video_id": video_id,
                    "track": track,
                    "reason": quality_record.get(
                        "reason",
                        "Previous download was skipped.",
                    ),
                }
            )

        elif is_premium_blocked(record):
            # Liked but never attempted; already known to be premium.
            premium.append(
                {
                    "video_id": video_id,
                    "track": track,
                    "reason": (
                        "Not downloaded at this quality."
                    ),
                }
            )

        else:
            not_downloaded.append(
                {
                    "video_id": video_id,
                    "track": track,
                    "reason": "Not downloaded at this quality.",
                }
            )

    current_ids = set(
        current_map.keys()
    )

    # Previously downloaded tracks that are no longer
    # present in Liked Music.
    for video_id, record in records.items():
        if video_id in current_ids:
            continue

        qualities = record.get(
            "qualities",
            {},
        )

        quality_record = qualities.get(
            quality,
            {},
        )

        if quality_record.get(
            "status"
        ) != "downloaded":
            continue

        file = quality_record.get(
            "file"
        )

        if not file:
            continue

        no_longer_liked.append(
            {
                "video_id": video_id,
                "title": record.get(
                    "title",
                    "Unknown",
                ),
                "artist": record.get(
                    "artist",
                    "Unknown",
                ),
                "file": file,
                "file_exists": Path(file).exists(),
                "reason": (
                    "Downloaded previously, "
                    "but this track is no longer "
                    "in YouTube Music Liked Music."
                ),
            }
        )

    inventory["last_quality_scan"] = {
        "quality": quality,
        "timestamp": int(time.time()),
    }

    save_inventory(
        account,
        inventory,
    )

    return {
        "liked_downloaded": liked_downloaded,
        "not_downloaded": not_downloaded,
        "missing_files": missing_files,
        "no_longer_liked": no_longer_liked,
        "failed": failed,
        "skipped": skipped,
        "unavailable": unavailable,
        "premium": premium,
        "liked_total": len(current_map),
        "downloaded_total": len(liked_downloaded),
        "not_downloaded_total": len(not_downloaded),
        "missing_total": len(missing_files),
        "no_longer_liked_total": len(
            no_longer_liked
        ),
        "failed_total": len(failed),
        "skipped_total": len(skipped),
        "premium_total": len(premium),
        "quality": quality,
    }


# ============================================================================
# Cross-account duplicates
# ============================================================================

def _normalized_title_artist(
    title: str | None,
    artist: str | None,
) -> tuple[str, str]:
    def normalize(
        value: str | None,
    ) -> str:
        return re.sub(
            r"\s+",
            " ",
            str(value or ""),
        ).strip().casefold()

    return (
        normalize(title),
        normalize(artist),
    )


def find_cross_account_duplicates(
    accounts: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Scan each account's stored inventory for songs liked in more
    than one account. Offline: reflects the last refresh per
    account. Returns two lists:
      - exact:    same YouTube video ID liked in >=2 accounts
      - by_title: same (title, artist) liked as different uploads
                  across >=2 accounts
    """
    per_account = []

    for account in accounts:
        with account_inventory_lock(
            account["slug"]
        ):
            inventory = load_inventory(
                account
            )

        records = {}

        for video_id, record in inventory[
            "tracks"
        ].items():
            if not record.get(
                "currently_liked"
            ):
                continue

            records[video_id] = record

        per_account.append(
            (
                account,
                records,
            )
        )

    video_owners: dict[
        str,
        list[tuple[dict[str, Any], dict[str, Any]]],
    ] = {}

    for account, records in per_account:
        for video_id, record in records.items():
            video_owners.setdefault(
                video_id,
                [],
            ).append(
                (
                    account,
                    record,
                )
            )

    name_by_slug = {
        account["slug"]: account["name"]
        for account in accounts
    }

    exact = []

    for video_id, owners in sorted(
        video_owners.items(),
        key=lambda item: (
            -len(item[1]),
            str(
                item[1][0][1].get(
                    "artist",
                    "",
                )
            ),
        ),
    ):
        if len(owners) < 2:
            continue

        first = owners[0][1]

        entries = []

        for account, record in owners:
            qualities = record.get(
                "qualities",
                {},
            )

            downloaded = sorted(
                {
                    quality
                    for quality, quality_record
                    in qualities.items()
                    if quality_record.get(
                        "status"
                    )
                    == "downloaded"
                }
            )

            entries.append(
                {
                    "slug": account["slug"],
                    "name": account["name"],
                    "downloaded": bool(
                        downloaded
                    ),
                    "qualities": downloaded,
                }
            )

        exact.append(
            {
                "video_id": video_id,
                "title": first.get(
                    "title",
                    "Unknown Title",
                ),
                "artist": first.get(
                    "artist",
                    "Unknown Artist",
                ),
                "album": first.get(
                    "album"
                ),
                "accounts": entries,
            }
        )

    title_owners: dict[
        tuple[str, str],
        dict[str, Any],
    ] = {}

    for account, records in per_account:
        for video_id, record in records.items():
            key = _normalized_title_artist(
                record.get("title"),
                record.get("artist"),
            )

            if not any(key):
                continue

            entry = title_owners.setdefault(
                key,
                {
                    "title": record.get(
                        "title",
                        "Unknown Title",
                    ),
                    "artist": record.get(
                        "artist",
                        "Unknown Artist",
                    ),
                    "video_ids": set(),
                    "account_slugs": {},
                },
            )

            entry["video_ids"].add(
                video_id
            )
            entry["account_slugs"].setdefault(
                account["slug"],
                [],
            ).append(video_id)

    by_title = []

    for key, entry in sorted(
        title_owners.items(),
        key=lambda item: (
            -len(item[1]["account_slugs"]),
            item[1]["title"].casefold(),
        ),
    ):
        if len(entry["account_slugs"]) < 2:
            continue

        # Different uploads (video IDs), not the same upload
        # counted twice — that case is reported under exact.
        if len(entry["video_ids"]) < 2:
            continue

        by_title.append(
            {
                "title": entry["title"],
                "artist": entry["artist"],
                "total_uploads": len(
                    entry["video_ids"]
                ),
                "accounts": [
                    {
                        "slug": slug,
                        "name": name_by_slug.get(
                            slug,
                            slug,
                        ),
                        "upload_count": len(
                            video_ids
                        ),
                        "video_ids": sorted(
                            video_ids
                        ),
                    }
                    for slug, video_ids in sorted(
                        entry[
                            "account_slugs"
                        ].items()
                    )
                ],
            }
        )

    return {
        "exact": exact,
        "by_title": by_title,
        "duplicate_count": len(exact),
        "account_count": len(accounts),
        "scanned_at": int(time.time()),
    }


def duplicate_removal_candidates(
    accounts: list[dict[str, Any]],
    report: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Flatten a duplicates report into one removable item per
    (song, account, upload video id). Drives the removal table
    in the Duplicates screen.
    """
    account_by_slug = {
        account["slug"]: account
        for account in accounts
    }

    inventories: dict[str, dict[str, Any]] = {}

    def has_local_files(
        account: dict[str, Any],
        video_id: str,
    ) -> bool:
        inventory = inventories.get(
            account["slug"]
        )

        if inventory is None:
            with account_inventory_lock(
                account["slug"]
            ):
                inventory = load_inventory(
                    account
                )

            inventories[
                account["slug"]
            ] = inventory

        record = inventory["tracks"].get(
            video_id
        )

        if record is None:
            return False

        return any(
            quality_record.get("file")
            for quality_record in (
                record.get("qualities") or {}
            ).values()
        )

    candidates = []

    for entry in report.get(
        "exact",
        [],
    ):
        video_id = entry["video_id"]

        for account_entry in entry[
            "accounts"
        ]:
            account = account_by_slug.get(
                account_entry["slug"]
            )

            if account is None:
                continue

            candidates.append(
                {
                    "title": entry["title"],
                    "artist": entry["artist"],
                    "slug": account["slug"],
                    "account_name": account["name"],
                    "video_id": video_id,
                    "kind": "same upload",
                    "has_files": has_local_files(
                        account,
                        video_id,
                    ),
                }
            )

    for entry in report.get(
        "by_title",
        [],
    ):
        for account_entry in entry[
            "accounts"
        ]:
            account = account_by_slug.get(
                account_entry["slug"]
            )

            if account is None:
                continue

            for video_id in account_entry.get(
                "video_ids",
                [],
            ):
                candidates.append(
                    {
                        "title": entry["title"],
                        "artist": entry["artist"],
                        "slug": account["slug"],
                        "account_name": account["name"],
                        "video_id": video_id,
                        "kind": "different upload",
                        "has_files": has_local_files(
                            account,
                            video_id,
                        ),
                    }
                )

    return candidates


def remove_duplicates_from_account(
    account: dict[str, Any],
    video_ids: list[str],
    delete_files: bool = True,
) -> dict[str, Any]:
    """
    Unlike a batch of songs in one account, then verify the result
    by re-fetching the account's liked library.

    Local state (downloaded files, inventory records) is only
    touched for uploads that are confirmed gone from the fresh
    liked list, so a silently-failed unlike never deletes files.

    Returns a summary dict:
      unliked       - video ids confirmed removed from the library
      still_liked   - video ids still present after the retry scan
      errors        - video id -> error message (unlike or scan)
      files_deleted - video id -> deleted local file paths
    """
    from ytmusicapi import YTMusic

    video_ids = sorted(set(video_ids))

    ytm = build_ytmusic_client(account["auth"])

    attempts: list[str] = []
    errors: dict[str, str] = {}

    for video_id in video_ids:
        try:
            ytm.rate_song(
                video_id,
                "INDIFFERENT",
            )
        except Exception as error:
            errors[video_id] = str(error)
        else:
            attempts.append(video_id)

    unliked: list[str] = []
    still_liked: list[str] = []
    deleted_files: dict[str, list[str]] = {}

    if attempts:
        try:
            current = _liked_video_id_set(
                account
            )
        except Exception as error:
            for video_id in attempts:
                errors[video_id] = (
                    f"could not verify unlike: {error}"
                )
        else:
            still_present = [
                video_id
                for video_id in attempts
                if video_id in current
            ]

            verify_error = None

            if still_present:
                # YouTube can take a moment to apply the
                # unlike; rescan once before reporting stuck.
                time.sleep(3)

                try:
                    current = _liked_video_id_set(
                        account
                    )
                except Exception as error:
                    verify_error = error

            if verify_error is not None:
                for video_id in attempts:
                    errors[video_id] = (
                        "could not verify unlike: "
                        f"{verify_error}"
                    )
            else:
                still_liked = [
                    video_id
                    for video_id in attempts
                    if video_id in current
                ]

                unliked = [
                    video_id
                    for video_id in attempts
                    if video_id not in current
                ]

                if (
                    unliked
                    and delete_files
                ):
                    deleted_files = (
                        _purge_unliked_tracks(
                            account,
                            unliked,
                        )
                    )

    return {
        "unliked": sorted(unliked),
        "still_liked": sorted(still_liked),
        "errors": errors,
        "files_deleted": deleted_files,
    }


def _liked_video_id_set(
    account: dict[str, Any],
) -> set[str]:
    return {
        track["videoId"]
        for track in fetch_all_liked_tracks(
            account
        )
        if track.get("videoId")
    }


def _purge_unliked_tracks(
    account: dict[str, Any],
    video_ids: list[str],
) -> dict[str, list[str]]:
    """
    Delete local files and inventory records for uploads that
    were confirmed unliked. Returns video id -> deleted paths.
    """
    deleted_by_video: dict[str, list[str]] = {}

    with account_inventory_lock(
        account["slug"]
    ):
        inventory = load_inventory(
            account
        )

        records = inventory["tracks"]

        for video_id in video_ids:
            deleted_files = []

            record = records.pop(
                video_id,
                None,
            )

            if record is not None:
                for quality_record in (
                    record.get("qualities") or {}
                ).values():
                    file = quality_record.get(
                        "file"
                    )

                    if not file:
                        continue

                    path = Path(file)

                    if path.exists():
                        try:
                            path.unlink()
                        except OSError:
                            pass
                        else:
                            deleted_files.append(
                                str(path)
                            )

            for quality, _ in QUALITY_OPTIONS:
                quality_dir = (
                    account["output"]
                    / names_for_quality(quality)
                )

                for temporary in quality_dir.glob(
                    f".ytm_{video_id}*"
                ):
                    try:
                        temporary.unlink()
                    except OSError:
                        pass

            if deleted_files:
                deleted_by_video[video_id] = (
                    deleted_files
                )

        liked_count = inventory.get(
            "liked_count"
        )

        if isinstance(
            liked_count,
            int,
        ):
            inventory["liked_count"] = max(
                0,
                liked_count - len(video_ids),
            )

        save_inventory(
            account,
            inventory,
        )

    return deleted_by_video


def unliked_inventory_records(
    account: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Inventory records for uploads no longer in Liked Music, with
    the local files that would be freed by a purge. Read-only.
    """
    with account_inventory_lock(
        account["slug"]
    ):
        inventory = load_inventory(
            account
        )

    items = []

    for video_id, record in (
        inventory["tracks"].items()
    ):
        if record.get(
            "currently_liked",
            True,
        ):
            continue

        # Search-added and playlist-synced tracks are kept on disk
        # even though they are not part of Liked Music.
        if record.get("source") in {
            "manual",
            "playlist",
        }:
            continue

        files = []

        for quality_record in (
            record.get("qualities") or {}
        ).values():
            file = quality_record.get(
                "file"
            )

            if not file:
                continue

            path = Path(file)

            if path.exists():
                files.append(
                    {
                        "path": str(path),
                        "size_bytes": path.stat().st_size,
                    }
                )

        if not files:
            continue

        items.append(
            {
                "video_id": video_id,
                "title": record.get(
                    "title",
                    "Unknown",
                ),
                "artist": record.get(
                    "artist",
                    "Unknown",
                ),
                "files": files,
                "size_bytes": sum(
                    entry["size_bytes"]
                    for entry in files
                ),
            }
        )

    items.sort(
        key=lambda item: (
            item["title"].lower()
        )
    )

    return items


def purge_unliked_records(
    account: dict[str, Any],
    video_ids: list[str],
) -> dict[str, Any]:
    """
    Delete local files + inventory records for uploads that were
    confirmed unliked. Returns per-video deletion summary.
    """
    sizes_by_id = {
        item["video_id"]: item["size_bytes"]
        for item in unliked_inventory_records(
            account
        )
    }

    deleted = _purge_unliked_tracks(
        account,
        video_ids,
    )

    return {
        "video_ids": sorted(
            set(video_ids)
        ),
        "deleted_files": deleted,
        "freed_bytes": sum(
            sizes_by_id.get(video_id, 0)
            for video_id in set(video_ids)
        ),
    }


# ============================================================================
# Exports / backups
# ============================================================================

def export_directory() -> Path:
    path = CONFIG_DIR / "exports"
    path.mkdir(
        parents=True,
        exist_ok=True,
    )
    return path


def _export_json(
    payload: dict[str, Any],
    prefix: str,
) -> Path:
    timestamp = time.strftime(
        "%Y%m%d-%H%M%S"
    )

    path = (
        export_directory()
        / f"{prefix}-{timestamp}.json"
    )

    temporary = path.with_suffix(".tmp")

    with temporary.open(
        "w",
        encoding="utf-8",
    ) as fh:
        json.dump(
            payload,
            fh,
            indent=2,
            ensure_ascii=False,
        )

    temporary.replace(path)

    return path


def export_duplicates_report(
    report: dict[str, Any],
) -> Path:
    """Write a duplicate report to config/exports as JSON."""
    return _export_json(
        report,
        "duplicates",
    )


def write_backup_snapshot() -> Path:
    """
    Snapshot settings.json plus every account's metadata and
    inventory into a single timestamped JSON file. Restore is
    not supported; this is a readable backup of app state.
    """
    snapshot = {
        "app_version": APP_VERSION,
        "app_build": APP_BUILD,
        "created_at": int(time.time()),
        "settings": load_settings(),
        "accounts": [],
    }

    for account in get_accounts():
        with account_inventory_lock(
            account["slug"]
        ):
            snapshot["accounts"].append(
                {
                    "slug": account["slug"],
                    "name": account["name"],
                    "inventory": load_inventory(
                        account
                    ),
                }
            )

    return _export_json(
        snapshot,
        "backup",
    )


# ============================================================================
# Tools: upgrade wizard / premium retry / yt-dlp update
# ============================================================================

def upgrade_quality_tracks(
    account: dict[str, Any],
    target_quality: str,
) -> list[dict[str, Any]]:
    """
    Tracks that were downloaded at some other quality but are not yet
    available at `target_quality` (and are not premium-blocked). Used
    by the upgrade-quality wizard to re-download at a higher tier.
    """
    inventory = load_inventory(account)
    records = inventory["tracks"]
    current = {
        track["videoId"]: track
        for track in inventory_current_tracks(account)
        if track.get("videoId")
    }

    candidates = []

    for video_id, record in records.items():
        if is_premium_blocked(record):
            continue

        qualities = record.get("qualities", {})
        target = qualities.get(target_quality, {})

        if target.get("status") == "downloaded":
            continue

        if not any(
            entry.get("status") == "downloaded"
            for entry in qualities.values()
        ):
            continue

        track = current.get(video_id)
        if track is None:
            track = {
                "videoId": video_id,
                "title": record.get("title", video_id),
                "artists": [{"name": record.get("artist", "")}],
                "album": record.get("album"),
            }

        candidates.append(track)

    candidates.sort(
        key=lambda track: track_title(track).lower()
    )

    return candidates


def premium_retry_tracks(
    account: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Tracks currently marked premium-blocked. Retrying periodically is
    the only way to discover a track was unlocked (e.g. after a
    subscription change). Returns tracks with their premium record.
    """
    inventory = load_inventory(account)
    records = inventory["tracks"]
    current = {
        track["videoId"]: track
        for track in inventory_current_tracks(account)
        if track.get("videoId")
    }

    retry = []

    for video_id, record in records.items():
        if not is_premium_blocked(record):
            continue

        track = current.get(video_id)
        if track is None:
            continue

        retry.append(
            {
                "video_id": video_id,
                "title": record.get("title", video_id),
                "artist": record.get("artist", ""),
                "album": record.get("album"),
                "track": track,
            }
        )

    return retry


def check_yt_dlp_update() -> dict[str, Any]:
    """
    Compare the installed yt-dlp with the latest GitHub release.
    Returns current/latest versions and whether an update is due.
    """
    current = "unknown"
    binary = yt_dlp_binary()

    try:
        result = subprocess.run(
            [
                str(binary),
                "--version",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            current = (
                result.stdout.strip()
                or result.stderr.strip()
                or "unknown"
            )
    except Exception:
        pass

    latest = current

    try:
        data = github_json(
            "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
        )
        latest = (
            data.get("tag_name")
            or data.get("name")
            or latest
        ).lstrip("v")
        current_version = current.lstrip("v")
        available = bool(
            latest and current_version and latest != current_version
        )
    except Exception:
        latest = current
        available = False

    return {
        "current": current,
        "latest": latest,
        "update_available": available,
        "checked_at": int(time.time()),
    }


def update_yt_dlp() -> dict[str, Any]:
    """
    Self-update the yt-dlp binary in place (the venv's copy in source
    runs, DATA_DIR/bin's copy in frozen builds).
    """
    binary = yt_dlp_binary()

    try:
        result = subprocess.run(
            [
                str(binary),
                "--update",
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        return {
            "ok": result.returncode == 0,
            "output": (
                result.stdout.strip()
                + "\n"
                + result.stderr.strip()
            ).strip(),
        }
    except Exception as error:
        return {
            "ok": False,
            "output": str(error),
        }


def import_spotify_playlist(
    playlist_url: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """
    Resolve a public Spotify playlist to a list of {title, artist,
    album} tracks using an anonymously obtained Spotify client token.
    Each track still needs to be matched to YouTube Music before it
    can be downloaded (see map_spotify_tracks).
    """
    import urllib.error

    playlist_id = None

    match = re.search(
        r"playlist[/:]([A-Za-z0-9]{10,40})",
        playlist_url,
    )
    if match:
        playlist_id = match.group(1)

    if not playlist_id:
        raise ValueError(
            "Not a Spotify playlist URL (expected open.spotify.com/playlist/…)"
        )

    def _get_access_token() -> str:
        request = urllib.request.Request(
            "https://open.spotify.com/get_access_token",
            headers={
                "User-Agent": "Mozilla/5.0",
            },
        )
        with urllib.request.urlopen(
            request,
            timeout=30,
        ) as response:
            data = json.loads(
                response.read().decode("utf-8")
            )
        token = data.get(
            "accessToken"
        ) or data.get("token")
        if not token:
            raise ValueError(
                "Spotify refused an anonymous token"
            )
        return str(token)

    token = _get_access_token()

    def _api(path: str) -> dict[str, Any]:
        request = urllib.request.Request(
            "https://api.spotify.com/v1/" + path,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "Mozilla/5.0",
            },
        )
        with urllib.request.urlopen(
            request,
            timeout=30,
        ) as response:
            return json.loads(
                response.read().decode("utf-8")
            )

    try:
        data = _api(
            f"playlists/{playlist_id}"
        )
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise ValueError(
                "Spotify playlist is private or unavailable "
                "without a Spotify account"
            )
        raise ValueError(
            f"Spotify request failed: {error.code}"
        )

    title = data.get("name", "Spotify playlist")

    tracks = []

    offset = 0
    while offset < limit:
        batch = _api(
            f"playlists/{playlist_id}/tracks?"
            f"limit=50&offset={offset}"
        )
        for item in batch.get(
            "items",
            [],
        ):
            entry = item.get("track")
            if not entry or entry.get("is_local"):
                continue
            artists = [
                artist.get("name", "")
                for artist in entry.get("artists", [])
            ]
            tracks.append(
                {
                    "title": entry.get("name", "Unknown"),
                    "artist": ", ".join(artists),
                    "album": (
                        entry.get("album") or {}
                    ).get("name"),
                    "duration_ms": entry.get(
                        "duration_ms",
                    ),
                    "spotify_id": entry.get(
                        "id",
                    ),
                }
            )
            if len(tracks) >= limit:
                break

        if len(tracks) >= limit:
            break

        if not batch.get("next"):
            break

        offset += 50

    return {
        "title": title,
        "tracks": tracks,
    }


def map_spotify_tracks(
    spotify_tracks: list[dict[str, Any]],
    account: dict[str, Any] | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """
    Match Spotify tracks to YouTube Music results so they can be
    downloaded. Best-match search per track, in batches.
    """
    results = []

    for spotify in spotify_tracks[:limit]:
        query = (
            f"{spotify.get('title', '')} "
            f"{spotify.get('artist', '')}"
        ).strip()

        if not query:
            continue

        try:
            matches = search_tracks(
                query,
                limit=1,
                account=account,
            )
        except Exception:
            matches = []

        if not matches:
            continue

        matched = matches[0]

        results.append(
            {
                "videoId": matched.get("videoId"),
                "title": track_title(matched),
                "artists": (
                    matched.get("artists")
                    or [
                        {
                            "name": spotify.get(
                                "artist",
                                "Unknown",
                            )
                        }
                    ]
                ),
                "album": get_album(matched),
                "matched_title": matched.get(
                    "title",
                    "",
                ),
            }
        )

    return results


# ============================================================================
# Within-account duplicates
# ============================================================================

def within_account_duplicates(
    account: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Group an account's inventory by normalized title+artist and report
    groups that contain more than one video id. Useful for catching
    re-releases, covers and remasters in the same library.
    """
    inventory = load_inventory(account)
    records = inventory["tracks"]

    groups: dict[
        tuple[str, str],
        list[dict[str, Any]],
    ] = {}

    for video_id, record in records.items():
        if is_premium_blocked(record):
            continue

        if not any(
            entry.get("status") == "downloaded"
            for entry in (
                record.get("qualities") or {}
            ).values()
        ):
            continue

        key = _normalized_title_artist(
            record.get("title"),
            record.get("artist"),
        )

        groups.setdefault(
            key,
            [],
        ).append(
            {
                "video_id": video_id,
                "title": record.get("title", "Unknown"),
                "artist": record.get("artist", "Unknown"),
                "album": record.get("album"),
                "qualities": sorted(
                    (
                        quality
                        for quality, entry in (
                            record.get("qualities") or {}
                        ).items()
                        if entry.get("status") == "downloaded"
                    ),
                    key=lambda quality: (
                        QUALITY_OPTIONS_INDEX.get(
                            quality,
                            99,
                        )
                    ),
                ),
            }
        )

    return [
        {
            "title": entries[0]["title"],
            "artist": entries[0]["artist"],
            "tracks": entries,
            "count": len(entries),
        }
        for entries in groups.values()
        if len(entries) > 1
    ]


QUALITY_OPTIONS_INDEX = {
    quality: index
    for index, (_label, quality) in enumerate(QUALITY_OPTIONS)
}


# ============================================================================
# Download quality
# ============================================================================
def quality_config(
    quality: str,
) -> dict[str, Any]:
    if quality == "best":
        return {
            "audio_format": "best",
            "audio_quality": None,
            "extension": "m4a",
        }

    if quality == "mp3_320":
        return {
            "audio_format": "mp3",
            "audio_quality": "320K",
            "extension": "mp3",
        }

    if quality == "mp3_v0":
        return {
            "audio_format": "mp3",
            "audio_quality": "0",
            "extension": "mp3",
        }

    if quality == "mp3_256":
        return {
            "audio_format": "mp3",
            "audio_quality": "256K",
            "extension": "mp3",
        }

    if quality == "mp3_192":
        return {
            "audio_format": "mp3",
            "audio_quality": "192K",
            "extension": "mp3",
        }

    if quality == "mp3_128":
        return {
            "audio_format": "mp3",
            "audio_quality": "128K",
            "extension": "mp3",
        }

    if quality == "flac":
        return {
            "audio_format": "flac",
            "audio_quality": "0",
            "extension": "flac",
        }

    if quality == "m4a_256":
        return {
            "audio_format": "m4a",
            "audio_quality": "256K",
            "extension": "m4a",
        }

    raise ValueError(
        f"Unknown quality: {quality}"
    )


# ============================================================================
# Download worker
# ============================================================================

@dataclass(frozen=True)
class DownloadSession:
    quality: str
    existing_policy: str
    session: str = "loop"


PREMIUM_BLOCK_REASON = "premium"


def is_premium_failure(reason: str) -> bool:
    """True when a yt-dlp failure means the video is behind the YouTube
    Music Premium wall (not just a transient error)."""
    if not reason:
        return False
    r = reason.lower()
    return (
        "premium" in r
        or "members only" in r
        or "music premium" in r
        or "youtube premium" in r
        or "join youtube premium" in r
        or "requires a subscription" in r
        or "subscription" in r
        or "requires subscription" in r
        or "subscription required" in r
        or "age-restricted" in r
        or "age restricted" in r
        or "sign in" in r
        or "not available on this account" in r
        or "this video is unavailable" in r
    )


def is_cookieless_403_failure(reason: str) -> bool:
    """True when a yt-dlp failure is the signed-out CDN cap.

    Signed-out sessions (an auth file without valid YouTube cookies
    leaves yt-dlp unauthenticated) may only fetch the first ~1 MB of
    a stream URL; YouTube's GVS PO-token experiment refuses larger or
    mid-file ranges with HTTP 403. Cookie sessions mint unrestricted
    URLs and never hit this.
    """
    if not reason:
        return False
    r = reason.lower()
    return (
        ("403" in r or "forbidden" in r)
        and not is_premium_failure(r)
    )


COOKIELESS_403_HINT = (
    "YouTube only serves the first ~1 MB of a stream to signed-out "
    "sessions, and this account's stored sign-in has no valid YouTube "
    "cookies. Re-add the account with \"Import sign-in from browser\" "
    "or by pasting browser request headers to download full tracks."
)


_YTDLP_FORWARDED_HEADERS = (
    "user-agent",
    "authorization",
    "x-goog-authuser",
    "x-goog-visitor-id",
    "x-origin",
    "referer",
    "origin",
)


def _read_auth_headers(auth_path: Path) -> dict[str, str]:
    """Return the lowercase header map stored in a ytmusicapi auth file
    (accounts/<slug>/.headers_auth.json), or {} when unreadable."""
    try:
        if not auth_path or not auth_path.exists():
            return {}
        data = json.loads(auth_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}

        # OAuth device-flow accounts store a RefreshingToken JSON here
        # instead of browser headers. Token fields are not HTTP headers —
        # never forward them (or leak access/refresh tokens) to yt-dlp.
        try:
            from ytmusicapi.auth.oauth.token import Token

            is_oauth_token = all(
                key in data for key in Token.members()
            )
        except Exception:
            is_oauth_token = False

        if is_oauth_token:
            return {}
        headers = data.get("headers") or data.get("request_headers")
        if isinstance(headers, str):
            parsed = {}
            for line in headers.splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                parsed[key.strip().lower()] = value.strip()
            return parsed
        if isinstance(headers, dict):
            return {str(k).lower(): str(v) for k, v in headers.items()}
        # Flat dict of headers (current ytmusicapi format).
        return {str(k).lower(): str(v) for k, v in data.items()}
    except Exception:
        return {}


def _write_netscape_cookies(cookie_header: str, path: Path) -> bool:
    """Convert a `name=value; name2=value2` Cookie header into a
    Netscape cookies.txt file yt-dlp can consume. Returns success."""
    try:
        pairs = []
        for part in cookie_header.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, value = part.split("=", 1)
            name = name.strip()
            if name:
                pairs.append((name, value.strip()))
        if not pairs:
            return False
        expires = 2147483647
        lines = ["# Netscape HTTP Cookie File", ""]
        for name, value in pairs:
            for domain in (".youtube.com", ".google.com"):
                lines.append(
                    f"{domain}\tTRUE\t/\tTRUE\t{expires}\t{name}\t{value}"
                )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        return True
    except Exception:
        return False


def _yt_dlp_auth_args(
    auth_path: Path,
    cookie_file: Path,
) -> list[str]:
    """Build yt-dlp auth arguments from the account's auth file.

    Prefers a Netscape cookie file (yt-dlp's own cookie jar) and forwards
    the remaining identifying headers. Never logs secret values.
    """
    headers = _read_auth_headers(auth_path)
    args: list[str] = []

    cookie_header = headers.get("cookie", "")
    if cookie_header and _write_netscape_cookies(cookie_header, cookie_file):
        args.extend(["--cookies", str(cookie_file)])

    for key in _YTDLP_FORWARDED_HEADERS:
        value = headers.get(key)
        if not value:
            continue
        if key == "user-agent":
            args.extend(["--user-agent", value])
        else:
            title = "-".join(part.capitalize() for part in key.split("-"))
            args.extend(["--add-header", f"{title}: {value}"])

    if "referer" not in headers:
        args.extend(["--add-header", "Referer: https://music.youtube.com/"])
    if "origin" not in headers:
        args.extend(["--add-header", "Origin: https://music.youtube.com"])

    return args


def is_premium_blocked(record: dict[str, Any]) -> bool:
    """True when a track record is known to be behind the YouTube
    Music Premium wall: either an explicit block or any failed
    attempt whose reason mentions premium (covers records written
    before blocks existed)."""
    blocked = record.get("blocked") or {}
    if blocked.get("reason") == PREMIUM_BLOCK_REASON:
        return True
    for entry in (record.get("qualities") or {}).values():
        if entry.get("status") == "failed" and is_premium_failure(
            entry.get("reason")
        ):
            return True
    return False


def file_sha256(path: Path, chunk_size: int = 1 << 20) -> str:
    """SHA-256 hex digest of a file, streamed in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class DownloadWorker:
    def __init__(
        self,
        account: dict[str, Any],
        tracks: list[dict[str, Any]],
        sessions: list[DownloadSession],
        settings: dict[str, Any],
        callback,
        output_dir: Path | None = None,
        source: str = "library",
    ):
        self.account = account
        self.tracks = tracks
        self.sessions = sessions
        self.settings = settings
        self.callback = callback
        self.output_dir = output_dir
        self.source = source

        self.cancelled = False
        self._stop = False

        # Guards shared counters/inventory writes when a stage runs
        # multiple downloads concurrently (max_parallel_downloads > 1).
        self._io_lock = threading.Lock()

        self.active_quality = None
        self.active_track_no = 0
        self.active_title = None
        self.active_artist = None
        self.active_progress = 0

        self.downloaded = 0
        self.skipped = 0
        self.failed = 0
        self.unavailable = 0

        self.results = []

    def cancel(self) -> None:
        self.cancelled = True

    def stop_after_stage(self) -> None:
        self._stop = True

    def emit(
        self,
        event: str,
        **data,
    ) -> None:
        self.callback(
            event,
            data,
        )

    def counts(self) -> None:
        with self._io_lock:
            downloaded = self.downloaded
            skipped = self.skipped
            failed = self.failed
            unavailable = self.unavailable
        self.emit(
            "counts",
            downloaded=downloaded,
            skipped=skipped,
            failed=failed,
            unavailable=unavailable,
        )

    def write_result(
        self,
        inventory: dict[str, Any],
        video_id: str,
        track: dict[str, Any],
        quality: str,
        status: str,
        reason: str,
        file: str | None = None,
        sha256: str | None = None,
    ) -> dict[str, Any]:
        with self._io_lock:
            records = inventory["tracks"]

            record = records.setdefault(
                video_id,
                {
                    "video_id": video_id,
                    "qualities": {},
                },
            )

            record["title"] = track_title(track)
            record["artist"] = get_artist(track)
            record["album"] = get_album(track)

            # A successful or skipped download proves the video is no
            # longer behind the premium wall; clear any prior block.
            if status in ("downloaded", "skipped"):
                record.pop("blocked", None)

            qualities = record.setdefault(
                "qualities",
                {},
            )

            qualities[quality] = {
                "status": status,
                "reason": reason,
                "file": file,
                "sha256": sha256,
                "size_bytes": (
                    int(Path(file).stat().st_size)
                    if sha256 and file and Path(file).exists()
                    else None
                ),
                "updated_at": int(time.time()),
            }

            self.results.append(
                {
                    "timestamp": int(time.time()),
                    "video_id": video_id,
                    "title": track_title(track),
                    "artist": get_artist(track),
                    "quality": quality,
                    "status": status,
                    "reason": reason,
                    "file": file,
                }
            )

            return record

    def run(self) -> None:
        try:
            self._run()
        except Exception as error:
            # A stage aborted mid-run (transient API error, corrupt
            # inventory, race with a concurrent refresh, ...). Do not
            # let the thread die silently and leave the UI stuck with
            # a dead worker: record the failure and still finish so
            # the session row is cleaned up and the user is told.
            self.emit(
                "log",
                text=f"Unexpected error: {error}",
            )

            self.emit(
                "finished",
                downloaded=self.downloaded,
                skipped=self.skipped,
                failed=self.failed,
                unavailable=self.unavailable,
                cancelled=self.cancelled,
                stop=self._stop,
                total=len(self.tracks),
                error=str(error),
                results=self.results,
            )

    def _run(self) -> None:
        looping = any(
            session.session == "loop"
            for session in self.sessions
        )

        while not (self.cancelled or self._stop):
            for stage_index, session in enumerate(
                self.sessions
            ):
                if self.cancelled or self._stop:
                    break

                self.emit(
                    "stage",
                    index=stage_index + 1,
                    total=len(self.sessions),
                    quality=session.quality,
                )

                self.run_stage(
                    session
                )

            if not looping:
                break

        self.emit(
            "finished",
            downloaded=self.downloaded,
            skipped=self.skipped,
            failed=self.failed,
            unavailable=self.unavailable,
            cancelled=self.cancelled,
            stop=self._stop,
            total=len(self.tracks),
            results=self.results,
        )

    def run_stage(
        self,
        session: DownloadSession,
    ) -> None:
        # Serialize same-account inventory reads/writes so a
        # concurrent refresh cannot drop records this stage wrote.
        # Different accounts use different locks and run in parallel.
        with account_inventory_lock(
            self.account["slug"]
        ):
            self._run_stage(session)

    def _run_stage(
        self,
        session: DownloadSession,
    ) -> None:
        quality = session.quality
        policy = session.existing_policy

        self.active_quality = quality

        if self.output_dir is not None:
            output = self.output_dir
        else:
            output = quality_directory(
                self.account,
                quality,
            )

        inventory = update_inventory_from_library(
            self.account,
            self.tracks,
            mark_liked=(self.source == "library"),
            source=self.source,
        )

        total = len(self.tracks)

        # Build yt-dlp auth arguments once per stage. The auth file holds
        # the same cookies/session the ytmusicapi client uses, so yt-dlp
        # must send them too or YouTube returns 403/sign-in errors.
        cookie_fd, cookie_name = tempfile.mkstemp(
            prefix=".ytm_ytdl_cookies_",
            suffix=".txt",
        )
        os.close(cookie_fd)
        cookie_file = Path(cookie_name)
        auth_args = _yt_dlp_auth_args(
            Path(self.account.get("auth", "")),
            cookie_file,
        )
        # yt-dlp authenticates with cookies only; an OAuth account has a
        # token file, so its downloads run signed-out and can hit the
        # ~1 MB CDN cap (see is_cookieless_403_failure).
        has_cookies = "--cookies" in auth_args

        def download_one(
            number: int,
            track: dict[str, Any],
        ) -> None:
            if self.cancelled:
                return

            video_id = track.get("videoId")
            title = track_title(track)
            artist = get_artist(track)

            self.active_track_no = number
            self.active_title = title
            self.active_artist = artist

            self.emit(
                "track",
                number=number,
                total=total,
                title=title,
                artist=artist,
            )

            if not video_id:
                reason = (
                    "YouTube Music entry has no video ID; "
                    "it is unavailable for downloading."
                )

                with self._io_lock:
                    self.unavailable += 1

                self.write_result(
                    inventory,
                    f"unavailable:{number}",
                    track,
                    quality,
                    "unavailable",
                    reason,
                )

                self.emit(
                    "status",
                    text=reason,
                )

                self.counts()
                return

            existing = find_existing_quality_file(
                track,
                output,
                quality,
            )

            destination = existing

            # Same quality exists.
            if (
                destination is not None
                and destination.exists()
                and destination.stat().st_size > 0
            ):
                if policy == "skip":
                    reason = (
                        "Same track already exists at "
                        f"the selected quality "
                        f"({quality_label(quality)})."
                    )

                    with self._io_lock:
                        self.skipped += 1

                    self.write_result(
                        inventory,
                        video_id,
                        track,
                        quality,
                        "skipped",
                        reason,
                        str(destination),
                    )

                    self.emit(
                        "progress",
                        value=100,
                    )

                    self.emit(
                        "status",
                        text=f"↷ SKIPPED — {reason}",
                    )

                    self.counts()
                    return

                # Replace mode.
                try:
                    destination.unlink()
                except Exception as error:
                    reason = (
                        "Existing file could not be replaced: "
                        f"{error}"
                    )

                    with self._io_lock:
                        self.failed += 1

                    self.write_result(
                        inventory,
                        video_id,
                        track,
                        quality,
                        "failed",
                        reason,
                        str(destination),
                    )

                    self.emit(
                        "status",
                        text=f"✗ FAILED — {reason}",
                    )

                    self.counts()
                    return

            # Clean old temporary files.
            for temporary in output.glob(
                f".ytm_{video_id}*"
            ):
                temporary.unlink(
                    missing_ok=True
                )

            temporary_template = (
                output
                / f".ytm_{video_id}.%(ext)s"
            )

            url = (
                "https://www.youtube.com/watch?v="
                f"{video_id}"
            )

            yt_dlp = (
                yt_dlp_binary()
            )

            deno = (
                deno_binary()
            )

            ffmpeg = Path(
                imageio_ffmpeg.get_ffmpeg_exe()
            )

            config = quality_config(
                quality
            )

            command = [
                str(yt_dlp),

                "--js-runtimes",
                f"deno:{deno}",

                "--remote-components",
                "ejs:github",

                "--no-playlist",

                "--extract-audio",

                "--audio-format",
                config["audio_format"],

                "--embed-thumbnail",

                "--add-metadata",

                "--convert-thumbnails",
                "jpg",

                "--no-overwrites",

                "--ffmpeg-location",
                str(ffmpeg),

                "--newline",

                "--output",
                str(temporary_template),
            ]

            # Send the account's cookies/session so YouTube does not
            # reject the request as signed-out/unauthenticated.
            command.extend(auth_args)
            # The web_music player client only serves full formats to a
            # signed-in session (cookie jar); without cookies it returns
            # UNPLAYABLE/format-18-only. OAuth accounts store a token,
            # not cookies, so they fall back to yt-dlp's default client,
            # which works for signed-out playback.
            if "--cookies" in auth_args:
                command.extend([
                    "--extractor-args",
                    "youtube:player_client=web_music",
                ])
            command.append(url)

            if config["audio_quality"] is not None:
                command.extend(
                    [
                        "--audio-quality",
                        config["audio_quality"],
                    ]
                )

            rate_limit = str(
                self.settings.get(
                    "download_rate_limit",
                    "",
                )
            ).strip()
            if rate_limit:
                command.extend(
                    [
                        "--limit-rate",
                        rate_limit,
                    ]
                )

            # Best source: ask yt-dlp for best audio-only stream
            # and do not unnecessarily re-encode it.
            if quality == "best":
                command.extend(
                    [
                        "--format",
                        "bestaudio/best",
                    ]
                )

            retries = max(
                1,
                int(
                    self.settings.get(
                        "retry_attempts",
                        3,
                    )
                ),
            )

            success = False
            last_error = "Unknown download error."

            for attempt in range(
                1,
                retries + 1,
            ):
                if self.cancelled:
                    break

                self.emit(
                    "status",
                    text=(
                        f"Downloading "
                        f"({attempt}/{retries})"
                    ),
                )

                try:
                    process = subprocess.Popen(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                        cwd=str(Path.cwd()),
                    )

                    assert process.stdout is not None

                    for line in iter(
                        process.stdout.readline,
                        "",
                    ):
                        if self.cancelled:
                            try:
                                process.terminate()
                                try:
                                    process.wait(timeout=5)
                                except subprocess.TimeoutExpired:
                                    process.kill()
                                    try:
                                        process.wait(timeout=5)
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                            break

                        line = line.strip()

                        if not line:
                            continue

                        percent_match = re.search(
                            r"(\d+(?:\.\d+)?)%",
                            line,
                        )

                        if percent_match:
                            try:
                                self.emit(
                                    "progress",
                                    value=float(
                                        percent_match.group(1)
                                    ),
                                )
                            except ValueError:
                                pass

                        l = line
                        low = str(l).lower()
                        if (
                            "error:" in low
                            or "http error" in low
                            or "has been removed" in low
                            or "not available" in low
                            or "sign in" in low
                            or "members only" in low
                            or "music premium" in low
                            or "youtube premium" in low
                            or "premium" in low
                            or "age-restricted" in low
                            or "age restricted" in low
                            or "private video" in low
                            or "unavailable" in low
                            or ("requires" in low and ("login" in low or "sign in" in low or "subscription" in low))
                            or "403" in low or "401" in low
                        ):
                            if last_error == "Unknown download error.":
                                last_error = str(l)
                            else:
                                if len(last_error) < 200:
                                    last_error = last_error + " | " + str(l)
                                else:
                                    last_error = str(l)

                        self.emit(
                            "log",
                            text=line,
                        )

                    try:
                        return_code = process.wait(timeout=300)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        try:
                            process.wait(timeout=10)
                        except Exception:
                            pass
                        return_code = -1
                    if (
                        return_code == 0
                        and not self.cancelled
                    ):
                        success = True
                        break

                except Exception as error:
                    last_error = str(error)

                if attempt < retries:
                    time.sleep(
                        int(
                            self.settings.get(
                                "retry_delay",
                                3,
                            )
                        )
                    )

            if self.cancelled:
                return

            # Find output.
            extension = config["extension"]

            candidates = list(
                output.glob(
                    f".ytm_{video_id}*.{extension}"
                )
            )

            if not candidates:
                # For best-source yt-dlp can select m4a/webm/opus
                # depending on source. Find any produced file.
                candidates = [
                    path
                    for path in output.glob(
                        f".ytm_{video_id}.*"
                    )
                    if path.is_file()
                    and not path.name.endswith(".part")
                ]

            if not success or not candidates:
                reason = (
                    last_error
                    if last_error and last_error != "Unknown download error."
                    else (
                        "yt-dlp failed to produce "
                        "the requested audio."
                    )
                )

                if not has_cookies and is_cookieless_403_failure(
                    reason
                ):
                    reason = f"{reason} — {COOKIELESS_403_HINT}"

                with self._io_lock:
                    self.failed += 1

                record = self.write_result(
                    inventory,
                    video_id,
                    track,
                    quality,
                    "failed",
                    reason,
                )

                # A premium-restricted video cannot be fetched with the
                # current account; remember that so the UI stops offering
                # to download it and future syncs don't hammer it.
                if is_premium_failure(reason):
                    with self._io_lock:
                        record["blocked"] = {
                            "reason": PREMIUM_BLOCK_REASON,
                            "blocked_at": int(time.time()),
                        }
                    self.emit(
                        "status",
                        text=(
                            "✗ PREMIUM — This track needs YouTube "
                            "Music Premium and cannot be downloaded."
                        ),
                    )
                else:
                    self.emit(
                        "status",
                        text=f"✗ FAILED — {reason}",
                    )

                for temporary in output.glob(
                    f".ytm_{video_id}*"
                ):
                    temporary.unlink(
                        missing_ok=True
                    )

                self.counts()
                return

            downloaded = candidates[0]

            if quality == "best":
                destination = output / track_filename(
                    track,
                    quality,
                    downloaded.suffix.lstrip("."),
                )
            else:
                destination = output / track_filename(
                    track,
                    quality,
                )

            try:
                if destination.exists():
                    destination.unlink()

                downloaded.replace(
                    destination
                )

            except Exception as error:
                reason = (
                    "Downloaded audio could not be moved "
                    f"into the library: {error}"
                )

                with self._io_lock:
                    self.failed += 1

                self.write_result(
                    inventory,
                    video_id,
                    track,
                    quality,
                    "failed",
                    reason,
                )

                self.emit(
                    "status",
                    text=f"✗ FAILED — {reason}",
                )

                self.counts()
                return

            # Record a verified download.
            with self._io_lock:
                self.downloaded += 1

            try:
                file_hash = file_sha256(destination)
            except Exception:
                file_hash = None

            self.write_result(
                inventory,
                video_id,
                track,
                quality,
                "downloaded",
                (
                    "Successfully downloaded at "
                    f"{quality_label(quality)}."
                ),
                str(destination),
                sha256=file_hash,
            )

            self.emit(
                "progress",
                value=100,
            )

            self.emit(
                "status",
                text=(
                    "✓ DOWNLOADED — "
                    f"{quality_label(quality)}"
                ),
            )

            self.counts()

            with self._io_lock:
                save_inventory(
                    self.account,
                    inventory,
                )

        max_parallel = max(
            1,
            int(
                self.settings.get(
                    "max_parallel_downloads",
                    1,
                ) or 1
            ),
        )

        try:
            if max_parallel <= 1:
                for number, track in enumerate(
                    self.tracks,
                    1,
                ):
                    if self.cancelled:
                        break
                    download_one(
                        number,
                        track,
                    )
            else:
                from concurrent.futures import ThreadPoolExecutor

                with ThreadPoolExecutor(
                    max_workers=max_parallel,
                ) as pool:
                    pending = []

                    for number, track in enumerate(
                        self.tracks,
                        1,
                    ):
                        if self.cancelled:
                            break
                        pending.append(
                            pool.submit(
                                download_one,
                                number,
                                track,
                            )
                        )

                    for future in pending:
                        try:
                            future.result()
                        except Exception as error:
                            # Record failure for this task
                            msg = str(error)
                            self.emit(
                                "log",
                                text=f"Download error: {msg}",
                            )
                            with self._io_lock:
                                self.failed += 1
                            self.emit("status", text=f"✗ FAILED — {msg}")
                            self.counts()
        finally:
            save_inventory(
                self.account,
                inventory,
            )

            # Remove the transient cookie file holding the account session.
            try:
                if cookie_file.exists():
                    cookie_file.unlink()
            except OSError:
                pass


# ============================================================================
# Add account modal
# ============================================================================

class AddAccountScreen(ModalScreen):
    CSS = """
    AddAccountScreen {
        align: center middle;
    }

    #modal {
        width: 90%;
        height: 90%;
        background: $surface;
        border: double $primary;
        padding: 2;
    }

    #title {
        color: $accent;
        text-style: bold;
        padding-bottom: 1;
    }

    #instructions {
        height: auto;
        padding-bottom: 1;
    }

    #name {
        margin-bottom: 1;
    }

    #headers {
        height: 1fr;
        border: solid $primary;
    }

    #status {
        height: auto;
        min-height: 2;
        padding: 1;
    }

    #buttons {
        height: auto;
    }

    #buttons Button {
        margin-right: 1;
    }
    """

    def compose(self) -> ComposeResult:
        browsers = list_importable_browsers()
        self._browsers = browsers
        default_browser = next(
            (
                entry["id"]
                for entry in browsers
                if entry.get("available")
            ),
            None,
        )
        yield Container(
            Label(
                "ADD YOUTUBE MUSIC ACCOUNT",
                id="title",
            ),

            Static(
                "Automatic (recommended):\n"
                "1. Sign into YouTube Music in your browser.\n"
                "2. Pick that browser (and profile) below —\n"
                '   "Auto-detect" finds the signed-in one.\n'
                "3. Click \"Import sign-in from browser\".\n\n"
                "Manual fallback:\n"
                "1. Open YouTube Music, open Developer Tools → Network.\n"
                "2. Find a POST request to /browse with status 200.\n"
                "3. Right-click → Copy → Copy request headers.\n"
                "4. Paste the complete request headers below.\n\n"
                "Authentication is stored locally as a protected "
                ".headers_auth.json file.",
                id="instructions",
            ),

            Input(
                placeholder="Account name, e.g. Shay Mercer",
                id="name",
            ),

            TextArea(
                "",
                placeholder="Paste request headers here (manual fallback)...",
                id="headers",
            ),

            Select(
                [
                    (
                        entry["label"]
                        + (
                            ""
                            if entry["available"]
                            else " (not installed)"
                        ),
                        entry["id"],
                    )
                    for entry in browsers
                ],
                value=(
                    default_browser
                    if default_browser is not None
                    else Select.BLANK
                ),
                allow_blank=True,
                prompt="Import sign-in from browser...",
                id="browser",
            ),

            Select(
                self._profile_options(default_browser),
                value="auto",
                allow_blank=False,
                prompt="Profile...",
                id="profile",
            ),

            Label(
                "Ready",
                id="status",
            ),

            Horizontal(
                Button(
                    "Open YouTube Music",
                    id="open",
                ),

                Button(
                    "Test & Add",
                    id="add",
                    variant="success",
                ),

                Button(
                    "Import sign-in from browser",
                    id="add-browser",
                ),

                Button(
                    "Cancel",
                    id="cancel",
                ),

                id="buttons",
            ),

            id="modal",
        )

    def _profile_options(
        self,
        browser_id: str | None,
    ) -> list[tuple[str, str]]:
        """Profile choices for one browser: auto-detect first, then
        every profile with an honest offline session hint."""
        options: list[tuple[str, str]] = [
            ("Auto-detect (recommended)", "auto"),
        ]
        entry = next(
            (
                candidate
                for candidate in getattr(self, "_browsers", [])
                if browser_id
                and candidate["id"] == browser_id
                and candidate.get("available")
            ),
            None,
        )
        for profile_entry in (entry or {}).get("profiles", []):
            display = profile_entry.get("name") or profile_entry["id"]
            label = f"{display} ({profile_entry['id']})"
            session = profile_entry.get("session")
            if session is True:
                label += " · session cookies present"
            elif session is False:
                label += " · no Google session"
            options.append((label, profile_entry["id"]))
        return options

    def on_select_changed(
        self,
        event: Select.Changed,
    ) -> None:
        if event.select.id != "browser":
            return

        browser_id = (
            None
            if event.value is Select.BLANK
            else str(event.value)
        )
        profile_select = self.query_one(
            "#profile",
            Select,
        )
        profile_select.set_options(
            self._profile_options(browser_id)
        )
        profile_select.value = "auto"

    def on_button_pressed(
        self,
        event: Button.Pressed,
    ) -> None:
        button = event.button.id

        if button == "cancel":
            self.dismiss(None)
            return

        if button == "open":
            webbrowser.open(
                "https://music.youtube.com"
            )

            self.query_one(
                "#status",
                Label,
            ).update(
                "YouTube Music opened."
            )

            return

        if button == "add-browser":
            import_name = self.query_one(
                "#name",
                Input,
            ).value.strip()

            if not import_name:
                self.query_one(
                    "#status",
                    Label,
                ).update(
                    "ERROR: Enter an account name."
                )
                return

            browser_select = self.query_one(
                "#browser",
                Select,
            )

            browser_id = browser_select.value

            if browser_id is Select.BLANK:
                self.query_one(
                    "#status",
                    Label,
                ).update(
                    "ERROR: Choose which browser to import from."
                )
                return

            profile_value = self.query_one(
                "#profile",
                Select,
            ).value
            profile = (
                None
                if profile_value is Select.BLANK
                or str(profile_value) == "auto"
                else str(profile_value)
            )

            event.button.disabled = True

            self.query_one(
                "#status",
                Label,
            ).update(
                "Importing the browser's YouTube sign-in..."
            )

            threading.Thread(
                target=self.import_worker,
                args=(
                    import_name,
                    str(browser_id),
                    profile,
                ),
                daemon=True,
            ).start()

            return

        if button != "add":
            return

        name = self.query_one(
            "#name",
            Input,
        ).value.strip()

        headers = self.query_one(
            "#headers",
            TextArea,
        ).text.strip()

        if not name:
            self.query_one(
                "#status",
                Label,
            ).update(
                "ERROR: Enter an account name."
            )
            return

        if not headers:
            self.query_one(
                "#status",
                Label,
            ).update(
                "ERROR: Paste the request headers."
            )
            return

        event.button.disabled = True

        self.query_one(
            "#status",
            Label,
        ).update(
            "Creating authentication and testing the "
            "complete liked-music playlist..."
        )

        threading.Thread(
            target=self.create_worker,
            args=(name, headers),
            daemon=True,
        ).start()

    def create_worker(
        self,
        name: str,
        headers: str,
    ) -> None:
        try:
            account = create_account(
                name,
                headers,
            )

            self.app.call_from_thread(
                self.success,
                account,
            )

        except Exception as error:
            self.app.call_from_thread(
                self.failure,
                str(error),
            )

    def import_worker(
        self,
        name: str,
        browser: str,
        profile: str | None = None,
    ) -> None:
        try:
            account = import_account_from_browser(
                name,
                browser,
                profile,
            )

            self.app.call_from_thread(
                self.success,
                account,
            )

        except Exception as error:
            self.app.call_from_thread(
                self.failure,
                str(error),
            )

    def update_status(
        self,
        message: str,
    ) -> None:
        self.query_one(
            "#status",
            Label,
        ).update(
            message
        )

    def success(
        self,
        account: dict[str, Any],
    ) -> None:
        self.dismiss(account)

    def failure(
        self,
        error: str,
    ) -> None:
        self.query_one(
            "#add",
            Button,
        ).disabled = False

        self.query_one(
            "#add-browser",
            Button,
        ).disabled = False

        self.query_one(
            "#status",
            Label,
        ).update(
            f"ERROR: {error}"
        )


# ============================================================================
# Download options modal
# ============================================================================

class DownloadOptionsScreen(ModalScreen):
    CSS = """
    DownloadOptionsScreen {
        align: center middle;
    }

    #modal {
        width: 72%;
        height: auto;
        max-height: 90%;
        background: $surface;
        border: double $primary;
        padding: 2;
    }

    #title {
        color: $accent;
        text-style: bold;
        padding-bottom: 1;
    }

    .row {
        height: auto;
        margin-bottom: 1;
    }

    .section-label {
        color: $accent;
        text-style: bold;
        margin-top: 1;
    }

    RadioSet {
        height: auto;
        margin-bottom: 1;
    }

    #warning {
        color: $warning;
        padding: 1;
    }

    #buttons {
        height: auto;
        margin-top: 1;
    }

    #buttons Button {
        margin-right: 1;
    }
    """

    def __init__(
        self,
        account: dict[str, Any],
        track_count: int,
        default_quality: str,
        default_policy: str,
        default_session: str,
    ):
        super().__init__()

        self.account = account
        self.track_count = track_count
        self.default_quality = default_quality
        self.default_policy = default_policy
        self.default_session = default_session

    def compose(self) -> ComposeResult:
        quality_values = [
            (label, value)
            for label, value in QUALITY_OPTIONS
        ]

        yield Container(
            Label(
                "DOWNLOAD OPTIONS",
                id="title",
            ),

            Static(
                f"Account: {self.account['name']}\n"
                f"Tracks in current liked list: {self.track_count}\n\n"
                "Choose the settings for THIS download session.",
            ),

            Label(
                "Audio quality",
                classes="section-label",
            ),

            RadioSet(
                *[
                    RadioButton(
                        label,
                        value=value == self.default_quality,
                        id=value,
                    )
                    for label, value in quality_values
                ],
                name="quality",
            ),

            Label(
                "If audio already exists",
                classes="section-label",
            ),

            RadioSet(
                RadioButton(
                    "Skip existing file",
                    value=self.default_policy == "skip",
                    id="skip",
                ),
                RadioButton(
                    "Replace existing file",
                    value=self.default_policy == "replace",
                    id="replace",
                ),
                name="policy",
            ),

            Label(
                "Session",
                classes="section-label",
            ),

            RadioSet(
                RadioButton(
                    "Run the whole list once",
                    value=self.default_session == "once",
                    id="once",
                ),
                RadioButton(
                    "Loop until stopped",
                    value=self.default_session == "loop",
                    id="loop",
                ),
                RadioButton(
                    "Skip anything already downloaded",
                    value=self.default_session == "skip",
                    id="skip",
                ),
                name="session",
            ),

            Static(
                "⚠ These settings are locked when Start is pressed "
                "and cannot change until the session finishes.",
                id="warning",
            ),

            Horizontal(
                Button(
                    "Start Download",
                    id="start",
                    variant="success",
                ),
                Button(
                    "Cancel",
                    id="cancel",
                ),
                id="buttons",
            ),

            id="modal",
        )

    def on_button_pressed(
        self,
        event: Button.Pressed,
    ) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return

        if event.button.id != "start":
            return

        quality = checked_radio_value(
            self,
            "quality",
        )

        policy = checked_radio_value(
            self,
            "policy",
        )

        session_mode = checked_radio_value(
            self,
            "session",
        )

        if quality is None:
            self.app.notify(
                "Choose an audio quality.",
                severity="warning",
            )
            return

        if policy is None:
            self.app.notify(
                "Choose an existing-file policy.",
                severity="warning",
            )
            return

        if session_mode is None:
            self.app.notify(
                "Choose a session mode.",
                severity="warning",
            )
            return

        session = DownloadSession(
            quality=str(quality),
            existing_policy=str(policy),
            session=str(session_mode),
        )

        # Persist only as the next-session default.
        try:
            settings = self.app.settings
            settings["default_quality"] = session.quality
            settings["default_existing_policy"] = session.existing_policy
            settings["default_session"] = session.session
            save_settings(settings)
        except Exception:
            pass

        self.dismiss(session)


# ============================================================================
# Most Played download modal
# ============================================================================

class MostPlayedScreen(ModalScreen):
    CSS = """
    MostPlayedScreen {
        align: center middle;
    }

    #modal {
        width: 86%;
        height: auto;
        max-height: 90%;
        background: $surface;
        border: double $primary;
        padding: 2;
    }

    #title {
        color: $accent;
        text-style: bold;
        padding-bottom: 1;
    }

    .row {
        height: auto;
        margin-bottom: 1;
    }

    .section-label {
        color: $accent;
        text-style: bold;
        margin-top: 1;
    }

    #ranking {
        height: 1fr;
        min-height: 10;
        max-height: 16;
        border: solid $secondary;
        margin-top: 1;
        margin-bottom: 1;
    }

    #count-row {
        height: auto;
        margin-bottom: 1;
    }

    #count-input {
        width: 20;
    }

    RadioSet {
        height: auto;
        margin-bottom: 1;
    }

    #hint {
        color: $text-muted;
        padding: 1;
    }

    #buttons {
        height: auto;
        margin-top: 1;
    }

    #buttons Button {
        margin-right: 1;
    }
    """

    def __init__(
        self,
        account: dict[str, Any],
        tracks: list[dict[str, Any]],
        plays: dict[str, int],
        default_limit: int,
        default_quality: str,
        default_policy: str,
        default_session: str,
    ):
        super().__init__()

        self.account = account
        self.tracks = tracks
        self.plays = plays
        self.default_limit = default_limit
        self.default_quality = default_quality
        self.default_policy = default_policy
        self.default_session = default_session

    def compose(self) -> ComposeResult:
        quality_values = [
            (label, value)
            for label, value in QUALITY_OPTIONS
        ]

        yield Container(
            Label(
                "MOST PLAYED",
                id="title",
            ),

            Static(
                f"Account: {self.account['name']}\n"
                "Ranks your watch history by play frequency. "
                "Pick how many of the most-played tracks to download.\n"
                "Click a row to set the count.",
            ),

            ListView(
                *[
                    ListItem(
                        Label(
                            (
                                f"#{index + 1} · {self.plays[track.get('videoId', '')]} plays"
                                f" · {get_artist(track)} — {track_title(track)}"
                            )
                        )
                    )
                    for index, track in enumerate(
                        self.tracks[:100]
                    )
                ],
                id="ranking",
            ),

            Horizontal(
                Label(
                    "Top N to download",
                    classes="setting-label",
                ),
                Input(
                    value=str(self.default_limit),
                    id="count-input",
                    classes="setting-input",
                ),
                id="count-row",
            ),

            Label(
                "Audio quality",
                classes="section-label",
            ),

            RadioSet(
                *[
                    RadioButton(
                        label,
                        value=value == self.default_quality,
                        id=value,
                    )
                    for label, value in quality_values
                ],
                name="quality",
            ),

            Label(
                "If audio already exists",
                classes="section-label",
            ),

            RadioSet(
                RadioButton(
                    "Skip existing file",
                    value=self.default_policy == "skip",
                    id="skip",
                ),
                RadioButton(
                    "Replace existing file",
                    value=self.default_policy == "replace",
                    id="replace",
                ),
                name="policy",
            ),

            Label(
                "Session",
                classes="section-label",
            ),

            RadioSet(
                RadioButton(
                    "Run the whole list once",
                    value=self.default_session == "once",
                    id="once",
                ),
                RadioButton(
                    "Loop until stopped",
                    value=self.default_session == "loop",
                    id="loop",
                ),
                RadioButton(
                    "Skip anything already downloaded",
                    value=self.default_session == "skip",
                    id="skip",
                ),
                name="session",
            ),

            Static(
                "Downloads go into the Most_Played folder. "
                "Settings are locked once Start is pressed.",
                id="hint",
            ),

            Horizontal(
                Button(
                    "Start Download",
                    id="start",
                    variant="success",
                ),
                Button(
                    "Cancel",
                    id="cancel",
                ),
                id="buttons",
            ),

            id="modal",
        )

    def on_list_view_selected(
        self,
        event: ListView.Selected,
    ) -> None:
        index = self.query_one(
            "#ranking",
            ListView,
        ).index

        if index is not None:
            self.query_one(
                "#count-input",
                Input,
            ).value = str(
                index + 1
            )

    def on_button_pressed(
        self,
        event: Button.Pressed,
    ) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return

        if event.button.id != "start":
            return

        count_input = self.query_one(
            "#count-input",
            Input,
        )

        try:
            count = int(
                count_input.value
            )
        except ValueError:
            self.app.notify(
                "Enter a number of tracks to download.",
                severity="warning",
            )
            return

        if count < 1:
            self.app.notify(
                "Enter at least 1 track.",
                severity="warning",
            )
            return

        if count > len(self.tracks):
            count = len(self.tracks)

        quality = checked_radio_value(
            self,
            "quality",
        )

        policy = checked_radio_value(
            self,
            "policy",
        )

        session_mode = checked_radio_value(
            self,
            "session",
        )

        if quality is None:
            self.app.notify(
                "Choose an audio quality.",
                severity="warning",
            )
            return

        if policy is None:
            self.app.notify(
                "Choose an existing-file policy.",
                severity="warning",
            )
            return

        if session_mode is None:
            self.app.notify(
                "Choose a session mode.",
                severity="warning",
            )
            return

        session = DownloadSession(
            quality=str(quality),
            existing_policy=str(policy),
            session=str(session_mode),
        )

        self.dismiss(
            {
                "count": count,
                "session": session,
            }
        )


# ============================================================================
# Listening stats modal (local play log — parity with the web insights view)
# ============================================================================

def compute_play_stats() -> tuple[dict[str, Any], dict[str, Any]]:
    """Aggregate files/<slug>/.plays.json for the TUI.

    Returns (play_stats, history_stats) — the same local data the web
    insights view shows, read directly without starting the web server.
    """
    module = sys.modules[__name__]
    from web import plays as plays_module

    return (
        plays_module.play_stats(module),
        plays_module.history_stats(module, None, 90),
    )


class PlayStatsScreen(ModalScreen):
    CSS = """
    PlayStatsScreen {
        align: center middle;
    }

    #modal {
        width: 88%;
        height: 86%;
        background: $surface;
        border: double $primary;
        padding: 2;
    }

    #title {
        color: $accent;
        text-style: bold;
        padding-bottom: 1;
    }

    #summary {
        height: auto;
        padding-bottom: 1;
    }

    #stats-content {
        height: 1fr;
        overflow-y: auto;
        border: solid $secondary;
        padding: 1;
    }

    #buttons {
        height: auto;
        margin-top: 1;
    }

    #buttons Button {
        margin-right: 1;
    }
    """

    def __init__(
        self,
        stats: dict[str, Any],
        history: dict[str, Any],
    ) -> None:
        super().__init__()

        self.stats = stats
        self.history = history

    def render_summary(self) -> str:
        stats = self.stats
        history = self.history

        total = int(stats.get("total_plays", 0) or 0)
        unique = int(stats.get("unique_tracks", 0) or 0)

        parts = [
            f"Total plays: {total}",
            f"Unique tracks: {unique}",
        ]

        streaks = history.get("streaks") or {}
        current = int(streaks.get("current", 0) or 0)
        if current:
            best = int(streaks.get("best", 0) or 0)
            streak = f"Streak: {current} day(s)"
            if best > current:
                streak += f" (best {best})"
            parts.append(streak)

        week = history.get("week") or {}
        if "this" in week:
            parts.append(
                f"This week: {week.get('this', 0)} "
                f"(prev {week.get('prev', 0)})"
            )

        return "  ·  ".join(parts)

    def render_content(self) -> str:
        stats = self.stats
        lines: list[str] = []

        per_account = stats.get("per_account") or {}
        if per_account:
            lines.append("PER ACCOUNT")
            for slug, count in sorted(
                per_account.items(),
                key=lambda item: -item[1],
            ):
                lines.append(f"  {slug:<20} {count:>5} plays")
            lines.append("")

        lines.append("TOP TRACKS (local plays)")
        top_tracks = stats.get("top_tracks") or []
        if top_tracks:
            for index, item in enumerate(top_tracks[:15], 1):
                plays = int(item.get("plays", 0) or 0)
                title = item.get("title") or item.get("id", "?")
                artist = item.get("artist") or ""
                lines.append(
                    f"  #{index:<3}{plays:>5}  {title} — {artist}"
                )
        else:
            lines.append(
                "  No plays recorded yet — play tracks in the web UI "
                "and they will show up here."
            )

        top_artists = stats.get("top_artists") or []
        if top_artists:
            lines.append("")
            lines.append("TOP ARTISTS")
            for index, item in enumerate(top_artists[:8], 1):
                plays = int(item.get("plays", 0) or 0)
                lines.append(
                    f"  #{index:<3}{plays:>5}  "
                    f"{item.get('artist', '?')}"
                )

        return "\n".join(lines)

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label("LISTENING STATS", id="title"),
            Static(
                self.render_summary(),
                id="summary",
            ),
            Static(
                self.render_content(),
                id="stats-content",
            ),
            Horizontal(
                Button(
                    "Refresh",
                    id="refresh",
                ),
                Button(
                    "Close",
                    id="close",
                ),
                id="buttons",
            ),
            id="modal",
        )

    def on_button_pressed(
        self,
        event: Button.Pressed,
    ) -> None:
        if event.button.id == "close":
            self.dismiss(None)
            return

        if event.button.id == "refresh":
            try:
                stats, history = compute_play_stats()
            except Exception as error:
                self.app.notify(
                    f"Could not reload listening stats: {error}",
                    severity="error",
                )
                return
            self.stats = stats
            self.history = history
            self.query_one("#summary", Static).update(
                self.render_summary(),
            )
            self.query_one("#stats-content", Static).update(
                self.render_content(),
            )
            self.app.notify(
                "Listening stats reloaded.",
                severity="information",
            )


# ============================================================================
# Results / inventory modal
# ============================================================================

class LibraryStatusScreen(ModalScreen):
    CSS = """
    LibraryStatusScreen {
        align: center middle;
    }

    #modal {
        width: 94%;
        height: 92%;
        background: $surface;
        border: double $primary;
        padding: 2;
    }

    #title {
        color: $accent;
        text-style: bold;
        padding-bottom: 1;
    }

    #summary {
        height: auto;
        min-height: 5;
        padding-bottom: 1;
    }

    #quality-stats {
        height: auto;
        border: solid $secondary;
        padding: 1;
        margin-bottom: 1;
    }

    #health {
        height: auto;
        border: solid $secondary;
        padding: 1;
        margin-bottom: 1;
    }

    #download-filter {
        margin-bottom: 1;
    }

    #downloads {
        height: auto;
        border: solid $secondary;
        padding: 1;
        margin-bottom: 1;
    }

    #details {
        height: auto;
        border: solid $secondary;
        padding: 1;
    }

    #content {
        height: 1fr;
        overflow: auto;
    }

    #buttons {
        height: auto;
        margin-top: 1;
    }
    """

    def __init__(
        self,
        account: dict[str, Any],
        quality: str,
        report: dict[str, Any],
    ):
        super().__init__()

        self.account = account
        self.quality = quality
        self.report = report

    def render_downloaded(
        self,
        query: str = "",
    ) -> str:
        items = self.report.get(
            "liked_downloaded",
            [],
        )

        query = query.strip().lower()

        filtered = []

        for item in items:
            artist = get_artist(
                item["track"]
            )
            title = track_title(
                item["track"]
            )
            file = item.get(
                "file",
                "",
            )

            if not query or (
                query in artist.lower()
                or query in title.lower()
                or query in file.lower()
            ):
                filtered.append(
                    (artist, title, file)
                )

        if not filtered:
            return "══ DOWNLOADED (0) ══\n  None."

        lines = [
            (
                f"══ DOWNLOADED "
                f"({len(filtered)}) ══"
            )
        ]

        for artist, title, file in filtered:
            lines.extend(
                [
                    f"  • {artist} — {title}",
                    f"    File: {file}",
                ]
            )

        return "\n".join(lines)

    def on_mount(self) -> None:
        account = self.account

        def work() -> None:
            try:
                stats = aggregate_quality_stats(
                    account
                )
            except Exception:
                stats = None

            try:
                health = account_health_summary(
                    account
                )
            except Exception:
                health = None

            try:
                self.app.call_from_thread(
                    self.stats_ready,
                    stats,
                )
            except Exception:
                pass

            try:
                self.app.call_from_thread(
                    self.health_ready,
                    health,
                )
            except Exception:
                pass

        threading.Thread(
            target=work,
            daemon=True,
        ).start()

    def stats_ready(
        self,
        stats: list[dict[str, Any]] | None,
    ) -> None:
        try:
            widget = self.query_one(
                "#quality-stats",
                Static,
            )
        except Exception:
            return

        if stats is None:
            widget.update(
                "Per-file-type stats unavailable."
            )
            return

        lines = []
        total_count = 0
        total_bytes = 0
        total_seconds = 0.0

        for stat in stats:
            total_count += stat["count"]
            total_bytes += stat["bytes"]
            total_seconds += stat["seconds"]

            lines.append(
                (
                    f"  {stat['label']}: "
                    f"{stat['count']} songs · "
                    f"{format_size(stat['bytes'])} · "
                    f"{format_duration(stat['seconds'])}"
                )
            )

        lines.append(
            "  " + "-" * 40
        )
        lines.append(
            (
                f"  TOTAL: {total_count} songs · "
                f"{format_size(total_bytes)} · "
                f"{format_duration(total_seconds)}"
            )
        )

        widget.update(
            "\n".join(lines)
        )

    def health_ready(
        self,
        health: dict[str, Any] | None,
    ) -> None:
        try:
            widget = self.query_one(
                "#health",
                Static,
            )
        except Exception:
            return

        if health is None:
            widget.update(
                "Health summary unavailable."
            )
            return

        counts = health["status_counts"]

        def status_row(
            label: str,
            status: str,
        ) -> str | None:
            value = counts.get(
                status,
                0,
            )

            if not value:
                return None

            return (
                f"  {label}: "
                f"{value}"
            )

        def flag_entries(
            title: str,
            entries: list[dict[str, Any]],
            formatter,
        ) -> list[str]:
            lines = [f"  \u26a0 {title}: {len(entries)}"]

            if not entries:
                lines.append("    None.")
                return lines

            shown = entries[:20]

            for entry in shown:
                lines.extend(
                    formatter(entry)
                )

            if len(entries) > len(shown):
                lines.append(
                    f"    \u2026 and "
                    f"{len(entries) - len(shown)} more."
                )

            return lines

        lines = [
            "══ HEALTH SUMMARY ══",
            f"  Liked tracks: {health['liked_total']}",
        ]

        for label, status in (
            ("Downloaded quality records", "downloaded"),
            ("Failed quality records", "failed"),
            ("Skipped quality records", "skipped"),
            ("Unavailable quality records", "unavailable"),
        ):
            row = status_row(
                label,
                status,
            )

            if row is not None:
                lines.append(row)

        lines.extend(
            flag_entries(
                "Liked with no download in any quality",
                health[
                    "liked_without_download"
                ],
                lambda entry: [
                    (
                        f"    \u2022 {entry['artist']} "
                        f"\u2014 {entry['title']}"
                    )
                ],
            )
        )

        lines.extend(
            flag_entries(
                "Failed downloads on liked tracks",
                health["failed_on_liked"],
                lambda entry: [
                    (
                        f"    \u2022 {entry['artist']} "
                        f"\u2014 {entry['title']} "
                        f"(quality: {entry['quality']}, "
                        f"{entry['reason']})"
                    )
                ],
            )
        )

        widget.update(
            "\n".join(lines)
        )

    def on_input_changed(
        self,
        event: Input.Changed,
    ) -> None:
        if event.input.id == "download-filter":
            self.query_one(
                "#downloads",
                Static,
            ).update(
                self.render_downloaded(
                    event.value
                )
            )

    def compose(self) -> ComposeResult:
        r = self.report

        summary = (
            f"Quality: {quality_label(self.quality)}\n"
            f"Liked tracks: {r['liked_total']}\n"
            f"Downloaded now: {r['downloaded_total']}\n"
            f"Not downloaded: {r['not_downloaded_total']}\n"
            f"Previously downloaded but file missing: "
            f"{r['missing_total']}\n"
            f"Downloaded but no longer liked: "
            f"{r['no_longer_liked_total']}\n"
            f"Previously failed: {r['failed_total']}\n"
            f"Previously skipped: {r['skipped_total']}\n"
            f"Premium-locked: {r['premium_total']}"
        )

        lines = []

        def add_section(
            title: str,
            items: list[dict[str, Any]],
            formatter,
        ):
            lines.append("")
            lines.append(
                f"══ {title} ({len(items)}) ══"
            )

            if not items:
                lines.append("  None.")
                return

            for item in items:
                lines.extend(
                    formatter(item)
                )

        add_section(
            "NOT DOWNLOADED",
            r["not_downloaded"],
            lambda item: [
                (
                    f"  • {get_artist(item['track'])} — "
                    f"{track_title(item['track'])}"
                ),
                f"    Reason: {item['reason']}",
                f"    Video ID: {item['video_id']}",
            ],
        )

        add_section(
            "MISSING FILES",
            r["missing_files"],
            lambda item: [
                (
                    f"  • {get_artist(item['track'])} — "
                    f"{track_title(item['track'])}"
                ),
                f"    Reason: {item['reason']}",
                f"    Expected: {item['file']}",
            ],
        )

        add_section(
            "NO LONGER LIKED",
            r["no_longer_liked"],
            lambda item: [
                (
                    f"  • {item['artist']} — "
                    f"{item['title']}"
                ),
                f"    Reason: {item['reason']}",
                f"    File: {item['file']}",
            ],
        )

        add_section(
            "FAILED",
            r["failed"],
            lambda item: [
                (
                    f"  • {get_artist(item['track'])} — "
                    f"{track_title(item['track'])}"
                ),
                f"    Reason: {item['reason']}",
            ],
        )

        add_section(
            "SKIPPED",
            r["skipped"],
            lambda item: [
                (
                    f"  • {get_artist(item['track'])} — "
                    f"{track_title(item['track'])}"
                ),
                f"    Reason: {item['reason']}",
            ],
        )

        add_section(
            "PREMIUM-LOCKED",
            r["premium"],
            lambda item: [
                (
                    f"  • {get_artist(item['track'])} — "
                    f"{track_title(item['track'])}"
                ),
                (
                    "    Reason: requires YouTube Music "
                    "Premium to download."
                ),
            ],
        )

        yield Container(
            Label(
                "LIBRARY STATUS",
                id="title",
            ),

            Vertical(
                Static(
                    summary,
                    id="summary",
                ),

                Static(
                    "Scanning audio files...",
                    id="quality-stats",
                ),

                Static(
                    "Computing health summary...",
                    id="health",
                ),

                Input(
                    placeholder="Filter downloaded tracks...",
                    id="download-filter",
                ),

                Static(
                    self.render_downloaded(),
                    id="downloads",
                ),

                Static(
                    "\n".join(lines),
                    id="details",
                ),

                id="content",
            ),

            Horizontal(
                Button(
                    "Repair Downloads",
                    id="repair",
                ),
                Button(
                    "Purge Unliked",
                    id="purge",
                ),
                Button(
                    "Close",
                    id="close",
                ),
                id="buttons",
            ),

            id="modal",
        )

    def on_button_pressed(
        self,
        event: Button.Pressed,
    ) -> None:
        if event.button.id == "close":
            self.dismiss(None)
            return

        if event.button.id == "repair":
            pending = repair_pending_tracks(
                self.account,
                self.quality,
            )

            if not pending:
                self.app.notify(
                    "Nothing to repair \u2014 every liked track "
                    "already has a file at this quality.",
                    severity="information",
                )
                return

            self.dismiss(None)

            self.app.start_download(
                self.account,
                pending,
                DownloadSession(
                    quality=self.quality,
                    existing_policy="skip",
                    session="once",
                ),
            )

            self.app.notify(
                (
                    f"Repairing {len(pending)} track(s) at "
                    f"{quality_label(self.quality)} \u2026"
                ),
                severity="information",
            )
            return

        if event.button.id == "purge":
            unliked = unliked_inventory_records(
                self.account
            )

            if not unliked:
                self.app.notify(
                    "No no-longer-liked files to purge.",
                    severity="information",
                )
                return

            self.dismiss(None)

            self.app.push_screen(
                PurgeUnlikedScreen(
                    self.account,
                    unliked,
                )
            )


# ============================================================================
# Purge unliked modal
# ============================================================================

class PurgeUnlikedScreen(ModalScreen):
    CSS = """
    PurgeUnlikedScreen {
        align: center middle;
    }

    #modal {
        width: 80%;
        height: 80%;
        background: $surface;
        border: double $error;
        padding: 2;
    }

    #title {
        color: $error;
        text-style: bold;
        padding-bottom: 1;
    }

    #summary {
        height: auto;
        padding-bottom: 1;
    }

    #content {
        height: 1fr;
        overflow-y: auto;
        border: solid $secondary;
        padding: 0 1;
        margin-bottom: 1;
    }

    #content .entry {
        height: auto;
        padding-bottom: 1;
    }

    #buttons {
        height: auto;
        margin-top: 1;
    }

    #buttons Button {
        margin-right: 1;
    }
    """

    def __init__(
        self,
        account: dict[str, Any],
        unliked: list[dict[str, Any]],
    ):
        super().__init__()
        self.account = account
        self.unliked = unliked

    def compose(self) -> ComposeResult:
        total_bytes = sum(
            item["size_bytes"]
            for item in self.unliked
        )

        preview = self.unliked[:30]

        lines = []

        for item in preview:
            lines.extend(
                [
                    (
                        f"\u2022 {item['artist']} \u2014 "
                        f"{item['title']} "
                        f"({format_size(item['size_bytes'])})"
                    )
                ]
            )

        if len(self.unliked) > len(preview):
            lines.append(
                f"\u2026 and "
                f"{len(self.unliked) - len(preview)} more."
            )

        yield Container(
            Label(
                "PURGE UNLIKED",
                id="title",
            ),
            Static(
                (
                    f"{len(self.unliked)} track(s) were downloaded "
                    f"but are no longer in Liked Music. Deleting their "
                    f"local files frees {format_size(total_bytes)}.\n"
                ),
                id="summary",
            ),
            VerticalScroll(
                *[
                    Static(
                        "\n".join(lines),
                        classes="entry",
                    )
                ],
                id="content",
            ),
            Horizontal(
                Button(
                    "Cancel",
                    id="cancel",
                ),
                Button(
                    "Delete Files",
                    id="delete",
                    variant="error",
                ),
                id="buttons",
            ),
            id="modal",
        )

    def on_button_pressed(
        self,
        event: Button.Pressed,
    ) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return

        if event.button.id != "delete":
            return

        video_ids = [
            item["video_id"]
            for item in self.unliked
        ]

        try:
            result = purge_unliked_records(
                self.account,
                video_ids,
            )
        except Exception as error:
            self.app.notify(
                f"Purge failed: {error}",
                severity="error",
            )
            return

        self.dismiss(None)

        self.app.notify(
            (
                f"Purged {len(result['video_ids'])} track(s), "
                f"freed {format_size(result['freed_bytes'])}."
            ),
            severity="success",
            timeout=8,
        )


# ============================================================================
# Duplicates modal
# ============================================================================

class DuplicatesScreen(ModalScreen):
    CSS = """
    DuplicatesScreen {
        align: center middle;
    }

    #modal {
        width: 94%;
        height: 92%;
        background: $surface;
        border: double $primary;
        padding: 2;
    }

    #title {
        color: $accent;
        text-style: bold;
        padding-bottom: 1;
    }

    #summary {
        height: auto;
        padding-bottom: 1;
    }

    #content {
        height: 1fr;
        overflow-y: auto;
        border: solid $secondary;
        padding: 0 1;
        margin-bottom: 1;
    }

    #content .section-heading {
        text-style: bold;
        color: $accent;
        padding-top: 1;
        padding-bottom: 1;
    }

    #content .dup-row {
        height: auto;
        width: 100%;
        padding-bottom: 1;
    }

    #content .dup-row Checkbox {
        padding: 0 1 0 0;
    }

    #content .dup-row Label {
        height: auto;
        width: 1fr;
    }

    #buttons {
        height: auto;
        align-horizontal: right;
    }

    #buttons Button {
        margin-left: 1;
    }
    """

    def __init__(
        self,
        report: dict[str, Any],
        accounts: list[dict[str, Any]],
    ) -> None:
        super().__init__()
        self.report = report
        self.accounts = accounts
        self.candidates = duplicate_removal_candidates(
            accounts,
            report,
        )
        self.removed: set[int] = set()

    def _row_label(
        self,
        index: int,
    ) -> str:
        candidate = self.candidates[index]

        files_word = (
            "downloaded"
            if candidate["has_files"]
            else "no local files"
        )

        return (
            f"  {candidate['account_name']}: "
            f"{candidate['artist']} \u2014 "
            f"{candidate['title']} "
            f"[{candidate['kind']}, {files_word}]"
        )

    def _summary_text(self) -> str:
        report = self.report
        total = len(self.candidates)
        remaining = total - len(self.removed)

        text = (
            f"{report['duplicate_count']} duplicate songs across "
            f"{report['account_count']} accounts, based on the last "
            "refresh of each account. "
        )

        if remaining:
            text += (
                f"{remaining} removable copy/copies found. "
                "Check the rows you want to delete, then press "
                "\u201cRemove selected\u201d."
            )
        else:
            text += (
                "All duplicate copies found have been removed."
            )

        return text

    def compose(self) -> ComposeResult:
        yield Container(
            Label(
                "DUPLICATES ACROSS ACCOUNTS",
                id="title",
            ),
            Static(
                self._summary_text(),
                id="summary",
            ),
            VerticalScroll(
                Label(
                    "SAME UPLOAD LIKED IN MULTIPLE ACCOUNTS",
                    classes="section-heading",
                ),
                *(
                    self._build_row(index)
                    for index, candidate in enumerate(
                        self.candidates
                    )
                    if candidate["kind"] == "same upload"
                ),
                Label(
                    "SAME SONG, DIFFERENT UPLOADS",
                    classes="section-heading",
                ),
                *(
                    self._build_row(index)
                    for index, candidate in enumerate(
                        self.candidates
                    )
                    if candidate["kind"]
                    == "different upload"
                ),
                id="content",
            ),
            Horizontal(
                Button(
                    "Remove selected",
                    id="remove",
                ),
                Button(
                    "Export JSON",
                    id="export",
                ),
                Button(
                    "Close",
                    id="close",
                ),
                id="buttons",
            ),
            id="modal",
        )

    def _build_row(
        self,
        index: int,
    ) -> Horizontal:
        return Horizontal(
            Checkbox(
                value=False,
                id=f"dup-{index}",
            ),
            Label(
                self._row_label(index),
                id=f"duplabel-{index}",
            ),
            classes="dup-row",
        )

    def on_button_pressed(
        self,
        event: Button.Pressed,
    ) -> None:
        if event.button.id == "remove":
            self._remove_selected(
                event.button
            )

        elif event.button.id == "export":
            try:
                path = export_duplicates_report(
                    self.report
                )

                self.notify(
                    f"Duplicates report saved to {path}.",
                    severity="success",
                    timeout=8,
                )

            except Exception as error:
                self.notify(
                    f"Could not export report: {error}",
                    severity="error",
                )

        elif event.button.id == "close":
            self.dismiss(None)

    def _remove_selected(
        self,
        button: Button,
    ) -> None:
        indexes = [
            index
            for index in range(
                len(self.candidates)
            )
            if index not in self.removed
            and self.query_one(
                f"#dup-{index}",
                Checkbox,
            ).value
        ]

        if not indexes:
            self.notify(
                "No duplicate rows selected.",
                severity="warning",
            )
            return

        button.disabled = True

        account_by_slug = {
            account["slug"]: account
            for account in self.accounts
        }

        threading.Thread(
            target=self._remove_worker,
            args=(
                indexes,
                account_by_slug,
            ),
            daemon=True,
        ).start()

    def _remove_worker(
        self,
        indexes: list[int],
        account_by_slug: dict[str, dict[str, Any]],
    ) -> None:
        groups: dict[str, list[int]] = {}

        for index in indexes:
            groups.setdefault(
                self.candidates[index]["slug"],
                [],
            ).append(index)

        results: list[tuple[int, list[str]]] = []
        failures: list[tuple[int, str]] = []

        for slug, group_indexes in groups.items():
            account = account_by_slug.get(
                slug
            )

            if account is None:
                for index in group_indexes:
                    failures.append(
                        (
                            index,
                            "account no longer exists",
                        )
                    )
                continue

            indexes_by_video: dict[str, list[int]] = {}

            for index in group_indexes:
                indexes_by_video.setdefault(
                    self.candidates[index][
                        "video_id"
                    ],
                    [],
                ).append(index)

            video_ids = list(
                indexes_by_video.keys()
            )

            try:
                outcome = remove_duplicates_from_account(
                    account,
                    video_ids,
                )
            except Exception as error:
                for index in group_indexes:
                    failures.append(
                        (
                            index,
                            str(error),
                        )
                    )
                continue

            for video_id in outcome["unliked"]:
                deleted = outcome[
                    "files_deleted"
                ].get(
                    video_id,
                    [],
                )

                for index in indexes_by_video[
                    video_id
                ]:
                    results.append(
                        (
                            index,
                            deleted,
                        )
                    )

            for video_id in outcome["still_liked"]:
                for index in indexes_by_video[
                    video_id
                ]:
                    failures.append(
                        (
                            index,
                            "still liked after refresh",
                        )
                    )

            for video_id, message in (
                outcome["errors"].items()
            ):
                for index in indexes_by_video.get(
                    video_id,
                    [],
                ):
                    failures.append(
                        (
                            index,
                            message,
                        )
                    )

        self.app.call_from_thread(
            self._remove_done,
            results,
            failures,
        )

    def _remove_done(
        self,
        results: list[tuple[int, list[str]]],
        failures: list[tuple[int, str]],
    ) -> None:
        unliked_songs = []

        for index, deleted in results:
            self.removed.add(index)

            checkbox = self.query_one(
                f"#dup-{index}",
                Checkbox,
            )
            checkbox.value = False
            checkbox.disabled = True

            label = self.query_one(
                f"#duplabel-{index}",
                Label,
            )

            note = (
                f"{len(deleted)} file(s) deleted"
                if deleted
                else "no local files"
            )

            label.update(
                f"  \u2714 REMOVED \u2014 {note} "
                f"({self.candidates[index]['slug']})"
            )

            candidate = self.candidates[index]

            unliked_songs.append(
                f"{candidate['artist']} \u2014 "
                f"{candidate['title']} "
                f"({candidate['account_name']})"
            )

        for index, message in failures:
            self.notify(
                f"Could not remove \u201c"
                f"{self.candidates[index]['title']}"
                f"\u201d from "
                f"{self.candidates[index]['account_name']}: "
                f"{message}",
                severity="error",
                timeout=8,
            )

        self.query_one(
            "#summary",
            Static,
        ).update(
            self._summary_text()
        )

        button = self.query_one(
            "#remove",
            Button,
        )
        button.disabled = (
            len(self.removed)
            >= len(self.candidates)
        )

        if unliked_songs:
            shown = unliked_songs[:5]
            joined = "; ".join(shown)

            extra = (
                f" (+{len(unliked_songs) - 5} more)"
                if len(unliked_songs) > 5
                else ""
            )

            word = (
                "song"
                if len(unliked_songs) == 1
                else "songs"
            )

            self.notify(
                f"Unliked {len(unliked_songs)} {word} "
                f"and refreshed to confirm: {joined}"
                f"{extra}",
                severity="success",
                timeout=10,
            )


# ============================================================================
# Settings modal
# ============================================================================

class SettingsScreen(ModalScreen):
    CSS = """
    SettingsScreen {
        align: center middle;
    }

    #settings-modal {
        width: 76%;
        height: auto;
        max-height: 90%;
        background: $surface;
        border: double $primary;
        padding: 2;
    }

    #settings-title {
        color: $accent;
        text-style: bold;
        padding-bottom: 1;
    }

    .setting-row {
        height: auto;
        margin-bottom: 1;
    }

    .setting-label {
        width: 32%;
        padding-top: 1;
    }

    .setting-input {
        width: 68%;
    }

    #settings-help {
        color: $text-muted;
        padding: 1;
        height: auto;
    }

    #settings-buttons {
        height: auto;
        margin-top: 1;
    }

    #settings-buttons Button {
        margin-right: 1;
    }
    """

    def compose(self) -> ComposeResult:
        quality_values = [
            (label, value)
            for label, value in QUALITY_OPTIONS
        ]

        yield Container(
            Label(
                "SETTINGS",
                id="settings-title",
            ),

            Static(
                "These are the defaults used when starting a new "
                "download session. You can still choose the quality "
                "and existing-file policy before each download.\n\n"
                "Once a download session starts, its quality and "
                "existing-file policy are locked until the session ends.",
                id="settings-help",
            ),

            Horizontal(
                Label(
                    "Default audio quality",
                    classes="setting-label",
                ),
                Select(
                    quality_values,
                    value=self.app.settings.get(
                        "default_quality",
                        "best",
                    ),
                    id="default-quality",
                    classes="setting-input",
                ),
                classes="setting-row",
            ),

            Horizontal(
                Label(
                    "Existing file policy",
                    classes="setting-label",
                ),
                Select(
                    [
                        (
                            "Skip if same quality exists",
                            "skip",
                        ),
                        (
                            "Replace if same quality exists",
                            "replace",
                        ),
                    ],
                    value=self.app.settings.get(
                        "default_existing_policy",
                        "skip",
                    ),
                    id="existing-policy",
                    classes="setting-input",
                ),
                classes="setting-row",
            ),

            Horizontal(
                Label(
                    "Default session mode",
                    classes="setting-label",
                ),
                Select(
                    [
                        (
                            "Run the whole list once",
                            "once",
                        ),
                        (
                            "Loop until stopped",
                            "loop",
                        ),
                        (
                            "Download missing across all qualities",
                            "skip",
                        ),
                    ],
                    value=self.app.settings.get(
                        "default_session",
                        "loop",
                    ),
                    id="default-session",
                    classes="setting-input",
                ),
                classes="setting-row",
            ),

            Horizontal(
                Label(
                    "Default most-played count",
                    classes="setting-label",
                ),
                Input(
                    value=str(
                        self.app.settings.get(
                            "default_limit",
                            25,
                        )
                    ),
                    id="default-limit",
                    classes="setting-input",
                ),
                classes="setting-row",
            ),

            Horizontal(
                Label(
                    "Retry attempts",
                    classes="setting-label",
                ),
                Input(
                    value=str(
                        self.app.settings.get(
                            "retry_attempts",
                            3,
                        )
                    ),
                    id="retry-attempts",
                    classes="setting-input",
                ),
                classes="setting-row",
            ),

            Horizontal(
                Label(
                    "Retry delay (seconds)",
                    classes="setting-label",
                ),
                Input(
                    value=str(
                        self.app.settings.get(
                            "retry_delay",
                            3,
                        )
                    ),
                    id="retry-delay",
                    classes="setting-input",
                ),
                classes="setting-row",
            ),

            Horizontal(
                Button(
                    "Save Settings",
                    id="save-settings",
                    variant="success",
                ),
                Button(
                    "Export Backup",
                    id="export-backup",
                ),
                Button(
                    "Cancel",
                    id="cancel-settings",
                ),
                id="settings-buttons",
            ),

            id="settings-modal",
        )

    def on_button_pressed(
        self,
        event: Button.Pressed,
    ) -> None:
        if event.button.id == "export-backup":
            try:
                path = write_backup_snapshot()

                self.app.notify(
                    f"Backup saved to {path}.",
                    severity="success",
                    timeout=8,
                )

            except Exception as error:
                self.app.notify(
                    f"Could not write backup: {error}",
                    severity="error",
                )

            return

        if event.button.id == "cancel-settings":
            self.dismiss(None)
            return

        if event.button.id != "save-settings":
            return

        quality = self.query_one(
            "#default-quality",
            Select,
        ).value

        policy = self.query_one(
            "#existing-policy",
            Select,
        ).value

        retry_attempts_text = self.query_one(
            "#retry-attempts",
            Input,
        ).value.strip()

        retry_delay_text = self.query_one(
            "#retry-delay",
            Input,
        ).value.strip()

        default_limit_text = self.query_one(
            "#default-limit",
            Input,
        ).value.strip()

        if quality is Select.BLANK:
            self.app.notify(
                "Choose a default audio quality.",
                severity="warning",
            )
            return

        if policy is Select.BLANK:
            self.app.notify(
                "Choose an existing-file policy.",
                severity="warning",
            )
            return

        try:
            retry_attempts = int(
                retry_attempts_text
            )
            retry_delay = int(
                retry_delay_text
            )
            default_limit = int(
                default_limit_text
            )
        except ValueError:
            self.app.notify(
                "Retry attempts, delay and most-played count "
                "must be whole numbers.",
                severity="warning",
            )
            return

        if not 1 <= retry_attempts <= 20:
            self.app.notify(
                "Retry attempts must be between 1 and 20.",
                severity="warning",
            )
            return

        if not 0 <= retry_delay <= 300:
            self.app.notify(
                "Retry delay must be between 0 and 300 seconds.",
                severity="warning",
            )
            return

        if not 1 <= default_limit <= 100:
            self.app.notify(
                "Most-played count must be between 1 and 100.",
                severity="warning",
            )
            return

        # Merge over the settings currently on disk (reloaded fresh) —
        # writing only the six keys this modal edits would wipe every
        # other persisted key (last.fm, automation, rate limit, ...).
        settings = load_settings()
        settings.update(
            {
                "default_quality": str(quality),
                "default_existing_policy": str(policy),
                "default_session": str(
                    self.query_one(
                        "#default-session",
                        Select,
                    ).value
                ),
                "default_limit": default_limit,
                "retry_attempts": retry_attempts,
                "retry_delay": retry_delay,
            }
        )

        save_settings(settings)
        self.dismiss(settings)


# ============================================================================
# Remove account modal
# ============================================================================

class ConfirmDeleteScreen(ModalScreen):
    CSS = """
    ConfirmDeleteScreen {
        align: center middle;
    }

    #modal {
        width: 65%;
        height: auto;
        background: $surface;
        border: double $error;
        padding: 2;
    }

    #buttons {
        height: auto;
        margin-top: 2;
    }

    #buttons Button {
        margin-right: 1;
    }
    """

    def __init__(
        self,
        account: dict[str, Any],
    ):
        super().__init__()
        self.account = account

    def compose(self) -> ComposeResult:
        yield Container(
            Label(
                "REMOVE ACCOUNT",
            ),

            Static(
                f"Remove '{self.account['name']}'?\n\n"
                "Authentication and account metadata will be removed.\n"
                "Downloaded music files will NOT be deleted.",
            ),

            Horizontal(
                Button(
                    "Cancel",
                    id="cancel",
                ),
                Button(
                    "Remove",
                    id="remove",
                    variant="error",
                ),
                id="buttons",
            ),
            id="modal",
        )

    def on_button_pressed(
        self,
        event: Button.Pressed,
    ) -> None:
        if event.button.id == "cancel":
            self.dismiss(False)

        elif event.button.id == "remove":
            self.dismiss(
                delete_account(
                    self.account
                )
            )


# ============================================================================
# Cancel download modal
# ============================================================================

class CancelSessionScreen(ModalScreen):
    CSS = """
    CancelSessionScreen {
        align: center middle;
    }

    #modal {
        width: 68%;
        height: auto;
        max-height: 90%;
        background: $surface;
        border: double $error;
        padding: 2;
    }

    #title {
        color: $error;
        text-style: bold;
        padding-bottom: 1;
    }

    #cancel-list {
        height: auto;
        border: solid $secondary;
        margin-top: 1;
        margin-bottom: 1;
    }

    #buttons {
        height: auto;
    }

    #buttons Button {
        margin-right: 1;
    }
    """

    def __init__(
        self,
        active_workers: list[DownloadWorker],
    ):
        super().__init__()

        self.active_workers = active_workers

    def compose(self) -> ComposeResult:
        items = []

        for worker in self.active_workers:
            slug = worker.account["slug"]
            name = worker.account["name"]

            quality = (
                worker.active_quality
                or "…"
            )

            stop_item = ListItem(
                Label(
                    f"Stop after current stage — "
                    f"{name} ({quality_label(quality)})"
                ),
                id=f"stop-{slug}",
            )
            stop_item.action = {
                "kind": "stage",
                "worker": worker,
            }

            cancel_item = ListItem(
                Label(
                    f"Cancel entire download — {name}"
                ),
                id=f"cancel-{slug}",
            )
            cancel_item.action = {
                "kind": "account",
                "worker": worker,
            }

            items.extend(
                [
                    stop_item,
                    cancel_item,
                ]
            )

        all_item = ListItem(
            Label(
                "Cancel all downloads"
            ),
            id="cancel-all",
        )
        all_item.action = {
            "kind": "all",
        }

        items.append(
            all_item
        )

        yield Container(
            Label(
                "CANCEL DOWNLOAD",
                id="title",
            ),

            Static(
                "Choose which running download to stop.",
            ),

            ListView(
                *items,
                id="cancel-list",
            ),

            Horizontal(
                Button(
                    "Cancel",
                    id="close",
                ),
                id="buttons",
            ),

            id="modal",
        )

    def on_list_view_selected(
        self,
        event: ListView.Selected,
    ) -> None:
        action = getattr(
            event.item,
            "action",
            None,
        )

        if action:
            self.dismiss(
                action
            )

    def on_button_pressed(
        self,
        event: Button.Pressed,
    ) -> None:
        if event.button.id == "close":
            self.dismiss(None)


# ============================================================================
# Main TUI
# ============================================================================

class YTMusicDownloader(App):
    TITLE = f"YT Music Downloader v{APP_VERSION}"
    SUB_TITLE = f"Multi-account YouTube Music Synchronizer • v{APP_VERSION} • {APP_BUILD}"

    CSS = """
    Screen {
        background: $surface;
    }

    Header {
        background: $primary;
    }

    Footer {
        background: $panel;
    }

    #main {
        height: 1fr;
    }

    #sidebar {
        width: 30%;
        border: solid $primary;
        padding: 1;
    }

    #content {
        width: 70%;
        border: solid $primary;
        padding: 1;
    }

    #accounts-title {
        text-style: bold;
        color: $accent;
        padding-bottom: 1;
    }

    #accounts-list {
        height: 1fr;
        border: solid $secondary;
    }

    .account-item {
        padding: 1;
    }

    #account-name {
        text-style: bold;
        color: $accent;
        padding-bottom: 1;
    }

    #account-status {
        height: auto;
        padding-bottom: 1;
    }

    #download-panel {
        border: solid $secondary;
        padding: 1;
        height: auto;
    }

    #current-track {
        text-style: bold;
        padding-bottom: 1;
    }

    #progress {
        margin-bottom: 1;
    }

    #status {
        height: auto;
        min-height: 2;
        padding-bottom: 1;
    }

    #counts {
        height: auto;
        padding-bottom: 1;
    }

    #buttons {
        height: auto;
        margin-top: 1;
    }

    #buttons Button {
        margin-right: 1;
    }

    #help {
        padding-top: 1;
        color: $text-muted;
    }

    #active-sessions-panel {
        height: 1fr;
        margin-top: 1;
        border: solid $secondary;
        padding: 1;
    }

    #active-sessions-title {
        text-style: bold;
        color: $accent;
        padding-bottom: 1;
    }

    #active-sessions {
        height: 1fr;
        overflow-y: auto;
    }

    .session-row {
        padding-bottom: 1;
    }
    """

    BINDINGS = [
        ("a", "add_account", "Add Account"),
        ("d", "download", "Download"),
        ("m", "most_played", "Most Played"),
        ("u", "duplicates", "Duplicates"),
        ("r", "refresh", "Refresh"),
        ("s", "status", "Library Status"),
        ("h", "play_stats", "Play Stats"),
        ("p", "settings", "Settings"),
        ("delete", "delete_account", "Remove"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()

        ensure_directories()

        self.settings = load_settings()

        self.accounts = []
        self.selected_account = None
        self.current_tracks = []
        self.current_quality = self.settings.get(
            "default_quality",
            "best",
        )

        self.download_workers: dict[str, DownloadWorker] = {}
        self.refreshing: set[str] = set()
        self.fetching_most_played: set[str] = set()
        self.scanning_duplicates = False
        self.environment = None

    def compose(self) -> ComposeResult:
        yield Header()

        with Horizontal(id="main"):
            with Vertical(id="sidebar"):
                yield Label(
                    "YOUTUBE MUSIC ACCOUNTS",
                    id="accounts-title",
                )

                yield ListView(
                    id="accounts-list"
                )

                with Horizontal(id="buttons"):
                    yield Button(
                        "+ Add Account",
                        id="add-account",
                        variant="success",
                    )

                    yield Button(
                        "Settings",
                        id="settings",
                    )

                    yield Button(
                        "Remove",
                        id="remove-account",
                        variant="error",
                    )

            with Vertical(id="content"):
                yield Label(
                    "Select an account",
                    id="account-name",
                )

                yield Label(
                    "No account selected.",
                    id="account-status",
                )

                with Vertical(id="download-panel"):
                    yield Label(
                        "Ready",
                        id="current-track",
                    )

                    yield ProgressBar(
                        total=100,
                        show_eta=False,
                        id="progress",
                    )

                    yield Label(
                        "Ready",
                        id="status",
                    )

                    yield Label(
                        "Downloaded: 0    "
                        "Skipped: 0    "
                        "Failed: 0    "
                        "Unavailable: 0",
                        id="counts",
                    )

                    with Horizontal(id="buttons"):
                        yield Button(
                            "Refresh Liked Music",
                            id="refresh",
                        )

                        yield Button(
                            "Download All",
                            id="download",
                            variant="success",
                        )

                        yield Button(
                            "Most Played",
                            id="most-played",
                        )

                        yield Button(
                            "Duplicates",
                            id="duplicates",
                        )

                        yield Button(
                            "Library Status",
                            id="status-button",
                        )

                        yield Button(
                            "Settings",
                            id="settings-main",
                        )

                        yield Button(
                            "Cancel",
                            id="cancel",
                            variant="error",
                        )

                yield Vertical(
                    Label(
                        "ACTIVE SESSIONS",
                        id="active-sessions-title",
                    ),
                    Vertical(
                        id="active-sessions",
                    ),
                    id="active-sessions-panel",
                )

                yield Static(
                    "A Add Account    "
                    "D Download    "
                    "M Most Played    "
                    "U Duplicates    "
                    "R Refresh    "
                    "S Library Status    "
                    "P Settings    "
                    "Delete Remove    "
                    "Q Quit",
                    id="help",
                )

        yield Footer()

    def on_mount(self) -> None:
        self.environment = {
            "python": VENV_PYTHON,
            "yt_dlp": yt_dlp_binary(),
            "deno": deno_binary(),
            "ffmpeg": Path(
                imageio_ffmpeg.get_ffmpeg_exe()
            ),
        }

        self.query_one(
            "#status",
            Label,
        ).update(
            "✓ Project environment ready"
        )

        self.refresh_accounts()

    # ------------------------------------------------------------------
    # Accounts
    # ------------------------------------------------------------------

    def refresh_accounts(self) -> None:
        self.accounts = get_accounts()

        account_list = self.query_one(
            "#accounts-list",
            ListView,
        )

        account_list.clear()

        for account in self.accounts:
            item = ListItem(
                Label(
                    f"♪  {account['name']}"
                )
            )

            item.account = account
            account_list.append(item)

        if not self.accounts:
            self.selected_account = None

            self.query_one(
                "#account-name",
                Label,
            ).update(
                "No accounts configured"
            )

            self.query_one(
                "#account-status",
                Label,
            ).update(
                "Press A or + Add Account."
            )

    def on_list_view_selected(
        self,
        event: ListView.Selected,
    ) -> None:
        account = getattr(
            event.item,
            "account",
            None,
        )

        if not account:
            return

        self.selected_account = account
        self.current_tracks = []

        self.show_account(
            account
        )

    def show_account(
        self,
        account: dict[str, Any],
    ) -> None:
        self.query_one(
            "#account-name",
            Label,
        ).update(
            account["name"]
        )

        self.query_one(
            "#account-status",
            Label,
        ).update(
            "✓ Authentication configured\n"
            f"Output: {account['output']}\n"
            "Refresh to retrieve the COMPLETE liked list."
        )

        self.query_one(
            "#current-track",
            Label,
        ).update(
            "Ready"
        )

        self.query_one(
            "#progress",
            ProgressBar,
        ).update(
            progress=0
        )

    # ------------------------------------------------------------------
    # Add account
    # ------------------------------------------------------------------

    def action_add_account(self) -> None:
        self.push_screen(
            AddAccountScreen(),
            self.account_added,
        )

    def account_added(
        self,
        account: dict[str, Any] | None,
    ) -> None:
        if not account:
            return

        self.refresh_accounts()

        for existing in self.accounts:
            if existing["slug"] == account["slug"]:
                self.selected_account = existing
                self.show_account(existing)
                break

        self.notify(
            (
                f"✓ {account['name']} added.\n"
                f"Found {account['track_count']} liked tracks."
            ),
            severity="information",
            timeout=10,
        )

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def action_settings(self) -> None:
        self.push_screen(
            SettingsScreen(),
            self.settings_saved,
        )

    def settings_saved(
        self,
        settings: dict[str, Any] | None,
    ) -> None:
        if settings is None:
            return

        self.settings = settings
        self.current_quality = settings.get(
            "default_quality",
            "best",
        )

        policy_label = (
            "Skip"
            if settings.get("default_existing_policy") == "skip"
            else "Replace"
        )

        self.query_one(
            "#status",
            Label,
        ).update(
            (
                "✓ Settings saved\n"
                f"Default quality: "
                f"{quality_label(self.current_quality)}\n"
                f"Existing-file policy: {policy_label}"
            )
        )

        self.notify(
            "Settings saved.",
            severity="information",
        )

    # ------------------------------------------------------------------
    # Remove account
    # ------------------------------------------------------------------

    def action_delete_account(self) -> None:
        if not self.selected_account:
            self.notify(
                "Select an account first.",
                severity="warning",
            )
            return

        self.push_screen(
            ConfirmDeleteScreen(
                self.selected_account
            ),
            self.account_deleted,
        )

    def account_deleted(
        self,
        deleted: bool,
    ) -> None:
        if not deleted:
            return

        self.selected_account = None
        self.current_tracks = []

        self.refresh_accounts()

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def action_refresh(self) -> None:
        if not self.selected_account:
            self.notify(
                "Select an account first.",
                severity="warning",
            )
            return

        slug = self.selected_account["slug"]

        if slug in self.refreshing:
            self.notify(
                "That account is already refreshing.",
                severity="warning",
            )
            return

        self.refreshing.add(slug)

        self.query_one(
            "#status",
            Label,
        ).update(
            "Retrieving the COMPLETE liked-music list..."
        )

        threading.Thread(
            target=self.refresh_worker,
            args=(self.selected_account,),
            daemon=True,
        ).start()

    def refresh_worker(
        self,
        account: dict[str, Any],
    ) -> None:
        slug = account["slug"]

        try:
            with self.inventory_lock(slug):
                tracks = fetch_all_liked_tracks(
                    account
                )

                report = update_inventory_from_library(
                    account,
                    tracks,
                )

                status_report = scan_inventory(
                    account,
                    tracks,
                    self.current_quality,
                )

                save_status_snapshot(
                    account,
                    self.current_quality,
                    status_report,
                )

            self.call_from_thread(
                self.refresh_complete,
                slug,
                tracks,
                report,
                status_report,
            )

        except Exception as error:
            self.call_from_thread(
                self.refresh_failed,
                slug,
                str(error),
            )

    def refresh_complete(
        self,
        slug: str,
        tracks: list[dict[str, Any]],
        inventory: dict[str, Any],
        status_report: dict[str, Any],
    ) -> None:
        self.refreshing.discard(slug)

        selected = (
            self.selected_account
            if self.selected_account
            and self.selected_account["slug"] == slug
            else None
        )

        if not selected:
            return

        self.current_tracks = tracks

        # Keep the displayed account count synchronized with the
        # complete liked-music list just fetched.
        if self.selected_account:
            self.selected_account["track_count"] = len(tracks)
            save_account_metadata(
                self.selected_account,
                track_count=len(tracks),
            )

        quality_label_text = quality_label(
            status_report.get(
                "quality",
                self.current_quality,
            )
        )

        self.query_one(
            "#status",
            Label,
        ).update(
            (
                f"✓ Found {len(tracks)} liked tracks.\n"
                "Inventory updated.\n"
                f"Not downloaded: "
                f"{status_report.get('not_downloaded_total', 0)} · "
                f"Missing files: "
                f"{status_report.get('missing_total', 0)} "
                f"({quality_label_text})"
            )
        )

        self.query_one(
            "#current-track",
            Label,
        ).update(
            "Ready"
        )

        self.notify(
            (
                f"Found {len(tracks)} liked tracks.\n"
                f"Not downloaded: "
                f"{status_report.get('not_downloaded_total', 0)}\n"
                f"Missing files: "
                f"{status_report.get('missing_total', 0)} "
                f"({quality_label_text})"
            ),
            severity="information",
            timeout=12,
        )

    def refresh_failed(
        self,
        slug: str,
        error: str,
    ) -> None:
        self.refreshing.discard(slug)

        self.query_one(
            "#status",
            Label,
        ).update(
            f"Could not refresh liked music.\n{error}"
        )

        self.notify(
            error,
            severity="error",
            timeout=12,
        )

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def action_download(self) -> None:
        if not self.selected_account:
            self.notify(
                "Select an account first.",
                severity="warning",
            )
            return

        if not self.current_tracks:
            self.notify(
                "Refresh liked music first.",
                severity="warning",
            )
            return

        slug = self.selected_account["slug"]

        if slug in self.download_workers:
            self.notify(
                "That account already has a download running.",
                severity="warning",
            )
            return

        account = self.selected_account
        tracks = list(
            self.current_tracks
        )

        self.push_screen(
            DownloadOptionsScreen(
                account,
                len(tracks),
                self.settings.get(
                    "default_quality",
                    "best",
                ),
                self.settings.get(
                    "default_existing_policy",
                    "skip",
                ),
                self.settings.get(
                    "default_session",
                    "loop",
                ),
            ),
            lambda session, account=account, tracks=tracks: (
                self.download_options_selected(
                    account,
                    tracks,
                    session,
                )
            ),
        )

    def action_most_played(self) -> None:
        if not self.selected_account:
            self.notify(
                "Select an account first.",
                severity="warning",
            )
            return

        slug = self.selected_account["slug"]

        if slug in self.download_workers:
            self.notify(
                "That account already has a download running.",
                severity="warning",
            )
            return

        if slug in self.fetching_most_played:
            self.notify(
                "Watch history is already being retrieved.",
                severity="warning",
            )
            return

        account = self.selected_account
        self.fetching_most_played.add(slug)

        self.query_one(
            "#status",
            Label,
        ).update(
            "Retrieving watch history..."
        )

        threading.Thread(
            target=self.most_played_worker,
            args=(account,),
            daemon=True,
        ).start()

    def most_played_worker(
        self,
        account: dict[str, Any],
    ) -> None:
        slug = account["slug"]

        try:
            tracks, plays = fetch_most_played(
                account
            )

            save_most_played_list(
                account,
                tracks,
                plays,
            )

            self.call_from_thread(
                self.most_played_complete,
                slug,
                tracks,
                plays,
            )

        except Exception as error:
            self.call_from_thread(
                self.most_played_failed,
                slug,
                str(error),
            )

    def most_played_complete(
        self,
        slug: str,
        tracks: list[dict[str, Any]],
        plays: dict[str, int],
    ) -> None:
        self.fetching_most_played.discard(slug)

        if not tracks:
            self.notify(
                "No watch history found.",
                severity="warning",
            )
            return

        account = next(
            (
                candidate
                for candidate in self.accounts
                if candidate["slug"] == slug
            ),
            None,
        )

        if account is None:
            return

        self.push_screen(
            MostPlayedScreen(
                account,
                tracks,
                plays,
                self.settings.get(
                    "default_limit",
                    25,
                ),
                self.settings.get(
                    "default_quality",
                    "best",
                ),
                self.settings.get(
                    "default_existing_policy",
                    "skip",
                ),
                self.settings.get(
                    "default_session",
                    "loop",
                ),
            ),
            lambda result, account=account, tracks=tracks: (
                self.most_played_selected(
                    account,
                    tracks,
                    result,
                )
            ),
        )

    def most_played_failed(
        self,
        slug: str,
        message: str,
    ) -> None:
        self.fetching_most_played.discard(slug)

        self.notify(
            f"Could not fetch watch history: {message}",
            severity="error",
        )

    def most_played_selected(
        self,
        account: dict[str, Any],
        tracks: list[dict[str, Any]],
        result: dict[str, Any] | None,
    ) -> None:
        if result is None:
            return

        count = int(
            result["count"]
        )
        session = result["session"]

        selected = tracks[:count]

        if not selected:
            self.notify(
                "Nothing to download.",
                severity="warning",
            )
            return

        output_dir = (
            account["output"]
            / "Most_Played"
        )

        output_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.start_download(
            account,
            selected,
            session,
            output_dir=output_dir,
        )

    def action_play_stats(self) -> None:
        """Open the local listening-stats modal (parity with the web
        insights view). Failures surface the real reason instead of a
        generic crash."""
        try:
            stats, history = compute_play_stats()
        except Exception as error:
            self.notify(
                f"Could not build listening stats: {error}",
                severity="error",
            )
            return

        self.push_screen(
            PlayStatsScreen(
                stats,
                history,
            )
        )

    def action_duplicates(self) -> None:
        if not self.accounts:
            self.notify(
                "Add an account first.",
                severity="warning",
            )
            return

        if self.scanning_duplicates:
            self.notify(
                "Duplicate scan already running.",
                severity="warning",
            )
            return

        self.scanning_duplicates = True

        self.query_one(
            "#status",
            Label,
        ).update(
            "Scanning inventories for duplicate songs..."
        )

        accounts = list(self.accounts)

        threading.Thread(
            target=self.duplicates_worker,
            args=(accounts,),
            daemon=True,
        ).start()

    def duplicates_worker(
        self,
        accounts: list[dict[str, Any]],
    ) -> None:
        try:
            report = (
                find_cross_account_duplicates(
                    accounts
                )
            )

            self.call_from_thread(
                self.duplicates_complete,
                report,
            )

        except Exception as error:
            self.call_from_thread(
                self.duplicates_failed,
                str(error),
            )

    def duplicates_complete(
        self,
        report: dict[str, Any],
    ) -> None:
        self.scanning_duplicates = False

        self.query_one(
            "#status",
            Label,
        ).update(
            "Ready"
        )

        if (
            not report["exact"]
            and not report["by_title"]
        ):
            self.notify(
                "No duplicate songs found across accounts.",
                severity="success",
            )
            return

        self.push_screen(
            DuplicatesScreen(
                report,
                self.accounts,
            )
        )

    def duplicates_failed(
        self,
        message: str,
    ) -> None:
        self.scanning_duplicates = False

        self.query_one(
            "#status",
            Label,
        ).update(
            "Ready"
        )

        self.notify(
            f"Duplicate scan failed: {message}",
            severity="error",
        )

    def download_options_selected(
        self,
        account: dict[str, Any],
        tracks: list[dict[str, Any]],
        session: DownloadSession | None,
    ) -> None:
        if session is None:
            return

        self.start_download(
            account,
            tracks,
            session,
        )

    def start_download(
        self,
        account: dict[str, Any],
        tracks: list[dict[str, Any]],
        session: DownloadSession,
        output_dir: Path | None = None,
    ) -> None:
        # The session object is immutable.
        # UI changes cannot affect it once the worker starts.
        self.current_quality = session.quality

        sessions = build_sessions(
            session
        )

        slug = account["slug"]
        is_selected = (
            self.selected_account is not None
            and self.selected_account["slug"] == slug
        )

        if is_selected:
            self.query_one(
                "#progress",
                ProgressBar,
            ).update(
                progress=0
            )

            self.query_one(
                "#counts",
                Label,
            ).update(
                "Downloaded: 0    "
                "Skipped: 0    "
                "Failed: 0    "
                "Unavailable: 0"
            )

            self.query_one(
                "#status",
                Label,
            ).update(
                (
                    f"Session: {session_mode_label(session.session)} | "
                    f"{quality_label(session.quality)} | "
                    f"{'Skip' if session.existing_policy == 'skip' else 'Replace'}"
                )
            )

        worker = DownloadWorker(
            account=account,
            tracks=tracks,
            sessions=sessions,
            settings=dict(self.settings),
            callback=None,
            output_dir=output_dir,
        )

        worker.callback = (
            lambda event, data: self.worker_event(
                worker,
                event,
                data,
            )
        )

        self.download_workers[slug] = worker

        self.upsert_session_row(
            worker,
            stage="starting",
        )

        threading.Thread(
            target=worker.run,
            daemon=True,
        ).start()

    def worker_event(
        self,
        worker: DownloadWorker,
        event: str,
        data: dict[str, Any],
    ) -> None:
        self.call_from_thread(
            self.handle_worker_event,
            worker,
            event,
            data,
        )

    def handle_worker_event(
        self,
        worker: DownloadWorker,
        event: str,
        data: dict[str, Any],
    ) -> None:
        account = worker.account
        slug = account["slug"]
        is_selected = (
            self.selected_account is not None
            and self.selected_account["slug"] == slug
        )

        if event == "stage":
            self.upsert_session_row(
                worker,
                stage="stage",
                quality=data["quality"],
            )

            if is_selected:
                self.query_one(
                    "#status",
                    Label,
                ).update(
                    (
                        f"Stage {data['index']}/{data['total']} — "
                        f"{quality_label(data['quality'])}"
                    )
                )

                self.query_one(
                    "#progress",
                    ProgressBar,
                ).update(
                    progress=0
                )

        elif event == "track":
            self.upsert_session_row(
                worker,
                stage="track",
                track_no=data["number"],
                total=data["total"],
            )

            if is_selected:
                self.query_one(
                    "#current-track",
                    Label,
                ).update(
                    (
                        f"[{data['number']}/{data['total']}] "
                        f"{data['artist']} — "
                        f"{data['title']}"
                    )
                )

                self.query_one(
                    "#progress",
                    ProgressBar,
                ).update(
                    progress=0
                )

        elif event == "progress":
            self.upsert_session_row(
                worker,
                progress=data["value"],
            )

            if is_selected:
                self.query_one(
                    "#progress",
                    ProgressBar,
                ).update(
                    progress=data["value"]
                )

        elif event == "status":
            if is_selected:
                self.query_one(
                    "#status",
                    Label,
                ).update(
                    data["text"]
                )

        elif event == "counts":
            self.upsert_session_row(
                worker,
                counts=data,
            )

            if is_selected:
                self.query_one(
                    "#counts",
                    Label,
                ).update(
                    (
                        f"Downloaded: {data['downloaded']}    "
                        f"Skipped: {data['skipped']}    "
                        f"Failed: {data['failed']}    "
                        f"Unavailable: {data['unavailable']}"
                    )
                )

        elif event == "finished":
            if self.download_workers.get(slug) is worker:
                del self.download_workers[slug]

            self.remove_session_row(
                slug
            )

            if data.get("error"):
                message = (
                    f"✗ Download finished after an unexpected error — "
                    f"{account['name']}."
                )
            elif data["cancelled"]:
                message = (
                    f"Download cancelled — {account['name']}."
                )
            elif data["stop"]:
                message = (
                    f"Session stopped after current stage — "
                    f"{account['name']}."
                )
            else:
                message = (
                    f"✓ Download session complete — "
                    f"{account['name']}."
                )

            if is_selected:
                self.query_one(
                    "#status",
                    Label,
                ).update(
                    message
                )

            self.notify(
                (
                    f"{message}\n"
                    f"Downloaded: {data['downloaded']}\n"
                    f"Skipped: {data['skipped']}\n"
                    f"Failed: {data['failed']}\n"
                    f"Unavailable: {data['unavailable']}"
                )
                + self._failed_titles_summary(
                    data.get("results", [])
                ),
                severity="information",
                timeout=12,
            )

    # ------------------------------------------------------------------
    # Active sessions display
    # ------------------------------------------------------------------

    def _failed_titles_summary(
        self,
        results: list[dict[str, Any]],
    ) -> str:
        failed = [
            result
            for result in results
            if result.get("status") == "failed"
        ]

        if not failed:
            return ""

        shown = failed[:5]
        more = len(failed) - len(shown)

        lines = ["", "Failed tracks:"]

        for result in shown:
            lines.append(
                f"  \u2022 {result['artist']} \u2014 "
                f"{result['title']}"
            )

        if more > 0:
            lines.append(
                f"  \u2026 and {more} more."
            )

        return "\n".join(lines)

    def inventory_lock(
        self,
        slug: str,
    ) -> threading.Lock:
        return account_inventory_lock(
            slug
        )

    def upsert_session_row(
        self,
        worker: DownloadWorker,
        stage: str = "track",
        quality: str | None = None,
        track_no: int = 0,
        total: int = 0,
        progress: int | None = None,
        counts: dict[str, int] | None = None,
    ) -> None:
        slug = worker.account["slug"]
        account_name = worker.account["name"]

        container = self.query_one(
            "#active-sessions",
            Vertical,
        )

        existing = container.query_one_optional(
            f"#session-{slug}",
            Label,
        )

        if quality is None:
            quality = (
                worker.active_quality
                or "…"
            )

        if progress is None:
            progress = worker.active_progress
        else:
            worker.active_progress = progress

        track_text = "Starting…"

        if (
            worker.active_track_no
            and worker.active_title
        ):
            track_text = (
                f"[{worker.active_track_no}"
                f"/{len(worker.tracks)}] "
                f"{worker.active_artist} — "
                f"{worker.active_title}"
            )

        if track_no and total:
            track_text = (
                f"[{track_no}/{total}] "
                f"{worker.active_artist} — "
                f"{worker.active_title}"
            )

        line = (
            f"♪ {account_name} · {quality_label(quality)} · "
            f"{track_text} · {progress}%"
        )

        if counts:
            line += (
                f"  D:{counts['downloaded']} "
                f"S:{counts['skipped']} "
                f"F:{counts['failed']} "
                f"U:{counts['unavailable']}"
            )

        if existing is None:
            container.mount(
                Label(
                    line,
                    id=f"session-{slug}",
                    classes="session-row",
                )
            )
        else:
            existing.update(
                line
            )

    def remove_session_row(
        self,
        slug: str,
    ) -> None:
        container = self.query_one(
            "#active-sessions",
            Vertical,
        )

        existing = container.query_one_optional(
            f"#session-{slug}",
            Label,
        )

        if existing is not None:
            existing.remove()

    # ------------------------------------------------------------------
    # Library status
    # ------------------------------------------------------------------

    def action_status(self) -> None:
        if not self.selected_account:
            self.notify(
                "Select an account first.",
                severity="warning",
            )
            return

        tracks = (
            self.current_tracks
            or inventory_current_tracks(
                self.selected_account
            )
        )

        if not tracks:
            self.notify(
                (
                    "No liked tracks recorded yet. "
                    "Refresh liked music first."
                ),
                severity="warning",
            )
            return

        quality = self.current_quality

        try:
            status_path = status_file(
                self.selected_account
            )

            if status_path.exists():
                snapshot = json.loads(
                    status_path.read_text()
                )
                quality = snapshot.get(
                    "quality",
                    quality,
                )

            report = scan_inventory(
                self.selected_account,
                tracks,
                quality,
            )

            self.push_screen(
                LibraryStatusScreen(
                    self.selected_account,
                    quality,
                    report,
                )
            )

        except Exception as error:
            self.notify(
                str(error),
                severity="error",
                timeout=10,
            )

    # ------------------------------------------------------------------
    # Cancel
    # ------------------------------------------------------------------

    def action_cancel(self) -> None:
        if not self.download_workers:
            self.notify(
                "No download is running.",
                severity="information",
            )
            return

        self.push_screen(
            CancelSessionScreen(
                list(
                    self.download_workers.values()
                )
            ),
            self.cancel_selected,
        )

    def cancel_selected(
        self,
        action: dict[str, Any] | None,
    ) -> None:
        if action is None:
            return

        if action["kind"] == "all":
            for worker in list(
                self.download_workers.values()
            ):
                worker.cancel()

            self.query_one(
                "#status",
                Label,
            ).update(
                "Cancelling all downloads after "
                "the current processes stop..."
            )
            return

        worker = action["worker"]

        if action["kind"] == "stage":
            worker.stop_after_stage()

            self.query_one(
                "#status",
                Label,
            ).update(
                (
                    f"Stopping {worker.account['name']} after "
                    "the current stage..."
                )
            )

        elif action["kind"] == "account":
            worker.cancel()

            self.query_one(
                "#status",
                Label,
            ).update(
                (
                    f"Cancelling {worker.account['name']} after "
                    "the current process stops..."
                )
            )

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------

    def on_button_pressed(
        self,
        event: Button.Pressed,
    ) -> None:
        button = event.button.id

        if button == "add-account":
            self.action_add_account()

        elif button in {"settings", "settings-main"}:
            self.action_settings()

        elif button == "remove-account":
            self.action_delete_account()

        elif button == "download":
            self.action_download()

        elif button == "most-played":
            self.action_most_played()

        elif button == "duplicates":
            self.action_duplicates()

        elif button == "status-button":
            self.action_status()

        elif button == "cancel":
            self.action_cancel()


# ============================================================================
# Main
# ============================================================================

def print_help() -> None:
    print(f"ytm-ll-dl-modern.py v{APP_VERSION}  (build {APP_BUILD})")
    print()
    print("Multi-account YouTube Music liked-music downloader (TUI + web UI).")
    print()
    print("Usage:")
    print("    ./run.sh                 Start the TUI")
    print("    ./run.sh --tui           Start the TUI")
    print("    ./run.sh --web           Start the web UI and open a browser")
    print("    ./run.sh --version       Print version info")
    print("    ./run.sh --help          Show this help")
    print("    ./run.sh serve           Start the local Spotify-style web UI")
    print()
    print("Serve options:")
    print("    --lan          Share on your network (play on your phone/other device)")
    print("    --host HOST    Bind address (default 127.0.0.1)")
    print("    --port PORT    Port (default 8765)")
    print("    --token TOKEN  Require this token for LAN/remote access")
    print("    --open         Open the web UI in your default browser")
    print()
    print("Play on another device (same Wi-Fi):")
    print("    ./run.sh serve --lan")
    print("    Then open the printed 'Network' URL on your phone.")
    print()
    print("First-time setup:")
    print("    ./install.sh             Create ./venv and install everything")
    print()
    print("Files:")
    print("    config/settings.json     Persistent settings (created on first run)")
    print("    accounts/<slug>/         Per-account data and authentication")
    print("    files/<slug>/            Downloaded music and per-track inventory")
    print()
    if IS_FROZEN:
        print("Standalone build data directory:")
        print(f"    {DATA_DIR}")
        print()


def start_web_ui(
    host: str,
    port: int,
    token: str | None,
    open_browser: bool,
) -> None:
    from web.server import run_server

    if host not in {"127.0.0.1", "localhost", "::1"} and not token:
        print(
            "Binding to a non-loopback address requires --token. "
            "Refusing to start."
        )
        sys.exit(1)

    try:
        run_server(
            sys.modules[__name__],
            host=host,
            port=port,
            token=token,
            open_browser=open_browser,
        )
    except OSError as error:
        # A double-clicked launcher must not dump a traceback when
        # another instance already holds the port.
        if error.errno in {98, 48, 10048}:  # EADDRINUSE per platform
            shown = "127.0.0.1" if host in {"", "0.0.0.0", "::"} else host
            print()
            print(f"Port {port} is already in use.")
            print()
            print("The web UI may already be running — open this address:")
            print(f"    http://{shown}:{port}/")
            print()
            print("Or start on a different port:  ./run.sh serve --lan --port 8766")
            sys.exit(1)
        raise


def parse_serve_args(
    args: list[str],
) -> tuple[str, int, str | None, bool]:
    """
    Extract --host/--port/--token/--open from a serve argument list.
    """
    host = "127.0.0.1"
    port = 8765
    token = None
    open_browser = False

    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--host" and index + 1 < len(args):
            host = args[index + 1]
            index += 2
        elif arg in {"--lan", "--network"}:
            host = "0.0.0.0"
            index += 1
        elif arg == "--port" and index + 1 < len(args):
            try:
                port = int(args[index + 1])
            except ValueError:
                print(f"Invalid port: {args[index + 1]}")
                sys.exit(1)
            index += 2
        elif arg == "--token" and index + 1 < len(args):
            token = args[index + 1]
            index += 2
        elif arg == "--open":
            open_browser = True
            index += 1
        else:
            index += 1

    # Sharing on the network is only allowed with a token. When the user
    # asks for a non-loopback bind but gives no token, generate one so the
    # common "serve --lan" case just works (printed as part of the URL).
    if host not in {"127.0.0.1", "localhost", "::1"} and not token:
        token = secrets.token_urlsafe(12)

    return host, port, token, open_browser


def has_interactive_console() -> bool:
    """
    True when both stdin and stdout are a real terminal. A frozen
    binary double-clicked from a file manager has no console.
    """
    try:
        return bool(
            sys.stdin is not None
            and sys.stdin.isatty()
            and sys.stdout is not None
            and sys.stdout.isatty()
        )
    except Exception:
        return False


def launch_menu() -> str:
    """
    Simple chooser shown by standalone builds when launched from a
    terminal. Returns "tui" or "web".
    """
    print()
    print(f"  YT Music Downloader v{APP_VERSION}")
    print()
    print("  [1] Terminal UI (TUI)")
    print("  [2] Web UI (opens in your browser)")
    print("  [3] Exit")
    print()

    while True:
        try:
            choice = input("  Selection [1/2/3]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return "web"

        if choice in {"1", "tui"}:
            return "tui"

        if choice in {"2", "web"}:
            return "web"

        if choice in {"3", "q", "quit", "exit"}:
            return "quit"

        print("  Please enter 1, 2 or 3.")


def start_tui() -> None:
    ensure_directories()

    app = YTMusicDownloader()
    app.run()


def main() -> None:
    args = sys.argv[1:]

    if any(arg in {"--version", "-V"} for arg in args):
        print(f"ytm-ll-dl-modern.py v{APP_VERSION}")
        print(f"Build: {APP_BUILD}")
        return

    if any(arg in {"--help", "-h"} for arg in args):
        print_help()
        return

    if args and args[0] == "serve":
        host, port, token, open_browser = parse_serve_args(args[1:])
        start_web_ui(host, port, token, open_browser)
        return

    if "--web" in args:
        host, port, token, open_browser = parse_serve_args(args)
        # --web implies opening a browser unless --host says otherwise
        # on a LAN address (a wildcard bind still opens the local URL).
        start_web_ui(host, port, token, open_browser or host in {
            "127.0.0.1", "localhost", "::1", "0.0.0.0", "::",
        })
        return

    if "--tui" in args:
        start_tui()
        return

    # No arguments.
    if IS_FROZEN:
        ensure_directories()

        if not has_interactive_console():
            # Double-clicked with no terminal: the web UI is the only
            # surface that works without one, so start it and open the
            # user's browser.
            print(f"YT Music Downloader v{APP_VERSION}")
            print()
            print("No terminal detected — starting the web UI.")
            print(f"Data directory: {DATA_DIR}")
            print()
            start_web_ui("127.0.0.1", 8765, None, True)
            return

        mode = launch_menu()

        if mode == "quit":
            return

        if mode == "web":
            start_web_ui("127.0.0.1", 8765, None, True)
            return

        start_tui()
        return

    # Source runs keep the historical default: straight into the TUI.
    start_tui()


if __name__ == "__main__":
    main()