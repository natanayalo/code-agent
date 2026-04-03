"""Shared bounded stream helpers for sandbox process I/O."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

# Maximum bytes captured from stdout/stderr. Output beyond this is discarded
# to prevent disk or memory exhaustion from runaway sandbox processes.
MAX_OUTPUT_SIZE_BYTES = 2 * 1024 * 1024  # 2 MB


class ByteReadStream(Protocol):
    """Minimal protocol for objects that expose byte reads."""

    def read(self, size: int = -1) -> bytes: ...


def read_stream_bounded(
    stream: ByteReadStream,
    limit: int,
    on_limit: Callable[[], None] | None = None,
) -> bytearray:
    """Read from *stream* into a bytearray, discarding bytes beyond *limit*.

    If *on_limit* is provided, it is invoked if the captured data exceeds *limit*,
    and the stream reading terminates early.

    The stream is drained to its end unless *on_limit* is called, so the subprocess
    pipe never blocks regardless of how much data the process produces.

    Partial data already read is preserved if the stream is closed or an I/O
    error occurs mid-read.
    """
    buf = bytearray()
    try:
        for chunk in iter(lambda: stream.read(65536), b""):
            remaining = (limit + 1) - len(buf)
            if remaining > 0:
                buf.extend(chunk[:remaining])
            if len(buf) > limit:
                if on_limit:
                    on_limit()
                    return buf
    except (OSError, ValueError):
        pass
    return buf


def decode_bounded(buf: bytearray, limit: int) -> str:
    """Decode *buf* to text, appending a truncation marker when needed."""
    text = buf.decode("utf-8", errors="replace")
    if len(buf) > limit:
        text = text[:limit] + "\n... (truncated)"
    return text
