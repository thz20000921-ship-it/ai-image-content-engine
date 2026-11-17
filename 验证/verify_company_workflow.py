"""Offline integration checks with a fake image provider. Never contacts the paid API."""
from __future__ import annotations
import hashlib
import base64
import io
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import sys
import threading
import time
import uuid
import zipfile

project = Path(__file__).resolve().parent.parent
runtime = project / "runtime"
data = project / "验证" / ("整套流程离线数据-" + uuid.uuid4().hex[:8])
source = data / "webui-outputs" / "source-data"
source.mkdir(parents=True)
os.environ["ILAB_CONJURE_DATA_DIR"] = str(data)
sys.path.insert(0, str(runtime / "app"))

from PIL import Image, ImageChops
from fastapi.testclient import TestClient
from codex_image.client import ImageResult
from codex_image.webui.app import create_app
from codex_image.webui.provider_settings import ProviderSettings
from codex_image.webui.company_workflow import apply_watermark, workflow_root, original_paths, seed_defaults


def png(color, size=(300, 400)):
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, "PNG")
    return output.getvalue()


fixture = png("#f2f7f4", (900, 1200))
calls = []
active = 0
max_active = 0
guard = threading.Lock()
fail_page_three = True
fake_delay = .025


class FakeClient:
    def generate_image(self, **kw):
        global active, max_active
        target = int(re.search(r"仅生成第 (\d+) 页", kw["prompt"]).group(1))
        with guard:
            active += 1
            max_active = max(max_active, active)
            first = base64.b64decode(kw["reference_images"][0].split(",", 1)[1])
            calls.append({"page": target, "reference_count": len(kw["reference_images"]), "size": kw["size"], "prompt": kw["prompt"], "target_first":first == png((20+(target-1)*11,60+(target-1)*7,100+(target-1)*4))})
        try:
            time.sleep(fake_delay)
            if fail_page_three and target == 3:
                raise RuntimeError("OFFLINE_FIXTURE: reference limit rejected on page 3")
            return ImageResult(image_bytes=fixture, revised_prompt="", output_format="png", size="900x1200", background="opaque", quality="medium", usage={})
        finally:
            with guard:
                active -= 1


ProviderSettings(data / "webui-api-settings.json").write({
    "providers": [{"id":"offline", "name":"Offline fixture", "base_url":"https://fixture.invalid/v1", "api_key":"offline-fixture-credential", "image_model":"gpt-image-2", "api_mode":"images", "images_concurrency":1}],
    "active_provider_id":"offline",
})
settings_hash = hashlib.sha256((data / "webui-api-settings.json").read_bytes()).hexdigest()
original_connect = socket.socket.connect


def offline_connect(sock, address):
    if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1", "localhost"):
        raise AssertionError("External network disabled during this test")
    return original_connect(sock, address)


socket.socket.connect = offline_connect


def make_app():
    return create_app(input_root=data / "webui-inputs", output_root=data / "webui-outputs", source_data_root=source,
        client_factory=lambda: FakeClient(), auth_checker=lambda: True, auto_start_queue=True,
        # 2026-11-12: 公司任务改成自动重试共 3 次（首次 + 2 次重试），见下方 calls 计数。
        auto_retry=True, batch_delay_seconds=0,
        **{name + "_path": data / filename for name, filename in {
            "webui_settings":"webui-settings.json", "auth_settings":"webui-auth-settings.json",
            "api_settings":"webui-api-settings.json", "network_egress_settings":"webui-network-egress-settings.json",
            "color_settings":"webui-color-settings.json", "prompt_snippets":"webui-prompt-snippets.json",
            "prompt_templates":"webui-prompt-templates.json",
        }.items()})


def form(count, selected=None, ident=None):
    return {"batch_id": ident or uuid.uuid4().hex, "prompt":"OFFLINE: 保留参考图布局，封面出现女生陪驾。", "changes":"全套汽车换成保时捷，车身改为粉红色；人物换成卡通人物；仅第1页背景改为浅蓝色。", "selected_pages":json.dumps(selected or list(range(1, count + 1))), "confirmed_count":len(selected or list(range(1, count + 1))), "opacity":13, "quality":"medium"}


def files(count):
    return [("reference_images", (f"page-{i+1}.png", png((20+i*11, 60+i*7, 100+i*4)), "image/png")) for i in range(count)]


def wait_batch(client, ident):
    limit = time.monotonic() + 20
    while time.monotonic() < limit:
        batch = client.get(f"/api/company-workflow/batches/{ident}").json()
        if all(p["status"] in {"completed", "failed", "partial_failed"} for p in batch["pages"]):
            return batch
        time.sleep(.05)
    raise AssertionError(batch)


app = make_app()
with TestClient(app, base_url="http://127.0.0.1") as client:
    assert client.get("/").status_code == 200
    assert 'id="companyApp"' in client.get("/").text and "上传一套参考图" in client.get("/").text
    config = client.get("/api/company-workflow").json()
    assert config["provider"]["ready"]
    logo = client.get("/api/company-workflow/logo").content
    assert hashlib.sha256(logo).hexdigest() == hashlib.sha256((runtime / "app/company-defaults/新手驾到logo-.png").read_bytes()).hexdigest()
    preview = client.get("/api/company-workflow/preview").content
    (project / "验证/固定水印-白底预览.png").write_bytes(preview)
    marked = apply_watermark(fixture, logo)
    with Image.open(io.BytesIO(fixture)) as a, Image.open(io.BytesIO(marked)) as b:
        assert a.size == b.size and ImageChops.difference(a.convert("RGB"), b.convert("RGB")).getbbox()
    (project / "验证/固定水印-本地样图.png").write_bytes(marked)
    invalid = form(5); invalid["confirmed_count"] = 4
    assert client.post("/api/company-workflow/batches", data=invalid, files=files(5)).status_code == 400
    assert len(calls) == 0
    duplicate = files(1) * 2
    assert client.post("/api/company-workflow/batches", data=form(2), files=duplicate).status_code == 400
    five_form = form(5)
    response = client.post("/api/company-workflow/batches", data=five_form, files=files(5))
    assert response.status_code == 200, response.text
    first = wait_batch(client, five_form["batch_id"])
    assert [p["page_number"] for p in first["pages"]] == list(range(1, 6))
    # 5 页 + 失败页自动重试 2 次（共 3 次尝试）。
    assert len(calls) == 7 and max_active == 1
    assert all(c["reference_count"] == 1 and c["size"] == "1152x1536" for c in calls)
    assert all(c["target_first"] and "全套汽车换成保时捷" in c["prompt"] and "本套明确修改要求 > 基础指令" in c["prompt"] for c in calls)
    assert first["reference_strategy"] == "target_only"
    for page in first["pages"]:
        assert page["reference_url"] and page["reference_thumbnail_url"]
        assert client.get(page["reference_url"]).content == png((20+(page['page_number']-1)*11,60+(page['page_number']-1)*7,100+(page['page_number']-1)*4))
        meta = app.state.ctx.storage.read_metadata(page["task_id"])
        assert len(meta["reference_assets"]) == 5  # Full source set retained; only the target is transmitted.
        assert meta["params"]["company_page"]["target_asset_id"] == meta["reference_image_order"][0]["id"]
    assert all("唯一附件就是原套第" in c["prompt"] and "不附带其他页面" in c["prompt"] for c in calls)
    assert sum(p["status"] == "completed" for p in first["pages"]) == 4
    failed = first["pages"][2]
    assert failed["status"] == "failed" and "OFFLINE_FIXTURE" in failed["error"]
    original_output_hashes = {}
    for page in first["pages"]:
        if page.get("url"):
            output = client.get(page["url"])
            assert output.status_code == 200
            original_output_hashes[page["task_id"]] = hashlib.sha256(output.content).hexdigest()
            assert client.get(f"/api/company-workflow/tasks/{page['task_id']}/original").content == fixture
    replay = client.post("/api/company-workflow/batches", data=five_form, files=files(5))
    assert replay.status_code == 200 and len(calls) == 7
    five_form["quality"] = "high"
    assert client.post("/api/company-workflow/batches", data=five_form, files=files(5)).status_code == 409
    fail_page_three = False
    assert client.post(f"/api/tasks/{failed['task_id']}/retry-failed", json={}).status_code == 200
    retried = wait_batch(client, first["id"])
    assert all(p["status"] == "completed" for p in retried["pages"]) and len(calls) == 8
    for page in retried["pages"]:
        if page["task_id"] in original_output_hashes:
            assert hashlib.sha256(client.get(page["url"]).content).hexdigest() == original_output_hashes[page["task_id"]]
    zipped = client.get(f"/api/company-workflow/batches/{first['id']}/download")
    assert zipped.status_code == 200
    with zipfile.ZipFile(io.BytesIO(zipped.content)) as archive:
        assert len([n for n in archive.namelist() if n.endswith(".png")]) == 5
        assert archive.namelist()[0] == "01-第1页.png"
    seven_form = form(7, [1, 2, 3, 4, 5])
    response = client.post("/api/company-workflow/batches", data=seven_form, files=files(7))
    assert response.status_code == 200, response.text
    seven = wait_batch(client, seven_form["batch_id"])
    assert all(p["status"] == "completed" for p in seven["pages"])
    assert len(calls) == 13 and all(c["reference_count"] == 1 for c in calls[-5:])
    assert [c["page"] for c in calls[-5:]] == [1, 2, 3, 4, 5]
    dynamic_cases = {}
    for reference_count in (2, 3, 6, 7):
        start = len(calls)
        dynamic_form = form(reference_count)
        response = client.post("/api/company-workflow/batches", data=dynamic_form, files=files(reference_count))
        assert response.status_code == 200, response.text
        dynamic = wait_batch(client, dynamic_form["batch_id"])
        assert dynamic["request_count"] == reference_count
        assert all(p["status"] == "completed" for p in dynamic["pages"])
        assert len(calls) - start == reference_count and all(c["target_first"] and c["reference_count"] == 1 for c in calls[start:])
        dynamic_cases[str(reference_count)] = {"jobs":reference_count,"fake_outputs":reference_count,"passed":True}
    # Follow the configured provider limit rather than rejecting a non-1 setting.
    saved_api = (data / "webui-api-settings.json").read_bytes()
    fixture_settings = app.state.ctx.api_settings.read()
    fixture_settings["providers"][0]["concurrency"] = 3
    app.state.ctx.api_settings.write(fixture_settings)
    assert client.get("/api/company-workflow").json()["provider"]["ready"]
    assert client.get("/api/company-workflow").json()["provider"]["concurrency"] == 3
    fake_delay = .5
    parallel_form = form(3)
    start = len(calls)
    response = client.post("/api/company-workflow/batches", data=parallel_form, files=files(3))
    assert response.status_code == 200, response.text
    parallel = wait_batch(client, parallel_form["batch_id"])
    assert all(p["status"] == "completed" for p in parallel["pages"])
    assert max_active == 3 and len(calls) - start == 3
    assert all(c["reference_count"] == 1 and c["target_first"] for c in calls[start:])
    for page in parallel["pages"]:
        meta = app.state.ctx.storage.read_metadata(page["task_id"])
        assert meta["api_images_concurrency"] == 3 and meta["generation_snapshot"]["provider_concurrency"] == 3
    fake_delay = .025
    (data / "webui-api-settings.json").write_bytes(saved_api)
    # A local watermark fault is recoverable from the saved raw output without purchasing a new image.
    import codex_image.webui.company_workflow as workflow
    real_apply = workflow.apply_watermark
    def fail_mark(*args, **kwargs):
        raise RuntimeError("OFFLINE local watermark failure")
    workflow.apply_watermark = fail_mark
    local_form = form(1)
    response = client.post("/api/company-workflow/batches", data=local_form, files=files(1))
    assert response.status_code == 200, response.text
    broken = wait_batch(client, local_form["batch_id"])
    assert broken["pages"][0]["status"] == "failed" and broken["pages"][0]["original_available"]
    workflow.apply_watermark = real_apply
    call_count = len(calls)
    assert client.post(f"/api/tasks/{broken['pages'][0]['task_id']}/retry-failed", json={}).status_code == 200
    repaired = wait_batch(client, local_form["batch_id"])
    assert repaired["pages"][0]["status"] == "completed" and len(calls) == call_count
    # Local repeat must use the original and never compound the watermark.
    store = app.state.ctx.storage
    task = repaired["pages"][0]["task_id"]
    meta = store.read_metadata(task)
    target = store.output_path(meta["outputs"][0]["file"])
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    store.write_output(task, fixture, "png", index=1)
    assert hashlib.sha256(target.read_bytes()).hexdigest() == digest
    assert client.put("/api/company-workflow", json={"prompt":"保留的个人指令", "opacity":11}).status_code == 200
    # Deleting a fixture task through storage also removes its retained raw image.
    raw, raw_info = original_paths(store, task)
    store.delete_task(task)
    assert not raw.exists() and not raw_info.exists()
    deleted_page = first["pages"][0]["task_id"]
    store.delete_task(deleted_page)
    download_after_delete = client.get(f"/api/company-workflow/batches/{first['id']}/download")
    assert download_after_delete.status_code == 200
    with zipfile.ZipFile(io.BytesIO(download_after_delete.content)) as archive:
        assert len([n for n in archive.namelist() if n.endswith(".png")]) == 4
    assert client.get("/api/company-workflow/batches/../../bad").status_code in (400,404)
    assert hashlib.sha256((data / "webui-api-settings.json").read_bytes()).hexdigest() == settings_hash

# New app instance simulates replacing program/restarting with the same personal directory.
app2 = make_app()
with TestClient(app2, base_url="http://127.0.0.1") as client:
    config2 = client.get("/api/company-workflow").json()
    assert config2["prompt"] == "保留的个人指令" and config2["opacity"] == 11
    assert config2["logo_sha256"] == config["logo_sha256"]
    assert len(client.get("/api/company-workflow/batches").json()["batches"]) == 8
    assert len(calls) == call_count
    assert len(client.get("/api/tasks").json()["tasks"]) == 30

for database in source.glob("*.db"):
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
report = {"passed":True, "external_api_requests":0, "offline_fake_calls":len(calls),
          "five_references_five_jobs":True, "seven_references_five_selected_jobs":True,
          "dynamic_page_counts":dynamic_cases, "per_run_changes_override_base_prompt":True, "target_reference_sent_first":True,
          "only_target_page_transmitted":True, "full_set_retained_locally":True, "source_result_comparison_mapping":True,
          "configured_concurrency_three_supported":True, "serial_concurrency_one_verified":True,
          "concurrency_max":max_active, "failed_page_preserved":True, "manual_retry_only_failed_page":True,
          "auto_retry_three_attempts":True,
          "idempotent_submission":True, "watermark_raw_retained":True, "watermark_repair_no_api":True,
          "watermark_no_double_application":True, "ordered_zip":True, "settings_and_history_after_restart":True,
          "sqlite_integrity":"ok", "fixture_directory":str(data)}
(project / "验证/整套流程离线验证结果.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=2))
