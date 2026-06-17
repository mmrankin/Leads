"""Production WSGI entrypoint — serves the Flask app with waitress (threaded,
no fork; well-behaved on macOS). Port comes from $PORT. Run via run_server.sh.
"""
import os
from waitress import serve
from app import app

if __name__ == "__main__":
    serve(app, host="0.0.0.0", port=int(os.environ.get("PORT", "5000")),
          threads=8, ident="dealer-platform")
