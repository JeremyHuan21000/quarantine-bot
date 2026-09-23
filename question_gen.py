"""
Turns a fixture into a hard-to-guess daily question.

Weighted toward markets that are NOT close to a coinflip, since the
point of quarantine release is that it should be genuinely difficult:
- Match Result (3-way: Home/Draw/Away) - weighted heaviest, draws are a trap
- Over/Under with an extreme line (0.5, 1.5, 4.5, 5.5) - lopsided, easy to
  misjudge in either direction
- BTTS and the "obvious" 2.5 goals line are included but weighted low,
  since they're closer to a genuine 50/50 in most matches
"""
import random

QTYPE_RESULT = "result"
QTYPE_OVER_UNDER = "over_under"
QTYPE_BTTS = "btts"

# (qtype, weight)
WEIGHTED_TYPES = [
    (QTYPE_RESULT, 5),
    (QTYPE_OVER_UNDER, 4),
    (QTYPE_BTTS, 1),
]

# Lines biased toward the extremes rather than the obvious middle (2.5)
OVER_UNDER_LINES = [0.5, 1.5, 4.5, 5.5]


def _weighted_choice(pairs):
    types, weights = zip(*pairs)
    return random.choices(types, weights=weights, k=1)[0]


def generate_question(match: dict):
    """
    Given a football-data.org match dict, return:
    (qtype, question_text, options, extra) where extra holds anything
    needed later to resolve (e.g. the over/under line).
    """
    home = match["homeTeam"]["name"]
    away = match["awayTeam"]["name"]
    sport = match.get("sport", "Soccer")

    # Football-specific score markets do not translate cleanly to every sport.
    # Start multi-sport testing with an unambiguous game-winner question.
    if sport != "Soccer":
        text = f"{home} vs {away} — Game winner?"
        options = [home, away]
        if sport == "American Football":
            options.insert(1, "Tie")
        return QTYPE_RESULT, text, options, {"sport": sport}

    qtype = _weighted_choice(WEIGHTED_TYPES)

    if qtype == QTYPE_RESULT:
        text = f"{home} vs {away} — Full-time result?"
        options = [home, "Draw", away]
        return qtype, text, options, {}

    if qtype == QTYPE_OVER_UNDER:
        line = random.choice(OVER_UNDER_LINES)
        text = f"{home} vs {away} — Total goals: Over or Under {line}?"
        options = [f"Over {line}", f"Under {line}"]
        return qtype, text, options, {"line": line}

    # BTTS
    text = f"{home} vs {away} — Both teams to score?"
    options = ["Yes", "No"]
    return qtype, text, options, {}


def resolve_answer(qtype: str, extra: dict, home_goals: int, away_goals: int, home_name: str, away_name: str):
    """Return the correct option string for a finished match."""
    if qtype == QTYPE_RESULT:
        if home_goals > away_goals:
            return home_name
        if away_goals > home_goals:
            return away_name
        return "Tie" if extra.get("sport") == "American Football" else "Draw"

    if qtype == QTYPE_OVER_UNDER:
        line = extra["line"]
        total = home_goals + away_goals
        return f"Over {line}" if total > line else f"Under {line}"

    # BTTS
    return "Yes" if home_goals > 0 and away_goals > 0 else "No"
