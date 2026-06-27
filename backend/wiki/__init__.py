"""answer cache layer.

The wiki sits between the v0.36 skill router and the agent loop's LLM
call. Skills that opt-in (``metadata.zlagent.wiki_cache.enabled: true``)
have their successful answers stored as ``WikiEntry`` rows keyed by a
canonical form of the user query. Subsequent paraphrased queries hit
the cache and skip the LLM entirely, returning in <100ms.

This mirrors the layering MemoryStore uses: a thin storage class
(``WikiStore``) plus a tiny pure-function helper (``normalize_query``).
We deliberately avoid embeddings for the canonical-form
approach is good enough for the travel-guide MVP and keeps us
dependency-free. The lookup() implementation is the swap point if a
later version wants vectors.
"""

from .normalizer import normalize_query, normalize_for_kind
from .store import WikiStore, WikiHit

__all__ = ["WikiStore", "WikiHit", "normalize_query", "normalize_for_kind"]
