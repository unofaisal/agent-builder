# tools/frappe_tools/document_delete.py
#
# INTENTIONALLY DISABLED: the @tool line below stays commented out. If you enable it,
# also uncomment FRAPPE_DELETE_DOC in schema.py and expose it as destructive over MCP.

import frappe

from agent_builder.mcp_tools._common import (
	ArgError,
	coerce_bool,
	dumps,
	error_response,
	load_doc,
	load_meta,
	norm_args,
	safe_rollback,
)


# @tool(schema_name="frappe_delete_doc")
def frappe_delete_doc(args: dict, **kwargs) -> str:
	"""Delete an existing Frappe document (draft or cancelled only)."""
	doctype = name = None
	try:
		args = norm_args(args, kwargs)
		doctype_in = str(args.get("doctype") or "").strip()
		name = str(args.get("name") or "").strip()
		force = coerce_bool(args.get("force"), False)

		if not doctype_in or not name:
			return dumps({"error": "Both doctype and name are required", "error_type": "invalid_argument"})

		doctype, meta, err = load_meta(doctype_in)
		if err:
			return dumps(err)
		if meta.istable or meta.issingle:
			return dumps(
				{
					"error": f"'{doctype}' is a child-table or Single doctype and cannot be deleted directly.",
					"error_type": "not_deletable",
					"doctype": doctype,
				}
			)

		doc, err = load_doc(doctype, name)
		if err:
			return dumps(err)
		name = doc.name
		doc.check_permission("delete")

		if doc.docstatus == 1:
			return dumps(
				{
					"error": f"{doctype} '{name}' is submitted. Cancel it first; submitted documents cannot be deleted.",
					"error_type": "invalid_docstatus",
					"docstatus": 1,
					"doctype": doctype,
					"name": name,
				}
			)

		frappe.delete_doc(doctype, name, force=force, ignore_permissions=False)
		frappe.db.commit()
		return dumps({"name": name, "doctype": doctype, "status": "deleted"})

	except ArgError as e:
		return dumps(error_response(e, doctype=doctype, name=name))
	except Exception as e:
		safe_rollback()
		payload = error_response(e, title="Document Delete Error", doctype=doctype, name=name)
		if payload.get("error_type") == "link_exists":
			payload["hint"] = "It is linked to other documents. Remove those links first."
		return dumps(payload)
