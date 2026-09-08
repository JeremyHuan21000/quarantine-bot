"""
Thin wrapper around the football-data.org free-tier API.

Free tier covers 12 top competitions, delayed scores/schedules, and
does NOT include corners, cards, or other match-stats data - so
questions here are limited to things derivable from the final score:
match result (home/draw/away), total goals (over/under), and BTTS.

Docs: https://www.football-data.org/documentation/quickstart
"""
import os
import datetime
import requests

API_KEY = os.getenv("FOOTBALL_DATA_API_KEY")
BASE_URL = "https://api.football-data.org/v4"

# The 12 competitions available on the free tier (codes per football-data.org docs)
FREE_COMPETITIONS = [
    "PL", "ELC", "BL1", "SA", "PD", "FL1", "DED", "PPL",
    "CL", "EC", "WC", "BSA",
]

HEADERS = {"X-Auth-Token": API_KEY} if API_KEY else {}


def get_todays_fixtures():
    """Return today's scheduled matches across the free competitions."""
    today = datetime.date.today().isoformat()
    url = f"{BASE_URL}/matches"
    params = {"dateFrom": today, "dateTo": today, "status": "SCHEDULED"}
    resp = requests.get(url, headers=HEADERS, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    matches = data.get("matches", [])
    # Free tier returns all competitions the key has access to; filter defensively
    return [m for m in matches if m.get("competition", {}).get("code") in FREE_COMPETITIONS]


def get_match(match_id: int):
    """Return current data for a single match (used to check if it's finished)."""
    url = f"{BASE_URL}/matches/{match_id}"
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.raise_for_status()
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
        "finished": finished,
        "home_goals": score.get("home"),
        "away_goals": score.get("away"),
        "home_name": match.get("homeTeam", {}).get("name"),
        "away_name": match.get("awayTeam", {}).get("name"),
    }
