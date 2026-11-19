#!/usr/bin/env bash
# Use: source ./activate.sh (not bash ./activate.sh)
_SEMOPS_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export SEMOPS_GATEWAY_ROOT="$_SEMOPS_ROOT"
case ":$PATH:" in
  *":$_SEMOPS_ROOT/bin:"*) ;;
  *) export PATH="$_SEMOPS_ROOT/bin:$PATH" ;;
esac
unset _SEMOPS_ROOT
printf 'SemOps Gateway activated. Commands: doctor | start server/status | cache pull/push/status | runs check/build\n'
