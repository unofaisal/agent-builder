# tools/frappe_tools/document_update.py

import frappe

from agent_builder.mcp_tools._common import (
	NO_VALUE_TYPES,
	PROTECTED_ON_UPDATE,
	TABLE_TYPES,
	ArgError,
	apply_child_table_update,
	coerce_dict,
	diff_child_rows,
	diff_top_level,
	dumps,
	error_response,
	load_doc,
	load_meta,
	loads_loose,
	norm_args,
	safe_rollback,
	workflow_state_field,
)


def _child_doctype_response(doctype, name):
	info = {
		"error": (
			f"'{doctype}' is a child-table doctype and cannot be updated directly. "
			"Update the parent document instead and pass the child rows under the "
			"table fieldname in `data`."
		),
		"error_type": "child_doctype_direct_update",
		"child_doctype": doctype,
	}
	try:
		parent_name = frappe.db.get_value(doctype, name, "parent") if name else None
		parent_type = frappe.db.get_value(doctype, name, "parenttype") if name else None
		parent_field = frappe.db.get_value(doctype, name, "parentfield") if name else None
		if parent_name and parent_type and parent_field:
			info["parent_doctype"] = parent_type
			info["parent_name"] = parent_name
			info["parent_table_fieldname"] = parent_field
			info["suggestion"] = (
				f"Call frappe_update_doc with doctype='{parent_type}', name='{parent_name}', "
				f"data={{'{parent_field}': [{{'name': '{name}', ...fields to change...}}]}}"
			)
	except Exception:
		pass
	return info


def frappe_update_doc(args: dict, **kwargs) -> str:
	"""Update an existing Frappe document."""
	doctype = name = None
	try:
		args = norm_args(args, kwargs)
		doctype_in = str(args.get("doctype") or "").strip()
		name = str(args.get("name") or "").strip()
		data = coerce_dict(args.get("data"), "data")

		if not doctype_in:
			return dumps({"error": "doctype is required", "error_type": "invalid_argument"})

		doctype, meta, err = load_meta(doctype_in)
		if err:
			return dumps(err)

		# Saving a child row alone skips the parent's validate() pipeline, so
		# derived fields (row amount, grand_total, ...) never recompute.
		if meta.istable:
			return dumps(_child_doctype_response(doctype, name))

		if meta.issingle:
			name = doctype
		elif not name:
			return dumps(
				{"error": "Both doctype and name are required", "error_type": "invalid_argument"}
			)

		if not data:
			return dumps(
				{
					"error": "data is empty; nothing to update",
					"error_type": "invalid_argument",
					"doctype": doctype,
					"name": name,
				}
			)

		doc, err = load_doc(doctype, name)
		if err:
			return dumps(err)
		name = doc.name
		doc.check_permission("write")

		if doc.docstatus == 2:
			return dumps(
				{
					"error": f"Cannot modify cancelled document {doctype} '{name}'. Cancelled documents are read-only.",
					"error_type": "document_cancelled",
					"docstatus": 2,
				}
			)

		table_fields = {f.fieldname: f.options for f in meta.fields if f.fieldtype in TABLE_TYPES}
		protected = set(PROTECTED_ON_UPDATE)
		wf_field = workflow_state_field(doctype)
		if wf_field:
			protected.add(wf_field)

		applied, ignored = [], []
		sent_scalars, sent_tables = {}, {}

		for field, value in data.items():
			if field in protected:
				ignored.append({"field": field, "reason": "protected; cannot be set directly"})
				continue
			if field in table_fields:
				err, mode, cleaned = apply_child_table_update(doc, field, table_fields[field], value)
				if err is not None:
					safe_rollback()
					err.update({"doctype": doctype, "name": name})
					return dumps(err)
				sent_tables[field] = (mode, cleaned)
				applied.append(field)
				continue

			df = meta.get_field(field)
			if df is None:
				ignored.append({"field": field, "reason": f"not a field of {doctype}"})
				continue
			if df.fieldtype in NO_VALUE_TYPES or df.fieldtype == "Read Only" or df.read_only:
				ignored.append({"field": field, "reason": "read-only / layout field"})
				continue

			if isinstance(value, str) and value[:1] in "[{":
				value = loads_loose(value)
			doc.set(field, value)
			sent_scalars[field] = value
			applied.append(field)

		if not applied:
			return dumps(
				{
					"error": "None of the provided fields could be updated.",
					"error_type": "no_updatable_fields",
					"doctype": doctype,
					"name": name,
					"ignored_fields": ignored,
				}
			)

		doc.save()
		doc.reload()

		warnings = diff_top_level(doc, sent_scalars, meta)
		for field, (mode, rows) in sent_tables.items():
			warnings.extend(diff_child_rows(doc, field, rows, mode=mode))

		result = {
			"name": doc.name,
			"doctype": doctype,
			"status": "saved",
			"docstatus": doc.docstatus,
			"modified": str(doc.modified),
			"updated_fields": applied,
		}
		if ignored:
			result["ignored_fields"] = ignored
		if warnings:
			result["warnings"] = warnings

		frappe.db.commit()
		return dumps(result)

	except ArgError as e:
		safe_rollback()
		return dumps(error_response(e, doctype=doctype, name=name))
	except Exception as e:
		# No commit happens on failure, but the request layer commits on HTTP 200,
		# so roll back explicitly or a half-applied save could persist.
		safe_rollback()
		return dumps(error_response(e, title="Document Update Error", doctype=doctype, name=name))
