"""基于标准库 http.server 的 REST API。

无需第三方依赖即可运行：

    python -m biodiversity_review.api --port 8080

审查人员身份通过 ``X-Actor`` 请求头传递（默认 secretariat）。
所有写操作都会在服务审计日志中留痕，可通过 ``GET /audit`` 检索。
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .audit import AuditLog
from .errors import ReviewError
from .service import ReviewService
from .storage import Store

DEFAULT_ACTOR = "secretariat"

# 真正“创建新资源”的 POST 动作返回 201；其余是状态流转/裁决动作，返回 200。
_CREATING_ACTIONS = {
    "create_cycle", "create_project", "revise_boundary", "record_milestone",
    "submit", "add_supplement", "expert_review", "compile_batch",
}


def create_service(snapshot: dict[str, Any] | None = None) -> ReviewService:
    store = Store()
    if snapshot is not None:
        store.import_state(snapshot)
    return ReviewService(store=store, audit=AuditLog())


# 路由：(method, compiled_regex) -> handler 名
# handler 在 _dispatch 中按名分发，避免模块级持有可变状态。
_ROUTES: list[tuple[str, re.Pattern[str], str]] = [
    ("POST", re.compile(r"^/cycles$"), "create_cycle"),
    ("GET", re.compile(r"^/cycles/(?P<country_code>[A-Za-z]{2,4})$"), "get_cycle"),

    ("POST", re.compile(r"^/projects$"), "create_project"),
    ("GET", re.compile(r"^/projects$"), "list_projects"),
    ("GET", re.compile(r"^/projects/(?P<project_id>[^/]+)$"), "get_project"),
    ("GET", re.compile(r"^/projects/(?P<project_id>[^/]+)/boundaries$"), "boundary_history"),
    ("POST", re.compile(r"^/projects/(?P<project_id>[^/]+)/boundary-revisions$"), "revise_boundary"),
    ("POST", re.compile(r"^/projects/(?P<project_id>[^/]+)/milestones$"), "record_milestone"),
    ("GET", re.compile(r"^/projects/(?P<project_id>[^/]+)/evidence-pack$"), "evidence_pack"),
    ("POST", re.compile(r"^/projects/(?P<project_id>[^/]+)/submissions$"), "submit"),

    ("GET", re.compile(r"^/submissions$"), "list_submissions"),
    ("GET", re.compile(r"^/submissions/(?P<submission_id>[^/]+)$"), "get_submission"),
    ("POST", re.compile(r"^/submissions/(?P<submission_id>[^/]+)/supplements$"), "add_supplement"),
    ("POST", re.compile(r"^/submissions/(?P<submission_id>[^/]+)/reviews$"), "expert_review"),
    ("POST", re.compile(r"^/submissions/(?P<submission_id>[^/]+)/approve$"), "approve"),
    ("POST", re.compile(r"^/submissions/(?P<submission_id>[^/]+)/reject$"), "reject"),
    ("POST", re.compile(r"^/submissions/(?P<submission_id>[^/]+)/withdraw$"), "withdraw"),
    ("POST", re.compile(r"^/submissions/(?P<submission_id>[^/]+)/claims/(?P<claim_id>[^/]+)/revoke$"),
     "revoke_claim"),

    ("GET", re.compile(r"^/conflicts$"), "list_conflicts"),
    ("POST", re.compile(r"^/conflicts/(?P<conflict_id>[^/]+)/resolution$"), "resolve_conflict"),

    ("POST", re.compile(r"^/batches/(?P<batch_id>[^/]+)/compile$"), "compile_batch"),
    ("GET", re.compile(r"^/batches/(?P<batch_id>[^/]+)$"), "get_batch"),
    ("GET", re.compile(r"^/batches/(?P<batch_id>[^/]+)/versions/(?P<version>\d+)$"), "get_batch_version"),
    ("GET", re.compile(r"^/batches/(?P<batch_id>[^/]+)/impacts$"), "list_batch_impacts"),

    ("GET", re.compile(r"^/audit$"), "audit"),
]


class ReviewRequestHandler(BaseHTTPRequestHandler):
    server_version = "BiodiversityReview/1.0"

    # 由 server 注入
    service: ReviewService

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静些；审计日志才是正式记录
        return

    # -- 工具 ----------------------------------------------------------

    def _actor(self) -> str:
        return self.headers.get("X-Actor", DEFAULT_ACTOR) or DEFAULT_ACTOR

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReviewError(f"请求体不是合法 JSON：{exc}")
        if not isinstance(body, dict):
            raise ReviewError("请求体必须是 JSON 对象")
        return body

    def _send_json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # -- 分发 ----------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parts = urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        query = parse_qs(parts.query)
        try:
            for route_method, pattern, action in _ROUTES:
                if route_method != method:
                    continue
                match = pattern.match(path)
                if match:
                    handler: Callable[..., Any] = getattr(self, f"_do_{action}")
                    result = handler(query=query, params=match.groupdict(), body=self._safe_body())
                    status = 201 if method == "POST" and action in _CREATING_ACTIONS else 200
                    self._send_json(status, result)
                    return
            self._send_json(404, {"error": "not_found", "message": f"无此路由：{method} {path}"})
        except ReviewError as exc:
            self._send_json(exc.http_status, {
                "error": type(exc).__name__, "message": str(exc),
            })
        except (KeyError, TypeError) as exc:
            self._send_json(422, {"error": "ValidationError", "message": f"参数缺失或类型错误：{exc}"})

    def _safe_body(self) -> dict[str, Any]:
        try:
            return self._read_json()
        except ReviewError:
            raise
        except Exception as exc:  # noqa: BLE001 - 归一化为 400
            raise ReviewError(f"无法解析请求体：{exc}") from exc

    # ==================================================================
    # 动作处理器
    # ==================================================================

    def _do_create_cycle(self, body: dict, **_: Any) -> Any:
        return self.service.register_cycle(
            country_code=body["country_code"],
            cycle_code=body["cycle_code"],
            name=body["name"],
            indicators=body.get("indicators", []),
            milestones=body.get("milestones", []),
            actor=self._actor(),
        )

    def _do_get_cycle(self, params: dict, **_: Any) -> Any:
        return self.service.get_cycle(params["country_code"])

    def _do_create_project(self, body: dict, **_: Any) -> Any:
        return self.service.register_project(
            project_id=body["project_id"],
            country_code=body["country_code"],
            site_codes=body["site_codes"],
            effective_from=body.get("effective_from"),
            actor=self._actor(),
        )

    def _do_list_projects(self, **_: Any) -> Any:
        return {"projects": self.service.list_projects()}

    def _do_get_project(self, params: dict, **_: Any) -> Any:
        return self.service.get_project(params["project_id"])

    def _do_boundary_history(self, params: dict, **_: Any) -> Any:
        return {"revisions": self.service.boundary_history(params["project_id"])}

    def _do_revise_boundary(self, params: dict, body: dict, **_: Any) -> Any:
        return self.service.revise_boundary(
            params["project_id"],
            site_codes=body["site_codes"],
            rationale=body["rationale"],
            effective_from=body.get("effective_from"),
            actor=self._actor(),
        )

    def _do_record_milestone(self, params: dict, body: dict, **_: Any) -> Any:
        return self.service.record_milestone(
            params["project_id"],
            milestone=body["milestone"],
            achieved_on=body.get("achieved_on"),
            note=body.get("note", ""),
            actor=self._actor(),
        )

    def _do_evidence_pack(self, params: dict, **_: Any) -> Any:
        return self.service.project_evidence_pack(params["project_id"])

    def _do_submit(self, params: dict, body: dict, **_: Any) -> Any:
        return self.service.submit(
            project_id=params["project_id"],
            submitted_by=body.get("submitted_by", self._actor()),
            claims=body["claims"],
            attachments=body.get("attachments", []),
            submission_id=body.get("submission_id"),
            milestone_code=body.get("milestone_code"),
            note=body.get("note", ""),
        )

    def _do_list_submissions(self, query: dict, **_: Any) -> Any:
        return {"submissions": self.service.list_submissions(
            project_id=query.get("project_id", [None])[0])}

    def _do_get_submission(self, params: dict, **_: Any) -> Any:
        return self.service.get_submission(params["submission_id"])

    def _do_add_supplement(self, params: dict, body: dict, **_: Any) -> Any:
        return self.service.add_supplement(
            params["submission_id"],
            submitted_by=body.get("submitted_by", self._actor()),
            note=body["note"],
            attachments=body.get("attachments", []),
        )

    def _do_expert_review(self, params: dict, body: dict, **_: Any) -> Any:
        return self.service.submit_expert_review(
            params["submission_id"],
            reviewer=body.get("reviewer", self._actor()),
            decision=body["decision"],
            note=body.get("note", ""),
            checked_attachment_ids=body.get("checked_attachment_ids", []),
        )

    def _do_approve(self, params: dict, **_: Any) -> Any:
        return self.service.approve_submission(params["submission_id"], actor=self._actor())

    def _do_reject(self, params: dict, body: dict, **_: Any) -> Any:
        return self.service.reject_submission(
            params["submission_id"], actor=self._actor(), reason=body.get("reason", ""))

    def _do_withdraw(self, params: dict, body: dict, **_: Any) -> Any:
        return self.service.withdraw_submission(
            params["submission_id"], actor=self._actor(), reason=body.get("reason", ""))

    def _do_revoke_claim(self, params: dict, body: dict, **_: Any) -> Any:
        return self.service.revoke_claim(
            params["submission_id"],
            params["claim_id"],
            actor=self._actor(),
            reason=body["reason"],
        )

    def _do_list_conflicts(self, query: dict, **_: Any) -> Any:
        return {"conflicts": self.service.list_conflicts(
            project_id=query.get("project_id", [None])[0],
            status=query.get("status", [None])[0],
        )}

    def _do_resolve_conflict(self, params: dict, body: dict, **_: Any) -> Any:
        return self.service.resolve_conflict(
            params["conflict_id"],
            actor=self._actor(),
            resolution_status=body["status"],
            rationale=body["rationale"],
        )

    def _do_compile_batch(self, params: dict, body: dict, **_: Any) -> Any:
        return self.service.compile_batch(
            params["batch_id"], actor=self._actor(), label=body.get("label", ""))

    def _do_get_batch(self, params: dict, **_: Any) -> Any:
        return self.service.get_batch(params["batch_id"])

    def _do_get_batch_version(self, params: dict, **_: Any) -> Any:
        return self.service.get_batch_version(params["batch_id"], int(params["version"]))

    def _do_list_batch_impacts(self, params: dict, **_: Any) -> Any:
        return {"impacts": self.service.list_batch_impacts(params["batch_id"])}

    def _do_audit(self, query: dict, **_: Any) -> Any:
        entity_type = query.get("entity_type", [None])[0]
        entity_id = query.get("entity_id", [None])[0]
        events = self.service.audit.all_events()
        if entity_type:
            events = [e for e in events if e.entity_type == entity_type]
        if entity_id:
            events = [e for e in events if e.entity_id == entity_id]
        return {"events": [e.to_dict() for e in events]}


def create_server(host: str = "127.0.0.1", port: int = 0, *,
                  service: ReviewService | None = None) -> ThreadingHTTPServer:
    service = service or create_service()

    class _Handler(ReviewRequestHandler):
        pass

    _Handler.service = service
    server = ThreadingHTTPServer((host, port), _Handler)
    server.service = service  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="生物多样性项目成果审查后端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)

    server = create_server(args.host, args.port)
    print(f"审查后端已启动：http://{args.host}:{server.server_address[1]}")  # noqa: T201
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
