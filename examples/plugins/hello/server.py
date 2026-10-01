"""The hello plugin's service: a greeting as JSON, a health check and a page.

Started by cousin-supervisor as `plugin:hello`; it listens on PLUGIN_PORT,
on loopback only. The console reaches it through /plugins/hello/.
"""
import html
import http.server
import json
import os
import urllib.parse


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/healthz":
            self.send(200, "text/plain", b"ok")
        elif url.path == "/hello":
            name = urllib.parse.parse_qs(url.query).get("name", ["world"])[0]
            self.send(200, "application/json", json.dumps({"greeting": "hello, %s" % name}).encode())
        elif url.path.startswith("/page/"):
            slug = html.escape(url.path[len("/page/"):])
            # relative, so it resolves under the console's /plugins/hello/ prefix
            page = ("<!doctype html><title>hello</title><p id=g>...</p><script>"
                    "fetch('../hello?name=%s').then(r => r.json())"
                    ".then(d => { document.getElementById('g').textContent = d.greeting; });"
                    "</script>" % urllib.parse.quote(slug))
            self.send(200, "text/html; charset=utf-8", page.encode())
        else:
            self.send(404, "text/plain", b"not found")

    def send(self, status, ctype, body):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = int(os.environ["PLUGIN_PORT"])
    http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
