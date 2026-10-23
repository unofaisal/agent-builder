# tools/frappe_tools/document_report_list.py

"""List the Frappe reports the current user can actually run."""

import frappe

from agent_builder.mcp_tools._common import (
	ArgError,
	coerce_bool,
	coerce_int,
	dumps,
	error_response,
	norm_args,
)

_RUNNABLE_TYPES = ("Query Report", "Script Report", "Custom Report")


def frappe_list_reports(args: dict, **kwargs) -> str:
	"""List available Frappe reports, optionally filtered by module, report type, or name substring."""
	try:
		args = norm_args(args, kwargs)
		module = args.get("module")
		report_type = args.get("report_type")
		name_contains = args.get("name_contains")
		ref_doctype = args.get("ref_doctype")
		include_disabled = coerce_bool(args.get("include_disabled"), False)
		limit = coerce_int(args.get("limit"), 100, 1, 500)

		filters = {}
		if module:
			filters["module"] = module
		if report_type:
			filters["report_type"] = report_type
		if ref_doctype:
			filters["ref_doctype"] = ref_doctype
		if name_contains:
			filters["report_name"] = ["like", f"%{name_contains}%"]
		if not include_disabled:
			filters["disabled"] = 0

		# Frappe decides who can *run* a report via get_report_doc(): the report's
		# role list plus "report" permission on its reference doctype. Listing with
		# get_list("Report") would apply the Report doctype's own permissions, which
		# is a different (often narrower) rule, so mirror the run-time rule instead.
		reports = frappe.get_all(
			"Report",
			filters=filters,
			fields=[
				"name",
				"report_name",
				"report_type",
				"module",
				"ref_doctype",
				"reference_report",
				"is_standard",
				"disabled",
			],
			order_by="report_name",
		)

		if reports:
			# Custom Reports inherit roles/ref_doctype from their reference report.
			base_names = {r.reference_report or r.name for r in reports}
			role_rows = frappe.get_all(
				"Has Role",
				filters={"parenttype": "Report", "parent": ["in", list(base_names)]},
				fields=["parent", "role"],
			)
			allowed_roles = {}
			for row in role_rows:
				allowed_roles.setdefault(row.parent, set()).add(row.role)

			base_ref = {}
			missing = [n for n in base_names if n not in {r.name for r in reports}]
			if missing:
				for row in frappe.get_all(
					"Report", filters={"name": ["in", missing]}, fields=["name", "ref_doctype"]
				):
					base_ref[row.name] = row.ref_doctype
			for r in reports:
				base_ref.setdefault(r.name, r.ref_doctype)

			my_roles = set(frappe.get_roles())
			ref_ok = {}

			def can_report_on(dt):
				if not dt:
					return True
				if dt not in ref_ok:
					try:
						ref_ok[dt] = bool(frappe.has_permission(dt, "report"))
					except Exception:
						ref_ok[dt] = False
				return ref_ok[dt]

			permitted = []
			for r in reports:
				base = r.reference_report or r.name
				roles = allowed_roles.get(base)
				if roles and not (roles & my_roles):
					continue
				if not can_report_on(base_ref.get(base) or r.ref_doctype):
					continue
				permitted.append(r)
			reports = permitted

		total = len(reports)
		reports = reports[:limit]
		out = [
			{
				"name": r.name,
				"report_name": r.report_name,
				"report_type": r.report_type,
				"module": r.module,
				"ref_doctype": r.ref_doctype,
				"is_standard": r.is_standard,
				"disabled": r.disabled,
				"runnable": r.report_type in _RUNNABLE_TYPES,
			}
			for r in reports
		]

		result = {
			"reports": out,
			"count": len(out),
			"total_count": total,
			"has_more": total > len(out),
			"filters_applied": {k: v for k, v in filters.items()},
		}
		if any(not r["runnable"] for r in out):
			result["note"] = (
				"runnable=false (Report Builder) reports cannot be run by frappe_generate_report; "
				"query their ref_doctype with frappe_get_list or frappe_query instead."
			)
		return dumps(result)

	except ArgError as e:
		return dumps(error_response(e))
	except Exception as e:
		return dumps(error_response(e, title="List Reports Error"))
