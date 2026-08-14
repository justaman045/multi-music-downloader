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
