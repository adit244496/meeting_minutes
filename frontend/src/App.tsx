import { useEffect, useState } from "react";
import { Link, Navigate, Outlet, useNavigate } from "react-router-dom";

import { api, token, type User } from "./lib/api";

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const navigate = useNavigate();

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

  if (loading) return <div className="app muted" style={{ padding: 48 }}>Loading…</div>;
  if (!user) return <Navigate to="/login" replace />;

  return (
    <div className="app">
      <header className="topbar">
        <span className="brand">Meeting Minutes</span>
        <nav>
          <Link to="/meetings">Meetings</Link>
          {user.role === "admin" && <Link to="/admin">Users &amp; Voices</Link>}
          <span className="who">{user.full_name}</span>
          <button
            className="small"
            onClick={() => {
              token.clear();
              navigate("/login");
            }}
          >
            Sign out
          </button>
        </nav>
      </header>
      <Outlet context={{ user }} />
    </div>
  );
}
