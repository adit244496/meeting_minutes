import { useEffect, useRef, useState } from "react";

/** Where the API lives.
 *
 *  A built app defaults to its own origin: the API serves the frontend, so
 *  "" is right whether that is an IP, a domain, or behind nginx. Only the dev
 *  server needs an absolute URL, because Vite serves the UI on its own port.
 *  Setting VITE_API_URL overrides both - but forgetting it no longer points a
 *  deployed build at localhost, which failed with ERR_CONNECTION_REFUSED. */
const BASE = import.meta.env.VITE_API_URL ?? (import.meta.env.DEV ? "http://localhost:8017" : "");
export const API_BASE = BASE;
const TOKEN_KEY = "mm.token";

export const token = {
  get: () => localStorage.getItem(TOKEN_KEY),
  set: (value: string) => localStorage.setItem(TOKEN_KEY, value),
  clear: () => localStorage.removeItem(TOKEN_KEY),
};

class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const jwt = token.get();
  if (jwt) headers.set("Authorization", `Bearer ${jwt}`);
  if (typeof init.body === "string" && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`${BASE}${path}`, { ...init, headers });

  if (response.status === 401) {
    token.clear();
    window.location.hash = "#/login";
    throw new ApiError(401, "Session expired");
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(response.status, body.detail ?? response.statusText);
  }
  return response.status === 204 ? (undefined as T) : response.json();
}

// ---------- types ----------

export type Role = "admin" | "member";
export type MeetingStatus =
  | "created"
  | "uploaded"
  | "processing"
  | "transcribed"
  | "completed"
  | "failed";

export interface DepartmentBrief {
  id: string;
  name: string;
}

export interface Department extends DepartmentBrief {
  member_count: number;
  meeting_count: number;
}

export interface User {
  id: string;
  email: string;
  full_name: string;
  role: Role;
  is_active: boolean;
  departments: DepartmentBrief[];
  voiceprint_count?: number;
  enrolled_seconds?: number;
}

export interface Meeting {
  id: string;
  title: string;
  agenda: string | null;
  status: MeetingStatus;
  source: string;
  duration_seconds: number | null;
  language_hint: string | null;
  asr_provider: string | null;
  error: string | null;
  started_at: string;
  processed_at: string | null;
  audio_deleted_at: string | null;
  transcript_deleted_at: string | null;
  minutes_deleted_at: string | null;
  series_id: string | null;
  series_name: string | null;
  department_id: string | null;
  department_name: string | null;
  is_live: boolean;
  live_transcribed_until: number | null;
  has_recording: boolean;
}

export interface Series {
  id: string;
  name: string;
  meeting_count: number;
  last_meeting_at: string | null;
}

export interface SeriesMeeting {
  id: string;
  title: string;
  status: MeetingStatus;
  started_at: string;
  duration_seconds: number | null;
  minutes: Pick<
    Minutes,
    "kind" | "summary" | "key_points" | "decisions" | "action_items" | "open_questions" | "follow_ups"
  > | null;
}

export interface SeriesSuggestion {
  suggest: boolean;
  series_id?: string | null;
  series_name?: string;
  meetings?: { id: string; title: string; started_at: string }[];
}

export type FollowUpStatus = "done" | "in_progress" | "not_started" | "blocked" | "dropped" | "not_discussed";

export interface FollowUp {
  item: string;
  kind?: "action_item" | "open_question";
  owner?: string | null;
  status: FollowUpStatus;
  note?: string | null;
}

export interface Participant {
  speaker_label: string;
  user_id: string | null;
  display_name: string;
  confidence: number;
  is_manual: boolean;
  speaking_seconds: number;
}

export interface Segment {
  idx: number;
  start_ms: number;
  end_ms: number;
  speaker_label: string;
  language: string | null;
  scripts: string | null;
  text: string;
}

export type MinutesKind = "short" | "detailed";

/** Languages a transcript can be translated into, in the order they are offered. */
export const TRANSCRIPT_LANGUAGES = [
  ["en", "English"],
  ["bn", "Bengali"],
  ["hi", "Hindi"],
] as const;

export type TranscriptLanguage = (typeof TRANSCRIPT_LANGUAGES)[number][0];

export interface TranscriptTranslation {
  language: TranscriptLanguage;
  model: string;
  created_at: string;
  segments: { idx: number; text: string }[];
}

export interface Minutes {
  kind: MinutesKind;
  summary: string;
  key_points: string[];
  follow_ups: FollowUp[];
  topics: { title: string; discussion: string; speakers: string[] }[];
  decisions: { decision: string; rationale?: string | null; decided_by?: string | null }[];
  action_items: { task: string; owner: string; due?: string | null; priority: string }[];
  open_questions: string[];
  languages_detected: string[];
  model: string;
  created_at: string;
  version: number;
  source: string;
  edited_at: string | null;
}

export interface MinutesVersion {
  version: number;
  source: string;
  model: string;
  created_at: string;
  created_by_name: string | null;
  summary: string;
  key_points: string[];
  follow_ups: FollowUp[];
  topics: Minutes["topics"];
  decisions: Minutes["decisions"];
  action_items: Minutes["action_items"];
  open_questions: string[];
  languages_detected: string[];
}

export interface RetentionSetting {
  key: string;
  label: string;
  description: string;
  unit: string;
  minimum: number;
  maximum: number;
  value: number;
}

/** An API key or model choice, as the admin panel is allowed to see it.
 *
 *  `masked` is the whole story for a secret — the real value is never sent to
 *  the browser. For a non-secret (a model name, the provider) it is the value. */
export interface CredentialSetting {
  key: string;
  label: string;
  description: string;
  env_var: string;
  secret: boolean;
  placeholder: string;
  choices: string[];
  /** Offered in a dropdown; unlike `choices`, other values are still allowed. */
  suggestions: string[];
  configured: boolean;
  source: "database" | "environment" | "unset";
  masked: string;
  updated_at: string | null;
}

/** A model a member can pick when regenerating minutes. */
export interface MinutesModel {
  id: string;
  label: string;
  provider: string;
  default: boolean;
}

export interface MinutesModelCatalog {
  models: { id: string; label: string; provider: string; enabled: boolean; key_configured: boolean }[];
  using_default: boolean;
  default_ids: string[];
}

export interface MeetingDetail extends Meeting {
  participants: Participant[];
  segments: Segment[];
  minutes: Minutes[];
}

export interface FeatureToggle {
  key: string;
  label: string;
  description: string;
  enabled: boolean;
}

export interface Progress {
  meeting_id: string;
  stage: string;
  percent: number;
  message: string;
  /** Unix seconds when the worker sent this update. */
  ts?: number;
}

const FINAL_STAGES = new Set([
  "done",
  "failed",
  "minutes_done",
  "minutes_failed",
  "translate_done",
  "translate_failed",
]);

/** Live processing progress for one meeting over SSE.
 *
 *  EventSource reconnects on its own after a dropped connection (a proxy
 *  timeout, the API reloading), so errors are not treated as the end - only a
 *  "done" or "failed" event is. `onFinish` fires once, so the page can reload. */
export function useMeetingProgress(
  id: string | undefined,
  active: boolean,
  onFinish?: (progress: Progress) => void,
): Progress | null {
  const [progress, setProgress] = useState<Progress | null>(null);
  const finish = useRef(onFinish);
  finish.current = onFinish;

  useEffect(() => {
    if (!id || !active) return;
    const stream = api.progressStream(id);
    stream.onmessage = (event) => {
      let update: Progress;
      try {
        update = JSON.parse(event.data) as Progress;
      } catch {
        return;
      }
      setProgress(update);
      if (FINAL_STAGES.has(update.stage)) {
        stream.close();
        finish.current?.(update);
      }
    };
    return () => stream.close();
  }, [id, active]);

  return progress;
}

// ---------- endpoints ----------

export const api = {
  async login(email: string, password: string) {
    const form = new URLSearchParams({ username: email, password });
    const response = await fetch(`${BASE}/api/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: form,
    });
    if (!response.ok) throw new ApiError(response.status, "Incorrect email or password");
    const data = (await response.json()) as { access_token: string };
    token.set(data.access_token);
    return data;
  },

  me: () => request<User>("/api/auth/me"),

  listUsers: () => request<User[]>("/api/users"),
  createUser: (body: {
    email: string;
    full_name: string;
    password?: string;
    role: Role;
    department_ids?: string[];
  }) => request<User>("/api/users", { method: "POST", body: JSON.stringify(body) }),
  /** Admin only: replaces every department this person belongs to. */
  setUserDepartments: (userId: string, departmentIds: string[]) =>
    request<User>(`/api/users/${userId}/departments`, {
      method: "PUT",
      body: JSON.stringify({ department_ids: departmentIds }),
    }),

  listDepartments: () => request<Department[]>("/api/departments"),
  createDepartment: (name: string) =>
    request<Department>("/api/departments", { method: "POST", body: JSON.stringify({ name }) }),
  renameDepartment: (id: string, name: string) =>
    request<Department>(`/api/departments/${id}`, { method: "PATCH", body: JSON.stringify({ name }) }),
  deleteDepartment: (id: string) => request<void>(`/api/departments/${id}`, { method: "DELETE" }),
  /** Moves a meeting between departments; null leaves it unassigned. */
  assignDepartment: (meetingId: string, departmentId: string | null) =>
    request<Meeting>(`/api/meetings/${meetingId}/department`, {
      method: "PUT",
      body: JSON.stringify({ department_id: departmentId }),
    }),
  enrollVoice: (userId: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request(`/api/users/${userId}/voiceprints`, { method: "POST", body: form });
  },

  listMeetings: (q?: string) =>
    request<Meeting[]>(`/api/meetings${q ? `?q=${encodeURIComponent(q)}` : ""}`),
  getMeeting: (id: string) => request<MeetingDetail>(`/api/meetings/${id}`),
  overview: () =>
    request<{ total_meetings: number; by_status: Record<string, number>; total_hours: number; identified_speakers: number }>(
      "/api/meetings/stats/overview",
    ),
  audioUrl: (id: string) => request<{ url: string }>(`/api/meetings/${id}/audio-url`),
  /** Edit a meeting's own details - its title or agenda. */
  updateMeeting: (id: string, body: { title?: string; agenda?: string | null }) =>
    request<Meeting>(`/api/meetings/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  createMeeting: (body: {
    title: string;
    agenda?: string | null;
    source?: string;
    language_hint?: string | null;
    series_id?: string | null;
    new_series_name?: string | null;
    department_id?: string | null;
  }) => request<Meeting>("/api/meetings", { method: "POST", body: JSON.stringify(body) }),

  listSeries: () => request<Series[]>("/api/series"),
  seriesMeetings: (seriesId: string) =>
    request<{ series: Series; meetings: SeriesMeeting[] }>(`/api/series/${seriesId}/meetings`),
  seriesSuggestion: (meetingId: string) =>
    request<SeriesSuggestion>(`/api/meetings/${meetingId}/series-suggestion`),
  assignSeries: (
    meetingId: string,
    body: { series_id?: string | null; new_series_name?: string | null; include_meeting_ids?: string[] },
  ) => request<Meeting>(`/api/meetings/${meetingId}/series`, { method: "PUT", body: JSON.stringify(body) }),
  uploadAudio: (id: string, file: Blob, filename: string) => {
    const form = new FormData();
    form.append("file", file, filename);
    return request<Meeting>(`/api/meetings/${id}/audio`, { method: "POST", body: form });
  },
  reprocess: (id: string) => request<Meeting>(`/api/meetings/${id}/reprocess`, { method: "POST" }),
  deleteMeeting: (id: string) => request<void>(`/api/meetings/${id}`, { method: "DELETE" }),
  /** Admin only: removes the audio, keeps transcript and minutes. */
  deleteRecording: (id: string) => request<Meeting>(`/api/meetings/${id}/audio`, { method: "DELETE" }),

  liveStart: (id: string) => request<{ live: boolean }>(`/api/meetings/${id}/live/start`, { method: "POST" }),
  /** Save a live recording from the server's copy, when the recording tab is gone. */
  liveFinish: (id: string) => request<Meeting>(`/api/meetings/${id}/live/finish`, { method: "POST" }),
  /** A short playable clip per speaker, so a voice can be recognised and named. */
  speakerSamples: (id: string) =>
    request<{ samples: Record<string, string>; reason?: string }>(`/api/meetings/${id}/speakers/samples`),

  /** Languages this transcript has been translated into, and any job running. */
  listTranslations: (id: string) =>
    request<{
      languages: TranscriptLanguage[];
      in_progress: boolean;
      /** Which language is being made right now, when one is. */
      language: TranscriptLanguage | null;
      percent: number | null;
      message: string | null;
      age_seconds: number | null;
      /** Queued, but no worker has taken it - a stopped worker looks exactly
       *  like a slow one from here, so the server says which it is. */
      unclaimed: boolean;
    }>(`/api/meetings/${id}/transcript/translations`),
  getTranslation: (id: string, language: TranscriptLanguage) =>
    request<TranscriptTranslation>(`/api/meetings/${id}/transcript?language=${language}`),
  /** Queues a translation; progress arrives on progressStream as "translate". */
  translateTranscript: (id: string, language: TranscriptLanguage) =>
    request<{ queued: boolean }>(`/api/meetings/${id}/transcript/translate?language=${language}`, {
      method: "POST",
    }),

  liveStop: (id: string) => request<{ live: boolean }>(`/api/meetings/${id}/live/stop`, { method: "POST" }),
  /** Send the next piece of a live recording. Resolves to the next sequence
   *  number the server expects - lower than `seq + 1` when pieces went missing. */
  async liveChunk(id: string, seq: number, blob: Blob, ext: string): Promise<number> {
    const headers = new Headers({ "Content-Type": blob.type || "application/octet-stream" });
    const jwt = token.get();
    if (jwt) headers.set("Authorization", `Bearer ${jwt}`);
    const response = await fetch(`${BASE}/api/meetings/${id}/live/chunk?seq=${seq}&ext=${ext}`, {
      method: "POST",
      headers,
      body: blob,
    });
    const body = await response.json().catch(() => ({}));
    if (response.status === 409 && typeof body.detail?.expected_seq === "number") return body.detail.expected_seq;
    if (!response.ok) throw new ApiError(response.status, typeof body.detail === "string" ? body.detail : "Upload failed");
    return body.next_seq as number;
  },
  relabel: (id: string, body: { speaker_label: string; user_id?: string | null; enroll?: boolean }) =>
    request<MeetingDetail>(`/api/meetings/${id}/speakers`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  /** Queues generation; progress arrives on progressStream as "minutes" events. */
  regenerateMinutes: (id: string, kind: MinutesKind, language?: string, model?: string) =>
    request<{ queued: boolean }>(
      `/api/meetings/${id}/minutes?kind=${kind}${language ? `&language=${language}` : ""}${
        model ? `&model=${encodeURIComponent(model)}` : ""
      }`,
      { method: "POST" },
    ),
  currentProgress: (id: string) =>
    request<Partial<Progress> & { age_seconds: number | null }>(`/api/meetings/${id}/progress`),
  updateMinutes: (
    id: string,
    kind: MinutesKind,
    body: Partial<
      Pick<
        Minutes,
        "summary" | "key_points" | "follow_ups" | "topics" | "decisions" | "action_items" | "open_questions"
      >
    >,
  ) =>
    request<Minutes>(`/api/meetings/${id}/minutes?kind=${kind}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  listMinutesVersions: (id: string, kind: MinutesKind) =>
    request<MinutesVersion[]>(`/api/meetings/${id}/minutes/versions?kind=${kind}`),
  restoreMinutesVersion: (id: string, kind: MinutesKind, version: number) =>
    request<Minutes>(`/api/meetings/${id}/minutes/versions/${version}/restore?kind=${kind}`, {
      method: "POST",
    }),

  listToggles: () => request<FeatureToggle[]>("/api/settings"),
  setToggle: (key: string, enabled: boolean) =>
    request<FeatureToggle>(`/api/settings/${key}`, {
      method: "PATCH",
      body: JSON.stringify({ enabled }),
    }),

  listRetention: () => request<RetentionSetting[]>("/api/settings/numbers"),
  setRetention: (key: string, value: number) =>
    request<RetentionSetting>(`/api/settings/numbers/${key}`, {
      method: "PATCH",
      body: JSON.stringify({ value }),
    }),

  // Admin-only, and the response carries a mask rather than the key.
  listCredentials: () => request<CredentialSetting[]>("/api/settings/credentials"),
  listMinutesModels: () => request<MinutesModel[]>("/api/settings/minutes-models"),
  minutesModelCatalog: () => request<MinutesModelCatalog>("/api/settings/minutes-models/catalog"),
  setCredential: (key: string, value: string) =>
    request<CredentialSetting>(`/api/settings/credentials/${key}`, {
      method: "PUT",
      body: JSON.stringify({ value }),
    }),

  /** Every transcript and/or set of minutes from the last `days` days as a ZIP. */
  exportMeetings: (kind: "transcript" | "minutes" | "both", days: number): Promise<void> =>
    api.download(`/api/exports/meetings?kind=${kind}&days=${days}`, "neo-minutes-export.zip"),

  /** Fetch a protected file and hand it to the browser as a download.
   *
   *  A plain <a href> cannot send the Authorization header, so the file is
   *  fetched as a blob first. The server's filename is preferred - it carries
   *  the meeting title, which may be in Devanagari or Bengali. */
  async download(path: string, fallbackName: string) {
    const headers = new Headers();
    const jwt = token.get();
    if (jwt) headers.set("Authorization", `Bearer ${jwt}`);

    const response = await fetch(`${BASE}${path}`, { headers });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new ApiError(response.status, body.detail ?? response.statusText);
    }

    const disposition = response.headers.get("Content-Disposition") ?? "";
    const utf8 = /filename\*=UTF-8''([^;]+)/i.exec(disposition);
    const plain = /filename="([^"]+)"/i.exec(disposition);
    const name = utf8 ? decodeURIComponent(utf8[1]) : plain ? plain[1] : fallbackName;

    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = name;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  },

  // SSE has no Authorization header; see the endpoint docstring for the
  // production fix (short-lived signed token in the query string).
  progressStream: (id: string) => new EventSource(`${BASE}/api/meetings/${id}/events`),
};

export function formatDuration(seconds: number | null | undefined): string {
  if (!seconds) return "-";
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h > 0 ? `${h}h ${m}m` : m > 0 ? `${m}m ${s}s` : `${s}s`;
}

/** Wall-clock time of a point in a recording, e.g. "14:32".
 *
 *  A live transcript is read while the meeting is still happening, so "08:12"
 *  into the recording answers the wrong question - people want to know when
 *  something was said, not how far in. `seconds` adds them for a live line. */
export function clockAt(startedAt: string, ms: number, seconds = false): string {
  const at = new Date(new Date(startedAt).getTime() + ms);
  return at.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    ...(seconds ? { second: "2-digit" } : {}),
  });
}

export function formatTimestamp(ms: number): string {
  const total = Math.floor(ms / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}
