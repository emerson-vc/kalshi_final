"""``combo-edge fit``: retrain the maker model, rerun truth-model calibration, refresh the timing map."""

from __future__ import annotations

from typing import Any

from combo_edge.context import default_db


def fit_all(env: str) -> dict[str, Any]:
    from combo_edge.learning.calibration import run_calibration
    from combo_edge.learning.maker_model import train
    from combo_edge.learning.timing import update_recommendation
    from combo_edge.scan import runtime_settings

    db = default_db()
    m = train(db, env, runtime_settings(db).maker_half_life_days)
    cal = run_calibration(db, env)
    rec = update_recommendation(db, env)
    return {"maker": None if m is None else {"kind": m.kind, "n": m.n_train}, "calibration": cal["text"], "recommendation": rec}
