"""The single source of truth for the package version.

Both the runtime (``syscon_tts.__version__``) and the built distribution
(``pyproject.toml`` reads this via ``[tool.setuptools.dynamic]``) take the
version from here, so the two can never disagree -- the drift that shipped a
0.1.0 wheel reporting 0.0.4 is structurally impossible now. Bump this one line
to cut a release; nothing else records the number.
"""

__version__ = "0.1.3"
