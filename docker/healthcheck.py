"""Check local program health without exposing operator credentials."""
import json
import os
import ssl
import time
import urllib.request

port = int(os.environ.get("BREADCAST_PORT", "8080"))
for scheme in ("http", "https"):
    try:
        # HTTPS fallback is only for the loopback health probe with direct TLS.
        def read():
            with urllib.request.urlopen(f"{scheme}://127.0.0.1:{port}/healthz", timeout=2,
                                        context=ssl._create_unverified_context() if scheme == "https" else None) as response:
                return json.load(response)
        first = read()
        time.sleep(0.3)
        health = read()
        if health["gateway"] and not health["program"]["error"] and \
                health["program"]["frames_written"] > first["program"]["frames_written"]:
            raise SystemExit(0)
    except (OSError, ValueError, KeyError):
        pass
raise SystemExit(1)
