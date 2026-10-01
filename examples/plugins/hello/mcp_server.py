"""The hello plugin's MCP server: one tool, `hello`, over stdio.

The runner starts it for each cousin that enables the plugin. It speaks
JSON-RPC 2.0, one message per line on stdin and stdout, with the standard
library only, and asks the plugin's service (HELLO_URL) for the greeting.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

TOOL = {
    "name": "hello",
    "description": "Greet someone by name, through the hello plugin's service.",
    "inputSchema": {"type": "object",
                    "properties": {"name": {"type": "string", "description": "who to greet"}},
                    "required": ["name"]},
}


def greet(name):
    url = "%s/hello?name=%s" % (os.environ["HELLO_URL"], urllib.parse.quote(name))
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.load(r)["greeting"]


def answer(request):
    method, params = request.get("method"), request.get("params") or {}
    if method == "initialize":
        return {"protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "hello", "version": "0.1.0"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [TOOL]}
    if method == "tools/call":
        if params.get("name") != "hello":
            raise LookupError("unknown tool: %s" % params.get("name"))
        try:
            text, failed = greet(str((params.get("arguments") or {}).get("name", ""))), False
        except OSError as err:
            text, failed = "the hello service did not answer: %s" % err, True
        return {"content": [{"type": "text", "text": text}], "isError": failed}
    raise LookupError("method not found: %s" % method)


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        request = json.loads(line)
        if "id" not in request:
            continue                    # a notification: nothing to answer
        try:
            reply = {"jsonrpc": "2.0", "id": request["id"], "result": answer(request)}
        except LookupError as err:
            reply = {"jsonrpc": "2.0", "id": request["id"],
                     "error": {"code": -32601, "message": str(err)}}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
