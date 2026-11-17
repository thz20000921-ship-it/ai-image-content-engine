"""Import allowed note images through normal browser access.

Login, CAPTCHA and access restrictions stop import. Users can supply local materials
instead. No challenge solving, cookie extraction or access-control bypass is provided.
"""
from __future__ import annotations

import html
import io
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from PIL import Image

from .company_browser import DriverError, capture_note

BUILD = "0.9.8-company-r4"

Platform = Literal["xiaohongshu", "douyin"]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
RENDER_TIMEOUT_SECONDS = 120
VIRTUAL_TIME_BUDGET_MS = 20000
DOWNLOAD_TIMEOUT_SECONDS = 60
MAX_IMAGES = 12

PLATFORM_LABELS: dict[str, str] = {"xiaohongshu": "小红书", "douyin": "抖音"}
BROWSER_PATHS: tuple[str, ...] = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


class FetchError(Exception):
    """A user-facing failure: the message is shown on the page as-is."""


@dataclass(frozen=True)
class NoteImage:
    url: str
    width: int = 0
    height: int = 0


@dataclass(frozen=True)
class FetchedNote:
    platform: Platform
    title: str
    source_url: str
    folder: Path
    files: tuple[Path, ...]
    total_images: int = 0


def platform_of(url: str) -> Platform:
    host = (urlsplit(url).hostname or "").lower()
    if (host.endswith("xiaohongshu.com") or host.endswith("xhslink.com")
            or host == "rednote.com" or host.endswith(".rednote.com")):
        return "xiaohongshu"
    if host.endswith("douyin.com") or host.endswith("iesdouyin.com"):
        return "douyin"
    raise FetchError("目前只支持小红书和抖音的图文链接。")


def link_of(text: str) -> str:
    """Pick the first http(s) URL out of share text such as '标题 😆 短链 😆 链接'."""
    match = re.search(r"https?://[^\s，。；、<>\"'）】]+", str(text or ""))
    if not match:
        raise FetchError("没找到链接。把小红书或抖音的分享内容整段粘进来就行。")
    return match.group(0).rstrip(".,;")


def _resolve_short_link(url: str) -> str:
    """Follow an xhslink/v.douyin short link to the real note address."""
    host = (urlsplit(url).hostname or "").lower()
    if not (host.endswith("xhslink.com") or host in {"v.douyin.com", "v.iesdouyin.com"}):
        return url
    try:
        response = httpx.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=25)
        return str(response.url)
    except httpx.HTTPError as error:
        raise FetchError("短链接打不开，请换成 App 里“复制链接”得到的完整链接。") from error


def _xhs_note_url(url: str) -> str:
    """Rewrite a share address into the form the note page accepts.

    ``/discovery/item/<id>`` is rewritten by the site into ``/explore/<id>`` while
    dropping ``xsec_token``, which lands on the login wall, so the token is kept
    explicitly on the explore address.
    """
    parts = urlsplit(url)
    match = re.search(r"/(?:discovery/item|explore|item)/([0-9a-fA-F]{24})", parts.path)
    if not match:
        raise FetchError("这个小红书链接里没有笔记编号，请复制笔记页的分享链接。")
    token = ""
    for pair in parts.query.split("&"):
        key, _, value = pair.partition("=")
        if key == "xsec_token" and value:
            token = value
    query = f"xsec_token={token}&xsec_source=pc_share" if token else "xsec_source=pc_share"
    host = parts.hostname or ""
    # 国际站(rednote.com)的笔记只在它自己的域名上能打开，不能换成国内站域名
    site = "www.rednote.com" if host == "rednote.com" or host.endswith(".rednote.com") else "www.xiaohongshu.com"
    return f"https://{site}/explore/{match.group(1)}?{query}"


def find_browser() -> Path:
    override = os.environ.get("ILAB_BROWSER_PATH", "").strip()
    candidates = ((override,) if override else ()) + BROWSER_PATHS
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            return path
    raise FetchError("没有找到 Edge 或 Chrome，无法打开链接。请安装 Edge 后再试。")


def _douyin_dom(url: str, profile: Path, browser: Path) -> str:
    profile.mkdir(parents=True, exist_ok=True)
    command = [
        str(browser),
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={profile}",
        f"--user-agent={USER_AGENT}",
        f"--virtual-time-budget={VIRTUAL_TIME_BUDGET_MS}",
        "--dump-dom",
        url,
    ]
    try:
        finished = subprocess.run(
            command, capture_output=True, timeout=RENDER_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as error:
        raise FetchError("浏览器打开这个链接超时，请重试。") from error
    dom = finished.stdout.decode("utf-8", "replace")
    if len(dom) < 2000:
        raise FetchError("浏览器没有返回页面内容，请重试。")
    return dom


def _douyin_images(dom: str) -> list[NoteImage]:
    """Douyin serves every slide on several hosts; keep the first host per slide."""
    ordered: list[NoteImage] = []
    seen: set[str] = set()
    for raw in re.findall(r'https?://[^\s"\'<>]*douyinpic\.com/[^\s"\'<>]*', dom):
        url = html.unescape(raw).replace("\\/", "/")
        if "tplv-dy-aweme-images" not in url:
            continue
        key = url.split("?")[0].split("~tplv")[0].rsplit("/", 1)[-1]
        if key in seen:
            continue
        seen.add(key)
        ordered.append(NoteImage(url))
    return ordered


def _douyin_title(dom: str) -> str:
    for pattern in (
        r'<meta[^>]+name="description"[^>]+content="([^"]*)"',
        r'<meta[^>]+property="og:description"[^>]+content="([^"]*)"',
    ):
        match = re.search(pattern, dom)
        if match:
            text = html.unescape(match.group(1)).strip()
            return re.split(r"于\d{8}发布在抖音", text)[0][:120]
    return ""


def _safe_name(text: str, fallback: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "", str(text or "")).strip(" .")
    cleaned = re.sub(r"\s+", "", cleaned)
    return cleaned[:28] or fallback


def target_folder(root: Path, platform: Platform, title: str) -> Path:
    """One folder per note: date first, then the note title."""
    root.mkdir(parents=True, exist_ok=True)
    base = f"{datetime.now():%Y-%m-%d}-{_safe_name(title, PLATFORM_LABELS[platform] + '图文')}"
    folder, index = root / base, 2
    while folder.exists():
        folder, index = root / f"{base}-{index}", index + 1
    folder.mkdir(parents=True)
    return folder


def _suffix(data: bytes) -> str:
    with Image.open(io.BytesIO(data)) as image:
        fmt = (image.format or "").lower()
    return {"jpeg": ".jpg", "png": ".png", "webp": ".webp", "gif": ".gif"}.get(fmt, ".jpg")


def download(images: list[NoteImage], folder: Path, platform: Platform) -> tuple[Path, ...]:
    base_referer = "https://www.xiaohongshu.com/" if platform == "xiaohongshu" else "https://www.douyin.com/"
    headers = {"User-Agent": USER_AGENT, "Referer": base_referer, "Accept": "image/webp,image/*,*/*"}
    saved: list[Path] = []
    with httpx.Client(headers=headers, follow_redirects=True, timeout=DOWNLOAD_TIMEOUT_SECONDS) as client:
        for number, image in enumerate(images, start=1):
            try:
                # 国际站图片来自 rednotecdn，Referer 用对应站点更稳
                referer = "https://www.rednote.com/" if "rednote" in image.url else base_referer
                response = client.get(image.url, headers={"Referer": referer})
                response.raise_for_status()
                data = response.content
                if len(data) < 2048:
                    raise ValueError("图片内容为空")
                suffix = _suffix(data)
            except FetchError:
                raise
            except Exception as error:  # noqa: BLE001 - name the failing slide, keep the rest
                raise FetchError(f"第 {number} 张图片下载失败：{error}") from error
            path = folder / f"{number:02d}{suffix}"
            path.write_bytes(data)
            saved.append(path)
    return tuple(saved)


def write_source_note(folder: Path, platform: Platform, title: str, url: str, count: int) -> None:
    text = "\n".join([
        "[来源]",
        f"平台: {PLATFORM_LABELS[platform]}",
        f"标题: {title}",
        f"链接: {url}",
        f"图片数: {count}",
        f"抓取时间: {datetime.now():%Y-%m-%d %H:%M}",
        "",
        "说明: 图片由本机浏览器渲染笔记页后直接下载，未经任何第三方站点。",
        "用途: 仅作本机图文改编的参考图；二次生成前请自行确认素材使用权。",
    ])
    (folder / "来源.txt").write_text(text + "\n", encoding="utf-8")


def probe(link: str, *, profile: Path, browser: Path | None = None) -> tuple[Platform, str, list[NoteImage], str, int]:
    """Resolve a share link to (platform, title, images, note url, total images found).

    ``images`` is capped at ``MAX_IMAGES``; the fifth value is the real count, so the
    caller can tell the user that a long note was truncated.
    """
    url = _resolve_short_link(link_of(link))
    platform = platform_of(url)
    exe = browser or find_browser()
    if platform == "xiaohongshu":
        try:
            captured = capture_note(
                _xhs_note_url(url), browser=exe, profile=profile, settle_seconds=8.0,
            )
        except DriverError as error:
            raise FetchError(str(error)) from error
        images = [
            NoteImage(str(item.get("url") or "").replace("http://", "https://", 1),
                      int(item.get("width") or 0), int(item.get("height") or 0))
            for item in captured.get("images") or []
            if str(item.get("url") or "").startswith("http")
        ]
        title = str(captured.get("title") or "").strip()
        final_url = str(captured.get("url") or "")
        if captured.get("access_restricted"):
            raise FetchError("平台要求登录、验证或访问受限，已停止导入。请使用正常获权的本地素材。")
        if not images and "/login" in final_url:
            raise FetchError("平台要求登录，已停止导入。请正常登录并确认素材使用权，或上传本地素材。")
    else:
        dom = _douyin_dom(url, profile, exe)
        if re.search(r"<title[^>]*>[^<]*(?:验证码|安全验证|访问受限|登录)", dom, re.I):
            raise FetchError("平台要求登录、验证或访问受限，已停止导入，请上传获权的本地素材。")
        images, title = _douyin_images(dom), _douyin_title(dom)
    if not images:
        raise FetchError("这个链接里没有图片：可能是视频作品，或链接已失效。")
    total = len(images)
    return platform, title, images[:MAX_IMAGES], url, total


def fetch(
    link: str,
    *,
    download_root: Path,
    profile: Path,
    browser: Path | None = None,
) -> FetchedNote:
    """Resolve the share link and save its images under ``download_root``."""
    platform, title, images, url, total = probe(link, profile=profile, browser=browser)
    folder = target_folder(download_root, platform, title)
    files = download(images, folder, platform)
    write_source_note(folder, platform, title, url, len(files))
    return FetchedNote(platform=platform, title=title, source_url=url, folder=folder, files=files,
                       total_images=total)


if __name__ == "__main__":  # manual check: python -m codex_image.webui.company_fetch <link> <root>
    link_arg = sys.argv[1] if len(sys.argv) > 1 else ""
    root = Path(sys.argv[2]) if len(sys.argv) > 2 else Path.cwd() / "下载图文"
    profile_dir = Path(os.environ.get("ILAB_PROFILE_DIR") or (Path.home() / "iLab-CONJURE-Data" / "browser-profile"))
    note = fetch(link_arg, download_root=root, profile=profile_dir)
    print(json.dumps({
        "platform": note.platform, "title": note.title, "folder": str(note.folder),
        "files": [path.name for path in note.files], "total": note.total_images,
    }, ensure_ascii=False))
