# h2h/auth_cognito.py
from __future__ import annotations
from typing import Optional, Tuple

import time
import logging

import jwt
from jwt import PyJWKClient, ExpiredSignatureError
from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed

logger = logging.getLogger(__name__)


def _cfg(key: str, default=None):
    """Read COGNITO.* from settings with a default."""
    return getattr(settings, "COGNITO", {}).get(key, default)


def _issuer_and_jwks() -> tuple[str, str]:
    """
    Prefer explicit settings; else derive standard Cognito URLs:
      issuer = https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}
      jwks   = issuer + "/.well-known/jwks.json"
    """
    issuer = _cfg("ISSUER")
    jwks = _cfg("JWKS_URL")
    region = _cfg("REGION")
    pool = _cfg("USER_POOL_ID")

    if not issuer:
        if not (region and pool):
            raise RuntimeError("COGNITO.ISSUER or (REGION & USER_POOL_ID) must be configured")
        issuer = f"https://cognito-idp.{region}.amazonaws.com/{pool}"

    if not jwks:
        jwks = issuer.rstrip("/") + "/.well-known/jwks.json"

    return issuer, jwks


class CognitoJWTAuthentication(BaseAuthentication):
    """
    DRF authentication for AWS Cognito OIDC JWTs (ID or Access tokens).

    Behavior:
      • If Authorization header is missing/malformed → return None (anonymous).
      • If a Bearer token is present → verify RS256 signature, issuer and (optional) audience.
      • On invalid/expired token:
          - by default (STRICT_INVALID_TOKENS=False): return None (anonymous) to keep public endpoints working.
          - if STRICT_INVALID_TOKENS=True: raise AuthenticationFailed -> 401.
      • Maps token claims to a Django user:
          - First by profile.cognito_sub (from 'sub'/'username')
          - Fallback by email (case-insensitive)
        (Assumes users are created at /auth/sso/callback; otherwise we raise user_not_found)

    Settings (under COGNITO):
      REGION: "ap-south-1"
      USER_POOL_ID: "ap-south-1_XXXXXXXXX"
      CLIENT_ID / AUDIENCE: your app client id (used to verify 'aud' on ID tokens)
      ISSUER: override full issuer if desired
      JWKS_URL: override JWKS URL if desired
      STRICT_INVALID_TOKENS: bool (default False) – raise on bad tokens instead of soft-anon
    """

    _jwks_client: Optional[PyJWKClient] = None
    _jwks_url: Optional[str] = None
    _last_log_at: float = 0.0

    def authenticate_header(self, request) -> str:
        return 'Bearer realm="api"'

    # ---- Internals ---------------------------------------------------------

    def _get_jwks_client(self) -> PyJWKClient:
        issuer, jwks_url = _issuer_and_jwks()
        if self._jwks_client is None or self._jwks_url != jwks_url:
            # cache JWKS client; PyJWKClient internally caches keys
            self._jwks_client = PyJWKClient(jwks_url, cache_keys=True)
            self._jwks_url = jwks_url
        return self._jwks_client

    def _soft_or_raise(self, msg: str) -> None:
        """
        If STRICT_INVALID_TOKENS is true → raise 401; otherwise return None (treat as anonymous).
        """
        strict = bool(_cfg("STRICT_INVALID_TOKENS", False))
        if strict:
            raise AuthenticationFailed(msg)
        # soft: behave as if no credentials were provided
        return None

    # ---- Main hook ---------------------------------------------------------

    def authenticate(self, request) -> Optional[Tuple[object, dict]]:
        # Accept only proper Bearer tokens; otherwise allow anonymous.
        header = get_authorization_header(request)
        if not header:
            return None

        parts = header.split()
        if len(parts) == 0 or parts[0].lower() != b"bearer":
            return None
        if len(parts) != 2:
            # "Bearer" with no token or extra parts → treat as anonymous to keep public endpoints working.
            return None

        raw_token = parts[1].decode("utf-8", errors="ignore").strip()
        if not raw_token:
            return None

        # Verify JWT
        try:
            issuer, _ = _issuer_and_jwks()
            audience = _cfg("AUDIENCE") or _cfg("CLIENT_ID")  # verify_aud only if provided

            jwks_client = self._get_jwks_client()
            signing_key = jwks_client.get_signing_key_from_jwt(raw_token).key

            decoded = jwt.decode(
                raw_token,
                signing_key,
                algorithms=["RS256"],
                audience=audience if audience else None,
                issuer=issuer,
                options={"verify_aud": bool(audience)},  # only verify aud if we have one
            )

            token_use = (decoded.get("token_use") or "").lower()
            if token_use not in {"id", "access"}:
                return self._soft_or_raise("invalid_token_use")

            # Claims we can use to map the user
            sub = decoded.get("sub") or decoded.get("username")
            email = (decoded.get("email") or "").strip().lower()

            User = get_user_model()
            user = None
            if sub:
                # assumes a OneToOne Field 'profile' with 'cognito_sub' on your Profile model
                user = User.objects.filter(profile__cognito_sub=sub).first()
            if not user and email:
                user = User.objects.filter(email__iexact=email).first()

            if not user:
                return self._soft_or_raise("user_not_found")

            return (user, decoded)

        except ExpiredSignatureError:
            return self._soft_or_raise("token_expired")
        except AuthenticationFailed:
            # Already handled
            raise
        except Exception as e:
            # Don't spam logs
            now = time.time()
            if now - self._last_log_at > 30:
                logger.debug("Cognito auth error: %r", e)
                self._last_log_at = now
            return self._soft_or_raise("invalid_token")
