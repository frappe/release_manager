"""Compare release_tests runs and write the release-check report.

    compare.py failed-suites <run.json>
        print the suites with a failing step, comma separated (for the re-run)

    compare.py report --mode upgrade|fresh --version 16 --out <dir>
        read <dir>/{baseline,candidate,rerun}.json and commits-*.json, classify
        every failed check, write <dir>/summary.md and <dir>/evidence.json,
        append the summary to $GITHUB_STEP_SUMMARY, and exit 1 when the
        candidate has confirmed regressions (upgrade) or failures (fresh).

Classes, for a check that failed on the candidate:
    flaky            failed, then passed when its suite was re-run once
    regression       passed on the released version, fails on the candidate
    already failing  failed on the released version too
    failing          no baseline to compare with (fresh mode, or a new check)
and "fixed" for a check that failed on the released version and now passes.
Harness failures (the test itself broke) are counted, never called regressions.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

KEY = tuple[str, str]


def _load(path: Path) -> dict | None:
    return json.loads(path.read_text()) if path.exists() else None


def _steps(run: dict | None) -> dict[KEY, dict]:
    out: dict[KEY, dict] = {}
    for target in (run or {}).get("targets", []):
        for suite in target.get("suites", []):
            for step in suite.get("steps", []):
                out[(suite["suite"], step["step"])] = step
    return out


def _kind(step: dict) -> str:
    return (step.get("details") or {}).get("kind", "needs triage")


def failed_suites(path: str) -> str:
    run = _load(Path(path))
    names = {s for (s, _), step in _steps(run).items() if step["status"] == "fail"}
    return ",".join(sorted(names))


def classify(baseline: dict | None, candidate: dict, rerun: dict | None) -> dict[str, list]:
    base, cand, again = _steps(baseline), _steps(candidate), _steps(rerun)
    rerun_suites = {s for (s, _) in again}
    out: dict[str, list] = {k: [] for k in ("regression", "failing", "already failing", "flaky", "harness", "fixed")}
    for key, step in cand.items():
        before = base.get(key, {}).get("status")
        if step["status"] != "fail":
            if before == "fail":
                out["fixed"].append((key, step))
            continue
        if key[0] in rerun_suites and again.get(key, {}).get("status") == "pass":
            out["flaky"].append((key, step))
        elif _kind(step) == "harness":
            out["harness"].append((key, step))
        elif baseline is None or before not in ("pass", "fail"):
            out["failing"].append((key, step))
        elif before == "pass":
            out["regression"].append((key, step))
        else:
            out["already failing"].append((key, step))
    return out


def _commits(out_dir: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(out_dir.glob("commits-*.json"))]


def _detail_md(key: KEY, step: dict) -> str:
    d = step.get("details") or {}
    lines = [f"- **{key[0]} · {key[1]}** ({_kind(step)})"]
    for label, field in (("Check", "check"), ("User", "user"), ("Roles", "roles"), ("Expected", "expected"),
                         ("Actual", "actual"), ("Endpoint", "endpoint"), ("Upstream change", "guards")):
        value = d.get(field)
        if value:
            lines.append(f"  - {label}: {', '.join(value) if isinstance(value, list) else value}")
    if not d.get("actual") and step.get("error"):
        lines.append(f"  - Error: {step['error']}")
    return "\n".join(lines)


def report(mode: str, version: str, out_dir: Path) -> int:
    baseline = _load(out_dir / "baseline.json")
    candidate = _load(out_dir / "candidate.json")
    rerun = _load(out_dir / "rerun.json")
    if candidate is None:
        print("no candidate run found", file=sys.stderr)
        return 2
    groups = classify(baseline, candidate, rerun)
    summary = candidate.get("summary", {})
    blocking = groups["regression"] if mode == "upgrade" else groups["failing"]

    if mode == "upgrade":
        title = f"v{version} upgrade: version-{version} → version-{version}-hotfix"
    else:
        title = f"v{version} fresh install: version-{version}-hotfix"
    md = [f"## {title}", ""]
    verdict = f"**{len(blocking)} regression(s)**" if mode == "upgrade" else f"**{len(blocking)} failing check(s)**"
    md.append(
        f"{verdict} · run rate {summary.get('run_rate', 0)}% "
        f"({summary.get('pass', 0) + summary.get('issue', 0)} of {summary.get('total', 0)} checks reached a verdict) · "
        f"{len(groups['flaky'])} flaky · {len(groups['harness'])} broken test(s) · {summary.get('skip', 0)} skipped"
    )
    md.append("")
    md.append("| Phase | App | Branch | Commit |")
    md.append("| --- | --- | --- | --- |")
    for c in _commits(out_dir):
        for app, info in c["apps"].items():
            md.append(f"| {c['phase']} | {app} | {info['branch']} | `{info['commit'][:10]}` |")
    md.append("")
    sections = (
        ("Regressions: passed on the released version, fail on the candidate", "regression"),
        ("Failing checks", "failing"),
        ("Already failing on the released version", "already failing"),
        ("Flaky: failed once, passed on re-run (not counted)", "flaky"),
        ("Broken tests (harness, not the product)", "harness"),
        ("Fixed by the candidate", "fixed"),
    )
    for heading, group in sections:
        items = groups[group]
        if not items:
            continue
        md.append(f"### {heading} ({len(items)})")
        md.append("")
        if group in ("regression", "failing"):
            md.extend(_detail_md(k, s) for k, s in items)
        else:
            md.extend(f"- {k[0]} · {k[1]}" for k, _ in items)
        md.append("")
    text = "\n".join(md) + "\n"
    (out_dir / "summary.md").write_text(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(text)
    print(text)

    evidence = {
        "version": version,
        "mode": mode,
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "run_url": (
            f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
            if os.environ.get("GITHUB_RUN_ID") else None
        ),
        "commits": _commits(out_dir),
        "summary": summary,
        "counts": {g: len(v) for g, v in groups.items()},
        "findings": [
            {"class": g, "suite": k[0], "step": k[1], "kind": _kind(s), "details": s.get("details"), "error": s.get("error")}
            for g in ("regression", "failing", "already failing", "flaky", "harness")
            for k, s in groups[g]
        ],
    }
    (out_dir / "evidence.json").write_text(json.dumps(evidence, indent=2))
    return 1 if blocking else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("failed-suites")
    f.add_argument("run")
    r = sub.add_parser("report")
    r.add_argument("--mode", choices=("upgrade", "fresh"), required=True)
    r.add_argument("--version", required=True)
    r.add_argument("--out", required=True)
    args = p.parse_args(argv)
    if args.cmd == "failed-suites":
        print(failed_suites(args.run))
        return 0
    return report(args.mode, args.version, Path(args.out))


if __name__ == "__main__":
    sys.exit(main())
