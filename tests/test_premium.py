"""
Premium-lock detection: tracks behind the YouTube Music Premium wall
must be classified separately from ordinary failures, excluded from
download/repair queues, and reported in the status snapshot.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

APP_PATH = Path(__file__).resolve().parent.parent / "ytm-ll-dl-modern.py"


def load_app() -> object:
    spec = importlib.util.spec_from_file_location(
        "ytm_ll_dl_app_premium",
        str(APP_PATH),
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["ytm_ll_dl_app_premium"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def app():
    return load_app()


def make_account(app, slug: str, base: Path) -> dict:
    account_dir = base / "accounts" / slug
    output = base / "files" / slug
    output.mkdir(parents=True, exist_ok=True)
    account_dir.mkdir(parents=True, exist_ok=True)
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


PREMIUM_REASON = (
    "ERROR: [youtube] vidX: This video is only available to "
    "Music Premium members"
)

TRACK = {
    "videoId": "vidX",
    "title": "Premium Song",
    "artists": [{"name": "Someone"}],
    "album": {"name": "An Album"},
}


def seed_inventory(account: dict, app, **quality_kwargs) -> dict:
    inventory = app.load_inventory(account)
    record = {
        "video_id": "vidX",
        "title": "Premium Song",
        "artist": "Someone",
        "album": "An Album",
        "currently_liked": True,
        "qualities": {
            "mp3_320": {
                "status": "failed",
                "reason": quality_kwargs.pop(
                    "reason",
                    PREMIUM_REASON,
                ),
                "file": None,
                **quality_kwargs,
            }
        },
    }
    inventory["tracks"]["vidX"] = record
    app.save_inventory(account, inventory)
    return record


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

def test_premium_failure_detection(app):
    assert app.is_premium_failure(PREMIUM_REASON)
    assert app.is_premium_failure("This video is only available to Music Premium")
    assert not app.is_premium_failure("HTTP Error 403: Forbidden")
    assert not app.is_premium_failure(None)


def test_premium_blocked_from_failed_reason(app):
    record = {"qualities": {"best": {"status": "failed", "reason": PREMIUM_REASON}}}
    assert app.is_premium_blocked(record)


def test_premium_blocked_from_block_field(app):
    record = {"blocked": {"reason": app.PREMIUM_BLOCK_REASON}}
    assert app.is_premium_blocked(record)


def test_premium_not_blocked_on_plain_failure(app):
    record = {"qualities": {"best": {"status": "failed", "reason": "HTTP Error 403"}}}
    assert not app.is_premium_blocked(record)


# ---------------------------------------------------------------------------
# scan_inventory segregation
# ---------------------------------------------------------------------------

def test_scan_inventory_segregates_premium(app, tmp_path):
    account = make_account(app, "prem", tmp_path)
    seed_inventory(account, app)

    report = app.scan_inventory(account, [TRACK], "mp3_320")

    assert [i["video_id"] for i in report["premium"]] == ["vidX"]
    assert report["premium_total"] == 1
    assert report["not_downloaded"] == []
    assert report["not_downloaded_total"] == 0
    assert report["failed"] == []
    assert report["failed_total"] == 0


def test_scan_inventory_keeps_plain_failure(app, tmp_path):
    account = make_account(app, "fail", tmp_path)
    seed_inventory(account, app, reason="HTTP Error 403: Forbidden")

    report = app.scan_inventory(account, [TRACK], "mp3_320")

    assert report["failed_total"] == 1
    assert [i["video_id"] for i in report["failed"]] == ["vidX"]
    assert report["premium_total"] == 0


# ---------------------------------------------------------------------------
# Status snapshot
# ---------------------------------------------------------------------------

def test_status_snapshot_includes_premium(app, tmp_path):
    account = make_account(app, "snap", tmp_path)
    seed_inventory(account, app)

    report = app.scan_inventory(account, [TRACK], "mp3_320")
    app.save_status_snapshot(account, "mp3_320", report)

    snapshot = json.loads(app.status_file(account).read_text(encoding="utf-8"))
    assert snapshot["counts"]["premium"] == 1
    assert [e["video_id"] for e in snapshot["premium"]] == ["vidX"]


# ---------------------------------------------------------------------------
# Download worker bookkeeping
# ---------------------------------------------------------------------------

def test_write_result_sets_and_clears_block(app, tmp_path):
    account = make_account(app, "wrk", tmp_path)
    inventory = app.load_inventory(account)

    worker = app.DownloadWorker(
        account=account,
        tracks=[TRACK],
        sessions=[
            app.DownloadSession(
                quality="mp3_320",
                existing_policy="skip",
                session="once",
            )
        ],
        settings={"retry_attempts": 1, "retry_delay": 0},
        callback=None,
    )

    failed = worker.write_result(
        inventory,
        "vidX",
        TRACK,
        "mp3_320",
        "failed",
        PREMIUM_REASON,
    )
    # write_result returns the live record; the worker then marks the
    # block on premium failures (see run loop), and detection picks
    # it up.
    failed["blocked"] = {"reason": app.PREMIUM_BLOCK_REASON}
    assert app.is_premium_blocked(failed)

    # A later successful download clears the block.
    ok = worker.write_result(
        inventory,
        "vidX",
        TRACK,
        "mp3_320",
        "downloaded",
        "Success",
    )
    assert "blocked" not in ok
    assert not app.is_premium_blocked(ok)


# ---------------------------------------------------------------------------
# Web metadata status
# ---------------------------------------------------------------------------

def test_metadata_track_status_premium(app):
    web_metadata = pytest.importorskip("web.metadata")
    record = {"qualities": {"mp3_320": {"status": "failed", "reason": PREMIUM_REASON}}}
    assert web_metadata._track_status(record, has_file=False) == "premium"

    with_block = {"blocked": {"reason": app.PREMIUM_BLOCK_REASON}}
    assert web_metadata._track_status(with_block, has_file=False) == "premium"

    # A present file wins over a stale block (track became available).
    assert web_metadata._track_status(with_block, has_file=True) == "downloaded"
