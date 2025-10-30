
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

LEeway_SECONDS = 120  # tolerate small clock skew

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
    """
    Verify RS256 signature + issuer + exp/iat, but handle audience differences:
      - ID token: validate aud == CLIENT_ID (aud may be str or list)
      - Access token: validate client_id == CLIENT_ID (no aud claim)
    """
    cfg = _cfg()
    if not cfg["issuer"] or not cfg["client_id"]:
        raise AuthenticationFailed("Cognito issuer/client not configured")

    # Peek (no signature) just to learn token_use
    try:
        unverified = jwt.decode(token, options={"verify_signature": False, "verify_aud": False})
    except Exception:
        unverified = {}

    token_use = unverified.get("token_use")  # "id" or "access"

    # Get signing key via JWKS
    signing_key = _jwks_client().get_signing_key_from_jwt(token).key

    # Verify signature/issuer/time. Do NOT verify aud here.
    try:
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            issuer=cfg["issuer"],
            options={
                "require": ["exp", "iat", "iss"],
                "verify_aud": False,  # we’ll check aud/client_id ourselves below
            },
            leeway=LEeway_SECONDS,
        )
    except Exception as e:
        raise AuthenticationFailed(f"JWT verify failed: {e}")

    # Now enforce audience semantics by token type
    app_id = cfg["client_id"]

    if token_use == "id" or ("aud" in claims and token_use is None):
        aud = claims.get("aud")
        if isinstance(aud, (list, tuple, set)):
            ok = app_id in aud
        else:
            ok = (aud == app_id)
        if not ok:
            raise AuthenticationFailed("Invalid audience for ID token")
    elif token_use == "access":
        if claims.get("client_id") != app_id:
            raise AuthenticationFailed("Invalid client_id for access token")
    else:
        # Unknown; allow either check to pass
        if not (claims.get("aud") == app_id or claims.get("client_id") == app_id):
            raise AuthenticationFailed("Token audience/client mismatch")

    # Final sanity: still ensure token_use is acceptable
    if claims.get("token_use") not in ("id", "access"):
        raise AuthenticationFailed("Invalid token_use")

    return claims

def _get_or_create_user_from_claims(claims: dict) -> User:
    sub   = claims.get("sub")
    email = (claims.get("email") or "").strip().lower()
    if not sub:
        raise AuthenticationFailed("sub missing in token")

    uname = f"cog_{sub}"[:30]
    user, _ = User.objects.get_or_create(username=uname, defaults={"email": email or ""})
    if email and user.email != email:
        user.email = email
        user.save(update_fields=["email"])
    return user

class CognitoJWTAuthentication(BaseAuthentication):
    def authenticate(self, request) -> Optional[Tuple[User, None]]:
        auth = get_authorization_header(request).split()
        if not auth or auth[0].lower() != b"bearer":
            return None
        if len(auth) == 1:
            raise AuthenticationFailed("Invalid Authorization header")
        token = auth[1].decode("utf-8")

        claims = _decode_and_verify(token)
        user = _get_or_create_user_from_claims(claims)
        return (user, None)
