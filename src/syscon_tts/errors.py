"""The base class of every error this package raises on purpose.

Each module defines its own errors next to the code that raises them; this
module only exists so they can share a parent without an import cycle. A
caller that wants "anything syscon_tts can go wrong with" -- the APU around
``ensure()``, say -- catches :class:`SysconTTSError` instead of a tuple of
unrelated classes.
"""


class SysconTTSError(Exception):
    """Base class for every error this package raises."""
