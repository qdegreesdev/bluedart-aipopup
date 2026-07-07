import urllib.request, urllib.parse, json, ssl
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

data = urllib.parse.urlencode({
    'user_last_login_date': '2026-06-25T10:00:00.000Z', 
    'user_current_login_date': '2026-07-06T12:00:00.000Z'
}).encode('utf-8')
req = urllib.request.Request('http://localhost:8000/api/login-popup-summary', data=data)
try:
    with urllib.request.urlopen(req, context=ctx) as response:
        res = json.loads(response.read().decode())
        print("--- AI SUMMARY (HTML for Popup) ---")
        print(res.get("ai_summary"))
        print("\n--- Audio URL ---")
        print(res.get("ai_summary_audio_url"))
except Exception as e:
    print('API call failed:', e)
