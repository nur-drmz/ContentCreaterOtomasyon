# YoutubeOtomationv0

Experimental provider-comparison notebooks for a YouTube Shorts content pipeline.
The main production path is now **source video / trend ingest → analysis →
script → voiceover → render → private YouTube upload → visibility approval**.
Trend/channel discovery is an independent research sidecar; it never selects or
downloads a source without an explicit human licence decision. YouTube publishing
is gated by three human approvals (metadata, private upload, visibility) and
always starts as `private`; nothing is made public without a separate explicit
approval.

The original product/release direction is tracked in [`ROADMAP.md`](ROADMAP.md).
Detailed status, acceptance criteria, open risks, evidence, and session-by-session
progress are tracked in [`MILESTONES.md`](MILESTONES.md). Voice-over remains a
required main pipeline stage, not an optional research feature.

## Creator Console (pre-alpha web UI)

```bash
python -m creator_console          # http://127.0.0.1:8765/
python -m creator_console --demo-fixtures
python verify_console.py           # console test suite
python verify_faults.py            # fault-injection + multi-process worker
python verify_browser.py           # real Chromium UI tests (Playwright)
```

A private, single-user, unauthenticated local console over the same canonical
`local_pipeline` modules — no extra dependency, no Node, no build step. See
[`CONSOLE.md`](CONSOLE.md) for screens, architecture, and the multi-user
boundaries that are designed for but deliberately not faked.

## Colab Secrets — exact names

| Secret name (Colab 🔑 panel) | Backward-compatible alias | Used for |
| --- | --- | --- |
| `GeminiKey` | `GOOGLE_API_KEY` | Gemini Developer API — every Google capability |
| `YoutubeKEY` | `YOUTUBE_DATA_API_KEY` | YouTube Data API v3 (read-only public metadata) |

The master notebook also supports manually selected `secondary` profiles using
`GeminiKeySecondary` and `YoutubeKEYSecondary`. Set
`GOOGLE_CREDENTIAL_PROFILE` / `YOUTUBE_CREDENTIAL_PROFILE` in the configuration
cell; the runtime resolves exactly that profile and never rotates to another
one after a 429. Profile labels are written to manifests, while secret values
remain private. The capability cache and quota ledger use profile-scoped file
names so legitimate project/channel environments cannot contaminate each
other's local estimates. Profiles must not be used to bypass provider quotas.

The alias on the left is tried first, then the one on the right. **These are two
different keys** — a Gemini key will not work for the YouTube Data API and vice
versa. Optional (all skipped cleanly when absent): `TMDB_API_KEY`,
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `ELEVENLABS_API_KEY`, `GROQ_API_KEY`,
`CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_AUTH_TOKEN`, `AZURE_SPEECH_KEY`,
`AZURE_SPEECH_REGION`.

Secret **values** are never printed, logged, or written to an artifact —
notebooks only ever report `configured (GeminiKey)` / `missing`. If Colab raises
while reading a secret (usually because *Notebook access* is off for that
secret), the notebook prints an actionable instruction instead of a stack trace.

### One key, many models — and quota is per PROJECT

The single `GeminiKey` reaches every model your project is entitled to
(`gemini-3.6-flash`, `gemini-3.5-flash`, the Flash-Lite models, Gemma, the TTS
preview models, `gemini-embedding-2`, …). You do **not** need a key per model.

Rate limits are **applied per project, not per API key**
([docs](https://ai.google.dev/gemini-api/docs/rate-limits)), so issuing a second
key in the same project does not raise any limit. Creating extra
accounts/projects to dodge RPD/RPM/TPM *would* be quota circumvention, so this
repo forbids it: `ALLOW_ACCOUNT_QUOTA_ROTATION = False`, enforced by
`google_ai.assert_no_account_quota_rotation()` and by a regression test that
fails if key/account rotation is ever introduced
(`[ga:no-account-rotation]`). The router changes **models within one project**,
and may later change **provider** — never accounts.

## Setup

- **Colab:** open any notebook in `experiments/colab/`; each one installs its
  own dependencies in its first cell and reads API keys from Colab Secrets
  (🔑 panel). No repo clone is required for notebooks 01–05 (each is
  self-contained); note that notebooks 02, 03, and 06 read files that earlier
  notebooks wrote to `results/` in the *same* Colab session/runtime.
- **Local Windows:** run `setup_local.ps1` once, then `python verify_local.py`
  to smoke-test the local pipeline. Notebooks auto-detect the local `.venv`
  and activate it. See `.env.example` for the environment variables the
  notebooks read. Creator Console automatically loads the Git-ignored
  `.env.local` file; standalone notebooks still use Colab Secrets or variables
  already present in the local process environment.

## Execution order

1. **`01_video_analysis_hotmoment.ipynb`** — transcribes the source video
   (faster-whisper), detects scenes (PySceneDetect), analyzes audio energy
   (librosa), and produces local hot-moment *candidates*
   (`results/hot_moments.json`, always `review_required`).
2. **Moment Explorer review** — the master notebook displays every candidate
   preview and writes a human-reviewed, preview/hash-bound
   `results/moment_selection.json`. A plain boolean cannot bypass this gate.
3. **`02_script_generation.ipynb`** — standalone comparison notebook. In the
   master pipeline, script generation accepts only a valid current
   `moment_selection.json`; the generated script is then reviewed and approved
   through its displayed SHA-256.
4. **`03_tts_voice.ipynb`** — loads `selected_script.json`; production TTS
   (Gemini / ElevenLabs / OpenAI / Piper) only runs if the script was approved; gTTS is always
   `development_placeholder` and can never become `approved`.
5. **`06_render_pipeline.ipynb`** — combines the approved moment, script, and
   voiceover into a vertical Shorts render (`results/render/final_short.mp4`);
   stays `review_required` until a human sets `RENDER_APPROVED = True`.
   Uploads nothing anywhere.
6. **`04_trend_opportunity_scoring.ipynb`** — TMDB + Google Trends heuristic,
   optional LLM scoring, and quota-efficient YouTube Data API v3 enrichment
   (`results/youtube_data/normalized_videos.csv`). Independent of steps 1–5;
   run whenever you want fresh trend/history data.
7. **`05_viral_prediction.ipynb`** — reads `normalized_videos.csv` from step
   6. Below `MIN_HISTORY_ROWS` (default 20) it reports `insufficient_data`
   and does not produce a confident prediction.

## The Google model router (`local_pipeline/google_ai.py`)

Every Google call in this repo — from the master notebook and from notebooks
01/02/04/05 alike — goes through **one** canonical module. No notebook cell
constructs a `genai.Client(...)` or hardcodes `model='gemini-…'` any more; a
regression test (`[ga:no-hardcoded-gemini-in-notebooks]`) fails if one comes
back.

### Task routing table

| Task | Route (tried in order, stops at the first success) | thinking_level |
| --- | --- | --- |
| `hotmoment_review`, `script_generation`, `consistency_review`, `quality_audit` | `gemini-3.6-flash` → `gemini-3.5-flash` → `gemini-3.5-flash-lite` → `gemma-4-31b-it` | medium (3.6/3.5), low (lite), none (Gemma) |
| `bulk_classification`, `viral_interpretation`, `metadata_generation`, `transcript_cleanup` | `gemini-3.5-flash-lite` → `gemini-3.1-flash-lite` → `gemma-4-26b-a4b-it` | minimal |
| `grounded_research` | `gemini-2.5-flash` + Google Search grounding | low |
| `tts` | `gemini-3.1-flash-tts-preview` → `gemini-2.5-flash-preview-tts` | — |
| `embeddings` | `gemini-embedding-2` (via `embed_content`, 768 dims) | — |
| `antigravity_research` | `antigravity-preview-05-2026` (experimental, **off** by default) | — |

A successful first call **ends** the route. The production path never iterates a
comparison catalog; deliberate multi-model benchmarking exists only in the
standalone notebooks 02/03 and is explicit.

**Never routed automatically:** any model configured with zero free quota for
this project — `gemini-2.5-pro`, `gemini-3.1-pro-preview`, and the image models.
They stay in `MODEL_REGISTRY` only so the exclusion is auditable.

**Capability discovery** intersects `client.models.list()` with an explicit
allowlist and then drops zero-quota models. `models.list()` alone does not prove
a model has usable free quota. The sanitized result is cached in
`results/google_capability_manifest.json` for a configurable TTL, so a rerun does
not re-probe the catalog. A one-time smoke test is available via
`RUN_GOOGLE_CONNECTION_TEST = True`; it pings only the **primary** model of each
selected task, never the whole catalog, and never prints the key.

### Fallback order and error policy

| Condition | Behaviour |
| --- | --- |
| RPM 429 | honor `Retry-After`, else exponential backoff with jitter (bounded); then next model |
| RPD / daily 429 | **no long sleep** — model marked unavailable until midnight Pacific, then next model |
| 401 / 403 | credential disabled for the whole run; actionable, sanitized message; stops |
| 404 / model unavailable | that model only is disabled; continue down the allowlist |
| 5xx / timeout | bounded retry, then fallback; circuit breaker opens after 3 consecutive transients (15 min cooldown → half-open probe → close on success) |
| invalid structured output | exactly **one** corrective retry on the same model, then fallback |
| safety refusal | **stops** — never rotates model or provider to get around safety |
| everything exhausted | `insufficient_data` / `failed`, resumable state preserved, output **never** fabricated |

Structured output is validated twice: schema/syntax first, then **semantics** —
`{"script": ""}` is valid JSON and is still rejected.

### Quota ledger (`results/google_quota_usage.json`)

A **local estimate**, explicitly labelled `estimated: true` — not a claim about
authoritative server quota, which only live 429s and AI Studio can tell you. Per
model it tracks requests/tokens in the current minute, requests in the current
project day, successes, failures, last call, last 429, cooldown, configured
RPM/TPM/RPD, estimated remaining RPD, and server-reported `usage_metadata` when
present. It is persisted (atomic write + lock file) so re-running a notebook cell
does not forget usage, and daily counters roll over at the documented
**midnight-Pacific** reset. When the local estimate already proves a limit is
spent, the call is not attempted at all. A manual reset is possible but
**requires an explicit reason**, which is recorded — and it corrects only the
local estimate; it can never reset server quota, and swapping credentials is not
a supported way to reset anything.

### Free-tier data policy

Content submitted on the **unpaid** tier may be used by Google to improve their
products and may be read by human reviewers
([terms](https://ai.google.dev/gemini-api/terms)). Therefore
`ALLOW_RAW_MEDIA_TO_FREE_TIER = False`: the raw (copyrighted) source video/audio
is **not** uploaded. Local FFmpeg / faster-whisper / PySceneDetect / librosa stay
the first analysis stage, and only derived transcript windows, timestamps,
scene/audio scores and candidate metadata are sent. Raw media upload requires an
explicit opt-in plus confirmation that you hold the necessary rights. Every
artifact records `raw_media_submitted` and the data-use notice.

### Cross-provider fallback (optional, credential-guarded)

The master pipeline consumes this switch for script generation,
Gemini-TTS-to-Azure fallback, and embeddings. Text fallbacks must validate the
same script JSON contract before they become an artifact; a safety refusal is
terminal and never causes a provider switch. Transcription adapters are exposed
for a future explicit opt-in, but raw audio is not automatically sent to a
cloud provider.

| Capability | Order |
| --- | --- |
| High-quality text | Google Route A → Groq `openai/gpt-oss-120b` → Cloudflare `@cf/openai/gpt-oss-120b` |
| High-volume text | Google Route B → Groq `qwen/qwen3.6-27b` → Cloudflare multilingual Qwen |
| Embeddings | `gemini-embedding-2` → Cloudflare `@cf/baai/bge-m3` → local multilingual sentence-transformers |
| Transcription | **local faster-whisper (primary, unchanged)** → Groq `whisper-large-v3-turbo` → Cloudflare `@cf/openai/whisper-large-v3-turbo` |
| TTS | Gemini TTS route → Azure Neural `tr-TR-AhmetNeural` / `tr-TR-EmelNeural` |

Missing credentials are **not** an error: the adapter reports `skipped` and the
router moves on. No adapter ever invents a successful result. Cloudflare model
IDs drift often — re-verify against Cloudflare's Workers AI model list before
relying on them.

### Gemini TTS

Turkish by default (`tr-TR`), configurable voice and narration direction, with
its own audio implementation (not the text parser). Gemini returns headerless
24 kHz/16-bit/mono PCM, which is wrapped into a valid WAV and then validated
(container, codec, sample rate, size, duration, non-emptiness). Scripts are split
only when they exceed the chunk limit. Both TTS models are **preview**, so every
Gemini voiceover is `review_required` — and because approval is bound to the
audio file's SHA-256, regenerating or falling back to another voice/model
automatically invalidates the previous approval instead of inheriting it.

## Quality policy

Every artifact JSON/CSV carries a `status` from a fixed vocabulary:
`experimental`, `development_placeholder`, `review_required`, `approved`,
`insufficient_data`, `failed`, `production_ready`. Local heuristics and free
providers (gTTS) can generate candidates but never self-promote to
`production_ready` — that requires explicit human approval, recorded in the
same JSON file.

## Single-file Colab use: `00_youtube_automation_master.ipynb`

For normal Colab use you only need to open **`00_youtube_automation_master.ipynb`**.
It is a thin orchestrator over the same canonical `local_pipeline` modules, with
one toggle per phase:

```python
RUN_VIDEO_ANALYSIS = False
RUN_SCRIPT_GENERATION = False
RUN_TTS = False
RUN_RENDER = False
RUN_YOUTUBE_DATA = False       # independent research sidecar
RUN_VIRAL_PREDICTION = False
```

### Google router configuration cell

```python
GOOGLE_CREDENTIAL_PROFILE = 'primary'   # manual selection; never auto-rotated
YOUTUBE_CREDENTIAL_PROFILE = 'primary'  # manual selection; never auto-rotated
GOOGLE_ROUTE_MODE = 'auto'              # 'auto' | 'explicit'
GOOGLE_TASK_MODEL_OVERRIDES = {}
ENABLE_GOOGLE_MODEL_FALLBACK = True
ENABLE_CROSS_PROVIDER_FALLBACK = True
MAX_GOOGLE_MODEL_ATTEMPTS = 4
MAX_PROVIDER_ATTEMPTS = 3
RUN_GOOGLE_CONNECTION_TEST = False
ENABLE_GOOGLE_SEARCH_GROUNDING = False
RUN_ANTIGRAVITY_RESEARCH = False
ALLOW_RAW_MEDIA_TO_FREE_TIER = False
ALLOW_ACCOUNT_QUOTA_ROTATION = False    # policy: never set this to True

# One independent switch per Google capability
RUN_GOOGLE_HOTMOMENT_REVIEW = False
RUN_GOOGLE_SCRIPT_GENERATION = True
RUN_GOOGLE_BULK_CLASSIFICATION = False
RUN_GOOGLE_GROUNDED_RESEARCH = False
RUN_GOOGLE_TTS = True          # required main-pipeline voice stage
RUN_GOOGLE_EMBEDDINGS = False
```

The master notebook also defaults to `TTS_PROVIDER = 'gemini'`. This uses the
configured `GeminiKey`; Piper, ElevenLabs, and OpenAI remain selectable provider
alternatives. Voice generation still waits for an approved script and its output
must be listened to and hash-approved before render approval can succeed.

`GOOGLE_ROUTE_MODE = 'explicit'` uses **only** `GOOGLE_TASK_MODEL_OVERRIDES`;
nothing is silently substituted, so an explicit choice stays honest (and a
zero-quota model is still refused).

Before anything executes, the notebook prints the selected route — model IDs and
policy state only, never a secret:

```
Task: script_generation
Route: gemini-3.6-flash -> gemini-3.5-flash -> gemini-3.5-flash-lite -> gemma-4-31b-it
Credential: configured (GeminiKey)
Account rotation: disabled
Raw media upload: disabled
```

### Order to run the master notebook

1. Phase toggles cell → 2. Google router config cell → 3. Setup/Secrets cell →
4. `local_pipeline` bootstrap cell → 5. Paths cell → 6. **Google capability
discovery + route display** cell → 7. Main pipeline phases 1–4 in order, running
each phase's *review* and *approval* cell before moving on → 8. Optional research
sidecars (YouTube discovery / viral prediction) → 9. Google capability
commands section → 10. Execution summary.

All six toggles are functional — you do **not** need to upload or run notebooks
01–06 separately. For the main pipeline, edit `SOURCE_VIDEO_PATH`,
`USE_GOOGLE_DRIVE`, the Moment Explorer form, and the displayed SHA-256 review/
approval fields for script, voice-over, and render. Only when running the optional research
sidecar should you edit `YOUTUBE_DISCOVERY_TERMS` and `MAX_SEARCH_CALLS_PER_RUN`.

`RUN_YOUTUBE_DATA=True` and `AUTO_DISCOVER_CHANNELS=True` run the independent
research sidecar without a manual channel list. It searches each term with the configured
date/view-count orders, fetches public video details, ranks candidates using
view velocity, recency, engagement, and repeat appearances, then feeds the top
channels into the existing upload-history collector. `YOUTUBE_CHANNEL_INPUTS`
remains an optional manual supplement. Search is capped by
`MAX_SEARCH_CALLS_PER_RUN`, cached, and reported separately in
`quota_usage.json`. Discovery outputs are `discovered_videos.csv`,
`discovered_channels.csv`, and `discovery_manifest.json`; all remain
`review_required` or `insufficient_data`, never automatically
`production_ready`. These files are decision-support material only and are not
read by the main source-video-to-Shorts pipeline.

**Dependency-aware phases:** if a phase is enabled it runs; if its prerequisite
phase is disabled, the master notebook locates and schema-validates the existing
artifact on disk (Drive-persisted, `/content/results`, or local `results/`); if
the artifact is missing it stops **that phase only** with an actionable error
(`phase_status[...] = 'failed'`) and never prints a false success.

**How it works with no repo:** every notebook that needs a `local_pipeline`
module carries a zlib-compressed Base85 + SHA-256 snapshot of it. The internal
bootstrap cell is collapsed in Colab/Jupyter so this implementation detail does
not dominate the notebook UI. Modules load via: direct
import → Drive/ancestor copy → `PROJECT_REPO_URL` clone → embedded snapshot.
A hash mismatch raises rather than importing broken code.

## Regenerating embedded module payloads (required after editing `local_pipeline/`)

The embedded snapshots go stale the moment you edit anything under
`local_pipeline/`. `verify_local.py` fails loudly when that happens. To fix:

```bash
python tools/refresh_embedded_payloads.py          # rewrite stale payloads in place
python tools/refresh_embedded_payloads.py --check  # verify only; exit 1 if stale
python verify_local.py                             # full suite
```

## Repo layout

- `experiments/colab/` — the notebooks listed above, plus the
  `00_youtube_automation_master.ipynb` orchestrator.
- `local_pipeline/` — the **canonical** phase modules; both the standalone
  notebooks and the master notebook call these same functions, so there are no
  divergent copies:
  - `video_analysis.py` — clip/audio extraction, transcription, scene
    detection, audio peaks, hot-moment scoring, `hot_moments.json` schema.
  - `script_generation.py` — prompt construction, LLM response parsing,
    per-provider calls, `selected_script.json` schema.
  - `tts.py` — TTS model registry, per-provider calls,
    `selected_voiceover.json` schema (gTTS can never be `approved`).
  - `render_utils.py` — ffmpeg filtergraphs, ffprobe/ffmpeg-fallback probing,
    subtitle generation, and the full render + approval-gate pipeline.
  - `youtube_data.py` — read-only YouTube Data API v3 keyword discovery,
    trend/channel ranking, upload-history collection, TTL cache (SHA-256 keyed),
    quota estimates, and credential sanitization.
  - `viral_prediction.py` — statistical baselines and holdout evaluation.
  - `google_ai.py` — **the** Google router: secret aliases, client creation,
    model registry + configured free quotas, capability discovery, task routes,
    quota ledger, retry/backoff/circuit breaker, structured-output validation,
    sanitized errors, per-capability commands, artifact contract.
  - `provider_adapters.py` — optional Groq / Cloudflare / Azure adapters that
    skip cleanly when their credentials are missing.

## What the test suite actually proves

`python verify_local.py` mixes three kinds of check — read the labels:

- **Genuinely executed against real local tooling:** ffmpeg/ffprobe media
  probing, faster-whisper transcription, PySceneDetect, librosa, the full render
  pipeline smoke test, WAV parsing, file hashing, JSON/artifact IO.
- **Mocked (no network, no credential):** every Google, Groq, Cloudflare, Azure,
  YouTube, OpenAI, Anthropic and ElevenLabs call. Clients and HTTP transports are
  injected doubles; credentials are fake strings that are never real keys; all
  fixtures live in temp directories that are deleted afterwards. Nothing is
  uploaded or published.
- **Static / syntax-only:** notebook JSON + `ast.parse` validation, embedded
  payload SHA-256 synchronization, secret scanning of tracked files, and
  source-level checks that no notebook cell hardcodes a Gemini model or builds a
  `genai.Client` directly.

Because every provider call is mocked, a green run does **not** prove your real
credentials work. That requires running the master notebook in Colab with the
real secrets and reviewing the resulting artifact chain by hand.
- `tools/refresh_embedded_payloads.py` — keeps notebook-embedded module
  snapshots in sync with `local_pipeline/`.
- `requirements-local.txt`, `setup_local.ps1`, `verify_local.py` — local
  Windows/Jupyter environment setup and the full verification suite.
- `.env.example` — variable names only, no real values.
