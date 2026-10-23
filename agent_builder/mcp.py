# <your_app>/<your_app>/mcp.py  (same dir as hooks.py)
#
# Exposes the existing agent_builder Frappe tools over MCP (Streamable HTTP)
# using frappe-mcp. Two endpoints:
#   /api/method/<your_app>.mcp.handle_mcp      -> read + write tools
#   /api/method/<your_app>.mcp.handle_mcp_ro   -> read-only tools
#
# Auth/RBAC: neither endpoint is allow_guest. Frappe's OAuth2 bearer handling
# sets frappe.session.user, so every tool runs as the calling user and the
# permission checks inside the tools apply unchanged.

import importlib
import json

import frappe
import frappe_mcp
from frappe_mcp import Tool, ToolAnnotations

mcp = frappe_mcp.MCP("frappe-agent-tools")
mcp_ro = frappe_mcp.MCP("frappe-agent-tools-readonly")

# TODO: adjust to your real package path.
_TOOLS_PKG = "agent_builder.mcp_tools"
_SCHEMA_MODULE = "agent_builder.mcp_tools.schema"

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
WRITE_DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)

# (schema/tool name, module file, function name, annotations)
# Commented-out tools (frappe_delete_doc, frappe_aggregate) are intentionally
# NOT exposed. frappe_query covers aggregation with row-level permissions.
TOOLS = [
	("frappe_get_doc", "document_get", "frappe_get_doc", READ),
	("frappe_get_list", "document_list", "frappe_get_list", READ),
	("frappe_get_doctype_info", "document_doctype_info", "frappe_get_doctype_info", READ),
	("frappe_search_documents", "document_search_gloabal", "frappe_search_documents", READ),
	("frappe_search_link", "document_search_link", "frappe_search_link", READ),
	("frappe_query", "document_query", "frappe_query", READ),
	("frappe_list_reports", "list_report", "frappe_list_reports", READ),
	("frappe_generate_report", "generate_report", "frappe_generate_report", READ),
	("frappe_get_report_filters", "generate_report", "frappe_get_report_filters", READ),
	("frappe_get_workflow_info", "document_workflow_info", "frappe_get_workflow_info", READ),
	("frappe_get_pending_approvals", "get_pending_workflows", "frappe_get_pending_approvals", READ),
	("frappe_create_doc", "document_create", "frappe_create_doc", WRITE),
	("frappe_update_doc", "document_update", "frappe_update_doc", WRITE),
	("frappe_submit_doc", "document_submit", "frappe_submit_doc", WRITE_DESTRUCTIVE),
	("frappe_run_workflow", "document_execute_workflow", "frappe_run_workflow", WRITE_DESTRUCTIVE),
]


def _load_schemas() -> dict:
	"""schema.py holds module-level dicts {name, description, parameters}.
	Collect them by tool name, no registry or decorator needed."""
	mod = importlib.import_module(_SCHEMA_MODULE)
	out = {}
	for value in vars(mod).values():
		if isinstance(value, dict) and "name" in value and "parameters" in value:
			out[value["name"]] = value
	return out


def _adapt(fn):
	"""Existing tools take one `args: dict` and return a JSON string.
	frappe-mcp calls tools with keyword arguments, so bridge the two."""

	def wrapper(**kwargs):
		return json.loads(fn(kwargs))

	wrapper.__name__ = getattr(fn, "__name__", "tool")
	return wrapper


def _description(schema: dict) -> str:
	# Descriptions reference tools that don't exist under that name.
	return schema["description"].replace("frappe_save_doc", "frappe_update_doc")


def _register_tools(server, read_only: bool):
	if getattr(server, "_tools_loaded", False):
		return
	schemas = _load_schemas()
	for name, module, func, annotations in TOOLS:
		if read_only and not annotations.get("readOnlyHint"):
			continue
		schema = schemas.get(name)
		if schema is None:
			continue  # no schema -> don't expose
		fn = getattr(importlib.import_module(f"{_TOOLS_PKG}.{module}"), func)
		server.add_tool(
			Tool(
				name=name,
				description=_description(schema),
				input_schema=schema["parameters"],
				output_schema=None,
				annotations=annotations,
				fn=_adapt(fn),
			)
		)
	server._tools_loaded = True

def _require_login():
    if frappe.session.user == "Guest":
        raise frappe.AuthenticationError("Authentication required")

@mcp.register(allow_guest=True)  
def handle_mcp():
	_require_login()
	_register_tools(mcp, read_only=False)

@mcp_ro.register(allow_guest=True)
def handle_mcp_ro():
	_require_login()
	_register_tools(mcp_ro, read_only=True)
