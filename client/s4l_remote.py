#!/usr/bin/env python3
"""s4l_remote - run Sim4Life iSolve jobs on the remote GPU server.

Workflow: Sim4Life writes <uuid>_Input.h5 -> this script copies it to the
server over SSH -> isolve-run starts iSolve in Docker on a chosen GPU inside a
detached tmux session -> this script copies <uuid>_Output.h5 and the log back
next to the input file.

Only the Python standard library plus the system ssh/scp are used. Login is
key-based (BatchMode): no password is ever stored or prompted for. Nothing here
opens or touches local ports, so the FlexLM SSH tunnel is unaffected.
"""
import argparse
import configparser
import getpass
import os
import posixpath
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import List, Optional

RUNNER = "/usr/local/bin/isolve-run"
INPUT_RE = re.compile(r"^(?P<base>[A-Za-z0-9][A-Za-z0-9._-]*)_Input\.h5$")
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]
GPU_NOTE = "GPU 0 and 1 are usable and shared with other users; GPU 2 has MIG enabled and cannot run CUDA."


class S4LError(Exception):
    """An expected failure with a message meant for the user."""


class SshError(S4LError):
    """ssh/scp could not reach the server (as opposed to a remote command failing)."""


# --------------------------------------------------------------------------- config

@dataclass
class Config:
    host: str = "USER@SERVER_HOSTNAME"
    remote_dir: str = "sim4life_jobs"
    default_gpu: int = 0
    poll_seconds: int = 30
    log_suffix: str = "_Output.log"
    owner: str = ""


def load_config(path: Optional[str] = None) -> Config:
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")
    cp = configparser.ConfigParser(inline_comment_prefixes=None)
    if not cp.read(path, encoding="utf-8"):
        raise S4LError(f"config file not found: {path}")
    sec = cp["s4l"] if cp.has_section("s4l") else {}
    cfg = Config()
    try:
        cfg.host = sec.get("host", cfg.host).strip() or cfg.host
        cfg.remote_dir = sec.get("remote_dir", cfg.remote_dir).strip() or cfg.remote_dir
        cfg.default_gpu = int(sec.get("default_gpu", cfg.default_gpu))
        cfg.poll_seconds = max(1, int(sec.get("poll_seconds", cfg.poll_seconds)))
    except ValueError as e:
        raise S4LError(f"bad number in {path}: {e}")
    if "SERVER_HOSTNAME" in cfg.host or cfg.host.startswith("USER@"):
        raise S4LError(f"edit 'host' in {path}: replace USER@SERVER_HOSTNAME with the real "
                       "account and server host name.")
    cfg.log_suffix = sec.get("log_suffix", cfg.log_suffix).strip() or cfg.log_suffix
    cfg.owner = sec.get("owner", "").strip() or getpass.getuser()
    return cfg


# --------------------------------------------------------------------------- jobs

@dataclass
class Job:
    path: str       # absolute local path of the input file
    name: str       # <uuid>_Input.h5
    base: str       # <uuid>

    @property
    def local_dir(self) -> str:
        return os.path.dirname(self.path)

    @property
    def stem(self) -> str:
        return self.name[:-3]

    @property
    def id8(self) -> str:
        return re.sub(r"[^A-Za-z0-9]", "_", self.base[:8])

    @property
    def session(self) -> str:
        return f"s4l_{self.id8}"

    @property
    def output_name(self) -> str:
        return f"{self.base}_Output.h5"

    def local_log_name(self, cfg: Config) -> str:
        return f"{self.base}{cfg.log_suffix}"


def make_job(arg: str) -> Job:
    path = os.path.abspath(os.path.expanduser(arg))
    name = os.path.basename(path)
    m = INPUT_RE.match(name)
    if not m:
        raise S4LError(
            f"'{name}' is not a Sim4Life input file: it must be named <uuid>_Input.h5 "
            "(in Sim4Life: right-click the solver > Write Input File; the file appears in "
            "<Project>.smash_Results). Letters, digits, '.', '_' and '-' only.")
    return Job(path=path, name=name, base=m.group("base"))


def check_gpu(gpu: int, allow_gpu2: bool) -> None:
    if not 0 <= gpu <= 9:
        raise S4LError(f"GPU must be a single digit, got {gpu}")
    if gpu == 2 and not allow_gpu2:
        raise S4LError("GPU 2 has MIG enabled with no instances, so CUDA cannot use it. "
                       "Pick GPU 0 or 1 (run 'gpus' to see the load), or pass --allow-gpu2.")


# --------------------------------------------------------------------------- ssh / scp

def _run(argv: List[str], cwd: Optional[str] = None, capture_stdout: bool = True,
         stdin_text: Optional[str] = None):
    try:
        return subprocess.run(
            argv, cwd=cwd, input=stdin_text, text=True, errors="replace",
            stdout=subprocess.PIPE if capture_stdout else None,
            stderr=subprocess.PIPE, stdin=None if stdin_text is not None else subprocess.DEVNULL)
    except FileNotFoundError:
        raise S4LError(f"'{argv[0]}' not found. On Windows enable Settings > Apps > Optional "
                       "features > OpenSSH Client.")


def _ssh_failure(host: str, stderr: str, what: str) -> S4LError:
    msg = stderr.strip()
    low = msg.lower()
    if "permission denied" in low or "host key verification failed" in low:
        hint = ("The server rejected your SSH key. Run setup_ssh_key.ps1 once (it asks for the "
                "account password a single time), and connect once by hand with 'ssh "
                f"{host}' to accept the host key.")
    else:
        hint = (f"Cannot reach {host}. Is the GlobalProtect VPN connected? Check with: ssh {host}")
    return SshError(f"{what} failed. {hint}\n  ssh said: {msg or '(no message)'}")


class Remote:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._home: Optional[str] = None

    def ssh(self, command: str, check: bool = True) -> subprocess.CompletedProcess:
        """Run one command string on the server. Exit status 255 means ssh itself failed."""
        p = _run(["ssh", *SSH_OPTS, self.cfg.host, command])
        if p.returncode == 255:
            raise _ssh_failure(self.cfg.host, p.stderr, "SSH connection")
        if check and p.returncode != 0:
            raise S4LError(f"remote command failed (exit {p.returncode}): {p.stderr.strip() or p.stdout.strip()}")
        return p

    def bash(self, script: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        """Run a one-line bash script with positional arguments, fully shell-quoted."""
        return self.ssh(shlex.join(["bash", "-c", script, "s4l", *args]), check=check)

    @property
    def home(self) -> str:
        if self._home is None:
            self._home = self.bash('printf %s "$HOME"').stdout.strip()
            if not self._home.startswith("/"):
                raise S4LError(f"could not determine the home directory on {self.cfg.host}")
        return self._home

    @property
    def jobs(self) -> str:
        return posixpath.join(self.home, self.cfg.remote_dir)

    def rpath(self, filename: str) -> str:
        return posixpath.join(self.jobs, filename)

    def scp_up(self, local_dir: str, name: str, dest: str) -> None:
        # cwd + bare file name sidesteps spaces and "C:" drive letters in local paths.
        p = _run(["scp", *SSH_OPTS, name, f"{self.cfg.host}:{dest}"], cwd=local_dir, capture_stdout=False)
        if p.returncode != 0:
            raise _ssh_failure(self.cfg.host, p.stderr, f"Copying {name} to the server")

    def scp_down(self, remote_file: str, local_dir: str, name: str) -> None:
        part = name + ".part"
        p = _run(["scp", *SSH_OPTS, f"{self.cfg.host}:{remote_file}", part], cwd=local_dir, capture_stdout=False)
        if p.returncode != 0:
            _remove(os.path.join(local_dir, part))
            raise _ssh_failure(self.cfg.host, p.stderr, f"Copying {posixpath.basename(remote_file)} from the server")
        os.replace(os.path.join(local_dir, part), os.path.join(local_dir, name))


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# --------------------------------------------------------------------------- status

RUNNING, DONE, UNKNOWN = "RUNNING", "DONE", "UNKNOWN"

STATUS_SCRIPT = (
    'd=$1; s=$2; sess=$3; '
    'echo "OWNER=$(head -n1 "$d/$s.owner" 2>/dev/null)"; '
    'if tmux has-session -t "=$sess" 2>/dev/null; then echo SESSION=1; else echo SESSION=0; fi; '
    'if [ -e "$d/$s.done" ]; then echo "DONE=$(head -n1 "$d/$s.done")"; fi; '
    'echo "LICENSE=$(grep -i license "$d/$s.log" 2>/dev/null | tail -n 1)"; '
    'echo ---LOG---; tail -n 5 "$d/$s.log" 2>/dev/null; true'
)


@dataclass
class Status:
    state: str
    code: Optional[int]
    owner: str
    license_line: str
    log_tail: List[str]


def get_status(remote: Remote, job: Job) -> Status:
    out = remote.bash(STATUS_SCRIPT, remote.jobs, job.stem, job.session).stdout
    head, _, tail = out.partition("---LOG---\n")
    kv = {}
    for line in head.splitlines():
        k, _, v = line.partition("=")
        kv[k] = v
    code = None
    if "DONE" in kv:
        try:
            code = int(kv["DONE"].strip())
        except ValueError:
            code = -1
    if code is not None:
        state = DONE
    elif kv.get("SESSION") == "1":
        state = RUNNING
    else:
        state = UNKNOWN
    return Status(state, code, kv.get("OWNER", "").strip(), kv.get("LICENSE", "").strip(),
                  tail.splitlines())


def describe(st: Status) -> str:
    if st.state == DONE:
        return f"DONE (exit code {st.code})"
    return st.state


LICENSE_HINT = ("The log mentions the license. If the checkout failed, check that FlexLM is up on the "
                "server (systemctl status flexlm-speag) and that port 27000 is listening; the solver "
                "uses 27000@localhost on the server itself, not your SSH tunnel.")


def print_failure_help(code: int, log_lines: List[str], license_line: str = "") -> None:
    print(f"The solver failed with exit code {code}.", file=sys.stderr)
    if license_line or any("license" in ln.lower() for ln in log_lines):
        shown = license_line or next(ln for ln in log_lines if "license" in ln.lower())
        print(f"  license-related log line: {shown.strip()}", file=sys.stderr)
        print(f"  {LICENSE_HINT}", file=sys.stderr)


# --------------------------------------------------------------------------- commands

def cmd_submit(cfg: Config, args, remote: Optional[Remote] = None) -> int:
    job = make_job(args.input)
    if not os.path.isfile(job.path):
        raise S4LError(f"input file not found: {job.path}")
    gpu = cfg.default_gpu if args.gpu is None else args.gpu
    check_gpu(gpu, args.allow_gpu2)
    remote = remote or Remote(cfg)

    # Connectivity check, jobs folder, and "does this job already exist?"
    p = remote.bash('mkdir -p "$1" && '
                    'if tmux has-session -t "=$2" 2>/dev/null; then echo EXISTS; else echo FREE; fi',
                    remote.jobs, job.session)
    if "EXISTS" in p.stdout.split():
        raise S4LError(f"a job with session '{job.session}' is already running on the server "
                       f"(see 'list'); wait for it or cancel it first.")

    print(f"Copying {job.name} to {cfg.host}:{remote.jobs}/ ...")
    remote.scp_up(job.local_dir, job.name, remote.rpath(job.name))

    done, log, errf = (remote.rpath(job.stem + ext) for ext in (".done", ".log", ".err"))
    gpu_args = ("--allow-gpu2 " if args.allow_gpu2 else "") + str(gpu)
    wrapper = (f"sudo -n {RUNNER} {gpu_args} {shlex.quote(job.name)} 2>{shlex.quote(errf)}; rc=$?; "
               f"if [ ! -e {shlex.quote(done)} ]; then "
               f"{{ cat {shlex.quote(errf)}; echo \"isolve-run exited with status $rc before the solver ran\"; }} "
               f"> {shlex.quote(log)}; echo $rc > {shlex.quote(done)}; fi; rm -f {shlex.quote(errf)}")
    remote.bash('rm -f "$1"; printf "%s\\n" "$2" > "$3"; tmux new-session -d -s "$4" "$5"',
                done, cfg.owner, remote.rpath(job.stem + ".owner"), job.session, wrapper)
    print(f"Started GPU {gpu} job '{job.session}' for owner '{cfg.owner}'.")
    print(f"  status: s4l_remote.py status \"{args.input}\"")
    return 0


def cmd_status(cfg: Config, args, remote: Optional[Remote] = None) -> int:
    job = make_job(args.input)
    remote = remote or Remote(cfg)
    st = get_status(remote, job)
    print(f"{job.name}  owner: {st.owner or '(unknown)'}")
    print(describe(st))
    if st.log_tail:
        print("--- last log lines ---")
        print("\n".join(st.log_tail))
    elif st.state != RUNNING:
        print("(no log on the server)")
    if st.state == UNKNOWN:
        print("No finished marker and no running session: the job was never submitted, or the "
              "server restarted mid-run.", file=sys.stderr)
    return 0


def _local_conflicts(job: Job, cfg: Config, want_output: bool, force: bool) -> None:
    names = ([job.output_name] if want_output else []) + [job.local_log_name(cfg)]
    clash = [n for n in names if os.path.exists(os.path.join(job.local_dir, n))]
    if clash and not force:
        raise S4LError(f"{', '.join(clash)} already exist in {job.local_dir}; "
                       "use --force to overwrite.")


def _fetch_done(cfg: Config, remote: Remote, job: Job, st: Status, force: bool) -> int:
    """Download results of a finished job; returns the solver's exit code."""
    _local_conflicts(job, cfg, want_output=(st.code == 0), force=force)
    if st.code == 0:
        listing = remote.bash('[ -e "$1" ] && echo YES || echo NO; ls -1 "$2"* 2>/dev/null',
                              remote.rpath(job.output_name), remote.rpath(job.base)).stdout.split()
        if not listing or listing[0] != "YES":
            raise S4LError(
                f"iSolve exited with code 0 but {job.output_name} is missing in {remote.jobs} on the "
                f"server. Files for this job there: {', '.join(listing[1:]) or '(none)'}. "
                "Check the log; iSolve may have written the output under another name.")
        print(f"Fetching {job.output_name} ...")
        remote.scp_down(remote.rpath(job.output_name), job.local_dir, job.output_name)
    # iSolve writes its own <uuid>_Output.log next to the output; <stem>.log is the captured
    # terminal output. After a successful run prefer iSolve's own log, after a failure the
    # captured one (it also holds errors iSolve printed before it could open its log).
    own, captured = remote.rpath(job.base + "_Output.log"), remote.rpath(job.stem + ".log")
    candidates = [own, captured] if st.code == 0 else [captured, own]
    found = remote.bash('for f in "$@"; do if [ -e "$f" ]; then echo "$f"; break; fi; done',
                        *candidates).stdout.strip()
    if found:
        print(f"Fetching {posixpath.basename(found)} as {job.local_log_name(cfg)} ...")
        remote.scp_down(found, job.local_dir, job.local_log_name(cfg))
    else:
        print("  (no log on the server)", file=sys.stderr)
    if st.code != 0:
        print_failure_help(st.code, _tail_local(os.path.join(job.local_dir, job.local_log_name(cfg)), 20),
                           st.license_line)
        print("--- last log lines ---", file=sys.stderr)
        print("\n".join(_tail_local(os.path.join(job.local_dir, job.local_log_name(cfg)), 20)), file=sys.stderr)
    return st.code


def _tail_local(path: str, n: int) -> List[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()[-n:]
    except OSError:
        return []


def cmd_fetch(cfg: Config, args, remote: Optional[Remote] = None) -> int:
    job = make_job(args.input)
    remote = remote or Remote(cfg)
    st = get_status(remote, job)
    if st.state != DONE:
        raise S4LError(f"job is {st.state}, nothing to fetch yet (see 'status').")
    code = _fetch_done(cfg, remote, job, st, args.force)
    if code == 0:
        print(f"Done. Results are in {job.local_dir}")
    return 0 if code == 0 else 1


def cmd_run(cfg: Config, args, remote: Optional[Remote] = None, sleep=time.sleep) -> int:
    job = make_job(args.input)
    if not os.path.isfile(job.path):
        raise S4LError(f"input file not found: {job.path}")
    # Fail before using any GPU time if the results could not be saved.
    _local_conflicts(job, cfg, want_output=True, force=args.force)
    remote = remote or Remote(cfg)
    cmd_submit(cfg, args, remote)

    failures = 0
    try:
        while True:
            try:
                st = get_status(remote, job)
                failures = 0
            except SshError as e:
                failures += 1
                if failures >= 10:
                    raise
                print(f"[{time.strftime('%H:%M:%S')}] connection problem ({failures}/10), retrying: "
                      f"{str(e).splitlines()[0]}", file=sys.stderr)
                sleep(cfg.poll_seconds)
                continue
            last = st.log_tail[-1].strip()[:100] if st.log_tail else ""
            print(f"[{time.strftime('%H:%M:%S')}] {describe(st)}  {last}", flush=True)
            if st.state == DONE:
                break
            if st.state == UNKNOWN:
                raise S4LError("the job disappeared from the server without a result "
                               "(server restart or session killed). See 'status' and the log.")
            sleep(cfg.poll_seconds)
    except KeyboardInterrupt:
        print("\nInterrupted. The job keeps running on the server; use 'status', 'fetch' or 'cancel'.",
              file=sys.stderr)
        return 130
    code = _fetch_done(cfg, remote, job, st, args.force)
    if code == 0:
        print(f"Done. Results are in {job.local_dir}")
    return 0 if code == 0 else 1


def cmd_list(cfg: Config, args, remote: Optional[Remote] = None) -> int:
    remote = remote or Remote(cfg)
    script = (
        'cd "$1" 2>/dev/null || exit 0; '
        'for s in $(tmux list-sessions -F "#{session_name}" 2>/dev/null | grep "^s4l_"); do '
        'o=$(cat "${s#s4l_}"*_Input.owner 2>/dev/null | head -n1); echo "RUNNING|$s|$o"; done; '
        'for f in *_Input.done; do [ -e "$f" ] || continue; '
        'echo "DONE|${f%.done}|$(head -n1 "$f")|$(head -n1 "${f%.done}.owner" 2>/dev/null)"; done; true'
    )
    out = remote.bash(script, remote.jobs).stdout
    running = [ln.split("|") for ln in out.splitlines() if ln.startswith("RUNNING|")]
    finished = [ln.split("|") for ln in out.splitlines() if ln.startswith("DONE|")]
    print("Running:")
    for _, session, owner in running or []:
        print(f"  {session:<14} owner: {owner or '(unknown)'}")
    if not running:
        print("  (none)")
    print("Finished:")
    for _, stem, code, owner in finished:
        print(f"  {stem:<48} exit {code:<4} owner: {owner or '(unknown)'}")
    if not finished:
        print("  (none)")
    return 0


def cmd_gpus(cfg: Config, args, remote: Optional[Remote] = None) -> int:
    remote = remote or Remote(cfg)
    p = remote.ssh("nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used --format=csv")
    print(p.stdout.strip())
    print(GPU_NOTE)
    return 0


def cmd_cancel(cfg: Config, args, remote: Optional[Remote] = None) -> int:
    job = make_job(args.input)
    remote = remote or Remote(cfg)
    st = get_status(remote, job)
    if st.owner and st.owner != cfg.owner and not args.force:
        raise S4LError(f"this job belongs to '{st.owner}', you are '{cfg.owner}'. "
                       "Use --force if you really mean to cancel it.")
    if not st.owner:
        print("Note: the server has no owner recorded for this job.", file=sys.stderr)
    if st.state == DONE:
        print(f"Job already finished ({describe(st)}); nothing to cancel.")
        return 0
    p = remote.ssh(f"sudo -n {RUNNER} --cancel {shlex.quote(job.name)}", check=False)
    if p.returncode == 4:
        print("No running container for this job (it may not have started yet).")
    elif p.returncode != 0:
        raise S4LError(f"isolve-run --cancel failed (exit {p.returncode}): {p.stderr.strip()}")
    else:
        print("Container stopped.")
    # The runner writes <stem>.done itself once the container is gone; give it a moment,
    # then kill the tmux session and make sure the job is marked finished.
    remote.bash('for i in $(seq 1 20); do tmux has-session -t "=$1" 2>/dev/null || break; sleep 1; done; '
                'tmux kill-session -t "=$1" 2>/dev/null; '
                '[ -e "$2" ] || echo 143 > "$2"; true',
                job.session, remote.rpath(job.stem + ".done"))
    print(f"Cancelled {job.session}.")
    return 0


# --------------------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="s4l_remote.py",
                                 description="Run Sim4Life iSolve jobs on the remote GPU server.")
    ap.add_argument("--config", help="path to config.ini (default: next to this script)")
    sub = ap.add_subparsers(dest="command", required=True)

    def add_input(p):
        p.add_argument("input", help="path to <uuid>_Input.h5")

    def add_gpu(p):
        p.add_argument("--gpu", type=int, help="GPU index (default: default_gpu from config.ini)")
        p.add_argument("--allow-gpu2", action="store_true", help="allow the MIG-enabled GPU 2 (normally refused)")

    p = sub.add_parser("submit", help="copy the input file to the server and start the job")
    add_input(p); add_gpu(p); p.set_defaults(func=cmd_submit)
    p = sub.add_parser("status", help="show job state and the last 5 log lines")
    add_input(p); p.set_defaults(func=cmd_status)
    p = sub.add_parser("fetch", help="copy output and log back next to the input file")
    add_input(p); p.add_argument("--force", action="store_true", help="overwrite existing local files")
    p.set_defaults(func=cmd_fetch)
    p = sub.add_parser("run", help="submit, wait for completion, then fetch")
    add_input(p); add_gpu(p); p.add_argument("--force", action="store_true", help="overwrite existing local files")
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("list", help="list running and finished jobs with their owners")
    p.set_defaults(func=cmd_list)
    p = sub.add_parser("gpus", help="show GPU utilisation and memory to pick a free GPU")
    p.set_defaults(func=cmd_gpus)
    p = sub.add_parser("cancel", help="stop a running job")
    add_input(p); p.add_argument("--force", action="store_true", help="cancel even if another owner started it")
    p.set_defaults(func=cmd_cancel)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(load_config(args.config), args)
    except S4LError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
