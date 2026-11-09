"""End-to-end tests of client/s4l_remote.py against fake ssh/scp/sudo.

"The server" is this machine: the fake ssh runs commands locally with a private
HOME, real tmux runs the (fake) runner. No network, GPU, docker or root needed.
Run:  python3 tests/test_client.py
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "client"))
import s4l_remote as s4l  # noqa: E402


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="s4l-test-")
        self.home = os.path.join(self.tmp, "server home")
        os.makedirs(self.home)
        # Windows-like project folder with spaces
        self.work = os.path.join(self.tmp, "My Project.smash_Results")
        os.makedirs(self.work)
        self.cfg_path = os.path.join(self.tmp, "config.ini")
        self.write_config(owner="alice")
        self.env = {
            "PATH": os.path.join(HERE, "fakebin") + os.pathsep + os.environ["PATH"],
            "FAKE_SERVER_HOME": self.home,
            "FAKE_RUNNER": os.path.join(HERE, "fakebin", "fake-isolve-run"),
            "TMUX_TMPDIR": self.tmp,
        }
        self._old = {k: os.environ.get(k) for k in list(self.env) + ["FAKE_SSH_DOWN"]}
        os.environ.update(self.env)

    def tearDown(self):
        subprocess.run(["tmux", "kill-server"], env=dict(os.environ), capture_output=True)
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_config(self, owner="alice", poll=1):
        with open(self.cfg_path, "w") as f:
            f.write(f"[s4l]\nhost = testuser@testserver\nremote_dir = sim4life_jobs\ndefault_gpu = 0\n"
                    f"poll_seconds = {poll}\nlog_suffix = _Output.log\nowner = {owner}\n")

    def make_input(self, tag="a"):
        name = f"{tag}1b2c3d4-aaaa-bbbb-cccc-000000000000_Input.h5"
        path = os.path.join(self.work, name)
        with open(path, "w") as f:
            f.write("input")
        return path

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = s4l.main(["--config", self.cfg_path, *argv])
        return rc, out.getvalue(), err.getvalue()

    def jobs(self, name):
        return os.path.join(self.home, "sim4life_jobs", name)

    # ---- happy path -------------------------------------------------------
    def test_run_fetches_output_and_log(self):
        inp = self.make_input()
        rc, out, err = self.cli("run", inp)
        self.assertEqual(rc, 0, out + err)
        base = os.path.basename(inp)[:-len("_Input.h5")]
        self.assertTrue(os.path.isfile(os.path.join(self.work, base + "_Output.h5")))
        with open(os.path.join(self.work, base + "_Output.log")) as f:
            log = f.read()
        self.assertIn("iSolve own log", log)   # iSolve's own _Output.log is preferred after success
        with open(self.jobs(os.path.basename(inp)[:-3] + ".owner")) as f:
            self.assertEqual(f.read().strip(), "alice")

    def test_fetch_never_overwrites_without_force(self):
        inp = self.make_input()
        self.assertEqual(self.cli("run", inp)[0], 0)
        rc, _, err = self.cli("fetch", inp)
        self.assertEqual(rc, 1)
        self.assertIn("--force", err)
        self.assertEqual(self.cli("fetch", inp, "--force")[0], 0)
        # run refuses up front, before submitting anything
        rc, _, err = self.cli("run", inp)
        self.assertEqual(rc, 1)
        self.assertIn("already exist", err)

    # ---- failures ---------------------------------------------------------
    def test_failed_job_fetches_log_only_and_flags_license(self):
        inp = self.make_input("fail")
        rc, out, err = self.cli("run", inp)
        self.assertEqual(rc, 1)
        base = os.path.basename(inp)[:-len("_Input.h5")]
        self.assertFalse(os.path.exists(os.path.join(self.work, base + "_Output.h5")))
        self.assertTrue(os.path.exists(os.path.join(self.work, base + "_Output.log")))
        with open(os.path.join(self.work, base + "_Output.log")) as f:
            self.assertIn("license checkout failed", f.read())   # captured log after a failure
        self.assertIn("exit code 3", err)
        self.assertIn("license", err.lower())
        self.assertIn("flexlm", err.lower())

    def test_missing_output_after_zero_exit(self):
        inp = self.make_input("nooutput")
        rc, out, err = self.cli("run", inp)
        self.assertEqual(rc, 1)
        self.assertIn("is missing", err)

    def test_runner_that_cannot_start_is_reported(self):
        os.environ["FAKE_RUNNER"] = os.path.join(self.tmp, "does-not-exist")
        rc, out, err = self.cli("run", self.make_input())
        self.assertEqual(rc, 1)
        self.assertIn("before the solver ran", out + err)

    def test_gpu2_refused_unless_allowed(self):
        inp = self.make_input()
        rc, _, err = self.cli("submit", inp, "--gpu", "2")
        self.assertEqual(rc, 1)
        self.assertIn("GPU 2", err)
        self.assertFalse(os.path.exists(self.jobs(os.path.basename(inp))))

    def test_wrong_file_name(self):
        bad = os.path.join(self.work, "model_Output.h5")
        open(bad, "w").close()
        rc, _, err = self.cli("submit", bad)
        self.assertEqual(rc, 1)
        self.assertIn("_Input.h5", err)

    def test_vpn_down(self):
        os.environ["FAKE_SSH_DOWN"] = "1"
        rc, _, err = self.cli("submit", self.make_input())
        self.assertEqual(rc, 1)
        self.assertIn("VPN", err)

    # ---- long-running jobs: status, list, duplicate, cancel -----------------
    def test_lifecycle_slow_job(self):
        inp = self.make_input("slow")
        self.assertEqual(self.cli("submit", inp, "--gpu", "1")[0], 0)
        time.sleep(1)
        rc, out, _ = self.cli("status", inp)
        self.assertIn("RUNNING", out)
        self.assertIn("alice", out)
        self.assertIn("GPU 1", out)
        rc, out, _ = self.cli("list")
        self.assertIn("s4l_slow1b2c", out)
        self.assertIn("alice", out)
        # same session again is refused
        rc, _, err = self.cli("submit", inp)
        self.assertEqual(rc, 1)
        self.assertIn("already running", err)
        # fetch before completion is refused
        rc, _, err = self.cli("fetch", inp)
        self.assertEqual(rc, 1)
        self.assertIn("RUNNING", err)
        # another owner may not cancel without --force
        self.write_config(owner="bob")
        rc, _, err = self.cli("cancel", inp)
        self.assertEqual(rc, 1)
        self.assertIn("alice", err)
        rc, out, err = self.cli("cancel", inp, "--force")
        self.assertEqual(rc, 0, out + err)
        rc, out, _ = self.cli("status", inp)
        self.assertIn("DONE (exit code 143)", out)
        self.assertIn("Finished", self.cli("list")[1])

    def test_status_unknown_for_unsubmitted_job(self):
        rc, out, _ = self.cli("status", self.make_input("zzz"))
        self.assertEqual(rc, 0)
        self.assertIn("UNKNOWN", out)

    # ---- unit-level -------------------------------------------------------
    def test_session_name_matches_runner_convention(self):
        job = s4l.make_job("0a1b2c3d-1111-2222-3333-444455556666_Input.h5")
        self.assertEqual(job.session, "s4l_0a1b2c3d")
        self.assertEqual(job.output_name, "0a1b2c3d-1111-2222-3333-444455556666_Output.h5")

    def test_placeholder_host_is_rejected(self):
        shipped = os.path.join(HERE, "..", "client", "config.ini")
        with self.assertRaises(s4l.S4LError) as cm:
            s4l.load_config(shipped)
        self.assertIn("SERVER_HOSTNAME", str(cm.exception))

    def test_default_owner_is_windows_user(self):
        self.write_config(owner="")
        self.assertEqual(s4l.load_config(self.cfg_path).owner, s4l.getpass.getuser())


if __name__ == "__main__":
    unittest.main(verbosity=2)
