#!/usr/bin/env python3
"""Every 5 hours: ask the claude-haiku agent profile what model it is.

Deterministic poller around a single conversation turn - no LLM reasoning in
the script itself. Tags the conversation `temporary=true` so the
delete-planning-store-audit-conversations sweeper cleans it up afterwards.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

PROMPT = "what model are you"
CONVERSATION_TAGS = {"temporary": "true"}
POLL_INTERVAL_SECONDS = 5
MAX_WAIT_SECONDS = 180
TERMINAL_STATUSES = ("finished", "error", "stuck")

BASE = os.environ["AGENT_SERVER_URL"].rstrip("/")
KEY = os.environ.get("SESSION_API_KEY") or os.environ.get("OH_SESSION_API_KEYS_0", "")


def _observability_headers():
    try:
        from openhands.sdk.automation import automation_observability_headers

        return dict(automation_observability_headers(span_name="automation.conversation"))
    except Exception:  # noqa: BLE001 - SDK is optional here
        return {}


def call(method, path, body=None):
    headers = {"X-Session-API-Key": KEY, "Content-Type": "application/json"}
    headers.update(_observability_headers())
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def fire_callback(status="COMPLETED", error=None):
    url = os.environ.get("AUTOMATION_CALLBACK_URL", "")
    if not url:
        return
    body = {"status": status, "run_id": os.environ.get("AUTOMATION_RUN_ID", "")}
    if error:
        body["error"] = error
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(body).encode(), headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ.get('AUTOMATION_CALLBACK_API_KEY', '')}",
        }))
    except Exception as e:  # noqa: BLE001
        print(f"Callback error: {e}")


def resolve_profile_id():
    """The automation service injects the resolved profile id for us; fall back
    to a by-name lookup if that env var is ever absent."""
    pid = os.environ.get("AUTOMATION_AGENT_PROFILE_ID")
    if pid:
        return pid
    name = os.environ.get("AUTOMATION_AGENT_PROFILE_NAME", "claude-haiku")
    listing = call("GET", "/api/agent-profiles") or {}
    for p in listing.get("profiles", []):
        if p.get("name") == name:
            return p["id"]
    raise RuntimeError(f"agent profile {name!r} not found")


def main():
    try:
        profile_id = resolve_profile_id()
        conversation_id = str(uuid.uuid4())
        workdir = os.path.join(
            os.environ.get("WORKSPACE_BASE") or str(Path.home() / "workspace"),
            "claude-haiku-healthcheck",
            conversation_id,
        )
        os.makedirs(workdir, exist_ok=True)

        call(
            "POST",
            "/api/conversations",
            {
                "conversation_id": conversation_id,
                "tags": dict(CONVERSATION_TAGS),
                "agent_profile_id": profile_id,
                "workspace": {"kind": "LocalWorkspace", "working_dir": workdir},
                "secrets": {},
                "initial_message": {
                    "role": "user",
                    "content": [{"type": "text", "text": PROMPT}],
                    "run": True,
                },
            },
        )
        print(f"started conversation {conversation_id} on profile {profile_id}")

        status = None
        deadline = time.time() + MAX_WAIT_SECONDS
        while time.time() < deadline:
            time.sleep(POLL_INTERVAL_SECONDS)
            info = call("GET", f"/api/conversations/{conversation_id}") or {}
            status = info.get("execution_status")
            if status in TERMINAL_STATUSES:
                break

        if status != "finished":
            raise RuntimeError(
                f"conversation {conversation_id} ended with status={status!r} "
                f"(polled for {MAX_WAIT_SECONDS}s)"
            )

        try:
            reply = call("GET", f"/api/conversations/{conversation_id}/agent_final_response")
            print(f"response: {json.dumps(reply)[:500]}")
        except Exception as e:  # noqa: BLE001 - logging only, not fatal to the healthcheck
            print(f"could not fetch final response (non-fatal): {e}")

        print(f"healthcheck OK: claude-haiku profile responded (conversation {conversation_id})")
        fire_callback("COMPLETED")
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}", file=sys.stderr)
        fire_callback("FAILED", str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
