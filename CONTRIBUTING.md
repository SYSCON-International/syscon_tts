# Contributing to Syscon TTS

Thanks for working on Syscon TTS. This guide covers local setup, conventions,
and the workflow for changes. For how the package is built see
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); for deployment see
[DEPLOY.md](DEPLOY.md).

## Development setup

```bash
git clone https://github.com/SYSCON-International/syscon_tts.git
cd syscon_tts
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

This works on **Linux, Windows, and macOS**. Piper is platform-gated in
`pyproject.toml`, so the install never fails on a machine it can't support —
you simply get a package that can't synthesize. Check which mode you're in:

```bash
syscon-tts doctor
```

To exercise real synthesis you need Linux with Python 3.9–3.11:

```bash
syscon-tts download-voices en_us_amy   # one voice is enough for a smoke test
syscon-tts speak -v en_us_amy -o /tmp/hi.wav "Hello"
```

On Windows or macOS, generate WAVs on a Linux host, drop them in your
`SYSCON_TTS_ALERTS_DIR`, and the cache-hit path exercises the full alert
pipeline locally.

## Running tests

```bash
pytest
```

The suite runs **without Piper and without voice models**, on every platform:
`test_api.py` mocks the engine, and `test_alerts.py` injects a fake one. Keep it
that way — a green suite on a developer laptop is the point.

CI runs the same `pytest` on Linux (3.9, 3.11), Windows, and macOS, and asserts
that Piper's presence matches what the dependency marker intends for each
platform. If you change that marker, expect the "Confirm Piper presence" step to
tell you about it. See [.github/workflows/ci.yml](.github/workflows/ci.yml).

## Code conventions

- **Keep the interface adapters thin.** All synthesis logic lives in
  `engine.py`; all voice knowledge lives in `voices.py`. `api.py`, `cli.py`, and
  `alerts.py` are adapters — new behaviour usually belongs in the core.
- **Only `engine.py` may import Piper, and only inside a function.** This is
  what makes the package importable on Windows and macOS. A module-level
  `import piper` anywhere breaks the cross-platform contract.
- **Never derive runtime paths from the source tree.** Everything resolves
  through `Settings` (`config.py`). `PACKAGE_DIR` is for bundled package data
  only — `site-packages` is not a writable data location.
- **Configuration is environment variables**, resolved once into `Settings`.
  Don't read `os.environ` elsewhere; add a field to `Settings`.
- **src layout.** Package code lives under `src/syscon_tts/`. Imports use the
  installed package name (`from syscon_tts...`).
- **Match the surrounding style** — module docstrings, type hints, and the
  existing error-type pattern (`VoiceError` / `SynthesisError` subclasses mapped
  to HTTP status codes in `api.py`).
- Add or update tests for any behaviour change; keep Piper mocked in unit tests.

### Don't break filename parity

`sanitize_file_name()` in `alerts.py` reproduces Django's
`get_valid_filename(text)[:50]`, which the APU uses to name the same files. If
the two ever disagree, every lookup becomes a cache miss and the APU serves
nothing. `tests/test_alerts.py` pins the expected outputs; if you touch that
function, re-verify against real Django rather than reasoning about the regex.

## Adding a voice or language

Voices are **data, not code** — no source changes required:

1. Add an entry to [`src/syscon_tts/data/voices.json`](src/syscon_tts/data/voices.json)
   with a unique `id` and the `model` / `config` filenames plus their
   `model_url` / `config_url` from
   [huggingface.co/rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices).
2. `syscon-tts download-voices <new_id>`.
3. Verify with `syscon-tts list-voices` (shows `installed: yes`).

If you change the manifest schema itself, update `VoiceProfile` /
`VoiceRegistry` in `voices.py` and the tests.

## Releasing

The package is published to PyPI as
[`syscon-tts`](https://pypi.org/project/syscon-tts/) using an API token, the
same approach as `django-search-filter-sort`.

**One-time setup:** create a PyPI API token and add it to this repo as the
`PYPI_API_TOKEN` secret (Settings → Secrets and variables → Actions). Scope it
to the `syscon-tts` project once that project exists — the first upload needs
an account-scoped token, which should then be replaced with a project-scoped
one.

**Each release:**

1. Bump `version` in `pyproject.toml` **and** `__version__` in
   `src/syscon_tts/__init__.py`. `tests/test_config.py` fails if they disagree,
   because only `pyproject.toml` drives the published distribution — a drift
   would ship a package whose `--version` contradicts PyPI.
2. Merge to `main` with CI green.
3. Tag and push:
   ```bash
   git tag v0.0.2 && git push origin v0.0.2
   ```

[`release.yml`](.github/workflows/release.yml) verifies the tag matches
`pyproject.toml`, builds an sdist and wheel, runs `twine check`, and uploads.
It can also be run manually from the Actions tab against **TestPyPI** to
rehearse a release.

To publish from a local checkout instead — using your `~/.pypirc`:

```bash
scripts/release.sh --test          # rehearse against TestPyPI
scripts/release.sh --build-only    # build and check, upload nothing
scripts/release.sh                 # the real thing
```

Prefer the workflow: it builds from a clean tagged checkout rather than your
working directory, so the artifact always corresponds to a known commit.

**PyPI versions are immutable.** A bad publish needs a new version number, not
a re-upload — which is why neither path passes `--skip-existing`. An upload
that collides with an existing version should fail loudly, since it almost
always means the version wasn't bumped.

## Branching, commits, and PRs

- Branch off `main`; use a descriptive branch name (e.g. `add-italian-voice`,
  `fix-mp3-error-handling`).
- Keep commits focused with clear messages (imperative mood, explain the *why*).
- Open a PR against `main`. CI must pass on all four matrix entries before merge.
- Update the relevant docs (`README.md`, `DEPLOY.md`, or
  `docs/ARCHITECTURE.md`) in the same PR when behaviour or interfaces change.

## Where to make common changes

| Change | File(s) |
|---|---|
| APU integration / caching behaviour | `src/syscon_tts/alerts.py` (+ tests) |
| Synthesis behaviour (speed, formats, model cache) | `src/syscon_tts/engine.py` |
| New CLI command or flag | `src/syscon_tts/cli.py` |
| New/changed API endpoint or field | `src/syscon_tts/api.py` (+ tests) |
| Voice lookup / install detection | `src/syscon_tts/voices.py` |
| Model download behaviour | `src/syscon_tts/download.py` |
| New setting / default | `src/syscon_tts/config.py` |
| New voice / language | `src/syscon_tts/data/voices.json` (data only) |
| Dependencies, extras, platform markers | `pyproject.toml` |
| Deployment / packaging | `scripts/`, `systemd/`, `.github/workflows/` |
