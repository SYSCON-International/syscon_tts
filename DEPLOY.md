# Deploying Syscon TTS

Provisioning and validation guide. The production target is the **PlantStar APU
(Linux x86-64, Python 3.9–3.11)**; the same steps work on any Linux box.

This package is the only speech backend PlantStar ships — there is no VoiceText
fallback behind it.

---

## 1. Install

```bash
# Confirm the interpreter is in range first:
python3 --version            # must be 3.9.x / 3.10.x / 3.11.x

pip install syscon-tts
syscon-tts download-voices --language en-us   # one-time, needs internet
syscon-tts doctor                             # must report "Ready."
```

`doctor` is the gate. It exits non-zero and explains the problem if Piper is
missing on a Linux host, if no models are on disk, or if a configured voice id
does not exist. Wire it into provisioning rather than discovering a broken TTS
at the first alert.

**Why the Python constraint:** Piper's `piper-phonemize` dependency ships
prebuilt wheels only for CPython **3.9, 3.10, 3.11**, and only for Linux. On
Python 3.12+ or on Windows/macOS the dependency marker skips it and the install
still succeeds — you get a package that can replay cached audio but not
generate it. See [README.md](README.md#platform-support).

### Where the files go

| What | Where | Notes |
|---|---|---|
| Voice models | `/var/lib/syscon-tts/voices` | The default. No configuration needed. |
| Generated alert WAVs | `MEDIA_ROOT/public_alert_sounds` | Passed in by the APU (see §2). |

Voice models are large (~60 MB each), immutable, and re-downloadable, and they
are per-machine. `/var/lib` is the FHS home for exactly that, and it has three
practical advantages on an APU:

* **Backups stay small.** `backup_plantstar` copies the *entire* media tree
  (`shutil.copytree(MEDIA_DIRECTORY_PATH, ...)`) into every backup, which is
  then compressed, encrypted, and shipped off-box. Models under `MEDIA_ROOT`
  would add hundreds of megabytes to every backup at every site.
* **Code deploys do not touch it.** `sideload_code` only writes under
  `website/`, and `pip install -U` never touches `/var/lib`.
* **It cannot leak into git.**

Generated audio is the opposite — small, disposable, and it has to be
web-served, so it stays in `MEDIA_ROOT/public_alert_sounds` where the APU
already deletes it after playback.

If a site needs the models elsewhere (a separate data partition, say), set
`SYSCON_TTS_VOICES_DIR` and keep it consistent between the provisioning shell
and the Django process.

### Provision only the languages a site speaks

```bash
syscon-tts download-voices --language es-mx    # Mexican Spanish only
syscon-tts download-voices --language zh-hans --accept-license
syscon-tts list-voices                         # confirm what landed
```

A bare `download-voices` fetches the whole catalogue, which is rarely what a
single site wants.

### The license gate

Piper's public catalogue mixes public-domain voices with non-commercial ones.
Since PlantStar is sold, the bundled catalogue includes only voices whose
upstream model card permits commercial use, and the tooling refuses to quietly
install anything else:

* `list-voices` shows each voice's license, marking any that need review.
* `doctor` lists installed voices that still need a review before shipping.
* `download-voices` skips them in bulk and refuses them by name until you pass
  `--accept-license`.

**Mandarin needs a decision before any Chinese site goes live.**
`zh_CN-huayan` is the only Mandarin voice Piper 1.2 can run that is not
explicitly non-commercial, and its model card states the dataset license as
*Unknown*. The alternatives are worse: `zh_CN-xiao_ya` is CC BY-NC-SA (and
needs Piper ≥ 1.4), and `zh_CN-chaowen` is fine-tuned from it. Get sign-off, or
source a Mandarin voice elsewhere.

---

## 2. Replace `call_tts()` in the APU

The APU's previous implementation shelled out to the VoiceText binary
(`PublicAddressWebSocketHandler.call_tts`). Everything downstream — the
websocket push, `/media/` serving, `check_for_file_delete` — only cares that
`MEDIA_ROOT/public_alert_sounds/<file_name>.wav` exists, so that method is the
entire integration surface.

```python
import asyncio
import logging
from functools import lru_cache
from pathlib import Path

from django.conf import settings

from syscon_tts import AlertSynthesizer, SynthesisUnavailableError
from website.apps.plantstar_shared.base_app.utils.shared.directory_and_file_utils import (
    set_root_www_data_ownership_and_permissions_for_path,
)

logger = logging.getLogger("tornado_socket_manager")


@lru_cache(maxsize=1)
def _synthesizer():
    """Built on first use, never at import.

    The instance caches loaded voice models, so it must outlive the request --
    but building it at module scope would let an unprovisioned TTS install take
    down the whole Tornado process at startup.
    """
    return AlertSynthesizer(
        alerts_dir=Path(settings.MEDIA_ROOT, "public_alert_sounds"),
    )


def _render_alert_audio(message, file_name, language):
    """Blocking: runs in a worker thread, never on the IO loop."""
    result = _synthesizer().ensure(
        message, file_name=file_name, language=language
    )
    if not result.cached:
        # Everything under MEDIA_ROOT is root:www-data 0770 on a live system,
        # and the web server has to read this file back out. The helper is
        # IS_LIVE-gated, so it is a no-op on a development box.
        set_root_www_data_ownership_and_permissions_for_path(result.path)
    return result


async def call_tts(self, message, file_name):
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, _render_alert_audio, message, file_name, self.tts_language
        )
    except SynthesisUnavailableError:
        # This host can never synthesize (no Piper). Any previously generated
        # WAV is still returned by ensure(); reaching here means there was none.
        logger.warning("TTS unavailable on this host for %r", file_name)
        raise

    self.open_files.append(file_name)
```

Three changes from the VoiceText version worth calling out:

1. **It must run in a thread.** `call_tts` is invoked from the async
   `send_message_to_all_web_socket_client`, so the caller becomes
   `await self.call_tts(message, file_name)`. The old `subprocess.run` blocked
   too, but it blocked on a *remote* VT server; Piper is one to three seconds
   of local CPU, and on the IO loop that stalls every other socket.
2. **Ownership and permissions.** This package writes the file `0644` by
   default (configurable with `SYSCON_TTS_FILE_MODE`), but it does not know
   about `www-data`. Apply the APU's own helper afterwards, and create the
   directory with `create_directory_at_path_and_set_ownership` during setup
   rather than letting `ensure()` create it.
3. **`file_name` stays the APU's.** This package computes the same value
   (`get_valid_filename(text)[:50]`, verified byte-for-byte against Django),
   but there is no reason to have two authorities.

The old `message.replace('"', "")` was argv hygiene for the binary. It is
harmless now, and only affects spoken text — `get_valid_filename` strips quotes
from the file name either way.

Note that `check_for_file_delete` unlinks each WAV once no client holds it, so
the cache-first path rarely hits in practice. The win from a long-lived
`AlertSynthesizer` is the **in-memory model cache**, not the file cache — call
`synthesizer.preload(language=...)` in `message_controller_initialization` to
pay the model load at startup instead of during the first announcement.

### Choosing the voice

The VoiceText numeric `TTS_VOICE_ID` catalogue is gone. Two options, in order
of preference:

1. **Announce in the site's language.** Pass `language=` (`"es-mx"`,
   `"zh-hans"`, `"en-us"` — Django's locale spelling works as-is) and let the
   package resolve it. Nothing to map, nothing to keep in sync.
2. **Store a voice id.** Replace the integer `TTS Voice ID` setting with a
   string `TTS Voice Name` holding an id such as `es_mx_ald`, and pass
   `voice=`. Ids are stable across releases; new voices are added, never
   renamed.

Per-language defaults let one site announce in several languages without any
APU-side table:

```
SYSCON_TTS_DEFAULT_VOICES=es-mx=es_mx_ald,zh-hans=zh_cn_huayan
```

Whichever you pick, note that `TTS_VOICE_ID_TO_VOICE_NAME_MAP[int(voice_id)]`
in `message_controller_initialization` raises `KeyError` on the fixture default
of `0` — use `.get()` with a fallback when you touch that line.

### The `IS_LIVE` gate

`call_tts` sits behind `if settings.IS_LIVE:`, and development boxes set
`IS_LIVE = False`. With Piper the real precondition is "Piper is importable and
a voice model is on disk", not "this is production", so prefer:

```python
from syscon_tts import piper_available

if settings.IS_LIVE or piper_available():
    ...
```

**Do not flip `IS_LIVE` itself.** The same flag gates the `chown`/`chmod` calls
above and module-level hardware imports in the device-interface layer; flipping
it breaks unrelated subsystems on Windows. To test the synthesis path directly:

```bash
syscon-tts alert "Press 4 cavity pressure exceeded." \
  --alerts-dir <MEDIA_ROOT>/public_alert_sounds
```

---

## 3. Backup and restore

The old `/usr/vt` VoiceText directory (`TTS_DIRECTORY_PATH` in
`backup_restore_plantstar_base.py`) no longer exists, and that handling can go.

The recommended policy for its replacement:

* **Back up the selection, not the models.** Which voices a site uses is a
  handful of bytes in the settings table, already covered by the database dump.
  Restore re-runs `syscon-tts download-voices --language <site language>`.
* **Air-gapped sites** copy `/var/lib/syscon-tts/voices` across by hand (§6),
  or add it to the backup explicitly — it is a deliberate opt-in, not the
  default, because it multiplies backup size.

After a restore, `syscon-tts doctor` is the check that the site can still
speak.

---

## 4. Validate

```bash
syscon-tts doctor          # expect "Ready."
syscon-tts list-voices     # installed: yes for what you provisioned

# Generate a real file and confirm it plays.
syscon-tts speak -v en_us_kristin -o test.wav \
  "Machine four cycle complete. Cavity pressure nominal."
file test.wav              # -> RIFF ... WAVE audio, 16 bit, mono 22050 Hz
ls -l test.wav             # tens-to-hundreds of KB, not 0
aplay test.wav

# Exercise the APU code path (cache first, then synthesis).
syscon-tts alert "Press 4 cavity pressure exceeded."
syscon-tts alert "Press 4 cavity pressure exceeded."   # -> [cached]

# And the non-English path, if the site uses one.
syscon-tts alert -l es-mx "Presion de cavidad excedida en prensa 4."
```

If `doctor` reports `voices installed 0/N`, the models are not where the
service expects; check `SYSCON_TTS_VOICES_DIR`. Synthesis then fails with a
`VoiceNotInstalledError` (HTTP `503`).

### Non-English text: three things to check

* **One utterance, one voice.** A message mixing English and Chinese is spoken
  entirely by whichever voice was selected. Split it instead.
* **File names keep their own characters.** `get_valid_filename` is
  Unicode-aware, so a Chinese message produces a Chinese file name. The URL the
  APU hands the browser (`hostname + "/media/public_alert_sounds/" + name +
  ".wav"`) therefore needs percent-encoding.
* **The 50-character truncation counts characters, not bytes.** Fifty Han
  characters is a much longer utterance than fifty Latin ones, so distinct
  messages collide sooner. If that bites, pass a `file_name` with a short hash
  suffix — the APU already controls that value.

---

## 5. Run as a systemd service (optional)

Only needed if you want the HTTP server. The APU itself does not — it imports
the library in-process.

See [`systemd/syscon-tts.service`](systemd/syscon-tts.service). Install into
`/opt/syscon-tts`, adjust `User` and the path variables, then:

```bash
sudo cp systemd/syscon-tts.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now syscon-tts
systemctl status syscon-tts
```

The unit binds `127.0.0.1` by default. Change `SYSCON_TTS_HOST` only if
something off-box genuinely needs to reach it.

---

## 6. Air-gapped installs

Everything runs offline; only the model download needs internet.

1. On a networked machine with a **matching Linux x86-64 / Python 3.9–3.11**
   environment:
   ```bash
   pip download syscon-tts -d wheelhouse
   SYSCON_TTS_VOICES_DIR=./voices syscon-tts download-voices --language es-mx
   ```
2. Copy `wheelhouse/` and `voices/` to the APU.
3. Install from the local copies:
   ```bash
   pip install --no-index --find-links wheelhouse syscon-tts
   sudo mkdir -p /var/lib/syscon-tts/voices
   sudo cp voices/* /var/lib/syscon-tts/voices/
   syscon-tts doctor
   ```

`pip download` must run on a host matching the APU's platform and interpreter,
or it will fetch wheels Piper cannot use.

Models copied in this way are picked up even if they are not in the catalogue:
any `.onnx` + `.onnx.json` pair in the voices directory is discovered, with its
language read from the Piper config. That is also how a site adds a
custom-trained voice — see [README.md](README.md#per-deployment-voices).

---

## 7. Tuning for the APU CPU

Voices come in quality tiers: **x_low** and **low** (fastest, smallest),
**medium** (default, balanced), **high** (best, largest). Piper is CPU-only; on
a weak APU CPU, `medium` may synthesize slower than you want.

To make a voice faster, add its lower-quality model to an overlay manifest and
point `SYSCON_TTS_EXTRA_MANIFEST` at it — copy the entry, change the `medium`
occurrences in `model`, `config`, `model_url`, and `config_url`:

```
es_MX-ald-medium.onnx   ->  es_MX-ald-x_low.onnx
.../ald/medium/...      ->  .../ald/x_low/...
```

The shipped catalogue carries only `medium` models, so a site that wants a
lower tier adds it this way. Not every voice publishes a lower tier — check
the voice's folder at <https://huggingface.co/rhasspy/piper-voices>.

Rule of thumb on APU-class CPUs: `medium` runs a few times faster than
real-time; `low` is faster still with a modest quality drop. Measure on the
actual hardware before committing.

### Memory

Each resident `medium` model costs roughly 50–120 MB of RSS. The engine keeps
up to `SYSCON_TTS_MAX_LOADED_VOICES` (default 3) loaded and evicts
least-recently-used beyond that. A trilingual site therefore holds ~180 MB —
and both the HTTP and HTTPS Tornado socket managers load their own copies, so
size for two processes.

### Benchmarking (post-deployment)

`scripts/benchmark.py` measures real synthesis performance on the host it runs
on. Run it **on the APU after deployment**. For each installed voice it reports
cold model-load time, resident-memory growth, and the **real-time factor
(RTF = synth_time ÷ audio_seconds)** plus latency for short/medium/long
messages.

```bash
python scripts/benchmark.py                     # all installed voices
python scripts/benchmark.py en_us_kristin --runs 5
python scripts/benchmark.py --json > "bench-$(hostname).json"
```

Example output (format only — RTF depends entirely on the host CPU):

```
VOICE            COLD LOAD     MEM  SHORT RTF  MED RTF  LONG RTF  LONG LAT
--------------------------------------------------------------------------
en_us_kristin       1.312s 119.8MB      0.081    0.095     0.073    1.326s
...
```

Interpreting it:
- **RTF < 1.0** means faster than real-time; the smaller, the snappier. If RTF
  approaches or exceeds 1.0 for the messages you actually send, drop that voice
  to a lower tier (above) and re-benchmark.
- **LONG LAT** is the wall-clock time to synthesize the long sample — the most
  realistic "how long will an operator wait" figure.
- **MEM** is the RSS added by loading that voice; sum the voices you expect to
  keep loaded to size the process.
- **COLD LOAD** is paid once per voice on first use, then amortized (voices stay
  cached in the `AlertSynthesizer`). This is why the APU should hold one
  instance rather than building one per alert, and why `preload()` at startup
  is worth the line.

Record results with [docs/bench-results-template.md](docs/bench-results-template.md).

---

## 8. Configuration reference

Full table in [README.md](README.md#configuration). The ones that matter on an
APU:

| Variable | Default | Purpose |
|---|---|---|
| `SYSCON_TTS_VOICES_DIR` | `/var/lib/syscon-tts/voices` | Where the `.onnx` models live |
| `SYSCON_TTS_ALERTS_DIR` | platform data dir | Where alert WAVs are written (the APU passes this in code) |
| `SYSCON_TTS_DEFAULT_VOICE` | `en_us_kristin` | Voice used when none is requested |
| `SYSCON_TTS_DEFAULT_VOICES` | — | Per-language defaults, `es-mx=es_mx_ald,...` |
| `SYSCON_TTS_MAX_LOADED_VOICES` | `3` | Models kept resident in memory |
| `SYSCON_TTS_FILE_MODE` | `0644` | Mode for generated audio |
| `SYSCON_TTS_EXTRA_MANIFEST` | — | Site voices layered over the catalogue |
| `SYSCON_TTS_MANIFEST` | bundled | Replace the voice catalogue outright |

Inside Django, prefer constructor keywords over environment variables — the
Process Spawner would otherwise have to propagate them to the Tornado socket
managers:

```python
AlertSynthesizer(
    alerts_dir=Path(settings.MEDIA_ROOT, "public_alert_sounds"),
    default_voices={"es_MX": "es_mx_ald"},
)
```
