# combo-edge

A local tool that finds +EV **Kalshi NFL same-game 2-leg combos**: ones where makers price correlated legs at
roughly the product of the leg prices. It probes combos with RFQs at your real stake, logs every quote, learns
where the edge is, and can place bets through one gated module (`combo_edge/execution.py`). That module
defaults to **paper** mode.

- Dashboard: http://localhost:8000 (`uv run combo-edge`), localhost only.
- Data: `data/` (SQLite, logs, nflverse cache, reports). It's gitignored.
- Spec: [spec.md](spec.md).

## Safety model (read first)

- **It never places resting orders.** The only order-type call is the RFQ quote accept
  (`PUT /communications/rfqs/{rfq}/quotes/{quote}/accept`). Only `execution.py` can send it, and the HTTP
  client refuses it without a capability object that only that module holds. Every RFQ is sent with
  `rest_remainder: false`.
- `tests/test_safety.py` fails the build if:
  - the accept path appears anywhere else
  - any resting-order, order-group, block-trade, batch, quote-create or confirm endpoint appears
  - the default mode isn't paper
- **Execution modes:**
  - **off**: nothing executes.
  - **paper** (default): records exactly what it would have bought at the real quote, using the same gates.
  - **live**: needs the toggle on My bets, plus typing `GO LIVE PROD` (or `GO LIVE DEMO`), plus at least 20
    agreeing quote-side checks on that environment. The first live trade is always a **$1 canary**,
    verified against your fills before anything bigger is allowed.
- **Kill switch:** the red KILL button, `uv run combo-edge kill`, or `touch data/KILL`. It sets execution to
  off, stops the running scan, cancels every open RFQ *this tool* created and pauses the schedulers. Clearing
  it leaves execution **off** until you pick a mode.
- **Limits:** $40/day, $120 per rolling 7 days and 6 bets/day (all editable). Open stakes count as lost until
  they settle. Hitting a limit sets execution to off; you turn it back on yourself.
- `COMBO_EDGE_FORBID_LIVE=1` in `.env` makes live impossible to enable on this machine.

## Mac setup (Apple Silicon, native arm64)

The project uses uv with its own Python 3.12, not Homebrew. That avoids the Rosetta Homebrew under
`/usr/local`, so everything runs native arm64.

```bash
# 1. uv (Astral's installer; puts uv in ~/.local/bin)
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"          # add this line to ~/.zshrc too

# 2. Python 3.12 + dependencies (locked in uv.lock)
cd ~/kalshi_final
uv python install 3.12
uv sync

# 3. sanity check
uv run combo-edge doctor
uv run pytest -q                               # ~2.5 min; network is blocked inside tests
```

`doctor` should report `python 3.12.x on arm64`. If it says `x86_64`, you're running under Rosetta. Open
a native terminal and re-run `uv sync`.

## API keys and `.env`

**Your current `.env` is not usable, and its key must be replaced.** It holds a bare key ID plus an inline
private key, with no `NAME=` lines. That private key was printed during the build, so treat it as exposed.

1. **Delete the old key:** kalshi.com → Account → API keys.
2. **Create a new prod key.** Optionally create a demo key too at demo.kalshi.co (demo keys are separate).
3. **Save each private key outside the repo:**
   ```bash
   mkdir -p ~/.kalshi && chmod 700 ~/.kalshi
   # save the downloaded key as ~/.kalshi/prod.pem (and ~/.kalshi/demo.pem)
   chmod 600 ~/.kalshi/*.pem
   ```
4. **Create `.env` from the template:**
   ```bash
   cp .env.example .env
   ```
   Fill in `KALSHI_PROD_API_KEY_ID=<uuid>` and `KALSHI_PROD_PRIVATE_KEY_PATH=/Users/<you>/.kalshi/prod.pem`.
   Add the `DEMO` ones too if you made a demo key. Both RSA and Ed25519 keys work.
5. **Check it:** `uv run combo-edge doctor`. It prints variable *names* only, never values, then makes a
   signed read-only call and a websocket auth check per environment.

`.env`, `*.pem` and `*.key` are gitignored. Never commit them.

## First run (in this order)

1. **Mock, offline:**
   ```bash
   uv run combo-edge --env mock
   ```
   Press **Run scan** and look around the tabs. The fake exchange has synthetic makers, so its EVs mean nothing.
2. **Demo:**
   ```bash
   uv run combo-edge --env demo
   ```
   Run a scan with a budget of 20. Health should show quotes arriving over the websocket and **0 open RFQs**
   left afterwards. Demo may have few or no makers, so an empty result there is not a bug.
3. **Prod, paper mode:**
   ```bash
   uv run combo-edge --env prod scan --budget 10 --max-new 10
   ```
   This creates at most 10 combos and sends at most 10 RFQs at $5, all cancelled. Check `doctor` for
   `prod side mapping`. Each quote is sizing-checked to confirm that buying YES means accepting the
   maker's `no` side.
4. **Run the dashboard on prod:**
   ```bash
   uv run combo-edge --env prod
   ```
   Let exploration run for a few days, with scans plus the recorder and re-quote toggles on Health. Watch
   **Learning** (strata filling toward 30 quotes each) and the paper book on **My bets**.
5. **Review paper results:** decision log, paper P&L, CLV. Nothing executes until a stratum has 30 or more
   quotes **and** the fair model's lower bound is still +EV **and** EV after fees is at least +15%.
6. **Optional: live on demo.** This tests the accept/confirm flow end to end, if demo has makers.
7. **Prod canary.** On My bets choose Live and type `GO LIVE PROD`. The first accepted trade is $1. It's
   verified against `GET /portfolio/fills` (YES side, contract count). Then Health offers the
   **Advanced tier upgrade**, which needs an API-created order (403 before that).
8. **Live:** stakes are ¼-Kelly with a shrink for uncertainty, capped at $10/combo, $40/game and $30/player.

## Using it

- **Scan:**
  - The Run button runs `run_scan()`, the same entry point as the CLI and the scheduler.
  - The results table shows quoted payout, leg product, ratio, fair payout, EV (and EV at the lower
    bound), suggested stake, the gate outcome, and a **Shop** button. Shop sends 3 RFQs a few seconds
    apart and shows the best quote and the dispersion. It never buys.
  - **Max payout** box above the table:
    - Accepts `40`, `40x` or `<40x`, with quick buttons for <25x, <40x, <60x, <100x and All.
    - Rows without a quote use the expected payout.
    - Combines with the other filters, and is remembered in this browser.
    - The CLI equivalent is `scan --max-payout 40`.
- **My bets:**
  - Execution mode and the typed confirmation.
  - Bankroll and caps.
  - Your real Kalshi combo positions, with a **Re-check** button that re-RFQs each one.
  - Exposure vs caps, with stacking warnings.
  - **Paper and live books kept separate**, with P&L, CLV, and the full decision log.
- **Learning:**
  - Strata progress, edge vs hours to kickoff / liquidity / family / day / tailness.
  - Maker-model accuracy, promotions, truth-model calibration.
  - "When to press Run".
- **Health:**
  - Combo creations vs the 5,000/week cap.
  - API tier and buckets, fill rate and latency, coverage, side-mapping status, pending cancels, errors.
  - Scheduler toggles and the upgrade-tier offer.

### Background agents (launchd)

Toggles on Health, or `uv run combo-edge launchd install|uninstall|status <agent>`:

| Agent | When | What |
|---|---|---|
| `recorder` | 24/7 (KeepAlive) | Read-only public RFQ feed. Indexes other people's NFL combos (free tickers) and demand. |
| `requote` | every 10 min, **on by default** (install the agent from Health) | Re-RFQs the panel at 144/96/72/48/24/12/6/1 h before kickoff. Never creates combos, never bets. This is where CLV comes from. |
| `fullscan` | every 30 min, **off by default** | Full-scan mode runs a scan when new player-prop markets open, or when the timing map says now is a good time. Early-week mode (its own Health button) runs at most one scan a day, Tue–Thu 9am–9pm, when props are open. Execution follows the current mode. |

The agents run as `com.combo-edge.*` in `~/Library/LaunchAgents` and log to `data/logs/`. **They only
run while the Mac is awake.**
- To keep it awake while plugged in: run `caffeinate -dimsu &`, or set System Settings → Battery → Options
  → "Prevent automatic sleeping on power adapter when the display is off".
- To wake it before Sunday slates: `sudo pmset repeat wakeorpoweron MTWRFSU 07:00:00`.

### CLI

```
uv run combo-edge                      dashboard (env from KALSHI_ENV; --env mock|demo|prod to override)
uv run combo-edge [--env E] scan [--dry-run] [--budget N] [--max-new N] [--stake $] [--window s]
uv run combo-edge doctor | limits | upgrade-tier | kill
uv run combo-edge recorder | requote-due | fullscan-check
uv run combo-edge launchd {install,uninstall,status} {recorder,requote,fullscan}
uv run combo-edge nflverse-sync | fit | exploration-report
```

## How it works (short)

1. **Universe.** NFL games and kickoffs come from `/milestones`. Every related event's markets are parsed
   into legs, including full threshold ladders and extremes. Leg liquidity is never used as a filter.
2. **Candidates.** Eight template families of correlated same-game pairs, plus exploration strata
   (leg-type pair × same-team/opponent/game).
3. **Core sweeps** ([config/core.yaml](config/core.yaml)). These are your proven patterns:
   - QB 3+/4+/5+ pass TD × own team o17.5–31.5
   - WR/TE 2+/3+ TD × own team total
   - QB 275–325+ yds × own WR/TE 100–190+ rec yds
   - Team ML × 1H lead

   - QB 3+/4+ pass TD × own team total, main line ± 7 (40% of core). 5+ is explore-only.
   - QB 275–325+ yds × own top-2 WR/TE 100–190+ rec yds (25%)
   - Top-2 WR/TE 2+/3+ TD × own team total (20%). Receivers rank by their anytime-TD price.
   - Team ML × 1H lead (15%)

   They get 50% of each run's RFQ budget. A sweep's unused share flows to the next one. Within a sweep
   it goes round-robin across players, rungs nearest the middle of the payout band first. A rung quoted
   in the last 6 hours goes to the back, unless a leg has since moved more than 3¢. Combos Kalshi rejects
   as implied (e.g. 4+ pass TD with o21.5) are learned and skipped.
4. **Payout band.** The core, best-ranked and free pools only send combos whose expected payout is
   8x–50x. Longer combos, and families that haven't proven themselves, go out only through exploration.
   The unproven families are `rb_x_ml_spread`, `qb_x_game_total`, every `no_*` family and the
   exploration strata pairs. They are released once promoted, or after 30 quotes with a positive bandit
   posterior. The band and the split (50% core / 25% best-ranked / 15% free / 10% exploration) are
   editable on the Learning tab.
5. **Tail guards.** These stop the model's edge from growing as both legs get longer:
   - Copula conditionals are capped at the family's strongest anchor.
   - The fair joint is capped at 6× independence.
   - The lower bound widens by 10% of fair per doubling of payout above 20x.
   - The simulator draws each game's pass-TD share from a Beta, and its FGs give way to TDs on big days.
     That matches nflverse: P(team o28.5 | QB 4+ pass TD) ≈ 0.92 at implied 22.5–27.5.
   - The daily history calibration shifts each family's conditional toward nflverse.

   `uv run python scripts/diagnose_selection.py` shows where a run's budget goes, by payout bucket and
   family. It runs offline, on the recorded markets.
6. **Fair price.**
   - Ladder-consistent leg probabilities from the Kalshi books.
   - A drive-based joint simulator fit to the team-total, spread and total ladders, with player shares
     from nflverse.
   - A t-copula (df 4) prior anchored on your finds.
   - Blended into a fair price with a lower bound.
7. **Budget and probe.**
   - The local combo index (ours, the feed, previous runs) is checked before any `POST
     /multivariate_event_collections/…`. At most 500 new combos per run, and 5,000/week minus a 300
     reserve. Every POST is counted.
   - Each RFQ runs at your stake (`target_cost_dollars`, fee-inclusive). Quotes arrive over the websocket.
   - The execution decision runs **while the RFQ is still open**, then the RFQ is cancelled.
8. **Learning.**
   - Labels: edge = fair × quoted payout − 1.
   - A maker model (scikit-learn HistGradientBoosting), retrained every run.
   - A Thompson bandit over strata, with auto-promotion of pair types that pay.
   - Re-quote CLV. Rewards use EV at the quote plus CLV, never win/loss.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `doctor`: `.env … not NAME=value` | Rebuild `.env` from `.env.example`. Keys go in files referenced by `*_PRIVATE_KEY_PATH`. |
| `401` / signature errors | Wrong key for the environment (demo and prod keys differ), or the clock is off (System Settings → General → Date & Time → automatic). |
| `403` on upgrade-tier | Expected until one live accept has executed. Kalshi requires an API-created order among your last 100. |
| `429` | The limiter runs at 60% of the buckets that `/account/limits` reports. Check for another script or app using the same key. |
| `409` on RFQ create | You (or the app) already have an open RFQ on that combo. The tool deletes only its own RFQs. Close app RFQs yourself. |
| A game shows red in Coverage | Player props exist but produced no candidates. The coverage report lists unparsed series and examples. |
| Leftover open RFQs after a crash | The next scan cancels them at start (only ones this tool created), or run `combo-edge kill` now. |
| `nflverse … download failed` | Offline. It uses the cache in `data/nflverse/`. Run `combo-edge nflverse-sync` when online. |
| Python is `x86_64` | Rosetta terminal. Use a native one and `uv sync` again. |

## Not verified against the live API (yet)

These are undocumented, or couldn't be exercised without valid keys. Each is handled conservatively:

| Unverified | How it's handled |
|---|---|
| Whether resolving an existing combo counts toward the 5,000/week creation cap | Every POST is counted. |
| Which rate bucket the combo POST bills | Default write cost. |
| RFQ expiry when not cancelled | Always cancelled; pending-cancel queue. |
| The exact fee table | The PDF returned 429. Quadratic 0.07·C·P·(1−P), rounded up. Actual contract counts come from the quotes. |
| The combo market link format | Shown as "unverified". |
| Whether demo has makers | — |
| **The accept side mapping** (buy YES = `accepted_side: "no"`) | Inferred from docs. Confirmed by the per-quote sizing check, then the $1 canary. Any accepted-contract mismatch deletes the RFQ before the maker confirms and turns execution off. |
