#!/bin/bash
# Trade-In valuation caches — nightly refresh (inv_prefix_count tracks live
# inventory). vin8_map only rebuilds when missing; pass --vin8 to force it.
cd /Users/markrankin/claude/trade-in || exit 1
exec /Users/markrankin/claude/dealer-leads/.venv/bin/python \
     /Users/markrankin/claude/trade-in/refresh_caches.py "$@"
