# Meeting Minutes

Multilingual meeting recording (English / Hindi / Bengali) with speaker
identification and automatic minutes.

- **Record** in the room from a browser, or upload an existing recording
- **Transcribe** with a hosted provider, per-segment language and script detection
- **Identify** who spoke by matching against voice samples an admin enrolled up front
- **Summarise** into structured minutes — decisions, action items with owners, open questions
- **Retain** everything; browse and search meeting history

Stack: React + Vite (frontend), FastAPI + Celery (backend), Postgres + pgvector,
Redis. Recordings on local disk. No GPU required.

---

## Deploying to a server

See **[`DEPLOYMENT.md`](DEPLOYMENT.md)** — native install on port **8017**,
with `meeting_minutes.service`, `meeting_minutes_worker.service` and
`meeting_minutes.nginx.conf` alongside it. That is the recommended production
path; this app is an ordinary Python service (20 packages plus ffmpeg, no ML
runtime), so containerising it buys little on a host already running nginx and
Postgres.

Docker below stays the easiest way to run it locally.

---

## Quick start

```bash
cp .env.example .env
# Set at minimum: GEMINI_API_KEY, SECRET_KEY, ADMIN_PASSWORD
# (ANTHROPIC_API_KEY is only needed once you turn minutes on - see Pipeline phases)

docker compose up --build
```

| Service | URL |
|---|---|
| App | http://localhost:5173 |
| API docs | http://localhost:8000/docs |

Sign in with `ADMIN_EMAIL` / `ADMIN_PASSWORD` from your `.env`.

The first run downloads the ECAPA speaker model (~20MB) into a named volume; the
first enrollment or meeting is therefore slower than the rest.

---

## Run the pipeline spike first

**Do this before building anything on top.** It runs the exact pipeline the
production worker runs — no database, no auth, no queue — so you find out whether
transcript quality on *your* audio is acceptable before you have invested in
everything around it.

```bash
docker compose run --rm api python scripts/spike.py /samples/meeting.m4a

# with speaker enrollment
docker compose run --rm api python scripts/spike.py /samples/meeting.m4a \
    --enroll "Priya Sharma=/samples/priya.wav" \
    --enroll "Rahul Das=/samples/rahul.wav"

# with minutes
docker compose run --rm api python scripts/spike.py /samples/meeting.m4a --minutes
```

Drop audio files into `samples/` in this repo; it is mounted into the api and
worker containers at `/samples`.

### Getting test audio

**`samples/RECORDING_SCRIPT.md` is the real benchmark.** Read it aloud with two
colleagues on the microphone and in the room you will actually use — five
minutes of recording. It is deliberately built to break things: English
technical terms inside Hindi and Bengali sentences, mid-sentence switches,
numbers, product names, and one genuine overlap. It ships with a scoring
checklist for what to look for in the output.

For a **smoke test only** — proving the pipeline runs before you gather people —
there is a synthetic generator:

```bash
docker compose run --rm api python /samples/generate_test_audio.py
docker compose run --rm api python scripts/spike.py /samples/synthetic_mixed.wav
```

That file is robotic espeak-ng speech with no room acoustics, no accents and no
natural code-switching prosody. It will tell you whether the code works. It will
tell you nothing about whether a provider is accurate — do not choose a provider
from it.

### Benchmark the providers — do not take anyone's word on Bengali

English and Hindi are comfortable across every provider. Bengali is the variable
that should pick your default, and the only honest way to choose is to measure:

```bash
for p in elevenlabs sarvam gemini; do
  docker compose run --rm api python scripts/spike.py \
      /samples/bengali_meeting.wav --provider $p --json /tmp/out-$p.json
done
```

Then diff the transcripts against a human reference. Swapping providers for good
is a one-line change to `ASR_PROVIDER`.

### The three providers

| | Gemini *(default)* | ElevenLabs Scribe | Sarvam |
|---|---|---|---|
| Type | Multimodal LLM | Dedicated ASR | Dedicated ASR (Indic) |
| Cost / hour | **~$0.04** | $0.22 | $0.53 with diarization |
| Code-switching | **Best** — instructable | Decodes one language per window | Indic-first, code-mix aware |
| Diarization | Prompted, same call | Built in | Batch API only (not wired) |
| Timestamps | Model-generated, approximate | Word-level, force-aligned | — |

**Why Gemini is the default.** Real meetings here code-switch mid-sentence, and
that is the one thing no vendor publishes accuracy numbers for. A CTC or
transducer ASR commits to one language per audio window and has no instruction
channel; a language model can be told *"speakers switch mid-sentence, keep
code-switched words in their own script"* — and the prompt in
`app/asr/gemini.py` says exactly that. It is also roughly 5x cheaper.

**The two tradeoffs you are accepting.**

*Timestamps are approximate.* They come from a language model rather than a
forced-alignment step. Speaker identification cuts audio at those boundaries to
build voiceprints, so this matters at phase 2, not phase 1. If enrollment
matching turns out weak, ElevenLabs' word-level timestamps are the fallback —
and you can run identification on one provider while transcribing with another
if it comes to that.

*Truncation risk.* The whole transcript is model *output*, so it must fit the
output token budget, and Bengali runs ~3x more tokens per word than English.
`app/asr/gemini.py` compares the last segment's timestamp against the real audio
duration and fails loudly below 75% coverage, rather than silently storing half
a meeting. If you hit it on long meetings, split the recording or switch
provider for those.

### Re-checking the choice

The spike prints a code-switching report — % of segments mixing scripts, script
distribution, and sample mixed segments. A **falling** mixed-share means the
provider is transliterating English into Devanagari rather than keeping it in
Latin, which is the failure you cannot see by skimming.

```bash
for p in gemini elevenlabs sarvam; do
  docker compose run --rm api python scripts/spike.py       /samples/mixed_meeting.wav --provider $p --json /tmp/out-$p.json
done
```

---

## Architecture

```
React (Vite + TS)
     │  REST + SSE (job progress)
FastAPI ──── Postgres + pgvector ──── disk (or S3)
     │
   Redis ──── Celery worker
                 ├── ASR provider (hosted)   → transcript + diarization
                 ├── ECAPA embeddings (CPU)  → speaker identification
                 └── Claude Opus 5           → structured minutes
```

### The pipeline

`backend/app/pipeline.py`, five stages:

1. **Normalise** — ffmpeg to 16 kHz mono WAV
2. **Transcribe** — hosted ASR returns text, word timestamps, detected language, and
   anonymous diarization labels (`SPEAKER_00`, `SPEAKER_01`, …)
3. **Identify** — pool ~30s per speaker cluster, embed with ECAPA-TDNN, cosine-match
   against enrolled voiceprints
4. **Persist** — segments and participants
5. **Minutes** — Claude with structured outputs

### Why no GPU

The expensive model (ASR) is rented. The only model running locally is the
speaker encoder — ~20MB, and it sees ~30 seconds per speaker rather than the
whole meeting. A 1-hour meeting's identification step is a few seconds of CPU.

### Why identification is per-cluster, not per-segment

The diarizer has already grouped a person's turns together. Making one
identification decision from ~30s of pooled audio is far more robust than
embedding each short segment and voting — a 1.2-second *"haan, theek hai"* carries
almost no speaker information, and per-segment matching lets a handful of those
flip a speaker mid-meeting.

Assignment is greedy and one-to-one: the highest-scoring (cluster, user) pair
wins, then both leave the pool. Without that constraint, two similar-sounding
colleagues can both be labelled the same person — the failure mode users notice
and lose trust over.

### The seam that matters

`backend/app/asr/base.py` defines one result shape; every provider normalises into
it. Adding Deepgram, Google Chirp or Azure means one new file and one line in
`app/asr/__init__.py` — no changes anywhere else.

---

## Voice enrollment

Admins add users and upload voice samples under **Users & Voices**.

- **Three samples of ~20s each beats one 60s sample** — more natural variation captured
- Clean speech, no background chatter
- Minimum 5s per sample; ~60s total per person is the target the UI shows progress against

**The cheapest enrollment path is correction.** When someone renames "Unknown
Speaker 2" to a real person in a finished meeting, that speaker's audio is
harvested into a new voiceprint automatically. The system gets better with use,
and corrections are sticky — a reprocess will not undo them.

### Tuning the match threshold

`SPEAKER_MATCH_THRESHOLD` (default `0.35`) is cosine similarity between L2-normalised
ECAPA embeddings. **This default is a starting point, not a tuned value** — it
depends on your microphones, your room, and how similar your people sound.

Run the spike with `--threshold` over a meeting where you know the ground truth:

- Too many wrong names → raise it
- Too many "Unknown Speaker N" → lower it

Prefer erring high. A confident wrong name is worse than an honest unknown.

---

## Pipeline phases

Two flags in `.env` control how much of the pipeline runs automatically. Both
default to **off**, so a fresh install produces transcripts only.

| Phase | Flags | What you get |
|---|---|---|
| **1. Transcript** | both `false` | Timestamped, per-segment language-tagged transcript with speakers separated as "Speaker 1/2/3" |
| **2. Names** | `AUTO_IDENTIFY_SPEAKERS=true` | Diarized clusters matched to enrolled users by voiceprint |
| **3. Minutes** | `AUTO_GENERATE_MINUTES=true` | Structured minutes on every meeting |

### Admin feature toggles

Separate from the `.env` flags above: these are product behaviours an admin
flips from **Users & Voices** in the app, and they take effect immediately
with no restart. Declared in `app/features.py`.

| Toggle | Default | Effect |
|---|---|---|
| Voice enrollment | **off** | Admin can upload voice samples so speakers get real names. Needs an image built with `WITH_SPEAKER_ID=true` |
| Let people correct speaker names | **off** | Shows the reassign control on each meeting, and enrolls the corrected speaker's voice |

### Speaker features, and what each costs

| | Status | Cost |
|---|---|---|
| **Diarization** — "Speaker 1 / 2 / 3" in the transcript | **on** | Free — included in the ASR response |
| **Identification** — matching a voice to a named person | off | torch + speechbrain: ~2GB image, ~1GB RAM |
| **Enrollment** — admin uploads voice samples | off | same dependency |
| **Relabelling** — correcting a speaker afterwards | off | none |

Diarization stays on because it is free and a transcript without speaker turns
is a wall of text. The rest is off, and the image is built without torch:

```bash
# default - no torch, ~2GB smaller
docker compose build api

# with speaker identification
docker compose build --build-arg WITH_SPEAKER_ID=true api worker
```

Calling enrollment on a slim image raises a clear error telling you to rebuild,
rather than an ImportError traceback.

The server enforces these, not just the UI — the relabel endpoint returns 403
when the toggle is off, so hiding the control is defence in depth rather than
the only guard.

Start at phase 1 deliberately. It isolates ASR quality — the one thing that can
sink the project — from everything built on top of it, and it costs only the ASR
call. Move up a phase once the one below it is good enough.

Minutes are **always** available on demand from the meeting page regardless of the
flag, so leaving phase 3 off costs you nothing but the automation.

---

## Storage and retention

Recordings are written to **plain files on disk** by default — `./data/recordings`
in this repo, mounted into the api and worker containers at `/data`. No S3, no
MinIO, nothing to provision.

### What gets deleted, and what does not

| | Retention |
|---|---|
| Audio recordings | **7 days**, then deleted automatically |
| Transcripts (segments) | Kept indefinitely |
| Speakers and participants | Kept indefinitely |
| Meeting minutes | Kept indefinitely |
| Enrollment voiceprints | Kept indefinitely |

A daily job at 03:30 removes audio older than `RECORDING_RETENTION_DAYS` and
stamps `meetings.audio_deleted_at`. The meeting page then shows when the
recording was removed instead of a broken player — the written record survives,
only the audio goes.

Set `RECORDING_RETENTION_DAYS=0` to disable deletion entirely. To run the sweep
by hand:

```bash
docker compose exec worker python -c   "from app.worker.tasks import purge_old_recordings; print(purge_old_recordings())"
```

### Playback

`<audio>` cannot send an Authorization header, so playback links carry a
**signed token in the query string**, scoped to one meeting and valid for 15
minutes. A leaked link exposes one recording briefly rather than a whole session.

### Switching to S3 later

Set `STORAGE_BACKEND=s3` and fill in the `S3_*` block. Keys have the same shape
in both backends (`meetings/<uuid>/source.m4a`), so migrating means copying the
directory into a bucket — no code or schema changes.

---

## Running costs

Per **1 hour** of meeting audio:

| Item | Cost |
|---|---|
| ASR — Gemini Flash *(default)* | ~$0.04 |
| ASR — ElevenLabs Scribe (alternative) | $0.22 |
| Minutes — Claude Opus 5 | ~$0.20–0.30 |
| Minutes — Claude Sonnet 5 (alternative) | ~$0.08–0.12 |
| Speaker identification | Free — local CPU |

At phase 1 you pay only the ASR line. At 1 meeting-hour per working day that is
under **$1/month** on Gemini; with Opus 5 minutes on every meeting, roughly
**$6/month**.

Token counts depend heavily on language mix — Devanagari and Bengali script run
roughly 2–3× more tokens per word than English, and per-segment speaker/timestamp
labels add real overhead. Use `client.messages.count_tokens` on a real transcript
for an exact figure rather than estimating.

A 2-hour transcript still fits comfortably in one call against the 1M-token
context window. No chunking, no map-reduce.

---

## Audio quality dominates everything

More than any model choice:

- **Use a central omnidirectional USB conference mic.** A laptop mic in a meeting
  hall gives far-field, reverberant, overlapping speech, and diarization degrades
  hard. This is the highest-ROI purchase in the project.
- **Overlapping speech stays imperfect.** Two people talking at once is an open
  research problem. The UI shows match confidence rather than presenting uncertain
  attributions as fact — keep it that way.
- **Leave the language hint on auto-detect** for mixed-language meetings. Forcing
  `language=hi` on an English sentence produces garbage transliteration, and real
  meetings code-switch mid-sentence.

---

## Searching history

Meeting history search is currently keyword-based (titles and transcript text).

The `minutes.embedding` column and pgvector are already in place for semantic
search ("what did we decide about the vendor contract?"). To finish it: embed each
summary with a multilingual model — `paraphrase-multilingual-MiniLM-L12-v2` (384-dim,
covers Hindi and Bengali) fits the existing column and reuses the torch install
that is already in the image — and add a `<=>` ordered query to `list_meetings`.

---

## Security notes

Things deliberately left simple in the scaffold that need attention before this
handles real meetings:

- **The SSE progress endpoint is unauthenticated.** `EventSource` cannot set an
  `Authorization` header. It leaks only stage/percent for a UUID the caller must
  already know, but move it behind a short-lived signed query token.
- **All authenticated users can read all meetings.** There is no per-meeting ACL.
  Meeting minutes are often confidential — add one before rollout.
- **CORS** is open to `localhost:5173`. Tighten to your real origin.
- **Audio leaves your infrastructure** when it goes to a hosted ASR provider.
  Confirm that matches your compliance posture; if not, the provider seam is where
  you swap in a self-hosted model.

---

## Before production

- **Alembic migrations.** The app currently calls `Base.metadata.create_all()` at
  startup, which is fine for a scaffold and wrong for a schema that will change.
- **Verify the provider response shapes.** `app/asr/elevenlabs.py` and
  `app/asr/sarvam.py` parse defensively, but confirm the current field names
  against each provider's docs.
- **Sarvam diarization.** The sync endpoint implemented here is transcription-only;
  speaker diarization lives on their batch/job API. Wire that up if you adopt
  Sarvam as primary — see the note in `app/asr/sarvam.py`.
- **Disk headroom.** Recordings live on the host filesystem; at 1 hour/day with a
  7-day window that is a few hundred MB steady-state, but monitor it.

---

## What is not built yet

- **Zoom / Teams / Meet ingestion.** The `AudioSource` enum and pipeline already
  accommodate it; what is missing is the webhook receiver and recording fetch.
  Start with **Zoom** — it can be configured to record a separate audio file per
  participant, which makes speaker identification exact and voice enrollment
  redundant for those meetings. Teams (Graph API `callRecording`) and Meet (Meet API
  artifacts) give mixed audio and fall back to the diarize-then-identify path. If you
  want all three quickly, Recall.ai wraps them behind one API.
- **Semantic search** — see above.
- **Export** to PDF/DOCX, and emailing minutes to participants.

---

## Layout

```
backend/
  app/
    asr/           provider interface + Gemini (default), ElevenLabs, Sarvam
    speakers/      ECAPA embeddings, cluster→user matching
    minutes/       Claude structured-output generation
    worker/        Celery app and tasks
    api/           auth, users, meetings, minutes routers
    pipeline.py    the five-stage pipeline
    models.py      schema
  scripts/
    spike.py       standalone pipeline spike — start here
frontend/
  src/pages/       Login, Meetings, MeetingDetail, Admin
  src/lib/api.ts   typed API client
db/init.sql        pgvector extension
```
