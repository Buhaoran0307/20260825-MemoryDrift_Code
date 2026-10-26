"""MemOS Cloud: one ADD per event, completion polling and native readback.

Cloud API contracts: https://memos-docs.openmem.net/cn/api_docs/core/add_message/
and the get_memory, search_memory and message/get_status pages. Ingestion is
sequential. A lost ADD acknowledgement is never retried automatically.
"""
from __future__ import annotations

import json
from copy import deepcopy
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .cloud_memory import CloudHTTPError, CloudMemory, CloudMemoryError, digest, now
from .cloud_transport import CloudHTTPResponse, CloudHTTPTransport, CloudTransportError
from .llm_diagnostics import redact
from .mem0_platform_v3 import _read_bounded
from .memory_read_router import classify_cloud_failure


MEMOS_DETAIL_LISTS = (
    "memory_detail_list", "preference_detail_list", "event_detail_list",
    "profile_detail_list", "tool_memory_detail_list", "skill_detail_list",
)
MEMOS_MEMORY_VIEWS = ("detail_factual", "preference", "event", "profile", "tool_memory", "skill")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class MemosHTTPTransport(CloudHTTPTransport):
    @staticmethod
    def _request(method, url, headers, body, timeout):
        # Do not replay mutations or forward Token credentials across redirects.
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
                return CloudHTTPResponse(response.status, _read_bounded(response), dict(response.headers.items()))
        except HTTPError as exc:
            try:
                return CloudHTTPResponse(exc.code, _read_bounded(exc), dict(exc.headers.items()))
            finally:
                exc.close()
        except Exception as exc:
            secret = headers.get("Authorization", "").removeprefix("Token ")
            detail = {"status_code": None, "error_type": type(exc).__name__,
                      "error_message": redact(str(exc), (secret,))[:1024]}
            raise CloudTransportError(type(exc).__name__, detail) from None


def memos_memory_rows(data):
    """Preserve native categories and IDs when adapting six response lists."""
    rows = []
    fields = dict(zip(MEMOS_DETAIL_LISTS, (
        "memory_value", "preference", "event_value", "memory", "tool_value", "skill_value",
    )))
    for category in MEMOS_DETAIL_LISTS:
        native_rows = data.get(category)
        if not isinstance(native_rows, list) or not all(isinstance(row, dict) for row in native_rows):
            raise CloudMemoryError(f"MemOS response has invalid {category}")
        for native in native_rows:
            text = native.get(fields[category])
            if category == "profile_detail_list" and text is None:
                text = native.get("properties")
            if isinstance(text, (dict, list)):
                text = json.dumps(text, ensure_ascii=False, sort_keys=True)
            if not isinstance(text, str) or not text.strip():
                raise CloudMemoryError(f"MemOS {category} has no memory text")
            native_id = native.get("id")
            if not isinstance(native_id, str) or not native_id:
                raise CloudMemoryError(f"MemOS {category} has no memory ID")
            rows.append({**deepcopy(native), "id": category + ":" + native_id,
                         "native_id": native_id, "native_category": category,
                         "memory": text, "created_at": native.get("create_time") or native.get("created_at") or ""})
    return rows


class MemosCloudMemory(CloudMemory):
    _text_field = "memory"
    _authorization_scheme = "Token"

    def __init__(self, **kwargs):
        runtime = dict(kwargs.pop("runtime", None) or {})
        revision = runtime.pop("credential_revision", None)
        if revision is not None and (type(revision) is not int or revision < 1):
            raise ValueError("MemOS credential_revision must be a positive integer")
        runtime.setdefault("transport", MemosHTTPTransport())
        super().__init__(strategy="memos_cloud", runtime=runtime, **kwargs)
        if self._physical_scope and len(self._physical_scope) > 100:
            raise ValueError("MemOS physical user_id cannot exceed 100 characters")
        self._baseline_snapshot = None
        self._provider_snapshot = None
        self._source_account_id = self._account_id
        self._source_credential_revision = revision

    def _sanitize(self, value):
        # Only remove the runtime authentication credential here. Generic key
        # heuristics would alter legitimate experimental memory about api_key.
        if isinstance(value, str):
            return value.replace(self._api_key, "[REDACTED]") if self._api_key else value
        if isinstance(value, dict):
            return {self._sanitize(key): self._sanitize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._sanitize(item) for item in value]
        return deepcopy(value)

    def _record_request(self, detail):
        super()._record_request(self._sanitize({**detail, "account_id": self._account_id}))

    async def _request(self, *args, **kwargs):
        try:
            return self._sanitize(await super()._request(*args, **kwargs))
        except CloudMemoryError as exc:
            exc.diagnostics = self._sanitize(exc.diagnostics)
            if hasattr(exc, "cloud_failure"):
                exc.cloud_failure = self._sanitize(exc.cloud_failure)
            raise

    def _snapshot_options(self):
        return {**super()._snapshot_options(),
                "baseline_snapshot": deepcopy(self._baseline_snapshot),
                "provider_snapshot": deepcopy(self._provider_snapshot),
                "source_account_id": self._source_account_id,
                "source_credential_revision": self._source_credential_revision}

    def restore(self, snapshot):
        super().restore(snapshot)
        source_account = snapshot.options["source_account_id"]
        source_revision = snapshot.options["source_credential_revision"]
        # Offline source validation constructs an adapter without credentials.
        # Only an authenticated ingestion runtime can change remote state.
        if (not self._read_only and (self._api_key or self._account_id is not None)
                and (source_account != self._source_account_id
                     or source_revision != self._source_credential_revision)):
            raise ValueError("MemOS ingestion resume requires the original default account and credential revision")
        self._source_account_id, self._source_credential_revision = source_account, source_revision
        self._baseline_snapshot = deepcopy(snapshot.options["baseline_snapshot"])
        self._provider_snapshot = deepcopy(snapshot.options["provider_snapshot"])
        if self._remote_scope_id not in {None, self._scope()}:
            raise ValueError("MemOS snapshot remote scope does not match its logical scope")
        if self._ingestion_status == "ready" and self._records:
            if (not isinstance(self._provider_snapshot, dict)
                    or self._provider_snapshot.get("complete") is not True
                    or self._provider_snapshot.get("user_id") != self._scope()
                    or any(not r.get("readback", {}).get("complete")
                           or r.get("task_status") != "completed"
                           or r.get("readback", {}).get("user_id") != self._scope()
                           for r in self._records.values())):
                raise ValueError("MemOS ready snapshot requires completed native readback")

    def _user_id(self):
        return self._physical_scope or self._scope()

    async def _api(self, path, payload, *, read=False, allow_missing=False):
        try:
            response = await self._request("POST", path, payload, read=read)
        except CloudHTTPError as exc:
            # MemOS also returns its missing-user business code with HTTP 500.
            # Only the initial empty-scope probe may accept that response.
            failure = getattr(exc, "cloud_failure", {})
            if not (allow_missing and read and path == "/get/memory" and exc.status_code == 500
                    and failure.get("category") == "scope_missing"
                    and failure.get("provider_error_code") == "40103"):
                raise
            try:
                response = json.loads(exc.diagnostics.get("response_body", ""))
            except (ValueError, TypeError):
                raise exc
            if (not isinstance(response, dict) or response.get("code") != 40103
                    or response.get("message") != "User does not exist"):
                raise exc
        if (allow_missing and isinstance(response, dict) and response.get("code") == 40103
                and response.get("message") == "User does not exist"):
            return response
        if (not isinstance(response, dict) or type(response.get("code")) is not int
                or response["code"] != 0 or not isinstance(response.get("data"), dict)):
            failure = classify_cloud_failure(self._last_request_diagnostics.get("status_code"),
                                             response, provider="memos_cloud")
            if failure["category"] == "completed":
                failure["category"] = "provider_error"
            detail = {**self._last_request_diagnostics, "api_response": response,
                      "error_type": "MemOSAPIError", "cloud_failure": failure}
            self._record_request(detail)
            error = CloudMemoryError("MemOS API failed; inspect request audit", self._sanitize(detail))
            error.cloud_failure = failure
            raise error
        return response

    async def _readback(self, *, baseline=False):
        pages, merged, seen = [], {name: [] for name in MEMOS_DETAIL_LISTS}, set()
        expected_pages = expected_total = None
        for page in range(1, 10001):
            response = await self._api("/get/memory", {
                "user_id": self._user_id(), "page": page, "size": 50,
            }, read=True, allow_missing=baseline and page == 1)
            pages.append(response)
            if response.get("code") == 40103:
                return {"user_id": self._user_id(), "exported_at": now(), "complete": True,
                        "empty_scope_evidence": "api_40103_user_does_not_exist",
                        "pages": pages, "data": merged, "counts": {name: 0 for name in merged}}
            data = response["data"]
            total_pages, total, size = data.get("pages"), data.get("total"), data.get("size")
            if (type(total_pages) is not int or not 0 <= total_pages <= 10000
                    or type(total) is not int or total < 0
                    or type(size) is not int or not 0 <= size <= 50
                    or total > 0 and (size < 1 or total_pages != (total + size - 1) // size)
                    or data.get("current") != page and not (page == 1 and total == 0 and data.get("current") == 0)
                    or (expected_pages is not None and (total_pages != expected_pages or total != expected_total))):
                raise CloudMemoryError("MemOS readback pagination is invalid or changed during export")
            expected_pages, expected_total = total_pages, total
            for category in MEMOS_DETAIL_LISTS:
                rows = data.get(category)
                if (not isinstance(rows, list) or len(rows) > size
                        or not all(isinstance(row, dict) for row in rows)):
                    raise CloudMemoryError("MemOS readback must contain all six memory lists")
                for row in rows:
                    if not isinstance(row.get("id"), str) or not row["id"]:
                        raise CloudMemoryError("MemOS readback memory ID is missing")
                    key = (category, row["id"])
                    if key in seen:
                        raise CloudMemoryError("MemOS readback contains duplicate memory IDs")
                    seen.add(key)
                merged[category].extend(deepcopy(rows))
            if page >= total_pages:
                if max(map(len, merged.values()), default=0) != total:
                    raise CloudMemoryError("MemOS readback count does not match provider total")
                return {"user_id": self._user_id(), "exported_at": now(), "complete": True,
                        "pages": pages, "data": merged,
                        "counts": {name: len(rows) for name, rows in merged.items()}}
        raise CloudMemoryError("MemOS readback pagination limit exceeded")

    async def _ensure_scope(self):
        self._baseline_snapshot = await self._readback(baseline=True)
        if any(self._baseline_snapshot["counts"].values()):
            raise CloudMemoryError("MemOS experiment scope is already populated; refusing to mix memory")
        self._remote_scope_id = self._scope()

    async def observe(self, events):
        if self._read_only:
            raise CloudMemoryError("second-stage memory cannot ingest events")
        self._load_journal()
        # Resume accepted tasks before submitting anything else. Failed or
        # ambiguous events stop here and cannot cause a duplicate ADD.
        if self.pending_update_event_ids:
            await self.finalize_ingestion()
        for event in events:
            await super().observe([event])
            await self.finalize_ingestion()

    async def finalize_ingestion(self):
        if self._read_only:
            raise CloudMemoryError("second-stage cannot finalize ingestion")
        self._load_journal()
        if self._ingestion_status == "ready" and not self.pending_update_event_ids:
            return
        await super().finalize_ingestion()

    async def _submit(self, event, record):
        payload = {"user_id": self._scope(), "conversation_id": self._scope(),
                   "messages": [{"role": "assistant", "content": event.content}],
                   "allow_public": False, "async_mode": True}
        record.update(request=self._sanitize(payload), request_sha256=digest(payload), status_history=[])
        self._persist()
        response = await self._api("/add/message", payload)
        record["response"] = response
        data = response["data"]
        if (data.get("success") is not True or not isinstance(data.get("task_id"), str)
                or not data["task_id"] or data.get("status") not in {"running", "completed", "failed"}):
            raise CloudMemoryError("MemOS ADD did not return a usable task receipt")
        record.update(status="accepted", remote_id=data["task_id"], task_id=data["task_id"],
                      task_status=data["status"], status_history=[{"at": now(), "source": "add", "response": response}])
        self._persist()

    async def _poll_ready(self):
        ready = True
        for event_id, record in self._records.items():
            if record["status"] == "ready":
                continue
            self._current_event_id = event_id
            try:
                response = await self._api("/get/status", {"task_id": record["task_id"]}, read=True)
            except Exception as exc:
                record["status_history"].append({"at": now(), "source": "get_status",
                    "read_error": self._sanitize(getattr(exc, "diagnostics", {})),
                    "error_type": type(exc).__name__})
                self._persist()
                raise
            data = response["data"]
            record["status_history"].append({"at": now(), "source": "get_status", "response": response})
            status = data.get("status")
            if data.get("task_id") != record["task_id"] or status not in {"running", "completed", "failed"}:
                self._persist()
                raise CloudMemoryError("MemOS task status or task ID is invalid")
            record["task_status"] = status
            if status == "failed":
                record["status"] = "failed"
                self._persist()
                raise CloudMemoryError("MemOS task processing failed; inspect checkpoint")
            ready = ready and status == "completed"
            self._persist()
        if ready:
            self._provider_snapshot = await self._readback()
            for record in self._records.values():
                if record["status"] != "ready":
                    record["readback"] = {**deepcopy(self._provider_snapshot),
                        "snapshot_sha256": digest(self._provider_snapshot)}
            self._persist()
        return ready

    async def _search(self, query):
        payload = {"user_id": self._user_id(), "query": query,
                   "memory_limit_number": self.retrieval_top_k, "relativity": self.search_threshold,
                   "include_memory_view": list(MEMOS_MEMORY_VIEWS)}
        response = await self._api("/search/memory", payload, read=True)
        rows = memos_memory_rows(response["data"])
        self._query_details = {"response": response, "native_categories": list(MEMOS_DETAIL_LISTS),
                               "merge_order": "native_category_then_provider_order"}
        return rows[:self.retrieval_top_k], payload
