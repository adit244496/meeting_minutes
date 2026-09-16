import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useOutletContext, useParams } from "react-router-dom";

import { Menu, MenuGroup, MenuItem } from "../components/Menu";
import {
  IconAlert,
  IconCheck,
  IconChevronLeft,
  IconClock,
  IconClose,
  IconDownload,
  IconEdit,
  IconHistory,
  IconMeetings,
  IconPlus,
  IconRefresh,
  IconRepeat,
  IconSparkle,
  IconStar,
  IconStop,
  IconTrash,
} from "../components/icons";
import {
  API_BASE,
  api,
  formatDuration,
  formatTimestamp,
  type MeetingDetail as Detail,
  type Minutes,
  type MinutesKind,
  type MinutesModel,
  type MinutesVersion,
  type FollowUp,
  type FollowUpStatus,
  type Progress,
  type Series,
  type SeriesMeeting,
  type SeriesSuggestion,
  type User,
  useMeetingProgress,
} from "../lib/api";
import { formatElapsed, requestRemoteStop, useRecorder } from "../lib/recorder";

const ACTIVE = new Set(["created", "uploaded", "processing"]);

type Content = Pick<
  Minutes,
  "summary" | "key_points" | "follow_ups" | "decisions" | "action_items" | "topics" | "open_questions"
>;

const FOLLOW_UP_STATUS: Record<FollowUpStatus, { label: string; tone: string }> = {
  done: { label: "Done", tone: "ok" },
  in_progress: { label: "In progress", tone: "accent" },
  not_started: { label: "Not started", tone: "warn" },
  blocked: { label: "Blocked", tone: "err" },
  dropped: { label: "Dropped", tone: "muted" },
  not_discussed: { label: "Not discussed", tone: "muted" },
};

const SOURCE_LABEL: Record<string, string> = {
  generated: "Generated",
  edited: "Edited",
  restored: "Restored",
};

const LANGUAGES: [string, string][] = [
  ["en", "English"],
  ["hi", "Hindi"],
  ["bn", "Bengali"],
];

export default function MeetingDetail() {
  const { id = "" } = useParams();
  const [meeting, setMeeting] = useState<Detail | null>(null);
  const [users, setUsers] = useState<User[]>([]);
  const [tab, setTab] = useState<"minutes" | "transcript" | "series">("minutes");
  const [seriesList, setSeriesList] = useState<Series[]>([]);
  const [suggestion, setSuggestion] = useState<SeriesSuggestion | null>(null);
  const [kind, setKind] = useState<MinutesKind>("short");
  const kindChosen = useRef(false);
  const [audioSrc, setAudioSrc] = useState<string | null>(null);
  // Safari cannot play WebM at all, so a recording made in Chrome will not play
  // on an iPhone. Say so and offer the file rather than leaving a dead player.
  const [audioFailed, setAudioFailed] = useState(false);
  const [canRelabel, setCanRelabel] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const [editing, setEditing] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [history, setHistory] = useState<MinutesVersion[]>([]);
  const [preview, setPreview] = useState<MinutesVersion | null>(null);

  const load = useCallback(async () => {
    try {
      const detail = await api.getMeeting(id);
      setMeeting(detail);
      // A live recording has no minutes yet - open on the growing transcript.
      if (detail.is_live && !kindChosen.current && !detail.minutes.length) setTab("transcript");
      // Open on the short minutes; fall back to whatever exists.
      if (!kindChosen.current && detail.minutes.length) {
        kindChosen.current = true;
        if (!detail.minutes.some((m) => m.kind === "short")) setKind("detailed");
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
      .then(({ url }) => {
        if (cancelled) return;
        setAudioFailed(false);
        setAudioSrc(url.startsWith("http") ? url : API_BASE + url);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [meeting?.id, meeting?.audio_deleted_at, id]);

  // Follow the worker while the meeting is still being processed.
  // A live recording is "created" until it is uploaded, but it is not queued -
  // it gets its own banner rather than the processing card.
  const isLive = Boolean(meeting?.is_live);
  const isActive = Boolean(meeting && ACTIVE.has(meeting.status) && !isLive);
  const { user } = useOutletContext<{ user: User }>();
  const isAdmin = user?.role === "admin";
  const recorder = useRecorder();
  const navigate = useNavigate();
  const progress = useMeetingProgress(id, isActive, () => load());

  // Safety net for a missed "done" event.
  useEffect(() => {
    if (!isActive && !isLive) return;
    const poll = setInterval(load, isLive ? 10000 : 20000);
    return () => clearInterval(poll);
  }, [isActive, isLive, load]);

  const refreshHistory = useCallback(async () => {
    try {
      setHistory(await api.listMinutesVersions(id, kind));
    } catch {
      setHistory([]);
    }
  }, [id, kind]);

  useEffect(() => {
    if (showHistory) refreshHistory();
  }, [showHistory, refreshHistory]);

  async function run(action: () => Promise<unknown>, fallback: string) {
    setBusy(true);
    setError("");
    try {
      await action();
      return true;
    } catch (err) {
      setError(err instanceof Error ? err.message : fallback);
      return false;
    } finally {
      setBusy(false);
    }
  }

  // Minutes run as a background job; follow it over the same progress stream.
  const [runningKind, setRunningKind] = useState<MinutesKind | null>(null);
  const minutesProgress = useMeetingProgress(id, runningKind !== null, (final) => {
    setRunningKind(null);
    if (final.stage === "minutes_failed" || final.stage === "failed") {
      setError(final.message || "Could not generate minutes");
    }
    load();
    if (showHistory) refreshHistory();
  });

  // Pick up a generation already running, e.g. after a page reload.
  useEffect(() => {
    api
      .currentProgress(id)
      .then((state) => {
        if (state.stage === "minutes" && state.age_seconds != null && state.age_seconds < 300) {
          const detailed = (state.message ?? "").toLowerCase().includes("detailed");
          setRunningKind(detailed ? "detailed" : "short");
        }
      })
      .catch(() => undefined);
  }, [id]);

  function switchKind(next: MinutesKind) {
    setKind(next);
    setEditing(false);
    setPreview(null);
  }

  // Models an admin allowed for regeneration. "" means the admin's default model.
  const [models, setModels] = useState<MinutesModel[]>([]);
  const [model, setModel] = useState("");
  useEffect(() => {
    api
      .listMinutesModels()
      .then((list) => {
        setModels(list);
        const preferred = list.find((m) => m.default) ?? list[0];
        if (preferred) setModel(preferred.id);
      })
      .catch(() => undefined);
  }, []);

  async function generate(target: MinutesKind, language?: string) {
    const ok = await run(
      () => api.regenerateMinutes(id, target, language, model || undefined),
      "Could not start generating minutes",
    );
    if (ok) {
      setTab("minutes");
      switchKind(target);
      setRunningKind(target);
    }
  }

  async function relabel(speakerLabel: string, userId: string) {
    await run(async () => {
      setMeeting(
        await api.relabel(id, { speaker_label: speakerLabel, user_id: userId || null, enroll: Boolean(userId) }),
      );
    }, "Could not update speaker");
  }

  async function saveEdits(content: Content) {
    const ok = await run(async () => {
      await api.updateMinutes(id, kind, content);
      await load();
      if (showHistory) await refreshHistory();
    }, "Could not save minutes");
    if (ok) setEditing(false);
  }

  async function restore(version: number) {
    const ok = await run(async () => {
      await api.restoreMinutesVersion(id, kind, version);
      await load();
      await refreshHistory();
    }, "Could not restore that version");
    if (ok) setPreview(null);
  }

  async function download(path: string, fallback: string) {
    setError("");
    try {
      await api.download(`/api/meetings/${id}/download/${path}`, fallback);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Download failed");
    }
  }

  // Recurring meetings.
  useEffect(() => {
    api.listSeries().then(setSeriesList).catch(() => undefined);
  }, []);

  useEffect(() => {
    if (!meeting || meeting.series_id) {
      setSuggestion(null);
      return;
    }
    api
      .seriesSuggestion(meeting.id)
      .then((s) => setSuggestion(s.suggest ? s : null))
      .catch(() => undefined);
  }, [meeting?.id, meeting?.series_id]);

  async function stopLive() {
    // Recording in this tab: stop it directly.
    if (recorder.meetingId === id) {
      recorder.stop();
      setTimeout(load, 3000);
      return;
    }
    // Recording in another tab: ask it to stop and upload.
    if (await requestRemoteStop(id)) {
      setTimeout(load, 4000);
      return;
    }
    // Nobody answered - the recording tab is gone. Save what the server has.
    const upTo = meeting?.live_transcribed_until ? " (about the length of the live transcript and a little more)" : "";
    if (
      !window.confirm(
        `The tab that was recording this meeting is closed or not responding. Save the audio the server received${upTo} and make the final transcript and minutes?`,
      )
    ) {
      return;
    }
    await run(async () => {
      await api.liveFinish(id);
      await load();
    }, "Could not save the recording");
  }

  async function deleteRecording() {
    if (!window.confirm("Delete this meeting's recording? The transcript and minutes are kept. This cannot be undone.")) return;
    await run(async () => {
      await api.deleteRecording(id);
      setAudioSrc(null);
      await load();
    }, "Could not delete the recording");
  }

  async function deleteMeeting() {
    if (!window.confirm("Delete this meeting, with its recording, transcript and minutes? This cannot be undone.")) return;
    const ok = await run(() => api.deleteMeeting(id), "Could not delete the meeting");
    if (ok) navigate("/meetings");
  }

  async function changeSeries(body: Parameters<typeof api.assignSeries>[1]) {
    const ok = await run(async () => {
      await api.assignSeries(id, body);
      await load();
      setSeriesList(await api.listSeries());
    }, "Could not update the series");
    if (ok) setSuggestion(null);
  }

  function newSeries() {
    const name = window.prompt("Name for this recurring meeting", meeting?.title ?? "");
    if (name?.trim()) changeSeries({ new_series_name: name.trim() });
  }

  if (!meeting) {
    return <p className="dim">{error || "Loading…"}</p>;
  }

  const names = new Map(meeting.participants.map((p) => [p.speaker_label, p]));
  const minutes = meeting.minutes.find((m) => m.kind === kind) ?? null;
  const hasKind = (k: MinutesKind) => meeting.minutes.some((m) => m.kind === k);
  const speakers = meeting.participants.slice().sort((a, b) => b.speaking_seconds - a.speaking_seconds);
  const hasTranscript = meeting.segments.length > 0;
  const generatingThis = runningKind === kind;

  return (
    <>
      <Link to="/meetings" className="crumb">
        <IconChevronLeft size={14} />
        Meetings
      </Link>

      <div className="meeting-head">
        <div className="meeting-title">
          <h1>{meeting.title}</h1>
          <div className="meta-line">
            <span className={`pill ${meeting.status}`}>{meeting.status}</span>
            <span>{new Date(meeting.started_at).toLocaleString()}</span>
            {meeting.duration_seconds ? <span>{formatDuration(meeting.duration_seconds)}</span> : null}
            {speakers.length > 0 && <span>{speakers.length} speakers</span>}
          </div>
        </div>
        <div className="btn-group btn-group-tight">
        <Menu
          label={meeting.series_name ?? "Add to series"}
          icon={<IconRepeat size={14} />}
          className={meeting.series_name ? "menu-series" : undefined}
        >
          <MenuGroup title="Recurring meeting">
            {seriesList.map((s) => (
              <MenuItem
                key={s.id}
                checked={meeting.series_id === s.id}
                onClick={() => changeSeries({ series_id: s.id })}
                note={`${s.meeting_count}`}
              >
                {s.name}
              </MenuItem>
            ))}
            <MenuItem onClick={newSeries}>New series…</MenuItem>
            {meeting.series_id && (
              <MenuItem onClick={() => changeSeries({ series_id: null })}>Remove from series</MenuItem>
            )}
          </MenuGroup>
        </Menu>
        <Menu label="More" icon={<IconRefresh size={14} />}>
          <MenuItem
            onClick={() => download("audio", "recording")}
            disabled={Boolean(meeting.audio_deleted_at)}
            note={meeting.audio_deleted_at ? "Deleted" : undefined}
          >
            Download recording
          </MenuItem>
          <MenuItem
            onClick={() => run(() => api.reprocess(id).then(load), "Could not reprocess")}
            disabled={busy || isActive || Boolean(meeting.audio_deleted_at)}
          >
            Reprocess recording
          </MenuItem>
          {isAdmin && (
            <MenuGroup title="Administrator">
              <MenuItem
                onClick={deleteRecording}
                disabled={busy || isActive || isLive || !meeting.has_recording}
                note={meeting.audio_deleted_at ? "Deleted" : undefined}
              >
                Delete recording
              </MenuItem>
              <MenuItem onClick={deleteMeeting} disabled={busy}>
                Delete meeting
              </MenuItem>
            </MenuGroup>
          )}
        </Menu>
        </div>
      </div>

      {suggestion && (
        <div className="alert alert-info compact-gap">
          <IconRepeat size={15} />
          <span className="grow">
            Looks like a recurring meeting: {suggestion.meetings?.length ?? 0} earlier “{suggestion.series_name}”
            meeting{suggestion.meetings?.length === 1 ? "" : "s"}. Group them to compare meetings and follow up on
            the last one's action items.
          </span>
          <button
            className="btn btn-sm btn-primary"
            disabled={busy}
            onClick={() =>
              changeSeries(
                suggestion.series_id
                  ? { series_id: suggestion.series_id }
                  : {
                      new_series_name: suggestion.series_name,
                      include_meeting_ids: suggestion.meetings?.map((m) => m.id) ?? [],
                    },
              )
            }
          >
            Group as series
          </button>
          <button className="icon-link" onClick={() => setSuggestion(null)} aria-label="Dismiss suggestion">
            <IconClose size={14} />
          </button>
        </div>
      )}

      {error && (
        <div className="alert alert-err compact-gap">
          <IconAlert size={15} />
          <span className="grow">{error}</span>
          <button className="icon-link" onClick={() => setError("")} aria-label="Dismiss">
            <IconClose size={14} />
          </button>
        </div>
      )}

      {meeting.error && !isActive && meeting.status !== "failed" && (
        <div className="alert alert-warn compact-gap">
          <IconAlert size={15} />
          <span className="grow">{meeting.error}</span>
        </div>
      )}

      {meeting.error && !isActive && meeting.status === "failed" && (
        <div className="alert alert-err compact-gap">
          <IconAlert size={15} />
          <span className="grow mono small">{meeting.error}</span>
          {!meeting.audio_deleted_at && (
            <button
              className="btn btn-sm btn-primary"
              onClick={() => run(() => api.reprocess(id).then(load), "Could not retry")}
              disabled={busy}
            >
              Try again
            </button>
          )}
        </div>
      )}

      {isLive && (
        <div className="alert alert-info compact-gap live-banner">
          <span className="live-dot" aria-hidden="true" />
          <span className="grow">
            <strong>
              Recording{recorder.meetingId === id ? ` ${formatElapsed(recorder.seconds)}` : " in progress"}.
            </strong>{" "}
            The transcript is a live preview
            {meeting.live_transcribed_until
              ? `, up to ${formatTimestamp(meeting.live_transcribed_until * 1000)}`
              : ""}
            . The final transcript and minutes are made when recording stops.
          </span>
          <button className="btn btn-sm btn-rec" onClick={stopLive} disabled={busy}>
            <IconStop size={14} />
            Stop recording
          </button>
        </div>
      )}

      {isActive && (
        <ProcessingCard
          progress={progress}
          busy={busy}
          onRestart={() => run(() => api.reprocess(id).then(load), "Could not restart processing")}
        />
      )}

      {(audioSrc || speakers.length > 0) && (
        <div className="media-strip">
          {meeting.audio_deleted_at ? (
            <span className="small dim">
              Recording deleted {new Date(meeting.audio_deleted_at).toLocaleDateString()}
            </span>
          ) : (
            audioSrc &&
            (audioFailed ? (
              <span className="audio-failed small">
                This browser cannot play this recording.{" "}
                <button className="link-btn" onClick={() => download("audio", "recording")}>
                  Download it
                </button>{" "}
                to listen.
              </span>
            ) : (
              // eslint-disable-next-line jsx-a11y/media-has-caption
              <audio controls src={audioSrc} className="audio-inline" onError={() => setAudioFailed(true)} />
            ))
          )}
          {speakers.length > 0 && !canRelabel && (
            <div className="chips">
              {speakers.map((p) => (
                <span key={p.speaker_label} className="chip" title={p.user_id ? `Match ${p.confidence.toFixed(2)}` : "Not identified"}>
                  <strong>{p.display_name}</strong>
                  <span className="dim">{formatDuration(p.speaking_seconds)}</span>
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      {canRelabel && speakers.length > 0 && (
        <div className="card">
          <div className="table-wrap">
            <table className="table-compact">
              <tbody>
                {speakers.map((p) => (
                  <tr key={p.speaker_label}>
                    <td data-label="Speaker">
                      <strong>{p.display_name}</strong>{" "}
                      <span className="dim small">{formatDuration(p.speaking_seconds)}</span>
                    </td>
                    <td data-label="Assign" style={{ width: 240 }}>
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
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="tabbar">
        <div className="segmented" role="tablist">
          <button role="tab" aria-selected={tab === "minutes"} onClick={() => setTab("minutes")}>
            Minutes
          </button>
          <button role="tab" aria-selected={tab === "transcript"} onClick={() => setTab("transcript")}>
            Transcript <span className="count">{meeting.segments.length}</span>
          </button>
          {meeting.series_id && (
            <button role="tab" aria-selected={tab === "series"} onClick={() => setTab("series")}>
              Series{" "}
              <span className="count">
                {seriesList.find((s) => s.id === meeting.series_id)?.meeting_count ?? ""}
              </span>
            </button>
          )}
        </div>

        {tab === "transcript" && hasTranscript && (
          <Menu label="Download" icon={<IconDownload size={14} />}>
            <MenuItem onClick={() => download("transcript?fmt=docx", "transcript.docx")}>Word (.docx)</MenuItem>
            <MenuItem onClick={() => download("transcript?fmt=txt", "transcript.txt")}>Text (.txt)</MenuItem>
            <MenuItem onClick={() => download("transcript?fmt=srt", "transcript.srt")}>Subtitles (.srt)</MenuItem>
          </Menu>
        )}

        {tab === "minutes" && (
          <div className="segmented segmented-sm" role="tablist" aria-label="Minutes version">
            {(["short", "detailed"] as MinutesKind[]).map((k) => (
              <button key={k} role="tab" aria-selected={kind === k} onClick={() => switchKind(k)}>
                {k === "short" ? "Short" : "Detailed"}
                {!hasKind(k) && runningKind !== k && <span className="dot-empty" aria-label="not generated" />}
              </button>
            ))}
          </div>
        )}
      </div>

      {tab === "transcript" && (
        <div className="card">
          {hasTranscript ? (
            <>
              <div className="seg seg-head" aria-hidden="true">
                <span>Time</span>
                <span>Speaker</span>
                <span>Language</span>
                <span>Text</span>
              </div>
              {meeting.segments.map((s) => {
                const participant = names.get(s.speaker_label);
                const mixed = s.scripts?.includes("+");
                return (
                  <div key={s.idx} className={`seg ${participant?.user_id ? "" : "unknown"}`}>
                    <span className="ts">{formatTimestamp(s.start_ms)}</span>
                    <span className="who">{participant?.display_name ?? s.speaker_label}</span>
                    <span className={`lang ${mixed ? "mixed" : ""}`}>
                      {mixed ? "mixed" : s.language || "—"}
                    </span>
                    <span className="said">{s.text}</span>
                  </div>
                );
              })}
            </>
          ) : (
            <div className="empty">
              <p className="big">{meeting.transcript_deleted_at ? "Transcript deleted" : "No transcript yet"}</p>
              <p className="small">
                {meeting.transcript_deleted_at
                  ? `Removed ${new Date(meeting.transcript_deleted_at).toLocaleDateString()} under the retention policy.`
                  : "It appears here once processing finishes."}
              </p>
            </div>
          )}
        </div>
      )}

      {tab === "series" && meeting.series_id && (
        <SeriesPanel
          seriesId={meeting.series_id}
          currentId={meeting.id}
          canAddFollowUps={Boolean(minutes) && !(minutes?.follow_ups?.length) && runningKind === null}
          onRegenerate={() => generate(kind)}
        />
      )}

      {tab === "minutes" && generatingThis && (
        <MinutesProgressCard progress={minutesProgress} regenerating={Boolean(minutes)} />
      )}

      {tab === "minutes" && minutes && (
        <div className={`minutes-layout ${showHistory && !editing ? "with-history" : ""}`}>
          <div className="minutes-main">
            {/* overflow-visible: the header dropdowns and the sticky Save bar must not be clipped. */}
            <div className="card card-overflow">
              <div className="card-head card-head-tight">
                <div className="row" style={{ gap: 6 }}>
                  <span className="badge">v{minutes.version}</span>
                  {minutes.model && <span className="badge mono" title="Model that wrote these minutes">{minutes.model}</span>}
                  {minutes.source !== "generated" && (
                    <span className="badge badge-accent">{SOURCE_LABEL[minutes.source] ?? minutes.source}</span>
                  )}
                  <span className="dim tiny">
                    {new Date(minutes.edited_at ?? minutes.created_at).toLocaleString()}
                  </span>
                </div>
                {!editing && (
                  <div className="btn-group btn-group-tight">
                    <button
                      className="btn btn-sm btn-ghost"
                      onClick={() => {
                        setPreview(null);
                        setEditing(true);
                      }}
                      disabled={busy || runningKind !== null}
                    >
                      <IconEdit size={14} />
                      Edit
                    </button>
                    <button
                      className={`btn btn-sm btn-ghost ${showHistory ? "btn-active" : ""}`}
                      onClick={() => {
                        setPreview(null);
                        setShowHistory((v) => !v);
                      }}
                      aria-pressed={showHistory}
                    >
                      <IconHistory size={14} />
                      History
                    </button>
                    <Menu label={generatingThis ? "Generating…" : "Regenerate"} icon={<IconSparkle size={14} />}>
                      {models.length > 0 && (
                        <MenuGroup title="Model">
                          {models.map((m) => (
                            <MenuItem
                              key={m.id}
                              keepOpen
                              checked={model === m.id}
                              onClick={() => setModel(m.id)}
                              note={m.default ? "default" : undefined}
                            >
                              {m.label}
                            </MenuItem>
                          ))}
                        </MenuGroup>
                      )}
                      <MenuGroup title="Regenerate in">
                        {LANGUAGES.map(([code, name]) => (
                          <MenuItem
                            key={code}
                            onClick={() => generate(kind, code)}
                            disabled={busy || runningKind !== null || !hasTranscript}
                          >
                            {name}
                          </MenuItem>
                        ))}
                      </MenuGroup>
                    </Menu>
                    <Menu label="Download" icon={<IconDownload size={14} />}>
                      <MenuItem onClick={() => download(`minutes?kind=${kind}&fmt=docx`, `minutes-${kind}.docx`)}>
                        Word (.docx)
                      </MenuItem>
                      <MenuItem onClick={() => download(`minutes?kind=${kind}&fmt=md`, `minutes-${kind}.md`)}>
                        Markdown (.md)
                      </MenuItem>
                    </Menu>
                  </div>
                )}
              </div>

              {editing ? (
                <MinutesEditor initial={minutes} busy={busy} onCancel={() => setEditing(false)} onSave={saveEdits} />
              ) : (
                <>
                  {preview && (
                    <div className="preview-banner">
                      <span className="grow">
                        Viewing <strong>v{preview.version}</strong> · {SOURCE_LABEL[preview.source] ?? preview.source}
                        {preview.created_by_name ? ` by ${preview.created_by_name}` : ""} ·{" "}
                        {new Date(preview.created_at).toLocaleString()}
                      </span>
                      {preview.version !== minutes.version && (
                        <button className="btn btn-sm btn-primary" onClick={() => restore(preview.version)} disabled={busy}>
                          Restore
                        </button>
                      )}
                      <button className="btn btn-sm" onClick={() => setPreview(null)}>
                        Back to current
                      </button>
                    </div>
                  )}
                  <MinutesView content={preview ?? minutes} />
                </>
              )}
            </div>
            {kind === "short" && !editing && !hasKind("detailed") && runningKind !== "detailed" && (
              <p className="small dim upsell">
                Need the full record with every topic and rationale?{" "}
                <button className="link-btn" onClick={() => generate("detailed")} disabled={busy || !hasTranscript}>
                  Generate detailed minutes
                </button>
              </p>
            )}
          </div>

          {showHistory && !editing && (
            <aside className="card history-panel">
              <div className="card-head card-head-tight">
                <h3>History</h3>
                <button className="icon-link" onClick={() => setShowHistory(false)} aria-label="Close history">
                  <IconClose size={14} />
                </button>
              </div>
              <ol className="timeline">
                {history.length === 0 && <li className="dim small">No history yet.</li>}
                {history.map((v) => {
                  const isCurrent = v.version === minutes.version;
                  const isViewing = preview ? preview.version === v.version : isCurrent;
                  return (
                    <li key={v.version}>
                      <button
                        className="timeline-item"
                        aria-current={isViewing ? "true" : undefined}
                        onClick={() => setPreview(isCurrent ? null : v)}
                      >
                        <span className="timeline-head">
                          <strong>v{v.version}</strong>
                          {isCurrent && <span className="badge badge-ok">Current</span>}
                        </span>
                        <span className="timeline-meta">
                          {SOURCE_LABEL[v.source] ?? v.source}
                          {v.created_by_name ? ` by ${v.created_by_name}` : ""} ·{" "}
                          {new Date(v.created_at).toLocaleString()}
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ol>
            </aside>
          )}
        </div>
      )}

      {tab === "minutes" && !minutes && !generatingThis && (
        <div className="card">
          <div className="empty empty-compact">
            {isLive ? (
              <p className="small">Minutes are written automatically when the recording stops.</p>
            ) : meeting.minutes_deleted_at ? (
              <p>Minutes deleted {new Date(meeting.minutes_deleted_at).toLocaleDateString()} under the retention policy.</p>
            ) : !hasTranscript ? (
              <p>Minutes can be created once the transcript is ready.</p>
            ) : kind === "detailed" ? (
              <>
                <p className="big">Detailed minutes</p>
                <p className="small">Every topic, decision rationale and action item. Takes a minute or two.</p>
                <button className="btn btn-sm btn-primary" onClick={() => generate("detailed")} disabled={busy || runningKind !== null}>
                  <IconSparkle size={14} />
                  Generate detailed minutes
                </button>
              </>
            ) : (
              <>
                <p className="big">No short minutes yet</p>
                <button className="btn btn-sm btn-primary" onClick={() => generate("short")} disabled={busy || runningKind !== null}>
                  <IconSparkle size={14} />
                  Generate short minutes
                </button>
              </>
            )}
          </div>
        </div>
      )}
    </>
  );
}

// --------------------------------------------------------------------------
// Live processing progress
// --------------------------------------------------------------------------

const STAGES: { id: string; label: string; matches: string[] }[] = [
  { id: "prepare", label: "Prepare audio", matches: ["queued", "prepare"] },
  { id: "transcribe", label: "Transcribe", matches: ["transcribe", "waiting"] },
  { id: "speakers", label: "Speakers", matches: ["speakers"] },
  { id: "minutes", label: "Minutes", matches: ["minutes"] },
];

// Matches STALE_PROCESSING_SECONDS on the server, which allows the restart.
const STALE_SECONDS = 15 * 60;

function ProcessingCard({
  progress,
  busy,
  onRestart,
}: {
  progress: Progress | null;
  busy: boolean;
  onRestart: () => void;
}) {
  const stage = progress?.stage ?? "queued";
  const percent = Math.max(0, Math.min(100, progress?.percent ?? 0));
  const current = Math.max(0, STAGES.findIndex((s) => s.matches.includes(stage)));
  const waiting = stage === "waiting";

  // Re-render every 30s so "stuck" is noticed without a new event arriving.
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const tick = setInterval(() => setNow(Date.now() / 1000), 30000);
    return () => clearInterval(tick);
  }, []);
  // An update without a timestamp predates this check - treat it as stale too.
  const stuck = progress !== null && (!progress.ts || now - progress.ts > STALE_SECONDS);

  if (stuck) {
    return (
      <div className="card">
        <div className="card-head">
          <h3>Processing stopped</h3>
          <button className="btn btn-sm btn-primary" onClick={onRestart} disabled={busy}>
            <IconRefresh size={14} />
            Restart processing
          </button>
        </div>
        <div className="card-body">
          <p className="small">
            No progress for over 15 minutes — the worker was probably restarted mid-run. Last step:{" "}
            <span className="dim">{progress?.message || stage}</span>
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className={`card processing-card ${waiting ? "is-waiting" : ""}`} aria-live="polite">
      <div className="card-body">
        <ol className="stepper">
          {STAGES.map((s, i) => (
            <li key={s.id} data-state={i < current ? "done" : i === current ? "active" : "todo"}>
              <span className="step-dot">{i < current ? <IconCheck size={12} /> : i + 1}</span>
              <span className="step-label">{s.label}</span>
            </li>
          ))}
        </ol>

        <div className="spread" style={{ margin: "16px 0 8px" }}>
          <strong className="small">{progress?.message || "Queued — waiting for a worker"}</strong>
          <span className="dim small mono">{percent}%</span>
        </div>
        <div className={`bar ${waiting ? "bar-warn" : "bar-live"}`}>
          <i style={{ width: `${Math.max(percent, 2)}%` }} />
        </div>
        <p className="tiny dim" style={{ marginTop: 10 }}>
          This page updates live. You can leave it — processing continues in the background.
        </p>
      </div>
    </div>
  );
}

const MINUTES_STEPS: { label: string; from: number }[] = [
  { label: "Read transcript", from: 0 },
  { label: "Analyse", from: 15 },
  { label: "Summary & topics", from: 32 },
  { label: "Decisions & actions", from: 65 },
  { label: "Finish", from: 90 },
];

function MinutesProgressCard({
  progress,
  regenerating,
}: {
  progress: Progress | null;
  regenerating: boolean;
}) {
  const percent = Math.max(0, Math.min(100, progress?.percent ?? 1));
  let current = 0;
  MINUTES_STEPS.forEach((step, i) => {
    if (percent >= step.from) current = i;
  });

  return (
    <div className="card processing-card" aria-live="polite">
      <div className="card-head">
        <h3>{regenerating ? "Writing new minutes" : "Writing minutes"}</h3>
        <span className="dim tiny">
          {regenerating ? "The current version stays in the history" : "You can leave this page"}
        </span>
      </div>
      <div className="card-body">
        <ol className="stepper">
          {MINUTES_STEPS.map((s, i) => (
            <li key={s.label} data-state={i < current ? "done" : i === current ? "active" : "todo"}>
              <span className="step-dot">{i < current ? <IconCheck size={12} /> : i + 1}</span>
              <span className="step-label">{s.label}</span>
            </li>
          ))}
        </ol>
        <div className="spread" style={{ margin: "16px 0 8px" }}>
          <strong className="small">{progress?.message || "Starting…"}</strong>
          <span className="dim small mono">{percent}%</span>
        </div>
        <div className="bar bar-live">
          <i style={{ width: `${Math.max(percent, 2)}%` }} />
        </div>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// Read-only minutes
// --------------------------------------------------------------------------

// Numbers with their units ("7 days", "30%", "₹5,000", "2 weeks") and acronyms
// ("BOQ", "SAP", "TAT") are what a reader scans minutes for, so they are marked.
const EMPHASIS =
  /((?:₹|\$|Rs\.?\s?)?\d[\d,.]*\s?(?:%|k|K|lakh|crore|days?|weeks?|months?|years?|hours?|hrs?|mins?|minutes?)?|\b[A-Z][A-Z0-9]{1,}(?:\/[A-Z0-9]+)*\b)/g;

function Emphasize({ text }: { text: string }) {
  const parts = text.split(EMPHASIS);
  return (
    <>
      {parts.map((part, i) => {
        if (i % 2 === 0 || !part) return part;
        return /\d/.test(part) ? (
          <mark key={i} className="hl-num">
            {part}
          </mark>
        ) : (
          <strong key={i} className="hl-term">
            {part}
          </strong>
        );
      })}
    </>
  );
}

function SectionHead({ tone, icon, title, count }: { tone: string; icon: JSX.Element; title: string; count?: number }) {
  return (
    <h4 className={`md-head tone-${tone}`}>
      <span className="md-icon">{icon}</span>
      {title}
      {count !== undefined && <span className="md-count">{count}</span>}
    </h4>
  );
}

function initials(name: string) {
  const words = name.replace(/[^\p{L}\p{N} ]/gu, "").split(/\s+/).filter(Boolean);
  return ((words[0]?.[0] ?? "?") + (words[1]?.[0] ?? "")).toUpperCase();
}

function FollowUpsSection({ items }: { items: FollowUp[] }) {
  const done = items.filter((f) => f.status === "done").length;
  return (
    <section>
      <SectionHead
        tone="plum"
        icon={<IconRepeat size={13} />}
        title="Follow-ups from last meeting"
        count={items.length}
      />
      <p className="md-progress-line">
        <span className="md-progress">
          <i style={{ width: `${items.length ? (done / items.length) * 100 : 0}%` }} />
        </span>
        <span className="small dim">
          {done} of {items.length} done
        </span>
      </p>
      <ul className="md-followups">
        {items.map((f, i) => {
          const status = FOLLOW_UP_STATUS[f.status] ?? FOLLOW_UP_STATUS.not_discussed;
          return (
            <li key={i}>
              <span className={`fu-status tone-${status.tone}`}>{status.label}</span>
              <div className="grow">
                <p className="md-strong">
                  <Emphasize text={f.item} />
                  {f.owner && <span className="md-chip fu-owner">{f.owner}</span>}
                </p>
                {f.note && <p className="md-note">{f.note}</p>}
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function MinutesView({ content }: { content: Content }) {
  const stats: [string, number, string][] = [
    ["Decisions", content.decisions.length, "ok"],
    ["Action items", content.action_items.length, "accent"],
    ["Open questions", content.open_questions.length, "warn"],
    ["Topics", content.topics.length, "plum"],
  ];

  return (
    <div className="minutes-doc">
      <div className="md-stats" aria-label="At a glance">
        {stats
          .filter(([label, n]) => n > 0 || label !== "Topics")
          .map(([label, n, tone]) => (
            <span key={label} className={`md-stat tone-${tone} ${n === 0 ? "zero" : ""}`}>
              <b>{n}</b> {label}
            </span>
          ))}
      </div>

      <section>
        <SectionHead tone="accent" icon={<IconSparkle size={13} />} title="Summary" />
        <div className="md-summary">
          <Emphasize text={content.summary} />
        </div>
      </section>

      {(content.key_points?.length ?? 0) > 0 && (
        <section>
          <SectionHead tone="warn" icon={<IconStar size={13} />} title="Key highlights" />
          <ol className="md-highlights">
            {content.key_points.map((point, i) => (
              <li key={i}>
                <span className="md-n">{i + 1})</span>
                <span>
                  <Emphasize text={point} />
                </span>
              </li>
            ))}
          </ol>
        </section>
      )}

      {(content.follow_ups?.length ?? 0) > 0 && <FollowUpsSection items={content.follow_ups} />}

      {content.decisions.length > 0 && (
        <section>
          <SectionHead tone="ok" icon={<IconCheck size={13} />} title="Decisions" count={content.decisions.length} />
          <ol className="md-list">
            {content.decisions.map((d, i) => (
              <li key={i} className="md-decision">
                <span className="md-n">{i + 1})</span>
                <div className="grow">
                  <p className="md-strong">
                    <Emphasize text={d.decision} />
                  </p>
                  {d.rationale && (
                    <p className="md-note">
                      <span className="md-label">Why</span> <Emphasize text={d.rationale} />
                    </p>
                  )}
                  {d.decided_by && <span className="md-chip">Decided by {d.decided_by}</span>}
                </div>
              </li>
            ))}
          </ol>
        </section>
      )}

      {content.action_items.length > 0 && (
        <section>
          <SectionHead
            tone="accent"
            icon={<IconClock size={13} />}
            title="Action items"
            count={content.action_items.length}
          />
          <ul className="md-actions">
            {content.action_items.map((a, i) => (
              <li key={i} className={`md-action pri-${a.priority || "medium"}`}>
                <span className="md-n">{i + 1})</span>
                <div className="grow">
                  <p className="md-strong">
                    <Emphasize text={a.task} />
                  </p>
                  <div className="md-meta">
                    <span className={`md-owner ${a.owner === "Unassigned" ? "unassigned" : ""}`}>
                      <span className="md-avatar">{a.owner === "Unassigned" ? "?" : initials(a.owner)}</span>
                      {a.owner}
                    </span>
                    {a.due && (
                      <span className="md-chip due">
                        <IconClock size={11} /> Due {a.due}
                      </span>
                    )}
                    <span className={`md-priority ${a.priority || "medium"}`}>{a.priority || "medium"} priority</span>
                  </div>
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}

      {content.topics.length > 0 && (
        <section>
          <SectionHead
            tone="plum"
            icon={<IconMeetings size={13} />}
            title="Topics discussed"
            count={content.topics.length}
          />
          <ol className="md-list md-topics">
            {content.topics.map((t, i) => (
              <li key={i}>
                <span className="md-n">{i + 1})</span>
                <div className="grow">
                  <p className="md-strong">{t.title}</p>
                  <p className="md-note">
                    <Emphasize text={t.discussion} />
                  </p>
                  {t.speakers.length > 0 && (
                    <div className="md-meta">
                      {t.speakers.map((s) => (
                        <span key={s} className="md-chip">
                          {s}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              </li>
            ))}
          </ol>
        </section>
      )}

      {content.open_questions.length > 0 && (
        <section>
          <SectionHead
            tone="warn"
            icon={<IconAlert size={13} />}
            title="Open questions"
            count={content.open_questions.length}
          />
          <ul className="md-list">
            {content.open_questions.map((q, i) => (
              <li key={i} className="md-question">
                <span className="md-n">{i + 1})</span>
                <p className="grow">
                  <Emphasize text={q} />
                </p>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------
// Editor
// --------------------------------------------------------------------------

function MinutesEditor({
  initial,
  busy,
  onSave,
  onCancel,
}: {
  initial: Content;
  busy: boolean;
  onSave: (content: Content) => void;
  onCancel: () => void;
}) {
  const [draft, setDraft] = useState<Content>(() => ({
    summary: initial.summary,
    key_points: [...(initial.key_points ?? [])],
    follow_ups: (initial.follow_ups ?? []).map((f) => ({ ...f })),
    decisions: initial.decisions.map((d) => ({ ...d })),
    action_items: initial.action_items.map((a) => ({ ...a })),
    topics: initial.topics.map((t) => ({ ...t })),
    open_questions: [...initial.open_questions],
  }));

  function update<K extends keyof Content>(key: K, value: Content[K]) {
    setDraft((prev) => ({ ...prev, [key]: value }));
  }

  function patchAt<K extends "decisions" | "action_items" | "topics" | "follow_ups">(
    key: K,
    index: number,
    patch: Partial<Content[K][number]>,
  ) {
    setDraft((prev) => ({
      ...prev,
      [key]: (prev[key] as Content[K][number][]).map((item, i) => (i === index ? { ...item, ...patch } : item)),
    }));
  }

  function removeAt<K extends "decisions" | "action_items" | "topics" | "open_questions" | "key_points" | "follow_ups">(
    key: K,
    index: number,
  ) {
    setDraft((prev) => ({ ...prev, [key]: (prev[key] as unknown[]).filter((_, i) => i !== index) }));
  }

  function submit() {
    // Drop rows left empty rather than saving blank entries.
    onSave({
      summary: draft.summary.trim(),
      key_points: draft.key_points.map((p) => p.trim()).filter(Boolean),
      follow_ups: draft.follow_ups
        .filter((f) => f.item.trim())
        .map((f) => ({ ...f, note: f.note?.trim() || null })),
      decisions: draft.decisions
        .filter((d) => d.decision.trim())
        .map((d) => ({ ...d, decided_by: d.decided_by?.trim() || null, rationale: d.rationale?.trim() || null })),
      action_items: draft.action_items
        .filter((a) => a.task.trim())
        .map((a) => ({ ...a, owner: a.owner.trim() || "Unassigned", due: a.due?.trim() || null })),
      topics: draft.topics.filter((t) => t.title.trim() || t.discussion.trim()),
      open_questions: draft.open_questions.map((q) => q.trim()).filter(Boolean),
    });
  }

  return (
    <>
      <div className="card-body editor">
        <section className="editor-section">
          <label className="field">
            <span>Summary</span>
            <textarea rows={5} value={draft.summary} onChange={(e) => update("summary", e.target.value)} />
          </label>
        </section>

        <section className="editor-section">
          <div className="editor-section-head">
            <h4>Key highlights</h4>
            <button className="btn btn-sm btn-ghost" onClick={() => update("key_points", [...draft.key_points, ""])}>
              <IconPlus size={14} /> Add highlight
            </button>
          </div>
          {draft.key_points.length === 0 && <p className="dim small">No highlights.</p>}
          {draft.key_points.map((point, i) => (
            <div key={i} className="editor-item">
              <input
                type="text"
                placeholder="Highlight"
                value={point}
                onChange={(e) =>
                  update(
                    "key_points",
                    draft.key_points.map((x, j) => (j === i ? e.target.value : x)),
                  )
                }
                aria-label="Highlight"
              />
              <RemoveButton onClick={() => removeAt("key_points", i)} label="Remove highlight" />
            </div>
          ))}
        </section>

        {draft.follow_ups.length > 0 && (
          <section className="editor-section">
            <div className="editor-section-head">
              <h4>Follow-ups from last meeting</h4>
            </div>
            {draft.follow_ups.map((f, i) => (
              <div key={i} className="editor-item">
                <div className="editor-grid cols-decision">
                  <input
                    type="text"
                    value={f.item}
                    onChange={(e) => patchAt("follow_ups", i, { item: e.target.value })}
                    aria-label="Previous item"
                  />
                  <select
                    value={f.status}
                    onChange={(e) => patchAt("follow_ups", i, { status: e.target.value as FollowUpStatus })}
                    aria-label="Status"
                  >
                    {Object.entries(FOLLOW_UP_STATUS).map(([value, s]) => (
                      <option key={value} value={value}>
                        {s.label}
                      </option>
                    ))}
                  </select>
                  <input
                    type="text"
                    className="span-all"
                    placeholder="Note (optional)"
                    value={f.note ?? ""}
                    onChange={(e) => patchAt("follow_ups", i, { note: e.target.value })}
                    aria-label="Note"
                  />
                </div>
                <RemoveButton onClick={() => removeAt("follow_ups", i)} label="Remove follow-up" />
              </div>
            ))}
          </section>
        )}

        <section className="editor-section">
          <div className="editor-section-head">
            <h4>Decisions</h4>
            <button
              className="btn btn-sm btn-ghost"
              onClick={() => update("decisions", [...draft.decisions, { decision: "", decided_by: "", rationale: "" }])}
            >
              <IconPlus size={14} /> Add decision
            </button>
          </div>
          {draft.decisions.length === 0 && <p className="dim small">No decisions recorded.</p>}
          {draft.decisions.map((d, i) => (
            <div key={i} className="editor-item">
              <div className="editor-grid cols-decision">
                <input
                  type="text"
                  placeholder="Decision"
                  value={d.decision}
                  onChange={(e) => patchAt("decisions", i, { decision: e.target.value })}
                  aria-label="Decision"
                />
                <input
                  type="text"
                  placeholder="Decided by"
                  value={d.decided_by ?? ""}
                  onChange={(e) => patchAt("decisions", i, { decided_by: e.target.value })}
                  aria-label="Decided by"
                />
                <input
                  type="text"
                  className="span-all"
                  placeholder="Rationale (optional)"
                  value={d.rationale ?? ""}
                  onChange={(e) => patchAt("decisions", i, { rationale: e.target.value })}
                  aria-label="Rationale"
                />
              </div>
              <RemoveButton onClick={() => removeAt("decisions", i)} label="Remove decision" />
            </div>
          ))}
        </section>

        <section className="editor-section">
          <div className="editor-section-head">
            <h4>Action items</h4>
            <button
              className="btn btn-sm btn-ghost"
              onClick={() =>
                update("action_items", [...draft.action_items, { task: "", owner: "", due: "", priority: "medium" }])
              }
            >
              <IconPlus size={14} /> Add action item
            </button>
          </div>
          {draft.action_items.length === 0 && <p className="dim small">No action items recorded.</p>}
          {draft.action_items.map((a, i) => (
            <div key={i} className="editor-item">
              <div className="editor-grid cols-action">
                <input
                  type="text"
                  className="span-all"
                  placeholder="Task"
                  value={a.task}
                  onChange={(e) => patchAt("action_items", i, { task: e.target.value })}
                  aria-label="Task"
                />
                <input
                  type="text"
                  placeholder="Owner"
                  value={a.owner}
                  onChange={(e) => patchAt("action_items", i, { owner: e.target.value })}
                  aria-label="Owner"
                />
                <input
                  type="text"
                  placeholder="Due (optional)"
                  value={a.due ?? ""}
                  onChange={(e) => patchAt("action_items", i, { due: e.target.value })}
                  aria-label="Due"
                />
                <select
                  value={a.priority || "medium"}
                  onChange={(e) => patchAt("action_items", i, { priority: e.target.value })}
                  aria-label="Priority"
                >
                  <option value="high">High priority</option>
                  <option value="medium">Medium priority</option>
                  <option value="low">Low priority</option>
                </select>
              </div>
              <RemoveButton onClick={() => removeAt("action_items", i)} label="Remove action item" />
            </div>
          ))}
        </section>

        <section className="editor-section">
          <div className="editor-section-head">
            <h4>Topics discussed</h4>
            <button
              className="btn btn-sm btn-ghost"
              onClick={() => update("topics", [...draft.topics, { title: "", discussion: "", speakers: [] }])}
            >
              <IconPlus size={14} /> Add topic
            </button>
          </div>
          {draft.topics.length === 0 && <p className="dim small">No topics recorded.</p>}
          {draft.topics.map((t, i) => (
            <div key={i} className="editor-item">
              <div className="editor-grid">
                <input
                  type="text"
                  placeholder="Topic"
                  value={t.title}
                  onChange={(e) => patchAt("topics", i, { title: e.target.value })}
                  aria-label="Topic title"
                />
                <textarea
                  rows={3}
                  placeholder="What was discussed"
                  value={t.discussion}
                  onChange={(e) => patchAt("topics", i, { discussion: e.target.value })}
                  aria-label="Discussion"
                />
              </div>
              <RemoveButton onClick={() => removeAt("topics", i)} label="Remove topic" />
            </div>
          ))}
        </section>

        <section className="editor-section">
          <div className="editor-section-head">
            <h4>Open questions</h4>
            <button
              className="btn btn-sm btn-ghost"
              onClick={() => update("open_questions", [...draft.open_questions, ""])}
            >
              <IconPlus size={14} /> Add question
            </button>
          </div>
          {draft.open_questions.length === 0 && <p className="dim small">No open questions.</p>}
          {draft.open_questions.map((q, i) => (
            <div key={i} className="editor-item">
              <input
                type="text"
                placeholder="Question"
                value={q}
                onChange={(e) =>
                  update(
                    "open_questions",
                    draft.open_questions.map((x, j) => (j === i ? e.target.value : x)),
                  )
                }
                aria-label="Open question"
              />
              <RemoveButton onClick={() => removeAt("open_questions", i)} label="Remove question" />
            </div>
          ))}
        </section>
      </div>

      <div className="card-actions sticky-actions">
        <span className="dim small grow">Saving creates a new version. Earlier versions stay in the history.</span>
        <button className="btn btn-sm" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
        <button className="btn btn-sm btn-primary" onClick={submit} disabled={busy || !draft.summary.trim()}>
          <IconCheck size={14} />
          {busy ? "Saving…" : "Save changes"}
        </button>
      </div>
    </>
  );
}

function RemoveButton({ onClick, label }: { onClick: () => void; label: string }) {
  return (
    <button className="btn btn-sm btn-ghost btn-icon remove-btn" onClick={onClick} aria-label={label} title={label}>
      <IconTrash size={15} />
    </button>
  );
}

// --------------------------------------------------------------------------
// Recurring meeting series
// --------------------------------------------------------------------------

const CLOSED = new Set<FollowUpStatus>(["done", "dropped"]);

function normaliseItem(text: string) {
  return text.toLowerCase().replace(/[^\p{L}\p{N} ]/gu, "").replace(/\s+/g, " ").trim();
}

/** Action items still open across the series: raised in one meeting and not
 *  reported done (or dropped) in the follow-ups of any later meeting. */
function openActionItems(meetings: SeriesMeeting[]) {
  const open = new Map<string, { task: string; owner: string; due?: string | null; meeting: SeriesMeeting }>();
  const oldestFirst = meetings.slice().reverse();
  for (const m of oldestFirst) {
    for (const f of m.minutes?.follow_ups ?? []) {
      if (CLOSED.has(f.status)) open.delete(normaliseItem(f.item));
    }
    for (const a of m.minutes?.action_items ?? []) {
      open.set(normaliseItem(a.task), { task: a.task, owner: a.owner, due: a.due, meeting: m });
    }
  }
  return [...open.values()];
}

function SeriesPanel({
  seriesId,
  currentId,
  canAddFollowUps,
  onRegenerate,
}: {
  seriesId: string;
  currentId: string;
  canAddFollowUps: boolean;
  onRegenerate: () => void;
}) {
  const [data, setData] = useState<{ series: Series; meetings: SeriesMeeting[] } | null>(null);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);

  useEffect(() => {
    setData(null);
    api
      .seriesMeetings(seriesId)
      .then(setData)
      .catch((err) => setError(err instanceof Error ? err.message : "Could not load the series"));
  }, [seriesId]);

  if (error) return <div className="alert alert-err compact-gap">{error}</div>;
  if (!data) return <p className="dim small">Loading series…</p>;

  const open = openActionItems(data.meetings);
  const currentIndex = data.meetings.findIndex((m) => m.id === currentId);
  const hasEarlierWithMinutes = data.meetings.slice(currentIndex + 1).some((m) => m.minutes);

  return (
    <div className="series-panel">
      {canAddFollowUps && hasEarlierWithMinutes && (
        <div className="alert alert-info compact-gap">
          <IconRepeat size={15} />
          <span className="grow">
            These minutes were written before this meeting joined the series. Regenerate them to add follow-ups on
            the previous meeting's action items.
          </span>
          <button className="btn btn-sm btn-primary" onClick={onRegenerate}>
            Regenerate with follow-ups
          </button>
        </div>
      )}

      <div className="card">
        <div className="card-head card-head-tight">
          <SectionHead tone="accent" icon={<IconClock size={13} />} title="Open action items across the series" count={open.length} />
        </div>
        {open.length === 0 ? (
          <p className="small dim series-empty">Nothing outstanding — every earlier action item was reported done.</p>
        ) : (
          <ul className="series-open">
            {open.map((a, i) => (
              <li key={i}>
                <span className="md-box" aria-hidden="true" />
                <span className="grow">
                  <strong>{a.task}</strong>
                  <span className="dim small">
                    {" "}
                    · {a.owner}
                    {a.due ? ` · due ${a.due}` : ""} · from{" "}
                    {a.meeting.id === currentId ? (
                      "this meeting"
                    ) : (
                      <Link to={`/meetings/${a.meeting.id}`}>
                        {new Date(a.meeting.started_at).toLocaleDateString(undefined, { day: "numeric", month: "short" })}
                      </Link>
                    )}
                  </span>
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>

      <ol className="series-timeline">
        {data.meetings.map((m) => {
          const isCurrent = m.id === currentId;
          const mins = m.minutes;
          const doneCount = (mins?.follow_ups ?? []).filter((f) => f.status === "done").length;
          const isOpen = expanded === m.id;
          return (
            <li key={m.id} className={isCurrent ? "current" : ""}>
              <div className="st-dot" aria-hidden="true" />
              <div className="card st-card">
                <div className="st-head">
                  <div className="grow">
                    <div className="st-title">
                      {isCurrent ? <strong>{m.title}</strong> : <Link to={`/meetings/${m.id}`}>{m.title}</Link>}
                      {isCurrent && <span className="badge badge-accent">This meeting</span>}
                    </div>
                    <span className="dim tiny">
                      {new Date(m.started_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}
                      {m.duration_seconds ? ` · ${formatDuration(m.duration_seconds)}` : ""}
                    </span>
                  </div>
                  {mins && (
                    <div className="st-counts">
                      <span className="md-stat tone-ok"><b>{mins.decisions.length}</b> decisions</span>
                      <span className="md-stat tone-accent"><b>{mins.action_items.length}</b> actions</span>
                      {mins.follow_ups.length > 0 && (
                        <span className="md-stat tone-plum">
                          <b>{doneCount}/{mins.follow_ups.length}</b> followed up
                        </span>
                      )}
                    </div>
                  )}
                </div>

                {mins ? (
                  <>
                    {mins.key_points.length > 0 ? (
                      <ol className="md-highlights compact">
                        {mins.key_points.map((p, i) => (
                          <li key={i}>
                            <span className="md-n">{i + 1})</span>
                            <span>{p}</span>
                          </li>
                        ))}
                      </ol>
                    ) : (
                      <p className="small st-summary">{mins.summary}</p>
                    )}
                    <button className="link-btn small" onClick={() => setExpanded(isOpen ? null : m.id)}>
                      {isOpen ? "Hide details" : "Show decisions and action items"}
                    </button>
                    {isOpen && (
                      <div className="st-details">
                        {mins.key_points.length > 0 && <p className="small">{mins.summary}</p>}
                        {mins.decisions.length > 0 && (
                          <>
                            <h5>Decisions</h5>
                            <ul>{mins.decisions.map((d, i) => <li key={i}>{d.decision}</li>)}</ul>
                          </>
                        )}
                        {mins.action_items.length > 0 && (
                          <>
                            <h5>Action items</h5>
                            <ul>
                              {mins.action_items.map((a, i) => (
                                <li key={i}>
                                  {a.task} <span className="dim">— {a.owner}</span>
                                </li>
                              ))}
                            </ul>
                          </>
                        )}
                        {mins.follow_ups.length > 0 && <FollowUpsSection items={mins.follow_ups} />}
                      </div>
                    )}
                  </>
                ) : (
                  <p className="small dim">No minutes yet.</p>
                )}
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
