"""Delete all finished conversations tagged odysseuschange=planning-store-audit. No LLM."""
import json, os, sys, urllib.parse, urllib.request

TAG_KEY, TAG_VALUE = "odysseuschange", "planning-store-audit"
BASE = os.environ["AGENT_SERVER_URL"].rstrip("/")
KEY = os.environ.get("SESSION_API_KEY") or os.environ.get("OH_SESSION_API_KEYS_0", "")

def call(method, path):
    req = urllib.request.Request(BASE + path, method=method, headers={"X-Session-API-Key": KEY})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode()
        return json.loads(body) if body else None

def fire_callback(status="COMPLETED", error=None):
    url = os.environ.get("AUTOMATION_CALLBACK_URL", "")
    if not url: return
    body = {"status": status, "run_id": os.environ.get("AUTOMATION_RUN_ID", "")}
    if error: body["error"] = error
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(body).encode(), headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ.get('AUTOMATION_CALLBACK_API_KEY', '')}",
        }))
    except Exception as e: print(f"Callback error: {e}")

def matching():
    page = None
    while True:
        q = "limit=100" + (f"&page_id={urllib.parse.quote(page)}" if page else "")
        d = call("GET", f"/api/conversations/search?{q}")
        for c in d.get("items", []):
            if (c.get("tags") or {}).get(TAG_KEY) == TAG_VALUE:
                yield c
        page = d.get("next_page_id")
        if not page: return

try:
    targets = list(matching())  # collect first so deletion doesn't disturb paging
    deleted = skipped = failed = 0
    for c in targets:
        if c.get("execution_status") == "running":
            skipped += 1; print(f"skip running {c['id']}"); continue
        try:
            call("DELETE", f"/api/conversations/{c['id']}")
            deleted += 1; print(f"deleted {c['id']} {c.get('title')}")
        except Exception as e:
            failed += 1; print(f"failed {c['id']}: {e}", file=sys.stderr)
    print(f"deleted={deleted} skipped_running={skipped} failed={failed}")
    if failed: raise RuntimeError(f"{failed} deletions failed")
    fire_callback("COMPLETED")
except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    fire_callback("FAILED", str(e))
    sys.exit(1)
