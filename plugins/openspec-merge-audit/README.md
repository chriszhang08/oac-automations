# openspec-merge-audit

Daily cron automation at 02:00 UTC (script runner, no LLM in the poller). One run covers everything merged since the previous run, so a missed day is caught up.

1. Per store, search the watched repos for PRs merged into their default branch since the last poll (with a 10 minute overlap; handled PRs are de-duplicated), and sort them in merge order.
2. Link a PR to its planning change by head branch `odysseus/pr<N>-<change-id>`. Fork branches are never linked.
3. Skip a store whose previous conversation is still running. Re-send PRs from a conversation that ended in error, at most twice.
4. Start one conversation per store with merges. It plans the whole batch, archives the linked changes in merge order, reconciles specs touched by more than one OSOW, audits each affected spec once against code, and opens a PR in the store repo. The PR lists the cross-OSOW specs and the order to merge things in. The agent never merges anything.

## Naming and stacking

- Branch: `openspec-audit/<YYYYMMDDTHHMMSSZ>` (UTC end of the poll window).
- The script picks the base: the tip of the open `openspec-audit/*` PRs in the store (the one that no other open audit PR is based on), or the default branch if none are open. The agent is told the base and never decides it.
- Merge from the bottom of the stack up. Enable "delete branch on merge" on the store repos so GitHub retargets the next PR.

## Create the automation

```bash
curl -X POST "$AUTOMATION_HOST/api/automation/v1/plugin-spec" \
  -H "X-Session-API-Key: $OPENHANDS_AUTOMATION_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"OpenSpec merge audit",
       "plugin":{"source":"github:chriszhang08/oac-automations","repo_path":"plugins/openspec-merge-audit"}}'
```

Config defaults (stores, windows, secret name) come from `.plugin/plugin.json`.

## Test

```bash
python3 -m unittest discover -s tests
# read-only run against real GitHub: prints the task instead of starting a conversation
AGENT_SERVER_URL=... SESSION_API_KEY=... AUTOMATION_CONFIG_PATH=cfg.json python3 scripts/main.py --dry-run
```

Only a local Agent Canvas (`AGENT_SERVER_URL` set) is supported. To move off GitHub, replace the `GitHub` class in `scripts/main.py`; the rest is provider-neutral.
