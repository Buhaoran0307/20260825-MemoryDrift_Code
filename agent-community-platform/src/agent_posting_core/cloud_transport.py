"""Cloud HTTP transport retaining bounded, redacted failure evidence."""
from __future__ import annotations
import asyncio
import io
import json
import socket
import ssl
import time
import threading
from dataclasses import dataclass, field
from http.client import HTTPSConnection
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, HTTPSHandler, build_opener, urlopen
from .llm_diagnostics import http_failure_detail, redact
from .mem0_platform_v3 import Mem0PlatformHTTPResponse, _read_bounded

@dataclass(frozen=True)
class CloudHTTPResponse(Mem0PlatformHTTPResponse):
    connection_attempts: list = field(default_factory=list)

class CloudTransportError(RuntimeError):

    def __init__(self, message, diagnostics):
        super().__init__(message)
        self.diagnostics = diagnostics

def response_detail(status, body, headers, secrets):
    if isinstance(body, (dict, list)):
        body = json.dumps(body, ensure_ascii=False).encode('utf-8')
    elif isinstance(body, str):
        body = body.encode('utf-8')
    stream = io.BytesIO(body)
    return http_failure_detail(SimpleNamespace(code=status, headers=headers, read=stream.read, close=stream.close), secrets=secrets)

class CloudHTTPTransport:

    async def request(self, method, url, *, headers, body, timeout):
        return await asyncio.to_thread(self._request, method, url, headers, body, timeout)

    @staticmethod
    def _request(method, url, headers, body, timeout):
        key = headers.get('Authorization', '').removeprefix('Bearer ')
        secrets = [key]
        if body:
            try:
                payload = json.loads(body)
                if isinstance(payload, dict) and isinstance(payload.get('content'), str):
                    secrets.append(payload['content'])
            except (ValueError, UnicodeError):
                pass
        attempts = []
        request = Request(url, data=body, headers=headers, method=method)
        try:
            open_request = urlopen
            with open_request(request, timeout=timeout) as r:
                response = CloudHTTPResponse(r.status, _read_bounded(r), dict(r.headers.items()), attempts)
                return response
        except HTTPError as exc:
            detail = http_failure_detail(exc, secrets=tuple(secrets))
            detail['connection_attempts'] = attempts
            raise CloudTransportError(f'HTTP {exc.code}', detail) from None
        except Exception as exc:
            detail = {'status_code': None, 'error_type': type(exc).__name__, 'error_message': redact(str(exc), tuple(secrets))[:1024], 'connection_attempts': attempts}
            raise CloudTransportError(f"{type(exc).__name__}: {detail['error_message']}", detail) from None
