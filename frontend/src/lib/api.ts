const BASE = import.meta.env.VITE_API_URL ?? "http://localhost:8000";
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
  if (init.body && !(init.body instanceof FormData)) {
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

export interface User {
  id: string;
  email: string;
  full_name: string;
  role: Role;
  is_active: boolean;
  voiceprint_count?: number;
  enrolled_seconds?: number;
}

export interface Meeting {
  id: string;
  title: string;
  status: MeetingStatus;
  source: string;
  duration_seconds: number | null;
  language_hint: string | null;
  asr_provider: string | null;
  error: string | null;
  started_at: string;
  processed_at: string | null;
  audio_deleted_at: string | null;
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

export interface Minutes {
  summary: string;
  topics: { title: string; discussion: string; speakers: string[] }[];
  decisions: { decision: string; rationale?: string | null; decided_by?: string | null }[];
  action_items: { task: string; owner: string; due?: string | null; priority: string }[];
  open_questions: string[];
  languages_detected: string[];
  model: string;
  created_at: string;
}

export interface MeetingDetail extends Meeting {
  participants: Participant[];
  segments: Segment[];
  minutes: Minutes | null;
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
  createUser: (body: { email: string; full_name: string; password?: string; role: Role }) =>
    request<User>("/api/users", { method: "POST", body: JSON.stringify(body) }),
  enrollVoice: (userId: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request(`/api/users/${userId}/voiceprints`, { method: "POST", body: form });
  },

  listMeetings: (q?: string) =>
    request<Meeting[]>(`/api/meetings${q ? `?q=${encodeURIComponent(q)}` : ""}`),
  getMeeting: (id: string) => request<MeetingDetail>(`/api/meetings/${id}`),
  audioUrl: (id: string) => request<{ url: string }>(`/api/meetings/${id}/audio-url`),
  createMeeting: (body: { title: string; source?: string; language_hint?: string | null }) =>
    request<Meeting>("/api/meetings", { method: "POST", body: JSON.stringify(body) }),
  uploadAudio: (id: string, file: Blob, filename: string) => {
    const form = new FormData();
    form.append("file", file, filename);
    return request<Meeting>(`/api/meetings/${id}/audio`, { method: "POST", body: form });
  },
  reprocess: (id: string) => request<Meeting>(`/api/meetings/${id}/reprocess`, { method: "POST" }),
  deleteMeeting: (id: string) => request<void>(`/api/meetings/${id}`, { method: "DELETE" }),
  relabel: (id: string, body: { speaker_label: string; user_id?: string | null; enroll?: boolean }) =>
    request<MeetingDetail>(`/api/meetings/${id}/speakers`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  regenerateMinutes: (id: string, language?: string) =>
    request<Minutes>(`/api/meetings/${id}/minutes${language ? `?language=${language}` : ""}`, {
      method: "POST",
    }),

  listToggles: () => request<FeatureToggle[]>("/api/settings"),
  setToggle: (key: string, enabled: boolean) =>
    request<FeatureToggle>(`/api/settings/${key}`, {
      method: "PATCH",
      body: JSON.stringify({ enabled }),
    }),

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

export function formatTimestamp(ms: number): string {
  const total = Math.floor(ms / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}
