#!/usr/bin/env python3
"""Audit health check: fix `openspec validate` failures on open audit PRs.

Deterministic poller; the only LLM work is the fix conversation, started on the
claude-haiku agent profile when a PR has a failing `openspec validate` step.
No such failure means exit 0. Standard library only.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

PLUGIN_ROOT = Path(os.environ.get("AUTOMATION_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)
DEFAULTS = {
    "store_repo": "chriszhang08/odysseus-planner",
    "title_prefix": "[openspec-audit]",
    "validate_marker": "openspec validate",
    "github_secret": "GITHUB_TOKEN",
}
# Same tag the audit automation uses; the sweeper deletes finished conversations carrying it.
CONVERSATION_TAGS = {"temporary": "true"}
POLL_INTERVAL_SECONDS = 10
MAX_WAIT_SECONDS = 1500
TERMINAL_STATUSES = ("finished", "error", "stuck")
# The claude-haiku profile wraps the Claude Code ACP agent, which needs its own
# credential to start a turn at all.
ACP_AUTH_SECRET = "CLAUDE_CODE_OAUTH_TOKEN"
FAILED = ("failure", "timed_out", "cancelled")


def _key():
    return os.environ.get("SESSION_API_KEY") or os.environ.get("OH_SESSION_API_KEYS_0", "")


def load_config():
    cfg = dict(DEFAULTS)
    path = os.environ.get("AUTOMATION_CONFIG_PATH")
    if path and Path(path).exists():
        cfg.update(json.loads(Path(path).read_text()))
    return cfg


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
        }), timeout=30)
    except Exception as e:  # noqa: BLE001
        print(f"Callback error (non-fatal): {e}")


def _observability_headers():
    try:
        from openhands.sdk.automation import automation_observability_headers

        return dict(automation_observability_headers(span_name="automation.conversation"))
    except Exception:  # noqa: BLE001 - SDK is optional here
        return {}


class GitHub:
    def __init__(self, token):
        self.token = token

    def get(self, path, params=None):
        url = "https://api.github.com" + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "openspec-audit-healthcheck",
        })
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def open_audit_prs(self, repo, prefix):
        out = []
        for page in range(1, 6):
            items = self.get(f"/repos/{repo}/pulls", {"state": "open", "per_page": 100, "page": page})
            out += [p for p in items if p["title"].startswith(prefix)]
            if len(items) < 100:
                break
        return out

    def validate_failure(self, repo, sha, marker):
        """URL of a run at `sha` where a step naming `marker` failed, else None."""
        runs = self.get(f"/repos/{repo}/actions/runs", {"head_sha": sha, "per_page": 50})
        for run in runs.get("workflow_runs", []):
            if run.get("conclusion") not in FAILED:
                continue
            jobs = self.get(f"/repos/{repo}/actions/runs/{run['id']}/jobs", {"per_page": 100})
            for job in jobs.get("jobs", []):
                for step in job.get("steps", []):
                    if marker in step["name"] and step.get("conclusion") in FAILED:
                        return run["html_url"]
        return None


class AgentServer:
    def __init__(self, url, key):
        self.url, self.key = url.rstrip("/"), key

    def req(self, method, path, body=None):
        headers = {"X-Session-API-Key": self.key, "Content-Type": "application/json"}
        headers.update(_observability_headers())
        r = urllib.request.Request(self.url + path, method=method, headers=headers,
                                   data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(r, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None

    def profile_id(self):
        pid = os.environ.get("AUTOMATION_AGENT_PROFILE_ID")
        if pid:
            return pid
        name = os.environ.get("AUTOMATION_AGENT_PROFILE_NAME", "claude-haiku")
        for p in (self.req("GET", "/api/agent-profiles") or {}).get("profiles", []):
            if p.get("name") == name:
                return p["id"]
        raise RuntimeError(f"agent profile {name!r} not found")

    def lookup(self, name):
        return {"kind": "LookupSecret",
                "url": f"{self.url}/api/settings/secrets/{urllib.parse.quote(name)}",
                "headers": {"X-Session-API-Key": self.key}}

    def start(self, cid, profile_id, prompt, github_secret):
        try:
            self.req("GET", f"/api/conversations/{cid}")
            return "exists"
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
        workdir = os.path.join(os.environ.get("WORKSPACE_BASE") or str(Path.home() / "workspace"),
                               "openspec-audit-healthcheck", cid)
        os.makedirs(workdir, exist_ok=True)
        self.req("POST", "/api/conversations", {
            "conversation_id": cid,
            "tags": dict(CONVERSATION_TAGS),
            "agent_profile_id": profile_id,
            "workspace": {"kind": "LocalWorkspace", "working_dir": workdir},
            "secrets": {ACP_AUTH_SECRET: self.lookup(ACP_AUTH_SECRET),
                        "GITHUB_TOKEN": self.lookup(github_secret)},
            "initial_message": {"role": "user", "content": [{"type": "text", "text": prompt}], "run": True},
        })
        return "started"

    def wait(self, cid):
        deadline = time.time() + MAX_WAIT_SECONDS
        status = None
        while time.time() < deadline:
            time.sleep(POLL_INTERVAL_SECONDS)
            status = (self.req("GET", f"/api/conversations/{cid}") or {}).get("execution_status")
            if status in TERMINAL_STATUSES:
                break
        return status


def render(template, values):
    for k, v in values.items():
        template = template.replace("{{" + k + "}}", str(v))
    return template


def main():
    try:
        cfg = load_config()
        repo = cfg["store_repo"]
        gh = GitHub(os.environ.get("GITHUB_TOKEN") or get_secret(cfg["github_secret"]))
        failing = []
        for pr in gh.open_audit_prs(repo, cfg["title_prefix"]):
            sha = pr["head"]["sha"]
            if (pr["head"].get("repo") or {}).get("full_name") != repo:
                print(f"#{pr['number']}: fork branch, skipped")
                continue
            url = gh.validate_failure(repo, sha, cfg["validate_marker"])
            if url:
                failing.append((pr, sha, url))
            else:
                print(f"#{pr['number']}: no openspec validate failure")

        if not failing:
            print("healthy: no openspec validate failures on open audit PRs")
            fire_callback("COMPLETED")
            return

        agent = AgentServer(os.environ["AGENT_SERVER_URL"], _key())
        profile = agent.profile_id()
        template = (PLUGIN_ROOT / "scripts" / "audit_fix_task.md").read_text()
        started, errors = [], []
        for pr, sha, url in failing:
            # One conversation per (PR, head commit): a rerun on the same commit does not repeat it.
            cid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"openspec-audit-fix/{repo}/{pr['number']}/{sha}"))
            prompt = render(template, {"STORE_REPO": repo, "PR_NUMBER": pr["number"],
                                       "HEAD_REF": pr["head"]["ref"], "HEAD_SHA": sha, "RUN_URL": url})
            state = agent.start(cid, profile, prompt, cfg["github_secret"])
            print(f"#{pr['number']}: validate failing, conversation {cid} {state}")
            if state == "started":
                started.append((pr["number"], cid))
        for number, cid in started:
            status = agent.wait(cid)
            print(f"#{number}: conversation {cid} ended {status!r}")
            if status != "finished":
                errors.append(f"#{number}: status={status!r}")
        if errors:
            raise RuntimeError("fix conversation did not finish: " + "; ".join(errors))
        fire_callback("COMPLETED")
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}", file=sys.stderr)
        fire_callback("FAILED", str(e))
        sys.exit(1)


def get_secret(name):
    base = os.environ["AGENT_SERVER_URL"].rstrip("/")
    req = urllib.request.Request(f"{base}/api/settings/secrets/{name}", headers={"X-Session-API-Key": _key()})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode().strip()


if __name__ == "__main__":
    main()
