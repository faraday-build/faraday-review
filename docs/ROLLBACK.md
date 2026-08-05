# Rollback

The plane is shadow-only: it authorizes nothing, so rollback is removal of
observation, never of enforcement.

## Disable for one repository

```bash
gh api -X DELETE repos/OWNER/REPO/contents/.github/workflows/faraday-review.yml \
  -f message="chore: disable faraday-review shadow" \
  -f sha="$(gh api repos/OWNER/REPO/contents/.github/workflows/faraday-review.yml --jq .sha)"
# or via a normal PR removing the file (preferred under branch protection)
```

## Disable estate-wide

Remove/empty the reusable workflow in `tomrivera-ops/.github` via PR:
`.github/workflows/reusable-faraday-review.yml`. Consumers fail cleanly
(missing reusable workflow ⇒ workflow error, no decisions produced, no
authorization impact).

## Un-pin / re-pin policy

```bash
# roll back to a previous released digest (never to an unreleased tree)
gh variable set FARADAY_POLICY_DIGEST --org tomrivera-ops \
  --body "sha256:<previous released digest>"
```

## Revert this repo's enforcement (founder-only, break-glass)

```bash
gh api -X DELETE repos/faraday-build/faraday-review/rulesets/20418235
```

Deleting ruleset 20418235 removes required checks on main — do this only
to unwedge a broken CI that blocks its own fix, and restore it
immediately after:

```bash
gh api -X POST repos/faraday-build/faraday-review/rulesets --input ruleset.json
```

## Telemetry

`artifacts/reviews/telemetry.jsonl` is append-only run data. Never delete
during rollback — it is the Phase 2 calibration input.
