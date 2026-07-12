"""Shared-widget helper tests: the canonical formatting primitives every screen leans on.

Most widgets are exercised through their host screens' tests; what lives here are the
pure text helpers whose exact output *is* the app-wide convention — get these right once
and every caller inherits it.
"""

from __future__ import annotations

from meshterm.ui.widgets import _format_age, format_ago


def test_format_age_is_the_bare_column_form() -> None:
    """The lane form stays suffix-free at every magnitude, for aligned age columns."""
    assert _format_age(None) == "never"
    assert _format_age(5) == "now"
    assert _format_age(90) == "1m"
    assert _format_age(7200) == "2h"
    assert _format_age(180000) == "2d"
    assert _format_age(1300000) == "2w"


def test_format_ago_speaks_grammatical_prose() -> None:
    """The prose form says "5m ago" but bare "now"/"never" — never "now ago"."""
    assert format_ago(5) == "now"
    assert format_ago(None) == "never"
    assert format_ago(90) == "1m ago"
    assert format_ago(7200) == "2h ago"
