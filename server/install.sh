#!/usr/bin/env bash
# Idempotent server-side installer. Run once as an administrator:
#     sudo ./install.sh
#     sudo ./install.sh <account>      (or: sudo S4L_USER=<account> ./install.sh)
# <account> is the shared Unix account that runs jobs; it gets the sudoers rule
# and the ~/sim4life_jobs folder.
#
# Does NOT touch /usr/local/isolve-sim4life_* and does NOT touch flexlm-speag.
set -euo pipefail

IMAGE="isolve:9.6.2"
ISOLVE_DIR="/usr/local/isolve-sim4life_9.6.2-24047_amd64"
RUNNER_DST="/usr/local/bin/isolve-run"
SUDOERS_DST="/etc/sudoers.d/isolve-run"
S4L_USER="${1:-${S4L_USER:-}}"
[[ -n $S4L_USER ]] || { echo "usage: sudo $0 <account>   (the shared account that runs jobs)" >&2; exit 1; }

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[[ $EUID -eq 0 ]] || { echo "install.sh: run as root: sudo $0" >&2; exit 1; }
command -v docker >/dev/null || { echo "install.sh: docker not found" >&2; exit 1; }
command -v visudo >/dev/null || { echo "install.sh: visudo not found" >&2; exit 1; }
command -v setpriv >/dev/null || { echo "install.sh: setpriv (util-linux) not found" >&2; exit 1; }
[[ -x $ISOLVE_DIR/bin/iSolve ]] || { echo "install.sh: $ISOLVE_DIR/bin/iSolve not found" >&2; exit 1; }
command -v tmux >/dev/null || echo "install.sh: WARNING: tmux not found; the client needs it (sudo apt install tmux)" >&2
pw=$(getent passwd "$S4L_USER") || { echo "install.sh: unknown user '$S4L_USER'" >&2; exit 1; }
IFS=: read -r _ _ uid gid _ home _ <<<"$pw"

echo "==> Building image $IMAGE"
docker build -t "$IMAGE" "$here"

echo "==> Installing $RUNNER_DST"
install -o root -g root -m 0755 "$here/isolve-run" "$RUNNER_DST"

echo "==> Installing $SUDOERS_DST (user: $S4L_USER)"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
# Same rule as sudoers-isolve, with the account name substituted.
sed -E "s/^USER([[:space:]])/${S4L_USER}\1/" "$here/sudoers-isolve" >"$tmp"
if ! visudo -cf "$tmp" >/dev/null; then
    echo "install.sh: sudoers file failed visudo check; not installed" >&2
    exit 1
fi
install -o root -g root -m 0440 "$tmp" "$SUDOERS_DST"

echo "==> Creating $home/sim4life_jobs"
install -d -o "$uid" -g "$gid" -m 0755 "$home/sim4life_jobs"

echo "==> Smoke test: iSolve --solver_types on GPU 0"
smoke_rc=0
smoke_out="$(docker run --rm --gpus "device=0" --network host \
    -v "$ISOLVE_DIR:/opt/isolve:ro" "$IMAGE" --solver_types 2>&1)" || smoke_rc=$?
printf '%s\n' "$smoke_out"
if (( smoke_rc != 0 )); then
    echo "RESULT: smoke test FAILED (docker run exit code $smoke_rc)" >&2
    exit 1
fi
if grep -qi cuda <<<"$smoke_out"; then
    echo "RESULT: CUDA solvers are listed - GPU runs should work."
else
    echo "RESULT: no CUDA solver listed - check the driver, nvidia-container-toolkit and the license." >&2
fi
echo "Installed. Runner: $RUNNER_DST   Jobs: $home/sim4life_jobs"
