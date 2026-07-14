# PlantStar TTS

Offline text-to-speech service for the **PlantStar APU server** (Linux x86-64).

Takes arbitrary text and produces a voice audio file (WAV, or MP3 if `ffmpeg`
is present), with several selectable voice profiles across multiple languages.
It runs **fully offline** — once the voice models are downloaded during setup,
no internet connection is required.

- **Engine:** [Piper](https://github.com/rhasspy/piper) — a fast, CPU-only
  neural TTS engine. No GPU, no cloud, no per-request licensing.
- **Interfaces:** HTTP REST API **and** a command-line tool.
- **Voices:** configured in a single JSON manifest; add any of Piper's ~40
  languages by editing one file.

**Documentation:** [DEPLOY.md](DEPLOY.md) (deployment & validation recipes) ·
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (architecture, operations, and full
API/CLI reference).

---

## Voice profiles (default set)

| ID                | Language | Voice                    |
|-------------------|----------|--------------------------|
| `en_us_amy`       | en_US    | Amy — female (default)   |
| `en_us_ryan`      | en_US    | Ryan — male              |
| `en_gb_alan`      | en_GB    | Alan — male              |
| `es_es_davefx`    | es_ES    | DaveFX — male (Spanish)  |
| `fr_fr_siwis`     | fr_FR    | Siwis — female (French)  |
| `de_de_thorsten`  | de_DE    | Thorsten — male (German) |

Add/remove voices in [`config/voices.json`](config/voices.json), then run
`scripts/download_voices.sh`. Browse the full catalogue at
<https://huggingface.co/rhasspy/piper-voices>.

---

## Requirements

- **Linux x86-64** (the APU target).
- **Python 3.9–3.11.** ⚠️ The Piper dependency (`piper-phonemize`) ships
  prebuilt wheels only for Python **3.7–3.11**. Python 3.12+ has no wheel and
  will fail to install from source on most hosts. If the APU has 3.12/3.13,
  use the **Docker** deployment below (it pins Python 3.11).
- `curl` or `wget` (for the one-time model download).
- Optional: `ffmpeg` (only needed for MP3 output; WAV needs nothing extra).

---

## Install & run (bare metal)

```bash
cd plantstar-tts
scripts/install.sh          # creates .venv, installs deps, downloads voices
source .venv/bin/activate

# CLI
plantstar-tts list-voices
plantstar-tts speak -v en_us_amy -o hello.wav "Hello from PlantStar"
plantstar-tts speak -v de_de_thorsten -f mp3 -o ansage.mp3 --text-file notice.txt

# HTTP API
plantstar-tts serve --host 0.0.0.0 --port 5002
```

The internet is needed **only** for `download_voices.sh`. If the APU has no
internet, run that script on any networked machine and copy the resulting
`voices/` directory onto the APU.

## Install & run (Docker — recommended when Python != 3.9–3.11)

```bash
# 1. Fetch voice models on any machine (needs internet once):
scripts/download_voices.sh

# 2. Build and run (models are mounted, not baked into the image):
docker build -t plantstar-tts .
docker run -d -p 5002:5002 -v "$PWD/voices:/app/voices" --name plantstar-tts plantstar-tts
```

## Run as a systemd service

See [`systemd/plantstar-tts.service`](systemd/plantstar-tts.service). Deploy to
`/opt/plantstar-tts`, adjust the `User`/paths, then:

```bash
sudo cp systemd/plantstar-tts.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now plantstar-tts
```

---

## HTTP API

| Method | Path          | Description                                  |
|--------|---------------|----------------------------------------------|
| GET    | `/health`     | Liveness check + how many voices are installed |
| GET    | `/voices`     | List voice profiles and their install state  |
| POST   | `/synthesize` | Render text → audio file                      |

### `POST /synthesize`

Request body:

```json
{
  "text": "Cavity pressure exceeded the limit on press 4.",
  "voice": "en_us_amy",
  "format": "wav",
  "speed": 1.0,
  "sentence_silence": 0.2
}
```

- `voice` — optional; defaults to the server default (`en_us_amy`).
- `format` — `wav` (default) or `mp3` (needs `ffmpeg`).
- `speed` — multiplier, `1.0` = normal, `>1` faster, `<1` slower.
- `sentence_silence` — seconds of pause between sentences.

Response: the audio file bytes with the appropriate `Content-Type`
(`audio/wav` or `audio/mpeg`).

```bash
curl -s -X POST http://localhost:5002/synthesize \
  -H 'Content-Type: application/json' \
  -d '{"text":"Machine 4 cycle complete.","voice":"en_us_ryan"}' \
  -o alert.wav
```

Status codes: `404` unknown voice · `503` voice not downloaded ·
`400` bad input / synthesis error · `413` text too long.

---

## Configuration (environment variables)

| Variable                       | Default             | Purpose                              |
|--------------------------------|---------------------|--------------------------------------|
| `PLANTSTAR_TTS_HOME`           | repo root           | Base dir for config/voices           |
| `PLANTSTAR_TTS_MANIFEST`       | `config/voices.json`| Voice manifest path                  |
| `PLANTSTAR_TTS_VOICES_DIR`     | `voices/`           | Where `.onnx` models live            |
| `PLANTSTAR_TTS_HOST`           | `0.0.0.0`           | API bind host                        |
| `PLANTSTAR_TTS_PORT`           | `5002`              | API bind port                        |
| `PLANTSTAR_TTS_DEFAULT_VOICE`  | `en_us_amy`         | Voice used when none is specified    |
| `PLANTSTAR_TTS_MAX_CHARS`      | `20000`             | Max text length per request          |

---

## Adding a voice / language

1. Find a voice at <https://huggingface.co/rhasspy/piper-voices> (note its
   language folder, name, and quality).
2. Add an entry to `config/voices.json` with a unique `id` and the
   `model` / `config` filenames plus their `model_url` / `config_url`.
3. Run `scripts/download_voices.sh <your_new_id>`.
4. `plantstar-tts list-voices` should now show it as `installed: yes`.

---

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pip install -e .
pytest            # unit + API tests (Piper is mocked; no models needed)
```

---

## Performance / benchmarking

Piper runs on CPU, so synthesis speed depends on the host. After deploying to an
APU, measure it there with the bundled benchmark:

```bash
python scripts/benchmark.py            # per-voice cold load, memory, RTF, latency
```

It reports the **real-time factor** (RTF = synthesis_time ÷ audio_seconds) for
short/medium/long messages. Record results using
[docs/bench-results-template.md](docs/bench-results-template.md) and use them to
pick voice quality tiers. Full guidance: [DEPLOY.md §6](DEPLOY.md#6-tuning-for-the-apu-cpu).

---

## Notes / limitations

- Piper is CPU-based. On a typical APU-class CPU, `medium`-quality voices
  synthesize a few times faster than real time. For lower latency on weak
  CPUs, switch a voice to its `low`-quality model in the manifest.
- The service holds each used voice model in memory after first use to avoid
  reload latency; expect ~50–120 MB RSS per loaded `medium` voice.
- This build was verified on the developer machine for API/CLI wiring, error
  handling, and the test suite. **Actual audio synthesis must be validated on
  the APU** (or via the Docker image) since Piper's binary wheels are
  platform/Python-version specific.
