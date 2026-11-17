"""Versioned local Agent API. All paid work uses the existing page queue."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import secrets
import tempfile
import zipfile
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response

from .. import engine as e, company_analyze
from ..atomic_files import atomic_write_text
from ..cancellation import request_task_cancellation, finalize_task_cancellation
from ..company_workflow import BUILD, seed_defaults, workflow_root, write_json, original_paths
from ..context import WebUIContext
from ..executor_inputs import _bytes_to_data_url
from ..storage import utc_now
from .generation import _prepare_generation_submission, _persist_generation_submission, _prepare_raster_uploads, _validate_task_image_resources
from .tasks import OneTimeTaskArchiveResponse


def register_engine_routes(app: FastAPI, ctx: WebUIContext) -> None:
    root = workflow_root(ctx.storage)
    shared = ctx.route_helpers["company_batches"]
    lock = shared["lock"]
    token_path = root / "agent-token.txt"
    if not token_path.exists():
        atomic_write_text(token_path, secrets.token_urlsafe(32), mode=0o600)

    def local(request: Request):
        if not request.client or request.client.host not in {"127.0.0.1", "::1", "testclient"}:
            raise HTTPException(403, "Agent接口只接受本机请求。")

    def authorize(request: Request):
        local(request)
        expected = os.environ.get("IMAGE_ENGINE_TOKEN") or token_path.read_text(encoding="utf-8").strip()
        if not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + expected):
            raise HTTPException(401, "需要本机 Agent Bearer 令牌。")

    @app.get("/api/engine/session", include_in_schema=False)
    def session(request: Request, response: Response):
        local(request)
        if (request.headers.get("x-requested-with") != "image-engine-webui"
            or request.headers.get("sec-fetch-site") != "same-origin"):
            raise HTTPException(403, "只能从本机工作台获取会话。")
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.url.netloc:
            raise HTTPException(403, "会话来源无效。")
        response.headers["Cache-Control"] = "no-store"
        return {"token": os.environ.get("IMAGE_ENGINE_TOKEN") or token_path.read_text(encoding="utf-8").strip()}

    router = APIRouter(prefix="/api/engine", dependencies=[Depends(authorize)])

    def load(key: str) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", key):
            raise HTTPException(400, "任务ID无效。")
        batch = shared["read"](e.job_id(key))
        if not batch.get("engine_schema") or batch.get("external_id") != key:
            raise HTTPException(404, "没有找到该引擎任务。")
        return batch

    def choose_provider(job: e.Job) -> tuple[dict, dict]:
        settings = ctx.api_settings.public_settings()
        ident = job.generation.provider_id or settings["active_provider_id"]
        provider = next((p for p in settings["providers"] if p["id"] == ident), None)
        if not provider or not provider.get("api_key_set"):
            raise ValueError("所选供应商尚未配置密钥。请使用原有 API 与系统设置。")
        candidates = [b for b in provider.get("bindings", []) if "generate" in b["operations"]
                      and (not job.generation.binding_id or b["id"] == job.generation.binding_id)
                      and (not job.generation.model_id or b["canonical_model_id"] == job.generation.model_id)]
        if not candidates:
            raise ValueError("供应商没有匹配的可生成模型绑定。")
        if len(candidates) > 1:
            candidates = [b for b in candidates if b.get("is_default")] or candidates
        if len(candidates) != 1:
            raise ValueError("存在多个模型绑定，请明确 generation.model_id/binding_id。")
        return provider, candidates[0]

    def prepare(job: e.Job, *, revision: str = ""):
        provider, binding = choose_provider(job)
        refs, urls = [], {}
        for reference in job.references:
            try:
                row = ctx.reference_asset_storage.read_item(reference.asset_id)
                path = ctx.reference_asset_storage.image_path(reference.asset_id)
                urls[row["id"]] = _bytes_to_data_url(path.read_bytes(), filename=path.name)
                refs.append(row)
            except (OSError, ValueError):
                raise ValueError(f"素材不存在或不可读取：{reference.asset_id}") from None
        _validate_task_image_resources(reference_assets=refs, gallery_refs=[])
        planned = []
        parameters = dict(job.generation.parameters)
        model_id = binding["canonical_model_id"]
        parameters["output.count"] = 1
        if model_id.startswith("gpt-image"):
            parameters.setdefault("canvas.size", job.generation.size)
            parameters.setdefault("gpt.quality", job.generation.quality)
            parameters["output.format"] = "png"
        for page in job.pages:
            selected = e.selected_assets(job, page)
            if job.mode == "edit" and not selected:
                raise ValueError(f"编辑第{page.page_number}页需要明确指定授权母版 asset_ids。")
            text = e.page_prompt(job, page, revision)
            prepared = _prepare_generation_submission(
                ctx, operation="generate", prompt=text, prompt_for_model=text, ui_language="zh-CN",
                prompt_fidelity="original", main_model="", model=model_id,
                size=job.generation.size, resolution=None, ratio=None, orientation=None,
                quality=job.generation.quality, background=None, output_format="png",
                input_fidelity=None, moderation=None, output_compression=None, n=1, web_search=False,
                auth_source="api", effective_api_mode="responses" if binding["protocol_profile"].endswith("responses") else "images",
                effective_codex_mode=None, effective_api_provider_id=provider["id"],
                effective_api_provider_name=provider["name"], effective_api_images_concurrency=provider["concurrency"],
                requested_backend=binding["protocol_profile"], canonical_model_id=model_id,
                provider_id=provider["id"], binding_id=binding["id"],
                parameters_json=json.dumps(parameters), explicit_form_fields=frozenset(),
                image_data_urls=[urls[ident] for ident in selected], mask_data_url=None, reference_file_inputs=[],
            )
            planned.append((page, text, prepared))
        return provider, binding, refs, planned

    def view(batch: dict) -> dict:
        value = shared["view"](batch)
        value.pop("engine_request", None)
        value.pop("retry_requests", None)
        value["job_id"] = batch["external_id"]
        value["batch_id"] = batch["id"]
        statuses = []
        complete = failed = 0
        texts, images = {}, {}
        duplicate_texts, duplicate_images = [], []
        for page in value["pages"]:
            content = page.get("content") or {}
            text_key = (content.get("title", "") + "\n" + content.get("copy", "")).strip()
            if text_key and text_key in texts:
                duplicate_texts.append([texts[text_key], page["page_number"]])
            texts[text_key] = page["page_number"]
            status = page.get("status", "missing")
            statuses.append(status)
            if page.get("url"):
                complete += 1
                meta = ctx.storage.read_metadata(page["task_id"])
                output = next(r for r in meta["outputs"] if r.get("status") == "completed" and r.get("file"))
                path = ctx.storage.output_path(output["file"])
                page["output_path"] = str(path.resolve())
                page["quality_check"] = e.quality_check(path, batch["engine_request"]["generation"]["size"])
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                page["sha256"] = digest
                if digest in images:
                    duplicate_images.append([images[digest], page["page_number"]])
                images[digest] = page["page_number"]
                page["download_url"] = f"/api/engine/jobs/{batch['external_id']}/pages/{page['page_number']}/image"
            elif status in {"failed", "partial_failed", "missing"}:
                failed += 1
            page["expected_text"] = page.get("content", {})
            page["text_review"] = page.get("text_review", {"status": "unverified"})
        if batch.get("cancelled"):
            status = "cancelled"
        elif any(s == "running" for s in statuses):
            status = "running"
        elif any(s in {"queued", "staged"} for s in statuses):
            status = "queued"
        elif complete == len(statuses) and statuses:
            status = "completed"
        elif failed:
            status = "partial_failed" if complete else "failed"
        else:
            status = batch.get("submission_state", "preparing")
        value.update(status=status, completed_pages=complete, failed_pages=failed,
                     total_pages=len(batch["pages"]),
                     download_url=f"/api/engine/jobs/{batch['external_id']}/download")
        value["content_check"] = {
            "page_sequence_valid": [p["page_number"] for p in value["pages"]] == list(range(1, len(batch["pages"]) + 1)),
            "duplicate_expected_text_pages": duplicate_texts, "identical_image_pages": duplicate_images,
            "semantic_consistency": "unverified", "text_recognition": "not_performed"}
        value["budgets"] = [json.loads(e.budget_path(ctx.storage, ident).read_text(encoding="utf-8"))
                            for ident in batch.get("budget_ids", []) if e.budget_path(ctx.storage, ident).exists()]
        return value

    def persist_pages(batch, job, prepared_data, rows, budget_ids, *, revision=""):
        provider, binding, refs, prepared_pages = prepared_data
        brand = e.cache_brand(ctx.storage, ctx.reference_asset_storage, job.brand)
        for page, text, prepared in prepared_pages:
            row = next(r for r in rows if r["page_number"] == page.page_number)
            # This makes resume after a partial disk write idempotent.
            if ctx.storage.metadata_path(row["task_id"]).exists():
                continue
            prepared.params["company_page"] = {
                "engine_schema": 1, "batch_id": batch["id"], "page_number": page.page_number,
                "page_count": len(job.pages), "brand": brand, "reference_strategy": "selected",
                "selected_asset_ids": e.selected_assets(job, page), "budget_ids": budget_ids,
                "revision": revision,
            }
            _persist_generation_submission(
                ctx, prepared=prepared, operation="generate", task_id=row["task_id"], created_at=utc_now(),
                prompt=text, requested_backend=binding["protocol_profile"], auth_source="api",
                effective_api_provider_id=provider["id"], effective_api_provider_name=provider["name"],
                effective_api_images_concurrency=provider["concurrency"], input_files=[], mask_file=None,
                gallery_refs=[], reference_assets=refs, file_references=[],
                reference_image_order=[{"kind": "asset", "id": r["id"]} for r in refs], enqueue=False)
            meta = ctx.storage.read_metadata(row["task_id"])
            meta["status"] = "staged"
            ctx.storage.write_metadata(row["task_id"], meta)

    def ensure_budget(ident, authorization, count):
        if not e.budget_path(ctx.storage, ident).exists():
            # Monetary caps require a local, user-maintained price ceiling.
            pricing = seed_defaults(root).get("pricing") or {}
            e.create_budget(ctx.storage, ident, authorization, count, pricing=pricing)

    def submit(job: e.Job, parent_budget: str = "", *, enqueue=True) -> dict:
        ident = e.job_id(job.task_id)
        path = shared["path"](ident)
        digest = e.fingerprint(job.model_dump())
        if path.exists():
            batch = load(job.task_id)
            if batch["fingerprint"] != digest:
                raise HTTPException(409, "同一任务ID已用于不同内容，请新建ID。")
            if batch.get("submission_state") == "preparing":
                planned = e.Job.model_validate(batch["engine_request"])
                ensure_budget(ident, planned.authorization, len(planned.pages))
                data = prepare(planned)
                with ctx.reference_asset_storage.protect_submission(r.asset_id for r in planned.references):
                    persist_pages(batch, planned, data, batch["pages"], batch["budget_ids"])
                batch["submission_state"] = "ready"
                write_json(path, batch)
                if enqueue:
                    shared["enqueue"](batch)
            return batch
        # Existing delete receipts also block stale requests from purchasing again.
        if (root / "deleted-batches" / f"{ident}.json").exists():
            raise HTTPException(410, "此任务已删除；不能复用旧ID重复计费。")
        planned = e.planned_job(job)
        data = prepare(planned)
        budget_ids = [ident] + ([parent_budget] if parent_budget else [])
        # Validate authorization before writing any manifest.
        if not job.authorization.approved or job.authorization.max_image_requests < len(planned.pages):
            raise ValueError(f"需明确授权至少{len(planned.pages)}次图片请求。")
        batch = {"id": ident, "external_id": job.task_id, "engine_schema": 1,
                 "fingerprint": digest, "created_at": utc_now(), "build": BUILD,
                 "title": planned.title or planned.theme, "changes": planned.requirements,
                 "mode": planned.mode, "reference_strategy": "selected",
                 "reference_count": len(planned.references), "request_count": len(planned.pages),
                 "selected_pages": [p.page_number for p in planned.pages],
                 "engine_request": planned.model_dump(), "budget_ids": budget_ids,
                 "submission_state": "preparing", "revisions": [], "pages": []}
        for page in planned.pages:
            task = ctx.storage.create_task("generate")
            batch["pages"].append({"page_number": page.page_number, "task_id": task.task_id,
                                  "enqueued": False, "version": 1, "versions": [],
                                  "content": page.model_dump()})
        write_json(path, batch)
        ensure_budget(ident, planned.authorization, len(planned.pages))
        with ctx.reference_asset_storage.protect_submission(r.asset_id for r in planned.references):
            persist_pages(batch, planned, data, batch["pages"], budget_ids)
        batch["submission_state"] = "ready"
        write_json(path, batch)
        if enqueue:
            shared["enqueue"](batch)
        return batch

    @router.get("/status")
    def status():
        public = ctx.api_settings.public_settings()
        providers = [{"id": p["id"], "name": p["name"], "ready": p.get("api_key_set", False),
                      "concurrency": p["concurrency"], "bindings": p.get("bindings", [])}
                     for p in public["providers"]]
        config = seed_defaults(root)
        return {"api_version": 1, "build": BUILD, "modes": ["inspired", "original", "edit"],
                "reference_policies": ["inspiration", "layout"],
                "active_provider_id": public["active_provider_id"], "providers": providers,
                "analysis": {"configured": bool(config.get("analysis_api_key") and config.get("analysis_model")
                                              and config.get("analysis_base_url")),
                             "model": config.get("analysis_model", "")},
                "limits": {"pages_per_job": e.MAX_PAGES, "jobs_per_batch": e.MAX_BATCH,
                           "assets_per_job": 24, "assets_per_page": 4, "auto_paid_retries": 0},
                "pricing": config.get("pricing") or {"status": "unknown", "message": "供应商未提供可验证单价；使用请求次数额度。"},
                "queue": ctx.queue_worker_health.snapshot(), "migration": config.get("migration"),
                "text_accuracy": "requires_review"}

    @router.post("/assets")
    async def assets(images: list[UploadFile] = File(...)):
        if not 1 <= len(images) <= 24:
            raise HTTPException(400, "一次导入1～24张图片。")
        uploaded = await _prepare_raster_uploads(images, preserve_positions=True)
        return {"assets": [ctx.reference_asset_storage.create_or_touch(i.filename, i.data, i.mime_type)
                           for i in uploaded]}

    @router.post("/plan")
    def plan(job: e.Job):
        try:
            planned = e.planned_job(job)
            return {"job": planned.model_dump(), "planner": "local_facts_only",
                    "paid_calls": 0, "requires_content_review": job.content_mode == "brief",
                    "note": "本地规划只组织所给事实；可先修改页面，再以final提交。"}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @router.post("/estimate")
    def estimate(job: e.Job):
        try:
            planned = e.planned_job(job)
            return {"task_id": job.task_id, "output_pages": len(planned.pages),
                    "reference_assets": len(planned.references), "initial_image_requests": len(planned.pages),
                    "max_image_requests": job.authorization.max_image_requests, "automatic_paid_retries": 0,
                    "price": seed_defaults(root).get("pricing") or None, "paid_calls": 0}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @router.post("/analyze")
    async def analyze(payload: e.AnalysisRequest):
        if not payload.approved or payload.max_analysis_requests != 1:
            raise HTTPException(400, "分析单独计费，需明确授权1次分析请求。")
        ident = e.fingerprint(["analysis", payload.analysis_id])[:32]
        path = root / "analyses" / f"{ident}.json"
        digest = e.fingerprint(payload.job.model_dump())
        with lock:
            if path.exists():
                saved = json.loads(path.read_text(encoding="utf-8"))
                if saved["fingerprint"] != digest:
                    raise HTTPException(409, "分析ID已用于不同任务数据。")
                if saved["status"] == "completed":
                    return saved
                raise HTTPException(409, "此分析已发起过，结果或费用不确定，未重复调用。")
            config = seed_defaults(root)
            if not (config.get("analysis_base_url") and config.get("analysis_api_key") and config.get("analysis_model")):
                raise HTTPException(400, "请先在原有设置中配置分析模型、地址和密钥。")
            images = []
            references = [r for r in payload.job.references if r.purpose == "reference"]
            if len(references) > company_analyze.MAX_PAGES:
                raise HTTPException(400, "一次分析最多12张参考素材。")
            try:
                images = [ctx.reference_asset_storage.image_path(r.asset_id).read_bytes() for r in references]
            except (OSError, ValueError):
                raise HTTPException(400, "分析素材不存在。") from None
            write_json(path, {"status": "started", "fingerprint": digest, "analysis_id": payload.analysis_id})
        from starlette.concurrency import run_in_threadpool
        from functools import partial
        try:
            transport = ctx.network_egress_manager.transport(ctx.network_egress_manager.snapshot(),
                timeout_seconds=company_analyze.ANALYSIS_TIMEOUT_SECONDS)
            result = await run_in_threadpool(partial(company_analyze.analyze, images,
                base_url=config["analysis_base_url"], api_key=config["analysis_api_key"], model=config["analysis_model"],
                brief=payload.job.model_dump(), protocol=config.get("analysis_protocol", "chat_completions"),
                max_tokens=int(config.get("analysis_max_tokens", company_analyze.MAX_TOKENS)), transport=transport))
            value = {"status": "completed", "fingerprint": digest, "analysis_id": payload.analysis_id,
                     "analysis": result["value"], "elapsed": result["elapsed"], "model": result["model"],
                     "requires_review": True}
            write_json(path, value)
            return value
        except company_analyze.AnalyzeError as exc:
            write_json(path, {"status": "failed", "fingerprint": digest, "analysis_id": payload.analysis_id})
            raise HTTPException(502, str(exc)) from None

    @router.post("/jobs")
    def create(job: e.Job):
        try:
            with lock, ctx.reference_asset_storage.protect_submission(r.asset_id for r in job.references):
                return view(submit(job))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        except OSError:
            raise HTTPException(503, "提交写盘中断；请保留同一任务ID查询或恢复，未自动另建付费任务。") from None

    @router.get("/jobs")
    def jobs():
        values = [json.loads(p.read_text(encoding="utf-8")) for p in (root / "batches").glob("*.json")]
        visible = [b for b in sorted(values, key=lambda b: b["created_at"], reverse=True)
                   if b.get("engine_schema") and not b.get("hidden_at")][:100]
        return {"jobs": [view(b) for b in visible]}

    @router.get("/jobs/{key}")
    def get_job(key: str):
        with lock:
            return view(load(key))

    @router.post("/batches")
    def create_many(payload: e.BatchRequest):
        digest = e.fingerprint(payload.model_dump())
        ident = e.fingerprint(["batch-budget", payload.batch_id])[:32]
        path = root / "engine-batches" / f"{ident}.json"
        try:
            with lock:
                if path.exists():
                    saved = json.loads(path.read_text(encoding="utf-8"))
                    if saved["fingerprint"] != digest:
                        raise HTTPException(409, "批量ID已用于不同请求。")
                    if saved.get("state") == "submitted":
                        for key in saved["job_ids"]:
                            record = load(key)
                            if record.get("submission_state") in {"ready", "waiting_turn"}:
                                shared["enqueue"](record)
                        return batch_state(payload.batch_id)
                planned = [e.planned_job(job) for job in payload.jobs]
                count = sum(len(job.pages) for job in planned)
                # Preflight all new jobs before any batch is released to the queue.
                for original, job in zip(payload.jobs, planned, strict=True):
                    if shared["path"](e.job_id(job.task_id)).exists():
                        existing = load(job.task_id)
                        if existing["fingerprint"] != e.fingerprint(original.model_dump()):
                            raise HTTPException(409, f"任务ID内容冲突：{job.task_id}")
                    else:
                        prepare(job)
                    if not original.authorization.approved or original.authorization.max_image_requests < len(job.pages):
                        raise ValueError(f"{job.task_id} 未授权足够页数。")
                ensure_budget(ident, payload.authorization, count)
                saved = {"id": payload.batch_id, "fingerprint": digest,
                         "job_ids": [job.task_id for job in payload.jobs], "state": "preparing",
                         "expected_pages": count, "budget_id": ident}
                write_json(path, saved)
                records = [submit(job, ident, enqueue=False) for job in payload.jobs]
                saved["state"] = "submitted"
                write_json(path, saved)
                for record in records:
                    if record.get("submission_state") in {"ready", "waiting_turn"}:
                        shared["enqueue"](record)
                return batch_state(payload.batch_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    def read_many(key):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", key):
            raise HTTPException(400, "批量ID无效。")
        path = root / "engine-batches" / f"{e.fingerprint(['batch-budget', key])[:32]}.json"
        if not path.exists():
            raise HTTPException(404, "没有找到批量任务。")
        return json.loads(path.read_text(encoding="utf-8"))

    @router.get("/batches/{key}")
    def batch_state(key: str):
        saved = read_many(key)
        values = []
        for ident in saved["job_ids"]:
            try:
                values.append(view(load(ident)))
            except HTTPException as exc:
                if exc.status_code not in {404, 410}:
                    raise
                values.append({"job_id": ident, "status": "missing"})
        return {**saved, "jobs": values,
                "budget": json.loads(e.budget_path(ctx.storage, saved["budget_id"]).read_text(encoding="utf-8"))}

    def current_output(batch, number: int, version: int | None = None):
        row = next((p for p in batch["pages"] if p["page_number"] == number), None)
        if not row:
            raise HTTPException(404, "页码不存在。")
        if version is not None and version != row["version"]:
            row = next((v for v in row["versions"] if v["version"] == version), None)
            if not row:
                raise HTTPException(404, "页面版本不存在。")
        try:
            meta = ctx.storage.read_metadata(row["task_id"])
            output = next(r for r in meta.get("outputs", []) if r.get("status") == "completed" and r.get("file"))
            path = ctx.storage.output_path(output["file"])
            if not path.is_file():
                raise FileNotFoundError()
            return path
        except (FileNotFoundError, StopIteration):
            raise HTTPException(409, "该页此版本还没有生成结果。") from None

    @router.get("/jobs/{key}/pages/{number}/image")
    def image(key: str, number: int, version: int | None = None):
        return FileResponse(current_output(load(key), number, version), media_type="image/png",
                            filename=f"{number:02d}-v{version or 'current'}.png")

    def archive(batches: list[dict]):
        temp = root / "temp"
        temp.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(suffix=".zip", dir=temp)
        os.close(fd)
        path = Path(name)
        try:
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as target:
                total = 0
                for batch in batches:
                    report = view(batch)
                    if report["completed_pages"] != report["total_pages"]:
                        raise HTTPException(409, "有未完成页面，完整导出暂不可用；可单独下载已完成页。")
                    folder = batch["external_id"]
                    # Export delivery metadata, not the local progress/debug response.
                    manifest = {key: report[key] for key in (
                        "job_id", "batch_id", "title", "mode", "build", "created_at",
                        "status", "completed_pages", "failed_pages", "total_pages", "content_check")}
                    manifest["pages"] = []
                    for row in report["pages"]:
                        source = current_output(batch, row["page_number"])
                        total += source.stat().st_size
                        if total > 512 * 1024 * 1024:
                            raise HTTPException(413, "单次导出超过512MB，请分套下载。")
                        filename = f"{row['page_number']:02d}-第{row['page_number']}页.png"
                        target.write(source, f"{folder}/{filename}")
                        exported = {key: row[key] for key in (
                            "page_number", "task_id", "version", "status", "content",
                            "expected_text", "sha256", "quality_check", "text_review")}
                        exported["output_path"] = filename
                        manifest["pages"].append(exported)
                    target.writestr(folder + "/manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            return OneTimeTaskArchiveResponse(path, filename="image-engine-results.zip")
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    @router.get("/jobs/{key}/download")
    def download(key: str):
        with lock:
            return archive([load(key)])

    @router.get("/batches/{key}/download")
    def download_many(key: str):
        with lock:
            return archive([load(k) for k in read_many(key)["job_ids"]])

    @router.post("/jobs/{key}/cancel")
    def cancel(key: str):
        with lock:
            batch = load(key)
            batch["cancelled"] = True
            batch["submission_state"] = "cancelled"
            write_json(shared["path"](batch["id"]), batch)
            for page in batch["pages"]:
                with ctx.storage._task_write_lock(page["task_id"]), ctx.queue_storage.exclusive():
                    meta = ctx.storage.read_metadata(page["task_id"])
                    if meta.get("status") == "completed":
                        continue
                    request_task_cancellation(ctx.storage, page["task_id"])
                    ctx.queue_storage.remove_waiting(page["task_id"])
                    running = ctx.running_worker_tasks.get(page["task_id"])
                    if running is not None:
                        running.get_loop().call_soon_threadsafe(running.cancel)
                    else:
                        finalize_task_cancellation(ctx.storage, page["task_id"])
            return view(batch)

    def resume_record(batch):
        if any(p["task_id"] in ctx.active_task_ids for p in batch["pages"]):
            if not batch.get("cancelled"):
                return view(batch)
            raise HTTPException(409, "仍有页请求尚未退出，请稍后恢复。")
        job = e.Job.model_validate(batch["engine_request"])
        if batch.get("submission_state") == "preparing":
            data = prepare(job)
            ensure_budget(batch["id"], job.authorization, len(job.pages))
            persist_pages(batch, job, data, batch["pages"], batch["budget_ids"])
        for page in batch["pages"]:
            meta = ctx.storage.read_metadata(page["task_id"])
            if meta.get("cancel_requested"):
                # Dispatched requests are uncertain, so never automatically repurchase them.
                if not original_paths(ctx.storage, page["task_id"])[0].exists() and int(meta.get("attempts") or 0):
                    continue
                for field in ("cancel_requested", "cancel_requested_at", "cancelled_at"):
                    meta.pop(field, None)
                meta.update(status="staged", error="")
                ctx.storage.write_metadata(page["task_id"], meta, reset_cancellation=True)
        batch["cancelled"] = False
        batch["submission_state"] = "ready"
        write_json(shared["path"](batch["id"]), batch)
        shared["enqueue"](batch)
        return view(batch)

    @router.post("/jobs/{key}/resume")
    def resume(key: str):
        try:
            with lock:
                return resume_record(load(key))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @router.post("/jobs/{key}/retry")
    def retry(key: str, payload: e.Retry):
        with lock:
            batch = load(key)
            previous = batch.setdefault("retry_requests", {}).get(payload.request_id)
            digest = e.fingerprint(payload.model_dump())
            if previous:
                if previous["fingerprint"] != digest:
                    raise HTTPException(409, "重试ID内容冲突。")
                if previous.get("state") == "preparing":
                    finish_retry(batch, previous)
                return view(batch)
            if not payload.acknowledge_uncertain_charge:
                raise HTTPException(400, "重试可能再次计费，需明确确认；仍受原授权总请求上限约束。")
            if len(set(payload.page_numbers)) != len(payload.page_numbers):
                raise HTTPException(400, "重试页码不能重复。")
            targets = []
            for number in payload.page_numbers:
                row = next((r for r in batch["pages"] if r["page_number"] == number), None)
                if not row:
                    raise HTTPException(400, "重试页码不存在。")
                meta = ctx.storage.read_metadata(row["task_id"])
                if meta.get("status") not in {"failed", "partial_failed"} or row["task_id"] in ctx.active_task_ids:
                    raise HTTPException(409, "仅能重试已经停止的失败页。")
                targets.append((row, meta))
            needed = {}
            for row, meta in targets:
                if not original_paths(ctx.storage, row["task_id"])[0].exists():
                    for ident in meta["params"]["company_page"]["budget_ids"]:
                        needed[ident] = needed.get(ident, 0) + 1
            for ident, count in needed.items():
                ledger = json.loads(e.budget_path(ctx.storage, ident).read_text(encoding="utf-8"))
                if ledger["used"] + count > ledger["limit"]:
                    raise HTTPException(409, "原授权请求额度不足；不会发起重试。可另行授权指定页新版本。")
            record = {"fingerprint": digest, "pages": payload.page_numbers, "state": "preparing"}
            batch["retry_requests"][payload.request_id] = record
            write_json(shared["path"](batch["id"]), batch)
            finish_retry(batch, record)
            return view(batch)

    def finish_retry(batch, record):
        for row in batch["pages"]:
            if row["page_number"] in record["pages"]:
                meta = ctx.storage.read_metadata(row["task_id"])
                if meta.get("status") not in {"failed", "partial_failed"}:
                    continue
                for field in ("cancel_requested", "cancel_requested_at", "cancelled_at", "last_error", "generation_error"):
                    meta.pop(field, None)
                meta.update(status="staged", error="", retrying_failed_slots=[1], retry_requested_at=utc_now())
                ctx.storage.write_metadata(row["task_id"], meta, reset_cancellation=True)
        record["state"] = "submitted"
        batch["cancelled"] = False
        batch["submission_state"] = "ready"
        write_json(shared["path"](batch["id"]), batch)
        shared["enqueue"](batch)

    @router.post("/jobs/{key}/revisions")
    def revise(key: str, revision: e.Revision):
        try:
            with lock:
                batch = load(key)
                digest = e.fingerprint(revision.model_dump())
                previous = next((r for r in batch["revisions"] if r["id"] == revision.revision_id), None)
                if previous:
                    if previous["fingerprint"] != digest:
                        raise HTTPException(409, "修订ID内容冲突。")
                    if previous["state"] != "submitted":
                        finish_revision(batch, previous)
                    return view(batch)
                if any(ctx.storage.read_metadata(p["task_id"]).get("status") in {"queued", "staged", "running", "cancelling"}
                       for p in batch["pages"]):
                    raise HTTPException(409, "请等待原任务停止或完成，再修改指定页面。")
                numbers = [p.page_number for p in revision.pages]
                if len(set(numbers)) != len(numbers) or any(n > len(batch["pages"]) for n in numbers):
                    raise HTTPException(400, "修订页码无效或重复。")
                job = e.Job.model_validate(batch["engine_request"])
                replacement = {p.page_number: p.model_dump(exclude_unset=True) for p in revision.pages}
                updated = job.model_copy(update={"pages": [replacement.get(p.page_number, p) for p in job.pages],
                    "visual_rules": revision.visual_rules if revision.visual_rules is not None else job.visual_rules,
                    "references": revision.references if revision.references is not None else job.references,
                    "product": revision.product if revision.product is not None else job.product}, deep=True)
                updated.pages = [e.Page.model_validate({**old.model_dump(), **replacement.get(old.page_number, {})})
                                 for old in job.pages]
                updated = e.Job.model_validate(updated.model_dump())
                # All-set style changes must explicitly list every affected page.
                if revision.visual_rules is not None and set(numbers) != set(range(1, len(job.pages) + 1)):
                    raise HTTPException(400, "修改整套风格时请明确列出全部需要重生成的页面。")
                prepare(updated, revision=revision.instructions)  # Validate before manifest.
                ident = e.fingerprint(["revision-budget", key, revision.revision_id])[:32]
                ensure_budget(ident, revision.authorization, len(numbers))
                rows = []
                for page in updated.pages:
                    if page.page_number not in numbers:
                        continue
                    task = ctx.storage.create_task("generate")
                    old = next(p for p in batch["pages"] if p["page_number"] == page.page_number)
                    rows.append({"page_number": page.page_number, "task_id": task.task_id, "enqueued": False,
                                 "version": old["version"] + 1, "versions": old["versions"] + [
                                     {k: v for k, v in old.items() if k != "versions"}],
                                 "content": page.model_dump()})
                record = {"id": revision.revision_id, "fingerprint": digest, "state": "preparing",
                          "request": updated.model_dump(), "instructions": revision.instructions,
                          "rows": rows, "budget_id": ident}
                batch["revisions"].append(record)
                write_json(shared["path"](batch["id"]), batch)
                finish_revision(batch, record)
                return view(batch)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    def finish_revision(batch, record):
        job = e.Job.model_validate(record["request"])
        numbers = {r["page_number"] for r in record["rows"]}
        # Keep whole-set context in every prompt, but persist only affected pages.
        data = prepare(job, revision=record["instructions"])
        provider, binding, refs, pages = data
        with ctx.reference_asset_storage.protect_submission(r.asset_id for r in job.references):
            persist_pages(batch, job, (provider, binding, refs, [p for p in pages if p[0].page_number in numbers]),
                          record["rows"], [record["budget_id"]], revision=record["id"])
        rows = {p["page_number"]: p for p in record["rows"]}
        batch["pages"] = [rows.get(p["page_number"], p) for p in batch["pages"]]
        batch["engine_request"] = job.model_dump()
        batch["budget_ids"] = list(dict.fromkeys(batch["budget_ids"] + [record["budget_id"]]))
        record["state"] = "submitted"
        batch["cancelled"] = False
        batch["submission_state"] = "ready"
        write_json(shared["path"](batch["id"]), batch)
        shared["enqueue"](batch)

    @router.post("/jobs/{key}/pages/{number}/text-review")
    def text_review(key: str, number: int, payload: dict = Body(...)):
        with lock:
            batch = load(key)
            row = next((p for p in batch["pages"] if p["page_number"] == number), None)
            if not row:
                raise HTTPException(404, "页码不存在。")
            observed = payload.get("observed_text")
            if not isinstance(observed, str) or len(observed) > 10000:
                raise HTTPException(400, "请提供人工或外部OCR实际识别的 observed_text，最多10000字。")
            expected = "\n".join(t for t in (row["content"]["title"], row["content"]["copy"]) if t)
            normalize = lambda text: re.sub(r"\s+", "", text)
            row["text_review"] = {"status": "matched" if normalize(expected) == normalize(observed) else "mismatch",
                                  "method": "caller_supplied_transcription", "observed_text": observed,
                                  "expected_text": expected, "version": row["version"], "checked_at": utc_now()}
            write_json(shared["path"](batch["id"]), batch)
            return row["text_review"]

    app.include_router(router)
