"""Bedrock Agent action group -> Okta Agent Gateway (MCP over HTTP).

Per invocation: initialize -> notifications/initialized -> tools/call.
The user's Okta access token arrives in sessionAttributes and is sent only as
the Authorization header; the Gateway performs the token exchange for GitHub.

Uses only the standard library, so no `requests` layer is needed.
"""
import json
import os
import urllib.error
import urllib.request

GATEWAY_URL = os.environ.get("OKTA_AGENT_GATEWAY_URL")
# Must match the prefix the Okta Agent Gateway puts on tool names (check with an MCP tools/list call)
TOOL_PREFIX = os.environ.get("TOOL_PREFIX", "GitHubMCP_")
PROTOCOL_VERSION = "2025-06-18"
TIMEOUT = 25  # Lambda timeout should be >= 30s


class GatewayError(Exception):
    pass


def _parse_body(raw, content_type):
    """Gateway may answer with plain JSON or an SSE stream; return the JSON-RPC message."""
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return {}
    if "text/event-stream" in (content_type or "") or text.startswith(("event:", "data:")):
        last = None
        for line in text.splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload:
                    last = payload
        return json.loads(last) if last else {}
    return json.loads(text)


def _post(token, message, session_id=None):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    req = urllib.request.Request(
        GATEWAY_URL, data=json.dumps(message).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = _parse_body(resp.read(), resp.headers.get("Content-Type"))
            return body, resp.headers.get("Mcp-Session-Id") or session_id
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:1000]
        print(f"[ERROR] Gateway HTTP {e.code} for {message.get('method')}: {detail}")
        raise GatewayError(f"Gateway HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        print(f"[ERROR] Gateway unreachable: {e.reason}")
        raise GatewayError(f"Gateway unreachable: {e.reason}")


def _check_rpc(body, step):
    # The Gateway returns HTTP 200 for failures, so inspect the JSON-RPC envelope.
    if "error" in body:
        print(f"[ERROR] JSON-RPC error at {step}: {json.dumps(body['error'])}")
        raise GatewayError(f"{step}: {body['error'].get('message', body['error'])}")
    return body.get("result", {})


def call_tool(token, tool_name, arguments):
    init, session_id = _post(token, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "bedrock-okta-demo", "version": "1.0"},
        },
    })
    _check_rpc(init, "initialize")
    _post(token, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session_id)

    body, _ = _post(token, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }, session_id)
    result = _check_rpc(body, f"tools/call {tool_name}")

    text = "\n".join(
        c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"
    ) or json.dumps(result)
    if result.get("isError"):
        print(f"[ERROR] Tool {tool_name} returned isError: {text[:1000]}")
        raise GatewayError(f"Tool {tool_name} failed: {text[:1000]}")
    return text


def _arguments(event):
    """Build tool arguments from the action-group requestBody and query/path parameters."""
    args = {}
    props = (
        event.get("requestBody", {}).get("content", {}).get("application/json", {}).get("properties", [])
    )
    for item in list(props) + list(event.get("parameters", [])):
        value, kind = item.get("value"), item.get("type")
        try:
            if kind == "integer":
                value = int(value)
            elif kind == "number":
                value = float(value)
            elif kind == "boolean":
                value = str(value).lower() == "true"
            elif kind in ("array", "object"):
                value = json.loads(value)
        except (TypeError, ValueError):
            pass
        args[item["name"]] = value
    return args


def _reply(event, status, body):
    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": event.get("actionGroup", ""),
            "apiPath": event.get("apiPath", ""),
            "httpMethod": event.get("httpMethod", ""),
            "httpStatusCode": status,
            "responseBody": {"application/json": {"body": json.dumps(body)}},
        },
    }


def lambda_handler(event, context):
    # Never log the whole event: sessionAttributes carries the access token.
    print(f"Action: {event.get('actionGroup')} {event.get('httpMethod')} {event.get('apiPath')}")

    attrs = {**event.get("promptSessionAttributes", {}), **event.get("sessionAttributes", {})}
    token = attrs.get("accessToken")
    if not token:
        print("[ERROR] accessToken missing from sessionAttributes")
        return _reply(event, 400, {"error": "accessToken not found in sessionAttributes"})
    if not GATEWAY_URL:
        print("[ERROR] OKTA_AGENT_GATEWAY_URL is not set")
        return _reply(event, 500, {"error": "OKTA_AGENT_GATEWAY_URL not configured"})

    tool = TOOL_PREFIX + event.get("apiPath", "").lstrip("/")
    arguments = _arguments(event)
    print(f"Calling tool {tool} with arguments {json.dumps(arguments)}")

    try:
        return _reply(event, 200, {"result": call_tool(token, tool, arguments)})
    except GatewayError as e:
        # Return 200 so Bedrock hands the error text to the agent instead of raising dependencyFailedException.
        return _reply(event, 200, {"error": str(e)})
    except Exception as e:  # keep the agent responsive on unexpected failures
        print(f"[ERROR] Unexpected: {type(e).__name__}: {e}")
        return _reply(event, 500, {"error": f"{type(e).__name__}: {e}"})
