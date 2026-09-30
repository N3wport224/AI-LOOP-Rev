"""Developer API tier: a metered, key-authenticated REST API over the hiring-signal data.

Served under ``/v1/`` by the public listener (the same aiohttp app as the Stripe webhook),
reachable through the Cloudflare Tunnel. See ``api.server`` for the routes and ``api.openapi``
for the OpenAPI 3.1 document (``/openapi.json``, rendered at ``/docs/api``).
"""
