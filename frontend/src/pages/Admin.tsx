import { useEffect, useMemo, useRef, useState } from "react";

import {
  IconAlert,
  IconCheck,
  IconClock,
  IconClose,
  IconEye,
  IconEyeOff,
  IconKey,
  IconPlus,
  IconSearch,
  IconShield,
  IconSliders,
  IconTrash,
  IconUpload,
  IconUsers,
} from "../components/icons";
import {
  api,
  type CredentialSetting,
  type Department,
  type FeatureToggle,
  type MinutesModelCatalog,
  type RetentionSetting,
  type Role,
  type User,
} from "../lib/api";

// Matches MIN_ENROLLMENT_SECONDS / RECOMMENDED_ENROLLMENT_SECONDS on the server.
const RECOMMENDED_SECONDS = 60;

type Section = "people" | "departments" | "providers" | "retention" | "features";

const SECTIONS: { id: Section; label: string; hint: string; icon: JSX.Element }[] = [
  { id: "people", label: "People", hint: "Accounts and roles", icon: <IconUsers /> },
  { id: "departments", label: "Departments", hint: "Who can see which meetings", icon: <IconShield /> },
  { id: "providers", label: "AI providers", hint: "API keys and models", icon: <IconKey /> },
  { id: "retention", label: "Data retention", hint: "How long data is kept", icon: <IconClock /> },
  { id: "features", label: "Features", hint: "Optional capabilities", icon: <IconSliders /> },
];

const TRANSCRIPTION_PROVIDERS: Record<string, { name: string; blurb: string; keyId: string; modelId?: string }> = {
  gemini: {
    name: "Google Gemini",
    blurb: "Recommended. Best with mixed-language speech and lowest cost.",
    keyId: "gemini_api_key",
    modelId: "gemini_model",
  },
  elevenlabs: {
    name: "ElevenLabs Scribe",
    blurb: "Dedicated speech recognition with precise word timings.",
    keyId: "elevenlabs_api_key",
  },
  sarvam: {
    name: "Sarvam AI",
    blurb: "Speech recognition focused on Indian languages.",
    keyId: "sarvam_api_key",
  },
};

const MINUTES_PROVIDERS: Record<string, { name: string; blurb: string; keyId: string; modelId: string }> = {
  anthropic: {
    name: "Anthropic Claude",
    blurb: "Careful, well-structured minutes from long transcripts.",
    keyId: "anthropic_api_key",
    modelId: "anthropic_model",
  },
  openai: {
    name: "OpenAI",
    blurb: "GPT models with strict structured output.",
    keyId: "openai_api_key",
    modelId: "openai_model",
  },
};

const RETENTION_PRESETS: { days: number; label: string }[] = [
  { days: 7, label: "7 days" },
  { days: 14, label: "14 days" },
  { days: 30, label: "1 month" },
  { days: 90, label: "3 months" },
  { days: 180, label: "6 months" },
  { days: 365, label: "1 year" },
  { days: 0, label: "Forever" },
];

function initials(name: string) {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? "")
    .join("");
}

function describeDays(days: number) {
  return RETENTION_PRESETS.find((p) => p.days === days)?.label ?? `${days} days`;
}

export default function Admin() {
  const [section, setSection] = useState<Section>("people");
  const [error, setError] = useState("");
  const [toast, setToast] = useState("");
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  function notify(message: string) {
    setToast(message);
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(""), 2400);
  }

  useEffect(() => () => {
    if (toastTimer.current) clearTimeout(toastTimer.current);
  }, []);

  function fail(err: unknown, fallback: string) {
    setError(err instanceof Error ? err.message : fallback);
  }

  const current = SECTIONS.find((s) => s.id === section)!;

  return (
    <>
      <div className="page-head">
        <h1>Settings</h1>
        <p className="lead">Manage people, AI providers, data retention and optional features.</p>
      </div>

      <div className="settings-layout">
        <nav className="settings-nav" aria-label="Settings sections">
          {SECTIONS.map((s) => (
            <button
              key={s.id}
              className="settings-nav-item"
              aria-current={section === s.id ? "page" : undefined}
              onClick={() => {
                setSection(s.id);
                setError("");
              }}
            >
              {s.icon}
              <span>
                <span className="label">{s.label}</span>
                <span className="hint">{s.hint}</span>
              </span>
            </button>
          ))}
        </nav>

        <div className="settings-panel">
          <div className="section-title">
            <h2>{current.label}</h2>
          </div>

          {error && (
            <div className="alert alert-err" style={{ marginBottom: 16 }}>
              <IconAlert size={16} />
              <span className="grow">{error}</span>
              <button className="icon-link" onClick={() => setError("")} aria-label="Dismiss">
                <IconClose size={14} />
              </button>
            </div>
          )}

          {section === "people" && <PeopleSection onError={fail} notify={notify} />}
          {section === "departments" && <DepartmentsSection onError={fail} notify={notify} />}
          {section === "providers" && <ProvidersSection onError={fail} notify={notify} />}
          {section === "retention" && <RetentionSection onError={fail} notify={notify} />}
          {section === "features" && <FeaturesSection onError={fail} notify={notify} />}
        </div>
      </div>

      {toast && (
        <div className="toast" role="status">
          <IconCheck size={15} />
          {toast}
        </div>
      )}
    </>
  );
}

type SectionProps = {
  onError: (err: unknown, fallback: string) => void;
  notify: (message: string) => void;
};

// --------------------------------------------------------------------------
// People
// --------------------------------------------------------------------------

function PeopleSection({ onError, notify }: SectionProps) {
  const [users, setUsers] = useState<User[]>([]);
  const [departments, setDepartments] = useState<Department[]>([]);
  const [enrollmentOn, setEnrollmentOn] = useState(false);
  const [filter, setFilter] = useState("");
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState(false);
  const [enrolling, setEnrolling] = useState<string | null>(null);
  const [editingDepts, setEditingDepts] = useState<string | null>(null);
  const [form, setForm] = useState({
    full_name: "",
    email: "",
    password: "",
    role: "member" as Role,
    department_ids: [] as string[],
  });
  const fileInput = useRef<HTMLInputElement>(null);

  async function refresh() {
    try {
      setUsers(await api.listUsers());
    } catch (err) {
      onError(err, "Could not load people");
    }
  }

  useEffect(() => {
    refresh();
    api.listDepartments().then(setDepartments).catch(() => undefined);
    api
      .listToggles()
      .then((t) => setEnrollmentOn(t.find((x) => x.key === "voice_enrollment_enabled")?.enabled ?? false))
      .catch(() => undefined);
  }, []);

  async function saveDepartments(user: User, ids: string[]) {
    const previous = users;
    setUsers((prev) =>
      prev.map((u) =>
        u.id === user.id
          ? { ...u, departments: departments.filter((d) => ids.includes(d.id)) }
          : u,
      ),
    );
    try {
      await api.setUserDepartments(user.id, ids);
      notify(
        ids.length === 0
          ? `${user.full_name} is no longer in any department`
          : `${user.full_name}: ${ids.length} department${ids.length === 1 ? "" : "s"}`,
      );
    } catch (err) {
      setUsers(previous);
      onError(err, "Could not change departments");
    }
  }

  const visible = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return users;
    return users.filter((u) => u.full_name.toLowerCase().includes(q) || u.email.toLowerCase().includes(q));
  }, [users, filter]);

  const admins = users.filter((u) => u.role === "admin").length;

  async function createUser(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      await api.createUser({
        email: form.email.trim(),
        full_name: form.full_name.trim(),
        password: form.password || undefined,
        role: form.role,
        department_ids: form.department_ids,
      });
      notify(`${form.full_name.trim()} added`);
      setForm({ full_name: "", email: "", password: "", role: "member", department_ids: [] });
      setAdding(false);
      await refresh();
    } catch (err) {
      onError(err, "Could not add that person");
    } finally {
      setBusy(false);
    }
  }

  async function uploadSample(file: File) {
    if (!enrolling) return;
    setBusy(true);
    try {
      await api.enrollVoice(enrolling, file);
      notify("Voice sample added");
      await refresh();
    } catch (err) {
      onError(err, "Enrollment failed");
    } finally {
      setBusy(false);
      setEnrolling(null);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  return (
    <>
      <div className="stat-row">
        <div className="stat">
          <span className="stat-value">{users.length}</span>
          <span className="stat-label">People</span>
        </div>
        <div className="stat">
          <span className="stat-value">{admins}</span>
          <span className="stat-label">Administrators</span>
        </div>
        <div className="stat">
          <span className="stat-value">{users.length - admins}</span>
          <span className="stat-label">Members</span>
        </div>
      </div>

      {adding && (
        <div className="card">
          <div className="card-head">
            <h3>Add a person</h3>
            <button className="icon-link" onClick={() => setAdding(false)} aria-label="Close">
              <IconClose size={16} />
            </button>
          </div>
          <form onSubmit={createUser}>
            <div className="card-body">
              <div className="form-grid cols-2">
                <label className="field">
                  <span>Full name</span>
                  <input
                    type="text"
                    placeholder="Priya Sharma"
                    value={form.full_name}
                    onChange={(e) => setForm({ ...form, full_name: e.target.value })}
                    autoFocus
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
                  <span>Password</span>
                  <input
                    type="password"
                    placeholder="At least 8 characters"
                    value={form.password}
                    onChange={(e) => setForm({ ...form, password: e.target.value })}
                    minLength={8}
                  />
                  <span className="hint">Optional. Leave blank for people who only appear in transcripts.</span>
                </label>
                <label className="field">
                  <span>Role</span>
                  <select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as Role })}>
                    <option value="member">Member — can record and view meetings</option>
                    <option value="admin">Administrator — can also change settings</option>
                  </select>
                </label>
                {form.role === "member" && departments.length > 0 && (
                  <div className="field span-2">
                    <span>Departments</span>
                    <div className="dept-picker">
                      {departments.map((d) => (
                        <label key={d.id} className="dept-option">
                          <input
                            type="checkbox"
                            checked={form.department_ids.includes(d.id)}
                            onChange={(e) =>
                              setForm({
                                ...form,
                                department_ids: e.target.checked
                                  ? [...form.department_ids, d.id]
                                  : form.department_ids.filter((id) => id !== d.id),
                              })
                            }
                          />
                          <span>{d.name}</span>
                        </label>
                      ))}
                    </div>
                    <span className="hint">
                      They will see the meetings of these departments. Without one, they see only the meetings
                      they record themselves.
                    </span>
                  </div>
                )}
              </div>
            </div>
            <div className="card-actions">
              <button type="button" className="btn btn-sm" onClick={() => setAdding(false)} disabled={busy}>
                Cancel
              </button>
              <button className="btn btn-sm btn-primary" type="submit" disabled={busy}>
                {busy ? "Adding…" : "Add person"}
              </button>
            </div>
          </form>
        </div>
      )}

      <div className="card">
        <div className="toolbar">
          <div className="search-field">
            <IconSearch size={15} />
            <input
              type="search"
              placeholder="Search by name or email"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
          </div>
          {!adding && (
            <button className="btn btn-sm btn-primary" onClick={() => setAdding(true)}>
              <IconPlus size={15} />
              Add person
            </button>
          )}
        </div>

        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Role</th>
                <th>Departments</th>
                {enrollmentOn && <th>Voice profile</th>}
                {enrollmentOn && <th aria-label="Actions" />}
              </tr>
            </thead>
            <tbody>
              {visible.map((u) => {
                const seconds = u.enrolled_seconds ?? 0;
                const pct = Math.min(100, (seconds / RECOMMENDED_SECONDS) * 100);
                return (
                  <tr key={u.id}>
                    <td data-label="Name">
                      <span className="person">
                        <span className="avatar">{initials(u.full_name)}</span>
                        <span className="person-text">
                          <strong>{u.full_name}</strong>
                          <span>{u.email}</span>
                        </span>
                      </span>
                    </td>
                    <td data-label="Role">
                      <span className={`badge ${u.role === "admin" ? "badge-accent" : ""}`}>
                        {u.role === "admin" ? "Administrator" : "Member"}
                      </span>
                    </td>
                    <td data-label="Departments" className="cell-stack">
                      {u.role === "admin" ? (
                        <span className="dim tiny">Every meeting</span>
                      ) : editingDepts === u.id ? (
                        <div className="dept-picker">
                          {departments.length === 0 && (
                            <span className="dim tiny">No departments yet — add one first.</span>
                          )}
                          {departments.map((d) => {
                            const on = u.departments.some((x) => x.id === d.id);
                            return (
                              <label key={d.id} className="dept-option">
                                <input
                                  type="checkbox"
                                  checked={on}
                                  onChange={(e) =>
                                    saveDepartments(
                                      u,
                                      e.target.checked
                                        ? [...u.departments.map((x) => x.id), d.id]
                                        : u.departments.map((x) => x.id).filter((id) => id !== d.id),
                                    )
                                  }
                                />
                                <span>{d.name}</span>
                              </label>
                            );
                          })}
                          <button className="btn btn-sm" onClick={() => setEditingDepts(null)}>
                            Done
                          </button>
                        </div>
                      ) : (
                        <button
                          className="dept-chips"
                          onClick={() => setEditingDepts(u.id)}
                          title="Change departments"
                        >
                          {u.departments.length === 0 ? (
                            <span className="badge badge-warn">None — own meetings only</span>
                          ) : (
                            u.departments.map((d) => (
                              <span key={d.id} className="badge">
                                {d.name}
                              </span>
                            ))
                          )}
                        </button>
                      )}
                    </td>
                    {enrollmentOn && (
                      <td data-label="Voice profile" className="cell-stack" style={{ minWidth: 180 }}>
                        <span style={{ display: "block", width: "100%" }}>
                          <span className="bar" style={{ display: "block", marginBottom: 4 }}>
                            <i style={{ width: `${pct}%` }} />
                          </span>
                          <span className="dim tiny">
                            {u.voiceprint_count ?? 0} sample{u.voiceprint_count === 1 ? "" : "s"} ·{" "}
                            {seconds.toFixed(0)}s of {RECOMMENDED_SECONDS}s
                          </span>
                        </span>
                      </td>
                    )}
                    {enrollmentOn && (
                      <td data-label="" style={{ textAlign: "right" }}>
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
              {visible.length === 0 && (
                <tr>
                  <td colSpan={enrollmentOn ? 5 : 3}>
                    <div className="empty">
                      <p className="big">{filter ? "No one matches that search" : "No people yet"}</p>
                    </div>
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        <div className="card-foot">
          Departments decide which meetings somebody can open — administrators see all of them.{" "}
          {enrollmentOn
            ? "For the best voice profile, add three clean samples of about 20 seconds each."
            : "Speakers are labelled Speaker 1, 2, 3. Turn on voice enrollment under Features to identify people by name."}
        </div>
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

// --------------------------------------------------------------------------
// Departments
// --------------------------------------------------------------------------

function DepartmentsSection({ onError, notify }: SectionProps) {
  const [departments, setDepartments] = useState<Department[]>([]);
  const [users, setUsers] = useState<User[]>([]);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  async function refresh() {
    const [d, u] = await Promise.all([api.listDepartments(), api.listUsers()]);
    setDepartments(d);
    setUsers(u);
  }

  useEffect(() => {
    refresh()
      .catch((err) => onError(err, "Could not load departments"))
      .finally(() => setLoaded(true));
  }, []);

  async function create(event: React.FormEvent) {
    event.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    try {
      await api.createDepartment(name.trim());
      notify(`${name.trim()} added`);
      setName("");
      await refresh();
    } catch (err) {
      onError(err, "Could not add that department");
    } finally {
      setBusy(false);
    }
  }

  async function rename(d: Department, next: string) {
    setRenaming(null);
    if (!next.trim() || next.trim() === d.name) return;
    try {
      await api.renameDepartment(d.id, next.trim());
      notify(`Renamed to ${next.trim()}`);
      await refresh();
    } catch (err) {
      onError(err, "Could not rename that department");
    }
  }

  async function remove(d: Department) {
    const warning =
      d.meeting_count > 0
        ? `Delete ${d.name}? Its ${d.meeting_count} meeting${d.meeting_count === 1 ? "" : "s"} will stay, ` +
          "but only administrators will be able to open them until they are reassigned."
        : `Delete ${d.name}?`;
    if (!window.confirm(warning)) return;
    try {
      await api.deleteDepartment(d.id);
      notify(`${d.name} deleted`);
      await refresh();
    } catch (err) {
      onError(err, "Could not delete that department");
    }
  }

  async function setMembership(user: User, departmentId: string, join: boolean) {
    const ids = user.departments.map((d) => d.id);
    const next = join ? [...ids, departmentId] : ids.filter((id) => id !== departmentId);
    const previous = users;
    setUsers((prev) =>
      prev.map((u) =>
        u.id === user.id ? { ...u, departments: departments.filter((d) => next.includes(d.id)) } : u,
      ),
    );
    try {
      await api.setUserDepartments(user.id, next);
      setDepartments((prev) =>
        prev.map((d) =>
          d.id === departmentId ? { ...d, member_count: d.member_count + (join ? 1 : -1) } : d,
        ),
      );
    } catch (err) {
      setUsers(previous);
      onError(err, "Could not change membership");
    }
  }

  const members = users.filter((u) => u.role !== "admin");

  return (
    <>
      <div className="card">
        <div className="card-head card-head-lg">
          <div>
            <h2 className="card-title">Who can see which meetings</h2>
            <p className="card-sub">
              A meeting belongs to one department, and only that department's people can open it —
              its transcript, minutes, recording and downloads alike. Administrators see everything.
              A meeting with no department stays private to whoever recorded it.
            </p>
          </div>
        </div>
        <div className="card-body">
          <form className="key-row" onSubmit={create}>
            <input
              type="text"
              placeholder="Finance, Operations, Legal…"
              value={name}
              maxLength={120}
              disabled={busy}
              onChange={(e) => setName(e.target.value)}
              aria-label="New department name"
            />
            <button className="btn btn-sm btn-primary" type="submit" disabled={busy || !name.trim()}>
              <IconPlus size={15} />
              Add department
            </button>
          </form>
        </div>
      </div>

      {!loaded && <p className="dim small">Loading…</p>}

      {loaded && departments.length === 0 && (
        <div className="empty">
          <p className="big">No departments yet</p>
          <p className="dim small">
            Until you add one, every meeting stays visible only to administrators and the person who
            recorded it.
          </p>
        </div>
      )}

      {departments.map((d) => (
        <div className="card" key={d.id}>
          <div className="card-head">
            {renaming === d.id ? (
              <input
                type="text"
                defaultValue={d.name}
                autoFocus
                maxLength={120}
                onBlur={(e) => rename(d, e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                  if (e.key === "Escape") setRenaming(null);
                }}
              />
            ) : (
              <h3>
                {d.name}
                <span className="dim tiny" style={{ marginLeft: 8, fontWeight: 400 }}>
                  {d.member_count} member{d.member_count === 1 ? "" : "s"} · {d.meeting_count} meeting
                  {d.meeting_count === 1 ? "" : "s"}
                </span>
              </h3>
            )}
            <span className="row-actions">
              <button className="btn btn-sm" onClick={() => setRenaming(d.id)}>
                Rename
              </button>
              <button className="btn btn-sm btn-danger" onClick={() => remove(d)}>
                <IconTrash size={14} />
                Delete
              </button>
            </span>
          </div>
          <div className="card-body">
            {members.length === 0 ? (
              <p className="dim small">Everyone is an administrator, so nobody needs a department yet.</p>
            ) : (
              <div className="dept-picker">
                {members.map((u) => (
                  <label key={u.id} className="dept-option">
                    <input
                      type="checkbox"
                      checked={u.departments.some((x) => x.id === d.id)}
                      onChange={(e) => setMembership(u, d.id, e.target.checked)}
                    />
                    <span>
                      {u.full_name} <span className="dim tiny">{u.email}</span>
                    </span>
                  </label>
                ))}
              </div>
            )}
          </div>
        </div>
      ))}
    </>
  );
}

// --------------------------------------------------------------------------
// AI providers
// --------------------------------------------------------------------------

function ProvidersSection({ onError, notify }: SectionProps) {
  const [creds, setCreds] = useState<CredentialSetting[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    api
      .listCredentials()
      .then(setCreds)
      .catch((err) => onError(err, "Could not load provider settings"))
      .finally(() => setLoaded(true));
  }, []);

  const byKey = useMemo(() => new Map(creds.map((c) => [c.key, c])), [creds]);

  async function save(key: string, value: string, message: string) {
    const updated = await api.setCredential(key, value);
    setCreds((prev) => prev.map((c) => (c.key === key ? updated : c)));
    notify(message);
  }

  if (!loaded) return <p className="dim small">Loading…</p>;

  const asr = byKey.get("asr_provider")?.masked || "gemini";
  const minutesProvider = byKey.get("minutes_provider")?.masked || "anthropic";

  return (
    <>
      <ProviderGroup
        title="Transcription"
        description="Turns meeting audio into a timestamped transcript with speakers."
        providers={TRANSCRIPTION_PROVIDERS}
        selected={asr}
        byKey={byKey}
        onSelect={(id) =>
          save("asr_provider", id, `Transcription now uses ${TRANSCRIPTION_PROVIDERS[id].name}`).catch((err) =>
            onError(err, "Could not change the provider"),
          )
        }
        save={save}
        onError={onError}
      />

      <ProviderGroup
        title="Meeting minutes"
        description="Writes the summary, decisions and action items from the transcript."
        providers={MINUTES_PROVIDERS}
        selected={minutesProvider}
        byKey={byKey}
        onSelect={(id) =>
          save("minutes_provider", id, `Minutes now use ${MINUTES_PROVIDERS[id].name}`).catch((err) =>
            onError(err, "Could not change the provider"),
          )
        }
        save={save}
        onError={onError}
      />

      <RegenerationModelsCard byKey={byKey} save={save} onError={onError} />

      <p className="tiny dim" style={{ marginTop: 4 }}>
        <IconKey size={12} /> Keys are encrypted before they are stored and are never shown again — only the last
        four characters. Changes apply to the next meeting without a restart.
      </p>
    </>
  );
}

function ProviderGroup({
  title,
  description,
  providers,
  selected,
  byKey,
  onSelect,
  save,
  onError,
}: {
  title: string;
  description: string;
  providers: Record<string, { name: string; blurb: string; keyId: string; modelId?: string }>;
  selected: string;
  byKey: Map<string, CredentialSetting>;
  onSelect: (id: string) => void;
  save: (key: string, value: string, message: string) => Promise<void>;
  onError: (err: unknown, fallback: string) => void;
}) {
  const active = providers[selected] ?? Object.values(providers)[0];
  const key = byKey.get(active.keyId);
  const model = active.modelId ? byKey.get(active.modelId) : undefined;

  return (
    <div className="card">
      <div className="card-head card-head-lg">
        <div>
          <h2 className="card-title">{title}</h2>
          <p className="card-sub">{description}</p>
        </div>
      </div>
      <div className="card-body">
        <div className="choice-grid" role="radiogroup" aria-label={`${title} provider`}>
          {Object.entries(providers).map(([id, p]) => {
            const configured = byKey.get(p.keyId)?.configured ?? false;
            return (
              <button
                key={id}
                role="radio"
                aria-checked={selected === id}
                className="choice"
                onClick={() => selected !== id && onSelect(id)}
              >
                <span className="choice-head">
                  <span className="radio-dot" />
                  <strong>{p.name}</strong>
                </span>
                <span className="choice-blurb">{p.blurb}</span>
                <span className={`status-dot ${configured ? "ok" : ""}`}>
                  {configured ? "Key configured" : "No key"}
                </span>
              </button>
            );
          })}
        </div>

        <div className="provider-fields">
          {key && <SecretField setting={key} providerName={active.name} save={save} onError={onError} />}
          {model && <ModelField setting={model} save={save} onError={onError} />}
        </div>
      </div>
    </div>
  );
}

function sourceBadge(c: CredentialSetting) {
  if (c.source === "database") return <span className="badge badge-ok">Saved</span>;
  if (c.source === "environment") return <span className="badge">From server config</span>;
  return <span className="badge badge-warn">Not set</span>;
}

function SecretField({
  setting,
  providerName,
  save,
  onError,
}: {
  setting: CredentialSetting;
  providerName: string;
  save: (key: string, value: string, message: string) => Promise<void>;
  onError: (err: unknown, fallback: string) => void;
}) {
  const [draft, setDraft] = useState("");
  const [reveal, setReveal] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setDraft("");
    setReveal(false);
  }, [setting.key]);

  async function submit(value: string, message: string) {
    setBusy(true);
    try {
      await save(setting.key, value, message);
      setDraft("");
      setReveal(false);
    } catch (err) {
      onError(err, "Could not save the key");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="field-block">
      <div className="field-block-head">
        <label htmlFor={`f-${setting.key}`}>{providerName} API key</label>
        {sourceBadge(setting)}
      </div>
      <form
        className="key-row"
        onSubmit={(e) => {
          e.preventDefault();
          if (draft.trim()) submit(draft, `${providerName} key saved`);
        }}
      >
        <div className="input-affix">
          <input
            id={`f-${setting.key}`}
            type={reveal ? "text" : "password"}
            value={draft}
            placeholder={setting.configured ? `Current key ${setting.masked}` : setting.placeholder}
            disabled={busy}
            autoComplete="off"
            spellCheck={false}
            onChange={(e) => setDraft(e.target.value)}
          />
          <button
            type="button"
            className="affix-btn"
            onClick={() => setReveal((r) => !r)}
            aria-label={reveal ? "Hide key" : "Show key"}
            title={reveal ? "Hide key" : "Show key"}
          >
            {reveal ? <IconEyeOff size={15} /> : <IconEye size={15} />}
          </button>
        </div>
        <button className="btn btn-sm btn-primary" type="submit" disabled={busy || !draft.trim()}>
          {busy ? "Saving…" : setting.configured ? "Replace" : "Save"}
        </button>
        {setting.source === "database" && (
          <button
            type="button"
            className="btn btn-sm btn-danger"
            disabled={busy}
            onClick={() => submit("", `${providerName} key removed`)}
          >
            Remove
          </button>
        )}
      </form>
      <p className="field-help">
        {setting.description}
        {setting.source === "environment" && " A key saved here overrides the one in the server config."}
        {setting.source === "database" && " Removing it falls back to the server config, if one is set there."}
      </p>
    </div>
  );
}

function ModelField({
  setting,
  save,
  onError,
}: {
  setting: CredentialSetting;
  save: (key: string, value: string, message: string) => Promise<void>;
  onError: (err: unknown, fallback: string) => void;
}) {
  const suggestions = setting.suggestions ?? [];
  const known = (value: string) => suggestions.includes(value);
  const [draft, setDraft] = useState(setting.masked);
  const [custom, setCustom] = useState(Boolean(setting.masked) && !known(setting.masked));
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setDraft(setting.masked);
    setCustom(Boolean(setting.masked) && !known(setting.masked));
  }, [setting.key, setting.masked]);

  async function commit(value: string) {
    setBusy(true);
    try {
      await save(setting.key, value, `Model set to ${value}`);
    } catch (err) {
      onError(err, "Could not save the model");
    } finally {
      setBusy(false);
    }
  }

  const dirty = draft.trim() !== setting.masked;

  return (
    <div className="field-block">
      <div className="field-block-head">
        <label htmlFor={`f-${setting.key}`}>{setting.label}</label>
      </div>
      <form
        className="key-row"
        onSubmit={(e) => {
          e.preventDefault();
          if (dirty && draft.trim()) commit(draft.trim());
        }}
      >
        {suggestions.length > 0 && (
          <select
            id={`f-${setting.key}`}
            className="mono-input"
            value={custom ? "__custom" : draft}
            disabled={busy}
            onChange={(e) => {
              if (e.target.value === "__custom") {
                setCustom(true);
                setDraft("");
                return;
              }
              setCustom(false);
              setDraft(e.target.value);
              if (e.target.value !== setting.masked) commit(e.target.value);
            }}
          >
            {!setting.masked && <option value="">Choose a model…</option>}
            {suggestions.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
            <option value="__custom">Other (type a model id)…</option>
          </select>
        )}
        {(custom || suggestions.length === 0) && (
          <>
            <input
              id={suggestions.length ? `f-${setting.key}-custom` : `f-${setting.key}`}
              type="text"
              className="mono-input"
              value={draft}
              placeholder={setting.placeholder}
              disabled={busy}
              spellCheck={false}
              onChange={(e) => setDraft(e.target.value)}
            />
            <button className="btn btn-sm" type="submit" disabled={busy || !dirty || !draft.trim()}>
              {busy ? "Saving…" : "Update"}
            </button>
          </>
        )}
      </form>
      <p className="field-help">{setting.description} Applies to new meetings only.</p>
    </div>
  );
}

function RegenerationModelsCard({
  byKey,
  save,
  onError,
}: {
  byKey: Map<string, CredentialSetting>;
  save: (key: string, value: string, message: string) => Promise<void>;
  onError: (err: unknown, fallback: string) => void;
}) {
  const [data, setData] = useState<MinutesModelCatalog | null>(null);
  const [busy, setBusy] = useState(false);
  const keysSignature = ["anthropic_api_key", "openai_api_key", "minutes_models"]
    .map((k) => `${byKey.get(k)?.configured}:${byKey.get(k)?.masked}`)
    .join("|");

  useEffect(() => {
    api
      .minutesModelCatalog()
      .then(setData)
      .catch((err) => onError(err, "Could not load the model list"));
  }, [keysSignature]);

  if (!data) return null;

  async function toggle(id: string, on: boolean) {
    if (!data) return;
    const next = data.models.filter((m) => (m.id === id ? on : m.enabled)).map((m) => m.id);
    if (next.length === 0) {
      onError(new Error("Keep at least one model available for regeneration"), "");
      return;
    }
    setBusy(true);
    try {
      await save("minutes_models", next.join(","), "Regeneration models updated");
    } catch (err) {
      onError(err, "Could not save the model list");
    } finally {
      setBusy(false);
    }
  }

  const groups: [string, string][] = [
    ["openai", "OpenAI"],
    ["anthropic", "Anthropic Claude"],
  ];

  return (
    <div className="card">
      <div className="card-head card-head-lg">
        <div>
          <h2 className="card-title">Models for regenerating minutes</h2>
          <p className="card-sub">
            Members pick from these on a meeting page. A model is only offered when its provider has a key.
            {data.using_default && " Showing the default: GPT-4o mini and Claude Sonnet 5."}
          </p>
        </div>
      </div>
      <div className="card-body model-checklist">
        {groups.map(([provider, name]) => {
          const models = data.models.filter((m) => m.provider === provider);
          const hasKey = models[0]?.key_configured ?? false;
          return (
            <fieldset key={provider} className="model-group" disabled={busy}>
              <legend>
                {name}
                <span className={`status-dot ${hasKey ? "ok" : ""}`}>{hasKey ? "Key configured" : "No key"}</span>
              </legend>
              {models.map((m) => (
                <label key={m.id} className={`model-option ${m.enabled && !hasKey ? "inactive" : ""}`}>
                  <input type="checkbox" checked={m.enabled} onChange={(e) => toggle(m.id, e.target.checked)} />
                  <span className="grow">
                    {m.label} <span className="mono dim tiny">{m.id}</span>
                  </span>
                  {m.enabled && !hasKey && <span className="tiny dim">Hidden until a key is added</span>}
                </label>
              ))}
            </fieldset>
          );
        })}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// Retention
// --------------------------------------------------------------------------

const RETENTION_COPY: Record<string, { title: string; body: string }> = {
  retention_days_recordings: {
    title: "Recordings",
    body: "Audio files are the largest item. Transcripts and minutes stay when a recording is deleted.",
  },
  retention_days_transcripts: {
    title: "Transcripts",
    body: "The word-by-word transcript. Minutes stay when a transcript is deleted.",
  },
  retention_days_minutes: {
    title: "Meeting minutes",
    body: "Minutes and their full edit history.",
  },
};

function RetentionSection({ onError, notify }: SectionProps) {
  const [items, setItems] = useState<RetentionSetting[]>([]);
  const [custom, setCustom] = useState<Record<string, boolean>>({});

  useEffect(() => {
    api
      .listRetention()
      .then((rows) => {
        setItems(rows);
        setCustom(
          Object.fromEntries(rows.map((r) => [r.key, !RETENTION_PRESETS.some((p) => p.days === r.value)])),
        );
      })
      .catch((err) => onError(err, "Could not load retention settings"));
  }, []);

  async function save(key: string, value: number) {
    const previous = items;
    setItems((prev) => prev.map((r) => (r.key === key ? { ...r, value } : r)));
    try {
      await api.setRetention(key, value);
      const title = RETENTION_COPY[key]?.title ?? "Setting";
      notify(value === 0 ? `${title} are kept forever` : `${title} are kept for ${describeDays(value)}`);
    } catch (err) {
      setItems(previous);
      onError(err, "Could not save retention setting");
    }
  }

  return (
    <div className="card">
      <div className="card-head card-head-lg">
        <div>
          <h2 className="card-title">Automatic deletion</h2>
          <p className="card-sub">
            Choose how long each kind of data is kept. Download anything you need first — deletion runs daily at
            03:30 and cannot be undone.
          </p>
        </div>
      </div>
      <div className="card-body">
        {items.length === 0 && <p className="dim small">Loading…</p>}
        {items.map((r) => {
          const copy = RETENTION_COPY[r.key] ?? { title: r.label, body: r.description };
          const isCustom = custom[r.key];
          return (
            <div key={r.key} className="setting-row">
              <div className="setting-copy">
                <span className="label">{copy.title}</span>
                <span className="desc">{copy.body}</span>
              </div>
              <div className="setting-control">
                <select
                  value={isCustom ? "custom" : String(r.value)}
                  onChange={(e) => {
                    if (e.target.value === "custom") {
                      setCustom((c) => ({ ...c, [r.key]: true }));
                      return;
                    }
                    setCustom((c) => ({ ...c, [r.key]: false }));
                    save(r.key, Number(e.target.value));
                  }}
                  aria-label={`Keep ${copy.title.toLowerCase()} for`}
                >
                  {RETENTION_PRESETS.map((p) => (
                    <option key={p.days} value={p.days}>
                      {p.days === 0 ? "Keep forever" : `Keep for ${p.label}`}
                    </option>
                  ))}
                  <option value="custom">Custom…</option>
                </select>
                {isCustom && (
                  <span className="with-unit">
                    <input
                      type="number"
                      min={Math.max(1, r.minimum)}
                      max={r.maximum}
                      defaultValue={r.value || 60}
                      onBlur={(e) => {
                        const value = Number(e.target.value);
                        if (value >= 1 && value !== r.value) save(r.key, value);
                      }}
                      onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
                      style={{ width: 110 }}
                    />
                    <span className="dim small">days</span>
                  </span>
                )}
                <span className="note">
                  {r.value === 0 ? "Never deleted" : `Deleted ${describeDays(r.value)} after the meeting`}
                </span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// Features
// --------------------------------------------------------------------------

function FeaturesSection({ onError, notify }: SectionProps) {
  const [toggles, setToggles] = useState<FeatureToggle[]>([]);

  useEffect(() => {
    api
      .listToggles()
      .then(setToggles)
      .catch((err) => onError(err, "Could not load features"));
  }, []);

  async function flip(t: FeatureToggle, enabled: boolean) {
    setToggles((prev) => prev.map((x) => (x.key === t.key ? { ...x, enabled } : x)));
    try {
      await api.setToggle(t.key, enabled);
      notify(`${t.label} ${enabled ? "turned on" : "turned off"}`);
    } catch (err) {
      setToggles((prev) => prev.map((x) => (x.key === t.key ? { ...x, enabled: !enabled } : x)));
      onError(err, "Could not update that feature");
    }
  }

  return (
    <div className="card">
      <div className="card-body">
        {toggles.length === 0 && <p className="dim small">Loading…</p>}
        {toggles.map((t) => (
          <label key={t.key} className="setting-row">
            <span className="setting-copy">
              <span className="label">{t.label}</span>
              <span className="desc">{t.description}</span>
            </span>
            <span className="switch">
              <input
                type="checkbox"
                role="switch"
                checked={t.enabled}
                onChange={(e) => flip(t, e.target.checked)}
              />
            </span>
          </label>
        ))}
      </div>
      <div className="card-foot">Changes take effect immediately for everyone.</div>
    </div>
  );
}
