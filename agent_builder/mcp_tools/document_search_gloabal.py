# tools/frappe_tools/document_search_global.py

"""Global search by name/title across doctypes.

The default doctype list covers the most commonly searched records. Use the
'doctypes' parameter to search additional or different doctypes.
"""

import time

import frappe

from agent_builder.mcp_tools._common import (
	ArgError,
	coerce_int,
	coerce_list,
	dumps,
	error_response,
	like_search,
	norm_args,
	resolve_doctype,
)

_TIME_BUDGET_SECONDS = 8.0

DEFAULT_SEARCH_DOCTYPES = [
	# People & Organizations
	"User",
	"Contact",
	"Customer",
	"Supplier",
	"Employee",
	# Items & Products
	"Item",
	"Product Bundle",
	"BOM",
	# Transactions
	"Sales Order",
	"Purchase Order",
	"Quotation",
	"Sales Invoice",
	"Purchase Invoice",
	"Delivery Note",
	"Purchase Receipt",
	# CRM
	"Lead",
	"Opportunity",
	# Projects & Tasks
	"Project",
	"Task",
	# Company structure
	"Company",
	"Cost Center",
	"Warehouse",
	# Accounting
	"Account",
]


def _interleave(groups: list) -> list:
	"""Round-robin so early doctypes don't crowd out later ones."""
	out, i = [], 0
	while any(i < len(g) for g in groups):
		for g in groups:
			if i < len(g):
				out.append(g[i])
		i += 1
	return out


def frappe_search_documents(args: dict, **kwargs) -> str:
	"""Search by name or title across doctypes. Use 'doctypes' param to specify which doctypes to search (default covers common ones). Use frappe_get_list when you know the doctype."""
	try:
		args = norm_args(args, kwargs)
		query = str(args.get("query") or "").strip()
		if not query:
			return dumps({"error": "query is required", "error_type": "invalid_argument"})
		limit = coerce_int(args.get("limit"), 20, 1, 200)
		per_doctype = coerce_int(args.get("per_doctype"), 5, 1, 50)
		doctypes = coerce_list(args.get("doctypes"), "doctypes") or DEFAULT_SEARCH_DOCTYPES

		started = time.monotonic()
		groups, searched, skipped = [], [], []

		for requested in doctypes:
			if time.monotonic() - started > _TIME_BUDGET_SECONDS:
				skipped.append({"doctype": requested, "reason": "time budget exceeded"})
				continue
			try:
				doctype = resolve_doctype(str(requested))
				if not doctype:
					skipped.append({"doctype": requested, "reason": "doctype not found / app not installed"})
					continue
				meta = frappe.get_meta(doctype)
				if meta.issingle or meta.istable:
					skipped.append({"doctype": doctype, "reason": "single or child-table doctype"})
					continue
				if not frappe.has_permission(doctype, "read"):
					skipped.append({"doctype": doctype, "reason": "no read permission"})
					continue

				# One failing doctype (custom app, odd schema) must not sink the whole search.
				rows = like_search(doctype, query, limit=per_doctype, meta=meta)
				searched.append(doctype)
				group = []
				for row in rows:
					entry = {"name": row.get("name"), "doctype": doctype}
					if meta.title_field and row.get(meta.title_field) not in (None, row.get("name")):
						entry["title"] = row.get(meta.title_field)
					group.append(entry)
				groups.append(group)
			except Exception as e:
				skipped.append({"doctype": requested, "reason": f"search failed: {str(e)[:120]}"})

		results = _interleave(groups)
		payload = {
			"query": query,
			"results": results[:limit],
			"count": min(len(results), limit),
			"total_found": len(results),
			"searched_doctypes": searched,
		}
		if skipped:
			payload["skipped_doctypes"] = skipped
		return dumps(payload)

	except ArgError as e:
		return dumps(error_response(e))
	except Exception as e:
		return dumps(error_response(e, title="Search Documents Error"))
