# Release Manager — Roadmap

**Goal:** grow Release Manager from a *test runner* into a *release management
platform* that mirrors how Frappe/ERPNext actually ship software.

This document is written for someone new to the codebase (e.g. an intern). It
explains Frappe's release process first, then lays out the features to build, in
order, each with **Why / What to build / Done when**.

---

## 1. How Frappe/ERPNext release software (the model we're targeting)

Frappe ships with a strict, automated process. If our platform is going to
*manage* releases, it has to understand this model.

- **Branches**
  - `develop` — the active development branch. All pull requests (PRs) merge here.
  - `version-15`, `version-16` — one **stable branch per major version**. No direct
    commits; changes arrive only through release automation.
  - `version-15-hotfix`, `version-16-hotfix` — where **backported bug fixes** land
    before being cut as patch releases. (Our own test targets are literally named
    `Hotfix-v16-FC` / `V15-Hotfix-FC`, so we already live in this world.)
- **Semantic Versioning** — `MAJOR.MINOR.PATCH`. The size of the bump is decided
  from **PR labels** (a breaking change → MAJOR, a feature → MINOR, a fix → PATCH).
- **`bench release` automation** — bumps the version, compiles a **changelog** from
  merged PRs, creates a **git tag**, and pushes the release to GitHub.
- **CI/CD** — GitHub Actions run the test suites on every PR. **Patch releases go
  out roughly weekly** once tests pass; automation merges validated fixes into the
  stable branches.
- **Deployment commands** admins run on their servers:
  - `bench switch-to-branch version-16 frappe erpnext` — move an install from one
    branch to another.
  - `bench update` — pull the latest patches, rebuild assets, run DB migrations.
- **Major upgrades** (e.g. v15 → v16) isolate breaking changes. On managed
  hosting, auto-updates are **held ~6 months** so self-hosted users can read the
  migration guides, audit custom apps, and run **staging simulations** first.

**One-line summary:** code flows `develop → version-N(-hotfix) → tag → weekly
patch → bench update on sites`, gated by tests, with major upgrades held and
simulated before rollout.

---

## 2. Where this app is today (Phase 0)

Release Manager is a Frappe **control plane** driving the standalone
[`release_tests`](https://github.com/umairsy/release_tests) HTTP engine. Today it
can:

- Register target sites (**Testing Site**) and detect installed apps + versions.
- Keep a catalog of **Release Test Case** / **Test Case Group** (synced from the
  engine's suites).
- Run **Release Tests** (API smoke flows + Cypress UI) against a site, version-aware,
  and record **Test Result**s with corrective-action tracking.
- Schedule runs via **Release Test Plan** (Manual/Hourly/Daily/Weekly).
- Show it all in the Desk workspace and the Vue dashboard at `/release`.

**So today the app answers "does this site/version still work?"** The roadmap
below makes it also answer **"what are we releasing, is it ready, and how do we
ship and upgrade it?"** — using the existing test engine as the quality gate.

> Architecture note: keep the split. `release_tests` (engine) stays a Frappe-free
> HTTP client that *executes* checks; Release Manager (this app) *models,
> orchestrates, persists, and reports*. Most phases below add DocTypes here and,
> where needed, capabilities to the engine.

---

## 3. The roadmap

Each phase is a shippable increment. Rough dependency order is top-to-bottom;
**Phase 4 (quality gates) is the highest-value tie-in** and everything before it
is foundation for it.

### Phase 1 — Model apps, versions & branches (foundation)
**Why:** you cannot manage releases without first representing apps, their
versions, and Frappe's branches as data.
**What to build:**
- `App` DocType — name, git repo URL, `is_frappe_app`.
- `Release Branch` DocType — e.g. `develop`, `version-16`, `version-16-hotfix`
  (app + branch name + kind: development / stable / hotfix).
- `App Version` DocType — app + semver + branch + git tag + release date.
- Extend **Testing Site** to store, per installed app, the **branch** (not just the
  version it already detects).
- A **version matrix** view: every site × app → branch + version.
**Done when:** for each registered site you can see which branch + version each app
is on, and there's a catalog of known versions.

### Phase 2 — Define a "Release"
**Why:** the Release is the object you manage; make it explicit.
**What to build:**
- `Release` DocType — app + source branch (e.g. `version-16-hotfix`) + bump type
  (major/minor/patch) + computed target version + status
  (`Draft → Validating → Ready → Released`) + changelog + linked Test Plan.
- Semver bump helper (current version + bump type → next version).
**Done when:** you can create a Release, choose a bump type, and it computes the
next version and lists the checks it needs.

### Phase 3 — Changelog & PR/label integration (mirror `bench release`)
**Why:** Frappe builds changelogs from merged PRs grouped by label; so should we.
**What to build:**
- GitHub integration (API token / GitHub App) to fetch **merged PRs since the last
  tag** on a branch.
- Group PRs by label (feature / fix / breaking) → auto-draft the Release changelog.
- Detect a breaking-change label → suggest a MAJOR bump.
**Done when:** opening a Release auto-drafts a changelog from GitHub PRs since the
last tag, grouped by type.

### Phase 4 — Quality gates (tie tests to release readiness) ⭐
**Why:** this is where the existing test engine becomes a *gate*. A Release should
only become "Ready" when its suites pass on a site running the candidate branch.
**What to build:**
- `Release Gate` — required Test Plan(s) + target site(s) on the candidate branch;
  the Release cannot advance to `Ready` until they pass.
- **Regression matrix** — run the same suites on the current stable *and* the
  candidate (e.g. `version-16` vs `version-16-hotfix`) and diff pass/fail.
- Promotion is blocked/allowed by gate status.
**Done when:** a Release flips to `Ready` automatically only when its gate passes on
a site running the candidate branch.

### Phase 5 — Release execution / automation
**Why:** automate the actual cut, like `bench release`.
**What to build:**
- From a `Ready` Release, trigger the tag + GitHub release (call `bench release`,
  or dispatch a GitHub Actions workflow that does it).
- Record the resulting tag / version / release URL back on the Release.
- Show the release workflow's CI status.
**Done when:** clicking "Release" on a Ready release cuts the tag + GitHub release
(or dispatches the CI that does) and records the outcome.

### Phase 6 — Deploy / update orchestration
**Why:** admins run `bench update` / `bench switch-to-branch`; the platform should
drive and verify these on managed sites.
**What to build:**
- `Deployment` DocType — pick site(s) + target branch/version → run
  `bench switch-to-branch` / `bench update` via a **runner on the bench host** →
  run the post-update smoke suite → record result.
- Take a backup before, with a rollback hook.
- Fold the weekly-patch update + smoke run into the existing scheduled Test Plans.
**Done when:** you can pick a site + target version; the platform backs it up,
updates it, runs the smoke suite, and reports pass/fail.

### Phase 7 — Major-version upgrade campaigns (the 6-month hold)
**Why:** major upgrades are high-risk, isolated, and held ~6 months; model that.
**What to build:**
- `Upgrade Campaign` — source → target major, affected sites, migration-guide
  checklist, custom-app audit, hold window + deadline per site.
- **Staging simulation** — clone a prod site → `switch-to-branch` to the new major
  → migrate → run the full suite → produce a **go/no-go upgrade report**.
- Track each site's upgrade state + deadline; notify before the hold expires.
**Done when:** for a v15 → v16 campaign you can run a staging simulation on a cloned
prod site, get a go/no-go report, and track every site's upgrade status + deadline.

### Phase 8 — Provision a site from scratch at pinned versions
**Why:** reproducible fresh environments at exact versions (for staging sims, CI,
and demos).
**What to build:**
- A declarative **site spec** — site name + list of `(app, git-ref)` — that
  provisions a fresh site at those versions and runs a baseline suite.
- Reuse it for Phase 7 staging simulations.
- Prerequisites to codify: matching Python/Node per Frappe major; Redis; a DB with
  root creds; network access to app repos; app **dependency order** (framework →
  erpnext → hrms/india_compliance/webshop; telephony → helpdesk); unique site names.
**Done when:** from a spec you can spin up a fresh site at pinned app versions and it
shows up as a Testing Site with a green baseline run.

### Phase 9 — Observability, notifications & governance (cross-cutting)
**Why:** stakeholders need to see health and be told when something breaks.
**What to build:**
- Dashboards: release health, per-version regression trends, upgrade readiness.
- Notifications (email / Slack) on gate failures, failed weekly runs, upcoming
  upgrade deadlines.
- Approvals + audit log (who approved / released what) and RBAC
  (Release Manager vs Viewer).
**Done when:** people are notified on failures/approvals and can see release +
upgrade health at a glance.

---

## 4. Cross-cutting concerns (apply to several phases)

- **The bench-host runner** (Phases 5–8) executes `bench` commands on a host.
  ⚠️ Hard-won lesson from this project: long-running bench processes (`bench start`
  with `--noreload`, background workers) **do not pick up on-disk app/version
  changes until restarted**. Any runner that updates apps **must restart the
  relevant processes** and confirm health before running checks.
- **GitHub integration & secrets** (Phases 3, 5) — store tokens as Frappe
  `Password` fields; never commit credentials.
- **Multi-app** — everything is keyed by App; don't hard-code `frappe`/`erpnext`.
- **Security & permissions** — releases and deployments are powerful actions; gate
  them behind roles and log them.

---

## 5. Suggested sequencing

1. **Foundation:** Phase 1 → Phase 2.
2. **Highest value:** Phase 4 (gates) — makes the existing tests decide readiness.
3. **Automation:** Phase 3 (changelog) → Phase 5 (cut) → Phase 6 (deploy).
4. **Scale/quality:** Phase 7 (upgrades) → Phase 8 (provisioning) → Phase 9
   (observability & governance).

Ship each phase end-to-end (DocType + API + a bit of UI + a test) before starting
the next.

---

## 6. Glossary (for newcomers)

- **develop** — Frappe's active development branch; PRs merge here.
- **version-N** — the stable branch for major version N (e.g. `version-16`).
- **hotfix branch** — `version-N-hotfix`, where backported fixes wait to be released.
- **Semantic Versioning (semver)** — `MAJOR.MINOR.PATCH`; breaking / feature / fix.
- **`bench release`** — Frappe's tool to bump version, build changelog, tag, push.
- **`bench update`** — pull patches, rebuild assets, run DB migrations on a site.
- **`bench switch-to-branch`** — move an install to a different branch/version.
- **migration** — DB schema/data changes an app runs when you update it.
- **gate** — a required set of tests that must pass before a release advances.
- **campaign** — a coordinated major-version upgrade across many sites.
- **staging simulation** — rehearsing an upgrade on a clone of production first.
