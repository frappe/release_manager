# Release Manager

A Frappe app that is the **control plane / UI** for the
[`release_tests`](../release_tests) execution engine. It lets you register target
Frappe sites, keep a master catalog of test cases, run release tests (manually or
on a schedule), and review results with corrective-action tracking — from the
Frappe Desk or the Vue dashboard at **`/release`**.

> The product, app package, and repo are all named **`release_manager`**. The
> DocType names below match what you see in the Frappe Desk.

## Running against Frappe Cloud (or any remote site)

The engine is a pure external HTTP client, so no install is needed on the target.
To test a Frappe Cloud site:

1. On that site, create an **API key + secret** for a user with the needed roles
   (User → API Access → Generate Keys).
2. Add a **Testing Site** with `base_url = https://<site>.frappe.cloud`,
   `auth_type = token`, and those `api_key`/`api_secret`. Leave Host header blank
   (real DNS). Click **Test Connection**, then run.

Token auth is verified working end-to-end (engine `use_token`).

It runs on one local **control site** (e.g. `releasemanager.localhost`) and drives *other*
sites over HTTP via the engine, so it never has to be installed on the systems
under test. The same Site records later point at Frappe Cloud URLs with API
tokens — no rework.

## Concepts (DocTypes)

- **Testing Site** — a target site: URL, Host header, and login or token auth.
  "Test Connection" detects installed apps + versions.
- **Release Test Case** — the test master, synced from the engine's suite catalog
  ("Sync Catalog"). Each maps to an engine suite (e.g. `erpnext`).
- **Test Case Group** — bundles test cases so a whole area runs at once (see below).
- **Release Test Plan** — a saved selection of sites + test cases with a frequency
  (Manual/Hourly/Daily/Weekly) for scheduled runs.
- **Release Test** — one execution ("run"): pick a Site, pick Test Cases, "Run Now".
  Runs in the background and records status + step/pass/fail counts.
- **Test Result** — per run/site/suite outcome with step-by-step detail, an
  error log, and `corrective_action` + `action_status` fields to triage.

## Flow

1. Add Testing Sites and click **Test Connection**.
2. **Sync Catalog** on Release Test Case to load available suites.
3. Create a Release Test → choose a Site + Test Cases → **Run Now**.
4. Watch status, then review Test Results; record corrective actions.
5. (Optional) Save a Release Test Plan and set a frequency for scheduled runs.

The **Release Manager** Workspace gives shortcuts and pass/fail dashboard charts.

## Deploying to Frappe Cloud

Add **only this app** in *Add app from GitHub* (`https://github.com/frappe/release_manager`).
The `release_tests` engine is a plain Python library, not a Frappe app — it is declared
as a pip dependency here and installed automatically with this app. Adding it through
that dialog will fail, and should.

Two things make the repo acceptable to Frappe Cloud, both in `pyproject.toml`:

- `[tool.bench.frappe-dependencies]` — without it the dialog reports *"Could not find a
  compatible Frappe version in pyproject.toml"*.
- `[tool.bench.assets]` — tells the build to compile the Vue dashboard, otherwise
  `/release` deploys empty.

The engine is referenced as `git+https://github.com/frappe/release_tests.git@main`, so
a bench build picks up whatever is on that branch. Push `release_tests` before
rebuilding if you want engine changes included.

## Dev install

```bash
# engine into the bench env
benches/frappe-bench/env/bin/python -m pip install -e ~/release_tests
# app into the bench, then on the control site
bench --site releasemanager.localhost install-app release_manager
```

## Test Groups

A **Test Case Group** bundles test cases so you can run a whole area at once. Running a
group (Desk "Run Group" button, or `release_manager.api.run_group(site, group)`) creates one
Release Test for every case in the group. `sync_catalog` auto-seeds per-app groups (e.g.
"ERPNext - Distribution" = all erpnext cases); you can also curate your own, mixing apps.

## Web UI (`/release`)

A Vue single-page app (frappe-ui + Vite, in `frontend/`) served at
`http://<site>/release`, reusing the standard Frappe login. Views: **Runs list** → **New Run**
(pick a Site + a Group) → **Run detail** (live suites → steps, errors, and a corrective-action
/ re-run panel that updates while the run is in flight).

Build it (independent of `bench build`/esbuild):

```bash
cd frontend && yarn && yarn build      # outputs to release_manager/public/frontend + www/release/index.html
# first time only, ensure the asset symlink exists:
ln -sfn $PWD/../release_manager/public <bench>/sites/assets/release_manager
```

Dev: `cd frontend && yarn dev` (Vite proxies `/api` to the bench).
