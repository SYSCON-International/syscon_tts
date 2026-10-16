# Deploying Syscon TTS

Provisioning and validation guide. The production target is the **PlantStar APU
(Linux x86-64, Python 3.9–3.11)**; the same steps work on any Linux box.

This package is the only speech backend PlantStar ships — there is no VoiceText
fallback behind it.

---

## 1. Install

# In the commands below, `<apu-venv>` is the APU's own virtualenv (the one the
# Tornado managers run from), not a separate one — install into that, or the
# library the APU imports is a different install from the one you pinned.

```bash
# Confirm the interpreter is in range first:
<apu-venv>/bin/python --version      # must be 3.9.x / 3.10.x / 3.11.x

# PINNED install. A bare `pip install syscon-tts` is wrong twice over: it floats
# the native runtime (onnxruntime, piper-phonemize, protobuf, flatbuffers), so
# two sites provisioned weeks apart run different engines, AND it can resolve to
# the mistaken 0.1.0 upload (slated to be yanked). Always pin the version.

# Preferred — hash-locked, fully reproducible. The lock is published as a GitHub
# release asset per version, built ON LINUX/py3.11 (a lock generated on Windows
# or macOS omits piper-tts, because the dependency marker excludes those
# platforms). Download the asset for THIS release, then:
<apu-venv>/bin/pip install --require-hashes -r requirements-tts-<version>-linux-py311.txt

# Fallback if you cannot use the lock — still pinned to an exact version, just
# without transitive hash pinning (NOT a bare float):
<apu-venv>/bin/pip install "syscon-tts==<this release>"

# To regenerate the lock for a release, run on a Linux/py3.11 host:
#   pip-compile --generate-hashes --output-file requirements-tts-<version>-linux-py311.txt \
#       <(echo "syscon-tts==<this release>")
# and attach it to the release (it doubles as the air-gapped wheelhouse input, §6).

<apu-venv>/bin/syscon-tts download-voices --language en-us   # one-time, needs internet
<apu-venv>/bin/syscon-tts doctor                             # must report "Ready."
```

The host also needs **glibc ≥ 2.28** (RHEL/CentOS 8+, Debian 10+,
Ubuntu 18.10+) on x86-64 or aarch64 — that is all `piper-phonemize` ships a
wheel for. On anything older, musl, or 32-bit ARM the engine will not install;
see pyproject's dependency comment.

> **Point `doctor` at what the APU actually reads.** A bare `syscon-tts doctor`
> checks the package defaults — `/var/lib/syscon-tts/voices` and the bundled
> catalogue. The APU embeds the library with its *own* paths and catalogue, so
> a bare `doctor` can print "Ready." while the PA page reports "not installed."
> **On the APU, ignore the `/var/lib` path entirely** (that is the standalone
> default, not the APU's) and run the gate with the APU's environment, from the
> APU's venv, as a user that can read the voices directory — on a `0770`
> voices dir that means root (`sudo`) or a `www-data`-group account:
>
> ```bash
> sudo env \
>   SYSCON_TTS_VOICES_DIR="$BASE_DIR/text_to_speech_voices" \
>   SYSCON_TTS_MANIFEST="$BASE_DIR/.../text_to_speech_voice_catalogue.json" \
>   SYSCON_TTS_WORKER_USER=<the worker account> \
>   <apu-venv>/bin/syscon-tts doctor --language es-mx
> ```
>
> Fill `$BASE_DIR` and the catalogue path from the APU's Django settings — they
> are **not** the package defaults, and `syscon-tts` is on the venv's PATH, not
> root's. Running it as root also lets `doctor` exercise the real worker drop to
> `SYSCON_TTS_WORKER_USER` (C-66).
>
> **What this gate still cannot see**, so check it separately: it validates the
> voice configured in the *catalogue/env*, not the per-site **TTS Voice
> Overrides** value the APU may have stored (that lives in the APU DB); and it
> does not know the APU catalogue's own license state — a voice held in the
> package catalogue (zh_cn_huayan) is only held on the APU if the APU catalogue
> mirrors the hold, and a voice cleared here is only offered on the APU once
> the APU catalogue releases it (C-63). Confirm the overrides value and the
> APU catalogue's holds by hand.

`doctor` is the gate. It exits non-zero and explains the problem if Piper is
missing on a Linux host, if no models are on disk, if an installed model does
not match its pinned SHA-256, or if a configured voice id does not exist. Wire
it into provisioning rather than discovering a broken TTS at the first alert.
It also prints the data directory it chose and why. Re-hashing every model
takes a moment; `doctor --quick` skips that check for routine use.

**Why the Python constraint:** Piper's `piper-phonemize` 1.1.0 dependency
publishes Linux wheels only for CPython **3.9, 3.10, 3.11** (plus Intel-macOS
wheels, and nothing for Windows). The dependency marker installs Piper only on
Linux with Python < 3.12. Anywhere else the install still succeeds — you get a
package that can replay cached audio but not generate it. See
[README.md](README.md#platform-support).

**Run provisioning as the user that can write the voices directory.** On Linux
`/var/lib/syscon-tts` is used whenever it exists, even when the current user
cannot write to it — models only need to be readable, so `doctor` run as an
ordinary user still finds what `sudo` installed. `download-voices` into it
without write access fails with an explicit permission error; re-run it with
`sudo` (or point `SYSCON_TTS_VOICES_DIR` somewhere writable).

An interrupted download (Ctrl-C, a dropped SSH session) leaves a partial
`.syscon-tts-*.part` file in the voices directory. `doctor` reports these, and
the next `download-voices` run removes any that have not been written to for
an hour, so just rerun it.

**Downloads are pinned and verified.** Catalogue URLs point at one commit of
`rhasspy/piper-voices` rather than `main`, and each file is checked against
the SHA-256 in the manifest before it is installed; a mismatch is refused
(`IntegrityError`) and the file is never put where the engine would load it.

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

### Engine licensing

The voices are not the only licence question. `piper-tts` 1.2.0 is MIT, but
its required `piper-phonemize~=1.1.0` wheels bundle **espeak-ng (GPL-3.0)**,
and `piper-tts` ≥ 1.3 (now `OHF-voice/piper1-gpl`) is GPL-3.0-or-later.
Whether PlantStar can ship that on customer hardware is **open — a legal
decision is pending.** Do not upgrade `piper-tts` past 1.2.x on an APU, and do
not change the exact `piper-tts==1.2.0` pin, without legal review; a test
fails if the pin moves. See the README's
[Engine licensing](README.md#engine-licensing) section.

### Synthesis runs in an isolated worker process

The `espeak-ng` that `piper-phonemize 1.1.0` bundles (a 2023 build) can
**segfault on crafted text** — reproduced at 150–200 characters, inside the
APU's 200-character public-address limit. Synthesis therefore does **not** run
in the process that calls `ensure()`. On a host that can run Piper,
`AlertSynthesizer` spawns a separate worker process for it, so a crash there
surfaces as a `SynthesisError` the caller catches (the APU already swallows it
and the announcement simply goes silent) instead of taking down the root
Tornado socket manager and every websocket it serves. The worker keeps the
voice-model cache, so isolation costs nothing in *per-announcement latency* (the
cache lives in the worker and is reused); it does cost one extra resident
process holding the models — see §7 "Memory". It is spawned lazily on the first
cache miss and respawned if it ever dies.

**Set `SYSCON_TTS_WORKER_USER` on the APU** (or pass `worker_user=` in code).
Because the APU's Tornado runs as root, this is what keeps a memory-corruption
bug from having root to work with. Isolation contains the *crash* either way,
but a fault in a worker that is still root is corruption with root's privileges,
so this is not optional on the APU. **If it is unset and the worker would run as
root, the first synthesis logs a warning** (`Synthesis worker is running as
root…`) — treat that line in the APU log as a provisioning defect, not noise.

The account has to be able to *read* the voices directory — and on the APU that
directory is `root:www-data 0770`, so a brand-new dedicated account **fails
silently**: the worker cannot open the models and every announcement dies with
`VoiceNotInstalledError`. `doctor` now catches this (it loads a voice through a
real worker; run it as root on the APU so it exercises the drop), but choose one
of these so it never happens:

- **A dedicated `syscon-tts` account, added to group `www-data`.** It then has
  the group's read access to `0770`, and shares nothing else with the web
  server. Create it as a system account with no login shell:
  `sudo useradd --system --no-create-home --shell /usr/sbin/nologin -g www-data syscon-tts`,
  then `SYSCON_TTS_WORKER_USER=syscon-tts`. **Preferred.**
- **A dedicated group on a tighter voices directory.** Make the voices dir
  `root:<ttsgroup> 0750` with models `0640` (this also satisfies C-5 part 2 —
  www-data should not be able to *write* models the root process loads), add the
  worker account to `<ttsgroup>`, and set `SYSCON_TTS_WORKER_USER` to it.

Do **not** set it to `www-data` itself: that shares the web server's uid, so a
compromised web process could `ptrace` the worker, read its `/proc`, or
`SIGSTOP` it (which the `SYSCON_TTS_WORKER_TIMEOUT` below then has to clean up).
Use a separate account.

The worker reads models and returns audio over a hardened channel: the root
caller never unpickles what the worker sends (replies are length-framed bytes
decoded as JSON, not a pickle stream), so a compromised worker cannot pivot
into the parent. A worker that *hangs* rather than crashes — crafted text that
sends espeak-ng into a loop — is bounded by `SYSCON_TTS_WORKER_TIMEOUT`
(default 30 s): past it the worker is killed and the call raises
`SynthesisError`. Lower it only if an announcement should fail faster; a
200-character alert renders in well under a second.

Isolation can be turned off with `SYSCON_TTS_WORKER_ISOLATION=0` (or
`worker_isolation=False`) — do this only to diagnose, never on an APU. When
you capture a crashing message, add it to the worker tests
(`tests/test_worker.py`) so CI keeps proving the isolation holds; the suite
already drives a crash *and* a hang through `AlertSynthesizer.ensure()` on
every platform via the self-test hook, and against real Piper in the Linux
`synthesis` CI job.

---

## 2. Replace `call_tts()` in the APU

> ⚠️ **This recipe is stale — do not follow it verbatim for the current APU.**
> It is written against the handler as it was *before* the Syscon-TTS
> integration branch (`v2.0.2.8.0-1823-Syscon-TTS`). It references
> `send_message_to_all_web_socket_client`, `tts_voice_name` and per-client
> rendering that no longer exist, a 1-argument `call_tts` (the shipped one
> takes `message, file_name, language_code`), and tells you *not* to pass
> `file_name` — but the shipped handler does pass an explicitly-built name.
> See **`SECURITY_HANDOFF_APU_TTS.md` → "C-10: §2 call_tts recipe"** for the
> reconciliation against the shipped handler and the file-name contract before
> relying on anything below.

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


def _render_alert_audio(message, language):
    """Blocking: runs in a worker thread, never on the IO loop."""
    return _synthesizer().ensure(message, language=language)


async def call_tts(self, message):
    """Render ``message`` and return the file name the audio was written to."""
    try:
        result = await asyncio.get_running_loop().run_in_executor(
            None, _render_alert_audio, message, self.tts_language
        )
    except SynthesisUnavailableError:
        # This host can never synthesize (no Piper). A matching previously
        # generated WAV is still returned by ensure(); reaching here means
        # there was none.
        logger.warning("TTS unavailable on this host for %r", message)
        raise

    self.open_files.append(result.file_name)
    return result.file_name
```

**Use the name `call_tts` returns** for everything downstream: the client's
audio URL (`send_text_to_speech_file`) and `open_files`. Do not pass a
`file_name` into `ensure()`, and do not use the name the queue item carries.
Producers name files differently: `AlertProcessor` builds names that end in a
UTC offset (`+00-00`), and `ensure()` does not accept `+`. Forwarding such a
name raises `InvalidAlertNameError` on every machine alert. The APU swallows
that exception, so those alerts would go silent. Letting the package choose
the name avoids this, whichever producer queued the message.

If something needs the name before the audio exists, ask the package:
`_synthesizer().file_name_for(message, language=self.tts_language)` returns
the name `ensure()` will use for the same text and language. It reads the
manifest but loads no model, so it is cheap.

**`self.tts_language` does not exist yet** on `PublicAddressWebSocketHandler`.
The integrator adds it: set it in `message_controller_initialization` (from
the site's locale setting, for example), and copy it across in
`transfer_message_controller_variables` alongside `tts_voice_name`, so every
handler announces in the same voice.

Three changes from the VoiceText version worth calling out:

1. **It must run in a thread.** `call_tts` is invoked from the async
   `send_message_to_all_web_socket_client`, so the caller becomes
   `file_name = await self.call_tts(message)`. The old `subprocess.run` blocked
   too, but it blocked on a *remote* VT server; Piper is one to three seconds
   of local CPU, and on the IO loop that stalls every other socket.
2. **Ownership and permissions.** This package writes the file `0644` by
   default (configurable with `SYSCON_TTS_FILE_MODE`). It sets the mode on the
   open file before the rename, so the web server can read the WAV as soon as
   it appears. Do **not** follow up with
   `set_root_www_data_ownership_and_permissions_for_path` or any other chown
   or chmod by path. The Tornado manager runs as root, and the alerts
   directory is `root:www-data 0770`, so www-data can replace the new WAV with
   a symlink between the rename and the chown. Root would then chown or chmod
   whatever the symlink points at, such as `/etc/shadow`. The file does not
   need www-data ownership: `0644` already lets the web server read it, and
   root does the deleting. Create the directory once, at provisioning time,
   and set ownership on the directory itself — never with a helper that walks
   its contents:

   ```bash
   install -d -o root -g www-data -m 0770 "$MEDIA_ROOT/public_alert_sounds"
   ```

   Do **not** use `create_directory_at_path_and_set_ownership` (or any helper
   that runs `chown`/`chmod` by path over every entry in the directory). Those
   walks follow symlinks and run as root on a www-data-writable directory, so a
   planted symlink gets its target re-owned `root:www-data 0770` — a direct
   root-escalation route (read `/etc/shadow`, drop a sudoers file). Setting the
   mode on the directory once, at creation, needs no such walk. `ensure()` also
   creates the directory safely on its own (`O_NOFOLLOW`, mode on the
   descriptor) if you would rather not pre-create it; what must not happen is a
   recurring root chown/chmod walk over its contents.
3. **The file name comes from the package, not `get_valid_filename`.**
   The package's name is the first 41 sanitized characters plus a hash of the
   full text and voice (50 characters at most). The old `[:50]` truncation gave
   two messages sharing a long prefix the same file, so a listener still
   waiting to play the first heard the second.

The old `message.replace('"', "")` was argv hygiene for the binary. It is
harmless now, and only affects spoken text — keep it or drop it, but apply it
it before `call_tts` (and before any `file_name_for()` call), since the name
hashes the text.

The file cache does real work. `main()` calls `call_tts` once **per connected
client** for the same message, so with N clients `ensure()` renders once and
is a cache hit the other N−1 times — and concurrent calls for the same file
wait for the first rather than rendering N times. A hit requires the file's
embedded fingerprint (text, voice, speed, sentence silence) to match, so a
leftover VoiceText WAV is re-rendered, never announced. `check_for_file_delete`
still unlinks each WAV once no client holds it. The other win from a
long-lived `AlertSynthesizer` is the **in-memory model cache** — warm it at
startup so the multi-second model load is not paid during the first
announcement.

`preload()` is a multi-second CPU burn (it loads the model), so it must **not**
run on the Tornado IO loop — doing so stalls every socket that manager serves
for ~1.5 s per controller start. Fire it off the loop, and swallow failures, so
an unprovisioned or dev host (no model, no Piper) does not close the PA socket
on open. Match the APU's own `prepare_text_to_speech`:

```python
async def message_controller_initialization(self):
    ...
    language = self.tts_language

    def _warm():
        # Build the synthesizer AND preload inside the executor. The FIRST
        # _synthesizer() call constructs it -- loads settings, builds the
        # registry, imports piper -- which is itself ~0.1-0.2 s and must not run
        # on the IO loop either. Evaluating _synthesizer() in the run_in_executor
        # arguments (the obvious way) would run that part on the loop.
        _synthesizer().preload(language=language)

    async def _warm_async():
        try:
            await asyncio.get_running_loop().run_in_executor(None, _warm)
        except Exception:
            # exc_info keeps the traceback: a preload failure is how you learn
            # the model or Piper is missing, so do not throw it away.
            logger.warning("TTS preload skipped", exc_info=True)

    # Keep a reference: asyncio holds only a weak reference to a bare task, so a
    # fire-and-forget `ensure_future(...)` can be garbage-collected mid-flight.
    self._tts_warm_task = asyncio.ensure_future(_warm_async())
```

(`_synthesizer()` is the module-level cached `AlertSynthesizer` from §2, not a
bare `synthesizer` name. `preload(voice=None, language=...)` returns the voice
id it loaded.)

Alert text longer than `max_alert_chars` (default 1,000 characters,
`SYSCON_TTS_MAX_ALERT_CHARS`) raises `AlertTextTooLongError`, a
`SynthesisError`: one runaway message would otherwise hold its voice's lock
and block every other announcement in that voice.

The package logs under the `syscon_tts` logger (model loads, synthesis timings
and evictions at INFO; cache hits/misses at DEBUG; language fallbacks at
WARNING). The package attaches no handler of its own; give the `syscon_tts`
logger one in the APU's logging configuration to see them.

### Choosing the voice

> **Upgrading a Korean or Japanese VoiceText site? Stop.** Piper has **no**
> Korean or Japanese voice in this catalogue, so those sites would silently
> announce in English after the upgrade. There is no in-place migration for
> them. The old VoiceText→Piper voice mapping, the "TTS Voice Overrides"
> setting and migration 0021 are APU-side concerns — see
> **`SECURITY_HANDOFF_APU_TTS.md` → "C-13: VoiceText→Piper migration"** for the
> mapping table and the pre-upgrade check that must gate those locales.

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
SYSCON_TTS_DEFAULT_VOICES=es-es=es_es_carlfm
```

(Only needed to override the automatic choice — here, the smaller Castilian
voice instead of `es_es_davefx`. An exact-locale voice always wins otherwise.)

#### The lessac-lineage voices are cleared (2026-10-16)

Five catalogued voices are fine-tuned from `en_US-lessac`, whose Blizzard 2013
data is described as research-only. Their *dataset* licenses are fine (public
domain / CC0 / CC BY 4.0, or Unknown for Huayan); the model lineage was the
open question, and those voices were held for license review until it was
decided. The lineage was cleared for commercial use on 2026-10-16:

| Language | Voice | Status |
| --- | --- | --- |
| Spanish (Mexico) | `es_mx_ald` | cleared — the es-MX voice |
| Spanish (Spain) | `es_es_davefx` | cleared (`es_es_carlfm`, x_low, is also available) |
| French | `fr_fr_siwis` | cleared — credit the SIWIS corpus (CC BY 4.0) |
| German | `de_de_thorsten` | cleared |
| Mandarin Chinese | `zh_cn_huayan` | **still held** — dataset license is Unknown |

**Sites on the old Mexican Spanish interim** (`es-mx=es_es_carlfm`) should drop
that mapping and run `syscon-tts download-voices --language es-mx`; the native
`es_mx_ald` is then selected automatically. A French or German site that fell
back to English needs only the download.

**Mandarin is still unresolved.** Clearing the lineage did not touch Huayan's
Unknown dataset license, so a Chinese site still falls back to the default
English voice unless the operator explicitly accepts the risk
(`download-voices zh_cn_huayan --accept-license`). The reasoning and how to
reverse the clearance are in `voices.json` under `_review_lessac` and
`_review_es_mx`.

> **⚠️ The APU has its own voice catalogue — these holds do not reach it
> automatically.** `plantstar_apu` ships its own
> `text_to_speech_voice_catalogue.json`, which is authoritative for the APU and
> its Text To Speech Voices page, and the APU's explicit `voice=` ids override
> the `SYSCON_TTS_DEFAULT_VOICES` mapping above. So a hold set or released here
> does nothing on the APU until the APU catalogue mirrors it. The APU must
> release the four cleared lessac voices, keep `zh_cn_huayan` held, and skip
> held voices in its download/overrides view — see **`SECURITY_HANDOFF_APU_TTS.md` → "C-63"**. The durable fix is for
> the APU to consume this catalogue (`SYSCON_TTS_MANIFEST`) or generate its own
> from it, so there is one source of truth.

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

**Do not flip `IS_LIVE` itself.** The same flag gates the APU's `chown`/`chmod` helpers
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
* **File names keep their own characters.** The sanitizing rule (Django's
  `get_valid_filename`) is Unicode-aware, so a Chinese message produces a
  Chinese file name. The URL the APU hands the browser (`hostname +
  "/media/public_alert_sounds/" + name + ".wav"`) therefore needs
  percent-encoding.
* **The readable prefix counts characters, not bytes.** Forty-one Han
  characters cover far more of a message than forty-one Latin ones. That no
  longer causes collisions: the hash suffix `file_name_for()` appends covers
  the full text and the voice, so distinct messages always get distinct files.

---

## 5. Run as a systemd service (optional)

Only needed if you want the HTTP server, for local tools or smoke tests. The
APU itself does not — it imports the library in-process (§2), so a normal APU
install has no `syscon-tts` service at all.

[`systemd/syscon-tts.service`](systemd/syscon-tts.service) is a **template**.
Every line marked `EDIT ME` is a placeholder: point `ExecStart` at the
`syscon-tts` entry point of the Python environment the package is installed
into on that host (the `/opt/syscon-tts/.venv` path in the file is only an
example), and set `User`/`Group` to an unprivileged account that can read the
voices directory. Then:

```bash
sudo cp systemd/syscon-tts.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now syscon-tts
systemctl status syscon-tts
```

The unit grants no write access anywhere (`ProtectSystem=strict`, no
`ReadWritePaths`) and sets no alerts directory: `/synthesize` returns audio in
the response body and never writes to the alerts directory. It binds
`127.0.0.1` by default. Change `SYSCON_TTS_HOST` only if something off-box
genuinely needs to reach it.

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

`download-voices` on the networked machine verifies each file against the
manifest's SHA-256, and `doctor` on the APU re-hashes the copied models
against the same pins, so a file damaged in transit is caught there rather
than at the first alert.

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

Then either drop `model_sha256` / `config_sha256` from the copied entry or,
better, replace them with the new files' hashes (`sha256sum` after a first
download). They are optional in an overlay but recommended: with them,
`download-voices` and `doctor` verify the files; with the old values copied
across, both would refuse them.

The shipped catalogue carries only `medium` models, so a site that wants a
lower tier adds it this way. Not every voice publishes a lower tier — check
the voice's folder at <https://huggingface.co/rhasspy/piper-voices>.

Rule of thumb on APU-class CPUs: `medium` runs a few times faster than
real-time; `low` is faster still with a modest quality drop. Measure on the
actual hardware before committing.

### CPU threads

By default onnxruntime gives each synthesis one thread per physical core, so
an announcement saturates the whole CPU for its length — everything else on
the APU stalls with it. Cap it:

* in the APU, with the constructor keyword: `AlertSynthesizer(..., threads=2)`;
* everywhere else, with `SYSCON_TTS_THREADS=2`.

Fewer threads means a slower render but a responsive host. Measure the
trade-off on the actual hardware with `syscon-tts benchmark --threads N`
(below), comparing a few values of N against the default.

### Memory

Each resident `medium` model costs roughly **139 MB** of RSS once it has
rendered once — the model plus the onnxruntime inference arena, which is
allocated on the first synthesis, not at load. These are the `syscon-tts
benchmark` figures after a warm render; measure on your own hardware.

**With worker isolation on (the APU default), the models live in the worker
process, not in the Tornado manager.** So budget per manager as two parts:

- the **manager** itself: the Python runtime plus this package and its registry,
  but *no* onnx models and no inference arena — onnxruntime is imported only in
  the worker. Call it ~40–80 MB on top of whatever Django already costs.
- the **worker**: a clean `python -I` interpreter (~40 MB base — it does **not**
  re-import the embedder's Django app; that is the point of the subprocess
  worker) plus `SYSCON_TTS_MAX_LOADED_VOICES` × ~139 MB.

With one resident voice that is roughly **40–80 MB (manager) + ~180 MB
(worker) ≈ 0.22–0.26 GB per manager**, and **both** the HTTP and HTTPS Tornado
managers run their own pair — so **~0.5 GB** for the site at one voice each, and
about 0.65 GB more for every additional resident voice across the two workers.
An unbounded `MAX_LOADED_VOICES` on a multilingual site is what pushes an APU
into swap or the OOM killer, whose symptom is slow or dropped announcements.

**For the APU, set `SYSCON_TTS_MAX_LOADED_VOICES=1` (or 2 for a bilingual
site) and `SYSCON_TTS_THREADS=2`.** One or two resident voices covers almost
every site, and capping threads leaves CPU for machine monitoring during a
render. Earlier guidance here said 50–120 MB per voice and ignored both the
arena and the worker process; re-size any APU provisioned against the old
numbers.

### Benchmarking (post-deployment)

`syscon-tts benchmark` (installed with the package) measures real synthesis
performance on the host it runs on. It needs Piper, so run it **on the APU
after deployment**. For each installed voice it reports cold model-load time,
resident-memory growth, and the **real-time factor
(RTF = synth_time ÷ audio_seconds)** plus latency for short/medium/long
messages.

```bash
syscon-tts benchmark                            # all installed voices
syscon-tts benchmark en_us_kristin --runs 5
syscon-tts benchmark --threads 2                # with synthesis capped at 2 threads
syscon-tts benchmark --json > "bench-$(hostname).json"
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
| `SYSCON_TTS_THREADS` | unset (one per physical core) | CPU threads per synthesis (§7) |
| `SYSCON_TTS_MAX_ALERT_CHARS` | `1000` | Longest alert text `ensure()` accepts |
| `SYSCON_TTS_FILE_MODE` | `0644` | Mode for generated audio |
| `SYSCON_TTS_WORKER_USER` | — | Account the synthesis worker drops to when started as root (set this on the APU) |
| `SYSCON_TTS_WORKER_ISOLATION` | `1` | Run synthesis in a worker process; `0` disables it (diagnostic only) |
| `SYSCON_TTS_WORKER_TIMEOUT` | `30` | Seconds a synthesis request waits before the worker is killed as hung |
| `SYSCON_TTS_EXTRA_MANIFEST` | — | Site voices layered over the catalogue |
| `SYSCON_TTS_MANIFEST` | bundled | Replace the voice catalogue outright |

Inside Django, prefer constructor keywords over environment variables — the
Process Spawner would otherwise have to propagate them to the Tornado socket
managers:

```python
AlertSynthesizer(
    alerts_dir=Path(settings.MEDIA_ROOT, "public_alert_sounds"),
    default_voices={"es_MX": "es_mx_ald"},
    threads=2,
)
```

Keywords are normalized exactly like environment variables (`"es-mx"` keys
become `es_MX`, `file_mode="0644"` is octal, `"5002"` becomes an int), and bad
values raise `ValueError` at construction.
