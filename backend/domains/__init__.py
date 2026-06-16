"""Domain knowledge packs.

Each subpackage under ``domains/`` is a self-contained vertical:
its own tools, wiki renderers, routing hooks, and REST endpoints.
Domains depend only on generic infrastructure (``tools/``, ``wiki/``,
``core/``) — never on each other.

To add a new domain (e.g. paper retrieval) create
``domains/<name>/`` with a ``bootstrap.py`` exposing a registration
function and call it from ``app.py``. To remove a domain, comment
out its bootstrap call; nothing else changes.
"""
