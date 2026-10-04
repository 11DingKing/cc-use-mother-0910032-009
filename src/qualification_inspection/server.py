"""人员资质到期巡检 HTTP 服务启动入口。

用法：

    python3 -m qualification_inspection.server [--host 127.0.0.1] [--port 8080]

默认使用系统时钟；演示/回归可用 ``--today YYYY-MM-DD`` 注入固定时钟。
服务为内存仓储，进程重启数据清空；生产部署应替换为持久化仓储。
"""
from __future__ import annotations

import argparse
from datetime import date

from .api import serve
from .policy import FixedClock
from .service import InspectionService


def main() -> None:
    parser = argparse.ArgumentParser(description="人员资质到期巡检 HTTP 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--today",
        help="注入固定日期（YYYY-MM-DD），用于回放/演示；缺省用系统时钟",
    )
    parser.add_argument(
        "--grace-days", type=int, default=30,
        help="到期后续期已提交待核验的管理宽限天数（默认 30）",
    )
    args = parser.parse_args()

    clock = FixedClock(date.fromisoformat(args.today)) if args.today else None
    service = InspectionService(clock, grace_days=args.grace_days)
    server = serve(service, host=args.host, port=args.port)
    print(
        f"巡检服务已启动：http://{args.host}:{args.port} "
        f"（今日={service.clock.today().isoformat()}）"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")


if __name__ == "__main__":
    main()
