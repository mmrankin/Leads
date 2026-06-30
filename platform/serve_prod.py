"""Production WSGI entrypoint for the platform admin (waitress)."""
import os
from waitress import serve
from admin import app

if __name__ == "__main__":
    # Bind to localhost only — reachable solely via the Cloudflare tunnel, not
    # directly over the LAN. Override with BIND_HOST=0.0.0.0 if LAN access is needed.
    serve(app, host=os.environ.get("BIND_HOST", "127.0.0.1"),
          port=int(os.environ.get("PORT", "5050")),
          threads=8, ident="dealer-platform-admin")
