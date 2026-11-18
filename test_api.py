import httpx, json
from smoke_login import authed_client

base = "http://localhost:8000"


def main():
    with authed_client(base) as client:
        # Health check
        r = client.get(f"{base}/health")
        print("HEALTH:", r.status_code, r.text)

        # Root
        r = client.get(f"{base}/")
        print("ROOT:", r.status_code, r.text[:200])

        # Scan (GET oder POST?)
        try:
            r = client.get(f"{base}/scan", timeout=10)
            print("SCAN GET:", r.status_code, r.text[:300])
        except Exception as e:
            print("SCAN GET ERROR:", e)

        try:
            r = client.post(f"{base}/scan", timeout=10)
            print("SCAN POST:", r.status_code, r.text[:300])
        except Exception as e:
            print("SCAN POST ERROR:", e)

        # Proposals
        try:
            r = client.get(f"{base}/proposals", timeout=10)
            print("PROPOSALS:", r.status_code, r.text[:300])
        except Exception as e:
            print("PROPOSALS ERROR:", e)


if __name__ == "__main__":
    main()
