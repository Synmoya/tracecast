"""Low-level filesystem helpers shared by the storage IO modules.

The single primitive here is :func:`atomic_write_text`: write to a temp file in
the *same directory* as the target, fsync, then ``os.replace`` onto the final
path. ``os.replace`` is atomic on a single filesystem, so a reader never sees a
half-written artifact and a crash mid-write leaves the previous version intact.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: str | os.PathLike[str], text: str) -> None:
    """Atomically write UTF-8 ``text`` to ``path`` (newline-normalised to ``\\n``).

    Parent directories are created as needed. The temp file lives in the target
    directory so the final ``os.replace`` is a same-filesystem atomic rename.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        # On success the temp file was renamed away; on failure clean it up so we
        # never leak partial writes.
        if tmp.exists():
            tmp.unlink()
