#!/bin/sh
# Launch reachy-mini-daemon against whichever USB serial port Reachy is on.
# Exits non-zero when no port is present or when the port vanishes mid-run,
# so launchd (KeepAlive) restarts it after the throttle interval.
set -eu

UV_BIN="${UV_BIN:-uv}"
PORT_GLOB="${AMBIENT_SERIAL_PORT_GLOB:-/dev/cu.usbmodem*}"
POLL_S="${AMBIENT_SERIAL_POLL_S:-2}"

port="${AMBIENT_SERIAL_PORT:-}"
if [ -z "$port" ]; then
    # shellcheck disable=SC2086
    for candidate in $PORT_GLOB; do
        if [ -e "$candidate" ]; then
            port="$candidate"
            break
        fi
    done
fi

if [ -z "$port" ] || [ ! -e "$port" ]; then
    echo "run-daemon: no serial port matching $PORT_GLOB; is Reachy plugged in?" >&2
    exit 69
fi

echo "run-daemon: starting reachy-mini-daemon on $port"
"$UV_BIN" run reachy-mini-daemon --serialport "$port" "$@" &
daemon_pid=$!

stop_daemon() {
    kill "$daemon_pid" 2>/dev/null || true
    wait "$daemon_pid" 2>/dev/null || true
}
trap 'stop_daemon; exit 0' INT TERM

while kill -0 "$daemon_pid" 2>/dev/null; do
    if [ ! -e "$port" ]; then
        echo "run-daemon: serial port $port vanished; stopping daemon for restart" >&2
        stop_daemon
        exit 75
    fi
    sleep "$POLL_S"
done

wait "$daemon_pid" && status=0 || status=$?
echo "run-daemon: reachy-mini-daemon exited with status $status" >&2
# A clean daemon exit is still unexpected for a supervised service; return
# non-zero so launchd's KeepAlive (SuccessfulExit=false) restarts it.
if [ "$status" -eq 0 ]; then
    exit 75
fi
exit "$status"
