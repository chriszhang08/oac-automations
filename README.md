# oac-automations

OpenHands plugin-spec automations.

| Plugin | What it does |
|---|---|
| [`openspec-merge-audit`](plugins/openspec-merge-audit) | Hourly: finds merged PRs in watched repos, then archives the matching OpenSpec changes and audits specs against code in the planning store, opening a stacked PR. |
| [`claude-haiku-healthcheck`](plugins/claude-haiku-healthcheck) | Every 5 hours: asks the `claude-haiku` agent profile "what model are you" and checks the turn finishes, as a lightweight health check of that profile's endpoint. |
| [`delete-planning-store-audit-conversations`](plugins/delete-planning-store-audit-conversations) | Daily 03:00 UTC: deletes finished conversations tagged `temporary` (the tag `openspec-merge-audit` and `claude-haiku-healthcheck` set). |
| [`delete-old-conversations`](plugins/delete-old-conversations) | Mondays 03:00 UTC: deletes conversations not updated in 14 days. |

The two `delete-*` plugins are copies of automations that are already deployed as raw tarballs. Their manifests are not deployed or tested; the live automations still run the tarball versions.
