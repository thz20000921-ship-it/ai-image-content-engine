"""Integration acceptance with an explicit fake provider and all external sockets blocked."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import io
import json
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine_fixture as fixture
from engine_fixture import CALLS, FAIL, make_app, png, example, PROJECT
from fastapi.testclient import TestClient
from codex_image.webui import engine, company_analyze
from codex_image.webui.company_workflow import seed_defaults, workflow_root, original_paths, write_json
from codex_image.webui.recovery import _recover_queue_state
from codex_image.webui.cancellation import requeue_task_after_shutdown
from codex_image.webui.routes import engine as engine_routes
from PIL import Image, ImageChops

RESULTS = []
external_connect = socket.socket.connect


def guard_connect(sock, address):
    if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1", "localhost"}:
        raise AssertionError("External network disabled in offline acceptance")
    return external_connect(sock, address)


socket.socket.connect = guard_connect


class EngineAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="image-engine-offline-")
        self.data = Path(self.temp.name)
        CALLS.clear()
        FAIL.clear()
        fixture.DELAY = .01

    def tearDown(self):
        self.temp.cleanup()

    @contextmanager
    def service(self, start=True):
        app = make_app(self.data, start=start)
        self.app = app
        self.ctx = app.state.ctx
        token = (workflow_root(self.ctx.storage) / "agent-token.txt").read_text()
        with TestClient(app, base_url="http://127.0.0.1",
                        headers={"Authorization": "Bearer " + token}) as client:
            self.client = client
            yield client

    def post(self, path, value):
        response = self.client.post("/api/engine" + path, json=value)
        self.assertEqual(response.status_code, 200, response.text[:1500])
        return response.json()

    def wait(self, key, timeout=15):
        deadline = time.monotonic() + timeout
        value = None
        while time.monotonic() < deadline:
            self.ctx.route_helpers["company_batches"]["promote"]()
            response = self.client.get("/api/engine/jobs/" + key)
            self.assertEqual(response.status_code, 200, response.text)
            value = response.json()
            if value["status"] in {"completed", "failed", "partial_failed", "cancelled"}:
                return value
            time.sleep(.04)
        self.fail(f"Job did not settle: {key}: {value}")

    def asset(self, label="自制咖啡冲煮说明（离线参考）"):
        response = self.client.post("/api/engine/assets", files=[("images", ("fixture.png", png(label), "image/png"))])
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["assets"][0]["id"]

    def assert_delivery_manifest(self, archive, job):
        folder = job["job_id"]
        text = archive.read(folder + "/manifest.json").decode("utf-8")
        manifest = json.loads(text)
        self.assertEqual(manifest["job_id"], folder)
        self.assertEqual(manifest["total_pages"], len(job["pages"]))
        for field in ("budgets", "revisions", "download_url", "engine_request", "changes"):
            self.assertNotIn(field, manifest)
        for path in (self.data, self.data.resolve()):
            self.assertNotIn(json.dumps(str(path), ensure_ascii=False)[1:-1], text)
        self.assertEqual(len(manifest["pages"]), len(job["pages"]))
        for exported, current in zip(manifest["pages"], job["pages"], strict=True):
            path = exported["output_path"]
            self.assertEqual(path, f"{current['page_number']:02d}-第{current['page_number']}页.png")
            self.assertFalse(PurePosixPath(path).is_absolute())
            self.assertFalse(PureWindowsPath(path).drive)
            self.assertEqual(PurePosixPath(path).parts, (path,))
            for field in ("page_number", "task_id", "version", "sha256", "expected_text"):
                self.assertEqual(exported[field], current[field])
            self.assertEqual(hashlib.sha256(archive.read(folder + "/" + path)).hexdigest(), current["sha256"])
            self.assertNotIn("download_url", exported)
        return manifest

    def test_01_local_security_and_secret_redaction(self):
        with self.service(start=False) as client:
            self.assertEqual(client.get("/api/engine/status", headers={"Authorization": ""}).status_code, 401)
            value = client.get("/api/engine/status")
            self.assertEqual(value.status_code, 200)
            self.assertNotIn("offline-placeholder-not-a-real-key", value.text)
            self.assertNotIn("api_key_masked", value.text)
            self.assertEqual(value.json()["limits"]["jobs_per_batch"], 50)
            self.assertEqual(client.post("/api/engine/jobs", json=example(), headers={"Origin": "https://foreign.invalid"}).status_code, 403)
            self.assertEqual(client.get("/api/engine/status", headers={"Host": "foreign.invalid"}).status_code, 400)
            self.assertEqual(client.get("/api/engine/session").status_code, 403)
            session = client.get("/api/engine/session", headers={"x-requested-with": "image-engine-webui",
                                                                 "sec-fetch-site": "same-origin"})
            self.assertEqual(session.status_code, 200)
            self.assertEqual(session.headers["cache-control"], "no-store")
            self.assertEqual(client.post("/api/company-workflow/analyze").status_code, 422)
            self.assertEqual(sum(r.path == "/api/company-workflow/analyze" for r in self.app.routes), 1)
            bad = example(); bad["pages"][1]["page_number"] = 1
            self.assertEqual(client.post("/api/engine/jobs", json=bad).status_code, 422)
            zero = example(); zero["authorization"] = {"approved": False, "max_image_requests": 0}
            self.assertEqual(client.post("/api/engine/jobs", json=zero).status_code, 400)
            money = example("money"); money["authorization"].update(max_cost=1, currency="CNY")
            self.assertEqual(client.post("/api/engine/jobs", json=money).status_code, 400)
            self.assertEqual(len(CALLS), 0)

    def test_02_exact_old_settings_backup_and_migration(self):
        root = self.data / "migration"
        root.mkdir()
        old = {"schema_version": 1, "prompt": "自定义前缀\n陪驾精准引流版：女生与陪驾，新手驾到\n个人备注保留",
               "analysis_api_key": "local-secret-sentinel", "api_key": "preserve-too", "opacity": 12,
               "logo_sha256": "a" * 64, "logo_name": "新手驾到logo-.png", "download_dir": "D:/user-place",
               "custom_value": 42, "copy_rules": ["封面女生与陪驾", "个人自定义规则"]}
        raw = json.dumps(old, ensure_ascii=False).encode("utf-8")
        (root / "settings.json").write_bytes(raw)
        (root / "history.png").write_bytes(png("历史结果"))
        old_image = (root / "history.png").read_bytes()
        config = seed_defaults(root)
        self.assertEqual(Path(config["migration"]["backup"]).read_bytes(), raw)
        self.assertEqual(config["analysis_api_key"], old["analysis_api_key"])
        self.assertEqual(config["api_key"], old["api_key"])
        self.assertEqual(config["legacy_inactive_prompt"], old["prompt"])
        self.assertEqual(config["legacy_inactive_copy_rules"], old["copy_rules"])
        self.assertEqual(config["copy_rules"], ["个人自定义规则"])
        self.assertEqual(config["custom_value"], 42)
        self.assertEqual(config["download_dir"], old["download_dir"])
        self.assertFalse(config["watermark_enabled"])
        self.assertNotIn("陪驾", config["prompt"])
        self.assertEqual((root / "history.png").read_bytes(), old_image)
        self.assertEqual(seed_defaults(root), config)
        custom = self.data / "custom"
        custom.mkdir()
        write_json(custom / "settings.json", {"schema_version": 1, "prompt": "用户自写烘焙风格", "download_dir": "custom"})
        self.assertEqual(seed_defaults(custom)["prompt"], "用户自写烘焙风格")
        derived = self.data / "derived"; derived.mkdir()
        write_json(derived / "settings.json", {"schema_version": 1, "prompt": "封面必须出现女生陪驾"})
        self.assertNotIn("陪驾", seed_defaults(derived)["prompt"])
        baseline = json.loads((PROJECT / "验证/fixtures/改造前校验.json").read_text(encoding="utf-8"))
        factory = baseline["legacy_copy_rules"]
        stock = self.data / "stock"; stock.mkdir()
        write_json(stock / "settings.json", {"schema_version": 1, "prompt": "保留个人通用指令",
                                            "copy_rules": factory + ["保留我的品牌配色要求"]})
        self.assertEqual(seed_defaults(stock)["copy_rules"], ["保留我的品牌配色要求"])

    def test_03_old_pending_paused_and_history_retained(self):
        app = make_app(self.data, start=False)
        ctx = app.state.ctx
        root = workflow_root(ctx.storage)
        task = ctx.storage.create_task("generate")
        meta = {"task_id": task.task_id, "created_at": "2026-11-16T00:00:00+00:00",
                "updated_at": "2026-11-16T00:00:00+00:00", "mode": "generate", "status": "queued",
                "prompt": "旧行业提示词", "params": {"company_page": {"batch_id": "a" * 32}}, "outputs": []}
        ctx.storage.write_metadata(task.task_id, meta)
        ctx.queue_storage.enqueue(task.task_id)
        write_json(root / "batches" / ("a" * 32 + ".json"),
                   {"id": "a" * 32, "created_at": meta["created_at"], "submission_state": "submitted",
                    "request_count": 1, "pages": [{"task_id": task.task_id, "page_number": 1}]})
        with self.service(start=False) as client:
            self.assertEqual(self.ctx.storage.read_metadata(task.task_id)["status"], "paused")
            self.assertNotIn(task.task_id, self.ctx.queue_storage.read_state()["waiting"])
            batch = client.get("/api/company-workflow/batches/" + "a" * 32).json()
            self.assertEqual(batch["submission_state"], "needs_review")
            self.assertEqual(client.post("/api/company-workflow/batches/" + "a" * 32 + "/resume").status_code, 409)
            self.assertTrue((root / "migration-backups" / f"{task.task_id}-metadata.json").exists())

    def test_04_no_reference_exact_copy_and_idempotency(self):
        with self.service() as client:
            job = example("original", 5)
            before = (self.data / "webui-api-settings.json").read_bytes()
            self.post("/jobs", job)
            value = self.wait(job["task_id"])
            self.assertEqual(value["status"], "completed")
            self.assertEqual(len(CALLS), 5)
            self.assertTrue(all(c["references"] == 0 for c in CALLS))
            self.assertEqual(value["budgets"][0]["used"], 5)
            self.assertEqual((self.data / "webui-api-settings.json").read_bytes(), before)
            for call, expected, row in zip(CALLS, job["pages"], value["pages"], strict=True):
                for forbidden in ("陪驾", "女生", "新手驾到", "保时捷"):
                    self.assertNotIn(forbidden, call["prompt"])
                self.assertIn(expected["copy"], call["prompt"])
                meta = self.ctx.storage.read_metadata(row["task_id"])
                self.assertEqual(meta["params"]["n"], 1)
                self.assertEqual(meta["generation_snapshot"]["remote_model_id"], "offline-remote-model")
                self.assertTrue(row["quality_check"]["file_valid"])
                self.assertEqual(row["quality_check"]["text_accuracy"], "unverified")
                image = client.get(row["download_url"])
                self.assertEqual(image.content[:8], b"\x89PNG\r\n\x1a\n")
                self.assertEqual(client.get(f"/api/company-workflow/tasks/{row['task_id']}/original").content, image.content)
            self.post("/jobs", job)
            self.assertEqual(len(CALLS), 5)
            different = deepcopy(job); different["theme"] = "Different"
            self.assertEqual(client.post("/api/engine/jobs", json=different).status_code, 409)
            self.assertTrue(value["content_check"]["page_sequence_valid"])
            self.assertFalse(value["content_check"]["duplicate_expected_text_pages"])
            zipped = client.get(value["download_url"])
            self.assertEqual(zipped.status_code, 200)
            with zipfile.ZipFile(io.BytesIO(zipped.content)) as archive:
                self.assertEqual(len([n for n in archive.namelist() if n.endswith(".png")]), 5)

    def test_05_one_reference_five_outputs_and_source_reuse(self):
        with self.service():
            asset = self.asset()
            first = example("inspired-1", 5)
            first.update(mode="inspired", references=[{"asset_id": asset, "source": "自制咖啡行业参考", "rights": "inspiration"}])
            self.post("/jobs", first)
            result = self.wait(first["task_id"])
            self.assertEqual(result["status"], "completed")
            self.assertEqual([c["references"] for c in CALLS], [1, 0, 0, 0, 0])
            self.assertTrue(all("橘子智剪" in c["prompt"] for c in CALLS))
            second = deepcopy(first); second.update(task_id="inspired-2", theme="新的原创内容主题")
            self.post("/jobs", second)
            self.assertEqual(self.wait(second["task_id"])["status"], "completed")
            self.assertEqual(len(CALLS), 10)
            denied = deepcopy(first); denied["task_id"] = "bad-edit"; denied["mode"] = "edit"
            self.assertEqual(self.client.post("/api/engine/jobs", json=denied).status_code, 422)

    def test_06_page_revision_only_and_version_download(self):
        with self.service() as client:
            job = example("revision", 3)
            self.post("/jobs", job)
            original = self.wait(job["task_id"])
            hashes = [hashlib.sha256(client.get(p["download_url"]).content).hexdigest() for p in original["pages"]]
            revision = {"revision_id": "fix-title", "pages": [{"page_number": 2, "title": "已修正的第二页标题"}],
                        "authorization": {"approved": True, "max_image_requests": 1}}
            self.post("/jobs/revision/revisions", revision)
            value = self.wait(job["task_id"])
            self.assertEqual(len(CALLS), 4)
            self.assertEqual(value["pages"][1]["version"], 2)
            self.assertEqual(value["pages"][1]["content"]["copy"], job["pages"][1]["copy"])
            for index in (0, 2):
                self.assertEqual(original["pages"][index]["task_id"], value["pages"][index]["task_id"])
                self.assertEqual(hashes[index], hashlib.sha256(client.get(value["pages"][index]["download_url"]).content).hexdigest())
            old = client.get("/api/engine/jobs/revision/pages/2/image?version=1")
            self.assertEqual(hashlib.sha256(old.content).hexdigest(), hashes[1])
            self.post("/jobs/revision/revisions", revision)
            self.assertEqual(len(CALLS), 4)
            revision["pages"][0]["title"] = "another"
            self.assertEqual(client.post("/api/engine/jobs/revision/revisions", json=revision).status_code, 409)

    def test_07_failure_explicit_retry_and_budget_limit(self):
        with self.service():
            job = example("retry", 3); job["authorization"]["max_image_requests"] = 4
            FAIL.add(2)
            self.post("/jobs", job)
            value = self.wait(job["task_id"])
            self.assertEqual(value["failed_pages"], 1)
            self.assertEqual(len(CALLS), 3)
            FAIL.clear()
            retry = {"request_id": "retry-1", "page_numbers": [2], "acknowledge_uncertain_charge": True}
            self.post("/jobs/retry/retry", retry)
            value = self.wait("retry")
            self.assertEqual(value["status"], "completed")
            self.assertEqual(len(CALLS), 4)
            self.post("/jobs/retry/retry", retry)
            self.assertEqual(len(CALLS), 4)
            FAIL.add(2)
            revision = {"revision_id": "failed-revision", "pages": [{"page_number": 2, "title": "需要修正"}],
                        "authorization": {"approved": True, "max_image_requests": 1}}
            self.post("/jobs/retry/revisions", revision)
            self.wait("retry")
            retry["request_id"] = "beyond-budget"
            self.assertEqual(self.client.post("/api/engine/jobs/retry/retry", json=retry).status_code, 409)
            self.assertEqual(len(CALLS), 5)

    def test_08_original_cache_recovery_is_free(self):
        with self.service():
            job = example("cache", 1)
            self.post("/jobs", job)
            value = self.wait("cache")
            row = value["pages"][0]
            meta = self.ctx.storage.read_metadata(row["task_id"])
            meta.update(status="failed", outputs=[{"index": 1, "status": "failed", "error": "offline local postprocess failure"}],
                        generated_count=0, failed_count=1, output_files=[])
            self.ctx.storage.write_metadata(row["task_id"], meta)
            self.post("/jobs/cache/retry", {"request_id": "local-cache", "page_numbers": [1], "acknowledge_uncertain_charge": True})
            self.assertEqual(self.wait("cache")["status"], "completed")
            self.assertEqual(len(CALLS), 1)
            self.assertEqual(self.client.get("/api/engine/jobs/cache").json()["budgets"][0]["used"], 1)

    def test_09_batch_twelve_and_twenty_independent_jobs(self):
        with self.service():
            for count in (12, 20):
                start = len(CALLS)
                jobs = [example(f"b{count}-{i}", 1 + i % 3) for i in range(count)]
                for i, job in enumerate(jobs):
                    job["theme"] = f"独立主题 {i}"
                total = sum(len(job["pages"]) for job in jobs)
                payload = {"batch_id": f"batch-{count}", "jobs": jobs,
                           "authorization": {"approved": True, "max_image_requests": total}}
                value = self.post("/batches", payload)
                self.assertEqual(len(value["jobs"]), count)
                self.assertEqual(value["expected_pages"], total)
                self.assertEqual(len({j["id"] for j in value["jobs"]}), count)
                for job in jobs:
                    self.assertEqual(self.wait(job["task_id"], timeout=25)["status"], "completed")
                value = self.client.get(f"/api/engine/batches/batch-{count}").json()
                self.assertEqual(value["budget"]["used"], total)
                self.assertEqual(len(CALLS) - start, total)
                # A whole set is finished before a subsequent set starts.
                actual = [c["page"] for c in CALLS[start:]]
                expected = [p["page_number"] for job in jobs for p in job["pages"]]
                self.assertEqual(actual, expected)
                self.post("/batches", payload)
                self.assertEqual(len(CALLS) - start, total)
                zipped = self.client.get(f"/api/engine/batches/batch-{count}/download")
                self.assertEqual(zipped.status_code, 200)
                with zipfile.ZipFile(io.BytesIO(zipped.content)) as archive:
                    self.assertEqual(len([n for n in archive.namelist() if n.endswith(".png")]), total)
                    for job in value["jobs"]:
                        self.assert_delivery_manifest(archive, job)
                RESULTS.append({"case": f"batch_{count}", "works": count, "mock_pages": total, "real_paid_requests": 0})

    def test_10_cancel_and_resume_unstarted_jobs(self):
        with self.service(start=False):
            self.post("/jobs", example("cancel", 3))
            self.assertEqual(self.post("/jobs/cancel/cancel", {})["status"], "cancelled")
            self.assertEqual(len(CALLS), 0)
            self.post("/jobs/cancel/resume", {})
            batch = self.client.get("/api/engine/jobs/cancel").json()
            self.assertTrue(all(p["status"] in {"queued", "staged"} for p in batch["pages"]))
            self.assertEqual(batch["budgets"][0]["used"], 0)
        with self.service():
            self.assertEqual(self.wait("cancel")["status"], "completed")
            self.assertEqual(len(CALLS), 3)

    def test_11_restart_recovery_and_uncertain_running(self):
        with self.service(start=False):
            self.post("/jobs", example("restart", 3))
            saved = self.client.get("/api/engine/jobs/restart").json()
            running = saved["pages"][0]["task_id"]
            meta = self.ctx.storage.read_metadata(running)
            meta.update(status="running", attempts=1)
            self.ctx.storage.write_metadata(running, meta)
            _recover_queue_state(self.ctx.storage, self.ctx.queue_storage)
            self.assertEqual(self.ctx.storage.read_metadata(running)["status"], "failed")
            self.assertNotIn(running, self.ctx.queue_storage.read_state()["waiting"])
            # Simulate a charged result cached before final metadata write.
            row = saved["pages"][1]; raw, info = original_paths(self.ctx.storage, row["task_id"])
            raw.parent.mkdir(parents=True, exist_ok=True); raw.write_bytes(png("恢复缓存"))
            write_json(info, {"format": "png", "size": "512x768"})
            meta = self.ctx.storage.read_metadata(row["task_id"]); meta.update(status="running", attempts=1)
            self.ctx.storage.write_metadata(row["task_id"], meta)
            _recover_queue_state(self.ctx.storage, self.ctx.queue_storage)
            self.assertIn(self.ctx.storage.read_metadata(row["task_id"])["status"], {"queued", "completed"})
        with self.service():
            value = self.wait("restart")
            self.assertEqual(value["completed_pages"], 2)
            self.assertEqual(value["failed_pages"], 1)
            self.assertEqual(len(CALLS), 1)

    def test_12_planning_analysis_contract_and_quality_review(self):
        with self.service():
            job = example("brief", 5); job.update(content_mode="brief", pages=[])
            plan = self.post("/plan", job)
            self.assertEqual(len(plan["job"]["pages"]), 5)
            self.assertEqual(plan["paid_calls"], 0)
            self.assertEqual(len(CALLS), 0)
            text = company_analyze.build_task(1, job)
            self.assertIn('"actual_content"', text)
            self.assertNotIn("车辆", text)
            self.assertIn("final", text)
            response = company_analyze._parse('```json\n{"sources":[],"pages":[{"page_number":1}],"visual_rules":[]}\n```')
            self.assertEqual(response["pages"][0]["page_number"], 1)
            self.post("/jobs", example("review", 1))
            value = self.wait("review")
            row = value["pages"][0]
            bad = self.post("/jobs/review/pages/1/text-review", {"observed_text": "错字"})
            self.assertEqual(bad["status"], "mismatch")
            good = self.post("/jobs/review/pages/1/text-review",
                {"observed_text": row["content"]["title"] + "\n" + row["content"]["copy"]})
            self.assertEqual(good["status"], "matched")
            self.assertEqual(good["method"], "caller_supplied_transcription")
            broken = self.data / "broken.png"; broken.write_bytes(b"not an image")
            self.assertFalse(engine.quality_check(broken, "1024x1536")["file_valid"])

    def test_13_partial_submission_and_batch_restart(self):
        with self.service(start=False):
            original = engine_routes._persist_generation_submission
            writes = []
            def disk_failure(*args, **kwargs):
                writes.append(kwargs["task_id"])
                if len(writes) == 2:
                    raise OSError("OFFLINE_DISK_FAILURE")
                return original(*args, **kwargs)
            job = example("partial-submit", 3)
            with patch.object(engine_routes, "_persist_generation_submission", side_effect=disk_failure):
                response = self.client.post("/api/engine/jobs", json=job)
            self.assertEqual(response.status_code, 503)
            self.assertFalse(self.ctx.queue_storage.read_state()["waiting"])
            saved = self.client.get("/api/engine/jobs/partial-submit").json()
            before_ids = [p["task_id"] for p in saved["pages"]]
            self.post("/jobs", job)
            after = self.client.get("/api/engine/jobs/partial-submit").json()
            self.assertEqual(before_ids, [p["task_id"] for p in after["pages"]])
            payload = {"batch_id": "restart-batch", "jobs": [example("after-restart-1", 1), example("after-restart-2", 2)],
                       "authorization": {"approved": True, "max_image_requests": 3}}
            self.post("/batches", payload)
        with self.service():
            for key in ("partial-submit", "after-restart-1", "after-restart-2"):
                self.assertEqual(self.wait(key)["status"], "completed")
            self.assertEqual(len(CALLS), 6)

    def test_14_brand_ratio_png_conversion_and_version_cleanup(self):
        with self.service() as client:
            out = io.BytesIO()
            logo = Image.new("RGBA", (100, 25), "#e80000"); logo.save(out, "PNG")
            response = client.post("/api/engine/assets", files=[("images", ("own-logo.png", out.getvalue(), "image/png"))])
            asset = response.json()["assets"][0]["id"]
            job = example("brand", 1)
            job["brand"] = {"enabled": True, "logo_asset_id": asset, "rights_confirmed": True}
            self.post("/jobs", job)
            value = self.wait("brand")
            row = value["pages"][0]
            result = client.get(row["download_url"]).content
            raw = original_paths(self.ctx.storage, row["task_id"])[0].read_bytes()
            with Image.open(io.BytesIO(result)) as marked, Image.open(io.BytesIO(raw)) as source:
                bbox = ImageChops.difference(marked.convert("RGB"), source.convert("RGB")).getbbox()
                self.assertIsNotNone(bbox)
                self.assertAlmostEqual((bbox[2]-bbox[0])/(bbox[3]-bbox[1]), 4, delta=.1)
                self.assertEqual(marked.format, "PNG")
            self.post("/jobs/brand/revisions", {"revision_id": "brand-v2", "pages": [{"page_number": 1, "title": "修改品牌页标题"}],
                                              "authorization": {"approved": True, "max_image_requests": 1}})
            after = self.wait("brand")
            tasks = [row["task_id"], after["pages"][0]["task_id"]]
            deleted = client.delete("/api/company-workflow/batches/" + after["id"])
            self.assertEqual(deleted.status_code, 200, deleted.text)
            for task in tasks:
                self.assertFalse(self.ctx.storage.metadata_path(task).exists())
                self.assertFalse(original_paths(self.ctx.storage, task)[0].exists())
            self.assertEqual(client.post("/api/engine/jobs", json=job).status_code, 410)

    def test_15_analysis_idempotency_and_confirmed_text_protection(self):
        with self.service() as client:
            root = workflow_root(self.ctx.storage)
            config = seed_defaults(root)
            config.update(analysis_model="offline-vision", analysis_base_url="https://offline.invalid/v1",
                          analysis_api_key="offline-vision-placeholder", analysis_protocol="chat_completions")
            write_json(root / "settings.json", config)
            seen = []
            class Transport:
                def request_bounded(self, **kwargs):
                    seen.append(json.loads(kwargs["body"]))
                    value = {"theme": "错误的参考主题", "sources": [{"reference_index": 1, "source": "模型猜测来源",
                              "actual_content": "咖啡资料", "role": "cover", "transferable_methods": ["信息层次"],
                              "do_not_copy": ["第三方品牌"]}], "visual_rules": ["清晰层级"],
                             "pages": [{"page_number": 1, "title": "不应覆盖的模型标题", "copy": "不应覆盖的正文"}]}
                    return SimpleNamespace(status=200, body=json.dumps(
                        {"choices": [{"message": {"content": json.dumps(value, ensure_ascii=False)}}]}).encode())
            job = example("analyze", 1)
            asset = self.asset()
            job.update(mode="inspired", references=[{"asset_id": asset, "source": "用户声明的自制参考", "rights": "owned"}])
            payload = {"analysis_id": "analysis-1", "job": job, "approved": True, "max_analysis_requests": 1}
            with patch.object(self.ctx.network_egress_manager, "transport", return_value=Transport()):
                result = self.post("/analyze", payload)
                again = self.post("/analyze", payload)
                self.assertEqual(result, again)
                self.assertEqual(len(seen), 1)
                self.assertEqual(result["analysis"]["theme"], job["theme"])
                self.assertEqual(result["analysis"]["pages"][0]["title"], job["pages"][0]["title"])
                self.assertEqual(result["analysis"]["pages"][0]["copy"], job["pages"][0]["copy"])
                self.assertEqual(result["analysis"]["sources"][0]["source"], "用户声明的自制参考")
                payload["job"]["theme"] = "Changed"
                self.assertEqual(client.post("/api/engine/analyze", json=payload).status_code, 409)
            self.assertEqual(len(CALLS), 0)

    def test_16_batch_failure_retry_keeps_successful_pages(self):
        with self.service() as client:
            jobs = [example("batch-failure-1", 2), example("batch-failure-2", 1)]
            jobs[0]["authorization"]["max_image_requests"] = 3
            FAIL.add(2)
            self.post("/batches", {"batch_id": "batch-failure", "jobs": jobs,
                "authorization": {"approved": True, "max_image_requests": 4}})
            first = self.wait(jobs[0]["task_id"])
            second = self.wait(jobs[1]["task_id"])
            self.assertEqual(first["failed_pages"], 1)
            self.assertEqual(second["status"], "completed")
            before = client.get(first["pages"][0]["download_url"]).content
            FAIL.clear()
            self.post("/jobs/batch-failure-1/retry",
                {"request_id": "batch-retry-one", "page_numbers": [2], "acknowledge_uncertain_charge": True})
            after = self.wait(jobs[0]["task_id"])
            self.assertEqual(after["status"], "completed")
            self.assertEqual(client.get(after["pages"][0]["download_url"]).content, before)
            self.assertEqual(len(CALLS), 4)
            batch = client.get("/api/engine/batches/batch-failure").json()
            self.assertEqual(batch["budget"]["used"], 4)

    def test_17_raw_format_and_platform_access_restrictions(self):
        from codex_image.webui import company_fetch
        with self.service(start=False):
            task = self.ctx.storage.create_task("generate")
            self.ctx.storage.write_metadata(task.task_id, {"task_id": task.task_id, "mode": "generate",
                "created_at": "2026-11-16T00:00:00Z", "status": "staged",
                "params": {"company_page": {"engine_schema": 1, "brand": {"enabled": False}}}})
            jpeg = io.BytesIO()
            Image.open(io.BytesIO(png("原始JPEG离线样图"))).convert("RGB").save(jpeg, "JPEG")
            path = self.ctx.storage.write_output(task.task_id, jpeg.getvalue(), "jpeg")
            self.assertEqual(path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(original_paths(self.ctx.storage, task.task_id)[0].read_bytes(), jpeg.getvalue())
            captured = {"url": "https://www.xiaohongshu.com/explore/" + "a"*24, "title": "离线参考",
                        "images": [{"url": f"https://fixture.invalid/{i}.png", "width": 300, "height": 400} for i in range(15)]}
            with patch.object(company_fetch, "_resolve_short_link", side_effect=lambda value: value), \
                 patch.object(company_fetch, "find_browser", return_value=Path("offline-browser")), \
                 patch.object(company_fetch, "capture_note", return_value=captured):
                _, _, images, _, total = company_fetch.probe(captured["url"], profile=self.data / "profile")
                self.assertEqual(len(images), 12)
                self.assertEqual(total, 15)
                captured["access_restricted"] = True
                with self.assertRaises(company_fetch.FetchError):
                    company_fetch.probe(captured["url"], profile=self.data / "profile")

    def test_18_running_cancel_does_not_repeat_uncertain_request(self):
        with self.service():
            fixture.DELAY = .8
            self.post("/jobs", example("running-cancel", 3))
            until = time.monotonic() + 5
            while not CALLS and time.monotonic() < until:
                time.sleep(.01)
            self.assertEqual(len(CALLS), 1)
            self.post("/jobs/running-cancel/cancel", {})
            while self.ctx.active_task_ids and time.monotonic() < until:
                time.sleep(.01)
            self.post("/jobs/running-cancel/resume", {})
            value = self.wait("running-cancel")
            self.assertEqual(value["completed_pages"], 2)
            self.assertEqual(value["failed_pages"], 1)
            self.assertEqual(len(CALLS), 3)
            self.assertEqual(value["budgets"][0]["used"], 3)

    def test_19_authorized_edit_fact_correction_and_set_style(self):
        with self.service() as client:
            assets = [self.asset("自有参考一"), self.asset("自有参考二")]
            job = example("edit", 2)
            job.update(mode="edit", product={"name": "纠错样例", "facts": ["旧版错误占位事实"]},
                       references=[{"asset_id": asset, "source": "自有测试材料", "rights": "owned"} for asset in assets])
            for page, asset in zip(job["pages"], assets, strict=True):
                page["asset_ids"] = [asset]
            self.post("/jobs", job)
            before = self.wait("edit")
            self.assertEqual([call["references"] for call in CALLS], [1, 1])
            revision = {"revision_id": "correct-product-fact", "product": {"name": "纠错样例", "facts": ["已经确认的更正事实"]},
                        "pages": [{"page_number": 2, "title": "产品事实已纠正", "copy": "已经确认的更正描述"}],
                        "authorization": {"approved": True, "max_image_requests": 1}}
            self.post("/jobs/edit/revisions", revision)
            after = self.wait("edit")
            self.assertEqual(before["pages"][0]["task_id"], after["pages"][0]["task_id"])
            self.assertNotIn("旧版错误占位事实", CALLS[-1]["prompt"])
            self.assertIn("已经确认的更正事实", CALLS[-1]["prompt"])
            style = {"revision_id": "whole-set-style", "visual_rules": ["全套使用新色板与统一边距"],
                     "pages": [{"page_number": 2}], "authorization": {"approved": True, "max_image_requests": 2}}
            self.assertEqual(client.post("/api/engine/jobs/edit/revisions", json=style).status_code, 400)
            style["pages"] = [{"page_number": 1}, {"page_number": 2}]
            self.post("/jobs/edit/revisions", style)
            result = self.wait("edit")
            self.assertEqual(len(CALLS), 5)
            self.assertEqual([row["version"] for row in result["pages"]], [2, 3])

    def test_20_interrupted_retry_and_duplicate_image_warning(self):
        with self.service() as client:
            job = example("retry-write-interrupted", 2)
            job["authorization"]["max_image_requests"] = 4
            FAIL.update({1, 2})
            self.post("/jobs", job)
            self.assertEqual(self.wait(job["task_id"])["failed_pages"], 2)
            FAIL.clear()
            original = self.ctx.storage.write_metadata
            staged = []
            def interrupted(task_id, metadata, **kwargs):
                if metadata.get("status") == "staged":
                    staged.append(task_id)
                    if len(staged) == 2:
                        raise OSError("OFFLINE_RETRY_WRITE_INTERRUPTED")
                return original(task_id, metadata, **kwargs)
            payload = {"request_id": "one-retry", "page_numbers": [1, 2], "acknowledge_uncertain_charge": True}
            with patch.object(self.ctx.storage, "write_metadata", side_effect=interrupted):
                with self.assertRaises(OSError):
                    self.client.post("/api/engine/jobs/retry-write-interrupted/retry", json=payload)
            self.assertEqual(len(CALLS), 2)
            self.post("/jobs/retry-write-interrupted/retry", payload)
            result = self.wait(job["task_id"])
            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(CALLS), 4)
            # Duplicate detection is exact-byte evidence, not a semantic-model claim.
            first = Path(result["pages"][0]["output_path"])
            second = Path(result["pages"][1]["output_path"])
            second.write_bytes(first.read_bytes())
            checked = client.get("/api/engine/jobs/" + job["task_id"]).json()
            self.assertEqual(checked["content_check"]["identical_image_pages"], [[1, 2]])

    def test_21_mixed_materials_select_reference_and_explicit_product_page(self):
        with self.service() as client:
            product = self.asset("用户自有产品截图（离线）")
            reference = self.asset("创意参考一（离线）")
            other = self.asset("创意参考二（离线）")
            job = example("mixed-materials", 3)
            job.update(mode="inspired", references=[
                {"asset_id": product, "purpose": "product_material", "rights": "owned"},
                {"asset_id": reference, "purpose": "reference", "rights": "owned"},
                {"asset_id": other, "purpose": "reference", "rights": "licensed"},
            ])
            job["pages"][1]["asset_ids"] = [product]
            self.post("/jobs", job)
            result = self.wait(job["task_id"])
            self.assertEqual(result["status"], "completed")
            expected = [[reference], [product], []]
            self.assertEqual([c["references"] for c in CALLS], [1, 1, 0])
            for row, selected, call in zip(result["pages"], expected, CALLS, strict=True):
                meta = self.ctx.storage.read_metadata(row["task_id"])
                self.assertEqual(meta["params"]["company_page"]["selected_asset_ids"], selected)
                if selected:
                    self.assertEqual(row["reference_url"], f"/api/reference-assets/{selected[0]}/image")
                else:
                    self.assertNotIn("reference_url", row)
                for ident in (product, reference, other):
                    self.assertEqual(ident in call["prompt"], ident in selected)
            planned = engine.Job.model_validate(job)
            for mode in ("inspired", "edit", "original"):
                current = planned.model_copy(update={"mode": mode}, deep=True)
                cover = current.pages[0]
                self.assertEqual(engine.selected_assets(current, cover), [] if mode == "original" else [reference])
                explicit = cover.model_copy(update={"asset_ids": [product, other]})
                self.assertEqual(engine.selected_assets(current, explicit), [product, other])
                self.assertEqual(engine.selected_assets(current, current.pages[2]), [])
            # Product-only input is valid for original generation, but never a default attachment.
            product_only = deepcopy(job)
            product_only.update(task_id="product-only-edit", mode="edit", references=job["references"][:1])
            self.assertEqual(client.post("/api/engine/jobs", json=product_only).status_code, 400)
            self.assertEqual(len(CALLS), 3)
            product_only["mode"] = "original"
            current = engine.Job.model_validate(product_only)
            self.assertEqual(engine.selected_assets(current, current.pages[0]), [])
            product_only["mode"] = "inspired"
            self.assertEqual(client.post("/api/engine/jobs", json=product_only).status_code, 422)
            self.assertEqual(len(CALLS), 3)

    def test_22_export_manifest_is_portable_after_page_revision(self):
        with self.service() as client:
            job = example("portable-export", 2)
            job["requirements"] = "仅本机执行所需的内部素材位置：" + str(self.data / "private-materials")
            self.post("/jobs", job)
            self.wait(job["task_id"])
            self.post("/jobs/portable-export/revisions", {
                "revision_id": "portable-page-2", "pages": [{"page_number": 2, "title": "交付版第二页"}],
                "instructions": "本机审核记录：" + str(self.data.resolve()),
                "authorization": {"approved": True, "max_image_requests": 1},
            })
            result = self.wait(job["task_id"])
            self.assertEqual([p["version"] for p in result["pages"]], [1, 2])
            self.assertTrue(all(Path(p["output_path"]).is_absolute() for p in result["pages"]))
            response = client.get(result["download_url"])
            self.assertEqual(response.status_code, 200)
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                manifest = self.assert_delivery_manifest(archive, result)
                self.assertEqual(manifest["pages"][1]["content"]["title"], "交付版第二页")
            after = client.get("/api/engine/jobs/portable-export").json()
            self.assertEqual([p["output_path"] for p in after["pages"]], [p["output_path"] for p in result["pages"]])
            self.assertEqual(len(after["pages"][1]["versions"]), 1)
            self.assertEqual(len(CALLS), 3)


if __name__ == "__main__":
    started = time.monotonic()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(EngineAcceptance)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {"date": datetime.now().astimezone().isoformat(), "mode": "OFFLINE_EXPLICIT_FAKE_PROVIDER",
              "real_model_calls": 0, "external_network": "blocked", "tests": result.testsRun,
              "failures": len(result.failures), "errors": len(result.errors),
              "elapsed_seconds": round(time.monotonic() - started, 2), "batch_cases": RESULTS,
              "unverified": ["real image model", "real vision model", "generated text accuracy",
                             "real product features and screenshots", "platform reference fetching"]}
    (PROJECT / "验证" / "通用引擎离线测试结果.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    raise SystemExit(0 if result.wasSuccessful() else 1)
