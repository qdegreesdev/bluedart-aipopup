"""
AI Summary Service — Executive briefing for login popup.
Uses last_login → now comparison (no week logic).
"""
import json
from datetime import datetime
from openai import OpenAI
from loguru import logger
from config import settings

def _fmt_num(val):
    if isinstance(val, (int, float)):
        return int(val) if val == int(val) else val
    try:
        f = float(val)
        return int(f) if f.is_integer() else f
    except (ValueError, TypeError):
        return val


def generate_ai_summary(nps_data: dict, demographics: list, critical_issues: list, survey_comparison: list, last_login_label: str, current_login_dt: datetime, html_format: bool = False, prev_critical_issues: list = None) -> dict:
    hour = current_login_dt.hour
    if 5 <= hour < 12:
        greeting = "Good Morning"
    elif 12 <= hour < 17:
        greeting = "Good Afternoon"
    else:
        greeting = "Good Evening"

    if not settings.openai_api_key or settings.openai_api_key.startswith("sk-your"):
        logger.warning("No valid API key — using rule-based summary.")
        return _fallback_summary(nps_data, demographics, critical_issues, survey_comparison, last_login_label, current_login_dt, html_format)

    try:
        valid_demos = [d for d in demographics if d.get("type", "").lower() == "city" and d.get("responses", 0) > 10]
        # Modify the 'type' to say 'Branch' for clearer display if it is 'City'
        for d in valid_demos:
            if d.get("type") == "City":
                d["type"] = "Branch"
        
        top_gainers   = sorted([d for d in valid_demos if d.get("delta", 0) > 0], key=lambda x: -x["delta"])[:3]
        top_decliners = sorted([d for d in valid_demos if d.get("delta", 0) < 0], key=lambda x:  x["delta"])[:3]
        top_issues = []
        if critical_issues:
            for theme_group in critical_issues[:5]:
                theme_name = theme_group.get("issue", "Unknown")
                theme_count = theme_group.get("count", 0)
                sample_strings = []
                for s in theme_group.get("samples", [])[:2]:
                    loc_parts = []
                    for k, v in s.get("loc_data", {}).items():
                        if v and v != "Unknown":
                            loc_parts.append(f"{k}:{v}")
                    loc_str = ", ".join(loc_parts) or "Unknown Location"
                    churn_flag = " [CHURN RISK DETECTED BY DATABASE]" if s.get("churn_intent") else ""
                    sample_strings.append(f"Verbatim: \"{s.get('verbatim', '')}\" ({loc_str}){churn_flag}")
                
                samples_joined = " | ".join(sample_strings)
                top_issues.append(f"Theme: {theme_name} (Total Contribution: {theme_count} complaints) -> {samples_joined}")

        prev_top_issues = []
        if prev_critical_issues:
            for theme_group in prev_critical_issues[:5]:
                theme_name = theme_group.get("issue", "Unknown")
                theme_count = theme_group.get("count", 0)
                sample_strings = []
                for s in theme_group.get("samples", [])[:2]:
                    loc_parts = []
                    for k, v in s.get("loc_data", {}).items():
                        if v and v != "Unknown":
                            loc_parts.append(f"{k}:{v}")
                    loc_str = ", ".join(loc_parts) or "Unknown Location"
                    sample_strings.append(f"Verbatim: \"{s.get('verbatim', '')}\" ({loc_str})")
                
                samples_joined = " | ".join(sample_strings)
                prev_top_issues.append(f"Theme: {theme_name} (Total Contribution: {theme_count} complaints) -> {samples_joined}")

        top_survey_gainers = [s for s in survey_comparison if s.get("trend") == "up"][:2]
        top_survey_decliners = [s for s in survey_comparison if s.get("trend") == "down"][:2]
        delta         = _fmt_num(nps_data.get("delta", 0) or 0)
        trend_word    = "improved" if delta >= 0 else "declined"

        period_label = nps_data.get('current_period_label', f'since {last_login_label}')

        html_instruction = ""
        if html_format:
            html_instruction = """You MUST format the summary using clean HTML. 
If there is no change in NPS and no significant data, output a simple message like: <p>I have analyzed the changes since your last visit.</p><p>Overall NPS remains steady at <strong>[NPS]</strong>.</p>
Otherwise, provide a narrative summary wrapped in <p> tags instead of bullet points. Use <strong> tags for key metrics and area names.

CRITICAL STYLING & INTERACTIVITY RULES:
1. Make the summary visually stunning and interactive. Include a <style> block at the top with modern CSS (e.g., elegant box-shadows, rounded corners, soft alternating row colors, smooth hover transitions on table rows).
2. The comparison table MUST have a sleek, premium design. Add a hover effect on <tr> so they highlight when the user points at them. Use padded cells.
3. Use HTML <details open> and <summary> tags for the customer concerns section so it is expanded by default. Make the theme names clickable summaries that expand to show deeper insights or explanations.
4. Style key numbers or metrics (like NPS or points) as subtle "badges" using inline CSS (e.g., background-color, border-radius, padding).

CRITICAL LOGIC RULES FOR NARRATIVE:
1. Your tone and concluding sentence MUST match the actual data trend. If overall NPS has declined, the conclusion must warn about attrition risk or the need for action, rather than using blanket positive phrases like "recovery momentum". However, you MUST still highlight specific bright spots (like the top improving regions or touchpoints) to show the positive impact of what is working well.
2. If overall NPS is improving, conclude with an encouraging statement about accelerating the positive momentum.
3. Completely ignore touchpoints with 0 or very few responses when determining the highest change. A 100-point drop with 0 responses is just a lack of data, NOT a drastic decline.
4. Do not use unordered lists (<ul> or <li>) for the main narrative, but you can use them inside <details> tags.
5. Keep the summary short, punchy, and highly impactful. Avoid wordy, repetitive phrasing (e.g., instead of "In terms of customer concerns, several themes have emerged", just say "Key customer concerns include").
6. Format numbers cleanly: if a decimal value ends in .0, omit the decimal entirely (e.g., use 100 instead of 100.0). Never use hyphens between a number and the word "point" (e.g., use "100 points" instead of "100-points")."""

        # Compile survey comparison for context (ignoring statistically insignificant ones)
        survey_comp_str = chr(10).join([
            f"- {s['name']}: NPS {s['current_nps']} (was {s['previous_nps']}, Delta: {'+' if s['delta'] >= 0 else ''}{s['delta']} pts, Responses: {s['responses']})"
            for s in survey_comparison if s.get('responses', 0) >= 5
        ])

        context = f"""
You are an analyst for a CX platform (SurveyCXM).
Generate a concise executive summary for a login popup.
Be specific, data-driven, and action-oriented. Professional but conversational.
Do NOT include the date range or time period in the summary text.

DATA FOR PERIOD ({period_label}):
- NPS: {_fmt_num(nps_data.get('current', 0))} (was {_fmt_num(nps_data.get('previous', 0))}, {'+' if delta >= 0 else ''}{delta} pts, {trend_word})
- Responses this period: {nps_data.get('total_responses', 0)} (was {nps_data.get('prev_total_responses', 0)})
- Promoters: {_fmt_num(nps_data.get('promoters_pct', 0))}% | Passives: {_fmt_num(nps_data.get('passives_pct', 0))}% | Detractors: {_fmt_num(nps_data.get('detractors_pct', 0))}%

TOUCHPOINT / SURVEY PERFORMANCE:
{survey_comp_str or "No touchpoint comparison data available"}

TOP IMPROVING AREAS (in this period):
{chr(10).join([f"- {d['name']} ({d['type']}): NPS {_fmt_num(d['current_nps'])} ({'+' if float(d.get('delta',0)) >= 0 else ''}{_fmt_num(d['delta'])} pts)" for d in top_gainers]) or "None significant"}

TOP DECLINING AREAS (in this period):
{chr(10).join([f"- {d['name']} ({d['type']}): NPS {_fmt_num(d['current_nps'])} ({_fmt_num(d['delta'])} pts)" for d in top_decliners]) or "None significant"}

CRITICAL CUSTOMER ISSUES - PREVIOUS PERIOD (since before last login):
{chr(10).join([f"- {issue}" for issue in prev_top_issues]) or "No critical issues flagged in previous period"}

CRITICAL CUSTOMER ISSUES - CURRENT PERIOD (since last login):
{chr(10).join([f"- {issue}" for issue in top_issues]) or "No critical issues flagged"}

Provide JSON with:
- "summary": 2-3 short, highly concise paragraphs in an impressive executive briefing tone. Eliminate all fluff and filler words. You MUST start the summary exactly like this: "<p>I have analyzed the changes since your last visit.</p>". Follow it with narrative paragraphs briefly describing the overall NPS movement, the strongest area gain, and the worst decline. Also, analyze the touchpoint performance data to identify the highest change and its effect on customer experience. Next, succinctly describe the top customer concerns and provide a clean, modern HTML table comparing the top themes between the Previous Period (Before Last Login) and the Current Period (Since Last Login) along with their overall contribution (number of complaints). Ensure the table uses <table>, <thead>, <tbody>, <tr>, <th>, and <td> tags. Conclude with a short forward-looking statement. If any verbatims indicate CHURN RISK, explicitly mention it. Only include areas or concerns actually present in the data. CRITICAL INSTRUCTION: You MUST write the area names exactly as formatted in the list, completely preserving the bracketed tags "(Region)" and "(Location)" inside the name. If you omit "(Region)" or "(Location)", the output will be rejected. Correct Example: "WEST (Region) - BOM (Location) - ADR (Branch) is leading the turnaround". {html_instruction}
- "key_points": exactly 5 bullet strings, each under 15 words
- "critical_vocs": exactly 5 most critical verbatim quotes representing the CURRENT top themes (or all available if less than 5) with their location data, extracted from the CRITICAL CUSTOMER ISSUES - CURRENT PERIOD section. Ensure these VOCs directly align with the top themes identified in the summary. Format as an array of objects: {{"verbatim": "exact quote", "extra_info": "Label1:Value1, Label2:Value2"}}. Omit extra_info if location data is not provided.
"""

        response = None
        last_error = None
        
        try:
            client = OpenAI(api_key=settings.openai_api_key)
            model = settings.openai_model
            
            logger.info(f"Attempting summary generation using OpenAI ({model})...")
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": context}],
                temperature=0.3,
                max_tokens=2000,
                response_format={"type": "json_object"},
            )
        except Exception as e:
            logger.warning(f"OpenAI API call failed: {e}")
            last_error = e

        if not response:
            logger.error(f"OpenAI API failed. Last error: {last_error}")
            return _fallback_summary(nps_data, demographics, critical_issues, survey_comparison, last_login_label, current_login_dt, html_format)

        result = json.loads(response.choices[0].message.content)
        return {
            "summary":       result.get("summary", ""),
            "key_points":    result.get("key_points", []),
            "critical_vocs": result.get("critical_vocs", []),
        }

    except Exception as e:
        logger.error(f"AI summary error in generate_ai_summary outer block: {e}")
        return _fallback_summary(nps_data, demographics, critical_issues, survey_comparison, last_login_label, current_login_dt, html_format)


def _fallback_summary(nps_data: dict, demographics: list, critical_issues: list, survey_comparison: list, last_login_label: str, current_login_dt: datetime, html_format: bool = False) -> dict:
    delta      = _fmt_num(nps_data.get("delta", 0) or 0)
    current    = _fmt_num(nps_data.get("current", 0) or 0)
    trend_word = "improved" if delta >= 0 else "declined"
    valid_demos = [d for d in demographics if d.get("type", "").lower() == "city" and d.get("responses", 0) > 10]
    for d in valid_demos:
        if d.get("type") == "City":
            d["type"] = "Branch"
    
    top_gainer   = sorted([d for d in valid_demos if d.get("delta", 0) > 0], key=lambda x: -x["delta"])[0] if [d for d in valid_demos if d.get("delta", 0) > 0] else None
    top_decliner = sorted([d for d in valid_demos if d.get("delta", 0) < 0], key=lambda x: x["delta"])[0] if [d for d in valid_demos if d.get("delta", 0) < 0] else None
    top_issue    = critical_issues[0]["issue"] if critical_issues else "response quality"

    period_label = nps_data.get('current_period_label', f'since {last_login_label}')

    hour = current_login_dt.hour
    if 5 <= hour < 12:
        greeting = "Good Morning"
    elif 12 <= hour < 17:
        greeting = "Good Afternoon"
    else:
        greeting = "Good Evening"

    if html_format:
        if delta == 0 and not top_gainer and not top_decliner:
            summary = f"<p>I have analyzed the changes since your last visit.</p><p>Overall NPS remains steady at <strong>{current}</strong>.</p>"
        else:
            summary = f"<p>I have analyzed the changes since your last visit.</p>"
            summary += (
                f"<p>The {'good' if delta >= 0 else 'bad'} news is that overall NPS has {trend_word} by "
                f"<strong>{abs(delta)} points</strong>, bringing the current score to <strong>{current}</strong>.</p>"
            )
            
            # Find touchpoint with the highest absolute delta change
            top_tp_change = None
            if survey_comparison:
                sorted_tp = sorted(survey_comparison, key=lambda x: abs(x.get("delta", 0)), reverse=True)
                if sorted_tp:
                    top_tp_change = sorted_tp[0]

            if top_gainer:
                summary += (
                    f"<p><strong>{top_gainer['name']}</strong> is leading the turnaround, "
                    f"posting a strong <strong>{_fmt_num(abs(top_gainer['delta']))} points improvement</strong> and setting the benchmark for customer experience performance.</p>"
                )
            if top_decliner:
                summary += (
                    f"<p>However, <strong>{top_decliner['name']}</strong> requires attention, with NPS declining by <strong>{_fmt_num(abs(top_decliner['delta']))} points</strong>, making it the largest contributor to recent negative movement.</p>"
                )
                
            if top_tp_change:
                tp_direction = "improved" if top_tp_change.get("delta", 0) >= 0 else "declined"
                summary += (
                    f"<p>Among the touchpoints, <strong>{top_tp_change['name']}</strong> saw the most significant shift, "
                    f"which {tp_direction} by <strong>{abs(_fmt_num(top_tp_change['delta']))} points</strong>, now at a score of <strong>{_fmt_num(top_tp_change['current_nps'])}</strong>. This change directly affects overall customer journey impressions.</p>"
                )

            if top_issue and critical_issues:
                summary += f"<p>The top themes driving dissatisfaction remain <strong>{top_issue}</strong>.</p>"
            
            summary += "<p>If addressed promptly, the current recovery momentum can be accelerated while reducing attrition risk in vulnerable segments.</p>"
    else:
        summary = (
            f"Since your last login on {last_login_label}, your overall NPS has {trend_word} by "
            f"{abs(delta)} points, now at {current}. "
        )
        if top_gainer:
            summary += (
                f"{top_gainer['name']} ({top_gainer['type']}) is your strongest area "
                f"with a {top_gainer['delta']:+} pt gain. "
            )
        if top_decliner:
            summary += (
                f"{top_decliner['name']} shows a {top_decliner['delta']} pt decline and needs immediate attention. "
            )
            
        # Add touchpoint change in non-HTML fallback
        top_tp_change = None
        if survey_comparison:
            sorted_tp = sorted(survey_comparison, key=lambda x: abs(x.get("delta", 0)), reverse=True)
            if sorted_tp:
                top_tp_change = sorted_tp[0]
        if top_tp_change:
            summary += (
                f"The highest touchpoint variance is {top_tp_change['name']} showing a change of {top_tp_change['delta']} points. "
            )
            
        summary += f"Top customer concern: '{top_issue}' — addressing this will have maximum CX impact."

    key_points = [
        f"NPS {trend_word} {abs(delta)} pts to {current} since last login",
        f"Promoters {nps_data.get('promoters_pct', 0)}% | Detractors {nps_data.get('detractors_pct', 0)}%",
    ]
    if top_gainer:
        key_points.append(f"{top_gainer['name']} leads with {top_gainer['delta']:+} pt improvement")
    if top_decliner:
        key_points.append(f"{top_decliner['name']} declining — action needed")
    key_points.append(f"Top VOC issue: {top_issue}")

    critical_vocs = []
    if critical_issues and critical_issues[0].get("samples"):
        for s in critical_issues[0]["samples"][:5]:
            loc_parts = []
            for k, v in s.get("loc_data", {}).items():
                if v and v != "Unknown":
                    loc_parts.append(f"{k}:{v}")
            loc_str = ", ".join(loc_parts)
            voc = {
                "verbatim": s.get("verbatim", ""),
            }
            if loc_str:
                voc["extra_info"] = loc_str
            critical_vocs.append(voc)

    return {"summary": summary, "key_points": key_points[:5], "critical_vocs": critical_vocs}


def answer_user_question(nps_data: dict, demographics: list, critical_issues: list, question: str, survey_comparison: list = None) -> str:
    """Answers a specific user question using the popup data context with full drill-down detail."""
    if not settings.openai_api_key or settings.openai_api_key.startswith("sk-your"):
        return "System error: No valid OpenAI key configured."

    try:

        delta      = nps_data.get("delta", 0) or 0
        trend_word = "improved" if delta >= 0 else "declined"

        # Full demographic detail with previous NPS for drill-down (capped to top 20 to save tokens)
        sorted_demos = sorted(demographics, key=lambda x: x.get('responses', 0), reverse=True)[:20]
        demo_str = chr(10).join([
            f"- {d['name']} ({d['type']}): NPS {d['current_nps']} (was {d.get('previous_nps','?')}, Delta: {d['delta']:+} pts, Responses: {d['responses']}, Trend: {d.get('trend','?')})"
            for d in sorted_demos
        ])

        # Touchpoint detail
        tp_str = ""
        if survey_comparison:
            tp_str = chr(10).join([
                f"- {s['name']}: NPS {s['current_nps']} (was {s['previous_nps']}, Delta: {'+' if s['delta'] >= 0 else ''}{s['delta']} pts, Responses: {s['responses']})"
                for s in survey_comparison
            ])

        # Issues with verbatim samples and churn counts (capped to top 4 to save tokens)
        issues_str = chr(10).join([
            f"- {i['issue']} | Count: {i['count']} | Severity: {i['severity']} | Churn/Critical Signals: {i.get('critical_count', 0)} | Samples: \"{' | '.join([s.get('verbatim', '') for s in i.get('samples', [])[:2]])}\""
            for i in critical_issues[:4]
        ])

        # Pre-sort for quick AI reference
        valid_demos = [d for d in demographics if d.get("type", "").lower() == "region" and d.get("responses", 0) > 10]
        top_gainers   = sorted([d for d in valid_demos if d.get("delta", 0) > 0], key=lambda x: -x["delta"])[:3]
        top_decliners = sorted([d for d in valid_demos if d.get("delta", 0) < 0], key=lambda x:  x["delta"])[:3]
        churn_signals = [i for i in critical_issues if i.get("critical_count", 0) > 0]

        system_prompt = f"""
You are an intelligent AI analyst for a CX intelligence platform (SurveyCXM).
You have access to the user's REAL-TIME data from the database for their login window.
Answer in a detailed drill-down style — like a smart data analyst explaining findings.
You MUST dynamically format your entire response using standard HTML tags (e.g., <p>, <br>, <strong>, <ul>, <ol>, <li>). Do NOT use Markdown formatting (no asterisks **, no hyphens -).
Provide clear, concise, and complete answers directly addressing the user's question. 
When making comparisons, briefly state your methodology, then highlight ONLY the most relevant data points (e.g., the top 1 or 2 regions or touchpoints) rather than listing every single data point.
Specifically, if the user asks about touchpoint changes or effects, identify which touchpoint has the highest change (drop or improvement) and describe its impact.
Keep your response focused and readable, avoiding unnecessary length while ensuring the conclusion is well-explained.
Never say "I don't have data" if the context has relevant info.
For general/industry questions, combine context data with your broader knowledge.

PERIOD: Since the user's last login

NPS OVERVIEW:
- Current NPS    : {nps_data.get('current', 0)} (previous: {nps_data.get('previous', 0)}, change: {'+' if delta >= 0 else ''}{delta} pts — {trend_word})
- Total Responses: {nps_data.get('total_responses', 0)} (previous period: {nps_data.get('prev_total_responses', 0)})
- Promoters: {nps_data.get('promoters_pct', 0)}% | Passives: {nps_data.get('passives_pct', 0)}% | Detractors: {nps_data.get('detractors_pct', 0)}%

TOUCHPOINT PERFORMANCE:
{tp_str or "No touchpoint data available."}

TOP IMPROVING AREAS:
{chr(10).join([f"- {d['name']} ({d['type']}): NPS {d['current_nps']} ({d['delta']:+} pts)" for d in top_gainers]) or "None"}

TOP DECLINING AREAS:
{chr(10).join([f"- {d['name']} ({d['type']}): NPS {d['current_nps']} ({d['delta']:+} pts)" for d in top_decliners]) or "None"}

ALL DEMOGRAPHICS (Region / State / City — full list):
{demo_str or "No demographic data available."}

CRITICAL CUSTOMER ISSUES (Voice of Customer with verbatim samples):
{issues_str or "No critical issues flagged."}

CHURN INTENT SIGNALS:
{chr(10).join([f"- {i['issue']}: {i['critical_count']} churn signals" for i in churn_signals]) or "No explicit churn signals in current period."}
"""

        response = None
        last_error = None
        
        try:
            client = OpenAI(api_key=settings.openai_api_key)
            model = settings.openai_model
            
            logger.info(f"Attempting QA response using OpenAI ({model})...")
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": question}
                ],
                temperature=0.3,
                max_tokens=2000,
            )
        except Exception as e:
            logger.warning(f"OpenAI QA API call failed: {e}")
            last_error = e

        if not response:
            logger.error(f"OpenAI QA failed. Last error: {last_error}")
            raise RuntimeError(f"OpenAI QA failed. Last error: {last_error}")

        return response.choices[0].message.content.strip()

    except Exception as e:
        logger.error(f"Ask AI error in outer block: {e}")
        delta = nps_data.get("delta", 0) or 0
        current = nps_data.get("current", 0) or 0
        trend_word = "improved" if delta >= 0 else "declined"
        top_issue = critical_issues[0]["issue"] if critical_issues else "response quality"
        top_gainer = next((d for d in demographics if d.get("trend") == "up"), None)
        top_decliner = next((d for d in demographics if d.get("trend") == "down"), None)
        
        # Check touchpoints
        top_tp_change = None
        if survey_comparison:
            sorted_tp = sorted(survey_comparison, key=lambda x: abs(x.get("delta", 0)), reverse=True)
            if sorted_tp:
                top_tp_change = sorted_tp[0]
        
        ans = f"<p><em>Note: Live AI analysis is temporarily experiencing high traffic. Here is an instant overview of your data:</em></p>"
        ans += f"<p>The overall NPS has <strong>{trend_word}</strong> by <strong>{abs(delta)} points</strong> and is currently at <strong>{current}</strong>.</p>"
        if top_issue:
            ans += f"<p>The most critical concern is <strong>{top_issue}</strong>.</p>"
        if top_tp_change:
            tp_dir = "increased" if top_tp_change.get("delta", 0) >= 0 else "decreased"
            ans += f"<p>The touchpoint with the highest change is <strong>{top_tp_change['name']}</strong>, which has {tp_dir} by <strong>{abs(top_tp_change['delta'])} points</strong> to a score of <strong>{top_tp_change['current_nps']}</strong>. This indicates that customer sentiment for this specific step of the journey has shifted considerably.</p>"
        if top_gainer:
            ans += f"<p>The strongest demographic improvement was in <strong>{top_gainer['name']}</strong> (gained {_fmt_num(abs(top_gainer['delta']))} points).</p>"
        if top_decliner:
            ans += f"<p>The largest demographic drop was in <strong>{top_decliner['name']}</strong> (declined {_fmt_num(abs(top_decliner['delta']))} points).</p>"
        return ans
