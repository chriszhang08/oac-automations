"""Delete OpenHands conversations not updated in the last 14 days. No LLM.
Set DRY_RUN=1 to only list what would be deleted."""
import json, os, sys, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

MAX_AGE_DAYS = int(os.environ.get("MAX_AGE_DAYS", "14"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"
BASE = os.environ["AGENT_SERVER_URL"].rstrip("/")
KEY = os.environ.get("SESSION_API_KEY") or os.environ.get("OH_SESSION_API_KEYS_0", "")
CUTOFF = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)

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

def last_activity(c):
    ts = c.get("updated_at") or c.get("created_at")
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None

def old_conversations():
    page = None
    while True:
        q = "limit=100" + (f"&page_id={urllib.parse.quote(page)}" if page else "")
        d = call("GET", f"/api/conversations/search?{q}")
        for c in d.get("items", []):
            t = last_activity(c)
            if t and t < CUTOFF:
                yield c
        page = d.get("next_page_id")
        if not page: return

try:
    targets = list(old_conversations())  # collect first so deletion doesn't disturb paging
    deleted = skipped = failed = 0
    for c in targets:
        if c.get("execution_status") == "running":
            skipped += 1; print(f"skip running {c['id']}"); continue
        if DRY_RUN:
            print(f"would delete {c['id']} {c.get('updated_at')} {c.get('title')}"); continue
        try:
            call("DELETE", f"/api/conversations/{c['id']}")
            deleted += 1; print(f"deleted {c['id']} {c.get('title')}")
        except Exception as e:
            failed += 1; print(f"failed {c['id']}: {e}", file=sys.stderr)
    print(f"cutoff={CUTOFF.isoformat()} matched={len(targets)} deleted={deleted} skipped_running={skipped} failed={failed}")
    if failed: raise RuntimeError(f"{failed} deletions failed")
    fire_callback("COMPLETED")
except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    fire_callback("FAILED", str(e))
    sys.exit(1)
