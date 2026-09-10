import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { IconAlert, IconChevronLeft, IconRefresh, IconSparkle } from "../components/icons";
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
      .then((t) =>
        setCanRelabel(t.find((x) => x.key === "speaker_relabel_enabled")?.enabled ?? false),
      )
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
      setMeeting(
        await api.relabel(id, {
          speaker_label: speakerLabel,
          user_id: userId || null,
          enroll: Boolean(userId),
        }),
      );
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
      setError(err instanceof Error ? err.message : "Could not generate minutes");
    } finally {
      setBusy(false);
    }
  }

  if (!meeting) {
    return <p className="dim">{error || "Loading…"}</p>;
  }

  const names = new Map(meeting.participants.map((p) => [p.speaker_label, p]));
  const minutes = meeting.minutes;
  const speakers = meeting.participants
    .slice()
    .sort((a, b) => b.speaking_seconds - a.speaking_seconds);

  return (
    <>
      <Link to="/meetings" className="crumb">
        <IconChevronLeft size={14} />
        All meetings
      </Link>

      <div className="page-head">
        <div className="page-head-row">
          <div style={{ minWidth: 0 }}>
            <h1>{meeting.title}</h1>
            <div className="row small dim" style={{ marginTop: 7, gap: 8 }}>
              <span className={`pill ${meeting.status}`}>{meeting.status}</span>
              <span>{new Date(meeting.started_at).toLocaleString()}</span>
              {meeting.duration_seconds ? (
                <span className="mono">{formatDuration(meeting.duration_seconds)}</span>
              ) : null}
              {meeting.asr_provider && <span>via {meeting.asr_provider}</span>}
            </div>
          </div>

          <div className="btn-group">
            <button
              className={`btn btn-sm ${minutes ? "" : "btn-primary"}`}
              onClick={() => regenerate()}
              disabled={busy || !meeting.segments.length}
            >
              <IconSparkle size={15} />
              {busy ? "Working…" : minutes ? "Regenerate minutes" : "Generate minutes"}
            </button>
            <select
              defaultValue=""
              disabled={busy || !meeting.segments.length}
              onChange={(e) => e.target.value && regenerate(e.target.value)}
              style={{ width: "auto", minHeight: 34, fontSize: "0.82rem" }}
              aria-label="Translate minutes"
            >
              <option value="">Translate…</option>
              <option value="en">English</option>
              <option value="hi">Hindi</option>
              <option value="bn">Bengali</option>
            </select>
            <button
              className="btn btn-sm"
              onClick={() => api.reprocess(id).then(load)}
              disabled={busy}
            >
              <IconRefresh size={15} />
              Reprocess
            </button>
          </div>
        </div>
      </div>

      {error && (
        <div className="alert alert-err" style={{ marginBottom: 16 }}>
          <IconAlert size={16} />
          <span>{error}</span>
        </div>
      )}

      {meeting.error && (
        <div className="card">
          <div className="card-head">
            <h3>Processing failed</h3>
          </div>
          <div className="card-body">
            <p className="small mono err">{meeting.error}</p>
          </div>
        </div>
      )}

      {ACTIVE.has(meeting.status) && (
        <div className="card">
          <div className="card-body">
            <div className="spread" style={{ marginBottom: 9 }}>
              <strong className="small">{progress?.message ?? "Queued…"}</strong>
              <span className="dim small mono">{progress?.percent ?? 0}%</span>
            </div>
            <div className="bar">
              <i style={{ width: `${progress?.percent ?? 0}%` }} />
            </div>
          </div>
        </div>
      )}

      {meeting.audio_deleted_at ? (
        <p className="small dim" style={{ marginBottom: 16 }}>
          Recording deleted on {new Date(meeting.audio_deleted_at).toLocaleDateString()} under
          the retention policy. The transcript and minutes below are kept permanently.
        </p>
      ) : (
        audioSrc && (
          <div className="card">
            <div className="card-head">
              <h3>Recording</h3>
            </div>
            <div className="card-body">
              {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
              <audio controls src={audioSrc} style={{ width: "100%" }} />
            </div>
          </div>
        )
      )}

      {speakers.length > 0 && (
        <div className="card">
          <div className="card-head">
            <h3>Speakers</h3>
            <span className="dim tiny">{speakers.length} detected</span>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Speaker</th>
                  <th>Match</th>
                  <th>Speaking time</th>
                  {canRelabel && <th>Assign</th>}
                </tr>
              </thead>
              <tbody>
                {speakers.map((p) => (
                  <tr key={p.speaker_label}>
                    <td data-label="Speaker">
                      <span>
                        <strong>{p.display_name}</strong>
                        <span className="dim tiny mono" style={{ marginLeft: 8 }}>
                          {p.speaker_label}
                        </span>
                      </span>
                    </td>
                    <td data-label="Match">
                      {p.is_manual ? (
                        <span className="pill completed">confirmed</span>
                      ) : p.user_id ? (
                        <span className="pill transcribed">
                          matched {p.confidence.toFixed(2)}
                        </span>
                      ) : (
                        <span className="dim small">not identified</span>
                      )}
                    </td>
                    <td data-label="Speaking time" className="mono small">
                      {formatDuration(p.speaking_seconds)}
                    </td>
                    {canRelabel && (
                      <td data-label="Assign">
                        <select
                          value={p.user_id ?? ""}
                          disabled={busy}
                          onChange={(e) => relabel(p.speaker_label, e.target.value)}
                          style={{ minHeight: 36, maxWidth: 220 }}
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
          </div>
          {canRelabel && (
            <div className="card-foot">
              Naming an unidentified speaker also enrolls their voice, so the next meeting
              recognises them automatically.
            </div>
          )}
        </div>
      )}

      <div className="segmented" role="tablist" style={{ marginBottom: 14 }}>
        <button
          role="tab"
          aria-selected={tab === "transcript"}
          onClick={() => setTab("transcript")}
        >
          Transcript ({meeting.segments.length})
        </button>
        <button role="tab" aria-selected={tab === "minutes"} onClick={() => setTab("minutes")}>
          Minutes
        </button>
      </div>

      {tab === "transcript" && (
        <div className="card">
          {meeting.segments.length ? (
            meeting.segments.map((s) => {
              const participant = names.get(s.speaker_label);
              const mixed = s.scripts?.includes("+");
              return (
                <div key={s.idx} className={`seg ${participant?.user_id ? "" : "unknown"}`}>
                  <span className="ts">{formatTimestamp(s.start_ms)}</span>
                  <span className="who">
                    {participant?.display_name ?? s.speaker_label}
                    {(s.language || mixed) && (
                      <span className={`tag ${mixed ? "mixed" : ""}`}>
                        {mixed ? "mixed" : s.language}
                      </span>
                    )}
                  </span>
                  <span className="said">{s.text}</span>
                </div>
              );
            })
          ) : (
            <div className="empty">
              <p className="big">No transcript yet</p>
              <p className="small">It appears here once processing finishes.</p>
            </div>
          )}
        </div>
      )}

      {tab === "minutes" &&
        (minutes ? (
          <>
            <div className="card">
              <div className="card-head">
                <h3>Summary</h3>
              </div>
              <div className="card-body">
                <p className="summary-text">{minutes.summary}</p>
              </div>
            </div>

            {minutes.decisions.length > 0 && (
              <div className="card">
                <div className="card-head">
                  <h3>Decisions</h3>
                  <span className="dim tiny">{minutes.decisions.length}</span>
                </div>
                <div className="card-body">
                  <ul className="item-list">
                    {minutes.decisions.map((d, i) => (
                      <li key={i}>
                        <div className="item-head">
                          <strong>{d.decision}</strong>
                          {d.decided_by && <span className="dim small">{d.decided_by}</span>}
                        </div>
                        {d.rationale && <p className="item-note">{d.rationale}</p>}
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
            )}

            {minutes.action_items.length > 0 && (
              <div className="card">
                <div className="card-head">
                  <h3>Action items</h3>
                  <span className="dim tiny">{minutes.action_items.length}</span>
                </div>
                <div className="card-body">
                  <ul className="item-list">
                    {minutes.action_items.map((a, i) => (
                      <li key={i}>
                        <div className="task">
                          <span className="box" />
                          <div className="grow">
                            <div className="item-head">
                              <span>{a.task}</span>
                              <span className="dim small nowrap">
                                {a.owner}
                                {a.due ? ` · due ${a.due}` : ""}
                              </span>
                            </div>
                          </div>
                        </div>
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
            )}

            {minutes.topics.length > 0 && (
              <div className="card">
                <div className="card-head">
                  <h3>Topics</h3>
                </div>
                <div className="card-body">
                  <ul className="item-list">
                    {minutes.topics.map((t, i) => (
                      <li key={i}>
                        <strong>{t.title}</strong>
                        <p className="item-note">{t.discussion}</p>
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
            )}

            {minutes.open_questions.length > 0 && (
              <div className="card">
                <div className="card-head">
                  <h3>Open questions</h3>
                </div>
                <div className="card-body">
                  <ul className="item-list">
                    {minutes.open_questions.map((q, i) => (
                      <li key={i}>{q}</li>
                    ))}
                  </ul>
                </div>
              </div>
            )}

            <p className="tiny dim">
              Generated by {minutes.model}
              {minutes.languages_detected.length > 0 &&
                ` · languages: ${minutes.languages_detected.join(", ")}`}
            </p>
          </>
        ) : (
          <div className="card">
            <div className="empty">
              <p className="big">No minutes yet</p>
              <p className="small">Generate them from the transcript with the button above.</p>
            </div>
          </div>
        ))}
    </>
  );
}
