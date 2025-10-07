import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Optional for local dev; harmless on Vercel (it won't read local .env)
load_dotenv(BASE_DIR / ".env")

# ---- Core ----
SECRET_KEY = "KkL6v8c3wYH$e@7pR2nZqX1!G9mJt4b^S0u+EaF*(d)_rA5CzVQ"  # hard-coded
DEBUG = False  # production-safe on Vercel

ALLOWED_HOSTS = [
    "localhost",
    "127.0.0.1",
    ".vercel.app",
    "h2h-backend-vpk9.vercel.app",  # your deployment
]

# If you POST from the Vercel domain, add it to CSRF trusted origins
CSRF_TRUSTED_ORIGINS = [
    "https://h2h-backend-vpk9.vercel.app",
]

# ---- Apps ----
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "corsheaders",
    "h2h",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
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

# ---- CORS ----
# If you have a frontend domain, add it here (comma-separated via env still works locally)
CORS_ALLOWED_ORIGINS = [
    *[o.strip() for o in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if o.strip()]
]
CORS_ALLOW_CREDENTIALS = True

# ---- DRF ----
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.AllowAny",
    ],
}

# ---- Cognito (still via env so you can rotate secrets without code changes) ----
COGNITO = {
    "REGION": os.getenv("COGNITO_REGION"),
    "DOMAIN": os.getenv("COGNITO_DOMAIN"),
    "USER_POOL_ID": os.getenv("COGNITO_USER_POOL_ID"),
    "CLIENT_ID": os.getenv("COGNITO_APP_CLIENT_ID"),
    "CLIENT_SECRET": os.getenv("COGNITO_APP_CLIENT_SECRET"),
    "REDIRECT_URI": os.getenv("COGNITO_REDIRECT_URI"),
    "LOGOUT_REDIRECT_URI": os.getenv("COGNITO_LOGOUT_REDIRECT_URI"),
    "SCOPES": os.getenv("COGNITO_SCOPES", "openid email"),
}

# ---- Razorpay (optional; leave blank until configured) ----
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET")
