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

`ffmpeg` runs on every meeting. Python 3.10+ is required — SQLAlchemy resolves
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

## 9. Later: domain and TLS

Only when you want a proper hostname. Until then nginx is not involved at all.

```bash
sudo cp meeting_minutes.nginx.conf /etc/nginx/sites-available/meeting_minutes
sudo ln -s /etc/nginx/sites-available/meeting_minutes /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d mom.ambujaneotia.com
```

Change `server_name` in the file first. Nothing in the app changes — nginx just
proxies everything to 8017. Once that is working, close 8017 on the NSG so
traffic only arrives over 443.

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
| Service starts then exits immediately | Usually `.env` — systemd is stricter about quoting than a shell |
| `permission denied` writing recordings | `LOCAL_STORAGE_DIR` does not exist or is not owned by `srvadmin` |
| `CREATE TABLE` permission denied | Missing `GRANT ALL ON SCHEMA public` on PostgreSQL 15+ |
| `type "vector" does not exist` | `CREATE EXTENSION vector` not run **in this database** — it is per database, not per server |
| `ModuleNotFoundError: No module named 'psycopg2'` | `DATABASE_URL` scheme — needs `postgresql+psycopg://`. Now auto-corrected, so this means an old checkout |
| `Temporary failure in name resolution` for `db` or `redis` | Docker container hostnames left in `.env`. Native uses `127.0.0.1` |
| Meetings reach `transcribed` then fail | `ANTHROPIC_API_KEY` empty while `AUTO_GENERATE_MINUTES=true` |
| 502 from nginx, but `systemctl` says running | uvicorn's master survives children that crash at import. Run `scripts/smoke_import.py` for the real error |

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
