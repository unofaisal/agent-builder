calculate_loading_plan = {
  "name": "calculate_loading_plan",
  "description": "Calculate the lowest-trip loading plan for a Quotation of pipes, rolls, or both. Applies the rules saved in Loading Planner Settings and returns the complete report JSON (summary, loading_plans, leftover_items, optimization_notes). All arithmetic is done by this tool; do not recalculate.",
  "parameters": {
    "type": "object",
    "properties": {
      "quotation_name": {
        "type": "string",
        "description": "Exact Quotation name, e.g. SAL-QTN-26-13087"
      }
    },
    "required": ["quotation_name"]
  }
}
