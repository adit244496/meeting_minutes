import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { Menu } from "../components/Menu";
import {
  IconAlert,
  IconClose,
  IconDownload,
  IconMic,
  IconRepeat,
  IconSearch,
  IconStop,
  IconUpload,
} from "../components/icons";
import {
  api,
  formatDuration,
  formatTimestamp,
  type Meeting,
  type MeetingDetail,
  type Series,
  useMeetingProgress,
} from "../lib/api";
import { formatElapsed, micSupport, useRecorder } from "../lib/recorder";

const ACTIVE = new Set(["created", "uploaded", "processing"]);

function speakerName(label: string) {
  const n = /(\d+)$/.exec(label);
  return n ? `Speaker ${Number(n[1]) + 1}` : label;
}

/** A stable colour per title, so a recurring meeting keeps its colour in the list. */
function tint(text: string) {
  let h = 0;
  for (const ch of text) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return `hsl(${h} 62% 52%)`;
}

function monogram(text: string) {
  const words = text.replace(/[^\p{L}\p{N} ]/gu, " ").split(/\s+/).filter(Boolean);
  return ((words[0]?.[0] ?? "?") + (words[1]?.[0] ?? "")).toUpperCase();
}

function MeetingRow({ meeting: m, onFinish }: { meeting: Meeting; onFinish: () => void }) {
  // "created" with no audio yet is not being processed, so no stream for it.
  const active = ACTIVE.has(m.status) && m.status !== "created";
  const progress = useMeetingProgress(m.id, active, onFinish);
  const waiting = progress?.stage === "waiting";

  return (
    <Link to={`/meetings/${m.id}`} className="list-row">
      <span className="row-mark" style={{ ["--tint" as string]: tint(m.series_name ?? m.title) }} aria-hidden="true">
        {monogram(m.series_name ?? m.title)}
      </span>
      <span className="cell-title">
        <span className="title">
          {m.title}
          {m.is_live && <span className="live-chip">● Live</span>}
          {m.series_name && (
            <span className="series-chip" title="Recurring meeting series">
              <IconRepeat size={11} />
              {m.series_name}
            </span>
          )}
        </span>
        {active ? (
          <span className="row-progress">
            <span className={`bar ${waiting ? "bar-warn" : "bar-live"}`}>
              <i style={{ width: `${Math.max(progress?.percent ?? 0, 2)}%` }} />
            </span>
            <span className="meta">{progress ? `${progress.percent}% · ${progress.message}` : "Queued…"}</span>
          </span>
        ) : (
          <span className="meta">
            {new Date(m.started_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}
            {m.duration_seconds ? ` · ${formatDuration(m.duration_seconds)}` : ""}
          </span>
        )}
      </span>
      <span className="cell-status">
        <span className={`pill ${m.status}`}>{m.is_live ? "recording" : m.status}</span>
      </span>
    </Link>
  );
}

/** The live transcript of the recording in progress, under the new-meeting bar. */
function LiveTranscript({ meetingId }: { meetingId: string }) {
  const [detail, setDetail] = useState<MeetingDetail | null>(null);
  const progress = useMeetingProgress(meetingId, true);

  useEffect(() => {
    let cancelled = false;
    const pull = () =>
      api
        .getMeeting(meetingId)
        .then((d) => !cancelled && setDetail(d))
        .catch(() => undefined);
    pull();
    const poll = setInterval(pull, 15000);
    return () => {
      cancelled = true;
      clearInterval(poll);
    };
  }, [meetingId, progress?.message]);

  return (
    <div className="live-transcript" ref={(el) => el && (el.scrollTop = el.scrollHeight)}>
      {progress?.stage === "live" && progress.message && <p className="dim tiny live-status">{progress.message}</p>}
      {detail?.segments.length ? (
        detail.segments.slice(-40).map((s) => (
          <p key={s.idx}>
            <span className="ts">{formatTimestamp(s.start_ms)}</span>
            <strong>{speakerName(s.speaker_label)}</strong> {s.text}
          </p>
        ))
      ) : (
        <p className="dim small">
          The live transcript appears about a minute after recording starts, then follows a minute or two behind.
        </p>
      )}
    </div>
  );
}

export default function Meetings() {
  const [meetings, setMeetings] = useState<Meeting[]>([]);
  const [query, setQuery] = useState("");
  const [title, setTitle] = useState("");
  const [language, setLanguage] = useState("");
  // "" = not recurring, "new" = create a series from the title, otherwise a series id.
  const [seriesChoice, setSeriesChoice] = useState("");
  const [seriesList, setSeriesList] = useState<Series[]>([]);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState("");
  const [stats, setStats] = useState<{ total: number; hours: number; done: number; active: number } | null>(null);

  const recorder = useRecorder();
  const recording = recorder.status === "recording" || recorder.status === "starting";
  const fileInput = useRef<HTMLInputElement>(null);
  const mic = useMemo(micSupport, []);

  const [exportDays, setExportDays] = useState(7);
  const [exportKind, setExportKind] = useState<"minutes" | "transcript" | "both">("minutes");
  const [exporting, setExporting] = useState(false);

  useEffect(() => {
    api.listSeries().then(setSeriesList).catch(() => undefined);
  }, []);

  const refresh = useCallback(async (q?: string) => {
    api
      .overview()
      .then((o) =>
        setStats({
          total: o.total_meetings,
          hours: o.total_hours,
          done: (o.by_status.completed ?? 0) + (o.by_status.transcribed ?? 0),
          active: (o.by_status.processing ?? 0) + (o.by_status.uploaded ?? 0),
        }),
      )
      .catch(() => undefined);
    try {
      setMeetings(await api.listMeetings(q));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load meetings");
    }
  }, []);

  useEffect(() => {
    refresh(query || undefined);
    // Anything mid-flight will change status; a slow poll keeps the list honest
    // without holding an SSE connection open for every row.
    const poll = setInterval(() => refresh(query || undefined), 15000);
    return () => clearInterval(poll);
    // savedCount changes when a recording starts or finishes uploading.
  }, [refresh, query, recorder.savedCount]);

  async function exportArchive() {
    setExporting(true);
    setError("");
    try {
      await api.exportMeetings(exportKind, exportDays);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Export failed");
    } finally {
      setExporting(false);
    }
  }

  function meetingOptions() {
    if (!title.trim()) throw new Error("Give the meeting a title first");
    return {
      title: title.trim(),
      language_hint: language || null,
      series_id: seriesChoice && seriesChoice !== "new" ? seriesChoice : null,
      new_series_name: seriesChoice === "new" ? title.trim() : null,
    };
  }

  async function onUpload(file: File) {
    setUploading(true);
    setError("");
    try {
      const meeting = await api.createMeeting({ ...meetingOptions(), source: "upload" });
      await api.uploadAudio(meeting.id, file, file.name);
      setTitle("");
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  async function toggleRecording() {
    if (recording) {
      recorder.stop();
      return;
    }
    setError("");
    try {
      await recorder.start(meetingOptions());
      setTitle("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start recording");
    }
  }

  return (
    <>
      <div className="page-head page-head-row">
        <div>
          <h1>Meetings</h1>
          <p className="lead">Record live or upload a file — transcripts and minutes are written for you.</p>
        </div>
        {stats && (
          <div className="kpis" aria-label="Overview">
            <span className="kpi">
              <b>{stats.total}</b>
              <small>meetings</small>
            </span>
            <span className="kpi">
              <b>{stats.hours < 10 ? stats.hours.toFixed(1) : Math.round(stats.hours)}</b>
              <small>hours</small>
            </span>
            <span className="kpi tone-ok">
              <b>{stats.done}</b>
              <small>ready</small>
            </span>
            {stats.active > 0 && (
              <span className="kpi tone-accent">
                <b>{stats.active}</b>
                <small>processing</small>
              </span>
            )}
          </div>
        )}
      </div>

      <div className="card new-meeting">
        <input
          type="text"
          placeholder="Meeting title, e.g. Weekly project review"
          value={recording ? recorder.title : title}
          onChange={(e) => setTitle(e.target.value)}
          disabled={recording}
          aria-label="Meeting title"
        />
        <select
          value={language}
          onChange={(e) => setLanguage(e.target.value)}
          disabled={recording}
          aria-label="Language"
          title="Keep auto-detect for mixed-language meetings"
        >
          <option value="">Auto-detect language</option>
          <option value="en">English</option>
          <option value="hi">Hindi</option>
          <option value="bn">Bengali</option>
        </select>
        <select
          value={seriesChoice}
          onChange={(e) => setSeriesChoice(e.target.value)}
          disabled={recording}
          aria-label="Recurring meeting"
          title="Link recurring meetings so each one follows up on the last"
        >
          <option value="">One-off meeting</option>
          <option value="new">New recurring series</option>
          {seriesList.length > 0 && (
            <optgroup label="Add to series">
              {seriesList.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name} ({s.meeting_count})
                </option>
              ))}
            </optgroup>
          )}
        </select>
        <button
          className={`btn ${recording ? "btn-rec" : "btn-primary"}`}
          onClick={toggleRecording}
          disabled={recorder.status === "saving" || recorder.status === "starting" || uploading || !mic.ok}
          title={mic.ok ? undefined : mic.reason}
        >
          {recording ? <IconStop size={15} /> : <IconMic size={15} />}
          {recorder.status === "starting"
            ? "Starting…"
            : recording
              ? `Stop ${formatElapsed(recorder.seconds)}`
              : recorder.status === "saving"
                ? "Saving…"
                : "Record"}
        </button>
        <button className="btn" onClick={() => fileInput.current?.click()} disabled={uploading || recording}>
          <IconUpload size={15} />
          {uploading ? "Uploading…" : "Upload"}
        </button>
        <input
          ref={fileInput}
          type="file"
          accept="audio/*,video/*"
          className="sr-only"
          onChange={(e) => e.target.files?.[0] && onUpload(e.target.files[0])}
        />
      </div>

      {(error || (!mic.ok && mic.reason)) && (
        <div className={`alert ${error ? "alert-err" : "alert-warn"} compact-gap`}>
          <IconAlert size={15} />
          <span className="grow">{error || mic.reason}</span>
          {error && (
            <button className="icon-link" onClick={() => setError("")} aria-label="Dismiss">
              <IconClose size={14} />
            </button>
          )}
        </div>
      )}

      {recorder.status === "recording" && recorder.meetingId && recorder.liveOn && (
        <div className="card live-card">
          <div className="card-head card-head-tight">
            <div className="row" style={{ gap: 8 }}>
              <span className="live-dot" aria-hidden="true" />
              <h3>Live transcript</h3>
            </div>
            <Link className="small" to={`/meetings/${recorder.meetingId}`}>
              Open meeting
            </Link>
          </div>
          <LiveTranscript meetingId={recorder.meetingId} />
        </div>
      )}

      {/* overflow-visible so the Export dropdown is not clipped by the card. */}
      <div className="card card-overflow">
        <div className="toolbar">
          <label className="search-box">
            <IconSearch size={16} />
            <input
              type="search"
              placeholder="Search titles and transcripts…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              aria-label="Search meetings"
            />
            {query && (
              <button type="button" className="icon-link" onClick={() => setQuery("")} aria-label="Clear search">
                <IconClose size={14} />
              </button>
            )}
          </label>

          <Menu label="Export" icon={<IconDownload size={15} />}>
            <div className="menu-form">
              <label className="field">
                <span>Timeframe</span>
                <select value={exportDays} onChange={(e) => setExportDays(Number(e.target.value))}>
                  <option value={7}>Last 7 days</option>
                  <option value={30}>Last 1 month</option>
                  <option value={90}>Last 3 months</option>
                  <option value={365}>Last 1 year</option>
                  <option value={0}>All time</option>
                </select>
              </label>
              <label className="field">
                <span>Include</span>
                <select value={exportKind} onChange={(e) => setExportKind(e.target.value as typeof exportKind)}>
                  <option value="minutes">Meeting minutes</option>
                  <option value="transcript">Transcripts</option>
                  <option value="both">Minutes and transcripts</option>
                </select>
              </label>
              <button className="btn btn-sm btn-primary btn-block" data-close onClick={exportArchive} disabled={exporting}>
                <IconDownload size={14} />
                {exporting ? "Preparing…" : "Download ZIP"}
              </button>
            </div>
          </Menu>
        </div>
        <div className="list">
          {meetings.map((m) => (
            <MeetingRow key={m.id} meeting={m} onFinish={() => refresh(query || undefined)} />
          ))}

          {meetings.length === 0 && (
            <div className="empty">
              <p className="big">{query ? "No matches" : "No meetings yet"}</p>
              <p className="small">
                {query ? "Try a different search term." : "Give a meeting a title above, then record or upload audio."}
              </p>
            </div>
          )}
        </div>
      </div>
    </>
  );
}
