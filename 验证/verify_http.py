"""Independent TCP HTTP client -> real local FastAPI server -> explicit fake provider."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine_fixture import PROJECT, example, png
import httpx


def main():
    evidence = PROJECT / "验证" / "证据" / ("离线HTTP-" + uuid.uuid4().hex[:8])
    evidence.mkdir(parents=True)
    data = evidence / "isolated-data"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    log_path = evidence / "server.log"
    log = log_path.open("wb")
    process = subprocess.Popen([sys.executable, str(PROJECT / "验证" / "serve_offline.py"),
                                "--data-dir", str(data), "--port", str(port)],
                               stdout=log, stderr=subprocess.STDOUT,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    base = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(base_url=base, timeout=30) as client:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("Offline server exited; see evidence/server.log")
                try:
                    if client.get("/api/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.1)
            else:
                raise RuntimeError("Offline HTTP service did not start")
            client.headers["Authorization"] = "Bearer " + (
                data / "webui-outputs" / "source-data" / "company-workflow" / "agent-token.txt").read_text().strip()
            status = client.get("/api/engine/status"); status.raise_for_status()
            reference = png("自制非软件行业参考\n咖啡冲煮信息层级示意\n只作离线导入与传输测试")
            (evidence / "参考-咖啡-离线.png").write_bytes(reference)
            response = client.post("/api/engine/assets",
                files=[("images", ("coffee.png", reference, "image/png"))])
            response.raise_for_status()
            asset = response.json()["assets"][0]["id"]
            job = example("http-original-content", 5)
            job.update(mode="inspired", references=[{"asset_id": asset, "source": "自制咖啡参考图", "rights": "inspiration"}])
            response = client.post("/api/engine/jobs", json=job); response.raise_for_status()
            ident = response.json()["job_id"]

            def wait():
                until = time.monotonic() + 30
                while time.monotonic() < until:
                    progress = client.get("/api/engine/jobs/" + ident)
                    progress.raise_for_status()
                    value = progress.json()
                    if value["status"] == "completed":
                        return value
                    if value["status"] in {"failed", "partial_failed"}:
                        raise AssertionError(value)
                    time.sleep(.1)
                raise AssertionError("Job did not finish")

            result = wait()
            hashes = []
            for row in result["pages"]:
                download = client.get(row["download_url"]); download.raise_for_status()
                assert download.content.startswith(b"\x89PNG\r\n\x1a\n")
                hashes.append(hashlib.sha256(download.content).hexdigest())
                (evidence / f"模拟成品-{row['page_number']:02d}-v1.png").write_bytes(download.content)
            revised = client.post("/api/engine/jobs/" + ident + "/revisions", json={
                "revision_id": "fix-page-3", "pages": [{"page_number": 3, "title": "第3页标题已局部修正"}],
                "authorization": {"approved": True, "max_image_requests": 1}})
            revised.raise_for_status()
            final = wait()
            for index, row in enumerate(final["pages"]):
                download = client.get(row["download_url"]); download.raise_for_status()
                if index != 2:
                    assert hashlib.sha256(download.content).hexdigest() == hashes[index]
                else:
                    assert row["version"] == 2
                    (evidence / "模拟成品-03-v2.png").write_bytes(download.content)
            response = client.get(final["download_url"]); response.raise_for_status()
            (evidence / "离线模拟交付.zip").write_bytes(response.content)
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                assert len([name for name in archive.namelist() if name.endswith(".png")]) == 5
                manifest = json.loads(archive.read(ident + "/manifest.json"))
                for exported, row in zip(manifest["pages"], final["pages"], strict=True):
                    assert exported["version"] == row["version"]
                    assert exported["output_path"] == f"{row['page_number']:02d}-第{row['page_number']}页.png"
                    assert hashlib.sha256(archive.read(ident + "/" + exported["output_path"])).hexdigest() == row["sha256"]
                assert "budgets" not in manifest and "revisions" not in manifest
            example_output = evidence / "example-client"
            completed = subprocess.run([
                sys.executable, str(PROJECT / "示例/agent_client.py"), "--base-url", base,
                "--data-dir", str(data), "--task-id", "http-example-client", "--execute",
                "--approve-image-requests", "3", "--output", str(example_output)],
                capture_output=True, timeout=45, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            assert completed.returncode == 0, completed.stderr.decode(errors="replace")
            delivered = example_output / "http-example-client"
            manifest = json.loads((delivered / "manifest.json").read_text(encoding="utf-8"))
            for page in manifest["pages"]:
                assert hashlib.sha256((delivered / page["output_path"]).read_bytes()).hexdigest() == page["sha256"]
            assert str(data) not in json.dumps(manifest, ensure_ascii=False).replace("\\\\", "\\")
            (evidence / "最终进度.json").write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
            report = {"transport": "real loopback TCP HTTP, independent client process",
                      "provider": "EXPLICIT_OFFLINE_FIXTURE", "real_model_calls": 0,
                      "source_assets": 1, "output_pages": 5, "page_revision": 3,
                      "initial_requests_simulated": 5, "revision_requests_simulated": 1,
                      "other_pages_unchanged": True, "png_download": True, "zip_download": True,
                      "relative_manifest_paths": True, "example_client_portable_manifest": True,
                      "example_client_requests_simulated": 3,
                      "evidence_directory": evidence.relative_to(PROJECT).as_posix(),
                      "unverified": ["real model output", "real generated text", "product functionality"]}
            (PROJECT / "验证" / "本机HTTP验证结果.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(report, ensure_ascii=False))
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        log.close()


if __name__ == "__main__":
    main()
