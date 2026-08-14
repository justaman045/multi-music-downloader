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
