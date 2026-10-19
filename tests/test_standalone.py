"""
Tests for the standalone (frozen executable) runtime support:
data-dir resolution, the venv-guard bypass, runtime binary helpers,
and the launch-mode dispatch used by the double-click menu.

These tests monkeypatch IS_FROZEN on the already-imported module and
re-import where needed; they never touch the network or real data.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


APP_PATH = Path(__file__).resolve().parent.parent / "ytm-ll-dl-modern.py"


def load_app(name: str = "ytm_ll_dl_app_frozen") -> object:
    spec = importlib.util.spec_from_file_location(name, str(APP_PATH))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def app():
    return load_app()


# ---------------------------------------------------------------------------
# Data directory resolution
# ---------------------------------------------------------------------------


def test_source_mode_data_dir_is_project_local(app):
    """Unfrozen runs keep data project-local (repo convention)."""
    assert not app.IS_FROZEN
    assert app.DATA_DIR == app.PROJECT_DIR
    assert app.ACCOUNTS_DIR == app.PROJECT_DIR / "accounts"
    assert app.FILES_DIR == app.PROJECT_DIR / "files"
    assert app.CONFIG_DIR == app.PROJECT_DIR / "config"


def test_ytm_data_dir_override_wins(app, monkeypatch, tmp_path):
    monkeypatch.setenv("YTM_DATA_DIR", str(tmp_path / "custom"))
    resolved = app.resolve_data_dir()
    assert resolved == (tmp_path / "custom").resolve()


def test_frozen_data_dir_linux(app, monkeypatch, tmp_path):
    monkeypatch.delenv("YTM_DATA_DIR", raising=False)
    monkeypatch.setattr(app, "IS_FROZEN", True)
    monkeypatch.setattr(app.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    resolved = app.resolve_data_dir()
    assert resolved == tmp_path / "xdg" / "yt-music-downloader"


def test_frozen_data_dir_macos(app, monkeypatch):
    monkeypatch.delenv("YTM_DATA_DIR", raising=False)
    monkeypatch.setattr(app, "IS_FROZEN", True)
    monkeypatch.setattr(app.sys, "platform", "darwin")
    resolved = app.resolve_data_dir()
    assert resolved.name == "YTMusicDownloader"
    assert "Application Support" in resolved.parts


def test_frozen_data_dir_windows(app, monkeypatch, tmp_path):
    monkeypatch.delenv("YTM_DATA_DIR", raising=False)
    monkeypatch.setattr(app, "IS_FROZEN", True)
    monkeypatch.setattr(app.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    resolved = app.resolve_data_dir()
    assert resolved == tmp_path / "appdata" / "YTMusicDownloader"


# ---------------------------------------------------------------------------
# venv guard bypass + runtime binaries
# ---------------------------------------------------------------------------


def test_venv_guard_bypassed_when_frozen(app, monkeypatch):
    monkeypatch.setattr(app, "IS_FROZEN", True)
    assert app.require_virtualenv() is True


def test_runtime_bin_dir_source_mode(app):
    assert not app.IS_FROZEN
    assert app.runtime_bin_dir() == app.VENV_BIN


def test_runtime_bin_dir_frozen(app, monkeypatch, tmp_path):
    monkeypatch.setattr(app, "IS_FROZEN", True)
    monkeypatch.setattr(app, "DATA_DIR", tmp_path)
    assert app.runtime_bin_dir() == tmp_path / "bin"


def test_yt_dlp_binary_has_windows_extension(app, monkeypatch, tmp_path):
    monkeypatch.setattr(app, "IS_FROZEN", True)
    monkeypatch.setattr(app, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app.os, "name", "nt")
    assert app.yt_dlp_binary() == tmp_path / "bin" / "yt-dlp.exe"
    assert app.deno_binary() == tmp_path / "bin" / "deno.exe"

    monkeypatch.setattr(app.os, "name", "posix")
    assert app.yt_dlp_binary() == tmp_path / "bin" / "yt-dlp"
    assert app.deno_binary() == tmp_path / "bin" / "deno"


def test_source_mode_binaries_point_into_venv(app):
    binary = app.yt_dlp_binary()
    assert binary.parent == app.VENV_BIN
    assert binary.name.startswith("yt-dlp")


# ---------------------------------------------------------------------------
# Bundled-asset lookup
# ---------------------------------------------------------------------------


def test_bundled_asset_none_in_source_mode(app):
    assert app.bundled_asset("bin/deno") is None


def test_bundled_asset_reads_meipass(app, monkeypatch, tmp_path):
    monkeypatch.setattr(app, "IS_FROZEN", True)
    monkeypatch.setattr(app.sys, "_MEIPASS", str(tmp_path), raising=False)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "deno").write_text("fake")
    found = app.bundled_asset("bin/deno")
    assert found == tmp_path / "bin" / "deno"
    assert app.bundled_asset("bin/nope") is None


# ---------------------------------------------------------------------------
# Launch dispatch
# ---------------------------------------------------------------------------


def test_has_interactive_console_with_piped_stdin(monkeypatch):
    module = load_app("ytm_ll_dl_app_console")
    monkeypatch.setattr(module.sys, "stdin", None)
    assert module.has_interactive_console() is False


def test_parse_serve_args(app):
    host, port, token, open_browser = app.parse_serve_args(
        ["--port", "9000", "--token", "abc", "--open"]
    )
    assert (host, port, token, open_browser) == ("127.0.0.1", 9000, "abc", True)

    host, port, token, open_browser = app.parse_serve_args([])
    assert (host, port, token, open_browser) == ("127.0.0.1", 8765, None, False)


def test_parse_serve_args_lan(app):
    """--lan shares the UI on the network and must always carry a token."""
    host, port, token, open_browser = app.parse_serve_args(["--lan", "--port", "9001"])
    assert host == "0.0.0.0"
    assert port == 9001
    assert isinstance(token, str) and len(token) >= 12, (
        "--lan must auto-generate a token so network sharing stays gated"
    )
    assert open_browser is False

    # An explicit token is honoured.
    host, _port, token, _open = app.parse_serve_args(["--lan", "--token", "mine"])
    assert host == "0.0.0.0"
    assert token == "mine"

    # Same auto-token guarantee for an explicit wildcard --host.
    host, _port, token, _open = app.parse_serve_args(["--host", "0.0.0.0"])
    assert host == "0.0.0.0"
    assert token

    # Loopback binds stay token-free.
    _host, _port, token, _open = app.parse_serve_args(["--host", "127.0.0.1"])
    assert token is None


def test_launch_menu_accepts_tui(app, monkeypatch):
    answers = iter(["9", "1"])
    monkeypatch.setattr("builtins.input", lambda _p: next(answers))
    assert app.launch_menu() == "tui"


def test_launch_menu_accepts_web_and_quit(app, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _p: "web")
    assert app.launch_menu() == "web"

    monkeypatch.setattr("builtins.input", lambda _p: "quit")
    assert app.launch_menu() == "quit"

    # EOF (closed stdin) falls back to web.
    def _eof(_p):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)
    assert app.launch_menu() == "web"


def test_frozen_no_console_starts_web(app, monkeypatch, tmp_path):
    """Double-click with no terminal -> web UI + browser, never TUI."""
    module = load_app("ytm_ll_dl_app_dispatch")
    monkeypatch.setattr(module, "IS_FROZEN", True)
    monkeypatch.setattr(module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(module, "ACCOUNTS_DIR", tmp_path / "accounts")
    monkeypatch.setattr(module, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(module, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(module, "CONFIG_FILE", tmp_path / "config" / "s.json")
    monkeypatch.setattr(module, "has_interactive_console", lambda: False)
    monkeypatch.setattr(module.sys, "argv", ["app"])

    called = {}
    monkeypatch.setattr(
        module, "start_web_ui",
        lambda host, port, token, open_browser: called.update(
            host=host, port=port, token=token, open_browser=open_browser
        ),
    )
    monkeypatch.setattr(
        module, "start_tui",
        lambda: called.update(tui=True),
    )

    module.main()

    assert called.get("tui") is not True
    assert called["host"] == "127.0.0.1"
    assert called["port"] == 8765
    assert called["open_browser"] is True
    # Data dirs were created under DATA_DIR.
    assert (tmp_path / "accounts").is_dir()


def test_frozen_menu_routes_tui(app, monkeypatch, tmp_path):
    module = load_app("ytm_ll_dl_app_dispatch2")
    monkeypatch.setattr(module, "IS_FROZEN", True)
    monkeypatch.setattr(module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(module, "ACCOUNTS_DIR", tmp_path / "accounts")
    monkeypatch.setattr(module, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(module, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(module, "CONFIG_FILE", tmp_path / "config" / "s.json")
    monkeypatch.setattr(module, "has_interactive_console", lambda: True)
    monkeypatch.setattr(module, "launch_menu", lambda: "tui")
    monkeypatch.setattr(module.sys, "argv", ["app"])

    started = {}
    monkeypatch.setattr(module, "start_tui", lambda: started.update(tui=True))
    monkeypatch.setattr(
        module, "start_web_ui",
        lambda *a: started.update(web=True),
    )

    module.main()
    assert started == {"tui": True}


def test_web_flag_starts_web_without_menu(app, monkeypatch, tmp_path):
    module = load_app("ytm_ll_dl_app_dispatch3")
    monkeypatch.setattr(module, "IS_FROZEN", True)
    monkeypatch.setattr(module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(module, "ACCOUNTS_DIR", tmp_path / "accounts")
    monkeypatch.setattr(module, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(module, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(module, "CONFIG_FILE", tmp_path / "config" / "s.json")
    monkeypatch.setattr(module.sys, "argv", ["app", "--web", "--port", "9999"])

    called = {}
    monkeypatch.setattr(
        module, "start_web_ui",
        lambda host, port, token, open_browser: called.update(port=port),
    )
    monkeypatch.setattr(module, "start_tui", lambda: called.update(tui=True))

    module.main()
    assert called == {"port": 9999}


# ---------------------------------------------------------------------------
# Browser-cookie account import (automatic sign-in)
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_app(tmp_path):
    """Fresh app module with all data dirs redirected into tmp_path."""
    module = load_app("ytm_ll_dl_app_isolated")
    module.PROJECT_DIR = tmp_path
    module.ACCOUNTS_DIR = tmp_path / "accounts"
    module.FILES_DIR = tmp_path / "files"
    module.CONFIG_DIR = tmp_path / "config"
    module.CONFIG_FILE = tmp_path / "config" / "settings.json"
    module.ensure_directories()
    return module


_HEADERS_RAW = "\n".join(
    [
        # Assembled rather than written out: a literal cookie assignment
        # here is indistinguishable from a real leaked header to the CI
        # secret scan (see .github/workflows/ci.yml).
        "cookie: VISITOR_INFO1_LIVE=abc; " + "__Secure-" + "3PAPISID=0f1c2d3e4a5b; SOCS=1",
        "x-goog-authuser: 0",
        "authorization: SAPISIDHASH 0",
        "x-origin: https://music.youtube.com",
    ]
)


@pytest.fixture(autouse=True)
def _no_live_probe(monkeypatch):
    """Keep browser-import tests hermetic: the signed-out pre-check
    probe reports 'unknown' unless a test overrides it."""
    import browsercookies

    monkeypatch.setattr(browsercookies, "header_is_live", lambda header: None)


def test_list_importable_browsers_detects_firefox(isolated_app, monkeypatch, tmp_path):
    """A fake Firefox profile with a cookie store is detected."""
    monkeypatch.setenv("HOME", str(tmp_path))
    profile = tmp_path / ".mozilla" / "firefox" / "abc123.default-release"
    profile.mkdir(parents=True)
    (profile / "cookies.sqlite").touch()
    browsers = {b["id"]: b for b in isolated_app.list_importable_browsers()}
    assert browsers["firefox"]["available"] is True
    # Chromium-family browsers are absent in this fake HOME.
    assert browsers["chrome"]["available"] is False


def test_import_account_from_browser_creates_account(isolated_app, monkeypatch):
    """Browser import flows through the same create_account() path as
    the manual paste: auth file + account.json + inventory."""
    import ytmusicapi

    class Dummy:
        def get_liked_songs(self, n=10000):
            return {"tracks": []}

    monkeypatch.setattr(ytmusicapi, "YTMusic", lambda *a, **k: Dummy())
    monkeypatch.setattr(
        "browsercookies.headers_raw_for",
        lambda browser, profile=None: _HEADERS_RAW,
    )

    account = isolated_app.import_account_from_browser("Browser Account", "firefox")
    assert account["slug"] == "browser_account"
    assert (isolated_app.ACCOUNTS_DIR / "browser_account" / ".headers_auth.json").exists()
    assert (isolated_app.ACCOUNTS_DIR / "browser_account" / "account.json").exists()
    assert "browser_account" in [a["slug"] for a in isolated_app.get_accounts()]


def test_import_account_from_browser_duplicate_raises(isolated_app, monkeypatch):
    import ytmusicapi

    class Dummy:
        def get_liked_songs(self, n=10000):
            return {"tracks": []}

    monkeypatch.setattr(ytmusicapi, "YTMusic", lambda *a, **k: Dummy())
    monkeypatch.setattr(
        "browsercookies.headers_raw_for",
        lambda browser, profile=None: _HEADERS_RAW,
    )
    isolated_app.import_account_from_browser("Dup Browser", "chrome")
    with pytest.raises(ValueError, match="already exists"):
        isolated_app.import_account_from_browser("Dup Browser", "chrome")


def test_import_account_from_browser_propagates_clear_error(isolated_app, monkeypatch):
    """Browser-level problems (no cookies, locked store, v20
    app-bound encryption) surface with their actionable message and
    leave no half-made account behind."""
    import browsercookies

    def boom(browser, profile=None):
        raise browsercookies.BrowserError("Close Chrome completely and try again.")

    monkeypatch.setattr("browsercookies.headers_raw_for", boom)
    with pytest.raises(ValueError, match="Close Chrome completely"):
        isolated_app.import_account_from_browser("Doomed", "chrome")
    assert list(isolated_app.ACCOUNTS_DIR.iterdir()) == []


def test_import_precheck_blocks_signed_out_session(isolated_app, monkeypatch):
    """A cookie set YouTube reports as signed out fails fast with the
    playback-works-while-signed-out explanation, before any account
    scaffolding is created."""
    import browsercookies

    monkeypatch.setattr(
        "browsercookies.headers_raw_for",
        lambda browser, profile=None: _HEADERS_RAW,
    )
    monkeypatch.setattr(browsercookies, "header_is_live", lambda header: False)
    with pytest.raises(ValueError, match="Playing music .* works while signed out"):
        isolated_app.import_account_from_browser("Dead Session", "chrome")
    assert list(isolated_app.ACCOUNTS_DIR.iterdir()) == []


def test_import_precheck_passes_when_live_or_unknown(isolated_app, monkeypatch):
    """Live (True) and inconclusive (None) probes both proceed with
    the normal import path."""
    import ytmusicapi

    class Dummy:
        def get_liked_songs(self, n=10000):
            return {"tracks": []}

    monkeypatch.setattr(ytmusicapi, "YTMusic", lambda *a, **k: Dummy())
    monkeypatch.setattr(
        "browsercookies.headers_raw_for",
        lambda browser, profile=None: _HEADERS_RAW,
    )
    for result, account_name in ((True, "Live Session"), (None, "Unknown Session")):
        monkeypatch.setattr(
            "browsercookies.header_is_live",
            lambda header, r=result: r,
        )
        account = isolated_app.import_account_from_browser(account_name, "chrome")
        assert account["slug"]


def test_account_creation_error_signed_out_is_actionable(isolated_app):
    """A signed-out session makes YouTube return a sign-in prompt page;
    the raw KeyError text is a wall of renderer JSON, so the mapped
    error must state the real reason and which profile was used."""
    raw = KeyError(
        "Unable to find 'twoColumnBrowseResultsRenderer' using path "
        "['contents', 'twoColumnBrowseResultsRenderer', ...]"
    )
    error = isolated_app.account_creation_error(
        raw,
        source="chrome profile 'Profile 10'",
    )
    text = str(error)
    assert "not signed in to YouTube Music" in text
    assert "sign-in prompt" in text
    assert "Profile 10" in text


def test_account_creation_error_other_keeps_detail_truncated(isolated_app):
    """Non-sign-in failures keep the underlying reason but never dump
    an unbounded wall of text."""
    error = isolated_app.account_creation_error(
        RuntimeError("network exploded " + "x" * 5000)
    )
    text = str(error)
    assert "Failed to create account" in text
    assert "network exploded" in text
    assert len(text) < 600
    assert "source" not in text


# ---------------------------------------------------------------------------
# OAuth token files must never be treated as yt-dlp auth
# ---------------------------------------------------------------------------


def _write_oauth_token_file(path):
    """Write the exact JSON shape RefreshingToken dumps to disk
    (verified: the six Token.members() keys)."""
    import json

    path.write_text(
        json.dumps(
            {
                "scope": "https://www.googleapis.com/auth/youtube",
                "token_type": "Bearer",
                "access_token": "at",
                "refresh_token": "rt",
                "expires_at": 9999999999,
                "expires_in": 3600,
            }
        )
    )
    return path


def test_read_auth_headers_is_empty_for_oauth_token_file(isolated_app, tmp_path):
    """An OAuth token JSON must never be parsed as HTTP headers — its
    fields would otherwise be forwarded to yt-dlp as bogus headers (and
    risk leaking the access/refresh tokens)."""
    token_file = _write_oauth_token_file(tmp_path / ".headers_auth.json")
    assert isolated_app._read_auth_headers(token_file) == {}


def test_read_auth_headers_still_parses_header_files(isolated_app, tmp_path):
    """The OAuth guard must not break browser-header accounts."""
    import json

    auth = tmp_path / ".headers_auth.json"
    auth.write_text(
        json.dumps(
            {
                "headers": {
                    "Cookie": "VISITOR_INFO1_LIVE=abc; SOCS=1",
                    "User-Agent": "Mozilla/5.0",
                }
            }
        )
    )
    headers = isolated_app._read_auth_headers(auth)
    assert headers.get("cookie") == "VISITOR_INFO1_LIVE=abc; SOCS=1"
    assert headers.get("user-agent") == "Mozilla/5.0"


def test_yt_dlp_auth_args_omit_cookies_for_oauth_account(isolated_app, tmp_path):
    """OAuth accounts have no cookies, so the args carry no --cookies.
    The worker only adds player_client=web_music when --cookies is
    present (unsigned web_music returns format-18/UNPLAYABLE), so this
    also pins the web_music gating."""
    token_file = _write_oauth_token_file(tmp_path / ".headers_auth.json")
    args = isolated_app._yt_dlp_auth_args(token_file, tmp_path / "cookies.txt")
    assert "--cookies" not in args
    # Tokens must not leak into headers either.
    assert not any("at" == a or "rt" == a for a in args)


def test_yt_dlp_auth_args_write_cookies_for_header_account(isolated_app, tmp_path):
    """A browser-header account with a Cookie header produces a
    --cookies jar (the verified-working download path)."""
    import json

    auth = tmp_path / ".headers_auth.json"
    auth.write_text(
        json.dumps({"headers": {"Cookie": "VISITOR_INFO1_LIVE=abc; SOCS=1"}})
    )
    cookie_file = tmp_path / "cookies.txt"
    args = isolated_app._yt_dlp_auth_args(auth, cookie_file)
    assert "--cookies" in args
    assert cookie_file.exists()
    jar = cookie_file.read_text()
    assert "VISITOR_INFO1_LIVE" in jar


def test_web_music_gated_on_cookies_in_worker_source(app):
    """Static guard: player_client=web_music may only be added inside
    the '--cookies' in auth_args check."""
    source = APP_PATH.read_text(encoding="utf-8")
    lines = source.splitlines()
    for i, line in enumerate(lines):
        if "youtube:player_client=web_music" in line:
            # Walk back to the enclosing `if` for this extend block.
            guard = "\n".join(lines[max(0, i - 8):i])
            assert '"--cookies" in auth_args' in guard, (
                "web_music must stay gated on cookie presence"
            )
            break
    else:
        raise AssertionError("expected a web_music player_client site")


def test_build_ytmusic_client_refuses_oauth_token_file(isolated_app, tmp_path):
    """Google OAuth account creation was removed (YouTube blocks every
    third-party OAuth token on its internal API). A hand-placed token
    file must not silently authenticate: ytmusicapi rejects it without
    an explicit oauth_credentials client."""
    from ytmusicapi.exceptions import YTMusicUserError

    token_file = _write_oauth_token_file(tmp_path / ".headers_auth.json")
    with pytest.raises(YTMusicUserError):
        isolated_app.build_ytmusic_client(token_file)


def test_build_ytmusic_client_keeps_header_accounts_unchanged(isolated_app, tmp_path):
    """Header accounts (flat browser-request shape with a SAPISIDHASH
    authorization, like the real .headers_auth.json files) must keep the
    plain YTMusic(auth) path — no oauth_credentials required."""
    import json

    auth = tmp_path / ".headers_auth.json"
    auth.write_text(
        json.dumps(
            {
                "cookie": (
                    "VISITOR_INFO1_LIVE=abc; "
                    + "__Secure-" + "3PAPISID=0f1c2d3e4a5b; SOCS=1"
                ),
                "authorization": (
                    "SAPISIDHASH 1700000000:"
                    "0f1c2d3e4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d"
                ),
                "user-agent": "Mozilla/5.0",
            }
        )
    )
    client = isolated_app.build_ytmusic_client(auth)
    assert client is not None
    assert client.auth_type.name == "BROWSER"


def test_cookieless_403_failure_detection_and_hint(isolated_app):
    """The cookieless-403 hint fires only for plain 403/Forbidden
    failures, never for premium/sign-in walls (which have their own
    handling), and the hint text itself must not be mistaken for a
    premium failure by is_premium_failure."""
    is_403 = isolated_app.is_cookieless_403_failure
    assert is_403("ERROR: unable to download video data: HTTP Error 403: Forbidden")
    assert is_403("HTTP Error 403: Forbidden")
    # Premium / sign-in walls keep their dedicated path.
    assert not is_403("Sign in to confirm you're not a bot")
    assert not is_403("This video is unavailable")
    assert not is_403("")
    assert not is_403("yt-dlp failed to produce the requested audio.")

    hinted = (
        "ERROR: unable to download video data: HTTP Error 403: Forbidden"
        " — " + isolated_app.COOKIELESS_403_HINT
    )
    # The hint must not flip the premium classification of the reason.
    assert not isolated_app.is_premium_failure(hinted)
    assert "signed-out" in isolated_app.COOKIELESS_403_HINT
