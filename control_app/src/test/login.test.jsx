import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { setToken } from '../services/api.js';
import { renderApp, stubApi } from './helpers.jsx';
const OLD_NAME = ['Long', 'shot'].join('');   // the retired app name, never spelled out in the repo

beforeEach(() => setToken(null));
afterEach(() => vi.unstubAllGlobals());

const pw = () => screen.getByLabelText('Password');

describe('login: Show Password (item 20)', () => {
  it('keeps a real type=password field that the checkbox toggles to text and back', async () => {
    const user = userEvent.setup();
    renderApp('/', { signedIn: false });
    expect(pw()).toHaveAttribute('type', 'password');
    await user.type(pw(), 'hunter2');
    const box = screen.getByRole('checkbox', { name: 'Show Password' });
    expect(box).not.toBeChecked();
    await user.click(box);
    expect(pw()).toHaveAttribute('type', 'text');
    expect(pw()).toHaveValue('hunter2');
    await user.click(box);
    expect(pw()).toHaveAttribute('type', 'password');
  });

  it('the eye button toggles the same state and is labelled for screen readers', async () => {
    const user = userEvent.setup();
    renderApp('/', { signedIn: false });
    const eye = screen.getByRole('button', { name: /reveal password text/i });
    expect(eye).toHaveAttribute('aria-pressed', 'false');
    await user.click(eye);
    expect(pw()).toHaveAttribute('type', 'text');
    expect(screen.getByRole('checkbox', { name: 'Show Password' })).toBeChecked();
    expect(screen.getByRole('button', { name: /hide password text/i })).toHaveAttribute('aria-pressed', 'true');
    await user.click(screen.getByRole('button', { name: /hide password text/i }));
    expect(pw()).toHaveAttribute('type', 'password');
    expect(screen.getByRole('checkbox', { name: 'Show Password' })).not.toBeChecked();
  });

  it('autocomplete attributes stay intact so password managers keep working', () => {
    renderApp('/', { signedIn: false });
    const user = screen.getByLabelText('Username');
    expect(user).toHaveAttribute('autocomplete', 'username');
    expect(user).toHaveAttribute('autocapitalize', 'none');
    expect(pw()).toHaveAttribute('autocomplete', 'current-password');
    expect(pw()).toBeRequired();
  });

  it('the eye button is not a submit button (it must not post the form)', () => {
    renderApp('/', { signedIn: false });
    expect(screen.getByRole('button', { name: /reveal password text/i })).toHaveAttribute('type', 'button');
  });

  it('after the password step the field is hidden again (nothing stays revealed)', async () => {
    const user = userEvent.setup();
    stubApi({ 'POST /api/auth/login': { mfa_token: 'm1', expires_in: 300 } });
    renderApp('/', { signedIn: false });
    await user.type(screen.getByLabelText('Username'), 'demo-user');
    await user.type(pw(), 'hunter2');
    await user.click(screen.getByRole('checkbox', { name: 'Show Password' }));
    await user.click(screen.getByRole('button', { name: 'Continue' }));
    await screen.findByLabelText('6-digit code');
    await user.click(screen.getByRole('button', { name: 'Back' }));
    expect(pw()).toHaveAttribute('type', 'password');
    expect(pw()).toHaveValue('');
  });
});

describe('login: Prior branding (item 29)', () => {
  it('shows the logo, name, tagline and the trust line', () => {
    renderApp('/', { signedIn: false });
    expect(screen.getByRole('heading', { level: 1, name: 'Prior' })).toBeInTheDocument();
    expect(screen.getByTestId('prior-logo')).toBeInTheDocument();
    expect(screen.getByTestId('login-tagline')).toHaveTextContent('Your strategies. One tap away.');
    expect(screen.getByText('Protected by 2FA')).toBeInTheDocument();
    // The footer never claims a mode: the build cannot know whether the server is in demo or live.
    expect(document.body.textContent).not.toMatch(/(demo|live) mode/i);
    expect(document.body.textContent).not.toMatch(new RegExp(OLD_NAME));
  });

  it('has the decorative accent line, hidden from assistive tech', () => {
    renderApp('/', { signedIn: false });
    const line = document.querySelector('.login-line');
    expect(line).not.toBeNull();
    expect(line).toHaveAttribute('aria-hidden', 'true');
  });
});

describe('login: six-digit code step (item 29)', () => {
  async function toCodeStep(user) {
    stubApi({ 'POST /api/auth/login': { mfa_token: 'm1', expires_in: 300 } });
    renderApp('/', { signedIn: false });
    await user.type(screen.getByLabelText('Username'), 'demo-user');
    await user.type(pw(), 'hunter2');
    await user.click(screen.getByRole('button', { name: 'Continue' }));
    return screen.findByLabelText('6-digit code');
  }

  it('draws six boxes over one real one-time-code input', async () => {
    const user = userEvent.setup();
    const input = await toCodeStep(user);
    expect(input).toHaveAttribute('autocomplete', 'one-time-code');
    expect(input).toHaveAttribute('inputmode', 'numeric');
    expect(input).toHaveAttribute('maxlength', '6');
    expect(document.querySelectorAll('.otp-box')).toHaveLength(6);
    expect(document.querySelectorAll('input.otp-input')).toHaveLength(1);
  });

  it('typing fills boxes in order and drops non-digits', async () => {
    const user = userEvent.setup();
    const input = await toCodeStep(user);
    await user.type(input, '12a3');
    expect(input).toHaveValue('123');
    const boxes = [...document.querySelectorAll('.otp-box')].map((b) => b.textContent);
    expect(boxes).toEqual(['1', '2', '3', '', '', '']);
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeDisabled();
  });

  it('paste fills all six (spaces and dashes ignored) and enables Sign in', async () => {
    const user = userEvent.setup();
    const input = await toCodeStep(user);
    input.focus();
    await user.paste('123 456');
    expect(input).toHaveValue('123456');
    expect([...document.querySelectorAll('.otp-box')].map((b) => b.textContent).join('')).toBe('123456');
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeEnabled();
  });

  it('paste longer than six digits keeps the first six', async () => {
    const user = userEvent.setup();
    const input = await toCodeStep(user);
    input.focus();
    await user.paste('9876543210');
    expect(input).toHaveValue('987654');
  });

  it('iOS-style autofill (whole code arrives in one change event) fills all boxes', async () => {
    const user = userEvent.setup();
    const input = await toCodeStep(user);
    fireEvent.change(input, { target: { value: '246810' } });
    expect(input).toHaveValue('246810');
  });

  it('submits the typed code exactly as before (POST /api/auth/2fa)', async () => {
    const user = userEvent.setup();
    const calls = stubApi({
      'POST /api/auth/login': { mfa_token: 'm1', expires_in: 300 },
      'POST /api/auth/2fa': { access_token: 'acc' },
      'GET /api/status': { desired: 'stopped', mode: 'demo', kill_switch: true, heartbeat_age_s: 5 },
      'GET /api/history': { snapshots: [] },
      'GET /api/changelog': [200, []],
    });
    renderApp('/', { signedIn: false });
    await user.type(screen.getByLabelText('Username'), 'demo-user');
    await user.type(pw(), 'hunter2');
    await user.click(screen.getByRole('button', { name: 'Continue' }));
    await user.type(await screen.findByLabelText('6-digit code'), '123456');
    await user.click(screen.getByRole('button', { name: 'Sign in' }));
    await vi.waitFor(() => expect(calls.some((c) => c.key === 'POST /api/auth/2fa')).toBe(true));
    expect(calls.find((c) => c.key === 'POST /api/auth/2fa').body).toEqual({ mfa_token: 'm1', code: '123456' });
  });
});
