"""
Django settings for voucher_system project.
Production-ready for Railway.app (HTTPS, PostgreSQL, Persistent Media, Secure Cookies)
"""

import os
from pathlib import Path
import dj_database_url

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get(
    'SECRET_KEY',
    'django-insecure-fallback-key-only-for-local-testing'
)

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.environ.get('DEBUG', 'False') == 'True'

# Detect Railway environment
IS_RAILWAY = os.environ.get('RAILWAY_ENVIRONMENT') or os.environ.get('PORT')

# ALLOWED_HOSTS – Auto-detect Railway domain
RAILWAY_STATIC_URL = os.environ.get('RAILWAY_STATIC_URL', '')
if RAILWAY_STATIC_URL:
    ALLOWED_HOSTS = [RAILWAY_STATIC_URL.split('//')[-1].split('/')[0]]
else:
    ALLOWED_HOSTS = ['*']  # Fallback for local dev

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'vouchers.apps.VouchersConfig',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'voucher_system.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'voucher_system.wsgi.application'

# Database – PostgreSQL via DATABASE_URL on Railway, SQLite local
DATABASES = {
    'default': dj_database_url.config(
        default='sqlite:///' + str(BASE_DIR / 'db.sqlite3'),
        conn_max_age=600
    )
}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# Media files (uploads) – Persistent volume on Railway
MEDIA_URL = '/media/'
if IS_RAILWAY:
    MEDIA_ROOT = Path('/app/media')
    print(f"[RAILWAY] MEDIA_ROOT = {MEDIA_ROOT}")
else:
    MEDIA_ROOT = BASE_DIR / 'media'
    print(f"[LOCAL] MEDIA_ROOT = {MEDIA_ROOT}")

# Auto-create media directories
os.makedirs(MEDIA_ROOT / 'vouchers' / 'attachments', exist_ok=True)
os.makedirs(MEDIA_ROOT / 'vouchers' / 'particulars', exist_ok=True)

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Login / Logout
LOGIN_REDIRECT_URL = 'home'
LOGOUT_REDIRECT_URL = 'home'

# REST Framework
REST_FRAMEWORK = {
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
    'DEFAULT_PARSER_CLASSES': [
        'rest_framework.parsers.JSONParser',
        'rest_framework.parsers.FormParser',
        'rest_framework.parsers.MultiPartParser',
    ],
}

# =============================================================================
# PRODUCTION SECURITY SETTINGS (RAILWAY-OPTIMIZED)
# =============================================================================

if IS_RAILWAY:
    # HTTPS & Proxy (Railway terminates TLS)
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SECURE_SSL_REDIRECT = False  # Railway already redirects HTTP → HTTPS

    # Secure cookies (required for CSRF over HTTPS)
    CSRF_COOKIE_SECURE = True
    SESSION_COOKIE_SECURE = True

    # Additional security headers
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = 'DENY'

    # HSTS (enable after testing)
    # SECURE_HSTS_SECONDS = 31536000
    # SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    # SECURE_HSTS_PRELOAD = True

# Logging – Console for Railway, file for local
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
        },
        'file': {
            'class': 'logging.FileHandler',
            'filename': BASE_DIR / 'debug.log',
        },
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': 'INFO' if IS_RAILWAY else 'INFO',
        },
        'vouchers': {
            'handlers': ['console'],
            'level': 'DEBUG' if IS_RAILWAY else 'DEBUG',
        },
    },
}