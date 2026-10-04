"""启动人员资质到期巡检 HTTP 服务。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qualification_inspection.api import create_server  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="人员资质到期巡检 HTTP 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--store", default="data/inspection.json", help="JSON 仓储文件")
    args = parser.parse_args()

    server = create_server(args.host, args.port, store=args.store)
    print(f"资质巡检服务已启动: http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
