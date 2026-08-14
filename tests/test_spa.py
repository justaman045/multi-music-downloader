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
