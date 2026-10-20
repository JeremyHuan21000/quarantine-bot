"""Multi-sport event provider backed by TheSportsDB v1 API."""
import datetime
import os

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("THESPORTSDB_API_KEY", "123").strip() or "123"
BASE_URL = f"https://www.thesportsdb.com/api/v1/json/{API_KEY}"

# Curated major competitions. Override with comma-separated IDs in .env.
DEFAULT_LEAGUE_IDS = (
    "4328",  # English Premier League
    "4331",  # German Bundesliga
    "4332",  # Italian Serie A
    "4334",  # French Ligue 1
    "4335",  # Spanish La Liga
    "4480",  # UEFA Champions League
    "4481",  # UEFA Europa League
    "5071",  # UEFA Conference League
    "4490",  # UEFA Nations League
    "4429",  # FIFA World Cup
    "4380",  # NHL
    "4387",  # NBA
    "4391",  # NFL
    "4424",  # MLB
)
LEAGUE_IDS = tuple(
    item.strip()
    for item in os.getenv("SPORTSDB_LEAGUE_IDS", ",".join(DEFAULT_LEAGUE_IDS)).split(",")
    if item.strip()
)

UPCOMING_STATUSES = {None, "", "NS", "TBD", "Scheduled", "Not Started"}
FINISHED_STATUSES = {"FT", "AOT", "AET", "AP", "Final", "Finished"}
VOID_STATUSES = {"PST", "CANC", "ABD", "Postponed", "Cancelled", "Canceled", "Abandoned"}


class ProviderUnavailable(requests.RequestException):
    """Rate limited or timed out."""


def _get(endpoint, **params):
    url = f"{BASE_URL}/{endpoint}"
    try:
        response = requests.get(url, params=params, timeout=8)
    except requests.Timeout as exc:
        raise ProviderUnavailable("TheSportsDB timeout") from exc
    if response.status_code == 429:
        raise ProviderUnavailable("TheSportsDB HTTP 429")
    response.raise_for_status()
    return response.json()


def _to_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _utc_timestamp(event):
    timestamp = event.get("strTimestamp")
    if timestamp:
        return timestamp.rstrip("Z") + "Z"
    date = event.get("dateEvent")
    event_time = event.get("strTime") or "00:00:00"
    return f"{date}T{event_time.rstrip('Z')}Z" if date else None


def _normalize_event(event):
    """Return the provider-independent match shape consumed by the bot."""
    return {
        "id": int(event["idEvent"]),
        "utcDate": _utc_timestamp(event),
        "status": event.get("strStatus"),
        "sport": event.get("strSport") or "Unknown",
        "competition": {
            "id": event.get("idLeague"),
            "name": event.get("strLeague") or "Unknown competition",
        },
        "homeTeam": {"name": event.get("strHomeTeam")},
        "awayTeam": {"name": event.get("strAwayTeam")},
        "score": {
            "fullTime": {
                "home": _to_int(event.get("intHomeScore")),
                "away": _to_int(event.get("intAwayScore")),
            }
        },
        "_raw": event,
    }


def get_todays_fixtures():
    """Fetch today’s fixtures plus overnight North American games."""
    today = datetime.datetime.now(datetime.timezone.utc).date()
    dates = (today.isoformat(), (today + datetime.timedelta(days=1)).isoformat())
    # Tomorrow is only for North American sports, never next-evening soccer.
    overnight_leagues = {"4380", "4387", "4391", "4424"}
    matches = []
    seen = set()
    # Try the preferred North American sports before soccer. A rate limit
    # later in the lookup should not discard fixtures already retrieved.
    preferred = ("4380", "4391", "4387", "4424")
    ordered_leagues = tuple(l for l in preferred if l in LEAGUE_IDS) + tuple(
        l for l in LEAGUE_IDS if l not in preferred
    )
    rate_limited = False

    # The free daily endpoint can return only a small slice of a league's
    # schedule. Merge it with the league's next event so a postponed or
    # already-finished event does not hide a later eligible game today.
    for league_id in ordered_leagues:
        allowed_dates = dates if league_id in overnight_leagues else dates[:1]
        events = []
        requests_to_make = tuple(
            ("eventsday.php", {"d": date, "l": league_id}) for date in allowed_dates
        )
        for endpoint, params in requests_to_make:
            try:
                data = _get(endpoint, **params)
                events.extend(data.get("events") or [])
            except ProviderUnavailable as exc:
                print(f"[sports_api] Provider unavailable at league {league_id}: {exc}")
                rate_limited = True
                break
            except requests.RequestException as exc:
                print(
                    f"[sports_api] Failed to fetch {endpoint} for league "
                    f"{league_id}: {exc}"
                )

        for event in events:
            event_id = event.get("idEvent")
            if not event_id or event_id in seen:
                continue
            if event.get("dateEvent") not in allowed_dates:
                continue
            if str(event.get("strPostponed", "no")).lower() == "yes":
                continue
            status = event.get("strStatus")
            home_score = _to_int(event.get("intHomeScore"))
            if status in UPCOMING_STATUSES and home_score is None:
                matches.append(_normalize_event(event))
                seen.add(event_id)

        if rate_limited:
            break  # Avoid hammering the provider after a 429 or timeout.

    if rate_limited and not matches:
        raise ProviderUnavailable("TheSportsDB unavailable before any fixtures were fetched")
    return matches


def get_match(match_id):
    """Fetch and normalize one event for the automatic resolution loop."""
    data = _get("lookupevent.php", id=match_id)
    events = data.get("events") or []
    if not events:
        raise LookupError(f"TheSportsDB event {match_id} was not found")
    return _normalize_event(events[0])


def extract_result(match):
    raw = match.get("_raw", {})
    status = match.get("status")
    postponed = str(raw.get("strPostponed", "no")).lower() == "yes"
    score = match.get("score", {}).get("fullTime", {})
    home_score = score.get("home")
    away_score = score.get("away")
    finished = status in FINISHED_STATUSES

    # Some completed records receive scores before the status text is updated.
    if status in (None, "", "NS") and home_score is not None and away_score is not None:
        finished = True

    return {
        "status": "POSTPONED" if postponed else (status or "UNKNOWN"),
        "finished": finished,
        "voided": postponed or status in VOID_STATUSES,
        "home_goals": home_score,
        "away_goals": away_score,
        "home_name": match.get("homeTeam", {}).get("name"),
        "away_name": match.get("awayTeam", {}).get("name"),
    }
