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
import json
import platform
import re
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

APP_VERSION = "3.11.0"
APP_BUILD = "2026-08-13-github-release-prep"


PROJECT_DIR = Path(__file__).resolve().parent
VENV_DIR = PROJECT_DIR / "venv"
VENV_BIN = VENV_DIR / "bin"
VENV_PYTHON = VENV_BIN / "python"


def require_virtualenv() -> bool:
    """
    This application only runs from inside a virtualenv. Returns
    True when a virtualenv is active, otherwise prints setup
    instructions and returns False.
    """
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
    Verify the virtualenv runtime: required Python packages must be
    importable and the project-local Deno binary must exist. Deno is
    downloaded automatically when missing; missing Python packages
    produce clear install instructions instead of an ImportError.
    """
    required = (
        "textual",
        "ytmusicapi",
        "yt_dlp",
        "imageio_ffmpeg",
    )

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
    deno_path = bin_dir / "deno"

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

    with tempfile.TemporaryDirectory(
        dir=str(PROJECT_DIR)
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

ACCOUNTS_DIR = PROJECT_DIR / "accounts"
FILES_DIR = PROJECT_DIR / "files"
CONFIG_DIR = PROJECT_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "settings.json"

DEFAULT_SETTINGS = {
    "default_quality": "best",
    "default_existing_policy": "skip",
    "default_session": "loop",
    "default_limit": 25,
    "retry_attempts": 3,
    "retry_delay": 3,
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


def create_account(
    name: str,
    headers_raw: str,
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

        ytm = YTMusic(
            str(auth_file)
        )

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

    except Exception:
        shutil.rmtree(
            directory,
            ignore_errors=True,
        )
        raise


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

    ytm = YTMusic(
        str(account["auth"])
    )

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

    ytm = YTMusic(
        str(account["auth"])
    )

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
        record["currently_liked"] = True

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
            failed.append(
                {
                    "video_id": video_id,
                    "track": track,
                    "reason": quality_record.get(
                        "reason",
                        "Previous download failed.",
                    ),
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
        "liked_total": len(current_map),
        "downloaded_total": len(liked_downloaded),
        "not_downloaded_total": len(not_downloaded),
        "missing_total": len(missing_files),
        "no_longer_liked_total": len(
            no_longer_liked
        ),
        "failed_total": len(failed),
        "skipped_total": len(skipped),
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

    ytm = YTMusic(
        str(account["auth"])
    )

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


class DownloadWorker:
    def __init__(
        self,
        account: dict[str, Any],
        tracks: list[dict[str, Any]],
        sessions: list[DownloadSession],
        settings: dict[str, Any],
        callback,
        output_dir: Path | None = None,
    ):
        self.account = account
        self.tracks = tracks
        self.sessions = sessions
        self.settings = settings
        self.callback = callback
        self.output_dir = output_dir

        self.cancelled = False
        self._stop = False

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
        self.emit(
            "counts",
            downloaded=self.downloaded,
            skipped=self.skipped,
            failed=self.failed,
            unavailable=self.unavailable,
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
    ) -> None:
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

        qualities = record.setdefault(
            "qualities",
            {},
        )

        qualities[quality] = {
            "status": status,
            "reason": reason,
            "file": file,
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
        )

        total = len(self.tracks)

        for number, track in enumerate(
            self.tracks,
            1,
        ):
            if self.cancelled:
                break

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
                continue

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
                    continue

                # Replace mode.
                try:
                    destination.unlink()
                except Exception as error:
                    reason = (
                        "Existing file could not be replaced: "
                        f"{error}"
                    )

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
                    continue

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
                VENV_BIN / "yt-dlp"
            )

            deno = (
                VENV_BIN / "deno"
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

                url,
            ]

            if config["audio_quality"] is not None:
                command.extend(
                    [
                        "--audio-quality",
                        config["audio_quality"],
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
                    )

                    assert process.stdout is not None

                    for line in iter(
                        process.stdout.readline,
                        "",
                    ):
                        if self.cancelled:
                            process.terminate()
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

                        if (
                            "ERROR:" in line
                            or "HTTP Error" in line
                            or "has been removed" in line
                            or "not available" in line
                            or "Sign in" in line
                        ):
                            last_error = line

                        self.emit(
                            "log",
                            text=line,
                        )

                    return_code = process.wait()

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
                break

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
                    if last_error
                    else (
                        "yt-dlp failed to produce "
                        "the requested audio."
                    )
                )

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

                for temporary in output.glob(
                    f".ytm_{video_id}*"
                ):
                    temporary.unlink(
                        missing_ok=True
                    )

                self.counts()
                continue

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
                continue

            # Record a verified download.
            self.downloaded += 1

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

            save_inventory(
                self.account,
                inventory,
            )

        save_inventory(
            self.account,
            inventory,
        )


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
        yield Container(
            Label(
                "ADD YOUTUBE MUSIC ACCOUNT",
                id="title",
            ),

            Static(
                "1. Log into the desired YouTube Music account.\n"
                "2. Open YouTube Music in Firefox/Chrome.\n"
                "3. Open Developer Tools → Network.\n"
                "4. Find a POST request to /browse with status 200.\n"
                "5. Right-click → Copy → Copy request headers.\n"
                "6. Paste the complete request headers below.\n\n"
                "The headers are stored locally as a protected "
                ".headers_auth.json file.",
                id="instructions",
            ),

            Input(
                placeholder="Account name, e.g. Shay Mercer",
                id="name",
            ),

            TextArea(
                "",
                placeholder="Paste request headers here...",
                id="headers",
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
        settings = self.app.settings
        settings["default_quality"] = session.quality
        settings["default_existing_policy"] = session.existing_policy
        settings["default_session"] = session.session
        save_settings(settings)

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
            f"Previously skipped: {r['skipped_total']}"
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

        settings = {
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
            "yt_dlp": VENV_BIN / "yt-dlp",
            "deno": VENV_BIN / "deno",
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
                ),
                severity="information",
                timeout=12,
            )

    # ------------------------------------------------------------------
    # Active sessions display
    # ------------------------------------------------------------------

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

        elif button == "refresh":
            self.action_refresh()

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
    print("Multi-account YouTube Music liked-music downloader (TUI).")
    print()
    print("Usage:")
    print("    ./run.sh                 Start the TUI")
    print("    ./run.sh --version       Print version info")
    print("    ./run.sh --help          Show this help")
    print()
    print("First-time setup:")
    print("    ./install.sh             Create ./venv and install everything")
    print()
    print("Files:")
    print("    config/settings.json     Persistent settings (created on first run)")
    print("    accounts/<slug>/         Per-account data and authentication")
    print("    files/<slug>/            Downloaded music and per-track inventory")
    print()


def main() -> None:
    if any(arg in {"--version", "-V"} for arg in sys.argv[1:]):
        print(f"ytm-ll-dl-modern.py v{APP_VERSION}")
        print(f"Build: {APP_BUILD}")
        return

    if any(arg in {"--help", "-h"} for arg in sys.argv[1:]):
        print_help()
        return

    ensure_directories()

    app = YTMusicDownloader()
    app.run()


if __name__ == "__main__":
    main()