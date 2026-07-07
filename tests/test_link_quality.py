"""Tests for link-quality regression detection against a rolling baseline."""

from __future__ import annotations

from datetime import timedelta

from meshterm.core.models import Hop, TraceResult, utcnow
from meshterm.services import link_quality

_NOW = utcnow()


def _trace(snr: float, age_s: int, *, success: bool = True, hop: str = "3d") -> TraceResult:
    """Build a two-hop trace; bottleneck SNR is ``snr`` and it is ``age_s`` seconds old."""
    return TraceResult(
        target="Alice",
        success=success,
        hops=[Hop(0, hop, snr), Hop(1, None, snr + 1)] if success else [],
        timestamp=_NOW - timedelta(seconds=age_s),
    )


def test_detects_snr_regression() -> None:
    """A recent SNR drop past the threshold is flagged, with critical severity."""
    # Baseline (older) ~+8 dB, recent (newer) ~+2 dB: a 6 dB drop.
    baseline = [_trace(8.0, age_s=100 + i) for i in range(5)]
    recent = [_trace(2.0, age_s=i) for i in range(3)]
    report = link_quality.analyze_target("Alice", baseline + recent, recent_count=3)

    assert report.status == "critical"  # 6 dB drop ≥ 2× the 3 dB threshold
    snr_reg = next(c for c in report.regressions if c.metric == "min SNR (dB)")
    assert snr_reg.baseline == 8.0
    assert snr_reg.recent == 2.0
    assert snr_reg.delta == -6.0
    # The per-hop link also regressed.
    assert any(c.kind == "link" and c.regressed for c in report.regressions)


def test_healthy_link_reports_ok() -> None:
    """A steady link shows no regressions and an ok status."""
    traces = [_trace(7.5, age_s=i) for i in range(8)]
    report = link_quality.analyze_target("Alice", traces, recent_count=3)
    assert report.status == "ok"
    assert report.regressions == []
    assert report.changes  # metrics were still evaluated


def test_success_rate_regression() -> None:
    """A jump in recent failures is flagged even when surviving traces look fine."""
    baseline = [_trace(7.0, age_s=100 + i) for i in range(5)]
    recent = [_trace(7.0, age_s=i, success=False) for i in range(3)]
    report = link_quality.analyze_target("Alice", baseline + recent, recent_count=3)

    success_reg = next(c for c in report.regressions if c.metric == "success rate")
    assert success_reg.baseline == 1.0
    assert success_reg.recent == 0.0
    assert report.status == "critical"


def test_insufficient_history() -> None:
    """Too little baseline data yields an insufficient-data verdict, not a false alarm."""
    traces = [_trace(8.0, age_s=i) for i in range(3)]  # only 3 total, < min baseline + recent
    report = link_quality.analyze_target("Alice", traces, recent_count=3, min_baseline=3)
    assert report.status == "insufficient-data"
    assert report.changes == []


def test_links_only_compared_when_present_in_both_windows() -> None:
    """A path that appears in only one window is not reported as a link regression."""
    # Baseline goes via hop 'aa'; recent via a different hop 'bb' — no shared link.
    baseline = [_trace(8.0, age_s=100 + i, hop="aa") for i in range(5)]
    recent = [_trace(8.0, age_s=i, hop="bb") for i in range(3)]
    report = link_quality.analyze_target("Alice", baseline + recent, recent_count=3)
    link_changes = [c for c in report.changes if c.kind == "link"]
    # Only the shared 'X → us' return links overlap; the 'us → aa'/'us → bb' legs don't.
    assert all("aa" not in c.subject or "bb" not in c.subject for c in link_changes)
    assert report.status == "ok"


def test_resolver_names_link_subjects() -> None:
    """A resolver renames hop hashes in link subjects for readable output."""
    baseline = [_trace(8.0, age_s=100 + i) for i in range(5)]
    recent = [_trace(2.0, age_s=i) for i in range(3)]
    report = link_quality.analyze_target(
        "Alice",
        baseline + recent,
        recent_count=3,
        resolve=lambda h: "Repeater" if h == "3d" else h,
        device_label="Me",
    )
    link_regs = [c for c in report.regressions if c.kind == "link"]
    assert any("Repeater" in c.subject for c in link_regs)
    assert any("Me" in c.subject for c in link_regs)
