# tools/frappe_tools/document_workflow_info.py

import frappe

from agent_builder.mcp_tools._common import ArgError, dumps, error_response, load_meta, norm_args


def frappe_get_workflow_info(args: dict, **kwargs) -> str:
	"""Get workflow states and transitions for a DocType, if one is defined."""
	doctype = None
	try:
		args = norm_args(args, kwargs)
		doctype_in = str(args.get("doctype") or "").strip()
		if not doctype_in:
			return dumps({"error": "doctype is required", "error_type": "invalid_argument"})

		doctype, meta, err = load_meta(doctype_in)
		if err:
			return dumps(err)
		if not frappe.has_permission(doctype, "read"):
			return dumps(
				{
					"error": f"No permission to access DocType '{doctype}'",
					"error_type": "permission_denied",
					"doctype": doctype,
				}
			)

		workflow_name = frappe.db.get_value("Workflow", {"document_type": doctype, "is_active": 1}, "name")
		if not workflow_name:
			return dumps(
				{
					"doctype": doctype,
					"has_workflow": False,
					"message": (
						f"No active workflow defined for '{doctype}'. "
						"Use frappe_update_doc / frappe_submit_doc directly."
					),
				}
			)

		workflow = frappe.get_doc("Workflow", workflow_name)
		my_roles = set(frappe.get_roles())

		states = [
			{
				"state": s.state,
				"doc_status": s.doc_status,
				"allow_edit": s.allow_edit,
				"update_field": getattr(s, "update_field", None) or None,
				"update_value": getattr(s, "update_value", None) or None,
			}
			for s in workflow.states
		]
		transitions = [
			{
				"state": t.state,
				"action": t.action,
				"next_state": t.next_state,
				"allowed": t.allowed,
				"allow_self_approval": getattr(t, "allow_self_approval", None),
				"condition": getattr(t, "condition", None) or None,
				"you_have_role": t.allowed in my_roles,
			}
			for t in workflow.transitions
		]

		return dumps(
			{
				"doctype": doctype,
				"has_workflow": True,
				"workflow_name": workflow.name,
				"workflow_state_field": workflow.workflow_state_field,
				"states": states,
				"transitions": transitions,
				"note": (
					"you_have_role only reflects roles; a transition may also be gated by its "
					"condition or self-approval rule. Use frappe_run_workflow on a specific "
					"document to see which actions are actually available."
				),
			}
		)

	except ArgError as e:
		return dumps(error_response(e, doctype=doctype))
	except Exception as e:
		return dumps(error_response(e, title="Get Workflow Info Error", doctype=doctype))
