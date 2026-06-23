from fastapi.testclient import TestClient
import json
from main import app

client = TestClient(app)

print("--- Testing /api/login-popup-summary ---")
response = client.post(
    "/api/login-popup-summary",
    data={
        "user_last_login_date": "01/06/2026",
        "user_current_login_date": "15/06/2026"
    }
)

if response.status_code == 200:
    data = response.json()
    print("[OK] SUCCESS: /api/login-popup-summary")
    print(f"   Greeting: {data.get('greeting')}")
    print(f"   Is Data Found: {data.get('is_data_found')}")
    with open("test_output.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
else:
    print(f"[FAIL] FAILED /api/login-popup-summary with status {response.status_code}")
    print(response.text)

print("\n--- Testing /api/ask-ai ---")
response_ai = client.post(
    "/api/ask-ai",
    data={
        "user_last_login_date": "01/06/2026",
        "user_current_login_date": "15/06/2026",
        "question": "What is the biggest problem?"
    }
)

if response_ai.status_code == 200:
    data_ai = response_ai.json()
    print("[OK] SUCCESS: /api/ask-ai")
    print(f"   Answer: {data_ai.get('answer')[:120]}...")
else:
    print(f"[FAIL] FAILED /api/ask-ai with status {response_ai.status_code}")
    print(response_ai.text)

print("\n--- Testing /api/health ---")
response_health = client.get("/api/health")
if response_health.status_code == 200:
    print(f"[OK] SUCCESS: /api/health -> {response_health.json()}")
else:
    print(f"[FAIL] FAILED /api/health with status {response_health.status_code}")
