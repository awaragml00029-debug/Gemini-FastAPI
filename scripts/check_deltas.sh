#!/usr/bin/env bash
#
# Guards this fork's custom deltas against silent loss during upstream syncs.
#
# Background: app/server/gems.py was carried on nine branches and then quietly
# vanished when sync/upstream-main-20260527 was hand-assembled. Nobody noticed
# until /v1/gems started 404ing in production. Every assertion below encodes a
# delta that has been lost, or nearly lost, that way.
#
# Run from the repository root. Exits non-zero on the first failure.

set -uo pipefail

cd "$(dirname "$0")/.."

failed=0

fail() {
    printf '  \033[31mFAIL\033[0m  %s\n' "$1"
    failed=1
}

pass() {
    printf '  \033[32mok\033[0m    %s\n' "$1"
}

echo "Checking fork custom deltas..."

# --- Gems: custom Gemini Gems CRUD + model aliasing -------------------------

if [ -f app/server/gems.py ]; then
    pass "app/server/gems.py exists"
else
    fail "app/server/gems.py is missing -- the Gems endpoints were dropped"
fi

if grep -q 'include_router(gems_router' app/main.py; then
    pass "gems_router is registered in app/main.py"
else
    fail "app/main.py does not register gems_router -- /v1/gems will 404"
fi

if grep -q '_resolve_model_and_gem' app/server/chat.py; then
    pass "gems model aliasing (_resolve_model_and_gem) present in chat.py"
else
    fail "chat.py lost _resolve_model_and_gem -- '<model>-gems-<id>' aliases break"
fi

# --- Health: dual-path compatibility ----------------------------------------
#
# Both decorators must sit on the handler itself. Do NOT "fix" a missing
# /v1/health by adding include_router(health_router, prefix="/v1") in main.py --
# that stacks onto the existing decorator and yields a bogus /v1/v1/health.

if grep -q '@router.get("/health"' app/server/health.py; then
    pass "/health route present"
else
    fail "app/server/health.py lost the /health route"
fi

if grep -q '@router.get("/v1/health"' app/server/health.py; then
    pass "/v1/health route present"
else
    fail "app/server/health.py lost the /v1/health compatibility route"
fi

if grep -q 'include_router(health_router, prefix="/v1"' app/main.py; then
    fail "main.py adds a /v1 prefix to health_router -- this creates /v1/v1/health"
else
    pass "health_router is not double-prefixed"
fi

echo

if [ "$failed" -ne 0 ]; then
    echo "Custom deltas are missing. An upstream sync most likely dropped them."
    echo "See scripts/check_deltas.sh for what each assertion protects."
    exit 1
fi

echo "All fork custom deltas intact."
