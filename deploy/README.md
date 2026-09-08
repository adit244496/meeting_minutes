# Native deployment runbook

For a single Ubuntu server that already runs nginx and Postgres. About 20
minutes end to end.

Docker is still supported (`docker compose up` at the repo root) and is the
easier path for local development. Native is the better fit for a server that
already has nginx and Postgres, since this app is now an ordinary Python
service — 20 pip packages and ffmpeg, with no ML runtime.

---

## 1. System packages

```bash
sudo apt update
sudo apt install -y python3-venv ffmpeg redis-server nginx
sudo systemctl enable --now redis-server
```

`ffmpeg` is used on every meeting. `libsndfile1` and `espeak-ng` are only needed
for speaker identification and the synthetic test generator respectively — skip
them unless you turn those on.

Python 3.10 or newer is required: SQLAlchemy resolves `Mapped[str | None]`
annotations at runtime. Ubuntu 22.04 (3.10) and 24.04 (3.12) both work.

## 2. Service account and directories

Running as a dedicated unprivileged user is what makes the systemd hardening in
the unit files meaningful.

```bash
sudo useradd --system --home /opt/meeting-minutes --shell /usr/sbin/nologin meetingminutes
sudo mkdir -p /opt/meeting-minutes /var/lib/meeting-minutes/recordings
sudo chown -R meetingminutes:meetingminutes /opt/meeting-minutes /var/lib/meeting-minutes
```

`/var/lib/meeting-minutes` is the only path the services may write to — it holds
recordings and the Celery beat schedule.

## 3. Code and dependencies

```bash
sudo -u meetingminutes git clone https://github.com/adit244496/meeting_minutes.git /opt/meeting-minutes
cd /opt/meeting-minutes
sudo -u meetingminutes python3 -m venv venv
sudo -u meetingminutes venv/bin/pip install --upgrade pip
sudo -u meetingminutes venv/bin/pip install -r backend/requirements.txt
```

## 4. Database

You already have Postgres with pgvector:

```bash
sudo -u postgres psql <<'SQL'
CREATE DATABASE meeting_minutes;
CREATE USER mm WITH PASSWORD 'change-this';
GRANT ALL PRIVILEGES ON DATABASE meeting_minutes TO mm;
SQL

sudo -u postgres psql -d meeting_minutes -c 'CREATE EXTENSION IF NOT EXISTS vector;'
sudo -u postgres psql -d meeting_minutes -c 'GRANT ALL ON SCHEMA public TO mm;'
```

The last line matters on Postgres 15+, where `public` is no longer writable by
default and table creation fails without it.

## 5. Configuration

```bash
sudo -u meetingminutes cp .env.example .env
sudo -u meetingminutes nano .env
```

Change these from the defaults:

```bash
SECRET_KEY=<long random string>
ADMIN_PASSWORD=<something real>
GEMINI_API_KEY=<your key>
ANTHROPIC_API_KEY=<your key>          # only if AUTO_GENERATE_MINUTES=true

# Native paths, not the container ones
DATABASE_URL=postgresql+psycopg://mm:change-this@127.0.0.1:5432/meeting_minutes
REDIS_URL=redis://127.0.0.1:6379/0
LOCAL_STORAGE_DIR=/var/lib/meeting-minutes/recordings
```

```bash
sudo chmod 600 /opt/meeting-minutes/.env
```

**systemd parses `.env` itself — it is not a shell.** Do not quote values, do
not use `$VAR` expansion, and avoid `#` inside a value. A Postgres password with
special characters must be URL-encoded inside `DATABASE_URL`.

## 6. Frontend

Built once and served as static files by nginx. `VITE_API_URL` is empty so the
app calls its own origin — no CORS, and signed audio and SSE URLs resolve
without extra configuration.

```bash
cd /opt/meeting-minutes/frontend
sudo -u meetingminutes npm ci
sudo -u meetingminutes VITE_API_URL= npm run build
```

Rebuild after any frontend change; nginx serves `dist/` directly.

## 7. Services

```bash
sudo cp deploy/meeting-minutes-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meeting-minutes-api meeting-minutes-worker
systemctl status meeting-minutes-api meeting-minutes-worker
```

## 8. nginx

Edit `server_name` in the config first, then:

```bash
sudo cp deploy/nginx-meeting-minutes.conf /etc/nginx/sites-available/meeting-minutes
sudo ln -s /etc/nginx/sites-available/meeting-minutes /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d meetings.example.com
```

## 9. Verify

```bash
curl -s localhost:8000/health          # {"status":"ok",...}
curl -s https://meetings.example.com/health
journalctl -u meeting-minutes-worker -f
```

Sign in at your domain with `ADMIN_EMAIL` / `ADMIN_PASSWORD`, then upload a
short recording and watch the worker log.

---

## Operating it

**Deploying a change**

```bash
cd /opt/meeting-minutes
sudo -u meetingminutes git pull
sudo -u meetingminutes venv/bin/pip install -r backend/requirements.txt   # if deps changed
sudo -u meetingminutes bash -c 'cd frontend && npm ci && VITE_API_URL= npm run build'  # if frontend changed
sudo systemctl restart meeting-minutes-api meeting-minutes-worker
```

**Logs**

```bash
journalctl -u meeting-minutes-api -f
journalctl -u meeting-minutes-worker -f     # transcription errors appear here
```

**Retention** runs daily at 03:30 and deletes recordings older than
`RECORDING_RETENTION_DAYS`. Transcripts and minutes are never deleted. To run it
by hand:

```bash
cd /opt/meeting-minutes/backend
sudo -u meetingminutes ../venv/bin/python -c \
  "from app.worker.tasks import purge_old_recordings; print(purge_old_recordings())"
```

**Backups.** Recordings self-delete after 7 days; the transcripts and minutes in
Postgres are the artifact worth keeping. Back up the database, not the disk:

```bash
pg_dump -Fc meeting_minutes > meeting_minutes_$(date +%F).dump
```

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `413 Request Entity Too Large` on upload | `client_max_body_size` — nginx defaults to 1 MB |
| Progress bar never moves | SSE being buffered; check `proxy_buffering off` on `/api/meetings/` |
| Worker starts then exits | Usually `.env` — systemd is stricter about quoting than a shell |
| `permission denied` writing recordings | `LOCAL_STORAGE_DIR` outside `ReadWritePaths` in the unit file |
| `CREATE TABLE` permission denied | Missing `GRANT ALL ON SCHEMA public` on Postgres 15+ |
| Meetings reach `transcribed` then fail | `ANTHROPIC_API_KEY` empty while `AUTO_GENERATE_MINUTES=true` |
