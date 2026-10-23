# tools/frappe_tools/document_list.py

import re

import frappe

from agent_builder.mcp_tools._common import (
	ArgError,
	coerce_filters,
	coerce_int,
	coerce_list,
	did_you_mean,
	dumps,
	error_response,
	find_parent_doctypes,
	load_meta,
	norm_args,
	valid_fieldnames,
)

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ORDER_PART_RE = re.compile(r"^`?([A-Za-z_][A-Za-z0-9_]*)`?(?:\s+(asc|desc))?$", re.I)
_DEFAULT_FIELDS = ["name", "creation", "modified"]
 
 
def _filter_fields(filters):
	"""Yield (doctype_or_None, fieldname) for every field a filter references."""
	if isinstance(filters, dict):
		for key in filters:
			yield None, key
	elif isinstance(filters, (list, tuple)):
		for item in filters:
			if isinstance(item, dict):
				for key in item:
					yield None, key
			elif isinstance(item, (list, tuple)):
				if len(item) >= 4 and isinstance(item[0], str) and isinstance(item[1], str):
					yield item[0], item[1]
				elif len(item) >= 3 and isinstance(item[0], str):
					yield None, item[0]
 
 
def _bad_filter_fields(filters, doctype, valid):
	bad = []
	for dt, field in _filter_fields(filters):
		if dt not in (None, doctype):
			continue  # child-table filter, validated by Frappe
		if not isinstance(field, str) or not _IDENT_RE.match(field):
			continue  # qualified / exotic syntax: leave to Frappe
		if field not in valid:
			bad.append(field)
	return bad
 
 
def _safe_order_by(order_by, valid):
	"""Keep only 'field [asc|desc]' parts whose field exists. Returns (str|None, dropped)."""
	if not order_by or not isinstance(order_by, str):
		return None, []
	kept, dropped = [], []
	for part in order_by.split(","):
		match = _ORDER_PART_RE.match(part.strip())
		if match and match.group(1) in valid:
			kept.append(f"{match.group(1)} {(match.group(2) or 'asc').lower()}")
		else:
			dropped.append(part.strip())
	return (", ".join(kept) if kept else None), dropped
 
 
_COUNT_SCAN_CAP = 10000
 
 
def _try_count(doctype, base, fields):
	save_point = "agent_count_try"
	frappe.db.savepoint(save_point)
	try:
		rows = frappe.get_list(doctype, fields=fields, **base)
		if rows and rows[0].get("total_count") is not None:
			return int(rows[0]["total_count"])
	except Exception:
		frappe.db.rollback(save_point=save_point)
	return None
 
 
def _permitted_count(doctype, filters, parent_doctype):
	"""Permission-aware count -> (count, exact, method).
 
	frappe.db.count ignores User Permissions (leaks totals). The aggregate syntax
	differs across Frappe versions, so try the known forms, then fall back to a
	capped permission-aware scan so a number is always returned.
	"""
	base = {"filters": filters or None, "ignore_permissions": False}
	if parent_doctype:
		base["parent_doctype"] = parent_doctype
 
	for fields in (
		[{"COUNT": "name", "as": "total_count"}],
		["count(name) as total_count"],
		[f"count(distinct `tab{doctype}`.name) as total_count"],
	):
		count = _try_count(doctype, base, fields)
		if count is not None:
			return count, True, "aggregate"
 
	names = frappe.get_list(doctype, pluck="name", limit_page_length=_COUNT_SCAN_CAP + 1, **base)
	exact = len(names) <= _COUNT_SCAN_CAP
	return min(len(names), _COUNT_SCAN_CAP), exact, "scan"
 
 
def frappe_get_list(args: dict, **kwargs) -> str:
	"""Search and list Frappe documents with optional filtering."""
	doctype = None
	try:
		args = norm_args(args, kwargs)
		doctype_in = str(args.get("doctype") or "").strip()
		if not doctype_in:
			return dumps({"error": "doctype is required", "error_type": "invalid_argument"})
 
		doctype, meta, err = load_meta(doctype_in)
		if err:
			return dumps(err)
		if meta.issingle:
			return dumps(
				{
					"error": f"'{doctype}' is a Single doctype (one record). Use frappe_get_doc instead.",
					"error_type": "single_doctype",
					"doctype": doctype,
				}
			)
 
		warnings = []
		valid = valid_fieldnames(meta)
 
		filters = coerce_filters(args.get("filters"))
		bad = _bad_filter_fields(filters, doctype, valid)
		if bad:
			return dumps(
				{
					"error": f"Unknown filter field(s) for {doctype}: {', '.join(bad)}",
					"error_type": "invalid_filter_field",
					"doctype": doctype,
					"did_you_mean": {b: did_you_mean(b, valid) for b in bad},
					"hint": (
						"Use frappe_get_doctype_info for valid fieldnames. Child-table fields must be "
						"filtered as [child_doctype, field, operator, value]."
					),
				}
			)
 
		requested_fields = coerce_list(args.get("fields", None), "fields")
		fields = []
		dropped_fields = []
		for field in requested_fields or []:
			field = str(field).strip()
			if not field:
				continue
			if _IDENT_RE.match(field) and field not in valid:
				dropped_fields.append(field)
			else:
				fields.append(field)
		if dropped_fields:
			warnings.append(
				{
					"unknown_fields_ignored": dropped_fields,
					"did_you_mean": {f: did_you_mean(f, valid) for f in dropped_fields},
				}
			)
		fields = fields or list(_DEFAULT_FIELDS)
 
		limit = coerce_int(args.get("limit"), 20, 1, 1000)
		offset = coerce_int(args.get("offset", args.get("start")), 0, 0)
 
		order_by, dropped_order = _safe_order_by(args.get("order_by") or "creation desc", valid)
		if dropped_order:
			warnings.append({"order_by_ignored": dropped_order})
 
		parent_doctype = args.get("parent_doctype")
		if meta.istable and not parent_doctype:
			parents = find_parent_doctypes(doctype)
			if len(parents) == 1:
				parent_doctype = parents[0]
				warnings.append({"parent_doctype_inferred": parent_doctype})
			else:
				return dumps(
					{
						"error": (
							f"'{doctype}' is a child-table doctype; pass parent_doctype. "
							"Alternatively use frappe_get_doc on the parent record - child rows come back nested."
						),
						"error_type": "child_doctype_needs_parent",
						"candidate_parent_doctypes": parents,
					}
				)
 
		get_list_kwargs = {
			"filters": filters or None,
			"fields": fields,
			"limit_page_length": limit,
			"limit_start": offset,
			"ignore_permissions": False,
		}
		if order_by:
			get_list_kwargs["order_by"] = order_by
		if parent_doctype:
			get_list_kwargs["parent_doctype"] = parent_doctype
 
		rows = frappe.get_list(doctype, **get_list_kwargs)
 
		try:
			total_count, exact, _method = _permitted_count(doctype, filters, parent_doctype)
			has_more = total_count > offset + len(rows)
			if not exact:
				warnings.append({"total_count_capped": f"count capped at {_COUNT_SCAN_CAP}; the real total is at least this"})
		except Exception:
			total_count = None
			has_more = len(rows) == limit  # best effort if even the fallback fails
 
		result = {
			"doctype": doctype,
			"data": rows,
			"count": len(rows),
			"offset": offset,
			"total_count": total_count,
			"has_more": has_more,
			"filters_applied": filters,
		}
		if has_more:
			result["next_offset"] = offset + len(rows)
		if warnings:
			result["warnings"] = warnings
		return dumps(result)
 
	except ArgError as e:
		return dumps(error_response(e, doctype=doctype))
	except Exception as e:
		return dumps(error_response(e, title="Document List Error", doctype=doctype))
 
