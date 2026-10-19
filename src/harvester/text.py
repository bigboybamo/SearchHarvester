"""Plain-text helpers shared by the ATS clients and the search providers."""

import re
from html.parser import HTMLParser

_BLOCK_TAGS = frozenset(
    {"address", "article", "blockquote", "br", "div", "footer", "h1", "h2", "h3", "h4", "h5",
     "h6", "header", "hr", "li", "ol", "p", "pre", "section", "table", "td", "th", "tr", "ul"}
)  # fmt: skip
_SKIPPED_TAGS = frozenset({"script", "style"})


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth = max(self._skip_depth - 1, 0)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            # Source newlines are insignificant in HTML; only block tags break lines.
            self.parts.append(_WHITESPACE.sub(" ", data))


_WHITESPACE = re.compile(r"\s+")


def html_to_text(markup: str) -> str:
    """Plain text from an HTML fragment: tags dropped, one line per block, whitespace collapsed."""
    extractor = _TextExtractor()
    extractor.feed(markup)
    extractor.close()
    lines = (" ".join(line.split()) for line in "".join(extractor.parts).split("\n"))
    return "\n".join(line for line in lines if line)
