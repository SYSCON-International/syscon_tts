"""Entry point for the isolated synthesis worker -- its own clean interpreter.

``WorkerEngine`` starts this as::

    python -I <this file>

Running this file directly, rather than re-importing the embedder's ``__main__``
the way ``multiprocessing`` "spawn" does, is deliberate and is the fix for a
real problem on the APU. There the embedder's ``__main__`` is
``ProcessSpawner.py``, whose *module-level* ``django.setup()`` would re-run in a
spawned child: roughly 2,900 modules and ~200 MB of RSS imported **as root**,
before any privilege drop -- and an outright failure on any host whose main
script is not guarded by ``if __name__ == "__main__"`` (every synthesis would
then fail, with a misleading "espeak-ng fault" message). A clean ``-I``
interpreter running only this module imports none of that.

Privilege dropping is done by the parent's subprocess call (``user=``/``group=``
on :class:`subprocess.Popen`), *before* exec, so on the APU this process starts
already unprivileged -- there is no window in which this code runs as root.

Protocol (see :mod:`syscon_tts.worker`): the parent writes one JSON settings
frame, then length-framed JSON requests, on this process's stdin; this process
writes tagged frames back on its stdout. Frames are raw length-prefixed bytes --
nothing is pickled in either direction -- so a worker that an espeak-ng fault
turned hostile cannot reach the parent through object deserialization.
"""

import json
import os
import sys


def main() -> int:
    # Make ``syscon_tts`` importable without relying on PYTHONPATH (which ``-I``
    # strips): this file lives inside the package, so its grandparent directory
    # is the one that holds the ``syscon_tts`` package. This keeps the worker
    # working whether the package is pip-installed or run from a checkout.
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if pkg_parent not in sys.path:
        sys.path.insert(0, pkg_parent)

    # Keep the framing channel clean. The real stdout is dup'd for frames, then
    # fd 1 is pointed at stderr so a stray ``print`` -- or a C library that
    # writes to stdout -- cannot corrupt the binary frame stream. Diagnostics
    # still reach the parent's stderr.
    frame_out = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    frame_in = sys.stdin.buffer

    from syscon_tts import worker
    from syscon_tts.config import Settings

    settings = Settings(**json.loads(worker._read_frame(frame_in)))
    worker._serve(settings, frame_in, frame_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
