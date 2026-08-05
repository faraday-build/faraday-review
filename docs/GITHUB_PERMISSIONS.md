# GitHub Permissions

## Shadow workflow token (ceiling, guard-enforced)

```yaml
permissions:
  contents: read        # checkout + diff range for gitleaks
  pull-requests: write  # the one idempotent governed summary comment
  checks: write         # the governed-summary check run
  actions: read         # observe required-check workflow runs
```

`workflow_hardening.py` fails CI if any workflow requests more than this
ceiling, omits `permissions:` entirely, or requests write on any scope
outside the set above.

## What the plane can NEVER do with this token

- approve a PR (`pull-requests: write` allows comments and labels, and
  the `bot-review-not-authoritative` check fails if any Bot APPROVED
  review appears)
- merge a PR (requires `contents: write`, which is not granted)
- push code, tags, or releases
- read or write org/repo secrets

## Human/CLI operations used during Phase 1 build

- repo creation, ruleset management: `tomrivera-PSE` PAT with `repo`,
  `admin:org` (founder-authorized, interactive)
- governance/contract releases: signed tags pushed by the founder account

## Known org-level risk (not this plane's token)

`tomrivera-ops/.github/.github/workflows/cli-approve-merge.yml` mints an
org-wide App token that can approve+merge any PR — revoked by ADR-0012
but still present. See DISCOVERY §9.1; retirement is a founder action.
