"""Server-side API for Release Manager.

Bridges the Frappe Desk (DocTypes, buttons, scheduler) to the ``release_tests``
execution engine. The engine does the HTTP work against target sites; this
module turns its plain-dict results into Test Result records.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from traceback import format_exception

import frappe
from frappe.utils import now_datetime

from release_manager.diagnosis import diagnose

_STEP_TO_RESULT = {"pass": "Passed", "fail": "Failed", "skip": "Skipped"}


# --------------------------------------------------------------------- helpers
def _site_config(site_doc) -> dict:
    """Build the engine's site-config dict from a Testing Site document."""
    cfg = {
        "label": site_doc.name,
        "url": site_doc.base_url,
        "host_header": site_doc.host_header or None,
        "auth": site_doc.auth_type,
        "username": site_doc.username,
        "api_key": site_doc.api_key,
        # Gates the customisations suite, which writes schema to the target.
        "allow_customisations": bool(site_doc.get("allow_customisations")),
    }
    if site_doc.auth_type == "login":
        cfg["password"] = site_doc.get_password("password") if site_doc.password else None
    else:
        cfg["api_secret"] = site_doc.get_password("api_secret") if site_doc.api_secret else None
    return cfg


# Labels for the structured failure details the engine attaches to a failed step
# (what was checked, as whom, expected vs actual, where, and the upstream change).
_DETAIL_LABELS = (
    ("kind", "Kind"),
    ("check", "Check"),
    ("user", "User"),
    ("roles", "Roles"),
    ("expected", "Expected"),
    ("actual", "Actual"),
    ("endpoint", "Endpoint"),
    ("guards", "Guards"),
    ("hint", "Hint"),
)


def _step_error(step: dict) -> str | None:
    """A step's error, expanded with its failure details when the engine sent them,
    so the Test Result says what broke and for whom without re-running."""
    details = step.get("details") if step.get("status") == "fail" else None
    if not details:
        return step.get("error") or (
            f"Skipped: {step['error']}" if step.get("status") == "skip" and step.get("error") else None
        )
    lines = []
    for key, label in _DETAIL_LABELS:
        value = details.get(key)
        if value:
            lines.append(f"{label}: {', '.join(value) if isinstance(value, list) else value}")
    return "\n".join(lines)


def _run_rate_lines(totals: dict, kinds: dict) -> list[str]:
    """Summary lines led by run rate: the share of checks that reached a verdict.

    The suites exist to catch problems users would hit, so a check that found an
    issue counts as a result; a skip or a broken test means nothing was learned.
    """
    total = sum(totals.values())
    if not total:
        return []
    issues = kinds.get("issue", 0)
    reached = totals.get("pass", 0) + issues
    return [
        "",
        f"Run rate: {round(100 * reached / total)}% ({reached} of {total} checks reached a verdict)",
        f"  Issues found: {issues}",
        f"  Needs triage: {kinds.get('needs triage', 0)}",
        f"  Passed: {totals.get('pass', 0)}",
        f"  Not checked: {totals.get('skip', 0)} skipped, {kinds.get('harness', 0)} broken tests",
    ]


def _suites_for_run(run_doc) -> list[str] | None:
    """Resolve the selected Test Cases to engine suite names (None = all)."""
    if not run_doc.test_cases:
        return None
    suites = []
    for row in run_doc.test_cases:
        suite = frappe.db.get_value("Release Test Case", row.test_case, "suite")
        if suite:
            suites.append(suite)
    return suites or None


# ------------------------------------------------------------- catalog + sites
@frappe.whitelist()
def sync_catalog() -> int:
    """Upsert Release Test Case records from the engine's suite catalog."""
    from release_tests import api as engine

    count = 0
    for entry in engine.catalog():
        preview = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(entry["steps"]))
        existing = frappe.db.get_value("Release Test Case", {"suite": entry["suite"]}, "name")
        doc = (
            frappe.get_doc("Release Test Case", existing)
            if existing
            else frappe.new_doc("Release Test Case")
        )
        if not existing:
            doc.test_case_name = entry["suite"]
        doc.suite = entry["suite"]
        doc.required_app = entry["required_app"]
        doc.description = entry["description"]
        doc.steps_preview = preview
        doc.guards = "\n".join(entry.get("guards") or [])
        doc.save(ignore_permissions=True)
        count += 1
    seed_example_groups()
    seed_curated_groups()
    frappe.db.commit()
    return count


@frappe.whitelist()
def seed_example_groups() -> int:
    """Create one curated group per app from existing Test Cases' required_app.

    Idempotent and non-destructive: only creates a group if it doesn't exist, so
    it never overwrites groups the user has hand-edited. Gives sensible defaults
    like "ERPNext - Distribution" (all erpnext cases) out of the box.
    """
    labels = {
        "erpnext": "ERPNext - Distribution",
        "hrms": "Frappe HR",
        "frappe": "Framework",
        "india_compliance": "India Compliance",
        "crm": "Frappe CRM",
    }
    by_app: dict[str, list[str]] = {}
    for case in frappe.get_all(
        "Release Test Case", filters={"enabled": 1}, fields=["name", "required_app"]
    ):
        by_app.setdefault(case.required_app or "other", []).append(case.name)

    created = 0
    for app, cases in by_app.items():
        group_name = labels.get(app, app.replace("_", " ").title())
        if frappe.db.exists("Test Case Group", group_name):
            continue
        group = frappe.new_doc("Test Case Group")
        group.group_name = group_name
        group.description = f"Auto-seeded: all {app} test cases."
        for case in cases:
            group.append("test_cases", {"test_case": case})
        group.insert(ignore_permissions=True)
        created += 1
    return created


# Groups that cut across apps, so they can't be derived from required_app the way
# seed_example_groups does. Membership is by engine suite name.
_CURATED_GROUPS = {
    "Release Regressions": (
        "Suites guarding specific upstream fixes. Each carries the commits/PRs it covers "
        "in its Guards field.",
        ["frappe_filters", "frappe_jinja_sandbox", "frappe_permissions"],
    ),
    "Version 16 Polished (Sep 2026)": (
        "The version-16-polished release of Frappe, ERPNext and HRMS: security fixes checked "
        "as users with different roles, new features, and the upgrade patches. Needs "
        "Allow Customisations on the site; skips on sites not running version-16-polished.",
        [
            "v16p_upgrade_checks",
            "v16p_security",
            "v16p_frappe_features",
            "v16p_erpnext",
            "v16p_hrms_security",
            "v16p_hrms",
        ],
    ),
    "Customisation Layer": (
        "Custom Fields, Property Setters, Client/Server Scripts and a custom DocType — "
        "run against the dedicated customisation site.",
        ["customisations"],
    ),
}


@frappe.whitelist()
def seed_curated_groups() -> int:
    """Create the cross-app groups the weekly release plans are built from.

    Kept separate from ``seed_example_groups`` because that one buckets cases by
    ``required_app``; these deliberately mix apps (a regression group spans frappe
    and erpnext) so they can't come from the same mapping. Idempotent: an existing
    group is left exactly as the user has edited it.
    """
    created = 0
    for group_name, (description, suites) in _CURATED_GROUPS.items():
        if frappe.db.exists("Test Case Group", group_name):
            continue
        cases = [
            name
            for suite in suites
            if (name := frappe.db.get_value("Release Test Case", {"suite": suite}, "name"))
        ]
        if not cases:
            continue
        group = frappe.new_doc("Test Case Group")
        group.group_name = group_name
        group.description = description
        for case in cases:
            group.append("test_cases", {"test_case": case})
        group.insert(ignore_permissions=True)
        created += 1
    return created


@frappe.whitelist()
def run_group(site: str, group: str, trigger: str = "Manual") -> str:
    """Create + queue a Release Test for every Test Case in a group."""
    group_doc = frappe.get_doc("Test Case Group", group)
    run = frappe.new_doc("Release Test")
    run.run_title = f"{group_doc.group_name} — {site}"
    run.site = site
    run.trigger = trigger
    for row in group_doc.test_cases:
        run.append("test_cases", {"test_case": row.test_case})
    run.insert(ignore_permissions=True)
    run_now(run.name)
    frappe.db.commit()
    return run.name


@frappe.whitelist()
def test_connection(site: str) -> dict:
    """Detect installed apps + versions for a Testing Site and store the result."""
    from release_tests import api as engine

    doc = frappe.get_doc("Testing Site", site)
    try:
        versions = engine.detect_site(_site_config(doc))
        summary = "OK — " + ", ".join(
            f"{a} {v.get('version') or v.get('branch') or '?'}" for a, v in sorted(versions.items())
        )
        doc.db_set("last_status", summary)
        doc.db_set("last_detected_apps", json.dumps(versions, indent=1))
        frappe.db.commit()
        return {"ok": True, "status": summary}
    except Exception as exc:  # noqa: BLE001
        doc.db_set("last_status", f"FAILED — {exc}")
        frappe.db.commit()
        return {"ok": False, "error": str(exc)}


# ------------------------------------------------------------------- run a test
def mark_run_failed(job, connection, type, value, traceback) -> None:  # noqa: A002
    """RQ failure callback — stop a crashed job leaving its run stuck at Queued.

    ``execute_run`` only sets status="Running" *after* ``frappe.get_doc`` succeeds,
    so anything that breaks earlier leaves the Release Test showing "Queued"
    forever. Frappe's own ``log_error`` fallback can't help there: it needs a DB
    query too, so a broken worker fails inside the error handler as well and no
    Error Log is ever written (seen 2026-08-25, stale worker holding pre-upgrade
    modules after a frappe version bump). Recording the traceback on the run
    itself is the only place guaranteed to be reachable.
    """
    from frappe.utils.background_jobs import truncate_failed_registry

    run = (job.kwargs or {}).get("run")
    try:
        if run and frappe.db and frappe.db.exists("Release Test", run):
            detail = "".join(format_exception(type, value, traceback))
            frappe.db.set_value(
                "Release Test",
                run,
                {
                    "status": "Failed",
                    "finished_at": now_datetime(),
                    "log": f"Background job failed:\n\n{detail}"[:5000],
                },
                update_modified=False,
            )
            frappe.db.commit()
    except Exception:  # noqa: BLE001 - a failure handler must never itself fail
        pass
    # Preserve frappe's default behaviour, which we displace by passing on_failure.
    truncate_failed_registry(job, connection, type, value, traceback)


@frappe.whitelist()
def run_now(run: str) -> bool:
    """Queue execution of a Release Test in the background."""
    doc = frappe.get_doc("Release Test", run)
    doc.db_set("status", "Queued")
    frappe.db.commit()
    frappe.enqueue(
        "release_manager.api.execute_run",
        queue="long",
        timeout=1500,
        run=run,
        enqueue_after_commit=True,
        on_failure=mark_run_failed,
    )
    return True


def execute_run(run: str) -> None:
    """Run selected suites for a Release Test's site, recording each suite live.

    Streams via ``engine.iter_run_site`` so every suite's Test Result + the run
    log/counters are committed the moment that suite finishes — the RunDetail
    poller then ticks the queue green and grows the log in real time.
    """
    from release_tests import api as engine

    doc = frappe.get_doc("Release Test", run)
    doc.db_set("status", "Running")
    doc.db_set("started_at", now_datetime())
    frappe.db.delete("Test Result", {"run": run})  # re-running replaces prior results
    frappe.db.commit()

    site = frappe.get_doc("Testing Site", doc.site)
    suites = _suites_for_run(doc)

    totals = {"pass": 0, "fail": 0, "skip": 0}
    kinds: dict[str, int] = {}  # failures by kind: issue / needs triage / harness
    log_lines: list[str] = []
    any_fail = any_pass = False

    try:
        for event in engine.iter_run_site(_site_config(site), suites=suites):
            etype = event.get("type")
            if etype == "error":
                # Fatal: couldn't connect to / detect the target, so nothing can run.
                doc.db_set("status", "Failed")
                doc.db_set(
                    "log",
                    "\n".join([*log_lines, f"Connection/detection failed: {event['error']}"]).strip(),
                    commit=True,
                )
                doc.db_set("finished_at", now_datetime())
                frappe.db.commit()
                return
            if etype == "versions":
                doc.db_set("detected_apps", json.dumps(event.get("versions", {}), indent=1), commit=True)
                continue
            if etype == "done":
                doc.db_set("transactions_created", event.get("transactions", 0), commit=True)
                continue

            # A suite just finished — record it. Isolate this per suite so that a
            # single bad suite (or a hiccup while saving its result) is logged and
            # the run keeps going through the remaining suites instead of aborting.
            suite = event["suite"]
            try:
                errs = []
                res = frappe.new_doc("Test Result")
                res.run = run
                res.site = doc.site
                res.suite = suite["suite"]
                res.test_case = suite["suite"]
                res.app_version = suite.get("app_version")
                res.status = _STEP_TO_RESULT.get(suite["status"], "Skipped")
                res.duration_ms = sum(st["duration_ms"] for st in suite["steps"])
                for st in suite["steps"]:
                    res.append(
                        "steps",
                        {
                            "suite": st["suite"],
                            "step": st["step"],
                            "status": st["status"],
                            "duration_ms": st["duration_ms"],
                            "error": _step_error(st),
                        },
                    )
                    if st.get("error"):
                        errs.append(f"{st['step']}: {_step_error(st)}")
                res.error = "\n".join(errs)
                if res.status in ("Failed", "Partial"):
                    res.corrective_action = diagnose(res.error) or res.corrective_action
                res.insert(ignore_permissions=True)

                # Only count a suite's steps once its result is safely recorded.
                for st in suite["steps"]:
                    totals[st["status"]] = totals.get(st["status"], 0) + 1
                    if st["status"] == "fail":
                        kind = (st.get("details") or {}).get("kind", "needs triage")
                        kinds[kind] = kinds.get(kind, 0) + 1
                log_lines.append(f"[{res.status}] {suite['suite']} ({suite.get('app_version') or '-'})")
                if suite.get("skip_reason"):
                    log_lines.append(f"    skipped: {suite['skip_reason']}")
                any_fail = any_fail or suite["status"] == "fail"
                any_pass = any_pass or suite["status"] == "pass"
            except Exception as exc:  # noqa: BLE001 - record + carry on to the next suite
                frappe.db.rollback()
                any_fail = True
                log_lines.append(f"[Errored] {suite.get('suite', '?')}: {exc}")

            doc.db_set("total_steps", sum(totals.values()))
            doc.db_set("passed", totals.get("pass", 0))
            doc.db_set("failed", totals.get("fail", 0))
            doc.db_set("skipped", totals.get("skip", 0))
            doc.db_set("log", "\n".join(log_lines), commit=True)  # live commit
    except Exception as exc:  # noqa: BLE001 - the engine stream itself died; finalize what we have
        frappe.db.rollback()
        any_fail = True
        log_lines.append(f"Execution error: {exc}")

    doc.db_set("status", "Failed" if any_fail and not any_pass else "Partial" if any_fail else "Passed")
    doc.db_set("finished_at", now_datetime())
    log_lines.extend(_run_rate_lines(totals, kinds))
    doc.db_set("log", "\n".join(log_lines) or "No applicable suites ran.", commit=True)
    frappe.db.commit()


# -------------------------------------------------------------- UI (Cypress) run
_UI_TESTS_DEFAULT = os.path.expanduser("~/release_tests/ui")
_CY_STATE = {"passed": "pass", "failed": "fail", "pending": "skip", "skipped": "skip"}


def _ui_tests_path() -> str:
    return frappe.conf.get("release_ui_tests_path") or _UI_TESTS_DEFAULT


def _node_binary() -> str:
    for cand in (shutil.which("node"), "/opt/homebrew/bin/node", "/usr/local/bin/node"):
        if cand and os.path.exists(cand):
            return cand
    frappe.throw("Node.js not found — install it or set it on the worker's PATH.")


def _cypress_env(site_doc) -> dict:
    """Forward the site's URL + login credentials to the Cypress specs."""
    env = dict(os.environ)
    env["CYPRESS_BASE_URL"] = site_doc.base_url
    if site_doc.auth_type == "login":
        if site_doc.username:
            env["CYPRESS_ADMIN_USER"] = site_doc.username
        password = site_doc.get_password("password") if site_doc.password else None
        if password:
            env["CYPRESS_ADMIN_PASSWORD"] = password
    return env


@frappe.whitelist()
def run_ui_test(site: str, headed: int = 1) -> str:
    """Create + queue a UI (Cypress) Release Test for the site's Frappe version."""
    site_doc = frappe.get_doc("Testing Site", site)
    version = site_doc.frappe_version or "v16"
    run = frappe.new_doc("Release Test")
    run.site = site
    run.layer = "UI"
    run.trigger = "Manual"
    run.run_title = f"UI Tests ({version}) — {site}"
    run.insert(ignore_permissions=True)
    run.db_set("status", "Queued")
    frappe.enqueue(
        "release_manager.api.execute_ui_run",
        queue="long",
        timeout=1500,
        run=run.name,
        headed=int(headed),
        enqueue_after_commit=True,
        on_failure=mark_run_failed,
    )
    frappe.db.commit()
    return run.name


def execute_ui_run(run: str, headed: int = 1) -> None:
    """Shell out to Cypress for the site's version and record UI Test Results."""
    doc = frappe.get_doc("Release Test", run)
    doc.db_set("status", "Running")
    doc.db_set("started_at", now_datetime())
    frappe.db.delete("Test Result", {"run": run})
    frappe.db.commit()

    site_doc = frappe.get_doc("Testing Site", doc.site)
    version = site_doc.frappe_version or "v16"
    ui_path = _ui_tests_path()
    result_file = os.path.join(tempfile.gettempdir(), f"cy-{run}.json")

    env = _cypress_env(site_doc)
    env.update(
        {"SPEC_DIR": f"cypress/e2e/{version}", "RESULT_FILE": result_file, "HEADED": "1" if int(headed) else "0"}
    )
    doc.db_set(
        "log", f"Launching Cypress ({version}, {'headed' if int(headed) else 'headless'}) in {ui_path}…", commit=True
    )

    try:
        proc = subprocess.run(
            [_node_binary(), "run.js"], cwd=ui_path, env=env, capture_output=True, text=True, timeout=1200
        )
    except Exception as exc:  # noqa: BLE001
        doc.db_set("status", "Failed")
        doc.db_set("log", f"Failed to launch Cypress: {exc}")
        doc.db_set("finished_at", now_datetime())
        frappe.db.commit()
        return

    data = {}
    if os.path.exists(result_file):
        with open(result_file) as handle:
            data = json.load(handle)
    _record_ui_results(doc, version, data, proc)


def _GUI_HINT() -> str:
    return (
        "Cypress could not start a browser. On macOS the bench must be started from a "
        "GUI Terminal session (run `bench start` in Terminal.app), not a headless/SSH "
        "context — Electron needs a window session even in headless mode. Start the bench "
        "there and re-run. (For unattended runs, use the Linux CI workflow.)"
    )


def _record_ui_results(doc, version: str, data: dict, proc) -> None:
    if data.get("error") or (not data.get("specs") and proc.returncode):
        raw = data.get("error") or (proc.stderr or proc.stdout or "Cypress run failed")
        launch_failed = any(
            marker in raw
            for marker in ("Could not find Cypress test run results", "no-sandbox", "release-test")
        )
        message = (_GUI_HINT() + "\n\n---\n" + raw) if launch_failed else raw
        doc.db_set("status", "Failed")
        doc.db_set("log", message[:5000])
        doc.db_set("finished_at", now_datetime())
        frappe.db.commit()
        return

    totals = {"pass": 0, "fail": 0, "skip": 0}
    log_lines: list[str] = []
    any_fail = any_pass = False

    for spec in data.get("specs", []):
        errs = []
        res = frappe.new_doc("Test Result")
        res.run = doc.name
        res.site = doc.site
        res.layer = "UI"
        res.suite = spec.get("spec")
        res.test_case = spec.get("spec")
        res.app_version = version
        spec_fail = spec_pass = False
        duration = 0
        for test in spec.get("tests", []):
            status = _CY_STATE.get(test.get("state"), "skip")
            totals[status] += 1
            duration += int(test.get("duration") or 0)
            res.append(
                "steps",
                {
                    "suite": spec.get("spec"),
                    "step": test.get("title"),
                    "status": status,
                    "duration_ms": int(test.get("duration") or 0),
                    "error": test.get("error"),
                },
            )
            if status == "fail":
                spec_fail = True
                errs.append(f"{test.get('title')}: {test.get('error')}")
            elif status == "pass":
                spec_pass = True
        res.duration_ms = duration
        res.status = "Failed" if spec_fail and not spec_pass else "Partial" if spec_fail else "Passed"
        res.error = "\n".join(e for e in errs if e)
        if res.status in ("Failed", "Partial"):
            res.corrective_action = diagnose(res.error) or res.corrective_action
        res.insert(ignore_permissions=True)
        log_lines.append(f"[{res.status}] {spec.get('spec')}")
        any_fail = any_fail or spec_fail
        any_pass = any_pass or spec_pass
        doc.db_set("log", "\n".join(log_lines), commit=True)

    doc.db_set("total_steps", sum(totals.values()))
    doc.db_set("passed", totals["pass"])
    doc.db_set("failed", totals["fail"])
    doc.db_set("skipped", totals["skip"])
    doc.db_set("status", "Failed" if any_fail and not any_pass else "Partial" if any_fail else "Passed")
    doc.db_set("finished_at", now_datetime())
    videos = [s["video"] for s in data.get("specs", []) if s.get("video")]
    tail = ("\n\nVideos:\n" + "\n".join(videos)) if videos else ""
    doc.db_set("log", ("\n".join(log_lines) or "No specs ran.") + tail)
    frappe.db.commit()


@frappe.whitelist()
def run_progress(run: str) -> dict:
    """A run's planned suites as a live queue, merged with completed results.

    Powers the Frappe-Cloud-style job list: every selected suite shows up front as
    ``Queued``; the next one becomes ``Running`` while the run is in flight; finished
    ones carry their real status + Test Result name so the UI can green-tick them.
    """
    doc = frappe.get_doc("Release Test", run)
    results = {
        r.suite: r
        for r in frappe.get_all(
            "Test Result", filters={"run": run}, fields=["name", "suite", "status", "duration_ms"]
        )
    }
    planned: list[str] = []
    for row in doc.test_cases:
        suite = frappe.db.get_value("Release Test Case", row.test_case, "suite") or row.test_case
        if suite not in planned:
            planned.append(suite)

    # UI runs have no test cases — enumerate the version's Cypress specs so they
    # show up as a queue too (spec relative path matches the Test Result suite).
    if not planned and doc.layer == "UI":
        version = frappe.db.get_value("Testing Site", doc.site, "frappe_version") or "v16"
        spec_dir = os.path.join(_ui_tests_path(), "cypress", "e2e", version)
        if os.path.isdir(spec_dir):
            planned = [f"cypress/e2e/{version}/{f}" for f in sorted(os.listdir(spec_dir)) if f.endswith(".cy.js")]

    running = doc.status in ("Queued", "Running")
    order = planned or sorted(results.keys())
    items, first_pending = [], True
    for suite in order:
        res = results.get(suite)
        if res:
            items.append({"suite": suite, "status": res.status, "duration_ms": res.duration_ms, "result": res.name})
        else:
            status = "Running" if (running and first_pending) else "Queued"
            first_pending = False
            items.append({"suite": suite, "status": status, "duration_ms": None, "result": None})
    seen = {i["suite"] for i in items}
    for suite, res in results.items():
        if suite not in seen:
            items.append({"suite": suite, "status": res.status, "duration_ms": res.duration_ms, "result": res.name})
    return {"run_status": doc.status, "log": doc.log, "items": items}


# ----------------------------------------------------------------- plans + cron
@frappe.whitelist()
def run_plan(plan: str, trigger: str = "Manual") -> list[str]:
    """Create + queue one Release Test per site in a Test Plan."""
    p = frappe.get_doc("Release Test Plan", plan)
    case_links = [c.test_case for c in p.cases]
    runs = []
    for plan_site in p.sites:
        run = frappe.new_doc("Release Test")
        run.run_title = f"{p.plan_name} — {plan_site.site}"
        run.site = plan_site.site
        run.plan = plan
        run.trigger = trigger
        for tc in case_links:
            run.append("test_cases", {"test_case": tc})
        run.insert(ignore_permissions=True)
        run_now(run.name)
        runs.append(run.name)
    frappe.db.commit()
    return runs


@frappe.whitelist()
def create_and_run(site: str, group: str | None = None, test_cases=None, title: str | None = None) -> str:
    """Create + queue a Release Test from a group, an explicit case list, or all applicable.

    Used by the Vue UI's New Run / re-run flows. ``test_cases`` may be a JSON string.
    """
    run = frappe.new_doc("Release Test")
    run.site = site
    run.trigger = "Manual"

    cases: list[str] = []
    if group:
        group_doc = frappe.get_doc("Test Case Group", group)
        cases = [row.test_case for row in group_doc.test_cases]
        run.run_title = title or f"{group_doc.group_name} — {site}"
    elif test_cases:
        cases = json.loads(test_cases) if isinstance(test_cases, str) else list(test_cases)
        run.run_title = title or f"{', '.join(cases)} — {site}"
    else:
        run.run_title = title or f"All applicable — {site}"

    for case in cases:
        run.append("test_cases", {"test_case": case})
    run.insert(ignore_permissions=True)
    run_now(run.name)
    frappe.db.commit()
    return run.name


@frappe.whitelist()
def dashboard_stats() -> dict:
    """Aggregate counters for the Release Manager dashboard."""
    by_status: dict[str, int] = {}
    for row in frappe.db.sql(
        "select status, count(name) as n from `tabTest Result` group by status", as_dict=True
    ):
        by_status[row.status] = row.n
    by_layer: dict[str, int] = {}
    for row in frappe.db.sql(
        "select coalesce(layer,'API') as layer, count(name) as n from `tabTest Result`"
        " where status != 'Skipped' group by layer",
        as_dict=True,
    ):
        by_layer[row.layer] = row.n
    transactions = frappe.db.sql(
        "select coalesce(sum(transactions_created), 0) from `tabRelease Test`"
    )[0][0]
    return {
        "runs": frappe.db.count("Release Test"),
        "tests_run": sum(n for s, n in by_status.items() if s != "Skipped"),
        "transactions": int(transactions or 0),
        "passed": by_status.get("Passed", 0),
        "failed": by_status.get("Failed", 0),
        "skipped": by_status.get("Skipped", 0),
        "partial": by_status.get("Partial", 0),
        "api_tests": by_layer.get("API", 0),
        "ui_tests": by_layer.get("UI", 0),
    }


@frappe.whitelist()
def rerun(run: str) -> str:
    """Clone a run's site + test cases into a fresh Release Test and queue it.

    UI runs re-run through the Cypress path so "Run again" repeats the browser test
    instead of falling back to the API engine.
    """
    src = frappe.get_doc("Release Test", run)
    if src.layer == "UI":
        return run_ui_test(src.site)
    new = frappe.new_doc("Release Test")
    new.site = src.site
    new.run_title = src.run_title
    new.trigger = "Manual"
    for row in src.test_cases:
        new.append("test_cases", {"test_case": row.test_case})
    new.insert(ignore_permissions=True)
    run_now(new.name)
    frappe.db.commit()
    return new.name


def _run_scheduled(frequency: str) -> None:
    plans = frappe.get_all(
        "Release Test Plan", filters={"enabled": 1, "frequency": frequency}, pluck="name"
    )
    for plan in plans:
        run_plan(plan, trigger="Scheduled")


def run_hourly() -> None:
    _run_scheduled("Hourly")


def run_daily() -> None:
    _run_scheduled("Daily")


def run_weekly() -> None:
    _run_scheduled("Weekly")
