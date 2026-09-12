import { useEffect, useRef, useState } from "react";

import {
  IconAlert,
  IconCheck,
  IconEye,
  IconEyeOff,
  IconKey,
  IconUpload,
} from "../components/icons";
import {
  api,
  type CredentialSetting,
  type FeatureToggle,
  type RetentionSetting,
  type Role,
  type User,
} from "../lib/api";

// Matches MIN_ENROLLMENT_SECONDS / RECOMMENDED_ENROLLMENT_SECONDS on the server.
const RECOMMENDED_SECONDS = 60;

type Section = "people" | "providers" | "retention" | "features";

// Four panels rather than five stacked cards: the page had grown long enough
// that the People table sat below three screens of settings.
const SECTIONS: { id: Section; label: string }[] = [
  { id: "people", label: "People" },
  { id: "providers", label: "Providers & keys" },
  { id: "retention", label: "Data retention" },
  { id: "features", label: "Features" },
];

export default function Admin() {
  const [section, setSection] = useState<Section>("people");
  const [users, setUsers] = useState<User[]>([]);
  const [toggles, setToggles] = useState<FeatureToggle[]>([]);
  const [retention, setRetention] = useState<RetentionSetting[]>([]);
  const [creds, setCreds] = useState<CredentialSetting[]>([]);
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

  // Credential editing. `drafts` holds what is being typed; the stored key is
  // never sent to the browser, so there is nothing to prefill for a secret.
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [reveal, setReveal] = useState<Record<string, boolean>>({});
  const [savingKey, setSavingKey] = useState<string | null>(null);
  const [savedKey, setSavedKey] = useState<string | null>(null);
  const savedTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

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
    api.listRetention().then(setRetention).catch(() => undefined);
    api.listCredentials().then(setCreds).catch(() => undefined);
  }, []);

  useEffect(() => () => {
    if (savedTimer.current) clearTimeout(savedTimer.current);
  }, []);

  async function saveRetention(key: string, value: number) {
    const previous = retention;
    setRetention((prev) => prev.map((r) => (r.key === key ? { ...r, value } : r)));
    try {
      await api.setRetention(key, value);
    } catch (err) {
      setRetention(previous);
      setError(err instanceof Error ? err.message : "Could not save retention setting");
    }
  }

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

  async function saveCredential(key: string, value: string) {
    setSavingKey(key);
    setError("");
    try {
      const updated = await api.setCredential(key, value);
      setCreds((prev) => prev.map((c) => (c.key === key ? updated : c)));
      setDrafts((prev) => {
        const next = { ...prev };
        delete next[key];
        return next;
      });
      setReveal((prev) => ({ ...prev, [key]: false }));
      setSavedKey(key);
      if (savedTimer.current) clearTimeout(savedTimer.current);
      savedTimer.current = setTimeout(() => setSavedKey(null), 2200);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save that setting");
    } finally {
      setSavingKey(null);
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

  function statusPill(c: CredentialSetting) {
    if (c.source === "database") return <span className="pill completed">saved here</span>;
    if (c.source === "environment") return <span className="pill transcribed">from .env</span>;
    return <span className="pill created">not set</span>;
  }

  function note(c: CredentialSetting) {
    if (c.source === "environment") {
      return `Currently coming from ${c.env_var}. Saving here overrides it without a restart.`;
    }
    if (c.source === "database") {
      const when = c.updated_at ? new Date(c.updated_at).toLocaleString() : "";
      return c.secret
        ? `Saved${when ? ` ${when}` : ""}. The stored key is never shown — type a new one to replace it, or clear it to use ${c.env_var} again.`
        : `Saved${when ? ` ${when}` : ""}. Clear it to use ${c.env_var} again.`;
    }
    return `Not configured here or in ${c.env_var}.`;
  }

  const keys = creds.filter((c) => c.secret);
  const choices = creds.filter((c) => !c.secret);

  function credentialRow(c: CredentialSetting) {
    // A secret starts blank (there is nothing to prefill); a model name or
    // provider is not secret, so it is prefilled and editable in place.
    const draft = drafts[c.key] ?? (c.secret ? "" : c.masked);
    const isSelect = c.choices.length > 0;
    const saving = savingKey === c.key;

    return (
      <div className="setting-row" key={c.key}>
        <div className="setting-copy">
          <span className="label">
            {c.label}
            {statusPill(c)}
          </span>
          <span className="desc">{c.description}</span>
          <span className="env pill plain mono tiny">{c.env_var}</span>
        </div>

        <div className="setting-control">
          <div className="key-row">
            {isSelect ? (
              <select
                value={draft}
                disabled={saving}
                onChange={(e) => saveCredential(c.key, e.target.value)}
              >
                {c.choices.map((choice) => (
                  <option key={choice} value={choice}>
                    {choice}
                  </option>
                ))}
              </select>
            ) : (
              <input
                type={c.secret && !reveal[c.key] ? "password" : "text"}
                value={draft}
                placeholder={c.secret && c.configured ? c.masked : c.placeholder}
                disabled={saving}
                autoComplete="off"
                spellCheck={false}
                onChange={(e) => setDrafts((prev) => ({ ...prev, [c.key]: e.target.value }))}
              />
            )}

            {c.secret && (
              // Reveals only what is being typed — useful for catching a
              // truncated paste. It cannot show the stored key.
              <button
                className="btn btn-sm btn-icon"
                onClick={() => setReveal((prev) => ({ ...prev, [c.key]: !prev[c.key] }))}
                aria-label={reveal[c.key] ? "Hide what I typed" : "Show what I typed"}
                title={reveal[c.key] ? "Hide what I typed" : "Show what I typed"}
              >
                {reveal[c.key] ? <IconEyeOff size={14} /> : <IconEye size={14} />}
              </button>
            )}

            {!isSelect && (
              <button
                className="btn btn-sm btn-primary"
                onClick={() => saveCredential(c.key, draft)}
                disabled={saving || !draft.trim() || (!c.secret && draft === c.masked)}
              >
                {savedKey === c.key ? <IconCheck size={14} /> : null}
                {saving ? "Saving…" : savedKey === c.key ? "Saved" : "Save"}
              </button>
            )}

            {c.source === "database" && (
              <button
                className="btn btn-sm btn-danger"
                onClick={() => saveCredential(c.key, "")}
                disabled={saving}
                title={`Remove the stored value and fall back to ${c.env_var}`}
              >
                Clear
              </button>
            )}
          </div>
          <span className="note">{note(c)}</span>
        </div>
      </div>
    );
  }

  return (
    <>
      <div className="page-head">
        <h1>Users &amp; settings</h1>
        <p className="lead">
          People, provider credentials, how long recordings and minutes are kept, and
          which optional features are switched on.
        </p>
      </div>

      {error && (
        <div className="alert alert-err" style={{ marginBottom: 16 }}>
          <IconAlert size={16} />
          <span>{error}</span>
        </div>
      )}

      <div className="segmented wide" role="tablist" style={{ marginBottom: 16 }}>
        {SECTIONS.map((s) => (
          <button
            key={s.id}
            role="tab"
            aria-selected={section === s.id}
            onClick={() => setSection(s.id)}
          >
            {s.label}
          </button>
        ))}
      </div>

      {section === "providers" && (
        <>
          <div className="card">
            <div className="card-head">
              <h3>API keys</h3>
              <span className="dim tiny">
                <IconKey size={13} /> encrypted before storing
              </span>
            </div>
            <div className="card-body">
              {keys.length === 0 ? (
                <p className="dim small">No providers available.</p>
              ) : (
                keys.map(credentialRow)
              )}
            </div>
            <div className="card-foot">
              Keys are encrypted with SECRET_KEY and never sent back to the browser — the
              panel shows only the last four characters. A key saved here takes effect on
              the next meeting, with no restart, and overrides the same key in .env.
            </div>
          </div>

          <div className="card">
            <div className="card-head">
              <h3>Provider &amp; models</h3>
            </div>
            <div className="card-body">
              {choices.length === 0 ? (
                <p className="dim small">No options available.</p>
              ) : (
                choices.map(credentialRow)
              )}
            </div>
            <div className="card-foot">
              Changing the model affects new transcriptions only. If transcription starts
              failing with “overloaded”, or English terms come back transliterated into
              Devanagari or Bengali, switch the Gemini model here rather than redeploying.
            </div>
          </div>
        </>
      )}

      {section === "retention" && (
        <div className="card">
          <div className="card-head">
            <h3>Data retention</h3>
            <span className="dim tiny">0 = keep forever</span>
          </div>
          <div className="card-body">
            {retention.length === 0 ? (
              <p className="dim small">No retention settings available.</p>
            ) : (
              retention.map((r) => (
                <div key={r.key} className="setting-row">
                  <div className="setting-copy">
                    <span className="label">{r.label}</span>
                    <span className="desc">{r.description}</span>
                  </div>
                  <div className="setting-control">
                    <span className="with-unit">
                      <input
                        type="number"
                        min={r.minimum}
                        max={r.maximum}
                        value={r.value}
                        onChange={(e) =>
                          setRetention((prev) =>
                            prev.map((x) =>
                              x.key === r.key ? { ...x, value: Number(e.target.value) } : x,
                            ),
                          )
                        }
                        onBlur={(e) => saveRetention(r.key, Number(e.target.value))}
                        style={{ width: 110 }}
                      />
                      <span className="dim small">{r.value === 0 ? "forever" : r.unit}</span>
                    </span>
                  </div>
                </div>
              ))
            )}
          </div>
          <div className="card-foot">
            The retention job runs daily at 03:30. Each tier is independent: deleting
            recordings keeps their transcripts, and deleting transcripts keeps the minutes.
            Meetings themselves are never deleted — the page says what was removed and when.
          </div>
        </div>
      )}

      {section === "features" && (
        <div className="card">
          <div className="card-head">
            <h3>Features</h3>
          </div>
          <div className="card-body">
            {toggles.length === 0 ? (
              <p className="dim small">No settings available.</p>
            ) : (
              toggles.map((t) => (
                <label key={t.key} className="setting-row">
                  <span className="setting-copy">
                    <span className="label">{t.label}</span>
                    <span className="desc">{t.description}</span>
                  </span>
                  <span className="switch">
                    <input
                      type="checkbox"
                      checked={t.enabled}
                      onChange={(e) => flip(t.key, e.target.checked)}
                    />
                  </span>
                </label>
              ))
            )}
          </div>
        </div>
      )}

      {section === "people" && (
        <>
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
                              <span
                                className="bar"
                                style={{ display: "block", marginBottom: 4 }}
                              >
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
            <div className="card-foot">
              {enrollmentOn
                ? "Three samples of about 20 seconds each work better than one 60-second sample — they capture more of the natural variation in how somebody speaks. Use clean speech with no background chatter."
                : "Transcripts label speakers as Speaker 1, 2, 3. Turn on voice enrollment under Features to put real names to them."}
            </div>
          </div>
        </>
      )}

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
