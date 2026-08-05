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

# --- Stability hardening (commit a32782d) -----------------------------------
#
# "Stabilize Gemini request lifecycle": request timeouts, stream idle
# detection, and client revival, so a stalled Gemini session fails fast
# instead of blocking the server. This is why the fork runs more reliably
# than upstream. Every symbol below is fork-only -- upstream has no
# equivalent, so an upstream sync that overwrites these files silently
# removes the hardening and the service degrades with no visible error.

require_symbol() {
    local symbol="$1" file="$2" why="$3"
    if grep -q -- "$symbol" "$file"; then
        pass "$symbol in $file"
    else
        fail "$file lost $symbol -- $why"
    fi
}

require_symbol '_process_conversation_with_timeout' app/server/chat.py \
    "input preprocessing can hang forever"
require_symbol '_stream_with_idle_timeout' app/server/chat.py \
    "a stalled stream never fails, holding the connection open"
require_symbol '_send_stream_with_split' app/server/chat.py \
    "oversized payloads are no longer split before sending"
require_symbol 'INPUT_PREPROCESS_TIMEOUT_SECONDS' app/server/chat.py \
    "the preprocessing timeout bound is gone"
require_symbol 'STREAM_CHUNK_HEARTBEAT_SECONDS' app/server/chat.py \
    "chunk heartbeat detection is gone"
require_symbol 'def request_scope' app/services/client.py \
    "in-flight requests are no longer tracked, so close() can cut them off"
require_symbol 'def active_requests' app/services/client.py \
    "the pool cannot tell which clients are busy"
require_symbol 'def mark_unavailable' app/services/client.py \
    "dead clients cannot be taken out of rotation"
require_symbol '_restart_client' app/services/pool.py \
    "dead clients are never revived"
require_symbol '_run_pool_init_in_background' app/main.py \
    "startup blocks on client init instead of serving immediately"

# --- Remote media fetch hardening -------------------------------------------
#
# Also fork-only. This one was written on 2026-05-25 and then lost three days
# later when the next sync branch was assembled by hand -- production has been
# fetching remote media with no SSRF guard, no timeout, no size cap, and a
# generic fingerprint ever since. Restored 2026-08-05.

require_symbol '_validate_remote_url' app/utils/helper.py \
    "remote fetches can be pointed at localhost, private ranges, or cloud metadata"
require_symbol '_is_public_ip' app/utils/helper.py \
    "the SSRF guard cannot classify addresses"
require_symbol 'REMOTE_FETCH_TIMEOUT_SECONDS' app/utils/helper.py \
    "a slow remote host can hang the fetch indefinitely"
require_symbol 'MAX_REMOTE_MEDIA_BYTES' app/utils/helper.py \
    "an oversized remote file can exhaust memory"
require_symbol 'allow_redirects=False' app/utils/helper.py \
    "curl follows redirects itself, so a public URL can bounce into the private network unchecked"
require_symbol 'curl_cffi_fetch_options' app/services/client.py \
    "media fetches stop using the account's own proxy and TLS fingerprint"
require_symbol 'fetch_impersonate' app/server/chat.py \
    "the per-client fingerprint never reaches the fetch path"

echo

if [ "$failed" -ne 0 ]; then
    echo "Custom deltas are missing. An upstream sync most likely dropped them."
    echo "See scripts/check_deltas.sh for what each assertion protects."
    exit 1
fi

echo "All fork custom deltas intact."
