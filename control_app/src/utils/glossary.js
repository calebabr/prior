// Single source of plain-English definitions (docs/ANALYTICS_CONTRACT.md "Glossary / help icons").
// Each entry: {id, term, short, body, group, related?, aliases?} (aliases: other words people search for). ids are kebab-case and used as
// /terminology#<id> anchors and as the HelpIcon `term` prop. A test checks that every
// HelpIcon term used in src/ exists here, so add the entry before using a new id.

import { titleCase } from './format.js';

const RAW_GROUPS = [
  { id: 'orders', title: 'Orders & quoting' },
  { id: 'money', title: 'Money & risk' },
  { id: 'performance', title: 'Performance & statistics' },
  { id: 'strategy', title: 'Strategy' },
  { id: 'safety', title: 'Safety & operations' },
];

// titleCase() plus two glossary-specific fixes: "vs" stays lowercase and code identifiers
// (max_open_frac, run_budget) are left exactly as written.
function titleTerm(raw) {
  const raws = String(raw).split(/(\s+)/);
  return titleCase(raw)
    .split(/(\s+)/)
    .map((tok, i) => (/_/.test(raws[i]) ? raws[i] : tok.replace(/^Vs$/, 'vs')))
    .join('');
}

export const GROUPS = RAW_GROUPS.map((g) => ({ ...g, title: titleCase(g.title) }));

const RAW_GLOSSARY = [
  // ---------------------------------------------------------------- Orders & quoting
  {
    id: 'contract',
    group: 'orders',
    term: 'Contract',
    aliases: ['share'],
    short: 'One yes/no share that pays $1 if it is right and $0 if it is wrong.',
    body:
      'Kalshi sells contracts on questions like "Will event X happen by Friday?". Each contract pays exactly $1.00 at settlement if its side (YES or NO) turns out right, and $0 if wrong. The price, between 1¢ and 99¢, is roughly the market\'s probability. Buying NO at 60¢ means paying $0.60 to win $1.00 back (a 40¢ gain) if the answer is no.',
    related: ['series', 'settlement', 'fee'],
  },
  {
    id: 'series',
    group: 'orders',
    term: 'Series',
    short: 'A family of daily markets, such as KXDEMOB.',
    body:
      'A series is a recurring set of markets with the same question each day, identified by a ticker prefix such as KXDEMOA. Each day a series has several brackets (markets). Settings let you choose which of the allowed series the bot quotes.',
    related: ['contract', 'cell'],
  },
  {
    id: 'quote',
    group: 'orders',
    term: 'Quote',
    aliases: ['bid', 'order'],
    short: 'The bid the bot currently has resting in a market.',
    body:
      'A quote is the price and size the bot is offering to buy at. On every run the bot looks at each market it trades, decides whether a side of that market is inside one of the strategy\'s cells, and places, keeps or cancels its bids as the strategy decides.',
    related: ['resting-bid', 're-quote', 'best-bid'],
  },
  {
    id: 'resting-bid',
    group: 'orders',
    term: 'Resting bid',
    aliases: ['resting order', 'limit order'],
    short: 'A buy order that waits in the order book until someone sells into it.',
    body:
      'A resting bid sits in Kalshi\'s order book at a fixed price and waits. It only fills when another trader chooses to sell at that price. Stop cancels the bot\'s resting bids.',
    related: ['post-only', 'best-bid', 'fill', 'stop'],
  },
  {
    id: 'best-bid',
    group: 'orders',
    term: 'Best bid',
    short: 'The highest price anyone is currently offering to buy at.',
    body:
      'The best bid is the top of the buy side of the order book. Bids at the same price wait in line by arrival time.',
    related: ['spread', 'queue-position', 'resting-bid'],
  },
  {
    id: 'spread',
    group: 'orders',
    term: 'Spread',
    short: 'The gap between the best bid and the cheapest offer to sell.',
    body:
      'If the best bid for NO is 52¢ and the cheapest seller asks 54¢, the spread is 2¢. Buying immediately means paying the ask (crossing the spread); waiting at the bid saves the spread but may not fill.',
    related: ['best-bid', 'maker-taker'],
  },
  {
    id: 'maker-taker',
    group: 'orders',
    term: 'Maker vs taker',
    aliases: ['maker', 'taker'],
    short: 'Makers post orders and wait; takers trade instantly against them.',
    body:
      'A maker adds an order to the book and waits. A taker trades right away against an existing order and usually pays a higher fee. On Analytics, "maker" fills are the bot\'s resting orders; "taker" fills are arbitrage legs or manual trades. The strategy\'s statistics apply to maker fills only.',
    related: ['post-only', 'fee', 'arbitrage'],
  },
  {
    id: 'post-only',
    group: 'orders',
    term: 'Post-only',
    short: 'An order flag that refuses to trade instantly, so the bot is always the maker.',
    body:
      'A post-only order is rejected by Kalshi if it would match immediately. So it always rests in the book as a maker order.',
    related: ['maker-taker', 'resting-bid'],
  },
  {
    id: 'fill',
    group: 'orders',
    term: 'Fill',
    aliases: ['filled', 'execution'],
    short: 'When someone sells into the bot\'s bid and it actually buys contracts.',
    body:
      'A fill is a completed trade. A bid can fill fully or partly (some of its contracts). Each fill shows the side, price, count and fee.',
    related: ['fill-rate', 'position', 'settlement'],
  },
  {
    id: 're-quote',
    group: 'orders',
    term: 'Re-quote',
    aliases: ['requote', 'reprice', 're-price'],
    short: 'Cancelling and replacing a bid because the best bid moved.',
    body:
      'Each run the bot checks its resting bids. If the best bid has moved, or the market has left its cell, the old bid is cancelled and a new one placed (or none). A re-quote puts the bid at the back of the line at the new price, so frequent re-quotes can lower the fill rate.',
    related: ['quote', 'queue-position'],
  },
  {
    id: 'queue-position',
    group: 'orders',
    term: 'Queue position',
    aliases: ['queue', 'queue ahead', 'line'],
    short: 'How many contracts are ahead of the bot\'s bid at the same price.',
    body:
      'At one price, Kalshi fills bids first-come, first-served. Queue position (or "queue ahead") is how many contracts were already waiting at that price when the bot joined. A small number means the bot is near the front and likely to fill; a large number means sellers must trade through the others first.',
    related: ['front-of-line', 'fill-quality', 'best-bid'],
  },
  {
    id: 'front-of-line',
    group: 'orders',
    term: 'Front of line',
    aliases: ['front fill', 'front'],
    short: 'A fill where the bot\'s bid was first in the queue.',
    body:
      'A "front" fill happened while the bot was at (or near) the front of the queue at its price: a seller came along and the bot was first to be matched. This is the normal kind of fill for a resting order.',
    related: ['queue-position', 'trade-through', 'fill-quality'],
  },
  {
    id: 'trade-through',
    group: 'orders',
    term: 'Trade-through',
    short: 'A fill where the price dropped through the bot\'s bid, filling everyone.',
    body:
      'A trade-through happens when sellers push the price below the bot\'s bid, so every bid at that price fills at once. It often means news moved the market against the side the bot bought, so trade-through fills tend to lose more often. A rising trade-through share is worth watching.',
    related: ['fill-quality', 'front-of-line'],
  },
  {
    id: 'fill-quality',
    group: 'orders',
    term: 'Fill quality',
    aliases: ['fill quality'],
    short: 'Whether fills came from the front of the line or from trade-throughs.',
    body:
      'Fill quality splits fills into "front" (the bot was first in line) and "trade-through" (the price fell through the bid). More front fills is better. The Analytics fill-quality chart shows, per week, how many fills of each kind there were, the front rate (front fills ÷ all labelled fills), the average queue ahead, and how many re-quotes happened.',
    related: ['front-of-line', 'trade-through', 'queue-position', 're-quote'],
  },
  {
    id: 'fill-rate',
    group: 'orders',
    term: 'Fill rate',
    short: 'The share of the bot\'s bids that actually got filled.',
    body:
      'Fill rate = bids that filled ÷ bids placed (or quote episodes that ended in a fill). A low fill rate is normal for resting orders: most bids are cancelled or re-quoted before anyone sells into them. It is a measure of activity, not of profitability.',
    related: ['fill', 'queue-position'],
  },

  {
    id: 'rest-time',
    group: 'orders',
    term: 'Rest time',
    short: 'How long a quote sat in the order book before it filled or was pulled.',
    body:
      'Rest time runs from the moment the bot places a bid until it fills, or until the bot cancels or re-prices it at a later run. Analytics shows the median: half the quotes rested less than that, half rested longer. It also shows the median minutes to fill for quotes that did fill. Short rest times with few fills usually mean frequent re-quotes. Long times to fill mean the bot is waiting deep in the queue.',
    related: ['missed-fill', 're-quote', 'fill-rate', 'queue-position'],
  },
  {
    id: 'missed-fill',
    group: 'orders',
    term: 'Missed fill (unfilled quote)',
    aliases: ['unfilled', 'missed'],
    short: 'A quote that ended without filling at all.',
    body:
      'A quote is "missed" when it ends with no fill. That happens when it expires with the market, is cancelled by a re-quote or a re-price, or is cancelled by Stop. Missed fills cost nothing because no money changes hands, but they are trades the strategy wanted and did not get. Analytics counts quotes as filled, partly filled or unfilled; fill rate = filled / all closed quotes. Many misses are normal for resting orders.',
    related: ['fill-rate', 'rest-time', 'quote', 'resting-bid'],
  },
  // ---------------------------------------------------------------- Money & risk
  {
    id: 'balance',
    group: 'money',
    term: 'Balance (cash)',
    aliases: ['cash'],
    short: 'Uninvested cash in the Kalshi account.',
    body:
      'Balance is the cash in the account that is not tied up in open positions. Cash reserved by resting bids may still show here until they fill. Total account value is roughly balance + exposure.',
    related: ['exposure', 'headroom'],
  },
  {
    id: 'position',
    group: 'money',
    term: 'Position',
    short: 'Contracts the bot owns in a market that has not settled yet.',
    body:
      'A position is what the bot holds after fills: for example 12 NO contracts in one bracket. Positions are what Stop does NOT close: Stop cancels resting bids, and existing positions still settle normally.',
    related: ['exposure', 'settlement', 'stop'],
  },
  {
    id: 'exposure',
    group: 'money',
    term: 'Exposure ($ at risk)',
    aliases: ['at risk', 'risk'],
    short: 'What the bot paid for contracts that have not settled yet.',
    body:
      'Exposure is the total cost of open positions: the most that could be lost if every one of them lost. It is shown against a limit: exposure can never exceed the open fraction of (balance + exposure).',
    related: ['position', 'open-fraction', 'headroom'],
  },
  {
    id: 'open-fraction',
    group: 'money',
    term: 'Open fraction (max_open_frac)',
    aliases: ['max open frac', 'max_open_frac'],
    short: 'Hard cap on how much of the account can be at risk at once.',
    body:
      'Open fraction is a setting between 0.05 and 0.5. Exposure can never exceed that fraction of (balance + exposure). At 0.5, at most half the account is ever in open positions. The server enforces a ceiling of 0.5 no matter what the app sends.',
    related: ['exposure', 'headroom', 'budget'],
  },
  {
    id: 'headroom',
    group: 'money',
    term: 'Headroom',
    short: 'How much more the bot may put at risk before hitting the open-fraction cap.',
    body:
      'Headroom = open fraction × (balance + exposure) − exposure. When headroom is near zero the bot stops placing new bids until positions settle. Negative headroom cannot happen by design; if you ever see it, stop and check.',
    related: ['open-fraction', 'exposure'],
  },
  {
    id: 'budget',
    group: 'money',
    term: 'Budget per run (run_budget)',
    aliases: ['run budget', 'max order', 'max_order'],
    short: 'Most dollars of new bids the bot may place in one run.',
    body:
      'run_budget caps the total of new bids per run, split evenly across the enabled series. Each single order is also capped by max_order. It is the main lever for how much the bot trades. The open-fraction cap still applies on top of it, and the server rejects values above its ceiling.',
    related: ['open-fraction', 'headroom'],
  },
  {
    id: 'fee',
    group: 'money',
    term: 'Fee',
    aliases: ['fees', 'commission'],
    short: 'What Kalshi charges per trade; maker fees are lower than taker fees.',
    body:
      'Kalshi charges a small fee per contract traded, larger for takers than for makers and larger for prices near 50¢. All P&L numbers in the app are after fees. Arbitrage minimum profit is also measured after taker fees.',
    related: ['maker-taker', 'pnl'],
  },
  {
    id: 'settlement',
    group: 'money',
    term: 'Settlement',
    aliases: ['settle', 'settled', 'payout'],
    short: 'When the market\'s answer is known and each contract pays $1 or $0.',
    body:
      'After the event (for example the official result is published), Kalshi settles the market: winning contracts pay $1.00 and losing ones $0. Only then is the trade\'s P&L real. Analytics counts a fill as "settled" once its market has a yes/no result; until then it is "open".',
    related: ['realised-pnl', 'position'],
  },
  {
    id: 'pnl',
    group: 'money',
    term: 'P&L (profit and loss)',
    aliases: ['pnl', 'profit and loss', 'profit', 'loss', 'p and l'],
    short: 'Money made or lost, after fees.',
    body:
      'For one settled fill: P&L = contracts × (payout − price) − fee, where payout is $1 for a win and $0 for a loss. A win at 60¢ makes 40¢ per contract; a loss at 60¢ loses 60¢.',
    related: ['realised-pnl', 'pnl-window', 'roi', 'win-rate'],
  },
  {
    id: 'realised-pnl',
    group: 'money',
    term: 'Realised P&L',
    aliases: ['realized pnl', 'realized p&l', 'realised pnl'],
    short: 'P&L from trades that have already settled. Open positions are not counted.',
    body:
      'Realised P&L only includes fills whose market has settled, so it is money actually won or lost. Open positions have no realised P&L yet. The app does not show "unrealised" mark-to-market numbers.',
    related: ['pnl', 'settlement'],
  },
  {
    id: 'pnl-window',
    group: 'money',
    term: 'P&L window (7 / 30 days)',
    aliases: ['7 day pnl', '30 day pnl', '7 / 30 day p&l'],
    short: 'Realised P&L over the last 7 or 30 days, by settlement date.',
    body:
      'The 7-day and 30-day figures add up realised P&L for trades that settled in that window. Short windows are noisy: a single loss can make a week negative even when the strategy is fine. Judge the strategy on longer windows and on the confidence intervals in Analytics.',
    related: ['realised-pnl', 'confidence-interval', 'sample-size'],
  },
  {
    id: 'drawdown',
    group: 'money',
    term: 'Drawdown',
    aliases: ['max drawdown', 'dd'],
    short: 'How far cumulative P&L has fallen below its previous high.',
    body:
      'Drawdown at a point = cumulative P&L minus the highest cumulative P&L reached so far, so it is zero at a new high and negative after losses. Max drawdown is the deepest dip in the window.',
    related: ['pnl', 'win-rate'],
  },

  // ---------------------------------------------------------------- Performance & statistics
  {
    id: 'win-rate-by-strategy',
    group: 'performance',
    term: 'Win rate by strategy',
    aliases: ['per-set win rate', 'arbitrage win rate'],
    short: 'Maker is counted per fill, arbitrage per set. The two are never pooled into one number.',
    body:
      'A maker win is one settled fill that paid out. An arbitrage win is one whole set (all its legs) whose total P&L is above zero, because a set is bought as one package. Mixing fills and sets in one percentage would mean nothing, so each strategy gets its own rate with its own 95% range.',
    related: ['win-rate', 'confidence-interval', 'arb-set'],
  },
  {
    id: 'arb-set',
    group: 'strategy',
    term: 'Arbitrage set',
    aliases: ['set', 'distinct sets'],
    short: 'One bracket purchase made up of several legs, counted once however often it was seen.',
    body:
      'The scanner can see the same opportunity on many scans. A set is the one stored record for it. "Sightings" count how often it was seen; "sets" count it once. Status: Partial = a leg did not fill fully; Filled = all legs filled, none settled; Open = some legs settled; Settled = every filled leg settled.',
    related: ['arbitrage', 'legging', 'win-rate-by-strategy'],
  },
  {
    id: 'scans-since-restart',
    group: 'safety',
    term: 'Scans since restart',
    aliases: ['scan counter'],
    short: 'How many scans the server has run since it last started. It resets on every restart.',
    body:
      'This counter lives in memory, so a deploy or a free-tier sleep resets it to zero. It is not a history. For history use the distinct sets and sightings counted from the stored log.',
    related: ['arb-set', 'arbitrage'],
  },
  {
    id: 'risk-rule-outcome',
    group: 'strategy',
    term: 'Risk rule outcome',
    aliases: ['skipped by rule', 'risk check'],
    short: 'What happened to each logged set: filled, partial, error, skipped by a risk rule, or log-only.',
    body:
      'Before placing a set the bot checks its risk caps. A set that fails a cap is skipped and the rule is recorded. Log-only means the scanner was not allowed to trade at that time.',
    related: ['arb-set', 'arbitrage'],
  },
  {
    id: 'ledger',
    group: 'money',
    term: 'Honest ledger',
    aliases: ['reconciliation', 'residual'],
    short: 'P&L by strategy, checked against how much the account balance really changed.',
    body:
      'The ledger lists fills, cost, fees and realized P&L per strategy. The reconciliation compares the balance change with net deposits plus realized P&L and fees. The residual is the part that is not explained. If it is larger than the tolerance it is flagged, and the numbers above should not be trusted until it is understood. Anything the server cannot know shows as "not available".',
    related: ['realised-pnl', 'scorecard'],
  },
  {
    id: 'scorecard',
    group: 'safety',
    term: 'Scorecard',
    aliases: ['health scorecard'],
    short: 'Run reliability, order errors, churn, settlement follow-through and a tiny-sample return.',
    body:
      'A one-page health check of the bot over the chosen window. Metrics the bot does not record yet say "not tracked yet" instead of showing zero. The return shown is from demo play money with a small sample and is indicative only.',
    related: ['ledger', 'sample-size'],
  },
  {
    id: 'shadow-capital',
    group: 'strategy',
    term: 'Shadow capital',
    aliases: ['simulated capital'],
    short: 'The size of the simulated account the shadow estimate pretends to trade.',
    body:
      'Dollar P&L and the open-position ceiling scale with this number; rates and ROI do not. Changing it needs a fresh 2FA code. It affects only the simulation and never places orders.',
    related: ['pnl'],
  },
  {
    id: 'roi',
    group: 'performance',
    term: 'ROI (return on cost)',
    aliases: ['return', 'return on investment'],
    short: 'P&L divided by what was paid for the contracts.',
    body:
      'ROI = P&L ÷ cost. If the bot spent $100 on contracts that settled and made $1.50, ROI is 1.5%. ROI is per trade cost, not per year.',
    related: ['pnl', 'edge'],
  },
  {
    id: 'period-return',
    group: 'performance',
    term: 'Period percent (day, week, month)',
    aliases: ['daily return', 'weekly return', 'monthly return', 'return rate'],
    short: 'Profit settled in a day, week or month as a share of the account at the start of it.',
    body:
      'Return = realized P&L settled in the period ÷ the base. The dollar figure is always shown; the percent is a dash when the base cannot be trusted. Each period has its own base, so daily percentages do not add up to the weekly one, but the dollars do. This is account growth, not the same as Profit per $1 Spent, which divides by what was paid for contracts.',
    related: ['return-base', 'partial-period', 'small-sample', 'roi'],
  },
  {
    id: 'return-base',
    group: 'performance',
    term: 'Base (modified Dietz)',
    aliases: ['dietz', 'base', 'starting balance'],
    short: 'The account balance at the start of the period, adjusted for money you added or took out.',
    body:
      'The base is the balance when the period began plus deposits and minus withdrawals, each counted for the part of the period it was in the account (a deposit on Wednesday counts for about four sevenths of a Monday-to-Sunday week). That way moving money in or out never looks like a gain or a loss. If there is no reliable starting balance, or the base is under $10, the percent shows a dash.',
    related: ['period-return', 'pnl'],
  },
  {
    id: 'small-sample',
    group: 'performance',
    term: 'Small sample (fewer than 5 fills)',
    aliases: ['faded bar', 'few fills', 'noise'],
    short: 'A period with fewer than 5 settled fills; one fill can swing it, so read it as noise.',
    body:
      'Bars with fewer than 5 settled fills are drawn faded with a dotted outline. One winning or losing fill can swing such a period, so it says little about the strategy. Arbitrage counts legs, so 5 fills can be a single set.',
    related: ['period-return', 'confidence-interval'],
  },
  {
    id: 'partial-period',
    group: 'performance',
    term: 'So far (partial period)',
    aliases: ['so far', 'current period', 'in progress'],
    short: 'The current day, week or month, still in progress.',
    body:
      'The latest bar covers time that has not finished yet, so it is marked "so far" and drawn with a dashed outline. Its percent is on the time elapsed. It is never scaled up to a full period and never projected.',
    related: ['period-return'],
  },
  {
    id: 'indicative',
    group: 'performance',
    term: 'Indicative only',
    aliases: ['demo, indicative only'],
    short: 'A number from demo money or a small sample; it describes that account, not what to expect live.',
    body:
      'A demo balance is arbitrary, and a short live history is a small sample. Percentages from either are shown for orientation only and should not be read as an expected return.',
    related: ['small-sample', 'period-return'],
  },
  {
    id: 'win-rate',
    group: 'performance',
    term: 'Win rate',
    aliases: ['hit rate', 'win %'],
    short: 'Share of settled fills whose side turned out right.',
    body:
      'Win rate = winning settled fills ÷ all settled fills. A high win rate is not enough on its own: it must be higher than the average price paid. That difference is the edge.',
    related: ['edge', 'confidence-interval'],
  },
  {
    id: 'edge',
    group: 'performance',
    term: 'Edge',
    short: 'Win rate minus average price paid: how much better than break-even.',
    body:
      'Buying at 60¢ breaks even if the side wins 60% of the time. Edge = win rate − average price, so a 62% win rate at an average 60¢ is a +2 point edge. In words: win rate minus break-even (the price paid). Positive edge means the side bought wins more often than its price implies. Analytics shows edge with a 95% confidence interval; if the whole interval is above zero, the edge is statistically convincing.',
    related: ['win-rate', 'break-even', 'confidence-interval'],
  },
  {
    id: 'sample-size',
    group: 'performance',
    term: 'Sample size (n)',
    aliases: ['n'],
    short: 'How many settled fills a number is based on.',
    body:
      'n is the number of settled fills behind a statistic. With small n, results swing a lot by chance: 10 fills with 10 wins says very little. Confidence intervals get narrower as n grows. Treat any cell, series or week with fewer than about 30 settled fills as anecdotal.',
    related: ['confidence-interval', 'ci-bars'],
  },
  {
    id: 'confidence-interval',
    group: 'performance',
    term: '95% confidence interval',
    aliases: ['ci', 'confidence interval', 'range', '95% ci', 'wilson'],
    short: 'The range the true value very likely lies in, given the data so far.',
    body:
      'A 95% confidence interval (CI) is a range around a measured number. If you repeated the experiment many times, 95% of such ranges would contain the true value. The app uses the Wilson score interval for win rate, which behaves well with small samples and with win rates near 100%. Edge CI = Wilson bounds − average price. A narrow CI means the number is well measured; a wide one means more data is needed.',
    related: ['ci-bars', 'sample-size', 'edge', 'win-rate'],
  },
  {
    id: 'ci-bars',
    group: 'performance',
    term: 'CI bars (how to read them)',
    aliases: ['ci bar', 'cibar', 'error bars'],
    short: 'A dot for the measured value and a line for its 95% range.',
    body:
      'A CI bar shows a measured value as a dot and its 95% confidence interval as a shaded bar along a horizontal axis. On Markets, the CiBar graph shows each series\' return (ROI) on an axis from −8% to +8%: the solid grey vertical line is zero (break-even), the dashed line is the expected return (when a benchmark is configured), and the bar is green when the whole interval is above zero, red when it is entirely below zero, and grey when it crosses zero. The edge and win-rate CI bars in Analytics work the same way. How to read one: bar entirely right of zero = very likely making money; bar crossing zero = not yet distinguishable from luck (usually just too few fills); bar entirely left of zero = very likely losing. A long bar means few fills. Compare series by whether their bars overlap, not just by where the dots sit. Too Early means fewer than 20 markets, so the bar is not judged yet.',
    related: ['confidence-interval', 'edge', 'sample-size'],
  },

  {
    id: 'markets-count',
    group: 'performance',
    term: 'Markets vs fills',
    aliases: ['markets', 'distinct markets'],
    short: 'How many different markets the fills came from, not just how many fills.',
    body:
      'The bot can fill several times in the same market (one ticker), for example three small fills on the same bracket. Those fills all win or all lose together, so they are really one result, not three. "Markets" counts distinct tickers, and "fills" counts trades. For judging the edge, the number of markets is the honest sample size. That is why the verdict waits for at least 20 markets, however many fills there are. Fills in the same market win or lose together, so they count once. Too Early means fewer than 20 markets.',
    related: ['sample-size', 'clustered-ci', 'edge-verdict'],
  },
  {
    id: 'clustered-ci',
    group: 'performance',
    term: 'Clustered range (clustered CI)',
    aliases: ['cluster ci', 'clustered confidence interval', 'clustered range'],
    short: 'A 95% range that treats fills in the same market as one outcome. Wider, and more honest.',
    body:
      'Fills in the same market move together: if the side bought loses, every fill on that market loses at once. The ordinary per-fill interval (Wilson) treats each fill as an independent coin flip, so it looks more certain than the data really is. The clustered range groups fills by market (ticker) and measures how much results vary from market to market. It is usually wider than the per-fill range, and that width is the honest amount of uncertainty. The edge verdict uses the clustered range, and the per-fill range is shown only for comparison. In short: fills in the same market win or lose together, so they count once.',
    related: ['confidence-interval', 'markets-count', 'edge-verdict', 'ci-bars'],
  },
  {
    id: 'break-even',
    group: 'performance',
    term: 'Break-even',
    aliases: ['breakeven', 'break even'],
    short: 'The win rate needed to cover the price paid.',
    body:
      'If you pay 60¢ for a contract that pays $1.00 when it wins, you need to win 60% of the time just to break even (before fees). Edge is how far the actual win rate sits above that line. On the return charts, break-even is the zero line.',
    related: ['edge', 'win-rate', 'fee'],
  },
  {
    id: 'likely-range',
    group: 'performance',
    term: 'Likely Range',
    aliases: ['95% range', 'range of likely values', 'plausible range'],
    short: '95% likely range around the estimate: where the true value probably sits.',
    body:
      'A measured return or win rate from a limited number of markets is only an estimate. The likely range is the 95% confidence interval around it: if the same test were repeated many times, about 95 of 100 such ranges would contain the true value. A narrow range means a more reliable estimate; a range that crosses zero means the result could still be luck.',
    related: ['confidence-interval', 'ci-bars', 'clustered-ci'],
  },
  {
    id: 'bot-vs-you',
    group: 'performance',
    term: 'Bot vs You',
    aliases: ['bot only', 'bot + you', 'bot and you', 'view', 'manual'],
    short: 'Bot Only or Bot + You: manual trades never count in bot statistics.',
    body:
      'Some screens can show the bot alone (Bot Only) or the bot together with trades you placed yourself on Kalshi (Bot + You). Your manual trades are shown so the totals match your account, but they are never used in the bot\'s statistics, edge verdicts or expected-return checks, because they were not made by the strategy.',
    related: ['my-trades', 'edge-verdict', 'pnl'],
  },
  {
    id: 'my-trades',
    group: 'performance',
    term: 'My Trades',
    aliases: ['manual trades', 'your trades', 'own trades'],
    short: 'Your own manual trades, kept apart from the bot\'s results.',
    body:
      'My Trades lists the trades you placed yourself on Kalshi, outside the bot. They are kept separate so they never change the bot\'s win rate, edge or verdicts. You can add a short note to any of them to remember why you made the trade.',
    related: ['bot-vs-you', 'journal-note'],
  },
  {
    id: 'journal-note',
    group: 'performance',
    term: 'Journal Note',
    aliases: ['note', 'trade note'],
    short: 'A short note you can attach to a trade.',
    body:
      'A journal note is up to 200 characters of plain text attached to one of your manual trades, for example why you took it or what you expected. It is for your own reference, is stored with your account data, and is never used in any statistic.',
    related: ['my-trades', 'bot-vs-you'],
  },
  {
    id: 'expected-edge',
    group: 'performance',
    term: 'Expected edge (benchmark)',
    aliases: ['expectation', 'benchmark', 'expected return'],
    short: 'What the optional private benchmark expects for each cell.',
    body:
      "When the deployment supplies a private benchmark file, each cell's expected win rate and return come from it, and live results are compared with them. When a cell has no benchmark of its own, the file's default return (if any) is used, and the comparison is on return (ROI) only. With no benchmark file there is nothing to compare with. The benchmark is fixed reference data; it is not the cells table and never changes trading.",
    related: ['edge', 'edge-verdict', 'cell', 'roi'],
  },
  {
    id: 'edge-verdict',
    group: 'performance',
    term: 'Edge verdict',
    aliases: ['verdict', 'on track', 'too early'],
    short: 'Too early, On track, Below or Above: how a cell compares with its benchmark.',
    body:
      'Each cell gets one verdict. Too early: fewer than 20 different markets have settled, so any verdict would be luck. Below: even the top of the clustered 95% range is under the benchmark, so the cell is very likely doing worse than expected and is worth a closer look. Above: even the bottom of the range is over the expectation. On track: the expectation is inside the range, so results are consistent with the benchmark. The comparison uses win rate when the benchmark has one, otherwise return (ROI). A single "Below" is a prompt to look, not proof the edge is gone.',
    related: ['expected-edge', 'clustered-ci', 'markets-count', 'cell'],
  },
  {
    id: 'calibration',
    group: 'performance',
    term: 'Calibration chart',
    aliases: ['calibration chart', 'reliability'],
    short: 'Win rate plotted against the price paid; points above the diagonal are edge.',
    body:
      "The calibration chart puts fills into 2-cent price buckets across the price range of the strategy's cells and plots each bucket's actual win rate against its price. The diagonal line is \"fair price\": a 60¢ contract that wins exactly 60% of the time. Points above the diagonal mean the side bought at that price won more often than its price implied. Points below it mean that price lost money. Each point has a clustered 95% range; a range that crosses the diagonal is not yet distinguishable from fair. Buckets with few markets are noisy.",
    related: ['edge', 'clustered-ci', 'price-band', 'win-rate'],
  },
  // ---------------------------------------------------------------- Strategy
  {
    id: 'cell',
    group: 'strategy',
    term: 'Cell',
    short: 'One combination of price range, hours to close and side.',
    body:
      'The strategy is a table of cells. Each cell is [min price, max price, min hours, max hours, side]. The bot only quotes when a market falls inside a cell. Cells are part of the strategy and are read-only in the app: nobody edits them. In Analytics, "by cell" groups fills by the cell they matched, so you can check each cell\'s edge against its benchmark.',
    related: ['price-band', 'time-to-close'],
  },
  {
    id: 'price-band',
    group: 'strategy',
    term: 'Price band',
    short: 'Price groups used in Analytics, one per cell price range.',
    body:
      'Fills are grouped by the price paid, one band per distinct price range of the strategy\'s cells (anything else is "other"). Each band needs its own check that the edge is positive.',
    related: ['cell', 'edge'],
  },
  {
    id: 'time-to-close',
    group: 'strategy',
    term: 'Time to close',
    aliases: ['ttc', 'hours to close'],
    short: 'Hours from the fill until the market stops trading.',
    body:
      'Time to close (ttc) is measured from the fill to the market\'s close. Analytics groups it into buckets whose edges are the hour windows of the strategy\'s cells. Each cell names the time-to-close window it applies to.',
    related: ['cell', 'price-band'],
  },
  {
    id: 'demo-vs-live',
    group: 'strategy',
    term: 'Demo vs live',
    aliases: ['demo', 'live', 'mode', 'real money'],
    short: 'Demo uses Kalshi\'s practice exchange; live uses real money.',
    body:
      'In demo mode the bot trades on Kalshi\'s demo exchange with play money, so nothing real is at stake. Live mode trades real money and is allowed only if the server environment permits it; switching needs a fresh 2FA code and typing LIVE. Analytics and history are filtered by mode so demo results never mix with live ones.',
    related: ['two-factor', 'stop'],
  },
  {
    id: 'arbitrage',
    group: 'strategy',
    term: 'Bracket arbitrage',
    aliases: ['arb'],
    short: 'Buying every bracket of one event when the set costs less than $1 after fees.',
    body:
      'The brackets of one bracket event are mutually exclusive: exactly one wins. If buying YES on every bracket (or NO on every one, adjusted) costs less than the guaranteed payout after fees, the set locks in a profit. The scanner looks for these; it is log-only unless separately switched to live. Each part of the set is a "leg", sent as a taker order.',
    related: ['legging', 'maker-taker', 'kill-switch'],
  },
  {
    id: 'legging',
    group: 'strategy',
    term: 'Legging (unmatched leg)',
    aliases: ['unmatched leg', 'leg', 'legs'],
    short: 'When only some legs of an arbitrage set fill, leaving an unhedged bet.',
    body:
      'An arbitrage set is only risk-free if every leg fills. If one leg fails or partly fills, the bot holds an ordinary, unhedged position: that is legging risk. When it happens the arbitrage kill switch trips and an alert is sent. Check Kalshi positions, then acknowledge it on the Arbitrage page before turning arbitrage back on.',
    related: ['arbitrage', 'kill-switch'],
  },

  // ---------------------------------------------------------------- Safety & operations
  {
    id: 'stop',
    group: 'safety',
    term: 'Stop',
    aliases: ['stop button', 'halt'],
    short: 'The red button: Halts trading and cancels all resting bids. No code needed.',
    body:
      'Stop halts both strategies (maker and arbitrage): it sets both kill switches, marks the bot stopped and cancels the bot\'s resting orders. It needs one tap and one confirm, never a 2FA code, and the kill switch is set on the server even if the database or Kalshi is unreachable (the result then lists any orders it could not cancel; tap Stop again). It does not sell positions you already hold; they settle normally. Starting again needs a fresh 2FA code.',
    related: ['kill-switch', 'two-factor', 'position'],
  },
  {
    id: 'kill-switch',
    group: 'safety',
    term: 'Kill switch',
    aliases: ['killswitch'],
    short: 'A flag that blocks all new orders until it is cleared.',
    body:
      'A kill switch is a hard "no trading" flag. Stop sets it for the maker strategy and for arbitrage. Arbitrage also has its own automatic kill switch that trips on a legging event. While set, the bot places no orders even if a run is due. Clearing it means pressing Start (with 2FA), or for legging, acknowledging the unmatched leg.',
    related: ['stop', 'legging'],
  },
  {
    id: 'heartbeat',
    group: 'safety',
    term: 'Heartbeat',
    short: 'A timestamp the bot writes each run to prove it is alive.',
    body:
      'Every run writes a heartbeat. If the latest heartbeat is older than 30 minutes while the bot should be running, the status turns red and an alert can be sent. A stale heartbeat usually means the free server fell asleep or crashed; the keep-awake pings on the Server page exist to prevent that.',
    related: ['stop'],
  },
  {
    id: 'keep-awake',
    group: 'safety',
    term: 'Keep-awake pings',
    aliases: ['pinger', 'self ping', 'sleep', 'cron-job.org'],
    short: 'Regular requests that stop the free server from falling asleep.',
    body:
      'The free Render server sleeps after 15 minutes without an incoming request, and a sleeping server cannot trade. Two things keep it awake: the server pinging itself, and an external cron-job.org job calling it. Both intervals (and poll_minutes) must be 14 minutes or less. If the server does sleep, the first request wakes it in up to a minute.',
    related: ['heartbeat'],
  },
  {
    id: 'audit-log',
    group: 'safety',
    term: 'Audit log',
    aliases: ['audit'],
    short: 'A record of every control action: what, when, from where, and whether it worked.',
    body:
      'Every sign-in and control action (Stop, Start, mode switches, settings saves, pausing a series, logging out all sessions) is written to the audit log with the time, user, IP address and result. Use it to check that an action really happened, or to spot activity you did not do. A failed entry is marked in red.',
    related: ['two-factor', 'stop'],
  },
  {
    id: 'csv-export',
    group: 'safety',
    term: 'CSV export',
    aliases: ['csv', 'download', 'export'],
    short: 'Download the trades you are looking at as a spreadsheet file.',
    body:
      '"Download CSV" in Trade history saves the trades that match the current filters (window, series, side, cell and so on) as a .csv file that opens in Excel, Numbers or Google Sheets. It holds up to 5,000 rows, and a note shows when it was cut short. It contains trade data only: no keys, passwords or account secrets. The file is saved on your phone, so treat it like any other financial record.',
    related: ['fill', 'pnl'],
  },
  {
    id: 'two-factor',
    group: 'safety',
    term: 'Two-factor code (2FA)',
    aliases: ['2fa', 'two factor', 'totp', 'authenticator', 'mfa', 'code'],
    short: 'The 6-digit code from your authenticator app, needed for risky actions.',
    body:
      'Actions that can start trading or change risk (Start, switching to live, saving settings, pausing a series, server settings) need a fresh 6-digit code from your authenticator app. Buttons that need one show a lock icon. Stop never asks for one, so you can always halt the bot quickly.',
    related: ['stop', 'demo-vs-live'],
  },
];

// Title Case pass (UX item 21): term names go through titleCase(); the sentences (short, body) stay as written.
export const GLOSSARY = RAW_GLOSSARY.map((e) => ({ ...e, term: titleTerm(e.term) }));

const BY_ID = new Map(GLOSSARY.map((e) => [e.id, e]));

export function getTerm(id) {
  return BY_ID.get(id) || null;
}
