// Words for the Live Arming page. Nothing here changes what arming does.
//   demo       the app (maker) mode is demo
//   arbMode    GET /api/arming -> arbitrage.mode ('live' = arbitrage is set to Live, armed or not)
//   arbOn      status.arb.state is not 'stopped'

/** The note above the checklist. */
export function armNote({ demo, arbLive }) {
  if (!demo) return 'Live is on: arming lets a strategy place real orders now.';
  if (arbLive) return 'Arbitrage is on and set to Live: arming it lets it place real orders now. Checks are for live.';
  return 'Arming only takes effect when live mode is on. You can arm now while in demo mode, then switch to live. Checks are for live.';
}

/** The line in the "Arm X?" dialog: {text, danger} (danger = real orders can follow). */
export function armWarning(strategy, { demo, arbMode, arbOn }) {
  const idle = { text: 'This arms the strategy for live. It does not start live trading.', danger: false };
  if (strategy === 'arbitrage') {
    if (arbMode === 'live') {
      return arbOn
        ? { text: 'Arbitrage is on and set to Live: arming lets it place real orders now.', danger: true }
        : { text: 'Arbitrage is set to Live but off: after arming, turning it on trades real money.', danger: true };
    }
    return demo ? idle : { text: 'Arbitrage is not set to Live, so arming alone places no orders.', danger: false };
  }
  return demo ? idle : { text: 'Live is on: arming lets this strategy place real orders now.', danger: true };
}

/**
 * The "No kill switch active" check failed: why, and the steps that clear it from the stopped state.
 * Stop sets both switches. Only Start clears the maker's; only turning arbitrage on clears the arbitrage one (it
 * comes back as log only). In Live, Start is refused until Maker is armed, and arming needs both switches clear, so
 * that case goes through Demo.
 */
export function killSwitchHelp(check, { demo, makerArmed }) {
  const detail = String((check && check.detail) || '');
  let maker = /maker kill switch: true/i.test(detail);
  let arb = /arbitrage kill switch: true/i.test(detail);
  if (!maker && !arb) maker = arb = true; // detail not understood: give both steps
  const which = maker && arb ? 'Both kill switches are' : maker ? 'The maker kill switch is' : 'The arbitrage kill switch is';
  const cause = `${which} on. Stop turns both on. To clear:`;
  const arbStep = 'Open Arbitrage, Controls, and tap Turn on. It comes back as log only.';
  const again = 'Wait for one run, then tap Run checks again.';
  if (maker && !demo && !makerArmed) {
    return {
      cause,
      steps: [
        'On Mode, switch to Demo (Start is refused in Live until Maker is armed).',
        'On Home, tap Start.',
        ...(arb ? [arbStep] : []),
        again,
        'Arm, then tap Stop. Arming stays on.',
        'On Mode, switch to Live while the bot is stopped.',
        'On Home, tap Start. This starts real trading.',
        ...(arb ? ['To run arbitrage again, turn it on and tap Go live.'] : []),
      ],
    };
  }
  return {
    cause,
    steps: [
      ...(maker ? [demo ? 'On Home, tap Start. This starts the bot in demo.' : 'On Home, tap Start. In Live this starts real trading.'] : []),
      ...(arb ? [arbStep] : []),
      again,
    ],
  };
}
