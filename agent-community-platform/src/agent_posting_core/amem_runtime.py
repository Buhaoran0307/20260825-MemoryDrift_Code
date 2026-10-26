"""Lazy, isolated runtime around the unmodified pinned A-Mem source."""
from __future__ import annotations

import asyncio
import builtins
import copy
import hashlib
import importlib.util
import json
import logging
import threading
import types
import uuid
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from .amem_official import (
    AMEM_COMMIT, AMEM_EMBEDDING_MODEL, AMemOfficialDependencyError,
    AMemOfficialError, AMemOfficialResponseError, note_id,
    AMemOfficialTransportError,
)
from .llm import LLMRequest
from .memory_attribution_probe import probe_receipt
from .llm_diagnostics import LLMHTTPError

_MODEL_LOCK = threading.Lock()
_LOGGER = logging.getLogger(__name__)
_RESPONSE_ATTEMPTS = 3
_SOURCE_HASHES = {
    "memory_system.py": "fa58c3ed405e37d298a3d11e5cd69459bc9709004461c0e01fc0c497c77c949b",
    "retrievers.py": "a4cd9645b9df0774f7f67bdfff3cf4d1a41911b4bac3843208db2019b57e311d",
    "llm_controller.py": "44e7d228326fe78dd8ecb8c8176364511547f5fef7cd7d58e433e883caeae0bb",
}


@lru_cache(maxsize=1)
def _upstream_code() -> Any:
    try:
        spec = importlib.util.find_spec("agentic_memory")
        if spec is None or not spec.submodule_search_locations:
            raise ImportError("agentic_memory is not installed")
        root = Path(next(iter(spec.submodule_search_locations)))
        for filename, expected in _SOURCE_HASHES.items():
            source = (root / filename).read_text(encoding="utf-8")
            if hashlib.sha256(source.encode()).hexdigest() != expected:
                raise AMemOfficialDependencyError(
                    f"A-Mem source differs from pinned commit {AMEM_COMMIT}: {filename}"
                )
        return compile((root / "memory_system.py").read_text(encoding="utf-8"),
                       str(root / "memory_system.py"), "exec")
    except (ImportError, OSError) as exc:
        raise AMemOfficialDependencyError(
            "A-Mem runtime is missing; install requirements.txt in the platform interpreter"
        ) from exc


class _PlatformController:
    def __init__(self, llm: Any, agent_id: str, loop: Any,
                 response_validation_max_attempts: int = _RESPONSE_ATTEMPTS,
                 response_tolerance_enabled: bool = True) -> None:
        if type(response_tolerance_enabled) is not bool:
            raise ValueError("response_tolerance_enabled must be a boolean")
        self.llm_client, self.agent_id, self.loop = llm, agent_id, loop
        self.response_validation_max_attempts = response_validation_max_attempts
        self.response_tolerance_enabled = response_tolerance_enabled
        self.failure: Exception | None = None
        self.neighbor_ids: list[str] = []
        self.neighbor_states: dict[str, dict[str, Any]] = {}
        self.source_event_id: str | None = None

    def get_completion(self, prompt: str, response_format: dict[str, Any],
                       temperature: float = 0.7) -> str:
        attempts: list[dict[str, Any]] = []
        attempt = 0
        try:
            from jsonschema import ValidationError, validate

            if self.llm_client is None:
                raise AMemOfficialError("A-Mem write requires a platform LLM")
            # Use the same contract on the wire and locally. Upstream extraction
            # omits required/strict even though downstream needs all its fields.
            output_format = copy.deepcopy(response_format)
            schema = output_format["json_schema"]["schema"]
            schema.setdefault("required", list(schema["properties"]))
            schema.setdefault("additionalProperties", False)
            output_format["json_schema"]["strict"] = True
            stage = "evolution" if "should_evolve" in schema["properties"] else "analysis"
            wire_schema = schema
            if stage == "evolution" and self.response_tolerance_enabled:
                prompt, wire_schema = self._evolution_contract(prompt)
                output_format["json_schema"]["schema"] = wire_schema
            # Include the contract in text for compatible endpoints that do not
            # enforce response_format. Source notes retain their original text.
            messages = [
                {"role": "system", "content": (
                    "Respond with only a JSON object matching this JSON Schema. "
                    "Include every required field with its exact type.\n"
                    + json.dumps(wire_schema, ensure_ascii=False)
                )},
                {"role": "user", "content": prompt},
            ]
            retry_messages: list[dict[str, str]] = []
            for attempt in range(1, self.response_validation_max_attempts + 1):
                # Leave temperature/token settings and transport retries to the
                # platform. Only invalid returned content is retried here.
                request = LLMRequest(
                    messages=copy.deepcopy(messages + retry_messages),
                    tool_choice=None, purpose="memory", agent_id=self.agent_id,
                    response_format=copy.deepcopy(output_format),
                )
                probe_receipt(self.llm_client, "call_context", request=request,
                              stage=stage, source_event_id=self.source_event_id,
                              attempt=attempt)
                response = asyncio.run_coroutine_threadsafe(
                    self.llm_client.complete(request), self.loop,
                ).result()
                content = response.content.strip()
                lines = content.splitlines()
                if (len(lines) >= 3 and lines[0].lower() in {"```json", "```"}
                        and lines[-1] == "```"):
                    content = "\n".join(lines[1:-1]).strip()
                validation_schema = schema
                conversion = None
                try:
                    parsed = json.loads(content)
                    if stage == "evolution" and self.response_tolerance_enabled:
                        if isinstance(parsed, dict) and "neighbor_updates" in parsed:
                            validation_schema = wire_schema
                            parsed = self._normalize_keep_fields(parsed)
                            validate(parsed, wire_schema)
                            parsed, conversion = self._convert_neighbor_updates(parsed)
                        else:
                            # Historical model responses remain usable, but never
                            # repair identifiers or missing neighbors by guessing.
                            validate(parsed, schema)
                            parsed, conversion = self._convert_legacy_references(parsed)
                        validation_schema = schema
                    validate(parsed, schema)
                    self._validate_semantics(parsed)
                except (ValueError, ValidationError) as exc:
                    detail = {"type": type(exc).__name__, "message": str(exc)}
                    if isinstance(exc, ValidationError):
                        detail.update(message=exc.message, path=list(exc.absolute_path),
                                      schema_path=list(exc.absolute_schema_path),
                                      validator=exc.validator)
                    message = f"A-Mem {stage} response invalid after {attempt} attempt(s)"
                    error = AMemOfficialResponseError(
                        message, request=request, response=response, parsed_content=content,
                        parse_error=exc if isinstance(exc, json.JSONDecodeError) else None,
                    )
                    error.diagnostics.update(
                        strategy="a_mem_official", stage=stage,
                        source_event_id=self.source_event_id,
                        validation_error=detail,
                        response_schema=copy.deepcopy(validation_schema),
                        attempt=attempt, max_attempts=self.response_validation_max_attempts,
                    )
                    if conversion is not None:
                        error.diagnostics["response_conversion"] = conversion
                    attempts.append(copy.deepcopy(error.diagnostics))
                    if attempt == self.response_validation_max_attempts:
                        error.diagnostics["attempts"] = attempts
                        raise error from exc
                    self._log_response("amem_response_retry", stage, attempt, detail)
                    retry_messages = [
                        {"role": "assistant", "content": response.content},
                        {"role": "user", "content": (
                            "The previous response failed validation: "
                            + json.dumps(detail, ensure_ascii=False)
                            + ". Regenerate the complete JSON object for the original task. "
                            "Preserve the source meaning; follow the schema and include all fields. "
                            + self._repair_guidance(stage)
                            + "Return no Markdown or explanation."
                        )},
                    ]
                    continue
                probe_receipt(self.llm_client, "validated", request=request)
                if attempts:
                    self._log_response("amem_response_recovered", stage, attempt,
                                       conversion=conversion)
                if conversion is not None:
                    self._log_response("amem_response_converted", stage, attempt,
                                       conversion=conversion)
                    return json.dumps(parsed, ensure_ascii=False)
                return content
            raise AssertionError("A-Mem response retry loop ended without a result")
        except AMemOfficialResponseError as exc:
            self.failure = exc
            raise
        except LLMHTTPError as exc:
            diagnostics = copy.deepcopy(exc.diagnostics)
            diagnostics.update(strategy="a_mem_official", stage=stage,
                               source_event_id=self.source_event_id)
            if attempts:
                diagnostics.update(
                    response_validation_attempts=copy.deepcopy(attempts),
                    response_validation_attempt=attempt,
                    response_validation_max_attempts=self.response_validation_max_attempts,
                )
            self.failure = AMemOfficialTransportError(
                "A-Mem model request failed (HTTP transport; see memory_provider_response)",
                diagnostics,
            )
            raise self.failure from exc
        except Exception as exc:
            # Upstream catches Exception and prints it. Remember the failure and
            # emit only a safe message; the caller rolls back after SDK returns.
            self.failure = AMemOfficialError(f"A-Mem model request failed ({type(exc).__name__})")
            if attempts:
                self.failure.diagnostics = {
                    "strategy": "a_mem_official", "stage": stage,
                    "source_event_id": self.source_event_id,
                    "error_type": type(exc).__name__,
                    "response_validation_attempts": copy.deepcopy(attempts),
                    "response_validation_attempt": attempt,
                    "response_validation_max_attempts": self.response_validation_max_attempts,
                }
            raise self.failure from exc

    def _evolution_contract(self, prompt: str) -> tuple[str, dict[str, Any]]:
        references = [f"n{index}" for index in range(len(self.neighbor_ids))]
        reference_schema: dict[str, Any] = {"type": "string"}
        if references:
            reference_schema["enum"] = references
        # The installed SDK still consumes its original arrays. This request
        # protocol makes coverage explicit before conversion to those arrays.
        properties = {
            "should_evolve": {"type": "boolean"},
            "actions": {"type": "array", "uniqueItems": True, "items": {
                "type": "string", "enum": ["strengthen", "update_neighbor"]}},
            "suggested_connections": {"type": "array", "uniqueItems": True,
                                      "maxItems": len(references),
                                      "items": copy.deepcopy(reference_schema)},
            "tags_to_update": {"type": "array", "items": {"type": "string"}},
            "neighbor_updates": {
                "type": "array", "minItems": len(references), "maxItems": len(references),
                "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "neighbor_ref": copy.deepcopy(reference_schema),
                        "mode": {"type": "string", "enum": ["keep", "update"]},
                        "context": {"type": ["string", "null"]},
                        "tags": {"type": ["array", "null"], "items": {"type": "string"}},
                    },
                    "required": ["neighbor_ref", "mode", "context", "tags"],
                },
            },
        }
        schema = {"type": "object", "properties": properties,
                  "required": list(properties), "additionalProperties": False}
        # Replace only the evolution instructions after the original note and
        # retrieved neighbors, avoiding contradictory legacy array instructions.
        task_start = prompt.rfind("\n                                Based on this information, determine:")
        if task_start >= 0:
            prompt = prompt[:task_start]
        prompt += (
            "\nAnalyze the new memory's keywords and context together with its nearest "
            "neighbors. Decide whether it should evolve based on those relationships, "
            "using the strengthen and/or update_neighbor actions. For strengthen, select "
            "the neighbor connections in suggested_connections. tags_to_update contains "
            "the updated tags for the new memory, determined by its content characteristics "
            "so it can be retrieved and categorized later. For update_neighbor, update "
            "neighbor context and tags based on your understanding of those memories; "
            "preserve a neighbor's original context and tags when no change is needed. "
            "Use the short neighbor references from the following mapping instead of "
            "copying the actual memory IDs printed in the input memory index labels. "
            "suggested_connections selects connections; it does not select which neighbors "
            "must appear in neighbor_updates. Return every input neighbor exactly once in "
            "neighbor_updates, including neighbors not selected for a connection. "
            "For each neighbor use {\"neighbor_ref\": \"n0\", \"mode\": \"keep\", "
            "\"context\": null, \"tags\": null} to "
            "preserve its existing context and tags without copying them. For an intentional "
            "update use mode=update and supply both context (a nonempty string) and tags "
            "(an array of strings). Updates require should_evolve=true and the update_neighbor "
            "action. If no neighbor needs changing, explicitly keep each one. "
            f"Required neighbor references in input-neighbor order: {json.dumps(references)}. "
            "Neighbor reference mapping (short reference to actual input memory ID): "
            + json.dumps(dict(zip(references, self.neighbor_ids)))
            + ". "
            "Return only the JSON object specified by the system schema."
        )
        return prompt, schema

    @staticmethod
    def _normalize_keep_fields(parsed: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(parsed)
        entries = result.get("neighbor_updates")
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and entry.get("mode") == "keep":
                    # A declared keep already specifies the original value.
                    # Normalize its omitted optional fields, never missing notes
                    # or missing fields for an intentional update.
                    entry.setdefault("context", None)
                    entry.setdefault("tags", None)
        return result

    def _convert_neighbor_updates(self, parsed: dict[str, Any]) -> tuple[dict[str, Any], dict]:
        references = {f"n{index}": identifier for index, identifier in enumerate(self.neighbor_ids)}
        entries = parsed["neighbor_updates"]
        by_reference: dict[str, dict[str, Any]] = {}
        for entry in entries:
            reference = entry["neighbor_ref"]
            if reference in by_reference:
                raise ValueError(f"Duplicate neighbor_ref {reference}; each neighbor must appear once")
            by_reference[reference] = entry
        missing = sorted(set(references) - set(by_reference))
        unknown = sorted(set(by_reference) - set(references))
        if missing or unknown:
            raise ValueError(f"Neighbor coverage mismatch: missing={missing}, unknown={unknown}")
        contexts, tags = [], []
        kept = updated = 0
        for reference, identifier in references.items():
            entry = by_reference[reference]
            if entry["mode"] == "keep":
                state = self.neighbor_states.get(identifier)
                if state is None:
                    raise ValueError(f"Cannot keep {reference}: original neighbor state is unavailable")
                if any(entry[name] is not None and entry[name] != state[name]
                       for name in ("context", "tags")):
                    raise ValueError(f"Keep entry {reference} conflicts with its original context or tags")
                contexts.append(state["context"])
                tags.append(copy.deepcopy(state["tags"]))
                kept += 1
            else:
                if not parsed["should_evolve"] or "update_neighbor" not in parsed["actions"]:
                    raise ValueError(f"Update entry {reference} requires should_evolve and update_neighbor")
                if (not isinstance(entry.get("context"), str) or not entry["context"].strip()
                        or not isinstance(entry.get("tags"), list)):
                    raise ValueError(f"Update entry {reference} requires nonempty context and tags array")
                contexts.append(entry["context"])
                tags.append(copy.deepcopy(entry["tags"]))
                updated += 1
        result = {name: copy.deepcopy(parsed[name]) for name in (
            "should_evolve", "actions", "suggested_connections", "tags_to_update")}
        result["suggested_connections"] = [references[ref] for ref in parsed["suggested_connections"]]
        result.update(new_context_neighborhood=contexts, new_tags_neighborhood=tags)
        return result, {"protocol": "neighbor_updates", "neighbors": len(references),
                        "kept": kept, "updated": updated}

    def _convert_legacy_references(self, parsed: dict[str, Any]) -> tuple[dict[str, Any], dict]:
        references = {f"n{index}": identifier for index, identifier in enumerate(self.neighbor_ids)}
        result = copy.deepcopy(parsed)
        connections = result["suggested_connections"]
        result["suggested_connections"] = [references.get(ref, ref) for ref in connections]
        if len(set(result["suggested_connections"])) != len(result["suggested_connections"]):
            raise ValueError("Evolution connections contain duplicate neighbor references after mapping")
        unknown = set(result["suggested_connections"]) - set(self.neighbor_ids)
        if unknown:
            raise ValueError("Evolution connections contain unknown neighbor references; "
                             f"use only {list(references)} or exact retrieved memory IDs")
        return result, {"protocol": "legacy", "neighbors": len(references),
                        "short_references_mapped": sum(ref in references for ref in connections)}

    def _repair_guidance(self, stage: str) -> str:
        if stage != "evolution":
            return ""
        if self.response_tolerance_enabled:
            references = [f"n{index}" for index in range(len(self.neighbor_ids))]
            return (
                f"Use only these neighbor references: {json.dumps(references)}. "
                "Return neighbor_updates with exactly one entry for every listed reference, "
                "even those absent from suggested_connections. For unchanged neighbors use "
                "mode=keep with context=null and tags=null; for mode=update supply both fields. "
                "Never invent, shorten, or approximate a memory ID. "
            )
        return (f"Both neighborhood arrays must each contain exactly {len(self.neighbor_ids)} "
                "entries in input-neighbor order, including unchanged neighbors. "
                "Use exact retrieved memory IDs for connections. ")

    def _validate_semantics(self, parsed: dict[str, Any]) -> None:
        if "context" in parsed and not parsed["context"].strip():
            raise ValueError("Empty note context")
        if parsed.get("should_evolve"):
            if not parsed["actions"] or set(parsed["actions"]) - {"strengthen", "update_neighbor"}:
                raise ValueError("Actions must include strengthen or update_neighbor only")
            if set(parsed["suggested_connections"]) - set(self.neighbor_ids):
                raise ValueError("Evolution connections must reference retrieved neighbors")
            if "update_neighbor" in parsed["actions"]:
                if len(parsed["new_context_neighborhood"]) != len(self.neighbor_ids) or len(
                    parsed["new_tags_neighborhood"]
                ) != len(self.neighbor_ids):
                    raise ValueError(
                        "Both neighborhood arrays must have one entry per retrieved neighbor "
                        f"in the same order (expected {len(self.neighbor_ids)} entries each)"
                    )
                if not all(context.strip() for context in parsed["new_context_neighborhood"]):
                    raise ValueError("Empty neighbor context")

    def _log_response(self, event: str, stage: str, attempt: int,
                      detail: dict[str, Any] | None = None,
                      conversion: dict[str, Any] | None = None) -> None:
        # Keep raw output in structured diagnostics, never in the worker log.
        record = {"event": event, "agent_id": self.agent_id, "stage": stage,
                  "source_event_id": self.source_event_id, "attempt": attempt}
        if detail is not None:
            record.update(error_type=detail["type"], path=detail.get("path"),
                          validator=detail.get("validator"))
        if conversion is not None:
            record["conversion"] = copy.deepcopy(conversion)
        _LOGGER.warning(json.dumps(record, ensure_ascii=False))


def _metadata(note: dict[str, Any]) -> dict[str, str]:
    return {key: json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict))
            else str(value) for key, value in note.items()}


def _related_memory_text(identifier: str, metadata: dict[str, Any]) -> str:
    """Match the pinned SDK's format without replacing text inside note fields."""
    return (
        f"memory index:{identifier}\ttalk start time:{metadata.get('timestamp', '')}"
        f"\tmemory content: {metadata.get('content', '')}"
        f"\tmemory context: {metadata.get('context', '')}"
        f"\tmemory keywords: {str(metadata.get('keywords', []))}"
        f"\tmemory tags: {str(metadata.get('tags', []))}\n"
    )


class _ScopedRetriever:
    """Same Chroma/default SentenceTransformer search, with an owned collection."""
    def __init__(self) -> None:
        import chromadb
        from chromadb.config import Settings
        from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

        self.failure: Exception | None = None
        self.last_ids: list[str] = []
        self.last_metadatas: list[dict[str, Any]] = []
        self._closed = False
        self.name = "amem_" + uuid.uuid4().hex
        self._db = chromadb.EphemeralClient(Settings(anonymized_telemetry=False))
        # SentenceTransformerEmbeddingFunction caches the model process-wide.
        # Serialize initial loading; subsequent agents share the default model.
        with _MODEL_LOCK:
            embedding = SentenceTransformerEmbeddingFunction(
                model_name=AMEM_EMBEDDING_MODEL,
                cache_folder=str(Path(__file__).resolve().parents[2] / "data" / "models"),
                local_files_only=True,
            )
        self.collection = self._db.create_collection(self.name, embedding_function=embedding)
        self.client = types.SimpleNamespace(reset=self.reset)

    def reset(self) -> None:
        ids = self.collection.get()["ids"]
        if ids:
            self.collection.delete(ids=ids)

    def add_document(self, document: str, metadata: dict[str, Any], doc_id: str) -> None:
        try:
            # Consolidation in the pinned SDK re-adds existing ids. Upsert
            # updates evolved metadata instead of Chroma ignoring duplicate ids.
            if self.collection.get(ids=[doc_id])["ids"]:
                self.collection.update(ids=[doc_id], metadatas=[_metadata(metadata)])
            else:
                self.collection.add(ids=[doc_id], documents=[document], metadatas=[_metadata(metadata)])
        except Exception as exc:
            self.failure = AMemOfficialError(f"A-Mem index write failed ({type(exc).__name__})")
            raise self.failure from exc

    def search(self, query: str, k: int = 5) -> dict[str, Any]:
        try:
            self.last_metadatas = []
            count = self.collection.count()
            if not count:
                self.last_ids = []
                return {"ids": [[]], "metadatas": [[]], "distances": [[]]}
            results = self.collection.query(query_texts=[query], n_results=min(k, count))
            self.last_ids = list(results["ids"][0])
            for group in results["metadatas"]:
                for item in group:
                    for name in ("keywords", "links", "tags", "evolution_history"):
                        item[name] = json.loads(item[name])
                    item["retrieval_count"] = int(item["retrieval_count"])
            self.last_metadatas = copy.deepcopy(results["metadatas"][0])
            return results
        except Exception as exc:
            self.failure = AMemOfficialError(f"A-Mem search failed ({type(exc).__name__})")
            raise self.failure from exc

    def close(self) -> None:
        if not self._closed:
            self._db.delete_collection(self.name)
            self._closed = True


class AMemRuntime:
    def __init__(self, *, llm: Any, agent_id: str, loop: Any,
                 records: list[dict[str, Any]], evo_cnt: int, evo_threshold: int,
                 evolution_neighbor_k: int = 5,
                 response_validation_max_attempts: int = _RESPONSE_ATTEMPTS,
                 response_tolerance_enabled: bool = True,
                 on_event_progress: Any = None) -> None:
        self.agent_id = agent_id
        self.controller = _PlatformController(
            llm, agent_id, loop, response_validation_max_attempts,
            response_tolerance_enabled,
        )
        self.evolution_neighbor_k = evolution_neighbor_k
        self.on_event_progress = on_event_progress
        self.retriever: Any = None
        try:
            # Load verified upstream code in a private module namespace. Its
            # constructor/consolidator receive instance-local collaborators;
            # other threads and the installed upstream module are untouched.
            module = types.ModuleType("agentic_memory._platform_instance")
            module.__package__ = "agentic_memory"
            controller_module = types.SimpleNamespace(
                LLMController=lambda *args: types.SimpleNamespace(llm=self.controller)
            )

            def platform_import(name, globals=None, locals=None, fromlist=(), level=0):
                # The upstream controller and its unused LiteLLM import are
                # replaced by the platform bridge. Importing LiteLLM otherwise
                # loads .env into os.environ and changes other agents' backends.
                if level == 1 and name == "llm_controller":
                    return controller_module
                if level == 0 and name == "litellm":
                    return types.SimpleNamespace(completion=self.controller.get_completion)
                return builtins.__import__(name, globals, locals, fromlist, level)

            module.__dict__["__builtins__"] = {**vars(builtins), "__import__": platform_import}
            exec(_upstream_code(), module.__dict__)
            self.retriever = _ScopedRetriever()
            module.ChromaRetriever = lambda **kwargs: self.retriever
            module.LLMController = lambda *args: types.SimpleNamespace(llm=self.controller)

            class ScopedSystem(module.AgenticMemorySystem):
                def find_related_memories(system, query: str, k: int = 5) -> Any:
                    # The pinned upstream process_memory() calls this method
                    # with k=5 explicitly.  The experiment's frozen setting
                    # must therefore override that argument here.
                    _, ranks = super().find_related_memories(
                        query, self.evolution_neighbor_k
                    )
                    ids = self.retriever.last_ids
                    self.controller.neighbor_ids = [ids[rank] for rank in ranks]
                    self.controller.neighbor_states = {
                        identifier: {
                            "context": system.memories[identifier].context,
                            "tags": copy.deepcopy(system.memories[identifier].tags),
                        }
                        for identifier in self.controller.neighbor_ids
                    }
                    positions = {key: i for i, key in enumerate(system.memories)}
                    # Upstream confuses retrieval ranks with insertion indices.
                    # Render only structural labels with actual IDs. Never replace
                    # matching literals inside source content or generated context.
                    text = "".join(
                        _related_memory_text(ids[rank], self.retriever.last_metadatas[rank])
                        for rank in ranks
                    )
                    return text, [positions[ids[rank]] for rank in ranks]

            self.system = ScopedSystem(evo_threshold=evo_threshold)
            self.system.evo_cnt = evo_cnt
            self.source_ids = {}
            for record in records:
                note = module.MemoryNote(**copy.deepcopy(record["note"]))
                self.system.memories[note.id] = note
                self.source_ids[note.id] = record["source_event_id"]
                self.retriever.collection.add(
                    ids=[note.id], documents=[note.content],
                    metadatas=[_metadata(vars(note))], embeddings=[record["embedding"]],
                )
        except BaseException as exc:
            if self.retriever is not None:
                self.retriever.close()
            if isinstance(exc, ImportError):
                raise AMemOfficialDependencyError(
                    "A-Mem dependencies are missing; install requirements.txt"
                ) from exc
            raise

    def set_loop(self, loop: Any) -> None:
        self.controller.loop = loop

    def _check_failure(self) -> None:
        if self.controller.failure is not None:
            raise self.controller.failure
        if self.retriever.failure is not None:
            raise self.retriever.failure

    def observe(self, events: list[Any]) -> Any:
        total = len(events)
        for index, event in enumerate(events, start=1):
            self.controller.source_event_id = event.event_id
            content = event.content
            # The pinned add_note does not call its own analyze_content method.
            # Invoke that upstream method explicitly before adding the note.
            analysis = self.system.analyze_content(content)
            self._check_failure()
            previous = {key: copy.deepcopy(vars(note)) for key, note in self.system.memories.items()}
            try:
                timestamp = datetime.fromisoformat(event.created_at.replace("Z", "+00:00")).strftime("%Y%m%d%H%M")
            except ValueError:
                timestamp = event.created_at or "memory-derived"
            identifier = self.system.add_note(
                content, id=note_id(self.agent_id, event.event_id),
                timestamp=timestamp, last_accessed=timestamp, **analysis,
            )
            self._check_failure()
            self.source_ids[identifier] = event.event_id
            # Evolved neighbors must be reflected by search immediately, while
            # their original document vectors remain unchanged.
            for key, note in self.system.memories.items():
                if any(link not in self.system.memories for link in note.links):
                    raise AMemOfficialError("A-Mem evolution returned an unknown memory link")
                if key in previous and previous[key] != vars(note):
                    self.retriever.collection.update(ids=[key], metadatas=[_metadata(vars(note))])
            if self.on_event_progress is not None:
                self.on_event_progress({
                    "status": "running",
                    "completed_events": index,
                    "total_events": total,
                    "current_event_id": (
                        events[index].event_id if index < total else None
                    ),
                    "last_completed_event_id": event.event_id,
                })
        vectors = self.retriever.collection.get(include=["embeddings"])
        by_id = dict(zip(vectors["ids"], vectors["embeddings"]))
        return [
            {"note": copy.deepcopy(vars(note)), "source_event_id": self.source_ids[key],
             "embedding": [float(n) for n in by_id[key]]}
            for key, note in self.system.memories.items()
        ], self.system.evo_cnt

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        results = self.system.search_agentic(query, k=limit)
        self._check_failure()
        return results

    def close(self) -> None:
        self.retriever.close()
