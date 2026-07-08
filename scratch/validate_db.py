from datetime import datetime, timezone
import json
from database import get_db_service

db = get_db_service()

# Parse the dates as per the API routes/popup.py logic
# user_last_login_date: 01/07/2026 -> 2026-07-01
# user_current_login_date: 05/07/2026 -> 2026-07-05
last_dt = datetime(2026, 7, 1, tzinfo=timezone.utc)
curr_dt = datetime(2026, 7, 5, tzinfo=timezone.utc)
diff = curr_dt - last_dt
prev_start = last_dt - diff
prev_end = last_dt

survey_ids = [21, 23, 24, 25, 27, 28, 29]

nps_data = db.get_nps_data(survey_ids, last_dt, curr_dt, prev_start, prev_end)
print("NPS Data:", json.dumps(nps_data, indent=2))

voice = db.get_customer_voice_data_for_surveys(survey_ids, last_dt, curr_dt)
records = voice.get("high_severity_records", [])

from collections import defaultdict
theme_map = defaultdict(list)
for r in records:
    theme_map[r["theme"]].append(r)

print("\nThemes:")
for theme, items in theme_map.items():
    print(f"{theme}: {len(items)}")

