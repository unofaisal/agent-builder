# agent_builder/oauth_url_fix.py
import frappe

def patch_oauth_urls():
	"""Dev-tunnel workaround. Remove once Frappe ships the fix (frappe PR #43251)."""
	if not str(frappe.conf.get("host_name") or "").startswith("https://"):
		return  # no-op unless host_name is set to an https URL
	import frappe.integrations.oauth2 as o2
	import frappe.oauth as fo
	if getattr(o2, "_ab_url_patched", False):
		return
	def _server_url():
		return frappe.get_value("Social Login Key", "frappe", "base_url") or frappe.utils.get_url()
	def _resource_url():
		return frappe.utils.get_url()
	for mod in (fo, o2):
		if hasattr(mod, "get_server_url"):
			mod.get_server_url = _server_url
		if hasattr(mod, "get_resource_url"):
			mod.get_resource_url = _resource_url
	o2._ab_url_patched = True