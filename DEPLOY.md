# Deploying PlantStar TTS

Deployment and validation guide for the offline text-to-speech service.
The production target is the **PlantStar APU (Linux x86-64)**; the same steps
work on any Linux box, and the Docker path works on macOS/Windows too.

> **Verified:** the Docker build, dependency install (`piper-tts` +
> `piper-phonemize` from Linux wheels), server startup, WAV + MP3 synthesis,
> and all six bundled voices were validated end-to-end on 2026-07-14 (native
> arm64 Linux container). The APU uses the x86-64 wheel of the same packages.

---

## 1. Pick a deployment path

| Your host | Recommended path |
|-----------|------------------|
| APU / any Linux x86-64 with **Python 3.9–3.11** | [Bare metal](#2a-bare-metal-linux-3911) |
| Any host **without** a suitable Python (incl. macOS/Windows, or Python 3.12+) | [Docker](#2b-docker) |

**Why the Python constraint:** Piper's `piper-phonemize` dependency ships
prebuilt wheels only for CPython **3.9, 3.10, 3.11**. There is **no macOS
Apple-Silicon (arm64) wheel at all** — on an M-series Mac you must use Docker.
Linux x86-64 and aarch64 both have wheels, so the APU is fully supported.

---

## 2a. Bare metal (Linux, Python 3.9–3.11)

```bash
# Confirm the interpreter is in range first:
python3 --version            # must be 3.9.x / 3.10.x / 3.11.x

cd plantstar-tts
scripts/install.sh           # creates .venv, installs deps, downloads all 6 voices
source .venv/bin/activate
plantstar-tts serve --port 5002
```

`scripts/install.sh` needs internet **once** (for pip + voice models). For an
air-gapped APU, see [§5](#5-air-gapped-installs).

Run it as a managed service with the provided unit — see [§4](#4-run-as-a-systemd-service).

---

## 2b. Docker

Requires Docker Engine / Docker Desktop.

```bash
cd plantstar-tts

# 1. Fetch voice models on a networked machine (models are mounted, not baked in):
scripts/download_voices.sh                 # all 6 voices
#   or a subset:  scripts/download_voices.sh en_us_amy en_us_ryan

# 2. Build the image (Python 3.11 is pinned inside the Dockerfile):
docker build -t plantstar-tts .

# 3. Run, mounting the voices directory:
docker run -d --name plantstar-tts -p 5002:5002 \
  -v "$PWD/voices:/app/voices" plantstar-tts
```

Lifecycle:

```bash
docker stop plantstar-tts       # stop (keeps the container + image)
docker start plantstar-tts      # restart it
docker logs -f plantstar-tts    # follow logs
docker rm -f plantstar-tts      # remove the container (models on host are kept)
```

> On Apple Silicon, Docker builds a native **arm64** image (fast). To rehearse
> the exact x86-64 APU build instead, add `--platform linux/amd64` to
> `docker build` / `docker run` — it runs under emulation and is slower.

---

## 3. Validate the deployment

Run these against the running service. **Step 3 is the real proof** — it must
produce a playable audio file.

```bash
# 1. Up, and how many voices are installed?
curl -s http://localhost:5002/health
#    expect: {"status":"ok","voices_total":6,"voices_installed":6}

# 2. List voices
curl -s http://localhost:5002/voices | python3 -m json.tool

# 3. Generate audio
curl -s -X POST http://localhost:5002/synthesize \
  -H 'Content-Type: application/json' \
  -d '{"text":"Machine four cycle complete. Cavity pressure nominal.","voice":"en_us_ryan"}' \
  -o test.wav

# 4. Confirm it is a real, non-empty WAV
file test.wav        # -> RIFF ... WAVE audio, 16 bit, mono 22050 Hz
ls -l test.wav       # tens-to-hundreds of KB, not 0
aplay test.wav       # Linux; on macOS use: afplay test.wav
```

CLI path (bare metal):

```bash
plantstar-tts list-voices
plantstar-tts speak -v de_de_thorsten -o ansage.wav "Maschine vier Zyklus abgeschlossen."
```

If `voices_installed` is `0`, the models are not where the service expects them
(`voices/`, or the mounted volume in Docker); synthesis will then return `503`.

**Language note:** the Spanish/French/German voices are language-specific
models. Send them text *in their own language* — feeding English to a German
voice produces poor pronunciation.

---

## 4. Run as a systemd service

See [`systemd/plantstar-tts.service`](systemd/plantstar-tts.service). Deploy the
app to `/opt/plantstar-tts`, adjust `User`/paths, then:

```bash
sudo cp systemd/plantstar-tts.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now plantstar-tts
systemctl status plantstar-tts
```

---

## 5. Air-gapped installs

The service runs fully offline; only model download needs the internet.

1. On any networked machine: `scripts/download_voices.sh`
2. Copy the resulting `voices/` directory onto the APU (same repo location, or
   point `PLANTSTAR_TTS_VOICES_DIR` at it).
3. For bare metal, also pre-stage pip dependencies (e.g. `pip download -r
   requirements.txt -d wheelhouse` on a matching Linux x86-64 / Python 3.9–3.11
   host, copy `wheelhouse/`, then `pip install --no-index --find-links wheelhouse -e .`).
   For Docker, `docker save plantstar-tts | gzip > image.tgz`, copy it, and
   `docker load < image.tgz` on the APU.

---

## 6. Tuning for the APU CPU

Voices come in quality tiers: **low** (fastest, smallest), **medium**
(default, balanced), **high** (best, largest). Piper is CPU-only; on a weak APU
CPU, `medium` may synthesize slower than you want.

To make a voice faster, switch it to its `low` model in
[`config/voices.json`](config/voices.json) — change the `medium` occurrences in
that voice's `model`, `config`, `model_url`, and `config_url` to `low`, e.g.:

```
en_US-amy-medium.onnx   ->  en_US-amy-low.onnx
.../amy/medium/...       ->  .../amy/low/...
```

Then re-download that voice (`scripts/download_voices.sh en_us_amy`) and
restart the service. Not every voice publishes a `low` tier — check the voice's
folder at <https://huggingface.co/rhasspy/piper-voices>.

Rule of thumb on APU-class CPUs: `medium` runs a few times faster than
real-time; `low` is faster still with a modest quality drop. Measure on the
actual hardware before committing to a tier — use the benchmark below.

### Benchmarking on the APU (post-deployment)

`scripts/benchmark.py` measures real synthesis performance on the host it runs
on. Run it **on the APU after deployment** so the numbers reflect the real CPU.
For each installed voice it reports cold model-load time, resident-memory
growth, and the **real-time factor (RTF = synth_time ÷ audio_seconds)** plus
latency for short/medium/long messages.

```bash
# Bare metal
source .venv/bin/activate
python scripts/benchmark.py                 # all installed voices
python scripts/benchmark.py en_us_amy --runs 5

# Docker (copy the script into the running container, then exec)
docker cp scripts/benchmark.py plantstar-tts:/app/benchmark.py
docker exec plantstar-tts python /app/benchmark.py

# Archive results per host for comparison
python scripts/benchmark.py --json > "bench-$(hostname).json"
```

Example output (format only — RTF depends entirely on the host CPU):

```
VOICE            COLD LOAD     MEM  SHORT RTF  MED RTF  LONG RTF  LONG LAT
--------------------------------------------------------------------------
en_us_amy           1.312s 119.8MB      0.081    0.095     0.073    1.326s
...
```

Interpreting it:
- **RTF < 1.0** means faster than real-time; the smaller, the snappier. If RTF
  approaches or exceeds 1.0 for the messages you actually send, drop that voice
  to its `low` model (above) and re-benchmark.
- **LONG LAT** is the wall-clock time to synthesize the long sample — the most
  realistic "how long will an operator wait" figure.
- **MEM** is the RSS added by loading that voice; sum the voices you expect to
  keep loaded to size the process.
- **COLD LOAD** is paid once per voice on first use, then amortized (voices stay
  cached). Preload critical voices at startup if first-hit latency matters.

---

## 7. Configuration reference

All settings are environment variables (see the table in
[README.md](README.md#configuration-environment-variables)). The most common:

| Variable | Default | Purpose |
|----------|---------|---------|
| `PLANTSTAR_TTS_PORT` | `5002` | API bind port |
| `PLANTSTAR_TTS_DEFAULT_VOICE` | `en_us_amy` | Voice used when none is requested |
| `PLANTSTAR_TTS_VOICES_DIR` | `voices/` | Where the `.onnx` models live |
