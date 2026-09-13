# SPDX-License-Identifier: Apache-2.0
"""Enforce SPDX license identifiers on all Python files.

Every Python source file in the MeshTerm project must declare its license with
a SPDX-License-Identifier comment on line 1 (or line 2 if it starts with a
shebang). This test walks the entire codebase and asserts the identifier is
present. A file lifted out of the repository carries no license marking
otherwise, so this gate ensures no file lands unmarked.
"""

from pathlib import Path


def test_spdx_headers():
    """Every Python file must have an SPDX-License-Identifier header."""
    repo_root = Path(__file__).parent.parent
    python_files = []

    # Collect all Python files from the required directories
    for directory in ["meshterm", "tests", "packaging", "scripts/xiao-radio"]:
        dir_path = repo_root / directory
        if dir_path.exists():
            if directory == "packaging":
                # For packaging, include both .py files and .spec files
                python_files.extend(dir_path.glob("*.py"))
                python_files.extend(dir_path.glob("*.spec"))
            else:
                python_files.extend(dir_path.rglob("*.py"))

    # Also check the extensionless scripts/meshterm-spi-bridge script
    spi_bridge = repo_root / "scripts" / "meshterm-spi-bridge"
    if spi_bridge.exists():
        python_files.append(spi_bridge)

    missing_spdx = []
    for filepath in sorted(python_files):
        try:
            content = filepath.read_text(encoding="utf-8")
            lines = content.split("\n", 2)  # Check first 2 lines max

            # Check for SPDX identifier on line 1 or 2
            has_spdx = False
            if lines and "SPDX-License-Identifier:" in lines[0]:
                has_spdx = True
            elif len(lines) > 1 and "SPDX-License-Identifier:" in lines[1]:
                has_spdx = True

            if not has_spdx:
                missing_spdx.append(str(filepath.relative_to(repo_root)))
        except Exception:
            # Skip files that can't be read
            continue

    assert not missing_spdx, (
        f"Found {len(missing_spdx)} Python file(s) without SPDX-License-Identifier:\n"
        + "\n".join(f"  {f}" for f in missing_spdx)
    )
