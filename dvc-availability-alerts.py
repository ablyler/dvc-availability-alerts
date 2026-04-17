#!/usr/bin/env pipenv run python

import json
import requests
import pandas as pd
from datetime import datetime
import argparse
import yaml
import time
import sqlite3
from functools import lru_cache
from pushover import Client
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://api.keyholdervacations.com/v2/dvc"
HEADERS = {
    "accept": "*/*",
    "origin": "https://dvcrentalstore.com",
    "referer": "https://dvcrentalstore.com/",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/147.0.0.0 Safari/537.36"
    ),
}

NON_WDW_RESORT_CODES = {"AULV", "HILTN", "VERO", "GCAL", "VDH"}
DEFAULT_LEVELS = {"high", "low"}
HTTP_TIMEOUT = (10, 30)
HTTP_RETRY_COUNT = 2
RETRYABLE_STATUS_CODES = (429, 500, 502, 503, 504)

LEVEL_LABELS = {
    "high": "Available",
    "low": "Going Quickly",
    "partial": "Partial Availability",
}

CHANGE_LABELS = {
    "new": "New",
    "gone": "No Longer Available",
    "upgraded": "Upgraded",
    "downgraded": "Downgraded",
}

LEVEL_ORDER = ["high", "low", "partial"]


def build_session():
    retry = Retry(
        total=HTTP_RETRY_COUNT,
        connect=HTTP_RETRY_COUNT,
        read=HTTP_RETRY_COUNT,
        status=HTTP_RETRY_COUNT,
        backoff_factor=1,
        status_forcelist=RETRYABLE_STATUS_CODES,
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


SESSION = build_session()


def initialize_db(db_path="alerts.db"):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            alert_name TEXT PRIMARY KEY,
            last_result TEXT
        )
    """)
    conn.commit()
    return conn


def fetch_last_result(conn, alert_name):
    cursor = conn.cursor()
    cursor.execute("SELECT last_result FROM alerts WHERE alert_name = ?", (alert_name,))
    row = cursor.fetchone()
    if not row:
        return {}
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return {}


def update_last_result(conn, alert_name, result_dict):
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO alerts (alert_name, last_result)
        VALUES (?, ?)
        ON CONFLICT(alert_name) DO UPDATE SET last_result = excluded.last_result
    """, (alert_name, json.dumps(result_dict)))
    conn.commit()


def fetch_json(path, params):
    response = SESSION.get(
        f"{BASE_URL}{path}",
        params=params,
        headers=HEADERS,
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()["data"]


@lru_cache(maxsize=1)
def fetch_room_metadata():
    data = fetch_json("/rooms", {"occupancy": 1})

    views = {v["id"]: v["description"] for v in data["views"].values()}
    types = {v["id"]: v["description"] for v in data["types"].values()}

    key_to_meta = {}
    for resort in data["resorts"]:
        for room_type_id, room_info in resort.get("rooms", {}).items():
            room_type = types.get(room_type_id, room_type_id)
            for view_id, avail_key in room_info.get("views", {}).items():
                key_to_meta[avail_key] = {
                    "ResortName": resort["name"],
                    "ResortCode": resort["resortCode"],
                    "RoomType": room_type,
                    "ViewType": views.get(view_id, view_id),
                }

    return key_to_meta


def fetch_resort_info(start_date, end_date, availability_levels, room_type_filter=None, exclude_non_wdw=False, resort_name_filter=None):
    try:
        start_date = datetime.strptime(start_date, "%Y-%m-%d").strftime("%Y-%m-%d")
        end_date = datetime.strptime(end_date, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return "Invalid date format. Please use YYYY-mm-dd."

    try:
        avail_data = fetch_json(
            "/availability/calendar",
            {"startDate": start_date, "endDate": end_date},
        )
        key_to_meta = fetch_room_metadata()
    except requests.Timeout:
        return "Timed out fetching data from Keyholder Vacations."
    except requests.RequestException as exc:
        return f"Error fetching data from Keyholder Vacations: {exc}"

    resorts = []
    for avail_key, avail_info in avail_data.items():
        level = avail_info.get("availabilityLevel", "none")
        if level not in availability_levels:
            continue
        meta = key_to_meta.get(avail_key, {})
        resort_code = meta.get("ResortCode", "")

        if exclude_non_wdw and resort_code in NON_WDW_RESORT_CODES:
            continue

        resort_name = meta.get("ResortName", avail_key)
        if resort_name_filter:
            pattern = "|".join(resort_name_filter)
            if not pd.Series([resort_name]).str.contains(pattern, case=False, na=False).iloc[0]:
                continue

        room_type = meta.get("RoomType", "")
        if room_type_filter and not pd.Series([room_type]).str.contains(room_type_filter, case=False, na=False).iloc[0]:
            continue

        resorts.append({
            "key": avail_key,
            "ResortName": resort_name,
            "RoomType": room_type,
            "ViewType": meta.get("ViewType", ""),
            "Points": avail_info.get("pointCost"),
            "Availability": level,
        })

    return resorts


def room_line(entry):
    return f"  {entry['ResortName']}: {entry['RoomType']}, {entry['ViewType']} – {entry['Points']} pts"


def build_diff_message(alert_name, current, previous):
    buckets = {"new": [], "upgraded": [], "downgraded": [], "gone": []}

    for key, entry in current.items():
        prev = previous.get(key, {})
        prev_level = prev.get("Availability")
        curr_level = entry["Availability"]

        if prev_level is None:
            buckets["new"].append(entry)
        elif prev_level != curr_level:
            curr_idx = LEVEL_ORDER.index(curr_level) if curr_level in LEVEL_ORDER else 99
            prev_idx = LEVEL_ORDER.index(prev_level) if prev_level in LEVEL_ORDER else 99
            bucket = "upgraded" if curr_idx < prev_idx else "downgraded"
            buckets[bucket].append((prev_level, entry))

    for key, entry in previous.items():
        if key not in current:
            buckets["gone"].append(entry)

    if not any(buckets.values()):
        return None

    sections = []

    if buckets["new"]:
        by_level = {}
        for e in buckets["new"]:
            by_level.setdefault(e["Availability"], []).append(e)
        for level in LEVEL_ORDER:
            if level not in by_level:
                continue
            label = LEVEL_LABELS.get(level, level)
            entries = by_level[level]
            sections.append(f"New – {label} ({len(entries)}):\n" + "\n".join(room_line(e) for e in entries))

    for bucket_key in ("upgraded", "downgraded"):
        if buckets[bucket_key]:
            label = CHANGE_LABELS[bucket_key]
            lines = []
            for prev_level, e in buckets[bucket_key]:
                prev_label = LEVEL_LABELS.get(prev_level, prev_level)
                curr_label = LEVEL_LABELS.get(e["Availability"], e["Availability"])
                lines.append(f"  {e['ResortName']}: {e['RoomType']}, {e['ViewType']} – {e['Points']} pts ({prev_label} → {curr_label})")
            sections.append(f"{label} ({len(lines)}):\n" + "\n".join(lines))

    if buckets["gone"]:
        sections.append(f"No Longer Available ({len(buckets['gone'])}):\n" + "\n".join(room_line(e) for e in buckets["gone"]))

    return f"{alert_name}\n\n" + "\n\n".join(sections)


def send_pushover_alert(message, pushover_config):
    client = Client(pushover_config["user_key"], api_token=pushover_config["api_token"])
    client.send_message(message, title="DVC Availability Alert")


def check_availability(conn, alert_config):
    availability_levels = set(alert_config.get("availability_levels", list(DEFAULT_LEVELS)))

    result = fetch_resort_info(
        start_date=alert_config["start_date"],
        end_date=alert_config["end_date"],
        availability_levels=availability_levels,
        room_type_filter=alert_config.get("room_type"),
        exclude_non_wdw=alert_config.get("exclude_non_wdw", False),
        resort_name_filter=alert_config.get("resort_names"),
    )

    if isinstance(result, str):
        print(result)
        return

    alert_name = alert_config.get("name", "Unnamed")
    current = {entry["key"]: entry for entry in result}
    previous = fetch_last_result(conn, alert_name)

    if current != previous:
        update_last_result(conn, alert_name, current)
        message = build_diff_message(alert_name, current, previous)
        if message:
            print(message)
            if "pushover" in alert_config:
                send_pushover_alert(message, alert_config["pushover"])
        else:
            print(f"No alertable changes for '{alert_name}'.")
    else:
        print(f"No changes for '{alert_name}'.")


def main():
    parser = argparse.ArgumentParser(description="Fetch Disney Vacation Club resort information.")
    parser.add_argument("config_file", help="Path to the YAML configuration file.")
    args = parser.parse_args()

    with open(args.config_file, "r") as file:
        config = yaml.safe_load(file)

    conn = initialize_db()

    while True:
        print(f"Checking availability at {datetime.now()}...")
        for alert_config in config.get("alerts", []):
            alert_name = alert_config.get("name", "Unnamed")
            try:
                check_availability(conn, alert_config)
            except Exception as exc:
                print(f"Unexpected error checking '{alert_name}': {exc}")
        time.sleep(300)


if __name__ == "__main__":
    main()
