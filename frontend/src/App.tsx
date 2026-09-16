import { useEffect, useState } from "react";
import { Link, NavLink, Navigate, Outlet, useLocation, useNavigate } from "react-router-dom";

import {
  IconChevronLeft,
  IconClose,
  IconMeetings,
  IconMenu,
  IconMoon,
  IconSignOut,
  IconSliders,
  IconStop,
  IconSun,
} from "./components/icons";
import { api, token, type User } from "./lib/api";
import { formatElapsed, RecorderProvider, useRecorder } from "./lib/recorder";
import { useTheme } from "./lib/theme";

/** Shown on every page while a recording runs or saves, so Stop is never out of reach. */
function RecordingBar() {
  const r = useRecorder();
  const location = useLocation();
  if (r.status === "idle" && !r.canRetryUpload) return null;

  const onMeetingPage = r.meetingId && location.pathname === `/meetings/${r.meetingId}`;
  return (
    <div className={`recording-bar ${r.status === "idle" ? "is-error" : ""}`} role="status" aria-live="polite">
      {r.status === "idle" ? (
        <>
          <span className="grow">{r.error || "The recording could not be uploaded."}</span>
          <button className="btn btn-sm btn-primary" onClick={r.retryUpload}>
            Retry upload
          </button>
        </>
      ) : (
        <>
          <span className="live-dot" aria-hidden="true" />
          <span className="recording-bar-text grow">
            <strong>
              {r.status === "starting"
                ? "Starting recording…"
                : r.status === "saving"
                  ? "Saving the recording…"
                  : `Recording ${formatElapsed(r.seconds)}`}
            </strong>
            {r.title && <span className="dim"> · {r.title}</span>}
            {r.status === "recording" && r.liveOn && <span className="dim"> · live transcript on</span>}
          </span>
          {r.meetingId && !onMeetingPage && r.status === "recording" && (
            <Link className="small" to={`/meetings/${r.meetingId}`}>
              View live transcript
            </Link>
          )}
          {r.status === "recording" && (
            <button className="btn btn-sm btn-rec" onClick={r.stop}>
              <IconStop size={14} />
              Stop
            </button>
          )}
        </>
      )}
    </div>
  );
}

function initials(name: string) {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? "")
    .join("");
}

function Brand() {
  return (
    <div className="brand">
      <span className="mark">NM</span>
      <span className="name">
        Neo <b>Minutes</b>
      </span>
    </div>
  );
}

const COLLAPSED_KEY = "nm.sidebar.collapsed";

function readCollapsed() {
  try {
    return localStorage.getItem(COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [navOpen, setNavOpen] = useState(false);
  // Desktop only: the sidebar shrinks to an icon rail. Phones use the drawer.
  const [collapsed, setCollapsed] = useState(readCollapsed);

  function toggleCollapsed() {
    setCollapsed((c) => {
      try {
        localStorage.setItem(COLLAPSED_KEY, c ? "0" : "1");
      } catch {
        // Private mode: the choice just is not remembered.
      }
      return !c;
    });
  }
  const { resolved, cycle } = useTheme();
  const navigate = useNavigate();
  const location = useLocation();

  useEffect(() => {
    if (!token.get()) {
      setLoading(false);
      return;
    }
    api
      .me()
      .then(setUser)
      .catch(() => token.clear())
      .finally(() => setLoading(false));
  }, []);

  // Close the drawer on navigation, or it stays open over the new page.
  useEffect(() => {
    setNavOpen(false);
  }, [location.pathname]);

  // Lock the page behind the drawer. Without this the content scrolls under the
  // finger on a phone while the drawer sits still, which feels broken.
  useEffect(() => {
    document.body.classList.toggle("nav-open", navOpen);
    return () => document.body.classList.remove("nav-open");
  }, [navOpen]);

  if (loading) {
    return (
      <div className="auth">
        <p className="dim">Loading…</p>
      </div>
    );
  }
  if (!user) return <Navigate to="/login" replace />;

  function signOut() {
    token.clear();
    navigate("/login");
  }

  const themeLabel = resolved === "dark" ? "Switch to light theme" : "Switch to dark theme";

  return (
    <RecorderProvider>
    <div className={`shell ${collapsed ? "is-collapsed" : ""}`}>
      {navOpen && (
        <button className="scrim" aria-label="Close menu" onClick={() => setNavOpen(false)} />
      )}

      <aside className="sidebar" data-open={navOpen}>
        <div className="sidebar-head">
          <Brand />
          <button
            className="collapse-btn"
            onClick={toggleCollapsed}
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          >
            <IconChevronLeft size={16} />
          </button>
        </div>

        <nav className="sidebar-nav">
          <span className="nav-section">Workspace</span>
          <NavLink to="/meetings" className="nav-item" title="Meetings">
            <IconMeetings />
            <span className="nav-label">Meetings</span>
          </NavLink>
          {user.role === "admin" && (
            <NavLink to="/admin" className="nav-item" title="Settings">
              <IconSliders />
              <span className="nav-label">Settings</span>
            </NavLink>
          )}
        </nav>

        <div className="sidebar-foot">
          <div className="who-row" title={`${user.full_name} · ${user.email}`}>
            <span className="avatar">{initials(user.full_name)}</span>
            <span className="who">
              <strong>{user.full_name}</strong>
              <span>{user.email}</span>
            </span>
          </div>
          <div className="row" style={{ gap: 8 }}>
            <button
              className="btn btn-ghost btn-sm grow side-btn"
              onClick={cycle}
              title={themeLabel}
              aria-label={themeLabel}
            >
              {resolved === "dark" ? <IconSun size={15} /> : <IconMoon size={15} />}
              <span className="nav-label">{resolved === "dark" ? "Light" : "Dark"}</span>
            </button>
            <button className="btn btn-ghost btn-sm grow side-btn" onClick={signOut} title="Sign out" aria-label="Sign out">
              <IconSignOut size={15} />
              <span className="nav-label">Sign out</span>
            </button>
          </div>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <button
            className="btn btn-ghost btn-icon"
            onClick={() => setNavOpen((open) => !open)}
            aria-label={navOpen ? "Close menu" : "Open menu"}
            aria-expanded={navOpen}
          >
            {navOpen ? <IconClose /> : <IconMenu />}
          </button>
          <Brand />
          <button
            className="btn btn-ghost btn-icon"
            style={{ marginLeft: "auto" }}
            onClick={cycle}
            aria-label={themeLabel}
          >
            {resolved === "dark" ? <IconSun /> : <IconMoon />}
          </button>
        </header>

        <RecordingBar />
        <main className="content">
          <Outlet context={{ user }} />
        </main>
      </div>
    </div>
    </RecorderProvider>
  );
}
