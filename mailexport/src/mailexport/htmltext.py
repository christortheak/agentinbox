"""HTML -> plain text. Drops <img> entirely (tracking pixels), skips
script/style, keeps anchor text (not hrefs)."""
from __future__ import annotations

import re
from html.parser import HTMLParser

_BLOCK = {
    "p", "div", "li", "tr", "table", "ul", "ol", "blockquote", "pre",
    "section", "article", "header", "footer", "h1", "h2", "h3", "h4", "h5", "h6",
}
_SKIP = {"script", "style", "head", "title", "template"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip_depth += 1
        elif tag == "br":
            self.parts.append("\n")
        elif tag in _BLOCK:
            self.parts.append("\n")
        # img: intentionally dropped, no alt text either

    def handle_startendtag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass  # keep whatever was extracted before the parse error
    text = "".join(p.parts)
    lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in text.splitlines()]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
