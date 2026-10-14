# Changelog

## 0.0.5

### Integrators must act

- **`doctor` now fails when a configured voice is not installed**, and takes
  `--language <code>` (repeatable) to require an installed voice for a locale.
  Before, it checked only that voices were *catalogued*, so a Spanish or
  Chinese site whose voice was never downloaded printed "Ready." and went live
  announcing in English. Provisioning scripts that treated exit 0 as "done"
  should pass the languages the site announces in.
- **APU memory figures corrected.** A resident `medium` voice costs ~139 MB
  after a warm render (model + onnxruntime arena), not the 50–120 MB the docs
  said; a default trilingual site needs ~0.9 GB across both Tornado managers.
  Set `SYSCON_TTS_MAX_LOADED_VOICES=1`–`2` and `SYSCON_TTS_THREADS=2` on the
  APU, and re-size any box provisioned against the old numbers (DEPLOY §7).
- **Pin the install.** DEPLOY now installs a pinned, hash-locked `syscon-tts`
  into the APU venv instead of a floating `pip install syscon-tts`; the engine
  needs glibc ≥ 2.28 on x86-64/aarch64 (the wheel marker now enforces the
  architecture so the install fails cleanly elsewhere instead of trying a
  nonexistent sdist).
- **DEPLOY §2 (`call_tts` recipe) is stale** against the shipped APU handler
  and now carries a warning banner; see `SECURITY_HANDOFF_APU_TTS.md` (C-10).
  Korean/Japanese sites have no Piper voice and must not be upgraded blindly
  (C-13).

### Integrators must act (review round 4)

- **The HTTP `/synthesize` default length cap dropped from 20000 to 2000
  characters**, and the server now admits one synthesis at a time
  (`SYSCON_TTS_MAX_CONCURRENT_SYNTHESIS=1`), shedding the rest with 503. Piper's
  memory grows ~1 MB per character of un-punctuated text, so the old limit let
  one unauthenticated request ask for tens of gigabytes. If you POST longer
  text, raise `SYSCON_TTS_MAX_CHARS` deliberately and size the host for it.
- **`download-voices --language <loc>` now fails loudly when every voice for a
  locale is held** (e.g. French/German, lessac family) instead of installing
  nothing and exiting 0; and for a locale with a held native voice but a cleared
  relative (es-MX), it installs the cleared one (`es_es_carlfm`) rather than
  nothing. Provisioning scripts that relied on the silent-empty behavior will
  now see a non-zero exit.
- **APU memory budget corrected again for the worker process.** Models live in
  the worker, not the Tornado manager; with the worker now a clean `-I`
  interpreter it is far lighter than a naive `multiprocessing` child, but still
  a separate resident process (~40 MB + ~139 MB/voice). Budget ~0.5 GB for a
  one-voice-each manager pair; see DEPLOY §7, and set `MAX_LOADED_VOICES=1`–`2`.

### Hardened (review round 4)

- **The synthesis worker is a fresh `python -I` interpreter, not a
  `multiprocessing` child.** Spawn re-imported the embedder's `__main__`; on the
  APU that re-ran `django.setup()` **as root**, doubling the worker's memory and
  root-time attack surface, and would fail every synthesis on any host whose
  main script lacks an `if __name__ == "__main__"` guard. The worker now runs
  only `syscon_tts._worker_entry`, drops privilege at exec (`Popen` user/group),
  and talks over length-framed stdin/stdout (still no pickle). A hung worker is
  bounded by `SYSCON_TTS_WORKER_TIMEOUT` and killed.
- **`doctor` shows the worker settings and exercises a real worker.** It prints
  isolation/user/timeout and, with Piper and an installed voice, loads a voice
  through an actual worker — so a `SYSCON_TTS_WORKER_USER` typo, or an account
  that cannot read the voices directory, is caught instead of printing "Ready."
  while every announcement fails. `worker_user` is also shape-validated in
  config.
- **HTTP input contract tightened.** Non-finite `speed`/`sentence_silence`
  (NaN/inf) now return a clean 422 instead of 500 (the validation-error response
  no longer chokes on the echoed NaN); empty/whitespace text is exactly 400.
- **Release tooling unblocked and guarded.** `release.yml` and `create_dist.sh`
  read the version from `_version.py` (reading `[project].version` raised
  `KeyError` under the dynamic version and blocked every release), and both now
  require the version to be **strictly newer than the newest live PyPI
  release**; `create_dist.sh` also refuses to build unless CI is green on HEAD.
- **CI fixed and strengthened.** The real-Piper job installs `[piper,dev]` (it
  lacked pytest), the respawn test is platform-independent (pid comparison via
  an unknown-voice probe, so it passes on Linux where Piper is present), the
  worker's success path is exercised against real Piper, and one alert is
  rendered as root with `SYSCON_TTS_WORKER_USER=nobody` to cover the drop.
- **Dependency marker uses exact `==` architecture matches** (not a substring
  `in`), and the docs state plainly that musl and glibc < 2.28 can only install
  the engine-free package with `pip install --no-deps`.

### Hardened (review round 3)

- **The caller no longer unpickles the worker's replies.** The synthesis worker
  is the process most exposed to a native espeak-ng fault, so it must not have a
  pickle channel into the root parent that unpickling could turn into code
  execution. The worker boundary now carries length-framed bytes
  (`send_bytes`/`recv_bytes`): requests are JSON, replies are a one-byte tag
  plus raw audio or JSON, the caller decodes with `json.loads` (which runs no
  code), an error maps only to a fixed whitelist of exception classes, and the
  frame size is capped.
- **A hung worker is now bounded, not just a crashed one.** Each request waits
  at most `SYSCON_TTS_WORKER_TIMEOUT` seconds (default 30); past it the worker
  is killed and the call raises `SynthesisError`, so crafted text that sends
  espeak-ng into a loop can no longer block an announcement forever.
- **Running the worker as root now warns.** When isolation is on and no
  `SYSCON_TTS_WORKER_USER` is set on a host where the worker would inherit root
  (the APU), the first spawn logs a warning. Isolation still contains the crash,
  but a fault in a *root* worker is corruption with root's privileges — set
  `worker_user`. (The APU-side change to set it is tracked in
  `SECURITY_HANDOFF_APU_TTS.md`, High #5.)
- **CI runs a real crash vector through `AlertSynthesizer`.** The alert path the
  APU actually uses is tested end to end: a worker crash (and a hang) surface to
  `ensure()` as a catchable error while the caller survives, on every platform
  via the self-test hook, and against real Piper in the Linux `synthesis` job.
- **The lessac-lineage hold now covers French, German and Chinese.**
  `fr_fr_siwis`, `de_de_thorsten` and `zh_cn_huayan` were verified against their
  model cards (at the pinned commit) to be fine-tuned from research-only
  `en_US-lessac`, like the two Spanish voices already held. Their *dataset*
  licenses are permissive (CC BY 4.0 / CC0) or Unknown, so they were shipping as
  cleared; all three now carry `license_review_required: true` and are flagged
  by `doctor` / skipped by bulk `download-voices`. **Only `en_us_kristin`,
  `en_us_john` and `es_es_carlfm` now ship cleared** — French, German and
  Chinese sites have no cleared native voice until legal signs off on the lessac
  family (there is a cleared interim only for Spanish). See `voices.json`
  `_review_lessac` and DEPLOY "Choosing the voice". The APU's own catalogue must
  mirror these holds (`SECURITY_HANDOFF_APU_TTS.md`, C-63).

### Fixed (review round 2)

- Model/config **load failures now raise `SynthesisError`**, not raw
  `InvalidProtobuf`/`KeyError`/`JSONDecodeError`, so `except SysconTTSError`
  actually covers a corrupt or partial model.
- The **CLI catches `SysconTTSError` and `ValueError`** and prints one `error:`
  line; a bad `SYSCON_TTS_*` value no longer dumps a traceback.
- **Download errors are wrapped as `DownloadError`** (mid-download timeout,
  `IncompleteRead`, disk-full, read-only, `FileExistsError`), and
  `verify_voice`/`doctor` report an **unreadable model** instead of crashing on
  it (the 0600-from-sudo case).
- **HTTP `speed`/`sentence_silence` are bounded** (0.5–2.0 and ≤ 1 s), with
  matching engine-side guards, closing an unauthenticated OOM via a tiny speed
  or long silences.
- The **version is single-sourced** from `syscon_tts._version`, read by
  pyproject via `[tool.setuptools.dynamic]`; the wheel metadata and
  `__version__` can no longer drift.
- **`benchmark`** keeps every voice resident and samples RSS after a warm
  render, so the MEM column reflects the real ~139 MB/voice instead of ~0.

 `es_mx_ald` is now held for
  license review (its model lineage traces to the research-only `en_US-lessac`,
  though its dataset is public domain), so `doctor` flags it and a bulk
  `download-voices` skips it. A new cleared voice, **`es_es_carlfm`** (Castilian,
  trained from scratch, public domain), is the interim substitute for es-MX:
  map it with `SYSCON_TTS_DEFAULT_VOICES=es-mx=es_es_carlfm`. `es_es_davefx`
  is held for the same lineage reason. See DEPLOY.md "Choosing the voice →
  Mexican Spanish is on an interim voice" and `voices.json` `_review_es_mx` for
  the one-field switch back once `es_mx_ald` is cleared.
- **Synthesis now runs in a worker process; set `SYSCON_TTS_WORKER_USER`.**
  The pinned `espeak-ng` can segfault on crafted text within the APU's
  200-character PA limit, and synthesis used to run inside the caller — on the
  APU, the root Tornado socket manager. `AlertSynthesizer` now spawns a
  separate worker for synthesis on hosts that can run Piper, so a crash
  becomes a catchable `SynthesisError` instead of killing the process. Set
  `SYSCON_TTS_WORKER_USER` (or `worker_user=`) to an unprivileged account that
  can read the voices directory so the worker does not run as root. The model
  cache is preserved; the worker is spawned lazily and respawned on crash.
  Disable with `SYSCON_TTS_WORKER_ISOLATION=0` for diagnostics only. See
  DEPLOY.md §1, "Synthesis runs in an isolated worker process".
- **`call_tts` recipe changed (DEPLOY.md §2).** Do not pass `file_name` to
  `ensure()`. Use the `file_name` it returns for the client URL and
  `open_files`. `AlertProcessor` names end in `+00-00`, which `ensure()`
  rejects, so forwarding the queue's name silenced every machine alert.
- **Remove the post-write `set_root_www_data_ownership_and_permissions_for_path`
  call.** A root chown by path in the www-data-writable alerts directory can
  be redirected through a symlink. The WAV is already written `0644`.
- **`ensure()` options are keyword-only.** `ensure(text, file_name=...)`
  still works; `ensure(text, name)` is now a `TypeError`.
- **Catch `SysconTTSError`.** Every package error now derives from it,
  including the new `AlertWriteError` (also an `OSError`) for a directory or
  WAV that could not be written. Existing `except` clauses keep working.
- **Models fetched by 0.1.0 or earlier are `0600`.** Run
  `sudo chmod 644 /var/lib/syscon-tts/voices/*`, or re-fetch with
  `download-voices --force`, so the service user and an unprivileged `doctor`
  can read them.

### Fixed

- A language whose voice is catalogued but not installed now falls back to the
  default voice, provided the default voice is installed, instead of raising
  `VoiceNotInstalledError` on every alert.
- `normalize_language("-")` raised `IndexError`; it now returns `""`.
- One malformed `.onnx.json` in the voices directory no longer aborts the
  whole registry.
- Downloaded models are installed `0644`.
- Text with a lone surrogate raises `InvalidAlertNameError` instead of a bare
  `UnicodeEncodeError`. A `file_name` over 251 bytes is refused before
  synthesis instead of failing with `ENAMETOOLONG` afterwards.
- A cache hit no longer waits behind an unrelated render that shares its lock
  stripe.
- Missing parent directories of the alerts directory get `dir_mode`, not the
  umask's mode. Directory modes are also set through a descriptor.
- Temp files orphaned by a killed render are removed after an hour, on the
  first `ensure()` per directory after the process starts.
- Partial model files left by an interrupted `download-voices` (up to ~60 MB
  each) are removed by the next `download-voices` run once they are an hour
  old. `doctor` reports them but does not delete them.
- Fallback-voice warnings are logged once, not on every call.
- Manifests are validated. `model`/`config` must be plain file names (a
  `../` path would have let a root `download-voices` write anywhere), digests
  must be 64 hex digits, `null` optional fields mean "not given", duplicate
  ids are an error, and a malformed file of any shape is a `VoiceError`. A
  UTF-8 BOM or UTF-16 manifest is now accepted.
- Locales with a script, codeset or modifier parse correctly: `sr-Latn-RS` ->
  `sr_RS`, `en_US.UTF-8` -> `en_US`, `es_MX@euro` -> `es_MX`.
- License strings are compared normalized, so model-card spellings such as
  `CC-BY 4.0` or an unlicense.org URL no longer get flagged for review.
- A dropped-in voice takes its locale from the most specific source: a
  config saying just `es` no longer turns an `es_ES-*.onnx` voice into es_MX.
- Discovery skips world-writable model files, and two files that map to one
  id no longer silently replace each other.
- A 0-byte file or a directory no longer counts as an installed model.
- Loggers are named per module (`syscon_tts.alerts`, ...). A handler on
  `syscon_tts` still receives everything.
- Alert WAV modes are set through the open descriptor, not the path.
- 0.1.0 was published to PyPI by mistake, with `__version__ = "0.0.4"`, and
  should be yanked. 0.0.5 is the real successor to 0.0.4.

### Release process

- `create_dist.sh` refuses to build unless the tree is clean, HEAD is
  `origin/main`, the tests pass, and the version is new to PyPI.
