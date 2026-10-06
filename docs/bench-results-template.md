# Benchmark Results — <HOST / APU NAME>

Record of a `syscon-tts benchmark` run on a target host. Copy this file to
`docs/bench-results-<host>.md`, fill it in after deployment, and commit it so
each APU's performance profile is tracked over time.

Generate the raw numbers with:

```bash
syscon-tts benchmark --json > bench-$(hostname).json   # archive
syscon-tts benchmark                                   # readable table
syscon-tts benchmark --threads 2                       # compare against a thread cap
```

## Environment

| Field | Value |
|-------|-------|
| Host / APU id | <e.g. apu-line3-01> |
| Date | <YYYY-MM-DD> |
| Install method | <pip / air-gapped wheelhouse> |
| CPU (model, cores) | <e.g. Intel Atom x6425E, 4 cores> |
| RAM | <e.g. 8 GB> |
| OS / Python | <e.g. Debian 12 / Python 3.11> |
| Service version | <e.g. 0.1.0> |
| Voice quality tier | <medium / low / mixed> |
| threads | <default (one per physical core) / e.g. 2> |
| benchmark --runs | <e.g. 5> |

## Results

Paste the table from `syscon-tts benchmark`, or fill in per voice:

| Voice | Cold load (s) | Mem (MB) | Short RTF | Med RTF | Long RTF | Long latency (s) |
|-------|---------------|----------|-----------|---------|----------|------------------|
| en_us_kristin |  |  |  |  |  |  |
| en_us_john |  |  |  |  |  |  |
| es_mx_ald |  |  |  |  |  |  |
| es_es_davefx |  |  |  |  |  |  |
| zh_cn_huayan |  |  |  |  |  |  |
| fr_fr_siwis |  |  |  |  |  |  |
| de_de_thorsten |  |  |  |  |  |  |

> **RTF** = synthesis_time ÷ audio_seconds. Lower is better; `< 1.0` = faster
> than real-time. See [DEPLOY.md §7](../DEPLOY.md#7-tuning-for-the-apu-cpu).

## Assessment

- **Acceptable for production?** <yes / no — based on the RTF and latency for the
  message lengths you actually send>
- **Chosen quality tier:** <medium / low, and which voices if mixed>
- **Chosen thread cap:** <default / N — and how the rest of the APU behaved during synthesis>
- **Voices to preload at startup:** <list, if first-hit cold-load latency matters>
- **Peak memory budget:** <sum of MEM for voices kept loaded × worker count>
- **Follow-up actions:** <e.g. "drop de_de_thorsten to low", "set SYSCON_TTS_THREADS=2">
