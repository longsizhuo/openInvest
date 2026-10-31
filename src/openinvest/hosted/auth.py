"""Auth0 resource-token verification and opaque, revocable portal sessions."""
import jwt
from jwt import PyJWKClient

SCOPES = {"invest:read", "invest:write", "invest:analyze"}


class AuthenticationError(ValueError):
    pass


class Authenticator:
    def __init__(self, settings, store, jwks=None):
        self.settings, self.store = settings, store
        self.jwks = jwks or PyJWKClient(settings.issuer + ".well-known/jwks.json", lifespan=300)

    def bearer(self, authorization):
        if not authorization.startswith("Bearer "):
            raise AuthenticationError("Authentication required")
        token = authorization[7:]
        if len(token) > 16384:
            raise AuthenticationError("Invalid credential")
        if token.startswith("oi_"):
            credential = self.store.authenticate(token, "pat")
            if not credential:
                raise AuthenticationError("Invalid credential")
            return self.store.account(credential["owner"]), SCOPES
        try:
            key = self.jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256"],
                                issuer=self.settings.issuer, audience=self.settings.audience,
                                options={"require": ["exp", "iat", "iss", "aud", "sub"]})
            if not isinstance(claims["sub"], str) or not claims["sub"]:
                raise ValueError()
            client = claims.get("azp") or claims.get("client_id")
            # Authenticated resource requests must never provision an account or grant.
            with self.store.db() as db:
                row = db.execute("SELECT id FROM accounts WHERE subject=? AND deleted=0", (claims["sub"],)).fetchone()
            account = self.store.account(row[0]) if row else None
            if not account or not client or not self.store.connect_oauth(account["id"], client):
                raise ValueError()
            if claims["iat"] <= account["revoked_before"]:
                raise ValueError()
            scopes = set(str(claims.get("scope", "")).split())
            return account, scopes
        except Exception:
            # Do not return provider errors containing tokens/claims to clients or logs.
            raise AuthenticationError("Invalid or unauthorized credential") from None


def resource_metadata(settings):
    return {"resource": settings.audience, "authorization_servers": [settings.issuer],
            "scopes_supported": sorted(SCOPES), "bearer_methods_supported": ["header"]}
