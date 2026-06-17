"""Production WSGI entrypoint for the platform admin (waitress)."""
import os
from waitress import serve
from admin import app

if __name__ == "__main__":
    serve(app, host="0.0.0.0", port=int(os.environ.get("PORT", "5050")),
          threads=8, ident="dealer-platform-admin")
