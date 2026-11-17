"""Company page workflow, local assets and deterministic watermark. Modified 2026-10-17."""
from __future__ import annotations

import hashlib
import io
import json
import re
import threading
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageOps

from .atomic_files import atomic_write_bytes, atomic_write_text

BUILD = "1.0.0-engine"
DEFAULTS = Path(__file__).resolve().parents[2] / "company-defaults"
_settings_lock = threading.RLock()
LEGACY_MARKERS = ("陪驾精准引流版", "女生」「陪驾", "女生与陪驾", "核心驾驶问题", "新手驾到")
LEGACY_RULE_DIGESTS = {
    "a5e10f95409b3f357b7a8eb2574dc964af5cf2181c063ebc16a23dec6f4ed28a",
    "97dd833c8fd96d4475f09679b4ebd907ddd68e584abb50e388f81a2b1f6bd400",
    "ac8c48f6d67d5ff17a2f384d749cd1107394531c89fb83d894abb38a095cdc5f",
    "2b2154eeeb10570e1b6cd85d57946dd53624521b596bca31254e504edecccdc9",
    "f7b02f8a11b0431f67c49ee4629cfd728c905e464a1d3fc9bb5532b7debe3741",
    "4942436e9ece069b60311775c5c93b44a1a40480a74ff2c97e178faa32005f12",
    "74c2660df9443f0251e85963e9b7266080b193f8ab72f1a296421ede78f5b228",
}


def legacy_policy(text: str) -> bool:
    return any(marker in text for marker in LEGACY_MARKERS) or (
        "陪驾" in text and any(word in text for word in ("必须", "强制", "封面", "默认", "保留", "精准引流")))


def workflow_root(storage: Any) -> Path:
    return storage.source_data_root / "company-workflow"


def write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2), mode=0o600)


def seed_defaults(root: Path) -> dict[str, Any]:
    """Back up exact bytes before migrating active industry defaults. Never touch keys."""
    with _settings_lock:
        root.mkdir(parents=True, exist_ok=True)
        path = root / "settings.json"
        defaults = {"schema_version": 2, "logo_sha256": "", "logo_name": "",
                    "watermark_enabled": False, "opacity": 13, "angle": 30,
                    "prompt": (DEFAULTS / "prompt.md").read_text(encoding="utf-8-sig")}
        if not path.exists():
            write_json(path, defaults)
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        if int(config.get("schema_version", 1)) < 2:
            raw = path.read_bytes()
            backup = root / "migration-backups" / f"settings-v1-{hashlib.sha256(raw).hexdigest()[:16]}.json"
            if not backup.exists():
                atomic_write_bytes(backup, raw, mode=0o600)
            archived = []
            if legacy_policy(str(config.get("prompt", ""))):
                config["legacy_inactive_prompt"] = config["prompt"]
                config["prompt"] = defaults["prompt"]
                archived.append("prompt")
            rules = config.get("copy_rules") or []
            retained = [rule for rule in rules if not legacy_policy(str(rule))
                        and hashlib.sha256(str(rule).encode()).hexdigest() not in LEGACY_RULE_DIGESTS]
            if retained != rules:
                config["legacy_inactive_copy_rules"] = rules
                config["copy_rules"] = retained
                config.pop("copy_marker", None)
                archived.append("copy_rules")
            config.update(schema_version=2, watermark_enabled=False,
                          migration={"backup": str(backup), "archived_fields": archived,
                                     "notice": "旧行业默认规则已停用；原配置完整备份，密钥和个人素材保留。"})
            for key, value in defaults.items():
                config.setdefault(key, value)
            write_json(path, config)
        return config


def pause_legacy_pending(storage: Any, queue: Any) -> None:
    """Old pending prompts must never run silently after this upgrade."""
    root = workflow_root(storage)
    seed_defaults(root)
    # Include old page metadata orphaned by an interrupted batch-manifest write.
    for meta in storage.read_tasks_from_metadata():
        page = (meta.get("params") or {}).get("company_page")
        if (not isinstance(page, dict) or page.get("engine_schema")
                or meta.get("status") not in {"queued", "running", "staged", "cancelling"}):
            continue
        task_id = meta["task_id"]
        backup = root / "migration-backups" / f"{task_id}-metadata.json"
        if not backup.exists():
            write_json(backup, meta)
        meta.update(status="paused", error="旧版在途任务已暂停，请按新模式确认内容后新建任务。")
        storage.write_metadata(task_id, meta)
        queue.remove_waiting(task_id)
    for path in (root / "batches").glob("*.json"):
        batch = json.loads(path.read_text(encoding="utf-8"))
        if batch.get("engine_schema"):
            continue
        changed = False
        for page in batch.get("pages", []):
            try:
                meta = storage.read_metadata(page["task_id"])
            except FileNotFoundError:
                continue
            if meta.get("status") == "paused":
                changed = changed or batch.get("submission_state") != "needs_review"
                continue
            if meta.get("status") not in {"queued", "running", "staged", "cancelling"}:
                continue
            backup = root / "migration-backups" / f"{page['task_id']}-metadata.json"
            if not backup.exists():
                write_json(backup, meta)
            meta.update(status="paused", error="旧版在途任务已暂停，请按新模式确认内容后新建任务。")
            storage.write_metadata(page["task_id"], meta)
            queue.remove_waiting(page["task_id"])
            changed = True
        if changed:
            backup = root / "migration-backups" / f"{batch['id']}-batch.json"
            if not backup.exists():
                write_json(backup, batch)
            batch["submission_state"] = "needs_review"
            write_json(path, batch)


def asset_path(root: Path, digest: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("固定水印素材编号无效")
    path = root / "assets" / f"{digest}.png"
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError("固定水印素材校验失败，请恢复原素材")
    return path


def apply_watermark(data: bytes, logo: bytes, opacity: int = 13) -> bytes:
    if not 10 <= opacity <= 15:
        raise ValueError("水印透明度必须在 10%～15% 之间")
    with Image.open(io.BytesIO(data)) as source:
        base = ImageOps.exif_transpose(source).convert("RGBA")
    with Image.open(io.BytesIO(logo)) as source:
        mark = ImageOps.exif_transpose(source).convert("RGBA")
    # Preserve all original shapes. Remove only the white matte, then tint gray.
    red, green, blue, alpha = mark.split()
    ink = ImageOps.invert(ImageChops.darker(ImageChops.darker(red, green), blue))
    ink = ImageChops.multiply(ink, alpha)
    bounds = ink.point(lambda v: 255 if v > 12 else 0).getbbox()
    if not bounds:
        raise ValueError("固定水印素材没有可见图形")
    ink = ink.crop(bounds)
    width = max(24, round(base.width * 0.28))
    height = max(1, round(ink.height * width / ink.width))
    ink = ink.resize((width, height), Image.Resampling.LANCZOS)
    ink = ink.point(lambda v: round(v * opacity / 100))
    stamp = Image.new("RGBA", ink.size, (140, 140, 140, 0))
    stamp.putalpha(ink)
    stamp = stamp.rotate(30, Image.Resampling.BICUBIC, expand=True)
    overlay = Image.new("RGBA", base.size)
    step_x, step_y = max(stamp.width + 12, round(width * 1.65)), max(stamp.height + 12, round(width * 1.2))
    for row, y in enumerate(range(-stamp.height // 2, base.height + stamp.height, step_y)):
        offset = -stamp.width // 3 + (step_x // 2 if row % 2 else 0)
        for x in range(offset - step_x, base.width + stamp.width, step_x):
            overlay.alpha_composite(stamp, (x, y))
    result = Image.alpha_composite(base, overlay)
    output = io.BytesIO()
    result.save(output, format="PNG")
    return output.getvalue()


def original_paths(storage: Any, task_id: str, index: int = 1) -> tuple[Path, Path]:
    storage._validate_task_id(task_id)
    prefix = workflow_root(storage) / "originals" / f"{task_id}-image-{index}"
    return prefix.with_suffix(".bin"), prefix.with_suffix(".json")


def store_and_watermark(storage: Any, task_id: str, data: bytes, index: int, page: dict) -> bytes:
    raw_path, info_path = original_paths(storage, task_id, index)
    if not raw_path.exists():
        atomic_write_bytes(raw_path, data, mode=0o600)
    # Reapplying always starts from the raw API result, never from a marked file.
    original = raw_path.read_bytes()
    with Image.open(io.BytesIO(original)) as image:
        info = {"size": f"{image.width}x{image.height}", "format": str(image.format or "PNG").lower()}
    write_json(info_path, info)
    if page.get("engine_schema"):
        brand = page.get("brand") or {}
        if brand.get("enabled"):
            with Image.open(io.BytesIO(original)) as image:
                base = ImageOps.exif_transpose(image).convert("RGBA")
            with Image.open(asset_path(workflow_root(storage), brand["sha256"])) as image:
                logo = ImageOps.exif_transpose(image).convert("RGBA")
            logo.thumbnail((max(1, round(base.width * brand["width_ratio"])),
                            max(1, round(base.height * .2))), Image.Resampling.LANCZOS)
            margin = max(8, round(min(base.size) * .025))
            base.alpha_composite(logo, (base.width - logo.width - margin, base.height - logo.height - margin))
            output = io.BytesIO()
            base.convert("RGB").save(output, "PNG")
            return output.getvalue()
        with Image.open(io.BytesIO(original)) as image:
            output = io.BytesIO()
            ImageOps.exif_transpose(image).save(output, "PNG")
            return output.getvalue()
    if not page.get("watermark_enabled", True):
        return original
    logo = asset_path(workflow_root(storage), str(page["logo_sha256"])).read_bytes()
    return apply_watermark(original, logo, int(page["opacity"]))


def cached_result(storage: Any, task_id: str) -> Any:
    """A local watermark failure must never purchase the same output again."""
    raw, info = original_paths(storage, task_id)
    if not raw.exists():
        return None
    from codex_image.client import ImageResult
    values = json.loads(info.read_text(encoding="utf-8")) if info.exists() else {}
    return ImageResult(image_bytes=raw.read_bytes(), output_format=values.get("format", "png"),
                       size=values.get("size", ""), quality="", background="", revised_prompt="", usage={})


def page_prompt(master: str, target: int, total: int, changes: str = "", selected: list[int] | None = None,
                policy: dict | None = None) -> str:
    from .company_copy import copy_policy
    master, copy_rules = copy_policy(
        master, total, selected if selected is not None else list(range(1, total + 1)), policy)
    return f"""【兼容参考页入口：本套 {total} 页】
仅生成第 {target} 页的 1 张独立图片，不合并其他页。
参考仅用于理解通用的信息层次和表达方法。围绕本次新内容原创设计，
不复制来源人物、品牌、文案、独特插画或整体视觉身份，不以去除他人水印重包装作为创作。
【用户本次要求】
{changes.strip() or '按用户基础指令围绕已确认事实创作。'}
【基础指令】
{master.rstrip()}
用户本次要求 > 产品事实与内容边界 > 已确认页面文案 > 全套一致性 > 参考表达方法。
不编造功能、评价、订单或效果证据；不指定行业、人物、颜色或水印。
{copy_rules}
"""


def reference_size(width: int, height: int) -> str:
    scale = 1536 / max(width, height)
    return f"{max(16, round(width * scale / 16) * 16)}x{max(16, round(height * scale / 16) * 16)}"


def page_reference_urls(params: dict, assets: list[dict], urls: list[str], fallback: list[str]) -> list[str]:
    """Keep the source set in history, while isolating the paid request to one page."""
    page = params.get("company_page") or {}
    if page.get("reference_strategy") == "selected":
        selected = page.get("selected_asset_ids", [])
        mapping = {asset["id"]: url for asset, url in zip(assets, urls, strict=True)}
        if any(ident not in mapping for ident in selected):
            raise ValueError("本页指定素材缺失，已停止请求。")
        return [mapping[ident] for ident in selected]
    if page.get("reference_strategy") != "target_only":
        return fallback
    target = [url for asset, url in zip(assets, urls, strict=True) if asset.get("id") == page.get("target_asset_id")]
    if len(target) != 1:
        raise ValueError("本页原图缺失，已停止请求；请核对参考图，不会用其他页替代")
    return target
