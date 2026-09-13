# SPDX-License-Identifier: Apache-2.0
"""Builds ``THIRD-PARTY-NOTICES.txt``: every license the frozen build has to carry.

A one-file PyInstaller build is a *copy* of MeshTerm and of everything MeshTerm imports
at runtime — Apache-2.0 §4(a)/(d) want MeshTerm's own LICENSE and NOTICE distributed
with every such copy, and each MIT/BSD/PSF dependency wants its own notice kept with
copies too. Before this module existed, the spec (``packaging/meshterm.spec``) bundled
neither: the five installers carried the bundled font's license and nothing else.

This is deliberately a *generator*, not a checked-in file: a checked-in copy invites
drift the moment a dependency changes hands or its license text, silently, the next time
someone bumps a version pin. It runs at build time (see the spec) and again in
:mod:`tests.test_notices`, walking the actual runtime dependency closure of the
``mesh-term`` distribution installed in *this* interpreter — so a build's notices always
match what that build actually bundled, on whichever platform built it. A dependency
only one platform needs (a bleak backend, ``tomli`` below 3.11) shows up only in that
platform's notices, because it was never in the other platforms' closures to begin with.

Two things get no exception:

* A dependency with no discoverable license file fails the whole build, loudly, rather
  than shipping quietly without one — see :data:`_VENDORED_LICENSE_GAPS` for the one
  currently-known exception, and why it is a documented fix rather than a silent one.
* Python's own PSF license is always the first entry, found the same way the ``license``
  builtin finds it (reusing its own candidate-file search, not re-deriving the layout it
  already knows) — venv or not, Windows or POSIX, a future interpreter layout it wasn't
  written against still resolves correctly, or fails loudly instead of guessing wrong.
"""

from __future__ import annotations

import builtins
import platform
import sys
from dataclasses import dataclass
from importlib.metadata import Distribution, PackagePath, distribution
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

#: The distribution this build actually ships — see ``pyproject.toml``. Not a parameter
#: callers are expected to vary: a notices file for a *different* distribution's closure
#: would not describe what this build bundles.
ROOT_DISTRIBUTION = "mesh-term"

#: A handful of dependencies whose wheels on PyPI carry a license *classifier* or
#: *expression* but were built and uploaded without the license text itself — verified
#: by inspecting each wheel directly (``unzip -l``), not assumed from the metadata
#: alone. Their real text is vendored here, unmodified, from each project's own
#: repository — see each vendored file's own header for the exact source and the date
#: it was pulled. Nothing else gets this treatment: a dependency not listed here still
#: fails the build the moment its wheel has the same gap, which is the point — this is
#: a documented fix for known, verified problems, not a general escape hatch for an
#: unknown future one.
#:
#: * ``pyserial`` — no newer release exists to bump to; 3.5 (2020) is still the latest
#:   pyserial on PyPI, and its wheel has never shipped a LICENSE, COPYING, or NOTICE
#:   file of any kind.
#: * The nine ``winrt-*`` packages — bleak's Windows BLE backend (``bleak.backends.
#:   winrt``) pulls these in only on ``sys_platform == "win32"``, so they only appear
#:   in a Windows build's closure. All nine are generated and published together by the
#:   pywinrt project and share the one upstream license file, which none of their
#:   wheels include.
_VENDORED_LICENSE_GAPS: dict[str, tuple[str, ...]] = {
    "pyserial": ("vendored-licenses/pyserial-LICENSE.txt",),
    "winrt-runtime": ("vendored-licenses/pywinrt-LICENSE.txt",),
    "winrt-windows-devices-bluetooth": ("vendored-licenses/pywinrt-LICENSE.txt",),
    "winrt-windows-devices-bluetooth-advertisement": ("vendored-licenses/pywinrt-LICENSE.txt",),
    "winrt-windows-devices-bluetooth-genericattributeprofile": (
        "vendored-licenses/pywinrt-LICENSE.txt",
    ),
    "winrt-windows-devices-enumeration": ("vendored-licenses/pywinrt-LICENSE.txt",),
    "winrt-windows-devices-radios": ("vendored-licenses/pywinrt-LICENSE.txt",),
    "winrt-windows-foundation": ("vendored-licenses/pywinrt-LICENSE.txt",),
    "winrt-windows-foundation-collections": ("vendored-licenses/pywinrt-LICENSE.txt",),
    "winrt-windows-storage-streams": ("vendored-licenses/pywinrt-LICENSE.txt",),
}

#: Conventional bare-directory license filenames a pre-PEP-639 wheel might bundle at its
#: dist-info root without ever declaring them in metadata. Matched case-insensitively by
#: prefix, so ``LICENSE``, ``LICENSE.txt``, ``LICENSE-MIT`` and ``License.rst`` all hit.
_CONVENTIONAL_LICENSE_PREFIXES = ("LICENSE", "LICENCE", "COPYING", "NOTICE")


class NoticesError(RuntimeError):
    """A license notice this build needs could not be found.

    Raised instead of quietly omitting an entry — a bundle that silently drops a notice
    is the exact failure mode this module exists to prevent, so a missing one stops the
    build rather than shipping.
    """


@dataclass(frozen=True)
class Notice:
    """One entry in the generated notices file.

    Attributes:
        name: The distribution's own name, as PyPI (or, for Python itself, the
            ``platform`` module) spells it — not canonicalised, so the rendered file
            reads the way a person searching for the package by its usual name expects.
        version: The installed version.
        summary: The license expression or classifier(s) this distribution declares,
            for the header line. Never the license text itself — that is :attr:`text`.
        text: The verbatim license text (one or more files, concatenated), unmodified.
    """

    name: str
    version: str
    summary: str
    text: str


def _python_notice() -> Notice:
    """Return the PSF license notice for the interpreter running this build.

    Reuses the candidate-file search the ``license`` builtin itself does
    (``site.setcopyright`` builds it from ``os.path.dirname(os.__file__)`` plus the
    Windows install-root layout, the POSIX lib layout, and a same-directory fallback)
    rather than re-deriving those paths and risking a layout it didn't anticipate. This
    resolves correctly from inside a venv too, without any venv-specific casing here,
    because ``os.__file__`` always points at the *base* interpreter's standard library.

    Raises:
        NoticesError: No candidate file exists, and the ``license`` builtin's own
            fallback text (a pointer to a URL, not license text) is not good enough to
            ship as a notice.
    """
    printer = getattr(builtins, "license", None)
    if printer is None:
        # `site` normally installs this at interpreter startup; it is only missing if
        # the interpreter was started with `-S`. Install it ourselves rather than give
        # up — a build environment that happens to do this shouldn't lose the notice.
        import site

        site.setcopyright()
        printer = builtins.license
    candidates = getattr(printer, "_Printer__filenames", None)
    if not candidates:
        raise NoticesError(
            "could not find Python's own license: the 'license' builtin exposes no "
            "candidate file list (unexpected interpreter internals)"
        )
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            return Notice(
                name="Python",
                version=platform.python_version(),
                summary="PSF License Agreement",
                text=path.read_text(encoding="utf-8"),
            )
    raise NoticesError(
        "could not find Python's own license text; looked in: "
        + ", ".join(str(c) for c in candidates)
    )


def _license_summary(dist: Distribution) -> str:
    """The short "which license" line for a distribution's header.

    Prefers the PEP 639 ``License-Expression`` (the SPDX expression a modern build
    backend writes), then the ``License ::`` classifiers older packages rely on
    instead, then the legacy free-text ``License`` field only if it is short enough to
    be a label rather than a pasted-in license itself. "unspecified" is a last resort,
    not a failure: an unclear *label* is a readability problem, unlike a missing license
    *file*, which is a compliance one and raises instead (see :func:`_license_text`).
    """
    metadata = dist.metadata
    expression = metadata.get("License-Expression")
    if expression:
        return expression
    classifiers = [c for c in metadata.get_all("Classifier") or [] if c.startswith("License")]
    if classifiers:
        return "; ".join(classifiers)
    legacy = metadata.get("License")
    if legacy and "\n" not in legacy and len(legacy) <= 100:
        return legacy
    return "unspecified"


def _dist_info_files(dist: Distribution) -> list[PackagePath]:
    """Every file RECORD lists under this distribution's own ``.dist-info`` directory.

    Built from :attr:`Distribution.files` rather than the private
    ``Distribution._path`` some tools reach for, so this stays correct however a future
    ``importlib.metadata`` lays a distribution's metadata out on disk — it only ever
    asks the distribution for files it already told us it owns.
    """
    files = dist.files or []
    return [f for f in files if str(f).split("/", 1)[0].endswith(".dist-info")]


def _license_files(dist: Distribution) -> list[PackagePath]:
    """The dist-info files that make up this distribution's license text.

    PEP 639's ``License-File`` metadata entries win when present, matched by basename
    since a build backend is free to nest them (hatchling puts every one under a
    ``licenses/`` subdirectory; other tools leave them at the dist-info root). Lacking
    any declaration at all — true of most wheels built before 2024 — falls back to
    whatever the wheel happened to bundle at the dist-info root under a conventional
    name, which is the same thing a person skimming the directory would go looking for.
    """
    dist_files = _dist_info_files(dist)
    declared = dist.metadata.get_all("License-File") or []
    if declared:
        wanted = [Path(name).name for name in declared]
        by_name = {Path(str(f)).name: f for f in dist_files}
        # Declared order, not RECORD's, so the concatenated text below reads in the
        # order the package's own author declared it.
        return [by_name[name] for name in wanted if name in by_name]
    return sorted(
        (
            f
            for f in dist_files
            # dist-info root only — one path segment below the directory itself. A
            # nested `licenses/` folder with no metadata declaration pointing at it
            # isn't a convention to guess at.
            if str(f).count("/") == 1
            and Path(str(f)).name.upper().startswith(_CONVENTIONAL_LICENSE_PREFIXES)
        ),
        key=str,
    )


def _vendored_license_files(canonical_name: str) -> list[Path]:
    """Local fallback license text for a dependency whose wheel ships none at all."""
    here = Path(__file__).parent
    return [here / rel for rel in _VENDORED_LICENSE_GAPS.get(canonical_name, ())]


def _license_text(dist: Distribution, canonical_name: str) -> str:
    """The concatenated, verbatim license text for one distribution.

    Raises:
        NoticesError: Nothing was found in the dist-info and nothing is vendored for
            it either. Nothing downstream is in a position to notice a quietly-missing
            notice, so this stops the build instead of shipping a gap.
    """
    matches = _license_files(dist)
    if matches:
        return "\n\n".join(
            f"--- {Path(str(f)).name} ---\n{f.locate().read_text(encoding='utf-8')}"
            for f in matches
        )
    vendored = _vendored_license_files(canonical_name)
    if vendored:
        for path in vendored:
            if not path.is_file():
                raise NoticesError(
                    f"{dist.metadata['Name']} {dist.version}: vendored license file "
                    f"missing at {path} (see _VENDORED_LICENSE_GAPS)"
                )
        return "\n\n".join(
            f"--- {path.name} ---\n{path.read_text(encoding='utf-8')}" for path in vendored
        )
    raise NoticesError(
        f"{dist.metadata['Name']} {dist.version}: no license file found in its "
        "dist-info (checked declared License-File entries and "
        "LICENSE*/LICENCE*/COPYING*/NOTICE* at the dist-info root) and none is "
        "vendored for it either — a bundle would ship this dependency without its "
        "license notice"
    )


def dependency_closure(root: str = ROOT_DISTRIBUTION) -> list[Distribution]:
    """The installed distributions ``root`` actually pulls in on this interpreter.

    Walks ``Requires-Dist`` recursively (``importlib.metadata`` exposes it as
    :attr:`Distribution.requires`), evaluating each requirement's environment marker
    against *this* interpreter and platform — with ``extra`` forced to the empty
    string, which is what "no extras requested" means to a marker (a bare
    ``pip install mesh-term`` asks for none), so a requirement guarded by
    ``extra == "dev"`` reads as false and the ``dev`` extra's own tooling (pytest,
    pytest-asyncio, ruff) never enters the closure. A marker that instead names a
    platform or Python version this interpreter doesn't match is excluded the same
    way — the point of evaluating markers at all rather than reading every
    ``Requires-Dist`` line flat.

    Returns:
        Distributions sorted by their canonical (PEP 503) name, deterministically, and
        never including ``root`` itself — MeshTerm's own license is ``LICENSE`` and
        ``NOTICE``, not a third-party notice.
    """
    environment = default_environment()
    environment["extra"] = ""
    root_key = canonicalize_name(root)
    seen: dict[str, Distribution] = {}
    pending = [root]
    while pending:
        name = pending.pop()
        key = canonicalize_name(name)
        if key in seen:
            continue
        dist = distribution(name)
        seen[key] = dist
        for raw in dist.requires or ():
            requirement = Requirement(raw)
            if requirement.marker is not None and not requirement.marker.evaluate(environment):
                continue
            pending.append(requirement.name)
    seen.pop(root_key, None)
    return sorted(seen.values(), key=lambda d: canonicalize_name(d.metadata["Name"]))


def collect_notices(root: str = ROOT_DISTRIBUTION) -> list[Notice]:
    """All notices this build's ``THIRD-PARTY-NOTICES.txt`` should carry.

    Python's own PSF notice always comes first — Python is not a PyPI distribution
    ``Requires-Dist`` can name, so it can never turn up in :func:`dependency_closure`.
    Every dependency follows it, sorted by name, for a stable, reviewable diff between
    one build's notices file and the next.
    """
    notices = [_python_notice()]
    for dist in dependency_closure(root):
        canonical = canonicalize_name(dist.metadata["Name"])
        notices.append(
            Notice(
                name=dist.metadata["Name"],
                version=dist.version,
                summary=_license_summary(dist),
                text=_license_text(dist, canonical),
            )
        )
    return notices


_HEADER = """\
Third-party notices
====================

This build of MeshTerm bundles the packages listed below as part of its Python
runtime. Each entry below reproduces that package's own license, verbatim and
unmodified, as its terms require. MeshTerm's own license and copyright notice are
in the LICENSE and NOTICE files bundled alongside this one, not repeated here.

Generated by packaging/notices.py at build time -- do not edit by hand.
"""

_RULE = "=" * 78


def render(notices: list[Notice]) -> str:
    """Render ``notices`` as one deterministic, newline-terminated UTF-8 document."""
    blocks = [_HEADER]
    for notice in notices:
        header = f"{notice.name} {notice.version} — {notice.summary}"
        blocks.append(f"{_RULE}\n{header}\n{_RULE}\n\n{notice.text.rstrip()}\n")
    return "\n".join(blocks).rstrip("\n") + "\n"


def write_third_party_notices(path: str | Path, root: str = ROOT_DISTRIBUTION) -> Path:
    r"""Generate and write ``THIRD-PARTY-NOTICES.txt`` to ``path``.

    Written as raw UTF-8 bytes rather than through text mode, so a Windows build
    doesn't translate the ``\n`` line endings to ``\r\n`` — the file is meant to be
    byte-identical in content across the platforms that each build it.

    Returns:
        ``path``, resolved to a :class:`~pathlib.Path`, for the caller's convenience.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render(collect_notices(root)).encode("utf-8"))
    return path


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python packaging/notices.py OUTPUT_PATH``."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: notices.py OUTPUT_PATH", file=sys.stderr)
        return 2
    written = write_third_party_notices(args[0])
    print(f"wrote {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
