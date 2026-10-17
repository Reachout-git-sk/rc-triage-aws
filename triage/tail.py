"""Read only the end of a log file.

Job logs can be gigabytes (a training loop printing every step), but the reason a
job died is almost always in the last few lines. So we seek to the end and read a
fixed number of bytes: the cost is the same for a 2 KB log and a 20 GB log.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import BinaryIO

DEFAULT_TAIL_BYTES = 16 * 1024
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def read_tail_from(fh: BinaryIO, max_bytes: int = DEFAULT_TAIL_BYTES) -> str:
    fh.seek(0, os.SEEK_END)
    size = fh.tell()
    start = max(0, size - max_bytes)
    fh.seek(start)
    data = fh.read(max_bytes)
    # errors="replace": a half-cut UTF-8 character or binary junk must not crash triage.
    text = data.decode("utf-8", errors="replace")
    if start > 0:
        # We probably landed in the middle of a line: drop that partial first line.
        newline = text.find("\n")
        text = text[newline + 1 :] if newline != -1 else text
    return _ANSI.sub("", text.replace("\r\n", "\n"))


def read_tail(path: Path, max_bytes: int = DEFAULT_TAIL_BYTES) -> str | None:
    """Return the last ~max_bytes of the file as text, or None if it does not exist."""
    try:
        with open(path, "rb") as fh:
            return read_tail_from(fh, max_bytes)
    except FileNotFoundError:
        return None
