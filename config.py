# ============================================================
# config.py
# ============================================================
import os
from datetime import timedelta

from dotenv import load_dotenv

# Load .env exactly once, before any os.getenv() is evaluated.
load_dotenv(override=False)


def _bool(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, default))
    except (TypeError, ValueError):
        return default


class Config:
    # ---- Core Flask ----
    ENV = os.getenv("FLASK_ENV", "production")
    DEBUG = _bool("FLASK_DEBUG", False)
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")
    JSON_SORT_KEYS = False

    # ---- Supabase ----
    SUPABASE_URL = os.getenv("SUPABASE_URL")
    SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")
    SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

    # Local JWT verification (zero network hops on every request).
    # New Supabase projects using asymmetric keys -> set SUPABASE_JWKS_URL instead.
    SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET")
    SUPABASE_JWKS_URL = os.getenv("SUPABASE_JWKS_URL")
    SUPABASE_JWT_AUD = os.getenv("SUPABASE_JWT_AUD", "authenticated")

    # ---- Session cookies ----
    ACCESS_COOKIE_NAME = os.getenv("ACCESS_COOKIE_NAME", "sacco_at")
    REFRESH_COOKIE_NAME = os.getenv("REFRESH_COOKIE_NAME", "sacco_rt")
    ACCESS_COOKIE_TTL = _int("ACCESS_COOKIE_TTL", 60 * 60)              # 1h
    REFRESH_COOKIE_TTL = _int("REFRESH_COOKIE_TTL", 60 * 60 * 24 * 30)  # 30d
    COOKIE_SECURE = _bool("COOKIE_SECURE", True)
    COOKIE_SAMESITE = os.getenv("COOKIE_SAMESITE", "Lax")

    # ---- Rate limiting ----
    RATELIMIT_STORAGE_URI = os.getenv("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_DEFAULT = os.getenv("RATELIMIT_DEFAULT", "300 per minute")

    @classmethod
    def validate(cls) -> None:
        missing = [
            name
            for name in ("SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY")
            if not getattr(cls, name)
        ]
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")


class DevelopmentConfig(Config):
    DEBUG = True
    COOKIE_SECURE = False


class TestingConfig(Config):
    TESTING = True
    DEBUG = True
    COOKIE_SECURE = False
    RATELIMIT_ENABLED = False


CONFIG_MAP = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": Config,
}