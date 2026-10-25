# Release check (CI)

Nightly, advisory check that frappe, erpnext, hrms and india_compliance still
work **together** on the v15 and v16 release candidates (`version-N-hotfix`).
It runs the existing [release_tests](https://github.com/frappe/release_tests)
suites; it adds no tests of its own and changes nothing in this app.

| Job | What it does |
| --- | --- |
| `upgrade` | Installs the released branch (`version-N`), runs the suites (baseline), upgrades to the candidate the way a customer update does (code, requirements, migrate) and runs them again. A check that passed before and fails after is a **regression**. |
| `fresh` | Installs the candidate directly and runs the suites. |

Failing suites are re-run once; a failure that passes on re-run is **flaky**
and not counted. Each job records the exact commit of every app.

**Where to look:** the run's summary page lists regressions in plain words
(check, user and roles, expected vs actual, endpoint). The `release-check-*`
artifact holds `summary.md`, `evidence.json` (kept for the evidence log) and
the raw engine reports.

**Run it by hand:** Actions → Release check → Run workflow (pick versions and
the release_tests branch). It also runs when anything under `ci/` changes.

Files: `release-check.yml` (workflow), `release_check.sh` (bench, site, runs),
`compare.py` (classification and report).
