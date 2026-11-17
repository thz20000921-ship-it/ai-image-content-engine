"""Ordered, idempotent per-page jobs using the upstream queue. Company modification."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import zipfile
import subprocess
import uuid
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile, Request
from fastapi.responses import FileResponse, Response
from PIL import Image

from codex_image.webui.company_workflow import (
    BUILD, apply_watermark, asset_path, original_paths, seed_defaults, workflow_root, write_json,
)
from codex_image.webui.context import WebUIContext
from codex_image.webui.storage import utc_now
from codex_image.webui.company_batch_delete import (
    BatchDeleteConflict, delete_company_batch, deleted_batch_path, purge_deleted_results,
)
from .generation import _prepare_raster_uploads
from .tasks import OneTimeTaskArchiveResponse
from codex_image.webui import company_analyze, company_fetch
from codex_image.webui import company_copy as company_copy_policy
from codex_image.webui.company_local import (
    native_pick, record_selection, read_selection, export_pages, request_analysis,
)

_NOTE_KEY_PATTERNS = (
    (re.compile(r"/(?:note|video|slides)/(\d{15,})"), "douyin"),
    (re.compile(r"/(?:explore|discovery/item)/([0-9a-fA-F]{24})"), "xiaohongshu"),
)


def source_key(url: str) -> str:
    """把分享链接归一化成「平台:笔记ID」，短链/完整链/带参数都算同一条。"""
    text = str(url or "")
    for pattern, platform in _NOTE_KEY_PATTERNS:
        match = pattern.search(text)
        if match:
            return f"{platform}:{match.group(1).lower()}"
    return ""


def register_company_workflow_routes(app: FastAPI, ctx: WebUIContext) -> None:
    root = workflow_root(ctx.storage)
    lock = threading.RLock()
    picker_lock = threading.Lock()
    analysis_lock = threading.Lock()
    exports = {}
    link_sets: dict[str, tuple[Path, ...]] = {}
    fetch_lock = threading.Lock()

    def copy_policy_config(config: dict) -> dict:
        """User-editable title/numbering rules; the shipped file is only the seed."""
        rules = config.get("copy_rules")
        if not isinstance(rules, list) or not rules or not all(isinstance(r, str) and r.strip() for r in rules):
            rules = []
        if not rules:
            return company_copy_policy.default_policy()
        return {"marker": config.get("copy_marker") or company_copy_policy.default_policy()["marker"],
                "rules": [r.strip() for r in rules]}

    def download_root() -> Path:
        config = seed_defaults(root)
        directory = str(config.get("download_dir") or "").strip()
        # 独立版：默认落在本程序自己的数据目录下（…/复刻图文-Data/下载图文），不碰旧版 iLab 目录
        fallback = root.parents[2] / "下载图文" if len(root.parents) > 2 else root / "下载图文"
        return Path(directory).expanduser() if directory else fallback

    def require_local(request: Request) -> None:
        if not request.client or request.client.host not in {"127.0.0.1", "::1", "testclient"}:
            raise HTTPException(403, "此功能只允许在运行软件的本机使用")
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.url.netloc:
            raise HTTPException(403, "请从本机图文工作台打开此功能")

    def pick(request: Request, *, folder=False) -> tuple[str, dict]:
        require_local(request)
        if not picker_lock.acquire(blocking=False):
            raise HTTPException(409, "已有选择窗口打开，请先完成或取消")
        try:
            return record_selection(root, native_pick(folder=folder), folder=folder)
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            raise HTTPException(400, str(exc) if isinstance(exc, ValueError) else "选择窗口未完成，请重试或使用浏览器上传") from None
        finally:
            picker_lock.release()

    def batch_path(batch_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", batch_id):
            raise HTTPException(400, "套图编号无效")
        return root / "batches" / f"{batch_id}.json"

    def read_batch(batch_id: str) -> dict:
        batch_path(batch_id)
        if deleted_batch_path(ctx.storage, batch_id).is_file():
            raise HTTPException(410, "这套图文已删除")
        try:
            return json.loads(batch_path(batch_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise HTTPException(404, "没有找到这套图")

    def provider_summary() -> dict:
        settings = ctx.api_settings.public_settings()
        provider = next((p for p in settings["providers"] if p["id"] == settings["active_provider_id"]), {})
        bindings = [b for b in provider.get("bindings", []) if "generate" in b["operations"]]
        binding = next((b for b in bindings if b.get("is_default")), bindings[0] if bindings else {})
        return {"id": provider.get("id"), "name": provider.get("name"), "base_url": provider.get("base_url"),
                "model": binding.get("remote_model_id"), "binding_id": binding.get("id"),
                "concurrency": provider.get("concurrency"), "api_key_set": provider.get("api_key_set", False),
                "ready": bool(binding) and bool(provider.get("api_key_set")) and 1 <= int(provider.get("concurrency") or 1) <= 32,
                "api_mode": binding.get("protocol_profile", "")}

    def batch_view(batch: dict) -> dict:
        value = {k: v for k, v in batch.items() if k not in {"fingerprint", "pages", "source_selection_id"}}
        value["source_folder_known"] = bool(batch.get("source_selection_id"))
        pages = []
        for page in batch.get("pages", []):
            row = dict(page)
            try:
                meta = ctx.storage.read_metadata(page["task_id"])
                row.update(status=meta.get("status"), elapsed_seconds=meta.get("elapsed_seconds"),
                           error=meta.get("error") or meta.get("last_error") or "",
                           generated_count=meta.get("generated_count", 0), failed_count=meta.get("failed_count", 0))
                records = meta.get("outputs", [])
                row["elapsed_seconds"] = sum(r.get("elapsed_seconds") or 0 for r in records) or None
                company = meta.get("params", {}).get("company_page", {})
                order = meta.get("reference_image_order") or []
                source_id = company.get("target_asset_id") or (order[0].get("id") if order else None)
                source = next((r for r in meta.get("reference_assets", []) if r.get("id") == source_id), None)
                if source:
                    row.update(reference_url=f"/api/reference-assets/{source_id}/image",
                               reference_thumbnail_url=f"/api/reference-assets/{source_id}/thumbnail",
                               reference_name=source.get("filename", ""))
                output = next((r for r in records if r.get("status") == "completed" and r.get("file")), None)
                if output:
                    row["url"] = f"/api/tasks/{page['task_id']}/outputs/{output.get('index', 1)}/image"
                    row["usage"] = output.get("usage") or {}
                if any(r.get("status") == "failed" for r in records):
                    row["error"] = row["error"] or next(r.get("error", "") for r in records if r.get("status") == "failed")
                raw, _ = original_paths(ctx.storage, page["task_id"])
                row["original_available"] = raw.is_file()
            except (FileNotFoundError, ValueError):
                row["status"] = "missing"
            pages.append(row)
        value["pages"] = pages
        return value

    def company_queue_busy() -> bool:
        """队列里是否还有公司在跑/在等的页（用于一套跑完再跑下一套）。"""
        state = ctx.queue_storage.read_state()
        busy = set(state.get("waiting") or [])
        busy |= {r.get("task_id") for r in (state.get("running") or {}).values() if isinstance(r, dict)}
        if not busy:
            return False
        for other_path in (root / "batches").glob("*.json"):
            try:
                other = json.loads(other_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for page in other.get("pages") or []:
                if isinstance(page, dict) and page.get("task_id") in busy:
                    return True
        return False

    def mark_batch_pages(batch: dict, status: str) -> None:
        """挂起/放行时同步页任务状态，供重启恢复区分。"""
        for page in batch.get("pages") or []:
            try:
                meta = ctx.storage.read_metadata(page["task_id"])
            except (FileNotFoundError, ValueError):
                continue
            if meta.get("status") == status or meta.get("status") not in {"queued", "staged"}:
                continue
            meta["status"] = status
            meta["updated_at"] = utc_now()
            ctx.storage.write_metadata(page["task_id"], meta)

    def enqueue_batch_pages(batch: dict) -> None:
        """把一套里还没入队的页推进队列。"""
        # Retry of the POST recovers submission only; it never retries generated/failed pages.
        for page in batch["pages"]:
            meta = ctx.storage.read_metadata(page["task_id"])
            state = ctx.queue_storage.read_state()
            running = [v.get("task_id") for v in state.get("running", {}).values() if isinstance(v, dict)]
            if meta.get("status") in {"queued", "staged"}:
                if meta.get("status") != "queued":
                    meta["status"] = "queued"
                    meta["updated_at"] = utc_now()
                    ctx.storage.write_metadata(page["task_id"], meta)
                if page["task_id"] not in state["waiting"] and page["task_id"] not in running:
                    ctx.queue_storage.enqueue(page["task_id"])
            page["enqueued"] = True
            write_json(batch_path(batch["id"]), batch)
        batch["submission_state"] = "submitted"
        write_json(batch_path(batch["id"]), batch)
        if ctx.queue_manager is not None:
            channels = ctx.route_helpers["queue_channels_for_source"]("api")
            ctx.queue_manager.channels = channels
            ctx.queue_manager.max_attempts = ctx.route_helpers["queue_max_attempts_for_channels"](channels)
        ctx.route_helpers["wake_queue_worker"]()

    def enqueue_staged(batch: dict) -> None:
        with lock:
            waiting_exists = any(
                p.stem != batch["id"] and json.loads(p.read_text(encoding="utf-8")).get("submission_state") == "waiting_turn"
                for p in (root / "batches").glob("*.json"))
            if batch.get("submission_state") != "waiting_turn":
                batch["turn_requested_at"] = utc_now()
            if company_queue_busy() or waiting_exists:
                # 上一套还没跑完：先挂起，由放行 ticker 稍后提交
                batch["submission_state"] = "waiting_turn"
                write_json(batch_path(batch["id"]), batch)
                mark_batch_pages(batch, "staged")
                return
            enqueue_batch_pages(batch)

    def promote_once() -> None:
        with lock:
            if company_queue_busy():
                return
            waiting = []
            for other_path in (root / "batches").glob("*.json"):
                value = json.loads(other_path.read_text(encoding="utf-8"))
                if value.get("submission_state") == "waiting_turn":
                    waiting.append(value)
            if waiting:
                waiting.sort(key=lambda b: b.get("turn_requested_at") or b.get("created_at") or "")
                enqueue_batch_pages(waiting[0])

    promotion_stop = threading.Event()

    def promote_waiting_batches() -> None:
        while not promotion_stop.wait(1):
            try:
                promote_once()
                ctx.queue_worker_health.record_success(channel_id="batch-promotion")
            except (OSError, ValueError) as exc:
                ctx.queue_worker_health.record_failure(exc, channel_id="batch-promotion")

    def start_promotion() -> None:
        if ctx.auto_start_queue:
            promotion_stop.clear()
            threading.Thread(target=promote_waiting_batches, name="company-queue-turn", daemon=True).start()

    @app.get("/api/company-workflow")
    def settings() -> dict:
        config = seed_defaults(root)
        return {"build": BUILD, "prompt": config["prompt"], "opacity": config["opacity"],
                "download_dir": str(download_root()),
                "copy_rules": copy_policy_config(config)["rules"],
                "copy_rules_shipped": company_copy_policy.default_policy()["rules"],
                "analysis_model": config.get("analysis_model", ""), "analysis_protocol": config.get("analysis_protocol", "chat_completions"),
                "analysis_base_url": config.get("analysis_base_url", ""),
                "analysis_ready": bool(config.get("analysis_base_url") and config.get("analysis_api_key") and config.get("analysis_model")),
                "logo_name": config["logo_name"], "logo_sha256": config["logo_sha256"],
                "logo_url": "/api/company-workflow/logo" if config.get("logo_sha256") else "",
                "watermark_enabled": config.get("watermark_enabled", False),
                "migration": config.get("migration"), "provider": provider_summary()}

    @app.put("/api/company-workflow")
    def save_settings(payload: dict = Body(...)) -> dict:
        prompt = str(payload.get("prompt", "")).strip()
        opacity = payload.get("opacity", 13)
        if not 1 <= len(prompt) <= 8000 or type(opacity) is not int or not 10 <= opacity <= 15:
            raise HTTPException(400, "指令需为1～8000字，水印透明度需为10%～15%")
        with lock:
            config = seed_defaults(root)
            config.update(prompt=prompt, opacity=opacity)
            if "analysis_protocol" in payload:
                if payload["analysis_protocol"] not in {"responses", "chat_completions"}:
                    raise HTTPException(400, "分析协议无效。")
                config["analysis_protocol"] = payload["analysis_protocol"]
            for key, limit in (("analysis_model", 200), ("analysis_base_url", 400), ("analysis_api_key", 400)):
                if key not in payload:
                    continue
                value = str(payload[key]).strip()
                if len(value) > limit or any(ord(c) < 32 for c in value):
                    raise HTTPException(400, "识别模型设置无效")
                config[key] = value
            rules = payload.get("copy_rules")
            if rules is not None:
                if not isinstance(rules, list) or not 1 <= len(rules) <= 12:
                    raise HTTPException(400, "规则需为 1～12 条")
                cleaned = []
                for rule in rules:
                    text = str(rule).strip()
                    if not 1 <= len(text) <= 2000:
                        raise HTTPException(400, "每条规则需为 1～2000 字")
                    cleaned.append(text)
                if sum(len(r) for r in cleaned) > 8000:
                    raise HTTPException(400, "规则总字数过多，请精简")
                config["copy_rules"] = cleaned
            directory = payload.get("download_dir")
            if directory is not None:
                text = str(directory).strip()
                if text and (len(text) > 260 or any(ord(c) < 32 for c in text)):
                    raise HTTPException(400, "保存目录无效")
                config["download_dir"] = text
            write_json(root / "settings.json", config)
        return settings()

    @app.post("/api/company-workflow/analyze")
    async def analyze_pages(request: Request, analysis_id: str = Form(...), confirmed: bool = Form(False),
                            context: str = Form("{}"), reference_images: list[UploadFile] = File(...)) -> dict:
        require_local(request)
        if not confirmed or not re.fullmatch(r"[0-9a-f]{32}", analysis_id):
            raise HTTPException(400, "请确认本次分析调用授权，并提供唯一 analysis_id。")
        if not 1 <= len(reference_images) <= 12:
            raise HTTPException(400, "请先准备 1～12 页参考图")
        config = seed_defaults(root)
        uploaded = await _prepare_raster_uploads(reference_images, preserve_positions=True)
        images = [image.data for image in uploaded]
        try:
            brief = json.loads(context)
            if not isinstance(brief, dict) or len(context) > 100000:
                raise ValueError()
        except ValueError:
            raise HTTPException(400, "分析任务数据无效。") from None
        path = root / "analyses" / f"{analysis_id}.json"
        digest = hashlib.sha256((context + "".join(image.sha256 for image in uploaded)).encode()).hexdigest()
        if not analysis_lock.acquire(blocking=False):
            raise HTTPException(409, "已有分析在进行。")
        try:
            if path.exists():
                saved = json.loads(path.read_text(encoding="utf-8"))
                if saved.get("fingerprint") != digest:
                    raise HTTPException(409, "分析ID已用于不同内容。")
                if saved.get("status") == "completed":
                    return saved
                raise HTTPException(409, "分析已发起过，可能已计费，未重复调用。")
            write_json(path, {"status": "started", "fingerprint": digest})
            from starlette.concurrency import run_in_threadpool
            from functools import partial
            transport = ctx.network_egress_manager.transport(ctx.network_egress_manager.snapshot(),
                timeout_seconds=company_analyze.ANALYSIS_TIMEOUT_SECONDS)
            result = await run_in_threadpool(partial(company_analyze.analyze,
                images,
                base_url=str(config.get("analysis_base_url") or ""),
                api_key=str(config.get("analysis_api_key") or ""),
                model=str(config.get("analysis_model") or ""),
                brief=brief, protocol=config.get("analysis_protocol", "chat_completions"),
                max_tokens=int(config.get("analysis_max_tokens", company_analyze.MAX_TOKENS)),
                transport=transport,
            ))
            value = {"status": "completed", "fingerprint": digest,
                     "summary": company_analyze.compose(result["value"]), "analysis": result["value"],
                     "style": result["value"].get("visual_rules", []), "pages": result["value"].get("pages", []),
                     "model": result["model"], "elapsed": result["elapsed"]}
            write_json(path, value)
            return value
        except company_analyze.AnalyzeError as exc:
            write_json(path, {"status": "failed", "fingerprint": digest})
            raise HTTPException(502, str(exc)) from None
        finally:
            analysis_lock.release()

    @app.post("/api/company-workflow/drafts")
    def create_draft(request: Request, payload: dict = Body(...)) -> dict:
        require_local(request)
        batch_id = str(payload.get("batch_id") or "")
        batch_path(batch_id)
        images = payload.get("images") or []
        if not isinstance(images, list) or not 1 <= len(images) <= 12:
            raise HTTPException(400, "草稿套需要 1～12 张参考图")
        with lock:
            if deleted_batch_path(ctx.storage, batch_id).is_file():
                raise HTTPException(410, "这套图文已删除，请重新取图")
            record = {
                "id": batch_id, "created_at": utc_now(), "build": BUILD, "status": "draft",
                "reference_count": len(images), "selected_pages": [], "request_count": 0,
                "company_draft": {
                    "title": str(payload.get("title") or "").strip()[:80],
                    "folder": str(payload.get("folder") or "").strip()[:260],
                    "source_url": str(payload.get("source_url") or "").strip()[:2000],
                    "source_key": source_key(str(payload.get("source_url") or "")),
                    "images": [str(name)[:120] for name in images],
                },
                "pages": [],
            }
            write_json(batch_path(batch_id), record)
        return batch_view(record)

    @app.get("/api/company-workflow/copy-rules")
    def copy_rules() -> dict:
        return copy_policy_config(seed_defaults(root))

    @app.post("/api/company-workflow/link")
    def fetch_link(request: Request, payload: dict = Body(...)) -> dict:
        require_local(request)
        text = str(payload.get("text", "")).strip()
        if not 1 <= len(text) <= 2000:
            raise HTTPException(400, "把小红书或抖音的分享内容粘贴进来")
        if not fetch_lock.acquire(blocking=False):
            raise HTTPException(409, "正在取上一条链接，请等几秒再试")
        try:
            note = company_fetch.fetch(text, download_root=download_root(), profile=root / "browser")
        except company_fetch.FetchError as exc:
            raise HTTPException(400, str(exc)) from None
        finally:
            fetch_lock.release()
        token = uuid.uuid4().hex
        with lock:
            link_sets[token] = note.files
            while len(link_sets) > 40:
                link_sets.pop(next(iter(link_sets)))
        return {"platform": note.platform, "title": note.title, "folder": str(note.folder),
                "count": len(note.files),
                "source_url": note.source_url,
                "total": max(note.total_images, len(note.files)),
                "truncated": max(note.total_images, len(note.files)) > len(note.files),
                "images": [{"name": path.name,
                            "url": f"/api/company-workflow/link-assets/{token}/{index}"}
                           for index, path in enumerate(note.files)]}

    @app.get("/api/company-workflow/link-assets/{token}/{index}")
    def link_image(request: Request, token: str, index: int) -> FileResponse:
        require_local(request)
        with lock:
            files = link_sets.get(token)
        if not files or not 0 <= index < len(files):
            raise HTTPException(404, "图片已失效，请重新取图")
        path = files[index]
        media = {".png": "image/png", ".webp": "image/webp"}.get(path.suffix.lower(), "image/jpeg")
        return FileResponse(path, media_type=media)

    @app.post("/api/company-workflow/download-dir")
    def choose_download_dir(request: Request) -> dict:
        token, value = pick(request, folder=True)
        directory = str(value.get("directory") or "").strip()
        if not directory:
            raise HTTPException(400, "没有选择文件夹，已保留原来的保存目录")
        with lock:
            config = seed_defaults(root)
            config["download_dir"] = directory
            write_json(root / "settings.json", config)
        return {"download_dir": directory}

    @app.get("/api/company-workflow/logo")
    def logo() -> FileResponse:
        config = seed_defaults(root)
        if not config.get("logo_sha256"):
            raise HTTPException(404, "默认不使用水印。请在任务中指定自己的品牌素材。")
        return FileResponse(asset_path(root, config["logo_sha256"]), media_type="image/png")

    @app.get("/api/company-workflow/preview")
    def preview(opacity: int = 13) -> Response:
        if not 10 <= opacity <= 15:
            raise HTTPException(400, "透明度无效")
        config = seed_defaults(root)
        if not config.get("logo_sha256"):
            raise HTTPException(404, "没有已配置水印。")
        buffer = io.BytesIO()
        Image.new("RGB", (900, 1200), "#ffffff").save(buffer, format="PNG")
        data = apply_watermark(buffer.getvalue(), asset_path(root, config["logo_sha256"]).read_bytes(), opacity)
        return Response(data, media_type="image/png")

    @app.get("/api/company-workflow/batches")
    def batches() -> dict:
        with lock:
            # 超过一天没动的空草稿（批量中断留下的）自动清掉，不留垃圾记录。
            cutoff = time.time() - 24 * 3600
            paths = []
            for candidate in (root / "batches").glob("*.json"):
                try:
                    stale = candidate.stat().st_mtime < cutoff
                    if stale:
                        value = json.loads(candidate.read_text(encoding="utf-8"))
                        if value.get("status") == "draft" and not value.get("pages"):
                            candidate.unlink(missing_ok=True)
                            continue
                except (OSError, ValueError):
                    pass
                paths.append(candidate)
            paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            visible = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
            return {"batches": [batch_view(b) for b in visible if not b.get("hidden_at")][:100]}

    @app.delete("/api/company-workflow/batches/{batch_id}")
    def delete_batch(batch_id: str) -> dict:
        with lock:
            batch_path(batch_id)
            if batch_path(batch_id).is_file():
                try:
                    pending = json.loads(batch_path(batch_id).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    pending = {}
                if not pending.get("pages"):
                    batch_path(batch_id).unlink(missing_ok=True)
                    return {"id": batch_id, "deleted": True, "pages": []}
            receipt = deleted_batch_path(ctx.storage, batch_id)
            try:
                if receipt.is_file():
                    value = json.loads(receipt.read_text(encoding="utf-8"))
                    purge_deleted_results(ctx.storage, value)
                    return value
                return delete_company_batch(ctx, read_batch(batch_id))
            except BatchDeleteConflict as exc:
                raise HTTPException(409, str(exc)) from None
            except (OSError, ValueError):
                raise HTTPException(500, "删除未完成，记录已保留，可再次点击删除。") from None

    @app.post("/api/company-workflow/originals/purge")
    def purge_originals(days: int = Body(7, embed=True)) -> dict:
        """手动清理旧的原图缓存。删掉后“重做水印/下载无水印原图”就没有了。"""
        if not 1 <= days <= 3650:
            raise HTTPException(400, "天数需为 1～3650")
        cutoff = time.time() - days * 86400
        pattern = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}-image-[1-9]\d*\.bin")
        removed = freed = 0
        with lock:
            for path in (root / "originals").glob("*-image-*.bin"):
                if not pattern.fullmatch(path.name) or path.is_symlink() or not path.is_file():
                    continue
                try:
                    if path.stat().st_mtime >= cutoff:
                        continue
                    info = path.with_suffix(".json")
                    size = path.stat().st_size + (info.stat().st_size if info.is_file() else 0)
                    path.unlink(missing_ok=True)
                    info.unlink(missing_ok=True)
                except OSError:
                    continue
                removed += 1
                freed += size
        return {"removed": removed, "freed_bytes": freed}

    @app.post("/api/company-workflow/local-pick")
    def local_pick(request: Request) -> dict:
        token, value = pick(request)
        return {"selection_id": token, "folder_name": Path(value["directory"]).name if token else "",
                "files": [{"name": row["name"], "url": f"/api/company-workflow/local-selections/{token}/{i}"}
                          for i, row in enumerate(value.get("files", []))]}

    @app.get("/api/company-workflow/local-selections/{token}/{index}")
    def local_image(request: Request, token: str, index: int) -> FileResponse:
        require_local(request)
        try:
            value = read_selection(root, token)
            if not 0 <= index < len(value["files"]):
                raise ValueError("图片编号无效")
            row = value["files"][index]
            path = Path(row["path"])
            if path.stat().st_size > 32 * 1024 * 1024 or hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
                raise ValueError("原图已修改，请重新选择")
            return FileResponse(path, filename=row["name"])
        except (ValueError, OSError):
            raise HTTPException(404, "原图无法读取或已改变，请重新选择") from None

    @app.post("/api/company-workflow/batches/{batch_id}/export")
    def local_export(request: Request, batch_id: str, payload: dict = Body(...)) -> dict:
        require_local(request)
        batch = read_batch(batch_id)
        token = batch.get("source_selection_id", "")
        if payload.get("choose_folder") or not token:
            token, _ = pick(request, folder=True)
            if not token:
                return {"cancelled": True}
        try:
            result = export_pages(ctx.storage, root, batch, token)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc) if isinstance(exc, ValueError) else "保存失败，请检查文件夹权限或更换位置") from None
        with lock:
            current = read_batch(batch_id)
            current["source_selection_id"] = token
            write_json(batch_path(batch_id), current)
        export_id = uuid.uuid4().hex
        exports[export_id] = result["path"]
        return {**result, "export_id": export_id}

    @app.post("/api/company-workflow/exports/{export_id}/open")
    def open_export(request: Request, export_id: str) -> dict:
        require_local(request)
        path = exports.get(export_id)
        if not path or not Path(path).is_dir():
            raise HTTPException(404, "保存位置已失效，请从资源管理器打开")
        os.startfile(path)
        return {"opened": True}

    @app.post("/api/company-workflow/analyze-legacy")
    async def analyze(
        request: Request, analysis_id: str = Form(...), model: str = Form(...), protocol: str = Form("responses"),
        changes: str = Form(""), confirmed: bool = Form(False), reference_images: list[UploadFile] = File(...),
    ) -> dict:
        require_local(request)
        if not confirmed or not re.fullmatch(r"[0-9a-f]{32}", analysis_id):
            raise HTTPException(400, "请先确认分析素材、模型与费用")
        if not model.strip() or len(model) > 200 or any(ord(c) < 32 for c in model) or protocol not in {"responses", "chat_completions"}:
            raise HTTPException(400, "请选择平台支持的看图模型和调用方式")
        if not 1 <= len(reference_images) <= 12 or len(changes) > 4000:
            raise HTTPException(400, "请上传1～12张参考图，修改要求不超过4000字")
        uploaded = await _prepare_raster_uploads(reference_images, preserve_positions=True)
        status_path = root / "analyses" / f"{analysis_id}.json"
        if not analysis_lock.acquire(blocking=False):
            raise HTTPException(409, "已有分析在进行，请等待")
        try:
            if status_path.exists():
                saved = json.loads(status_path.read_text(encoding="utf-8"))
                if saved.get("suggestion"):
                    return saved
                raise HTTPException(409, "本次分析已经发起过，未重复扣费。请先查看平台记录后再决定是否重新分析")
            write_json(status_path, {"status": "started", "model": model, "protocol": protocol})
            from starlette.concurrency import run_in_threadpool
            try:
                suggestion = await run_in_threadpool(request_analysis, ctx, model.strip(), protocol, [v.data for v in uploaded], changes)
                result = {"status": "completed", "model": model.strip(), "protocol": protocol, "suggestion": suggestion}
                write_json(status_path, result)
                return result
            except ValueError as exc:
                write_json(status_path, {"status": "failed", "model": model, "protocol": protocol})
                raise HTTPException(502, str(exc)) from None
        finally:
            analysis_lock.release()

    @app.get("/api/company-workflow/batches/{batch_id}")
    def get_batch(batch_id: str) -> dict:
        return batch_view(read_batch(batch_id))

    @app.post("/api/company-workflow/batches")
    async def create_batch(
        batch_id: str = Form(...), prompt: str = Form(...), selected_pages: str = Form(...),
        confirmed_count: int = Form(...), opacity: int = Form(13), quality: str = Form("medium"),
        changes: str = Form(""),
        source_selection_id: str = Form(""),
        title: str = Form(""),
        source_url: str = Form(""),
        reference_images: list[UploadFile] = File(...),
    ) -> dict:
        # Keep the legacy import contract readable, but require the bounded job API for paid work.
        raise HTTPException(410, "旧复刻提交已停用。请使用工作台新创作表单或 /api/engine/jobs，显式指定文案和请求额度。")

    @app.post("/api/company-workflow/batches/{batch_id}/resume")
    def resume(batch_id: str) -> dict:
        with lock:
            batch = read_batch(batch_id)
            if batch.get("submission_state") == "needs_review":
                raise HTTPException(409, "旧在途任务需按新模式确认内容后新建，历史和原始素材保留。")
            if batch["submission_state"] == "submitted":
                return batch_view(batch)
            if len(batch["pages"]) != batch["request_count"] or any(not ctx.storage.metadata_path(p["task_id"]).is_file() for p in batch["pages"]):
                raise HTTPException(409, "部分页面尚未保存。请保留这套记录，修复磁盘或文件权限后重新新建；本套未自动发起请求")
            enqueue_staged(batch)
            return batch_view(batch)

    # Share the existing scheduler/storage with the structured API; no second worker.
    ctx.route_helpers["company_batches"] = {
        "lock": lock, "read": read_batch, "path": batch_path, "view": batch_view,
        "enqueue": enqueue_staged, "download": download_root, "promote": promote_once,
        "start": start_promotion, "stop": promotion_stop.set,
    }

    @app.get("/api/company-workflow/tasks/{task_id}/original")
    def original(task_id: str) -> FileResponse:
        try:
            raw, info = original_paths(ctx.storage, task_id)
            values = json.loads(info.read_text(encoding="utf-8"))
            if not raw.is_file():
                raise FileNotFoundError()
        except (ValueError, FileNotFoundError):
            raise HTTPException(404, "未找到未加水印原图")
        fmt = values["format"]
        return FileResponse(raw, media_type=f"image/{fmt}", filename=f"{task_id}-original.{fmt}")

    @app.get("/api/company-workflow/batches/{batch_id}/download")
    def download(batch_id: str) -> FileResponse:
        batch = batch_view(read_batch(batch_id))
        temp_root = root / "temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(suffix=".zip", dir=temp_root)
        os.close(fd)
        path = Path(name)
        try:
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                count = 0
                total = 0
                for page in batch["pages"]:
                    try:
                        meta = ctx.storage.read_metadata(page["task_id"])
                    except FileNotFoundError:
                        continue
                    output = next((r for r in meta.get("outputs", []) if r.get("status") == "completed" and r.get("file")), None)
                    if output:
                        image_path = ctx.storage.output_path(output["file"])
                        if not image_path.is_file():
                            continue
                        total += image_path.stat().st_size
                        if total > 512 * 1024 * 1024:
                            raise HTTPException(413, "结果超过512MB，请逐页下载")
                        archive.write(image_path, f"{page['page_number']:02d}-第{page['page_number']}页{image_path.suffix}")
                        count += 1
                if not count:
                    raise HTTPException(409, "暂时没有可下载的成功页面")
                archive.writestr("结果说明.json", json.dumps(batch, ensure_ascii=False, indent=2))
            return OneTimeTaskArchiveResponse(path, filename=f"conjure-{batch_id[:8]}-pages.zip")
        except Exception:
            path.unlink(missing_ok=True)
            raise
