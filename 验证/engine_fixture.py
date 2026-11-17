"""Explicit offline provider, only imported by tests. Production never imports this file."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import re
import sys
import threading
import time

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "runtime" / "app"))
from PIL import Image, ImageDraw, ImageFont
from codex_image.client import ImageResult
from codex_image.webui.app import create_app
from codex_image.webui.provider_settings import ProviderSettings

CALLS = []
FAIL = set()
DELAY = .01
GUARD = threading.Lock()


def png(label="OFFLINE FIXTURE", color="#edf3f4", size=(512, 768)):
    output = io.BytesIO()
    image = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(image)
    font_path = Path("C:/Windows/Fonts/msyh.ttc")
    font = ImageFont.truetype(str(font_path), 18) if font_path.exists() else ImageFont.load_default()
    draw.text((22, 30), "离线模拟 / OFFLINE FIXTURE", fill="#a02328", font=font)
    for i, line in enumerate(label.splitlines()[:25]):
        draw.text((22, 85 + i * 25), line[:28], fill="#173c43", font=font)
    image.save(output, "PNG")
    return output.getvalue()


class FakeClient:
    def generate_image(self, **kwargs):
        prompt = kwargs["prompt"]
        page = int(re.search(r"仅生成第 (\d+) 页", prompt).group(1))
        with GUARD:
            CALLS.append({"page": page, "prompt": prompt,
                          "references": len(kwargs.get("reference_images") or []),
                          "size": kwargs.get("size"), "model": kwargs.get("model")})
        time.sleep(DELAY)
        if page in FAIL:
            raise RuntimeError("OFFLINE_SIMULATED_PROVIDER_FAILURE")
        digest = hashlib.sha256(prompt.encode()).hexdigest()
        title = prompt.split("【准确标题，逐字执行】\n", 1)[-1].split("\n", 1)[0]
        return ImageResult(image_bytes=png(f"模拟第 {page} 页\n{title}\n{digest[:20]}"),
            revised_prompt="", output_format="png", size="512x768", background="opaque",
            quality="low", usage={})


def make_app(data: Path, *, start=True, seed_provider=True):
    source = data / "webui-outputs" / "source-data"
    source.mkdir(parents=True, exist_ok=True)
    if seed_provider:
        ProviderSettings(data / "webui-api-settings.json").write({
            "providers": [{"id": "offline", "name": "OFFLINE TEST ONLY",
                           "base_url": "https://offline.invalid/v1",
                           "api_key": "offline-placeholder-not-a-real-key",
                           "image_model": "offline-remote-model", "api_mode": "images", "images_concurrency": 1}],
            "active_provider_id": "offline",
        })
    return create_app(input_root=data / "webui-inputs", output_root=data / "webui-outputs",
        source_data_root=source, client_factory=lambda: FakeClient(), auth_checker=lambda: True,
        auto_start_queue=start, auto_retry=True, batch_delay_seconds=0,
        **{name + "_path": data / filename for name, filename in {
            "webui_settings": "webui-settings.json", "auth_settings": "webui-auth-settings.json",
            "api_settings": "webui-api-settings.json", "network_egress_settings": "webui-network-egress-settings.json",
            "color_settings": "webui-color-settings.json", "prompt_snippets": "webui-prompt-snippets.json",
            "prompt_templates": "webui-prompt-templates.json",
        }.items()})


def example(key="offline-original", count=3):
    return {"task_id": key, "mode": "original", "project": "通用内容离线验证",
            "theme": "剪映模板如何用于批量视频制作", "audience": "电商内容团队",
            "product": {"name": "橘子智剪", "facts": [
                "此测试仅使用用户任务说明中的剪映模板与批量视频制作场景，不推断其他功能。"],
                "boundaries": ["未提供真实产品截图；不生成仿真界面，不宣称已验证产品功能。"]},
            "title": "剪映模板与批量视频制作",
            "content_mode": "final", "page_count": count,
            "pages": [{"page_number": i + 1, "role": "cover" if i == 0 else "explanation",
                       "title": ["先整理内容", "确认模板与素材", "核对成品"][i % 3],
                       "copy": f"离线验证第{i+1}页。以下仅为制作场景说明，不包含未经确认的产品功能。",
                       "visual": "原创抽象信息图，明确标记离线模拟，不绘制软件截图。"}
                      for i in range(count)],
            "generation": {"size": "1024x1536", "quality": "low"},
            "authorization": {"approved": True, "max_image_requests": count}}
