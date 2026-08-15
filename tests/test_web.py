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
