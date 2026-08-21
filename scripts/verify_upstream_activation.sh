#!/usr/bin/env bash
# Verify upstream activation: reload gateway, check PID/service/SHA.
# Called as root by ExecStartPost after the fixed updater succeeds.

set -euo pipefail

REPO="/home/seb/projects/hermes-agent"
STATE="/home/seb/.hermes/labs/bestplan-research/state"
REPORT="${STATE}/last-upstream-activation.txt"

log() { printf '[verify-upstream] %s\n' "$*"; }
fail() { log "FAIL: $*"; exit 1; }

before_pid="$(systemctl show hermes-gateway.service --property=MainPID --value)"
[[ "$before_pid" =~ ^[1-9][0-9]*$ ]] || fail "gateway has no valid MainPID: $before_pid"
systemctl is-active --quiet hermes-gateway.service || fail "gateway is not active before reload"

actual_sha="$(git -C "$REPO" rev-parse HEAD)"
remote_sha="$(git -C "$REPO" ls-remote sebmarion-fork refs/heads/main | awk 'NR==1 {print $1}')"
[[ -n "$remote_sha" && "$actual_sha" == "$remote_sha" ]] || fail "local/remote SHA mismatch: $actual_sha != $remote_sha"

if [[ -f "$REPORT" ]]; then
    activated_sha="$(awk -F= '$1 == "sha" {print $2; exit}' "$REPORT")"
    if [[ -n "$activated_sha" && "$activated_sha" == "$actual_sha" ]]; then
        log "SHA $actual_sha is already activated; no Hermes reload required"
        exit 0
    fi
fi

log "reloading gateway from verified SHA $actual_sha"
systemctl reload hermes-gateway.service

after_pid=""
for _ in $(seq 1 60); do
    after_pid="$(systemctl show hermes-gateway.service --property=MainPID --value)"
    if systemctl is-active --quiet hermes-gateway.service && [[ "$after_pid" =~ ^[1-9][0-9]*$ ]] && [[ "$after_pid" != "$before_pid" ]]; then
        break
    fi
    sleep 1
done
[[ "$after_pid" =~ ^[1-9][0-9]*$ && "$after_pid" != "$before_pid" ]] || fail "gateway did not acquire a new active PID (before=$before_pid after=$after_pid)"

mkdir -p "$STATE"
{
    printf 'status=ok\nsha=%s\nbefore_pid=%s\nafter_pid=%s\n' "$actual_sha" "$before_pid" "$after_pid"
    date --iso-8601=seconds
} > "$REPORT"
chown seb:seb "$REPORT"
log "upstream activation verified successfully (PID $before_pid -> $after_pid)"
exit 0
