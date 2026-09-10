import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { IconAlert, IconMoon, IconSun } from "../components/icons";
import { api } from "../lib/api";
import { useTheme } from "../lib/theme";

export default function Login() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const { resolved, cycle } = useTheme();
  const navigate = useNavigate();

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.login(email, password);
      navigate("/meetings");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign-in failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth">
      <button
        className="btn btn-ghost btn-icon"
        onClick={cycle}
        aria-label={resolved === "dark" ? "Switch to light theme" : "Switch to dark theme"}
        style={{ position: "fixed", top: 16, right: 16 }}
      >
        {resolved === "dark" ? <IconSun /> : <IconMoon />}
      </button>

      <form className="auth-card" onSubmit={submit}>
        <div className="brand" style={{ marginBottom: 22 }}>
          <span className="mark">MM</span>
          <span>
            <span className="name">Meeting Minutes</span>
            <span className="sub">Transcripts &amp; minutes</span>
          </span>
        </div>

        <div className="stack" style={{ gap: 12 }}>
          <label className="field">
            <span>Email</span>
            <input
              type="email"
              placeholder="you@company.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoComplete="username"
              autoFocus
              required
            />
          </label>

          <label className="field">
            <span>Password</span>
            <input
              type="password"
              placeholder="••••••••"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              required
            />
          </label>

          {error && (
            <div className="alert alert-err">
              <IconAlert size={16} />
              <span>{error}</span>
            </div>
          )}

          <button className="btn btn-primary btn-block" type="submit" disabled={busy}>
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </div>

        <div className="auth-langs">
          <span className="pill plain">English</span>
          <span className="pill plain">हिन्दी</span>
          <span className="pill plain">বাংলা</span>
        </div>
      </form>
    </div>
  );
}
