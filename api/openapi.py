"""OpenAPI 3.1 description of the API, plus a small JSON Schema checker used by the tests (and
available to anyone who wants to assert responses match the published contract)."""

from __future__ import annotations

from typing import Any

ERROR_CODES = {
    "missing_api_key": "No key sent. Use `Authorization: Bearer am_live_...` (or `X-API-Key`).",
    "invalid_api_key": "The key isn't recognised.",
    "key_revoked": "The key was revoked (subscription ended) or rotated.",
    "payment_required": "The subscription payment has been failing for over 7 days. Update the card (see your email) to restore access.",
    "insufficient_permission": "The key's plan doesn't include this endpoint.",
    "rate_limited": "Too many requests in a short burst. Retry after `Retry-After` seconds.",
    "quota_exceeded": "Today's request quota is used up. It resets at 00:00 UTC (`Retry-After`).",
    "too_many_auth_failures": "Too many failed authentications from this client. Retry later.",
    "invalid_parameter": "A query parameter is malformed or unknown; `param` names it.",
    "invalid_cursor": "The pagination cursor is malformed or belongs to other filters.",
    "cursor_expired": "The dataset was refreshed since the cursor was issued. Start again without it.",
    "not_found": "No such company, or no such route.",
    "method_not_allowed": "Wrong HTTP method for this route.",
    "internal_error": "Something failed on our side. Safe to retry.",
}

NULLABLE_STR = {"type": ["string", "null"]}


def spec(server_url: str = "", version: str = "1.0.0") -> dict[str, Any]:
    signal = {
        "type": "object",
        "required": ["company_id", "company", "stack", "intent_score", "urgency_score", "niches"],
        "properties": {
            "company_id": {"type": "string", "description": "Stable slug of the company name"},
            "company": {"type": "string"},
            "domain": NULLABLE_STR,
            "niches": {"type": "array", "items": {"type": "string"}},
            "stack": {"type": "array", "items": {"type": "string"}},
            "intent_tag": {**NULLABLE_STR, "examples": ["Urgency: High (Cloud Migration)"]},
            "intent_level": {"type": ["string", "null"], "enum": ["High", "Medium", "Low", None]},
            "intent_score": {"type": "integer", "minimum": 0, "maximum": 100},
            "intent_category": {"type": ["string", "null"], "enum": ["migration", "compliance", "leadership", None]},
            "commercial_signals": {"type": "array", "items": {"type": "string"}},
            "migration_path": {**NULLABLE_STR, "examples": ["Oracle → PostgreSQL"]},
            "urgency_score": {"type": "integer", "minimum": 0, "maximum": 100},
            "openings": {"type": "integer", "minimum": 0},
            "open_positions": {"type": "array", "items": {"type": "string"}},
            "remote_friendly": {"type": "boolean"},
            "careers_url": NULLABLE_STR,
            "careers_url_verified": {"type": "boolean"},
            "latest_posted_at": NULLABLE_STR,
        },
    }
    error = {
        "type": "object", "required": ["error"],
        "properties": {"error": {"type": "object", "required": ["code", "message", "status"], "properties": {
            "code": {"type": "string", "enum": sorted(ERROR_CODES)},
            "message": {"type": "string"},
            "status": {"type": "integer"},
            "param": NULLABLE_STR,
            "doc_url": {"type": "string"},
            "request_id": {"type": "string"},
        }}},
    }
    rate_headers = {
        "X-RateLimit-Limit": {"description": "Requests per day on this key's plan", "schema": {"type": "integer"}},
        "X-RateLimit-Remaining": {"description": "Requests left today", "schema": {"type": "integer"}},
        "X-RateLimit-Reset": {"description": "Unix time when the daily quota resets (00:00 UTC)", "schema": {"type": "integer"}},
        "X-Request-Id": {"description": "Quote this when reporting a problem", "schema": {"type": "string"}},
    }

    def err(desc: str) -> dict[str, Any]:
        return {"description": desc, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}

    throttled = {**err("Rate limited or quota exhausted"), "headers": {
        **rate_headers, "Retry-After": {"description": "Seconds to wait", "schema": {"type": "integer"}}}}
    common = {"400": err("Invalid parameter"), "401": err("Missing, invalid or revoked key"),
              "403": err("Key lacks permission"), "429": throttled, "500": err("Internal error")}
    q = lambda name, desc, schema, example=None: {"name": name, "in": "query", "required": False, "description": desc,  # noqa: E731
                                                  "schema": schema, **({"example": example} if example is not None else {})}
    return {
        "openapi": "3.1.0",
        "jsonSchemaDialect": "https://spec.openapis.org/oas/3.1/dialect/base",
        "info": {
            "title": "AutoMonetize Hiring-Signal API", "version": version,
            "summary": "Company-level tech stacks and buying-intent signals from live job postings.",
            "description": ("Every company currently hiring in the tracked niches, with its technology footprint, "
                            "buying-intent tag (migrations, compliance deadlines, first/founding hires), migration "
                            "history and active postings. Refreshed continuously from public job-board APIs.\n\n"
                            "**Errors** always look like `{\"error\": {\"code\", \"message\", \"status\", \"param\"}}`; "
                            "switch on `code`. Codes: " + ", ".join(f"`{c}`" for c in sorted(ERROR_CODES)) + "."),
        },
        "servers": [{"url": server_url or "/", "description": "Production"}],
        "security": [{"bearerAuth": []}, {"apiKeyHeader": []}],
        "paths": {
            "/v1/signals": {"get": {
                "operationId": "listSignals", "summary": "Query hiring and buying-intent signals",
                "description": "Sorted by intent score, then urgency, then company id (stable). Paginate with `cursor`.",
                "parameters": [
                    q("tech", "Only companies whose stack includes this technology (case-insensitive)", {"type": "string"}, "Kubernetes"),
                    q("intent_tag", "Substring of the intent tag, level or signal, e.g. `Cloud Migration`, `High`, `SOC 2`",
                      {"type": "string"}, "Cloud Migration"),
                    q("min_urgency", "Minimum hiring-urgency score", {"type": "integer", "minimum": 0, "maximum": 100}, 60),
                    q("min_intent", "Minimum buying-intent score", {"type": "integer", "minimum": 0, "maximum": 100}),
                    q("since", "Only companies with a posting on/after this ISO 8601 date", {"type": "string", "format": "date-time"},
                      "2026-09-01"),
                    q("limit", "Page size", {"type": "integer", "minimum": 1, "maximum": 100, "default": 25}),
                    q("cursor", "`pagination.next_cursor` from the previous page", {"type": "string"}),
                ],
                "responses": {"200": {"description": "A page of signals", "headers": rate_headers,
                                      "content": {"application/json": {"schema": {"$ref": "#/components/schemas/SignalList"}}}},
                              **common},
            }},
            "/v1/companies/{domain}": {"get": {
                "operationId": "getCompany", "summary": "One company: footprint, migration history, active postings",
                "parameters": [{"name": "domain", "in": "path", "required": True, "schema": {"type": "string"},
                                "description": "Company domain (e.g. `acme.com`) or `company_id`", "example": "acme.com"}],
                "responses": {"200": {"description": "The company", "headers": rate_headers,
                                      "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Company"}}}},
                              "404": err("Unknown company"), **common},
            }},
            "/v1/me": {"get": {
                "operationId": "getKey", "summary": "This key's plan, status and today's usage",
                "responses": {"200": {"description": "Key info", "headers": rate_headers,
                                      "content": {"application/json": {"schema": {"$ref": "#/components/schemas/KeyInfo"}}}},
                              **common},
            }},
            "/v1/orders/recover": {"post": {
                "operationId": "recoverOrders", "summary": "Re-send everything bought with an email address (no key needed)",
                "description": ("Always answers 202 with the same body, whether or not the address has purchases; the files go "
                                "to that address only. 3 requests per client per hour."),
                "security": [],
                "requestBody": {"required": True, "content": {"application/json": {"schema": {
                    "type": "object", "required": ["email"], "properties": {"email": {"type": "string", "format": "email"}}}}}},
                "responses": {"202": {"description": "Accepted", "content": {"application/json": {"schema": {
                    "type": "object", "required": ["object", "status", "message"], "properties": {
                        "object": {"const": "recovery_request"}, "status": {"const": "accepted"}, "message": {"type": "string"}}}}}},
                              "400": err("Invalid email"), "429": throttled},
            }},
            "/v1/auth/rotate": {"post": {
                "operationId": "rotateKey", "summary": "Replace this key; the old one stops working immediately",
                "responses": {"201": {"description": "The new key (shown only once)",
                                      "content": {"application/json": {"schema": {"$ref": "#/components/schemas/RotatedKey"}}}},
                              **common},
            }},
        },
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "am_live_<48 hex>"},
                "apiKeyHeader": {"type": "apiKey", "in": "header", "name": "X-API-Key"},
            },
            "schemas": {
                "Signal": signal,
                "SignalList": {"type": "object", "required": ["object", "data", "pagination", "meta"], "properties": {
                    "object": {"const": "list"},
                    "data": {"type": "array", "items": {"$ref": "#/components/schemas/Signal"}},
                    "pagination": {"type": "object", "required": ["limit", "total", "has_more", "next_cursor"], "properties": {
                        "limit": {"type": "integer"}, "total": {"type": "integer"}, "has_more": {"type": "boolean"},
                        "next_cursor": NULLABLE_STR}},
                    "meta": {"$ref": "#/components/schemas/Meta"}}},
                "Company": {"allOf": [{"$ref": "#/components/schemas/Signal"}, {
                    "type": "object", "required": ["object", "history", "active_postings", "meta"], "properties": {
                        "object": {"const": "company"},
                        "history": {"type": "array", "items": {"type": "object", "required": ["date", "intent_score", "stack"],
                                                                 "properties": {"date": {"type": "string"}, "intent_tag": NULLABLE_STR,
                                                                                "intent_score": {"type": "integer"},
                                                                                "migration_path": NULLABLE_STR,
                                                                                "stack": {"type": "array", "items": {"type": "string"}}}}},
                        "active_postings": {"type": "array", "items": {"type": "object", "required": ["title"], "properties": {
                            "title": {"type": "string"}, "location": NULLABLE_STR, "remote": {"type": "boolean"},
                            "posted_at": NULLABLE_STR, "url": NULLABLE_STR, "source": NULLABLE_STR,
                            "first_seen": {"type": "string"}, "last_seen": {"type": "string"}}}},
                        "meta": {"$ref": "#/components/schemas/Meta"},
                        "dossier_available": {"type": "boolean", "description": "An Executive Migration Dossier can be bought for this company"},
                        "dossier_url": {**NULLABLE_STR, "description": "Opens Stripe checkout for this company's dossier"},
                        "dossier_price_cents": {"type": ["integer", "null"]}}}]},
                "KeyInfo": {"type": "object", "required": ["object", "prefix", "status", "plan", "daily_quota", "used_today"],
                            "properties": {"object": {"const": "api_key"}, "prefix": {"type": "string"},
                                           "status": {"type": "string", "enum": ["active", "degraded"]},
                                           "plan": {"type": "string"}, "daily_quota": {"type": "integer"},
                                           "used_today": {"type": "integer"}, "permissions": {"type": "array", "items": {"type": "string"}}}},
                "RotatedKey": {"type": "object", "required": ["object", "key", "prefix", "status"], "properties": {
                    "object": {"const": "api_key"}, "key": {"type": "string", "pattern": "^am_live_[0-9a-f]{48}$"},
                    "prefix": {"type": "string"}, "status": {"type": "string"}, "previous_prefix": {"type": "string"}}},
                "Meta": {"type": "object", "required": ["dataset_version"], "properties": {"dataset_version": {"type": "string"}}},
                "Error": error,
            },
        },
    }


# ----------------------------------------------------------------------------- tiny validator
class SchemaError(AssertionError):
    pass


_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}


def _is(value: Any, t: str) -> bool:
    if t == "null":
        return value is None
    if t in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, _TYPES[t])


def validate(value: Any, schema: dict[str, Any], root: dict[str, Any], path: str = "$") -> None:
    """The subset of JSON Schema 2020-12 this spec uses: $ref, allOf, type (incl. lists), const,
    enum, required, properties, items, minimum/maximum, pattern."""
    import re

    if "$ref" in schema:
        node: Any = root
        for part in schema["$ref"].lstrip("#/").split("/"):
            node = node[part]
        return validate(value, node, root, path)
    for sub in schema.get("allOf", []):
        validate(value, sub, root, path)
    types = schema.get("type")
    if types is not None:
        types = [types] if isinstance(types, str) else types
        if not any(_is(value, t) for t in types):
            raise SchemaError(f"{path}: expected {types}, got {type(value).__name__} ({value!r})")
    if "const" in schema and value != schema["const"]:
        raise SchemaError(f"{path}: expected {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError(f"{path}: {value!r} not in {schema['enum']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise SchemaError(f"{path}: {value} < {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise SchemaError(f"{path}: {value} > {schema['maximum']}")
    if isinstance(value, str) and "pattern" in schema and not re.search(schema["pattern"], value):
        raise SchemaError(f"{path}: {value!r} doesn't match {schema['pattern']}")
    if isinstance(value, dict):
        for req in schema.get("required", []):
            if req not in value:
                raise SchemaError(f"{path}: missing {req!r}")
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                validate(value[key], sub, root, f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            validate(item, schema["items"], root, f"{path}[{i}]")
