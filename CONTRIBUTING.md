# Contributing to PlantStar TTS

Thanks for working on PlantStar TTS. This guide covers local setup, conventions,
and the workflow for changes. For how the service is built see
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); for deployment see
[DEPLOY.md](DEPLOY.md).

## Development setup

> **Platform note:** the Piper dependency (`piper-phonemize`) ships wheels only
> for CPython **3.9–3.11** on Linux x86-64 / aarch64 and macOS x86-64. There is
> **no macOS arm64 wheel** — on an Apple-Silicon Mac, develop against Docker (see
> [DEPLOY.md §2b](DEPLOY.md#2b-docker)) rather than a native venv.

```bash
python3 --version                     # must be 3.9.x / 3.10.x / 3.11.x
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt   # runtime + test deps
pip install -e .                      # editable install; provides the `plantstar-tts` CLI
```

Downloading voice models is only needed to exercise real synthesis locally:

```bash
scripts/download_voices.sh en_us_amy  # one voice is enough for a smoke test
plantstar-tts speak -v en_us_amy -o /tmp/hi.wav "Hello"
```

## Running tests

```bash
pytest
```

Tests **mock the Piper engine**, so the suite runs without `piper-tts` installed
or any voice models present. `tests/test_voices.py` covers the voice registry;
`tests/test_api.py` covers the HTTP endpoints via FastAPI's `TestClient`.

CI runs the same `pytest` on Linux x86-64 for Python 3.9 and 3.11 (see
[.github/workflows/ci.yml](.github/workflows/ci.yml)). CI installs the **full**
dependency set, so it also validates that Piper installs on the target platform.

## Code conventions

- **Keep the interface adapters thin.** All synthesis logic lives in
  `engine.py`; all voice knowledge lives in `voices.py`. `api.py` and `cli.py`
  are adapters and should stay that way — new behaviour usually belongs in the
  core, not an interface.
- **Configuration is environment variables**, resolved once into `Settings`
  (`config.py`). Don't read `os.environ` elsewhere; add a field to `Settings`.
- **src layout.** Package code lives under `src/plantstar_tts/`. Imports use the
  installed package name (`from plantstar_tts...`), not relative-to-repo paths.
- **Match the surrounding style** — module docstrings, type hints, and the
  existing error-type pattern (`VoiceError` / `SynthesisError` subclasses mapped
  to HTTP status codes in `api.py`).
- Add or update tests for any behaviour change; keep Piper mocked in unit tests.

## Adding a voice or language

Voices are **data, not code** — no source changes required:

1. Add an entry to [`config/voices.json`](config/voices.json) with a unique `id`
   and the `model` / `config` filenames plus their `model_url` / `config_url`
   from [huggingface.co/rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices).
2. `scripts/download_voices.sh <new_id>`.
3. Verify with `plantstar-tts list-voices` (shows `installed: yes`).

If you change the manifest schema itself, update `VoiceProfile` /
`VoiceRegistry` in `voices.py` and the tests.

## Branching, commits, and PRs

- Branch off `main`; use a descriptive branch name (e.g. `add-italian-voice`,
  `fix-mp3-error-handling`).
- Keep commits focused with clear messages (imperative mood, explain the *why*).
- Open a PR against `main`. CI (tests on 3.9 + 3.11) must pass before merge.
- Update the relevant docs (`README.md`, `DEPLOY.md`, or
  `docs/ARCHITECTURE.md`) in the same PR when behaviour or interfaces change.

## Where to make common changes

| Change | File(s) |
|--------|---------|
| New/changed API endpoint or field | `src/plantstar_tts/api.py` (+ tests) |
| New CLI command or flag | `src/plantstar_tts/cli.py` |
| Synthesis behaviour (speed, formats, caching) | `src/plantstar_tts/engine.py` |
| Voice lookup / install detection | `src/plantstar_tts/voices.py` |
| New setting / default | `src/plantstar_tts/config.py` |
| New voice / language | `config/voices.json` (data only) |
| Deployment / packaging | `Dockerfile`, `scripts/`, `systemd/`, `pyproject.toml` |
