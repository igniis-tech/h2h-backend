import base64
import json
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured

from .models import UserProfile


def _cfg() -> dict:
    """
    Lazy-read Cognito config so Django can start/migrate even if env vars aren’t set yet.
    We only validate when an SSO function is actually called.
    """
    cfg = getattr(settings, "COGNITO", {}) or {}
    domain = (cfg.get("DOMAIN") or "").rstrip("/")
    return {
        "DOMAIN": domain,
        "CLIENT_ID": cfg.get("CLIENT_ID"),
        "CLIENT_SECRET": cfg.get("CLIENT_SECRET"),
        "REDIRECT_URI": cfg.get("REDIRECT_URI"),
        "LOGOUT_REDIRECT_URI": cfg.get("LOGOUT_REDIRECT_URI") or cfg.get("REDIRECT_URI"),
        # Ask for all common attributes by default; you can narrow via settings if needed.
        "SCOPES": cfg.get("SCOPES") or "openid email profile phone address",
    }


def _require(keys: list[str], cfg: dict):
    missing = [k for k in keys if not cfg.get(k)]
    if missing:
        raise ImproperlyConfigured(
            "Missing COGNITO settings: " + ", ".join(missing) +
            ". Set them in environment or settings.COGNITO."
        )


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    token = f"{client_id}:{client_secret}".encode()
    return "Basic " + base64.b64encode(token).decode()


def build_authorize_url(state: str) -> str:
    cfg = _cfg()
    _require(["DOMAIN", "CLIENT_ID", "REDIRECT_URI"], cfg)
    auth_url = f"https://{cfg['DOMAIN']}/oauth2/authorize"
    params = {
        "client_id": cfg["CLIENT_ID"],
        "response_type": "code",
        "scope": cfg["SCOPES"],
        "redirect_uri": cfg["REDIRECT_URI"],
        "state": state or "",
    }
    return f"{auth_url}?{urlencode(params)}"


def exchange_code_for_tokens(code: str):
    cfg = _cfg()
    _require(["DOMAIN", "CLIENT_ID", "CLIENT_SECRET", "REDIRECT_URI"], cfg)
    token_url = f"https://{cfg['DOMAIN']}/oauth2/token"
    headers = {
        "Authorization": _basic_auth_header(cfg["CLIENT_ID"], cfg["CLIENT_SECRET"]),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "grant_type": "authorization_code",
        "client_id": cfg["CLIENT_ID"],
        "code": code,
        "redirect_uri": cfg["REDIRECT_URI"],
    }
    resp = requests.post(token_url, headers=headers, data=data, timeout=15)
    resp.raise_for_status()
    return resp.json()  # {access_token, id_token, refresh_token, ...}


def fetch_userinfo(access_token: str):
    cfg = _cfg()
    _require(["DOMAIN"], cfg)
    userinfo_url = f"https://{cfg['DOMAIN']}/oauth2/userInfo"
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(userinfo_url, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()  # {sub, email, name, given_name, family_name, phone_number, address, ...}


def get_or_create_user_from_userinfo(userinfo: dict) -> User:
    """
    Map Cognito OIDC claims into Django User + UserProfile.
    Requires your App Client to grant READ for the attributes
    and your OAuth scopes to include: openid email profile phone address.
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
            # Keep first/last optional; full_name is stored on profile
            first_name="",
            last_name="",
        )
    else:
        # Ensure we keep email in sync if it was empty before
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

    # Update existing profile fields if changed
    changed = False
    if not profile.cognito_sub:
        profile.cognito_sub = sub
        changed = True
    if profile.full_name != full_name:
        profile.full_name = full_name
        changed = True
    if profile.gender != gender:
        profile.gender = gender
        changed = True
    if profile.phone_number != phone_number:
        profile.phone_number = phone_number
        changed = True
    if profile.address != address_text:
        profile.address = address_text
        changed = True
    if profile.email_verified != email_verified:
        profile.email_verified = email_verified
        changed = True
    if profile.phone_number_verified != phone_number_verified:
        profile.phone_number_verified = phone_number_verified
        changed = True
    if changed:
        profile.save()

    return user


def build_logout_url(id_token_hint: str | None = None) -> str:
    cfg = _cfg()
    _require(["DOMAIN", "CLIENT_ID"], cfg)
    logout_url = f"https://{cfg['DOMAIN']}/logout"
    params = {
        "client_id": cfg["CLIENT_ID"],
        "logout_uri": cfg["LOGOUT_REDIRECT_URI"] or cfg["REDIRECT_URI"],
    }
    if id_token_hint:
        params["id_token_hint"] = id_token_hint
    return f"{logout_url}?{urlencode(params)}"
