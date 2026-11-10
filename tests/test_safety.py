"""Hard-rule guards: allowlist, privileged accept route, forbidden endpoints anywhere in the source."""

import re
from pathlib import Path

import pytest

from combo_edge import safety

PKG = Path(__file__).resolve().parent.parent / "combo_edge"
# The mock exchange SERVES the accept route for tests; execution.py is the only caller.
ACCEPT_ALLOWED_FILES = {PKG / "execution.py"}
ACCEPT_SERVER_DIR = PKG / "mock"


def _source_files() -> list[Path]:
    exts = {".py", ".html", ".js", ".yaml", ".yml", ".css"}
    return [p for p in PKG.rglob("*") if p.is_file() and p.suffix in exts and "vendor" not in p.parts]


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/markets"),
        ("GET", "/events/KXNFLGAME-26NOV05JACBAL"),
        ("GET", "/communications/rfqs/abc/quotes/def"),
        ("POST", "/communications/rfqs"),
        ("DELETE", "/communications/rfqs/abc"),
        ("POST", "/multivariate_event_collections/KXMVECROSSCATEGORY-R"),
        ("POST", "/account/api_usage_level/upgrade"),
    ],
)
def test_allowed(method: str, path: str) -> None:
    assert safety.check(method, path) == "public"


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/portfolio/orders"),
        ("POST", "/portfolio/events/orders"),
        ("POST", "/portfolio/events/orders/batched"),
        ("DELETE", "/portfolio/orders/x"),
        ("POST", "/portfolio/order_groups/create"),
        ("POST", "/communications/quotes"),
        ("PUT", "/communications/quotes/q1/accept"),
        ("PUT", "/communications/rfqs/r1/quotes/q1/confirm"),
        ("POST", "/communications/rfq_batches"),
        ("POST", "/communications/block-trade-proposals"),
        ("PUT", "/communications/rfqs/r1/quotes/q1/accept"),  # privileged: needs the execution capability
        ("GET", "/markets?status=open"),
    ],
)
def test_forbidden_without_capability(method: str, path: str) -> None:
    import combo_edge.execution  # noqa: F401 - registers the privileged accept route

    with pytest.raises(safety.ForbiddenEndpoint):
        safety.check(method, path)


def test_privileged_accept_needs_the_exact_capability() -> None:
    from combo_edge import execution

    with pytest.raises(safety.ForbiddenEndpoint):
        safety.check("PUT", "/communications/rfqs/r1/quotes/q1/accept", capability=object())
    assert safety.check("PUT", "/communications/rfqs/r1/quotes/q1/accept", capability=execution._CAPABILITY) == "privileged"


def test_only_execution_module_may_register_privileged_routes() -> None:
    with pytest.raises(safety.ForbiddenEndpoint):
        safety.register_privileged("PUT", r"^/x$", object(), owner_module="combo_edge.scan")


def test_accept_path_only_in_execution_module() -> None:
    offenders = []
    for p in _source_files():
        if p in ACCEPT_ALLOWED_FILES or ACCEPT_SERVER_DIR in p.parents:
            continue
        text = p.read_text(errors="ignore")
        if re.search(r"/accept\b|accept_rfq_quote|AcceptRFQQuote", text):
            offenders.append(str(p.relative_to(PKG)))
    assert not offenders, f"accept path outside execution.py: {offenders}"


FORBIDDEN_ANYWHERE = [
    r"portfolio/orders",
    r"portfolio/events/orders",
    r"order_groups",
    r"block-trade",
    r"block_trade",
    r"rfq_batches",
    r"/batched",
    r"/confirm\b",
    r"communications/quotes/\{",
    r"replace_existing\"?\s*[:=]\s*True",
    r"rest_remainder\"?\s*[:=]\s*True",
    r"\"rest_remainder\":\s*true",
]


def test_no_forbidden_endpoints_anywhere() -> None:
    offenders = []
    for p in _source_files():
        text = p.read_text(errors="ignore")
        for rx in FORBIDDEN_ANYWHERE:
            if re.search(rx, text):
                offenders.append(f"{p.relative_to(PKG)}: {rx}")
    assert not offenders, offenders


def test_rfq_body_never_rests_remainder() -> None:
    src = (PKG / "kalshi" / "api.py").read_text()
    assert '"rest_remainder": False' in src
    # Hidden by default everywhere. Your ID is shown only by the Shop button's comparison round and by the
    # top-N re-quote in id_requote.py (capped by id_requote_top); scan.py itself never sets the flag.
    assert "obscure_creator_id: bool = True" in src
    assert "obscure_creator_id: bool = True" in (PKG / "probe.py").read_text()
    assert "obscure_creator_id" not in (PKG / "scan.py").read_text()
    assert "replace_existing" not in src
