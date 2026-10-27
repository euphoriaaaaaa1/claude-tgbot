#!/usr/bin/env bash
# T22 public release double gate.
#
#   gate 1: public H acceptance tests (one bun process per test file)
#   gate 2: target repo Python unit + acceptance tests + import smoke
#
# Always runs against a throw-away HOME/TMPDIR and DISPATCHER_PORT=0 so it
# never touches a real dispatcher, Telegram, upstream model or dsh.
# Known-failure baseline: tests/gate-baseline.txt
#
# Usage: scripts/test-gate.sh [--python /path/to/python] [--bun /path/to/bun]
# Exit: 0 all green (or only baseline failures), 1 new failures, 2 setup error.
set -uo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT" || exit 2
PYTHON=${PYTHON:-python3}
BUN=${BUN:-bun}
BASELINE=${GATE_BASELINE:-"$ROOT/tests/gate-baseline.txt"}

while [ $# -gt 0 ]; do
  case "$1" in
    --python) PYTHON=$2; shift 2 ;;
    --bun) BUN=$2; shift 2 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "python not found: $PYTHON" >&2; exit 2
fi
if ! command -v "$BUN" >/dev/null 2>&1; then
  echo "bun not found: $BUN" >&2; exit 2
fi
if ! "$PYTHON" -c 'import pytest' >/dev/null 2>&1; then
  echo "pytest not installed for $PYTHON" >&2; exit 2
fi

OUT=${GATE_OUT:-$(mktemp -d "${TMPDIR:-/tmp}/t22-gate.XXXXXX")}
mkdir -p "$OUT/logs" "$OUT/h"
cleanup() {
  if [ "${GATE_KEEP_OUT:-0}" = "1" ]; then
    echo "gate output kept at $OUT"
  else
    rm -rf -- "$OUT"
  fi
}
trap cleanup EXIT

# 记录仓库根已有的 state.db*，跑完对比，抓出绕过 CLAUDEBOT_DB_PATH 的写入。
state_db_snapshot() {
  ( cd "$ROOT" || exit 1
    for f in state.db*; do
      [ -e "$f" ] && printf '%s\n' "$f"
    done )
}
STATE_DB_BEFORE="$OUT/state-db-before.txt"
state_db_snapshot | sort > "$STATE_DB_BEFORE"

# Isolation: never inherit a real HOME, channel dir or dispatcher port.
export HOME="$OUT/home"
export TMPDIR="$OUT/tmp"
export DISPATCHER_PORT=0
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
# 状态库也放进一次性目录：任何测试都不许在仓库根留下 state.db*。
export CLAUDEBOT_DB_PATH="$OUT/state.db"
mkdir -p "$HOME" "$TMPDIR"
unset HUB_CONFIGS_DIR HUB_ENV_FILE HUB_CHANNELS_DIR HUB_TELEGRAM_API_BASE \
      CLAUDE_SETTINGS_PATH CLAUDEBOT_CONFIG_DIR CLAUDEBOTLIFE_TEST_ROOT 2>/dev/null || true

CURRENT="$OUT/failures-current.txt"
: > "$CURRENT"

record_pytest_failures() {
  local log=$1
  grep -E '^FAILED |^ERROR ' "$log" 2>/dev/null \
    | sed -E 's/^(FAILED|ERROR) //; s/ - .*$//' \
    | sed 's/^/pytest:/' >> "$CURRENT"
}

run_pytest() {
  local label=$1; shift
  local log="$OUT/logs/$label.log"
  local before
  before=$(wc -l < "$CURRENT")
  echo "== python $label =="
  "$PYTHON" -m pytest "$@" -q -rfE -p no:cacheprovider \
    --junitxml="$OUT/logs/$label.xml" >"$log" 2>&1
  local rc=$?
  tail -n 2 "$log" | sed 's/^/   /'
  record_pytest_failures "$log"
  if [ $rc -ne 0 ] && [ "$(wc -l < "$CURRENT")" -eq "$before" ]; then
    echo "pytest:$label::process-exit-$rc" >> "$CURRENT"
  fi
  return $rc
}

run_pytest unit tests/unit
UNIT_RC=$?
run_pytest acceptance tests/acceptance
ACC_RC=$?

echo "== dependency import smoke =="
"$PYTHON" "$ROOT/scripts/import_smoke.py" >"$OUT/logs/import-smoke.log" 2>&1
SMOKE_RC=$?
tail -n 1 "$OUT/logs/import-smoke.log" | sed 's/^/   /'
if [ $SMOKE_RC -ne 0 ]; then
  if grep -q '^FAIL import ' "$OUT/logs/import-smoke.log"; then
    grep '^FAIL import ' "$OUT/logs/import-smoke.log" | sed 's/^FAIL import /import:/' >> "$CURRENT"
  else
    echo "import::process-exit-$SMOKE_RC" >> "$CURRENT"
  fi
fi

# Bun test files share process.env at module load, so batching them silently
# corrupts CHANNEL_DIR/BOT_NAME. Every file gets its own process.
run_bun_file() {
  local prefix=$1 f=$2 rel=$3
  local log="$OUT/h/$(printf '%s' "$rel" | tr '/' '_').log"
  if [ "$(basename "$f")" = "h17_budget.test.ts" ]; then
    echo "== $prefix $rel (slow, serial) =="
  else
    echo "== $prefix $rel =="
  fi
  "$BUN" test "$f" --timeout 120000 >"$log" 2>&1
  local rc=$?
  # 时序敏感用例（20 秒级超时）在整仓并发下会偶发失败：失败后单独重跑一次，
  # 第二次通过就按 flaky 记日志、不算新增失败。
  if [ $rc -ne 0 ]; then
    echo "   retry once (timing-sensitive)" >&2
    "$BUN" test "$f" --timeout 120000 >"$log.retry" 2>&1
    local rc2=$?
    if [ $rc2 -eq 0 ]; then
      echo "   flaky: passed on retry -> ${log%/*}/$(basename "$log").retry" >&2
      rc=0
    else
      mv "$log.retry" "$log"
      rc=$rc2
    fi
  fi
  if [ $rc -ne 0 ]; then
    grep -E '^\(fail\) ' "$log" 2>/dev/null \
      | sed -E 's/^\(fail\) //; s/ \[[0-9.]+ms\]$//' \
      | sed "s|^|$prefix:$rel::|" >> "$CURRENT"
    if ! grep -qE '^\(fail\) ' "$log"; then
      echo "$prefix:$rel::process-exit-$rc" >> "$CURRENT"
    fi
  fi
  tail -n 3 "$log" | sed 's/^/   /'
  return $rc
}

# gate 1: public H acceptance tests
H_RC=0
while IFS= read -r f; do
  run_bun_file bun "$f" "${f#"$ROOT"/}" || H_RC=1
done < <(find "$ROOT/tests/acceptance" -name '*.test.ts' -type f | sort)

# gate 2 adds the target repo's own dispatcher unit tests
DSP_RC=0
while IFS= read -r f; do
  run_bun_file bun-unit "$f" "${f#"$ROOT"/}" || DSP_RC=1
done < <(find "$ROOT/dispatcher" -name '*.test.ts' -type f \
           -not -path '*/node_modules/*' | sort)

sort -u "$CURRENT" -o "$CURRENT"

BASELINE_CLEAN="$OUT/baseline-clean.txt"
if [ -f "$BASELINE" ]; then
  grep -vE '^\s*(#|$)' "$BASELINE" | sed -E 's/[[:space:]]+$//' | sort -u > "$BASELINE_CLEAN"
else
  echo "warning: baseline not found: $BASELINE" >&2
  : > "$BASELINE_CLEAN"
fi
comm -13 "$BASELINE_CLEAN" "$CURRENT" > "$OUT/new-failures.txt"
comm -23 "$BASELINE_CLEAN" "$CURRENT" > "$OUT/fixed-failures.txt"

echo
echo "== gate summary =="
echo "unit=$([ $UNIT_RC -eq 0 ] && echo PASS || echo FAIL) acceptance=$([ $ACC_RC -eq 0 ] && echo PASS || echo FAIL) import-smoke=$([ $SMOKE_RC -eq 0 ] && echo PASS || echo FAIL) h-tests=$([ $H_RC -eq 0 ] && echo PASS || echo FAIL) dispatcher-unit=$([ $DSP_RC -eq 0 ] && echo PASS || echo FAIL)"
echo "failures: $(wc -l < "$CURRENT" | tr -d ' ') (baseline $(wc -l < "$BASELINE_CLEAN" | tr -d ' '))"
if [ -s "$OUT/new-failures.txt" ]; then
  echo "NEW failures vs baseline:"
  sed 's/^/  + /' "$OUT/new-failures.txt"
fi
if [ -s "$OUT/fixed-failures.txt" ]; then
  echo "baseline entries now passing:"
  sed 's/^/  - /' "$OUT/fixed-failures.txt"
fi
# Only failures outside the baseline fail the gate. A suite crash without a
# parseable failure line is recorded above as process-exit-<code>, so it cannot
# slip through as "all baseline".
if [ -s "$OUT/new-failures.txt" ]; then
  exit 1
fi

# 仓库根不许新增 state.db*（CLAUDEBOT_DB_PATH 没接住的写入）
STATE_DB_AFTER="$OUT/state-db-after.txt"
state_db_snapshot | sort > "$STATE_DB_AFTER"
STATE_DB_NEW="$OUT/state-db-new.txt"
comm -13 "$STATE_DB_BEFORE" "$STATE_DB_AFTER" > "$STATE_DB_NEW"
if [ -s "$STATE_DB_NEW" ]; then
  echo "NEW state.db* files in repo root:"
  sed 's/^/  + /' "$STATE_DB_NEW"
  exit 1
fi
exit 0
