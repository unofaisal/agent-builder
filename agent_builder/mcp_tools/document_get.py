# tools/frappe_tools/document_get.py

import frappe

from agent_builder.mcp_tools._common import (
	ArgError,
	dumps,
	error_response,
	load_doc,
	load_meta,
	norm_args,
)


def frappe_get_doc(args: dict, **kwargs) -> str:
	"""Retrieve a specific Frappe document by doctype and name."""
	doctype = name = None
	try:
		args = norm_args(args, kwargs)
		doctype_in = str(args.get("doctype") or "").strip()
		name = str(args.get("name") or "").strip()
		if not doctype_in:
			return dumps({"error": "doctype is required", "error_type": "invalid_argument"})

		doctype, meta, err = load_meta(doctype_in)
		if err:
			return dumps(err)

		if meta.issingle:
			name = doctype  # Single doctypes have exactly one record named after the doctype
		elif not name:
			return dumps(
				{"error": "name is required", "error_type": "invalid_argument", "doctype": doctype}
			)

		doc, err = load_doc(doctype, name)
		if err:
			return dumps(err)

		doc.check_permission("read")
		# Drop fields above the user's permlevel (as_dict() alone would leak them).
		doc.apply_fieldlevel_read_permissions()

		return dumps({"doctype": doctype, "name": doc.name, "data": doc.as_dict()})

	except ArgError as e:
		return dumps(error_response(e, doctype=doctype))
	except Exception as e:
		return dumps(error_response(e, title="Document Get Error", doctype=doctype, name=name))
