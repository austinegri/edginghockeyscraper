"""
A small, dependency-free HTML tree for the NHL's HTML game reports.

The reports nest tables inside table cells (on-ice players in the play-by-play,
the player cell of a penalty row, the two teams' side-by-side tables), which a
flat row collector can't follow. `parse_html` builds a tree with the standard
library's HTMLParser, closing an open <td>/<tr> when the next one starts, the way
browsers do, so a missing end tag can't swallow the rest of a table.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Iterator

_VOID = {'area', 'base', 'br', 'col', 'hr', 'img', 'input', 'link', 'meta', 'param'}


class Node:
    """One element: tag, attributes, and children (Nodes and text strings)."""

    __slots__ = ('tag', 'attrs', 'children', 'parent')

    def __init__(self, tag: str, attrs: dict[str, str], parent: Node | None) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[Node | str] = []
        self.parent = parent

    def __repr__(self) -> str:
        return f'<{self.tag} {self.attrs}>'

    @property
    def cls(self) -> str:
        return self.attrs.get('class') or ''

    def text(self) -> str:
        """All text below this node, whitespace collapsed; <br> and &nbsp; read as spaces."""
        parts: list[str] = []

        def walk(node: Node) -> None:
            for child in node.children:
                if isinstance(child, str):
                    parts.append(child)
                else:
                    if child.tag == 'br':
                        parts.append(' ')
                    walk(child)
                    if child.tag in ('td', 'th', 'tr'):
                        parts.append(' ')

        walk(self)
        return ' '.join(''.join(parts).replace('\xa0', ' ').split())

    def lines(self) -> list[str]:
        """Text split at <br> tags, each piece whitespace-collapsed."""
        out: list[str] = ['']

        def walk(node: Node) -> None:
            for child in node.children:
                if isinstance(child, str):
                    out[-1] += child
                elif child.tag == 'br':
                    out.append('')
                else:
                    walk(child)

        walk(self)
        return [' '.join(s.replace('\xa0', ' ').split()) for s in out]

    def iter(self, tag: str) -> Iterator[Node]:
        """Every descendant with this tag, in document order."""
        for child in self.children:
            if isinstance(child, Node):
                if child.tag == tag:
                    yield child
                yield from child.iter(tag)

    def find(self, tag: str, **attrs: str) -> Node | None:
        for node in self.iter(tag):
            if all(node.attrs.get(k) == v for k, v in attrs.items()):
                return node
        return None

    def kids(self, tag: str) -> list[Node]:
        """Direct children with this tag."""
        return [c for c in self.children if isinstance(c, Node) and c.tag == tag]

    def cells(self) -> list[Node]:
        """A row's own <td>/<th> cells (not those of tables nested inside them)."""
        return [c for c in self.children if isinstance(c, Node) and c.tag in ('td', 'th')]

    def rows(self) -> list[Node]:
        """A table's own rows, looking through <tbody>/<thead>/<tfoot>."""
        out: list[Node] = []
        for child in self.children:
            if isinstance(child, Node):
                if child.tag == 'tr':
                    out.append(child)
                elif child.tag in ('tbody', 'thead', 'tfoot'):
                    out.extend(child.kids('tr'))
        return out

    def ancestor(self, tag: str) -> Node | None:
        node = self.parent
        while node is not None and node.tag != tag:
            node = node.parent
        return node


class _TreeBuilder(HTMLParser):

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node('root', {}, None)
        self._cur = self.root

    def _close_open(self, tags: set[str], stop: set[str]) -> None:
        """Close the innermost open element in `tags`, unless a `stop` element comes first."""
        node: Node | None = self._cur
        while node is not None and node.tag not in stop:
            if node.tag in tags:
                self._cur = node.parent or self.root
                return
            node = node.parent

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == 'tr':
            self._close_open({'tr'}, {'table', 'root'})
        elif tag in ('td', 'th'):
            self._close_open({'td', 'th'}, {'tr', 'table', 'root'})
        node = Node(tag, {k: v or '' for k, v in attrs}, self._cur)
        self._cur.children.append(node)
        if tag not in _VOID:
            self._cur = node

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._cur.children.append(Node(tag, {k: v or '' for k, v in attrs}, self._cur))

    def handle_endtag(self, tag: str) -> None:
        node: Node | None = self._cur
        while node is not None and node.tag != 'root':
            if node.tag == tag:
                self._cur = node.parent or self.root
                return
            if node.tag == 'table':  # a stray end tag never closes past its table
                return
            node = node.parent

    def handle_data(self, data: str) -> None:
        self._cur.children.append(data)


def decode(html: bytes | str) -> str:
    if isinstance(html, str):
        return html
    try:
        return html.decode('utf-8')
    except UnicodeDecodeError:
        return html.decode('cp1252', errors='replace')


def parse_html(html: bytes | str) -> Node:
    builder = _TreeBuilder()
    builder.feed(decode(html))
    builder.close()
    return builder.root


def english(label: str) -> str:
    """The English half of a report header, which 2009-10-era reports print bilingually.

    'B G' -> 'G', 'MIN PIM' -> 'PIM', 'Temps Time' -> 'Time', 'Joueur/Player' -> 'Player',
    'TENT/BL A/B' -> 'A/B'. Plain English labels come back unchanged.
    """
    label = ' '.join(label.split())
    if not label:
        return label
    token = label.split(' ')[-1]
    if '/' in token and token not in ('A/B', '+/-'):
        token = token.split('/')[-1]
    return token


def side_of(node: Node) -> str | None:
    """'away' or 'home' for a node inside one of a report's side-by-side team columns.

    The reports lay the two teams' tables out as the first and last cells of one
    row. Walks up to the nearest cell that has siblings and reads its position.
    """
    cell = node.ancestor('td')
    while cell is not None:
        row = cell.parent
        siblings = row.cells() if row is not None else []
        if len(siblings) >= 2:
            index = siblings.index(cell)
            if index == 0:
                return 'away'
            if index == len(siblings) - 1:
                return 'home'
            return None
        cell = cell.ancestor('td')
    return None


_MMSS = re.compile(r'^(\d+):(\d\d)$')


def mmss(text: str) -> str | None:
    """'4:19' -> '04:19'; '' -> None. Matches the API's zero-padded 'MM:SS'."""
    m = _MMSS.match(text.strip())
    if not m:
        return None
    return f'{int(m.group(1)):02d}:{m.group(2)}'


def seconds(text: str) -> int:
    minutes, secs = text.split(':')
    return int(minutes) * 60 + int(secs)
