"""Auto-promotion: an explored pair type (Level-1 stratum not covered by a template) that keeps showing
a large fair-vs-quoted gap becomes a template family, and the run summary tells you.

Rule (configurable): >= promotion_min_quotes quotes across >= promotion_min_games games and
P(mean reward > promotion_edge) > promotion_prob under the stratum's posterior.
A promoted family generates every ladder rung of the two leg types with the stratum's relation.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterator
from typing import Any

from combo_edge import timeutil
from combo_edge.candidates import FAMILIES, Family, GameIndex, register_runtime_family
from combo_edge.learning.bandit import Posteriors, rewards
from combo_edge.learning.labels import edge_labels
from combo_edge.settings import RuntimeSettings
from combo_edge.sports.base import SidedLeg
from combo_edge.storage import Database, loads
from combo_edge.storage import schema as S
from combo_edge.strata import leg_type, relation

log = logging.getLogger(__name__)


def template_strata(db: Database, env: str) -> set[str]:
    return {r["stratum_l1"] for r in db.fetchall("SELECT DISTINCT stratum_l1 FROM candidates WHERE family NOT LIKE 'explore:%' AND family NOT LIKE 'feed:%'")
            if r["stratum_l1"]}


def check_promotions(db: Database, env: str, st: RuntimeSettings) -> list[dict[str, Any]]:
    post = Posteriors(rewards(db, env))
    labels = edge_labels(db, env)
    games: dict[str, set[str]] = defaultdict(set)
    for l in labels:
        if l.get("stratum_l1"):
            games[l["stratum_l1"]].add(l.get("game_code") or "")
    covered = template_strata(db, env)
    existing = {r["family_key"] for r in db.fetchall("SELECT family_key FROM promotions WHERE active=1")}
    promoted = []
    for arm, (m, se, n) in post.stats.items():
        if arm in covered or arm in existing or n < st.promotion_min_quotes or len(games[arm]) < st.promotion_min_games:
            continue
        p = post.prob_above(arm, st.promotion_edge)
        if p > st.promotion_prob:
            ta, tb, rel = _parse(arm)
            definition = {"type_a": ta, "type_b": tb, "relation": rel}
            evidence = {"n": n, "games": len(games[arm]), "mean": m, "se": se, "prob": p}
            db.insert(S.promotions, {"ts": timeutil.now(), "family_key": arm, "definition": definition, "evidence": evidence, "active": 1})
            register_promoted(arm, definition)
            promoted.append({"family": arm, **evidence})
    return promoted


def _parse(l1: str) -> tuple[str, str, str]:
    pair, rel = l1.rsplit("|", 1)
    a, _, b = pair.partition("×")
    return a, b, rel


def _generator(definition: dict[str, Any]):  # type: ignore[no-untyped-def]
    ta, tb, rel = definition["type_a"], definition["type_b"], definition["relation"]

    def gen(idx: GameIndex) -> Iterator[tuple[SidedLeg, SidedLeg]]:
        a_side = "yes"
        by_type: dict[str, list[Any]] = defaultdict(list)
        for leg in idx.legs:
            by_type[leg_type(SidedLeg(leg, a_side))].append(leg)
        for la in by_type.get(ta, []):
            for lb in by_type.get(tb, []):
                if la.ticker == lb.ticker:
                    continue
                for sa in ("yes", "no"):
                    for sb in ("yes", "no"):
                        a, b = SidedLeg(la, sa), SidedLeg(lb, sb)
                        if relation(a, b) == rel:
                            yield a, b

    return gen


def register_promoted(key: str, definition: dict[str, Any]) -> None:
    name = f"promoted:{key}"
    if name not in FAMILIES:
        register_runtime_family(Family(name, f"Auto-promoted from exploration: {key}", _generator(definition), prior_key=name, definition=definition))


def load_promotions(db: Database) -> int:
    n = 0
    for r in db.fetchall("SELECT family_key, definition FROM promotions WHERE active=1"):
        register_promoted(r["family_key"], loads(r["definition"], {}) or {})
        n += 1
    return n
