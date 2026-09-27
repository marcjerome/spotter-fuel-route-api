"""Per-client rate limits on the expensive paths.

Synchronous MILP solves hold a web worker for up to a few seconds, so they
get a tight limit that nudges clients to `mode=async`. Job submissions get a
looser one. Cheap requests (greedy/lp) are not throttled.
"""

from rest_framework.throttling import SimpleRateThrottle


def _params(request):
    return request.data if request.method == "POST" else request.query_params


class SyncMilpThrottle(SimpleRateThrottle):
    scope = "sync_milp"

    def get_cache_key(self, request, view):
        params = _params(request)
        if params.get("strategy") != "milp" or params.get("mode") == "async":
            return None
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class JobSubmitThrottle(SimpleRateThrottle):
    scope = "jobs"

    def get_cache_key(self, request, view):
        if _params(request).get("mode") != "async":
            return None
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}
