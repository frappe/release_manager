#!/usr/bin/env bash
# Cross-app release check: frappe + erpnext + hrms + india_compliance tested together.
#
#   ci/release_check.sh <15|16> <upgrade|fresh>
#
# upgrade: install the released branch (version-N), run the suites (baseline),
#          upgrade to the candidate (version-N-hotfix) the way a customer's
#          update does (switch code, install requirements, migrate), run again.
#          A step that passed on the baseline and fails on the candidate is a
#          regression.
# fresh:   install the candidate directly and run the suites.
#
# Failing suites are re-run once; a failure that passes on re-run is "flaky".
# Results land in $OUT_DIR. Expects: python, node + yarn, redis-server and
# mariadb (root password in $DB_ROOT_PASSWORD) on the machine, and the
# release_tests engine checked out at $ENGINE_DIR.
set -euo pipefail

VERSION=${1:?version: 15 or 16}
MODE=${2:?mode: upgrade or fresh}
WS=${GITHUB_WORKSPACE:-$(pwd)}
ENGINE_DIR=${ENGINE_DIR:-$WS/engine}
OUT_DIR=${OUT_DIR:-$WS/out}
BENCH_DIR=${BENCH_DIR:-$HOME/frappe-bench}
SITE=${SITE:-ci.localhost}
DB_ROOT_PASSWORD=${DB_ROOT_PASSWORD:-root}
export RT_ADMIN_PW=${RT_ADMIN_PW:-admin}

STABLE=version-$VERSION
CANDIDATE=version-$VERSION-hotfix
if [ "$MODE" = upgrade ]; then START=$STABLE; else START=$CANDIDATE; fi
APPS=(frappe erpnext hrms india_compliance)
LABEL=ci-v$VERSION-$MODE

mkdir -p "$OUT_DIR"
log() { echo "::group::$*"; }
endlog() { echo "::endgroup::"; }

# --- bench + site ---------------------------------------------------------
log "bench init ($START)"
bench init --skip-assets --frappe-branch "$START" --python "$(which python)" "$BENCH_DIR"
cd "$BENCH_DIR"
sed -i 's/^watch:/# watch:/' Procfile
endlog

log "get apps ($START)"
bench get-app --skip-assets --branch "$START" erpnext https://github.com/frappe/erpnext
bench get-app --skip-assets --branch "$START" hrms https://github.com/frappe/hrms
bench get-app --skip-assets --branch "$START" india_compliance https://github.com/resilient-tech/india-compliance
endlog

log "new site"
bench new-site "$SITE" --db-root-username root --db-root-password "$DB_ROOT_PASSWORD" \
  --admin-password "$RT_ADMIN_PW" \
  --install-app erpnext --install-app hrms --install-app india_compliance
bench --site "$SITE" set-config allow_tests true
# Finish the setup wizard with an Indian company so India Compliance is active.
fy_start=$(python - <<'PY'
import datetime as d
t = d.date.today()
y = t.year if t.month >= 4 else t.year - 1
print(f"{y}-04-01|{y + 1}-03-31")
PY
)
bench --site "$SITE" execute frappe.desk.page.setup_wizard.setup_wizard.setup_complete --kwargs "$(python - "$fy_start" <<'PY'
import json, sys
start, end = sys.argv[1].split("|")
print(json.dumps({"args": {
    "language": "English", "country": "India", "timezone": "Asia/Kolkata", "currency": "INR",
    "full_name": "CI Admin", "email": "ci-admin@example.com", "password": "ci-admin-pw",
    "company_name": "RT CI Pvt Ltd", "company_abbr": "RTCI", "chart_of_accounts": "Standard",
    "fy_start_date": start, "fy_end_date": end, "setup_demo": 0,
}}))
PY
)"
endlog

start_bench() {
  # own process group, so stop_bench can stop web, workers and redis together
  setsid bench start > "$OUT_DIR/bench-start-$1.log" 2>&1 &
  BENCH_PGID=$!
  for _ in $(seq 1 120); do
    if curl -sf -H "Host: $SITE" http://127.0.0.1:8000/api/method/ping >/dev/null; then return 0; fi
    sleep 2
  done
  echo "site did not come up"; tail -50 "$OUT_DIR/bench-start-$1.log"; return 1
}

stop_bench() {
  kill -INT -- "-$BENCH_PGID" 2>/dev/null || true
  sleep 5
  kill -KILL -- "-$BENCH_PGID" 2>/dev/null || true
  sleep 2
}

record_commits() {
  python - "$1" "${APPS[@]}" <<'PY' > "$OUT_DIR/commits-$1.json"
import json, subprocess, sys
phase, apps = sys.argv[1], sys.argv[2:]
out = {"phase": phase, "apps": {}}
for app in apps:
    git = lambda *a: subprocess.run(["git", "-C", f"apps/{app}", *a], capture_output=True, text=True).stdout.strip()
    out["apps"][app] = {"commit": git("rev-parse", "HEAD"), "branch": git("rev-parse", "--abbrev-ref", "HEAD")}
print(json.dumps(out, indent=2))
PY
  cat "$OUT_DIR/commits-$1.json"
}

# --- engine ---------------------------------------------------------------
log "engine"
python -m venv "$OUT_DIR/.engine"
"$OUT_DIR/.engine/bin/pip" install -q -e "$ENGINE_DIR"
cat > "$OUT_DIR/targets.toml" <<EOF
[[target]]
label = "$LABEL"
url = "http://127.0.0.1:8000"
auth = "login"
username = "Administrator"
password = "\$RT_ADMIN_PW"
host_header = "$SITE"
allow_customisations = true
EOF
endlog

# run_suites <phase> [suite-list]: writes $OUT_DIR/<phase>.json
run_suites() {
  local phase=$1 suites=${2:-}
  local dir="$OUT_DIR/runs/$phase"
  mkdir -p "$dir"
  local args=(run --config "$OUT_DIR/targets.toml" --target "$LABEL" --continue-on-fail --results-dir "$dir")
  [ -n "$suites" ] && args+=(--suite "$suites")
  log "suites: $phase"
  "$OUT_DIR/.engine/bin/release-tests" "${args[@]}" || true
  endlog
  cp "$(ls -t "$dir"/release-*.json | grep -v failures | head -1)" "$OUT_DIR/$phase.json"
}

# --- runs -----------------------------------------------------------------
start_bench "$START"
record_commits "$START"

if [ "$MODE" = upgrade ]; then
  run_suites baseline
  stop_bench

  log "upgrade to $CANDIDATE"
  for app in "${APPS[@]}"; do
    git -C "apps/$app" fetch --depth 1 upstream "$CANDIDATE"
    git -C "apps/$app" checkout -f -B "$CANDIDATE" FETCH_HEAD
  done
  bench setup requirements --python
  bench --site "$SITE" migrate
  endlog

  start_bench "$CANDIDATE"
  record_commits "$CANDIDATE"
fi

run_suites candidate
failed=$(python "$WS/ci/compare.py" failed-suites "$OUT_DIR/candidate.json")
if [ -n "$failed" ]; then
  run_suites rerun "$failed"
fi
stop_bench

python "$WS/ci/compare.py" report --mode "$MODE" --version "$VERSION" --out "$OUT_DIR"
