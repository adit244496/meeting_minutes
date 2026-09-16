# Deployment

Native install on the shared Ubuntu server, matching the pattern used by the
other apps. Port **8017**.

Reachable at **http://&lt;server-ip&gt;:8017** with no nginx involved — the API
serves the built frontend itself, the same way the hotel dashboard does. Add
nginx later when it gets a domain and TLS (step 8).

Two services, not one: the API, and a background worker that does the
transcription. **Both must be running** — without the worker, meetings upload
and then sit at "uploaded" forever.

```
browser ──► uvicorn :8017 ──┬─► frontend/dist  (served directly)
                            └─► /api
                                   │  queues job
                                   ▼
                             Redis ──► celery worker
                                          ├─ Gemini  (transcript)
                                          └─ Claude  (minutes)
```

---

## 1. System packages

```bash
sudo apt update
sudo apt install -y python3-venv ffmpeg redis-server
sudo systemctl enable --now redis-server
```

**`ffmpeg` is not optional** — every meeting is transcoded before transcription, so uploads fail without it. The API logs a warning at startup if it is missing. Python 3.10+ is required — SQLAlchemy resolves
`Mapped[str | None]` at runtime, so 3.9 fails at import.

## 2. Checkout and virtualenv

```bash
cd ~
git clone https://github.com/adit244496/meeting_minutes.git meeting_minutes
cd ~/meeting_minutes/meeting_minutes/backend
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt
```

The unit files assume exactly this layout —
`~srvadmin/meeting_minutes/meeting_minutes/backend`. Change the paths in both
`.service` files if you put it elsewhere.

## 3. Database

Uses the existing PostgreSQL with pgvector:

```bash
sudo -u postgres psql <<'SQL'
CREATE DATABASE meeting_minutes;
CREATE USER mm WITH PASSWORD 'change-this';
GRANT ALL PRIVILEGES ON DATABASE meeting_minutes TO mm;
SQL

sudo -u postgres psql -d meeting_minutes -c 'CREATE EXTENSION IF NOT EXISTS vector;'
sudo -u postgres psql -d meeting_minutes -c 'GRANT ALL ON SCHEMA public TO mm;'
```

Both of the last two lines matter:

- **`CREATE EXTENSION vector`** is per *database*. Having pgvector installed on
  the server is not enough — without this, startup fails with
  `type "vector" does not exist` partway through creating tables.
- **`GRANT ALL ON SCHEMA public`** is needed on PostgreSQL 15+, where `public`
  is no longer writable by default.

## 4. Configuration

The unit files read `backend/.env`, and the app reads the same file (its working
directory is `backend/`). Note this differs from Docker, which uses the `.env`
at the repository root.

```bash
cd ~/meeting_minutes/meeting_minutes/backend
cp ../.env.example .env
chmod 600 .env
nano .env
```

Set these:

```bash
SECRET_KEY=<long random string>
ADMIN_EMAIL=<your admin login>
ADMIN_PASSWORD=<something real>

GEMINI_API_KEY=<key>                  # transcription
ANTHROPIC_API_KEY=<key>               # minutes; required while AUTO_GENERATE_MINUTES=true

DATABASE_URL=postgresql+psycopg://mm:change-this@127.0.0.1:5432/meeting_minutes
REDIS_URL=redis://127.0.0.1:6379/0
LOCAL_STORAGE_DIR=/home/srvadmin/meeting_minutes/recordings
```

`.env.example` already defaults these to `127.0.0.1`, which is what a native
install needs; docker-compose overrides them with its own container hostnames.
If you ever see `Temporary failure in name resolution` for a host called `db` or
`redis`, a Docker-flavoured value has been copied into a native `.env`.

```bash
mkdir -p /home/srvadmin/meeting_minutes/recordings
```

**systemd parses `.env` itself — it is not a shell.** Do not quote values, do not
use `$VAR` expansion, and keep `#` out of the middle of a value. A database
password containing special characters must be URL-encoded inside
`DATABASE_URL`.

## 5. Frontend build

`VITE_API_URL` is left empty so the app calls its own origin — which works
whether it is reached on a bare IP or through nginx later.

```bash
cd ~/meeting_minutes/meeting_minutes/frontend
npm ci
VITE_API_URL= npm run build
```

Produces `frontend/dist`. The API picks it up automatically at startup and
serves it; if the directory is missing the API still runs, just without a UI.
The startup log says which happened.

## 6. Smoke test before starting anything

Catches import-time errors - a bad route decorator, a missing dependency -
without needing the database, Redis or an API key.

```bash
cd ~/meeting_minutes/meeting_minutes/backend
venv/bin/python scripts/smoke_import.py
```

Prints the route table and both "OK" lines. Worth running after every `git pull`:
uvicorn's master process survives a child that dies at import, so a broken build
still looks "active (running)" in systemctl while every request 502s.

## 7. Services

```bash
cd ~/meeting_minutes/meeting_minutes
sudo cp meeting_minutes.service meeting_minutes_worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meeting_minutes meeting_minutes_worker
systemctl status meeting_minutes meeting_minutes_worker
```

## 8. Open the port and verify

Add an inbound rule for TCP **8017** on the VM's Network Security Group.

```bash
curl -s localhost:8017/health         # {"status":"ok","asr_provider":"gemini",...}
journalctl -u meeting_minutes -f      # should log "Serving frontend from ..."
journalctl -u meeting_minutes_worker -f
```

Then open **http://&lt;server-ip&gt;:8017** and sign in with `ADMIN_EMAIL` /
`ADMIN_PASSWORD`. Upload a short recording and watch the worker log.

## 9. Domain and TLS — neominutes.ambujaneotia.com

The hostname lives in `meeting_minutes.nginx.conf` in this repo, so change it
here and deploy, never by editing the copy under `/etc/nginx` (the next `cp`
would overwrite it, and the repo would still claim the old name).

**Before any of this:** point an A record for `neominutes.ambujaneotia.com` at
the server's public IP, and open inbound **80** and **443** on the security
group. Check DNS has actually propagated - certbot fails if it has not:

```bash
dig +short neominutes.ambujaneotia.com      # must print the server's IP
```

```bash
cd ~/meeting_minutes/meeting_minutes && git pull
sudo cp meeting_minutes.nginx.conf /etc/nginx/sites-available/meeting_minutes
sudo ln -s /etc/nginx/sites-available/meeting_minutes /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
curl -s http://neominutes.ambujaneotia.com/health    # proxy works, still plain HTTP
sudo certbot --nginx -d neominutes.ambujaneotia.com
```

certbot rewrites that file in place to add the TLS server and the http->https
redirect; leave its edits alone.

### Certificate renewal

Certificates last 90 days and certbot renews them itself, from a systemd timer
it installs. Check it, and prove a renewal works:

```bash
systemctl list-timers | grep -i certbot     # should list a timer
sudo certbot renew --dry-run                # staging run; does not touch the real cert
sudo certbot certificates                   # what exists, and expiry dates
```

If no timer is listed, enable it (name depends on how certbot was installed):

```bash
sudo systemctl enable --now certbot.timer               # apt
sudo systemctl enable --now snap.certbot.renew.timer    # snap
```

Make nginx pick up a renewed certificate. The nginx plugin normally reloads it;
this records the hook in the renewal config so every future renewal does:

```bash
sudo certbot renew --deploy-hook "systemctl reload nginx"
```

**Leave port 80 open.** Renewal validates over HTTP, so closing 80 after moving
to HTTPS breaks renewal about 60 days later - the redirect to HTTPS is harmless.
The timer runs twice a day and does nothing until fewer than 30 days remain.

Nothing in the app changes: the frontend is built with an empty `VITE_API_URL`,
so it calls whatever origin it is served from, and the API serves the frontend
itself. No CORS entry is needed either, because browser and API share an origin.

Once HTTPS works, close 8017 on the security group so traffic only arrives over
443. **HTTPS also switches browser recording on** - microphones are blocked on
plain HTTP, so live transcription only works once this step is done.

---

## Recognising people by voice

Every meeting page plays a few seconds of each speaker, cut from the recording,
so somebody can listen and put a name to the voice. Naming a speaker also
*teaches* the system that voice, and later meetings match against what it has
learnt. Nothing is shared between installations - the voiceprints are rows in
your own database.

How it learns:

1. Someone names a speaker on a finished meeting (**Speakers**, pick a person).
2. That correction is saved as `is_manual`, so reprocessing never overwrites it,
   and the speaker's audio from that meeting is embedded into a **voiceprint**
   for that person - about 30 seconds of pooled speech, stored as 192 numbers,
   not audio.
3. In later meetings each diarized speaker is embedded the same way and compared
   against every enrolled voiceprint. The best match above
   `SPEAKER_MATCH_THRESHOLD` (0.35 by default) wins, one person per speaker.
   Below it, the speaker stays "Speaker 2" rather than guessing.
4. Each correction adds another voiceprint for that person, so recognition
   improves with use. An admin can also upload samples up front under
   **Settings > People**.

It is off until three things are true:

```bash
# 1. The packages: ~2GB, CPU-only torch. Native install only - the Docker image
#    takes --build-arg WITH_SPEAKER_ID=true instead.
cd ~/meeting_minutes/meeting_minutes/backend
venv/bin/pip install torch==2.5.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/cpu
venv/bin/pip install -r requirements-speaker.txt
sudo systemctl restart meeting_minutes meeting_minutes_worker
```

2. **Settings > Features > Recognise people by voice** - on.
3. **Settings > Features > Let people correct speaker names** - on, so there is
   a way to teach it in the first place. (**Voice enrollment** adds the
   upload-samples path for admins.)

The first meeting after enabling downloads a ~20MB model. Tune
`SPEAKER_MATCH_THRESHOLD` on your own audio: too many wrong names, raise it; too
many unknowns, lower it. A confident wrong name is worse than an honest unknown.

---

## Testing it

### A throwaway test recording

No Hindi or Bengali speakers to hand? Generate a synthetic one:

```bash
sudo apt install -y espeak-ng
cd ~/meeting_minutes/meeting_minutes
backend/venv/bin/python samples/generate_test_audio.py
```

Writes `samples/synthetic_mixed.wav` — three robotic voices covering the same
English / Hindi / Bengali script, with the English technical terms left in Latin
so you can see whether a provider transliterates them.

**This proves the pipeline runs. It is not a quality benchmark.** espeak-ng has
no room acoustics, no accents and no natural code-switching prosody, so a
provider can ace this and still struggle on your meetings. For the real
measurement read `samples/RECORDING_SCRIPT.md` aloud with two colleagues on the
microphone you will actually use.

### Run it through the pipeline

```bash
cd ~/meeting_minutes/meeting_minutes/backend
venv/bin/python scripts/spike.py ../samples/synthetic_mixed.wav
```

No database, queue or UI involved — the fastest way to see a provider error as a
clean traceback. Prints the transcript, the speaker split and a code-switching
report.

Then upload the same file through the web UI and watch
`journalctl -u meeting_minutes_worker -f` to exercise the full path.

---

## Operating it

**Deploy a change**

```bash
cd ~/meeting_minutes/meeting_minutes
git pull
cd backend && venv/bin/pip install -r requirements.txt          # if deps changed
cd ../frontend && npm ci && VITE_API_URL= npm run build          # if frontend changed
sudo systemctl restart meeting_minutes meeting_minutes_worker
```

**Logs**

```bash
journalctl -u meeting_minutes -f
journalctl -u meeting_minutes_worker -f    # transcription and minutes errors
```

**Retention** runs daily at 03:30 and deletes recordings older than
`RECORDING_RETENTION_DAYS` (7). Transcripts and minutes are kept indefinitely.
Run it by hand:

```bash
cd ~/meeting_minutes/meeting_minutes/backend
venv/bin/python -c "from app.worker.tasks import purge_old_recordings; print(purge_old_recordings())"
```

**Backups.** Recordings self-delete after a week; the transcripts and minutes in
PostgreSQL are what matters. Back up the database:

```bash
pg_dump -Fc meeting_minutes > meeting_minutes_$(date +%F).dump
```

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Page loads but is blank / 404 at `/` | Frontend not built — check the startup log for "No frontend build at ..." |
| Cannot reach the site at all | Port 8017 not open on the Network Security Group |
| `413 Request Entity Too Large` on upload | Only once nginx is in front: `client_max_body_size` defaults to 1 MB |
| Progress bar never moves, then jumps to done | Only behind nginx: SSE buffered; check `proxy_buffering off` |
| Meeting stays "uploaded" forever | The worker is not running — `systemctl status meeting_minutes_worker` |
| Translation stuck at "Queued — waiting for a worker" | Same cause. After 45s the page says so itself. The job is not lost: restart the worker and it runs |
| Worker logs `KeyError: 'transcripts.translate'` (or any task name) | The **worker process** predates that feature. A Celery worker only knows the tasks that existed when it started, and drops anything else — so a new API queues jobs the old worker silently discards. `sudo systemctl restart meeting_minutes_worker`. **`git pull` alone does not do this**, and these jobs are lost, so run them again |
| Worker was restarted but still runs the old code | `systemctl restart` on the wrong unit name silently does nothing useful — check `journalctl -u meeting_minutes_worker -n 5` for a recent start line |
| Service starts then exits immediately | Usually `.env` — systemd is stricter about quoting than a shell |
| Uploads fail, worker logs `ffmpeg not found on PATH` | `sudo apt install -y ffmpeg` |
| `permission denied` writing recordings | `LOCAL_STORAGE_DIR` does not exist or is not owned by `srvadmin` |
| `CREATE TABLE` permission denied | Missing `GRANT ALL ON SCHEMA public` on PostgreSQL 15+ |
| `type "vector" does not exist` | `CREATE EXTENSION vector` not run **in this database** — it is per database, not per server |
| `ModuleNotFoundError: No module named 'psycopg2'` | `DATABASE_URL` scheme — needs `postgresql+psycopg://`. Now auto-corrected, so this means an old checkout |
| `Temporary failure in name resolution` for `db` or `redis` | Docker container hostnames left in `.env`. Native uses `127.0.0.1` |
| Meetings reach `transcribed` then fail | `ANTHROPIC_API_KEY` empty while `AUTO_GENERATE_MINUTES=true` |
| 502 from nginx, but `systemctl` says running | uvicorn's master survives children that crash at import. Run `scripts/smoke_import.py` for the real error |
| `ERR_TOO_MANY_REDIRECTS` on an API call | An nginx `location` ending in `/` does not match the bare path and 301s to add the slash, while the app 307s to strip it. Use one `location /api/` |
| Record button disabled, "needs a secure connection" | Browsers only expose the microphone over HTTPS. Upload files until certbot has run |

---

## Notes

**Port 8017** is fixed in three places: both `.service` files (`--port`) and the
nginx config (`proxy_pass`). Change all three together.

**Binding.** The API listens on `0.0.0.0:8017`, matching the other apps, which
is what makes the IP deployment reachable. Once nginx and TLS are in front,
close 8017 on the NSG and optionally change `--host` to `127.0.0.1` — nginx
connects over loopback either way.

**No TLS on the IP deployment.** Traffic is plain HTTP, including the login
password. Fine for an internal trial on a trusted network; get a domain and
certbot before real meetings go through it.

**Docker** still works for local development (`docker compose up` at the repo
root) and is unaffected by any of this.
