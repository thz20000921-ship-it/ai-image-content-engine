"""Exercise the production launcher on isolated empty data, without model credentials."""
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine_fixture import PROJECT
import httpx

evidence = PROJECT / "验证" / "证据" / ("独立启动-" + uuid.uuid4().hex[:8])
evidence.mkdir(parents=True)
data = evidence / "fresh-data"
launcher = PROJECT / "runtime" / "app" / "run_engine.py"
with socket.socket() as occupied:
    occupied.bind(("127.0.0.1", 0)); occupied.listen()
    port = occupied.getsockname()[1]
    failed = subprocess.run([sys.executable, str(launcher), "--port", str(port), "--data-dir", str(data)],
        capture_output=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert failed.returncode == 3, failed.stderr.decode(errors="replace")
    assert b"occupied" in failed.stdout
    assert not data.exists()

with socket.socket() as reserve:
    reserve.bind(("127.0.0.1", 0))
    port = reserve.getsockname()[1]
log = (evidence / "launcher.log").open("wb")
process = subprocess.Popen([sys.executable, str(launcher), "--port", str(port), "--data-dir", str(data)],
    stdout=log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
try:
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as client:
        until = time.monotonic() + 20
        while time.monotonic() < until:
            if process.poll() is not None:
                raise AssertionError("Production launcher exited; see isolated launcher.log")
            try:
                response = client.get("/api/health")
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(.1)
        else:
            raise AssertionError("Production launcher did not start")
        token = (data / "webui-outputs/source-data/company-workflow/agent-token.txt").read_text().strip()
        client.headers["Authorization"] = "Bearer " + token
        status = client.get("/api/engine/status")
        status.raise_for_status()
        assert not any(p["ready"] for p in status.json()["providers"])
        assert client.get("/api/engine/jobs").json()["jobs"] == []
        assert "enginePageCopy" in client.get("/").text
        assert client.get("/static/engine.css").status_code == 200
    baseline = json.loads((PROJECT / "验证/fixtures/改造前校验.json").read_text(encoding="utf-8"))
    for relative, expected in baseline["sha256"].items():
        assert hashlib.sha256((PROJECT / relative).read_bytes()).hexdigest() == expected, relative
    report = {"production_launcher": "passed", "empty_personal_data": "passed",
              "occupied_port_rejected": True, "other_processes_terminated": False,
              "api_auth": "passed", "real_model_calls": 0, "configured_real_provider": False,
              "license_and_pyproject_preserved": True, "evidence_directory": str(evidence.resolve())}
    (PROJECT / "验证/独立启动验证结果.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
finally:
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill(); process.wait(timeout=5)
    log.close()
