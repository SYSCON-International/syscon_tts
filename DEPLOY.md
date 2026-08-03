# Deploying Syscon TTS

Provisioning and validation guide. The production target is the **PlantStar APU
(Linux x86-64, Python 3.9–3.11)**; the same steps work on any Linux box.

---

## 1. Install

```bash
# Confirm the interpreter is in range first:
python3 --version            # must be 3.9.x / 3.10.x / 3.11.x

pip install syscon-tts
syscon-tts download-voices   # one-time, needs internet
syscon-tts doctor            # must report "Ready."
```

`doctor` is the gate. It exits non-zero and explains the problem if Piper is
missing on a Linux host or no models are on disk. Wire it into provisioning
rather than discovering a broken TTS at the first alert.

**Why the Python constraint:** Piper's `piper-phonemize` dependency ships
prebuilt wheels only for CPython **3.9, 3.10, 3.11**, and only for Linux. On
Python 3.12+ or on Windows/macOS the dependency marker skips it and the install
still succeeds — you get a package that can replay cached audio but not
generate it. See [README.md](README.md#platform-support).

### Where the models go

By default, models land in `/var/lib/syscon-tts/voices` (or
`~/.local/share/syscon-tts/voices` if that is not writable). On an APU, point
them at the Django media tree instead so generated audio is served by the
existing `/media/` route:

```bash
export SYSCON_TTS_VOICES_DIR=<MEDIA_ROOT>/tts_voices
export SYSCON_TTS_ALERTS_DIR=<MEDIA_ROOT>/public_alert_sounds
syscon-tts download-voices
```

`MEDIA_ROOT` is `website/media/`, which is already gitignored — so the ~360 MB
of models never enter the source tree, and they sit beside the
`public_alert_sounds/` directory they feed.

---

## 2. Replace `call_tts()` in the APU

The APU's current implementation shells out to the proprietary VoiceText binary
(`PublicAddressWebSocketHandler.call_tts`):

```python
def call_tts(self, message, file_name):
    message = message.replace("\"", "")
    subprocess.run([
        "/usr/vt/sample/ttssample", "8", str(self.tts_server_ip),
        str(self.tts_server_port), str(message), str(len(message)),
        "public_alert_sounds", str(file_name), self.tts_voice_name, str(0)
    ])
    self.open_files.append(file_name)
```

Everything downstream — the websocket push, `/media/` serving,
`check_for_file_delete` — only cares that
`MEDIA_ROOT/public_alert_sounds/<file_name>.wav` exists. So this method is the
entire integration surface:

```python
from pathlib import Path
from django.conf import settings
from syscon_tts import AlertSynthesizer, SynthesisUnavailableError

# Module level, not per call: the instance caches loaded voice models, and
# rebuilding it per alert reintroduces a multi-second cold load every time.
_synthesizer = AlertSynthesizer()

def call_tts(self, message, file_name):
    message = message.replace('"', "")
    try:
        _synthesizer.ensure(
            message,
            file_name=file_name,
            voice=self.tts_voice_name,
            alerts_dir=Path(settings.MEDIA_ROOT, "public_alert_sounds"),
        )
    except SynthesisUnavailableError:
        # Development machine without Piper. Any previously generated WAV is
        # still returned by ensure(); this branch means there was none.
        logger.warning("TTS unavailable on this host for %r", file_name)
        raise
    self.open_files.append(file_name)
```

Pass `file_name` explicitly so the APU stays the single source of truth for
naming. The package computes the same value independently
(`get_valid_filename(text)[:50]`, verified byte-for-byte against Django), but
there is no reason to have two authorities.

### Voice IDs

The APU addresses voices by numeric `TTS_VOICE_ID` (default `100` =
`ENGLISH_FEMALE_KATE` → `"kate"`), mapped in
`base_app/global_definitions.py`. This package uses string ids like
`en_us_amy`. The two catalogues share no names, so you need a mapping.

Note the shape of the gap before writing one: the APU catalogue is Korean
(3–21), English (100–106), Chinese (200–205), and Japanese (300–307), while
this package ships en/es/fr/de. **Only the English block maps at all**, and the
four English female voices collapse onto `en_us_amy`. Adding Korean, Chinese,
and Japanese voices to the manifest from
<https://huggingface.co/rhasspy/piper-voices> is the way to close it.

### The `IS_LIVE` gate

`call_tts` is wrapped in `if settings.IS_LIVE:`, and development boxes set
`IS_LIVE = False`. **Do not flip it to test locally.** The same flag gates
`chown`/`chmod` calls that are Linux-only, plus module-level hardware imports
in the device-interface layer; flipping it breaks unrelated subsystems on
Windows. Test the synthesis path directly instead:

```bash
syscon-tts alert "Press 4 cavity pressure exceeded." \
  --alerts-dir <MEDIA_ROOT>/public_alert_sounds
```

---

## 3. Validate

```bash
syscon-tts doctor          # expect "Ready."
syscon-tts list-voices     # all six should show installed: yes

# Generate a real file and confirm it plays.
syscon-tts speak -v en_us_ryan -o test.wav \
  "Machine four cycle complete. Cavity pressure nominal."
file test.wav              # -> RIFF ... WAVE audio, 16 bit, mono 22050 Hz
ls -l test.wav             # tens-to-hundreds of KB, not 0
aplay test.wav

# Exercise the APU code path (cache first, then synthesis).
syscon-tts alert "Press 4 cavity pressure exceeded."
syscon-tts alert "Press 4 cavity pressure exceeded."   # -> [cached]
```

If `doctor` reports `voices installed 0/6`, the models are not where the
service expects; check `SYSCON_TTS_VOICES_DIR`. Synthesis then fails with a
`VoiceNotInstalledError` (HTTP `503`).

**Language note:** the Spanish/French/German voices are language-specific
models. Send them text *in their own language* — feeding English to a German
voice produces poor pronunciation.

---

## 4. Run as a systemd service (optional)

Only needed if you want the HTTP server. The APU itself does not — it imports
the library in-process.

See [`systemd/syscon-tts.service`](systemd/syscon-tts.service). Install into
`/opt/syscon-tts`, adjust `User` and the two path variables, then:

```bash
sudo cp systemd/syscon-tts.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now syscon-tts
systemctl status syscon-tts
```

The unit binds `127.0.0.1` by default. Change `SYSCON_TTS_HOST` only if
something off-box genuinely needs to reach it.

---

## 5. Air-gapped installs

Everything runs offline; only the model download needs internet.

1. On a networked machine with a **matching Linux x86-64 / Python 3.9–3.11**
   environment:
   ```bash
   pip download syscon-tts -d wheelhouse
   SYSCON_TTS_VOICES_DIR=./voices syscon-tts download-voices
   ```
2. Copy `wheelhouse/` and `voices/` to the APU.
3. Install from the local copies:
   ```bash
   pip install --no-index --find-links wheelhouse syscon-tts
   export SYSCON_TTS_VOICES_DIR=<MEDIA_ROOT>/tts_voices
   cp voices/* "$SYSCON_TTS_VOICES_DIR/"
   syscon-tts doctor
   ```

`pip download` must run on a host matching the APU's platform and interpreter,
or it will fetch wheels Piper cannot use.

---

## 6. Tuning for the APU CPU

Voices come in quality tiers: **low** (fastest, smallest), **medium**
(default, balanced), **high** (best, largest). Piper is CPU-only; on a weak APU
CPU, `medium` may synthesize slower than you want.

To make a voice faster, copy the bundled manifest, switch that voice to its
`low` model, and point `SYSCON_TTS_MANIFEST` at your copy — change the `medium`
occurrences in the voice's `model`, `config`, `model_url`, and `config_url`:

```
en_US-amy-medium.onnx   ->  en_US-amy-low.onnx
.../amy/medium/...      ->  .../amy/low/...
```

Then `syscon-tts download-voices en_us_amy` and restart. Not every voice
publishes a `low` tier — check the voice's folder at
<https://huggingface.co/rhasspy/piper-voices>.

Rule of thumb on APU-class CPUs: `medium` runs a few times faster than
real-time; `low` is faster still with a modest quality drop. Measure on the
actual hardware before committing.

### Benchmarking (post-deployment)

`scripts/benchmark.py` measures real synthesis performance on the host it runs
on. Run it **on the APU after deployment**. For each installed voice it reports
cold model-load time, resident-memory growth, and the **real-time factor
(RTF = synth_time ÷ audio_seconds)** plus latency for short/medium/long
messages.

```bash
python scripts/benchmark.py                 # all installed voices
python scripts/benchmark.py en_us_amy --runs 5
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
  cached in the `AlertSynthesizer`). This is why the APU should hold one
  instance rather than building one per alert.

Record results with [docs/bench-results-template.md](docs/bench-results-template.md).

---

## 7. Configuration reference

Full table in [README.md](README.md#configuration). The ones that matter on an
APU:

| Variable | Default | Purpose |
|---|---|---|
| `SYSCON_TTS_VOICES_DIR` | platform data dir | Where the `.onnx` models live |
| `SYSCON_TTS_ALERTS_DIR` | platform data dir | Where alert WAVs are written |
| `SYSCON_TTS_DEFAULT_VOICE` | `en_us_amy` | Voice used when none is requested |
| `SYSCON_TTS_MANIFEST` | bundled | Override the voice catalogue |
