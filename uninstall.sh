#!/usr/bin/env bash
# Remove every installed EEVEE Bridge version, its Houdini package registration, logs and caches.
set -e
installer_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$installer_dir/install.py" --uninstall-all "$@"
