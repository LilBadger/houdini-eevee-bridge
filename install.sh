#!/usr/bin/env bash
set -e
installer_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$installer_dir/install.py" "$@"
