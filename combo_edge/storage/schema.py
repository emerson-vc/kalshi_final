"""SQLite schema (SQLAlchemy Core). Timestamps are UTC epoch seconds (REAL). JSON lives in TEXT.

The quote log (``rfqs`` + ``quotes`` + ``leg_snapshots`` + ``candidates``) is the training data for
the maker model, so raw values are always kept and labels are recomputed from them.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
)

SCHEMA_VERSION = 1
metadata = MetaData()


def _id() -> Column:
    return Column("id", Integer, primary_key=True, autoincrement=True)


meta = Table("meta", metadata, Column("key", Text, primary_key=True), Column("value", Text), Column("updated_at", Float))

settings = Table(
    "settings", metadata, Column("key", Text, primary_key=True), Column("value", Text), Column("updated_at", Float)
)

runs = Table(
    "runs",
    metadata,
    _id(),
    Column("kind", Text),  # scan | requote | recheck | shop | fullscan | canary
    Column("env", Text),
    Column("status", Text),  # running | done | coverage_error | failed | aborted
    Column("started_at", Float),
    Column("finished_at", Float),
    Column("params", Text),
    Column("limits_before", Text),
    Column("limits_after", Text),
    Column("summary", Text),
    Column("coverage", Text),
    Column("error", Text),
)

games = Table(
    "games",
    metadata,
    Column("game_id", Text, primary_key=True),  # Kalshi milestone id
    Column("game_code", Text),  # e.g. 26NOV05JACBAL
    Column("title", Text),
    Column("home_team", Text),
    Column("away_team", Text),
    Column("home_team_uuid", Text),
    Column("away_team_uuid", Text),
    Column("kickoff", Float),
    Column("season", Integer),
    Column("week", Integer),
    Column("related_events", Text),
    Column("updated_at", Float),
)

legs = Table(
    "legs",
    metadata,
    Column("ticker", Text, primary_key=True),
    Column("game_code", Text),
    Column("event_ticker", Text),
    Column("series", Text),
    Column("kind", Text),
    Column("scope", Text),
    Column("team", Text),
    Column("opponent", Text),
    Column("player_name", Text),
    Column("player_uuid", Text),
    Column("player_code", Text),
    Column("jersey", Text),
    Column("position", Text),
    Column("threshold", Float),
    Column("outcome", Text),
    Column("line_label", Text),
    Column("protected", Integer),
    Column("precise", Integer),
    Column("title", Text),
    Column("open_time", Float),
    Column("first_seen", Float),
    Column("last_seen", Float),
    Column("result", Text),  # yes | no | scalar value once settled
    Column("settled_at", Float),
)
Index("ix_legs_game", legs.c.game_code)

leg_snapshots = Table(
    "leg_snapshots",
    metadata,
    _id(),
    Column("run_id", Integer),
    Column("ticker", Text),
    Column("ts", Float),
    Column("hours_to_start", Float),
    Column("yes_bid", Float),
    Column("yes_ask", Float),
    Column("no_bid", Float),
    Column("no_ask", Float),
    Column("yes_bid_size", Float),
    Column("yes_ask_size", Float),
    Column("spread", Float),
    Column("volume", Float),
    Column("open_interest", Float),
    Column("last_price", Float),
    Column("open_price", Float),
    Column("move_since_open", Float),
    Column("game_volume", Float),
    Column("model_p", Float),
    Column("blend_p", Float),
)
Index("ix_legsnap_ticker_ts", leg_snapshots.c.ticker, leg_snapshots.c.ts)
Index("ix_legsnap_run", leg_snapshots.c.run_id)

combos = Table(
    "combos",
    metadata,
    Column("ticker", Text, primary_key=True),
    Column("signature", Text),
    Column("collection", Text),
    Column("game_code", Text),
    Column("event_ticker", Text),
    Column("legs", Text),
    Column("n_legs", Integer),
    Column("source", Text),  # ours_new | ours_resolved | feed | rest | position
    Column("first_seen", Float),
    Column("kalshi_created_time", Float),
    Column("last_rfq_at", Float),
    Column("result", Text),
    Column("settled_at", Float),
)
Index("ix_combos_sig", combos.c.signature, unique=True)
Index("ix_combos_game", combos.c.game_code)

combo_rejections = Table(
    "combo_rejections",
    metadata,
    Column("signature", Text, primary_key=True),
    Column("reason_code", Text),
    Column("message", Text),
    Column("offending", Text),
    Column("rule_class", Text),
    Column("ts", Float),
    Column("run_id", Integer),
)

creation_ledger = Table(
    "creation_ledger",
    metadata,
    _id(),
    Column("ts", Float),
    Column("env", Text),
    Column("run_id", Integer),
    Column("signature", Text),
    Column("result", Text),  # new | resolved | rejected | error
    Column("combo_ticker", Text),
    Column("http_status", Integer),
    Column("detail", Text),
)
Index("ix_ledger_ts", creation_ledger.c.env, creation_ledger.c.ts)

candidates = Table(
    "candidates",
    metadata,
    _id(),
    Column("run_id", Integer),
    Column("signature", Text),
    Column("game_code", Text),
    Column("family", Text),
    Column("stratum_l1", Text),
    Column("stratum_l2", Text),
    Column("legs", Text),  # [[ticker, side], ...]
    Column("p_a", Float),
    Column("p_b", Float),
    Column("p_b_given_a", Float),
    Column("fair_joint", Float),
    Column("fair_lower", Float),
    Column("fair_source", Text),
    Column("fair_version", Text),
    Column("leg_payout_product", Float),
    Column("expected_ratio", Float),
    Column("expected_payout", Float),
    Column("expected_ev", Float),
    Column("unpriced", Integer),
    Column("pool", Text),  # exploit | explore | free | unpriced | panel
    Column("rank", Integer),
    Column("combo_ticker", Text),
    Column("existed", Integer),
    Column("hours_to_start", Float),
    Column("team_total_main", Float),
    Column("game_total_main", Float),
    Column("spread_main", Float),
    Column("feed_demand", Integer),
    Column("predicted_ratio", Float),
)
Index("ix_cand_run", candidates.c.run_id)
Index("ix_cand_sig", candidates.c.signature)

rfqs = Table(
    "rfqs",
    metadata,
    Column("rfq_id", Text, primary_key=True),
    Column("env", Text),
    Column("run_id", Integer),
    Column("candidate_id", Integer),
    Column("combo_ticker", Text),
    Column("signature", Text),
    Column("purpose", Text),  # probe | requote | recheck | shop | canary
    Column("target_cost", Float),
    Column("created_at", Float),
    Column("deleted_at", Float),
    Column("window_s", Float),
    Column("outcome", Text),  # quoted | no_quotes_timeout | rejected_combo | conflict_409 | create_error | ...
    Column("reason", Text),
    Column("first_quote_ms", Float),
    Column("n_quotes", Integer),
    Column("n_makers", Integer),
    Column("best_yes_bid", Float),
    Column("best_no_bid", Float),
    Column("second_no_bid", Float),
    Column("taker_yes_price", Float),
    Column("taker_yes_contracts", Float),
    Column("combo_payout", Float),
    Column("ws_ok", Integer),
    Column("rest_used", Integer),
    Column("hours_to_start", Float),
    Column("offset_h", Float),
    Column("cancel_attempts", Integer),
    Column("status", Text),  # open | closed | executed
    Column("feed_demand_pair", Integer),
)
Index("ix_rfqs_combo", rfqs.c.combo_ticker)
Index("ix_rfqs_run", rfqs.c.run_id)

quotes = Table(
    "quotes",
    metadata,
    _id(),
    Column("quote_id", Text),
    Column("rfq_id", Text),
    Column("maker_id", Text),
    Column("yes_bid", Float),
    Column("no_bid", Float),
    Column("yes_contracts_offered", Float),
    Column("no_contracts_offered", Float),
    Column("created_ts", Float),
    Column("recv_ts", Float),
    Column("latency_ms", Float),
    Column("source", Text),  # ws | rest | recorder
    UniqueConstraint("quote_id", "yes_bid", "no_bid", "source", name="uq_quote_obs"),
)
Index("ix_quotes_rfq", quotes.c.rfq_id)

pending_cancels = Table(
    "pending_cancels",
    metadata,
    Column("rfq_id", Text, primary_key=True),
    Column("env", Text),
    Column("first_failed_at", Float),
    Column("attempts", Integer),
    Column("last_error", Text),
)

rfq_leases = Table(
    "rfq_leases",
    metadata,
    Column("ticker", Text, primary_key=True),
    Column("holder", Text),
    Column("expires_at", Float),
)

feed_rfqs = Table(
    "feed_rfqs",
    metadata,
    Column("rfq_id", Text, primary_key=True),
    Column("env", Text),
    Column("ts", Float),
    Column("combo_ticker", Text),
    Column("game_code", Text),
    Column("signature", Text),
    Column("legs", Text),
    Column("n_legs", Integer),
    Column("creator_hash", Text),
    Column("contracts", Float),
    Column("target_cost", Float),
)
Index("ix_feed_ts", feed_rfqs.c.ts)
Index("ix_feed_sig", feed_rfqs.c.signature)

feed_pair_counts = Table(
    "feed_pair_counts",
    metadata,
    Column("day", Text),
    Column("leg_a", Text),
    Column("side_a", Text),
    Column("leg_b", Text),
    Column("side_b", Text),
    Column("n", Integer),
    PrimaryKeyConstraint("day", "leg_a", "side_a", "leg_b", "side_b"),
)

feed_leg_counts = Table(
    "feed_leg_counts",
    metadata,
    Column("day", Text),
    Column("leg", Text),
    Column("side", Text),
    Column("n", Integer),
    PrimaryKeyConstraint("day", "leg", "side"),
)

requote_panel = Table(
    "requote_panel",
    metadata,
    Column("combo_ticker", Text, primary_key=True),
    Column("env", Text),
    Column("signature", Text),
    Column("game_code", Text),
    Column("kickoff", Float),
    Column("added_at", Float),
    Column("reason", Text),
    Column("stratum_l1", Text),
    Column("active", Integer),
)

requote_slots = Table(
    "requote_slots",
    metadata,
    Column("combo_ticker", Text),
    Column("offset_h", Float),
    Column("due_at", Float),
    Column("status", Text),  # pending | done | missed | skipped_past | skipped_unopened | failed
    Column("rfq_id", Text),
    Column("done_at", Float),
    PrimaryKeyConstraint("combo_ticker", "offset_h"),
)

positions = Table(
    "positions",
    metadata,
    _id(),
    Column("ts", Float),
    Column("env", Text),
    Column("ticker", Text),
    Column("position", Float),
    Column("market_exposure", Float),
    Column("realized_pnl", Float),
    Column("fees_paid", Float),
    Column("raw", Text),
)

fills = Table(
    "fills",
    metadata,
    Column("fill_id", Text, primary_key=True),
    Column("env", Text),
    Column("ts", Float),
    Column("ticker", Text),
    Column("outcome_side", Text),
    Column("action", Text),
    Column("count", Float),
    Column("price", Float),
    Column("order_id", Text),
    Column("is_taker", Integer),
    Column("raw", Text),
)

settlements = Table(
    "settlements",
    metadata,
    _id(),
    Column("env", Text),
    Column("ts", Float),
    Column("ticker", Text),
    Column("revenue", Float),
    Column("result", Text),
    Column("raw", Text),
    UniqueConstraint("env", "ticker", name="uq_settlement"),
)

model_versions = Table(
    "model_versions",
    metadata,
    _id(),
    Column("kind", Text),  # maker | fair | calibration
    Column("created_at", Float),
    Column("n_train", Integer),
    Column("metrics", Text),
    Column("params", Text),
    Column("path", Text),
)

predictions = Table(
    "predictions",
    metadata,
    _id(),
    Column("candidate_id", Integer),
    Column("model_version_id", Integer),
    Column("predicted_ratio", Float),
    Column("predicted_edge", Float),
)

promotions = Table(
    "promotions",
    metadata,
    _id(),
    Column("ts", Float),
    Column("family_key", Text),
    Column("definition", Text),
    Column("evidence", Text),
    Column("active", Integer),
)

execution_decisions = Table(
    "execution_decisions",
    metadata,
    _id(),
    Column("ts", Float),
    Column("run_id", Integer),
    Column("env", Text),
    Column("mode", Text),
    Column("candidate_id", Integer),
    Column("signature", Text),
    Column("combo_ticker", Text),
    Column("rfq_id", Text),
    Column("quote_id", Text),
    Column("decision", Text),  # accept | paper_fill | reject | shop | abort | canary
    Column("reason", Text),
    Column("gates", Text),
    Column("stake", Float),
    Column("price", Float),
    Column("contracts", Float),
    Column("payout", Float),
    Column("ev", Float),
    Column("ev_lower", Float),
    Column("fair", Float),
)
Index("ix_exdec_ts", execution_decisions.c.ts)

executions = Table(
    "executions",
    metadata,
    _id(),
    Column("ts", Float),
    Column("book", Text),  # paper | live
    Column("env", Text),
    Column("decision_id", Integer),
    Column("run_id", Integer),
    Column("combo_ticker", Text),
    Column("signature", Text),
    Column("game_code", Text),
    Column("players", Text),
    Column("legs", Text),
    Column("rfq_id", Text),
    Column("quote_id", Text),
    Column("accepted_side", Text),
    Column("price", Float),
    Column("contracts", Float),
    Column("stake", Float),
    Column("fee", Float),
    Column("payout", Float),
    Column("fair", Float),
    Column("ev", Float),
    Column("status", Text),  # pending | executed | maker_no_confirm | aborted | failed | settled
    Column("order_id", Text),
    Column("verified", Integer),
    Column("verify_detail", Text),
    Column("is_canary", Integer),
    Column("result", Text),  # win | loss | void
    Column("pnl", Float),
    Column("settled_at", Float),
    Column("close_payout", Float),
    Column("clv", Float),
)
Index("ix_exec_book_ts", executions.c.book, executions.c.ts)

risk_state = Table(
    "risk_state", metadata, Column("key", Text, primary_key=True), Column("value", Text), Column("updated_at", Float)
)

errors = Table(
    "errors",
    metadata,
    _id(),
    Column("ts", Float),
    Column("where", Text),
    Column("message", Text),
    Column("detail", Text),
)

limits_log = Table(
    "limits_log",
    metadata,
    _id(),
    Column("ts", Float),
    Column("env", Text),
    Column("run_id", Integer),
    Column("phase", Text),  # before | after | startup
    Column("usage_tier", Text),
    Column("body", Text),
)

http_stats = Table(
    "http_stats",
    metadata,
    Column("day", Text),
    Column("env", Text),
    Column("endpoint", Text),
    Column("status", Integer),
    Column("n", Integer),
    PrimaryKeyConstraint("day", "env", "endpoint", "status"),
)
