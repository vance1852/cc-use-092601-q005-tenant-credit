"""无第三方依赖的多租户算力信用额度 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .errors import CreditError, ValidationFailed
from .service import CreditService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    def __init__(self, service: CreditService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b"") -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        query = parse_qs(parsed.query)
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = self._actor(normalized)
            service = self.service

            if method == "POST" and path == "/tenants":
                return Response(201, service.create_tenant(actor, payload["tenant_id"], payload["name"]))
            if method == "POST" and path == "/users":
                return Response(201, service.create_user(
                    actor, payload["user_id"], payload["display_name"], payload["role"],
                    payload.get("tenant_id")))
            if method == "POST" and path == "/pools":
                return Response(201, service.create_resource_pool(actor, payload))
            if method == "POST" and path == "/credits":
                return Response(201, service.grant_credit(actor, payload))
            if method == "POST" and path == "/rules":
                return Response(201, service.put_rule(
                    actor, payload.get("tenant_id"), payload.get("source_priority"),
                    bool(payload.get("allow_overage", False))))
            if method == "POST" and path == "/reservations":
                return Response(201, service.submit_reservation(actor, payload))
            if method == "GET" and len(parts) == 2 and parts[0] == "reservations":
                return Response(200, service.reservation(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "reservations" and parts[2] == "start":
                return Response(200, service.start_reservation(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "reservations" and parts[2] == "cancel":
                return Response(200, service.cancel_reservation(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "reservations" and parts[2] == "fail":
                return Response(200, service.fail_reservation(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "reservations" and parts[2] == "complete":
                return Response(200, service.complete_reservation(actor, parts[1]))
            if method == "GET" and path == "/reviews":
                include_all = query.get("scope", ["pending"])[0] == "all"
                return Response(200, service.list_reviews(actor, include_all=include_all))
            if method == "POST" and len(parts) == 3 and parts[0] == "reviews" and parts[2] == "decision":
                return Response(200, service.decide_review(
                    actor, int(parts[1]), bool(payload.get("approve", False)),
                    str(payload.get("note", ""))))
            if method == "POST" and path == "/periods/close":
                return Response(200, service.close_period(actor, payload["tenant_id"], payload["period_key"]))
            if method == "GET" and path == "/credits/summary":
                return Response(200, service.credit_summary(actor, query.get("tenant_id", [None])[0]))
            if method == "GET" and path == "/ledger":
                return Response(200, service.ledger(
                    actor, query.get("tenant_id", [None])[0],
                    query.get("reservation_id", [None])[0],
                    int(query.get("limit", ["100"])[0])))
            if method == "GET" and len(parts) == 3 and parts[0] == "ledger" and parts[1] == "entry":
                return Response(200, service.explain_ledger_entry(actor, int(parts[2])))
            if method == "GET" and path == "/audit/chain":
                return Response(200, service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except CreditError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CreditLedger/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动多租户算力信用额度服务")
    parser.add_argument("--database", type=Path, default=Path("credit_ledger.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(CreditService(connection))))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
