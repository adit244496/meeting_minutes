import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { IconAlert, IconEye, IconEyeOff, IconMoon, IconSun } from "../components/icons";
import { api } from "../lib/api";
import { useTheme } from "../lib/theme";

export default function Login() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [show, setShow] = useState(false);
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
        className="btn btn-ghost btn-icon auth-theme"
        onClick={cycle}
        aria-label={resolved === "dark" ? "Switch to light theme" : "Switch to dark theme"}
      >
        {resolved === "dark" ? <IconSun /> : <IconMoon />}
      </button>

      <form className="auth-card" onSubmit={submit}>
        <div className="auth-head">
          <span className="mark">NM</span>
          <h1>
            Neo <b>Minutes</b>
          </h1>
          <p>Meeting transcripts and minutes, written for you.</p>
        </div>

        <div className="stack" style={{ gap: 14 }}>
          <label className="field">
            <span>Email</span>
            <input
              type="email"
              placeholder="you@company.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoComplete="username"
              autoCapitalize="none"
              autoCorrect="off"
              inputMode="email"
              autoFocus
              required
            />
          </label>

          <label className="field">
            <span>Password</span>
            <span className="input-affix">
              <input
                type={show ? "text" : "password"}
                placeholder="Your password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                required
              />
              <button
                type="button"
                className="affix-btn"
                onClick={() => setShow((v) => !v)}
                aria-label={show ? "Hide password" : "Show password"}
                aria-pressed={show}
                title={show ? "Hide password" : "Show password"}
              >
                {show ? <IconEyeOff size={17} /> : <IconEye size={17} />}
              </button>
            </span>
          </label>

          {error && (
            <div className="alert alert-err">
              <IconAlert size={16} />
              <span>{error}</span>
            </div>
          )}

          <button className="btn btn-primary btn-block auth-submit" type="submit" disabled={busy}>
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </div>

        <p className="auth-foot">Ask an administrator if you need an account.</p>
      </form>
    </div>
  );
}
