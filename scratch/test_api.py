import urllib.request, urllib.parse, json, ssl
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

# Clear Cache
req_clear = urllib.request.Request('http://localhost:8000/api/clear-cache', method='POST')
try:
    urllib.request.urlopen(req_clear, context=ctx)
    print('Cache cleared')
except Exception as e:
    print('Cache clear failed:', e)

# Call API
data = urllib.parse.urlencode({
    'user_last_login_date': '2026-06-25T10:00:00Z', 
    'user_current_login_date': '2026-07-06T12:00:00Z'
}).encode('utf-8')
req = urllib.request.Request('http://localhost:8000/api/login-popup-summary', data=data)
try:
    with urllib.request.urlopen(req, context=ctx) as response:
        res = json.loads(response.read().decode())
        print(json.dumps(res, indent=2))
except Exception as e:
    print('API call failed:', e)
