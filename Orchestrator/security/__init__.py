"""Request-perimeter guards for the Orchestrator.

The Tailscale + LAN perimeter is the security boundary BY DESIGN — nothing in
this package adds login, sessions or API keys. It closes the bypasses that
work THROUGH that perimeter (a web page in the operator's browser, DNS
rebinding, the app proxy). See origin_guard.py and perimeter.py.
"""
