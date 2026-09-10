import { useEffect, useRef, useState } from "react";

import { IconAlert, IconUpload } from "../components/icons";
import { api, type FeatureToggle, type Role, type User } from "../lib/api";

// Matches MIN_ENROLLMENT_SECONDS / RECOMMENDED_ENROLLMENT_SECONDS on the server.
const RECOMMENDED_SECONDS = 60;

export default function Admin() {
  const [users, setUsers] = useState<User[]>([]);
  const [toggles, setToggles] = useState<FeatureToggle[]>([]);
  const [form, setForm] = useState({
    full_name: "",
    email: "",
    password: "",
    role: "member" as Role,
  });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [enrolling, setEnrolling] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const enrollmentOn =
    toggles.find((t) => t.key === "voice_enrollment_enabled")?.enabled ?? false;

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
      <div className="page-head">
        <h1>Users &amp; settings</h1>
        <p className="lead">
          {enrollmentOn
            ? "Enroll a few voice samples per person before their first meeting so the system can put real names to speakers."
            : "Transcripts label speakers as Speaker 1, 2, 3. Turn on voice enrollment below to put real names to them."}
        </p>
      </div>

      {error && (
        <div className="alert alert-err" style={{ marginBottom: 16 }}>
          <IconAlert size={16} />
          <span>{error}</span>
        </div>
      )}

      <div className="card">
        <div className="card-head">
          <h3>Features</h3>
        </div>
        <div className="card-body">
          <div className="stack" style={{ gap: 14 }}>
            {toggles.map((t) => (
              <label key={t.key} className="toggle-row">
                <input
                  type="checkbox"
                  checked={t.enabled}
                  onChange={(e) => flip(t.key, e.target.checked)}
                />
                <span>
                  <span className="label">{t.label}</span>
                  <span className="desc">{t.description}</span>
                </span>
              </label>
            ))}
            {toggles.length === 0 && <span className="dim small">No settings available.</span>}
          </div>
        </div>
      </div>

      <div className="card">
        <div className="card-head">
          <h3>Add a user</h3>
        </div>
        <div className="card-body">
          <form onSubmit={createUser}>
            <div className="form-grid cols-2">
              <label className="field">
                <span>Full name</span>
                <input
                  type="text"
                  placeholder="Priya Sharma"
                  value={form.full_name}
                  onChange={(e) => setForm({ ...form, full_name: e.target.value })}
                  required
                />
              </label>
              <label className="field">
                <span>Email</span>
                <input
                  type="email"
                  placeholder="priya@company.com"
                  value={form.email}
                  onChange={(e) => setForm({ ...form, email: e.target.value })}
                  required
                />
              </label>
              <label className="field">
                <span>Password (optional)</span>
                <input
                  type="password"
                  placeholder="Leave blank if they will not sign in"
                  value={form.password}
                  onChange={(e) => setForm({ ...form, password: e.target.value })}
                />
              </label>
              <label className="field">
                <span>Role</span>
                <select
                  value={form.role}
                  onChange={(e) => setForm({ ...form, role: e.target.value as Role })}
                >
                  <option value="member">Member</option>
                  <option value="admin">Admin</option>
                </select>
              </label>
            </div>
            <div className="row" style={{ marginTop: 14 }}>
              <button className="btn btn-primary" type="submit" disabled={busy}>
                Add user
              </button>
              <span className="small dim">
                Leave the password blank for people who only need to be recognised in
                transcripts.
              </span>
            </div>
          </form>
        </div>
      </div>

      <div className="card">
        <div className="card-head">
          <h3>People</h3>
          <span className="dim tiny">{users.length}</span>
        </div>
        <div className="table-wrap">
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
                    <td data-label="Name">
                      <strong>{u.full_name}</strong>
                    </td>
                    <td data-label="Email" className="dim small">
                      {u.email}
                    </td>
                    <td data-label="Role">
                      <span className="pill plain">{u.role}</span>
                    </td>
                    {enrollmentOn && (
                      <td data-label="Enrollment" style={{ minWidth: 180 }}>
                        <span style={{ display: "block", width: "100%" }}>
                          <span className="bar" style={{ display: "block", marginBottom: 4 }}>
                            <i style={{ width: `${pct}%` }} />
                          </span>
                          <span className="dim tiny mono">
                            {u.voiceprint_count ?? 0} sample(s) · {seconds.toFixed(0)}s of{" "}
                            {RECOMMENDED_SECONDS}s
                          </span>
                        </span>
                      </td>
                    )}
                    {enrollmentOn && (
                      <td data-label="">
                        <button
                          className="btn btn-sm"
                          disabled={busy}
                          onClick={() => {
                            setEnrolling(u.id);
                            fileInput.current?.click();
                          }}
                        >
                          <IconUpload size={14} />
                          Add sample
                        </button>
                      </td>
                    )}
                  </tr>
                );
              })}
              {users.length === 0 && (
                <tr>
                  <td colSpan={5}>
                    <div className="empty">
                      <p className="big">No users yet</p>
                    </div>
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        {enrollmentOn && (
          <div className="card-foot">
            Three samples of about 20 seconds each work better than one 60-second sample —
            they capture more of the natural variation in how somebody speaks. Use clean
            speech with no background chatter.
          </div>
        )}
      </div>

      <input
        ref={fileInput}
        type="file"
        accept="audio/*"
        className="sr-only"
        onChange={(e) => e.target.files?.[0] && uploadSample(e.target.files[0])}
      />
    </>
  );
}
