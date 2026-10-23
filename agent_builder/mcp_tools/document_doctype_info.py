# tools/frappe_tools/document_doctype_info.py

import frappe

from agent_builder.mcp_tools._common import (
	NO_VALUE_TYPES,
	ArgError,
	dumps,
	error_response,
	load_meta,
	norm_args,
	permitted_fieldnames,
	workflow_state_field,
)


def _serialize_field(field) -> dict:
	out = {
		"fieldname": field.fieldname,
		"label": field.label,
		"fieldtype": field.fieldtype,
		"options": field.options,
		"reqd": field.reqd,
		"read_only": field.read_only,
		"hidden": field.hidden,
		"default": field.default,
		"description": field.description,
	}
	if field.fieldtype == "Select" and field.options:
		out["select_options"] = [o for o in str(field.options).split("\n") if o.strip()]
	if getattr(field, "permlevel", 0):
		out["permlevel"] = field.permlevel
	if getattr(field, "allow_on_submit", 0):
		out["allow_on_submit"] = 1
	if getattr(field, "fetch_from", None):
		out["fetch_from"] = field.fetch_from
	return out


def _usable_fields(meta, permitted):
	"""Skip layout-only fields (Section/Column Breaks etc.) and fields the user
	cannot read; both are noise or leaks for an agent building payloads."""
	for f in meta.fields:
		if not f.fieldname or f.fieldtype in NO_VALUE_TYPES:
			continue
		if permitted is not None and f.fieldname not in permitted:
			continue
		yield f


def frappe_get_doctype_info(args: dict, **kwargs) -> str:
	"""Get DocType metadata: fields, child tables, link fields, permissions."""
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

		permitted = permitted_fieldnames(doctype)
		fields = [_serialize_field(f) for f in _usable_fields(meta, permitted)]
		usable_names = {f["fieldname"] for f in fields}

		link_fields = [
			{"fieldname": f.fieldname, "label": f.label, "options": f.options}
			for f in meta.get_link_fields()
			if f.fieldname in usable_names
		]

		# Include each child table's own field metadata inline so nested row
		# payloads can be built without a second lookup.
		child_tables = []
		for table_field in meta.get_table_fields():
			if table_field.fieldname not in usable_names:
				continue
			child_doctype = table_field.options
			entry = {
				"fieldname": table_field.fieldname,
				"label": table_field.label,
				"options": child_doctype,
				"reqd": table_field.reqd,
				"fields": [],
			}
			if child_doctype and frappe.db.exists("DocType", child_doctype):
				child_meta = frappe.get_meta(child_doctype)
				child_permitted = permitted_fieldnames(child_doctype, parenttype=doctype)
				entry["fields"] = [
					_serialize_field(f) for f in _usable_fields(child_meta, child_permitted)
				]
			child_tables.append(entry)

		your_permissions = {}
		for ptype in ("read", "write", "create", "delete", "submit", "cancel"):
			try:
				your_permissions[ptype] = bool(frappe.has_permission(doctype, ptype))
			except Exception:
				your_permissions[ptype] = None

		return dumps(
			{
				"doctype": doctype,
				"module": meta.module,
				"is_submittable": bool(meta.is_submittable),
				"is_tree": bool(meta.is_tree),
				"is_single": bool(meta.issingle),
				"is_child_table": bool(meta.istable),
				"naming_rule": meta.naming_rule,
				"autoname": meta.autoname,
				"title_field": meta.title_field,
				"search_fields": getattr(meta, "search_fields", None),
				"workflow_state_field": workflow_state_field(doctype),
				"your_permissions": your_permissions,
				"fields": fields,
				"link_fields": link_fields,
				"child_tables": child_tables,
			}
		)

	except ArgError as e:
		return dumps(error_response(e, doctype=doctype))
	except Exception as e:
		return dumps(error_response(e, title="Get DocType Info Error", doctype=doctype))
