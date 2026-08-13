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
