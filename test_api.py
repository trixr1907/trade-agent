import httpx, json

base = "http://localhost:8000"

# Health check
r = httpx.get(f"{base}/health")
print("HEALTH:", r.status_code, r.text)

# Root
r = httpx.get(f"{base}/")
print("ROOT:", r.status_code, r.text[:200])

# Scan (GET oder POST?)
try:
    r = httpx.get(f"{base}/scan", timeout=10)
    print("SCAN GET:", r.status_code, r.text[:300])
except Exception as e:
    print("SCAN GET ERROR:", e)

try:
    r = httpx.post(f"{base}/scan", timeout=10)
    print("SCAN POST:", r.status_code, r.text[:300])
except Exception as e:
    print("SCAN POST ERROR:", e)

# Proposals
try:
    r = httpx.get(f"{base}/proposals", timeout=10)
    print("PROPOSALS:", r.status_code, r.text[:300])
except Exception as e:
    print("PROPOSALS ERROR:", e)