"""
Database Service

Fetches real-time data from MySQL database for the SurveyCXM login popup.

DATE LOGIC:
  - current_period : last_login_dt  →  now          (what changed since you were here)
  - previous_period: (last_login_dt - window) → last_login_dt   (equal-length window before, for comparison)

Schema: dynamic per-survey tables (survey_responses_{id}, filter_hierarchy_{id}, voc_alerts, etc.)
"""

import logging
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any, Optional

import re
import pymysql
from pymysql.cursors import DictCursor

from config import settings

logger = logging.getLogger(__name__)

DB_AVAILABLE = False
_db_service_instance = None


class DatabaseService:
    """Service to fetch real-time data from MySQL database."""

    def __init__(self):
        self.engine = None
        self._connect()

    # ── Connection management ────────────────────────────────────────────────

    def _connect(self) -> None:
        try:
            from sqlalchemy import create_engine
            import urllib.parse
            escaped_password = urllib.parse.quote_plus(settings.survey_db_password)
            db_url = f"mysql+pymysql://{settings.survey_db_user}:{escaped_password}@{settings.survey_db_host}:{settings.survey_db_port}/{settings.survey_db_name}?charset=utf8mb4"
            self.engine = create_engine(
                db_url,
                pool_size=10,
                max_overflow=20,
                pool_pre_ping=True,
                pool_recycle=3600
            )
            logger.info(f"✅ MySQL pool created — host={settings.survey_db_host}, db={settings.survey_db_name}")
        except Exception as e:
            logger.error(f"❌ Database pool creation error: {e}")
            self.engine = None

    def _ensure_connection(self) -> bool:
        if self.engine is None:
            self._connect()
        return self.engine is not None

    @contextmanager
    def _cursor(self):
        if not self._ensure_connection():
            raise RuntimeError("No database connection available")
        conn = self.engine.raw_connection()
        try:
            cursor = conn.cursor(DictCursor)
            yield cursor
        finally:
            cursor.close()
            conn.close()

    def _execute_query(self, query: str, params: tuple = ()) -> list[dict]:
        normalized_params: tuple = ()
        try:
            if params is None:
                normalized_params = ()
            elif isinstance(params, tuple):
                normalized_params = params
            elif isinstance(params, list):
                normalized_params = tuple(params)
            else:
                normalized_params = (params,)
        except Exception:
            normalized_params = ()

        if isinstance(normalized_params, (str, bytes)):
            normalized_params = (normalized_params,)

        try:
            with self._cursor() as cursor:
                cursor.execute(query, normalized_params)
                return cursor.fetchall()
        except RuntimeError as e:
            logger.error(f"Database connection error: {e}")
            raise
        except Exception as e:
            logger.error(f"Query error: {e} | preview: {query[:120]}")
            raise

    @staticmethod
    def _safe_slug(slug: str) -> str:
        return f"`{slug.strip('`')}`"

    @staticmethod
    def _to_date(val) -> date:
        if isinstance(val, datetime):
            return val.date()
        if isinstance(val, date):
            return val
        return datetime.strptime(str(val)[:10], "%Y-%m-%d").date()

    # ── Connection test ──────────────────────────────────────────────────────

    def test_connection(self) -> dict[str, Any]:
        try:
            results = self._execute_query("SELECT 1 AS ok")
            if results:
                return {"connected": True, "database": settings.survey_db_name, "host": settings.survey_db_host}
            return {"connected": False, "error": "Empty result from SELECT 1"}
        except Exception as e:
            return {"connected": False, "error": str(e)}

    # ── Schema helpers ───────────────────────────────────────────────────────

    def _get_nps_slug(self, survey_id: int) -> str | None:
        if not hasattr(self, "_nps_slugs_cache"):
            self._nps_slugs_cache = {}
        if survey_id in self._nps_slugs_cache:
            return self._nps_slugs_cache[survey_id]

        result = self._execute_query(
            """
            SELECT question_slug FROM survey_questions
            WHERE survey_id = %s AND (question_type LIKE 'NPS%%' OR question_type = 'nps')
            ORDER BY sort ASC LIMIT 1
            """,
            (survey_id,),
        )
        slug = result[0]["question_slug"] if result else None
        self._nps_slugs_cache[survey_id] = slug
        return slug

    def _get_filter_labels(self, survey_id: int) -> dict[str, str]:
        # Since metadata_fields table is not used, return default labels directly to avoid database query errors.
        return {"f1": "Region", "f2": "State", "f3": "City", "f4": "Branch"}

    def _check_table_exists(self, table_name: str) -> bool:
        if not hasattr(self, "_table_exists_cache"):
            self._table_exists_cache = {}
        if table_name in self._table_exists_cache:
            return self._table_exists_cache[table_name]

        result = self._execute_query(
            """
            SELECT COUNT(*) AS cnt FROM information_schema.tables
            WHERE table_schema = %s AND table_name = %s
            """,
            (settings.survey_db_name, table_name),
        )
        exists = result and result[0]["cnt"] > 0
        if not exists:
            try:
                rows = self._execute_query(f"SHOW TABLES LIKE '{table_name}'")
                exists = bool(rows)
            except Exception:
                exists = False
        
        self._table_exists_cache[table_name] = exists
        return exists

    def _get_table_columns(self, table_name: str) -> list[str]:
        """Gets list of columns for a table and caches the result."""
        if not hasattr(self, "_table_cols_cache"):
            self._table_cols_cache = {}
        if table_name in self._table_cols_cache:
            return self._table_cols_cache[table_name]
        try:
            rows = self._execute_query(f"DESCRIBE {table_name}")
            cols = [row["Field"] for row in rows]
            self._table_cols_cache[table_name] = cols
            return cols
        except Exception:
            return []

    def _resolve_column_name(self, table_name: str, possible_names: list[str], default_name: str) -> str:
        """Finds the first existing column in table matching possible_names, case-insensitive."""
        try:
            cols = self._get_table_columns(table_name)
            for p in possible_names:
                for c in cols:
                    if c.lower() == p.lower():
                        return f"sr.`{c}`"
        except Exception:
            pass
        return f"sr.`{default_name}`"

    def _get_responses_table_name(self, survey_id: int) -> str:
        if str(survey_id) == "23":
            return "tp10_response_part"
        return f"survey_dynamic_id_{survey_id}"

    # ── NPS helpers ──────────────────────────────────────────────────────────

    def _get_nps_scores_for_range(
        self,
        survey_id: int,
        slug: str,
        start_dt: datetime,
        end_dt: datetime,
    ) -> list[dict]:
        responses_table = self._get_responses_table_name(survey_id)
        nps_col = self._resolve_column_name(responses_table, [slug, "NPS_SCORE", "nps_score", "nps"], slug)
        f1_col = self._resolve_column_name(responses_table, ["REGION", "region"], "REGION")
        f2_col = self._resolve_column_name(responses_table, ["LOCATION", "location", "area"], "LOCATION")
        f3_col = self._resolve_column_name(responses_table, ["BRANCH", "branch"], "BRANCH")
        num_col = self._resolve_column_name(responses_table, ["number", "cust_number", "cust_phone", "phone", "cust_acc_no"], "cust_acc_no")
        email_col = self._resolve_column_name(responses_table, ["EMAILID", "emailid", "EMAIL", "email", "cust_email"], "EMAILID")

        query = f"""
            SELECT
                {nps_col}  AS score,
                sr.response_datetime AS created_at,
                {f1_col} AS f1,
                {f2_col} AS f2,
                {f3_col} AS f3,
                {num_col} AS key_1,
                {email_col} AS key_2
            FROM {responses_table} sr
            WHERE sr.survey_id = %s
              AND {nps_col} IS NOT NULL
              AND sr.response_datetime >= %s
              AND sr.response_datetime  < %s
        """
        return self._execute_query(query, (survey_id, start_dt, end_dt))

    @staticmethod
    def _calculate_nps(scores: list[dict]) -> tuple[float, float, float, float]:
        if not scores:
            return 0.0, 0.0, 0.0, 0.0

        def to_float(val) -> float | None:
            try:
                return float(val)
            except (TypeError, ValueError):
                return None

        scores_float = [s for s in (to_float(r["score"]) for r in scores) if s is not None]
        total = len(scores_float)
        if total == 0:
            return 0.0, 0.0, 0.0, 0.0

        promoters  = sum(1 for s in scores_float if s >= 9)
        passives   = sum(1 for s in scores_float if 7 <= s <= 8)
        detractors = sum(1 for s in scores_float if s <= 6)

        nps           = round(((promoters - detractors) / total) * 100, 2)
        promoter_pct  = round((promoters  / total) * 100, 2)
        passive_pct   = round((passives   / total) * 100, 2)
        detractor_pct = round((detractors / total) * 100, 2)

        return nps, promoter_pct, passive_pct, detractor_pct

    # ── Date helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _build_periods(last_login_dt: datetime, now_dt: datetime) -> dict:
        """
        Build current and previous period date ranges from last login.

        current_period  : last_login_dt → now_dt
        previous_period : (last_login_dt - window) → last_login_dt
        window          : same duration as current period (so comparison is fair)
        """
        window = now_dt - last_login_dt
        if window.total_seconds() < 3600:          # guard: min 1 hour window
            window = timedelta(hours=1)

        prev_start = last_login_dt - window
        prev_end   = last_login_dt

        return {
            "cur_start":  last_login_dt,
            "cur_end":    now_dt,
            "prev_start": prev_start,
            "prev_end":   prev_end,
        }

    @staticmethod
    def _format_period_label(start: datetime, end: datetime) -> str:
        """Human-readable label e.g. '27/05/2026 → 03/06/2026'"""
        fmt = "%d/%m/%Y"
        return f"{start.strftime(fmt)} → {end.strftime(fmt)}"

    # ── Public data methods ──────────────────────────────────────────────────

    def get_nps_data(self, survey_id: int, last_login_dt: datetime, now_dt: datetime) -> dict[str, Any]:
        """
        NPS comparison: last_login → now  vs  equal window before last_login.
        """
        nps_slug = self._get_nps_slug(survey_id)
        if not nps_slug:
            logger.warning(f"No NPS slug found for survey {survey_id}")
            return {}

        p = self._build_periods(last_login_dt, now_dt)

        cur_scores  = self._get_nps_scores_for_range(survey_id, nps_slug, p["cur_start"],  p["cur_end"])
        prev_scores = self._get_nps_scores_for_range(survey_id, nps_slug, p["prev_start"], p["prev_end"])

        cur_nps,  cur_promo,  cur_passive,  cur_det  = self._calculate_nps(cur_scores)
        prev_nps, prev_promo, prev_passive, prev_det = self._calculate_nps(prev_scores)

        delta = round(cur_nps - prev_nps, 2)

        logger.debug(
            f"NPS — current: {cur_nps} ({len(cur_scores)} resp) | "
            f"previous: {prev_nps} ({len(prev_scores)} resp) | delta: {delta}"
        )

        return {
            "current":              cur_nps,
            "previous":             prev_nps,
            "delta":                delta,
            "trend":                "up" if delta >= 0 else "down",
            "total_responses":      len(cur_scores),
            "prev_total_responses": len(prev_scores),
            "promoters_pct":        cur_promo,
            "passives_pct":         cur_passive,
            "detractors_pct":       cur_det,
            "prev_promoters_pct":   prev_promo,
            "prev_passives_pct":    prev_passive,
            "prev_detractors_pct":  prev_det,
            "current_period_label": self._format_period_label(p["cur_start"],  p["cur_end"]),
            "previous_period_label":self._format_period_label(p["prev_start"], p["prev_end"]),
            # keep internals for drilldown
            "_cur_start":  p["cur_start"].isoformat(),
            "_cur_end":    p["cur_end"].isoformat(),
            "_prev_start": p["prev_start"].isoformat(),
            "_prev_end":   p["prev_end"].isoformat(),
            "_slug":       nps_slug,
        }

    def get_demographic_breakdown(
        self,
        survey_id: int,
        last_login_dt: datetime,
        now_dt: datetime,
    ) -> list[dict]:
        """
        NPS delta (current vs previous) broken down by Region → State → City.
        """
        p = self._build_periods(last_login_dt, now_dt)
        filter_labels = self._get_filter_labels(survey_id)
        results = []

        for dimension, label_key in [("region", "f1"), ("state", "f2"), ("city", "f3")]:
            rows = self._get_nps_drilldown(survey_id, dimension, p)
            label = filter_labels.get(label_key, dimension.title())
            for row in rows:
                cur_nps  = row["cur"]["nps"]
                prev_nps = row["prev"]["nps"]
                delta    = round(cur_nps - prev_nps, 2)
                results.append({
                    "type":         label,
                    "name":         row["name"],
                    "current_nps":  cur_nps,
                    "previous_nps": prev_nps,
                    "delta":        delta,
                    "trend":        "up" if delta >= 0 else "down",
                    "responses":    row["cur"]["count"],
                })

        results.sort(key=lambda x: abs(x["delta"]), reverse=True)
        return results

    def _get_nps_drilldown(self, survey_id: int, dimension: str, p: dict) -> list[dict]:
        """
        Single-query NPS drilldown using current vs previous period.
        """
        nps_slug = self._get_nps_slug(survey_id)
        if not nps_slug:
            return []

        responses_table = self._get_responses_table_name(survey_id)
        nps_col         = self._resolve_column_name(responses_table, [nps_slug, "NPS_SCORE", "nps_score", "nps"], nps_slug)

        f1_col = self._resolve_column_name(responses_table, ["REGION", "region"], "REGION")
        f2_col = self._resolve_column_name(responses_table, ["LOCATION", "location", "area"], "LOCATION")
        f3_col = self._resolve_column_name(responses_table, ["BRANCH", "branch"], "BRANCH")

        dim_map = {
            "region": f1_col,
            "state":  f2_col,
            "city":   f3_col,
        }
        field = dim_map.get(dimension.lower(), f1_col)

        cur_start  = p["cur_start"]
        cur_end    = p["cur_end"]
        prev_start = p["prev_start"]
        prev_end   = p["prev_end"]

        query = f"""
            SELECT
                {field} AS name,
                MAX({f1_col}) AS parent_region,
                -- current period (last_login → now)
                COUNT(CASE WHEN sr.response_datetime >= %s AND sr.response_datetime < %s THEN 1 END)                                 AS cur_count,
                COUNT(CASE WHEN sr.response_datetime >= %s AND sr.response_datetime < %s AND {nps_col} >= 9        THEN 1 END)   AS cur_promoters,
                COUNT(CASE WHEN sr.response_datetime >= %s AND sr.response_datetime < %s AND {nps_col} BETWEEN 7 AND 8 THEN 1 END) AS cur_passives,
                COUNT(CASE WHEN sr.response_datetime >= %s AND sr.response_datetime < %s AND {nps_col} <= 6        THEN 1 END)   AS cur_detractors,
                -- previous period (equal window before last_login)
                COUNT(CASE WHEN sr.response_datetime >= %s AND sr.response_datetime < %s THEN 1 END)                                 AS prev_count,
                COUNT(CASE WHEN sr.response_datetime >= %s AND sr.response_datetime < %s AND {nps_col} >= 9        THEN 1 END)   AS prev_promoters,
                COUNT(CASE WHEN sr.response_datetime >= %s AND sr.response_datetime < %s AND {nps_col} BETWEEN 7 AND 8 THEN 1 END) AS prev_passives,
                COUNT(CASE WHEN sr.response_datetime >= %s AND sr.response_datetime < %s AND {nps_col} <= 6        THEN 1 END)   AS prev_detractors
            FROM {responses_table} sr
            WHERE sr.survey_id = %s
              AND {nps_col} IS NOT NULL
              AND (
                  (sr.response_datetime >= %s AND sr.response_datetime < %s)
                  OR
                  (sr.response_datetime >= %s AND sr.response_datetime < %s)
              )
            GROUP BY {field}
            ORDER BY cur_count DESC
            LIMIT 20
        """
        params = (
            cur_start, cur_end, cur_start, cur_end, cur_start, cur_end, cur_start, cur_end,
            prev_start, prev_end, prev_start, prev_end, prev_start, prev_end, prev_start, prev_end,
            survey_id,
            cur_start, cur_end, prev_start, prev_end,
        )

        rows = self._execute_query(query, params)

        def _nps_stats(promoters, passives, detractors, total):
            if not total:
                return {"nps": 0.0, "promoter_pct": 0.0, "passive_pct": 0.0, "detractor_pct": 0.0}
            return {
                "nps":           round(((promoters - detractors) / total) * 100, 2),
                "promoter_pct":  round((promoters / total) * 100, 2),
                "passive_pct":   round((passives  / total) * 100, 2),
                "detractor_pct": round((detractors / total) * 100, 2),
            }

        result = []
        for r in rows:
            cur_total  = r["cur_count"]  or 0
            prev_total = r["prev_count"] or 0
            cur_stats  = _nps_stats(r["cur_promoters"],  r["cur_passives"],  r["cur_detractors"],  cur_total)
            prev_stats = _nps_stats(r["prev_promoters"], r["prev_passives"], r["prev_detractors"], prev_total)
            name = r["name"]
            if dimension == "city" and r.get("parent_region"):
                name = f"{r['parent_region']} - {name}"
            result.append({
                "name": name,
                "cur":  {**cur_stats,  "count": cur_total},
                "prev": {**prev_stats, "count": prev_total},
            })
        return result

    def get_customer_voice_data(self, survey_id: int, last_login_dt: datetime, now_dt: datetime) -> dict[str, Any]:
        """
        High-severity negative voc_alerts created since last login.
        """
        if not self._check_table_exists("voc_alerts"):
            logger.warning(f"voc_alerts table not found for survey {survey_id}")
            return {"high_severity_records": []}

        # Get the touchpoint code or survey name for this survey from the surveys table
        survey_rows = self._execute_query("SELECT touch_point, survey_name FROM surveys WHERE id = %s", (survey_id,))
        tp_pattern = "%"
        if survey_rows:
            tp_code = survey_rows[0].get("touch_point")
            survey_name = survey_rows[0].get("survey_name")
            if tp_code:
                tp_pattern = f"{tp_code.upper()}%"
            elif survey_name:
                tp_pattern = f"{survey_name}%"

        responses_table = self._get_responses_table_name(survey_id)
        
        # Check if responses table has AWB column
        cols = self._get_table_columns(responses_table)
        has_awb = any(x.lower() in ["awbnumber", "awb_number", "awb"] for x in cols)
        
        if has_awb:
            f1_col = self._resolve_column_name(responses_table, ["REGION", "region"], "REGION")
            f2_col = self._resolve_column_name(responses_table, ["LOCATION", "location", "zone", "area"], "LOCATION")
            f3_col = self._resolve_column_name(responses_table, ["BRANCH", "branch"], "BRANCH")
            awb_col = self._resolve_column_name(responses_table, ["AWBNUMBER", "awb_number", "awb"], "AWBNUMBER")

            rows = self._execute_query(
                f"""
                SELECT
                    COALESCE(v.priority_level, 'medium') AS priority_level,
                    COALESCE(v.category, 'General') AS theme,
                    v.customer_verbatim AS verbatim,
                    v.keyword,
                    v.is_critical,
                    v.created_at,
                    {f1_col} AS f1_val,
                    {f2_col} AS f2_val,
                    {f3_col} AS f3_val,
                    v.emp_name AS f4_val
                FROM voc_alerts v
                LEFT JOIN {responses_table} sr ON v.awb_number COLLATE utf8mb4_unicode_ci = {awb_col} COLLATE utf8mb4_unicode_ci
                WHERE v.touchpoint LIKE %s
                  AND v.is_critical = 1
                  AND v.nps_score <= 6
                  AND v.created_at >= %s
                  AND v.created_at  < %s
                ORDER BY
                    v.is_critical DESC,
                    v.created_at DESC
                """,
                (tp_pattern, last_login_dt, now_dt),
            )
        else:
            # Fallback when there is no AWB column to join on
            rows = self._execute_query(
                f"""
                SELECT
                    COALESCE(v.priority_level, 'medium') AS priority_level,
                    COALESCE(v.category, 'General') AS theme,
                    v.customer_verbatim AS verbatim,
                    v.keyword,
                    v.is_critical,
                    v.created_at,
                    NULL AS f1_val,
                    NULL AS f2_val,
                    NULL AS f3_val,
                    v.emp_name AS f4_val
                FROM voc_alerts v
                WHERE v.touchpoint LIKE %s
                  AND v.nps_score <= 6
                  AND v.created_at >= %s
                  AND v.created_at  < %s
                ORDER BY
                    v.is_critical DESC,
                    v.created_at DESC
                """,
                (tp_pattern, last_login_dt, now_dt),
            )

        priority_score_map = {"critical": 95, "urgent": 90, "high": 85, "medium": 70, "low": 50}
        records = []
        master_name_cache = {}
        for row in rows:
            priority       = (row.get("priority_level") or "").lower().strip()
            severity_score = priority_score_map.get(priority, 70)
            is_critical    = bool(row.get("is_critical")) or priority == "critical" or severity_score >= 90

            f1 = (row.get("f1_val") or "").strip()
            f2 = (row.get("f2_val") or "").strip()
            f3 = (row.get("f3_val") or "").strip()
            
            f1_full = self._resolve_master_name("master_regions", f1, master_name_cache)
            f2_full = self._resolve_master_name("master_locations", f2, master_name_cache)
            f3_full = self._resolve_master_name("master_branches", f3, master_name_cache)

            records.append({
                "severity_score": severity_score,
                "theme":          (row.get("theme") or "Unknown").strip(),
                "sub_category":   "",
                "verbatim":       re.sub(r'[\r\n]+', ' ', (row.get("verbatim") or "").strip())[:300],
                "f1_label":       "Region",
                "f1_val":         f1_full,
                "f2_label":       "Zone",
                "f2_val":         f2_full,
                "f3_label":       "Branch",
                "f3_val":         f3_full,
                "f4_label":       "Agent",
                "f4_val":         str(row.get("f4_val") if row.get("f4_val") is not None else "").strip(),
                "keyword":        (row.get("keyword") or "").strip(),
                "priority":       priority,
                "churn_intent":   is_critical,
            })

        return {"high_severity_records": records}

    def get_available_survey_ids(self, limit: int = 200) -> list[int]:
        if self._check_table_exists("surveys"):
            rows = self._execute_query(
                "SELECT id FROM surveys WHERE id IS NOT NULL ORDER BY id DESC LIMIT %s",
                (limit,),
            )
            ids = []
            for r in rows:
                try:
                    ids.append(int(r.get("id")))
                except Exception:
                    continue
            return sorted(set(ids))

        rows = self._execute_query(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = %s AND table_name LIKE 'survey_dynamic_id_%'
            ORDER BY table_name DESC LIMIT %s
            """,
            (settings.survey_db_name, limit * 3),
        )
        ids_set: set[int] = set()
        for r in rows:
            name = r.get("table_name") or ""
            parts = name.split("survey_dynamic_id_")
            if len(parts) != 2:
                continue
            try:
                ids_set.add(int(parts[1]))
            except Exception:
                continue
        ids = sorted(ids_set)
        return ids[-limit:] if len(ids) > limit else ids

    def get_survey_ids_by_client(self, client_id: int = None) -> list[int]:
        """Query the surveys table to find all survey IDs."""
        if not self._check_table_exists("surveys"):
            logger.warning("surveys table does not exist")
            return []
        try:
            rows = self._execute_query("SELECT id FROM surveys")
            ids = [int(r["id"]) for r in rows if r.get("id") is not None]
            
            valid_ids = []
            for sid in ids:
                if self._check_table_exists(self._get_responses_table_name(sid)):
                    valid_ids.append(sid)
            return valid_ids
        except Exception as e:
            logger.warning(f"Error querying 'id' from surveys: {e}")
            try:
                rows = self._execute_query("SELECT survey_id FROM surveys")
                ids = [int(r["survey_id"]) for r in rows if r.get("survey_id") is not None]
                valid_ids = []
                for sid in ids:
                    if self._check_table_exists(self._get_responses_table_name(sid)):
                        valid_ids.append(sid)
                return valid_ids
            except Exception as ex:
                logger.error(f"Error querying 'survey_id' from surveys: {ex}")
        return []

    def get_user_name_by_id(self, user_id: int = 2) -> str:
        """Query the users table to get the human-readable name of the user."""
        if not self._check_table_exists("users"):
            logger.warning("users table does not exist")
            return "bluedart"
        try:
            rows = self._execute_query("SELECT name FROM users WHERE id = %s", (user_id,))
            if rows and rows[0].get("name"):
                return rows[0]["name"]
        except Exception as e:
            logger.error(f"Error querying user name: {e}")
        return "bluedart"

    def get_nps_data_for_surveys(self, survey_ids: list[int], last_login_dt: datetime, now_dt: datetime) -> dict[str, Any]:
        """
        NPS comparison aggregated across multiple surveys.
        """
        if not survey_ids:
            return {}

        p = self._build_periods(last_login_dt, now_dt)
        all_cur_scores = []
        all_prev_scores = []

        for survey_id in survey_ids:
            nps_slug = self._get_nps_slug(survey_id)
            if not nps_slug:
                continue
            cur_scores  = self._get_nps_scores_for_range(survey_id, nps_slug, p["cur_start"],  p["cur_end"])
            prev_scores = self._get_nps_scores_for_range(survey_id, nps_slug, p["prev_start"], p["prev_end"])
            all_cur_scores.extend(cur_scores)
            all_prev_scores.extend(prev_scores)

        if not all_cur_scores and not all_prev_scores:
            return {}

        cur_nps,  cur_promo,  cur_passive,  cur_det  = self._calculate_nps(all_cur_scores)
        prev_nps, prev_promo, prev_passive, prev_det = self._calculate_nps(all_prev_scores)

        delta = round(cur_nps - prev_nps, 2)

        return {
            "current":              cur_nps,
            "previous":             prev_nps,
            "delta":                delta,
            "trend":                "up" if delta >= 0 else "down",
            "total_responses":      len(all_cur_scores),
            "prev_total_responses": len(all_prev_scores),
            "promoters_pct":        cur_promo,
            "passives_pct":         cur_passive,
            "detractors_pct":       cur_det,
            "prev_promoters_pct":   prev_promo,
            "prev_passives_pct":    prev_passive,
            "prev_detractors_pct":  prev_det,
            "current_period_label": self._format_period_label(p["cur_start"],  p["cur_end"]),
            "previous_period_label":self._format_period_label(p["prev_start"], p["prev_end"]),
            "_cur_start":  p["cur_start"].isoformat(),
            "_cur_end":    p["cur_end"].isoformat(),
            "_prev_start": p["prev_start"].isoformat(),
            "_prev_end":   p["prev_end"].isoformat(),
        }

    def get_survey_comparison(self, survey_ids: list[int], last_login_dt: datetime, now_dt: datetime) -> list[dict]:
        """
        Compare individual surveys against each other for the given time period.
        """
        if not survey_ids:
            return []

        p = self._build_periods(last_login_dt, now_dt)
        results = []

        try:
            format_strings = ','.join(['%s'] * len(survey_ids))
            query = f"SELECT id, survey_name AS name FROM surveys WHERE id IN ({format_strings})"
            rows = self._execute_query(query, tuple(survey_ids))
            survey_names = {int(row["id"]): row["name"] for row in rows}
        except Exception as e:
            logger.warning(f"Failed to fetch survey names: {e}")
            survey_names = {}

        for survey_id in survey_ids:
            nps_slug = self._get_nps_slug(survey_id)
            if not nps_slug:
                continue
            
            cur_scores  = self._get_nps_scores_for_range(survey_id, nps_slug, p["cur_start"],  p["cur_end"])
            prev_scores = self._get_nps_scores_for_range(survey_id, nps_slug, p["prev_start"], p["prev_end"])
            
            cur_nps,  _, _, _ = self._calculate_nps(cur_scores)
            prev_nps, _, _, _ = self._calculate_nps(prev_scores)
            
            delta = round(cur_nps - prev_nps, 2)
            
            results.append({
                "survey_id": survey_id,
                "name": survey_names.get(survey_id, f"Survey #{survey_id}"),
                "current_nps": cur_nps,
                "previous_nps": prev_nps,
                "delta": delta,
                "trend": "up" if delta >= 0 else "down",
                "responses": len(cur_scores)
            })
            
        results.sort(key=lambda x: abs(x["delta"]), reverse=True)
        return results

    def _get_raw_nps_drilldown(self, survey_id: int, dimension: str, p: dict) -> list[dict]:
        nps_slug = self._get_nps_slug(survey_id)
        if not nps_slug:
            return []
        responses_table = self._get_responses_table_name(survey_id)
        nps_col         = self._resolve_column_name(responses_table, [nps_slug, "NPS_SCORE", "nps_score", "nps"], nps_slug)

        f1_col = self._resolve_column_name(responses_table, ["REGION", "region"], "REGION")
        f2_col = self._resolve_column_name(responses_table, ["LOCATION", "location", "area"], "LOCATION")
        f3_col = self._resolve_column_name(responses_table, ["BRANCH", "branch"], "BRANCH")

        dim_map = {
            "region": f1_col,
            "state":  f2_col,
            "city":   "sr.BRANCH" if "sr.BRANCH" in f3_col else f3_col,
        }
        field = dim_map.get(dimension.lower(), f1_col)

        # Remove the 'sr.' prefix from the field used in GROUP BY and SELECT
        # for standard column grouping where sql requires clean aliases or exact fields
        cur_start  = p["cur_start"]
        cur_end    = p["cur_end"]
        prev_start = p["prev_start"]
        prev_end   = p["prev_end"]

        query = f"""
            SELECT
                {field} AS name,
                MAX({f1_col}) AS parent_region,
                MAX({f2_col}) AS parent_location,
                COUNT(CASE WHEN sr.response_datetime >= %s AND sr.response_datetime < %s THEN 1 END) AS cur_count,
                COUNT(CASE WHEN sr.response_datetime >= %s AND sr.response_datetime < %s AND {nps_col} >= 9 THEN 1 END) AS cur_promoters,
                COUNT(CASE WHEN sr.response_datetime >= %s AND sr.response_datetime < %s AND {nps_col} BETWEEN 7 AND 8 THEN 1 END) AS cur_passives,
                COUNT(CASE WHEN sr.response_datetime >= %s AND sr.response_datetime < %s AND {nps_col} <= 6 THEN 1 END) AS cur_detractors,
                COUNT(CASE WHEN sr.response_datetime >= %s AND sr.response_datetime < %s THEN 1 END) AS prev_count,
                COUNT(CASE WHEN sr.response_datetime >= %s AND sr.response_datetime < %s AND {nps_col} >= 9 THEN 1 END) AS prev_promoters,
                COUNT(CASE WHEN sr.response_datetime >= %s AND sr.response_datetime < %s AND {nps_col} BETWEEN 7 AND 8 THEN 1 END) AS prev_passives,
                COUNT(CASE WHEN sr.response_datetime >= %s AND sr.response_datetime < %s AND {nps_col} <= 6 THEN 1 END) AS prev_detractors
            FROM {responses_table} sr
            WHERE sr.survey_id = %s
              AND {nps_col} IS NOT NULL
              AND (
                  (sr.response_datetime >= %s AND sr.response_datetime < %s)
                  OR
                  (sr.response_datetime >= %s AND sr.response_datetime < %s)
              )
            GROUP BY {field}
            ORDER BY cur_count DESC
        """
        params = (
            cur_start, cur_end, cur_start, cur_end, cur_start, cur_end, cur_start, cur_end,
            prev_start, prev_end, prev_start, prev_end, prev_start, prev_end, prev_start, prev_end,
            survey_id,
            cur_start, cur_end, prev_start, prev_end,
        )
        return self._execute_query(query, params)

    def get_demographic_breakdown_for_surveys(
        self,
        survey_ids: list[int],
        last_login_dt: datetime,
        now_dt: datetime,
    ) -> list[dict]:
        if not survey_ids:
            return []

        p = self._build_periods(last_login_dt, now_dt)
        from collections import defaultdict

        agg_data = defaultdict(lambda: {
            "cur_count": 0, "cur_promoters": 0, "cur_passives": 0, "cur_detractors": 0,
            "prev_count": 0, "prev_promoters": 0, "prev_passives": 0, "prev_detractors": 0
        })

        master_name_cache = {}
        for survey_id in survey_ids:
            filter_labels = self._get_filter_labels(survey_id)
            for dimension, label_key in [("region", "f1"), ("state", "f2"), ("city", "f3")]:
                label = filter_labels.get(label_key, dimension.title())
                rows = self._get_raw_nps_drilldown(survey_id, dimension, p)
                for r in rows:
                    name = r["name"]
                    if dimension == "city":
                        parts = []
                        if r.get("parent_region"):
                            reg_full = self._resolve_master_name("master_regions", r["parent_region"], master_name_cache)
                            parts.append(f"{reg_full} (Region)")
                        if r.get("parent_location"):
                            loc_full = self._resolve_master_name("master_locations", r["parent_location"], master_name_cache)
                            parts.append(f"{loc_full} (Location)")
                        branch_full = self._resolve_master_name("master_branches", name, master_name_cache)
                        parts.append(branch_full)
                        name = " - ".join(parts)
                    elif dimension == "state":
                        loc_full = self._resolve_master_name("master_locations", name, master_name_cache)
                        name = loc_full
                        if r.get("parent_region"):
                            reg_full = self._resolve_master_name("master_regions", r["parent_region"], master_name_cache)
                            name = f"{reg_full} (Region) - {name}"
                    elif dimension == "region":
                        reg_full = self._resolve_master_name("master_regions", name, master_name_cache)
                        name = reg_full
                    key = (label, name)
                    agg_data[key]["cur_count"]      += r["cur_count"] or 0
                    agg_data[key]["cur_promoters"]  += r["cur_promoters"] or 0
                    agg_data[key]["cur_passives"]   += r["cur_passives"] or 0
                    agg_data[key]["cur_detractors"] += r["cur_detractors"] or 0
                    agg_data[key]["prev_count"]     += r["prev_count"] or 0
                    agg_data[key]["prev_promoters"] += r["prev_promoters"] or 0
                    agg_data[key]["prev_passives"]  += r["prev_passives"] or 0
                    agg_data[key]["prev_detractors"]+= r["prev_detractors"] or 0

        def _nps_stats(promoters, passives, detractors, total):
            if not total:
                return {"nps": 0.0, "promoter_pct": 0.0, "passive_pct": 0.0, "detractor_pct": 0.0}
            return {
                "nps":           round(((promoters - detractors) / total) * 100, 2),
                "promoter_pct":  round((promoters / total) * 100, 2),
                "passive_pct":   round((passives  / total) * 100, 2),
                "detractor_pct": round((detractors / total) * 100, 2),
            }

        results = []
        for (label, name), counts in agg_data.items():
            cur_stats  = _nps_stats(counts["cur_promoters"], counts["cur_passives"], counts["cur_detractors"], counts["cur_count"])
            prev_stats = _nps_stats(counts["prev_promoters"], counts["prev_passives"], counts["prev_detractors"], counts["prev_count"])
            delta      = round(cur_stats["nps"] - prev_stats["nps"], 2)
            results.append({
                "type":         label,
                "name":         name,
                "current_nps":  cur_stats["nps"],
                "previous_nps": prev_stats["nps"],
                "delta":        delta,
                "trend":        "up" if delta >= 0 else "down",
                "responses":    counts["cur_count"],
            })

        results.sort(key=lambda x: abs(x["delta"]), reverse=True)
        return results

    def get_customer_voice_data_for_surveys(self, survey_ids: list[int], last_login_dt: datetime, now_dt: datetime) -> dict[str, Any]:
        all_records = []
        for survey_id in survey_ids:
            voice = self.get_customer_voice_data(survey_id, last_login_dt, now_dt)
            all_records.extend(voice.get("high_severity_records", []))
            
        # Sort globally across all surveys by criticality and created_at descending
        all_records.sort(
            key=lambda x: (
                x.get('is_critical') or 0, 
                x.get('created_at') if isinstance(x.get('created_at'), datetime) else datetime.min
            ), 
            reverse=True
        )
        return {"high_severity_records": all_records}

    def get_survey_id_by_touchpoint(self, touch_point_id: Any) -> list[int]:
        """
        Given a touch_point_id (which can be a survey ID or touchpoint code),
        returns the matching survey ID(s) that have dynamic tables.
        """
        if not touch_point_id:
            return []
            
        # 1. Try treating as direct survey ID (integer)
        try:
            val = int(touch_point_id)
            if self._check_table_exists(self._get_responses_table_name(val)):
                return [val]
        except (ValueError, TypeError):
            pass

        # 2. Match by touch_point code or survey name
        tp_str = str(touch_point_id).strip().lower()
        if not tp_str:
            return []

        # If it is a raw number (like "8"), support mapping to "tp8"
        tp_variants = [tp_str]
        if tp_str.isdigit():
            tp_variants.append(f"tp{tp_str}")

        format_strings = ','.join(['%s'] * len(tp_variants))
        query = f"""
            SELECT id FROM surveys 
            WHERE (
                  LOWER(touch_point) IN ({format_strings}) 
                  OR LOWER(survey_name) IN ({format_strings})
                  OR LOWER(survey_name) LIKE %s
              )
        """
        params = tp_variants + tp_variants + [f"%tp{tp_str}%"]
        rows = self._execute_query(query, tuple(params))
        
        valid_ids = []
        for r in rows:
            sid = int(r["id"])
            if self._check_table_exists(self._get_responses_table_name(sid)):
                valid_ids.append(sid)
        return valid_ids


    def _resolve_master_name(self, table_name: str, code: str, cache_dict: dict = None) -> str:
        """Resolves a short name/code to its full name using in-memory master table caching."""
        if not code:
            return ""
        return str(code).strip()

    def close(self) -> None:
        if self.engine:
            try:
                self.engine.dispose()
            except Exception as e:
                logger.warning(f"Error closing engine: {e}")
            finally:
                self.engine = None


# ── Module-level singleton ────────────────────────────────────────────────────
def get_db_service() -> Optional[DatabaseService]:
    global _db_service_instance, DB_AVAILABLE
    if _db_service_instance is None or not DB_AVAILABLE:
        try:
            svc = DatabaseService()
            test = svc.test_connection()
            if test.get("connected"):
                _db_service_instance = svc
                DB_AVAILABLE = True
            else:
                logger.warning(f"⚠️  DB not available: {test.get('error')}. Using mock data.")
                DB_AVAILABLE = False
        except Exception as e:
            logger.warning(f"⚠️  DB init failed: {e}. Using mock data.")
            DB_AVAILABLE = False
    return _db_service_instance

def reset_db_service() -> None:
    """Clear the cached database service instance."""
    global _db_service_instance, DB_AVAILABLE
    if _db_service_instance:
        _db_service_instance.close()
    _db_service_instance = None
    DB_AVAILABLE = False
    logger.info("Database service cache cleared.")
