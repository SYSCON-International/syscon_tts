"""Temp files that become real files by rename, and cleanup of orphaned ones.

Alert WAVs and downloaded voice models are both written to a temp file beside
their destination and then renamed into place, so nothing ever reads a partial
file. The rename only works within one filesystem, which is why the temp file
cannot live in ``/tmp``. A process killed between the write and the rename
(SIGKILL, power loss, a dropped SSH session mid-download) leaves the temp file
behind, and nothing else removes it.

Temp files get a distinctive prefix so the sweep only ever deletes this
package's own leftovers. Age is judged by the last write, so a slow download
that is still running keeps its file fresh with every chunk and is never
mistaken for an orphan, even with two processes sharing a directory.
"""

from __future__ import annotations

import logging
import tempfile
import time
from pathlib import Path
from typing import List, Tuple

logger = logging.getLogger(__name__)

PREFIX = ".syscon-tts-"
SUFFIX = ".part"

#: Temp files from releases before 0.0.5, which used mkstemp's default prefix.
LEGACY_GLOB = "tmp*.part"

#: A temp file not written to for this long is an orphan. A render takes
#: seconds, and a download writes a chunk at least every few seconds.
STALE_SECONDS = 3600


def make_temp(directory: Path) -> Tuple[int, Path]:
    """Create a temp file in ``directory``. Returns ``(fd, path)``."""
    fd, name = tempfile.mkstemp(dir=str(directory), prefix=PREFIX, suffix=SUFFIX)
    return fd, Path(name)


def find_stale(directory: Path) -> List[Tuple[Path, int]]:
    """Orphaned temp files in ``directory``, as ``(path, size in bytes)``.

    Uses ``lstat``, so a symlink is reported (and later removed) as itself,
    never followed.
    """
    cutoff = time.time() - STALE_SECONDS
    try:
        candidates = list(Path(directory).glob(f"{PREFIX}*{SUFFIX}"))
        candidates += Path(directory).glob(LEGACY_GLOB)
    except OSError:
        return []
    stale = []
    for path in candidates:
        try:
            info = path.lstat()
        except OSError:
            continue
        if info.st_mtime < cutoff:
            stale.append((path, info.st_size))
    return stale


def sweep_stale(directory: Path) -> int:
    """Best-effort removal of orphaned temp files. Returns how many went."""
    removed = 0
    for path, _ in find_stale(directory):
        try:
            path.unlink()
        except OSError:
            continue
        removed += 1
        logger.info("Removed orphaned temp file %s", path)
    return removed
