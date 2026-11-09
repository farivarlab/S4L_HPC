# s4l-remote

Run Sim4Life (iSolve 9.6.2) solver jobs on a remote Linux GPU server from a Windows PC.
ARES only runs on Windows, so the workflow is:

```
Sim4Life GUI ──writes──▶ <Project>.smash_Results\<uuid>_Input.h5
      │
      ▼   client/s4l_remote.py   (ssh/scp over the GlobalProtect VPN)
SERVER_HOSTNAME:~/sim4life_jobs/<uuid>_Input.h5
      │   tmux session s4l_<uuid8>  →  sudo isolve-run <gpu> <file>
      ▼
docker (ubuntu:24.04 + host iSolve, --gpus device=N, --network host)
      │
      ▼   s4l_remote.py fetch
<Project>.smash_Results\<uuid>_Output.h5  +  <uuid>_Output.log
```

```
server/   Dockerfile, isolve-run, sudoers-isolve, install.sh, uninstall.sh
client/   s4l_remote.py, config.ini, setup_ssh_key.ps1
tests/    test_isolve_run.sh (runner validation), test_client.py (client vs. fake ssh)
```

Setup this was written for: a Linux server `SERVER_HOSTNAME` (Ubuntu 22.04, 3× A100; GPU 0 and 1 usable and shared, GPU 2 has MIG
enabled and is refused unless `--allow-gpu2`), one shared Unix account `USER` used by up to 5 people, FlexLM on
port 27000 of the same host. Nothing here touches `/usr/local/isolve-sim4life_*` or `flexlm-speag`.

> **Before use:** replace the placeholders `USER` (the shared account on the server) and
> `SERVER_HOSTNAME` (its host name) in `client/config.ini` (and in the commands below). The
> client and `setup_ssh_key.ps1` refuse to run until `host` in `config.ini` is edited. Server
> paths, GPU numbers and the iSolve version in `server/isolve-run` match one specific machine;
> adjust them for yours.

## Server install (once, by an administrator)

```bash
git clone <this repo> && cd s4l_hpc/server
sudo ./install.sh USER     # USER = the shared account that runs jobs
```

`install.sh` is idempotent. It builds `isolve:9.6.2`, installs `/usr/local/bin/isolve-run`
(root:root 0755), installs `/etc/sudoers.d/isolve-run` (0440, only after `visudo -cf` passes),
creates `~/sim4life_jobs`, then runs `iSolve --solver_types` on GPU 0 and prints whether CUDA
solvers are listed. `USER` is **not** added to the docker group; the sudoers rule allows exactly
`sudo isolve-run`. `sudo ./uninstall.sh` removes the runner and the sudoers file (image and jobs
folder stay).

## Windows setup (each lab member, once)

Requirements: Windows built-in OpenSSH client (Settings → Optional features) and Python 3.9+.
No pip packages.

1. Connect GlobalProtect VPN.
2. Optionally edit `client/config.ini`, e.g. `owner = milad` (default: your Windows user name).
3. In PowerShell: `.\client\setup_ssh_key.ps1`. It creates `~\.ssh\id_ed25519` if missing and
   adds the public key to the server's `authorized_keys` with the comment `<owner>@<computer>`.
   You type the password once, into ssh. Re-running does not add a duplicate. To revoke someone,
   delete their line from `~/.ssh/authorized_keys` on the server.

No script ever stores or asks for a password; every ssh/scp call uses `BatchMode=yes`.

## Usage

In Sim4Life: right-click the solver → **Write Input File**. Then, from the folder of the client:

```
python s4l_remote.py gpus                              # pick a free GPU (0 or 1)
python s4l_remote.py run "C:\Work\My Proj.smash_Results\<uuid>_Input.h5" --gpu 1
```

| command | what it does |
|---|---|
| `submit <in> [--gpu N]` | copy to the server, start `sudo isolve-run` in tmux `s4l_<uuid8>`, record the owner; refuses if the session exists |
| `status <in>` | owner, `RUNNING` / `DONE (exit code N)` / `UNKNOWN`, last 5 log lines |
| `fetch <in> [--force]` | copy `<uuid>_Output.h5` and the log next to the input; never overwrites without `--force` |
| `run <in> [--gpu N]` | submit, poll every `poll_seconds`, fetch on completion; on a non-zero exit fetches only the log and prints its tail |
| `list` | running `s4l*` sessions and finished jobs, with owners |
| `gpus` | `nvidia-smi` index, name, utilisation, memory used |
| `cancel <in> [--force]` | `docker stop` the container and kill the tmux session; refuses another owner's job without `--force` |

`submit`/`run` also take `--allow-gpu2` (GPU 2 is refused otherwise). Ctrl-C during `run` leaves
the job running on the server; use `status`, `fetch` or `cancel` afterwards. GPU vs. CPU is chosen in
the Sim4Life solver settings, not on the command line.

### config.ini

`host=USER@SERVER_HOSTNAME`, `remote_dir=sim4life_jobs` (must match the runner's `~/sim4life_jobs`),
`default_gpu=0`, `poll_seconds=30`, `log_suffix=_Output.log`, `owner=` (empty = Windows user).

The local log is saved as `<uuid><log_suffix>`. After a successful run it is iSolve's own
`<uuid>_Output.log` from the server; after a failed run it is the captured terminal output
(`<uuid>_Input.log`), which also holds errors printed before iSolve could open its log.
Compare the name with a local ARES run and edit `log_suffix` if Sim4Life expects another one.

## Running by hand (without the script)

```
scp <uuid>_Input.h5 USER@SERVER_HOSTNAME:sim4life_jobs/     # from the smash_Results folder
ssh USER@SERVER_HOSTNAME
tmux new -s horn                                                    # survives disconnects
sudo isolve-run 1 <uuid>_Input.h5                                   # 1 = GPU index
cat ~/sim4life_jobs/<uuid>_Input.done                               # 0 = success
scp USER@SERVER_HOSTNAME:sim4life_jobs/<uuid>_Output.h5 .    # back on the PC
scp USER@SERVER_HOSTNAME:sim4life_jobs/<uuid>_Output.log .
```

Do not mix this with `s4l_remote.py` for the same file at the same time (same session and container name).

## FlexLM tunnel

The Sim4Life GUI gets its license through its own tunnel
(`ssh -N -L 27000:localhost:27000 -L <vendor_port>:localhost:<vendor_port> USER@SERVER_HOSTNAME`,
GUI set to `27000@localhost`). `s4l_remote.py` never binds local ports, and never starts, reuses
or kills that tunnel, so it works with or without it. The solver on the server takes its license
from FlexLM directly (`LM_LICENSE_FILE=27000@localhost`, `--network host`).

## Errors you may see

| message | meaning |
|---|---|
| `Cannot reach … Is the GlobalProtect VPN connected?` | VPN down or host unreachable |
| `The server rejected your SSH key` | run `setup_ssh_key.ps1`; connect once with `ssh USER@SERVER_HOSTNAME` to accept the host key |
| `… is not a Sim4Life input file … <uuid>_Input.h5` | wrong file; use *Write Input File* |
| `GPU 2 has MIG enabled …` | use GPU 0/1 |
| `license-related log line …` | checkout failed; check `systemctl status flexlm-speag` and port 27000 on the server |
| `exited with code 0 but …_Output.h5 is missing` | iSolve wrote its output elsewhere or under another name; the message lists the job's files |

## Tests

```
bash tests/test_isolve_run.sh     # runner argument validation, no GPU/docker/root needed
python3 tests/test_client.py      # client end to end against fake ssh/scp/sudo (needs tmux)
```

## Security notes

`isolve-run` runs as root through a one-command sudoers rule, so it validates the GPU digit and
the file name (base name only, `…Input.h5`, safe characters), takes the user from `SUDO_USER`
(set by sudo, not spoofable) and writes `.log`/`.done` with the caller's privileges
(`setpriv`), so a symlink in the jobs folder cannot be used to overwrite root-owned files. The
container runs as the calling user, not root.
