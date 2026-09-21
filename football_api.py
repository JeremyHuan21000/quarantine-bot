"""
Thin wrapper around the football-data.org free-tier API.

Free tier covers 12 top competitions, delayed scores/schedules, and
does NOT include corners, cards, or other match-stats data - so
questions here are limited to things derivable from the final score:
match result (home/draw/away), total goals (over/under), and BTTS.

Docs: https://www.football-data.org/documentation/quickstart
"""
import os
import time
import datetime
import requests
from dotenv import load_dotenv

# Defensive: ensure .env is loaded even if this module gets imported before
# the caller loads it themselves - avoids a repeat of a nasty bug where
# API_KEY silently became None because it was read at import time, before
# load_dotenv() had run in bot.py.
load_dotenv()

API_KEY = os.getenv("FOOTBALL_DATA_API_KEY")
if not API_KEY:
    print("[football_api] WARNING: FOOTBALL_DATA_API_KEY is not set - all requests will fail with 403.")
BASE_URL = "https://api.football-data.org/v4"

# The 12 competitions available on the free tier (codes per football-data.org docs)
FREE_COMPETITIONS = [
    "PL", "ELC", "BL1", "SA", "PD", "FL1", "DED", "PPL",
    "CL", "EC", "WC", "BSA",
]

HEADERS = {"X-Auth-Token": API_KEY} if API_KEY else {}


def _get_with_retry(url, max_retries=3, backoff_seconds=15, **kwargs):
    """
    GET with retry/backoff specifically for rate-limit responses (403/429).
    The free tier allows 10 requests/minute - a burst of calls (e.g. from
    manual testing plus the bot's own tasks firing close together) can
    trip this temporarily, so a short wait-and-retry recovers cleanly
    instead of failing the whole check.
    """
    for attempt in range(max_retries):
        resp = requests.get(url, headers=HEADERS, timeout=10, **kwargs)
        if resp.status_code in (403, 429) and attempt < max_retries - 1:
            print(f"Rate-limited ({resp.status_code}) on {url}, retrying in {backoff_seconds}s...")
            time.sleep(backoff_seconds)
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()
    return resp


def get_todays_fixtures():
    """
    Return today's upcoming (not yet finished) matches across the free
    competitions.

    NOTE: the combined /v4/matches endpoint reliably returns an empty
    result on the free tier even when matches genuinely exist for the
    given date/competitions (confirmed via manual testing) - so instead
    we query each competition individually via /v4/competitions/{code}/matches,
    which does return real data, and filter by date ourselves in Python.
    A short delay between calls keeps us under the free tier's 10
    requests/minute limit.
    """
    today = datetime.date.today().isoformat()
    all_matches = []

    for code in FREE_COMPETITIONS:
        url = f"{BASE_URL}/competitions/{code}/matches"
        try:
            resp = _get_with_retry(url)
        except requests.RequestException as e:
            print(f"Failed to fetch {code} matches: {e}")
            time.sleep(6)
            continue

        data = resp.json()
        for m in data.get("matches", []):
            if m.get("utcDate", "").startswith(today):
                all_matches.append(m)

        time.sleep(6)  # stay comfortably under 10 requests/minute

    upcoming_statuses = {"SCHEDULED", "TIMED"}
    return [m for m in all_matches if m.get("status") in upcoming_statuses]


def get_match(match_id: int):
    """Return current data for a single match (used to check if it's finished)."""
    url = f"{BASE_URL}/matches/{match_id}"
    resp = _get_with_retry(url)
    return resp.json()


def extract_result(match: dict):
    """
    Given a finished match payload, return a dict with:
    home_goals, away_goals, home_name, away_name, finished (bool)
    """
    status = match.get("status")
    finished = status in ("FINISHED", "AWARDED")
    score = match.get("score", {}).get("fullTime", {})
    return {
        "status": status,
        "finished": finished,
        "voided": status in ("CANCELLED", "POSTPONED"),
        "home_goals": score.get("home"),
        "away_goals": score.get("away"),
        "home_name": match.get("homeTeam", {}).get("name"),
        "away_name": match.get("awayTeam", {}).get("name"),
    }
