"""
Headless tests for the web UI server (web/server.py).

Run from inside the project venv:

    venv/bin/pip install -r requirements-dev.txt
    venv/bin/python -m pytest tests/ -q

No network access. A real (tiny) audio file is written to the temp
project dir so the media-stream endpoint has something to serve.
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest

from web.server import Server


APP_PATH = Path(__file__).resolve().parent.parent / "ytm-ll-dl-modern.py"


def load_app() -> object:
    spec = importlib.util.spec_from_file_location(
        "ytm_ll_dl_web_app",
        str(APP_PATH),
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["ytm_ll_dl_web_app"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def app(tmp_path):
    module = load_app()
    module.PROJECT_DIR = tmp_path
    module.ACCOUNTS_DIR = tmp_path / "accounts"
    module.FILES_DIR = tmp_path / "files"
    module.CONFIG_DIR = tmp_path / "config"
    module.CONFIG_FILE = tmp_path / "config" / "settings.json"
    assert module.require_virtualenv()
    return module


def make_account(app, slug: str, base: Path) -> dict:
    name = "Test Account"
    account_dir = base / "accounts" / slug
    output = app.account_output_directory(name)
    output.mkdir(parents=True, exist_ok=True)
    (account_dir / ".headers_auth.json").parent.mkdir(parents=True, exist_ok=True)
    (account_dir / ".headers_auth.json").write_text("{}", encoding="utf-8")
    (account_dir / "account.json").write_text(
        json.dumps({"name": name, "slug": slug}),
        encoding="utf-8",
    )
    return {
        "slug": slug,
        "name": name,
        "auth": account_dir / ".headers_auth.json",
        "output": output,
    }


def seed_track(app, account: dict, video_id: str, ext: str = "mp3") -> None:
    out_dir = account["output"] / "MP3_320kbps"
    out_dir.mkdir(parents=True, exist_ok=True)
    file_path = out_dir / f"Artist, Song.{ext}"
    file_path.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00fakemp3data" * 100)
    inventory = {
        "version": 2,
        "last_library_sync": 123,
        "liked_count": 1,
        "tracks": {
            video_id: {
                "video_id": video_id,
                "qualities": {
                    "mp3_320": {
                        "status": "downloaded",
                        "reason": None,
                        "file": str(file_path),
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
    app.save_inventory(account, inventory)


@pytest.fixture
def server(app, tmp_path):
    account = make_account(app, "test_acct", tmp_path)
    seed_track(app, account, "AbC123xyz_9")

    srv = Server(("127.0.0.1", 0), app)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, port
    srv.shutdown()
    srv.server_close()


def request(port: int, method: str, path: str, body: str | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, body=body)
        res = conn.getresponse()
        data = res.read()
        return res.status, dict(res.getheaders()), data
    finally:
        conn.close()


def test_health(server):
    _srv, port = server
    status, _headers, data = request(port, "GET", "/api/health")
    assert status == 200
    assert json.loads(data) == {"ok": True}


def test_static_spa(server):
    _srv, port = server
    status, headers, data = request(port, "GET", "/")
    assert status == 200
    assert "text/html" in headers["Content-Type"]
    assert b"<!DOCTYPE html>" in data


def test_static_assets_are_not_cacheable(server):
    """A phone must never keep running a stale SPA (the cause of a link
    with ?token= still prompting for a token)."""
    _srv, port = server
    for path in ("/", "/assets/app.js", "/assets/styles.css"):
        status, headers, _data = request(port, "GET", path)
        assert status == 200, path
        assert headers.get("Cache-Control") == "no-store", path


def test_index_html_asset_urls_are_version_stamped(server, app):
    """index.html references assets with ?v=<APP_VERSION> so any upgrade
    forces a fresh fetch even through an over-eager cache."""
    _srv, port = server
    status, _headers, data = request(port, "GET", "/")
    assert status == 200
    html = data.decode("utf-8")
    version = app.APP_VERSION
    assert f"/assets/app.js?v={version}" in html
    assert f"/assets/styles.css?v={version}" in html
    # The versioned URL must still serve the real file.
    status, _headers, js = request(port, "GET", f"/assets/app.js?v={version}")
    assert status == 200
    assert b"captureUrlToken" in js


def test_accounts_and_library(server):
    _srv, port = server
    status, _headers, data = request(port, "GET", "/api/accounts")
    assert status == 200
    accounts = json.loads(data)
    assert accounts[0]["slug"] == "test_acct"

    status, _headers, data = request(port, "GET", "/api/library")
    assert status == 200
    library = json.loads(data)
    track = library["tracks"][0]
    assert track["id"] == "AbC123xyz_9"
    assert track["ext"] == "mp3"
    assert track["stream"].endswith("/api/media/test_acct/AbC123xyz_9/stream")


def test_delete_account_via_api(server, tmp_path):
    """DELETE /api/accounts/<slug> removes the account directory.

    Regression: the handler passed the bare slug string into
    delete_account(), which expects the account record (it reads
    account["directory"]) — every delete crashed with a TypeError."""
    _srv, port = server

    status, _headers, data = request(port, "DELETE", "/api/accounts/test_acct")
    assert status == 200
    assert json.loads(data) == {"deleted": "test_acct"}
    assert not (tmp_path / "accounts" / "test_acct").exists()

    # Gone now: listing is empty and a repeat delete is a 404.
    status, _headers, data = request(port, "GET", "/api/accounts")
    assert status == 200
    assert json.loads(data) == []
    status, _headers, _data = request(port, "DELETE", "/api/accounts/test_acct")
    assert status == 404


def test_media_stream_range_and_full(server):
    _srv, port = server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(
        "GET",
        "/api/media/test_acct/AbC123xyz_9/stream",
        headers={"Range": "bytes=0-99"},
    )
    res = conn.getresponse()
    assert res.status == 206
    assert int(res.getheader("Content-Range").split("/")[1]) > 100
    data = res.read()
    assert len(data) == 100
    conn.close()

    status, headers, data = request(
        port,
        "GET",
        "/api/media/test_acct/AbC123xyz_9/stream",
    )
    assert status == 200
    assert "audio" in headers["Content-Type"]
    assert len(data) > 100


def test_unknown_api_route(server):
    _srv, port = server
    status, _headers, data = request(port, "GET", "/api/nope")
    assert status == 404


def test_settings_roundtrip_via_api(server, app):
    _srv, port = server
    status, _headers, data = request(
        port,
        "POST",
        "/api/settings",
        body=json.dumps({"default_quality": "mp3_192"}),
    )
    assert status == 200
    assert json.loads(data)["default_quality"] == "mp3_192"
    assert app.load_settings()["default_quality"] == "mp3_192"


def test_settings_rate_limit_via_api(server, app):
    _srv, port = server
    status, _headers, data = request(
        port,
        "POST",
        "/api/settings",
        body=json.dumps({"download_rate_limit": "500K"}),
    )
    assert status == 200
    assert json.loads(data)["download_rate_limit"] == "500K"
    assert app.load_settings()["download_rate_limit"] == "500K"

    status, _headers, _data = request(
        port,
        "POST",
        "/api/settings",
        body=json.dumps({"download_rate_limit": "!!bogus!!"}),
    )
    assert status == 400


def test_playlist_export_csv(server, app, tmp_path):
    _srv, port = server
    import web.playlists

    playlist = web.playlists.create_playlist(
        app,
        "Test CSV",
        kind="custom",
        account="test_acct",
    )
    web.playlists.add_tracks(
        app,
        playlist["id"],
        [
            {
                "account": "test_acct",
                "video_id": "AbC123xyz_9",
                "title": "Song",
                "artist": "Artist",
                "album": "LP",
            }
        ],
    )
    playlist = web.playlists.get_playlist(app, playlist["id"])
    path = web.playlists.export_playlist(app, playlist, "csv")
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "video_id,account,title,artist,album" in text
    assert "AbC123xyz_9" in text
    assert "Song" in text

    status, _headers, data = request(
        port,
        "POST",
        f"/api/playlists/{playlist['id']}/export",
        body=json.dumps({"format": "csv"}),
    )
    assert status == 200
    assert Path(json.loads(data)["file"]).exists()


def test_history_endpoint(server, app, tmp_path):
    _srv, port = server
    from web import plays

    now = int(__import__("time").time())
    data = {
        "version": 1,
        "plays": {
            "AbC123xyz_9": {
                "video_id": "AbC123xyz_9",
                "count": 3,
                "first_played": now - 86400,
                "last_played": now,
            }
        },
        "recent": [
            {"video_id": "AbC123xyz_9", "ts": now - 2 * 86400, "progress": 90.0},
            {"video_id": "AbC123xyz_9", "ts": now - 3600, "progress": 80.0},
        ],
        "updated_at": now,
    }
    path = plays.plays_path(app, "test_acct")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")

    status, _headers, body = request(port, "GET", "/api/history?days=30")
    assert status == 200
    history = json.loads(body)
    assert history["total_plays"] == 2
    assert len(history["per_day"]) == 2
    assert history["top_tracks"][0]["id"] == "AbC123xyz_9"


def test_play_records_and_broadcasts_sse_event(server):
    """POSTing a play must persist it and broadcast a play_recorded event
    on the event bus so connected SPA clients can live-update."""
    srv, port = server
    queue_ = srv.web.bus.subscribe()
    try:
        status, _headers, body = request(
            port,
            "POST",
            "/api/plays",
            body=json.dumps(
                {"slug": "test_acct", "video_id": "AbC123xyz_9", "progress": 75}
            ),
        )
        assert status == 200
        result = json.loads(body)
        assert result["counted"] is True
        assert result["total"] == 1

        payload = queue_.get(timeout=5)
        event = json.loads(payload[len("data: ") :])
        assert event["type"] == "play_recorded"
        assert event["slug"] == "test_acct"
        assert event["video_id"] == "AbC123xyz_9"
        assert event["counted"] is True
        assert event["total"] == 1
    finally:
        srv.web.bus.unsubscribe(queue_)



def test_verify_endpoint_ok(server, app):
    _srv, port = server
    status, _headers, body = request(
        port,
        "POST",
        "/api/verify",
        body=json.dumps({"slug": "test_acct"}),
    )
    assert status == 200
    report = json.loads(body)
    assert report["summary"]["ok"] == 1
    assert report["results"]["AbC123xyz_9"]["status"] == "ok"


def test_verify_endpoint_missing(server, app):
    _srv, port = server
    import shutil

    account = app.get_accounts()[0]
    for quality_dir in account["output"].iterdir():
        if quality_dir.is_dir():
            shutil.rmtree(quality_dir)

    status, _headers, body = request(
        port,
        "POST",
        "/api/verify",
        body=json.dumps({"slug": "test_acct"}),
    )
    assert status == 200
    report = json.loads(body)
    assert report["summary"]["missing"] == 1
    assert report["results"]["AbC123xyz_9"]["status"] == "missing"


def test_search_anonymous_fallback(server, app, monkeypatch):
    """Without accounts= the search falls back to a plain (anonymous)
    results list so the SPA's simple path keeps working."""
    _srv, port = server

    def fake_search(query, limit=10, account=None):
        assert account is None
        return [
            {
                "videoId": "xyz9876abc",
                "title": "Anon Song",
                "artists": [{"name": "Anon Artist"}],
            }
        ]

    monkeypatch.setattr(app, "search_tracks", fake_search)
    status, _headers, body = request(port, "GET", "/api/search?q=song")
    assert status == 200
    payload = json.loads(body)
    assert isinstance(payload, list)
    assert payload[0]["videoId"] == "xyz9876abc"


def test_search_requires_query(server):
    _srv, port = server
    status, _headers, _body = request(port, "GET", "/api/search?q=")
    assert status == 400


def test_search_multi_account(server, app, monkeypatch):
    """accounts= runs an authenticated search per account and annotates
    results with which accounts can stream them (deduped by videoId)."""
    _srv, port = server

    def fake_search(query, limit=10, account=None):
        if account and account["slug"] == "test_acct":
            return [
                {
                    "videoId": "abc123def45",
                    "title": "Found Song",
                    "artists": [{"name": "Found Artist"}],
                    "album": {"name": "LP"},
                }
            ]
        return []

    monkeypatch.setattr(app, "search_tracks", fake_search)

    status, _headers, body = request(
        port,
        "GET",
        "/api/search?q=found&limit=8&accounts=test_acct",
    )
    assert status == 200
    payload = json.loads(body)
    assert payload["accounts"] == ["test_acct"]
    assert len(payload["results"]) == 1
    result = payload["results"][0]
    assert result["videoId"] == "abc123def45"
    assert result["accounts"] == ["test_acct"]
    assert result["owned_by"] == []


def test_search_multi_account_unknown(server):
    _srv, port = server
    status, _headers, _body = request(
        port,
        "GET",
        "/api/search?q=found&accounts=nope",
    )
    assert status == 404


def test_duplicates_export(server, app):
    _srv, port = server
    status, _headers, data = request(
        port,
        "POST",
        "/api/duplicates/export",
        body=json.dumps({}),
    )
    assert status == 200
    file = Path(json.loads(data)["file"])
    assert file.exists()
    assert file.parent == app.CONFIG_DIR / "exports"


def test_download_start_requires_valid_account(server):
    _srv, port = server
    status, _headers, data = request(
        port,
        "POST",
        "/api/downloads",
        body=json.dumps({"account": "no_such", "quality": "best"}),
    )
    assert status == 400


def test_token_gate(app, tmp_path):
    account = make_account(app, "test_acct", tmp_path)
    seed_track(app, account, "AbC123xyz_9")

    srv = Server(("127.0.0.1", 0), app, token="sekrit")
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        status, _headers, _data = request(port, "GET", "/api/health")
        assert status == 401
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/health", headers={"Authorization": "Bearer sekrit"})
        res = conn.getresponse()
        res.read()
        assert res.status == 200
        conn.close()
        status, _headers, _data = request(port, "GET", "/")
        assert status == 200
    finally:
        srv.shutdown()
        srv.server_close()


def _make_real_mp3(path: Path) -> None:
    """Generate a tiny valid MP3 with the bundled ffmpeg so mutagen can
    actually parse and re-save it (the seeded fixture bytes are fake)."""
    import subprocess

    import imageio_ffmpeg

    exe = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run(
        [
            exe,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=mono",
            "-t",
            "0.2",
            "-b:a",
            "96k",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def test_tag_editor_writes_file_tags_and_inventory(server, app):
    """SPA Edit-tags modal -> POST /api/tags must write real tags into every
    downloaded quality file and update the inventory record, and expected
    failures must come back as clear 400s (not generic 500s)."""
    _srv, port = server

    inv_path = app.account_output_directory("Test Account") / ".inventory.json"
    inventory = json.loads(inv_path.read_text(encoding="utf-8"))
    file_path = Path(
        inventory["tracks"]["AbC123xyz_9"]["qualities"]["mp3_320"]["file"]
    )
    _make_real_mp3(file_path)

    status, _headers, data = request(
        port,
        "POST",
        "/api/tags",
        body=json.dumps(
            {
                "slug": "test_acct",
                "video_id": "AbC123xyz_9",
                "tags": {
                    "title": "New Title",
                    "artist": "New Artist",
                    "album": "New Album",
                    "genre": "Ambient",
                    "year": "2024",
                },
            }
        ),
    )
    assert status == 200, data
    payload = json.loads(data)
    assert payload["title"] == "New Title"
    assert payload["files"] == [str(file_path)]

    import mutagen

    audio = mutagen.File(str(file_path))
    assert audio is not None
    assert str(audio.tags.get("TIT2")) == "New Title"
    assert str(audio.tags.get("TPE1")) == "New Artist"
    assert str(audio.tags.get("TALB")) == "New Album"

    saved = json.loads(inv_path.read_text(encoding="utf-8"))
    assert saved["tracks"]["AbC123xyz_9"]["title"] == "New Title"
    assert saved["tracks"]["AbC123xyz_9"]["artist"] == "New Artist"
    assert saved["tracks"]["AbC123xyz_9"]["album"] == "New Album"


def test_tag_editor_rejects_unknown_track_with_reason(server):
    _srv, port = server
    status, _headers, data = request(
        port,
        "POST",
        "/api/tags",
        body=json.dumps(
            {
                "slug": "test_acct",
                "video_id": "zzz123zzz_9",
                "tags": {"title": "x", "artist": "y"},
            }
        ),
    )
    assert status == 400
    assert b"Unknown track" in data

    status, _headers, data = request(
        port,
        "POST",
        "/api/tags",
        body=json.dumps(
            {"slug": "nope", "video_id": "AbC123xyz_9", "tags": {}}
        ),
    )
    assert status == 404


def test_history_insight_helpers():
    """Streak and week-compare helpers power the stats insight cards and
    must handle empty, broken, and multi-streak histories."""
    from datetime import date, timedelta

    from web import plays

    today = date.today()

    def days_ago(n: int) -> str:
        return (today - timedelta(days=n)).isoformat()

    # Current 5-day streak ending today + an older best streak of 10.
    per_day = {days_ago(i): 1 for i in range(5)}
    per_day.update({days_ago(20 + i): 1 for i in range(10)})
    streaks = plays._listening_streaks(per_day)
    assert streaks["current"] == 5
    assert streaks["best"] == 10

    # Nothing played today/yesterday: current streak is 0, best survives.
    streaks = plays._listening_streaks({days_ago(10): 1, days_ago(9): 1})
    assert streaks["current"] == 0
    assert streaks["best"] == 2

    assert plays._listening_streaks({}) == {"current": 0, "best": 0}

    # Streak survives until midnight: played only today -> current == 1.
    assert plays._listening_streaks({days_ago(0): 3})["current"] == 1

    week = plays._week_compare(
        {
            days_ago(0): 2,
            days_ago(6): 1,
            days_ago(7): 5,
            days_ago(13): 1,
            days_ago(20): 99,  # outside both windows
        }
    )
    assert week == {"this": 3, "prev": 6}


def test_history_endpoint_insight_fields(server, app):
    """/api/history must expose per_hour (24 buckets), streaks, and the
    week comparison for the stats insight cards."""
    import time as _time
    from datetime import date, timedelta

    from web import plays

    _srv, port = server
    now = int(_time.time())
    today = date.today()

    def noon_days_ago(n: int) -> int:
        target = today - timedelta(days=n)
        return int(
            _time.mktime(
                (target.year, target.month, target.day, 12, 0, 0, 0, 0, -1)
            )
        )

    data = {
        "version": 1,
        "plays": {
            "AbC123xyz_9": {
                "video_id": "AbC123xyz_9",
                "count": 4,
                "first_played": noon_days_ago(3),
                "last_played": now,
            }
        },
        # One play a day for the last 4 days, all at noon (hour bucket 12).
        "recent": [
            {"video_id": "AbC123xyz_9", "ts": noon_days_ago(i), "progress": 90.0}
            for i in range(4)
        ],
        "updated_at": now,
    }
    path = plays.plays_path(app, "test_acct")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")

    status, _headers, body = request(port, "GET", "/api/history?days=30")
    assert status == 200
    history = json.loads(body)

    per_hour = history["per_hour"]
    assert isinstance(per_hour, list) and len(per_hour) == 24
    assert sum(per_hour) == 4
    assert per_hour[12] == 4

    assert history["streaks"]["current"] == 4
    assert history["streaks"]["best"] >= 4
    assert history["week"]["this"] == 4


def test_library_export_formats(server):
    """POST /api/export/library writes M3U/CSV/JSON into config/exports
    and rejects bad formats/accounts with clear 400s."""
    _srv, port = server

    status, _headers, data = request(
        port, "POST", "/api/export/library", body=json.dumps({"format": "m3u"})
    )
    assert status == 200, data
    m3u_path = Path(json.loads(data)["file"])
    assert m3u_path.exists()
    text = m3u_path.read_text(encoding="utf-8")
    assert text.startswith("#EXTM3U")
    assert "Artist, Song.mp3" in text
    assert "Artist - Song" in text

    status, _headers, data = request(
        port, "POST", "/api/export/library", body=json.dumps({"format": "csv"})
    )
    assert status == 200
    csv_path = Path(json.loads(data)["file"])
    text = csv_path.read_text(encoding="utf-8")
    assert "video_id,account,title,artist,album,file" in text
    assert "AbC123xyz_9" in text

    status, _headers, data = request(
        port, "POST", "/api/export/library", body=json.dumps({"format": "json"})
    )
    assert status == 200
    payload = json.loads(Path(json.loads(data)["file"]).read_text(encoding="utf-8"))
    assert payload["kind"] == "library"
    assert payload["tracks"][0]["video_id"] == "AbC123xyz_9"
    assert payload["tracks"][0]["file"]

    status, _headers, data = request(
        port, "POST", "/api/export/library", body=json.dumps({"format": "xlsx"})
    )
    assert status == 400
    assert b"m3u" in data

    status, _headers, data = request(
        port,
        "POST",
        "/api/export/library",
        body=json.dumps({"format": "csv", "slug": "nope"}),
    )
    assert status == 400
    assert b"Unknown account" in data


def test_library_sync_link_and_skip(server, app, tmp_path):
    """POST /api/sync hardlinks downloaded files into the target tree
    (Account/Artist/Album/filename), is idempotent, and refuses unsafe
    targets."""
    _srv, port = server
    target = tmp_path / "sync_out"

    # Missing target -> 400 with the actual reason.
    status, _headers, data = request(
        port, "POST", "/api/sync", body=json.dumps({})
    )
    assert status == 400
    assert b"target" in data.lower()

    # Relative target -> 400.
    status, _headers, data = request(
        port, "POST", "/api/sync", body=json.dumps({"target": "relative/dir"})
    )
    assert status == 400
    assert b"absolute" in data

    # Dry run reports what would happen without touching disk.
    status, _headers, data = request(
        port,
        "POST",
        "/api/sync",
        body=json.dumps({"target": str(target), "dry_run": True}),
    )
    assert status == 200, data
    report = json.loads(data)
    assert report["dry_run"] is True
    assert report["scanned"] == 1
    assert report["linked"] == 1
    assert not target.exists()

    # Real sync creates a hardlink at the expected path.
    status, _headers, data = request(
        port, "POST", "/api/sync", body=json.dumps({"target": str(target)})
    )
    assert status == 200, data
    report = json.loads(data)
    assert report["linked"] == 1
    assert report["errors"] == []
    synced = target / "Test Account" / "Artist" / "LP" / "Artist, Song.mp3"
    assert synced.exists()

    source = Path(
        json.loads(
            (
                app.account_output_directory("Test Account") / ".inventory.json"
            ).read_text(encoding="utf-8")
        )["tracks"]["AbC123xyz_9"]["qualities"]["mp3_320"]["file"]
    )
    # Same filesystem in tmp -> hardlink, so no extra bytes used.
    assert source.stat().st_ino == synced.stat().st_ino

    # Second run: already in place, nothing re-linked.
    status, _headers, data = request(
        port, "POST", "/api/sync", body=json.dumps({"target": str(target)})
    )
    assert status == 200
    report = json.loads(data)
    assert report["linked"] == 0
    assert report["skipped"] == 1

    # Copy mode produces an independent file with identical content.
    copy_target = tmp_path / "sync_copy"
    status, _headers, data = request(
        port,
        "POST",
        "/api/sync",
        body=json.dumps({"target": str(copy_target), "mode": "copy"}),
    )
    assert status == 200
    copied = copy_target / "Test Account" / "Artist" / "LP" / "Artist, Song.mp3"
    assert copied.exists()
    assert copied.read_bytes() == source.read_bytes()
    assert copied.stat().st_ino != source.stat().st_ino

    # Syncing into the app's own files/ tree is refused.
    status, _headers, data = request(
        port,
        "POST",
        "/api/sync",
        body=json.dumps({"target": str(tmp_path / "files" / "out")}),
    )
    assert status == 400
    assert b"files/" in data


def _patched_manager_start(srv):
    """Replace DownloadManager.start with a recorder: the real one spawns
    a worker thread that shells out to yt-dlp, which must not happen in
    tests."""
    calls = []

    def fake_start(
        account,
        tracks,
        sessions,
        settings,
        output_dir=None,
        source="library",
    ):
        calls.append(
            {
                "slug": account["slug"],
                "tracks": [track.get("videoId") for track in tracks],
            }
        )
        return {
            "slug": account["slug"],
            "status": "queued",
            "finished": False,
            "counts": {"total": len(tracks)},
        }

    srv.web.downloads.start = fake_start
    return calls


def test_queue_add_remove_clear(server):
    """The persistent queue must dedupe on (account, video_id), expose a
    GET snapshot, and support remove/clear; empty adds are a clear 400."""
    _srv, port = server

    track = {
        "video_id": "AbC123xyz_9",
        "account": "test_acct",
        "title": "Song",
        "artist": "Artist",
    }
    status, _headers, data = request(
        port, "POST", "/api/queue", body=json.dumps({"tracks": [track]})
    )
    assert status == 200, data
    assert json.loads(data) == {"added": 1, "queued": 1}

    # Same track again: deduped, nothing added.
    status, _headers, data = request(
        port, "POST", "/api/queue", body=json.dumps({"tracks": [track]})
    )
    assert status == 200
    assert json.loads(data) == {"added": 0, "queued": 1}

    status, _headers, data = request(port, "GET", "/api/queue")
    assert status == 200
    snapshot = json.loads(data)
    assert snapshot["auto_start"] is False
    assert len(snapshot["items"]) == 1
    item = snapshot["items"][0]
    assert item["video_id"] == "AbC123xyz_9"
    assert item["account"] == "test_acct"

    # No tracks -> 400 with the actual reason.
    status, _headers, data = request(
        port, "POST", "/api/queue", body=json.dumps({"tracks": []})
    )
    assert status == 400
    assert b"No tracks" in data

    status, _headers, data = request(
        port, "POST", "/api/queue/remove", body=json.dumps({"ids": [item["id"]]})
    )
    assert status == 200
    assert json.loads(data) == {"removed": 1, "queued": 0}

    request(
        port, "POST", "/api/queue", body=json.dumps({"tracks": [track]})
    )
    status, _headers, data = request(
        port, "POST", "/api/queue/clear", body=json.dumps({})
    )
    assert status == 200
    assert json.loads(data) == {"removed": 1, "queued": 0}


def test_queue_start_consumes_items(server):
    """Starting the queue launches one batch per account via the download
    manager and consumes exactly the started items."""
    srv, port = server
    calls = _patched_manager_start(srv)

    status, _headers, _data = request(
        port,
        "POST",
        "/api/queue",
        body=json.dumps(
            {
                "tracks": [
                    {
                        "video_id": "AbC123xyz_9",
                        "account": "test_acct",
                        "title": "Song",
                    }
                ]
            }
        ),
    )
    assert status == 200

    status, _headers, data = request(
        port, "POST", "/api/queue/start", body=json.dumps({})
    )
    assert status == 200, data
    result = json.loads(data)
    assert result["started"] == ["test_acct"]
    assert result["deferred"] == []
    assert result["dropped"] == 0
    assert result["queued_left"] == 0
    assert calls == [{"slug": "test_acct", "tracks": ["AbC123xyz_9"]}]

    status, _headers, data = request(port, "GET", "/api/queue")
    assert json.loads(data)["items"] == []

    # Starting an empty queue is a no-op, not an error.
    status, _headers, data = request(
        port, "POST", "/api/queue/start", body=json.dumps({})
    )
    assert status == 200
    assert json.loads(data)["started"] == []


def test_queue_start_defers_busy_account(server):
    """An account with an active session must be deferred (items stay in
    the queue) and picked up once the slot frees."""
    srv, port = server
    calls = _patched_manager_start(srv)
    request(
        port,
        "POST",
        "/api/queue",
        body=json.dumps(
            {"tracks": [{"video_id": "AbC123xyz_9", "account": "test_acct"}]}
        ),
    )

    srv.web.downloads.workers["test_acct"] = {"snapshot": {"finished": False}}
    status, _headers, data = request(
        port, "POST", "/api/queue/start", body=json.dumps({})
    )
    assert status == 200
    result = json.loads(data)
    assert result["started"] == []
    assert result["deferred"] == ["test_acct"]
    assert result["queued_left"] == 1
    assert calls == []

    srv.web.downloads.workers["test_acct"]["snapshot"]["finished"] = True
    status, _headers, data = request(
        port, "POST", "/api/queue/start", body=json.dumps({})
    )
    assert status == 200
    assert json.loads(data)["started"] == ["test_acct"]
    assert len(calls) == 1
    srv.web.downloads.workers.clear()


def test_queue_start_drops_stale_items(server):
    """Items for a removed account or a track gone from the inventory are
    dropped on start so the queue can never jam."""
    srv, port = server
    calls = _patched_manager_start(srv)
    request(
        port,
        "POST",
        "/api/queue",
        body=json.dumps(
            {
                "tracks": [
                    {"video_id": "Gh0stVideo_1", "account": "ghost_acct"},
                    {"video_id": "missing123_x", "account": "test_acct"},
                ]
            }
        ),
    )

    status, _headers, data = request(
        port, "POST", "/api/queue/start", body=json.dumps({})
    )
    assert status == 200
    result = json.loads(data)
    assert result["started"] == []
    assert result["dropped"] == 2
    assert result["queued_left"] == 0
    assert calls == []


def test_queue_start_validation(server):
    _srv, port = server
    for body, needle in (
        ({"quality": "nope"}, b"Unknown quality"),
        ({"existing_policy": "explode"}, b"Unknown policy"),
        ({"session": "forever"}, b"Unknown session"),
    ):
        status, _headers, data = request(
            port, "POST", "/api/queue/start", body=json.dumps(body)
        )
        assert status == 400, body
        assert needle in data


def test_queue_auto_start_via_scheduler(server):
    """The auto_start toggle persists, and the scheduler tick starts the
    next batch when it is on (and stays quiet when it is off)."""
    srv, port = server
    calls = _patched_manager_start(srv)

    status, _headers, data = request(
        port, "POST", "/api/queue/auto", body=json.dumps({"enabled": True})
    )
    assert status == 200
    assert json.loads(data)["auto_start"] is True
    status, _headers, data = request(port, "GET", "/api/queue")
    assert json.loads(data)["auto_start"] is True

    request(
        port,
        "POST",
        "/api/queue",
        body=json.dumps(
            {"tracks": [{"video_id": "AbC123xyz_9", "account": "test_acct"}]}
        ),
    )

    srv.web.scheduler._queue_auto_start({})
    assert calls == [{"slug": "test_acct", "tracks": ["AbC123xyz_9"]}]
    status, _headers, data = request(port, "GET", "/api/queue")
    assert json.loads(data)["items"] == []

    # Disabled: the tick must not start anything.
    status, _headers, data = request(
        port, "POST", "/api/queue/auto", body=json.dumps({"enabled": False})
    )
    assert status == 200
    assert json.loads(data)["auto_start"] is False
    request(
        port,
        "POST",
        "/api/queue",
        body=json.dumps(
            {"tracks": [{"video_id": "AbC123xyz_9", "account": "test_acct"}]}
        ),
    )
    srv.web.scheduler._queue_auto_start({})
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Browser-cookie account import (automatic sign-in)
# ---------------------------------------------------------------------------


def test_browsers_endpoint(server, app, monkeypatch):
    _srv, port = server
    monkeypatch.setattr(
        app,
        "list_importable_browsers",
        lambda: [{"id": "firefox", "label": "Firefox", "available": True}],
    )
    status, _headers, data = request(port, "GET", "/api/browsers")
    assert status == 200
    assert json.loads(data) == [
        {"id": "firefox", "label": "Firefox", "available": True}
    ]


def test_account_browser_import_via_api(server, app, monkeypatch):
    _srv, port = server

    def fake_import(name, browser, profile=None):
        return {"slug": "browser_acct", "name": name, "track_count": 3}

    monkeypatch.setattr(app, "import_account_from_browser", fake_import)
    status, _headers, data = request(
        port,
        "POST",
        "/api/accounts",
        body=json.dumps(
            {"name": "Browser Acct", "browser": "firefox", "profile": ""}
        ),
    )
    assert status == 201
    payload = json.loads(data)
    assert payload["slug"] == "browser_acct"
    assert payload["track_count"] == 3


def test_account_browser_import_passes_profile_through(server, app, monkeypatch):
    """A picked profile reaches import_account_from_browser verbatim;
    an empty (auto) profile arrives as None."""
    _srv, port = server
    seen: list[object] = []

    def fake_import(name, browser, profile=None):
        seen.append(profile)
        return {"slug": "p", "name": name, "track_count": 0}

    monkeypatch.setattr(app, "import_account_from_browser", fake_import)
    for profile in ("Profile 10", ""):
        status, _headers, _data = request(
            port,
            "POST",
            "/api/accounts",
            body=json.dumps(
                {"name": "P", "browser": "chrome", "profile": profile}
            ),
        )
        assert status == 201
    assert seen == ["Profile 10", None]


def test_account_browser_import_error_is_clear(server, app, monkeypatch):
    """Browser import failures surface their actionable message."""
    _srv, port = server

    def boom(name, browser, profile=None):
        raise ValueError("Close Chrome completely and try again.")

    monkeypatch.setattr(app, "import_account_from_browser", boom)
    status, _headers, data = request(
        port,
        "POST",
        "/api/accounts",
        body=json.dumps({"name": "X", "browser": "chrome"}),
    )
    assert status == 400
    assert "Close Chrome completely" in json.loads(data)["error"]


def test_account_browser_import_requires_name(server):
    _srv, port = server
    status, _headers, data = request(
        port,
        "POST",
        "/api/accounts",
        body=json.dumps({"browser": "firefox"}),
    )
    assert status == 400
    assert "Name is required" in json.loads(data)["error"]


def test_oauth_account_mode_is_gone(server):
    """The Google device-flow sign-in was removed (YouTube blocks every
    third-party OAuth token on its internal API), so the old body
    shape falls through to the plain headers requirement."""
    _srv, port = server
    status, _headers, data = request(
        port,
        "POST",
        "/api/accounts",
        body=json.dumps({"name": "X", "oauth": "start"}),
    )
    assert status == 400
    assert "headers" in json.loads(data)["error"].lower()


def test_lan_addresses_shape():
    """LAN discovery returns usable IPv4 addresses (no loopback/wildcard).

    Used to print the "Network:" URLs a user opens on their phone.
    """
    from web.server import lan_addresses

    addrs = lan_addresses()
    assert isinstance(addrs, list)
    for ip in addrs:
        assert isinstance(ip, str) and ip
        assert not ip.startswith("127.")
        assert ip != "0.0.0.0"
    # Deduplicated.
    assert len(addrs) == len(set(addrs))
