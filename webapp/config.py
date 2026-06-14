"""Runtime configuration for the v2ph clone web app.

All paths can be overridden via environment variables so the app is portable
between machines / archive locations:

    V2PH_ARCHIVE   archive root that holds album folders, _avatars, _covers
    V2PH_DB        path to the SQLite profile DB
    V2PH_HOST      bind host (default 127.0.0.1)
    V2PH_PORT      bind port (default 8000)
"""
from __future__ import annotations

import os

ARCHIVE_ROOT = os.environ.get("V2PH_ARCHIVE", r"D:\v2ph_archive")
DB_PATH = os.environ.get("V2PH_DB", os.path.join(ARCHIVE_ROOT, "v2ph_profiles.sqlite3"))
AVATAR_DIR = os.environ.get("V2PH_AVATARS", os.path.join(ARCHIVE_ROOT, "_avatars"))
COVER_DIR = os.environ.get("V2PH_COVERS", os.path.join(ARCHIVE_ROOT, "_covers"))

HOST = os.environ.get("V2PH_HOST", "127.0.0.1")
PORT = int(os.environ.get("V2PH_PORT", "8000"))

SITE_NAME = os.environ.get("V2PH_SITE_NAME", "V2PH")
SITE_TAGLINE = "高清写真图片站"

PAGE_SIZE = 24          # album cards per listing page
PHOTOS_PER_PAGE = 40    # photos per page on an album viewer

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")

# --------------------------------------------------------------------------- #
# Accounts / subscriptions (stored in a SEPARATE read-write DB so the archive
# profile DB stays pristine & read-only).
# --------------------------------------------------------------------------- #
_HERE = os.path.dirname(os.path.abspath(__file__))
USER_DB = os.environ.get("V2PH_USER_DB", os.path.join(_HERE, "app_data.sqlite3"))

# Secret key for session cookies. Set V2PH_SECRET in production; otherwise a
# stable per-install key is derived and cached next to the user DB.
SECRET_KEY = os.environ.get("V2PH_SECRET", "")

FREE_PREVIEW_PHOTOS = int(os.environ.get("V2PH_FREE_PREVIEW", "6"))

# Subscription plans: key -> (days, price RMB). Mock checkout only.
PLANS = {
    "month": {"days": 30, "price": 28},
    "quarter": {"days": 90, "price": 68},
    "year": {"days": 365, "price": 198},
}
