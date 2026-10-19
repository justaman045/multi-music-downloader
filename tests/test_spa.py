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



def test_spa_mobile_nav_drawer_wired():
    """Regression: below 900px the sidebar is the only navigation. The old
    CSS did `#sidebar { display: none }` with no replacement, leaving small
    screens with no way to change view. The drawer must exist, be toggled
    from the hamburger, close on backdrop/Escape/navigation, and the CSS
    must not hide the sidebar outright."""
    src = read_spa()

    for needle in (
        "function openNav()",
        "function closeNav()",
        "function wireNavDrawer()",
        'document.body.classList.add("nav-open")',
        '$("#nav-backdrop")',
        'window.addEventListener("hashchange", closeNav)',
    ):
        assert needle in src, f"mobile nav drawer wiring missing: {needle}"

    # init() must wire the drawer.
    init_match = re.search(r"async function init\(\) \{(?P<body>.*?)\n\}", src, re.S)
    assert init_match, "init() not found"
    assert "wireNavDrawer();" in init_match.group("body")

    # Escape closes the drawer (the anonymous listener with the Escape key).
    keydown = re.search(
        r'document\.addEventListener\("keydown", \(e\) => \{.*?\n\}', src, re.S
    )
    assert keydown and "closeNav();" in keydown.group(0)

    css = (
        Path(__file__).resolve().parent.parent
        / "web" / "static" / "styles.css"
    ).read_text(encoding="utf-8")
    assert "#sidebar { display: none; }" not in css, (
        "styles.css hides the sidebar below 900px again — mobile users "
        "would lose all navigation"
    )
    assert "body.nav-open #sidebar" in css
    assert ".nav-toggle { display: inline-grid; }" in css


def test_spa_duplicate_keep_scoped_to_group():
    """Regression: 'Keep first copy' must only remove copies inside the
    group the button belongs to. Flattening every group of the account
    deleted tracks from unrelated duplicate groups too."""
    src = read_spa()
    handler = re.search(
        r'\$\$\("\[data-within-keep\]", root\)\.forEach\(\(btn\) => \{.*?\n  \}\);',
        src,
        re.S,
    )
    assert handler, "within-keep handler not found"
    body = handler.group(0)
    assert "dataset.groupIdx" in body
    assert "grp.tracks.filter" in body
    assert "flatMap((g) => g.tracks).filter" not in body, (
        "keep-first-copy still computes candidates across ALL groups"
    )
    # The button carries its group index.
    assert 'data-group-idx="${gi}"' in src


def test_spa_settings_cached_before_events():
    """Regression: notify() gates on state.settings.notifications_enabled,
    but state.settings was only ever set by the settings-save path —
    desktop notifications could never fire on a fresh page load."""
    src = read_spa()
    init_match = re.search(r"async function init\(\) \{(?P<body>.*?)\n\}", src, re.S)
    assert init_match, "init() not found"
    body = init_match.group("body")
    setting_pos = body.find("state.settings = await api(\"/api/settings\")")
    assert setting_pos != -1, "init() never caches /api/settings"
    library_pos = body.find("await loadLibrary()")
    assert 0 <= setting_pos < library_pos, (
        "settings must be cached before loadLibrary() so early download/"
        "refresh SSE events can consult notifications_enabled"
    )


def test_spa_tag_editor_modal():
    """The SPA must expose an Edit-tags modal that posts to /api/tags with
    every tag field, reachable from a per-row button."""
    src = read_spa()
    assert "async function editTagsModal(" in src
    assert 'api("/api/tags"' in src
    assert ".row-tags" in src
    for field in ("tg-title", "tg-artist", "tg-album", "tg-genre", "tg-year"):
        assert field in src, f"tag editor field missing: {field}"


def test_spa_smart_views_rule_engine():
    """Smart views must have a persisted rule engine: built-ins + saved
    views (localStorage), a filter/sort/limit applier, and a builder bar
    wired into the Songs route."""
    src = read_spa()
    for needle in (
        "const SV_BUILTINS",
        "function applySmartRules(",
        'localStorage.getItem("ls-smart-views")',
        'localStorage.setItem("ls-smart-view-active"',
        "function smartViewBuilderHTML(",
        "async function saveSmartViewDialog(",
        "function wireSmartViewBar(",
        'id="sv-select"',
        'id="sv-save"',
    ):
        assert needle in src, f"smart-view code missing: {needle}"

    # renderSongs must apply the rules (not just render all tracks).
    match = re.search(r"async function renderSongs\(\) \{(?P<body>.*?)\n\}", src, re.S)
    assert match, "renderSongs() not found"
    body = match.group("body")
    assert "applySmartRules(base, state.smartRules)" in body
    assert "wireSmartViewBar(base);" in body


def test_spa_stats_insights_and_scrobble_panel():
    """Stats view must render insight cards from the server-computed
    streaks/week fields, an hour-of-day chart from per_hour, and a last.fm
    panel with a working Flush button."""
    src = read_spa()
    for needle in (
        "h.streaks",
        "h.week",
        "h.per_hour",
        'id="insights-view"',
        'id="scrobble-panel"',
        'api("/api/lastfm")',
        'api("/api/lastfm/flush"',
        'id="lfm-flush"',
        "Listening by hour of day",
        "Day listening streak",
    ):
        assert needle in src, f"stats insight code missing: {needle}"

    # Server side: history_stats must return the fields the SPA reads.
    plays_src = (
        Path(__file__).resolve().parent.parent / "web" / "plays.py"
    ).read_text(encoding="utf-8")
    for needle in (
        "_listening_streaks",
        "_week_compare",
        '"per_hour"',
        '"streaks"',
        '"week"',
    ):
        assert needle in plays_src, f"plays.py insight field missing: {needle}"


def test_spa_export_sync_ui():
    """Settings must offer library export (M3U/CSV/JSON) and folder sync
    (target + hardlink/copy + dry run) wired to the new endpoints."""
    src = read_spa()
    for needle in (
        'api("/api/export/library"',
        'api("/api/sync"',
        'id="set-exp-m3u"',
        'id="set-exp-csv"',
        'id="set-exp-json"',
        'id="set-sync-run"',
        'id="set-sync-dry"',
        'id="set-sync-target"',
        "dry_run: dryRun",
        '"ls-sync-target"',
    ):
        assert needle in src, f"export/sync UI missing: {needle}"


def test_spa_download_queue_and_scheduler_ui():
    """The Downloads view must show a persistent queue panel (add via +Q
    row buttons, start with a quality dialog, per-item remove, clear,
    auto-start toggle) and a scheduler panel with Run-now buttons."""
    src = read_spa()
    for needle in (
        "async function renderQueuePanel(",
        "async function renderSchedulerPanel(",
        'api("/api/queue"',
        'api("/api/queue/start"',
        'api("/api/queue/auto"',
        'api("/api/queue/clear"',
        'api("/api/queue/remove"',
        'api("/api/scheduler"',
        "row-queue",
        ">Start queue</button>",
        "sched-run",
        '"queue_started"',
        "function fmtAgo(",
    ):
        assert needle in src, f"queue/scheduler UI code missing: {needle}"

    # The +Q button must post to the queue endpoint from its row handler.
    handler = re.search(
        r'\$\$\("\.row-queue", table\)\.forEach\(\(btn\) => \{.*?\n  \}\);',
        src,
        re.S,
    )
    assert handler, "row-queue click handler missing"
    assert 'api("/api/queue"' in handler.group(0)

    # Scheduler must be ticked with the queue auto-starter.
    sched = (
        Path(__file__).resolve().parent.parent / "web" / "scheduler.py"
    ).read_text(encoding="utf-8")
    assert "self._queue_auto_start(settings)" in sched
    assert "start_queued_batches" in sched


def test_spa_responsive_columns_and_chart_css():
    """The narrow-screen layout relies on styles.css: secondary track
    columns must be hidden below 900/620px (rows were unreadable on
    phones), and the insights bar charts ship their own classes — which
    shipped unstyled once, so assert they are defined."""
    css_path = Path(__file__).resolve().parent.parent / "web" / "static"
    css = (css_path / "styles.css").read_text(encoding="utf-8")
    src = read_spa()

    # Column classes exist on both <th> and <td> so CSS can hide them.
    for col in ("col-album", "col-year", "col-duration", "col-plays",
                "col-quality", "col-queue", "col-index"):
        assert f'class="{col}"' in src, f"track cell class {col} missing"
        count = src.count(f'class="{col}"')
        assert count >= 2, f"{col} must be on its <td> and <th> (found {count})"
    assert ".col-album, .col-year, .col-duration { display: none; }" in css
    # Phones keep the quality badge (it doubles as the download-state dot,
    # useful in a downloader) and drop the index/plays/playlist/tags columns.
    assert ".col-index, .col-plays, .col-pl, .col-tags { display: none; }" in css

    # Bar charts (insights view) must have real styles.
    for cls in (".bar-chart {", ".bar-col {", ".bar-fill {", ".bar-label {"):
        assert cls in css, f"styles.css missing {cls}"

    # Topbar title must truncate instead of pushing the search button off.
    assert "text-overflow: ellipsis" in css


def test_spa_mobile_shell_uses_dvh_and_safe_areas():
    """The app shell must size against the dynamic viewport (100dvh) so the
    player bar is never hidden behind the mobile URL bar, and it must respect
    the iOS home-indicator / status-bar insets."""
    css = (
        Path(__file__).resolve().parent.parent / "web" / "static" / "styles.css"
    ).read_text(encoding="utf-8")
    assert "height: 100dvh;" in css, "app shell must use 100dvh for mobile chrome"
    assert "env(safe-area-inset-bottom)" in css, "player/drawers must respect the home-indicator inset"
    assert "env(safe-area-inset-top)" in css, "sidebar must respect the status-bar inset"
    assert "overscroll-behavior: none" in css


def test_spa_queue_drawer_is_styled():
    """Regression: the 'Up next' drawer markup shipped with no CSS at all, so
    the queue button appeared to do nothing. Its panel, header, list and items
    must be defined, along with the dismiss backdrop."""
    css = (
        Path(__file__).resolve().parent.parent / "web" / "static" / "styles.css"
    ).read_text(encoding="utf-8")
    index = (
        Path(__file__).resolve().parent.parent / "web" / "static" / "index.html"
    ).read_text(encoding="utf-8")
    for needle in (".queue-drawer {", ".queue-head {", ".queue-title {",
                   ".queue-list {", ".queue-item {", ".queue-pos {",
                   ".queue-meta {", "#queue-backdrop {"):
        assert needle in css, f"styles.css missing queue drawer rule: {needle}"
    assert 'id="queue-backdrop"' in index
    src = read_spa()
    for needle in ("function closeQueueDrawer()",
                   '$("#queue-backdrop")',
                   "closeQueueDrawer();"):
        assert needle in src, f"queue drawer wiring missing: {needle}"


def test_spa_row_actions_are_compact():
    """Row action buttons are .btn.btn-ghost by markup; the stylesheet must
    shrink them to icon-sized circles or they render as huge pills that make
    the song table unusable on a phone."""
    css = (
        Path(__file__).resolve().parent.parent / "web" / "static" / "styles.css"
    ).read_text(encoding="utf-8")
    for cls in ("table.tracks .row-dl", "table.tracks .row-pl",
                "table.tracks .row-queue", "table.tracks .row-tags"):
        assert cls in css, f"styles.css missing compact row action rule for {cls}"
    assert "border-radius: 50%;" in css


def test_spa_topbar_actions_are_a_row():
    """Regression: the topbar icon buttons are display:grid, but their wrapper
    .topbar-left had no CSS — so each button became a block-level box and
    stacked vertically, producing a ~120px-tall topbar instead of ~60px."""
    css = (
        Path(__file__).resolve().parent.parent / "web" / "static" / "styles.css"
    ).read_text(encoding="utf-8")
    assert ".topbar-left {" in css, ".topbar-left must be styled as a flex row"
    block = css[css.index(".topbar-left {"):]
    block = block[: block.index("}")]
    assert "display: flex;" in block, ".topbar-left must be display:flex"
    assert "align-items: center;" in block


def test_spa_mobile_touch_targets():
    """Phones need finger-sized controls: the nav drawer rows, topbar icon
    buttons and player controls must be enlarged in the narrow-screen rules."""
    css = (
        Path(__file__).resolve().parent.parent / "web" / "static" / "styles.css"
    ).read_text(encoding="utf-8")
    narrow = css[css.index("@media (max-width: 900px)"):]
    assert ".nav-item { min-height: 44px; }" in narrow, "drawer nav rows must be 44px tall"
    assert ".icon-btn { width: 40px; height: 40px;" in narrow, "topbar icon buttons must grow on mobile"
    phone = css[css.index("@media (max-width: 620px)"):]
    assert ".ctl-btn { width: 40px; height: 40px;" in phone, "player controls must grow on phones"
    assert ".play-btn { width: 50px; height: 50px;" in phone



    """Playing a song must update the browser tab title (with a play marker),
    show the album art in the tab (dynamic favicon) and drive the OS /
    lock-screen media controls via the Media Session API."""
    src = read_spa()
    for needle in (
        "function updateNowPlayingMeta(",
        "function updateDocumentTitle()",
        "function setFaviconFromArt(",
        "function applyMediaSession(",
        "function wireMediaSession()",
        '"mediaSession" in navigator',
        "new MediaMetadata(",
        "navigator.mediaSession.setActionHandler",
        '"nexttrack"',
        '"previoustrack"',
        "document.title = ",
        'document.title = "LocalSpot"',
        "canvas.toDataURL(",
    ):
        assert needle in src, f"now-playing cosmetics missing: {needle}"

    # The favicon link must carry the id the SPA swaps out.
    index = (
        Path(__file__).resolve().parent.parent / "web" / "static" / "index.html"
    ).read_text(encoding="utf-8")
    assert 'id="favicon"' in index
    assert 'name="theme-color"' in index
    assert "viewport-fit=cover" in index
    assert "<title>LocalSpot</title>" in index

    # updatePlayerUI() is the single choke point that keeps the tab in sync.
    body = extract_function_balanced(src, "updatePlayerUI")
    assert "updateNowPlayingMeta(track)" in body, (
        "updatePlayerUI() must refresh the now-playing cosmetics"
    )

    # wirePlayer() must install the media-session action handlers once.
    wire = extract_function_balanced(src, "wirePlayer")
    assert "wireMediaSession();" in wire

    # The lock-screen scrubber needs duration + position, fed on timeupdate.
    assert "function updateMediaPositionState(" in src
    assert "navigator.mediaSession.setPositionState" in src
    timeupdate = src[src.index('audio.addEventListener("timeupdate"'):]
    assert "updateMediaPositionState();" in timeupdate[:600], (
        "timeupdate must feed the media-session position state"
    )

    # The queue drawer scrolls the now-playing row into view when opened.
    drawer = extract_function_balanced(src, "renderQueueDrawer")
    assert 'scrollIntoView({ block: "nearest" })' in drawer


def test_spa_view_has_content_transition():
    """A subtle fade-up on the rendered view keeps navigation feeling
    polished; the keyframe must exist and be applied to #view children."""
    css = (
        Path(__file__).resolve().parent.parent / "web" / "static" / "styles.css"
    ).read_text(encoding="utf-8")
    assert "@keyframes fade-up {" in css
    assert "#view > * { animation: fade-up 0.24s ease both; }" in css



# ---------------------------------------------------------------------------
# Account sign-in: browser-cookie import (no OAuth)
# ---------------------------------------------------------------------------


def test_spa_accounts_panel_offers_browser_import():
    """The add-account panel must offer browser-cookie import (the
    automatic sign-in path) and carry an honest note explaining why
    Google OAuth sign-in is gone — never an OAuth button."""
    src = read_spa()
    assert 'id="acc-import"' in src
    assert 'id="acc-browser"' in src
    assert 'id="acc-profile"' in src
    assert "acc-oauth" not in src
    assert "oauth" not in src.lower()
    # The honest explanation replaces the removed OAuth button.
    assert "Google blocks third-party apps" in src


def test_spa_profile_picker_follows_selected_browser():
    """The profile picker is a <select> repopulated from the selected
    browser's profiles; the empty value means auto-detect."""
    src = read_spa()
    assert '<select id="acc-profile"' in src
    assert 'Auto-detect profile' in src
    assert "populateProfiles" in src
    assert "select.onchange = populateProfiles" in src
    assert "entry.profiles" in src


def test_spa_import_handler_posts_browser_body():
    """The import button must POST {name, browser, profile} to
    /api/accounts and refresh the library on success."""
    src = read_spa()
    handler = re.search(
        r'const importBtn = \$\("#acc-import"\);(?P<body>.*?)\n  \}',
        src,
        re.S,
    )
    assert handler, "browser import handler missing from renderAccounts()"
    body = handler.group("body")
    assert 'method: "POST"' in body
    assert "/api/accounts" in body
    assert "browser" in body
    assert "await loadLibrary();" in body


def test_spa_loads_detected_browsers():
    """renderAccounts() must populate the picker from /api/browsers and
    preselect the first available browser."""
    src = read_spa()
    assert "/api/browsers" in src
    handler = re.search(
        r'api\("/api/browsers"\)\.then\(\(browsers\) => \{(?P<body>.*?)\n  \}\)',
        src,
        re.S,
    )
    assert handler, "browser list population missing"
    body = handler.group("body")
    assert "available" in body
    assert "disabled" in body


def test_spa_no_fabricated_stream_urls():
    """Regression: clicking a song name in some views did nothing (or played
    the wrong track) because rows were built with a fabricated `stream: true`
    flag and no data-index. Every playable row must carry the library's real
    stream URL (or null, which now toasts "isn't downloaded yet")."""
    src = read_spa()
    assert "stream: true" not in src, (
        "fabricated stream flag still present — playable rows must use the "
        "library's real stream URL"
    )
    body = extract_function_balanced(src, "renderStats")
    assert 'data-index="${i}"' in body, (
        "history track rows must expose data-index so a click maps to the "
        "clicked track"
    )
    assert "byKey.get(" in body, (
        "history rows must resolve real library tracks for playback"
    )


def test_spa_set_queue_guards_undownloaded_tracks():
    """Regression: setQueue() dropped the clicked track from the queue and
    silently started the first playable track instead, so clicking an
    undownloaded song played a different one. It must bail out with a toast
    before mutating player state."""
    src = read_spa()
    match = re.search(
        r"function setQueue\(tracks, startIndex = 0\) \{(?P<body>.*?)\n\}",
        src,
        re.S,
    )
    assert match, "setQueue() not found in app.js"
    body = match.group("body")
    assert "!clicked.stream" in body, (
        "setQueue() must detect a clicked track that has no downloaded file"
    )
    assert "isn't downloaded yet" in body, (
        "setQueue() must explain why nothing played"
    )
    assert body.index("isn't downloaded yet") < body.index(
        "state.player.queue = queueFor(tracks)"
    ), "the not-downloaded guard must run before player state is mutated"


def test_spa_stream_url_applies_token_at_use_time():
    """Regression: stream URLs were token-baked only at library-load time, so
    entering the API token later (via the 401 prompt) left playback 401ing.
    streamUrl() must apply the token when a track is actually played."""
    src = read_spa()
    match = re.search(r"function streamUrl\(track\) \{(?P<body>.*?)\n\}", src, re.S)
    assert match, "streamUrl() not found in app.js"
    body = match.group("body")
    assert 'searchParams.set("token", state.token)' in body, (
        "streamUrl() must add the API token at playback time"
    )
    assert 'searchParams.set("format", "opus")' in body, (
        "streamUrl() must keep the FLAC→Opus fallback"
    )


def test_spa_audio_error_names_track():
    """The audio error toast must name the failed track and hint at a fix
    instead of a bare 'Could not play this file'."""
    src = read_spa()
    assert "Couldn't play" in src, "audio error toast must name the track"
    assert "state.player.queue[state.player.index]" in src, (
        "audio error toast must reference the current track"
    )


def test_spa_search_rows_toast_undownloaded():
    """Clicking an undownloaded song in search must give feedback instead of
    closing the overlay and silently doing nothing."""
    src = read_spa()
    start = src.index("function renderSearchResults")
    idx = src.index(".search-row[data-type=song]", start)
    window = src[idx : idx + 700]
    assert "isn't downloaded yet" in window, (
        "search rows must toast when the clicked song isn't downloaded"
    )


def test_spa_home_grid_guards_undownloaded():
    """Regression: clicking an undownloaded home card played the first
    playable track instead of the one clicked. The card handler must check
    the track's stream before queueing."""
    src = read_spa()
    assert "if (t && !t.stream) {" in src, (
        "home grid card handler must guard undownloaded tracks"
    )


def test_spa_play_reported_flag_normalized():
    """restorePlayer() must reset state._playReported (the property playIndex
    and reportPlay actually use), not state.player._playReported."""
    body = extract_function_balanced(read_spa(), "restorePlayer")
    assert "state._playReported = false" in body
    assert "state.player._playReported" not in body, (
        "restorePlayer() must not set the wrong flag object"
    )


def test_spa_captures_token_from_url():
    """A LAN share link carries ?token=... so another device signs in on
    open. init() must capture it (persisting it and stripping it from the
    address bar) before any API call."""
    src = read_spa()
    assert "function captureUrlToken()" in src, "captureUrlToken() not defined"
    body = extract_function_balanced(src, "captureUrlToken")
    assert "URLSearchParams(location.search)" in body, (
        "captureUrlToken() must read the URL query string"
    )
    assert 'params.get("token")' in body
    assert 'localStorage.setItem("ls-token"' in body, (
        "captureUrlToken() must persist the token"
    )
    assert "history.replaceState" in body, (
        "captureUrlToken() must strip the token from the address bar"
    )

    init = extract_function_balanced(src, "init")
    assert "captureUrlToken();" in init, "init() must call captureUrlToken()"
    assert init.index("captureUrlToken();") < init.index("await loadLibrary();"), (
        "the URL token must be captured before the library is fetched"
    )


def test_spa_api_reprompts_on_401_with_stale_token():
    """A wrong/stale token (e.g. an old LAN link) must be correctable: api()
    prompts on any 401, not only when no token is set."""
    body = extract_function(read_spa(), "api")
    assert "res.status === 401 && !state._tokenPrompting" in body, (
        "api() must prompt on 401 even when a token is already present"
    )


def test_spa_token_input_is_mobile_safe():
    """The token field must not be mangled by a phone keyboard or password
    manager (which would make a correct token fail repeatedly), and a
    rejected token must not be re-prefilled (tapping Save would resend it)."""
    body = extract_function(read_spa(), "promptToken")
    assert 'autocomplete="off"' in body, "token input must not be autofilled"
    assert 'autocapitalize="none"' in body, "phone keyboards must not capitalize it"
    assert 'autocorrect="off"' in body
    assert 'spellcheck="false"' in body
    assert "rejected" in body, "a rejected token must be cleared from the input"
    assert 'rejected ? "" : ' in body, (
        "keep a prefilled token only when it was not just rejected"
    )


def test_spa_modal_enter_submits_primary_input():
    """Enter in a single-line input submits the primary action, so the token
    prompt works from a phone keyboard — but never fires for button focus
    (confirm dialogs stay safe)."""
    body = extract_function(read_spa(), "modal")
    assert "root.onkeydown" in body
    assert '"Enter"' in body
    assert 'tagName !== "INPUT"' in body, (
        "Enter must only auto-submit from text inputs, not buttons"
    )
    assert "btn-primary" in body


def test_spa_api_reconnects_events_after_token_entry():
    """After a successful re-auth the SSE stream must be rebuilt: it was
    opened before the token existed (or with a stale one) and would 401
    forever otherwise. A rejected token must also say so."""
    body = extract_function(read_spa(), "api")
    assert "reauthed" in body, "api() must track whether the retry succeeded"
    assert "connectEvents()" in body, (
        "api() must rebuild the SSE stream once authenticated"
    )
    assert "Token rejected" in body, (
        "a still-rejected token must surface a clear message"
    )


def test_spa_connect_events_is_reentrant():
    """connectEvents() must tear down any previous stream and polling timer
    so entering a token doesn't leave two EventSources fighting a 401."""
    body = extract_function_balanced(read_spa(), "connectEvents")
    assert "state._events" in body
    assert "state._events.close()" in body
    assert "clearInterval(state._pollTimer)" in body
    assert "state._pollTimer = pollTimer" in body
