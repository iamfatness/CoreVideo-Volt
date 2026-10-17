"""Service files and the settings file they use. Nothing here installs a service."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import volt
from volt import envfile

CONTROL = Path(volt.__file__).resolve().parents[1]
DEPLOY = CONTROL.parent / "deploy"
POWERSHELL = shutil.which("powershell") if os.name == "nt" else None


class EnvFile(unittest.TestCase):
    def test_format(self):
        text = "\ufeff# comment\n\nVOLT_PORT=9000\nexport VOLT_HOST = 0.0.0.0 \nVOLT_DB=\"C:\\Volt\\v.db\"\nVOLT_X='a b'\nEMPTY=\n"
        self.assertEqual(envfile.parse(text), {"VOLT_PORT": "9000", "VOLT_HOST": "0.0.0.0", "VOLT_DB": "C:\\Volt\\v.db",
                                               "VOLT_X": "a b", "EMPTY": ""})

    def test_a_bad_line_is_named_by_number_and_never_echoed(self):
        with self.assertRaises(envfile.EnvFileError) as ctx:
            envfile.parse("VOLT_PORT=1\nVOLT_TOKEN hunter2-secret-value\n", "volt.env")
        self.assertIn("line 2", str(ctx.exception))
        self.assertNotIn("hunter2", str(ctx.exception))

    def test_the_real_environment_wins_and_a_missing_file_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "volt.env"
            path.write_text("VOLT_PORT=1\nVOLT_HOST=h\n", encoding="utf-8")
            self.assertEqual(envfile.load(path, {"VOLT_PORT": "9"}), {"VOLT_HOST": "h"})
            with self.assertRaises(envfile.EnvFileError):
                envfile.load(Path(tmp) / "nope.env", {})

    def test_the_shipped_examples_parse(self):
        for rel in ("systemd/volt.env.example", "windows/volt.env.example"):
            values = envfile.parse((DEPLOY / rel).read_text(encoding="utf-8"), rel)
            self.assertIn("VOLT_DB", values)
            self.assertIn("VOLT_MIN_FREE_MB", values)

    def test_check_reads_the_file_and_does_not_print_its_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "volt.env"
            path.write_text(f"VOLT_DB={Path(tmp) / 'v.db'}\nVOLT_TOKEN=file-token-abcdef-0123456789\n"
                            f"VOLT_PROXY_DIR={Path(tmp) / 'p'}\nVOLT_MIN_FREE_MB=1\nVOLT_PORT=0\n", encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if not k.startswith("VOLT_")}
            out = subprocess.run([sys.executable, "-m", "volt", "check", "--env-file", str(path)], cwd=CONTROL,
                                 capture_output=True, text=True, timeout=120, env=env)
            self.assertIn(str(Path(tmp) / "v.db"), out.stdout)
            self.assertIn("token required", out.stdout)
            self.assertNotIn("file-token-abcdef", out.stdout + out.stderr)
            bad = subprocess.run([sys.executable, "-m", "volt", "check", "--env-file", str(Path(tmp) / "nope")],
                                 cwd=CONTROL, capture_output=True, text=True, timeout=120, env=env)
            self.assertEqual(bad.returncode, 1)


class Systemd(unittest.TestCase):
    def setUp(self):
        text = (DEPLOY / "systemd" / "volt.service").read_text(encoding="utf-8")
        self.lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]

    def test_unit_runs_the_supervisor_and_does_not_restart_a_config_error(self):
        self.assertTrue(any(ln.startswith("ExecStart=") and " run --env-file " in ln for ln in self.lines))
        self.assertIn("RestartPreventExitStatus=78", self.lines)
        self.assertIn("Restart=on-failure", self.lines)
        self.assertIn("KillSignal=SIGTERM", self.lines)
        self.assertIn("NoNewPrivileges=true", self.lines)
        sections = [ln for ln in self.lines if ln.startswith("[")]
        self.assertEqual(sections, ["[Unit]", "[Service]", "[Install]"])

    def test_exec_start_matches_the_console_script(self):
        exec_line = next(ln for ln in self.lines if ln.startswith("ExecStart="))
        self.assertTrue(exec_line.split()[0].endswith("/volt"))
        self.assertEqual(exit_code_of_config_error(), 78)


def exit_code_of_config_error() -> int:
    from volt import supervisor
    return supervisor.EX_CONFIG


@unittest.skipUnless(POWERSHELL, "Windows PowerShell only")
class WindowsInstaller(unittest.TestCase):
    script = DEPLOY / "windows" / "install-volt-task.ps1"

    def run_ps(self, *args):
        return subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(self.script), *args],
                              capture_output=True, text=True, timeout=120)

    def test_dry_run_prints_the_plan_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "VoltData"
            out = self.run_ps("-Python", sys.executable, "-DataDir", str(data), "-DryRun")
            self.assertEqual(out.returncode, 0, out.stderr)
            plan = json.loads(out.stdout)
            self.assertEqual(plan["action"], "register")
            self.assertEqual(plan["taskName"], "Volt")
            self.assertEqual(Path(plan["execute"]).resolve(), Path(sys.executable).resolve())
            self.assertEqual(plan["arguments"], f'-m volt run --env-file "{data / "volt.env"}"')
            self.assertEqual((plan["runAs"], plan["logonType"], plan["trigger"]), ("SYSTEM", "ServiceAccount", "AtStartup"))
            self.assertEqual(plan["restartCount"], 999)
            self.assertTrue(plan["createsEnvFile"])
            self.assertEqual(len(plan["directories"]), 4)
            self.assertFalse(data.exists(), "a dry run must not create anything")

    def test_a_named_user_uses_s4u_and_a_bad_python_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self.run_ps("-Python", sys.executable, "-DataDir", tmp, "-RunAs", "VOLT\\svc", "-DryRun")
            self.assertEqual(json.loads(out.stdout)["logonType"], "S4U")
            bad = self.run_ps("-Python", str(Path(tmp) / "no-python.exe"), "-DataDir", tmp, "-DryRun")
            self.assertNotEqual(bad.returncode, 0)

    def test_uninstall_dry_run(self):
        out = self.run_ps("-Uninstall", "-DryRun", "-TaskName", "VoltTest")
        self.assertEqual(json.loads(out.stdout), {"action": "unregister", "taskName": "VoltTest"})

    def test_the_template_has_the_placeholder_the_script_replaces(self):
        text = (DEPLOY / "windows" / "volt.env.example").read_text(encoding="utf-8")
        self.assertIn("@DATA_DIR@", text)


if __name__ == "__main__":
    unittest.main()
