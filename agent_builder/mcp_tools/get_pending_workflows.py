# tools/frappe_tools/document_pending_approvals.py

import frappe
from frappe.model.workflow import get_transitions
from frappe.query_builder import DocType, Order

from agent_builder.mcp_tools._common import (
	ArgError,
	coerce_bool,
	coerce_int,
	dumps,
	error_response,
	norm_args,
)

MAX_TRANSITION_DOCS = 20


def frappe_get_pending_approvals(args: dict, **kwargs) -> str:
	"""Get documents pending the current user's approval, via the Workflow Action system."""
	try:
		args = norm_args(args, kwargs)
		doctype_filter = str(args.get("doctype") or "").strip() or None
		limit = coerce_int(args.get("limit"), 50, 1, 200)
		include_actions = coerce_bool(args.get("include_actions"), True)

		user = frappe.session.user
		roles = frappe.get_roles(user)

		WA = DocType("Workflow Action")
		WAPR = DocType("Workflow Action Permitted Role")

		role_subquery = (
			frappe.qb.from_(WA)
			.join(WAPR)
			.on(WA.name == WAPR.parent)
			.select(WA.name)
			.where(WAPR.role.isin(roles))
		)

		query = (
			frappe.qb.from_(WA)
			.select(WA.name, WA.reference_doctype, WA.reference_name, WA.workflow_state, WA.user, WA.creation)
			.where(WA.status == "Open")
			.orderby(WA.creation, order=Order.desc)
			.limit(limit)
		)
		if user != "Administrator":
			query = query.where(WA.name.isin(role_subquery) | (WA.user == user))
		if doctype_filter:
			query = query.where(WA.reference_doctype == doctype_filter)

		pending = query.run(as_dict=True)

		# Only surface doctypes the user can read at all.
		readable = {}

		def can_read(dt):
			if dt not in readable:
				try:
					readable[dt] = bool(frappe.has_permission(dt, "read"))
				except Exception:
					readable[dt] = False
			return readable[dt]

		pending = [a for a in pending if can_read(a.reference_doctype)]

		if not pending:
			return dumps({"total_pending": 0, "doctypes_with_pending": [], "pending_approvals": {}})

		action_names = [a.name for a in pending]
		roles_rows = frappe.get_all(
			"Workflow Action Permitted Role",
			filters={"parent": ["in", action_names]},
			fields=["parent", "role"],
		)
		roles_map = {}
		for r in roles_rows:
			roles_map.setdefault(r.parent, []).append(r.role)

		# The same document can have several Open actions (one per notified user /
		# per role). Merge them so each document is listed once.
		merged = {}
		for action in pending:
			key = (action.reference_doctype, action.reference_name)
			entry = merged.get(key)
			if entry is None:
				entry = {
					"document_name": action.reference_name,
					"workflow_state": action.workflow_state,
					"permitted_roles": [],
					"creation": str(action.creation),
				}
				merged[key] = entry
			for role in roles_map.get(action.name, []):
				if role not in entry["permitted_roles"]:
					entry["permitted_roles"].append(role)

		transitions_map = {}
		if include_actions:
			for key in list(merged)[:MAX_TRANSITION_DOCS]:
				try:
					doc = frappe.get_doc(*key)
					transitions_map[key] = [
						{"action": t.get("action"), "next_state": t.get("next_state")}
						for t in get_transitions(doc)
					]
				except Exception:
					transitions_map[key] = []

		grouped = {}
		for key, entry in merged.items():
			if include_actions and key in transitions_map:
				entry["available_actions"] = transitions_map[key]
			grouped.setdefault(key[0], []).append(entry)

		result = {
			"total_pending": len(merged),
			"doctypes_with_pending": list(grouped.keys()),
			"pending_approvals": grouped,
		}
		if include_actions and len(merged) > MAX_TRANSITION_DOCS:
			result["actions_truncated"] = True
			result["actions_truncated_note"] = (
				f"Available actions shown for the first {MAX_TRANSITION_DOCS} documents only. "
				"Filter by doctype or set include_actions=false for the full list."
			)
		return dumps(result)

	except ArgError as e:
		return dumps(error_response(e))
	except Exception as e:
		return dumps(error_response(e, title="Pending Approvals Error"))
