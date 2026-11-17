"""Loopback-only launcher using bundled Python and the existing personal configuration."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import socket


def main() -> int:
    parser = argparse.ArgumentParser(description="Local AI image content engine")
    parser.add_argument("--port", type=int, default=int(os.environ.get("IMAGE_ENGINE_PORT", "8787")))
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1..65535")
    if args.data_dir:
        os.environ["ILAB_CONJURE_DATA_DIR"] = str(args.data_dir.resolve())
    elif not os.environ.get("ILAB_CONJURE_DATA_DIR"):
        os.environ["ILAB_CONJURE_DATA_DIR"] = str(Path.home() / "复刻图文-Data")
    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", args.port))
    except OSError:
        print(f"Port {args.port} is occupied. Keep the other program running and select --port 8788,")
        print("or set IMAGE_ENGINE_PORT. No process was terminated.")
        return 3
    data = Path(os.environ["ILAB_CONJURE_DATA_DIR"])
    data.mkdir(parents=True, exist_ok=True)
    from codex_image.webui.atomic_files import atomic_write_text
    for filename, body in (
        ("webui-auth-settings.json", '{"source":"api"}'),
        ("webui-settings.json", '{"locale":"zh-CN","lan_access_enabled":false}'),
    ):
        path = data / filename
        if not path.exists():
            atomic_write_text(path, body, mode=0o600)
    print(f"AI image engine: http://127.0.0.1:{args.port}")
    print(f"Personal data: {data.resolve()}")
    from codex_image.webui.server import main as serve
    return serve(["portable_webui_app:app", "--host", "127.0.0.1", "--port", str(args.port)])


if __name__ == "__main__":
    raise SystemExit(main())
