# Syscon TTS

Offline neural text-to-speech for the **PlantStar APU**.

Turns alert text into a voice WAV file with no cloud service, no per-request
licensing, and no internet connection at runtime. It replaces the APU's
previous VoiceText integration.

- **Engine:** [Piper](https://github.com/rhasspy/piper) — fast, CPU-only neural
  TTS. No GPU required.
- **Interfaces:** a Python library (what the APU uses), a CLI, and an optional
  HTTP server.
- **Voices:** seven voices across five languages out of the box — including
  every locale the APU's UI offers — configured in a single JSON manifest, and
  extensible per deployment by dropping model files into the voices directory.
- **Licensing:** the shipped catalogue only contains voices whose upstream
  license permits use in a product you sell. See
  [Voices and licensing](https://github.com/SYSCON-International/syscon_tts/blob/main/README.md#voices-and-licensing). The engine's own licensing is
  an open question — see [Engine licensing](https://github.com/SYSCON-International/syscon_tts/blob/main/README.md#engine-licensing).

```bash
pip install syscon-tts
syscon-tts download-voices      # one-time, needs internet
syscon-tts doctor               # confirm the install
```

---

## Platform support

Piper depends on `piper-phonemize`, whose 1.1.0 release publishes binary
wheels for **Linux on Python 3.9–3.11** and for Intel macOS (cp310–cp312) —
nothing for Windows, Apple Silicon, or Linux on Python 3.12+. Linux is the only
platform synthesis is tested on, so the dependency marker installs Piper only
on Linux with Python < 3.12. Rather than making that a hard install failure
everywhere else, the dependency is platform-gated, so the same
`pip install syscon-tts` works on every platform and gives you what that
platform can actually do:

| Platform | Synthesis | Replay cached audio | Library / CLI / manifest |
|---|:--:|:--:|:--:|
| **Linux x86-64, Python 3.9–3.11** (APU, customer machines) | yes | yes | yes |
| **Windows, macOS** (developer machines) | no | yes | yes |
| Linux, Python 3.12+ | no | yes | yes |

Developer machines can therefore install the package, browse voices, run the
test suite, and exercise the whole alert pipeline against WAVs generated on a
Linux box — they just can't generate new audio. Attempting to do so raises
`SynthesisUnavailableError`, which names the platform and explains why.

Run `syscon-tts doctor` any time to see which mode you're in.

> On Linux with Python 3.12+, Piper is skipped rather than failing the install.
> That is a deliberate trade — `doctor` reports it as a problem, so make it part
> of provisioning rather than discovering it at the first alert.

---

## Using it from the APU

The only contract the rest of the APU depends on is the side effect: a WAV
exists at `MEDIA_ROOT/public_alert_sounds/<file_name>.wav`. Everything
downstream — the websocket push, `/media/` serving, `check_for_file_delete` —
is agnostic about how the file got there. So the integration is a rewrite of
`call_tts()` and nothing else.

```python
from syscon_tts import AlertSynthesizer

# Build once and keep it. Each instance caches loaded voice models in memory;
# constructing one per alert reintroduces a multi-second cold load every time.
# Settings are passed field by field, so no environment plumbing is needed.
synthesizer = AlertSynthesizer(
    alerts_dir=Path(settings.MEDIA_ROOT, "public_alert_sounds"),
)

result = synthesizer.ensure(
    message,
    language=locale,         # optional: "es-mx", "zh-hans", ...
)
# result.file_name -> the name to build the client's audio URL from
# result.path    -> the WAV on disk
# result.cached  -> True if a matching file already existed (no synthesis)
# result.voice   -> the voice id, on a hit or a miss
```

Two things the APU must do around this call, both covered in
[DEPLOY.md](https://github.com/SYSCON-International/syscon_tts/blob/main/DEPLOY.md):
run it in a thread (synthesis is a seconds-long CPU burn and `call_tts` is
invoked from the Tornado IO loop), and use `result.file_name` rather than a
name of its own. Do not chown or chmod `result.path` afterwards: the file is
already readable, and a root chown by path in a www-data-writable directory is
a privilege escalation.

**File names are unique per message.** `file_name_for()` (and `ensure()` when
no `file_name` is given) returns the first 41 characters of the sanitized text
— Django's `get_valid_filename` rules, so a directory listing stays readable —
plus `_` and an 8-hex-digit hash of the full text and voice: 50 characters at
most, the APU's existing limit. Two messages sharing a long prefix, or one
message in two voices, never share a file. The APU must use this name rather
than its old `get_valid_filename(message)[:50]`. An explicit `file_name` must
be one plain path component (letters, digits, `_`, `-`, `.`), or `ensure()`
raises `InvalidAlertNameError`.

`ensure()` is **cache first, and checks what it finds.** Every WAV it writes
carries a fingerprint of its text, voice, speed and sentence silence in a
standard RIFF `LIST/INFO` chunk. A file whose fingerprint matches is returned
immediately without touching Piper; a file with a different fingerprint, or
none (VoiceText output, or audio from syscon-tts 0.0.x), is rendered again
rather than announced. Concurrent `ensure()` calls for the same file wait for
each other, so N clients announcing one message render it once. Alert text is
capped at `max_alert_chars` (1,000 by default); longer text raises
`AlertTextTooLongError`.

Full integration recipe, including the `IS_LIVE` gate, file ownership and the
threading requirement:
[DEPLOY.md](https://github.com/SYSCON-International/syscon_tts/blob/main/DEPLOY.md).

The library logs under the `syscon_tts` logger — model loads, synthesis
timings and evictions at INFO, cache hits and misses at DEBUG, and a language
falling back to the default voice at WARNING. Attach a handler to see them.

---

## Voices and licensing

| ID | Language | Voice | Upstream license |
|---|---|---|---|
| `en_us_kristin` | en_US | Kristin — female (**default**) | public domain |
| `en_us_john` | en_US | John — male | public domain |
| `es_mx_ald` | es_MX | Ald — Mexican Spanish | Unlicense |
| `es_es_davefx` | es_ES | DaveFX — male (Castilian) | CC0 |
| `zh_cn_huayan` | zh_CN | Huayan — Mandarin | **unknown — needs review** |
| `fr_fr_siwis` | fr_FR | Siwis — female | CC BY 4.0 (attribution) |
| `de_de_thorsten` | de_DE | Thorsten — male | CC0 |

Piper's catalogue mixes public-domain voices with non-commercial ones, and
PlantStar ships to paying customers, so licenses are tracked per voice.
`list-voices` shows them, `doctor` flags anything installed that still needs a
review, and a bulk `download-voices` skips those voices unless you pass
`--accept-license`.

Voices deliberately **not** catalogued: `en_US-ryan`, `en_US-hfc_female`,
`en_US-hfc_male` and `zh_CN-xiao_ya` (CC BY-NC-SA — non-commercial);
`en_US-amy`, `en_GB-alan`, `en_US-lessac` (license stated only as "see dataset
URL"); `zh_CN-chaowen` (CC0 dataset, but fine-tuned from the non-commercial
`xiao_ya`). Any of them can still be used by a site that has cleared it — see
"Per-deployment voices" below.

> **Mandarin is unresolved.** `zh_CN-huayan` is the only Mandarin voice Piper
> 1.2 can run whose license is not explicitly non-commercial, and its model
> card states the dataset license as *Unknown*. It needs legal sign-off before
> it goes to a customer. It serves both `zh-Hans` and `zh-Hant` sites — spoken
> Mandarin is the same, only the script differs — but spot-check Traditional
> text before promising it.

### Choosing a voice by language

The APU knows the site's locale; it does not need a voice-id table.

```python
synthesizer.ensure(message, language="es-mx")   # -> es_mx_ald
synthesizer.ensure(message, language="zh-hant") # -> zh_cn_huayan
```

`en`/`en-us`, `es`/`es-mx`, and `zh`/`zh-hans`/`zh-hant`/`zh-Hant-TW` all
normalize to the catalogue's spelling. Exact locale matches win over
same-language ones (a `es-MX` site prefers Mexican Spanish but still speaks
with the Castilian voice if that is what is installed), and a language nothing
can serve falls back to the default voice — on a plant floor, an announcement
in the wrong accent beats silence. Per-language defaults are configurable:

```
SYSCON_TTS_DEFAULT_VOICES=es-mx=es_mx_ald,zh-hans=zh_cn_huayan
```

One utterance is rendered by one voice, so a message mixing two languages is
spoken by whichever voice was selected. Split the message instead.

### Per-deployment voices

Every site provisions its own voice set. Two ways to add one without a new
release of this package:

1. **Drop the files in.** Any `.onnx` + `.onnx.json` pair in the voices
   directory that no catalogue entry claims is discovered automatically, with
   its language read from the Piper config. This covers models from
   <https://huggingface.co/rhasspy/piper-voices> (~40 languages) and
   custom-trained ones alike.
2. **Layer a manifest.** Point `SYSCON_TTS_EXTRA_MANIFEST` at a JSON file of
   the same shape as the bundled one; new ids are added, reused ids replace the
   bundled entry. Use this when the voice should also be downloadable.
   `model_sha256` / `config_sha256` are optional but recommended: with them,
   downloads are verified before install and `doctor` re-checks the files.

Fetch only what a site will actually speak:

```bash
syscon-tts download-voices --language es-mx
```

> Voice ids are a durable contract — the APU stores the selected id in its
> settings table — so a released id is never renamed, only added to.

---

## Engine licensing

This package is MIT, and so is `piper-tts` 1.2.0. But `piper-tts` 1.2.0
requires `piper-phonemize~=1.1.0`, whose wheels bundle **espeak-ng, which is
GPL-3.0**. And `piper-tts` 1.3 and later moved to
[OHF-voice/piper1-gpl](https://github.com/OHF-voice/piper1-gpl) and are
**GPL-3.0-or-later** themselves.

PlantStar is sold and installed on customer hardware, so shipping a
GPL-licensed component with it is a distribution question. **That question is
open: a legal decision is pending.** Until it is made:

- `piper-tts` is pinned to exactly `1.2.0`, and must not move past 1.2.x
  without legal review. `tests/test_config.py::test_piper_engine_stays_pinned_to_1_2_0`
  fails if the pin changes.
- Treat the espeak-ng component as unresolved, not cleared, when answering
  questions about what a customer install contains.

---

## CLI

```bash
syscon-tts doctor                        # platform, Piper, model hashes + license status
syscon-tts download-voices               # fetch the catalogue (needs internet; SHA-256 verified)
syscon-tts download-voices --language es-mx   # or just one site's language
syscon-tts download-voices en_us_kristin      # or one named voice
syscon-tts list-voices                   # catalogue, install state, licenses
syscon-tts list-voices -l zh-hans        # only what serves this locale

syscon-tts speak -v en_us_kristin -o hello.wav "Hello from PlantStar"
syscon-tts speak -l es-mx -o aviso.wav "Presion de cavidad excedida en prensa 4."
syscon-tts speak -v de_de_thorsten -f mp3 -o ansage.mp3 --text-file notice.txt

syscon-tts alert "Press 4 cavity pressure exceeded."   # cache-first, as the APU does
syscon-tts alert -l es-mx "Presion de cavidad excedida en prensa 4."
syscon-tts serve --port 5002                           # needs [server] extra
syscon-tts benchmark --threads 2                       # RTF per installed voice

syscon-tts -V doctor --quick       # -V logs model loads (-VV adds cache decisions);
                                   # --quick skips re-hashing installed models
```

`alert` exits `0` on success, `1` on an error, `2` on a usage error
(argparse), and `69` (`EX_UNAVAILABLE`) when the audio has to be synthesized
and this machine cannot run Piper — so a provisioning script can tell a typo
from a platform limitation.

---

## Configuration

All optional; every one has a working default.

| Variable | Default | Purpose |
|---|---|---|
| `SYSCON_TTS_MANIFEST` | bundled `data/voices.json` | Voice catalogue |
| `SYSCON_TTS_EXTRA_MANIFEST` | — | Second manifest layered over the first |
| `SYSCON_TTS_VOICES_DIR` | `<data dir>/voices` | Where `.onnx` models live |
| `SYSCON_TTS_ALERTS_DIR` | `<data dir>/alerts` | Where alert WAVs are written |
| `SYSCON_TTS_DEFAULT_VOICE` | `en_us_kristin` | Voice used when none is given |
| `SYSCON_TTS_DEFAULT_VOICES` | — | Per-language defaults, `es-mx=es_mx_ald,...` |
| `SYSCON_TTS_MAX_LOADED_VOICES` | `3` | Voice models kept resident in memory |
| `SYSCON_TTS_THREADS` | unset (onnxruntime's default: one per physical core) | CPU threads per synthesis |
| `SYSCON_TTS_MAX_ALERT_CHARS` | `1000` | Max alert text length (library / `alert`) |
| `SYSCON_TTS_FILE_MODE` | `0644` | Mode for generated audio |
| `SYSCON_TTS_DIR_MODE` | `0755` | Mode for directories this package creates |
| `SYSCON_TTS_HOST` | `127.0.0.1` | HTTP server bind host |
| `SYSCON_TTS_PORT` | `5002` | HTTP server bind port |
| `SYSCON_TTS_MAX_CHARS` | `20000` | Max text length per HTTP request |

The platform data dir on Linux is `/var/lib/syscon-tts` whenever that
directory exists — even if the current user cannot write to it, since models
only need to be readable — or when it can be created; otherwise
`~/.local/share/syscon-tts` (`$XDG_DATA_HOME`). It is
`%LOCALAPPDATA%\syscon-tts` on Windows and
`~/Library/Application Support/syscon-tts` on macOS. `syscon-tts doctor`
prints the data dir it chose and why; `download-voices` into a directory the
user cannot write fails with an explicit permission error.

An embedded caller that already knows its paths should skip the environment
entirely — every setting is a constructor keyword:

```python
AlertSynthesizer(alerts_dir=..., default_voices={"zh_CN": "zh_cn_huayan"})
```

Keywords get the same normalization as environment variables: language keys
are normalized (`{"es-mx": ...}` becomes `es_MX`), modes may be octal strings
(`file_mode="0644"`), and integers are coerced (`port="5002"`). Constructing
`Settings(...)` directly validates the same way.

### Where the files go on an APU

| What | Where | Why |
|---|---|---|
| Voice models | `/var/lib/syscon-tts/voices` (the default — no config needed) | Large, immutable, re-downloadable, and per-machine |
| Generated alert WAVs | `MEDIA_ROOT/public_alert_sounds` | Has to be web-served; ephemeral, deleted after playback |

Voice models deliberately do **not** live under `MEDIA_ROOT`: the APU's
`backup_plantstar` copies the entire media tree into every backup, which is
then compressed, encrypted and shipped off-box. Voice models would add hundreds
of megabytes to every backup at every site, to preserve files that a single
`syscon-tts download-voices` reproduces. Back up the *selection* — the voice
ids a site uses — not the models.

`/var/lib/syscon-tts` also survives `sideload_code` (which only writes under
`website/`) and `pip install -U`, and cannot leak into git.

---

## Optional HTTP server

```bash
pip install 'syscon-tts[server]'
syscon-tts serve --port 5002
```

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Liveness + whether this host can synthesize |
| GET | `/voices` | Voice profiles, licenses and install state (`?language=es-mx`) |
| POST | `/synthesize` | Render text → audio bytes |

```bash
curl -s -X POST http://localhost:5002/synthesize \
  -H 'Content-Type: application/json' \
  -d '{"text":"Machine 4 cycle complete.","voice":"en_us_john"}' \
  -o alert.wav
```

Status codes: `404` unknown voice · `501` this host can never synthesize
(no Piper) · `503` voice model not downloaded · `413` text too long ·
`400` bad input.

The APU does **not** need this — it imports the library in-process, which keeps
FastAPI and uvicorn out of the Django environment entirely.

---

## Development

```bash
git clone https://github.com/SYSCON-International/syscon_tts.git
cd syscon_tts
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

The full test suite passes on Windows and macOS without Piper — synthesis is
mocked throughout, so no voice models are needed. See
[CONTRIBUTING.md](https://github.com/SYSCON-International/syscon_tts/blob/main/CONTRIBUTING.md).

---

## Documentation

- [DEPLOY.md](https://github.com/SYSCON-International/syscon_tts/blob/main/DEPLOY.md)
  — provisioning an APU, the `call_tts()` replacement, air-gapped installs,
  tuning.
- [docs/ARCHITECTURE.md](https://github.com/SYSCON-International/syscon_tts/blob/main/docs/ARCHITECTURE.md)
  — module layout, design decisions, operations, troubleshooting.
- [CHANGELOG.md](https://github.com/SYSCON-International/syscon_tts/blob/main/CHANGELOG.md)
  — what changed in each release, and what an integrator has to act on.

---

## Performance

Piper runs on CPU, so throughput depends on the host. Measure on the target:

```bash
syscon-tts benchmark               # per-voice cold load, memory, RTF, latency
syscon-tts benchmark --threads 2   # the same, with synthesis capped at 2 threads
```

It reports the **real-time factor** (RTF = synthesis time ÷ audio seconds) for
short/medium/long messages. Record results with
[docs/bench-results-template.md](https://github.com/SYSCON-International/syscon_tts/blob/main/docs/bench-results-template.md)
and use them to pick quality tiers — switching a voice to its `low`-quality
model through an overlay manifest is the main lever on a weak CPU.

By default each synthesis uses one onnxruntime thread per physical core, which
saturates the CPU for the length of the utterance. On a host with other work
to do, cap it with `SYSCON_TTS_THREADS` (or `threads=` as a constructor
keyword).

Expect roughly 50–120 MB resident per loaded `medium` voice; models stay in
memory after first use to avoid reload latency.

## License

MIT — see [LICENSE](https://github.com/SYSCON-International/syscon_tts/blob/main/LICENSE).
The speech engine's dependencies are not all MIT — see
[Engine licensing](https://github.com/SYSCON-International/syscon_tts/blob/main/README.md#engine-licensing).
