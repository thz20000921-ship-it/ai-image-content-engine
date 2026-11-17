"""Explicit local UI/HTTP fixture service. No production credentials or external network."""
from pathlib import Path
import argparse
import socket
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine_fixture import make_app
import uvicorn


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args()
    original = socket.socket.connect

    def offline_connect(sock, address):
        if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1", "localhost"}:
            raise RuntimeError("Offline fixture rejects external network")
        return original(sock, address)

    socket.socket.connect = offline_connect
    app = make_app(args.data_dir)
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, proxy_headers=False)
