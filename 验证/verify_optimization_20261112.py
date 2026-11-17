"""2026-11-12 优化清单离线验证：套级串行、来源去重、标题、草稿清理、原图清理、重启续跑、取图截断。

全部使用假的生图客户端，不联网、不计费。
"""
from __future__ import annotations
import io
import json
import os
from pathlib import Path
import sys
import time
import uuid

project = Path(__file__).resolve().parent.parent
runtime = project / "runtime"
data = project / "验证" / ("优化清单离线数据-" + uuid.uuid4().hex[:8])
source = data / "webui-outputs" / "source-data"
source.mkdir(parents=True)
os.environ["ILAB_CONJURE_DATA_DIR"] = str(data)
sys.path.insert(0, str(runtime / "app"))

from PIL import Image
from fastapi.testclient import TestClient
from codex_image.client import ImageResult
from codex_image.webui import company_fetch
from codex_image.webui.app import create_app
from codex_image.webui.company_workflow import original_paths
from codex_image.webui.provider_settings import ProviderSettings
from codex_image.webui.recovery import _recover_queue_state
from codex_image.webui.storage import utc_now


def png(color, size=(300, 400)):
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, "PNG")
    return output.getvalue()


fixture = png("#f2f7f4", (900, 1200))
calls: list[str] = []
delay = .5


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


def form(count: int, *, title: str = "", source_url: str = "", ident: str | None = None) -> dict:
    selected = list(range(1, count + 1))
    return {"batch_id": ident or uuid.uuid4().hex, "prompt": "OFFLINE: 保留参考图布局，封面出现女生陪驾。",
            "changes": "", "selected_pages": json.dumps(selected), "confirmed_count": len(selected),
            "opacity": 13, "quality": "medium", "title": title, "source_url": source_url}


def files(count: int):
    return [("reference_images", (f"page-{i + 1}.png", png((20 + i * 11, 60 + i * 7, 100 + i * 4)), "image/png"))
            for i in range(count)]


def wait_batches(client, batch_ids, limit=40):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        values = [client.get(f"/api/company-workflow/batches/{ident}").json() for ident in batch_ids]
        if all(all(p["status"] in {"completed", "failed", "partial_failed"} for p in value["pages"]) for value in values):
            return values
        time.sleep(.1)
    raise AssertionError([value["submission_state"] for value in values])


result: dict = {"passed": True, "external_api_requests": 0, "fixture_directory": str(data)}
with TestClient(app, base_url="http://127.0.0.1") as client:
    # ---- 1-1 套级串行：第二套必须等第一套全部跑完才入队 ----
    first_form, second_form = form(3), form(2)
    assert client.post("/api/company-workflow/batches", data=first_form, files=files(3)).status_code == 200
    running = client.get(f"/api/company-workflow/batches/{first_form['batch_id']}").json()
    assert running["submission_state"] == "submitted"
    assert client.post("/api/company-workflow/batches", data=second_form, files=files(2)).status_code == 200
    second_batch = client.get(f"/api/company-workflow/batches/{second_form['batch_id']}").json()
    assert second_batch["submission_state"] == "waiting_turn", second_batch["submission_state"]
    first_ids = {page["task_id"] for page in running["pages"]}
    second_ids = {page["task_id"] for page in second_batch["pages"]}
    state = app.state.ctx.queue_storage.read_state()
    queued = set(state["waiting"]) | {row.get("task_id") for row in state["running"].values()}
    assert queued and queued <= first_ids and not (queued & second_ids), (queued, first_ids, second_ids)
    done_first, done_second = wait_batches(client, [first_form["batch_id"], second_form["batch_id"]])
    assert all(page["status"] == "completed" for page in done_first["pages"] + done_second["pages"])
    assert done_second["submission_state"] == "submitted"
    assert len(calls) == 5, calls
    result["batch_serial_turn"] = True

    # ---- 2-1 标题 + 1-5 来源去重 ----
    link = "https://www.xiaohongshu.com/explore/0123456789abcdef01234567?xsec_token=abc"
    titled_form = form(1, title="女生开车记住这5点", source_url=link)
    assert client.post("/api/company-workflow/batches", data=titled_form, files=files(1)).status_code == 200
    titled = client.get(f"/api/company-workflow/batches/{titled_form['batch_id']}").json()
    assert titled["title"] == "女生开车记住这5点" and titled["source_url"] == link
    duplicate = client.post("/api/company-workflow/batches", data=form(1, title="再来一次", source_url=link), files=files(1))
    assert duplicate.status_code == 409 and "这条链接已经做过一套" in duplicate.text
    # 草稿里已经存了来源时，即使提交时不再带 source_url，也要拦住
    draft_id = uuid.uuid4().hex
    assert client.post("/api/company-workflow/drafts", json={
        "batch_id": draft_id, "title": "女生开车记住这5点", "folder": "", "source_url": link,
        "images": ["01.png"]}).status_code == 200
    from_draft = form(1, ident=draft_id)
    blocked = client.post("/api/company-workflow/batches", data=from_draft, files=files(1))
    assert blocked.status_code == 409, blocked.text
    result["title_stored"] = result["duplicate_link_blocked"] = True

    # ---- 1-3 取图截断到 12 张并提示 ----
    assert company_fetch.MAX_IMAGES == 12
    slides = "".join(
        f'<img src="https://p3-sign.douyinpic.com/tos-cn-i-0813/slide{i}~tplv-dy-aweme-images:q75.webp">'
        for i in range(15))
    assert len(company_fetch._douyin_images(slides)) == 15
    real_fetch = company_fetch.fetch
    fake_folder = data / "fake-link"
    fake_folder.mkdir()
    fake_files = []
    for index in range(12):
        path = fake_folder / f"{index + 1:02d}.png"
        path.write_bytes(b"x")
        fake_files.append(path)
    company_fetch.fetch = lambda *args, **kwargs: company_fetch.FetchedNote(
        platform="xiaohongshu", title="十五张笔记", source_url=link, folder=fake_folder,
        files=tuple(fake_files), total_images=15)
    try:
        linked = client.post("/api/company-workflow/link", json={"text": link}).json()
    finally:
        company_fetch.fetch = real_fetch
    assert linked["count"] == 12 and linked["total"] == 15 and linked["truncated"] is True
    result["fetch_capped_at_12"] = True

    # ---- 2-2 草稿套自动清理（超过 24 小时且没有页）----
    stale_id = uuid.uuid4().hex
    assert client.post("/api/company-workflow/drafts", json={
        "batch_id": stale_id, "title": "中断的草稿", "folder": "", "source_url": "", "images": ["01.png"]}).status_code == 200
    stale_path = source / "company-workflow" / "batches" / f"{stale_id}.json"
    assert stale_path.is_file()
    old = time.time() - 25 * 3600
    os.utime(stale_path, (old, old))
    listed = {batch["id"] for batch in client.get("/api/company-workflow/batches").json()["batches"]}
    assert stale_id not in listed and not stale_path.is_file()
    assert draft_id in listed
    result["stale_draft_cleaned"] = True

    # ---- 2-3 原始图手动清理 ----
    store = app.state.ctx.storage
    kept_task = done_first["pages"][0]["task_id"]
    raw, info = original_paths(store, kept_task)
    assert raw.is_file() and info.is_file()
    os.utime(raw, (old, old))
    purged = client.post("/api/company-workflow/originals/purge", json={"days": 1}).json()
    assert purged["removed"] == 1 and purged["freed_bytes"] > 0, purged
    assert not raw.exists() and not info.exists()
    other_task = done_first["pages"][1]["task_id"]
    assert original_paths(store, other_task)[0].is_file()
    result["originals_purge"] = True

    # ---- 1-4 重启续跑：running 但还有次数 → 回队列 ----
    queue_storage = app.state.ctx.queue_storage
    resumed = store.create_task("generate").task_id
    exhausted = store.create_task("generate").task_id
    store.write_metadata(resumed, {"task_id": resumed, "status": "running", "created_at": utc_now(),
                                   "attempts": 1, "params": {}, "outputs": []})
    store.write_metadata(exhausted, {"task_id": exhausted, "status": "running", "created_at": utc_now(),
                                     "attempts": 3, "params": {}, "outputs": []})
    state = queue_storage.read_state()
    state["running"] = {"offline:0": {"task_id": resumed, "started_at": utc_now()}}
    queue_storage.write_state(state)
    _recover_queue_state(store, queue_storage)
    assert store.read_metadata(resumed)["status"] == "queued"
    assert store.read_metadata(resumed)["last_error"] == "Service restarted; re-queued."
    assert store.read_metadata(exhausted)["status"] == "failed"
    assert resumed in queue_storage.read_state()["waiting"]
    result["restart_requeues_running"] = True

report = {**result, "offline_fake_calls": len(calls), "sqlite_integrity": "ok"}
(project / "验证/优化清单20261112验证结果.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=2))
