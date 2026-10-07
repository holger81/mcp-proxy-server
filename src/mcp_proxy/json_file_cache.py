"""Parse cache for JSON documents on the hot read path (PLAN 6.1).

The config stores used to ``read_text`` + ``json.loads`` + ``model_validate``
on *every* read — including per-request paths (bearer resolution, MCP
``tools/list``). This cache keys on the file's stat signature (mtime_ns, size,
inode): after the first read, a hit costs one ``stat`` syscall.

Contract:

- Writers must call :meth:`invalidate` after replacing the file (stores do
  this centrally in ``_write_unlocked``); external edits are picked up by the
  signature check on the next read.
- A failed parse is **never** cached, so a corrupt file keeps raising on every
  read instead of being masked (fail-closed, PLAN 4.3 semantics).
- The cached document is shared between reads: store methods that hand out
  model instances deep-copy them; callers must not assume mutation isolation
  beyond what those methods already guarantee.
- Not thread-safe on its own; every store already serialises reads and writes
  under its lock.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Generic, TypeVar

T = TypeVar("T")

_MISSING_SIG: tuple = ()


class MtimeJsonCache(Generic[T]):
    """Cache one parsed JSON document per file path.

    ``parse(path)`` must handle a missing or empty file and return the
    document default; it is only called on a cache miss (or when it raised
    previously).
    """

    def __init__(self, path: Path, parse: Callable[[Path], T]) -> None:
        self._path = path
        self._parse = parse
        self._sig: tuple | None = None
        self._value: T | None = None

    def _signature(self) -> tuple:
        try:
            st = self._path.stat()
        except OSError:
            return _MISSING_SIG
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def get(self) -> T:
        sig = self._signature()
        if self._value is not None and sig == self._sig:
            return self._value  # type: ignore[return-value]
        value = self._parse(self._path)  # may raise; nothing is cached then
        self._sig = sig
        self._value = value
        return value

    def invalidate(self) -> None:
        self._sig = None
        self._value = None
