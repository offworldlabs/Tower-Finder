#!/bin/bash
# bash (not POSIX sh) is used for `wait -n` in the supervisor loop below, which
# lets us block on BOTH nginx and uvicorn at once and react to whichever exits
# first. bash 5.2 ships in the image; everything else here stays POSIX-simple.
set -e

# Tunable via env vars (docker-compose or .env)
# NOTE: workers MUST stay at 1 — the app uses in-memory shared state and a TCP
# server bound to a single port.  Concurrency is handled by FRAME_WORKERS
# threads inside the single process.
FRAME_WORKERS="${FRAME_WORKERS:-6}"
FRAME_QUEUE_SIZE="${FRAME_QUEUE_SIZE:-10000}"

export FRAME_WORKERS
export FRAME_QUEUE_SIZE

# Ensure the image-fresh constants.py is always imported instead of the
# potentially-stale copy inside the /app/backend/config named volume.
# We store the pristine copy at /app/deploy/config-image/config/constants.py
# (outside the volume) and prepend its parent to PYTHONPATH so Python finds
# it first via namespace-package merging, regardless of whether the cp below
# succeeds.  This means the app always runs with the correct constants even
# on servers where the volume is still root-owned.
export PYTHONPATH="/app/deploy/config-image${PYTHONPATH:+:$PYTHONPATH}"

# Also attempt an in-place refresh of the volume copy so that the next
# restart (or any tool that reads the file directly) also sees the new
# version.  Non-fatal: the PYTHONPATH override above guarantees correctness
# even when this fails.
if [ -f /app/deploy/config-image/config/constants.py ]; then
    cp /app/deploy/config-image/config/constants.py /app/backend/config/constants.py 2>/dev/null \
        || echo "[start.sh] Info: could not refresh volume constants.py (volume may be root-owned); PYTHONPATH override is active"
fi

# >>> nginx-profile-select >>>
# Copy in the nginx config for the active profile. NGINX_PROFILE defaults to
# RETINA_ENV (staging -> nginx-staging.conf, test -> nginx-test.conf) but stays a
# separate knob, so nginx can be swapped without changing RETINA_ENV, which
# drives the app's own auth gating. Paths are parameterised (APP_ROOT /
# NGINX_TARGET) so the block is unit-testable outside the container.
: "${APP_ROOT:=/app}"
: "${NGINX_TARGET:=/etc/nginx/sites-available/default}"
NGINX_PROFILE="${NGINX_PROFILE:-${RETINA_ENV:-}}"
if [ -n "${NGINX_PROFILE}" ]; then
    if [ -f "${APP_ROOT}/deploy/nginx-${NGINX_PROFILE}.conf" ]; then
        echo "[start.sh] Using nginx-${NGINX_PROFILE}.conf (profile=${NGINX_PROFILE})"
        cp "${APP_ROOT}/deploy/nginx-${NGINX_PROFILE}.conf" "${NGINX_TARGET}"
    else
        # A named profile with no matching config is a misconfiguration. Fail
        # closed rather than fall through to the baked default (the production
        # nginx.conf): a non-prod environment must never be served with prod
        # server_names, TLS and security headers. An empty profile (production)
        # skips this block and keeps the baked default.
        echo "[start.sh] ERROR: NGINX_PROFILE=${NGINX_PROFILE} but ${APP_ROOT}/deploy/nginx-${NGINX_PROFILE}.conf is missing; refusing to fall back to the baked production config" >&2
        exit 1
    fi
fi
# <<< nginx-profile-select <<<

# ── uvicorn supervision: exit on a persistent fast crash-loop ────────────────
# The fast in-process restart below absorbs *transient* uvicorn crashes without
# recreating the whole container. But if uvicorn crash-loops persistently the
# in-process loop alone would spin forever while nginx keeps the container
# "up" — Docker would mark it `unhealthy` yet never restart it, because
# `restart: unless-stopped` acts on container EXIT, not health. So we count
# consecutive *fast* failures and, past a threshold, tear down nginx and exit
# non-zero so Docker recreates the container and clears the wedged backend.
#
# T (MIN_UPTIME_S): a uvicorn run that stayed up at least this long before
#   dying is treated as a transient crash — the counter resets. 60s is chosen
#   to sit safely BELOW the compose healthcheck `start_period` (90s in both
#   docker-compose.yml and docker-compose.staging.yml). A slow-but-fine cold
#   boot keeps uvicorn *running* (it doesn't exit), so its run length grows
#   past 60s and can never be scored as a fast failure — this logic never
#   fights the healthcheck grace window.
# N (MAX_FAST_FAILURES): give up after this many *consecutive* sub-T crashes.
#   5 x (crash + 2s backoff) means at least ~10s of real thrashing before we
#   hand off to Docker — long enough to ride out a genuine transient, few
#   enough to recover a broken backend promptly without a restart storm.
MIN_UPTIME_S=60
MAX_FAST_FAILURES=5
FAST_FAILURES=0

cd /app/backend

# nginx runs in the foreground mode it always has (`daemon off;`, non-forking)
# but backgrounded here so the supervisor loop below is the controlling
# foreground process and can decide to stop nginx and exit. pid already set in
# /etc/nginx/nginx.conf.
nginx -g "daemon off;" &
NGINX_PID=$!

# Tracked so cleanup can stop children, and so a shutdown-driven uvicorn exit
# is never miscounted as a crash (see SHUTTING_DOWN below).
UVICORN_PID=""
SHUTTING_DOWN=0

# Stop both children on any exit path (normal shutdown, INT, or our own exit 1).
cleanup() {
  SHUTTING_DOWN=1
  if [ -n "$NGINX_PID" ]; then
    kill "$NGINX_PID" 2>/dev/null || true
  fi
  if [ -n "$UVICORN_PID" ]; then
    kill "$UVICORN_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT TERM INT

while true; do
  # If nginx died on its own, don't keep the container up serving nothing —
  # exit so Docker recreates it (mirrors the old nginx-foreground behaviour).
  if ! kill -0 "$NGINX_PID" 2>/dev/null; then
    echo "[supervisor] nginx is no longer running; exiting so Docker recreates the container"
    exit 1
  fi

  echo "[supervisor] starting uvicorn on ${UVICORN_HOST:-127.0.0.1}:8000..."
  # Default 127.0.0.1: only nginx (same container) reaches the app; port 8000
  # is never published to the host. Staging sets UVICORN_HOST=0.0.0.0 so the
  # fleet container can POST ground-truth/ADS-B to the API over the compose
  # network (8000 stays unpublished, so it is still not exposed off-host).
  START=$(date +%s)
  # Backgrounded + `wait` so our TERM/INT trap can interrupt promptly on
  # container stop (a plain foreground command would defer the trap).
  uvicorn main:app --host "${UVICORN_HOST:-127.0.0.1}" --port 8000 --workers 1 --log-level warning &
  UVICORN_PID=$!
  EXIT_CODE=0
  # Block until EITHER child exits, not just uvicorn. Waiting only on uvicorn
  # let nginx die unnoticed under a healthy uvicorn: the container stayed up —
  # and `healthy`, since the compose healthcheck hits uvicorn on :8000 directly,
  # bypassing nginx — so `restart: unless-stopped` never fired while :80/:443
  # served nothing. `wait -n` returns on the first of the two to exit, and `$?`
  # is that process's status. (`|| EXIT_CODE=$?` keeps `set -e` off our back.)
  wait -n "$NGINX_PID" "$UVICORN_PID" || EXIT_CODE=$?
  END=$(date +%s)

  # If the container is stopping, the exit is expected (SIGTERM), not a crash.
  # Break without counting it so normal shutdown never trips the give-up path.
  if [ "$SHUTTING_DOWN" = "1" ]; then
    echo "[supervisor] shutting down; not restarting uvicorn"
    break
  fi

  # nginx is the one that died? Recreate the container rather than loop-restart
  # uvicorn behind a dead proxy (this is the case the old `wait $UVICORN_PID`
  # could never reach). EXIT_CODE here is nginx's status, which is why it is not
  # fed into the uvicorn fast-failure logic below.
  if ! kill -0 "$NGINX_PID" 2>/dev/null; then
    echo "[supervisor] nginx exited (code $EXIT_CODE); stopping uvicorn and exiting so Docker recreates the container"
    kill "$UVICORN_PID" 2>/dev/null || true
    wait "$UVICORN_PID" 2>/dev/null || true
    exit 1
  fi

  RAN=$((END - START))
  if [ "$RAN" -ge "$MIN_UPTIME_S" ]; then
    echo "[supervisor] uvicorn ran ${RAN}s before exiting (code $EXIT_CODE); transient crash, resetting fast-failure counter"
    FAST_FAILURES=0
  else
    FAST_FAILURES=$((FAST_FAILURES + 1))
    echo "[supervisor] uvicorn exited fast after ${RAN}s (code $EXIT_CODE); fast failure ${FAST_FAILURES}/${MAX_FAST_FAILURES}"
    if [ "$FAST_FAILURES" -ge "$MAX_FAST_FAILURES" ]; then
      echo "[supervisor] ${MAX_FAST_FAILURES} consecutive fast failures; stopping nginx and exiting so Docker recreates the container"
      kill "$NGINX_PID" 2>/dev/null || true
      wait "$NGINX_PID" 2>/dev/null || true
      exit 1
    fi
  fi

  echo "[supervisor] restarting uvicorn in 2s..."
  sleep 2
done
