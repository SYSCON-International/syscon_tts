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
ruff check src tests     # ruff is in the [dev] extra; CI runs the same check
```

The suite runs **without Piper and without voice models**, on every platform:
`test_api.py` mocks the engine, `test_alerts.py` injects a fake one, and
`test_engine.py` substitutes a fake `piper` module. Keep it that way — a green
suite on a developer laptop is the point.

CI runs the same `pytest` on Linux (Python 3.9, 3.10, 3.11 and 3.12), Windows
and macOS, and asserts that Piper's presence matches what the dependency
marker intends for each platform. If you change that marker, expect the
"Confirm Piper presence" step to tell you about it. It also runs a lint job
(`ruff check src tests`), builds the sdist and wheel and `twine check`s them,
and smoke-tests `syscon-tts doctor` with no models installed, asserting it
exits `1` — exit `0` there would mean the provisioning gate is broken. See
[.github/workflows/ci.yml](.github/workflows/ci.yml).

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
- **Configuration is environment variables or keyword overrides**, resolved
  once into `Settings`. Don't read `os.environ` elsewhere; add a field to
  `Settings`, an entry in `ENV_NAMES`, and its coercion in `config.py`.
- **Log through `logging.getLogger("syscon_tts")`** and never configure
  handlers in the library — the APU owns its logging setup.
- **src layout.** Package code lives under `src/syscon_tts/`. Imports use the
  installed package name (`from syscon_tts...`).
- **Match the surrounding style** — module docstrings, type hints, and the
  existing error-type pattern (`VoiceError` / `SynthesisError` subclasses mapped
  to HTTP status codes in `api.py`).
- Add or update tests for any behaviour change; keep Piper mocked in unit tests.

### Don't change alert file names casually

The APU builds the client's audio URL from `AlertSynthesizer.file_name_for()`,
which returns `alert_file_name(text, voice_id)`: the first 41 characters of
`sanitize_file_name(text)` plus `_` and 8 hex digits of
`sha1(f"{text}|{voice_id}")`, 50 characters at most. Changing that algorithm
changes every name, so it is a CHANGELOG-worthy, integrator-visible change.

`sanitize_file_name()` reproduces Django's `get_valid_filename`, so the
readable prefix matches what the APU produced before. `tests/test_alerts.py`
pins the expected outputs; if you touch that function, re-verify against real
Django rather than reasoning about the regex.

Cached WAVs are reused only when their embedded fingerprint (`LIST/INFO/ICMT`
chunk; SHA-256 of text, voice, speed and sentence silence) matches. If you add
an input that changes the audio, add it to `request_fingerprint()` too, or a
stale file will be served.

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
   [huggingface.co/rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices),
   pinned to the same commit as the other entries (not `main`), and the
   `model_sha256` / `config_sha256` of both files.
   `test_every_catalogued_asset_is_pinned` fails without them.
3. `syscon-tts download-voices <new_id>` (verifies the hashes).
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
[`syscon-tts`](https://pypi.org/project/syscon-tts/). The normal path is CI:
push a version tag and [`release.yml`](.github/workflows/release.yml) tests,
builds and publishes. A local path with an API token, the same approach as
`django-search-filter-sort`, remains for when CI is not an option.

**Do not bump `piper-tts`** (exactly `1.2.0`) as part of a routine release.
Later versions are GPL-3.0-or-later and 1.2.0's own phonemizer bundles
GPL-3.0 espeak-ng; the distribution question is awaiting a legal decision.
`test_piper_engine_stays_pinned_to_1_2_0` fails if the pin moves. See the
README, "Engine licensing".

### Every release

1. Bump `version` in `pyproject.toml` **and** `__version__` in
   `src/syscon_tts/__init__.py`. `tests/test_config.py` fails if they disagree,
   because only `pyproject.toml` drives the published distribution — a drift
   would ship a package whose `--version` contradicts PyPI.
2. Add a [`CHANGELOG.md`](CHANGELOG.md) entry for the version, with anything
   an integrator has to act on listed first.
3. Merge to `main` with CI green.
4. Tag that commit and push the tag:
   ```bash
   git tag v0.0.5 && git push origin v0.0.5
   ```

`release.yml` then runs the full CI matrix against the tagged commit (it calls
`ci.yml`), checks that the tag equals the `pyproject.toml` version, builds and
`twine check`s the sdist and wheel, and publishes to PyPI with **Trusted
Publishing** (OIDC) through the `pypi` GitHub environment. No API token is
stored in the repository. PyPI is only ever published from a `v*` tag push;
running the workflow by hand (Actions → Release to PyPI → Run workflow)
publishes to **TestPyPI** only, through the `testpypi` environment, as a
rehearsal. Third-party actions are pinned by commit SHA — bump the SHA and its
version comment together.

The one-time Trusted Publishing setup (a GitHub publisher on pypi.org and
test.pypi.org, plus the two environments in the repo) is described at the top
of `release.yml`. Once it works, delete any old `PYPI_API_TOKEN` secret.

`create_dist.sh` builds an **sdist and a wheel**, unlike
`django-search-filter-sort`'s sdist-only script, and so does CI. The wheel is
what lets the APU install without running a build step or fetching build
dependencies — which matters on a locked-down or air-gapped host. Both belong
on PyPI; pip prefers the wheel automatically.

### Releasing from a workstation (fallback)

#### One-time setup

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

Use a project-scoped token — `syscon-tts` already exists on PyPI — so it
cannot touch other packages.

#### Uploading

Do steps 1–3 of [Every release](#every-release) first, then:

```bash
./create_dist.sh
./upload_dist.sh
git tag v0.0.5 && git push origin v0.0.5
```

Pushing the tag also triggers `release.yml`; its PyPI upload will then fail
because the files already exist, which is harmless but expected.

`upload_dist.sh` targets the `syscon_tts` section of `~/.pypirc` by default
(it exports `TWINE_REPOSITORY=syscon_tts`; override it, e.g.
`TWINE_REPOSITORY=testpypi ./upload_dist.sh`, to rehearse). When invoking
twine by hand, pass `--repository syscon_tts` — it names the `~/.pypirc`
section, not the package. Either way the section has to be named: with no
repository selected twine falls through to `[pypi]`, and a project-scoped
token sitting in `[syscon_tts]` would never be read.

### Caveats

**PyPI versions are immutable.** A bad publish needs a new version number — you
cannot re-upload over a released version.

`upload_dist.sh` does **not** pass `--skip-existing` by default, so forgetting
to bump the version fails loudly instead of silently uploading nothing. Extra
arguments are passed through to `twine upload`; to resume an upload that was
interrupted part-way, run `./upload_dist.sh --skip-existing`.

## Branching, commits, and PRs

- Branch off `main`; use a descriptive branch name (e.g. `add-italian-voice`,
  `fix-mp3-error-handling`).
- Keep commits focused with clear messages (imperative mood, explain the *why*).
- Run `pytest` and `ruff check src tests` before pushing.
- Open a PR against `main`. CI must pass on every test-matrix entry (Linux
  3.9–3.12, Windows, macOS), plus the lint and build jobs, before merge.
- Update the relevant docs (`README.md`, `DEPLOY.md`, or
  `docs/ARCHITECTURE.md`) in the same PR when behaviour or interfaces change,
  and add a `CHANGELOG.md` line for anything a user or integrator would notice.
  `README.md` is also the PyPI page, so links in it must be absolute
  `https://github.com/...` URLs.

## Where to make common changes

| Change | File(s) |
|---|---|
| APU integration / caching behaviour | `src/syscon_tts/alerts.py` (+ tests) |
| Synthesis behaviour (speed, formats, model cache, threads) | `src/syscon_tts/engine.py` |
| New CLI command or flag | `src/syscon_tts/cli.py` |
| Benchmark samples / output | `src/syscon_tts/benchmark.py` |
| New/changed API endpoint or field | `src/syscon_tts/api.py` (+ tests) |
| Voice lookup / install detection | `src/syscon_tts/voices.py` |
| Model download behaviour | `src/syscon_tts/download.py` |
| New setting / default | `src/syscon_tts/config.py` |
| New voice / language | `src/syscon_tts/data/voices.json` (data only) |
| Dependencies, extras, platform markers | `pyproject.toml` |
| Deployment / packaging | `systemd/`, `.github/workflows/`, `create_dist.sh`, `upload_dist.sh` |
| Release notes | `CHANGELOG.md` |
