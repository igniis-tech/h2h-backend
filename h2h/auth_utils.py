import base64
import requests
from urllib.parse import urlencode
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.contrib.auth.models import User
from .models import UserProfile


def _cfg() -> dict:
    """
    Lazy-read Cognito config so Django can start/migrate
    even if env vars aren’t set yet. We only validate when
    an SSO function is actually called.
    """
    cfg = getattr(settings, "COGNITO", {}) or {}
    # Normalize and provide safe fallbacks
    domain = (cfg.get("DOMAIN") or "").rstrip("/")
    return {
        "DOMAIN": domain,
        "CLIENT_ID": cfg.get("CLIENT_ID"),
        "CLIENT_SECRET": cfg.get("CLIENT_SECRET"),
        "REDIRECT_URI": cfg.get("REDIRECT_URI"),
        "LOGOUT_REDIRECT_URI": cfg.get("LOGOUT_REDIRECT_URI") or cfg.get("REDIRECT_URI"),
        "SCOPES": cfg.get("SCOPES") or "openid email",
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
    return resp.json()  # {sub, email, given_name, ...}


def get_or_create_user_from_userinfo(info: dict) -> User:
    sub = info.get("sub")
    email = (info.get("email") or "").lower()
    given = info.get("given_name", "")
    family = info.get("family_name", "")

    if not sub:
        raise ValueError("Cognito userinfo missing 'sub'")

    user = User.objects.filter(email=email).first() if email else None
    if not user:
        base_username = (email.split("@")[0] if email else f"cognito_{sub[:8]}")[:25]
        uname = base_username
        i = 1
        while User.objects.filter(username=uname).exists():
            i += 1
            uname = f"{base_username}{i}"[:30]
        user = User.objects.create(
            username=uname, email=email, first_name=given, last_name=family
        )

    profile, _ = UserProfile.objects.get_or_create(
        user=user,
        defaults={"cognito_sub": sub, "full_name": f"{given} {family}".strip()},
    )
    if profile.cognito_sub != sub:
        profile.cognito_sub = sub
        profile.save(update_fields=["cognito_sub"])

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
