# tools/frappe_tools/document_create.py

import frappe
from frappe.model.workflow import get_workflow_name

from agent_builder.mcp_tools._common import (
	BLOCKED_FIELDS,
	ArgError,
	clean_message,
	coerce_bool,
	coerce_dict,
	diff_child_rows,
	diff_top_level,
	dumps,
	error_response,
	load_meta,
	loads_loose,
	norm_args,
	normalize_rows,
	safe_rollback,
	snapshot_rows,
	table_fields_of,
	valid_fieldnames,
	workflow_state_field,
)

# Blocked so an LLM payload can't bypass lifecycle semantics: docstatus only
# moves via submit(), workflow_state only via a workflow transition,
# amended_from only via amend(), the rest is Frappe bookkeeping.


def frappe_create_doc(args: dict, **kwargs) -> str:
	"""Create a new Frappe document."""
	doctype = None
	try:
		args = norm_args(args, kwargs)
		data = coerce_dict(args.get("doc"), "doc")
		doctype_in = str(data.get("doctype") or "").strip()
		if not doctype_in:
			return dumps({"error": "doc.doctype is required", "error_type": "invalid_argument"})

		doctype, meta, err = load_meta(doctype_in)
		if err:
			return dumps(err)
		if meta.istable:
			return dumps(
				{
					"error": (
						f"'{doctype}' is a child-table doctype and cannot be created on its own. "
						"Create or update the parent document and pass rows under its table field."
					),
					"error_type": "child_doctype_direct_create",
					"doctype": doctype,
				}
			)
		if meta.issingle:
			return dumps(
				{
					"error": f"'{doctype}' is a Single doctype; use frappe_update_doc to change its values.",
					"error_type": "single_doctype",
					"doctype": doctype,
				}
			)

		submit = coerce_bool(args.get("submit"), False)
		validate_only = coerce_bool(args.get("validate_only"), False)

		frappe.has_permission(doctype, "create", throw=True)

		requested_name = data.get("name")
		if requested_name and frappe.db.exists(doctype, requested_name):
			return dumps(
				{
					"error": f"{doctype} '{requested_name}' already exists. Use frappe_update_doc to change it.",
					"error_type": "already_exists",
					"doctype": doctype,
					"name": requested_name,
				}
			)

		table_fields = table_fields_of(meta)
		valid = valid_fieldnames(meta)
		blocked = set(BLOCKED_FIELDS)
		wf_field = workflow_state_field(doctype)
		if wf_field:
			blocked.add(wf_field)

		payload = {"doctype": doctype}
		sent_tables = {}
		ignored = []
		for field, value in data.items():
			if field == "doctype":
				continue
			if field in blocked:
				ignored.append({"field": field, "reason": "protected; cannot be set directly"})
				continue
			if field in table_fields:
				rows = normalize_rows(field, value)
				payload[field] = rows
				sent_tables[field] = snapshot_rows(rows)
			elif field in valid or field.startswith("__"):
				payload[field] = loads_loose(value) if isinstance(value, str) and value[:1] in "[{" else value
			else:
				ignored.append({"field": field, "reason": f"not a field of {doctype}"})

		sent_scalars = {
			k: v for k, v in payload.items() if k not in sent_tables and k != "doctype" and k != "name"
		}

		doc = frappe.get_doc(payload)

		# Required-field checks are left to Frappe's own pipeline: many "reqd"
		# fields are only populated by set_missing_values() during validate().

		if validate_only:
			# run_method("validate") alone skips mandatory / link / unique checks and
			# reports false passes. A full insert inside a savepoint that is always
			# rolled back gives the real answer.
			save_point = "agent_validate_only"
			frappe.db.savepoint(save_point)
			try:
				doc.insert(ignore_permissions=False)
				would_be_name = doc.name
			finally:
				safe_rollback(save_point)
			result = {
				"status": "validated",
				"doctype": doctype,
				"would_be_name": would_be_name,
				"message": f"{doctype} data validation passed (nothing was saved)",
				"child_tables": list(table_fields.keys()),
				"next_step": "Call frappe_create_doc again with validate_only omitted to actually create it",
			}
			if ignored:
				result["ignored_fields"] = ignored
			return dumps(result)

		doc.insert(ignore_permissions=False)

		warnings = diff_top_level(doc, sent_scalars, meta)
		for field, rows in sent_tables.items():
			warnings.extend(diff_child_rows(doc, field, rows, mode="replace"))

		result = {
			"name": doc.name,
			"doctype": doctype,
			"docstatus": doc.docstatus,
			"status": "created",
			"submitted": False,
		}
		if ignored:
			result["ignored_fields"] = ignored

		if submit and doc.docstatus == 0:
			if not meta.is_submittable:
				result["submit_error"] = f"{doctype} is not submittable; saved as draft."
			elif get_workflow_name(doctype):
				result["submit_error"] = (
					f"{doctype} has an active workflow; saved as draft. "
					"Use frappe_get_workflow_info and frappe_run_workflow to move it forward."
				)
			else:
				save_point = "agent_pre_submit"
				frappe.db.savepoint(save_point)
				try:
					doc.check_permission("submit")
					doc.submit()
					result["submitted"] = True
					result["docstatus"] = 1
					result["status"] = "created_and_submitted"
				except frappe.PermissionError:
					safe_rollback(save_point)
					result["submit_error"] = "No permission to submit this document; saved as draft."
				except Exception as e:
					safe_rollback(save_point)
					result["submit_error"] = f"Created as draft. Submit failed: {clean_message(e)}"

		frappe.db.commit()

		if warnings:
			result["warnings"] = warnings
		return dumps(result)

	except ArgError as e:
		safe_rollback()
		return dumps(error_response(e, doctype=doctype))
	except Exception as e:
		safe_rollback()
		return dumps(
			error_response(e, title="Document Create Error", doctype=doctype, provided_fields=_keys(args))
		)


def _keys(args):
	try:
		return list(coerce_dict(args.get("doc")).keys())
	except Exception:
		return None
