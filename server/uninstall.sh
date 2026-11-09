#!/usr/bin/env bash
# Removes the runner and the sudoers rule. Leaves the isolve:9.6.2 image and
# ~/sim4life_jobs in place.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "uninstall.sh: run as root: sudo $0" >&2; exit 1; }
rm -fv /usr/local/bin/isolve-run /etc/sudoers.d/isolve-run
echo "Done. Image isolve:9.6.2 and the jobs folder were left untouched."
