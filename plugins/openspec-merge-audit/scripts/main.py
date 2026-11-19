#!/usr/bin/env python3
"""Daily: merged PRs -> one OpenSpec archive-and-audit conversation per store.

Deterministic poller. It uses no LLM itself; it only starts a conversation when a
store has merges to process. Standard library only.

Branch convention (shared with the conversation task in audit_task.md):

    openspec-audit/<YYYYMMDDTHHMMSSZ>   UTC end of the poll window

A new audit PR is stacked on the tip of the open audit PRs in the store, so the
script resolves its base deterministically and hands it to the agent.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

PLUGIN_ROOT = Path(
    os.environ.get("AUTOMATION_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent
)
BRANCH_PREFIX = "openspec-audit/"
BRANCH_RE = re.compile(r"^openspec-audit/(\d{8}T\d{6}Z)$")
HEAD_RE = re.compile(r"^odysseus/pr(\d+)-([A-Za-z0-9][A-Za-z0-9._-]*)$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
# The "Delete planning-store-audit conversations" automation deletes every finished
# conversation carrying this tag. Keep it in step with that automation.
CONVERSATION_TAGS = {"temporary": "true"}
HANDLED_LIMIT = 500
MAX_RETRIES = 2
SEARCH_MAX_PAGES = 3

DEFAULTS = {
    "window_minutes": 60,
    "overlap_minutes": 10,
    "stack_warn_depth": 5,
    "github_secret": "GITHUB_TOKEN",
}


# --------------------------------------------------------------------------
# Environment helpers (secrets, callback, state)
# --------------------------------------------------------------------------


def _session_key():
    return os.environ.get("SESSION_API_KEY") or os.environ.get("OH_SESSION_API_KEYS_0", "")


def get_secret(name):
    """Fetch a named secret from the agent server (local) or the OpenHands API (cloud)."""
    url = os.environ.get("AGENT_SERVER_URL", "").rstrip("/")
    if url:
        url = f"{url}/api/settings/secrets/{name}"
    else:
        api = os.environ["OPENHANDS_CLOUD_API_URL"].rstrip("/")
        url = f"{api}/api/v1/sandboxes/{os.environ['SANDBOX_ID']}/settings/secrets/{name}"
    req = urllib.request.Request(url, headers={"X-Session-API-Key": _session_key()})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode().strip()


def fire_callback(status="COMPLETED", error=None):
    """Signal run completion. Called once, on every exit path."""
    url = os.environ.get("AUTOMATION_CALLBACK_URL", "")
    if not url:
        return
    body = {"status": status, "run_id": os.environ.get("AUTOMATION_RUN_ID", "")}
    if error:
        body["error"] = error
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ.get('AUTOMATION_CALLBACK_API_KEY', '')}",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=30)
    except Exception as e:  # noqa: BLE001
        print(f"Callback error (non-fatal): {e}")


class StateStore:
    """One JSON document in the automation KV store, with a local-file fallback."""

    KEY = "state"

    def __init__(self):
        self.token = os.environ.get("AUTOMATION_KV_TOKEN", "")
        self.base = os.environ.get("AUTOMATION_API_URL", "").rstrip("/")

    @property
    def kv(self):
        return bool(self.token and self.base)

    def _path(self):
        payload = json.loads(os.environ.get("AUTOMATION_EVENT_PAYLOAD", "{}") or "{}")
        d = Path.home() / ".openhands" / "automation-state"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"openspec_merge_audit_{payload.get('automation_id', 'default')}.json"

    def load(self):
        data = None
        if self.kv:
            req = urllib.request.Request(
                f"{self.base}/v1/kv/{self.KEY}",
                headers={"Authorization": f"Bearer {self.token}"},
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.loads(r.read())["value"]
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise
        else:
            p = self._path()
            if p.exists():
                data = json.loads(p.read_text())
        return data or {"version": 1, "stores": {}}

    def save(self, state):
        if self.kv:
            req = urllib.request.Request(
                f"{self.base}/v1/kv/{self.KEY}",
                data=json.dumps(state).encode(),
                method="PUT",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                r.read()
            return
        p = self._path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(p)


# --------------------------------------------------------------------------
# GitHub (the only VCS-specific part; swap this class to support another host)
# --------------------------------------------------------------------------


class GitHub:
    def __init__(self, token, api="https://api.github.com"):
        self.token = token
        self.api = api.rstrip("/")

    def get(self, path, params=None):
        url = self.api + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "openspec-merge-audit",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def default_branch(self, repo):
        return self.get(f"/repos/{repo}")["default_branch"]

    def merged_prs(self, repo, since):
        """Normalized records of PRs merged into the repo's default branch since `since`."""
        default = self.default_branch(repo)
        q = f"repo:{repo} is:pr is:merged base:{default} merged:>={iso(since)}"
        numbers = []
        for page in range(1, SEARCH_MAX_PAGES + 1):
            res = self.get(
                "/search/issues",
                {"q": q, "per_page": 100, "page": page, "sort": "updated", "order": "asc"},
            )
            items = res.get("items", [])
            numbers += [i["number"] for i in items]
            if len(items) < 100:
                break
        records = []
        for n in numbers:
            pr = self.get(f"/repos/{repo}/pulls/{n}")
            if not pr.get("merged"):
                continue
            records.append(
                {
                    "repo": repo,
                    "number": n,
                    "title": pr["title"],
                    "url": pr["html_url"],
                    "author": (pr.get("user") or {}).get("login"),
                    "base_ref": pr["base"]["ref"],
                    "head_ref": pr["head"]["ref"],
                    "head_repo": ((pr["head"].get("repo") or {}).get("full_name")),
                    "merged_at": pr["merged_at"],
                    "merge_commit_sha": pr["merge_commit_sha"],
                }
            )
        return records

    def open_prs(self, repo):
        out = []
        for page in range(1, 6):
            items = self.get(
                f"/repos/{repo}/pulls", {"state": "open", "per_page": 100, "page": page}
            )
            for p in items:
                out.append(
                    {
                        "number": p["number"],
                        "head_ref": p["head"]["ref"],
                        "head_repo": ((p["head"].get("repo") or {}).get("full_name")),
                        "base_ref": p["base"]["ref"],
                    }
                )
            if len(items) < 100:
                break
        return out


# --------------------------------------------------------------------------
# Agent server
# --------------------------------------------------------------------------


class AgentServer:
    def __init__(self, url, key):
        self.url = url.rstrip("/")
        self.key = key

    def _req(self, method, path, body=None):
        headers = {"X-Session-API-Key": self.key, "Content-Type": "application/json"}
        headers.update(_observability_headers())
        req = urllib.request.Request(
            self.url + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else None

    def status(self, conversation_id):
        """execution_status of a conversation, or None if it does not exist."""
        try:
            return (self._req("GET", f"/api/conversations/{conversation_id}") or {}).get(
                "execution_status"
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise

    def resolve_profile(self):
        pid = os.environ.get("AUTOMATION_AGENT_PROFILE_ID")
        if pid:
            return pid
        listing = self._req("GET", "/api/agent-profiles") or {}
        name = os.environ.get("AUTOMATION_AGENT_PROFILE_NAME")
        if name:
            for p in listing.get("profiles", []):
                if p.get("name") == name:
                    return p["id"]
            raise RuntimeError(f"agent profile {name!r} not found")
        active = listing.get("active_agent_profile_id")
        if not active:
            raise RuntimeError("no agent profile selected and none active")
        return active

    def _secrets(self, profile_id, github_secret):
        listing = self._req(
            "GET", "/api/settings/secrets?" + urllib.parse.urlencode({"agent_profile_id": profile_id})
        ) or {}
        out = {}
        for item in listing.get("secrets", []):
            out[item["name"]] = {
                "kind": "LookupSecret",
                "url": f"{self.url}/api/settings/secrets/{urllib.parse.quote(item['name'])}",
                "headers": {"X-Session-API-Key": self.key},
            }
        # The task and prompt always refer to GITHUB_TOKEN, whatever the secret is named here.
        if github_secret != "GITHUB_TOKEN":
            out["GITHUB_TOKEN"] = {
                "kind": "LookupSecret",
                "url": f"{self.url}/api/settings/secrets/{urllib.parse.quote(github_secret)}",
                "headers": {"X-Session-API-Key": self.key},
            }
        if "GITHUB_TOKEN" not in out:
            raise RuntimeError("agent profile does not expose GITHUB_TOKEN")
        return out

    def start(self, conversation_id, profile_id, prompt, workdir, secret_name):
        """Start the conversation once. Safe to call again with the same id."""
        if self.status(conversation_id) is not None:
            return "exists"
        try:
            os.makedirs(workdir, exist_ok=True)
        except OSError:
            pass
        body = {
            "conversation_id": conversation_id,
            "tags": dict(CONVERSATION_TAGS),
            "agent_profile_id": profile_id,
            "workspace": {"kind": "LocalWorkspace", "working_dir": workdir},
            # Every secret the agent profile lists, fetched lazily by the agent
            # server. The automation does not restrict which secrets those are.
            "secrets": self._secrets(profile_id, secret_name),
            "initial_message": {
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
                "run": True,
            },
        }
        try:
            self._req("POST", "/api/conversations", body)
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                return "exists"
            raise
        return "started"


def _observability_headers():
    try:
        from openhands.sdk.automation import automation_observability_headers

        return dict(automation_observability_headers(span_name="automation.conversation"))
    except Exception:  # noqa: BLE001 - SDK is optional here
        return {}


# --------------------------------------------------------------------------
# Pure logic
# --------------------------------------------------------------------------


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def branch_for(ts):
    return BRANCH_PREFIX + ts.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_link(pr):
    """Link an implementation PR to its planning-store PR and change id.

    Only trusted when the head branch lives in the same repo; a fork can name its
    branch anything.
    """
    if pr.get("head_repo") != pr.get("repo"):
        return None
    m = HEAD_RE.match(pr.get("head_ref") or "")
    if not m or ".." in m.group(2):
        return None
    return {"osow_pr": int(m.group(1)), "change_id": m.group(2)}


def resolve_stack(open_prs, store, default_branch, new_branch, warn_depth=5):
    """Pick the base for the new audit PR from the store's open audit PRs.

    The tip is the open audit PR whose head branch is not the base of another
    open audit PR. With no audit PRs open the base is the default branch.
    """
    audit = [
        p
        for p in open_prs
        if p["head_repo"] == store and BRANCH_RE.match(p["head_ref"] or "")
    ]
    existing = next((p for p in audit if p["head_ref"] == new_branch), None)
    notes = []
    if existing:
        # A retry of the same window: keep the PR's own base.
        parent = next((p for p in audit if p["head_ref"] == existing["base_ref"]), None)
        return {
            "base_ref": existing["base_ref"],
            "parent_pr": parent["number"] if parent else None,
            "existing_pr": existing["number"],
            "depth": len(audit) - 1,
            "notes": notes,
        }
    if not audit:
        return {
            "base_ref": default_branch,
            "parent_pr": None,
            "existing_pr": None,
            "depth": 0,
            "notes": notes,
        }
    bases = {p["base_ref"] for p in audit}
    tips = [p for p in audit if p["head_ref"] not in bases] or audit
    tips.sort(key=lambda p: BRANCH_RE.match(p["head_ref"]).group(1))
    tip = tips[-1]
    if len(tips) > 1:
        notes.append(
            "Several audit PR stacks are open ("
            + ", ".join(f"#{p['number']}" for p in tips)
            + f"); stacked on the newest tip #{tip['number']}."
        )
    if len(audit) >= warn_depth:
        notes.append(f"{len(audit)} audit PRs are already open in this store; please merge from the bottom up.")
    return {
        "base_ref": tip["head_ref"],
        "parent_pr": tip["number"],
        "existing_pr": None,
        "depth": len(audit),
        "notes": notes,
    }


def _safe_json(obj):
    # Backticks cannot close the fenced block in the task text.
    return json.dumps(obj, indent=2, ensure_ascii=False).replace("`", "\\u0060")


def render_task(values):
    """Fill {{NAME}} slots in one pass so substituted text is never re-expanded."""
    template = (PLUGIN_ROOT / "scripts" / "audit_task.md").read_text()
    return re.sub(r"\{\{([A-Z_]+)\}\}", lambda m: str(values.get(m.group(1), "")), template)


def build_task(store, store_default, stack, branch, window_start, window_end, prs):
    parent = f"#{stack['parent_pr']}" if stack["parent_pr"] else "none (bottom of stack)"
    existing = f"#{stack['existing_pr']}" if stack["existing_pr"] else "none"
    notes = ""
    if stack["notes"]:
        notes = "- Notes: " + " ".join(stack["notes"])
    return render_task(
        {
            "STORE_REPO": store["store_repo"],
            "STORE_NAME": store["store_repo"].split("/")[1],
            "STORE_DEFAULT_BRANCH": store_default,
            "IMPL_REPOS": ", ".join(f"`{r}`" for r in store["repos"]),
            "WINDOW_START": iso(window_start),
            "WINDOW_END": iso(window_end),
            "BASE_REF": stack["base_ref"],
            "NEW_BRANCH": branch,
            "BRANCH_TS": branch[len(BRANCH_PREFIX):],
            "PARENT_PR": parent,
            "EXISTING_PR": existing,
            "STACK_NOTES": notes,
            "PRS_JSON": _safe_json(prs),
        }
    )


def _pr_key(pr):
    return f"{pr['repo']}#{pr['number']}"


def process_store(store, state, now, gh, agent, cfg, profile_id, dry_run=False, save=lambda: None):
    """Handle one store. Returns a short status string."""
    name = store["store_repo"]
    s = state["stores"].setdefault(name, {})

    # Is the previous conversation still working?
    carry = []
    prev = s.get("conversation_id")
    prev_status = agent.status(prev) if prev else None
    if prev_status == "running":
        print(f"[{name}] previous conversation {prev} still running; skipping")
        return "busy"
    if prev_status in ("error", "stuck") and s.get("unfinished"):
        if s.get("retries", 0) < MAX_RETRIES:
            carry = s["unfinished"]
            print(f"[{name}] previous conversation ended {prev_status}; retrying {len(carry)} PR(s)")
        else:
            print(f"[{name}] previous conversation ended {prev_status}; retries exhausted, dropping {len(s['unfinished'])} PR(s)")
            s["unfinished"], s["retries"] = [], 0
    elif prev_status not in ("error", "stuck"):
        s["unfinished"], s["retries"] = [], 0

    last = parse_iso(s["last_poll_ts"]) if s.get("last_poll_ts") else now - timedelta(minutes=cfg["window_minutes"])
    since = last - timedelta(minutes=cfg["overlap_minutes"])

    handled = set(s.get("handled", []))
    new = []
    for repo in store["repos"]:
        for pr in gh.merged_prs(repo, since):
            if _pr_key(pr) not in handled:
                new.append(pr)
    seen, prs = set(), []
    for pr in carry + new:
        if _pr_key(pr) not in seen:
            seen.add(_pr_key(pr))
            prs.append(pr)

    if not prs:
        print(f"[{name}] no merges since {iso(since)}")
        s["last_poll_ts"] = iso(now)
        return "no-merges"

    prs.sort(key=lambda p: (p["merged_at"], p["repo"], p["number"]))
    for i, pr in enumerate(prs, 1):
        pr["order"] = i
        pr["link"] = parse_link(pr)

    branch = s.get("pending_branch") or branch_for(now)
    s["pending_branch"] = branch
    store_default = gh.default_branch(name)
    stack = resolve_stack(gh.open_prs(name), name, store_default, branch, cfg["stack_warn_depth"])
    task = build_task(store, store_default, stack, branch, last, now, prs)
    conv_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"openspec-merge-audit:{name}:{branch}"))
    print(f"[{name}] {len(prs)} PR(s); base={stack['base_ref']} parent={stack['parent_pr']} branch={branch}")

    if dry_run:
        print(f"--- task for {name} ---\n{task}\n--- end ---")
        s.pop("pending_branch", None)
        return "dry-run"

    save()  # persist pending_branch so a crash before the start reuses the branch name
    workdir = os.path.join(
        os.environ.get("WORKSPACE_BASE") or str(Path.home() / "workspace"),
        "openspec-merge-audit",
        name.replace("/", "_"),
        branch[len(BRANCH_PREFIX):],
    )
    result = agent.start(conv_id, profile_id, task, workdir, cfg["github_secret"])
    print(f"[{name}] conversation {conv_id}: {result}")

    s["last_poll_ts"] = iso(now)
    s["conversation_id"] = conv_id
    s["branch"] = branch
    s["unfinished"] = prs
    s["retries"] = s.get("retries", 0) + 1 if carry else 0
    s["handled"] = (list(s.get("handled", [])) + [_pr_key(p) for p in new])[-HANDLED_LIMIT:]
    s.pop("pending_branch", None)
    return "started"


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def load_config():
    path = os.environ.get("AUTOMATION_CONFIG_PATH")
    cfg = json.loads(Path(path).read_text()) if path and Path(path).exists() else {}
    for k, v in DEFAULTS.items():
        cfg.setdefault(k, v)
    stores = cfg.get("stores")
    if not stores:
        raise RuntimeError("config has no stores")
    for st in stores:
        for r in [st["store_repo"], *st["repos"]]:
            if not REPO_RE.match(r):
                raise RuntimeError(f"invalid repo name: {r!r}")
    return cfg


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    dry_run = "--dry-run" in argv
    errors = []
    try:
        cfg = load_config()
        gh = GitHub(get_secret(cfg["github_secret"]))
        agent_url = os.environ.get("AGENT_SERVER_URL", "")
        if not agent_url:
            raise RuntimeError("AGENT_SERVER_URL is not set; only a local Agent Canvas is supported")
        agent = AgentServer(agent_url, _session_key())
        profile_id = None if dry_run else agent.resolve_profile()
        store_state = StateStore()
        state = store_state.load()
        now = datetime.now(timezone.utc).replace(microsecond=0)
        for store in cfg["stores"]:
            try:
                process_store(
                    store, state, now, gh, agent, cfg, profile_id,
                    dry_run=dry_run, save=lambda: store_state.save(state),
                )
            except Exception as e:  # noqa: BLE001 - one store must not stop the others
                print(f"[{store['store_repo']}] ERROR: {e}", file=sys.stderr)
                errors.append(f"{store['store_repo']}: {e}")
            if not dry_run:
                store_state.save(state)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}", file=sys.stderr)
        errors.append(str(e))
    if errors:
        fire_callback("FAILED", "; ".join(errors)[:1000])
        return 1
    fire_callback("COMPLETED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
