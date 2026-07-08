"""The MeshTerm wordmark, as block-glyph ASCII art for the startup screen.

Kept as a code constant (rather than a packaged data file) so it is always available at
runtime regardless of how the package is installed.
"""

from __future__ import annotations

#: The wordmark, one string per row. Rows are equal width, so the block centers cleanly.
LOGO: list[str] = [
    '███    ███ ███████ ███████ ██   ██ ████████ ███████ ██████  ███    ███',
    '████  ████ ██      ██      ██   ██    ██    ██      ██   ██ ████  ████',
    '██ ████ ██ █████   ███████ ███████    ██    █████   ██████  ██ ████ ██',
    '██  ██  ██ ██           ██ ██   ██    ██    ██      ██   ██ ██  ██  ██',
    '██      ██ ███████ ███████ ██   ██    ██    ███████ ██   ██ ██      ██',
]
