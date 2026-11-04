# agent_builder/native_api/tools/agent_tools/calculate_loading_plan.py
"""Deterministic loading calculation for a Quotation (pipes, rolls, or both).

The rules (margins, stack limits, nesting) come from the "Loading Planner
Settings" doctype, read inside the tool, so the model never copies numbers.
Returns the finished report in the schema the loading skill/template expects.
The agent reviews it against the quotation and vehicle data and writes the
final report.
"""
import json

import frappe

from agent_builder.native_api.tools.decorator import tool


@tool(schema_name="calculate_loading_plan")
async def calculate_loading_plan(args: dict, **kwargs) -> str:
    quotation_name = args.get("quotation_name")
    if not quotation_name:
        return json.dumps({"error": "quotation_name is required"})
    if not frappe.db.exists("Quotation", quotation_name):
        return json.dumps({"error": f"Quotation '{quotation_name}' does not exist"})

    # Imported here so agent_builder still loads on sites without apex_loading
    from apex_loading.apis.gemini_native import _get_available_vehicles, _get_items_from_quotation
    from apex_loading.apis.loading_config import load_config
    from apex_loading.apis.loading_planner import UnsupportedError, build_report_data, plan_loading

    try:
        items = _get_items_from_quotation(quotation_name)
        if not items:
            return json.dumps({"error": "No valid items in quotation."})
        vehicles = _get_available_vehicles()
        if not vehicles:
            return json.dumps({"error": "No vehicles available."})
        result = plan_loading(items, vehicles, load_config())
        report = build_report_data(result, quotation_name, frappe.utils.now()[:19])
    except UnsupportedError as e:
        return json.dumps({"error": str(e), "error_type": "unsupported"})
    except Exception:
        frappe.log_error(title="calculate_loading_plan failed", message=frappe.get_traceback())
        return json.dumps({"error": "Loading calculation failed. See the Error Log."})

    return json.dumps(report, default=str)
