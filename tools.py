"""LLM-callable tools so the PRIVATE agent can work the network agentically.

These belong to the private agent (it can pull from the network). They never
expose private context outward — outward-facing answers only ever go through
the envoy. Handlers return JSON strings, matching Hermes' tool convention.
"""
import json
import time

from . import matchmaker, dossier, sanitize


def build(client, card, llm=None):
    """Return a list of tool specs to register with ctx.register_tool."""

    def matchmake(params, **kwargs):
        # The autonomous brain. The cron prompt calls this a few times a day and
        # relays the result ONLY if it is not the silent marker. Real clock here
        # (this is an IO boundary, not a logic path — run_cycle stays clock-pure).
        result = matchmaker.run_and_persist(client, card, llm, time.time)
        return json.dumps({"result": result})

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

    # --- dossier / rings / conversations / reveals ---

    def dossier_view(params, **kwargs):
        # READ-ONLY views. "summary" NEVER contains contact values (only a
        # boolean) — see dossier.summary and the membrane tests.
        view = (params.get("view") or "summary").lower()
        if view == "ring1":
            return json.dumps({"ring1": dossier.get_ring1()})
        if view == "intents":
            return json.dumps({"intents": dossier.list_intents()})
        return json.dumps({"summary": dossier.summary()})

    def ask(params, **kwargs):
        # Open a discreet-ask thread with another agent and send the question.
        to = params.get("to", "")
        question = sanitize.clean_text(params.get("question", ""), max_len=500)
        if not to or not question:
            return json.dumps({"success": False, "error": "need 'to' and 'question'"})
        opened = client.open_thread(to, "ask", question[:120])
        tid = opened.get("thread_id")
        if not tid:
            return json.dumps({"success": False,
                               "error": opened.get("error", "open failed")})
        sent = client.send_thread(tid, question)
        return json.dumps({"success": True, "thread_id": tid,
                           "turn": sent.get("turn"), "error": sent.get("error")})

    # TODO(dig-integration): the follow-up agent will drive kind="dig" threads
    # from matchmaker (open_thread -> envoy.respond(mode="dig", ring1_facts=...)
    # per turn -> findings note), replacing the single-shot send_message
    # handshake. These thread ops + the envoy mode plumbing are the seam.
    def thread(params, **kwargs):
        action = (params.get("action") or "list").lower()
        tid = params.get("thread_id", "")
        if action == "list":
            return json.dumps(client.list_threads())
        if action == "read":
            if not tid:
                return json.dumps({"error": "need 'thread_id'"})
            res = client.read_thread(tid)
            # Counterpart content is untrusted -> sanitize before the agent sees it.
            for m in res.get("messages", []):
                m["text"] = sanitize.clean_text(m.get("text", ""), max_len=1000)
            return json.dumps(res)
        if action == "send":
            if not tid:
                return json.dumps({"error": "need 'thread_id'"})
            text = sanitize.clean_text(params.get("text", ""), max_len=1000)
            return json.dumps(client.send_thread(tid, text))
        if action == "close":
            if not tid:
                return json.dumps({"error": "need 'thread_id'"})
            return json.dumps(client.close_thread(tid))
        return json.dumps({"error": f"unknown action '{action}'"})

    def reveal_request(params, **kwargs):
        # GATED by commands.install_gate: a call with include_contact=true is
        # blocked unless it also carries human_approved=true. Defense in depth:
        # the handler itself embeds contact ONLY when BOTH flags are true.
        to = params.get("to", "")
        if not to:
            return json.dumps({"success": False, "error": "need 'to'"})
        context = sanitize.clean_text(params.get("context", ""), max_len=500)
        include = bool(params.get("include_contact")) and bool(params.get("human_approved"))
        opened = client.open_thread(to, "reveal_request", "reveal request")
        tid = opened.get("thread_id")
        if not tid:
            return json.dumps({"success": False,
                               "error": opened.get("error", "open failed")})
        body = {"reveal_request": True, "context": context,
                "card": card.public_dict()}
        if include:
            body["contact"] = dossier.get_contact()
        client.send_thread(tid, json.dumps(body))
        return json.dumps({"success": True, "thread_id": tid,
                           "included_contact": include})

    def reveal_respond(params, **kwargs):
        # GATED by commands.install_gate: approve=true is blocked unless
        # human_approved=true. Defense in depth: contact is released ONLY when
        # both are true and the human hasn't marked it never-share.
        tid = params.get("thread_id", "")
        if not tid:
            return json.dumps({"success": False, "error": "need 'thread_id'"})
        approve = bool(params.get("approve"))
        if not approve:
            client.send_thread(tid, json.dumps({"reveal": "declined"}))
            return json.dumps({"success": True, "approved": False})
        if not params.get("human_approved"):
            return json.dumps({"success": False,
                               "error": "human approval required to release contact"})
        contact = dossier.get_contact()
        if contact.get("never_share"):
            return json.dumps({"success": False,
                               "error": "contact is marked never-share"})
        client.send_thread(tid, json.dumps({"reveal": "approved", "contact": contact}))
        return json.dumps({"success": True, "approved": True})

    def intent(params, **kwargs):
        action = (params.get("action") or "list").lower()
        if action == "add":
            it = dossier.add_intent(params.get("text", ""))
            if not it:
                return json.dumps({"success": False, "error": "empty intent text"})
            return json.dumps({"success": True, "intent": it})
        if action == "retire":
            it = dossier.retire_intent(params.get("id"))
            if not it:
                return json.dumps({"success": False, "error": "no such intent"})
            return json.dumps({"success": True, "intent": it})
        return json.dumps({"success": True, "intents": dossier.list_intents()})

    return [
        {
            "name": "hermies_matchmake",
            "description": ("Run one autonomous matchmaking cycle. Returns "
                            '{"result": <text>} where <text> is either a human '
                            "notification or the marker HERMIES_SILENT (say "
                            "nothing to the human in that case)."),
            "schema": {
                "name": "hermies_matchmake",
                "description": ("Run one matchmaking cycle; relay the result only "
                                "if it is not HERMIES_SILENT."),
                "parameters": {"type": "object", "properties": {}},
            },
            "handler": matchmake,
        },
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
        {
            "name": "hermies_dossier",
            "description": ("Read-only views of your human's local dossier: "
                            "ring1 (approved shareable facts), intents (standing "
                            "searches), or summary (section counts + ring1 + "
                            "intents; NEVER contact values)."),
            "schema": {
                "name": "hermies_dossier",
                "description": "Read-only dossier views (never exposes contact identity).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "view": {"type": "string",
                                 "enum": ["ring1", "intents", "summary"],
                                 "description": "which view to return"},
                    },
                },
            },
            "handler": dossier_view,
        },
        {
            "name": "hermies_ask",
            "description": ("Run a discreet ask: open an 'ask' thread with "
                            "another agent's envoy and send a precise question. "
                            "Their human is not notified. Returns the thread_id."),
            "schema": {
                "name": "hermies_ask",
                "description": "Discreet ask to another agent's envoy.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "target agent handle"},
                        "question": {"type": "string", "description": "what to find out"},
                    },
                    "required": ["to", "question"],
                },
            },
            "handler": ask,
        },
        {
            "name": "hermies_thread",
            "description": ("Operate a threaded conversation: list your threads, "
                            "read one, send a turn, or close it. The hub enforces "
                            "a 12-message turn budget per thread."),
            "schema": {
                "name": "hermies_thread",
                "description": "List/read/send/close threaded conversations.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string",
                                   "enum": ["list", "read", "send", "close"]},
                        "thread_id": {"type": "string"},
                        "text": {"type": "string", "description": "message body for send"},
                    },
                    "required": ["action"],
                },
            },
            "handler": thread,
        },
        {
            "name": "hermies_reveal_request",
            "description": ("Ask another human to connect: open a reveal_request "
                            "thread with card-level context. Including your "
                            "human's contact identity requires human_approved=true "
                            "(also enforced by the pre_tool_call gate)."),
            "schema": {
                "name": "hermies_reveal_request",
                "description": "Request a real-world connection (contact release is approval-gated).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "target agent handle"},
                        "context": {"type": "string", "description": "why connect"},
                        "include_contact": {"type": "boolean",
                                            "description": "embed your human's contact identity"},
                        "human_approved": {"type": "boolean",
                                           "description": "set true ONLY after your human explicitly approves sending contact"},
                    },
                    "required": ["to"],
                },
            },
            "handler": reveal_request,
        },
        {
            "name": "hermies_reveal_respond",
            "description": ("Respond to an incoming reveal request. approve=true "
                            "releases your human's contact identity into the "
                            "thread and requires human_approved=true (also "
                            "enforced by the pre_tool_call gate)."),
            "schema": {
                "name": "hermies_reveal_respond",
                "description": "Approve/decline a reveal request (contact release is approval-gated).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "thread_id": {"type": "string"},
                        "approve": {"type": "boolean"},
                        "human_approved": {"type": "boolean",
                                           "description": "set true ONLY after your human explicitly approves releasing contact"},
                    },
                    "required": ["thread_id", "approve"],
                },
            },
            "handler": reveal_respond,
        },
        {
            "name": "hermies_intent",
            "description": ("Manage standing intents (persistent 'dig for X' "
                            "searches): add a new one, list them, or retire one "
                            "by id."),
            "schema": {
                "name": "hermies_intent",
                "description": "Add/list/retire standing intents.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["add", "list", "retire"]},
                        "text": {"type": "string", "description": "intent text (for add)"},
                        "id": {"description": "intent id (for retire)"},
                    },
                    "required": ["action"],
                },
            },
            "handler": intent,
        },
    ]
