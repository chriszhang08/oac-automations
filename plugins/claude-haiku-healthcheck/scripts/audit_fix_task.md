The CI job "Validate OpenSpec" is failing on `openspec validate` for an open audit PR. Fix it and push. Do only this; read only what you need.

## Inputs

- Planning store repo: `{{STORE_REPO}}`
- PR: #{{PR_NUMBER}} (head branch `{{HEAD_REF}}`, head commit `{{HEAD_SHA}}`)
- Failing run: {{RUN_URL}}

## Steps

1. Run `export GH_TOKEN="$GITHUB_TOKEN"` and then `gh auth setup-git`. Never print the token or write it to a file or remote URL.
2. Clone `{{STORE_REPO}}` and check out `{{HEAD_REF}}`. If its tip is not `{{HEAD_SHA}}`, stop and say so: a newer commit exists and the next run will handle it.
3. Install the pinned CLI the way `.github/workflows/validate.yml` does, then run `openspec validate --all --strict --no-interactive` to reproduce the failure.
4. Fix only what the validator reports, with the smallest edit to the OpenSpec files (specs, changes, archive). Do not touch code, workflows or unrelated specs, and do not change the meaning of a requirement just to satisfy the validator. If a fix needs a judgment call, stop and report instead.
5. Re-run the validate command until it passes.
6. Commit and push to `{{HEAD_REF}}` only. Never push to any other branch and never force-push. Do not merge, close or approve any PR.
7. Comment on PR #{{PR_NUMBER}} with what you fixed.

Finish with a short report: what failed, what you changed, and the validate result.
