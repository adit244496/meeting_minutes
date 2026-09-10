import { useEffect, useState } from "react";
import { NavLink, Navigate, Outlet, useLocation, useNavigate } from "react-router-dom";

import {
  IconClose,
  IconMeetings,
  IconMenu,
  IconMoon,
  IconSignOut,
  IconSun,
  IconUsers,
} from "./components/icons";
import { api, token, type User } from "./lib/api";
import { useTheme } from "./lib/theme";

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
      <span className="mark">MM</span>
      <span>
        <span className="name">Meeting Minutes</span>
        <span className="sub">English · हिन्दी · বাংলা</span>
      </span>
    </div>
  );
}

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [navOpen, setNavOpen] = useState(false);
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
    <div className="shell">
      {navOpen && (
        <button className="scrim" aria-label="Close menu" onClick={() => setNavOpen(false)} />
      )}

      <aside className="sidebar" data-open={navOpen}>
        <Brand />

        <nav className="sidebar-nav">
          <NavLink to="/meetings" className="nav-item">
            <IconMeetings />
            Meetings
          </NavLink>
          {user.role === "admin" && (
            <NavLink to="/admin" className="nav-item">
              <IconUsers />
              Users &amp; settings
            </NavLink>
          )}
        </nav>

        <div className="sidebar-foot">
          <div className="who-row">
            <span className="avatar">{initials(user.full_name)}</span>
            <span className="who">
              <strong>{user.full_name}</strong>
              <span>{user.email}</span>
            </span>
          </div>
          <div className="row" style={{ gap: 8 }}>
            <button
              className="btn btn-ghost btn-sm grow"
              onClick={cycle}
              title={themeLabel}
              aria-label={themeLabel}
            >
              {resolved === "dark" ? <IconSun size={15} /> : <IconMoon size={15} />}
              {resolved === "dark" ? "Light" : "Dark"}
            </button>
            <button className="btn btn-ghost btn-sm grow" onClick={signOut}>
              <IconSignOut size={15} />
              Sign out
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

        <main className="content">
          <Outlet context={{ user }} />
        </main>
      </div>
    </div>
  );
}
