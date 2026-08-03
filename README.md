# Syscon TTS

Offline neural text-to-speech for the **PlantStar APU**.

Turns alert text into a voice WAV file with no cloud service, no per-request
licensing, and no internet connection at runtime. It replaces the APU's
previous VoiceText integration.

- **Engine:** [Piper](https://github.com/rhasspy/piper) — fast, CPU-only neural
  TTS. No GPU required.
- **Interfaces:** a Python library (what the APU uses), a CLI, and an optional
  HTTP server.
- **Voices:** six voices across four languages out of the box, configured in a
  single JSON manifest.

```bash
pip install syscon-tts
syscon-tts download-voices      # one-time, needs internet
syscon-tts doctor               # confirm the install
```

---

## Platform support

Piper ships **Linux-only** binary wheels. Rather than making that a hard
install failure everywhere else, the dependency is platform-gated, so the same
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
synthesizer = AlertSynthesizer()

result = synthesizer.ensure(
    message,
    file_name=file_name,     # the APU's get_valid_filename(message)[:50]
    alerts_dir=Path(settings.MEDIA_ROOT, "public_alert_sounds"),
)
# result.path    -> the WAV on disk
# result.cached  -> True if it already existed (no synthesis happened)
```

`ensure()` is **cache first**: if the WAV is already there it returns
immediately without touching Piper. Only a cache miss synthesizes.

The package derives file names with the exact algorithm the APU already uses
(`django.utils.text.get_valid_filename(text)[:50]`), verified byte-for-byte
including Unicode and the empty-name error cases — so both sides always agree
on the path. Passing `file_name` explicitly is still recommended, so the APU
stays the single source of truth.

Full integration recipe, including the `IS_LIVE` gate and voice-ID mapping:
[DEPLOY.md](DEPLOY.md).

---

## Voice profiles (default set)

| ID | Language | Voice |
|---|---|---|
| `en_us_amy` | en_US | Amy — female (default) |
| `en_us_ryan` | en_US | Ryan — male |
| `en_gb_alan` | en_GB | Alan — male |
| `es_es_davefx` | es_ES | DaveFX — male (Spanish) |
| `fr_fr_siwis` | fr_FR | Siwis — female (French) |
| `de_de_thorsten` | de_DE | Thorsten — male (German) |

The manifest ships inside the wheel. To use your own catalogue, point
`SYSCON_TTS_MANIFEST` at a JSON file of the same shape, then run
`syscon-tts download-voices`. Browse the full ~40-language catalogue at
<https://huggingface.co/rhasspy/piper-voices>.

---

## CLI

```bash
syscon-tts doctor                    # platform, Piper, and model status
syscon-tts download-voices           # fetch all voices (needs internet)
syscon-tts download-voices en_us_amy # or just one
syscon-tts list-voices               # catalogue + install state

syscon-tts speak -v en_us_amy -o hello.wav "Hello from PlantStar"
syscon-tts speak -v de_de_thorsten -f mp3 -o ansage.mp3 --text-file notice.txt

syscon-tts alert "Press 4 cavity pressure exceeded."   # cache-first, as the APU does
syscon-tts serve --port 5002                           # needs [server] extra
```

`alert` exits `2` when the audio is absent *and* this machine cannot
synthesize — distinguishable from `1` for ordinary errors.

---

## Configuration

All optional; every one has a working default.

| Variable | Default | Purpose |
|---|---|---|
| `SYSCON_TTS_MANIFEST` | bundled `data/voices.json` | Voice catalogue |
| `SYSCON_TTS_VOICES_DIR` | platform data dir | Where `.onnx` models live |
| `SYSCON_TTS_ALERTS_DIR` | platform data dir | Where alert WAVs are written |
| `SYSCON_TTS_DEFAULT_VOICE` | `en_us_amy` | Voice used when none is given |
| `SYSCON_TTS_HOST` | `127.0.0.1` | HTTP server bind host |
| `SYSCON_TTS_PORT` | `5002` | HTTP server bind port |
| `SYSCON_TTS_MAX_CHARS` | `20000` | Max text length per request |

The platform data dir is `/var/lib/syscon-tts` on Linux when writable
(otherwise `~/.local/share/syscon-tts`), `%LOCALAPPDATA%\syscon-tts` on
Windows, and `~/Library/Application Support/syscon-tts` on macOS.

**On the APU**, override the two path variables so audio lands where Django
already serves it:

```
SYSCON_TTS_VOICES_DIR=<MEDIA_ROOT>/tts_voices
SYSCON_TTS_ALERTS_DIR=<MEDIA_ROOT>/public_alert_sounds
```

`MEDIA_ROOT` is already gitignored and is where every other APU runtime data
directory lives, so the ~360 MB of voice models stay out of the source tree.

---

## Optional HTTP server

```bash
pip install 'syscon-tts[server]'
syscon-tts serve --port 5002
```

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Liveness + whether this host can synthesize |
| GET | `/voices` | Voice profiles and install state |
| POST | `/synthesize` | Render text → audio bytes |

```bash
curl -s -X POST http://localhost:5002/synthesize \
  -H 'Content-Type: application/json' \
  -d '{"text":"Machine 4 cycle complete.","voice":"en_us_ryan"}' \
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
[CONTRIBUTING.md](CONTRIBUTING.md).

---

## Documentation

- [DEPLOY.md](DEPLOY.md) — provisioning an APU, the `call_tts()` replacement,
  air-gapped installs, tuning.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — module layout, design
  decisions, operations, troubleshooting.

---

## Performance

Piper runs on CPU, so throughput depends on the host. Measure on the target:

```bash
python scripts/benchmark.py            # per-voice cold load, memory, RTF, latency
```

It reports the **real-time factor** (RTF = synthesis time ÷ audio seconds) for
short/medium/long messages. Record results with
[docs/bench-results-template.md](docs/bench-results-template.md) and use them to
pick quality tiers — switching a voice to its `low`-quality model in the
manifest is the main lever on a weak CPU.

Expect roughly 50–120 MB resident per loaded `medium` voice; models stay in
memory after first use to avoid reload latency.

## License

MIT — see [LICENSE](LICENSE).
