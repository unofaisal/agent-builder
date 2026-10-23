app_name = "agent_builder"
app_title = "Agent Builder"
app_publisher = "Faisal Imali"
app_description = "Frappe/ErpNext Co-pilot"
app_email = "faisalimali1@gmail.com"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "agent_builder",
# 		"logo": "/assets/agent_builder/logo.png",
# 		"title": "Agent Builder",
# 		"route": "/agent_builder",
# 		"has_permission": "agent_builder.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/agent_builder/css/agent_builder.css"
# app_include_js = "/assets/agent_builder/js/agent_builder.js"

# include js, css files in header of web template
# web_include_css = "/assets/agent_builder/css/agent_builder.css"
# web_include_js = "/assets/agent_builder/js/agent_builder.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "agent_builder/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "agent_builder/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "agent_builder.utils.jinja_methods",
# 	"filters": "agent_builder.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "agent_builder.install.before_install"
# after_install = "agent_builder.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "agent_builder.uninstall.before_uninstall"
# after_uninstall = "agent_builder.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "agent_builder.utils.before_app_install"
# after_app_install = "agent_builder.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "agent_builder.utils.before_app_uninstall"
# after_app_uninstall = "agent_builder.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "agent_builder.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"agent_builder.tasks.all"
# 	],
# 	"daily": [
# 		"agent_builder.tasks.daily"
# 	],
# 	"hourly": [
# 		"agent_builder.tasks.hourly"
# 	],
# 	"weekly": [
# 		"agent_builder.tasks.weekly"
# 	],
# 	"monthly": [
# 		"agent_builder.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "agent_builder.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "agent_builder.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "agent_builder.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["agent_builder.utils.before_request"]
# after_request = ["agent_builder.utils.after_request"]

# Job Events
# ----------
# before_job = ["agent_builder.utils.before_job"]
# after_job = ["agent_builder.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"agent_builder.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

scheduler_events = {
      "cron": { 
         "*/1 * * * *": ["agent_builder.native_api.trigger.run_due_scheduled_triggers"]
      }
  }

doc_events = {
    "*": {
        "before_insert": "agent_builder.native_api.trigger.handle_doctype_event",
        "after_insert": "agent_builder.native_api.trigger.handle_doctype_event",
        "before_save": "agent_builder.native_api.trigger.handle_doctype_event",
        "on_update": "agent_builder.native_api.trigger.handle_doctype_event",
        "before_submit": "agent_builder.native_api.trigger.handle_doctype_event",
        "on_submit": "agent_builder.native_api.trigger.handle_doctype_event",
        "before_cancel": "agent_builder.native_api.trigger.handle_doctype_event",
        "on_cancel": "agent_builder.native_api.trigger.handle_doctype_event",
        "on_update_after_submit": "agent_builder.native_api.trigger.handle_doctype_event",
        "on_trash": "agent_builder.native_api.trigger.handle_doctype_event",
        "after_delete": "agent_builder.native_api.trigger.handle_doctype_event",
        "on_change": "agent_builder.native_api.trigger.handle_doctype_event",
    }
}

fixtures = [
     {"dt": "Role", "filters": [["name", "=", "Omnis User"]]}
]
app_include_js = [
    "/assets/agent_builder/js/chat_list.bundle.js",
    "/assets/agent_builder/js/chat_messages.bundle.js",
    "/assets/agent_builder/js/chat_realtime.bundle.js",
    "/assets/agent_builder/js/chat_ui.bundle.js",
    # "/assets/agent_builder/js/agent_notifications.js"
]
app_include_css = ["/assets/agent_builder/css/Chat_Ui.bundle.css"]

# agent_builder/hooks.py
before_request = ["agent_builder.oauth_url_fix.patch_oauth_urls"]