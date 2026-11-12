#!/bin/bash
set -e
SCRIPT_DIR="$(dirname "$(realpath "$0")")"
cd "$SCRIPT_DIR"
echo "Building hifiberry-jellyfin..."
dpkg-buildpackage -us -uc -b
echo "Built packages:"
ls -lh ../hifiberry-jellyfin_*.deb
