"""NFL team codes as Kalshi writes them, plus aliases to nflverse codes."""

from __future__ import annotations

KALSHI_TEAMS = {
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN", "DET", "GB", "HOU", "IND", "JAC",
    "KC", "LAC", "LAR", "LV", "MIA", "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB", "TEN", "WAS",
}
# Kalshi code -> nflverse code
TO_NFLVERSE = {"JAC": "JAX", "LAR": "LA"}
FROM_NFLVERSE = {v: k for k, v in TO_NFLVERSE.items()}


def split_game_code(game_code: str, known: set[str] | None = None) -> tuple[str, str] | None:
    """'26NOV05JACBAL' -> ('JAC', 'BAL') (away, home). Uses ``known`` codes first, then the full list."""
    letters = game_code[7:] if len(game_code) > 7 and game_code[:2].isdigit() else game_code
    for pool in ([known] if known else []) + [KALSHI_TEAMS]:
        if not pool:
            continue
        for i in range(2, len(letters) - 1):
            a, h = letters[:i], letters[i:]
            if a in pool and h in pool:
                return a, h
    return None


def nflverse_code(team: str | None) -> str | None:
    if team is None:
        return None
    return TO_NFLVERSE.get(team, team)
