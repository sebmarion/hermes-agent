#!/usr/bin/env bash
# Compatibility entrypoint: delegate activation to the frozen dual-repo coordinator.
set -euo pipefail
exec /usr/local/libexec/hermes-deployment-coordinator.py --activate --reason upstream-merge
