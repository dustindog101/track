"""trackcli -- a free, fast, zero-dependency package tracking CLI.

Package layout::

    trackcli/
        cli.py         verb dispatch (`track <number>`, help, refresh...)
                       + output routing (pretty vs JSON)
        helpsys.py     the `track help [topic]` manual, versioned in code
        engine.py      the provider fallback-chain orchestrator
        models.py      Shipment / Event dataclasses + error taxonomy
        status.py      raw provider text -> normalized status enum
        carriers.py    tracking-number -> carrier auto-detection
        cache.py       TTL disk cache + provider rate-limit cooldowns
        config.py      optional official-API keys (env vars / config.json)
        ui.py          pretty ANSI terminal renderer (no deps)
        providers/     one module per data source (see providers/base.py)

Everything works with the Python standard library only.  `rich` and
`playwright` are strictly optional extras.
"""

__version__ = "1.0.0"
