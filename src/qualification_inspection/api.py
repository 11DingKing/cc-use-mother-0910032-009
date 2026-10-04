"""HTTP 接口层（标准库实现，无第三方依赖）。

接口围绕 :class:`InspectionService` 暴露 JSON API；服务实例（连同注入
时钟）在启动时装配，便于测试与按日回放。所有日期使用 ISO ``YYYY-MM-DD``。

限制何时生效统一在授权视图 ``GET /authorizations/{id}`` 的
``restrictions[].effect_explanation`` 字段说明：

* ``effective_from``（含当日）起生效；
* ``effective_to``（含当日）为最后一个有效日；为 ``null`` 表示解除事件
  登记前持续有效；
* ``scheduling_frozen`` 仅冻结新排班（含未来候选排班），不影响当日及
  历史服务；``practice_suspended`` 停止一切执业并取消覆盖期内未来排班，
  历史服务记录保留。
"""
from __future__ import annotations

import json
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlparse

from .errors import ComplianceError, NotFoundError
from .service import InspectionService


def _iso(value):
    return value.isoformat() if isinstance(value, date) else value


def _risk_dict(item) -> dict:
    return {
        "id": item.id,
        "qualification_id": item.qualification_id,
        "category": item.category.value,
        "reason": item.reason,
        "renewal_case_id": item.renewal_case_id,
        "permit_id": item.permit_id,
        "supplement_due": _iso(item.supplement_due),
        "grace_deadline": _iso(item.grace_deadline),
        "created_on": _iso(item.created_on),
        "resolved_on": _iso(item.resolved_on),
    }


def _case_dict(case) -> dict:
    return {
        "id": case.id,
        "qualification_id": case.qualification_id,
        "status": case.status.value,
        "submitted_at": _iso(case.submitted_at),
        "documents": [{"name": d.name, "present": d.present} for d in case.documents],
        "missing_documents": case.missing_documents(),
        "supplement_due": _iso(case.supplement_due),
        "supplement_requested_at": _iso(case.supplement_requested_at),
        "verified_at": _iso(case.verified_at),
        "decided_valid_to": _iso(case.decided_valid_to),
        "withdrawn_at": _iso(case.withdrawn_at),
        "reject_reason": case.reject_reason,
    }


def _schedule_dict(entry) -> dict:
    return {
        "id": entry.id,
        "authorization_id": entry.authorization_id,
        "person_id": entry.person_id,
        "service_code": entry.service_code,
        "scheduled_on": _iso(entry.scheduled_on),
        "status": entry.status,
        "cancel_reason": entry.cancel_reason,
        "cancelled_at": _iso(entry.cancelled_at),
    }


def _permit_dict(permit) -> dict:
    return {
        "id": permit.id,
        "qualification_id": permit.qualification_id,
        "renewal_case_id": permit.renewal_case_id,
        "valid_from": _iso(permit.valid_from),
        "valid_to": _iso(permit.valid_to),
        "scope_note": permit.scope_note,
    }


def _suspension_dict(s) -> dict:
    return {
        "id": s.id,
        "qualification_id": s.qualification_id,
        "effective_from": _iso(s.effective_from),
        "reason": s.reason,
        "lifted_at": _iso(s.lifted_at),
        "lift_effective_date": _iso(s.lift_effective_date),
    }


class _Router:
    """最小方法+路径路由，路径参数以 ``{name}`` 占位。"""

    def __init__(self) -> None:
        self.routes: list[tuple[str, list[str], Callable]] = []

    def add(self, method: str, pattern: str, handler: Callable) -> None:
        self.routes.append((method, pattern.strip("/").split("/"), handler))

    def match(self, method: str, path: str):
        parts = path.strip("/").split("/")
        for route_method, pattern_parts, handler in self.routes:
            if route_method != method or len(pattern_parts) != len(parts):
                continue
            params: dict[str, str] = {}
            for p, actual in zip(pattern_parts, parts):
                if p.startswith("{") and p.endswith("}"):
                    params[p[1:-1]] = actual
                elif p != actual:
                    break
            else:
                return handler, params
        return None, None


def build_router(service: InspectionService) -> _Router:
    router = _Router()
    r = router.add

    def qualification_view(qual) -> dict:
        return {
            "id": qual.id,
            "person_id": qual.person_id,
            "qual_type": qual.qual_type,
            "valid_from": _iso(qual.valid_from),
            "valid_to": _iso(qual.valid_to),
            "new_valid_to": _iso(qual.new_valid_to),
            "status": qual.status.value,
        }

    r("POST", "qualifications", lambda b, p, q: qualification_view(
        service.register_qualification(
            b["person_id"], b["qual_type"], date.fromisoformat(b["valid_from"]),
            date.fromisoformat(b["valid_to"]), b.get("id"),
        )))

    r("POST", "authorizations", lambda b, p, q: service.authorization_view(
        service.issue_authorization(
            b["qualification_id"], b["service_code"], b["service_name"],
            date.fromisoformat(b["granted_from"]),
            date.fromisoformat(b["granted_to"]) if b.get("granted_to") else None,
            b.get("id"),
        ).id))

    r("GET", "authorizations/{id}", lambda b, p, q: service.authorization_view(p["id"]))

    r("POST", "renewals", lambda b, p, q: _case_dict(
        service.submit_renewal(
            b["qualification_id"], b["required_documents"],
            b.get("submitted_documents"), b.get("id"),
        )))

    r("POST", "renewals/{id}/supplement-request", lambda b, p, q: _case_dict(
        service.request_supplement(p["id"], date.fromisoformat(b["due"]))))

    r("POST", "renewals/{id}/supplement-documents", lambda b, p, q: _case_dict(
        service.submit_supplement_documents(p["id"], b["document_names"])))

    r("POST", "renewals/{id}/verify", lambda b, p, q: _case_dict(
        service.verify_renewal(p["id"], date.fromisoformat(b["new_valid_to"]))))

    r("POST", "renewals/{id}/reject", lambda b, p, q: _case_dict(
        service.reject_renewal(p["id"], b["reason"])))

    r("POST", "renewals/{id}/withdraw", lambda b, p, q: _case_dict(
        service.withdraw_renewal(p["id"])))

    r("POST", "permits", lambda b, p, q: _permit_dict(
        service.issue_permit(
            b["qualification_id"], b["renewal_case_id"],
            date.fromisoformat(b["valid_from"]), date.fromisoformat(b["valid_to"]),
            b.get("id"), b.get("scope_note", "宽限期临时执业"),
        )))

    r("POST", "suspensions", lambda b, p, q: _suspension_dict(
        service.suspend_by_regulator(
            b["qualification_id"], b["reason"],
            date.fromisoformat(b["effective_from"]) if b.get("effective_from") else None,
            b.get("id"),
        )))

    r("POST", "suspensions/{id}/lift", lambda b, p, q: _suspension_dict(
        service.lift_suspension(
            p["id"],
            date.fromisoformat(b["effective_date"]) if b.get("effective_date") else None,
        )))

    r("POST", "schedules", lambda b, p, q: _schedule_dict(
        service.add_schedule(
            b["authorization_id"], date.fromisoformat(b["scheduled_on"]), b.get("id"),
        )))

    r("GET", "schedules", lambda b, p, q: [
        _schedule_dict(s)
        for s in sorted(service.schedules.values(), key=lambda x: (x.scheduled_on, x.id))
    ])

    r("POST", "service-records", lambda b, p, q: {
        "id": (rec := service.add_service_record(
            b["authorization_id"], date.fromisoformat(b["service_date"]),
            b["service_name"],
        )).id,
        "authorization_id": rec.authorization_id,
        "service_date": _iso(rec.service_date),
        "service_name": rec.service_name,
    })

    def record_basis(body, params, query):
        result = service.service_record_basis(params["id"])
        result["service_date"] = _iso(result["service_date"])
        return result

    r("GET", "service-records/{id}/basis", record_basis)

    def run_inspection(body, params, query):
        as_of = date.fromisoformat(body["as_of"]) if body.get("as_of") else None
        run = service.run_inspection(as_of)
        return {**run, "as_of": _iso(run["as_of"])}

    r("POST", "inspections/run", run_inspection)

    def list_risks(body, params, query):
        qid = query.get("qualification_id", [None])[0]
        return [_risk_dict(i) for i in service.open_risk_items(qid)]

    r("GET", "risks", list_risks)

    r("GET", "clock", lambda b, p, q: {"today": _iso(service.clock.today())})

    return router


def create_handler(service: InspectionService) -> type[BaseHTTPRequestHandler]:
    router = build_router(service)

    class _Handler(BaseHTTPRequestHandler):
        def _send_json(self, status: int, payload) -> None:
            raw = json.dumps(payload, ensure_ascii=False, default=_iso).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if length == 0:
                return {}
            try:
                value = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                raise ComplianceError("请求体不是合法 JSON")
            if not isinstance(value, dict):
                raise ComplianceError("请求体必须是 JSON 对象")
            return value

        def _handle(self, method: str) -> None:
            parsed = urlparse(self.path)
            handler, params = router.match(method, parsed.path)
            if handler is None:
                self._send_json(404, {"error": f"无此接口：{method} {parsed.path}"})
                return
            try:
                body = self._read_body() if method == "POST" else {}
                query = parse_qs(parsed.query)
                result = handler(body, params or {}, query)
                self._send_json(200, result)
            except ComplianceError as exc:
                self._send_json(400, {"error": str(exc)})
            except NotFoundError as exc:
                self._send_json(404, {"error": str(exc)})
            except (KeyError, TypeError, ValueError) as exc:
                self._send_json(400, {"error": f"参数错误：{exc}"})

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def log_message(self, fmt, *args) -> None:
            return

    return _Handler


def serve(
    service: InspectionService, host: str = "127.0.0.1", port: int = 8080
) -> HTTPServer:
    """创建并返回 HTTP 服务（调用方负责 ``serve_forever``，便于测试）。"""
    return HTTPServer((host, port), create_handler(service))
