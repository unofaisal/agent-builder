# tools/frappe_tools/_common.py
"""Shared helpers for the Frappe agent tools. Not a tool itself (no @tool).

Keeps argument coercion, error reporting, child-table handling and
"what did Frappe actually save" diffing identical across every tool.
"""

import copy
import difflib
import html
import json
import re

import frappe

try:  # These live in frappe.model.workflow, NOT frappe.exceptions.
	from frappe.model.workflow import (
		WorkflowPermissionError,
		WorkflowStateError,
		WorkflowTransitionError,
	)
except Exception:  # pragma: no cover - unusual builds

	class WorkflowStateError(Exception):
		pass

	class WorkflowTransitionError(Exception):
		pass

	class WorkflowPermissionError(Exception):
		pass


# Fields an LLM-built payload must never set directly.
BLOCKED_FIELDS = frozenset(
	{
		"owner",
		"docstatus",
		"amended_from",
		"workflow_state",
		"creation",
		"modified",
		"modified_by",
		"idx",
	}
)
PROTECTED_ON_UPDATE = BLOCKED_FIELDS | {"name", "doctype"}

STANDARD_FIELDS = frozenset(
	{
		"name",
		"owner",
		"creation",
		"modified",
		"modified_by",
		"docstatus",
		"idx",
		"parent",
		"parenttype",
		"parentfield",
		"_assign",
		"_comments",
		"_liked_by",
		"_user_tags",
		"_seen",
	}
)

NO_VALUE_TYPES = frozenset(
	{"Section Break", "Column Break", "Tab Break", "HTML", "Button", "Image", "Fold", "Heading"}
)
TABLE_TYPES = ("Table", "Table MultiSelect")

SEARCHABLE_TYPES = frozenset(
	{"Data", "Link", "Small Text", "Text", "Dynamic Link", "Select", "Read Only", "Long Text"}
)

_DIFF_SKIP_KEYS = frozenset(
	{
		"name",
		"_delete",
		"doctype",
		"parent",
		"parenttype",
		"parentfield",
		"idx",
		"docstatus",
		"owner",
		"creation",
		"modified",
		"modified_by",
	}
)
# Only compare types that round-trip exactly (HTML editors sanitise, datetimes
# reformat; both would give false positives).
_DIFF_SCALAR_TYPES = frozenset(
	{
		"Data",
		"Link",
		"Select",
		"Int",
		"Float",
		"Currency",
		"Percent",
		"Check",
		"Date",
		"Dynamic Link",
		"Small Text",
		"Rating",
	}
)
_MAX_WARNINGS = 50


class ArgError(ValueError):
	"""Malformed tool arguments; reported as error_type=invalid_argument."""


# --------------------------------------------------------------------------
# Argument handling
# --------------------------------------------------------------------------


def dumps(payload) -> str:
	return json.dumps(payload, default=str)


def loads_loose(value):
	"""Models sometimes send objects/arrays as (over-escaped) JSON strings."""
	if not isinstance(value, str):
		return value
	text = value.strip()
	if not text or text[0] not in "{[":
		return value
	for candidate in (text, text.replace('\\"', '"')):
		try:
			return json.loads(candidate)
		except ValueError:
			continue
	return value


def norm_args(args, kwargs) -> dict:
	"""Accept args as dict, JSON string, or flattened into **kwargs."""
	kwargs = kwargs or {}
	payload = {}
	if isinstance(args, str):
		args = loads_loose(args)
	if isinstance(args, dict):
		payload = dict(args)
	nested = kwargs.get("args")
	if isinstance(nested, str):
		nested = loads_loose(nested)
	extra = {k: v for k, v in kwargs.items() if k != "args"}
	if isinstance(nested, dict):
		extra = {**extra, **nested}
	for key, value in extra.items():
		payload.setdefault(key, value)
	return payload


def coerce_dict(value, name="argument") -> dict:
	value = loads_loose(value)
	if value is None or value == "":
		return {}
	if isinstance(value, dict):
		return value
	raise ArgError(f"'{name}' must be an object, got {type(value).__name__}")


def coerce_filters(value, name="filters"):
	value = loads_loose(value)
	if value is None or value == "":
		return {}
	if isinstance(value, (dict, list)):
		return value
	raise ArgError(f"'{name}' must be an object or a list of filters, got {type(value).__name__}")


def coerce_list(value, name="argument"):
	value = loads_loose(value)
	if value is None or value == "":
		return None
	if isinstance(value, (list, tuple)):
		return list(value)
	if isinstance(value, str):
		return [p.strip() for p in value.split(",") if p.strip()]
	raise ArgError(f"'{name}' must be a list, got {type(value).__name__}")


def coerce_bool(value, default=False) -> bool:
	if value is None:
		return default
	if isinstance(value, bool):
		return value
	if isinstance(value, (int, float)):
		return bool(value)
	if isinstance(value, str):
		text = value.strip().lower()
		if text in ("true", "1", "yes", "y", "on"):
			return True
		if text in ("false", "0", "no", "n", "off", ""):
			return False
	return default


def coerce_int(value, default, minimum=None, maximum=None) -> int:
	try:
		number = int(float(value))
	except (TypeError, ValueError):
		number = default
	if minimum is not None:
		number = max(minimum, number)
	if maximum is not None:
		number = min(maximum, number)
	return number


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")

_ERROR_TYPES = (
	("PermissionError", "permission_denied"),
	("DoesNotExistError", "not_found"),
	("MandatoryError", "missing_required_field"),
	("CancelledLinkError", "cancelled_link"),
	("LinkValidationError", "invalid_link"),
	("DuplicateEntryError", "duplicate_entry"),
	("TimestampMismatchError", "stale_document"),
	("DocstatusTransitionError", "invalid_docstatus_transition"),
	("UpdateAfterSubmitError", "update_after_submit_not_allowed"),
	("CannotChangeConstantError", "field_cannot_be_changed"),
	("InvalidStatusError", "invalid_status"),
	("LinkExistsError", "link_exists"),
	("ValidationError", "validation_failed"),
)


def clean_message(exc_or_text) -> str:
	"""frappe.throw() messages are HTML; agents want plain text."""
	text = exc_or_text if isinstance(exc_or_text, str) else str(exc_or_text)
	return html.unescape(_TAG_RE.sub("", text)).strip()


def logged_messages(limit=5) -> list:
	"""Extra messages Frappe queued (e.g. one per failing child row)."""
	out = []
	try:
		for entry in list(getattr(frappe.local, "message_log", None) or [])[-limit:]:
			if isinstance(entry, str):
				entry = loads_loose(entry) if entry.strip().startswith("{") else {"message": entry}
			message = entry.get("message") if isinstance(entry, dict) else None
			if message:
				cleaned = clean_message(message)
				if cleaned and cleaned not in out:
					out.append(cleaned)
	except Exception:
		pass
	return out


def classify_error(exc):
	"""Return (error_type, is_expected). Unexpected errors get logged."""
	if isinstance(exc, ArgError):
		return "invalid_argument", True
	if isinstance(exc, WorkflowTransitionError):
		return "workflow_transition_error", True
	if isinstance(exc, WorkflowPermissionError):
		return "workflow_permission_error", True
	if isinstance(exc, WorkflowStateError):
		return "workflow_state_error", True
	for attr, label in _ERROR_TYPES:
		cls = getattr(frappe, attr, None)
		if isinstance(cls, type) and isinstance(exc, cls):
			return label, True
	return "internal_error", False


def parse_missing(message: str) -> list:
	"""MandatoryError text looks like '[Doctype, name]: Label 1, Label 2'."""
	match = re.match(r"^\[[^\]]*\]:\s*(.+)$", (message or "").strip(), re.S)
	if not match:
		return []
	return [p.strip() for p in match.group(1).split(",") if p.strip()]


def labels_to_fieldnames(doctype, labels) -> list:
	try:
		by_label = {(df.label or "").strip().lower(): df.fieldname for df in frappe.get_meta(doctype).fields}
	except Exception:
		return []
	return [by_label[l.strip().lower()] for l in labels if l.strip().lower() in by_label]


def error_response(exc, title="Tool Error", doctype=None, **context) -> dict:
	"""One place that turns any exception into a structured, agent-readable dict."""
	label, expected = classify_error(exc)
	message = clean_message(exc)
	payload = {"error": message or exc.__class__.__name__, "error_type": label}
	if doctype:
		payload["doctype"] = doctype
	payload.update({k: v for k, v in context.items() if v is not None})

	if label == "permission_denied" and not message:
		payload["error"] = "No permission for this operation"
	if label == "missing_required_field":
		labels = parse_missing(message)
		if labels:
			payload["missing_fields"] = labels
			mapped = labels_to_fieldnames(doctype, labels) if doctype else []
			if mapped:
				payload["missing_fieldnames"] = mapped
	if label in ("validation_failed", "missing_required_field", "invalid_link"):
		extra = [m for m in logged_messages() if m != message]
		if extra:
			payload["details"] = extra

	if not expected:
		low = message.lower()
		if "unknown column" in low:
			payload["hint"] = "A field name is wrong. Use frappe_get_doctype_info to see valid fieldnames."
		try:
			frappe.log_error(
				title=title,
				message=f"{doctype or ''} {context}: {exc!s}\n{frappe.get_traceback()}",
			)
		except Exception:
			pass
	return payload


def safe_rollback(save_point=None):
	try:
		if save_point:
			frappe.db.rollback(save_point=save_point)
		else:
			frappe.db.rollback()
	except Exception:
		pass


def get_cache():
	"""frappe.cache is an object on v15+, a callable on v14."""
	cache = frappe.cache
	return cache if hasattr(cache, "get_value") else cache()


# --------------------------------------------------------------------------
# DocType / document helpers
# --------------------------------------------------------------------------


def did_you_mean(word, options, n=3) -> list:
	lowered = {str(o).lower(): o for o in options}
	matches = difflib.get_close_matches(str(word).lower(), list(lowered), n=n, cutoff=0.6)
	return [lowered[m] for m in matches]


def resolve_doctype(doctype):
	"""Canonical DocType name (case-corrected) or None."""
	if not doctype or not isinstance(doctype, str):
		return None
	return frappe.db.exists("DocType", doctype.strip()) or None


def doctype_not_found(doctype) -> dict:
	payload = {
		"error": f"DocType '{doctype}' not found",
		"error_type": "doctype_not_found",
	}
	try:
		names = frappe.get_all("DocType", pluck="name")
		suggestions = did_you_mean(doctype, names)
		if suggestions:
			payload["did_you_mean"] = suggestions
	except Exception:
		pass
	return payload


def load_meta(doctype):
	"""Return (canonical_name, meta, error_dict)."""
	canonical = resolve_doctype(doctype)
	if not canonical:
		return None, None, doctype_not_found(doctype)
	return canonical, frappe.get_meta(canonical), None


def valid_fieldnames(meta) -> set:
	return {f.fieldname for f in meta.fields if f.fieldname} | set(STANDARD_FIELDS)


def table_fields_of(meta) -> dict:
	return {f.fieldname: f.options for f in meta.fields if f.fieldtype in TABLE_TYPES}


def permitted_fieldnames(doctype, parenttype=None):
	"""Fields the current user may read (permlevel-aware), or None if unknown."""
	try:
		from frappe.model import get_permitted_fields

		return set(get_permitted_fields(doctype, parenttype=parenttype))
	except Exception:
		pass
	try:
		return set(frappe.get_meta(doctype).get_permitted_fieldnames(parenttype=parenttype))
	except Exception:
		return None


def workflow_state_field(doctype):
	"""Name of the workflow state field if an active workflow exists, else None."""
	try:
		from frappe.model.workflow import get_workflow_name

		workflow = get_workflow_name(doctype)
		if not workflow:
			return None
		return frappe.db.get_value("Workflow", workflow, "workflow_state_field") or "workflow_state"
	except Exception:
		return None


def find_parent_doctypes(child_doctype) -> list:
	try:
		return frappe.get_all(
			"DocField",
			filters={"fieldtype": ["in", list(TABLE_TYPES)], "options": child_doctype},
			pluck="parent",
			distinct=True,
		)
	except Exception:
		return []


def searchable_fields(meta, permitted=None, max_fields=5) -> list:
	candidates = ["name"]
	if meta.title_field:
		candidates.append(meta.title_field)
	for field in (getattr(meta, "search_fields", None) or "").split(","):
		field = field.strip()
		if field:
			candidates.append(field)
	out = []
	for field in candidates:
		if field in out:
			continue
		if field != "name":
			df = meta.get_field(field)
			if not df or df.fieldtype not in SEARCHABLE_TYPES:
				continue
		if permitted is not None and field not in permitted:
			continue
		out.append(field)
	return out[:max_fields]


def like_search(doctype, query, limit=5, filters=None, meta=None) -> list:
	"""Permission-aware substring search over name + title + search fields.

	Many doctypes are named by series (CUST-0001) while people search by the
	human title (customer_name), so name-only matching misses most records.
	"""
	meta = meta or frappe.get_meta(doctype)
	permitted = permitted_fieldnames(doctype)
	fields = searchable_fields(meta, permitted)
	pattern = f"%{query}%"
	out_fields = ["name"]
	title = meta.title_field
	if title and title != "name" and (permitted is None or title in permitted):
		out_fields.append(title)
	return frappe.get_list(
		doctype,
		filters=filters or None,
		or_filters=[[f, "like", pattern] for f in fields],
		fields=out_fields,
		limit_page_length=limit,
		ignore_permissions=False,
	)


def suggest_names(doctype, name, limit=5) -> list:
	try:
		rows = like_search(doctype, name, limit=limit)
		return [r.get("name") for r in rows if r.get("name")]
	except Exception:
		return []


def load_doc(doctype, name):
	"""Return (doc, error_dict). Case-insensitive names resolve to canonical."""
	try:
		return frappe.get_doc(doctype, name), None
	except frappe.DoesNotExistError:
		payload = {
			"error": f"{doctype} '{name}' not found",
			"error_type": "not_found",
			"doctype": doctype,
			"name": name,
		}
		suggestions = suggest_names(doctype, name)
		if suggestions:
			payload["did_you_mean"] = suggestions
		return None, payload


# --------------------------------------------------------------------------
# Child tables
# --------------------------------------------------------------------------


def _clean_row(row: dict) -> dict:
	cleaned = {k: v for k, v in row.items() if k != "_delete"}
	if not cleaned.get("name"):
		cleaned.pop("name", None)
	return cleaned


def _renumber(doc, field):
	for idx, row in enumerate(doc.get(field) or [], 1):
		row.idx = idx


def normalize_rows(field, rows):
	"""Validate/coerce a child-table payload. Returns list[dict] or raises ArgError."""
	rows = loads_loose(rows)
	if isinstance(rows, dict):
		rows = [rows]
	if not isinstance(rows, list):
		raise ArgError(f"Child table '{field}' requires a list of dictionaries, got: {type(rows).__name__}")
	out = []
	for row in rows:
		row = loads_loose(row)
		if not isinstance(row, dict):
			raise ArgError(f"Child table '{field}' rows must be dictionaries, got: {type(row).__name__}")
		out.append(row)
	return out


def apply_child_table_update(doc, field, child_doctype, rows):
	"""Patch- or replace-mode child table update.

	Patch mode (any row has 'name'): matched rows updated, unmatched rows
	appended, '_delete': true removes. Replace mode (no row has 'name'): clear
	and refill. Returns (error_dict | None, mode, cleaned_rows).
	"""
	try:
		rows = normalize_rows(field, rows)
	except ArgError as e:
		return (
			{"error": str(e), "error_type": "child_table_handling_error", "field": field},
			None,
			None,
		)

	if not any(row.get("name") for row in rows):
		if any(row.get("_delete") for row in rows):
			return (
				{
					"error": f"'_delete' on child table '{field}' requires a 'name' to identify the row",
					"error_type": "child_row_not_found",
					"field": field,
				},
				None,
				None,
			)
		cleaned = [_clean_row(r) for r in rows]
		doc.set(field, [])
		for row in cleaned:
			doc.append(field, row)
		_renumber(doc, field)
		return None, "replace", cleaned

	existing = {r.name: r for r in (doc.get(field) or []) if getattr(r, "name", None)}
	for row in rows:
		row_name = row.get("name")
		delete = bool(row.get("_delete"))
		if not row_name:
			if delete:
				return (
					{
						"error": f"'_delete' on child table '{field}' requires a 'name' to identify the row",
						"error_type": "child_row_not_found",
						"field": field,
					},
					None,
					None,
				)
			doc.append(field, _clean_row(row))
			continue
		target = existing.get(row_name)
		if target is None:
			return (
				{
					"error": f"Row '{row_name}' not found in {child_doctype} table '{field}'",
					"error_type": "child_row_not_found",
					"field": field,
					"available_row_names": list(existing)[:50],
				},
				None,
				None,
			)
		if delete:
			doc.remove(target)
			existing.pop(row_name, None)
			continue
		for key, value in row.items():
			if key in ("name", "_delete"):
				continue
			target.set(key, value)
	_renumber(doc, field)
	return None, "patch", [_clean_row(r) if not r.get("_delete") else r for r in rows]


# --------------------------------------------------------------------------
# "What did Frappe actually save?" diffing
# --------------------------------------------------------------------------


def values_equal(saved, requested) -> bool:
	if requested is None:
		return True
	if saved is None:
		return requested in ("",)
	if str(saved).strip() == str(requested).strip():
		return True
	try:
		return float(saved) == float(requested)
	except (TypeError, ValueError):
		return False


def diff_child_rows(doc, field, sent_rows, mode="replace") -> list:
	"""Flag child-row values Frappe changed or dropped (rounding, UOM
	conversion, permlevel resets, unknown fields)."""
	saved_rows = list(doc.get(field) or [])
	pairs = []
	if mode == "replace":
		for idx, sent in enumerate(sent_rows):
			if idx < len(saved_rows):
				pairs.append((saved_rows[idx], sent))
	else:
		by_name = {r.name: r for r in saved_rows if getattr(r, "name", None)}
		unnamed = [r for r in sent_rows if not r.get("name")]
		tail = saved_rows[max(0, len(saved_rows) - len(unnamed)) :] if unnamed else []
		for sent in sent_rows:
			if sent.get("_delete") or not sent.get("name"):
				continue
			saved = by_name.get(sent["name"])
			if saved is not None:
				pairs.append((saved, sent))
		for saved, sent in zip(tail, unnamed):
			pairs.append((saved, sent))

	warnings = []
	for saved, sent in pairs:
		for key, requested in sent.items():
			if key in _DIFF_SKIP_KEYS:
				continue
			saved_val = saved.get(key)
			if values_equal(saved_val, requested):
				continue
			warnings.append(
				{
					"child_table": field,
					"row_idx": getattr(saved, "idx", None),
					"row_name": getattr(saved, "name", None),
					"field": key,
					"requested": requested,
					"saved": None if saved_val is None else str(saved_val),
				}
			)
			if len(warnings) >= _MAX_WARNINGS:
				return warnings
	return warnings


def diff_top_level(doc, sent: dict, meta) -> list:
	warnings = []
	for key, requested in sent.items():
		if key in _DIFF_SKIP_KEYS:
			continue
		df = meta.get_field(key)
		if not df or df.fieldtype not in _DIFF_SCALAR_TYPES:
			continue
		saved_val = doc.get(key)
		if values_equal(saved_val, requested):
			continue
		warnings.append(
			{
				"field": key,
				"requested": requested,
				"saved": None if saved_val is None else str(saved_val),
			}
		)
		if len(warnings) >= _MAX_WARNINGS:
			break
	return warnings


def snapshot_rows(rows):
	return copy.deepcopy(rows)
