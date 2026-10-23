# tools/frappe_tools/generate_report.py

import hashlib
import inspect
import json
import numbers
import time

import frappe
from frappe.desk.query_report import get_report_doc
from frappe.utils import add_months, getdate

from agent_builder.mcp_tools._common import (
	clean_message,
	error_response,
	get_cache as _cache,
	norm_args,
)

# A previous version of this tool hardcoded "Sales Analytics"/"Purchase Analytics"
# as permanently unrunnable, reasoning that ERPNext dumps raw data to the browser
# for client-side pivoting. That diagnosis was WRONG — verified against ERPNext's
# actual source (erpnext/selling/report/sales_analytics/sales_analytics.py):
# Analytics.run() returns a perfectly normal 6-tuple
# (columns, data, message, chart, report_summary, skip_total_row).
# The real bug: every live failure supplied 'based_on' (not a real field on this
# report) instead of the two actually-mandatory filters, 'tree_type' and
# 'doc_type' (per the report's own .js: reqd=1, options include
# Customer/Item/Territory/... and Sales Invoice/Quotation/...). When tree_type
# doesn't match one of get_data()'s known branches, self.data is never assigned,
# producing "'Analytics' object has no attribute 'data'" — a missing/invalid
# filter value, not a structural incompatibility. Purchase Analytics imports and
# reuses this exact same Analytics class, so the same fix applies to both.
# Lesson generalized below: instead of guessing or hardcoding filter names/options
# per report, read them from the report's own client-side .js filter definitions
# (frappe.query_reports[name] = {filters: [...]}), which every Query and Script
# Report ships. This fixes the *class* of bug, not just these two reports.

_RETRY_CACHE_PREFIX = "frappe_generate_report:last_call:"
_RETRY_CACHE_TTL = 90  # seconds

# --- Large-result handling -------------------------------------------------
# Reports vary wildly in row count (some return ~10, some return 10k+), and
# agent queries against them fall into a few recurring shapes:
#   - "what's the total/average of X"        -> report_summary / aggregates
#   - "top N by X"                           -> sort_by + row_limit
#   - "break down X by Y"                    -> group_by
#   - "find the row(s) where X"              -> row_filter
#   - "just give me everything"              -> CSV export, not 40 chat pages
# The full (unfiltered/unsorted/ungrouped) result is cached per (user, report,
# business-filters) so any of the above re-queries against the SAME underlying
# report run reuse the cache instead of re-executing a potentially expensive
# report each time.
_ROW_LIMIT_DEFAULT = 250
_ROW_LIMIT_MAX = 2000  # hard ceiling even if the caller asks for more
_RESULT_CACHE_PREFIX = "frappe_generate_report:result:"
_RESULT_CACHE_TTL = 600  # 10 minutes — enough for a few follow-up re-queries

# Above this many (post-filter, ungrouped) rows, with no sort/filter narrowing
# already applied, hand back a CSV download instead of forcing many
# pagination round-trips through the chat.
_EXPORT_ROW_THRESHOLD = 3000
# CSV export writes a *private* File that an MCP/API client cannot download with
# its bearer token, and creates a File record per call. Off by default.
_EXPORT_CSV_ENABLED = False
_RESULT_CACHE_MAX_ROWS = 20000  # don't park huge results in Redis
_FINANCIAL_STATEMENT_REPORTS = {
	"Profit and Loss Statement",
	"Balance Sheet",
	"Gross and Net Profit Report",
	"Cash Flow",
	"Trial Balance",
}
_FISCAL_YEAR_FILTER_KEYS = {"from_fiscal_year", "to_fiscal_year", "fiscal_year"}
_TRANSIENT_ERROR_NAMES = {
	"QueryTimeoutError",
	"QueryDeadlockError",
	"TimeoutError",
	"TimeLimitExceeded",
	"JobTimeoutException",
}
_MISSING = object()
_MAX_GROUPS_RETURNED = 200

# fieldtypes worth summing/averaging when building aggregates/group sums
_NUMERIC_FIELDTYPES = {"Currency", "Float", "Int", "Percent", "Duration"}
_ROW_FILTER_OPS = {"eq", "neq", "contains", "gt", "gte", "lt", "lte"}
_SORT_ORDERS = {"asc", "desc"}
_GROUP_AGGS = {"sum", "avg", "count"}

# Fallback Link-type filter keys worth checking for existence, used only when a
# report doesn't declare its own Link filters in a way we can parse (e.g. its
# filters live in server-side Python rather than the standard .js block).
_FALLBACK_LINK_FILTER_DOCTYPES = {
	"company": "Company",
	"customer": "Customer",
	"supplier": "Supplier",
	"item": "Item",
	"item_code": "Item",
	"project": "Project",
	"cost_center": "Cost Center",
	"warehouse": "Warehouse",
}

_DATE_FILTER_KEYS = (
	"from_date",
	"to_date",
	"period_start_date",
	"period_end_date",
	"posting_date",
	"transaction_date",
)

# Kept as a last-resort safety net for a report whose execute() still ends up in
# an inconsistent internal state (e.g. an unrecognized filter value skipped every
# branch of its own logic) even after defaults/validation above. This is no
# longer treated as "unfixable" — the message below reflects the real cause.
_INCOMPLETE_STATE_ERROR_SIGNATURES = (
	"object has no attribute 'data'",
	"object has no attribute 'chart'",
	"object has no attribute 'report_summary'",
)


def _split_top_level_objects(text: str) -> list:
	"""Split a JS array-literal's inner text into its top-level {...} object
	bodies, respecting brace nesting (a filter def's 'options: [{...}, {...}]'
	contains braces of its own, so a naive non-nesting regex grabs the wrong
	object — this is what silently broke default/option extraction before)."""
	objs, depth, start = [], 0, None
	for i, ch in enumerate(text):
		if ch == "{":
			if depth == 0:
				start = i
			depth += 1
		elif ch == "}":
			depth -= 1
			if depth == 0 and start is not None:
				objs.append(text[start + 1 : i])
				start = None
	return objs


def _read_report_filter_defs(report_doc) -> dict:
	"""Read a report's own client-side .js filter declarations and return
	{fieldname: {"default": str|None, "options": [str, ...]|None, "reqd": bool}}
	for whatever it declares. Works for both Query and Script Reports — both
	ship a frappe.query_reports[name] = {filters: [...]} block. Best-effort
	only: returns {} on any failure or if no .js file is found, and must never
	block report execution."""
	import os
	import re

	defs = {}
	try:
		report_folder = report_doc.name.lower().replace(" ", "_").replace("-", "_")
		module_folder = (report_doc.module or "").lower().replace(" ", "_")

		for app in frappe.get_installed_apps():
			js_path = os.path.join(
				frappe.get_app_path(app), module_folder, "report", report_folder, f"{report_folder}.js"
			)
			# nosemgrep: frappe-security-file-traversal — path built from frappe.get_app_path
			# + report metadata (module/name from the DB), not from user-supplied input.
			if not os.path.exists(js_path):
				continue

			with open(js_path, encoding="utf-8") as f:
				js_content = f.read()

			filters_start = js_content.find("filters:")
			bracket_start = js_content.find("[", filters_start) if filters_start != -1 else -1
			if bracket_start == -1:
				break

			depth, bracket_end = 0, -1
			for i in range(bracket_start, len(js_content)):
				if js_content[i] == "[":
					depth += 1
				elif js_content[i] == "]":
					depth -= 1
					if depth == 0:
						bracket_end = i
						break
			if bracket_end == -1:
				break

			for obj in _split_top_level_objects(js_content[bracket_start + 1 : bracket_end]):
				fieldname_match = re.search(r'fieldname:\s*["\']([^"\']+)["\']', obj)
				if not fieldname_match:
					continue
				entry = {"default": None, "options": None, "reqd": bool(re.search(r"reqd:\s*1", obj))}

				default_match = re.search(r'default:\s*["\']([^"\']+)["\']', obj)
				if default_match:
					entry["default"] = default_match.group(1)

				# Only treat 'options' as a Select's choice list when it's an array —
				# for Link fields 'options' is a bare doctype string (e.g. "Company"),
				# which this intentionally skips.
				options_match = re.search(r"options:\s*\[(.*?)\]", obj, re.S)
				if options_match:
					seen, opts = set(), []
					for q in re.findall(r'["\']([^"\']+)["\']', options_match.group(1)):
						if q not in seen:
							seen.add(q)
							opts.append(q)
					if opts:
						entry["options"] = opts

				defs[fieldname_match.group(1)] = entry
			break
	except Exception as e:
		frappe.log_error(
			title="Generate Report Error",
			message=f"Filter def extraction failed for {report_doc.name}: {e!s}",
		)

	return defs


def _apply_declared_defaults(filters: dict, filter_defs: dict) -> dict:
	"""Fill in any filter the report itself declares a string-literal default
	for, if it's still missing. (Defaults that are JS expressions rather than
	string literals — e.g. computed dates — aren't parsed here; _default_filters
	already covers dates/company via fiscal year and Global Defaults.)"""
	for fieldname, meta in filter_defs.items():
		if filters.get(fieldname) is None and meta.get("default") is not None:
			filters[fieldname] = meta["default"]
	return filters


def _validate_filter_values(filters: dict, filter_defs: dict = None):
	"""Catch bad Link references, invalid Select options, and unparseable dates
	before spending a report execution on them. Select-field validation prefers
	options the report itself declares (filter_defs) over any static guess,
	since valid option sets are report-specific and drift across versions.
	Returns None if clean, otherwise an error payload with concrete suggestions."""
	filter_defs = filter_defs or {}
	errors = []

	for key, doctype in _FALLBACK_LINK_FILTER_DOCTYPES.items():
		value = filters.get(key)
		if not value or isinstance(value, list):
			continue
		if not frappe.db.exists(doctype, value):
			similar = frappe.get_all(
				doctype, filters={"name": ["like", f"%{value}%"]}, fields=["name"], limit=3
			)
			suggestion = (
				f"Did you mean: {', '.join(s.name for s in similar)}?"
				if similar
				else f"Valid {doctype} examples: "
				f"{', '.join(v.name for v in frappe.get_all(doctype, fields=['name'], limit=5))}"
			)
			errors.append(f"Invalid {key}='{value}': no such {doctype}. {suggestion}")

	for fieldname, meta in filter_defs.items():
		options = meta.get("options")
		value = filters.get(fieldname)
		if options and value and value not in options:
			errors.append(f"Invalid {fieldname}='{value}'. Must be one of: {', '.join(options)}")

	missing_mandatory = [
		fn for fn, meta in filter_defs.items() if meta.get("reqd") and filters.get(fn) is None
	]
	if missing_mandatory:
		errors.append(
			f"Missing mandatory filter(s): {', '.join(missing_mandatory)}. "
			f"This report marks them reqd=1 in its own filter definition."
		)

	for key in _DATE_FILTER_KEYS:
		value = filters.get(key)
		if value:
			try:
				getdate(value)
			except Exception:
				errors.append(f"Invalid {key}='{value}'. Expected format: YYYY-MM-DD")

	if not errors:
		return None
	return {
		"error": "invalid_filter_values",
		"validation_errors": errors,
		"message": "Fix the filter values above and retry — the report was not executed.",
	}


def _default_filters(filters: dict, filter_defs: dict = None, report_name: str = None) -> dict:
	"""Fill in sensible defaults, but only for filters the report can actually use.

	Injecting period_*/periodicity/filter_based_on into every report makes script
	reports that turn each filter into a SQL condition fail on unknown columns.
	Financial statements get the full set; any other report only gets keys it
	declares (parsed from its own .js); with nothing known, only 'company'.
	"""
	filters = {k: v for k, v in filters.items() if v is not None}
	declared = set(filter_defs or {})
	is_financial = report_name in _FINANCIAL_STATEMENT_REPORTS

	def wants(key):
		return is_financial or key in declared

	# ERPNext's shared financial_statements.py (P&L, Balance Sheet, Cash Flow,
	# Gross and Net Profit, etc.) reads period_start_date/period_end_date and
	# from_fiscal_year/to_fiscal_year, NOT from_date/to_date/fiscal_year.
	if wants("period_start_date") and not filters.get("period_start_date") and filters.get("from_date"):
		filters["period_start_date"] = filters["from_date"]
	if wants("period_end_date") and not filters.get("period_end_date") and filters.get("to_date"):
		filters["period_end_date"] = filters["to_date"]

	if (wants("period_start_date") or wants("period_end_date")) and not filters.get(
		"period_start_date"
	) and not filters.get("period_end_date"):
		fy = frappe.db.get_value(
			"Fiscal Year",
			{"disabled": 0},
			["name", "year_start_date", "year_end_date"],
			order_by="year_start_date desc",
		)
		if fy:
			filters["period_start_date"] = str(fy[1])
			filters["period_end_date"] = str(fy[2])
			if wants("from_fiscal_year"):
				filters.setdefault("from_fiscal_year", fy[0])
			if wants("to_fiscal_year"):
				filters.setdefault("to_fiscal_year", fy[0])
		else:
			today = getdate()
			filters["period_end_date"] = str(today)
			filters["period_start_date"] = str(add_months(today, -12))

	if wants("filter_based_on"):
		filters.setdefault("filter_based_on", "Date Range")
	if wants("periodicity"):
		filters.setdefault("periodicity", "Yearly")

	if "company" not in filters and (wants("company") or not declared):
		default_company = frappe.db.get_single_value("Global Defaults", "default_company")
		if default_company:
			filters["company"] = default_company

	return filters


def _validate_fiscal_year_coverage(filters: dict):
	"""If a company + explicit date range is given but no active Fiscal Year for that
	company covers it, return an actionable error payload instead of letting the
	report raise an opaque 'Date X is not in any active Fiscal Year' exception.
	Confirmed live failure: company='My Company', from_date=2026-01-01 had no
	Fiscal Year record covering it, even though the *global* default Fiscal Year
	(used by _default_filters as a fallback) did exist."""
	company = filters.get("company")
	start = filters.get("period_start_date") or filters.get("from_date")
	end = filters.get("period_end_date") or filters.get("to_date")
	if not (company and start and end):
		return None

	covering = frappe.db.exists(
		"Fiscal Year",
		{"disabled": 0, "year_start_date": ("<=", end), "year_end_date": (">=", start)},
	)
	if covering:
		return None

	available = frappe.db.get_all(
		"Fiscal Year",
		filters={"disabled": 0},
		fields=["name", "year_start_date", "year_end_date"],
		order_by="year_start_date desc",
		limit=5,
	)
	return {
		"error": "no_fiscal_year_for_range",
		"company": company,
		"requested_range": [str(start), str(end)],
		"available_fiscal_years": [
			{"name": fy.name, "start": str(fy.year_start_date), "end": str(fy.year_end_date)}
			for fy in available
		],
		"message": (
			f"No active Fiscal Year covers {start} to {end} for company '{company}'. "
			f"This is a configuration/date issue, not a filter-shape problem — do not "
			f"retry with the same dates. Pick a date range within one of the "
			f"available_fiscal_years above, or ask the user to confirm the correct period."
		),
	}


def _run_prepared_or_direct(report_doc, filters: dict, run_name: str = None, max_wait: int = 120) -> dict:
	"""Check for a cached prepared report, otherwise execute synchronously.

	NOTE: We bypass `make_prepared_report` (which enqueues a background job)
	because this tool may run inside a worker where a nested job can deadlock.
	`run_name` is the report the caller asked for (a Custom Report resolves to a
	different base `report_doc`, but run() must be given the requested name).
	"""
	from frappe.core.doctype.prepared_report.prepared_report import get_completed_prepared_report
	from frappe.desk.query_report import get_prepared_report_result, run

	run_name = run_name or report_doc.name

	is_prepared = getattr(report_doc, "prepared_report", False) and not getattr(
		report_doc, "disable_prepared_report", False
	)

	if is_prepared:
		cached_name = get_completed_prepared_report(
			filters=filters, user=frappe.session.user, report_name=report_doc.name
		)
		if cached_name:
			result = get_prepared_report_result(report_doc, filters, dn=cached_name)
			if result and result.get("result"):
				return {**result, "source": "cached"}

	# run() fetches its OWN copy of the Report doc, so flipping the flag on
	# `report_doc` does not stop it returning empty for prepared reports. Pass
	# ignore_prepared_report where supported; keep the in-memory flip as fallback.
	run_kwargs = {
		"report_name": run_name,
		"filters": filters,
		"user": frappe.session.user,
		"is_tree": getattr(report_doc, "is_tree", 0),
		"parent_field": getattr(report_doc, "parent_field", None),
	}
	if "ignore_prepared_report" in inspect.signature(run).parameters:
		run_kwargs["ignore_prepared_report"] = True

	original_prepared_flag = getattr(report_doc, "prepared_report", 0)
	report_doc.prepared_report = 0
	snapshot = _push_form_dict(filters)
	try:
		result = run(**run_kwargs)
	finally:
		report_doc.prepared_report = original_prepared_flag
		_pop_form_dict(snapshot)

	result["source"] = "synchronous"
	return result


def _extract_args(args, kwargs):
	"""Normalize (report_name, filters) regardless of whether the MCP framework
	nested them under 'args', flattened them into kwargs, or passed filters as a
	JSON string."""
	payload = args if isinstance(args, dict) else kwargs.get("args") or {}
	report_name = payload.get("report_name") or kwargs.get("report_name")
	filters = payload.get("filters")
	if filters is None:
		filters = kwargs.get("filters")
	if filters is None:
		# Framework flattened the arguments directly into payload/kwargs
		flat = {k: v for k, v in {**kwargs, **payload}.items() if k not in ("report_name", "filters", "args")}
		filters = flat or {}
	if isinstance(filters, str):
		try:
			filters = json.loads(filters)
		except (TypeError, ValueError):
			filters = {}
	if not isinstance(filters, dict):
		filters = {}
	return report_name, filters


def _extract_pagination(filters: dict) -> dict:
	"""Pull our own result-shaping controls out of the filters dict so they
	never reach the report itself, and never affect the report-execution
	cache key (two calls that differ only in these are the SAME underlying
	report run). Keys: row_limit, row_offset, columns, sort_by, sort_order,
	group_by, group_agg, row_filter."""
	row_limit = filters.pop("row_limit", None)
	row_offset = filters.pop("row_offset", None)
	columns_wanted = filters.pop("columns", None)
	sort_by = filters.pop("sort_by", None)
	sort_order = filters.pop("sort_order", None)
	group_by = filters.pop("group_by", None)
	group_agg = filters.pop("group_agg", None)
	row_filter = filters.pop("row_filter", None)

	try:
		row_limit = int(row_limit) if row_limit is not None else _ROW_LIMIT_DEFAULT
	except (TypeError, ValueError):
		row_limit = _ROW_LIMIT_DEFAULT
	row_limit = max(1, min(row_limit, _ROW_LIMIT_MAX))

	try:
		row_offset = int(row_offset) if row_offset is not None else 0
	except (TypeError, ValueError):
		row_offset = 0
	row_offset = max(0, row_offset)

	if isinstance(columns_wanted, str):
		columns_wanted = [c.strip() for c in columns_wanted.split(",") if c.strip()]
	if not isinstance(columns_wanted, list):
		columns_wanted = None

	sort_order = str(sort_order).lower() if sort_order else "desc"
	if sort_order not in _SORT_ORDERS:
		sort_order = "desc"

	group_agg = str(group_agg).lower() if group_agg else "sum"
	if group_agg not in _GROUP_AGGS:
		group_agg = "sum"

	if isinstance(row_filter, dict):
		row_filter = [row_filter]
	if isinstance(row_filter, list):
		row_filter = [rf for rf in row_filter if isinstance(rf, dict) and rf.get("field") and "value" in rf]
		row_filter = row_filter or None
	else:
		row_filter = None

	return {
		"row_limit": row_limit,
		"row_offset": row_offset,
		"columns": columns_wanted,
		"sort_by": sort_by or None,
		"sort_order": sort_order,
		"group_by": group_by or None,
		"group_agg": group_agg,
		"row_filter": row_filter,
	}


def _num(value):
	"""float(value) for real numbers (int/float/Decimal), else None. Bools excluded."""
	if value is None or isinstance(value, bool):
		return None
	if isinstance(value, numbers.Number):
		try:
			return float(value)
		except (TypeError, ValueError, OverflowError):
			return None
	return None


def _loose_equal(actual, value) -> bool:
	if actual == value:
		return True
	if actual is None or value is None:
		return False
	try:
		return float(actual) == float(value)
	except (TypeError, ValueError):
		pass
	return str(actual).strip().lower() == str(value).strip().lower()


def _normalize_rows(rows, columns) -> list:
	"""Query Reports may return list/tuple rows; shape them as dicts keyed by
	column fieldname so row_filter / sort / group_by / aggregates all work."""
	names = list(_column_fieldnames(columns))
	out = []
	for r in rows or []:
		if isinstance(r, dict):
			out.append(dict(r))
		elif isinstance(r, (list, tuple)):
			out.append({(names[i] if i < len(names) else f"col_{i}"): v for i, v in enumerate(r)})
		else:
			out.append(r)
	return out


def _is_transient(exc) -> bool:
	"""Errors that can succeed on an identical retry must not be remembered."""
	if isinstance(exc, (frappe.PermissionError, TimeoutError)):
		return True
	return any(c.__name__ in _TRANSIENT_ERROR_NAMES for c in type(exc).__mro__)


def _similar_reports(name: str) -> list:
	try:
		words = name.split()
		token = max(words, key=len) if words else name
		return frappe.get_all(
			"Report",
			filters={"report_name": ["like", f"%{token}%"], "disabled": 0},
			pluck="name",
			limit_page_length=5,
		)
	except Exception:
		return []


def _report_builder_payload(report_doc, report_name: str) -> dict:
	saved = None
	try:
		saved = json.loads(report_doc.json) if report_doc.json else None
	except Exception:
		saved = None
	payload = {
		"error": "report_builder_not_supported",
		"error_type": "unsupported_report_type",
		"report_name": report_name,
		"ref_doctype": report_doc.ref_doctype,
		"message": (
			"Report Builder reports cannot be executed by this tool. Query the same data with "
			f"frappe_get_list or frappe_query on '{report_doc.ref_doctype}', using the saved "
			"filters/columns below as a guide."
		),
	}
	if isinstance(saved, dict):
		payload["saved_filters"] = saved.get("filters")
		payload["saved_columns"] = saved.get("fields") or saved.get("columns")
	return payload


def _push_form_dict(filters: dict) -> dict:
	"""Some reports read frappe.form_dict instead of the filters arg. Set the
	values for the run, remember what was there so it can be restored."""
	form_dict = getattr(frappe.local, "form_dict", None)
	if form_dict is None:
		return {}
	snapshot = {k: form_dict.get(k, _MISSING) for k in filters}
	form_dict.update(filters)
	return snapshot


def _pop_form_dict(snapshot: dict):
	form_dict = getattr(frappe.local, "form_dict", None)
	if form_dict is None:
		return
	for key, old in snapshot.items():
		if old is _MISSING:
			form_dict.pop(key, None)
		else:
			form_dict[key] = old


def _column_fieldnames(columns) -> dict:
	"""Map fieldname -> fieldtype from a report's column metadata. Handles both
	dict-shaped columns (Script Reports) and the 'label:type/options:width'
	shorthand strings some Query Reports use for their columns."""
	out = {}
	for col in columns or []:
		if isinstance(col, dict):
			fieldname = col.get("fieldname") or col.get("label")
			fieldtype = col.get("fieldtype")
			if fieldname:
				out[fieldname] = fieldtype
		elif isinstance(col, str):
			# e.g. "Amount:Currency:120"
			parts = col.split(":")
			if len(parts) >= 2:
				out[parts[0].strip()] = parts[1].strip()
	return out


def _numeric_aggregates(rows: list, columns) -> dict:
	"""Compute sum/avg/min/max/non_null_count for numeric-typed columns across
	the FULL row set, so a few hundred returned rows can still answer
	'what's total revenue' for a 10k-row report. Handles Decimal as well as
	int/float."""
	fieldtypes = _column_fieldnames(columns)
	numeric_fields = [fn for fn, ft in fieldtypes.items() if ft in _NUMERIC_FIELDTYPES]
	if not numeric_fields or not rows:
		return {}

	aggregates = {}
	for field in numeric_fields:
		values = [
			n for n in (_num(row.get(field)) for row in rows if isinstance(row, dict)) if n is not None
		]
		if not values:
			continue
		aggregates[field] = {
			"sum": round(sum(values), 4),
			"avg": round(sum(values) / len(values), 4),
			"min": min(values),
			"max": max(values),
			"non_null_count": len(values),
		}
	return aggregates


def _apply_row_filter(rows: list, row_filter) -> list:
	"""AND-combine simple field-level filters over already-fetched rows. Lets
	the agent do 'find the row(s) where customer contains Acme' against a
	cached 10k-row result instead of paging through everything to search
	client-side."""
	if not row_filter:
		return rows

	def matches(row):
		for rf in row_filter:
			field = rf.get("field")
			op = str(rf.get("op") or "eq").lower()
			if op not in _ROW_FILTER_OPS:
				op = "eq"
			value = rf.get("value")
			actual = row.get(field)
			try:
				if op == "eq" and not _loose_equal(actual, value):
					return False
				if op == "neq" and _loose_equal(actual, value):
					return False
				if op == "contains" and str(value).lower() not in str(actual or "").lower():
					return False
				if op in ("gt", "gte", "lt", "lte"):
					if actual is None:
						return False
					a, v = float(actual), float(value)
					if op == "gt" and not a > v:
						return False
					if op == "gte" and not a >= v:
						return False
					if op == "lt" and not a < v:
						return False
					if op == "lte" and not a <= v:
						return False
			except (TypeError, ValueError):
				return False
		return True

	return [r for r in rows if isinstance(r, dict) and matches(r)]


def _apply_sort(rows: list, sort_by, sort_order: str) -> list:
	"""Sort the FULL row set before any slicing happens, so 'top 10 by X'
	actually returns the top 10 rather than an arbitrary prefix. Best-effort:
	a bad/mixed-type sort field falls back to the original order rather than
	raising, since a sort request should never break the whole call."""
	if not sort_by:
		return rows

	def key(row):
		v = row.get(sort_by) if isinstance(row, dict) else None
		return (0, "") if v is None else (1, v)

	try:
		return sorted(rows, key=key, reverse=(sort_order == "desc"))
	except TypeError:
		return rows


def _group_and_aggregate(rows: list, columns, group_by: str, agg: str):
	"""Collapse rows into per-group numeric aggregates: the biggest lever for
	'break down X by Y' queries. Returns (groups, total_group_count, truncated)."""
	fieldtypes = _column_fieldnames(columns)
	numeric_fields = [fn for fn, ft in fieldtypes.items() if ft in _NUMERIC_FIELDTYPES and fn != group_by]

	buckets = {}
	for row in rows:
		if not isinstance(row, dict):
			continue
		key = row.get(group_by)
		key = "(empty)" if key in (None, "") else key
		bucket = buckets.setdefault(key, {"_count": 0, "_sums": {f: 0.0 for f in numeric_fields}})
		bucket["_count"] += 1
		for f in numeric_fields:
			v = _num(row.get(f))
			if v is not None:
				bucket["_sums"][f] += v

	grouped = []
	for key, bucket in buckets.items():
		entry = {group_by: key, "_count": bucket["_count"]}
		for f in numeric_fields:
			total = bucket["_sums"][f]
			if agg == "avg" and bucket["_count"]:
				entry[f] = round(total / bucket["_count"], 4)
			elif agg == "count":
				entry[f] = bucket["_count"]
			else:
				entry[f] = round(total, 4)
		grouped.append(entry)

	sort_field = numeric_fields[0] if numeric_fields else "_count"
	grouped.sort(key=lambda e: e.get(sort_field) or 0, reverse=True)

	truncated = len(grouped) > _MAX_GROUPS_RETURNED
	return grouped[:_MAX_GROUPS_RETURNED], len(grouped), truncated


def _maybe_export_csv(rows: list, columns, report_name: str):
	"""Best-effort: write the full result to a CSV via Frappe's file manager
	and return its file_url. For a genuine 'give me everything' pull on a
	10k-row report, one file beats 15-40 pagination round-trips through chat.
	Never raises — a failed export just means we fall back to pagination."""
	try:
		import csv
		import io

		from frappe.utils.file_manager import save_file

		fieldnames = list(_column_fieldnames(columns).keys())
		if not fieldnames and rows and isinstance(rows[0], dict):
			fieldnames = list(rows[0].keys())

		buffer = io.StringIO()
		writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
		writer.writeheader()
		for row in rows:
			if isinstance(row, dict):
				writer.writerow(row)

		safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in report_name)
		fname = f"{safe_name}_{int(time.time())}.csv"
		file_doc = save_file(fname, buffer.getvalue(), None, None, is_private=1)
		return file_doc.file_url
	except Exception as e:
		frappe.log_error(title="Generate Report Error", message=f"CSV export failed for {report_name}: {e!s}")
		return None


def frappe_generate_report(args: dict = None, **kwargs) -> str:
	"""Execute a Query Report, Script Report or Custom Report.

	Result-shaping controls (passed inside `filters`, stripped before the
	report itself sees them, and NOT part of the report's execution cache key,
	so re-querying with different shaping reuses the cached result):
	  - row_limit (default 250, max 2000) / row_offset: page through rows.
	  - sort_by / sort_order ("asc"|"desc", default "desc"): sort the FULL
		result before slicing, so row_limit=10 + sort_by=grand_total returns
		the real top 10.
	  - group_by / group_agg ("sum"|"avg"|"count", default "sum"): collapse rows
		into per-group numeric totals. Use for breakdowns/comparisons.
	  - row_filter: {"field": ..., "op": "eq"|"neq"|"contains"|"gt"|"gte"|
		"lt"|"lte", "value": ...} or a list of such dicts (AND-combined).
	  - columns: list (or comma-separated string) of fieldnames to include.

	Every response includes `report_summary`/`chart` if the report computed
	them. Use frappe_get_report_filters to see which filters a report accepts.
	Report Builder reports are not executable here (use frappe_get_list /
	frappe_query on their ref_doctype).
	"""
	report_name, filters = _extract_args(args, kwargs)

	if not report_name:
		return json.dumps({"error": "report_name is required", "error_type": "invalid_argument"})
	report_name = str(report_name).strip()

	canonical = frappe.db.exists("Report", report_name)
	if not canonical:
		return json.dumps(
			{
				"error": f"Report '{report_name}' not found",
				"error_type": "not_found",
				"did_you_mean": _similar_reports(report_name),
			},
			default=str,
		)
	report_name = canonical

	# Pop pagination/shaping keys BEFORE computing any cache key, so two calls
	# that differ only in row_limit/sort_by/group_by/row_filter/etc. are
	# treated as the same underlying report execution.
	pagination = _extract_pagination(filters)

	# Short-circuit identical retries after a *deterministic* failure (bad filter
	# shape, missing fiscal year, ...). Transient errors are never remembered.
	cache_key = (
		_RETRY_CACHE_PREFIX
		+ hashlib.sha256(
			f"{frappe.session.user}:{report_name}:{json.dumps(filters, sort_keys=True, default=str)}".encode()
		).hexdigest()
	)
	cached_error = _cache().get_value(cache_key)
	if cached_error:
		return json.dumps(
			{
				"error": "identical_retry_detected",
				"report_name": report_name,
				"previous_error": cached_error,
				"message": (
					"This exact report_name + filters combination was already attempted "
					f"in the last {_RETRY_CACHE_TTL}s and failed with the error above. "
					"Change the filters meaningfully or stop; retrying identically will not help."
				),
			},
			default=str,
		)

	# If this exact (user, report, business-filters) combination already ran
	# successfully recently, reuse it for ANY re-query (page, sort, group-by,
	# row filter). A hit implies the permission check passed for this user
	# within the TTL window.
	result_cache_key = (
		_RESULT_CACHE_PREFIX
		+ hashlib.sha256(
			f"{frappe.session.user}:{report_name}:{json.dumps(filters, sort_keys=True, default=str)}".encode()
		).hexdigest()
	)
	cached_full = _cache().get_value(result_cache_key)
	if cached_full:
		return json.dumps(
			_build_response(
				report_name,
				cached_full["report_type"],
				cached_full["rows"],
				cached_full["columns"],
				cached_full["extra"],
				filters,
				pagination,
				source="cached",
			),
			default=str,
		)

	def remember(payload):
		_cache().set_value(cache_key, payload, expires_in_sec=_RETRY_CACHE_TTL)
		return json.dumps(payload, default=str)

	try:
		# Same gate Frappe's own run() applies: role list, "report" permission on
		# the reference doctype, disabled flag, and Custom Report resolution.
		# (A separate Report.check_permission("read") is redundant and can wrongly
		# block users who are allowed to run the report.)
		report_doc = get_report_doc(report_name)
		report_type = report_doc.report_type

		if report_type == "Report Builder":
			return json.dumps(_report_builder_payload(report_doc, report_name), default=str)
		if report_type not in ("Query Report", "Script Report"):
			return json.dumps(
				{
					"error": f"Unsupported report type: {report_type}",
					"error_type": "unsupported_report_type",
					"report_name": report_name,
				}
			)

		user_keys = set(filters.keys())

		# Read this report's own declared filter defaults/options once and use them
		# to scope defaults, fill missing values and validate what's there.
		filter_defs = _read_report_filter_defs(report_doc)
		effective_filters = _default_filters(dict(filters), filter_defs, report_name)
		effective_filters = _apply_declared_defaults(effective_filters, filter_defs)

		# GUARDRAIL: bad Link references / invalid Select options / bad dates /
		# missing report-declared mandatory filters, before spending an execution.
		validation_error = _validate_filter_values(effective_filters, filter_defs)
		if validation_error:
			return remember(validation_error)

		# GUARDRAIL: stop agent loops on empty financial data.
		if report_name in _FINANCIAL_STATEMENT_REPORTS and effective_filters.get("company"):
			company = effective_filters["company"]
			if not frappe.db.exists("GL Entry", {"company": company, "is_cancelled": 0}):
				fy = frappe.db.get_value(
					"Fiscal Year",
					{"disabled": 0},
					["name", "year_start_date", "year_end_date"],
					order_by="year_start_date desc",
				)
				return json.dumps(
					{
						"error": "no_financial_data",
						"report_name": report_name,
						"company": company,
						"current_fiscal_year": fy[0] if fy else None,
						"fiscal_year_range": [str(fy[1]), str(fy[2])] if fy else None,
						"message": (
							f"No submitted accounting entries exist for company '{company}'. "
							"This is a data-completeness issue, not a filter or tool problem. "
							"Do not retry with different filters or re-check Sales/Purchase "
							"Invoice; GL Entry is authoritative and is confirmed empty. "
							"Report this to the user as-is."
						),
					},
					default=str,
				)

		# GUARDRAIL: company/date range not covered by any Fiscal Year. Only
		# relevant to financial statements or reports with fiscal-year filters.
		if report_name in _FINANCIAL_STATEMENT_REPORTS or _FISCAL_YEAR_FILTER_KEYS & set(filter_defs):
			fy_error = _validate_fiscal_year_coverage(effective_filters)
			if fy_error:
				return remember(fy_error)

		try:
			result = _run_prepared_or_direct(report_doc, effective_filters, run_name=report_name)
		except KeyError as e:
			missing_key = str(e).strip("'\"")
			return remember(
				{
					"error": f"missing_required_filter:{missing_key}",
					"report_name": report_name,
					"filters_sent": effective_filters,
					"hint": (
						f"The report script reads filters['{missing_key}'] directly and has no "
						f"default for it. Supply '{missing_key}' explicitly and retry once. Use "
						"frappe_get_report_filters to see the filters this report accepts."
					),
				}
			)
		except Exception as e:
			error_str = clean_message(e)
			if any(sig in error_str for sig in _INCOMPLETE_STATE_ERROR_SIGNATURES):
				declared = {
					fn: meta.get("options") for fn, meta in filter_defs.items() if meta.get("options")
				}
				return remember(
					{
						"error": "report_internal_state_incomplete",
						"report_name": report_name,
						"filters_sent": effective_filters,
						"message": (
							f"'{report_name}' raised {error_str}. This typically means a Select-type "
							"filter value didn't match any branch the report's own execute() checks "
							"for, so it never finished building its result. This is fixable: check "
							"filters_sent against this report's valid options below and retry with a "
							"matching value."
						),
						"declared_filter_options": declared or None,
					}
				)
			if "not in any active fiscal year" in error_str.lower():
				return remember(
					{
						"error": "no_fiscal_year_for_range",
						"report_name": report_name,
						"message": (
							f"{error_str}. This is a configuration/date issue, not a filter-shape "
							"problem. Pick a different date range or confirm the correct fiscal "
							"year with the user."
						),
					}
				)
			if "mandatory" in error_str.lower():
				return remember(
					{
						"error": error_str,
						"report_name": report_name,
						"hint": (
							f"The report script received these exact filters: {effective_filters}. "
							"If it still claims fields are mandatory, supply them explicitly "
							"(see frappe_get_report_filters) or use frappe_get_list / frappe_query "
							"on the underlying doctype."
						),
					}
				)
			raise

		columns = result.get("columns", [])
		rows = _normalize_rows(result.get("result", []), columns)
		auto_added = {k: v for k, v in effective_filters.items() if k not in user_keys}

		extra = {
			"report_summary": result.get("report_summary"),
			"chart": result.get("chart"),
			"message": result.get("message"),
			"auto_added": auto_added,
		}

		# Cache the full raw result so follow-up re-queries (page, group-by, row
		# filter) skip re-running the report, unless it is too big to park in Redis.
		if len(rows) <= _RESULT_CACHE_MAX_ROWS:
			_cache().set_value(
				result_cache_key,
				{"rows": rows, "columns": columns, "report_type": report_type, "extra": extra},
				expires_in_sec=_RESULT_CACHE_TTL,
			)

		return json.dumps(
			_build_response(
				report_name,
				report_type,
				rows,
				columns,
				extra,
				filters,
				pagination,
				source=result.get("source", "synchronous"),
			),
			default=str,
		)

	except frappe.PermissionError as e:
		return json.dumps(
			{
				"error": clean_message(e) or f"No permission to access report '{report_name}'",
				"error_type": "permission_denied",
				"report_name": report_name,
			}
		)
	except Exception as e:
		payload = error_response(e, title="Generate Report Error", report_name=report_name)
		if not _is_transient(e):
			_cache().set_value(cache_key, {"error": payload["error"]}, expires_in_sec=_RETRY_CACHE_TTL)
		return json.dumps(payload, default=str)


def frappe_get_report_filters(args: dict = None, **kwargs) -> str:
	"""List the filters a report accepts (fieldname, default, options, required)."""
	try:
		args = norm_args(args, kwargs)
		report_name = str(args.get("report_name") or "").strip()
		if not report_name:
			return json.dumps({"error": "report_name is required", "error_type": "invalid_argument"})

		canonical = frappe.db.exists("Report", report_name)
		if not canonical:
			return json.dumps(
				{
					"error": f"Report '{report_name}' not found",
					"error_type": "not_found",
					"did_you_mean": _similar_reports(report_name),
				},
				default=str,
			)

		report_doc = get_report_doc(canonical)  # raises PermissionError if not allowed
		defs = _read_report_filter_defs(report_doc)
		filters = [
			{
				"fieldname": fieldname,
				"default": meta.get("default"),
				"options": meta.get("options"),
				"required": bool(meta.get("reqd")),
			}
			for fieldname, meta in defs.items()
		]

		stored = []
		for row in report_doc.get("filters") or []:
			stored.append(
				{
					"fieldname": row.get("fieldname"),
					"label": row.get("label"),
					"fieldtype": row.get("fieldtype"),
					"options": row.get("options"),
					"required": bool(row.get("mandatory")),
					"default": row.get("default"),
				}
			)

		payload = {
			"report_name": canonical,
			"report_type": report_doc.report_type,
			"ref_doctype": report_doc.ref_doctype,
			"filters": filters or stored,
		}
		if not payload["filters"]:
			payload["note"] = (
				"No filter definitions could be read for this report; it may take none, or "
				"define them in code only. Try running it without filters and read the error."
			)
		return json.dumps(payload, default=str)

	except frappe.PermissionError as e:
		return json.dumps({"error": clean_message(e) or "No permission to access this report", "error_type": "permission_denied"})
	except Exception as e:
		return json.dumps(error_response(e, title="Get Report Filters Error"), default=str)


def _build_response(report_name, report_type, rows, columns, extra, filters, pagination, source):
	"""Shape the final payload: report_summary/chart always in full; then
	row_filter -> sort -> (group_by early-return) -> paginate, in that order,
	so 'top N' and 'find X' behave the way they read."""
	total_before_filter = len(rows)
	working_rows = _apply_row_filter(rows, pagination["row_filter"])
	working_rows = _apply_sort(working_rows, pagination["sort_by"], pagination["sort_order"])

	payload = {
		"report_name": report_name,
		"report_type": report_type,
		"filters_applied": dict(filters),
	}
	if extra.get("report_summary"):
		payload["report_summary"] = extra["report_summary"]
	if extra.get("chart"):
		payload["chart"] = extra["chart"]
	if extra.get("message"):
		payload["message"] = extra["message"]
	if extra.get("auto_added"):
		payload["filters_auto_added"] = extra["auto_added"]
	if pagination["row_filter"]:
		payload["row_filter_applied"] = pagination["row_filter"]
		payload["rows_before_filter"] = total_before_filter

	# Grouped queries answer "break down X by Y" directly — skip raw-row
	# pagination entirely, since the compact group table IS the answer.
	if pagination["group_by"]:
		grouped, group_count, groups_truncated = _group_and_aggregate(
			working_rows, columns, pagination["group_by"], pagination["group_agg"]
		)
		payload["grouped_by"] = pagination["group_by"]
		payload["group_agg"] = pagination["group_agg"]
		payload["grouped_data"] = grouped
		payload["group_count"] = group_count
		payload["source_row_count"] = len(working_rows)
		if groups_truncated:
			payload["note"] = (
				f"{group_count} groups found; showing the top {_MAX_GROUPS_RETURNED} by "
				f"{pagination['group_agg']}. Add a row_filter to narrow further if needed."
			)
		return payload

	total_count = len(working_rows)
	limit, offset = pagination["row_limit"], pagination["row_offset"]
	sliced = working_rows[offset : offset + limit]

	columns_wanted = pagination["columns"]
	out_columns = columns
	if columns_wanted:
		sliced = [{k: v for k, v in r.items() if k in columns_wanted} for r in sliced if isinstance(r, dict)]
		out_columns = [
			c
			for c in columns
			if (c.get("fieldname") if isinstance(c, dict) else c.split(":")[0]) in columns_wanted
		]

	payload["data"] = sliced
	payload["columns"] = out_columns
	payload["data_count"] = len(sliced)
	payload["total_row_count"] = total_count

	truncated = total_count > offset + len(sliced) or offset > 0
	if truncated:
		payload["pagination"] = {
			"row_offset": offset,
			"row_limit": limit,
			"returned": len(sliced),
			"remaining": max(0, total_count - offset - len(sliced)),
			"next_row_offset": offset + len(sliced) if offset + len(sliced) < total_count else None,
			"note": (
				f"Returned rows {offset}-{offset + len(sliced)} of {total_count}. "
				"For the next page, call again with the same report_name/filters plus "
				f"filters.row_offset={offset + len(sliced)}. Prefer filters.group_by for "
				"breakdowns, filters.sort_by for top-N, or report_summary/aggregates below "
				"over paging through everything."
			),
		}
		payload["aggregates"] = _numeric_aggregates(working_rows, columns) or None

		# A plain, unnarrowed 'just give me everything' pull on a huge report is
		# better served as one file than dozens of pagination round-trips.
		no_narrowing = offset == 0 and not pagination["row_filter"] and not pagination["sort_by"]
		if _EXPORT_CSV_ENABLED and no_narrowing and total_count > _EXPORT_ROW_THRESHOLD:
			download_url = _maybe_export_csv(working_rows, columns, report_name)
			if download_url:
				payload["full_result_csv"] = download_url
				payload["pagination"]["note"] += (
					f" The full {total_count}-row result was also exported to "
					f"{download_url} — share that with the user instead of paginating "
					f"if they want the complete dataset."
				)

	if not sliced and total_count == 0:
		payload["suggestion"] = (
			"Report returned 0 rows — auto-defaulted filters, or an active row_filter, "
			"may not match your data. Retry with explicit/looser filters. If you are "
			"checking financial data, verify underlying GL Entries or Sales Invoices "
			"exist for this period using frappe_get_list."
		)

	return payload
