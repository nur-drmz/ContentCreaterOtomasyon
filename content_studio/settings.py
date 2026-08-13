from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = "pre-alpha-local-development-only"
DEBUG = True
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.messages",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "studio.apps.StudioConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "content_studio.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

WSGI_APPLICATION = "content_studio.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "data" / "pre_alpha.sqlite3",
        "OPTIONS": {"timeout": 20},
    }
}

AUTH_PASSWORD_VALIDATORS = []
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Europe/Istanbul"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

SOURCE_LIBRARY_ROOT = Path(r"G:\Youtube kaynak videolar")
GENERATED_MEDIA_ROOT = Path(r"G:\AIContent")
MODEL_ROOT = Path(r"D:\AIContent\models")
MAX_SOURCE_SIZE_BYTES = 20 * 1024**3
MAX_SOURCE_DURATION_SECONDS = 180 * 60
MIN_GENERATED_FREE_BYTES = 20 * 1024**3
MIN_MODEL_FREE_BYTES = 10 * 1024**3
MIN_FREE_VRAM_BYTES = 8 * 1024**3
FFPROBE_PATH = None
FFPROBE_TIMEOUT_SECONDS = 30
FFMPEG_PATH = None
FFMPEG_TIMEOUT_SECONDS = 600
WHISPER_MODEL_NAME = "distil-large-v3.5"
PROXY_MAX_HEIGHT = 480

# Voice-over module (studio/voice/). This is the first feature that calls an
# external service; docs/pre-alpha-local/06-security-privacy.md §3 requires
# an explicit cost limit and per-request approval before any such call.
VOICE_COST_LIMIT_USD = 5.00
ELEVENLABS_COST_PER_CHARACTER = "0.00018"
