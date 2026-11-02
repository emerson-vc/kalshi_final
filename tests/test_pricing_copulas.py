import numpy as np
import pytest
import scipy.stats as st

from combo_edge import pricing
from combo_edge.copulas import Copula, fit_to_anchors, t_joint_yes
from combo_edge.prescore import load_priors
from combo_edge.sizing import kelly_stake

# Your hand-found fixtures: (leg payouts, combo payout, ratio)
FIXTURES = [
    ([5.56, 1.79], 10.00, 1.01),
    ([23.42, 1.90], 49.02, 1.10),
    ([13.40, 3.28], 22.83, 0.52),
    ([15.63, 4.31], 34.60, 0.51),
    ([1.73, 18.73], 27.55, 0.85),
    ([1.97, 2.09, 1.97], 8.18, 1.01),
]


@pytest.mark.parametrize("legs,combo,expected", FIXTURES)
def test_ratio_matches_fixtures(legs: list[float], combo: float, expected: float) -> None:
    r = pricing.ratio(combo, legs)
    assert r is not None and abs(r - expected) <= 0.011


@pytest.mark.parametrize("payout,cents", [(5.56, 17), (23.42, 4), (13.40, 7), (15.63, 6), (4.31, 22), (18.73, 5), (1.73, 56)])
def test_fixture_payouts_are_fee_inclusive_whole_cent_prices(payout: float, cents: int) -> None:
    assert abs(pricing.price_from_payout(payout) - cents / 100) < 0.0015
    assert abs(pricing.payout_fee_inclusive(cents / 100) - payout) / payout < 0.003


def test_target_cost_sizing_reproduces_darnold_combo() -> None:
    # $5 at 2.7c, whole contracts: 173 contracts -> 34.60x (the app's number)
    c = pricing.contracts_for_target(5.0, 0.027, step=1.0)
    assert c == 173
    assert round(c / 5.0, 2) == 34.60
    # fee-inclusive: principal + fee never exceeds the target
    c2 = pricing.contracts_for_target(5.0, 0.027)
    assert c2 * 0.027 + pricing.taker_fee(c2, 0.027) <= 5.0


def test_taker_fee_formula() -> None:
    assert pricing.taker_fee(100, 0.5) == 1.75  # 0.07*100*0.25 = 1.75
    assert pricing.taker_fee(10, 0.04) == 0.03  # 0.0269 -> rounded up to the cent


def test_taker_yes_price_is_one_minus_no_bid() -> None:
    assert pricing.taker_yes_price(0.94) == pytest.approx(0.06)
    assert pricing.taker_yes_price(None) is None


def test_kelly_example_from_spec() -> None:
    payout, ev = 20.0, 0.30
    fair = (1 + ev) / payout
    assert pricing.kelly_fraction(fair, payout) == pytest.approx(0.0158, abs=2e-4)
    assert kelly_stake(fair, fair, payout, 2000, 0.25, 10.0) == pytest.approx(7.89, abs=0.05)


def test_t_copula_matches_scipy_in_deep_tails() -> None:
    for rho in (-0.4, 0.3, 0.75):
        for pa, pb in [(0.17, 0.54), (0.002, 0.3), (0.04, 0.51), (0.01, 0.01)]:
            a, b = st.t.isf(pa, 4), st.t.isf(pb, 4)
            ref = st.multivariate_t(loc=[0, 0], shape=[[1, rho], [rho, 1]], df=4).cdf([-a, -b], maxpts=2_000_000, random_state=3)
            mine = float(t_joint_yes(np.array([pa]), np.array([pb]), rho, 4)[0])
            assert abs(mine - ref) / ref < 0.01


def test_copula_sides_partition_probability() -> None:
    c = Copula("t", 0.6, 4)
    pa, pb = np.array([0.2, 0.05]), np.array([0.5, 0.3])
    total = sum(c.joint(pa, pb, np.array([x, x]), np.array([y, y])) for x in (True, False) for y in (True, False))
    assert np.allclose(total, 1.0)


def test_tail_dependence_positive_and_never_gaussian() -> None:
    pri = load_priors()
    for name, fam in pri.families.items():
        assert fam.copula.kind in ("t", "gumbel", "clayton"), name
        if fam.copula.theta > 0:
            assert fam.copula.tail_dependence() > 0.0, name


def test_anchors_reproduced() -> None:
    pri = load_priors()
    f1 = pri.families["qb_ptd_x_team_total"]
    conds = [float(f1.copula.joint_yes(a, b)[0] / a) for a, b in [(0.17, 0.54), (0.07, 0.29), (0.06, 0.22)]]
    assert abs(np.mean(conds) - 0.88) < 0.03
    f4 = pri.families["wr_td_x_team_total"]
    assert abs(float(f4.copula.joint_yes(0.04, 0.51)[0] / 0.04) - 0.92) < 0.02
    f5 = pri.families["ml_x_h1_spread"]
    assert abs(float(f5.copula.joint_yes(0.19, 0.55)[0] / 0.19) - 0.85) < 0.02


def test_gumbel_and_clayton_fit() -> None:
    for kind in ("gumbel", "clayton"):
        c = fit_to_anchors([(0.04, 0.51, 0.92)], kind)  # type: ignore[arg-type]
        assert abs(float(c.joint_yes(0.04, 0.51)[0] / 0.04) - 0.92) < 0.03
        assert c.tail_dependence() > 0
