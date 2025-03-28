#!/usr/bin/env pipenv run python

import requests
import pandas as pd
from datetime import datetime
import argparse
import yaml
import time
import sqlite3
from pushover import Client

# Initialize SQLite database
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
    return row[0] if row else None

def update_last_result(conn, alert_name, result_str):
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO alerts (alert_name, last_result)
        VALUES (?, ?)
        ON CONFLICT(alert_name) DO UPDATE SET last_result = excluded.last_result
    """, (alert_name, result_str))
    conn.commit()

def fetch_resort_info(start_date, end_date, room_type_filter=None, exclude_non_wdw=False, resort_name_filter=None):
    # Validate and format dates
    try:
        start_date = datetime.strptime(start_date, "%Y-%m-%d").strftime("%Y%m%d")
        end_date = datetime.strptime(end_date, "%Y-%m-%d").strftime("%Y%m%d")
    except ValueError:
        return "Invalid date format. Please use YYYY-mm-dd."

    # Fetch data from the API
    url = f"https://dvc-points.herokuapp.com/get-resort-info?arrivalDate={start_date}&departureDate={end_date}"
    response = requests.get(url)

    if response.status_code != 200:
        return f"Error fetching data: {response.status_code}"

    data = response.json()

    # Process data into a DataFrame
    resorts = []
    for entry in data.values():
        resorts.append({
            "ResortName": entry.get("ResortName"),
            "RoomType": entry.get("RoomType"),
            "ViewType": entry.get("ViewType"),
            "Points": entry.get("Points"),
            "Availability": entry.get("Availability", {}).get("availability")
        })

    df = pd.DataFrame(resorts)

    # Filter for availability
    df = df[df["Availability"] == "Full"]

    # Filter by room type if specified
    if room_type_filter:
        df = df[df["RoomType"].str.contains(room_type_filter, case=False, na=False)]

    # Exclude non-Disney World resorts if specified
    if exclude_non_wdw:
        exclude_keywords = ["Aulani", "Beach", "Disneyland", "Hilton", "Californian"]
        pattern = '|'.join(exclude_keywords)
        df = df[~df["ResortName"].str.contains(pattern, case=False, na=False)]

    # Filter by resort name if specified
    if resort_name_filter:
        pattern = '|'.join(resort_name_filter)
        df = df[df["ResortName"].str.contains(pattern, case=False, na=False)]

    return df

def send_pushover_alert(message, pushover_config):
    client = Client(pushover_config["user_key"], api_token=pushover_config["api_token"])
    client.send_message(message, title="DVC Availability Alert")

def check_availability(conn, alert_config):
    # Fetch current availability
    result = fetch_resort_info(
        start_date=alert_config["start_date"],
        end_date=alert_config["end_date"],
        room_type_filter=alert_config.get("room_type"),
        exclude_non_wdw=alert_config.get("exclude_non_wdw", False),
        resort_name_filter=alert_config.get("resort_names")
    )

    if isinstance(result, str):
        print(result)
        return

    # Convert the result DataFrame to a string for comparison
    result_str = result.to_string(index=False)

    # Get the last sent result for this alert
    alert_name = alert_config.get("name", "Unnamed")
    last_result_str = fetch_last_result(conn, alert_name)

    # Check if the result has changed
    if result_str != last_result_str:
        # Update the last sent result in the database
        update_last_result(conn, alert_name, result_str)

        # Send the alert
        if not result.empty:
            message = f"Availability found for alert '{alert_name}':\n{result_str}"
            print(message)
            if "pushover" in alert_config:
                send_pushover_alert(message, alert_config["pushover"])
        else:
            print(f"No availability found for alert '{alert_name}'.")

def main():
    parser = argparse.ArgumentParser(description="Fetch Disney Vacation Club resort information.")
    parser.add_argument("config_file", help="Path to the YAML configuration file.")

    args = parser.parse_args()

    # Load configuration from YAML file
    with open(args.config_file, "r") as file:
        config = yaml.safe_load(file)

    # Initialize the SQLite database
    conn = initialize_db()

    # Run the check every 5 minutes
    while True:
        print(f"Checking availability at {datetime.now()}...")
        for alert_config in config.get("alerts", []):
            check_availability(conn, alert_config)
        time.sleep(300)  # Wait for 5 minutes

if __name__ == "__main__":
    main()