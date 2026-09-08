import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { api, formatDuration, type Meeting } from "../lib/api";

export default function Meetings() {
  const [meetings, setMeetings] = useState<Meeting[]>([]);
  const [query, setQuery] = useState("");
  const [title, setTitle] = useState("");
  const [language, setLanguage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const fileInput = useRef<HTMLInputElement>(null);
  const recorder = useRef<MediaRecorder | null>(null);
  const chunks = useRef<Blob[]>([]);
  const [recording, setRecording] = useState(false);

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
    const timer = setInterval(() => refresh(query || undefined), 15000);
    return () => clearInterval(timer);
  }, [refresh, query]);

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

      const mr = new MediaRecorder(stream);
      chunks.current = [];
      mr.ondataavailable = (e) => e.data.size > 0 && chunks.current.push(e.data);
      mr.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        setRecording(false);
        setBusy(true);
        try {
          const blob = new Blob(chunks.current, { type: mr.mimeType || "audio/webm" });
          const meeting = await startMeeting("browser_mic");
          await api.uploadAudio(meeting.id, blob, "recording.webm");
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
    } catch (err) {
      setError(err instanceof Error ? err.message : "Microphone unavailable");
    }
  }

  return (
    <>
      <h1>Meetings</h1>
      <p className="sub">
        Record in the room, or upload an existing recording. Processing runs after the
        meeting ends.
      </p>

      <div className="panel">
        <div className="row">
          <input
            className="grow"
            placeholder="Meeting title"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
          <select value={language} onChange={(e) => setLanguage(e.target.value)}>
            <option value="">Auto-detect language</option>
            <option value="en">English</option>
            <option value="hi">Hindi</option>
            <option value="bn">Bengali</option>
          </select>
          <button
            className={recording ? "rec" : "primary"}
            onClick={toggleRecording}
            disabled={busy}
          >
            {recording ? "■ Stop recording" : "● Record"}
          </button>
          <button onClick={() => fileInput.current?.click()} disabled={busy || recording}>
            Upload file
          </button>
          <input
            ref={fileInput}
            type="file"
            accept="audio/*,video/*"
            style={{ display: "none" }}
            onChange={(e) => e.target.files?.[0] && onUpload(e.target.files[0])}
          />
        </div>
        <p className="small muted" style={{ margin: "10px 0 0" }}>
          Leave language on auto-detect for mixed-language meetings — forcing one
          language degrades code-switched speech.
        </p>
        {error && <p className="err small">{error}</p>}
        {busy && <p className="small muted">Uploading…</p>}
      </div>

      <div className="row" style={{ marginBottom: 12 }}>
        <input
          className="grow"
          placeholder="Search titles and transcripts…"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            refresh(e.target.value || undefined);
          }}
        />
      </div>

      <div className="panel" style={{ padding: 0 }}>
        <table>
          <thead>
            <tr>
              <th>Title</th>
              <th>Date</th>
              <th>Length</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {meetings.map((m) => (
              <tr key={m.id}>
                <td>
                  <Link to={`/meetings/${m.id}`}>{m.title}</Link>
                </td>
                <td className="muted small mono">
                  {new Date(m.started_at).toLocaleString()}
                </td>
                <td className="muted small mono">{formatDuration(m.duration_seconds)}</td>
                <td>
                  <span className={`badge ${m.status}`}>{m.status}</span>
                </td>
              </tr>
            ))}
            {meetings.length === 0 && (
              <tr>
                <td colSpan={4} className="muted" style={{ padding: 24 }}>
                  No meetings yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </>
  );
}
