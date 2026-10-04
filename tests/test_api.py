"""HTTP 接口端到端测试。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qualification_inspection.api import create_server
from qualification_inspection.clock import FixedClock


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FixedClock(date(2026, 6, 10))
        self.server = create_server("127.0.0.1", 0, clock=self.clock)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def call(self, method: str, path: str, body: dict | None = None,
             query: dict | None = None) -> tuple[int, object]:
        url = f"http://127.0.0.1:{self.port}{path}"
        if query:
            from urllib.parse import urlencode
            url += "?" + urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_scenario_flow(self) -> None:
        status, _ = self.call("POST", "/persons", {"person_id": "P1", "name": "张三"})
        self.assertEqual(status, 200)
        status, _ = self.call("POST", "/qualifications", {
            "qual_id": "Q1", "person_id": "P1", "qual_type": "PRACTICE_LICENSE",
            "qual_number": "L-1", "issued_on": "2025-07-01",
            "expiry_on": "2026-06-30"})
        self.assertEqual(status, 200)
        status, _ = self.call("POST", "/authorizations", {
            "authorization_id": "A1", "person_id": "P1",
            "qual_id": "Q1", "scope": "门诊出诊"})
        self.assertEqual(status, 200)

        # 重复人员 → 409
        status, payload = self.call("POST", "/persons",
                                    {"person_id": "P1", "name": "张三"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "DOMAIN_ERROR")

        # 缺字段 → 400
        status, payload = self.call("POST", "/renewals", {"case_id": "C1"})
        self.assertEqual(status, 400)

        # 巡检 → 临期
        status, run = self.call("POST", "/inspections/run", {"period": "2026-06"})
        self.assertEqual(status, 200)
        self.assertEqual(run["created"], 1)

        status, risks = self.call("GET", "/risks", query={"person_id": "P1"})
        self.assertEqual(status, 200)
        self.assertEqual(risks[0]["category"], "EXPIRING_SOON")

        # 续期 → 宽限
        self.clock.advance_to(date(2026, 7, 5))
        self.assertEqual(self.call("POST", "/renewals",
                                   {"case_id": "C1", "qual_id": "Q1"})[0], 200)
        status, schedule = self.call("GET", "/schedule",
                                     query={"person_id": "P1", "day": "2026-07-05"})
        self.assertEqual(status, 200)
        self.assertTrue(schedule["allowed"])
        r = schedule["authorizations"][0]["restrictions"][0]
        self.assertEqual(r["type"], "GRACE_WINDOW")
        # 接口说明限制何时生效/何时可能硬化
        self.assertEqual(r["effective_from"], "2026-07-01")
        self.assertIsNotNone(r["effective_to"])

        # 撤回 → 失效，未来排班阻断
        self.assertEqual(self.call("POST", "/renewals/C1/withdraw",
                                   {"on": "2026-07-10"})[0], 200)
        status, impact = self.call("GET", "/schedule-impact",
                                   query={"person_id": "P1"})
        self.assertEqual(status, 200)
        windows = impact["windows"]
        self.assertEqual(windows[0]["type"], "LAPSED")
        self.assertEqual(windows[0]["effective_from"], "2026-07-11")

    def test_suspension_endpoints(self) -> None:
        self.call("POST", "/persons", {"person_id": "P2", "name": "李四"})
        self.call("POST", "/qualifications", {
            "qual_id": "Q2", "person_id": "P2", "qual_type": "PRACTICE_LICENSE",
            "qual_number": "L-2", "issued_on": "2025-01-01",
            "expiry_on": "2027-01-01"})
        self.call("POST", "/authorizations", {
            "authorization_id": "A2", "person_id": "P2",
            "qual_id": "Q2", "scope": "门诊出诊"})
        status, restriction = self.call("POST", "/suspensions", {
            "order_no": "S-9", "person_id": "P2", "reason": "调查",
            "start_on": "2026-06-01"})
        self.assertEqual(status, 200)
        self.assertEqual(restriction["effective_from"], "2026-06-01")
        status, schedule = self.call("GET", "/schedule",
                                     query={"person_id": "P2", "day": "2026-06-10"})
        self.assertFalse(schedule["allowed"])
        self.assertEqual(self.call("POST", "/suspensions/S-9/lift",
                                   {"lifted_on": "2026-06-20"})[0], 200)
        status, schedule = self.call("GET", "/schedule",
                                     query={"person_id": "P2", "day": "2026-06-20"})
        self.assertTrue(schedule["allowed"])

    def test_unknown_route_404(self) -> None:
        status, _ = self.call("GET", "/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
