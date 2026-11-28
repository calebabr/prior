// What the last Stop did, kept across page changes and reloads. Display only: nothing here sends or delays a Stop.
//
// - A failed, partial or unconfirmed Stop stays until a later successful Stop or until the owner dismisses it.
// - A successful Stop stays (the status card line) until the next Start.
// - A Stop that was sent but never answered (app closed or reloaded meanwhile) shows as unconfirmed.
// Stored in localStorage (no secrets: a time, a count and the server's short error names); memory if blocked.

import { useSyncExternalStore } from 'react';
import { stopToastTitle } from '../utils/overview.js';
import { STOP_COPY } from '../utils/stopCopy.js';

// Fired on window after every Stop request (ok or not) so open pages and the status can reload.
export const STOPPED_EVENT = 'km:stopped';
export const STOP_KEY = 'prior.stop';

// One page load. A pending Stop left by another one was never answered while this app could see it.
const SESSION = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
const EMPTY = { result: null, pending: [] };
const listeners = new Set();
let memory = null;
let broken = false;
let seq = 0;
let cache = { raw: undefined, data: EMPTY, view: null };

function readRaw() {
  if (broken) return memory;
  try {
    return window.localStorage.getItem(STOP_KEY);
  } catch {
    return memory;
  }
}

const isObject = (v) => !!v && typeof v === 'object' && !Array.isArray(v);
const text = (v) => (typeof v === 'string' ? v : '');

// The stored value is untrusted: a damaged one must never break the Stop button that reads it.
function cleanResult(r) {
  if (!isObject(r) || typeof r.kind !== 'string') return null;
  const details = Array.isArray(r.details) ? r.details.filter((d) => typeof d === 'string') : undefined;
  return { ...r, title: text(r.title), text: text(r.text), details };
}

function parse(raw) {
  try {
    const d = raw ? JSON.parse(raw) : null;
    if (!isObject(d)) return EMPTY;
    return { result: cleanResult(d.result), pending: Array.isArray(d.pending) ? d.pending.filter(isObject) : [] };
  } catch {
    return EMPTY;
  }
}

function orphanedBy(data) {
  const after = data.result ? data.result.sentAt : 0;
  return data.pending.filter((p) => p.session !== SESSION && p.sentAt > after);
}

const UNANSWERED = { kind: 'unconfirmed', title: STOP_COPY.unansweredTitle, text: STOP_COPY.unansweredText };

function load() {
  const raw = readRaw();
  if (raw !== cache.raw) {
    const data = parse(raw);
    const orphans = orphanedBy(data);
    const at = Math.max(0, ...orphans.map((p) => p.sentAt));
    cache = { raw, data, view: orphans.length ? { ...UNANSWERED, sentAt: at, resolvedAt: at } : data.result };
  }
  return cache;
}

function save(data) {
  const raw = JSON.stringify(data);
  memory = raw;
  try {
    window.localStorage.setItem(STOP_KEY, raw);
    broken = false;
  } catch {
    broken = true;
  }
  listeners.forEach((fn) => fn());
}

/** The record to show, or null: {kind: 'ok'|'partial'|'failed'|'unconfirmed', title, text, details, ...}. */
export function getStopRecord() {
  return load().view;
}

/** True while this page load has a Stop waiting for its answer. */
export function isStopPending() {
  return load().data.pending.some((p) => p.session === SESSION);
}

export function subscribeStop(fn) {
  listeners.add(fn);
  window.addEventListener('storage', fn);
  return () => {
    listeners.delete(fn);
    window.removeEventListener('storage', fn);
  };
}

export const useStopRecord = () => useSyncExternalStore(subscribeStop, getStopRecord);
export const useStopPending = () => useSyncExternalStore(subscribeStop, isStopPending);

/** Call right before sending a Stop. */
export function beginStop() {
  seq += 1;
  const token = { id: `${SESSION}:${seq}`, sentAt: Date.now() };
  const { data } = load();
  save({ ...data, pending: [...data.pending, { ...token, session: SESSION }] });
  return token;
}

/** Record the answer of that Stop. An answer to an older Stop never replaces what a newer one reported. */
export function resolveStop(token, outcome) {
  const { data } = load();
  const pending = data.pending.filter((p) => p.id !== token.id);
  const newer = data.result && data.result.sentAt > token.sentAt;
  const result = newer ? data.result : { ...outcome, sentAt: token.sentAt, resolvedAt: Date.now() };
  save({ result, pending: pending.filter((p) => p.session === SESSION || p.sentAt > result.sentAt) });
}

/** Another request that can resume trading was already on its way when Stop was tapped, and may have landed. */
export function noteCrossedStop(action) {
  const { data } = load();
  const at = Date.now() + 1; // newer than any Stop already sent, so that Stop's own success cannot hide this
  save({
    ...data,
    result: { kind: 'unconfirmed', title: STOP_COPY.crossedTitle, text: STOP_COPY.crossedText(action), sentAt: at, resolvedAt: at },
  });
}

export function dismissStopRecord() {
  const { data } = load();
  save({ result: null, pending: data.pending.filter((p) => p.session === SESSION) });
}

/** A Start happened here: the "Stopped at" line no longer applies. Failures are never cleared this way. */
export function clearStopSuccess() {
  const { data } = load();
  if (data.result && data.result.kind === 'ok') save({ ...data, result: null });
}

/** A status read that began after the Stop answered says the bot runs again (started elsewhere). */
export function clearStopSuccessIfRestarted(requestedAt) {
  const { data } = load();
  if (data.result && data.result.kind === 'ok' && requestedAt > data.result.resolvedAt) save({ ...data, result: null });
}

// ---------------------------------------------------------------- reading the server's answer

function arbFlag(r) {
  const a = r.arb && typeof r.arb === 'object' ? r.arb : r;
  return a.arb_kill_switch ?? a.kill_switch_arb ?? (r.arb ? a.kill_switch : undefined);
}

export function arbText(r) {
  if (!r) return '';
  const kill = arbFlag(r);
  if (kill === true) return ' Arbitrage stopped.';
  if (kill === false) return ' Arbitrage kill switch NOT confirmed.';
  return '';
}

const countOf = (r) => {
  const n = r.orders_cancelled ?? r.cancelled;
  return typeof n === 'number' && Number.isFinite(n) ? n : null;
};

/**
 * POST /api/bot/stop answered. Success only when the server reports the kill switch set, nothing wrong and (when it
 * says) arbitrage stopped; anything else is partial or unconfirmed.
 */
export function describeStop(response) {
  const r = response && typeof response === 'object' ? response : {};
  const errors = (Array.isArray(r.errors) ? r.errors : r.errors ? [r.errors] : []).map((e) =>
    typeof e === 'string' ? e : JSON.stringify(e),
  );
  const n = countOf(r);
  const cancelled = `cancelled ${n != null ? n : '?'} resting order(s).`;
  const killBad = r.kill_switch === false || arbFlag(r) === false;
  const notStopped = !!r.desired && r.desired !== 'stopped';
  if (r.kill_switch === true && !killBad && !notStopped && !errors.length) {
    return {
      kind: 'ok',
      title: stopToastTitle(r) || 'Stopped. Kill switch set.',
      text: `Bot is ${r.desired || 'stopped'}; ${cancelled}${arbText(r)}`,
      stoppedAt: r.stopped_at || null,
      cancelled: n,
    };
  }
  const details = errors.map((e) => `Not done: ${e}`);
  if (notStopped || (r.kill_switch !== true && r.kill_switch !== false)) {
    return {
      kind: 'unconfirmed',
      title: STOP_COPY.unconfirmedTitle,
      text: `${STOP_COPY.unconfirmedText}${arbText(r)}`,
      details,
    };
  }
  // Names the server uses for failures that are not about cancelling. Anything else counts as a cancel problem.
  const cancelProblem = errors.some((e) => !/^(store|kill_switch|arb_kill_switch):/.test(e));
  const killProblem = killBad || errors.some((e) => /^(arb_)?kill_switch:/.test(e));
  const title = cancelProblem
    ? STOP_COPY.partialCancelTitle
    : killProblem
      ? STOP_COPY.partialKillTitle
      : STOP_COPY.partialOtherTitle;
  return {
    kind: 'partial',
    title,
    text: `Server ${cancelled}${arbText(r)} ${STOP_COPY.checkKalshi}`,
    details,
  };
}

/** The request failed: nothing is known to be stopped. */
export function describeFailure(err) {
  const refused = !!err && err.status === 401;
  const why = String((err && err.message) || '').trim();
  return {
    kind: 'failed',
    title: refused ? STOP_COPY.refusedTitle : STOP_COPY.failedTitle,
    text: `${why && !/[.!?]$/.test(why) ? `${why}.` : why} ${STOP_COPY.failedNext}`.trim(),
  };
}
