# -*- coding: utf-8 -*-
import ast
import inspect
import os
import re
import textwrap

from odoo import http
from odoo.http import request


PATH_PARAM_RE = re.compile(r"<(?:(?P<converter>[^:>]+):)?(?P<name>[^>]+)>")
JSON_CONTAINER_NAMES = {"payload", "data", "body", "json_data", "request_data"}
QUERY_CONTAINER_NAMES = {"kwargs", "_kwargs", "params", "query_params"}


class SwaggerUIController(http.Controller):
    def _ensure_docs_access(self):
        user = request.env.user
        allowed_groups = (
            "base.group_system",
            "access_rights.group_admin",
            "access_rights.group_support_staff",
            "access_rights.group_support_staff_manager",
        )
        if user and (
            request.env.is_superuser()
            or any(user.has_group(group_name) for group_name in allowed_groups)
        ):
            return None
        return request.not_found()

    def _iter_custom_api_rules(self):
        routing_map = request.env["ir.http"].routing_map()
        seen = set()
        for rule in routing_map.iter_rules():
            route_path = getattr(rule, "rule", "") or ""
            if not route_path.startswith("/api"):
                continue
            if route_path in ("/api/docs", "/api/openapi.json"):
                continue

            endpoint = getattr(rule, "endpoint", None)
            addon_name = self._get_addon_name(endpoint)
            source_path = self._get_source_path(endpoint)
            if not addon_name or addon_name == "api_swagger_ui":
                continue

            methods = sorted(
                method for method in (rule.methods or set())
                if method not in {"HEAD", "OPTIONS"}
            )
            if not methods:
                continue

            dedupe_key = (route_path, tuple(methods), addon_name, getattr(endpoint, "__name__", ""))
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            yield rule, endpoint, source_path, addon_name, methods

    def _get_source_path(self, endpoint):
        method = getattr(endpoint, "original_endpoint", None) or endpoint
        try:
            return os.path.abspath(inspect.getsourcefile(method) or "")
        except (OSError, TypeError):
            return ""

    def _get_addon_name(self, endpoint):
        method = getattr(endpoint, "original_endpoint", None) or endpoint
        module_name = getattr(method, "__module__", "") or ""
        parts = module_name.split(".")
        if "addons" in parts:
            addon_index = parts.index("addons") + 1
            if addon_index < len(parts):
                return parts[addon_index]
        return ""

    def _rule_to_openapi_path(self, route_path):
        return PATH_PARAM_RE.sub(lambda match: "{%s}" % match.group("name"), route_path)

    def _build_parameters(self, route_path):
        parameters = []
        for match in PATH_PARAM_RE.finditer(route_path):
            converter = (match.group("converter") or "string").lower()
            name = match.group("name")
            schema = {"type": "string"}
            if converter in {"int", "integer"}:
                schema = {"type": "integer"}
            elif converter in {"float"}:
                schema = {"type": "number", "format": "float"}
            elif converter in {"uuid"}:
                schema = {"type": "string", "format": "uuid"}

            parameters.append({
                "name": name,
                "in": "path",
                "required": True,
                "schema": schema,
            })
        return parameters

    def _infer_schema_from_name(self, field_name, default=None):
        if isinstance(default, bool):
            return {"type": "boolean"}
        if isinstance(default, int) and not isinstance(default, bool):
            return {"type": "integer"}
        if isinstance(default, float):
            return {"type": "number", "format": "float"}
        if isinstance(default, list):
            return {"type": "array", "items": {"type": "string"}}
        if isinstance(default, dict):
            return {"type": "object", "additionalProperties": True}

        lowered = (field_name or "").lower()
        if lowered.endswith("_ids") or lowered.endswith("ids"):
            return {"type": "array", "items": {"type": "string"}}
        if lowered.endswith("_id") or lowered in {"id", "plan_id", "protocol_id", "subscription_id"}:
            return {"type": "integer"}
        if lowered.startswith("is_") or lowered.startswith("has_") or lowered.endswith("_enabled"):
            return {"type": "boolean"}
        if "email" in lowered:
            return {"type": "string", "format": "email"}
        if "date" in lowered or lowered.endswith("_at") or "time" in lowered:
            return {"type": "string", "format": "date-time"}
        if lowered in {"amount", "price", "minimumreward", "discount_value", "discount_amount"}:
            return {"type": "number"}
        return {"type": "string"}

    def _literal_value(self, node):
        try:
            return ast.literal_eval(node)
        except Exception:
            return None

    def _extract_container_name(self, node):
        if isinstance(node, ast.Name):
            return node.id
        return None

    def _extract_string_list(self, node, assigned_lists):
        if isinstance(node, ast.Name):
            return assigned_lists.get(node.id, [])
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            values = []
            for elt in node.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    values.append(elt.value)
            return values
        return []

    def _extract_doc_fields(self, doc):
        field_docs = {}
        for line in (doc or "").splitlines():
            stripped = line.strip()
            if not stripped.startswith(":param "):
                continue
            _, _, remainder = stripped.partition(":param ")
            name, _, description = remainder.partition(":")
            name = name.strip()
            description = description.strip()
            if name:
                field_docs[name] = description
        return field_docs

    def _collect_endpoint_inputs(self, endpoint):
        method = getattr(endpoint, "original_endpoint", None) or endpoint
        try:
            source = textwrap.dedent(inspect.getsource(method))
        except (OSError, TypeError):
            return {"query": {}, "body": {}, "required_query": set(), "required_body": set()}

        try:
            tree = ast.parse(source)
        except SyntaxError:
            return {"query": {}, "body": {}, "required_query": set(), "required_body": set()}

        assigned_lists = {}
        body_fields = {}
        query_fields = {}
        required_body = set()
        required_query = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                target_name = node.targets[0].id
                extracted = self._extract_string_list(node.value, assigned_lists)
                if extracted:
                    assigned_lists[target_name] = extracted

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                owner = node.func.value
                attr = node.func.attr

                if attr == "get" and len(node.args) >= 1 and isinstance(node.args[0], ast.Constant):
                    field_name = node.args[0].value
                    if not isinstance(field_name, str):
                        continue
                    default = self._literal_value(node.args[1]) if len(node.args) > 1 else None
                    container_name = self._extract_container_name(owner)
                    target_map = None
                    if container_name in JSON_CONTAINER_NAMES:
                        target_map = body_fields
                    elif container_name in QUERY_CONTAINER_NAMES:
                        target_map = query_fields
                    elif (
                        isinstance(owner, ast.Attribute)
                        and owner.attr == "args"
                        and isinstance(owner.value, ast.Attribute)
                        and owner.value.attr == "httprequest"
                    ):
                        target_map = query_fields

                    if target_map is not None and field_name not in target_map:
                        target_map[field_name] = {
                            "schema": self._infer_schema_from_name(field_name, default),
                        }

                if attr == "_validate_payload" and len(node.args) >= 2:
                    container_name = self._extract_container_name(node.args[0])
                    required_fields = self._extract_string_list(node.args[1], assigned_lists)
                    if container_name in JSON_CONTAINER_NAMES:
                        required_body.update(required_fields)
                    elif container_name in QUERY_CONTAINER_NAMES:
                        required_query.update(required_fields)

        return {
            "query": query_fields,
            "body": body_fields,
            "required_query": required_query,
            "required_body": required_body,
        }

    def _build_inferred_query_parameters(self, route_path, endpoint):
        path_names = {param["name"] for param in self._build_parameters(route_path)}
        inputs = self._collect_endpoint_inputs(endpoint)
        doc_fields = self._extract_doc_fields(inspect.getdoc(getattr(endpoint, "original_endpoint", None) or endpoint) or "")
        parameters = []
        for field_name in sorted(inputs["query"]):
            if field_name in path_names:
                continue
            parameter = {
                "name": field_name,
                "in": "query",
                "required": field_name in inputs["required_query"],
                "schema": inputs["query"][field_name]["schema"],
            }
            if field_name in doc_fields:
                parameter["description"] = doc_fields[field_name]
            parameters.append(parameter)
        return parameters

    def _build_request_body(self, methods, endpoint):
        if not any(method in {"POST", "PUT", "PATCH", "DELETE"} for method in methods):
            return None

        inputs = self._collect_endpoint_inputs(endpoint)
        if not inputs["body"]:
            return None

        method = getattr(endpoint, "original_endpoint", None) or endpoint
        doc_fields = self._extract_doc_fields(inspect.getdoc(method) or "")
        properties = {}
        required_fields = []
        for field_name in sorted(inputs["body"]):
            field_schema = dict(inputs["body"][field_name]["schema"])
            if field_name in doc_fields:
                field_schema["description"] = doc_fields[field_name]
            properties[field_name] = field_schema
            if field_name in inputs["required_body"]:
                required_fields.append(field_name)

        return {
            "required": bool(required_fields),
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": properties,
                        "additionalProperties": True,
                        **({"required": required_fields} if required_fields else {}),
                    }
                }
            },
        }

    def _build_tag(self, addon_name):
        return (addon_name or "custom").replace("_", " ").title()

    def _build_operation(self, rule, endpoint, source_path, addon_name, http_method):
        method = getattr(endpoint, "original_endpoint", None) or endpoint
        doc = inspect.getdoc(method) or ""
        doc_lines = [line.strip() for line in doc.splitlines() if line.strip()]
        summary = doc_lines[0] if doc_lines else "%s %s" % (http_method, rule.rule)
        description_parts = []
        if len(doc_lines) > 1:
            description_parts.append("\n".join(doc_lines[1:]))

        routing = getattr(endpoint, "routing", {}) or {}
        description_parts.append(
            "Odoo route metadata: auth=%s, type=%s, csrf=%s"
            % (
                routing.get("auth", "user"),
                routing.get("type", "http"),
                routing.get("csrf", True),
            )
        )

        operation = {
            "tags": [self._build_tag(addon_name)],
            "summary": summary,
            "description": "\n\n".join(part for part in description_parts if part),
            "operationId": "%s.%s.%s" % (
                getattr(method, "__module__", "custom"),
                getattr(method, "__name__", "operation"),
                http_method.lower(),
            ),
            "parameters": self._build_parameters(rule.rule) + self._build_inferred_query_parameters(rule.rule, endpoint),
            "responses": {
                "200": {"description": "Successful response"},
                "400": {"description": "Bad request"},
                "401": {"description": "Unauthorized"},
                "403": {"description": "Forbidden"},
                "404": {"description": "Not found"},
                "500": {"description": "Internal server error"},
            },
            "x-odoo-auth": routing.get("auth", "user"),
            "x-odoo-type": routing.get("type", "http"),
            "x-addon-name": addon_name,
            "x-source-file": source_path,
        }

        request_body = self._build_request_body([http_method], endpoint)
        if request_body:
            operation["requestBody"] = request_body

        if routing.get("auth") == "user":
            operation["security"] = [{"SessionAuth": []}, {"BearerAuth": []}]

        return operation

    def _build_openapi_spec(self):
        base_url = request.httprequest.url_root.rstrip("/")
        paths = {}
        tags = set()

        for rule, endpoint, source_path, addon_name, methods in self._iter_custom_api_rules():
            openapi_path = self._rule_to_openapi_path(rule.rule)
            path_item = paths.setdefault(openapi_path, {})
            tag = self._build_tag(addon_name)
            tags.add(tag)
            for http_method in methods:
                path_item[http_method.lower()] = self._build_operation(
                    rule, endpoint, source_path, addon_name, http_method
                )

        return {
            "openapi": "3.0.3",
            "info": {
                "title": "Zeeve Custom API",
                "version": "1.0.0",
                "description": "Generated from installed custom Odoo controllers under Custom-odoo-addons.",
            },
            "servers": [{"url": base_url}],
            "tags": [{"name": tag} for tag in sorted(tags)],
            "paths": dict(sorted(paths.items())),
            "components": {
                "securitySchemes": {
                    "SessionAuth": {
                        "type": "apiKey",
                        "in": "cookie",
                        "name": "session_id",
                    },
                    "BearerAuth": {
                        "type": "http",
                        "scheme": "bearer",
                        "bearerFormat": "JWT",
                    },
                }
            },
        }

    @http.route("/api/openapi.json", type="http", auth="user", methods=["GET"], csrf=False)
    def openapi_json(self):
        access_error = self._ensure_docs_access()
        if access_error:
            return access_error
        return request.make_json_response(self._build_openapi_spec())

    @http.route("/api/docs", type="http", auth="user", methods=["GET"], csrf=False)
    def swagger_ui(self):
        access_error = self._ensure_docs_access()
        if access_error:
            return access_error
        html = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Zeeve API Docs</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css" />
  <style>
    body { margin: 0; background: #f6f8fb; }
    .topbar { display: none; }
    .swagger-ui .information-container { padding-bottom: 0; }
  </style>
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    window.onload = function () {
      window.ui = SwaggerUIBundle({
        url: "/api/openapi.json",
        dom_id: "#swagger-ui",
        deepLinking: true,
        docExpansion: "list",
        displayRequestDuration: true,
        persistAuthorization: true,
      });
    };
  </script>
</body>
</html>
"""
        return request.make_response(html, headers=[("Content-Type", "text/html; charset=utf-8")])
