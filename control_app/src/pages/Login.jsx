import { useId, useState } from 'react';
import { Navigate } from 'react-router-dom';
import { useAuth } from '../context/AuthContext.jsx';
import WakingBanner from '../components/WakingBanner.jsx';
import PriorLogo from '../components/PriorLogo.jsx';
import CodeInput from '../components/CodeInput.jsx';
import { IconEye, IconEyeOff } from '../components/Icons.jsx';
import { isMockMode } from '../services/api.js';
import { APP_NAME, TAGLINE_LEAD, TAGLINE_ACCENT } from '../utils/routes.js';

function loginMessage(err) {
  if (!err) return null;
  if (err.status === 401) return 'Wrong username or password.';
  if (err.status === 423 || err.detail === 'account_locked') return 'Account locked after too many failures. Try again in 15 minutes.';
  if (err.status === 429) return 'Too many attempts. Wait a few minutes and try again.';
  return err.message || 'Sign-in failed.';
}

function codeMessage(err) {
  if (!err) return null;
  if (err.status === 401 || err.status === 403) return 'Code Rejected: Wrong, expired or already used. Wait for the next code.';
  if (err.status === 423 || err.detail === 'account_locked') return 'Account locked after too many failures. Try again in 15 minutes.';
  if (err.status === 429) return 'Too many attempts. Wait a few minutes and try again.';
  return err.message || 'Verification failed.';
}

export default function Login() {
  const { isAuthenticated, mfaPending, mfaUsername, login, verify, cancelMfa, expired } = useAuth();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const ids = useId();
  const userId = `${ids}-user`;
  const passId = `${ids}-pass`;

  if (isAuthenticated) return <Navigate to="/" replace />;

  async function onPassword(e) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await login(username.trim(), password);
      setPassword('');
      setShowPassword(false);
    } catch (err) {
      setError(loginMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function onCode(e) {
    e.preventDefault();
    if (busy || !/^\d{6}$/.test(code)) return;
    setBusy(true);
    setError(null);
    try {
      await verify(code);
    } catch (err) {
      setCode('');
      setError(codeMessage(err));
      setBusy(false);
    }
  }

  return (
    <div className="login-page">
      <div className="login-line" aria-hidden="true" />
      <WakingBanner />
      <div className="login-card card">
        <div className="login-brand">
          <PriorLogo size={48} />
          <h1 className="login-title">{APP_NAME}</h1>
          <p className="login-tagline" data-testid="login-tagline">
            {TAGLINE_LEAD} <span className="login-tagline-accent">{TAGLINE_ACCENT}</span>
          </p>
        </div>
        {isMockMode() && <p className="hint center login-mock">Mock Mode: Any username/password, any 6-digit code except 000000.</p>}
        {expired && !mfaPending && (
          <p className="notice notice-stale">
            {expired === 'logged_out_all'
              ? 'All sessions were logged out, on every device. Sign in again.'
              : expired === 'session_expired'
                ? 'Your Session Ended: All sessions were logged out or your login was reset. Please sign in again.'
                : 'Your session ended. Please sign in again.'}
          </p>
        )}

        {!mfaPending ? (
          <form onSubmit={onPassword} className="stack">
            <div className="field">
              <label htmlFor={userId} className="field-label">
                Username
              </label>
              <input
                id={userId}
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoComplete="username"
                autoCapitalize="none"
                autoCorrect="off"
                required
              />
            </div>
            <div className="field">
              <label htmlFor={passId} className="field-label">
                Password
              </label>
              <div className="pw-wrap">
                {/* A real password field: browsers and password managers treat it as one. "Show" only
                    switches the type to text while it is ticked, and it resets after sign-in. */}
                <input
                  id={passId}
                  type={showPassword ? 'text' : 'password'}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete="current-password"
                  autoCapitalize="none"
                  autoCorrect="off"
                  spellCheck={false}
                  required
                />
                <button
                  type="button"
                  className="pw-eye"
                  onClick={() => setShowPassword((v) => !v)}
                  aria-label={showPassword ? 'Hide password text' : 'Reveal password text'}
                  aria-pressed={showPassword}
                >
                  {showPassword ? <IconEyeOff /> : <IconEye />}
                </button>
              </div>
              <label className="field check login-show">
                <input type="checkbox" checked={showPassword} onChange={(e) => setShowPassword(e.target.checked)} />
                <span>Show Password</span>
              </label>
            </div>
            {error && (
              <p className="error-text" role="alert">
                {error}
              </p>
            )}
            <button type="submit" className="btn primary block" disabled={busy || !username || !password}>
              {busy ? 'Signing in…' : 'Continue'}
            </button>
          </form>
        ) : (
          <form onSubmit={onCode} className="stack">
            <p>
              Enter the 6-digit code from your authenticator app{mfaUsername ? ` for ${mfaUsername}` : ''}.
            </p>
            <div className="field">
              <span className="field-label" aria-hidden="true">
                2FA Code
              </span>
              <CodeInput value={code} onChange={setCode} disabled={busy} autoFocus />
            </div>
            {error && (
              <p className="error-text" role="alert">
                {error}
              </p>
            )}
            <button type="submit" className="btn primary block" disabled={busy || !/^\d{6}$/.test(code)}>
              {busy ? 'Verifying…' : 'Sign in'}
            </button>
            <button
              type="button"
              className="btn block"
              onClick={() => {
                setError(null);
                setCode('');
                cancelMfa();
              }}
              disabled={busy}
            >
              Back
            </button>
          </form>
        )}
        {/* No demo / live wording: the build cannot know the server's mode. The signed-in header shows it. */}
        <p className="login-trust">Protected by 2FA</p>
      </div>
    </div>
  );
}
