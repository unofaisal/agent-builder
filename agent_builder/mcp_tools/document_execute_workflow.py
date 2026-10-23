# tools/frappe_tools/document_run_workflow.py

import frappe
from frappe.model.workflow import apply_workflow, get_transitions, get_workflow_name

from agent_builder.mcp_tools._common import (
	ArgError,
	dumps,
	error_response,
	load_doc,
	load_meta,
	norm_args,
	safe_rollback,
)


def _list_transitions(doc) -> list:
	try:
		transitions = get_transitions(doc)
	except Exception:
		return []
	return [
		{
			"action": t.get("action"),
			"next_state": t.get("next_state"),
			"allowed_roles": t.get("allowed", "").split(",") if t.get("allowed") else [],
		}
		for t in transitions
	]


def frappe_run_workflow(args: dict, **kwargs) -> str:
	"""Execute a workflow action (Approve, Reject, Submit for Review, etc.) on a document."""
	doctype = name = None
	try:
		args = norm_args(args, kwargs)
		doctype_in = str(args.get("doctype") or "").strip()
		name = str(args.get("name") or "").strip()
		action = str(args.get("action") or "").strip()
		if not doctype_in or not name or not action:
			return dumps(
				{"error": "doctype, name, and action are all required", "error_type": "invalid_argument"}
			)

		doctype, meta, err = load_meta(doctype_in)
		if err:
			return dumps(err)

		workflow_name = get_workflow_name(doctype)
		if not workflow_name:
			return dumps(
				{
					"error": f"No workflow configured for {doctype}. Use frappe_update_doc or frappe_submit_doc instead.",
					"error_type": "no_workflow",
					"doctype": doctype,
				}
			)

		doc, err = load_doc(doctype, name)
		if err:
			return dumps(err)
		name = doc.name

		# Read is enough here: Frappe's own apply_workflow enforces the transition
		# roles and, when it saves/submits, the write/submit permission. Requiring
		# "write" up front wrongly blocks approvers whose transition is a submit.
		doc.check_permission("read")

		state_field = frappe.db.get_value("Workflow", workflow_name, "workflow_state_field") or "workflow_state"
		original_state = doc.get(state_field)

		available = _list_transitions(doc)
		by_lower = {t["action"].lower(): t["action"] for t in available if t["action"]}
		exact = by_lower.get(action.lower())
		if not exact:
			payload = {
				"error": f"Action '{action}' is not available for document in state '{original_state}'",
				"error_type": "action_not_available",
				"current_state": original_state,
				"available_actions": [t["action"] for t in available],
			}
			if not available:
				payload["note"] = (
					"No actions are available to the current user in this state (role, condition "
					"or self-approval rules). Use frappe_get_workflow_info to inspect the workflow."
				)
			return dumps(payload)

		before_docstatus = doc.docstatus
		updated_doc = apply_workflow(doc, exact)
		frappe.db.commit()

		new_state = updated_doc.get(state_field)
		new_docstatus = updated_doc.docstatus

		changes = []
		if original_state != new_state:
			changes.append(f"State: {original_state} -> {new_state}")
		if before_docstatus != new_docstatus:
			labels = {0: "Draft", 1: "Submitted", 2: "Cancelled"}
			changes.append(f"Docstatus: {labels.get(before_docstatus)} -> {labels.get(new_docstatus)}")

		return dumps(
			{
				"doctype": doctype,
				"name": name,
				"status": "workflow_action_applied",
				"action": exact,
				"previous_state": original_state,
				"current_state": new_state,
				"docstatus": new_docstatus,
				"changes": changes,
				"next_available_actions": [t["action"] for t in _list_transitions(updated_doc)],
			}
		)

	except ArgError as e:
		return dumps(error_response(e, doctype=doctype, name=name))
	except Exception as e:
		# Workflow exceptions (WorkflowTransitionError etc.) are classified in _common.
		safe_rollback()
		return dumps(error_response(e, title="Run Workflow Error", doctype=doctype, name=name))
