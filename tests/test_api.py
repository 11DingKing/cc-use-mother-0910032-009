"""HTTP 接口端到端测试（真实起服，标准库 urllib 调用）。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
from datetime import date
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qualification_inspection import FixedClock, InspectionService
from qualification_inspection.api import serve

D = date.fromisoformat


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = InspectionService(FixedClock(D("2026-10-05")))
        self.server = serve(self.service, port=0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def call(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = Request(
            self.base + "/" + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_full_flow_over_http(self) -> None:
        # 已过期资质登记。
        _, qual = self.call("POST", "qualifications", {
            "id": "Q1", "person_id": "P1", "qual_type": "执业证",
            "valid_from": "2025-01-01", "valid_to": "2026-10-04",
        })
        _, auth = self.call("POST", "authorizations", {
            "id": "A1", "qualification_id": "Q1", "service_code": "S001",
            "service_name": "普通门诊", "granted_from": "2025-01-01",
        })

        # 巡检：失效，并将停业限制对账到授权（自到期次日生效）。
        _, run = self.call("POST", "inspections/run", {})
        self.assertEqual(run["risk_opened"], 1)
        _, risks = self.call("GET", "risks")
        self.assertEqual(risks[0]["category"], "失效")

        _, auth = self.call("GET", "authorizations/A1")
        self.assertFalse(auth["can_practice_today"])
        # 授权视图中每项限制都说明何时生效。
        self.assertEqual(auth["restrictions"][0]["effective_from"], "2026-10-05")
        self.assertIn("生效日 2026-10-05",
                      auth["restrictions"][0]["effect_explanation"])

        # 提交续期并发放宽限临时凭证，再巡检：失效转宽限。
        self.call("POST", "renewals", {
            "id": "C1", "qualification_id": "Q1",
            "required_documents": ["执业证原件"],
            "submitted_documents": ["执业证原件"],
        })
        self.call("POST", "permits", {
            "id": "P1", "qualification_id": "Q1", "renewal_case_id": "C1",
            "valid_from": "2026-10-05", "valid_to": "2026-11-04",
        })
        self.call("POST", "inspections/run", {})
        _, risks = self.call("GET", "risks")
        self.assertEqual(risks[0]["category"], "宽限")

        # 宽限窗口内可排班；窗口外被接口拒绝（400）。
        status, _ = self.call("POST", "schedules", {
            "id": "S1", "authorization_id": "A1", "scheduled_on": "2026-10-20",
        })
        self.assertEqual(status, 200)
        status, body = self.call("POST", "schedules", {
            "authorization_id": "A1", "scheduled_on": "2026-11-10",
        })
        self.assertEqual(status, 400)
        self.assertIn("失效", body["error"])

        # 撤回续期：风险回到失效。
        self.call("POST", "renewals/C1/withdraw", {})
        self.call("POST", "inspections/run", {})
        _, risks = self.call("GET", "risks")
        self.assertEqual(risks[0]["category"], "失效")

        # 历史服务依据：补录失效前的服务，可证明当日合法。
        _, record = self.call("POST", "service-records", {
            "authorization_id": "A1", "service_date": "2026-10-03",
            "service_name": "普通门诊",
        })
        _, basis = self.call("GET", f"service-records/{record['id']}/basis")
        self.assertTrue(basis["practice_was_lawful"])

        # 幂等：同日重复巡检无变化。
        _, run = self.call("POST", "inspections/run", {})
        self.assertEqual((run["risk_opened"], run["risk_resolved"]), (0, 0))

    def test_unknown_route_and_entity(self) -> None:
        status, body = self.call("GET", "nope")
        self.assertEqual(status, 404)
        status, body = self.call("GET", "authorizations/NOPE")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
