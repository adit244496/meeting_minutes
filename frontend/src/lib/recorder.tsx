/** The in-browser meeting recorder, shared by the whole app.
 *
 *  It lives above the router outlet, not inside the Meetings page: a recorder
 *  owned by a page is torn down when the user opens a meeting, which left the
 *  microphone running with no Stop button and a recording that never uploaded.
 *  Here it survives navigation, and the app shell shows a recording bar with
 *  Stop on every page.
 *
 *  Other tabs can ask the recording tab to stop over a BroadcastChannel, so the
 *  live meeting's page offers Stop wherever it is open.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";

import { api } from "./api";

// How often a live recording sends its audio to the server.
const LIVE_SEND_MS = 30_000;
const CHANNEL = "neo-minutes-recorder";

type ChannelMessage = { type: "stop" | "pause" | "resume" | "ping" | "pong"; meetingId: string };

export type RecorderStatus = "idle" | "starting" | "recording" | "paused" | "saving";

export interface StartOptions {
  title: string;
  agenda: string | null;
  language_hint: string | null;
  series_id: string | null;
  new_series_name: string | null;
  department_id: string | null;
}

interface RecorderValue {
  status: RecorderStatus;
  meetingId: string | null;
  title: string;
  seconds: number;
  /** When recording started, as an ISO string - the meeting's own started_at,
   *  so a live transcript can show clock times rather than offsets. */
  startedAt: string | null;
  liveOn: boolean;
  error: string;
  canRetryUpload: boolean;
  /** Increments whenever a recording finishes uploading, so lists can refresh. */
  savedCount: number;
  start: (options: StartOptions) => Promise<void>;
  stop: () => void;
  pause: () => void;
  resume: () => void;
  retryUpload: () => void;
  clearError: () => void;
}

const RecorderContext = createContext<RecorderValue | null>(null);

export function useRecorder(): RecorderValue {
  const value = useContext(RecorderContext);
  if (!value) throw new Error("useRecorder must be used inside RecorderProvider");
  return value;
}

/** Browsers only expose navigator.mediaDevices in a secure context: HTTPS, or
 *  localhost. Served over plain HTTP on an IP it is simply undefined. */
export function micSupport(): { ok: boolean; reason?: string } {
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

export function formatElapsed(seconds: number) {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

interface Session {
  meetingId: string;
  enabled: boolean;
  ext: string;
  type: string;
  all: Blob[];
  pending: Blob[];
  pieces: Blob[];
  sent: number;
  sending: boolean;
}

export function RecorderProvider({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = useState<RecorderStatus>("idle");
  const [meetingId, setMeetingId] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [seconds, setSeconds] = useState(0);
  const [startedAt, setStartedAt] = useState<string | null>(null);
  const [liveOn, setLiveOn] = useState(false);
  const [error, setError] = useState("");
  const [failedUpload, setFailedUpload] = useState<{ id: string; blob: Blob; name: string } | null>(null);
  const [savedCount, setSavedCount] = useState(0);

  const recorder = useRef<MediaRecorder | null>(null);
  const session = useRef<Session | null>(null);
  const clock = useRef<ReturnType<typeof setInterval> | null>(null);
  const sender = useRef<ReturnType<typeof setInterval> | null>(null);
  const channel = useRef<BroadcastChannel | null>(null);

  const flush = useCallback(async () => {
    const s = session.current;
    if (!s || !s.enabled || s.sending) return;
    if (s.pending.length) {
      s.pieces.push(new Blob(s.pending, { type: s.type }));
      s.pending = [];
    }
    s.sending = true;
    try {
      while (s.sent < s.pieces.length) {
        // The server answers with the next piece it expects, lower than ours
        // if one went missing - the loop then resends from there.
        s.sent = await api.liveChunk(s.meetingId, s.sent, s.pieces[s.sent], s.ext);
      }
    } catch {
      // Network hiccup: the pieces stay queued and go with the next flush.
    } finally {
      s.sending = false;
    }
  }, []);

  const upload = useCallback(async (id: string, blob: Blob, name: string) => {
    setStatus("saving");
    try {
      await api.uploadAudio(id, blob, name);
      setFailedUpload(null);
      setSavedCount((n) => n + 1);
    } catch (err) {
      setFailedUpload({ id, blob, name });
      setError(
        `${err instanceof Error ? err.message : "Upload failed"} — the recording is still in this tab. Retry the upload.`,
      );
    } finally {
      setStatus("idle");
      setMeetingId(null);
      setLiveOn(false);
    }
  }, []);

  const stop = useCallback(() => {
    if (recorder.current && recorder.current.state !== "inactive") recorder.current.stop();
  }, []);

  const pause = useCallback(() => {
    if (recorder.current?.state !== "recording") return;
    recorder.current.pause();
    // The clock counts recorded time, so it stops with the microphone.
    if (clock.current) clearInterval(clock.current);
    setStatus("paused");
    // Send what is already recorded, so the live transcript catches up rather
    // than waiting for the recording to resume.
    flush();
  }, [flush]);

  const resume = useCallback(() => {
    if (recorder.current?.state !== "paused") return;
    recorder.current.resume();
    clock.current = setInterval(() => setSeconds((n) => n + 1), 1000);
    setStatus("recording");
  }, []);

  const start = useCallback(
    async (options: StartOptions) => {
      if (recorder.current && recorder.current.state !== "inactive") return;
      const support = micSupport();
      if (!support.ok) throw new Error(support.reason ?? "Recording is unavailable");
      setError("");
      setStatus("starting");

      let stream: MediaStream;
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          audio: {
            // The browser's defaults are tuned for one-to-one calls and can
            // suppress quieter speakers in a room; diarization needs everyone.
            echoCancellation: false,
            noiseSuppression: false,
            autoGainControl: true,
            channelCount: 1,
          },
        });
      } catch (err) {
        setStatus("idle");
        throw err instanceof Error ? new Error(`Microphone unavailable: ${err.message}`) : err;
      }

      try {
        // iOS Safari does not produce webm; ask for what the device supports.
        const mime = ["audio/webm", "audio/mp4"].find((t) => MediaRecorder.isTypeSupported?.(t));
        const mr = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
        const type = mr.mimeType || mime || "audio/webm";

        // The meeting exists from the start, so the live transcript has a home.
        const meeting = await api.createMeeting({ ...options, source: "browser_mic" });
        let enabled = false;
        try {
          enabled = (await api.liveStart(meeting.id)).live;
        } catch {
          // Live transcript unavailable: the recording still uploads at the end.
        }

        const s: Session = {
          meetingId: meeting.id,
          enabled,
          ext: type.includes("mp4") ? "mp4" : "webm",
          type,
          all: [],
          pending: [],
          pieces: [],
          sent: 0,
          sending: false,
        };
        session.current = s;

        mr.ondataavailable = (e) => {
          if (e.data.size > 0) {
            s.all.push(e.data);
            s.pending.push(e.data);
          }
        };
        mr.onstop = async () => {
          stream.getTracks().forEach((t) => t.stop());
          if (clock.current) clearInterval(clock.current);
          if (sender.current) clearInterval(sender.current);
          recorder.current = null;
          session.current = null;
          setStatus("saving");
          if (s.enabled) await api.liveStop(s.meetingId).catch(() => undefined);
          // The complete recording, not the live pieces, is what the final
          // transcript and minutes are made from.
          await upload(s.meetingId, new Blob(s.all, { type }), `recording.${s.ext === "mp4" ? "m4a" : "webm"}`);
        };

        mr.start(5000); // flush every 5s so a crash does not lose everything
        recorder.current = mr;
        setMeetingId(meeting.id);
        setTitle(meeting.title);
        setLiveOn(enabled);
        setSeconds(0);
        setStartedAt(meeting.started_at);
        setStatus("recording");
        clock.current = setInterval(() => setSeconds((n) => n + 1), 1000);
        if (enabled) sender.current = setInterval(flush, LIVE_SEND_MS);
        setSavedCount((n) => n + 1);
      } catch (err) {
        stream.getTracks().forEach((t) => t.stop());
        setStatus("idle");
        throw err;
      }
    },
    [flush, upload],
  );

  // Answer stop requests and "is anyone recording this?" pings from other tabs.
  useEffect(() => {
    if (typeof BroadcastChannel === "undefined") return;
    const ch = new BroadcastChannel(CHANNEL);
    channel.current = ch;
    ch.onmessage = (event: MessageEvent<ChannelMessage>) => {
      const current = session.current?.meetingId;
      if (!current || event.data.meetingId !== current) return;
      if (event.data.type === "stop") stop();
      if (event.data.type === "pause") pause();
      if (event.data.type === "resume") resume();
      if (event.data.type === "ping") ch.postMessage({ type: "pong", meetingId: current } satisfies ChannelMessage);
    };
    return () => ch.close();
  }, [stop, pause, resume]);

  // Closing or reloading the tab ends the recording, so warn first.
  useEffect(() => {
    if (status !== "recording" && status !== "paused" && status !== "saving") return;
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [status]);

  const value = useMemo<RecorderValue>(
    () => ({
      status,
      meetingId,
      title,
      seconds,
      startedAt,
      liveOn,
      error,
      canRetryUpload: failedUpload !== null,
      savedCount,
      start,
      stop,
      pause,
      resume,
      retryUpload: () => {
        if (!failedUpload) return;
        setError("");
        upload(failedUpload.id, failedUpload.blob, failedUpload.name);
      },
      clearError: () => setError(""),
    }),
    [status, meetingId, title, seconds, startedAt, liveOn, error, failedUpload, savedCount, start, stop, pause, resume, upload],
  );

  return <RecorderContext.Provider value={value}>{children}</RecorderContext.Provider>;
}

/** Ask whichever tab is recording `meetingId` to stop, pause or resume.
 *  Resolves true if a tab answered, false if none did (its tab was closed). */
export function requestRemoteControl(
  meetingId: string,
  action: "stop" | "pause" | "resume" = "stop",
  waitMs = 1500,
): Promise<boolean> {
  if (typeof BroadcastChannel === "undefined") return Promise.resolve(false);
  return new Promise((resolve) => {
    const ch = new BroadcastChannel(CHANNEL);
    const timer = setTimeout(() => {
      ch.close();
      resolve(false);
    }, waitMs);
    ch.onmessage = (event: MessageEvent<ChannelMessage>) => {
      if (event.data.type === "pong" && event.data.meetingId === meetingId) {
        clearTimeout(timer);
        ch.postMessage({ type: action, meetingId } satisfies ChannelMessage);
        ch.close();
        resolve(true);
      }
    };
    ch.postMessage({ type: "ping", meetingId } satisfies ChannelMessage);
  });
}
