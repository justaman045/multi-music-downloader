"""
Headless tests for ytm-ll-dl-modern.py.

Run from inside the project venv:

    venv/bin/pip install -r requirements-dev.txt
    venv/bin/python -m pytest tests/ -q

These tests never touch the network or the real accounts/ and files/
directories. Path constants are redirected to a per-test tmp dir.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest


APP_PATH = Path(__file__).resolve().parent.parent / "ytm-ll-dl-modern.py"


def load_app() -> object:
    """Import the app as a module (registered in sys.modules so the
    @dataclass decorator works) and return it."""
    spec = importlib.util.spec_from_file_location(
        "ytm_ll_dl_app",
        str(APP_PATH),
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["ytm_ll_dl_app"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def app():
    module = load_app()
    assert module.require_virtualenv(), (
        "Tests must run from inside the project virtualenv"
    )
    return module


@pytest.fixture
def isolated_paths(app, tmp_path):
    """Redirect the app's project-local path constants to a tmp dir so
    tests never touch real config/accounts/files data."""
    import types

    app.PROJECT_DIR = tmp_path
    app.ACCOUNTS_DIR = tmp_path / "accounts"
    app.FILES_DIR = tmp_path / "files"
    app.CONFIG_DIR = tmp_path / "config"
    app.CONFIG_FILE = tmp_path / "config" / "settings.json"
    return types.SimpleNamespace(
        project=tmp_path,
        accounts=tmp_path / "accounts",
        files=tmp_path / "files",
        config=tmp_path / "config",
        settings_file=tmp_path / "config" / "settings.json",
    )


def make_account(app, slug: str, base: Path) -> dict:
    """Build the account dict shape the app expects (output dir etc.)."""
    account_dir = base / "accounts" / slug
    output = base / "files" / slug
    output.mkdir(parents=True, exist_ok=True)
    (account_dir / ".headers_auth.json").parent.mkdir(parents=True, exist_ok=True)
    (account_dir / ".headers_auth.json").write_text("{}", encoding="utf-8")
    (account_dir / "account.json").write_text(
        json.dumps({"name": slug, "slug": slug}),
        encoding="utf-8",
    )
    return {
        "slug": slug,
        "name": slug,
        "auth": account_dir / ".headers_auth.json",
        "output": output,
    }


def write_inventory(account: dict, inventory: dict) -> None:
    app = sys.modules["ytm_ll_dl_app"]
    app.save_inventory(account, inventory)


# ---------------------------------------------------------------------------
# Module + guard
# ---------------------------------------------------------------------------

def test_version_format(app):
    assert re.fullmatch(r"\d+\.\d+\.\d+", app.APP_VERSION)
    assert app.APP_BUILD


def test_requires_virtualenv(app):
    assert app.require_virtualenv() is True


def test_quality_options(app):
    assert len(app.QUALITY_OPTIONS) == 8
    values = [value for _label, value in app.QUALITY_OPTIONS]
    assert values == [
        "best",
        "mp3_320",
        "mp3_v0",
        "mp3_256",
        "mp3_192",
        "mp3_128",
        "flac",
        "m4a_256",
    ]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def test_default_settings(app):
    assert app.DEFAULT_SETTINGS["default_quality"] == "best"
    assert app.DEFAULT_SETTINGS["default_existing_policy"] == "skip"


def test_settings_roundtrip(app, isolated_paths):
    app.save_settings({"default_quality": "mp3_320"})
    loaded = app.load_settings()
    assert loaded["default_quality"] == "mp3_320"
    # Missing keys fall back to defaults.
    assert loaded["default_existing_policy"] == "skip"
    assert loaded["retry_attempts"] == 3


def test_settings_corrupt_falls_back_to_defaults(app, isolated_paths):
    isolated_paths.settings_file.parent.mkdir(parents=True, exist_ok=True)
    isolated_paths.settings_file.write_text("{not json", encoding="utf-8")
    loaded = app.load_settings()
    assert loaded == app.DEFAULT_SETTINGS


def test_settings_missing_file_creates_defaults(app, isolated_paths):
    loaded = app.load_settings()
    assert isolated_paths.settings_file.exists()
    assert loaded == app.DEFAULT_SETTINGS


# ---------------------------------------------------------------------------
# String helpers
# ---------------------------------------------------------------------------

def test_slugify(app):
    assert app.account_slug("Shay Mercer") == "shay_mercer"
    assert app.account_slug('A: B / C\\D*E?F"G') == "a_b_c_d_e_f_g"
    assert app.account_slug("   ") == "account"


def test_safe_filename(app):
    assert app.safe_filename('Rock: The "Best" / Of? 2026') == (
        "Rock_ The _Best_ _ Of_ 2026"
    )
    assert app.safe_filename("...") == "unknown"


def test_deduplicate_tracks(app):
    tracks = [
        {"videoId": "a", "title": "One"},
        {"videoId": "b", "title": "Two"},
        {"videoId": "a", "title": "Duplicate"},
        {"videoId": None, "title": "Ignored"},
    ]
    result = app.deduplicate_tracks(tracks)
    assert [t["videoId"] for t in result] == ["a", "b"]


def test_get_artist_and_album(app):
    track = {
        "title": "Song",
        "artists": [{"name": "A"}, {"name": "B"}],
        "album": {"name": "LP"},
    }
    assert app.get_artist(track) == "A, B"
    assert app.get_album(track) == "LP"
    assert app.track_title(track) == "Song"
    assert app.get_artist({}) == "Unknown Artist"


# ---------------------------------------------------------------------------
# Filenames + quality dirs
# ---------------------------------------------------------------------------

def test_track_filename_extensions(app):
    track = {"title": "Song", "artists": [{"name": "Artist"}]}
    assert app.track_filename(track, "mp3_320") == "Artist, Song.mp3"
    assert app.track_filename(track, "flac") == "Artist, Song.flac"
    assert app.track_filename(track, "m4a_256") == "Artist, Song.m4a"
    assert app.track_filename(track, "best") == "Artist, Song.m4a"


def test_names_for_quality(app):
    assert app.names_for_quality("mp3_320") == "MP3_320kbps"
    assert app.names_for_quality("best") == "Best_Source"
    assert app.names_for_quality("nope") == "nope"


def test_quality_labels(app):
    assert app.quality_short_label("flac") == "FLAC"
    assert app.quality_short_label("mp3_192") == "MP3 192"


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

def test_inventory_default_empty(app, isolated_paths):
    account = make_account(app, "a1", isolated_paths.project)
    inventory = app.load_inventory(account)
    assert inventory["version"] == 2
    assert inventory["tracks"] == {}


def test_inventory_roundtrip(app, isolated_paths):
    account = make_account(app, "a1", isolated_paths.project)
    data = {
        "version": 2,
        "last_library_sync": 123,
        "liked_count": 1,
        "tracks": {
            "vid1": {
                "video_id": "vid1",
                "qualities": {
                    "mp3_320": {
                        "status": "downloaded",
                        "reason": None,
                        "file": "/tmp/x.mp3",
                    },
                },
                "title": "Song",
                "artist": "Artist",
                "album": "LP",
                "last_seen_liked": 123,
                "currently_liked": True,
            },
        },
    }
    write_inventory(account, data)
    loaded = app.load_inventory(account)
    assert loaded == data
    assert loaded["tracks"]["vid1"]["qualities"]["mp3_320"]["status"] == "downloaded"


def test_inventory_corrupt_returns_empty(app, isolated_paths):
    account = make_account(app, "a1", isolated_paths.project)
    app.inventory_file(account).write_text("[", encoding="utf-8")
    inventory = app.load_inventory(account)
    assert inventory == {"version": 2, "tracks": {}}


def test_build_sessions_skip_expands(app):
    session = app.DownloadSession(
        quality="mp3_320",
        existing_policy="skip",
        session="skip",
    )
    sessions = app.build_sessions(session)
    assert len(sessions) == 8
    for built in sessions:
        assert built.existing_policy == "skip"
        assert built.session == "once"


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------

def test_normalized_title_artist(app):
    assert app._normalized_title_artist("  Hello   World ", " Artist ") == (
        "hello world",
        "artist",
    )


def test_find_cross_account_duplicates(app, isolated_paths):
    base = isolated_paths.project
    acc_a = make_account(app, "aaa", base)
    acc_b = make_account(app, "bbb", base)

    shared_vid = "shared_video_1"
    inv_a = {
        "version": 2,
        "tracks": {
            shared_vid: {
                "video_id": shared_vid,
                "qualities": {},
                "title": "Same Upload",
                "artist": "Artist",
                "album": "LP",
                "last_seen_liked": 1,
                "currently_liked": True,
            },
            "unique_a": {
                "video_id": "unique_a",
                "qualities": {},
                "title": "Different Upload",
                "artist": "Artist",
                "album": "LP",
                "last_seen_liked": 1,
                "currently_liked": True,
            },
            "unliked_a": {
                "video_id": "unliked_a",
                "qualities": {},
                "title": "Not Liked",
                "artist": "Artist",
                "album": "LP",
                "last_seen_liked": 1,
                "currently_liked": False,
            },
        },
    }
    inv_b = {
        "version": 2,
        "tracks": {
            shared_vid: {
                "video_id": shared_vid,
                "qualities": {},
                "title": "Same Upload",
                "artist": "Artist",
                "album": "LP",
                "last_seen_liked": 1,
                "currently_liked": True,
            },
            "unique_b": {
                "video_id": "unique_b",
                "qualities": {},
                "title": "different upload",
                "artist": "artist",
                "album": "LP",
                "last_seen_liked": 1,
                "currently_liked": True,
            },
        },
    }
    write_inventory(acc_a, inv_a)
    write_inventory(acc_b, inv_b)

    report = app.find_cross_account_duplicates([acc_a, acc_b])

    exact_ids = [entry["video_id"] for entry in report["exact"]]
    assert shared_vid in exact_ids
    assert "unique_a" not in exact_ids
    assert "unliked_a" not in exact_ids

    by_title = report["by_title"]
    assert any(
        entry["title"].casefold() == "different upload"
        and len(entry["accounts"]) >= 2
        for entry in by_title
    )
