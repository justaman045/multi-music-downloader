"""
Headless TUI tests using Textual's run_test pilot.

Plain-pytest friendly: each test wraps the async pilot scenario in
``asyncio.run`` so no pytest-asyncio plugin is required.

Run from inside the project venv:

    venv/bin/python -m pytest tests/test_tui.py -q
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from textual.widgets import Button, ListView


APP_PATH = Path(__file__).resolve().parent.parent / "ytm-ll-dl-modern.py"


def load_app() -> object:
    spec = importlib.util.spec_from_file_location(
        "ytm_ll_dl_app_tui",
        str(APP_PATH),
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["ytm_ll_dl_app_tui"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def app():
    return load_app()


@pytest.fixture
def isolated(app, tmp_path):
    """Redirect project-local paths so the TUI never touches real data."""
    app.PROJECT_DIR = tmp_path
    app.ACCOUNTS_DIR = tmp_path / "accounts"
    app.FILES_DIR = tmp_path / "files"
    app.CONFIG_DIR = tmp_path / "config"
    app.CONFIG_FILE = tmp_path / "config" / "settings.json"
    return app


def make_account(app, slug: str, base: Path) -> dict:
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


# ---------------------------------------------------------------------------
# Boot / smoke
# ---------------------------------------------------------------------------

def test_tui_boots_with_empty_state(isolated):
    app = isolated.YTMusicDownloader()

    async def scenario() -> None:
        async with app.run_test() as pilot:
            assert app.accounts == []
            status = app.query_one("#status")
            assert "ready" in str(status.content).lower()
            await pilot.press("q")

    asyncio.run(scenario())


def test_tui_lists_accounts_from_disk(isolated):
    make_account(isolated, "alpha", isolated.PROJECT_DIR)

    app = isolated.YTMusicDownloader()

    async def scenario() -> None:
        async with app.run_test() as pilot:
            assert len(app.accounts) == 1
            assert app.accounts[0]["slug"] == "alpha"

            account_list = app.query_one("#accounts-list", ListView)
            assert len(account_list.children) == 1

            account_list.index = 0
            account_list.action_select_cursor()
            await pilot.pause()
            name_label = app.query_one("#account-name")
            assert "alpha" in str(name_label.content).lower()
            await pilot.press("q")

    asyncio.run(scenario())


def test_tui_settings_screen_opens(isolated):
    app = isolated.YTMusicDownloader()

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await pilot.press("p")
            await pilot.pause()
            assert len(app.screen_stack) == 2
            app.screen_stack[-1].query_one(
                "#cancel-settings",
                Button,
            ).press()
            await pilot.pause()
            assert len(app.screen_stack) == 1
            await pilot.press("q")

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Library Status actions
# ---------------------------------------------------------------------------

def test_repair_pending_tracks_filters_healthy(isolated):
    account = make_account(isolated, "repair", isolated.PROJECT_DIR)
    healthy = account["output"] / "MP3_320kbps" / "Artist - Healthy.mp3"
    healthy.parent.mkdir(parents=True, exist_ok=True)
    healthy.write_bytes(b"\x00" * 100)

    isolated.save_inventory(
        account,
        {
            "tracks": {
                "vid_healthy": {
                    "title": "Healthy",
                    "artist": "Artist",
                    "currently_liked": True,
                    "qualities": {
                        "mp3_320": {
                            "status": "downloaded",
                            "file": str(healthy),
                        }
                    },
                },
                "vid_failed": {
                    "title": "Broken",
                    "artist": "Artist",
                    "currently_liked": True,
                    "qualities": {
                        "mp3_320": {
                            "status": "failed",
                            "reason": "transient",
                        }
                    },
                },
                "vid_fresh": {
                    "title": "Fresh",
                    "artist": "Artist",
                    "currently_liked": True,
                    "qualities": {},
                },
                "vid_unliked": {
                    "title": "Gone",
                    "artist": "Artist",
                    "currently_liked": False,
                    "qualities": {},
                },
            }
        },
    )

    pending = isolated.repair_pending_tracks(
        account,
        "mp3_320",
    )

    assert {t["videoId"] for t in pending} == {
        "vid_failed",
        "vid_fresh",
    }


def test_unliked_preview_and_purge(isolated):
    account = make_account(isolated, "purge", isolated.PROJECT_DIR)
    junk = account["output"] / "FLAC" / "Song.flac"
    junk.parent.mkdir(parents=True, exist_ok=True)
    junk.write_bytes(b"\x00" * 1024)

    isolated.save_inventory(
        account,
        {
            "tracks": {
                "vid_gone": {
                    "title": "Song",
                    "artist": "Artist",
                    "currently_liked": False,
                    "qualities": {
                        "flac": {
                            "status": "downloaded",
                            "file": str(junk),
                        }
                    },
                },
                "vid_liked": {
                    "title": "Keep",
                    "artist": "Artist",
                    "currently_liked": True,
                    "qualities": {},
                },
            }
        },
    )

    preview = isolated.unliked_inventory_records(account)

    assert len(preview) == 1
    assert preview[0]["video_id"] == "vid_gone"
    assert preview[0]["size_bytes"] == 1024

    result = isolated.purge_unliked_records(
        account,
        ["vid_gone"],
    )

    assert result["freed_bytes"] == 1024
    assert not junk.exists()

    inventory = isolated.load_inventory(account)
    assert "vid_gone" not in inventory["tracks"]
    assert "vid_liked" in inventory["tracks"]


# ---------------------------------------------------------------------------
# Download worker events
# ---------------------------------------------------------------------------

def test_worker_finished_carries_results(isolated):
    account = make_account(isolated, "worker", isolated.PROJECT_DIR)
    worker = isolated.DownloadWorker(
        account=account,
        tracks=[
            {
                "title": "No Video ID",
                "artists": [{"name": "Someone"}],
            }
        ],
        sessions=[
            isolated.DownloadSession(
                quality="mp3_320",
                existing_policy="skip",
                session="once",
            )
        ],
        settings={"retry_attempts": 1, "retry_delay": 0},
        callback=None,
    )

    finished: dict = {}

    def capture(event: str, data: dict) -> None:
        if event == "finished":
            finished.update(data)

    worker.callback = capture
    worker.run()

    assert finished["unavailable"] == 1
    assert finished["failed"] == 0
    assert len(finished["results"]) == 1
    assert finished["results"][0]["status"] == "unavailable"
    assert finished["results"][0]["quality"] == "mp3_320"


def test_failed_titles_summary(isolated):
    app = isolated.YTMusicDownloader()

    async def scenario() -> None:
        async with app.run_test() as pilot:
            summary = app._failed_titles_summary(
                [
                    {"status": "failed", "artist": "A", "title": "T1"},
                    {"status": "failed", "artist": "B", "title": "T2"},
                    {"status": "skipped", "artist": "C", "title": "T3"},
                ]
            )
            assert "A — T1" in summary
            assert "B — T2" in summary
            assert "C — T3" not in summary

            assert app._failed_titles_summary([]) == ""
            await pilot.press("q")

    asyncio.run(scenario())


def test_settings_save_preserves_other_keys(isolated, tmp_path):
    """Regression: saving the TUI settings modal must merge into the
    settings on disk, not replace them — the modal edits six keys, and a
    bare overwrite wiped the other persisted keys (last.fm, automation,
    rate limit, ...)."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "settings.json"
    seeded = {
        "default_quality": "best",
        "lastfm_enabled": True,
        "lastfm_api_key": "keep-me",
        "auto_refresh_interval_hours": 6,
        "download_rate_limit": "2M",
        "notifications_enabled": False,
    }
    config_file.write_text(json.dumps(seeded), encoding="utf-8")

    app = isolated.YTMusicDownloader()

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await pilot.press("p")
            await pilot.pause()
            app.screen_stack[-1].query_one(
                "#save-settings",
                Button,
            ).press()
            await pilot.pause()
            assert len(app.screen_stack) == 1
            await pilot.press("q")

    asyncio.run(scenario())

    saved = json.loads(config_file.read_text(encoding="utf-8"))
    # Untouched keys survive the save.
    assert saved["lastfm_api_key"] == "keep-me"
    assert saved["lastfm_enabled"] is True
    assert saved["auto_refresh_interval_hours"] == 6
    assert saved["download_rate_limit"] == "2M"
    assert saved["notifications_enabled"] is False
    # And the modal's own keys are present.
    for key in (
        "default_quality",
        "default_existing_policy",
        "default_session",
        "default_limit",
        "retry_attempts",
        "retry_delay",
    ):
        assert key in saved


# ---------------------------------------------------------------------------
# Listening stats (Play Stats — parity with the web insights view)
# ---------------------------------------------------------------------------

def _seed_plays(app_module, slug: str, base: Path, plays: dict) -> None:
    """Write files/<slug>/.plays.json directly (the web player's data)."""
    out = base / "files" / slug
    out.mkdir(parents=True, exist_ok=True)
    (out / ".plays.json").write_text(
        json.dumps(
            {
                "version": 1,
                "plays": plays,
                "recent": [],
                "updated_at": 1700000000,
            }
        ),
        encoding="utf-8",
    )


def test_compute_play_stats_counts(isolated):
    """compute_play_stats() must aggregate .plays.json without any web
    server running — same source the web insights view uses."""
    make_account(isolated, "alpha", isolated.PROJECT_DIR)
    _seed_plays(
        isolated,
        "alpha",
        isolated.PROJECT_DIR,
        {
            "AbC123xyz_9": {
                "video_id": "AbC123xyz_9",
                "count": 3,
                "first_played": 1700000000,
                "last_played": 1700000100,
            },
            "XyZ777abc_8": {
                "video_id": "XyZ777abc_8",
                "count": 2,
                "first_played": 1700000200,
                "last_played": 1700000300,
            },
        },
    )

    stats, history = isolated.compute_play_stats()
    assert stats["total_plays"] == 5
    assert stats["unique_tracks"] == 2
    assert stats["per_account"]["alpha"] == 5
    assert isinstance(history["streaks"], dict)
    assert "current" in history["streaks"]
    assert "this" in history["week"]

    # The screen must render without raising and show the totals.
    screen = isolated.PlayStatsScreen.__new__(isolated.PlayStatsScreen)
    screen.stats = stats
    screen.history = history
    assert "Total plays: 5" in screen.render_summary()
    content = screen.render_content()
    assert "alpha" in content
    assert "TOP TRACKS" in content


def test_tui_play_stats_screen_opens(isolated):
    """The Play Stats binding (h) must open the modal, show local data,
    and close cleanly."""
    make_account(isolated, "alpha", isolated.PROJECT_DIR)
    _seed_plays(
        isolated,
        "alpha",
        isolated.PROJECT_DIR,
        {
            "AbC123xyz_9": {
                "video_id": "AbC123xyz_9",
                "count": 4,
                "first_played": 1700000000,
                "last_played": 1700000100,
            },
        },
    )
    app = isolated.YTMusicDownloader()

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await pilot.press("h")
            await pilot.pause()
            assert len(app.screen_stack) == 2
            screen = app.screen_stack[-1]
            assert "Total plays: 4" in str(screen.query_one("#summary").content)
            screen.query_one("#close", Button).press()
            await pilot.pause()
            assert len(app.screen_stack) == 1
            await pilot.press("q")

    asyncio.run(scenario())


def test_tui_play_stats_binding_registered(app):
    """Regression guard: the binding must stay in the main screen's
    BINDINGS or the h key silently stops working."""
    bindings = dict(
        (key, action)
        for key, action, _label in app.YTMusicDownloader.BINDINGS
    )
    assert bindings.get("h") == "play_stats"
    assert hasattr(app.YTMusicDownloader, "action_play_stats")
    assert hasattr(app, "PlayStatsScreen")
    assert hasattr(app, "compute_play_stats")


# ---------------------------------------------------------------------------
# Add-account screen: browser import (no OAuth)
# ---------------------------------------------------------------------------


def test_tui_add_account_screen_offers_browser_import(isolated, monkeypatch):
    """The add-account modal must show the browser picker + import
    button and no Google OAuth button (OAuth library reads are blocked
    by YouTube; browser-cookie import is the automatic path)."""
    from textual.widgets import Select

    # Pretend one browser is installed so the picker preselects it.
    monkeypatch.setattr(
        isolated,
        "list_importable_browsers",
        lambda: [
            {"id": "chrome", "label": "Google Chrome", "available": True},
            {"id": "firefox", "label": "Firefox", "available": False},
        ],
    )

    app = isolated.YTMusicDownloader()

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await pilot.press("a")
            await pilot.pause()
            assert len(app.screen_stack) == 2
            screen = app.screen_stack[-1]

            browser_select = screen.query_one("#browser", Select)
            assert browser_select.value == "chrome"

            profile_select = screen.query_one("#profile", Select)
            assert profile_select.value == "auto"

            button_ids = {b.id for b in screen.query("Button")}
            assert "add-browser" in button_ids
            assert "add-oauth" not in button_ids

            await pilot.press("q")

    asyncio.run(scenario())
