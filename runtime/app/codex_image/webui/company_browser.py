"""Minimal Chrome DevTools Protocol driver for the note-fetching page workflow.

Xiaohongshu refuses to render its note page for a headless browser, so the note is
opened in an ordinary window parked far outside the visible desktop and read through
CDP.  The standard library is enough: the portable runtime ships no websocket client
and this project does not install packages.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
STARTUP_SECONDS = 25.0


class DriverError(RuntimeError):
    """Raised when the browser cannot be driven."""


class WebSocket:
    def __init__(self, url: str, timeout: float = 60.0) -> None:
        if not url.startswith("ws://"):
            raise DriverError(f"unexpected websocket url: {url}")
        host_port, _, path = url[5:].partition("/")
        host, _, port = host_port.partition(":")
        self._sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        self._sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET /{path} HTTP/1.1\r\nHost: {host_port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(request.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += self._sock.recv(4096)
        if b"101" not in head.split(b"\r\n")[0]:
            raise DriverError("websocket upgrade refused")

    def send(self, text: str) -> None:
        data = text.encode()
        size = len(data)
        header = bytearray([0x81])
        if size < 126:
            header.append(0x80 | size)
        elif size < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", size)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", size)
        mask = os.urandom(4)
        header += mask
        self._sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, size: int) -> bytes:
        buffer = b""
        while len(buffer) < size:
            chunk = self._sock.recv(size - len(buffer))
            if not chunk:
                raise DriverError("websocket closed")
            buffer += chunk
        return buffer

    def recv(self) -> str:
        payload = b""
        while True:
            first, second = self._read(2)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            payload += self._read(length)
            if first & 0x80:
                return payload.decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


class Session:
    """One page target of a running browser."""

    def __init__(self, port: int, timeout: float = 90.0) -> None:
        self.port = port
        self.timeout = timeout
        self._socket = WebSocket(self._page_socket_url(port), timeout)
        self._id = 0

    @staticmethod
    def _page_socket_url(port: int) -> str:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=10) as response:
            targets = json.load(response)
        pages = [target for target in targets if target.get("type") == "page"]
        if not pages:
            raise DriverError("浏览器没有可用的页面")
        return pages[0]["webSocketDebuggerUrl"]

    def call(self, method: str, timeout: float | None = None, **params: Any) -> dict[str, Any]:
        self._id += 1
        message_id = self._id
        self._socket.send(json.dumps({"id": message_id, "method": method, "params": params}))
        limit = timeout if timeout is not None else self.timeout
        deadline = time.time() + limit
        while time.time() < deadline:
            message = json.loads(self._socket.recv())
            if message.get("id") == message_id:
                return message
        raise DriverError(f"{method} 超时")

    def evaluate(self, expression: str, timeout: float | None = None) -> Any:
        result = self.call("Runtime.evaluate", timeout, expression=expression, returnByValue=True)
        return ((result.get("result") or {}).get("result") or {}).get("value")

    def wait_loaded(self, seconds: float = 30.0) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                if self.evaluate("document.readyState") == "complete":
                    return
            except DriverError:
                pass
            time.sleep(0.5)

    def navigate(self, url: str, settle_seconds: float = 6.0) -> None:
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call("Page.navigate", url=url)
        self.wait_loaded()
        time.sleep(settle_seconds)

    def close(self) -> None:
        self._socket.close()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def launch(browser: Path, profile: Path, *, open_url: str = "about:blank", visible: bool = True) -> subprocess.Popen[bytes]:
    """Start the browser with a debugging port and return the process handle."""
    profile.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    command = [
        str(browser),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if not visible:
        command += ["--headless=new", f"--user-agent={CHROME_UA}"]
    else:
        # Park the window outside the desktop: the page renders as a normal browsing
        # session, while nobody has to watch a window appear on screen.
        command += ["--window-position=-32000,-32000", "--window-size=1280,900", "--new-window"]
    command.append(open_url)
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + STARTUP_SECONDS
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2):
                return process
        except Exception:  # noqa: BLE001 - the port is simply not open yet
            if process.poll() is not None:
                raise DriverError("浏览器启动失败，可能被其他窗口占用。请关闭后重试。")
            time.sleep(0.4)
    process.terminate()
    raise DriverError("浏览器启动超时，请重试。")


def port_of(process: subprocess.Popen[bytes]) -> int:
    for argument in process.args if isinstance(process.args, list) else []:
        if isinstance(argument, str) and argument.startswith("--remote-debugging-port="):
            return int(argument.split("=", 1)[1])
    raise DriverError("没有找到调试端口")


NOTE_QUERY = """
(() => {
  const state = window.__INITIAL_STATE__ || {};
  const map = (state.note && state.note.noteDetailMap) || {};
  const visibleRestriction = [...document.querySelectorAll(
    '.login-container, .login-mask, [class*="captcha"], [id*="captcha"], [class*="verify-wrap"]'
  )].some(node => {
    const box = node.getBoundingClientRect();
    const style = getComputedStyle(node);
    return box.width > 0 && box.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
  });
  const out = {url: location.href, title: "", images: [],
    access_restricted: visibleRestriction || /验证码|安全验证|访问受限/.test(document.title) || /\\/(?:login|captcha|verify)(?:\\/|\\?|$)/.test(location.pathname)};
  if (out.access_restricted) return JSON.stringify(out);
  for (const key in map) {
    const note = (map[key] || {}).note || {};
    const list = note.imageList || [];
    if (!list.length) continue;
    out.title = note.title || "";
    out.images = list.map((item) => ({
      url: item.urlDefault || item.url || "",
      width: item.width || 0,
      height: item.height || 0,
    }));
    break;
  }
  return JSON.stringify(out);
})()
"""


def capture_note(url: str, *, browser: Path, profile: Path, settle_seconds: float = 8.0) -> dict[str, Any]:
    """Render ``url`` in a parked window and return its note title and image list.

    Only the note is serialised: the page's whole ``__INITIAL_STATE__`` holds
    circular references and cannot be stringified.
    """
    process = launch(browser, profile, visible=True)
    session = None
    try:
        session = Session(port_of(process))
        session.navigate(url, settle_seconds=settle_seconds)
        raw = session.evaluate(NOTE_QUERY)
        if not isinstance(raw, str):
            return {"url": str(session.evaluate("location.href") or ""), "title": "", "images": []}
        try:
            return json.loads(raw)
        except ValueError:
            return {"url": "", "title": "", "images": []}
    finally:
        if session is not None:
            session.close()
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
