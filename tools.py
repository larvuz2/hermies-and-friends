"""LLM-callable tools so the PRIVATE agent can work the network agentically.

These belong to the private agent (it can pull from the network). They never
expose private context outward — outward-facing answers only ever go through
the envoy. Handlers return JSON strings, matching Hermes' tool convention.
"""
import json


def build(client, card):
    """Return a list of tool specs to register with ctx.register_tool."""

    def search_agents(params, **kwargs):
        agents = client.search_agents(params.get("query", ""))
        return json.dumps({"success": True, "agents": agents})

    def list_signals(params, **kwargs):
        handle = card.public_dict().get("handle", "")
        return json.dumps({"success": True, "signals": client.list_signals(handle)})

    def send_message(params, **kwargs):
        to = params.get("to", "")
        text = params.get("text", "")
        if not to or not text:
            return json.dumps({"success": False, "error": "need 'to' and 'text'"})
        return json.dumps({"success": True, "result": client.send_message(to, text)})

    def install_skill(params, **kwargs):
        # Gated by commands.install_gate (pre_tool_call). If we get here it was
        # approved. Real impl: download the bundle into the plugin skills dir.
        name = params.get("name", "")
        return json.dumps({"success": True, "installed": name,
                           "note": "stub — wire bundle download in Phase 1"})

    return [
        {
            "name": "hermies_search_agents",
            "description": "Search the Hermies network for other agents by keyword, offer, or guild.",
            "schema": {
                "name": "hermies_search_agents",
                "description": "Search the Hermies network for other agents.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "keyword, offer, or guild"}},
                    "required": ["query"],
                },
            },
            "handler": search_agents,
        },
        {
            "name": "hermies_list_signals",
            "description": "List current signals/matches surfaced for your human.",
            "schema": {
                "name": "hermies_list_signals",
                "description": "List current Hermies signals for your human.",
                "parameters": {"type": "object", "properties": {}},
            },
            "handler": list_signals,
        },
        {
            "name": "hermies_send_message",
            "description": "Send a message to another agent's envoy through the hub.",
            "schema": {
                "name": "hermies_send_message",
                "description": "Message another agent via the Hermies hub.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "target agent handle"},
                        "text": {"type": "string", "description": "message body"},
                    },
                    "required": ["to", "text"],
                },
            },
            "handler": send_message,
        },
        {
            "name": "hermies_install_skill",
            "description": "Install a skill package from the network (requires human approval).",
            "schema": {
                "name": "hermies_install_skill",
                "description": "Install a network skill (approval-gated).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "skill name, e.g. sol-herald:run-eval"},
                        "approved": {"type": "boolean", "description": "set true only after the human confirms"},
                    },
                    "required": ["name"],
                },
            },
            "handler": install_skill,
        },
    ]
