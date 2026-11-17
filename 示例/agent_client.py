"""Standard local HTTP example. Preview is free; execution requires explicit request scope."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8787")
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("ILAB_CONJURE_DATA_DIR") or Path.home() / "复刻图文-Data"))
    parser.add_argument("--job", type=Path, default=Path(__file__).with_name("原创任务.json"))
    parser.add_argument("--task-id")
    parser.add_argument("--output", type=Path, default=Path.cwd() / "图文交付")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approve-image-requests", type=int, default=0)
    args = parser.parse_args()
    if args.execute and not args.task_id:
        parser.error("执行时必须指定稳定 --task-id；重复运行沿用此ID，不额外付费。")
    payload = json.loads(args.job.read_text(encoding="utf-8-sig"))
    payload["task_id"] = args.task_id or "example-preview"
    payload["authorization"] = {"approved": args.execute, "max_image_requests": args.approve_image_requests}
    token = (args.data_dir / "webui-outputs" / "source-data" / "company-workflow" / "agent-token.txt").read_text(encoding="utf-8").strip()
    with httpx.Client(base_url=args.base_url, headers={"Authorization": "Bearer " + token}, timeout=60) as client:
        estimate = client.post("/api/engine/estimate", json=payload)
        estimate.raise_for_status()
        print(json.dumps(estimate.json(), ensure_ascii=False, indent=2))
        if not args.execute:
            print("仅预览，未提交生图任务。")
            return
        if args.approve_image_requests < estimate.json()["initial_image_requests"]:
            parser.error("授权请求数少于输出页数；未提交。")
        response = client.post("/api/engine/jobs", json=payload)
        response.raise_for_status()
        job = response.json()
        print(f"任务ID: {job['job_id']}；套图编号: {job['id']}")
        deadline = time.monotonic() + 3600
        while time.monotonic() < deadline:
            response = client.get(f"/api/engine/jobs/{job['job_id']}")
            response.raise_for_status()
            job = response.json()
            print(f"{job['status']}: {job['completed_pages']}/{job['total_pages']} 页；失败 {job['failed_pages']}")
            if job["status"] == "completed":
                break
            if job["status"] in {"failed", "partial_failed", "cancelled"}:
                raise RuntimeError("有页面未完成。查询错误和额度后显式重试；本示例不自动付费重试。")
            time.sleep(2)
        else:
            raise TimeoutError("停止本次轮询。任务仍保留，沿用同一任务ID查询。")
        directory = args.output / job["job_id"]
        directory.mkdir(parents=True, exist_ok=True)
        for row in job["pages"]:
            response = client.get(row["download_url"])
            response.raise_for_status()
            if not response.content.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("下载结果不是PNG文件。")
            (directory / f"{row['page_number']:02d}-v{row['version']}.png").write_bytes(response.content)
        response = client.get(job["download_url"])
        response.raise_for_status()
        (directory / "整套.zip").write_bytes(response.content)
        (directory / "manifest.json").write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已交付: {directory.resolve()}。请核对图中文字及产品事实。")


if __name__ == "__main__":
    main()
