"""Configuration.

Two layers:
- ``EnvSettings``: secrets and process settings from ``.env`` / environment variables.
- ``RuntimeSettings``: tunables you edit in the dashboard, persisted in SQLite (``settings`` table).

Secrets are never logged or echoed. Errors name the variable, never its value.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

EnvName = Literal["demo", "prod", "mock"]

REST_BASES: dict[str, str] = {
    "prod": "https://external-api.kalshi.com/trade-api/v2",
    "demo": "https://external-api.demo.kalshi.co/trade-api/v2",
    "mock": "https://mock.kalshi.local/trade-api/v2",
}
WS_URLS: dict[str, str] = {
    "prod": "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
    "demo": "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
    "mock": "wss://mock.kalshi.local/trade-api/ws/v2",
}
REST_PATH_PREFIX = "/trade-api/v2"
WS_SIGN_PATH = "/trade-api/ws/v2"


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    kalshi_env: EnvName = "demo"
    kalshi_demo_api_key_id: str = ""
    kalshi_demo_private_key_path: str = ""
    kalshi_demo_private_key_pem: str = ""
    kalshi_prod_api_key_id: str = ""
    kalshi_prod_private_key_path: str = ""
    kalshi_prod_private_key_pem: str = ""
    # Legacy single-key names; they apply to whatever KALSHI_ENV is.
    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = ""

    combo_edge_data_dir: str = "data"
    combo_edge_host: str = "127.0.0.1"
    combo_edge_port: int = 8000
    combo_edge_forbid_live: bool = False


@dataclass(frozen=True)
class Credentials:
    env: str
    key_id: str
    private_key_pem: bytes

    @property
    def masked_key_id(self) -> str:
        return (self.key_id[:4] + "…") if self.key_id else ""


class CredentialError(Exception):
    """Raised with a message that names variables but never echoes secret values."""


def load_env_settings() -> EnvSettings:
    # COMBO_EDGE_ENV_FILE lets tests (and alternate setups) point away from the repo's .env
    return EnvSettings(_env_file=os.environ.get("COMBO_EDGE_ENV_FILE") or ".env")  # type: ignore[call-arg]


def credentials_for(env: str, es: EnvSettings | None = None) -> Credentials | None:
    """Return credentials for ``env`` or None if none are configured.

    Raises CredentialError if they are configured but unusable (missing file, bad PEM header).
    """
    if env == "mock":
        return None
    es = es or load_env_settings()
    prefix = f"kalshi_{env}_"
    key_id = getattr(es, prefix + "api_key_id", "") or ""
    path = getattr(es, prefix + "private_key_path", "") or ""
    pem = getattr(es, prefix + "private_key_pem", "") or ""
    if not key_id and es.kalshi_env == env:
        key_id = es.kalshi_api_key_id
        path = path or es.kalshi_private_key_path
    if not key_id and not path and not pem:
        return None
    var = f"KALSHI_{env.upper()}"
    if not key_id:
        raise CredentialError(f"{var}_API_KEY_ID is empty")
    data: bytes
    if pem:
        data = pem.replace("\\n", "\n").encode()
    elif path:
        p = Path(os.path.expanduser(path))
        if not p.is_file():
            raise CredentialError(f"{var}_PRIVATE_KEY_PATH points to a file that does not exist ({p})")
        data = p.read_bytes()
    else:
        raise CredentialError(f"set {var}_PRIVATE_KEY_PATH (or {var}_PRIVATE_KEY_PEM)")
    if b"PRIVATE KEY-----" not in data:
        raise CredentialError(f"the {var} private key is not a PEM private key")
    return Credentials(env=env, key_id=key_id.strip(), private_key_pem=data)


def data_dir(es: EnvSettings | None = None) -> Path:
    es = es or load_env_settings()
    p = Path(es.combo_edge_data_dir).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def live_forbidden(es: EnvSettings | None = None) -> bool:
    es = es or load_env_settings()
    return bool(es.combo_edge_forbid_live) or os.environ.get("COMBO_EDGE_FORBID_LIVE", "") in {"1", "true", "yes"}


class RuntimeSettings(BaseModel):
    """Everything the dashboard can edit. Defaults follow the spec and the approved plan."""

    # --- scan / probe ---
    stake: float = Field(5.0, ge=0.5, le=10.0, description="RFQ target cost in dollars (fee-inclusive)")
    rfq_budget: int = Field(500, ge=1, le=5000, description="Max RFQs per run")
    max_new_creations_per_run: int = Field(500, ge=0, le=5000)
    weekly_creation_cap: int = 5000
    creation_reserve: int = Field(300, ge=0, description="Weekly creations left for your own app use")
    quote_window_s: float = Field(5.0, ge=1.0, le=30.0)
    max_open_rfqs: int = Field(40, ge=1, le=90)
    rate_utilization: float = Field(0.6, gt=0.05, le=0.9)
    max_candidates: int = 150_000
    max_legs: int = Field(2, ge=2, le=6)
    collection_ticker: str = "KXMVECROSSCATEGORY-R"
    hours_ahead_max: float = 216.0
    skip_starting_within_min: float = 10.0
    rest_crosscheck_frac: float = 0.05
    unpriced_share: float = 0.05
    # --- exploration / bandit ---
    exploration_mode: bool = True
    prot_in_exploration: bool = True
    # RFQ budget split per run (must sum to 1). Core = the sweeps in config/core.yaml.
    core_share: float = Field(0.50, ge=0.0, le=1.0)
    exploit_share: float = Field(0.25, ge=0.0, le=1.0)
    free_share: float = Field(0.15, ge=0.0, le=1.0)
    explore_share: float = Field(0.10, ge=0.0, le=1.0)
    core_requote_after_h: float = 6.0  # core combos quoted this recently go to the back of the next run's queue
    core_requote_move: float = 0.03  # ...unless a leg's price moved more than this (3c) since that quote
    # Payout band for the core, exploit and free pools (expected payout, or the leg-payout product). Combos
    # outside it can only be sent by the explore and unpriced pools.
    rank_payout_min: float = Field(8.0, ge=1.0)
    rank_payout_max: float = Field(50.0, ge=1.0)
    # Families that only the explore pool may send until promoted or until the bandit shows a positive
    # posterior over at least ``explore_only_release_quotes`` quotes. All no_* families and exploration
    # strata pairs (explore:*) are explore-only too.
    explore_only_families: list[str] = Field(default_factory=lambda: ["rb_x_ml_spread", "qb_x_game_total"])
    explore_only_release_quotes: int = 30
    bandit_min_clv_obs: int = 10  # arms with fewer CLV observations get half the model-EV reward
    stratum_target_quotes: int = 30
    promotion_min_quotes: int = 30
    promotion_min_games: int = 3
    promotion_edge: float = 0.05
    promotion_prob: float = 0.90
    maker_half_life_days: float = 7.0
    # --- re-quote panel ---
    requote_enabled: bool = True  # re-RFQs panel combos only: never creates combos, never bets
    requote_offsets_h: list[float] = Field(default_factory=lambda: [144, 96, 72, 48, 24, 12, 6, 1])
    panel_max: int = 150
    panel_top_k: int = 20
    panel_random_per_run: int = 30
    # --- sizing / exposure ---
    bankroll: float = 2000.0
    kelly_fraction: float = 0.25
    cap_combo: float = 10.0
    cap_game: float = 40.0
    cap_player: float = 30.0
    min_stake: float = 1.0
    stack_warn: int = 3
    # --- execution gates ---
    ev_min: float = 0.15
    stratum_min_quotes: int = 30
    fair_lower_pct: float = 0.10
    max_quote_age_s: float = 3.0
    stake_tolerance: float = 0.5
    reshop_near_miss: float = 0.03
    daily_loss_limit: float = 40.0
    weekly_loss_limit: float = 120.0
    max_bets_per_day: int = 6
    canary_stake: float = 1.0
    side_check_min_quotes: int = 20
    # --- shopping ---
    shop_rounds: int = 3
    shop_gap_s: float = 4.0
    # --- schedulers ---
    fullscan_enabled: bool = False
    fullscan_check_min: int = 30
    # early-week sweep: at most once a day, Tue-Thu 9am-9pm local, only when player props are open
    fullscan_early_week: bool = False
    fullscan_early_days: list[str] = Field(default_factory=lambda: ["Tue", "Wed", "Thu"])
    fullscan_early_hours: tuple[int, int] = (9, 21)
    # --- misc ---
    market_link_template: str = "https://kalshi.com/markets/{series_lower}/{event_lower}"

    @model_validator(mode="after")
    def _shares_sum_to_one(self) -> "RuntimeSettings":
        total = self.core_share + self.exploit_share + self.free_share + self.explore_share
        if self.rank_payout_min >= self.rank_payout_max:
            raise ValueError("payout band: min must be below max")
        if abs(total - 1.0) > 0.011:
            raise ValueError(f"core + exploit + free + explore shares must add up to 100% (got {total:.0%})")
        return self

    def budget_shares(self) -> dict[str, float]:
        """RFQ budget shares for this run, by pool."""
        return {"core": self.core_share, "exploit": self.exploit_share, "free": self.free_share, "explore": self.explore_share}

    def merged(self, changes: dict[str, Any]) -> "RuntimeSettings":
        data = self.model_dump()
        data.update({k: v for k, v in changes.items() if k in data})
        return RuntimeSettings.model_validate(data)
