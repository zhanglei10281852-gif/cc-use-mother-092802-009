"""面向审查人员的只读 HTTP API（仅依赖标准库）。

路由：
    GET /projects/{project_id}/evidence   按项目查看证据附件摘要
    GET /projects/{project_id}/conflicts  按项目查看冲突关系
    GET /claims/{claim_id}/audit          查看申报的审计轨迹
    GET /batches/{batch_id}/snapshots     查看基金批次汇总的版本演进
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

from .errors import NotFoundError
from .services import ReviewService


def to_jsonable(obj):
    """把领域对象递归转换为可 JSON 序列化的结构。"""
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, frozenset, set)):
        return [to_jsonable(v) for v in obj]
    return obj


def make_handler_class(service: ReviewService):
    class ReviewHandler(BaseHTTPRequestHandler):
        server_version = "BiodiversityReview/1.0"

        def do_GET(self) -> None:  # noqa: N802（标准库命名）
            path = urlparse(self.path).path.strip("/")
            parts = path.split("/") if path else []
            try:
                if len(parts) == 3 and parts[0] == "projects" and parts[2] == "evidence":
                    payload = service.project_evidence(parts[1])
                elif len(parts) == 3 and parts[0] == "projects" and parts[2] == "conflicts":
                    payload = service.project_conflicts(parts[1])
                elif len(parts) == 3 and parts[0] == "claims" and parts[2] == "audit":
                    payload = service.claim_audit_trail(parts[1])
                elif len(parts) == 3 and parts[0] == "batches" and parts[2] == "snapshots":
                    payload = service.batch_snapshots(parts[1])
                else:
                    self._send({"error": "not found"}, status=404)
                    return
            except NotFoundError as exc:
                self._send({"error": str(exc)}, status=404)
                return
            self._send(payload)

        def _send(self, payload, status: int = 200) -> None:
            body = json.dumps(to_jsonable(payload), ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:  # 测试与嵌入时保持安静
            pass

    return ReviewHandler


def create_server(
    service: ReviewService, host: str = "127.0.0.1", port: int = 0
) -> HTTPServer:
    """创建审查 API 服务；port=0 时由系统分配空闲端口。"""
    return HTTPServer((host, port), make_handler_class(service))
