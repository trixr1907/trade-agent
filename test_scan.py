import httpx, json

base = "http://localhost:8000"

print("=== SCAN TEST ===")
try:
    r = httpx.post(
        f"{base}/scan",
        json={"symbols": ["BTC/USDT", "ETH/USDT"], "timeframe": "1h", "limit": 120},
        timeout=30.0,
    )
    print("Status:", r.status_code)
    data = r.json()
    print("Mode:", data.get("mode"))
    print("Error:", data.get("error"))
    print("Signals:", len(data.get("signals", [])))
    for s in data.get("signals", []):
        print("  ->", json.dumps(s, indent=4))
    print("Proposals:", len(data.get("proposals", [])))
    for p in data.get("proposals", []):
        print("  ->", json.dumps(p, indent=4))
except Exception as e:
    print("FEHLER:", e)