from pathlib import Path
import os
import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-only-secret")
DEBUG = os.getenv("DEBUG", "True").lower() == "true"
ALLOWED_HOSTS = [x.strip() for x in os.getenv("ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if x.strip()]
CSRF_TRUSTED_ORIGINS = [
    x.strip()
    for x in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",")
    if x.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_celery_results",
    "django_celery_beat",
    "prospects",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
    )
}

AUTH_PASSWORD_VALIDATORS = []
LANGUAGE_CODE = "fr-fr"
TIME_ZONE = "Europe/Paris"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

USER_AGENT = os.getenv("USER_AGENT", "ProspectPilotBot/2.0")
PAGESPEED_API_KEY = os.getenv("PAGESPEED_API_KEY", "")
GOOGLE_OAUTH_CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
GOOGLE_OAUTH_REDIRECT_URI = os.getenv(
    "GOOGLE_OAUTH_REDIRECT_URI",
    "http://127.0.0.1:8000/integrations/search-console/callback/",
)
CRAWL_MAX_PAGES = int(os.getenv("CRAWL_MAX_PAGES", "12"))
CRAWL_DELAY_SECONDS = float(os.getenv("CRAWL_DELAY_SECONDS", "0.6"))
SEARCH_SCAN_SECONDS = float(os.getenv("SEARCH_SCAN_SECONDS", "25"))
SEARCH_SCAN_MAX_RESULTS = int(os.getenv("SEARCH_SCAN_MAX_RESULTS", "25"))
SEARCH_SCAN_CRAWL_PAGES = int(os.getenv("SEARCH_SCAN_CRAWL_PAGES", "3"))
SEARCH_SITE_CANDIDATES = int(os.getenv("SEARCH_SITE_CANDIDATES", "6"))
SEARCH_IMPORT_MAX_RESULTS = int(os.getenv("SEARCH_IMPORT_MAX_RESULTS", "75"))
SEARCH_API_MIN_INTERVAL = float(os.getenv("SEARCH_API_MIN_INTERVAL", "0.25"))
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000")

# ETAPE 4 — pipeline d'acquisition PredictNeed IA
ACQUISITION_PRESCORE_TOP_RATIO = float(os.getenv("ACQUISITION_PRESCORE_TOP_RATIO", "0.25"))
ACQUISITION_SITE_CONFIDENCE_THRESHOLD = int(os.getenv("ACQUISITION_SITE_CONFIDENCE_THRESHOLD", "55"))
ACQUISITION_QUICK_SCAN_PAGES = int(os.getenv("ACQUISITION_QUICK_SCAN_PAGES", "5"))
ACQUISITION_DOMAIN_DELAY_SECONDS = float(os.getenv("ACQUISITION_DOMAIN_DELAY_SECONDS", "1.0"))
ACQUISITION_SITE_CACHE_HOURS = int(os.getenv("ACQUISITION_SITE_CACHE_HOURS", "72"))

# ETAPE 20 — API serveur-à-serveur ProspectPilot <-> PredictNeed IA
PREDICTNEED_API_URL = os.getenv("PREDICTNEED_API_URL", "")
PREDICTNEED_SHARED_SECRET = os.getenv("PREDICTNEED_SHARED_SECRET", "")
PROSPECTPILOT_SHARED_SECRET = os.getenv("PROSPECTPILOT_SHARED_SECRET", "")
PROSPECTPILOT_PUBLIC_API_URL = os.getenv("PROSPECTPILOT_PUBLIC_API_URL", PUBLIC_BASE_URL)

# Mission 2 — identité e-mail PredictNeed IA (voir EmailComplianceProfile pour le juridique)
ALLOWED_SENDER_IDENTITIES = [
    x.strip() for x in os.getenv("ALLOWED_SENDER_IDENTITIES", "").split(",") if x.strip()
]
EMAIL_DKIM_SELECTOR = os.getenv("EMAIL_DKIM_SELECTOR", "")

CELERY_BROKER_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
CELERY_RESULT_BACKEND = "django-db"
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 600
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = os.getenv("SECURE_SSL_REDIRECT", "False" if DEBUG else "True").lower() == "true"
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "False" if DEBUG else "True").lower() == "true"
CSRF_COOKIE_SECURE = os.getenv("CSRF_COOKIE_SECURE", "False" if DEBUG else "True").lower() == "true"
SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "0" if DEBUG else "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = os.getenv("SECURE_HSTS_INCLUDE_SUBDOMAINS", "False").lower() == "true"
SECURE_HSTS_PRELOAD = os.getenv("SECURE_HSTS_PRELOAD", "False").lower() == "true"
X_FRAME_OPTIONS = os.getenv("X_FRAME_OPTIONS", "DENY")

EMAIL_BACKEND = os.getenv("EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = os.getenv("EMAIL_HOST", "ssl0.ovh.net")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "465"))
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "False").lower() == "true"
EMAIL_USE_SSL = os.getenv("EMAIL_USE_SSL", "True").lower() == "true"
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", EMAIL_HOST_USER or "contact@example.com")
EMAIL_SENDER_NAME = os.getenv("EMAIL_SENDER_NAME", "Ariane - ProspectPilot Pro")
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", DEFAULT_FROM_EMAIL)
COMPANY_NAME = os.getenv("COMPANY_NAME", "ProspectPilot Pro")
# Mission 4.1 : ne jamais fabriquer une adresse légale non confirmée. Reste vide
# tant que COMPANY_POSTAL_ADDRESS n'est pas explicitement renseigné (Fly secret).
COMPANY_POSTAL_ADDRESS = os.getenv("COMPANY_POSTAL_ADDRESS", "")
EMAIL_BATCH_LIMIT = int(os.getenv("EMAIL_BATCH_LIMIT", "20"))

PRODUCT_URL = os.getenv("PRODUCT_URL", PUBLIC_BASE_URL)

DROPCONTACT_API_KEY = os.getenv("DROPCONTACT_API_KEY", "")
APOLLO_API_KEY = os.getenv("APOLLO_API_KEY", "")
KASPR_API_KEY = os.getenv("KASPR_API_KEY", "")
LEMLIST_API_KEY = os.getenv("LEMLIST_API_KEY", "")
