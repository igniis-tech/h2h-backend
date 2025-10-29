
# auth_utils.py
import base64
import json
from urllib.parse import urlencode, quote

import requests
from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured

from .models import UserProfile
import requests
from django.core.exceptions import ImproperlyConfigured

def refresh_with_cognito(refresh_token: str, timeout: int = 15) -> dict:
    cfg = _cfg()
    base = _domain_base(cfg["DOMAIN"])
    token_url = f"{base}/oauth2/token"
    data = {
        "grant_type": "refresh_token",
        "client_id": cfg["CLIENT_ID"],
        "refresh_token": refresh_token,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if cfg.get("CLIENT_SECRET"):
        headers["Authorization"] = _basic_auth_header(cfg["CLIENT_ID"], cfg["CLIENT_SECRET"])
    resp = requests.post(token_url, headers=headers, data=data, timeout=timeout)
    payload = resp.json() if resp.headers.get("content-type","").startswith("application/json") else None
    if not resp.ok:
        raise RuntimeError(payload.get("error_description") if isinstance(payload, dict) else f"HTTP {resp.status_code}")
    return payload or {}


# ----------------------------
# Config helpers
# ----------------------------
def _cfg() -> dict:
    """
    Lazy-read Cognito config so Django can start/migrate even if env vars
    aren’t set yet. We validate only when a function is called.
    """
    cfg = getattr(settings, "COGNITO", {}) or {}
    return {
        "DOMAIN": (cfg.get("DOMAIN") or "").strip().rstrip("/"),
        "CLIENT_ID": cfg.get("CLIENT_ID"),
        "CLIENT_SECRET": cfg.get("CLIENT_SECRET"),   # may be None (public client)
        "REDIRECT_URI": cfg.get("REDIRECT_URI"),
        "LOGOUT_REDIRECT_URI": cfg.get("LOGOUT_REDIRECT_URI") or cfg.get("REDIRECT_URI"),
        "SCOPES": cfg.get("SCOPES") or "openid email profile phone address",
    }


def _require(keys: list[str], cfg: dict):
    missing = [k for k in keys if not cfg.get(k)]
    if missing:
        raise ImproperlyConfigured(
            "Missing COGNITO settings: " + ", ".join(missing) +
            ". Set them in environment or settings.COGNITO."
        )


def _domain_base(raw: str) -> str:
    """
    Normalize the Cognito domain to an absolute base URL with scheme
    and no trailing slash.
    """
    d = (raw or "").strip().rstrip("/")
    if not d:
        raise ImproperlyConfigured("COGNITO.DOMAIN not configured")
    if not d.startswith("http://") and not d.startswith("https://"):
        d = "https://" + d
    return d


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    token = f"{client_id}:{client_secret}".encode()
    return "Basic " + base64.b64encode(token).decode()


# ----------------------------
# Authorization URL
# ----------------------------
def build_authorize_url(state: str, redirect_uri: str | None = None) -> str:
    """
    Build the hosted-UI authorize URL.
    If redirect_uri is provided, it overrides settings.COGNITO.REDIRECT_URI.
    """
    cfg = _cfg()
    _require(["DOMAIN", "CLIENT_ID"], cfg)
    ru = (redirect_uri or cfg.get("REDIRECT_URI") or "").strip()
    if not ru:
        raise ImproperlyConfigured("COGNITO.REDIRECT_URI not configured and no redirect_uri provided")

    base = _domain_base(cfg["DOMAIN"])
    auth_url = f"{base}/oauth2/authorize"
    params = {
        "client_id": cfg["CLIENT_ID"],
        "response_type": "code",
        "scope": cfg["SCOPES"],
        "redirect_uri": ru,
        "state": state or "",
    }
    return f"{auth_url}?{urlencode(params)}"


# ----------------------------
# Token exchange
# ----------------------------
def exchange_code_for_tokens(
    code: str,
    *,
    redirect_uri: str | None = None,
    timeout: int = 15,
) -> dict:
    """
    Redeem an authorization code with Cognito.
    IMPORTANT: redirect_uri must MATCH the one used at /authorize.

    Returns the JSON from Cognito (access_token, id_token, token_type,
    expires_in, and possibly refresh_token).
    """
    cfg = _cfg()
    _require(["DOMAIN", "CLIENT_ID"], cfg)
    ru = (redirect_uri or cfg.get("REDIRECT_URI") or "").strip()
    if not ru:
        raise ImproperlyConfigured("redirect_uri required (COGNITO.REDIRECT_URI or function arg)")

    base = _domain_base(cfg["DOMAIN"])
    token_url = f"{base}/oauth2/token"

    data = {
        "grant_type": "authorization_code",
        "client_id": cfg["CLIENT_ID"],
        "code": code,
        "redirect_uri": ru,
    }

    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    # Use HTTP Basic only if a client secret is configured (confidential client).
    if cfg.get("CLIENT_SECRET"):
        headers["Authorization"] = _basic_auth_header(cfg["CLIENT_ID"], cfg["CLIENT_SECRET"])

    resp = requests.post(token_url, headers=headers, data=data, timeout=timeout)

    # Try to surface a helpful error from Cognito if non-200
    try:
        payload = resp.json()
    except Exception:
        payload = None

    if not resp.ok:
        msg = None
        if isinstance(payload, dict):
            msg = payload.get("error_description") or payload.get("error")
        msg = msg or f"OAuth token exchange failed (HTTP {resp.status_code})"
        raise RuntimeError(msg)

    return payload if isinstance(payload, dict) else resp.json()


# ----------------------------
# UserInfo
# ----------------------------
def fetch_userinfo(access_token: str, timeout: int = 15) -> dict:
    cfg = _cfg()
    _require(["DOMAIN"], cfg)
    base = _domain_base(cfg["DOMAIN"])
    userinfo_url = f"{base}/oauth2/userInfo"
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(userinfo_url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


# ----------------------------
# Django user mapping
# ----------------------------
def get_or_create_user_from_userinfo(userinfo: dict) -> User:
    """
    Map Cognito OIDC claims into Django User + UserProfile.
    Requires your app client to allow reading the attributes, and
    scopes including: openid email profile phone address.
    """
    sub = userinfo.get("sub")
    if not sub:
        raise ValueError("Cognito userinfo missing 'sub'")

    # Core claims
    email = (userinfo.get("email") or "").strip().lower()
    email_verified = bool(userinfo.get("email_verified") is True)

    # Name resolution
    full_name = (userinfo.get("name") or "").strip()
    if not full_name:
        given = (userinfo.get("given_name") or "").strip()
        family = (userinfo.get("family_name") or "").strip()
        composed = f"{given} {family}".strip()
        full_name = composed or (email.split("@")[0] if email else sub)

    # Optional claims
    gender = (userinfo.get("gender") or "").strip()
    phone_number = (userinfo.get("phone_number") or "").strip()
    phone_number_verified = bool(userinfo.get("phone_number_verified") is True)

    # Address: OIDC may return a dict with 'formatted'
    addr = userinfo.get("address")
    if isinstance(addr, dict):
        address_text = addr.get("formatted") or json.dumps(addr, ensure_ascii=False)
    else:
        address_text = (addr or "").strip()

    # --- Create/lookup User ---
    user = None
    if email:
        user = User.objects.filter(email__iexact=email).first()

    if not user:
        base_username = (email.split("@")[0] if email else f"cognito_{sub[:8]}")[:25]
        uname = base_username
        i = 1
        while User.objects.filter(username=uname).exists():
            i += 1
            uname = f"{base_username}{i}"[:30]
        user = User.objects.create(
            username=uname,
            email=email or "",
            first_name="",
            last_name="",
        )
    else:
        if email and user.email != email:
            user.email = email
            user.save(update_fields=["email"])

    # --- Upsert Profile ---
    profile, created = UserProfile.objects.get_or_create(
        user=user,
        defaults={
            "cognito_sub": sub,
            "full_name": full_name,
            "gender": gender,
            "phone_number": phone_number,
            "address": address_text,
            "email_verified": email_verified,
            "phone_number_verified": phone_number_verified,
        },
    )

    changed = False
    if not profile.cognito_sub:
        profile.cognito_sub = sub; changed = True
    if profile.full_name != full_name:
        profile.full_name = full_name; changed = True
    if profile.gender != gender:
        profile.gender = gender; changed = True
    if profile.phone_number != phone_number:
        profile.phone_number = phone_number; changed = True
    if profile.address != address_text:
        profile.address = address_text; changed = True
    if profile.email_verified != email_verified:
        profile.email_verified = email_verified; changed = True
    if profile.phone_number_verified != phone_number_verified:
        profile.phone_number_verified = phone_number_verified; changed = True
    if changed:
        profile.save()

    return user


# ----------------------------
# Logout URL
# ----------------------------
def build_logout_url(id_token_hint: str | None = None, logout_redirect_uri: str | None = None) -> str:
    cfg = _cfg()
    _require(["DOMAIN", "CLIENT_ID"], cfg)
    base = _domain_base(cfg["DOMAIN"])
    logout_url = f"{base}/logout"
    params = {
        "client_id": cfg["CLIENT_ID"],
        "logout_uri": (logout_redirect_uri or cfg["LOGOUT_REDIRECT_URI"] or cfg["REDIRECT_URI"]),
    }
    if id_token_hint:
        params["id_token_hint"] = id_token_hint
    return f"{logout_url}?{urlencode(params)}"



