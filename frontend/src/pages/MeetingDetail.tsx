import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import {
  API_BASE,
  api,
  formatDuration,
  formatTimestamp,
  type MeetingDetail as Detail,
  type Progress,
  type User,
} from "../lib/api";

const ACTIVE = new Set(["created", "uploaded", "processing"]);

export default function MeetingDetail() {
  const { id = "" } = useParams();
  const [meeting, setMeeting] = useState<Detail | null>(null);
  const [users, setUsers] = useState<User[]>([]);
  const [progress, setProgress] = useState<Progress | null>(null);
  // The transcript is the primary artifact; minutes are opt-in per phase, so
  // land on whichever the meeting actually has.
  const [tab, setTab] = useState<"minutes" | "transcript">("transcript");
  const tabChosen = useRef(false);
  const [audioSrc, setAudioSrc] = useState<string | null>(null);
  const [canRelabel, setCanRelabel] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const detail = await api.getMeeting(id);
      setMeeting(detail);
      if (!tabChosen.current && detail.minutes) {
        setTab("minutes");
        tabChosen.current = true;
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load meeting");
    }
  }, [id]);

  useEffect(() => {
    load();
    api.listUsers().then(setUsers).catch(() => undefined);
    api
      .listToggles()
      .then((t) => setCanRelabel(t.find((x) => x.key === "speaker_relabel_enabled")?.enabled ?? false))
      .catch(() => undefined);
  }, [load]);

  // Playback links are short-lived and signed, so fetch one only once the
  // meeting is known to still have audio.
  useEffect(() => {
    if (!meeting || meeting.audio_deleted_at) return;
    let cancelled = false;
    api
      .audioUrl(id)
      .then(({ url }) => !cancelled && setAudioSrc(url.startsWith("http") ? url : API_BASE + url))
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [meeting?.id, meeting?.audio_deleted_at, id]);

  // Follow the worker while the meeting is still being processed.
  useEffect(() => {
    if (!meeting || !ACTIVE.has(meeting.status)) return;

    const stream = api.progressStream(id);
    stream.onmessage = (event) => {
      const update = JSON.parse(event.data) as Progress;
      setProgress(update);
      if (update.stage === "done" || update.stage === "failed") {
        stream.close();
        load();
      }
    };
    stream.onerror = () => stream.close();
    return () => stream.close();
  }, [meeting?.status, id, load]);

  async function relabel(speakerLabel: string, userId: string) {
    setBusy(true);
    try {
      const updated = await api.relabel(id, {
        speaker_label: speakerLabel,
        user_id: userId || null,
        enroll: Boolean(userId),
      });
      setMeeting(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not update speaker");
    } finally {
      setBusy(false);
    }
  }

  async function regenerate(language?: string) {
    setBusy(true);
    try {
      await api.regenerateMinutes(id, language);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not regenerate minutes");
    } finally {
      setBusy(false);
    }
  }

  if (!meeting) return <p className="muted">{error || "Loading…"}</p>;

  const names = new Map(meeting.participants.map((p) => [p.speaker_label, p]));
  const minutes = meeting.minutes;

  return (
    <>
      <p className="small">
        <Link to="/meetings">← All meetings</Link>
      </p>
      <div className="spread">
        <div>
          <h1>{meeting.title}</h1>
          <p className="sub small">
            {new Date(meeting.started_at).toLocaleString()} ·{" "}
            {formatDuration(meeting.duration_seconds)} ·{" "}
            {meeting.asr_provider ?? "no provider"} ·{" "}
            <span className={`badge ${meeting.status}`}>{meeting.status}</span>
          </p>
        </div>
        <div className="row">
          <button
            className={meeting.minutes ? "" : "primary"}
            onClick={() => regenerate()}
            disabled={busy || !meeting.segments.length}
          >
            {busy ? "Working…" : meeting.minutes ? "Regenerate minutes" : "Generate minutes"}
          </button>
          <select
            defaultValue=""
            disabled={busy || !meeting.segments.length}
            onChange={(e) => e.target.value && regenerate(e.target.value)}
          >
            <option value="">Translate minutes…</option>
            <option value="en">English</option>
            <option value="hi">Hindi</option>
            <option value="bn">Bengali</option>
          </select>
          <button onClick={() => api.reprocess(id).then(load)} disabled={busy}>
            Reprocess audio
          </button>
        </div>
      </div>

      {error && <p className="err small">{error}</p>}

      {meeting.error && (
        <div className="panel">
          <h3>Processing failed</h3>
          <p className="err small mono">{meeting.error}</p>
        </div>
      )}

      {ACTIVE.has(meeting.status) && (
        <div className="panel">
          <div className="spread" style={{ marginBottom: 8 }}>
            <strong className="small">{progress?.message ?? "Queued…"}</strong>
            <span className="muted small mono">{progress?.percent ?? 0}%</span>
          </div>
          <div className="progress">
            <div style={{ width: `${progress?.percent ?? 0}%` }} />
          </div>
        </div>
      )}

      {meeting.participants.length > 0 && (
        <div className="panel">
          <h3>Speakers</h3>
          <table>
            <tbody>
              {meeting.participants
                .slice()
                .sort((a, b) => b.speaking_seconds - a.speaking_seconds)
                .map((p) => (
                  <tr key={p.speaker_label}>
                    <td style={{ width: 130 }} className="muted small mono">
                      {p.speaker_label}
                    </td>
                    <td>
                      <strong>{p.display_name}</strong>{" "}
                      {p.is_manual ? (
                        <span className="badge">confirmed</span>
                      ) : p.user_id ? (
                        <span className="muted small">
                          matched · confidence {p.confidence.toFixed(2)}
                        </span>
                      ) : (
                        <span className="muted small">no voiceprint match</span>
                      )}
                    </td>
                    <td className="muted small mono" style={{ width: 110 }}>
                      {formatDuration(p.speaking_seconds)}
                    </td>
                    {canRelabel && (
                      <td style={{ width: 220 }}>
                        <select
                          value={p.user_id ?? ""}
                          disabled={busy}
                          onChange={(e) => relabel(p.speaker_label, e.target.value)}
                        >
                          <option value="">Unidentified</option>
                          {users.map((u) => (
                            <option key={u.id} value={u.id}>
                              {u.full_name}
                            </option>
                          ))}
                        </select>
                      </td>
                    )}
                  </tr>
                ))}
            </tbody>
          </table>
          {canRelabel && (
            <p className="small muted" style={{ margin: "10px 0 0" }}>
              Naming an unidentified speaker also enrolls their voice, so the next
              meeting recognises them automatically.
            </p>
          )}
        </div>
      )}

      {meeting.audio_deleted_at ? (
        <p className="muted small">
          Recording deleted on{" "}
          {new Date(meeting.audio_deleted_at).toLocaleDateString()} under the retention
          policy. The transcript and minutes below are kept permanently.
        </p>
      ) : (
        audioSrc && (
          <div className="panel">
            <h3>Recording</h3>
            {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
            <audio controls src={audioSrc} style={{ width: "100%" }} />
          </div>
        )
      )}

      <div className="row" style={{ marginBottom: 12 }}>
        <button
          className={tab === "minutes" ? "primary" : ""}
          onClick={() => setTab("minutes")}
        >
          Minutes
        </button>
        <button
          className={tab === "transcript" ? "primary" : ""}
          onClick={() => setTab("transcript")}
        >
          Transcript ({meeting.segments.length})
        </button>
      </div>

      {tab === "minutes" &&
        (minutes ? (
          <>
            <div className="panel">
              <h3>Summary</h3>
              <p style={{ margin: 0 }}>{minutes.summary}</p>
            </div>

            {minutes.decisions.length > 0 && (
              <div className="panel">
                <h3>Decisions</h3>
                <ul className="clean">
                  {minutes.decisions.map((d, i) => (
                    <li key={i}>
                      <strong>{d.decision}</strong>
                      {d.decided_by && <span className="muted small"> — {d.decided_by}</span>}
                      {d.rationale && <div className="muted small">{d.rationale}</div>}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {minutes.action_items.length > 0 && (
              <div className="panel">
                <h3>Action items</h3>
                <ul className="clean">
                  {minutes.action_items.map((a, i) => (
                    <li key={i}>
                      <div className="spread">
                        <span>{a.task}</span>
                        <span className="muted small">
                          {a.owner}
                          {a.due ? ` · due ${a.due}` : ""} · {a.priority}
                        </span>
                      </div>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {minutes.topics.length > 0 && (
              <div className="panel">
                <h3>Topics</h3>
                <ul className="clean">
                  {minutes.topics.map((t, i) => (
                    <li key={i}>
                      <strong>{t.title}</strong>
                      <div className="muted small">{t.discussion}</div>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {minutes.open_questions.length > 0 && (
              <div className="panel">
                <h3>Open questions</h3>
                <ul className="clean">
                  {minutes.open_questions.map((q, i) => (
                    <li key={i}>{q}</li>
                  ))}
                </ul>
              </div>
            )}

            <p className="muted small">
              Generated by {minutes.model}
              {minutes.languages_detected.length > 0 &&
                ` · languages: ${minutes.languages_detected.join(", ")}`}
            </p>
          </>
        ) : (
          <div className="panel">
            <p className="muted" style={{ margin: 0 }}>
              No minutes yet. Generate them from the transcript with the button above —
              minutes are opt-in per meeting while you are validating transcript quality.
            </p>
          </div>
        ))}

      {tab === "transcript" && (
        <div className="panel">
          {meeting.segments.map((s) => {
            const participant = names.get(s.speaker_label);
            const unknown = !participant?.user_id;
            return (
              <div key={s.idx} className={`segment ${unknown ? "unknown" : ""}`}>
                <span className="ts">{formatTimestamp(s.start_ms)}</span>
                <span className="spk">
                  {participant?.display_name ?? s.speaker_label}
                  {s.language && <span className="lang"> {s.language}</span>}
                  {s.scripts?.includes("+") && (
                    <span className="lang mixed" title={`Scripts: ${s.scripts}`}>
                      {" "}mixed
                    </span>
                  )}
                </span>
                <span className="grow">{s.text}</span>
              </div>
            );
          })}
          {meeting.segments.length === 0 && <p className="muted">No transcript yet.</p>}
        </div>
      )}
    </>
  );
}
