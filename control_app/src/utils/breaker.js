// Loss / drawdown circuit breaker helpers (GET /api/breaker, status.breaker).

export const BREAKER_ACK_LABEL = 'I have checked the account and want to restart after the breaker tripped';

export const BREAKER_REASON = {
  drawdown: 'Drawdown limit',
  day_loss: 'Daily loss limit',
};

export function breakerReasonLabel(reason) {
  return BREAKER_REASON[reason] || 'Loss limit';
}

/** True when the status payload says the breaker has tripped. */
export function isTripped(status) {
  return !!(status && status.breaker && status.breaker.tripped);
}

/** A share as a percent for the form: 0.15 -> "15", 0.025 -> "2.5". */
export function shareToPercent(v) {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '';
  return String(Math.round(v * 100 * 1e4) / 1e4);
}

/** A percent typed in the form back to a share: "15" -> 0.15 (rounded so 0.15 is not 0.15000000000000002). */
export function percentToShare(text) {
  return Math.round((Number(text) / 100) * 1e6) / 1e6;
}

export const BREAKER_409 = {
  breaker_tripped: 'The loss circuit breaker is tripped. Check the account, then tick the acknowledgement and try again.',
};

/** The 409 text for a failed Start / arbitrage enable, or null for any other error. */
export function breaker409(err) {
  return err && err.status === 409 ? BREAKER_409[String(err.detail || '').split(/[\s:]/)[0]] || null : null;
}
