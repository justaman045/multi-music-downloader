"""
Headless tests for the SPA (web/static/app.js).

The SPA has no JS test harness, so these are static structural guards
against regressions that break client-side navigation or rendering.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


SPA_PATH = Path(__file__).resolve().parent.parent / "web" / "static" / "app.js"


def read_spa() -> str:
    return SPA_PATH.read_text(encoding="utf-8")


def extract_navigate(src: str) -> str:
    match = re.search(
        r"async function navigate\(\) \{(?P<body>.*?)\n\}",
        src,
        re.S,
    )
    assert match, "navigate() not found in app.js"
    return match.group("body")


def test_spa_has_navigate_fallback_guard():
    """Regression: every successful route render must return before the
    Home fallback. A missing return makes renderHome() overwrite whatever
    page the user clicked on (sidebar clicks appear to do nothing)."""
    body = extract_navigate(read_spa())

    block = re.search(r"if \(fn\) \{(?P<inner>.*?)\n    \}", body, re.S)
    assert block, "routes handler 'if (fn) {...}' block missing in navigate()"

    inner = block.group("inner")
    assert "await fn();" in inner
    assert "return;" in inner, (
        "renderHome() fallback is reachable after a successful route render — "
        "the 'if (fn)' branch must return so unknown routes are the only "
        "case that renders Home"
    )


def test_spa_render_home_is_fallback():
    """renderHome() must appear only as the unknown-route fallback."""
    body = extract_navigate(read_spa())
    fallbacks = re.findall(r"return renderHome\(\);", body)
    assert len(fallbacks) == 1


def test_spa_exposes_all_nav_routes():
    """Every sidebar nav route must have a registered renderer."""
    src = read_spa()
    routes_match = re.search(
        r"const routes = \{(?P<body>.*?)\n\};",
        src,
        re.S,
    )
    assert routes_match, "routes table not found in app.js"
    routes_body = routes_match.group("body")

    expected = {
        "home",
        "songs",
        "artists",
        "albums",
        "years",
        "genres",
        "most-played",
        "duplicates",
        "status",
        "downloads",
        "accounts",
        "settings",
    }
    registered = set(
        re.findall(r'^\s{2}"?([a-z0-9-]+)"?:\s*\w+', routes_body, re.M)
    )
    assert expected.issubset(registered), (
        f"missing route registrations: {sorted(expected - registered)}"
    )


def extract_function(src: str, name: str) -> str:
    match = re.search(
        rf"(?:async )?function {name}\((?P<body>.*?)\n\}}",
        src,
        re.S,
    )
    assert match, f"{name}() not found in app.js"
    return match.group("body")


def extract_function_balanced(src: str, name: str) -> str:
    """Extract a function body honoring nested braces, unlike the naive
    extract_function() which stops at the first standalone '}'."""
    match = re.search(rf"function {name}\(\) \{{", src)
    assert match, f"{name}() not found in app.js"
    start = match.end()
    depth = 1
    i = start
    while i < len(src) and depth:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{name}() braces unbalanced"
    return src[start : i - 1]


def test_spa_download_ui_gated_on_needs_download():
    """Download affordances must only appear for online-only tracks.

    Regression: the view-level Download button and per-row download control
    must be gated on needsDownload() so fully-local views show no download UI.
    """
    src = read_spa()

    table = extract_function(src, "trackTable")
    assert re.search(r"online\.length \? `<button id=\"download-view\"", table), (
        "trackTable must only render #download-view when online tracks exist"
    )

    rows = extract_function(src, "trackRowsHTML")
    assert re.search(r"needsDownload\(t\) \? `<button class=\"btn btn-ghost row-dl\"", rows), (
        "trackRowsHTML must render a per-row download control only for "
        "online-only tracks"
    )

    assert "const online = tracks.filter(needsDownload)" in table


def test_spa_no_manual_account_gate():
    """Downloading must not require manually selecting an account first.

    Regression: every download handler used to bail out with a toast asking
    the user to pick a single account. Downloads now target the account each
    track belongs to automatically.
    """
    src = read_spa()
    assert "Select a single account" not in src, (
        "manual account-selection gate still present in download handlers"
    )
    assert "Select a single account to download" not in src


def test_spa_auto_download_helpers():
    """The grouped auto-download helpers must exist."""
    src = read_spa()
    for helper in ("needsDownload", "groupByAccount", "startAutoDownload", "accountHasActiveDownload"):
        assert re.search(
            rf"(?:async )?function {helper}\(",
            src,
        ), f"{helper}() not defined in app.js"


def test_spa_rate_limit_setting_ui():
    """The rate-limit setting must be present in the Settings form and
    included in the save payload."""
    src = read_spa()
    assert 'id="set-rate-limit"' in src, "rate-limit input missing from Settings"
    assert "download_rate_limit:" in src, "rate-limit key missing from settings save payload"


def test_spa_verify_files_ui():
    """The Status page must offer file verification and the dialog must
    POST to /api/verify."""
    src = read_spa()
    assert "Verify files" in src, "verify button missing from Status page"
    assert '"/api/verify"' in src, "verify dialog must POST to /api/verify"


def test_spa_history_charts_ui():
    """The Stats page must fetch per-period history and render it."""
    src = read_spa()
    assert "/api/history?days=" in src, "history fetch missing from Stats page"
    assert "hist-range" in src, "history range selector missing"


def test_spa_multi_account_search():
    """YTM search must send the account list and annotate ownership."""
    src = read_spa()
    assert "accountsParam" in src, "search must send the accounts parameter"
    assert "owned_by" in src, "search results must annotate owned_by accounts"


def test_spa_bind_track_table_null_safe():
    """Regression: bindTrackTable() must tolerate containers without a
    tracks table (e.g. the Stats page, which renders stat cards only).
    Before the fix, unguarded $$('.row-dl', table) / $$('.row-pl', table)
    called null.querySelectorAll and crashed renderStats()."""
    src = read_spa()
    assert 'if (table) $$(".row-dl", table)' in src, ".row-dl binding must be guarded by table existence"
    assert 'if (table) $$(".row-pl", table)' in src, ".row-pl binding must be guarded by table existence"


def test_spa_play_report_live_updates_library():
    """reportPlay() must update state.library plays from the server response
    and re-render the aggregate views so counts appear without a reload."""
    src = read_spa()
    assert 'hit.plays = res.total' in src, "reportPlay() must write the server's total back into state.library"
    assert '["home", "stats"].includes(state.route)' in src, "reportPlay() must re-render home/stats after a recorded play"


def test_spa_play_recorded_sse_handler():
    """The SPA must handle the play_recorded SSE event by updating the
    library snapshot and re-rendering the active stats/home view."""
    src = read_spa()
    assert 'data.type === "play_recorded"' in src, "play_recorded SSE event handler missing"
    assert 'hit.plays = data.total' in src, "play_recorded handler must write the server total into state.library"


def test_spa_play_recorded_route_rerender():
    """The play_recorded handler must re-render the stats/home page when it
    is the active route (so an already-open Stats page updates live)."""
    src = read_spa()
    assert 'state.route === "stats" || state.route === "home"' in src, "play_recorded handler must re-render stats/home"


def test_spa_player_persist_helpers_exist():
    """The player snapshot helpers must exist so the last played queue can
    survive a page refresh / server restart."""
    src = read_spa()
    assert "function persistPlayer()" in src, "persistPlayer() not defined in app.js"
    assert "function restorePlayer()" in src, "restorePlayer() not defined in app.js"
    assert '"ls-player"' in src, "ls-player localStorage key must be used"


def test_spa_player_persists_identities_not_urls():
    """The persisted snapshot must store track identities, not token-baked
    stream/art URLs, so URLs can be re-derived from the fresh library."""
    src = read_spa()
    body = extract_function(src, "persistPlayer")
    assert "queue: state.player.queue.map((t) => ({ id: t.id, account: t.account }))" in body, (
        "persistPlayer() must store id/account identities only"
    )
    assert "index: state.player.index" in body
    assert "shuffle: state.player.shuffle" in body
    assert "repeat: state.player.repeat" in body


def test_spa_player_restore_resolves_from_library():
    """restorePlayer() must map persisted identities to fresh library tracks
    (with stream URLs) and rebuild the player without autoplaying."""
    src = read_spa()
    body = extract_function_balanced(src, "restorePlayer")
    assert "t.stream" in body, "restorePlayer() must only keep tracks that still have a stream"
    assert "state.player.queue = queue" in body
    assert "state.player.index = index" in body
    assert "audio.src = streamUrl(track)" in body
    assert "updatePlayerUI()" in body
    assert "audio.play()" not in body, "restorePlayer() must not autoplay"


def test_spa_player_restore_called_after_library_load():
    """init() must restore the player only after loadLibrary() so art/stream
    URLs carry the current token."""
    body = extract_function(read_spa(), "init")
    assert "restorePlayer();" in body, "init() must call restorePlayer()"
    assert body.index("await loadLibrary();") < body.index("restorePlayer();"), (
        "restorePlayer() must run after loadLibrary()"
    )


def test_spa_stop_playback_clears_snapshot():
    """stopPlayback() must clear the ls-player snapshot so a deliberate stop
    is not resurrected on the next refresh."""
    src = read_spa()
    body = extract_function(src, "stopPlayback")
    assert 'localStorage.removeItem("ls-player")' in body, (
        "stopPlayback() must clear the ls-player snapshot"
    )

