from rest_framework.throttling import SimpleRateThrottle


class ExternalApiThrottle(SimpleRateThrottle):
    """Limits endpoints that call paid or rate-limited third-party APIs."""

    scope = "external"

    def get_cache_key(self, request, view):
        user = getattr(request, "user", None)
        user_id = getattr(user, "pk", None) or getattr(user, "id", None)
        if user_id is not None and getattr(user, "is_authenticated", False):
            ident = user_id
        else:
            ident = self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}


class AuthThrottle(ExternalApiThrottle):
    """Limits login and registration attempts per user or IP."""

    scope = "auth"
