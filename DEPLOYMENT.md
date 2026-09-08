# Deployment

Native install on the shared Ubuntu server, matching the pattern used by the
other apps. Port **8017**.

Two services, not one: the API, and a background worker that does the
transcription. Both must be running.

```
browser ──► nginx :80/:443 ──┬─► static frontend  (frontend/dist)
                             └─► /api  ──► uvicorn :8017
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

That last line matters on PostgreSQL 15+, where `public` is no longer writable
by default — without it, table creation fails on first start.

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

```bash
mkdir -p /home/srvadmin/meeting_minutes/recordings
```

**systemd parses `.env` itself — it is not a shell.** Do not quote values, do not
use `$VAR` expansion, and keep `#` out of the middle of a value. A database
password containing special characters must be URL-encoded inside
`DATABASE_URL`.

## 5. Frontend build

`VITE_API_URL` is left empty so the app calls its own origin.

```bash
cd ~/meeting_minutes/meeting_minutes/frontend
npm ci
VITE_API_URL= npm run build
```

Produces `frontend/dist`, which nginx serves directly.

## 6. Services

```bash
cd ~/meeting_minutes/meeting_minutes
sudo cp meeting_minutes.service meeting_minutes_worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meeting_minutes meeting_minutes_worker
systemctl status meeting_minutes meeting_minutes_worker
```

## 7. nginx

Change `server_name` in the config first, then:

```bash
sudo cp meeting_minutes.nginx.conf /etc/nginx/sites-available/meeting_minutes
sudo ln -s /etc/nginx/sites-available/meeting_minutes /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d mom.ambujaneotia.com
```

## 8. Verify

```bash
curl -s localhost:8017/health         # {"status":"ok","asr_provider":"gemini",...}
journalctl -u meeting_minutes_worker -f
```

Sign in at the domain, upload a short recording, and watch the worker log.

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
| `413 Request Entity Too Large` on upload | `client_max_body_size` — nginx defaults to 1 MB |
| Progress bar never moves, then jumps to done | SSE buffered; check `proxy_buffering off` in the `/api/meetings/` block |
| Meeting stays "uploaded" forever | The worker is not running — `systemctl status meeting_minutes_worker` |
| Service starts then exits immediately | Usually `.env` — systemd is stricter about quoting than a shell |
| `permission denied` writing recordings | `LOCAL_STORAGE_DIR` does not exist or is not owned by `srvadmin` |
| `CREATE TABLE` permission denied | Missing `GRANT ALL ON SCHEMA public` on PostgreSQL 15+ |
| Meetings reach `transcribed` then fail | `ANTHROPIC_API_KEY` empty while `AUTO_GENERATE_MINUTES=true` |

---

## Notes

**Port 8017** is fixed in three places: both `.service` files (`--port`) and the
nginx config (`proxy_pass`). Change all three together.

**Binding.** The API listens on `0.0.0.0:8017`, matching the other apps. Only
80/443 should be open on the Network Security Group, so nothing reaches 8017
from outside — but if you want defence in depth, change `--host` to `127.0.0.1`
in `meeting_minutes.service`. nginx connects over loopback either way.

**Docker** still works for local development (`docker compose up` at the repo
root) and is unaffected by any of this.
