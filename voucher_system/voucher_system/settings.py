"""
Django settings for voucher_system project.
"""

from pathlib import Path
import os
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

# -----------------------------
# Security
# -----------------------------
SECRET_KEY = os.getenv("SECRET_KEY")  # No fallback in prod; set env var on Render

DEBUG = os.getenv("RENDER", None) is None   # DEBUG=False on Render, True on local

# Updated: Include Render's specific hostname
ALLOWED_HOSTS = [
    os.getenv('RENDER_EXTERNAL_HOSTNAME', '.onrender.com'),  # Covers Render + local
    "localhost",
    "127.0.0.1",
]

# -----------------------------
# Installed Apps
# -----------------------------
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

# -----------------------------
# Middleware
# -----------------------------
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",  # Must be after SecurityMiddleware
    
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = 'voucher_system.urls'

# -----------------------------
# Templates
# -----------------------------
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
                'vouchers.context_processors.company_context',
            ],
        },
    },
]

WSGI_APPLICATION = 'voucher_system.wsgi.application'

# -----------------------------
# Database (SQLite local → PostgreSQL on Render)
# -----------------------------
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

# If Render provides DATABASE_URL → use PostgreSQL
if os.getenv("DATABASE_URL"):
    DATABASES["default"] = dj_database_url.parse(
        os.getenv("DATABASE_URL"),
        conn_max_age=600,
        conn_health_checks=True,
    )

# -----------------------------
# Password Validators
# -----------------------------
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# -----------------------------
# Localization
# -----------------------------
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# -----------------------------
# Static Files (Render-Compatible)
# -----------------------------
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

# -----------------------------
# Media Files
# -----------------------------
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# Optional: Example for S3 media storage (add 'storages' to INSTALLED_APPS and requirements.txt)
# if os.getenv('USE_S3'):  # Set this env var on Render if using S3
#     import boto3
#     from storages.backends.s3boto3 import S3Boto3Storage
#     AWS_ACCESS_KEY_ID = os.getenv('AWS_ACCESS_KEY_ID')
#     AWS_SECRET_ACCESS_KEY = os.getenv('AWS_SECRET_ACCESS_KEY')
#     AWS_STORAGE_BUCKET_NAME = os.getenv('AWS_STORAGE_BUCKET_NAME')
#     AWS_S3_REGION_NAME = 'us-east-1'  # Your region
#     AWS_S3_CUSTOM_DOMAIN = f'{AWS_STORAGE_BUCKET_NAME}.s3.amazonaws.com'
#     AWS_DEFAULT_ACL = None
#     AWS_S3_OBJECT_PARAMETERS = {'CacheControl': 'max-age=86400'}
#     AWS_LOCATION = 'media'
#     DEFAULT_FILE_STORAGE = 'storages.backends.s3boto3.S3Boto3Storage'
#     MEDIA_URL = f'https://{AWS_S3_CUSTOM_DOMAIN}/{AWS_LOCATION}/'

# -----------------------------
# Logging (Updated: Console-only for Render)
# -----------------------------
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {  # Removed file handler to avoid write errors on Render
            'level': 'DEBUG' if DEBUG else 'INFO',
            'class': 'logging.StreamHandler',
        },
    },
    'loggers': {
        'vouchers': {
            'handlers': ['console'],
            'level': 'DEBUG' if DEBUG else 'INFO',
            'propagate': True,
        },
        'django': {  # Optional: Log Django errors to console
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}

# -----------------------------
# Auth Redirects
# -----------------------------
LOGIN_REDIRECT_URL = 'home'
LOGOUT_REDIRECT_URL = 'home'