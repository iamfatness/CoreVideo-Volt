"""Build and exercise the installed wheel from outside the source checkout."""

import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import venv
from contextlib import closing
from pathlib import Path

CONTROL = Path(__file__).resolve().parents[2] / "control"


def wait_for_port(server, log):
    deadline = time.monotonic() + 30
    while server.poll() is None and time.monotonic() < deadline:
        match = re.search(r"^Volt session on http://127\.0\.0\.1:(\d+)", log.read_text(errors="replace"), re.M)
        if match:
            return int(match[1])
        time.sleep(0.1)
    raise RuntimeError(f"Volt did not start (exit code {server.poll()}).")


def get(port, path, content_type):
    with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=5)) as conn:
        conn.request("GET", path)
        response = conn.getresponse()
        actual_type = response.headers.get_content_type()
        body = response.read()
        if response.status != 200 or actual_type != content_type or not body:
            raise RuntimeError(f"GET {path}: expected 200 with {content_type}, got {response.status} {actual_type}.")
        return body


def smoke_test(python, work, env):
    log = work / "serve.log"
    with log.open("wb") as output, subprocess.Popen(
        [python, "-I", "-m", "volt", "serve", "--host", "127.0.0.1", "--port", "0"],
        cwd=work, env=env, stdout=output, stderr=subprocess.STDOUT,
    ) as server:
        try:
            port = wait_for_port(server, log)
            if json.loads(get(port, "/ready", "application/json")) != {"status": "ready"}:
                raise RuntimeError("Volt reported an unexpected readiness status.")
            html = get(port, "/", "text/html")
            if b"<title>Volt operator</title>" not in html:
                raise RuntimeError("The root page did not serve the operator UI.")
            get(port, "/operator/theme.js", "text/javascript")
            get(port, "/operator/operator.css", "text/css")
            get(port, "/operator/operator.js", "text/javascript")
        except Exception:
            print(log.read_text(errors="replace"), file=sys.stderr)
            raise
        finally:
            if server.poll() is None:
                server.kill()
    print("Installed wheel passed readiness and operator UI checks.")


def main():
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("VOLT_") and key not in {"PYTHONPATH", "PYTHONHOME"}}
    with tempfile.TemporaryDirectory(prefix="volt-wheel-") as directory:
        work = Path(directory)
        subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", work, CONTROL],
                       cwd=work, env=env, check=True)
        wheel, = work.glob("volt_control-*.whl")
        venv.create(work / "venv", with_pip=True)
        scripts = work / "venv" / ("Scripts" if os.name == "nt" else "bin")
        extension = ".exe" if os.name == "nt" else ""
        python = scripts / f"python{extension}"
        subprocess.run([python, "-m", "pip", "install", wheel], cwd=work, env=env, check=True)
        subprocess.run([scripts / f"volt{extension}", "version"], cwd=work, env=env, check=True)
        smoke_test(python, work, env)


if __name__ == "__main__":
    main()
