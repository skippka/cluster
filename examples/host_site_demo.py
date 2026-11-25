"""Example web server for LAN hosting on a Cluster worker."""

import http.server
import socket

PORT = int("{PORT}") if "{PORT}".isdigit() else 8080


class DemoHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        html = f"""
        <html><body style="background:#0d1117;color:#e6edf3;font-family:sans-serif;padding:40px;">
          <h1 style="color:#34d399;">Hosted via Cluster LAN</h1>
          <p>Host: <b>{socket.gethostname()}</b></p>
        </body></html>
        """.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)


if __name__ == "__main__":
    http.server.ThreadingHTTPServer(("0.0.0.0", PORT), DemoHandler).serve_forever()
