"""Last-chance clean-up of the agent's words before they are turned into speech.

The prompt already bans line breaks and numbered or bulleted lists, but Claude
still slips now and then (eval b08). This joins lines into one flow of speech
and drops list markers at the start of a line ("1.", "2)", "-", "*", "•"), so
a slip is never read out to the caller as "one dot".

It works on the text stream as it arrives, chunk by chunk, so it adds no delay
beyond holding back a few characters at the start of each line.
"""

import re
from collections.abc import AsyncIterable, AsyncIterator, Iterable

# A list marker at the start of a line: "1." or "12)" or "-" or "*" or "•",
# followed by a space. "10.30" and "-5" are not markers (no space after).
_MARKER = re.compile(r"^[ \t]*(?:\d{1,2}[.)]|[-*•])[ \t]+")
# Characters a marker can be made of. While the start of a line contains only
# these, we cannot tell yet whether it is a marker, so we hold it back.
_MARKER_CHARS = set("0123456789.)-*• \t")
_LONGEST_MARKER = 8


class SpeechCleaner:
    """Feed it chunks with push(); call finish() at the end of the reply."""

    def __init__(self) -> None:
        self._line_start = True  # the reply's first line counts as a line start
        self._pending = ""  # held-back start of the current line
        self._joined_line = False  # a line break was removed before this line
        self._last = ""  # last character sent on, to choose the joiner

    def push(self, chunk: str) -> str:
        out: list[str] = []
        for ch in chunk:
            if ch in "\r\n":
                if not self._line_start:
                    self._joined_line = True
                self._line_start = True
                self._pending = ""
                continue
            if self._line_start:
                self._pending += ch
                still_unsure = (
                    all(c in _MARKER_CHARS for c in self._pending)
                    and len(self._pending) < _LONGEST_MARKER
                )
                if still_unsure:
                    continue
                out.append(self._start_line(self._pending))
                self._pending = ""
            else:
                out.append(ch)
            if out and out[-1]:
                self._last = out[-1][-1]
        return "".join(out)

    def finish(self) -> str:
        if self._line_start and self._pending.strip():
            # Ends inside what might have been a marker, such as "Room 1."
            text = self._start_line(self._pending)
            self._pending = ""
            return text
        return ""

    def _start_line(self, text: str) -> str:
        self._line_start = False
        text = _MARKER.sub("", text, count=1).lstrip(" \t")
        if not text:
            return ""
        if self._joined_line and self._last:
            # "nine in the morning" + "A cleaning" -> "nine in the morning, A cleaning"
            joiner = ", " if self._last.isalnum() else ("" if self._last.isspace() else " ")
            text = joiner + text
        self._joined_line = False
        if text:
            self._last = text[-1]
        return text


def clean_text(chunks: Iterable[str]) -> str:
    """The whole clean-up for text that is already complete. Used by the tests."""
    cleaner = SpeechCleaner()
    return "".join(cleaner.push(chunk) for chunk in chunks) + cleaner.finish()


async def clean_stream(chunks: AsyncIterable[str]) -> AsyncIterator[str]:
    cleaner = SpeechCleaner()
    async for chunk in chunks:
        text = cleaner.push(chunk)
        if text:
            yield text
    tail = cleaner.finish()
    if tail:
        yield tail
