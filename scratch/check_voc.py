import sys
from database import get_db_service
db = get_db_service()
rows = db._execute_query("""
SELECT priority_level, COUNT(*) as cnt
FROM voc_alerts 
GROUP BY priority_level
""")
print(rows)
