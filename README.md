# Agent Builder

A ERPNext co-pilot and autonomous agents system. Build dynamic Dashboards, Workspaces, and DocTypes through intelligent conversation. Real-time chat interface with streaming AI responses and multi-tool orchestration.

## Features

- **Autonomous Agent Integration** — Advanced AI agent framework with streaming responses and reasoning
- **Frappe Tools Plugin** — Direct CRUD operations on Frappe documents (read, list, save, delete)
- **Real-time Chat** — WebSocket-powered conversation with live token streaming
- **Multi-agent Orchestration** — Supervisor agent routing to specialized workers
- **Dynamic Content Creation** — Generate Dashboards, Charts, Workspaces, and DocTypes via conversation
- **Skill-based Architecture** — Modular skills for Frappe Dashboard, Charts, and Workspaces management

## Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/unofaisal/agent-builder.git --branch develop
bench install-app agent_builder
```

## Configuration

Add credential in the "Agent Setup" doctype

ie provider [OPENROUTER, GEMINI, ANTHROPIC, GLM]
and the API_KEY provided by your vendor

## Desk Usage

The Agent Builder chat widget icon is injected into the Frappe desk interface via JavaScript. For users to see the icon and access the chat widget:

**Required Role:** Users must have the **"Omnis User"** role assigned to their account.

The chat widget is injected into the desk interface on page load and will be hidden for users without this role. The role check is performed client-side in `agent_builder/public/js/chat_ui.bundle.js`.

## API Usage


### Send a Message
 ## This can be triggerd by email, or in any doctype event
```python
frappe.call({
  'method': 'agent_builder.api.agent.chat',
  'args': {
    'message': 'Add a number card showing total open orders',
    'chat_id': 'your-chat-id'
  },
  'callback': function(r) {
    console.log(r.message);
  }
})
```

## Architecture

### API Layer (`agent_builder/api/`)

- **`agent.py`** — Main chat endpoints using Hermes agent with Frappe tools
- **`agent_test.py`** — Testing endpoint for quick agent invocation


- **Frappe Tools Plugin** — Registers and manages Frappe CRUD tools
  - `frappe_get_doc` — Fetch single documents
  - `frappe_get_list` — Query with filters
  - `frappe_save_doc` — Create/update documents
  - `frappe_delete_doc` — Delete documents

- **Skills** — Documented agent behaviors
  - `frappe-dashboard` — Dashboard creation and management
  - `frappe-chart` — Dashboard Chart semantics
  - `frappe-workspace` — Workspace configuration and role-based visibility

### Legacy Agents (`agent_builder/agent2/`, `agent_builder/agent3/`)

- Experimental multi-agent orchestration systems

## License

MIT
