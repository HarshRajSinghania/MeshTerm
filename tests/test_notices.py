# SPDX-License-Identifier: Apache-2.0
"""Tests for ``packaging/notices.py``, the third-party notices generator.

Loaded by file path rather than as ``packaging.notices``: ``packaging`` is also the name
of a PyPI library that module itself imports (``packaging.markers``,
``packaging.requirements``), and the repository's ``packaging/`` directory is not a real
package — it has no ``__init__.py``, deliberately, so it can never shadow that library
(a regular package anywhere on ``sys.path`` always wins over a same-named directory with
no ``__init__.py``, which is what keeps ``import packaging.markers`` finding the real one
even with the repo root on the path). Reaching the module by file avoids relying on that
distinction holding forever.

Skips cleanly when ``mesh-term`` isn't installed in the interpreter running the tests —
the generator walks the *installed* dependency closure, so without an installed
distribution to walk there is nothing here to test.
"""

from __future__ import annotations

import importlib.util
import sys
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

import pytest
from packaging.requirements import Requirement

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on 3.10
    import tomli as tomllib

try:
    distribution("mesh-term")
except PackageNotFoundError:
    pytest.skip("mesh-term is not installed in this interpreter", allow_module_level=True)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "meshterm_packaging_notices", _REPO_ROOT / "packaging" / "notices.py"
)
assert _spec is not None and _spec.loader is not None
notices = importlib.util.module_from_spec(_spec)
# Registered before exec: `Notice` is a dataclass under `from __future__ import
# annotations`, and dataclasses resolves deferred annotations via `sys.modules[cls.
# __module__]` — a module never registered there fails with a confusing `NoneType has
# no attribute '__dict__'` the moment the class body runs, unrelated to anything this
# test is actually checking.
sys.modules[_spec.name] = notices
_spec.loader.exec_module(notices)

#: The copyleft licenses no *runtime* dependency of MeshTerm may carry. Checking for
#: "GPL" alone also catches LGPL and AGPL (both contain it as a substring); MPL needs
#: its own check. PyInstaller itself is GPL-with-bootloader-exception, and must never
#: appear here precisely because it is a build tool, not part of the runtime closure
#: this module walks — its absence from the generated notices is the thing this test
#: guards, not an oversight to work around.
_COPYLEFT_MARKERS = ("GPL", "MPL")


@pytest.fixture(scope="module")
def all_notices() -> list:
    """Every notice this interpreter's install of ``mesh-term`` would generate."""
    return notices.collect_notices()


def _direct_runtime_dependencies() -> list[Requirement]:
    """The ``[project.dependencies]`` MeshTerm itself declares, applicable here.

    Mirrors :func:`notices.dependency_closure`'s own marker evaluation (``extra`` forced
    to ``""``, meaning "no extras requested") so a dependency gated to a platform or
    Python version this interpreter isn't — the pre-3.11 ``tomli`` backport, one day a
    Windows- or macOS-only package — is excluded here exactly as it would be from the
    generated notices, rather than flagged as "missing".
    """
    from packaging.markers import default_environment

    environment = default_environment()
    environment["extra"] = ""
    pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = [Requirement(raw) for raw in pyproject["project"]["dependencies"]]
    return [req for req in requirements if req.marker is None or req.marker.evaluate(environment)]


def test_every_direct_dependency_has_a_notice(all_notices: list) -> None:
    """Every applicable ``pyproject.toml`` dependency turns up as a generated notice."""
    from packaging.utils import canonicalize_name

    covered = {canonicalize_name(n.name) for n in all_notices}
    missing = [
        req.name
        for req in _direct_runtime_dependencies()
        if canonicalize_name(req.name) not in covered
    ]
    assert not missing, f"no notice generated for: {missing}"


def test_every_notice_has_license_text(all_notices: list) -> None:
    """Nothing in the generated set carries empty or whitespace-only license text."""
    empty = [f"{n.name} {n.version}" for n in all_notices if not n.text.strip()]
    assert not empty, f"empty license text for: {empty}"


def test_python_entry_present_and_mentions_psf(all_notices: list) -> None:
    """Python's own PSF license is the first entry, and it is actually the PSF text."""
    assert all_notices, "no notices generated at all"
    python_entry = all_notices[0]
    assert python_entry.name == "Python"
    assert "PSF" in python_entry.summary or "PSF" in python_entry.text


def test_no_copyleft_dependency(all_notices: list) -> None:
    """No runtime dependency's declared license is GPL/LGPL/AGPL/MPL.

    MeshTerm's dependency closure is entirely MIT/BSD/ISC/PSF/Apache-2.0. PyInstaller
    itself is GPL-with-bootloader-exception but is a *build* tool, never a runtime
    dependency of the frozen app, so it must never appear in ``all_notices`` at all —
    this both confirms that and guards against a future dependency quietly adding one.
    """
    offenders = [
        f"{n.name} {n.version} ({n.summary})"
        for n in all_notices
        if any(marker in n.summary.upper() for marker in _COPYLEFT_MARKERS)
    ]
    assert not offenders, f"copyleft license found: {offenders}"


def test_output_is_deterministic() -> None:
    """Rendering the same closure twice produces byte-identical output."""
    first = notices.render(notices.collect_notices())
    second = notices.render(notices.collect_notices())
    assert first == second


def test_rendered_output_is_newline_terminated_utf8() -> None:
    """The rendered document ends with a single trailing newline and encodes as UTF-8."""
    rendered = notices.render(notices.collect_notices())
    assert rendered.endswith("\n")
    assert not rendered.endswith("\n\n")
    rendered.encode("utf-8")  # raises on anything that wouldn't round-trip


def test_write_third_party_notices_writes_utf8_lf(tmp_path: Path) -> None:
    """The file on disk matches ``render()`` exactly, with no newline translation."""
    out = notices.write_third_party_notices(tmp_path / "THIRD-PARTY-NOTICES.txt")
    raw = out.read_bytes()
    assert b"\r\n" not in raw
    assert raw.decode("utf-8") == notices.render(notices.collect_notices())
