# tools/frappe_tools/document_submit.py

import frappe
from frappe.model.workflow import get_workflow_name

from agent_builder.mcp_tools._common import (
	ArgError,
	dumps,
	error_response,
	load_doc,
	load_meta,
	norm_args,
	safe_rollback,
)


def frappe_submit_doc(args: dict, **kwargs) -> str:
	"""Submit a draft document. Only works on documents in draft state (docstatus=0)."""
	doctype = name = None
	try:
		args = norm_args(args, kwargs)
		doctype_in = str(args.get("doctype") or "").strip()
		name = str(args.get("name") or "").strip()
		if not doctype_in or not name:
			return dumps({"error": "Both doctype and name are required", "error_type": "invalid_argument"})

		doctype, meta, err = load_meta(doctype_in)
		if err:
			return dumps(err)
		if not meta.is_submittable:
			return dumps(
				{
					"error": f"{doctype} is not a submittable DocType",
					"error_type": "not_submittable",
					"doctype": doctype,
				}
			)

		doc, err = load_doc(doctype, name)
		if err:
			return dumps(err)
		name = doc.name
		doc.check_permission("submit")

		if doc.docstatus != 0:
			state = {1: "submitted", 2: "cancelled"}.get(doc.docstatus, "unknown")
			return dumps(
				{
					"error": f"Cannot submit {state} document {doctype} '{name}'. Only draft documents can be submitted.",
					"error_type": "invalid_docstatus",
					"docstatus": doc.docstatus,
					"doctype": doctype,
					"name": name,
				}
			)

		doc.submit()
		frappe.db.commit()
		doc.reload()

		return dumps(
			{
				"name": doc.name,
				"doctype": doctype,
				"docstatus": doc.docstatus,
				"status": "submitted",
				"modified": str(doc.modified),
			}
		)

	except ArgError as e:
		return dumps(error_response(e, doctype=doctype, name=name))
	except Exception as e:
		safe_rollback()
		payload = error_response(e, title="Document Submit Error", doctype=doctype, name=name)
		try:
			if doctype and get_workflow_name(doctype):
				payload["hint"] = (
					f"{doctype} has an active workflow; submission usually happens through a "
					"transition. Use frappe_get_workflow_info, then frappe_run_workflow."
				)
		except Exception:
			pass
		return dumps(payload)
