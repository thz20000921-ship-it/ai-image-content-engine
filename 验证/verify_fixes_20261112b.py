"""第二批修复（R1–R4）离线验证：超时递减、staged 重启、单图只跑一次、短链/长链同一套。

全部使用假客户端 + 打桩，不联网、不计费。
"""
from __future__ import annotations
import copy
import io
import json
import os
from pathlib import Path
import sys
import time
import uuid

project = Path(__file__).resolve().parent.parent
runtime = project / "runtime"
data = project / "验证" / ("R1-R4离线数据-" + uuid.uuid4().hex[:8])
source = data / "webui-outputs" / "source-data"
source.mkdir(parents=True)
os.environ["ILAB_CONJURE_DATA_DIR"] = str(data)
sys.path.insert(0, str(runtime / "app"))

from PIL import Image
from fastapi.testclient import TestClient
from codex_image.client import ImageResult
from codex_image.webui import company_fetch
from codex_image.webui import queue_runtime
from codex_image.webui.app import create_app
from codex_image.webui.provider_settings import ProviderSettings
from codex_image.webui.recovery import _recover_queue_state
from codex_image.webui.routes.company_workflow import source_key
from codex_image.webui.storage import utc_now


def png(color, size=(300, 400)):
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, "PNG")
    return output.getvalue()


fixture = png("#f2f7f4", (900, 1200))
calls: list[str] = []
delay = .6


class FakeClient:
    def generate_image(self, **kwargs):
        calls.append(kwargs["prompt"])
        time.sleep(delay)
        return ImageResult(image_bytes=fixture, revised_prompt="", output_format="png", size="900x1200",
                           background="opaque", quality="medium", usage={})


ProviderSettings(data / "webui-api-settings.json").write({
    "providers": [{"id": "offline", "name": "Offline fixture", "base_url": "https://fixture.invalid/v1",
                   "api_key": "offline-fixture-credential", "image_model": "gpt-image-2",
                   "api_mode": "images", "images_concurrency": 1}],
    "active_provider_id": "offline",
})
# 超时 900 秒，用来证明第 2、3 次尝试被压到 300。
(data / "webui-network-egress-settings.json").write_text(json.dumps({
    "mode": "system", "custom_proxy_url": "", "image_request_timeout_seconds": 900,
    "image_request_retry_count": 0, "asset_fake_ip_dns_fallback": False,
}), encoding="utf-8")

app = create_app(
    input_root=data / "webui-inputs", output_root=data / "webui-outputs", source_data_root=source,
    client_factory=lambda: FakeClient(), auth_checker=lambda: True, auto_start_queue=True,
    auto_retry=True, batch_delay_seconds=0,
    **{name + "_path": data / filename for name, filename in {
        "webui_settings": "webui-settings.json", "auth_settings": "webui-auth-settings.json",
        "api_settings": "webui-api-settings.json", "network_egress_settings": "webui-network-egress-settings.json",
        "color_settings": "webui-color-settings.json", "prompt_snippets": "webui-prompt-snippets.json",
        "prompt_templates": "webui-prompt-templates.json",
    }.items()},
)
ctx = app.state.ctx
store = ctx.storage
queue_storage = ctx.queue_storage


def form(count: int, *, source_url: str = "", ident: str | None = None) -> dict:
    selected = list(range(1, count + 1))
    return {"batch_id": ident or uuid.uuid4().hex, "prompt": "OFFLINE: 保留参考图布局，封面出现女生陪驾。",
            "changes": "", "selected_pages": json.dumps(selected), "confirmed_count": len(selected),
            "opacity": 13, "quality": "medium", "title": "离线标题", "source_url": source_url}


def files(count: int):
    return [("reference_images", (f"page-{i + 1}.png", png((20 + i * 11, 60 + i * 7, 100 + i * 4)), "image/png"))
            for i in range(count)]


def wait_batch(client, ident, limit=45):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        batch = client.get(f"/api/company-workflow/batches/{ident}").json()
        if all(p["status"] in {"completed", "failed", "partial_failed"} for p in batch["pages"]):
            return batch
        time.sleep(.1)
    raise AssertionError(batch)


def wait_metadata(task_id, predicate, limit=45):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        meta = store.read_metadata(task_id)
        if predicate(meta):
            return meta
        time.sleep(.05)
    raise AssertionError(store.read_metadata(task_id))


result: dict = {"passed": True, "external_api_requests": 0, "fixture_directory": str(data)}
with TestClient(app, base_url="http://127.0.0.1") as client:
    # ================= R2：第二套挂起时页任务是 staged，不是 queued =================
    first_form, second_form = form(2), form(2)
    assert client.post("/api/company-workflow/batches", data=first_form, files=files(2)).status_code == 200
    first = client.get(f"/api/company-workflow/batches/{first_form['batch_id']}").json()
    assert client.post("/api/company-workflow/batches", data=second_form, files=files(2)).status_code == 200
    second = client.get(f"/api/company-workflow/batches/{second_form['batch_id']}").json()
    assert second["submission_state"] == "waiting_turn", second["submission_state"]
    second_statuses = {store.read_metadata(page["task_id"])["status"] for page in second["pages"]}
    assert second_statuses == {"staged"}, second_statuses
    first_ids = {page["task_id"] for page in first["pages"]}
    second_ids = {page["task_id"] for page in second["pages"]}
    state = queue_storage.read_state()
    queued = set(state["waiting"]) | {row.get("task_id") for row in state["running"].values()}
    assert queued and queued <= first_ids and not (queued & second_ids)
    done_first, done_second = wait_batch(client, first_form["batch_id"]), wait_batch(client, second_form["batch_id"])
    assert all(p["status"] == "completed" for p in done_first["pages"] + done_second["pages"])
    assert done_second["submission_state"] == "submitted"
    result["staged_while_waiting_turn"] = True

    # staged 的页在“重启恢复”里不能被放回队列（这正是两套同时跑的根因）。
    # 恢复前 waiting 是空的：修复前这些页是 queued，会被 recovery 追加进 waiting。
    probe_ids = [page["task_id"] for page in done_second["pages"]]
    for task_id in probe_ids:
        meta = store.read_metadata(task_id)
        meta["status"] = "staged"
        store.write_metadata(task_id, meta)
    state = queue_storage.read_state()
    state["waiting"] = []
    state["running"] = {}
    queue_storage.write_state(state)
    _recover_queue_state(store, queue_storage)
    recovered = queue_storage.read_state()
    assert not recovered["waiting"], recovered["waiting"]
    assert not (set(probe_ids) & set(recovered["waiting"]))
    assert all(store.read_metadata(task_id)["status"] == "staged" for task_id in probe_ids)
    for task_id in probe_ids:
        meta = store.read_metadata(task_id)
        meta["status"] = "completed"
        store.write_metadata(task_id, meta)
    result["staged_not_requeued_after_restart"] = True

    # ================= R1 + R3：超时递减、只有公司页重试 =================
    observed: list[tuple[str, float]] = []
    real_execute = queue_runtime._execute_stored_task

    async def spy_execute(**kwargs):
        observed.append((kwargs["task_id"], kwargs["image_request_timeout_seconds"]))
        raise RuntimeError("OFFLINE: simulated upstream failure")

    queue_runtime._execute_stored_task = spy_execute
    try:
        company_form = form(1)
        assert client.post("/api/company-workflow/batches", data=company_form, files=files(1)).status_code == 200
        company = wait_batch(client, company_form["batch_id"])
        company_task = company["pages"][0]["task_id"]
        assert company["pages"][0]["status"] == "failed"
        timeouts = [value for task_id, value in observed if task_id == company_task]
        assert timeouts == [900, 300, 300], timeouts
        assert store.read_metadata(company_task)["attempts"] == 3
        result["retry_timeout_escalation"] = timeouts

        # 非公司任务（原生单图）：复制一份公司页任务，去掉 company_page
        clone_id = store.create_task("generate").task_id
        clone = copy.deepcopy(store.read_metadata(company_task))
        clone.update(task_id=clone_id, status="queued", attempts=0, outputs=[], error="", last_error="",
                     created_at=utc_now(), started_at=None, attempt_started_at=None, updated_at=utc_now())
        clone["params"] = {key: value for key, value in clone["params"].items() if key != "company_page"}
        store.write_metadata(clone_id, clone)
        queue_storage.enqueue(clone_id)
        ctx.route_helpers["wake_queue_worker"]()
        clone_meta = wait_metadata(clone_id, lambda meta: meta.get("status") == "failed")
        assert clone_meta["attempts"] == 1, clone_meta["attempts"]
        assert len([1 for task_id, _ in observed if task_id == clone_id]) == 1
        result["native_task_single_attempt"] = True
        store.delete_task(clone_id)
    finally:
        queue_runtime._execute_stored_task = real_execute

    # ================= R4：来源去重按笔记 ID =================
    assert source_key("https://www.douyin.com/note/7684539877870924495") == "douyin:7684539877870924495"
    assert source_key("https://v.douyin.com/ACOip9973ZY/") == ""
    assert source_key("https://www.xiaohongshu.com/explore/0123456789abcdef01234567?xsec_token=abc") \
        == "xiaohongshu:0123456789abcdef01234567"
    assert source_key("https://www.xiaohongshu.com/discovery/item/0123456789ABCDEF01234567") \
        == "xiaohongshu:0123456789abcdef01234567"

    delay = .05
    note_url = "https://www.douyin.com/note/7684539877870924495"
    first_link = form(1, source_url=note_url)
    assert client.post("/api/company-workflow/batches", data=first_link, files=files(1)).status_code == 200
    stored = client.get(f"/api/company-workflow/batches/{first_link['batch_id']}").json()
    assert stored["source_key"] == "douyin:7684539877870924495", stored.get("source_key")
    wait_batch(client, first_link["batch_id"])
    other_form = client.post("/api/company-workflow/batches",
                             data=form(1, source_url="https://www.douyin.com/video/7684539877870924495?previous_page=app"),
                             files=files(1))
    assert other_form.status_code == 409 and "这条链接已经做过一套" in other_form.text, other_form.text
    fresh_form = form(1, source_url="https://www.douyin.com/note/7699999999999999999")
    fresh = client.post("/api/company-workflow/batches", data=fresh_form, files=files(1))
    assert fresh.status_code == 200, fresh.text
    wait_batch(client, fresh_form["batch_id"])

    # 老记录（没有 source_key）也要能按链接算出 key 再比
    legacy_id = uuid.uuid4().hex
    legacy_path = source / "company-workflow" / "batches" / f"{legacy_id}.json"
    legacy_path.write_text(json.dumps({
        "id": legacy_id, "created_at": utc_now(), "status": "ready", "build": "legacy",
        "source_url": "https://www.douyin.com/note/7555555555555555555",
        "pages": [{"page_number": 1, "task_id": "legacy-task", "enqueued": True}],
        "request_count": 1, "submission_state": "submitted",
    }, ensure_ascii=False), encoding="utf-8")
    try:
        legacy_hit = client.post("/api/company-workflow/batches",
                                 data=form(1, source_url="https://www.douyin.com/note/7555555555555555555?is_from=share"),
                                 files=files(1))
        assert legacy_hit.status_code == 409, legacy_hit.text
    finally:
        legacy_path.unlink(missing_ok=True)

    # 短链取图后，前端拿到的是解析后的完整链接 → 同一篇再提交会被拦住
    real_fetch = company_fetch.fetch
    folder = data / "fake-short-link"
    folder.mkdir()
    short_files = []
    for index in range(1, 2):
        path = folder / f"{index:02d}.png"
        path.write_bytes(b"x")
        short_files.append(path)
    company_fetch.fetch = lambda *args, **kwargs: company_fetch.FetchedNote(
        platform="douyin", title="短链笔记", source_url=note_url, folder=folder,
        files=tuple(short_files), total_images=1)
    try:
        linked = client.post("/api/company-workflow/link", json={"text": "https://v.douyin.com/ACOip9973ZY/"}).json()
    finally:
        company_fetch.fetch = real_fetch
    assert linked["source_url"] == note_url, linked
    back_end = client.post("/api/company-workflow/batches",
                           data=form(1, source_url=linked["source_url"]), files=files(1))
    assert back_end.status_code == 409 and "这条链接已经做过一套" in back_end.text
    draft_key = client.post("/api/company-workflow/drafts", json={
        "batch_id": uuid.uuid4().hex, "title": "t", "folder": "", "source_url": note_url, "images": ["01.png"],
    }).json()
    assert draft_key["company_draft"]["source_key"] == "douyin:7684539877870924495"
    result["dedupe_by_note_id"] = True

report = {**result, "offline_fake_calls": len(calls)}
(project / "验证/R1-R4离线验证结果.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=2))
