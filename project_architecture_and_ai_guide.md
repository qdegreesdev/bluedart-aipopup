# BlueDart Popup API: Comprehensive Architecture & AI Guide

This document provides a broad and deep explanation of your entire project, how the components interact, and exactly how the Artificial Intelligence (AI) reads, processes, and responds to data.

---

## 1. High-Level Architecture Overview

Your project is a backend API built using **FastAPI** (Python) . Its primary goal is to serve an intelligent popup to users when they log in to the SurveyCXM platform. Instead of showing them raw dashboards, it provides a curated, AI-generated executive briefing of what happened while they were away.

The system consists of 4 main layers:
1. **API Routing Layer (`routes/popup.py`)**: The front door. It receives the user's login dates and handles sending the final response back to the frontend.
2. **Database Layer (`database.py`)**: The data engine. It connects to MySQL and crunches thousands of survey responses to generate high-level metrics (NPS scores, deltas, verbatims).
3. **AI Layer (`services/ai_service.py`)**: The brain. It takes the hard numbers from the database and turns them into a human-readable, professional narrative using OpenAI's Large Language Models (LLMs).
4. **Text-to-Speech (TTS) Layer (`services/tts_service.py`)**: The voice. It converts the AI's text into an audio file so the user can listen to their briefing.

---

## 2. Step-by-Step Data Flow

When a user triggers the popup (via `/api/login-popup-summary`), here is exactly what happens in order:

### Step A: The Request
The frontend sends the user's `last_login_date` and the `current_login_date`. 

### Step B: The Database Crunch
The `database.py` file does heavy lifting *before* the AI is ever involved. AI is expensive and sometimes slow, so we don't send raw database tables to it. Instead, Python does the math.
- It looks at the time since the user last logged in (Current Period).
- It looks at an equal amount of time *before* they logged in (Previous Period).
- It calculates the **NPS (Net Promoter Score)** for both periods and finds the difference (`delta`).
- It finds the **Top Gaining** branch/region and the **Top Declining** branch/region.
- It finds the **Highest Priority Customer Complaints** (Voice of Customer) by filtering for scores <= 6 marked as critical.

### Step C: The Prompt Construction
Now we have raw numbers (e.g., "NPS is 45, up 5 points. Top gainer is Mumbai. Top issue is late delivery"). The system formats these numbers into a specific text structure called a **Prompt**.

---

## 3. Deep Dive: How the AI Works

The AI is managed in `services/ai_service.py`. It uses OpenAI (like ChatGPT) under the hood.

### How the AI gets the data
The AI does not connect to the database directly. Instead, your Python code injects the computed metrics into a massive string (the "Prompt"). 

Here is what the prompt looks like behind the scenes:
```text
You are an analyst for a CX platform. Generate an executive summary.
DATA FOR PERIOD:
- NPS: 45 (was 40, +5 pts, improved)
- Responses: 1200
- Promoters: 50% | Passives: 30% | Detractors: 20%

TOP IMPROVING AREAS:
- Mumbai (Branch): NPS 60 (+15 pts)

TOP DECLINING AREAS:
- Delhi (Branch): NPS 20 (-10 pts)

CRITICAL CUSTOMER ISSUES:
- Verbatim: "My package was lost and customer service was rude." (Churn Risk Detected)
```

### How the AI Generates and Responds
1. **The Request**: `ai_service.py` sends this text block to OpenAI's API (`client.chat.completions.create`).
2. **The Constraints**: We tell the AI *exactly* how to format its response. We ask it to return a **JSON object** with three specific fields:
   - `summary`: An HTML formatted narrative.
   - `key_points`: 5 short bullet points.
   - `critical_vocs`: The top verbatim quotes formatted cleanly.
3. **The Rules**: The prompt contains strict rules: 
   - *"Your tone MUST match the actual data trend."*
   - *"If overall NPS has declined, the conclusion must warn about attrition risk."*
   - *"Do NOT use unordered lists in the summary."*
4. **The Output**: The AI reads the data, follows the rules, and streams back a response that sounds like a professional business analyst.

### What happens if the AI fails? (The Fallback)
If the OpenAI API is down, or if your API key expires, the code has a safety net. The `_fallback_summary` function in `ai_service.py` will take the exact same data and use basic Python string concatenation to build a sentence. 
- *Example Fallback*: `"Since your last login, your overall NPS has improved by 5 points. Mumbai is your strongest area..."*
This ensures the frontend never breaks, even if the AI is unreachable.

---

## 4. The "Ask AI" Feature (`/api/ask-ai`)

Your project also has a feature where the user can type a custom question (e.g., *"Why did Delhi's score drop?"*).

**How it works:**
1. The system fetches the exact same context (NPS metrics, regions, verbatims) from the database for their login window.
2. It sends the massive data context + the user's explicit question to OpenAI.
3. The prompt tells the AI: *"You are an intelligent AI analyst... Answer in a detailed drill-down style... Use ONLY the provided data."*
4. The AI reads the context, finds the data relevant to the user's question, and generates a custom, real-time HTML response answering the query.

## 5. Summary of the Magic

The "intelligence" of your project comes from the seamless handoff between **FastAPI**, **MySQL**, and **OpenAI**. 
- **MySQL** provides the absolute truth (hard data).
- **FastAPI** handles the logic and time windows.
- **OpenAI** provides the human touch (narrative and analysis).

Because the AI is strictly fenced in by the prompt (it is only given specific numbers and specific rules), it will not hallucinate or make up data. It will only ever summarize the exact database metrics passed to it.




Based on the backend code in database.py, here is how NPS is calculated, what columns are used, and the likely source of the "85%" metric:

1. How NPS is Calculated
The code calculates the NPS (Net Promoter Score) using standard logic:

python
promoters  = sum(1 for s in scores if s >= 9)    # Scores 9 or 10
passives   = sum(1 for s in scores if 7 <= s <= 8) # Scores 7 or 8
detractors = sum(1 for s in scores if s <= 6)    # Scores 0 through 6
nps = ((promoters - detractors) / total_responses) * 100
It subtracts the percentage of Detractors from the percentage of Promoters to get the final score, which ranges from -100 to +100.

2. What Columns are Used
The exact column used for the NPS score is dynamic because each survey can have different database table structures. The system follows this priority to find the column:

It first checks the survey_questions table to find the specific column identifier (called a "slug") where the question type is marked as NPS or nps.
It then looks at the survey's response table (survey_dynamic_id_{survey_id}) and tries to find a column matching that slug.
If it doesn't find it, it falls back to looking for common column names like NPS_SCORE, nps_score, or nps.
3. The "85%" Metric
There is no "85% NPS" defined in the core calculation logic. However, I found where 85 is used in the database code. It is used as a severity score for Customer Voice Alerts, not the NPS calculation itself.

When the system pulls critical negative feedback (get_customer_voice_data), it maps different priority levels to a severity score from 0-100:

critical: 95
urgent: 90
high: 85
medium: 70
low: 50
So if you are seeing "85" show up in your AI summaries or data, it indicates that a negative customer response (a Detractor) was flagged as a "High" priority issue.