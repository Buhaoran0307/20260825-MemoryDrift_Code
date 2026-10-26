from __future__ import annotations

from typing import Any



def build_openapi_spec() -> dict[str, Any]:
    post_schema = {
        "type": "object",
        "properties": {
            "post_id": {"type": "integer"},
            "agent_id": {"type": "string"},
            "display_name": {"type": "string"},
            "community": {"type": "string"},
            "title": {"type": "string"},
            "body": {"type": "string"},
            "created_at": {"type": "string"},
            "source": {"type": ["string", "null"]},
            "source_post_id": {"type": ["string", "null"]},
            "metadata": {"type": "object"},
            "upvotes": {
                "type": ["integer", "null"],
                "minimum": 0,
                "description": "Observed upvote count; null when unavailable.",
            },
            "downvotes": {
                "type": ["integer", "null"],
                "minimum": 0,
                "description": "Observed downvote count; null when unavailable.",
            },
        },
        "required": [
            "post_id",
            "agent_id",
            "display_name",
            "community",
            "title",
            "body",
            "created_at",
        ],
    }
    comment_schema = {
        "type": "object",
        "properties": {
            "comment_id": {"type": "integer"},
            "post_id": {"type": "integer"},
            "parent_comment_id": {"type": ["integer", "null"]},
            "agent_id": {"type": "string"},
            "display_name": {"type": "string"},
            "body": {"type": "string"},
            "created_at": {"type": "string"},
            "source": {"type": ["string", "null"]},
            "source_comment_id": {"type": ["string", "null"]},
            "metadata": {"type": "object"},
        },
        "required": ["comment_id", "post_id", "agent_id", "body", "created_at"],
    }

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Agent Posting Experiment API",
            "version": "0.1.0",
            "description": "Local three-agent demo API and read-only saved experiment viewer.",
        },
        "paths": {
            "/api/health": {
                "get": {
                    "summary": "Health check",
                    "responses": {"200": {"description": "Server state"}},
                }
            },
            "/api/experiment": {
                "get": {
                    "summary": "Read the active run manifest and polling configuration",
                    "responses": {"200": {"description": "Experiment progress"}},
                }
            },
            "/api/experiment/thread": {
                "get": {
                    "summary": "Read the latest fully committed thread snapshot",
                    "responses": {"200": {"description": "Committed thread state"}},
                }
            },
            "/api/agents": {
                "get": {
                    "summary": "List hosted demo agents",
                    "responses": {"200": {"description": "Agent list"}},
                }
            },
            "/api/tools": {
                "get": {
                    "summary": "List available tool schemas",
                    "responses": {"200": {"description": "Tool schema list"}},
                }
            },
            "/api/posts": {
                "get": {
                    "summary": "List posts",
                    "parameters": [
                        {
                            "name": "community",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "limit",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "integer", "default": 100},
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "Posts",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "posts": {"type": "array", "items": post_schema}
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/api/reset": {
                "post": {
                    "summary": "Reset demo database and bootstrap agents",
                    "responses": {"200": {"description": "Reset result"}},
                }
            },
            "/api/threads/{post_id}": {
                "get": {
                    "summary": "Read one post and its chronological comment history",
                    "parameters": [
                        {
                            "name": "post_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Thread history",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "post": post_schema,
                                            "comments": {
                                                "type": "array",
                                                "items": comment_schema,
                                            },
                                        },
                                        "required": ["post", "comments"],
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/api/rounds": {
                "get": {
                    "summary": "List fully committed round records",
                    "responses": {"200": {"description": "Round records"}},
                },
                "post": {
                    "summary": "Run one simulation round",
                    "requestBody": {
                        "required": False,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "community": {
                                            "type": "string",
                                            "default": "moltbook-demo",
                                        },
                                        "round_index": {"type": "integer"},
                                    },
                                }
                            }
                        },
                    },
                    "responses": {"201": {"description": "Round execution record"}},
                },
            },
            "/api/tools/call": {
                "post": {
                    "summary": "Execute a tool call through the orchestrator boundary",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "call_id": {"type": "string"},
                                        "caller_agent_id": {"type": "string"},
                                        "name": {"type": "string"},
                                        "arguments": {"type": "object"},
                                    },
                                    "required": ["name", "arguments"],
                                }
                            }
                        },
                    },
                    "responses": {
                        "201": {"description": "Tool execution result"},
                        "400": {"description": "Invalid request"},
                        "403": {"description": "Permission denied"},
                    },
                }
            },
        },
    }
