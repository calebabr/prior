// Every sentence about what Stop did, what it can reach and what a mode switch leaves behind, in one place.
//
// What these rest on (server behaviour; change the words here if the server changes):
// - Stop cancels the bot's resting orders in every mode that has keys (never the owner's own orders), and reports
//   any it could not cancel. A Stop that arrives while a Start or a switch is being checked wins.
// - Switching mode cancels nothing. Live resting orders have no expiry: they stay open and can fill until they are
//   cancelled (Stop, disarming the maker, or Kalshi) or their market closes. Filled positions stay until settlement.

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
  /** Under the buttons of a stopped bot. */
  alreadyStopped: () => 'Already stopped. Stop again cancels any leftover bot orders.',
};

/** A Start, mode switch or arbitrage change refused because a Stop came first (409 detail -> words). */
export const STOP_WON = {
  stopped_during_start: 'A Stop arrived first, so Stop won. Nothing was turned on.',
  stop_in_progress: 'A Stop is still being saved. Try again in a few seconds.',
};

/** Warning in the "Switch to DEMO?" dialog, one paragraph per entry. */
export const TO_DEMO_WARNING = [
  'Tap Stop BEFORE switching, while still in live. Switching cancels nothing.',
  'Otherwise live orders stay open and can fill until you tap Stop, cancel them on Kalshi, or the market closes.',
  'Filled positions stay until settlement.',
];
/** Arbitrage has its own Live setting, which a mode switch leaves alone. */
export const TO_DEMO_ARB = 'Arbitrage has its own Live setting. Switching does not change it.';
export const TO_DEMO_ARB_LIVE = 'Arbitrage is live now and keeps trading real money after the switch.';
