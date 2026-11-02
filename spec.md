# Build: Kalshi Combo Edge Scanner (full build)

You are building a personal tool that scans Kalshi's **NFL** combos (NFL only; no MLB, NBA or other sports) (multivariate events, priced through RFQs) and ranks the combos most likely to be +EV because the makers underprice the correlation between legs. I am the only user. I trade as a taker.

**My setup:** It runs locally on my laptop. My Kalshi API key is on the **Basic** tier, so include a one-time CLI command that calls Kalshi's "Upgrade Account API Usage Level" endpoint to move to Advanced. Kalshi requires at least 1 of the last 100 orders to come through the API first, so offer it after the first live execution, and explain a 403 clearly. **2-leg combos only** for now, but keep the leg count configurable. My stakes are **under $10 per combo**, mostly on longshots. I'm on a **Mac**, so the README setup should cover Homebrew, `uv` and Python 3.12.

**Where my last build failed:** the plumbing worked, but **its picks were worse than the ones I found by hand.** Candidate finding must never silently miss legs, and quote collection must be measured (see Probe below). Above all, the search has to aim where the edge actually is:
- **Scan full threshold ladders, extremes included** (4+ pass TD, 3+ TD, 160+/190+ rec yds, 1H -10.5). My best finds were tail-on-tail, not main lines. Never default to main lines only.

**Build everything in this spec in one go: Phases 1, 1.5, 2, 2.5 and 3.** Don't stop to ask for approval between phases. Build them in order, treat each phase as a milestone, and make each one's tests pass and get a smoke test against the mocked API (and demo where possible) before starting the next. Commit after each milestone with a clear message. At the end, give me one summary: what works, what you verified against the live docs, what you couldn't verify, and the exact steps for my first real run.

## Hard rules
- **Auto-placing bets is allowed only through the Execution module below**, with all of its guardrails. No other code path may accept a quote or place an order. Execution has three modes: `off`, `paper` (log exactly what it would have bought, at the real quote, and track the results as if it had) and `live`. **Default to `paper`.** Switching to `live` takes a deliberate toggle in the dashboard plus a typed confirmation.
- **Read the real docs before writing the client.** Read https://docs.kalshi.com (the RFQ guide, multivariate event collections, rate limits, auth and websockets). The endpoint names in this prompt come from my notes. Wherever they differ from the docs, the docs win. Tell me about every difference you find.
- **Demo first.** Support both the demo environment and production through config, with demo as the default. Keep secrets (API key id, RSA private key path, any odds API key) in `.env`, and keep `.env` gitignored.
- **Scan only on a button press.** The scanner runs only when I press Run. Full scans have no scheduler, cron or background loop running by default. The one exception is the Phase 1.5 re-quote panel, which never creates combos or buys. Structure the code so a scheduler can call the same `run_scan()` function later.

## What I know about Kalshi combos (verify against the docs)
- Combo flow:
  1. `GET /multivariate_event_collections` returns the collections. Read `associated_event_tickers`, `size_min`/`size_max` (leg limits), `is_single_market_per_event` and `is_all_yes` from each one live, and don't hardcode them.
  2. `PUT /multivariate_event_collections/{collection}/lookup` with `selected_markets` returns the combo ticker if the combo already exists. This does NOT use up creation budget.
  3. Only if the lookup fails, call `POST /multivariate_event_collections/{collection}` with `selected_markets` [{market_ticker, event_ticker, side}] to create the combo.
  4. Send the RFQ on the resulting combo ticker. Combo RFQs can also pass `mve_collection_ticker` and `mve_selected_legs`.
- **Combo creation is capped at 5,000 per week per user. This is the binding constraint, much tighter than the rate limits.** Cache every combo ticker in SQLite, always look a combo up before creating it, and track the creations used this week in the UI. Never create a combo for a candidate that isn't in the top N. Cap new creations at **500 per run** by default (configurable), and show the creations used this week against the 5,000 cap. Log `GET /account/limits` before and after each run, and check whether RFQs sent with raw `mve_selected_legs` also count as creations.
- **Mine combos other people already created.** `GET /multivariate_event_collections/{collection}/lookup?lookback_seconds=10|60|300|3600` lists recent lookups, the `multivariate_lookup` websocket channel streams them, and `rfq_created` events carry `mve_selected_legs`. Combos found this way already exist, so pricing them costs no creation budget. Log them as demand data too.
- Max 100 open RFQs at a time.
- Call `GET /account/limits` and `GET /account/endpoint_costs` at startup, and size the limiter from what they return.
- `POST /communications/rfqs` creates an RFQ. Makers reply with quotes carrying `yes_bid` and `no_bid`. Quotes are private to the requester and arrive on the authenticated `communications` websocket channel (`quote_created`), or through REST.
- Only one open RFQ is allowed per market ticker, and a duplicate returns 409.
- Every combo is a high-volatility market: after the taker accepts, the maker has 3 seconds to confirm, and execution follows after 1 second. Execution must handle a maker who doesn't confirm (the quote dies). Read the accept/confirm flow in the RFQ docs before building it.
- `rfq_created` is broadcast to every subscriber. Set `obscure_creator_id: true` on our RFQs.
- Rate limits are token buckets with about 10 tokens per request. On the Basic tier, writes are 100 tokens/s, which works out to about 10 writes/s. RFQ creation and cancellation count as writes. Build a token-bucket limiter and keep us well under the cap.
- Auth uses RSA-PSS signed headers (key id, timestamp, signature). Check the exact scheme in the docs.
- The taker fee is roughly `ceil(0.07 * C * P * (1-P))`. Confirm it, and check whether combos have their own fee schedule.

## Where the edge is (from 24 combos I found by hand)
Makers price "a star's tail stat + his own team's outcome" at about the product of the leg prices, as if the legs were independent, even though the true conditional probability is high. Define **ratio = combo payout ÷ (product of the leg payouts)**. A ratio near 1.0 on strongly correlated legs is the signal. Use these NFL finds as test fixtures:

| Combo | Leg payouts | Combo payout | Ratio |
|---|---|---|---|
| Stafford 4+ pass TD + LAR over 28.5 | 5.56, 1.79 | 10.00 | 1.01 |
| Lamb 3+ TD + DAL over 28.5 | 23.42, 1.90 | 49.02 | 1.10 |
| Stroud 4+ pass TD + HOU over 24.5 | 13.40, 3.28 | 22.83 | 0.52 |
| Darnold 4+ pass TD + SEA over 28.5 | 15.63, 4.31 | 34.60 | 0.51 |
| Stafford 275+ pass yds + Nacua 190+ rec yds | 1.73, 18.73 | 27.55 | 0.85 |
| UCL 3 legs from separate games (control) | 1.97, 2.09, 1.97 | 8.18 | 1.01 |

Some other patterns:
- Combos get quotes even when one leg has no single-market price. **Never filter candidates on leg liquidity.**
- Kalshi rejects combos where one leg logically forces another. Handle that rejection cleanly and skip the combo.
- Prices are softest **days** before game time, usually early in the week for NFL. The timing map in Phase 2.5 should confirm or refute that.

## Phase 1: the scan loop and the Run button
1. **Kalshi client** (`kalshi/`). Covers auth, the rate limiter, REST helpers for events, markets, multivariate collections and the RFQ create/list-quotes/cancel calls, and an optional websocket listener for quotes. Retry with backoff on 429 and 5xx errors.
2. **Universe** (`universe.py`). For the NFL only, list the open games and every leg eligible for combos. Parse each leg into a structured object: `{game, team, player?, stat, threshold, side, kalshi_ticker, yes_ask, no_ask}`. Stats include pass TD, pass yds, rec yds, anytime TD, team total, ML, spread, and 1H spread. Keep a `sports/` plugin layout so another sport can be added later, but build only NFL. Log any leg the parser can't handle instead of crashing.
3. **Templates** (`templates.py`). Candidate pairs come only from these families, all within the same game:
   - QB pass-TD ladder × his own team's total (overs) and team ML
   - QB pass-yds ladder × his own WR/TE rec-yds ladder
   - QB pass-TD ladder × his own WR/TE TD count
   - WR anytime/2+/3+ TD × his own team's total
   - Team ML × the same team's 1H spread ladder
   - The "No" versions of positively correlated pairs (for example, No QB pass TD + No WR TD)

   Make the families easy to add to. Cap the number of candidates per run through config.
4. **Prescore** (`prescore.py`). Before spending an RFQ, give each candidate a rough prior. Use a static table of conditional probabilities per family (I'll tune the values) to compute `fair_joint = P(legA) * P(legB | legA)`. `P(legA)` comes from the Kalshi leg mid, falling back to the ask. Rank the candidates by expected gap and send RFQs only to the top N.
5. **Probe** (`probe.py`). For each chosen candidate:
   - Create the RFQ sized with `target_cost_dollars` at my real stake. The default is $5, configurable in the UI. Makers may quote differently by size, so probe at the size I'd actually bet.
   - Collect quotes for a configurable window of a few seconds.
   - Record the best `yes_bid` and `no_bid`.
   - Cancel the RFQ.

   Run the probes concurrently, but respect the limiter and the one-open-RFQ-per-ticker rule.

   **Quote reliability.** This broke last time, so instrument it:
   - Open the authenticated websocket before sending any RFQs, and keep it open for the whole run.
   - Match each quote to its RFQ by id.
   - If the websocket drops, reconnect and fall back to REST.
   - For every RFQ, log how long until the first quote, how many makers quoted, and "no quote" outcomes along with their reason (rejected combo, 409, timeout, no makers).
   - Show the quote fill rate in the UI after each run.
   - Make the quote window configurable, and log the timing of every quote, so we can tune the window from the data.

   **Candidate coverage.** This also broke last time. After each run, print a coverage report:
   - games found
   - legs found per game, by stat type
   - legs the parser couldn't classify, with examples
   - candidates generated per template family
   - how many existed already, how many were created, and how many were skipped and why

   Fail loudly if a game that has player props produces zero candidates.
6. **Storage** (SQLite through SQLAlchemy or plain sqlite3). Store every leg snapshot, RFQ, quote and run with timestamps. Also store hours-to-start. **Log every pattern feature from day one**, because the model can't learn from anything we didn't record. For each leg, snapshot:
   - bid, ask, spread, top-of-book depth, volume and open interest at probe time
   - how much the leg has moved since the market opened
   - the game's total volume

   For each combo, record:
   - whether it already existed (someone else created it) or we created it
   - how many makers quoted, and the spread between the best and second-best quote
   - RFQ size
   - how often the RFQ feed has shown similar combos

   **This quote log is the most valuable data we will have.** Later we'll fit a model of how the makers price correlation from it.
7. **Ranking and output.** For each probed combo, compute:
   - the implied combo payout (taker side, after fees)
   - the product of leg payouts
   - the ratio
   - prior fair payout and EV%
   - time to start
   - a direct link to the market if one exists

   Sort the results by EV%.
8. **Dashboard.** This is the main way I use the tool, so it should feel like a real dashboard, not a debug page. It's a local web app at `http://localhost:8000`, launched with one command (for example `uv run combo-edge`), which opens the browser automatically. Use FastAPI plus server-rendered HTML with HTMX and Chart.js, with all JS bundled locally (no CDN and no React build step). It has a dark theme and must be readable on a laptop screen. Every phase adds its panels here, and anything the spec says to "show" or "report" lives in this dashboard. Tabs:
   - **Scan.** A big **Run scan** button, the RFQ budget and stake fields, and a live progress bar (legs found, candidates, lookups, creations, RFQs sent, quotes received). Below that, the ranked results table, sortable and filterable by family, team and time to kickoff. Columns: legs, quoted payout, product of leg payouts, ratio, fair payout, EV%, suggested stake, hours to kickoff, and number of makers. Each row links to the market.
   - **My bets.** The **Re-check my combos** button, then my open combo positions pulled from Kalshi with the price paid, latest re-quote and CLV. Below that, exposure per game and per player against the caps, with warnings, and settled history with P&L and CLV over time (chart).
   - **Learning** (Phases 1.5 and 2.5). Exploration progress (quotes per stratum and the strata still under-sampled), the top strata by mean edge with confidence intervals, an edge versus hours-to-kickoff chart, edge versus leg liquidity, maker-model accuracy and drift, newly promoted templates, and the "when to press Run" recommendation.
   - **Health.** Combo creations used this week against the 5,000 cap, rate-limit tier and usage, quote fill rate and time to first quote, the coverage report from the last run (unparsed legs with examples), the auto re-quote panel's on/off toggle and run log, and the last errors.
9. **Tests.** Cover the ratio math against the fixtures above, the leg parser on recorded API responses, the limiter, and fee math. Mock the Kalshi API in tests.

Phase 1 milestone check: I press Run on demo (or prod with read and RFQ permissions), it probes up to N combos, cancels every RFQ, and shows a ranked table. Every quote is in SQLite.

## Phase 1.5: exploration period (2–3 NFL weeks, right after Phase 1)
My edge so far has been gut feel, so the tool has to discover the patterns itself instead of only following my templates. Testing every 2-leg pair is impossible: each game has hundreds of legs across the ladders, so tens of thousands of pairs, against 5,000 creations a week. Run a designed experiment instead:
- **Strata.** Classify every possible same-game 2-leg pair by:
  - leg-type pair (for example QB pass TD × own team total, WR rec yds × opposing QB pass yds)
  - same team, opponent or game-level
  - the threshold "tailness" of each leg (main, alt, extreme, by implied probability bucket)
  - side (yes or no)
- **Sampling.** Each run spends most of its creation budget sampling evenly across strata (stratified random sampling), plus combos that other people already created, which are free. Keep sampling until each stratum has about 30 quotes, then shift budget toward strata with high mean edge or high uncertainty (Thompson sampling, as in Phase 2.5).
- **Time costs no creations.** A combo only has to be created once. Re-quoting it later is just an RFQ. So re-RFQ a fixed panel of created combos at set times (for example 6d, 4d, 2d, 1d, 6h and 1h before kickoff). That measures exactly how the edge moves toward game time without spending creation budget.
  - **This re-quote panel runs automatically**, and it's the one exception to the button-only rule. A local scheduler (launchd on macOS) re-RFQs the panel at those offsets during the exploration weeks. It never creates combos and never places bets. It needs a toggle in the UI, a log of every auto run, and setup notes for keeping the Mac awake (`caffeinate`, or `pmset` wake schedules).
- **Labels.** Edge = (fair joint ÷ implied quote probability) − 1. Use the best fair price available: the prescore table until the Phase 2 fair model lands. Always store the raw quotes and leg snapshots, so every label can be recomputed when the fair model improves. Add CLV from the re-quote panel. Also store the eventual outcome, but don't train on win/loss until there are thousands of settled combos.
- **Output.** At the end of exploration, write a report covering the top 20 strata by mean edge with confidence intervals, edge versus hours-to-start, edge versus leg liquidity, and which template families to keep, drop or add. Then switch the default budget to about 85% exploit and 15% explore.

## Phase 2
- **Our own fair leg prices, with no paid odds feed.** I don't want to depend on sportsbook APIs. Build fair probabilities ourselves:
  - **Game environment.** Derive implied team totals and the win probability from Kalshi's liquid game lines (spread, total, ML), using de-vigged mids.
  - **Player legs.** Use nflverse history to model each player's stat distribution given the team's implied total and the player's recent usage. Model pass TDs, rec yds, TDs and so on. Examples: a QB pass-TD count drawn from the team's TD distribution times his passing share; a WR's yards drawn from targets × yards per target.
  - **Compare to Kalshi.** Show model fair next to the Kalshi leg mid. Where they disagree a lot, flag the leg so I can check it by hand in OddsJam, and blend the two prices. Kalshi wins on liquid legs, and our model gets more weight on thin ones.
  - Keep a pluggable `OddsProvider` interface so a sportsbook or OddsJam feed can be added later. It stays off by default.
- **Conditional probabilities from history.** For the NFL, use nflverse play-by-play to estimate `P(team pts ≥ X | QB pass TD ≥ k)`, conditioned on that game's implied team total. Prefer a simple simulation or regression over raw frequencies in the tails, where the samples are small.
- **Maker model.** Fit the logged ratios by family, threshold and hours-to-start, then use the fit to predict the quote before probing, so the RFQ budget goes where predicted edge is highest.

## Phase 2.5: self-improving search (after Phase 2 works)
- **Learning loop 1 is the maker model**, and it gives fast feedback because every quote is a label. Train a gradient-boosted model (LightGBM) on the logged quotes. It predicts the quoted ratio from these features:
  - template family
  - leg thresholds
  - fair probability of each leg
  - the game's team total and spread
  - hours to start
  - day of week
  - how many makers quoted

  It also uses every liquidity and timing feature logged in Phase 1: leg spread, depth, volume and open interest, game volume, line movement since open, whether the combo already existed, the number of makers and the dispersion between their quotes, and the demand seen in the RFQ feed. Retrain it on every Run, and weight recent data more heavily, because the makers adapt.
- **The timing and liquidity map.** From the same data, show where edge concentrates by hours-to-start bucket (for example 6d / 3d / 1d / 6h / 1h) and by leg-liquidity bucket. Use it to recommend when I should press Run ("edge peaks Tue–Wed, avg +X%"). Later the scheduler will use it to time its own sweeps.
- **Learning loop 2 is the truth model.** Calibrate the fair joint probabilities against nflverse history and settled outcomes. Report calibration per family.
- **The probe policy is a contextual bandit.** Use Thompson sampling to split each run's RFQ and creation budget. Most of the budget goes to the highest predicted edge. A fixed slice (say 15%) goes to exploration: under-sampled families and thresholds, plus random correlated same-game pairs from the leg graph that aren't covered by a template. When an explored pair type keeps showing a large gap between fair and quoted, promote it to a template family automatically and tell me in the run summary.
- **The reward is EV at the quote**, measured as model fair minus the quoted price, together with closing-line value from the re-check. **Never use win/loss P&L as the reward.** On 10x-50x payouts it's far too noisy to learn from at our volume.
- **Show what it learned.** Add a "What the model learned" panel with edge by family, threshold and hours-to-start, plus the newly promoted families and the maker-model drift.

## Phase 3 (later)
- **Sizing and exposure (confirmed: I want this).** Bankroll is a setting in the UI, with a **$2,000** default. Default caps: at most $10 per combo, $40 total per game, $30 total per player, all editable. For example, a 20x combo with +30% edge gives full Kelly ≈ 1.6%, so 1/4 Kelly is about $8. Suggest a stake per play using fractional Kelly (start at 1/4 Kelly, shrunk further by how uncertain the model is), and cap total exposure per game and per player, since many good combos share a leg. Execution enforces these caps, and the dashboard warns when combos stack on the same player.
- **Quote shopping.** For any combo I'm about to buy, re-RFQ it 2–3 times a few seconds apart and show the best quote and the spread between quotes. Also track whether quotes on my RFQs get worse over time compared with similar combos in the public RFQ feed, which would mean makers are fading me.
- An optional full-scan scheduler. Its main job is a heavy sweep when lines first open. Build it, but **leave it off by default** behind a UI toggle.
- **Execution module (auto-place).** At the end of each run (manual, or scheduled once that's turned on), automatically accept the quotes that pass every gate:
  - EV at the quote ≥ a minimum (default +15%), *after* fees, using the blended fair price
  - Model confidence: the stratum has at least 30 quotes logged, and the fair model's uncertainty band still shows +EV at its lower bound
  - The quote is fresh, at most a few seconds old. If it's stale, re-RFQ (quote shopping) and re-check the gates on the new price
  - The stake comes from the 1/4-Kelly sizing and is clipped by the caps ($10 per combo, $40 per game, $30 per player). It also skips any combo that duplicates an open position
  - Daily and weekly loss limits, plus a max number of bets per day. When one trips, execution goes to `off` until I reset it
  - A kill switch in the dashboard that stops everything immediately

  Log every decision, both accepted and rejected, with the reason. Show executed bets and paper bets in the My bets tab, with P&L and CLV kept separate for paper and live. Accept quotes via the RFQ accept endpoint, as the docs describe it. Never place resting orders on the book.

## Working style
- Python 3.12, `uv` or `pip` with a lockfile, type hints, small modules, and a README with setup and run steps.
- Ask me before adding any paid service.
- After each phase, give me a short summary covering what works, what you verified against the live docs, and what you couldn't verify.
