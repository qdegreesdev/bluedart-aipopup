import asyncio
from datetime import datetime
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database import DatabaseService

async def main():
    db = DatabaseService()
    
    # 1. Test Dates
    last_dt = datetime(2026, 6, 19, 0, 0, 0)
    curr_dt = datetime(2026, 6, 20, 23, 59, 59)
    print(f"Testing DB for dates: {last_dt} to {curr_dt}")
    
    # 2. Get survey IDs
    surveys = db.get_survey_ids_by_client()
    if not surveys:
        print("No surveys found.")
        return
        
    print(f"Using survey ID: {surveys[0]}")
    
    # 3. Call the exact same function the API uses
    voice = db.get_customer_voice_data(surveys[0], last_dt, curr_dt)
    records = voice.get("high_severity_records", [])
    
    print(f"\n--- API FUNCTION RESULT ---")
    print(f"Total critical records found by API function: {len(records)}")
    for i, r in enumerate(records[:5]):
        print(f"API VOC {i+1}: {r['verbatim'][:100]}... (Created: {r.get('created_at', 'Unknown')})")
        
    # 4. Raw DB query to verify if the API missed anything
    print(f"\n--- RAW SQL DATABASE CHECK ---")
    query = """
    SELECT created_at, customer_verbatim, priority_level, is_critical, nps_score 
    FROM voc_alerts 
    WHERE nps_score <= 6 
      AND created_at >= %s 
      AND created_at < %s
    ORDER BY is_critical DESC, created_at DESC
    LIMIT 10
    """
    try:
        raw_rows = db._execute_query(query, (last_dt, curr_dt))
        print(f"Total raw detractors found in DB for this date range: {len(raw_rows)} (showing up to 10)")
        for i, row in enumerate(raw_rows):
            print(f"DB VOC {i+1} | Date: {row['created_at']} | Critical: {row['is_critical']} | Priority: {row['priority_level']}")
            print(f"   Text: {row['customer_verbatim'][:100]}...")
    except Exception as e:
        print(f"Raw query failed: {e}")

if __name__ == "__main__":
    asyncio.run(main())
