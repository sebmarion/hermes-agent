#!/usr/bin/env bash
# Canonical test runner for hermes-agent. Run this instead of calling
# `pytest` directly to guarantee your local run matches CI behavior.
#
# What this script enforces:
#   * Per-file isolation via scripts/run_tests_parallel.py — each test
#     file runs in its own freshly-spawned `python -m pytest <file>`
#     subprocess. No xdist, no shared workers, no module-level leakage
#     between files.
#   * TZ=UTC, LANG=C.UTF-8, PYTHONHASHSEED=0 (deterministic)
#   * Env vars blanked (conftest.py also does this, but this
#     is belt-and-suspenders for anyone running pytest outside our
#     conftest path — e.g. on a single file)
#   * Proper venv activation (probes .venv, venv, then ~/.hermes/...)
#
# Usage:
#   scripts/run_tests.sh                            # full suite
#   scripts/run_tests.sh -j 4                       # cap parallelism
#   scripts/run_tests.sh tests/agent/               # discover only here
#   scripts/run_tests.sh tests/agent/ tests/acp/    # multiple roots
#   scripts/run_tests.sh tests/foo.py               # single file
#   scripts/run_tests.sh tests/foo.py -q            # path + bare pytest flag
#   scripts/run_tests.sh tests/foo.py -v --tb=long  # bare flags "just work"
#   scripts/run_tests.sh -k 'pattern'               # value flags pass through too
#   scripts/run_tests.sh tests/foo.py -- --tb=long  # explicit '--' still works
#
# Bare pytest flags (anything starting with '-' that isn't one of this
# runner's own options: -j/--jobs, --paths, --slice, --file-timeout, etc.)
# are forwarded to each per-file pytest invocation automatically — no '--'
# separator required. The explicit '--' form still works and stacks with
# bare flags. Positional path arguments override the default discovery
# root (tests/).

set -euo pipefail

# ── Locate repo root ────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CALLER_HOME="${HOME:-}"

# ── Activate venv ───────────────────────────────────────────────────────────
VENV=""
for candidate in \
  "${VIRTUAL_ENV:-}" \
  "$REPO_ROOT/.venv" \
  "$REPO_ROOT/venv" \
  "${CALLER_HOME:+$CALLER_HOME/.hermes/hermes-agent/venv}"
do
  [ -n "$candidate" ] || continue
  if [ -f "$candidate/bin/activate" ]; then
    VENV="$candidate"
    break
  fi
done

if [ -z "$VENV" ]; then
  echo "error: no usable virtualenv found in VIRTUAL_ENV, $REPO_ROOT/.venv, $REPO_ROOT/venv, or caller HOME" >&2
  exit 1
fi

PYTHON="$VENV/bin/python"

# ── Private test HOME and state roots ───────────────────────────────────────
# Resolve the interpreter first, then remove the caller's HOME and state paths
# from the test environment.  Collection-time Path.home() calls therefore also
# resolve inside this runner-owned root.
umask 077
TEST_TMP_BASE="${TMPDIR:-/tmp}"
mkdir -p "$TEST_TMP_BASE"
TEST_PRIVATE_ROOT="$(mktemp -d "$TEST_TMP_BASE/hermes-agent-tests.XXXXXX")"
TEST_PRIVATE_HOME="$TEST_PRIVATE_ROOT/home"
TEST_PRIVATE_HERMES_HOME="$TEST_PRIVATE_HOME/.hermes"
TEST_PRIVATE_TMP="$TEST_PRIVATE_ROOT/tmp"
TEST_PRIVATE_XDG_CONFIG="$TEST_PRIVATE_ROOT/xdg-config"
TEST_PRIVATE_XDG_CACHE="$TEST_PRIVATE_ROOT/xdg-cache"
TEST_PRIVATE_XDG_DATA="$TEST_PRIVATE_ROOT/xdg-data"
TEST_PRIVATE_XDG_STATE="$TEST_PRIVATE_ROOT/xdg-state"
ISOLATION_SELFTEST="${HERMES_TEST_ISOLATION_SELFTEST:-}"
ISOLATION_CONTROL_DIR=""

mkdir -p \
  "$TEST_PRIVATE_HERMES_HOME" \
  "$TEST_PRIVATE_TMP" \
  "$TEST_PRIVATE_XDG_CONFIG" \
  "$TEST_PRIVATE_XDG_CACHE" \
  "$TEST_PRIVATE_XDG_DATA" \
  "$TEST_PRIVATE_XDG_STATE"

if [ "$ISOLATION_SELFTEST" = "1" ]; then
  ISOLATION_CONTROL_DIR="$TEST_PRIVATE_ROOT/isolation-control"
  mkdir -p "$ISOLATION_CONTROL_DIR"
fi

cleanup_private_root() {
  if [ -z "${TEST_PRIVATE_ROOT:-}" ] || [ ! -d "$TEST_PRIVATE_ROOT" ]; then
    return
  fi
  case "$(basename "$TEST_PRIVATE_ROOT")" in
    hermes-agent-tests.*)
      rm -rf -- "$TEST_PRIVATE_ROOT"
      ;;
    *)
      echo "error: refusing to remove unexpected test root: $TEST_PRIVATE_ROOT" >&2
      ;;
  esac
}

trap cleanup_private_root EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# ── Live-gateway plugin (computed before we drop env) ───────────────────────
EXTRA_PYTHONPATH=""
EXTRA_PYTEST_PLUGINS=""
if [ -n "$CALLER_HOME" ] && [ -f "$CALLER_HOME/.hermes/pytest_live_guard.py" ]; then
  cp "$CALLER_HOME/.hermes/pytest_live_guard.py" \
    "$TEST_PRIVATE_HERMES_HOME/pytest_live_guard.py"
  EXTRA_PYTHONPATH="$TEST_PRIVATE_HERMES_HOME"
  EXTRA_PYTEST_PLUGINS="pytest_live_guard"
fi


# ── Run in hermetic env ──────────────────────────────────────────────────────
# env -i: start with empty environment, opt-in only what we need.
# No credential var can leak — you'd have to explicitly add it here.
echo "▶ running per-file parallel test suite via run_tests_parallel.py"
echo "  (private HOME/state; TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0; clean env)"

cd "$REPO_ROOT"

set +e
env -i \
  PATH="$PATH" \
  HOME="$TEST_PRIVATE_HOME" \
  HERMES_HOME="$TEST_PRIVATE_HERMES_HOME" \
  XDG_CONFIG_HOME="$TEST_PRIVATE_XDG_CONFIG" \
  XDG_CACHE_HOME="$TEST_PRIVATE_XDG_CACHE" \
  XDG_DATA_HOME="$TEST_PRIVATE_XDG_DATA" \
  XDG_STATE_HOME="$TEST_PRIVATE_XDG_STATE" \
  TMPDIR="$TEST_PRIVATE_TMP" \
  VIRTUAL_ENV="$VENV" \
  TZ=UTC \
  LANG=C.UTF-8 \
  LC_ALL=C.UTF-8 \
  PYTHONHASHSEED=0 \
  PYTHONDONTWRITEBYTECODE=1 \
  ${HERMES_RUN_SLOW_PET_TESTS:+HERMES_RUN_SLOW_PET_TESTS="$HERMES_RUN_SLOW_PET_TESTS"} \
  ${ISOLATION_SELFTEST:+HERMES_TEST_ISOLATION_SELFTEST="$ISOLATION_SELFTEST"} \
  ${ISOLATION_CONTROL_DIR:+HERMES_TEST_ISOLATION_CONTROL_DIR="$ISOLATION_CONTROL_DIR"} \
  ${EXTRA_PYTHONPATH:+PYTHONPATH="$EXTRA_PYTHONPATH"} \
  ${EXTRA_PYTEST_PLUGINS:+PYTEST_PLUGINS="$EXTRA_PYTEST_PLUGINS"} \
  "$PYTHON" "$SCRIPT_DIR/run_tests_parallel.py" "$@"
TEST_STATUS=$?
set -e
exit "$TEST_STATUS"
