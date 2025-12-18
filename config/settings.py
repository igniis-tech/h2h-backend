import os
from pathlib import Path
from dotenv import load_dotenv
import dj_database_url
from corsheaders.defaults import default_headers  # ✅ add
import re  # keep

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# ---- Core ----
SECRET_KEY = 'django-insecure-#o*1r_%m6d$ofi^h%*r-_lmt6hi2(rucujd9=)d-g*sfnu@kpy'
DEBUG = True

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

ALLOWED_HOSTS = [
    "localhost",
    "127.0.0.1",
    "h2h-backend-vpk9.vercel.app",
    ".vercel.app",
    "highwaytoheal.in",  # allow vercel preview hosts (Host header)
    "highwaytohill.shop",
    "highwaytoheal.org",
    "admin.highwaytoheal.org",
]

# --- behind proxy / https ---
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

# ---- Cookies (stateless API; keep secure for previews) ----
# ---- Cookies (stateless API; keep secure for previews) ----
SESSION_COOKIE_SAMESITE = "None" if not DEBUG else "Lax"
CSRF_COOKIE_SAMESITE   = "None" if not DEBUG else "Lax"
SESSION_COOKIE_SECURE   = not DEBUG
CSRF_COOKIE_SECURE      = not DEBUG

# ---- Apps ----
INSTALLED_APPS = [
    'jazzmin',
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework.authtoken",
    "corsheaders",
    "h2h",
]

# ---- Middleware (CORS first) ----
MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ---- DB ----
if DATABASE_URL:
    DATABASES = {
        "default": dj_database_url.parse(
            DATABASE_URL,
            conn_max_age=600,
            ssl_require=True,
        )
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_PASSWORD_VALIDATORS = []

# ---- I18N/Timezone ----
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

# ---- Static ----
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"
STATICFILES_DIRS = [BASE_DIR / "static"]

# ---- CORS ----
CORS_ORIGIN_ALLOW_ALL = True # Enabled for Flutter Dev
CORS_ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:60311", 
    "https://h2h-frontend-new-ta3o.vercel.app",
    "https://highwaytoheal.in",
    "https://www.highwaytohill.shop",
    "https://highwaytoheal.org",
    "https://admin.highwaytoheal.org",
]
CORS_ALLOWED_ORIGIN_REGEXES = [
    r"^https://.*\.vercel\.app$",
    r"^https://.*\.highwaytoheal\.in$",
    r"^https://.*\.highwaytohill.shop$",
    r"^https://.*\.highwaytoheal\.org$",
]
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
CORS_ALLOW_HEADERS = list(default_headers) + [
    "Authorization",   # ✅ ensure case-exact
    "X-CSRFToken",     # ✅ common CSRF header
]

# ---- CSRF ----
CSRF_TRUSTED_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "https://h2h-frontend-new-ta3o.vercel.app",
    "https://h2h-backend-vpk9.vercel.app",
    "https://*.vercel.app",
    "https://highwaytoheal.in",
    "https://www.highwaytohill.shop",
    "https://highwaytoheal.org",
    "https://admin.highwaytoheal.org",
]

# ---- DRF ----
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.TokenAuthentication",
        "h2h.auth_cognito.CognitoJWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
}

from datetime import timedelta
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=60),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": False,
    "BLACKLIST_AFTER_ROTATION": False,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

# ---- Cognito (env-driven) ----
COGNITO = {
    "REGION": os.getenv("COGNITO_REGION"),
    "DOMAIN": os.getenv("COGNITO_DOMAIN"),
    "USER_POOL_ID": os.getenv("COGNITO_USER_POOL_ID"),
    "CLIENT_ID": os.getenv("COGNITO_APP_CLIENT_ID"),
    "CLIENT_SECRET": os.getenv("COGNITO_APP_CLIENT_SECRET"),
    "REDIRECT_URI": os.getenv("COGNITO_REDIRECT_URI"),
    "LOGOUT_REDIRECT_URI": os.getenv("COGNITO_LOGOUT_REDIRECT_URI"),
    "SCOPES": os.getenv("COGNITO_SCOPES", "openid email profile"),
}

# ---- Payment return URLs ----
PAYMENT_SUCCESS_URL = os.getenv("PAYMENT_SUCCESS_URL", "http://localhost:5173/register?payment=success")
PAYMENT_FAILED_URL  = os.getenv("PAYMENT_FAILED_URL",  "http://localhost:5173/register?payment=failed")
PAYMENT_RETURN_TO   = os.getenv("PAYMENT_RETURN_TO",   "http://localhost:5173/register")

# ---- Razorpay ----
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET")

# ---- Dev logging (helps catch JWT reasons in dev/preview) ----
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {
        "h2h": {"handlers": ["console"], "level": "DEBUG"},
        "h2h.auth": {"handlers": ["console"], "level": "WARNING"},
        "django.request": {"handlers": ["console"], "level": "WARNING"},
    },
}
