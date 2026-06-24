"""
Popup API Route

DATE LOGIC:
  - Accept `last_login` as ISO datetime string from the frontend.
  - current_period  : last_login → now      (what changed since you were here)
  - previous_period : equal window before last_login  (for comparison)
  - All W1/W2 week concepts removed.
"""
from datetime import datetime, timedelta

from pydantic import BaseModel
from fastapi import APIRouter, HTTPException, Query, Form, BackgroundTasks, Request
from fastapi.responses import PlainTextResponse, FileResponse, Response
import os
import uuid
from loguru import logger

from config import settings
import database
from database import get_db_service
from services.ai_service import generate_ai_summary, answer_user_question
from services.mock_data import get_mock_popup_data
from services.tts_service import generate_audio_file
from services.cache_service import cache_service

router = APIRouter()

@router.get("/audio/{filename}")
async def get_audio(filename: str):
    filepath = os.path.join("static", "audio", filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Audio file not found")
    
    return FileResponse(filepath, media_type="audio/mpeg")

_DEFAULT_LAST_LOGIN_HOURS = 24 * 7   # fallback: 7 days ago if not supplied


def _parse_datetime(dt_str: str | None, default_offset_hours: int | None = None, is_end_date: bool = False) -> datetime:
    """Parse ISO or custom format date strings (like DD/MM/YYYY, YYYY-MM-DD). Falls back to offset or now."""
    parsed_dt = None
    if dt_str:
        dt_str = dt_str.strip()
        try:
            parsed_dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            pass
        if not parsed_dt:
            formats = [
                "%d/%m/%Y %H:%M:%S",
                "%d/%m/%Y %I:%M %p",
                "%d/%m/%Y",
                "%d-%m-%Y %H:%M:%S",
                "%d-%m-%Y %I:%M %p",
                "%d-%m-%Y",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d",
            ]
            for fmt in formats:
                try:
                    parsed_dt = datetime.strptime(dt_str, fmt)
                    break
                except Exception:
                    pass

    if not parsed_dt:
        if default_offset_hours is not None:
            parsed_dt = datetime.now() - timedelta(hours=default_offset_hours)
        else:
            parsed_dt = datetime.now()
            
    # If this is the end of a date range and the user only supplied a date (00:00:00), 
    # push it to the end of the day so it includes all events on that day.
    if is_end_date and parsed_dt.hour == 0 and parsed_dt.minute == 0 and parsed_dt.second == 0:
        parsed_dt = parsed_dt.replace(hour=23, minute=59, second=59, microsecond=999999)
        
    return parsed_dt




@router.post("/ask-ai")
async def ask_ai(
    user_last_login_date: str = Form(...),
    user_current_login_date: str = Form(...),
    question: str = Form(...)
):
    current_login_dt = _parse_datetime(user_current_login_date, is_end_date=True)
    last_login_dt    = _parse_datetime(user_last_login_date, default_offset_hours=_DEFAULT_LAST_LOGIN_HOURS)

    if last_login_dt > current_login_dt:
        raise HTTPException(status_code=400, detail="Last login date cannot be in the future relative to the current login date.")

    db = get_db_service()
    client_id = 2
    if db and database.DB_AVAILABLE and not settings.use_mock_data:
        try:
            survey_ids = []
            for tp in [6, 7, 8, 9, 10]:
                survey_ids.extend(db.get_survey_id_by_touchpoint(tp))
        except Exception as e:
            logger.error(f"Ask AI db error (surveys): {e}")
            raise HTTPException(status_code=503, detail="Service Unavailable: Database error.")
    else:
        survey_ids = []

    if not database.DB_AVAILABLE or settings.use_mock_data or db is None:
        return {"answer": "We are currently experiencing a temporary database connection issue. Our team is working to restore live data access shortly."}

    try:
        import asyncio
        nps_data = await asyncio.to_thread(db.get_nps_data_for_surveys, survey_ids, last_login_dt, current_login_dt)
        if not nps_data:
            return {"answer": "There are no new survey responses recorded since your last login. You are fully caught up!"}

        demographics, voice, survey_comparison = await asyncio.gather(
            asyncio.to_thread(db.get_demographic_breakdown_for_surveys, survey_ids, last_login_dt, current_login_dt),
            asyncio.to_thread(db.get_customer_voice_data_for_surveys, survey_ids, last_login_dt, current_login_dt),
            asyncio.to_thread(db.get_survey_comparison, survey_ids, last_login_dt, current_login_dt)
        )
        critical_issues = await asyncio.to_thread(_aggregate_issues, voice.get("high_severity_records", []))

        answer = await asyncio.to_thread(answer_user_question, nps_data, demographics, critical_issues, question, survey_comparison)
        return {"answer": answer}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ask AI endpoint error: {e}")
        raise HTTPException(status_code=503, detail="Service Unavailable: Database error.")

@router.post("/login-popup-summary")
async def login_popup_summary(
    request: Request,
    background_tasks: BackgroundTasks,
    user_last_login_date: str = Form(...),
    user_current_login_date: str = Form(...)
):
    """
    Dedicated endpoint that accepts Form Data, securely queries the user's real name,
    and returns a pre-formatted HTML AI Summary wrapped in <p> and <strong> tags 
    alongside a personalized greeting.
    """
    try:
        last_login_dt = _parse_datetime(user_last_login_date, default_offset_hours=_DEFAULT_LAST_LOGIN_HOURS)
        current_login_dt = _parse_datetime(user_current_login_date, is_end_date=True)
            
        if last_login_dt > current_login_dt:
            raise HTTPException(status_code=400, detail="Last login date cannot be in the future relative to the current login date.")

        client_id = 2
        
        # Check Cache
        cache_key = f"{client_id}_{last_login_dt.isoformat()}_{current_login_dt.isoformat()}_all_tps"
        cached_response = cache_service.get_summary_cache(cache_key)
        if cached_response:
            logger.info("Returning fully cached popup response")
            return cached_response

        # Build human-readable dates for AI
        last_login_label = last_login_dt.strftime("%b %d, %Y")
        
        # Use the explicit base_url from .env if defined, otherwise fallback to the automatic request base url
        base_url = settings.base_url.rstrip("/") if settings.base_url else str(request.base_url).rstrip("/")
        
        db = get_db_service()
        if not db or not db._ensure_connection() or not database.DB_AVAILABLE or settings.use_mock_data:
            summary_text = "We are currently experiencing a temporary database connection issue. Our team is working to restore live data access shortly. Please check back soon."
            summary_audio_filename = f"tts_{uuid.uuid4().hex}.mp3"
            background_tasks.add_task(generate_audio_file, text=summary_text, filename=summary_audio_filename)
            return {
                "greeting": f"Good day User {client_id}",
                "ai_summary": "<p>We are currently experiencing a temporary database connection issue. Our team is working to restore live data access shortly. Please check back soon.</p>",
                "ai_summary_audio_url": f"{base_url}/api/audio/{summary_audio_filename}" if summary_audio_filename else None,
                "top_alert_VOC": [],
                "voc_audio_url": None,
                "is_data_found": 0
            }

        # 1. Fetch User Name
        user_name = db.get_user_name_by_id(client_id)
        
        hour = datetime.now().hour
        greeting_time = "Good Morning"
        if 12 <= hour < 17:
            greeting_time = "Good Afternoon"
        elif hour >= 17:
            greeting_time = "Good Evening"
        greeting = f"{greeting_time}, {user_name}"
        # 2. Fetch surveys and analytics with fallback for DB errors
        import asyncio
        try:
            survey_ids = []
            for tp in [6, 7, 8, 9, 10]:
                survey_ids.extend(db.get_survey_id_by_touchpoint(tp))

            # 3. Fetch analytics
            nps_data, demographics, voice, survey_comparison = await asyncio.gather(
                asyncio.to_thread(db.get_nps_data_for_surveys, survey_ids, last_login_dt, current_login_dt),
                asyncio.to_thread(db.get_demographic_breakdown_for_surveys, survey_ids, last_login_dt, current_login_dt),
                asyncio.to_thread(db.get_customer_voice_data_for_surveys, survey_ids, last_login_dt, current_login_dt),
                asyncio.to_thread(db.get_survey_comparison, survey_ids, last_login_dt, current_login_dt)
            )
            
            if not nps_data:
                nps_data = {}
            critical_issues = await asyncio.to_thread(_aggregate_issues, voice.get("high_severity_records", []))
        except Exception as e:
            logger.error(f"Database error during popup summary fetch: {e}")
            summary_text = "We are currently experiencing a temporary database connection issue. Our team is working to restore live data access shortly. Please check back soon."
            summary_audio_filename = f"tts_{uuid.uuid4().hex}.mp3"
            background_tasks.add_task(generate_audio_file, text=summary_text, filename=summary_audio_filename)
            response_data = {
                "greeting": greeting,
                "ai_summary": "<p>We are currently experiencing a temporary database connection issue. Our team is working to restore live data access shortly. Please check back soon.</p>",
                "ai_summary_audio_url": f"{base_url}/api/audio/{summary_audio_filename}" if summary_audio_filename else None,
                "top_alert_VOC": [],
                "voc_audio_url": None,
                "is_data_found": 0
            }
            # Store in cache for 1 minute for failures to prevent spamming broken DB
            cache_service.set_summary_cache(cache_key, response_data, ttl_seconds=60)
            return response_data

        if not nps_data and not critical_issues:
            summary_text = f"Welcome back! We have checked the system, and there are no new survey responses recorded since your last login on {last_login_label}. You are fully caught up!"
            summary_audio_filename = f"tts_{uuid.uuid4().hex}.mp3"
            background_tasks.add_task(generate_audio_file, text=summary_text, filename=summary_audio_filename)
            return {
                "greeting": greeting,
                "ai_summary": f"<p>Welcome back! We have checked the system, and there are <strong>no new survey responses</strong> recorded since your last login on <strong>{last_login_label}</strong>. You are fully caught up!</p>",
                "ai_summary_audio_url": f"{base_url}/api/audio/{summary_audio_filename}" if summary_audio_filename else None,
                "top_alert_VOC": [],
                "voc_audio_url": None,
                "is_data_found": 0
            }

        ai = await asyncio.to_thread(generate_ai_summary, nps_data, demographics, critical_issues, survey_comparison, last_login_label, current_login_dt, html_format=True)

        raw_vocs = voice.get("high_severity_records", [])
        top_alert_voc = []
        for r in raw_vocs:
            if r.get("verbatim") and len(top_alert_voc) < 5:
                extra_parts = []
                for x in [1, 2, 3, 4]:
                    if r.get(f"f{x}_val"):
                        extra_parts.append(f"{r.get(f'f{x}_label', f'Level {x}')}:{r[f'f{x}_val']}")
                extra_info = ", ".join(extra_parts)
                
                voc_entry = {"verbatim": r["verbatim"]}
                if extra_info:
                    voc_entry["extra_info"] = extra_info
                top_alert_voc.append(voc_entry)

        if not top_alert_voc:
            top_alert_voc = [{"verbatim": "No VOC found from your last login."}]
            voc_tts_text = "No critical feedback found from your last login."
        else:
            voc_texts = []
            for i, v in enumerate(top_alert_voc):
                text = f"Feedback {i+1}: {v.get('verbatim', '')}"
                extra = v.get('extra_info', '')
                if extra:
                    parts = [p.strip() for p in extra.split(',')]
                    clean_parts = []
                    for p in parts:
                        if p.lower().startswith("region:") or p.lower().startswith("zone:") or p.lower().startswith("branch:") or p.lower().startswith("agent:"):
                            clean_parts.append(p)
                    if clean_parts:
                        text += f". {' and '.join(clean_parts)}"
                voc_texts.append(text)
            voc_tts_text = "Here is the top customer feedback. " + " ".join(voc_texts)

        # Generate audio for summary and VOCs in the background
        summary_audio_filename = f"tts_{uuid.uuid4().hex}.mp3"
        voc_audio_filename = f"tts_{uuid.uuid4().hex}.mp3"
        
        background_tasks.add_task(generate_audio_file, text=ai.get("summary", ""), filename=summary_audio_filename)
        background_tasks.add_task(generate_audio_file, text=voc_tts_text, filename=voc_audio_filename)

        response_data = {
            "greeting": greeting,
            "ai_summary": ai.get("summary", ""),
            "ai_summary_audio_url": f"{base_url}/api/audio/{summary_audio_filename}" if summary_audio_filename else None,
            "top_alert_VOC": top_alert_voc,
            "voc_audio_url": f"{base_url}/api/audio/{voc_audio_filename}" if voc_audio_filename else None,
            "is_data_found": 1
        }
        
        # Store in cache for 10 minutes
        cache_service.set_summary_cache(cache_key, response_data, ttl_seconds=600)
        
        return response_data

    except Exception as e:
        logger.error(f"Login Popup Summary endpoint error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error.")




@router.get("/health")
async def health():
    db = get_db_service()
    if db:
        return db.test_connection()
    return {"connected": False, "error": "DB service not initialized"}

@router.post("/clear-cache")
async def clear_cache():
    """
    Clears any internal cache (like the database connection pool instance)
    so that the application is forced to establish a fresh connection on the next request.
    """
    try:
        from database import reset_db_service
        reset_db_service()
        return {"status": "success", "message": "Cache cleared successfully."}
    except Exception as e:
        logger.error(f"Error clearing cache: {e}")
        raise HTTPException(status_code=500, detail="Failed to clear cache.")


@router.get("/logs")
async def get_logs(lines: int = Query(100, description="Number of tail lines to return")):
    """
    Returns the most recent log details from the server as plain text.
    """
    try:
        import os
        log_file = "app.log"
        if not os.path.exists(log_file):
            return PlainTextResponse("Log file not found or empty.")
        
        with open(log_file, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
            tail_lines = all_lines[-lines:] if lines > 0 else all_lines
        
        return PlainTextResponse("".join(tail_lines))
    except Exception as e:
        logger.error(f"Error reading logs: {e}")
        raise HTTPException(status_code=500, detail="Failed to read logs.")


@router.post("/clear-logs")
async def clear_logs():
    """Clears the app.log file and outputs a clear sequence to the terminal."""
    try:
        import os
        log_file = "app.log"
        if os.path.exists(log_file):
            with open(log_file, "w", encoding="utf-8") as f:
                f.truncate(0)
        # Send clear screen ANSI code to the terminal
        print("\033c", end="")
        return {"status": "success", "message": "Logs and terminal cleared successfully."}
    except Exception as e:
        logger.error(f"Error clearing logs: {e}")
        raise HTTPException(status_code=500, detail="Failed to clear logs.")


@router.post("/clear-pycache")
async def clear_pycache():
    """Removes all __pycache__ directories and .pyc files recursively."""
    try:
        import os
        import shutil
        import glob
        
        project_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        
        # Recursively find and remove __pycache__ directories
        pycache_dirs = glob.glob(os.path.join(project_dir, '**', '__pycache__'), recursive=True)
        for d in pycache_dirs:
            try:
                shutil.rmtree(d)
            except Exception:
                pass
                
        # Recursively find and remove individual .pyc files
        pyc_files = glob.glob(os.path.join(project_dir, '**', '*.pyc'), recursive=True)
        for f in pyc_files:
            try:
                os.remove(f)
            except Exception:
                pass
                
        return {"status": "success", "message": f"Cleared {len(pycache_dirs)} __pycache__ dirs and {len(pyc_files)} .pyc files."}
    except Exception as e:
        logger.error(f"Error clearing pycache: {e}")
        raise HTTPException(status_code=500, detail="Failed to clear pycache.")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mock_with_ai(survey_id: str, last_login_dt: datetime, current_login_dt: datetime, last_login_label: str, client_id: int = 1, error: str = "") -> dict:
    from services.mock_data import get_mock_popup_data
    mock = get_mock_popup_data(survey_id, last_login_dt, current_login_dt)
    mock["client_id"] = client_id
    mock["last_login_date"] = last_login_dt.isoformat()
    mock["current_login_date"] = current_login_dt.isoformat()
    mock.pop("survey_id", None)
    mock.pop("last_login", None)
    mock.pop("data_as_of", None)
    ai = generate_ai_summary(
        mock["nps"], 
        mock["demographics"], 
        mock["critical_issues"], 
        mock.get("survey_comparison", []),
        last_login_label, 
        current_login_dt
    )
    mock["ai_summary"]    = ai.get("summary", "")
    mock["key_points"]    = ai.get("key_points", [])
    mock["top_alert_VOC"] = ai.get("critical_vocs", [])
    if error:
        mock["error"] = error
    return mock


def _aggregate_issues(records: list[dict]) -> list[dict]:
    """Group voc_alert rows by theme and produce ranked issue list."""
    from collections import defaultdict
    theme_map: dict[str, list] = defaultdict(list)
    for r in records:
        theme_map[r["theme"]].append(r)

    result = []
    for theme, items in theme_map.items():
        count          = len(items)
        critical_count = sum(1 for i in items if i.get("churn_intent"))
        max_severity   = max((i["severity_score"] for i in items), default=70)
        
        samples = []
        for i in items:
            if i.get("verbatim"):
                loc_data = {}
                for x in [1, 2, 3, 4]:
                    if i.get(f"f{x}_val"):
                        loc_data[i.get(f"f{x}_label", f"Level {x}")] = i[f"f{x}_val"]
                samples.append({
                    "verbatim": i.get("verbatim", "")[:200],
                    "loc_data": loc_data,
                    "severity_score": i.get("severity_score", 80),
                    "churn_intent": i.get("churn_intent", False)
                })

        severity = "critical" if max_severity >= 90 else ("high" if max_severity >= 80 else "medium")

        result.append({
            "issue":          theme,
            "count":          count,
            "severity":       severity,
            "severity_score": max_severity,
            "critical_count": critical_count,
            "sample":         samples[0]["verbatim"] if samples else "",
            "samples":        samples,
            "loc_data":       samples[0]["loc_data"] if samples else {}
        })

    result.sort(key=lambda x: (-x["severity_score"], -x["count"]))
    return result[:6]
