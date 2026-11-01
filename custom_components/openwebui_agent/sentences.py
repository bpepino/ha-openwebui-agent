"""Sentence chunking of streamed text, with the gateway's rule.

The text-to-speech entity speaks each sentence as soon as it is complete, so
the first sentence plays while the answer still streams. The rule is the
gateway's own (its ``SentenceSplitter``), so both cut a streamed answer at the
same places:

* a sentence ends after ``.`` ``!`` ``?`` ``;`` (also U+037E, the Greek
  question mark) or ``…`` when whitespace follows, and at a newline followed
  by whitespace (a paragraph break);
* never inside a number (``3.5``), a time (``7:30``) or after a short
  abbreviation (``e.g.``, ``Dr.``, a single initial ``J.``);
* the first chunk may end at a comma once it is at least 40 characters long;
* past 220 characters the text is split at the last space.

Pure; no Home Assistant imports.
"""

from __future__ import annotations

import re

TERMINATORS = ".!?;\u037e…"
ABBREVIATIONS = frozenset(
    {
        "e.g",
        "i.e",
        "etc",
        "vs",
        "mr",
        "mrs",
        "ms",
        "dr",
        "prof",
        "st",
        "no",
        "jr",
        "sr",
        "approx",
        "dept",
        "fig",
        "inc",
        "ltd",
        "co",
        "mt",
        "a.m",
        "p.m",
        "am",
        "pm",
        "κ",
        "κα",
        "κος",
        "π.χ",
        "δηλ",
        "βλ",
    }
)
_WORD_BEFORE = re.compile(r"([^\s]+)$")


class SentenceSplitter:
    """Give back whole sentences as streamed text completes them."""

    def __init__(self, first_min_chars: int = 40, max_chars: int = 220) -> None:
        """Start with an empty buffer."""
        self.first_min_chars = max(0, int(first_min_chars))
        self.max_chars = max(16, int(max_chars))
        self._buf = ""
        self.count = 0  # sentences returned so far

    @staticmethod
    def _is_abbreviation(before: str) -> bool:
        """Tell whether the word before a ``.`` is an abbreviation or an initial."""
        match = _WORD_BEFORE.search(before)
        if match is None:
            return False
        word = match.group(1).rstrip(".").lower()
        if not word:
            return False
        if len(word) == 1 and word.isalpha():
            return True  # an initial: "J. Smith"
        return word in ABBREVIATIONS

    def _boundary(self, text: str, i: int) -> bool:
        """Tell whether ``text[i]`` ends a sentence (``text[i + 1]`` exists)."""
        char, following = text[i], text[i + 1]
        if char == "\n":
            return following.isspace()
        if char == "," and self.count == 0:
            return following.isspace() and (i + 1) >= self.first_min_chars
        if char not in TERMINATORS or not following.isspace():
            return False  # "3.5", "7:30", "e.g.x"
        return not (char == "." and self._is_abbreviation(text[:i]))

    def feed(self, text: str) -> list[str]:
        """Add streamed text; return the sentences it completed (possibly none)."""
        if not text:
            return []
        self._buf += text
        out: list[str] = []
        while True:
            buf = self._buf
            cut = next(
                (i + 1 for i in range(len(buf) - 1) if self._boundary(buf, i)), -1
            )
            if cut < 0:
                if len(buf) <= self.max_chars:
                    break
                space = buf.rfind(" ", self.max_chars // 2, self.max_chars)
                cut = space if space > 0 else self.max_chars
            piece, self._buf = buf[:cut], buf[cut:]
            self._emit(piece, out)
        return out

    def flush(self) -> list[str]:
        """Return the rest of the text (the stream ended)."""
        out: list[str] = []
        buf, self._buf = self._buf, ""
        while len(buf) > self.max_chars:
            space = buf.rfind(" ", self.max_chars // 2, self.max_chars)
            cut = space if space > 0 else self.max_chars
            self._emit(buf[:cut], out)
            buf = buf[cut:]
        self._emit(buf, out)
        return out

    def _emit(self, piece: str, out: list[str]) -> None:
        if sentence := piece.strip():
            out.append(sentence)
            self.count += 1
