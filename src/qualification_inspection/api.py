"""HTTP 接口（标准库实现，无第三方依赖）。

每个写接口在成功时返回 200 与实体/结果 JSON；业务冲突返回 409，
参数错误返回 400，不存在返回 404。响应中的每项限制都带
``effective_from``（含，当日生效）与可选 ``effective_to``（含，当日为最后有效日）。
"""
from __future__ import annotations

import json
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .clock import Clock
from .models import RenewalCase
from .repository import JsonRepository, Repository
from .service import DomainError, QualificationService


def _json_default(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"不可序列化: {type(value)!r}")


# ── 请求 DTO → 服务方法 的声明式路由表 ──────────────────────────────────
# (方法, 路径模板, 服务方法名, 必填字段)
ROUTES: list[tuple[str, str, str, tuple[str, ...]]] = [
    ("POST", "/persons", "register_person", ("person_id", "name")),
    ("POST", "/qualifications", "register_qualification",
     ("qual_id", "person_id", "qual_type", "qual_number", "issued_on", "expiry_on")),
    ("POST", "/authorizations", "register_authorization",
     ("authorization_id", "person_id", "qual_id", "scope")),
    ("POST", "/authorizations/{id}/revoke", "revoke_authorization", ()),
    ("POST", "/renewals", "submit_renewal", ("case_id", "qual_id")),
    ("POST", "/renewals/{id}/supplement-request", "request_supplement",
     ("items", "deadline")),
    ("POST", "/renewals/{id}/supplement", "submit_supplement", ("doc_names",)),
    ("POST", "/renewals/{id}/approve", "approve_renewal", ("new_expiry_on",)),
    ("POST", "/renewals/{id}/reject", "reject_renewal", ("reason",)),
    ("POST", "/renewals/{id}/withdraw", "withdraw_renewal", ()),
    ("POST", "/credentials", "issue_credential",
     ("cred_id", "case_id", "valid_from", "valid_to")),
    ("POST", "/credentials/{id}/void", "void_credential", ("reason",)),
    ("POST", "/suspensions", "suspend_person",
     ("order_no", "person_id", "reason", "start_on")),
    ("POST", "/suspensions/{id}/lift", "lift_suspension", ()),
    ("POST", "/service-records", "record_service",
     ("record_id", "person_id", "authorization_id", "service_on")),
    ("POST", "/inspections/run", "run_monthly_inspection", ()),
]


class _Handler(BaseHTTPRequestHandler):
    server_version = "QualInspection/0.1"

    @property
    def service(self) -> QualificationService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音
        return

    # ── GET ─────────────────────────────────────────────────────────────
    def do_GET(self) -> None:
        with self.server._service_lock:  # type: ignore[attr-defined]
            self._handle_get()

    def _handle_get(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            if path == "/risks":
                self._ok(self.service.list_risks(
                    person_id=query.get("person_id"),
                    include_cleared=query.get("include_cleared") == "true",
                ))
            elif path == "/restrictions":
                self._ok(self.service.list_restrictions(
                    person_id=query.get("person_id"),
                    active_on=query.get("on"),
                ))
            elif path == "/schedule":
                self._require(query, ("person_id", "day"))
                self._ok(self.service.evaluate_schedule(query["person_id"], query["day"]))
            elif path == "/schedule-impact":
                self._require(query, ("person_id",))
                self._ok(self.service.future_schedule_impact(
                    query["person_id"],
                    from_on=query.get("from_on"),
                    through=query.get("through"),
                ))
            elif path == "/persons":
                self._ok([p.to_dict() for p in self.service.repo.persons.all()])
            elif path == "/qualifications":
                self._ok([q.to_dict() for q in self.service.repo.qualifications.all()])
            elif path == "/health":
                self._ok({"status": "ok", "today": self.service.today.isoformat()})
            else:
                self._err(404, "NOT_FOUND", f"无此路径: {path}")
        except DomainError as exc:
            self._err(409, "DOMAIN_ERROR", str(exc))
        except (KeyError, ValueError) as exc:
            self._err(400, "BAD_REQUEST", str(exc))

    # ── POST ────────────────────────────────────────────────────────────
    def do_POST(self) -> None:
        with self.server._service_lock:  # type: ignore[attr-defined]
            self._handle_post()

    def _handle_post(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            body = self._read_body()
            for _method, template, func_name, required in ROUTES:
                matched, ident = self._match(template, path)
                if not matched:
                    continue
                self._require(body, required)
                func = getattr(self.service, func_name)
                result = self._call(func_name, func, ident, body)
                if isinstance(result, RenewalCase):
                    payload = result.view()
                elif hasattr(result, "to_dict"):
                    payload = result.to_dict()
                else:
                    payload = result
                self._ok(payload)
                return
            self._err(404, "NOT_FOUND", f"无此路径: {path}")
        except DomainError as exc:
            self._err(409, "DOMAIN_ERROR", str(exc))
        except (KeyError, ValueError, TypeError) as exc:
            self._err(400, "BAD_REQUEST", str(exc))

    def _call(self, func_name: str, func: Callable[..., Any],
              ident: str | None, body: dict) -> Any:
        """按服务方法名分派；``ident`` 为路径中的资源标识。"""
        b = body
        if func_name == "register_person":
            return func(b["person_id"], b["name"])
        if func_name == "register_qualification":
            return func(b["qual_id"], b["person_id"], b["qual_type"],
                        b["qual_number"], b["issued_on"], b["expiry_on"])
        if func_name == "register_authorization":
            return func(b["authorization_id"], b["person_id"],
                        b["qual_id"], b["scope"], b.get("granted_on"))
        if func_name == "revoke_authorization":
            return func(ident, b.get("on"))
        if func_name == "submit_renewal":
            return func(b["case_id"], b["qual_id"], b.get("submitted_on"))
        if func_name == "request_supplement":
            return func(ident, b["items"], b["deadline"], b.get("requested_on"))
        if func_name == "submit_supplement":
            return func(ident, b["doc_names"], b.get("submitted_on"))
        if func_name == "approve_renewal":
            return func(ident, b["new_expiry_on"], b.get("decided_on"))
        if func_name == "reject_renewal":
            return func(ident, b["reason"], b.get("decided_on"))
        if func_name == "withdraw_renewal":
            return func(ident, b.get("on"))
        if func_name == "issue_credential":
            return func(b["cred_id"], b["case_id"], b["valid_from"],
                        b["valid_to"], b.get("issued_on"))
        if func_name == "void_credential":
            return func(ident, b["reason"], b.get("on"))
        if func_name == "suspend_person":
            return func(b["order_no"], b["person_id"], b["reason"],
                        b["start_on"], b.get("end_on"), b.get("scope"))
        if func_name == "lift_suspension":
            return func(ident, b.get("lifted_on"))
        if func_name == "record_service":
            return func(b["record_id"], b["person_id"],
                        b["authorization_id"], b["service_on"],
                        b.get("detail", ""))
        if func_name == "run_monthly_inspection":
            return func(b.get("period"))
        raise TypeError(f"未映射的服务方法: {func_name}")

    # ── 工具 ────────────────────────────────────────────────────────────
    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"请求体不是合法 JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    @staticmethod
    def _match(template: str, path: str) -> tuple[bool, str | None]:
        t_parts = [p for p in template.strip("/").split("/") if p]
        p_parts = [p for p in path.strip("/").split("/") if p]
        if len(t_parts) != len(p_parts):
            return False, None
        ident: str | None = None
        for t, p in zip(t_parts, p_parts):
            if t == "{id}":
                ident = p
            elif t != p:
                return False, None
        return True, ident

    @staticmethod
    def _require(body: dict[str, Any], fields: tuple[str, ...]) -> None:
        missing = [f for f in fields if f not in body]
        if missing:
            raise KeyError("缺少必填字段: " + "、".join(missing))

    def _ok(self, payload: Any) -> None:
        self._send(200, json.dumps(payload, ensure_ascii=False, default=_json_default))

    def _err(self, status: int, code: str, message: str) -> None:
        self._send(status, json.dumps({"error": code, "message": message},
                                      ensure_ascii=False))

    def _send(self, status: int, payload: str) -> None:
        data = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def create_server(
    host: str = "127.0.0.1",
    port: int = 8080,
    store: str | Repository | None = None,
    clock: Clock | None = None,
) -> ThreadingHTTPServer:
    """构造 HTTP 服务。``store`` 为文件路径时使用 JSON 持久化。"""
    repo = store if isinstance(store, Repository) else (
        JsonRepository(store) if isinstance(store, str) else Repository()
    )
    server = ThreadingHTTPServer((host, port), _Handler)
    server.service = QualificationService(repo, clock=clock)  # type: ignore[attr-defined]
    # 命令均在方法内部完成保存；加锁保证并发命令下的仓储一致性
    server._service_lock = threading.RLock()  # type: ignore[attr-defined]
    return server


def main() -> None:  # pragma: no cover
    import argparse

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


if __name__ == "__main__":  # pragma: no cover
    main()
