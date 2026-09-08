import { useEffect, useRef, useState } from "react";

import { api, type FeatureToggle, type Role, type User } from "../lib/api";

// Matches MIN_ENROLLMENT_SECONDS / RECOMMENDED_ENROLLMENT_SECONDS on the server.
const RECOMMENDED_SECONDS = 60;

export default function Admin() {
  const [users, setUsers] = useState<User[]>([]);
  const [toggles, setToggles] = useState<FeatureToggle[]>([]);
  const enrollmentOn = toggles.find((t) => t.key === "voice_enrollment_enabled")?.enabled ?? false;
  const [form, setForm] = useState({ full_name: "", email: "", password: "", role: "member" as Role });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [enrolling, setEnrolling] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  async function refresh() {
    try {
      setUsers(await api.listUsers());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load users");
    }
  }

  useEffect(() => {
    refresh();
    api.listToggles().then(setToggles).catch(() => undefined);
  }, []);

  async function flip(key: string, enabled: boolean) {
    setToggles((prev) => prev.map((t) => (t.key === key ? { ...t, enabled } : t)));
    try {
      await api.setToggle(key, enabled);
    } catch (err) {
      // Put it back if the server refused.
      setToggles((prev) => prev.map((t) => (t.key === key ? { ...t, enabled: !enabled } : t)));
      setError(err instanceof Error ? err.message : "Could not update setting");
    }
  }

  async function createUser(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.createUser({
        email: form.email.trim(),
        full_name: form.full_name.trim(),
        password: form.password || undefined,
        role: form.role,
      });
      setForm({ full_name: "", email: "", password: "", role: "member" });
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create user");
    } finally {
      setBusy(false);
    }
  }

  async function uploadSample(file: File) {
    if (!enrolling) return;
    setBusy(true);
    setError("");
    try {
      await api.enrollVoice(enrolling, file);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Enrollment failed");
    } finally {
      setBusy(false);
      setEnrolling(null);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  return (
    <>
      <h1>Users &amp; settings</h1>
      <p className="sub">
        {enrollmentOn
          ? "Enroll a few voice samples per person before their first meeting so the system can put names to speakers automatically."
          : "Transcripts label speakers as Speaker 1, 2, 3. Turn on voice enrollment below to put real names to them."}
      </p>

      <div className="panel">
        <h3>Features</h3>
        <div className="stack">
          {toggles.map((t) => (
            <label key={t.key} className="toggle-row">
              <input
                type="checkbox"
                checked={t.enabled}
                onChange={(e) => flip(t.key, e.target.checked)}
              />
              <span>
                <strong>{t.label}</strong>
                <span className="muted small" style={{ display: "block" }}>
                  {t.description}
                </span>
              </span>
            </label>
          ))}
          {toggles.length === 0 && <span className="muted small">No settings available.</span>}
        </div>
      </div>

      <div className="panel">
        <h3>Add a user</h3>
        <form className="row" onSubmit={createUser}>
          <input
            className="grow"
            placeholder="Full name"
            value={form.full_name}
            onChange={(e) => setForm({ ...form, full_name: e.target.value })}
            required
          />
          <input
            className="grow"
            type="email"
            placeholder="Email"
            value={form.email}
            onChange={(e) => setForm({ ...form, email: e.target.value })}
            required
          />
          <input
            type="password"
            placeholder="Password (optional)"
            value={form.password}
            onChange={(e) => setForm({ ...form, password: e.target.value })}
          />
          <select
            value={form.role}
            onChange={(e) => setForm({ ...form, role: e.target.value as Role })}
          >
            <option value="member">Member</option>
            <option value="admin">Admin</option>
          </select>
          <button className="primary" type="submit" disabled={busy}>
            Add
          </button>
        </form>
        <p className="small muted" style={{ margin: "10px 0 0" }}>
          Leave the password blank for people who only need to be recognised in
          transcripts and will not sign in.
        </p>
      </div>

      {error && <p className="err small">{error}</p>}

      <div className="panel" style={{ padding: 0 }}>
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Email</th>
              <th>Role</th>
              {enrollmentOn && <th>Voice enrollment</th>}
              {enrollmentOn && <th />}
            </tr>
          </thead>
          <tbody>
            {users.map((u) => {
              const seconds = u.enrolled_seconds ?? 0;
              const pct = Math.min(100, (seconds / RECOMMENDED_SECONDS) * 100);
              return (
                <tr key={u.id}>
                  <td>
                    <strong>{u.full_name}</strong>
                  </td>
                  <td className="muted small">{u.email}</td>
                  <td>
                    <span className="badge">{u.role}</span>
                  </td>
                  {enrollmentOn && (
                    <td style={{ minWidth: 200 }}>
                      <div className="progress" style={{ marginBottom: 4 }}>
                        <div style={{ width: `${pct}%` }} />
                      </div>
                      <span className="muted small mono">
                        {u.voiceprint_count ?? 0} sample(s) · {seconds.toFixed(0)}s of{" "}
                        {RECOMMENDED_SECONDS}s
                      </span>
                    </td>
                  )}
                  {enrollmentOn && (
                    <td style={{ width: 150 }}>
                      <button
                        disabled={busy}
                        onClick={() => {
                          setEnrolling(u.id);
                          fileInput.current?.click();
                        }}
                      >
                        Add sample
                      </button>
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <input
        ref={fileInput}
        type="file"
        accept="audio/*"
        style={{ display: "none" }}
        onChange={(e) => e.target.files?.[0] && uploadSample(e.target.files[0])}
      />

      {enrollmentOn && (
        <p className="small muted">
          Three samples of about 20 seconds each work better than one 60-second sample —
          they capture more of the natural variation in how somebody speaks. Use clean
          speech with no background chatter.
        </p>
      )}
    </>
  );
}
