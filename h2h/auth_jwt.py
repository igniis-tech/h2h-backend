# h2h/auth_jwt.py
import time
import json
import requests
from functools import lru_cache
from typing import Tuple, Optional

from django.conf import settings
from django.contrib.auth.models import User
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed

import jwt  # PyJWT
from jwt import PyJWKClient

def _cfg():
    c = getattr(settings, "COGNITO", {}) or {}
    domain = (c.get("DOMAIN") or "").rstrip("/")
    if domain and not domain.startswith(("http://", "https://")):
        domain = "https://" + domain
    region = (c.get("REGION") or "").strip()
    pool   = (c.get("USER_POOL_ID") or "").strip()
    client = (c.get("CLIENT_ID") or "").strip()
    issuer = f"https://cognito-idp.{region}.amazonaws.com/{pool}" if (region and pool) else None
    jwks   = f"{issuer}/.well-known/jwks.json" if issuer else None
    return {
        "domain": domain, "region": region, "user_pool_id": pool, "client_id": client,
        "issuer": issuer, "jwks_url": jwks,
    }

@lru_cache(maxsize=1)
def _jwks_client() -> PyJWKClient:
    cfg = _cfg()
    if not cfg["jwks_url"]:
        raise AuthenticationFailed("Cognito JWKS not configured")
    return PyJWKClient(cfg["jwks_url"])

def _decode_and_verify(token: str) -> dict:
    cfg = _cfg()
    if not cfg["issuer"] or not cfg["client_id"]:
        raise AuthenticationFailed("Cognito issuer/client not configured")

    jwks_client = _jwks_client()
    signing_key = jwks_client.get_signing_key_from_jwt(token).key

    # Accept RS256 only; verify issuer/audience/exp
    claims = jwt.decode(
        token,
        signing_key,
        algorithms=["RS256"],
        audience=cfg["client_id"],          # matches your App Client ID
        issuer=cfg["issuer"],
        options={"require": ["exp", "iat", "iss", "aud"]},
    )

    # Additional hardening: ensure this is an access or id token
    tuse = claims.get("token_use")
    if tuse not in ("access", "id"):
        raise AuthenticationFailed("Invalid token_use")

    # Small leeway
    now = int(time.time())
    if claims.get("exp", 0) < now:
        raise AuthenticationFailed("Token expired")

    return claims

def _get_or_create_user_from_claims(claims: dict) -> User:
    sub   = claims.get("sub")
    email = (claims.get("email") or "").strip().lower()

    if not sub:
        raise AuthenticationFailed("sub missing in token")

    # Prefer stable username derived from Cognito sub
    uname = f"cog_{sub}"[:30]
    user, created = User.objects.get_or_create(
        username=uname,
        defaults={"email": email or ""},
    )
    # Update email if changed
    if email and user.email != email:
        user.email = email
        user.save(update_fields=["email"])
    return user

class CognitoJWTAuthentication(BaseAuthentication):
    """
    DRF authentication that accepts `Authorization: Bearer <JWT>` issued by AWS Cognito.
    Stateless; no sessions or CSRF required.
    """
    def authenticate(self, request) -> Optional[Tuple[User, None]]:
        auth = get_authorization_header(request).split()
        if not auth or auth[0].lower() != b"bearer":
            return None
        if len(auth) == 1:
            raise AuthenticationFailed("Invalid Authorization header")
        token = auth[1].decode("utf-8")

        try:
            claims = _decode_and_verify(token)
            user = _get_or_create_user_from_claims(claims)
            return (user, None)
        except Exception as e:
            raise AuthenticationFailed(str(e))
