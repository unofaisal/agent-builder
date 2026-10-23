# tools/frappe_tools/document_search_link.py

import inspect

import frappe
from frappe.desk.search import search_link

from agent_builder.mcp_tools._common import (
	ArgError,
	coerce_filters,
	coerce_int,
	dumps,
	error_response,
	like_search,
	load_meta,
	norm_args,
)


def _call_search_link(doctype, query, filters, limit):
	"""search_link's contract differs by Frappe version: newer returns a list
	(page_length), older returns None and fills frappe.response['results']
	(page_len). Handle both."""
	params = inspect.signature(search_link).parameters
	kwargs = {"doctype": doctype, "txt": query}
	if filters:
		kwargs["filters"] = filters
	if "page_length" in params:
		kwargs["page_length"] = limit
	elif "page_len" in params:
		kwargs["page_len"] = limit

	results = search_link(**kwargs)
	if results is None:
		results = (getattr(frappe, "response", None) or {}).get("results")
	return results or []


def _normalise(results) -> list:
	out = []
	for r in results:
		if isinstance(r, dict):
			out.append(
				{
					"value": r.get("value") or r.get("name"),
					"label": r.get("label"),
					"description": r.get("description"),
				}
			)
		elif isinstance(r, (list, tuple)) and r:
			out.append({"value": r[0], "label": None, "description": r[1] if len(r) > 1 else None})
		elif isinstance(r, str):
			out.append({"value": r, "label": None, "description": None})
	return out


def frappe_search_link(args: dict, **kwargs) -> str:
	"""Fuzzy-search for valid values of a link field (e.g. resolving a customer name to its exact record name)."""
	doctype = None
	try:
		args = norm_args(args, kwargs)
		doctype_in = str(args.get("doctype") or "").strip()
		query = args.get("query")
		if not doctype_in or query is None:
			return dumps({"error": "doctype and query are required", "error_type": "invalid_argument"})
		query = str(query).strip()
		filters = coerce_filters(args.get("filters"))
		limit = coerce_int(args.get("limit"), 10, 1, 50)

		doctype, meta, err = load_meta(doctype_in)
		if err:
			return dumps(err)
		if not frappe.has_permission(doctype, "read"):
			return dumps(
				{
					"error": f"No read permission for DocType '{doctype}'",
					"error_type": "permission_denied",
					"doctype": doctype,
				}
			)

		source = "search_link"
		try:
			results = _normalise(_call_search_link(doctype, query, filters, limit))
		except Exception:
			# Some doctypes ship a custom search query that errors; fall back to a
			# permission-aware name/title LIKE search so the caller still gets matches.
			source = "like_search"
			rows = like_search(doctype, query, limit=limit, filters=filters or None, meta=meta)
			results = [
				{
					"value": r.get("name"),
					"label": r.get(meta.title_field) if meta.title_field else None,
					"description": None,
				}
				for r in rows
			]

		return dumps(
			{
				"doctype": doctype,
				"query": query,
				"results": results,
				"count": len(results),
				"filters_applied": filters,
				"source": source,
			}
		)

	except ArgError as e:
		return dumps(error_response(e, doctype=doctype))
	except Exception as e:
		return dumps(error_response(e, title="Search Link Error", doctype=doctype))
