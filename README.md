# oac-automations

OpenHands plugin-spec automations.

| Plugin | What it does |
|---|---|
| [`linear-issue-triage`](plugins/linear-issue-triage) | Every 5 minutes: classify new EkkoLLM issues with existing labels and link clear duplicates or related work. |
| [`openspec-merge-audit`](plugins/openspec-merge-audit) | Hourly: finds merged PRs in watched repos, then archives the matching OpenSpec changes and audits specs against code in the planning store, opening a stacked PR. |
| [`delete-planning-store-audit-conversations`](plugins/delete-planning-store-audit-conversations) | Daily 03:00 UTC: deletes finished conversations tagged `odysseuschange=planning-store-audit` (the tag `openspec-merge-audit` sets). |
| [`delete-old-conversations`](plugins/delete-old-conversations) | Mondays 03:00 UTC: deletes conversations not updated in 14 days. |

The two `delete-*` plugins are copies of automations that are already deployed as raw tarballs. Their manifests are not deployed or tested; the live automations still run the tarball versions.
