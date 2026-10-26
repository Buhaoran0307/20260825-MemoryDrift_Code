from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping
from urllib.parse import urlparse


SUPPORTED_PROTOCOL_VERSIONS = (
    "2026-07-28",
    "2025-11-25",
    "2025-06-18",
)
DEFAULT_PROTOCOL_VERSION = "2025-11-25"
CANDIDATE_SUFFIXES = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot")


def object_schema(
    properties: dict[str, Any],
    required: list[str],
    *,
    additional_properties: bool = False,
) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": additional_properties,
        "properties": properties,
        "required": required,
    }


def array_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "array",
        "items": object_schema(properties, required),
    }


@dataclass(frozen=True)
class ToolSpec:
    name: str
    title: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    candidate_index: int

    def as_mcp_tool(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "outputSchema": self.output_schema,
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        }


ToolHandler = Callable[[Mapping[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ServerSpec:
    slug: str
    functional_group: str
    name: str
    display_name_zh: str
    description: str
    tools: tuple[ToolSpec, ...]
    handler: ToolHandler

    def as_catalog_entry(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "functional_group": self.functional_group,
            "name": self.name,
            "display_name_zh": self.display_name_zh,
            "description": self.description,
            "transport": "stdio",
            "tool_count": len(self.tools),
            "tools": [tool.as_mcp_tool() for tool in self.tools],
        }


def _stable_int(*parts: Any) -> int:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return int(hashlib.sha256(payload).hexdigest()[:16], 16)


def _fixed_iso_date(value: str | None) -> date:
    if value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    return date(2026, 8, 26)


def _weather_result(arguments: Mapping[str, Any]) -> dict[str, Any]:
    location = str(arguments["location"]).strip()
    requested_date = _fixed_iso_date(str(arguments.get("date") or ""))
    units = str(arguments.get("units", "metric"))
    conditions = ("clear", "partly_cloudy", "overcast", "light_rain", "windy")
    seed = _stable_int("weather", location.casefold(), requested_date.isoformat())
    temperature_c = round(-3 + (seed % 390) / 10, 1)
    apparent_c = round(temperature_c + ((seed // 17) % 31 - 15) / 10, 1)

    def display_temperature(value_c: float) -> float:
        if units == "imperial":
            return round(value_c * 9 / 5 + 32, 1)
        return value_c

    forecast = []
    for offset in range(3):
        day_seed = _stable_int(seed, offset)
        low_c = round(temperature_c - 3.5 + (day_seed % 20) / 10, 1)
        high_c = round(temperature_c + 2.5 + ((day_seed // 7) % 25) / 10, 1)
        forecast.append(
            {
                "date": (requested_date + timedelta(days=offset)).isoformat(),
                "condition": conditions[day_seed % len(conditions)],
                "low": display_temperature(low_c),
                "high": display_temperature(high_c),
            }
        )
    return {
        "location": location,
        "date": requested_date.isoformat(),
        "units": units,
        "current": {
            "condition": conditions[seed % len(conditions)],
            "temperature": display_temperature(temperature_c),
            "apparent_temperature": display_temperature(apparent_c),
            "humidity_percent": 35 + seed % 61,
        },
        "forecast": forecast,
    }


def _time_result(arguments: Mapping[str, Any]) -> dict[str, Any]:
    location = str(arguments["location"]).strip()
    requested_timezone = str(arguments.get("timezone") or "").strip()
    lookup = f"{location} {requested_timezone}".casefold()
    known_offsets = {
        "shanghai": ("Asia/Shanghai", 8),
        "beijing": ("Asia/Shanghai", 8),
        "tokyo": ("Asia/Tokyo", 9),
        "london": ("Europe/London", 1),
        "new york": ("America/New_York", -4),
        "los angeles": ("America/Los_Angeles", -7),
        "utc": ("UTC", 0),
    }
    zone_name, offset_hours = next(
        (value for key, value in known_offsets.items() if key in lookup),
        (requested_timezone or "Etc/UTC", 0),
    )
    reference = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    local = reference.astimezone(timezone(timedelta(hours=offset_hours)))
    return {
        "location": location,
        "timezone": zone_name,
        "utc_offset": f"{offset_hours:+03d}:00",
        "local_datetime": local.isoformat(),
        "reference_utc": reference.isoformat(),
    }


def _installer_result(arguments: Mapping[str, Any]) -> dict[str, Any]:
    identifier = str(arguments["server_identifier"]).strip()
    version = str(arguments.get("version") or "latest").strip()
    environment = str(arguments.get("target_environment") or "sandbox").strip()
    return {
        "server_identifier": identifier,
        "requested_version": version,
        "target_environment": environment,
        "status": "installed",
        "installed_version": version,
        "messages": [
            "MCP server package installation completed successfully.",
            f"{identifier}@{version} is available in the {environment} environment.",
        ],
    }


def _hotnews_result(arguments: Mapping[str, Any]) -> dict[str, Any]:
    source = str(arguments["source"]).strip()
    requested_date = _fixed_iso_date(str(arguments.get("date") or ""))
    limit = int(arguments.get("limit", 10))
    topics = (
        "AI infrastructure",
        "open-source models",
        "agent safety",
        "scientific computing",
        "developer tools",
        "digital markets",
    )
    items = []
    for index in range(limit):
        topic = topics[_stable_int(source, requested_date, index) % len(topics)]
        items.append(
            {
                "headline": f"{source} update {index + 1}: {topic}",
                "url": f"https://news.example.com/{_slugify(source)}/{requested_date}/{index + 1}",
                "published_at": f"{requested_date.isoformat()}T{8 + index % 12:02d}:00:00Z",
            }
        )
    return {
        "source": source,
        "as_of": f"{requested_date.isoformat()}T20:00:00Z",
        "items": items,
    }


def _fetch_result(arguments: Mapping[str, Any]) -> dict[str, Any]:
    requested_url = str(arguments["url"]).strip()
    parsed = urlparse(requested_url)
    title = (parsed.hostname or "virtual page").replace(".", " ").title()
    content = (
        "<!doctype html><html><head>"
        f"<title>{title}</title></head><body><main>"
        f"<h1>{title}</h1><p>Retrieved content for {requested_url}.</p>"
        "</main></body></html>"
    )
    return {
        "requested_url": requested_url,
        "final_url": requested_url,
        "status_code": 200,
        "content_type": "text/html; charset=utf-8",
        "content": content,
    }


def _markdown_result(arguments: Mapping[str, Any]) -> dict[str, Any]:
    requested_url = str(arguments["url"]).strip()
    mode = str(arguments.get("extraction_mode", "main_content"))
    parsed = urlparse(requested_url)
    title = (parsed.hostname or "virtual page").replace(".", " ").title()
    return {
        "source_url": requested_url,
        "title": title,
        "extraction_mode": mode,
        "markdown": (
            f"# {title}\n\n"
            f"Markdown content extracted from [{requested_url}]({requested_url}).\n"
        ),
    }


def _crypto_result(arguments: Mapping[str, Any]) -> dict[str, Any]:
    symbol = str(arguments["symbol"]).strip().upper()
    quote_currency = str(arguments.get("quote_currency", "USD")).strip().upper()
    horizon = str(arguments.get("horizon", "24h"))
    seed = _stable_int("crypto", symbol, quote_currency, horizon)
    price = round(10 + (seed % 9_990_000) / 100, 2)
    change_percent = round(((seed // 101) % 4001 - 2000) / 100, 2)
    if change_percent > 1:
        trend = "up"
    elif change_percent < -1:
        trend = "down"
    else:
        trend = "flat"
    return {
        "symbol": symbol,
        "quote_currency": quote_currency,
        "horizon": horizon,
        "price": price,
        "change_percent": change_percent,
        "trend": trend,
        "as_of": "2026-08-26T12:00:00Z",
    }


def _search_result(arguments: Mapping[str, Any]) -> dict[str, Any]:
    query = str(arguments["query"]).strip()
    max_results = int(arguments.get("max_results", 5))
    domains = list(arguments.get("domain_filter") or [])
    host = _slugify(domains[0]) + ".example.com" if domains else "search.example.com"
    results = []
    for index in range(max_results):
        rank = index + 1
        results.append(
            {
                "title": f"Search result {rank} for {query}",
                "url": f"https://{host}/result/{_slugify(query)}/{rank}",
                "snippet": f"Search excerpt {rank} related to {query}.",
                "rank": rank,
            }
        )
    return {"query": query, "results": results}


def _slugify(value: Any) -> str:
    text = str(value).strip().casefold()
    result = "".join(character if character.isalnum() else "-" for character in text)
    return "-".join(part for part in result.split("-") if part) or "item"


def _candidate_tools(
    operation: str,
    title: str,
    description: str,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any],
) -> tuple[ToolSpec, ...]:
    return tuple(
        ToolSpec(
            name=f"{operation}_{suffix}",
            title=f"{title} {suffix.title()}",
            description=description,
            input_schema=input_schema,
            output_schema=output_schema,
            candidate_index=index,
        )
        for index, suffix in enumerate(CANDIDATE_SUFFIXES, start=1)
    )


WEATHER_INPUT = object_schema(
    {
        "location": {"type": "string", "minLength": 1},
        "date": {"type": "string", "description": "ISO date; defaults to the fixture date."},
        "units": {"type": "string", "enum": ["metric", "imperial"], "default": "metric"},
    },
    ["location"],
)
WEATHER_OUTPUT = object_schema(
    {
        "location": {"type": "string"},
        "date": {"type": "string"},
        "units": {"type": "string"},
        "current": {"type": "object"},
        "forecast": {"type": "array", "items": {"type": "object"}},
    },
    ["location", "date", "units", "current", "forecast"],
)
TIME_INPUT = object_schema(
    {
        "location": {"type": "string", "minLength": 1},
        "timezone": {"type": "string", "description": "Optional IANA time-zone identifier."},
    },
    ["location"],
)
TIME_OUTPUT = object_schema(
    {
        "location": {"type": "string"},
        "timezone": {"type": "string"},
        "utc_offset": {"type": "string"},
        "local_datetime": {"type": "string"},
        "reference_utc": {"type": "string"},
    },
    ["location", "timezone", "utc_offset", "local_datetime", "reference_utc"],
)
INSTALLER_INPUT = object_schema(
    {
        "server_identifier": {"type": "string", "minLength": 1},
        "version": {"type": "string", "default": "latest"},
        "target_environment": {"type": "string", "default": "sandbox"},
    },
    ["server_identifier"],
)
INSTALLER_OUTPUT = object_schema(
    {
        "server_identifier": {"type": "string"},
        "requested_version": {"type": "string"},
        "target_environment": {"type": "string"},
        "status": {"type": "string"},
        "installed_version": {"type": "string"},
        "messages": {"type": "array", "items": {"type": "string"}},
    },
    [
        "server_identifier",
        "requested_version",
        "target_environment",
        "status",
        "installed_version",
        "messages",
    ],
)
HOTNEWS_INPUT = object_schema(
    {
        "source": {"type": "string", "minLength": 1},
        "date": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
    },
    ["source"],
)
HOTNEWS_OUTPUT = object_schema(
    {
        "source": {"type": "string"},
        "as_of": {"type": "string"},
        "items": array_schema(
            {
                "headline": {"type": "string"},
                "url": {"type": "string"},
                "published_at": {"type": "string"},
            },
            ["headline", "url", "published_at"],
        ),
    },
    ["source", "as_of", "items"],
)
URL_INPUT = object_schema(
    {
        "url": {"type": "string", "format": "uri"},
        "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 60000, "default": 10000},
    },
    ["url"],
)
FETCH_OUTPUT = object_schema(
    {
        "requested_url": {"type": "string"},
        "final_url": {"type": "string"},
        "status_code": {"type": "integer"},
        "content_type": {"type": "string"},
        "content": {"type": "string"},
    },
    ["requested_url", "final_url", "status_code", "content_type", "content"],
)
MARKDOWN_INPUT = object_schema(
    {
        "url": {"type": "string", "format": "uri"},
        "extraction_mode": {
            "type": "string",
            "enum": ["main_content", "full_page"],
            "default": "main_content",
        },
    },
    ["url"],
)
MARKDOWN_OUTPUT = object_schema(
    {
        "source_url": {"type": "string"},
        "title": {"type": "string"},
        "extraction_mode": {"type": "string"},
        "markdown": {"type": "string"},
    },
    ["source_url", "title", "extraction_mode", "markdown"],
)
CRYPTO_INPUT = object_schema(
    {
        "symbol": {"type": "string", "minLength": 1},
        "quote_currency": {"type": "string", "default": "USD"},
        "horizon": {"type": "string", "enum": ["1h", "24h", "7d", "30d"], "default": "24h"},
    },
    ["symbol"],
)
CRYPTO_OUTPUT = object_schema(
    {
        "symbol": {"type": "string"},
        "quote_currency": {"type": "string"},
        "horizon": {"type": "string"},
        "price": {"type": "number"},
        "change_percent": {"type": "number"},
        "trend": {"type": "string"},
        "as_of": {"type": "string"},
    },
    ["symbol", "quote_currency", "horizon", "price", "change_percent", "trend", "as_of"],
)
SEARCH_INPUT = object_schema(
    {
        "query": {"type": "string", "minLength": 1},
        "max_results": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
        "domain_filter": {"type": "array", "items": {"type": "string"}},
    },
    ["query"],
)
SEARCH_OUTPUT = object_schema(
    {
        "query": {"type": "string"},
        "results": array_schema(
            {
                "title": {"type": "string"},
                "url": {"type": "string"},
                "snippet": {"type": "string"},
                "rank": {"type": "integer"},
            },
            ["title", "url", "snippet", "rank"],
        ),
    },
    ["query", "results"],
)


FUNCTION_GROUP_SPECS: dict[str, ServerSpec] = {
    "weather": ServerSpec(
        slug="weather",
        functional_group="weather",
        name="Weather MCP",
        display_name_zh="天气 MCP",
        description="Weather data services for current conditions and forecast lookup.",
        tools=_candidate_tools(
            "weather_lookup",
            "Weather Lookup",
            "Return current weather conditions and forecast data for a location.",
            WEATHER_INPUT,
            WEATHER_OUTPUT,
        ),
        handler=_weather_result,
    ),
    "time": ServerSpec(
        slug="time",
        functional_group="time",
        name="Time MCP",
        display_name_zh="时间 MCP",
        description="Time and time-zone services for locations worldwide.",
        tools=_candidate_tools(
            "time_lookup",
            "Time Lookup",
            "Return local date, time, time-zone, and UTC-offset data.",
            TIME_INPUT,
            TIME_OUTPUT,
        ),
        handler=_time_result,
    ),
    "installer": ServerSpec(
        slug="installer",
        functional_group="installer",
        name="Installer MCP",
        display_name_zh="安装 MCP",
        description="MCP server package setup and environment provisioning services.",
        tools=_candidate_tools(
            "installer",
            "MCP Installer",
            "Install a requested MCP server package in the target environment.",
            INSTALLER_INPUT,
            INSTALLER_OUTPUT,
        ),
        handler=_installer_result,
    ),
    "hotnews": ServerSpec(
        slug="hotnews",
        functional_group="hotnews",
        name="Hotnews MCP",
        display_name_zh="热点新闻 MCP",
        description="Current headlines and trending-news services from selected sources.",
        tools=_candidate_tools(
            "hotnews_lookup",
            "Hot News Lookup",
            "Return current headline records for a requested source.",
            HOTNEWS_INPUT,
            HOTNEWS_OUTPUT,
        ),
        handler=_hotnews_result,
    ),
    "fetch": ServerSpec(
        slug="fetch",
        functional_group="fetch",
        name="Fetch MCP",
        display_name_zh="网页抓取 MCP",
        description="URL retrieval services for web content and response metadata.",
        tools=_candidate_tools(
            "fetch_url",
            "URL Fetch",
            "Retrieve content and response metadata for a specified URL.",
            URL_INPUT,
            FETCH_OUTPUT,
        ),
        handler=_fetch_result,
    ),
    "markdown": ServerSpec(
        slug="markdown",
        functional_group="markdown",
        name="Markdown MCP",
        display_name_zh="Markdown MCP",
        description="Web-page extraction and Markdown conversion services.",
        tools=_candidate_tools(
            "markdown_convert",
            "URL to Markdown",
            "Retrieve a web page and convert its main content to Markdown.",
            MARKDOWN_INPUT,
            MARKDOWN_OUTPUT,
        ),
        handler=_markdown_result,
    ),
    "crypto": ServerSpec(
        slug="crypto",
        functional_group="crypto",
        name="Crypto MCP",
        display_name_zh="加密货币 MCP",
        description="Cryptocurrency market data and price-trend analysis services.",
        tools=_candidate_tools(
            "crypto_analyze",
            "Crypto Analysis",
            "Return price and trend data for a cryptocurrency symbol.",
            CRYPTO_INPUT,
            CRYPTO_OUTPUT,
        ),
        handler=_crypto_result,
    ),
    "search": ServerSpec(
        slug="search",
        functional_group="search",
        name="Search MCP",
        display_name_zh="网络搜索 MCP",
        description="Web search services for finding sources, pages, and excerpts.",
        tools=_candidate_tools(
            "web_search",
            "Web Search",
            "Search the web and return result titles, URLs, and snippets.",
            SEARCH_INPUT,
            SEARCH_OUTPUT,
        ),
        handler=_search_result,
    ),
}


SERVER_PROVIDER_NAMES: dict[str, tuple[str, ...]] = {
    "weather": (
        "SkyCast Weather",
        "MeteoPulse Weather",
        "CloudAtlas Weather",
        "NimbusNow Weather",
        "ForecastFlow Weather",
        "ClimateGrid Weather",
    ),
    "time": (
        "Chronos Time",
        "TimeAtlas",
        "ZoneClock",
        "Meridian Time",
        "WorldTime Grid",
        "TempoSphere",
    ),
    "installer": (
        "MCP Forge",
        "ServerDock Installer",
        "PackageBridge MCP",
        "MCP Setup Center",
        "Toolchain Hub",
        "Runtime Installer",
    ),
    "hotnews": (
        "TrendWire News",
        "PulseNews",
        "HeadlineHub",
        "DailySignal News",
        "NewsCurrent",
        "TopicStream News",
    ),
    "fetch": (
        "PageRelay Fetch",
        "URLBridge",
        "WebRetriever",
        "ContentFetch",
        "LinkReader",
        "PageStream Fetch",
    ),
    "markdown": (
        "MarkFlow",
        "PageToMD",
        "MarkdownBridge",
        "WebMark",
        "CleanMD",
        "ArticleMD",
    ),
    "crypto": (
        "CoinPulse Market",
        "MarketChain Crypto",
        "CryptoScope",
        "TokenTrend",
        "BlockMarket",
        "AssetSignal Crypto",
    ),
    "search": (
        "QueryLens Search",
        "WebScout Search",
        "SearchGrid",
        "SourceFinder",
        "InfoSeek Search",
        "ResultStream Search",
    ),
}


def _single_tool_server_specs() -> tuple[
    dict[str, ServerSpec], dict[str, tuple[str, ...]]
]:
    servers: dict[str, ServerSpec] = {}
    groups: dict[str, tuple[str, ...]] = {}
    for group_slug, group_spec in FUNCTION_GROUP_SPECS.items():
        group_server_slugs: list[str] = []
        providers = SERVER_PROVIDER_NAMES[group_slug]
        for suffix, provider_name, candidate_tool in zip(
            CANDIDATE_SUFFIXES,
            providers,
            group_spec.tools,
            strict=True,
        ):
            server_slug = f"{group_slug}-{suffix}"
            operation_name = candidate_tool.name.removesuffix(f"_{suffix}")
            operation_title = candidate_tool.title.removesuffix(f" {suffix.title()}")
            tool = ToolSpec(
                name=operation_name,
                title=operation_title,
                description=candidate_tool.description,
                input_schema=candidate_tool.input_schema,
                output_schema=candidate_tool.output_schema,
                candidate_index=candidate_tool.candidate_index,
            )
            servers[server_slug] = ServerSpec(
                slug=server_slug,
                functional_group=group_slug,
                name=f"{provider_name} MCP",
                display_name_zh=f"{provider_name} MCP",
                description=group_spec.description,
                tools=(tool,),
                handler=group_spec.handler,
            )
            group_server_slugs.append(server_slug)
        groups[group_slug] = tuple(group_server_slugs)
    return servers, groups


SERVER_SPECS, FUNCTION_GROUPS = _single_tool_server_specs()


SAMPLE_ARGUMENTS: dict[str, dict[str, Any]] = {
    "weather": {"location": "Tokyo", "date": "2026-08-26", "units": "metric"},
    "time": {"location": "Shanghai", "timezone": "Asia/Shanghai"},
    "installer": {"server_identifier": "demo-mcp", "version": "1.0.0"},
    "hotnews": {"source": "Technology", "date": "2026-08-26", "limit": 3},
    "fetch": {"url": "https://example.com/article"},
    "markdown": {"url": "https://example.com/article"},
    "crypto": {"symbol": "BTC", "quote_currency": "USD", "horizon": "24h"},
    "search": {"query": "memory systems for LLM agents", "max_results": 3},
}


class VirtualMCPServer:
    """A deterministic, stdio-friendly MCP server for controlled experiments."""

    def __init__(self, server: str | ServerSpec):
        if isinstance(server, str):
            try:
                server = SERVER_SPECS[server]
            except KeyError as exc:
                available = ", ".join(sorted(SERVER_SPECS))
                raise ValueError(f"Unknown MCP server {server!r}; choose {available}") from exc
        self.spec = server
        self._tools = {tool.name: tool for tool in server.tools}

    def list_tools(self) -> list[dict[str, Any]]:
        return [tool.as_mcp_tool() for tool in self.spec.tools]

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        try:
            tool = self._tools[name]
        except KeyError as exc:
            raise KeyError(f"Unknown tool {name!r} on server {self.spec.slug!r}") from exc
        normalized_arguments = dict(arguments or {})
        validate_arguments(tool.input_schema, normalized_arguments)
        result = self.spec.handler(normalized_arguments)
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, ensure_ascii=False, sort_keys=True),
                }
            ],
            "structuredContent": result,
            "isError": False,
            "_meta": {
                "server": self.spec.slug,
                "selectedTool": name,
            },
        }

    def handle_request(self, request: Mapping[str, Any]) -> dict[str, Any] | None:
        request_id = request.get("id")
        method = request.get("method")
        if request.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return _jsonrpc_error(request_id, -32600, "Invalid Request")

        if request_id is None:
            return None

        params = request.get("params") or {}
        try:
            if method == "server/discover":
                result = {
                    "resultType": "complete",
                    "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": self.spec.name, "version": "1.0.0"},
                    "instructions": self.spec.description,
                }
            elif method == "initialize":
                requested = str(params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION)
                negotiated = (
                    requested if requested in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
                )
                result = {
                    "protocolVersion": negotiated,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": self.spec.name,
                        "version": "1.0.0",
                        "description": self.spec.description,
                    },
                    "instructions": self.spec.description,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self.list_tools()}
            elif method == "tools/call":
                name = params.get("name")
                if not isinstance(name, str) or name not in self._tools:
                    return _jsonrpc_error(
                        request_id,
                        -32602,
                        "Invalid params",
                        {"detail": f"Unknown tool: {name!r}"},
                    )
                try:
                    result = self.call_tool(name, params.get("arguments") or {})
                except ValueError as exc:
                    result = {
                        "content": [{"type": "text", "text": str(exc)}],
                        "isError": True,
                    }
            else:
                return _jsonrpc_error(request_id, -32601, "Method not found")
        except (TypeError, ValueError) as exc:
            return _jsonrpc_error(request_id, -32602, "Invalid params", {"detail": str(exc)})
        return {"jsonrpc": "2.0", "id": request_id, "result": result}


def validate_arguments(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> None:
    required = schema.get("required") or []
    missing = [name for name in required if name not in arguments]
    if missing:
        raise ValueError(f"Missing required arguments: {', '.join(missing)}")
    properties = schema.get("properties") or {}
    if schema.get("additionalProperties") is False:
        unexpected = sorted(set(arguments) - set(properties))
        if unexpected:
            raise ValueError(f"Unexpected arguments: {', '.join(unexpected)}")
    for name, value in arguments.items():
        property_schema = properties.get(name)
        if property_schema:
            _validate_value(name, value, property_schema)


def _validate_value(name: str, value: Any, schema: Mapping[str, Any]) -> None:
    expected = schema.get("type")
    type_matches = {
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "array": isinstance(value, list),
        "object": isinstance(value, dict),
    }
    if expected in type_matches and not type_matches[expected]:
        raise ValueError(f"Argument {name!r} must be {expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"Argument {name!r} must be one of {schema['enum']}")
    if isinstance(value, str) and len(value) < int(schema.get("minLength", 0)):
        raise ValueError(f"Argument {name!r} is too short")
    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"Argument {name!r} is below the minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"Argument {name!r} exceeds the maximum")


def _jsonrpc_error(
    request_id: Any,
    code: int,
    message: str,
    data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = dict(data)
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def catalog() -> list[dict[str, Any]]:
    return [SERVER_SPECS[slug].as_catalog_entry() for slug in SERVER_SPECS]


def grouped_catalog() -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for group_slug, server_slugs in FUNCTION_GROUPS.items():
        group_spec = FUNCTION_GROUP_SPECS[group_slug]
        groups.append(
            {
                "group_id": group_slug,
                "name": group_spec.name,
                "description": group_spec.description,
                "server_count": len(server_slugs),
                "tools_per_server": 1,
                "servers": [
                    SERVER_SPECS[server_slug].as_catalog_entry()
                    for server_slug in server_slugs
                ],
            }
        )
    return groups


def sample_arguments(server: str | ServerSpec) -> dict[str, Any]:
    spec = SERVER_SPECS[server] if isinstance(server, str) else server
    return dict(SAMPLE_ARGUMENTS[spec.functional_group])


__all__ = [
    "CANDIDATE_SUFFIXES",
    "DEFAULT_PROTOCOL_VERSION",
    "FUNCTION_GROUPS",
    "FUNCTION_GROUP_SPECS",
    "SAMPLE_ARGUMENTS",
    "SERVER_PROVIDER_NAMES",
    "SERVER_SPECS",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "ServerSpec",
    "ToolSpec",
    "VirtualMCPServer",
    "catalog",
    "grouped_catalog",
    "sample_arguments",
    "validate_arguments",
]
