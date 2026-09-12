"""Markdown drawn in MeshTerm's own hand — the renderer behind the About pages.

A page of prose is the one kind of screen the app can't build out of rows, lanes and
marks: it is paragraphs. So the prose lives in ``.md`` files under ``meshterm/assets``
and this module turns them into Rich renderables — *not* by printing markdown's own
conventions (boxed headings, ``#`` marks left in the text, a syntax-highlighted slab for
every fence), but by mapping each construct onto the visual language the rest of the app
already speaks, so a written page sits beside a screenful of contacts without looking
imported from somewhere else:

* ``#`` is the page's own name in the brand hue — the wordmark, the author, whatever the
  page *is*; ``##`` is a section, drawn in the same accent every body heading uses, and
  ``###`` a sub-heading inside one. A heading may carry the standard muted aside by
  writing ``## Title · note``, and any emphasis inside a heading is its qualifier (that
  is how ``# MeshTerm *v0.1.0*`` prints the version muted beside the name).
* Prose hangs at :data:`INDENT` cells under its flush heading, as a block — a wrapped
  line lands under the paragraph it belongs to, never back at column 0 where it would
  read as a new, unheaded thought (``CLAUDE.md``'s hanging-indent rule).
* Two paragraphs are page *frame* rather than body, and both sit flush and muted: the
  **standfirst** directly under the ``#`` title (MeshTerm's one-line description, the
  author's "wrote MeshTerm") and the **colophon** — the document's last paragraph, below
  a ``---`` rule — where the copyright goes.
* Lists hang on a bullet in a two-column grid, so a wrapped item aligns under its own
  text and a nested list steps in under its parent. Quotes and fenced code run behind a
  rail down the left, drawn on *every* line including the wrapped ones.
* A fence tagged ``qr`` draws nothing of itself: its body is a URL, and the page shows
  the app's scannable code for it (:func:`~meshterm.ui.qr.qr_text`).
* Emphasis, code, links and struck-out runs take the ``md.*`` styles, which — like every
  style name here — mean something on both platforms (see :mod:`meshterm.ui.theme`). A
  link shows its target after the text unless the text already says it; nothing here
  emits an OSC hyperlink, because half the app's readers are a framebuffer console.

Parsing is :mod:`markdown_it` (CommonMark, plus tables and strikethrough) — a real
parser rather than a pile of regexes, so an author can write ordinary markdown and get
what they meant. Everything below walks its tree; nothing else in the app needs to know
markdown exists.

The output is a :class:`MarkdownDoc`: a plain Rich renderable *and* a list of top-level
blocks, which is what lets a screen render the page block by block and record where its
``##`` sections start — the same landmark machinery a grouped select list uses, so the
headings pin to the top row as their prose scrolls under them.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode
from rich import box
from rich.cells import cell_len
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.padding import Padding
from rich.rule import Rule
from rich.segment import Segment
from rich.styled import Styled
from rich.table import Table
from rich.text import Text

#: Cells a page's prose hangs at under its flush heading. Applied as
#: :class:`~rich.padding.Padding` around the whole block rather than as literal spaces,
#: so a paragraph that wraps keeps its indent on every line.
INDENT = 2

#: The parser: CommonMark, with the two GFM extensions a page actually wants. The
#: typographer stays off — it would turn quotes and dashes into characters the console
#: font may not hold, and the app writes its own em dashes anyway.
_MD = MarkdownIt("commonmark").enable(["table", "strikethrough"])

#: Bullets by nesting depth (cycled). Both glyphs are in the PicoCalc console font, and
#: neither is claimed elsewhere in the mark language: ``•`` is nobody's status and ``▸``
#: is the list-cursor shape, which is exactly what a sub-item is.
_BULLETS = ("•", "▸")

#: The left rail a quote or a fenced block runs behind, and the width it costs.
_RAIL = "│ "
_RAIL_W = cell_len(_RAIL)

#: The style a heading's own text takes, by level. ``h3`` and deeper fall through to
#: :data:`_SUBHEADING_STYLE` and are indented with the prose they head, because a
#: sub-heading belongs to its section rather than standing beside it.
_HEADING_STYLES = {"h1": "brand", "h2": "accent"}
_SUBHEADING_STYLE = "md.strong"

#: Schemes stripped from a link's target when it is shown beside the link text: what
#: matters on a 53-cell page is the host and the path, not the protocol.
_URL_NOISE = ("https://", "http://", "mailto:")


@dataclass(frozen=True)
class Block:
    """One top-level block of a rendered page.

    Attributes:
        renderable: The block's Rich content — a paragraph, a list, a rule, a blank
            separator line.
        heading: Whether this block is one of the page's section landmarks (see
            :attr:`MarkdownDoc.sections`).
        label: The heading's plain text, for a landmark; empty otherwise.
        qr: Whether this block is a ``qr`` fence's scannable code. Marked so a surface
            that cannot use one can drop it (see :meth:`MarkdownDoc.for_script`).
        rule: Whether this block is a ``---`` horizontal rule. Marked for the same
            reason: a rule is drawn to the full width of whatever prints it, and the
            scripted console is 16384 cells wide.
    """

    renderable: RenderableType
    heading: bool = False
    label: str = ""
    qr: bool = False
    rule: bool = False


class MarkdownDoc:
    """A rendered markdown page: one renderable, and the blocks it is made of.

    Printing it (the scripted CLI's ``meshterm about``) draws the blocks in order, so it
    behaves as any other Rich renderable. A full-screen page instead walks
    :attr:`blocks`, rendering each and noting the line its section headings land on —
    which is what makes a heading sticky and the ``^PgUp``/``^PgDn`` section jumps mean
    something on a page long enough to scroll.
    """

    def __init__(self, blocks: Iterable[Block]) -> None:
        """Hold the already-rendered ``blocks`` as one page."""
        self.blocks: tuple[Block, ...] = tuple(blocks)

    @property
    def sections(self) -> tuple[str, ...]:
        """The page's section headings, in order — the landmarks a jump steps by."""
        return tuple(block.label for block in self.blocks if block.heading)

    def for_script(self) -> MarkdownDoc:
        """The same page with what only a screen can use left out, and no gap where it stood.

        Two blocks are drawn for an eye and are worse than useless in a pipe:

        * A **QR** is a second rendering of a URL the page already prints as a link, drawn
          for a phone pointed at a screen. Redirected into a file it is a block of block
          characters wrapped around nothing new.
        * A **rule** is sized to the console it is printed on, and the scripted console is
          :data:`~meshterm.ui.script.WIDTH` cells wide — so the ``---`` above a colophon
          arrives as one 16384-character line, tens of kilobytes of ``─`` in a page of
          prose. It is also a frame, which the scripted CLI does not draw. The blank line
          on each side already says what the rule said.

        Returns:
            A new document; this one is untouched.
        """
        kept: list[Block] = []
        for block in self.blocks:
            if block.qr or block.rule:
                # The blank separator that opened the dropped block goes with it, so
                # taking one out does not leave two blank lines where it stood.
                if kept and isinstance(kept[-1].renderable, Text) and not kept[-1].renderable.plain:
                    kept.pop()
                continue
            kept.append(block)
        return MarkdownDoc(kept)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        """Render every block in order (what makes the document a plain renderable)."""
        for block in self.blocks:
            yield block.renderable


class _Railed:
    """A renderable with a rail drawn down its left edge, on every line it wraps to.

    Rich has no such box (a panel draws all four sides, a table cell draws its rail once
    against a multi-line neighbour), and the rail is the whole point of a quote or a
    fenced block: it has to survive the wrapping, or a quote three lines long looks like
    a quote one line long followed by two loose ones.
    """

    def __init__(self, body: RenderableType, *, style: str) -> None:
        """Draw ``body`` behind a rail in ``style``."""
        self._body = body
        self._style = style

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        """Render the body a rail's width narrower, then prefix each line with the rail."""
        rail = Segment(_RAIL, console.get_style(self._style, default="none"))
        inner = options.update_width(max(1, options.max_width - _RAIL_W))
        for line in console.render_lines(self._body, inner, pad=True):
            yield rail
            yield from line
            yield Segment.line()


def render_markdown(source: str) -> MarkdownDoc:
    """Render a markdown document the way MeshTerm draws prose.

    Args:
        source: The markdown text (CommonMark, plus tables and ``~~strikethrough~~``).

    Returns:
        The page, ready to print or to hang in a :class:`~meshterm.ui.about.AboutPage`.
    """
    return MarkdownDoc(_document(SyntaxTreeNode(_MD.parse(source)).children))


# -- the document's own shape ---------------------------------------------------------


def _document(nodes: Sequence[SyntaxTreeNode]) -> list[Block]:
    """Lay the top-level blocks out, with the page frame and the blank lines between.

    Blocks are separated by one blank line, except directly after a heading — a section's
    first paragraph sits *under* its heading, the way every other body section in the app
    is built. The two frame paragraphs (standfirst, colophon) are recognised here rather
    than in :func:`_block`, because what makes them frame is where they sit in the page,
    not anything about the paragraph itself.
    """
    landmark = _landmark_tag(nodes)
    blocks: list[Block] = []
    for index, node in enumerate(nodes):
        previous = nodes[index - 1] if index else None
        if blocks and (previous is None or previous.type != "heading"):
            blocks.append(Block(Text()))
        frame = _frame_paragraph(node, previous, last=index == len(nodes) - 1)
        if frame is not None:
            blocks.append(Block(frame))
            continue
        heading = node.type == "heading" and node.tag == landmark
        label = _inline(node.children[0], heading=True).plain if heading else ""
        qr = node.type == "fence" and node.info.strip() == "qr"
        rule = node.type == "hr"
        for rendered in _block(node, indent=INDENT, depth=0):
            blocks.append(Block(rendered, heading=heading, label=label, qr=qr, rule=rule))
            heading = False  # a heading is one block; never mark a stray second
    return blocks


def _landmark_tag(nodes: Sequence[SyntaxTreeNode]) -> str:
    """Which heading level this page's sections are: ``h2``, or ``h1`` if it has none.

    A page normally titles itself with ``#`` and sections itself with ``##``; one written
    entirely in ``#`` headings gets the same landmarks rather than none.
    """
    tags = {node.tag for node in nodes if node.type == "heading"}
    return "h2" if "h2" in tags else "h1"


def _frame_paragraph(
    node: SyntaxTreeNode, previous: SyntaxTreeNode | None, *, last: bool
) -> RenderableType | None:
    """The page frame's two flush muted lines, or ``None`` for an ordinary block.

    The standfirst is the paragraph directly under the page title; the colophon is the
    document's last paragraph, below a ``---`` rule. Both describe the page rather than
    saying anything in it, so both sit flush with the title and recede — the same voice
    an empty state speaks in.
    """
    if node.type != "paragraph" or previous is None:
        return None
    standfirst = previous.type == "heading" and previous.tag == "h1"
    colophon = last and previous.type == "hr"
    if not (standfirst or colophon):
        return None
    return _inline(node.children[0], style="muted")


# -- blocks ---------------------------------------------------------------------------


def _block(node: SyntaxTreeNode, *, indent: int, depth: int) -> list[RenderableType]:
    """Render one block node.

    Args:
        node: The block's syntax-tree node.
        indent: Cells to hang prose at — :data:`INDENT` at the page level, ``0`` inside a
            container that already provides the offset (a list item, a quote, a cell).
        depth: List-nesting depth, which picks the bullet.

    Returns:
        The block's renderables (usually one).
    """
    kind = node.type
    if kind == "heading":
        return [_heading(node, indent=indent)]
    if kind == "paragraph":
        return [_pad(_inline(node.children[0]), indent)]
    if kind in ("bullet_list", "ordered_list"):
        return [_pad(_list(node, depth=depth), indent)]
    if kind == "blockquote":
        quoted = Group(*_blocks(node.children, indent=0, depth=depth))
        return [_pad(_Railed(Styled(quoted, "md.quote"), style="md.bullet"), indent)]
    if kind == "fence" and node.info.strip() == "qr":
        return [_pad(_qr(node.content.strip()), indent)]
    if kind in ("fence", "code_block"):
        code = Text(node.content.rstrip("\n"), style="md.code")
        return [_pad(_Railed(code, style="md.rail"), indent)]
    if kind == "hr":
        return [Rule(style="md.rail")]
    if kind == "table":
        return [_pad(_table(node), indent)]
    if kind in ("html_block", "html_inline"):
        # A page has no business carrying raw HTML, but dropping content silently is
        # worse than showing it: print it as the muted literal it is.
        return [_pad(Text(node.content.rstrip("\n"), style="muted"), indent)]
    if node.children:
        return _blocks(node.children, indent=indent, depth=depth)
    return []


def _blocks(
    nodes: Sequence[SyntaxTreeNode], *, indent: int, depth: int, tight: bool = False
) -> list[RenderableType]:
    """Render sibling blocks, blank-separated unless ``tight`` (inside a list item)."""
    rendered: list[RenderableType] = []
    for index, node in enumerate(nodes):
        if rendered and not tight and nodes[index - 1].type != "heading":
            rendered.append(Text())
        rendered.extend(_block(node, indent=indent, depth=depth))
    return rendered


def _qr(data: str) -> RenderableType:
    """A ``qr`` fence: the URL inside it, drawn as the app's scannable code.

    The code is generated as the page opens rather than pasted into the ``.md`` as
    half-block art. Art cannot survive markdown — a row that happens to open on a light
    module opens on a *space*, and every parser in the world eats it, shearing that row
    one module to the left — and a code nobody can scan is worse than no code. Drawing it
    live also means the link is written once, in the fence, and the code can never drift
    from it.

    Args:
        data: The fence's body — the URL to encode.

    Returns:
        The code, from :func:`~meshterm.ui.qr.qr_text` — the same white-on-black widget
        the share screen draws, so a QR looks the same everywhere in MeshTerm and reads
        the same whatever palette the terminal is running. This is the one place a code
        sits inside a page rather than taking the whole frame: it is an illustration in
        the prose, drawn where the fence is.
    """
    from .qr import qr_text

    return qr_text(data)


def _pad(renderable: RenderableType, indent: int) -> RenderableType:
    """Hang ``renderable`` at ``indent`` cells (unchanged when it is flush)."""
    if not indent:
        return renderable
    return Padding(renderable, (0, 0, 0, indent))


def _heading(node: SyntaxTreeNode, *, indent: int) -> RenderableType:
    """A heading: the page title, a section, or a sub-heading inside one."""
    style = _HEADING_STYLES.get(node.tag, _SUBHEADING_STYLE)
    text = _inline(node.children[0], style=style, heading=True)
    return text if node.tag in _HEADING_STYLES else _pad(text, indent)


def _list(node: SyntaxTreeNode, *, depth: int) -> RenderableType:
    """A bullet or numbered list: one hanging-indent grid row per item.

    Each item is a two-column grid — its marker, then its content — so a wrapped item
    aligns under its own first line and a nested list steps in under the text it belongs
    to. A *loose* list (one the author spaced out in the source) keeps that spacing.
    """
    ordered = node.type == "ordered_list"
    start = int(node.attrs.get("start", 1)) if ordered else 1
    items = node.children
    markers = [
        f"{start + index}." if ordered else _BULLETS[depth % len(_BULLETS)]
        for index in range(len(items))
    ]
    width = max(cell_len(marker) for marker in markers) + 1 if markers else 0
    loose = any(
        child.type == "paragraph" and not child.hidden for item in items for child in item.children
    )

    rows: list[RenderableType] = []
    for marker, item in zip(markers, items, strict=True):
        if rows and loose:
            rows.append(Text())
        grid = Table.grid(padding=0)
        grid.add_column(width=width, no_wrap=True)
        grid.add_column(overflow="fold")
        body = _blocks(item.children, indent=0, depth=depth + 1, tight=not loose)
        grid.add_row(
            Text(marker.ljust(width), style="md.bullet"),
            Group(*body) if len(body) != 1 else body[0],
        )
        rows.append(grid)
    return Group(*rows)


def _table(node: SyntaxTreeNode) -> Table:
    """A markdown table as a Rich table: accent headers, one hairline under them.

    Column alignment follows the markdown's own ``---:``/``:---:`` markers, which the
    parser hands over as a CSS ``text-align`` on each cell.
    """
    table = Table(
        box=box.SIMPLE_HEAD,
        show_edge=False,
        pad_edge=False,
        header_style="accent",
        border_style="md.rail",
    )
    head = next((child for child in node.children if child.type == "thead"), None)
    body = next((child for child in node.children if child.type == "tbody"), None)
    header_cells = head.children[0].children if head and head.children else []
    for cell in header_cells:
        table.add_column(
            _inline(cell.children[0]) if cell.children else Text(), justify=_justify(cell)
        )
    for row in body.children if body else []:
        table.add_row(
            *(_inline(cell.children[0]) if cell.children else Text() for cell in row.children)
        )
    return table


def _justify(cell: SyntaxTreeNode) -> str:
    """A table cell's alignment, from the parser's ``text-align`` attribute."""
    align = str(cell.attrs.get("style", ""))
    if "right" in align:
        return "right"
    if "center" in align:
        return "center"
    return "left"


# -- inline runs ----------------------------------------------------------------------


def _inline(node: SyntaxTreeNode, *, style: str = "", heading: bool = False) -> Text:
    """Render an inline node's children into one styled :class:`~rich.text.Text`.

    Args:
        node: The ``inline`` node (a paragraph's, a heading's, a cell's single child).
        style: The base style the run's plain text takes.
        heading: Whether this run is a heading, which changes what its marks mean —
            emphasis becomes the muted qualifier, and a ``·`` opens the muted aside.

    Returns:
        The rendered run.
    """
    text = Text(style=style or "")
    _append(text, node.children, style=style, heading=heading)
    return text


def _append(text: Text, nodes: Sequence[SyntaxTreeNode], *, style: str, heading: bool) -> None:
    """Append each inline node to ``text``, in the style its mark calls for."""
    for node in nodes:
        kind = node.type
        if kind == "text":
            _append_text(text, node.content, style=style, heading=heading)
        elif kind == "strong":
            _append(text, node.children, style="md.strong", heading=heading)
        elif kind == "em":
            _append(text, node.children, style="muted" if heading else "md.em", heading=heading)
        elif kind == "s":
            _append(text, node.children, style="md.strike", heading=heading)
        elif kind == "code_inline":
            text.append(node.content, style="md.code")
        elif kind == "link":
            _append_link(text, node, style=style, heading=heading)
        elif kind == "image":
            _append(text, node.children, style="md.em", heading=heading)
        elif kind == "softbreak":
            text.append(" ", style=style or None)
        elif kind == "hardbreak":
            text.append("\n", style=style or None)
        elif kind == "html_inline":
            text.append(node.content, style=style or None)
        elif node.children:
            _append(text, node.children, style=style, heading=heading)
        elif node.content:
            text.append(node.content, style=style or None)


def _append_text(text: Text, content: str, *, style: str, heading: bool) -> None:
    """Append plain text — splitting a heading's ``·`` aside off into muted."""
    if heading and " · " in content:
        title, _, note = content.partition(" · ")
        text.append(title, style=style or None)
        text.append(f"  ·  {note}", style="muted")
        return
    if content:
        text.append(content, style=style or None)


def _append_link(text: Text, node: SyntaxTreeNode, *, style: str, heading: bool) -> None:
    """Append a link: its text, then its target unless the text already says it.

    Nothing here is clickable — one of the two platforms is a framebuffer console — so a
    link that reads "the repo" has to show *where* the repo is or it says nothing at all.
    A link whose text already carries the address (a bare URL, an autolink) shows it once.
    """
    label = Text()
    _append(label, node.children, style="md.link", heading=heading)
    text.append_text(label)
    target = _display_url(str(node.attrs.get("href", "")))
    if target and target.lower() not in label.plain.lower():
        text.append(" ", style=style or None)
        text.append(target, style="muted")


def _display_url(href: str) -> str:
    """A link target as a page shows it: no scheme, no ``www.``, no trailing slash.

    An in-page anchor shows nothing at all — it names a place in the page the reader is
    already on.
    """
    if not href or href.startswith("#"):
        return ""
    for noise in _URL_NOISE:
        if href.lower().startswith(noise):
            href = href[len(noise) :]
            break
    if href.lower().startswith("www."):
        href = href[4:]
    return href.rstrip("/")


__all__ = ["INDENT", "Block", "MarkdownDoc", "render_markdown"]
