#!/usr/bin/env bash
# Argument-validation tests for server/isolve-run. Needs no GPU, docker or root:
# everything goes through --dry-run, which prints the docker command.
set -u
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN="$here/../server/isolve-run"

user="$(id -un)"
home="$(getent passwd "$user" | cut -d: -f6)"
uid="$(id -u)"
gid="$(id -g)"
UUID="0a1b2c3d-1111-2222-3333-444455556666"
FILE="${UUID}_Input.h5"

pass=0
fail=0
ok()  { pass=$((pass + 1)); echo "ok   - $1"; }
bad() { fail=$((fail + 1)); echo "FAIL - $1"; [[ -n ${2:-} ]] && echo "       $2"; }

# expect_ok <name> <expected substring> -- args...
expect_ok() {
    local name=$1 want=$2
    shift 3
    local out rc
    out=$(SUDO_USER=$user "$RUN" "$@" 2>&1); rc=$?
    if (( rc == 0 )) && [[ $out == *"$want"* ]]; then ok "$name"; else bad "$name" "rc=$rc out=$out"; fi
}

# expect_fail <name> <expected stderr substring> -- args...
expect_fail() {
    local name=$1 want=$2
    shift 3
    local out rc
    out=$(SUDO_USER=$user "$RUN" "$@" 2>&1); rc=$?
    if (( rc != 0 )) && [[ $out == *"$want"* ]]; then ok "$name"; else bad "$name" "rc=$rc out=$out"; fi
}

# --- accepted -------------------------------------------------------------
expect_ok "gpu 0 builds the docker command" \
    "docker run --rm --gpus device=0 --network host --user $uid:$gid" -- --dry-run 0 "$FILE"
expect_ok "mounts solver read-only, jobs folder, workdir" \
    "-v /usr/local/isolve-sim4life_9.6.2-24047_amd64:/opt/isolve:ro -v $home/sim4life_jobs:/jobs -w /jobs" -- --dry-run 0 "$FILE"
expect_ok "container named s4l_<uuid8>, image, input path" \
    "--name s4l_0a1b2c3d isolve:9.6.2 /jobs/$FILE" -- --dry-run 1 "$FILE"
expect_ok "gpu 1 accepted" "--gpus device=1" -- --dry-run 1 "$FILE"
expect_ok "gpu 2 accepted with --allow-gpu2" "--gpus device=2" -- --dry-run --allow-gpu2 2 "$FILE"
expect_ok "--cancel prints docker stop" "docker stop s4l_0a1b2c3d" -- --dry-run --cancel "$FILE"
expect_ok "short name (no uuid) still gets a container name" "--name s4l_abc isolve:9.6.2 /jobs/abc_Input.h5" -- --dry-run 0 abc_Input.h5

# --- rejected -------------------------------------------------------------
expect_fail "gpu 2 refused by default" "GPU 2" -- --dry-run 2 "$FILE"
expect_fail "gpu 10 refused" "single digit" -- --dry-run 10 "$FILE"
expect_fail "gpu letter refused" "single digit" -- --dry-run x "$FILE"
expect_fail "gpu empty refused" "single digit" -- --dry-run "" "$FILE"
expect_fail "negative gpu refused" "unknown option" -- --dry-run -1 "$FILE"
expect_fail "path with slash refused" "base name only" -- --dry-run 0 "/etc/${FILE}"
expect_fail "relative path refused" "base name only" -- --dry-run 0 "../${FILE}"
expect_fail "windows separator refused" "base name only" -- --dry-run 0 "dir\\${FILE}"
expect_fail "non-Input.h5 refused" "must end in Input.h5" -- --dry-run 0 "${UUID}_Output.h5"
expect_fail "wrong extension refused" "must end in Input.h5" -- --dry-run 0 "${UUID}_Input.h5.txt"
expect_fail "bare Input.h5 refused" "must end in Input.h5" -- --dry-run 0 "Input.h5"
expect_fail "leading dash refused" "unknown option" -- --dry-run 0 "-${FILE}"
expect_fail "space in name refused" "must end in Input.h5" -- --dry-run 0 "a b_Input.h5"
expect_fail "shell metachar refused" "must end in Input.h5" -- --dry-run 0 'a;rm_Input.h5'
expect_fail "missing arguments -> usage" "Usage:" -- --dry-run 0
expect_fail "too many arguments -> usage" "Usage:" -- --dry-run 0 "$FILE" extra
expect_fail "--cancel needs one file" "Usage:" -- --dry-run --cancel
expect_fail "--cancel path refused" "base name only" -- --dry-run --cancel "/x/${FILE}"
expect_fail "unknown option refused" "unknown option" -- --dry-run --frobnicate 0 "$FILE"

# --- environment ----------------------------------------------------------
out=$(env -u SUDO_USER "$RUN" 0 "$FILE" 2>&1); rc=$?
if (( rc != 0 )) && [[ $out == *"SUDO_USER"* ]]; then ok "real run without sudo refused"; else bad "real run without sudo refused" "rc=$rc out=$out"; fi
out=$(SUDO_USER=no_such_user_xyz "$RUN" --dry-run 0 "$FILE" 2>&1); rc=$?
if (( rc != 0 )) && [[ $out == *"unknown user"* ]]; then ok "unknown SUDO_USER refused"; else bad "unknown SUDO_USER refused" "rc=$rc out=$out"; fi
out=$(SUDO_USER=root "$RUN" 0 "$FILE" 2>&1); rc=$?
if (( rc != 0 )) && [[ $out == *"root"* ]]; then ok "jobs as root refused"; else bad "jobs as root refused" "rc=$rc out=$out"; fi

# --dry-run must not create anything
before=$(ls -A "$home" 2>/dev/null | wc -l)
SUDO_USER=$user "$RUN" --dry-run 0 "$FILE" >/dev/null 2>&1
after=$(ls -A "$home" 2>/dev/null | wc -l)
if [[ $before == "$after" && ! -e "$home/sim4life_jobs/${UUID}_Input.log" ]]; then ok "dry-run writes nothing"; else bad "dry-run writes nothing"; fi

echo
echo "$pass passed, $fail failed"
(( fail == 0 ))
