// Every sentence about what Stop did, what it can reach and what a mode switch leaves behind, in one place.
//
// What these rest on (server behaviour; change the words here if the server changes):
// - Stop cancels the bot's resting orders in the CURRENT mode only. It never claims anything about the other mode.
// - Switching mode cancels nothing. Live resting orders have no expiry: they stay open and can fill until they are
//   cancelled on Kalshi or their market closes. Filled positions stay until settlement.

const modeWord = (mode) => (mode === 'live' ? 'live' : 'demo');

export const STOP_COPY = {
  checkKalshi: 'Check Kalshi for resting orders and cancel any left, then tap Stop again.',
  failedTitle: 'Stop did NOT reach the server',
  refusedTitle: 'Stop was refused: sign in again',
  failedNext: 'Tap Stop again. If it keeps failing, cancel resting orders in Kalshi.',
  partialCancelTitle: 'Stopped, but not everything was cancelled',
  partialKillTitle: 'Stopped, but a kill switch was not confirmed',
  partialOtherTitle: 'Stopped, but the server reported a problem',
  unconfirmedTitle: 'Stop was sent, but not confirmed',
  unconfirmedText: 'The server did not confirm the kill switch. Check Kalshi for resting orders, then tap Stop again.',
  unansweredTitle: 'Stop was sent, but no answer was seen',
  unansweredText: 'The app closed or reloaded before the server answered. Check Kalshi for resting orders, then tap Stop again.',
  crossedTitle: 'Check that the bot is stopped',
  crossedText: (action) => `"${action}" was already sent when you tapped Stop and may have gone through. Tap Stop again.`,
  /** Under the buttons of a stopped bot. Stop only reaches the current mode, and says so. */
  alreadyStopped: (mode) => `Already stopped. Stop again cancels any leftover ${modeWord(mode)} orders.`,
};

/** Warning in the "Switch to DEMO?" dialog, one paragraph per entry. */
export const TO_DEMO_WARNING = [
  'Tap Stop BEFORE switching, while still in live. Switching cancels nothing.',
  'Otherwise live orders stay open and can fill until you cancel them on Kalshi or the market closes. Stop in demo does not cancel them.',
  'Filled positions stay until settlement.',
];
/** Arbitrage has its own Live setting, which a mode switch leaves alone. */
export const TO_DEMO_ARB = 'Arbitrage has its own Live setting. Switching does not change it.';
export const TO_DEMO_ARB_LIVE = 'Arbitrage is live now and keeps trading real money after the switch.';
