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
syscon-tts download-voices en_us_kristin   # one voice is enough for a smoke test
syscon-tts speak -v en_us_kristin -o /tmp/hi.wav "Hello"
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

1. **Read the voice's `MODEL_CARD` first** and record its `license` verbatim in
   the entry. PlantStar is sold, so the bundled catalogue carries only voices
   whose upstream license permits commercial use (public domain, CC0,
   Unlicense, CC BY). Piper's catalogue contains plenty that do not —
   `en_US-ryan` and the `hfc_*` voices are CC BY-NC-SA, and `zh_CN-chaowen` is
   fine-tuned from a non-commercial voice, so its lineage is restricted too.
   Anything else must set `notes` explaining what is unresolved; the tests
   enforce that, and `doctor` surfaces it to operators.
2. Add an entry to [`src/syscon_tts/data/voices.json`](src/syscon_tts/data/voices.json)
   with a unique `id` and the `model` / `config` filenames plus their
   `model_url` / `config_url` from
   [huggingface.co/rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices).
3. `syscon-tts download-voices <new_id>`.
4. Verify with `syscon-tts list-voices` (shows `installed: yes`).

**Never rename or remove a released id.** The APU stores the selected voice id
in its settings table, so a rename silently breaks every site already using it;
`test_bundled_manifest_ids_are_stable_and_well_formed` guards this. Add a new
entry instead.

A site that only wants a voice locally does not need a manifest change at all:
dropping the `.onnx` + `.onnx.json` pair into the voices directory is enough
(they are discovered), or it can layer its own file via
`SYSCON_TTS_EXTRA_MANIFEST`.

If you change the manifest schema itself, update `VoiceProfile` /
`VoiceRegistry` in `voices.py` and the tests.

## Releasing

The package is published to PyPI as
[`syscon-tts`](https://pypi.org/project/syscon-tts/) using an API token, the
same approach as `django-search-filter-sort`.

### One-time setup

Add a section to `~/.pypirc` named for this project, holding its API token.
Naming the section after the GitHub repo keeps it obvious which token is which
when several Syscon packages share the file:

```ini
[distutils]
index-servers =
    pypi
    django-search-filter-sort
    syscon_tts

[syscon_tts]
repository = https://upload.pypi.org/legacy/
username = __token__
password = pypi-<token>
```

The very first upload needs an **account-scoped** token, because a
project-scoped token cannot be created until the project exists. Once
`syscon-tts` is on PyPI, replace it with a project-scoped one so this token
can't touch other packages.

### First release

```bash
./create_dist.sh
twine upload --repository syscon_tts dist/*
```

Use the explicit `--repository` for this one so the right token is selected.
`--repository` names the `~/.pypirc` section, not the package — the two just
happen to match here by convention.

### Every release after that

1. Bump `version` in `pyproject.toml` **and** `__version__` in
   `src/syscon_tts/__init__.py`. `tests/test_config.py` fails if they disagree,
   because only `pyproject.toml` drives the published distribution — a drift
   would ship a package whose `--version` contradicts PyPI.
2. Merge to `main` with CI green.
3. Build, upload, and tag:
   ```bash
   ./create_dist.sh
   ./upload_dist.sh
   git tag v0.0.2 && git push origin v0.0.2
   ```

`create_dist.sh` builds an **sdist and a wheel**, unlike
`django-search-filter-sort`'s sdist-only script. The wheel is what lets the APU
install without running a build step or fetching build dependencies — which
matters on a locked-down or air-gapped host. Both belong on PyPI; pip prefers
the wheel automatically.

### Optional: releasing from CI

[`release.yml`](.github/workflows/release.yml) does the same thing on a tag
push, plus a check that the tag matches `pyproject.toml`. It needs a
`PYPI_API_TOKEN` repo secret. It builds from a clean tagged checkout rather than
your working directory, so the artifact always corresponds to a known commit.
It can also be dispatched manually against TestPyPI to rehearse.

### Caveats

**PyPI versions are immutable.** A bad publish needs a new version number — you
cannot re-upload over a released version.

`upload_dist.sh` passes `--skip-existing`, matching the other Syscon package.
That makes a partially-failed upload safe to retry, but it also means
forgetting to bump the version fails *silently*. If an upload appears to do
nothing, check the version on PyPI against `syscon-tts --version`.

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
