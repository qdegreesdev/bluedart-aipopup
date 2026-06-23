import time
import sqlite3
import os
from threading import Lock
from typing import Any, Optional

class CacheService:
    def __init__(self):
        self._cache = {}
        self._lock = Lock()
        
        # Setup local SQLite DB for tracking dismissals (persists across restarts)
        self.db_path = os.path.join(os.path.dirname(__file__), "..", "local_tracking.db")
        self._init_db()

    def _init_db(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS popup_dismissals (
                        client_id INTEGER,
                        date_str TEXT,
                        PRIMARY KEY (client_id, date_str)
                    )
                """)
                conn.commit()
        except Exception as e:
            from loguru import logger
            logger.error(f"Failed to init local tracking DB: {e}")

    def get_summary_cache(self, key: str) -> Optional[dict]:
        with self._lock:
            if key in self._cache:
                item = self._cache[key]
                if time.time() < item["expires_at"]:
                    return item["data"]
                else:
                    del self._cache[key]
        return None

    def set_summary_cache(self, key: str, data: dict, ttl_seconds: int = 600):
        with self._lock:
            self._cache[key] = {
                "data": data,
                "expires_at": time.time() + ttl_seconds
            }

    def clear_cache(self):
        with self._lock:
            self._cache.clear()

    # --- Dismissal Tracking ---
    
    def mark_dismissed(self, client_id: int, date_str: str) -> bool:
        """Mark that a client has dismissed the popup for a specific date (YYYY-MM-DD)."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT OR IGNORE INTO popup_dismissals (client_id, date_str) VALUES (?, ?)",
                    (client_id, date_str)
                )
                conn.commit()
                return True
        except Exception as e:
            from loguru import logger
            logger.error(f"Failed to record dismissal: {e}")
            return False

    def has_dismissed(self, client_id: int, date_str: str) -> bool:
        """Check if a client has already dismissed the popup today."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT 1 FROM popup_dismissals WHERE client_id = ? AND date_str = ?",
                    (client_id, date_str)
                )
                return cursor.fetchone() is not None
        except Exception as e:
            from loguru import logger
            logger.error(f"Failed to check dismissal: {e}")
            return False

# Global instance
cache_service = CacheService()
