"""Strava web-sync: pull activities via Strava's web endpoints (web session).

Why this exists:
    Since 2026-06-01 Strava requires a paid subscription for API access, and
    previously issued API keys were deactivated. The web endpoints
    (training_activities list + per-activity streams) still work with a
    logged-in web session, so this script syncs data through those instead.

Login (JWT cookie preferred):
    STRAVA_JWT                    -> strava_remember_token cookie (~30 days)
    STRAVA_EMAIL + STRAVA_PASSWORD -> fallback only; Strava currently blocks
                                      unattended password login (reCAPTCHA)

Endpoints used:
    GET /athlete/training_activities?page=N   -> paginated activity list
    GET /activities/{id}/streams              -> per-activity streams (latlng, heartrate, ...)

Usage:
    python run_page/strava_web_sync.py --days 7
    # credentials come from env vars STRAVA_EMAIL/STRAVA_PASSWORD or STRAVA_JWT
"""

import argparse
import datetime
import json
import logging
import os
import sys
import time
import uuid

import pytz
import requests
from config import BASE_TIMEZONE, JSON_FILE, SQL_FILE, run_map, start_point
from generator.db import init_db, update_or_create_activity
from stravaweblib import WebClient

logger = logging.getLogger(__name__)

# Nominatim reverse-geocoding (used by update_or_create_activity for
# location_country) can hang indefinitely on a slow network. Give it a global
# timeout so the web sync never blocks forever on a single activity.
try:
    import geopy.geocoders

    geopy.geocoders.options.default_timeout = 10
except ImportError:
    # geopy is an optional dependency (only used for reverse-geocoding inside
    # update_or_create_activity); sync proceeds without a geocoding timeout.
    logger.debug("geopy not installed; skipping geocoder timeout guard")

TRAINING_ACTIVITIES_URL = (
    "https://www.strava.com/athlete/training_activities"
    "?keywords=&sport_type=&tags=&commute=&private_activities=&trainer=&gear="
    "&search_session_id={sid}&new_activity_only=false&page={page}"
)
STREAMS_URL = "https://www.strava.com/activities/{aid}/streams"

# Keep the list-call header set that the web client needs to return JSON models.
LIST_HEADERS = {
    "accept": "text/javascript, application/javascript, application/ecmascript, application/x-ecmascript",
    "x-requested-with": "XMLHttpRequest",
}


def _polyline_encode(latlng_list):
    """Encode [[lat, lng], ...] into a Google encoded polyline (matches stravalib)."""
    if not latlng_list:
        return ""
    out = ""
    prev_lat = prev_lng = 0
    for lat, lng in latlng_list:
        lat5 = round(lat * 1e5)
        lng5 = round(lng * 1e5)
        for val in (lat5 - prev_lat, lng5 - prev_lng):
            val = (val << 1) ^ (val >> 31)
            while val >= 0x20:
                out += chr((0x20 | (val & 0x1F)) + 63)
                val >>= 5
            out += chr(val + 63)
        prev_lat, prev_lng = lat5, lng5
    return out


class WebActivity:
    """Lightweight adapter exposing the attributes update_or_create_activity needs.

    Takes a raw dict from the training_activities list endpoint plus an optional
    streams dict (from the per-activity streams endpoint) to fill in polyline,
    heartrate and location fields.
    """

    def __init__(self, raw, streams=None):
        self.id = int(raw["id"])
        self.name = raw.get("name", "")
        self.type = (
            raw.get("sport_type") or raw.get("activity_type_display_name") or "Workout"
        )
        # subtype mirrors type (some generators/db layers read it, e.g. running_page)
        self.subtype = self.type
        # distance in meters
        self.distance = float(raw.get("distance_raw") or 0.0)
        # moving_time as timedelta (DB column is Interval)
        moving_secs = int(raw.get("moving_time_raw") or 0)
        self.moving_time = datetime.timedelta(seconds=moving_secs)
        elapsed_secs = int(raw.get("elapsed_time_raw") or 0) or moving_secs
        self.elapsed_time = datetime.timedelta(seconds=elapsed_secs)
        self.start_date = raw.get("start_time", "")
        # start_date_local_raw is a Unix timestamp -> local date string
        self.start_date_local = _fmt_local_date(raw)
        self.elevation_gain = raw.get("elevation_gain_raw") or 0.0
        self.source = "strava"

        # average_speed (m/s). Guard against zero moving time.
        self.average_speed = (self.distance / moving_secs) if moving_secs else 0.0

        # Fields only resolvable from the streams payload.
        self.average_heartrate = None
        self.summary_polyline = ""
        self.start_latlng = None
        if streams:
            hr = streams.get("heartrate")
            if hr:
                vals = [h for h in hr if h is not None]
                if vals:
                    self.average_heartrate = sum(vals) / len(vals)
            latlng = streams.get("latlng")
            if latlng:
                self.summary_polyline = _polyline_encode(latlng)
                self.start_latlng = start_point(latlng[0][0], latlng[0][1])

        # map attribute used by update_or_create_activity
        self.map = run_map(self.summary_polyline)


def _fmt_local_date(raw):
    """Format local date 'YYYY-MM-DD HH:MM:SS' in BASE_TIMEZONE.

    Prefers the true-UTC `start_time` ISO string (e.g.
    "2026-03-23T00:28:07+0000") and converts it to BASE_TIMEZONE.
    Falls back to `start_date_local_raw`, which is the athlete's local
    wall clock encoded as a naive UTC epoch — do NOT apply another
    timezone shift on it (that used to add +8h twice, see #fix).
    """
    local_tz = pytz.timezone(BASE_TIMEZONE)
    start_time = raw.get("start_time") if isinstance(raw, dict) else None
    if start_time:
        try:
            s = str(start_time).replace("+0000", "+00:00")
            dt = datetime.datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.UTC)
            return dt.astimezone(local_tz).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    ts = raw.get("start_date_local_raw") if isinstance(raw, dict) else raw
    if not ts:
        return ""
    dt = datetime.datetime.fromtimestamp(int(ts), tz=datetime.UTC)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _fetch_json(client, url, **extra_headers):
    resp = client._session.get(url, headers={**LIST_HEADERS, **extra_headers})
    resp.raise_for_status()
    return resp.json()


# Headers the per-activity streams endpoint requires (403 without the CSRF token).
STREAM_HEADERS = {
    "x-requested-with": "XMLHttpRequest",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
}


def _fetch_streams(client, aid):
    csrf = client.csrf  # dict like {'authenticity_token': '...'}
    headers = dict(STREAM_HEADERS)
    headers["referer"] = f"https://www.strava.com/activities/{aid}"
    # x-csrf-token is the value of the authenticity_token param
    if csrf:
        headers["x-csrf-token"] = next(iter(csrf.values()))
    resp = client._session.get(STREAMS_URL.format(aid=aid), headers=headers)
    resp.raise_for_status()
    return resp.json()


def _login(jwt=None, email=None, password=None):
    """Log in with a JWT cookie first; password login only as a fallback.

    Note (2026-11): Strava's web login now requires reCAPTCHA and uses a
    one-time-code/Next.js flow. Unattended password login from scripts and CI
    is rejected (HTTP 403 on /session and /login/request_otp), so the
    supported path is a browser-refreshed STRAVA_JWT cookie. Password login
    stays here in case Strava ever relaxes this.
    """
    failures = []
    if jwt:
        try:
            client = WebClient(jwt=jwt)
            print("Web login ok (JWT cookie)")
            return client
        except Exception as e:  # expired/invalid cookie, network errors...
            failures.append(f"JWT login: {type(e).__name__}: {e}")
            print(f"JWT login failed, trying password: {e}", file=sys.stderr)
    if email and password:
        try:
            client = WebClient(email=email, password=password)
            print("Web login ok (account password fallback)")
            return client
        except Exception as e:
            failures.append(f"password login: {type(e).__name__}: {e}")
            print(f"Password login failed: {e}", file=sys.stderr)
    if not failures:
        raise ValueError(
            "Either a JWT (strava_remember_token cookie) or both email and "
            "password are required to log in to Strava"
        )
    raise RuntimeError("Strava web login failed:\n  " + "\n  ".join(failures))


def run_strava_web_sync(jwt=None, days=7, only_run=False, email=None, password=None):
    client = _login(jwt, email, password)

    session = init_db(SQL_FILE)
    cutoff = time.time() - days * 86400

    # search_session_id: web client needs a fresh one
    sid = str(uuid.uuid4())

    page = 1
    total_fetched = 0
    while True:
        data = _fetch_json(client, TRAINING_ACTIVITIES_URL.format(sid=sid, page=page))
        models = data.get("models") or []
        if not models:
            break
        for raw in models:
            ts = raw.get("start_date_local_raw")
            if ts and ts < cutoff:
                # List is newest-first, so once we pass the cutoff the rest are older.
                return _finalize(session, total_fetched)
            if only_run and raw.get("sport_type") != "Run":
                continue
            _sync_one(client, session, raw)
            total_fetched += 1
        if page >= data.get("total", 1) // 20:
            break
        page += 1
        time.sleep(0.3)  # be gentle with rate limits

    return _finalize(session, total_fetched)


def _sync_one(client, session, raw):
    aid = int(raw["id"])
    streams = {}
    try:
        streams = _fetch_streams(client, aid)
    except requests.exceptions.RequestException as e:
        # A single activity's streams (polyline/heartrate) failing must not
        # abort the whole sync; fall back to the list-only activity.
        print(f"  streams fail {aid}: {e}")
    act = WebActivity(raw, streams)
    created = update_or_create_activity(session, act)
    session.commit()
    sys.stdout.write("+" if created else ".")
    sys.stdout.flush()


def _finalize(session, count):
    from generator import Generator

    gen = Generator(SQL_FILE)
    # running_page's Generator exposes load() (with indoor-fix logic) instead of
    # loadForMapping(); both return the list of activity dicts to write to JSON.
    activities_list = gen.load()
    with open(JSON_FILE, "w") as f:
        json.dump(activities_list, f, indent=0)
    print(f"\nDone: {count} activities synced.")
    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Sync Strava activities via web endpoints (JWT session)."
    )
    parser.add_argument(
        "jwt",
        nargs="?",
        default=None,
        help="Strava strava_remember_token JWT cookie value (optional)",
    )
    parser.add_argument(
        "--jwt",
        dest="jwt_opt",
        default=None,
        help="Strava strava_remember_token JWT cookie value",
    )
    parser.add_argument(
        "--email",
        dest="email",
        default=None,
        help="Strava account email (password login, no API needed)",
    )
    parser.add_argument(
        "--password",
        dest="password",
        default=None,
        help="Strava account password",
    )
    parser.add_argument(
        "--days", type=int, default=7, help="number of days to look back (default: 7)"
    )
    parser.add_argument(
        "--only-run",
        dest="only_run",
        action="store_true",
        help="only sync Run activities",
    )
    options = parser.parse_args()

    jwt = options.jwt or options.jwt_opt or os.environ.get("STRAVA_JWT")
    email = options.email or os.environ.get("STRAVA_EMAIL")
    password = options.password or os.environ.get("STRAVA_PASSWORD")
    if not jwt and not (email and password):
        parser.error(
            "provide a JWT or set --email/--password "
            "(or STRAVA_JWT / STRAVA_EMAIL+STRAVA_PASSWORD env vars)"
        )

    run_strava_web_sync(
        jwt,
        days=options.days,
        only_run=options.only_run,
        email=email,
        password=password,
    )
