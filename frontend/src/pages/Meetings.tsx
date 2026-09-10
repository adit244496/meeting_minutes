import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

import {
  IconAlert,
  IconMic,
  IconSearch,
  IconStop,
  IconUpload,
} from "../components/icons";
import { api, formatDuration, type Meeting } from "../lib/api";

/** Browsers only expose navigator.mediaDevices in a secure context: HTTPS, or
 *  localhost. Served over plain HTTP on an IP it is simply undefined, and
 *  touching .getUserMedia throws. Detect it up front and explain, rather than
 *  offering a Record button that crashes. */
function micSupport(): { ok: boolean; reason?: string } {
  if (typeof window === "undefined") return { ok: false };
  if (!window.isSecureContext) {
    return {
      ok: false,
      reason:
        "Recording needs a secure connection. This page is served over plain HTTP, " +
        "so the browser blocks microphone access. Upload a file instead, or set up " +
        "HTTPS to record in the browser.",
    };
  }
  if (!navigator.mediaDevices?.getUserMedia) {
    return { ok: false, reason: "This browser does not support audio recording." };
  }
  if (typeof MediaRecorder === "undefined") {
    return { ok: false, reason: "This browser does not support MediaRecorder." };
  }
  return { ok: true };
}

function elapsed(seconds: number) {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

export default function Meetings() {
  const [meetings, setMeetings] = useState<Meeting[]>([]);
  const [query, setQuery] = useState("");
  const [title, setTitle] = useState("");
  const [language, setLanguage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [recording, setRecording] = useState(false);
  const [seconds, setSeconds] = useState(0);

  const fileInput = useRef<HTMLInputElement>(null);
  const recorder = useRef<MediaRecorder | null>(null);
  const chunks = useRef<Blob[]>([]);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const mic = useMemo(micSupport, []);

  const refresh = useCallback(async (q?: string) => {
    try {
      setMeetings(await api.listMeetings(q));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load meetings");
    }
  }, []);

  useEffect(() => {
    refresh();
    // Anything mid-flight will change status; a slow poll keeps the list honest
    // without holding an SSE connection open for every row.
    const poll = setInterval(() => refresh(query || undefined), 15000);
    return () => clearInterval(poll);
  }, [refresh, query]);

  useEffect(() => () => { if (timer.current) clearInterval(timer.current); }, []);

  async function startMeeting(source: "upload" | "browser_mic") {
    if (!title.trim()) throw new Error("Give the meeting a title first");
    return api.createMeeting({
      title: title.trim(),
      source,
      language_hint: language || null,
    });
  }

  async function onUpload(file: File) {
    setBusy(true);
    setError("");
    try {
      const meeting = await startMeeting("upload");
      await api.uploadAudio(meeting.id, file, file.name);
      setTitle("");
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setBusy(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  async function toggleRecording() {
    if (recording) {
      recorder.current?.stop();
      return;
    }
    setError("");
    try {
      if (!title.trim()) throw new Error("Give the meeting a title first");
      if (!mic.ok) throw new Error(mic.reason ?? "Recording is unavailable");

      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          // The browser's aggressive defaults are tuned for one-to-one calls and
          // can suppress quieter speakers in a room; diarization needs everyone.
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: true,
          channelCount: 1,
        },
      });

      // iOS Safari does not produce webm; ask for what the device supports.
      const mime = ["audio/webm", "audio/mp4"].find(
        (t) => MediaRecorder.isTypeSupported?.(t),
      );
      const mr = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);

      chunks.current = [];
      mr.ondataavailable = (e) => e.data.size > 0 && chunks.current.push(e.data);
      mr.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        if (timer.current) clearInterval(timer.current);
        setRecording(false);
        setBusy(true);
        try {
          const type = mr.mimeType || "audio/webm";
          const blob = new Blob(chunks.current, { type });
          const ext = type.includes("mp4") ? "m4a" : "webm";
          const meeting = await startMeeting("browser_mic");
          await api.uploadAudio(meeting.id, blob, `recording.${ext}`);
          setTitle("");
          await refresh();
        } catch (err) {
          setError(err instanceof Error ? err.message : "Could not save recording");
        } finally {
          setBusy(false);
        }
      };

      mr.start(5000); // flush every 5s so a crash does not lose everything
      recorder.current = mr;
      setRecording(true);
      setSeconds(0);
      timer.current = setInterval(() => setSeconds((s) => s + 1), 1000);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Microphone unavailable");
    }
  }

  return (
    <>
      <div className="page-head">
        <h1>Meetings</h1>
        <p className="lead">
          Record in the room or upload an existing file. Transcription runs in the
          background once the meeting ends.
        </p>
      </div>

      <div className="card">
        <div className="card-head">
          <h3>New meeting</h3>
          {recording && (
            <span className="pill failed mono" aria-live="polite">
              Recording {elapsed(seconds)}
            </span>
          )}
        </div>

        <div className="card-body">
          <div className="form-grid cols-4">
            <label className="field">
              <span>Title</span>
              <input
                type="text"
                placeholder="e.g. Weekly project review"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                disabled={recording}
              />
            </label>

            <label className="field">
              <span>Language</span>
              <select
                value={language}
                onChange={(e) => setLanguage(e.target.value)}
                disabled={recording}
              >
                <option value="">Auto-detect (recommended)</option>
                <option value="en">English</option>
                <option value="hi">Hindi</option>
                <option value="bn">Bengali</option>
              </select>
            </label>

            <div className="btn-group" style={{ gridColumn: "span 2" }}>
              <button
                className={`btn ${recording ? "btn-rec" : "btn-primary"} grow`}
                onClick={toggleRecording}
                disabled={busy || !mic.ok}
                title={mic.ok ? undefined : mic.reason}
              >
                {recording ? <IconStop /> : <IconMic />}
                {recording ? "Stop recording" : "Record"}
              </button>
              <button
                className="btn grow"
                onClick={() => fileInput.current?.click()}
                disabled={busy || recording}
              >
                <IconUpload />
                Upload
              </button>
            </div>
          </div>

          <input
            ref={fileInput}
            type="file"
            accept="audio/*,video/*"
            className="sr-only"
            onChange={(e) => e.target.files?.[0] && onUpload(e.target.files[0])}
          />

          <div className="stack" style={{ marginTop: 14, gap: 10 }}>
            {!mic.ok && mic.reason && (
              <div className="alert alert-warn">
                <IconAlert size={16} />
                <span>{mic.reason}</span>
              </div>
            )}
            {error && (
              <div className="alert alert-err">
                <IconAlert size={16} />
                <span>{error}</span>
              </div>
            )}
            {busy && <p className="small dim">Uploading…</p>}
            <p className="small dim">
              Keep language on auto-detect for mixed-language meetings — forcing one
              language degrades code-switched speech.
            </p>
          </div>
        </div>
      </div>

      <div style={{ position: "relative", marginBottom: 14 }}>
        <input
          type="search"
          placeholder="Search titles and transcripts…"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            refresh(e.target.value || undefined);
          }}
          style={{ paddingLeft: 38 }}
        />
        <span
          style={{
            position: "absolute",
            left: 13,
            top: "50%",
            transform: "translateY(-50%)",
            display: "flex",
            color: "var(--text-3)",
            pointerEvents: "none",
          }}
        >
          <IconSearch size={16} />
        </span>
      </div>

      <div className="card">
        <div className="list">
          {meetings.map((m) => (
            <Link key={m.id} to={`/meetings/${m.id}`} className="list-row">
              <span className="cell-title">
                <span className="title">{m.title}</span>
                <span className="meta">
                  {new Date(m.started_at).toLocaleString()}
                  {m.duration_seconds ? ` · ${formatDuration(m.duration_seconds)}` : ""}
                </span>
              </span>
              <span className="cell-date num dim">
                {new Date(m.started_at).toLocaleDateString()}
              </span>
              <span className="cell-len num">{formatDuration(m.duration_seconds)}</span>
              <span className="cell-status">
                <span className={`pill ${m.status}`}>{m.status}</span>
              </span>
            </Link>
          ))}

          {meetings.length === 0 && (
            <div className="empty">
              <p className="big">{query ? "No matches" : "No meetings yet"}</p>
              <p className="small">
                {query
                  ? "Try a different search term."
                  : "Give a meeting a title above, then record or upload audio."}
              </p>
            </div>
          )}
        </div>
      </div>
    </>
  );
}
