import platform
from typing import Any

# ============== System ==============
BASE_URL = "https://www.v2ph.com"
AVAILABLE_LANGUAGES = ("zh-Hans", "ja", "zh-Hant", "en", "ko", "es", "fr", "ru", "de", "ar")
# v2ph only accepts the exact codes in AVAILABLE_LANGUAGES. Anything else
# (incl. the very common "zh") silently falls back to English on the server,
# which then poisons every scraped <a href> with ?hl=en. Map the obvious
# aliases users hand-write in config.yaml / CLI so we don't override their
# Chinese-locale intent with a bad fallback.
LANGUAGE_ALIASES: dict[str, str] = {
    "zh": "zh-Hans",
    "zh-cn": "zh-Hans",
    "zh_cn": "zh-Hans",
    "zh-hans": "zh-Hans",
    "zh-tw": "zh-Hant",
    "zh_tw": "zh-Hant",
    "zh-hk": "zh-Hant",
    "zh-hant": "zh-Hant",
    "jp": "ja",
    "ja-jp": "ja",
    "kr": "ko",
    "ko-kr": "ko",
}


def normalize_language(lang: str | None) -> str | None:
    """Return a canonical v2ph language code or ``None`` if unrecognised.

    Accepts case-insensitive input and the common aliases above. Returns
    ``None`` for empty / unknown values so callers can decide whether to
    fall back to a default or skip overriding entirely.
    """
    if not lang:
        return None
    key = lang.strip().lower()
    if not key:
        return None
    if key in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[key]
    for canonical in AVAILABLE_LANGUAGES:
        if canonical.lower() == key:
            return canonical
    return None
VALID_EXTENSIONS = (
    "jpg",
    "jpeg",
    "JPG",
    "JPEG",
    "png",
    "PNG",
    "gif",
    "bmp",
    "webp",
    "webm",
    "tiff",
    "svg",
    "mp4",
    "mov",
    "avi",
    "mkv",
    "wmv",
    "flv",
    "m4v",
)
IMAGE_PER_PAGE = 10

# For selenium webdriver
USER_OS = platform.system()
DEFAULT_CHROME_VERSION = "135.0.0.0"
DEFAULT_USER_AGENT = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{DEFAULT_CHROME_VERSION} Safari/537.36"


# Headers used by the image downloader against cdn.v2ph.com. The Accept
# value MUST be the image MIME list a real Chrome would send when loading
# an <img>; Cloudflare's bot-management on the CDN penalises requests
# whose Accept claims text/html while their sec-fetch-dest is "image".
HEADERS = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.v2ph.com/",
    "sec-fetch-dest": "image",
    "sec-fetch-mode": "no-cors",
    "sec-fetch-site": "same-site",
}


# ============== Default User Preference ==============
# The order should be same as model.py
DEFAULT_CONFIG: dict[str, dict[str, Any]] = {
    "static_config": {
        "bot_type": "drissionpage",
        "custom_user_agent": "",
        "custom_headers": {},
        "language": "ja",
        "chrome_args": None,
        "no_metadata": False,
        "force_download": False,
        # Re-scrape albums that are missing or partially downloaded while
        # still skipping albums that already have enough on-disk images.
        # Wired from ``sync_local.py --force-download``; NOT the same as
        # ``force_download`` which re-fetches everything blindly.
        "retry_incomplete": False,
        "terminate": False,
        "use_default_chrome_profile": False,
        "log_level": -1,
        "min_scroll_distance": 1000,
        "max_scroll_distance": 2000,
        "min_scroll_step": 300,
        "max_scroll_step": 500,
        "max_worker": 2,
        "rate_limit": 1000,
        "page_range": "",
        # path relative configurations
        "cookies_path": "",
        "download_dir": "",
        "metadata_path": "",
        "download_log_path": "",
        "system_log_path": "",
        # Do NOT pass default user-agent to config, it corrupts drissionpage's fingerprint
        "chrome_exec_path": {
            "Linux": "/usr/bin/google-chrome",
            "Darwin": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "Windows": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        },
        "chrome_profile_path": "",
        # Profile DB / avatar storage. Empty strings here mean "auto-derive
        # from download_dir at runtime"; set ``profile_db_path`` to a single
        # space (or any falsy-but-non-empty marker) in config.yaml is NOT
        # how you disable it - instead, leave it empty and disable by not
        # using the integrated extractor (planned: dedicated CLI flag).
        "profile_db_path": "",
        "avatar_dir": "",
        "cover_dir": "",
    },
    "runtime_config": {
        "url": "",
        "url_file": "",
    },
    "encryption_config": {
        "key_bytes": 32,
        "salt_bytes": 16,
        "nonce_bytes": 24,
        "kdf_ops_limit": 2**4,
        "kdf_mem_limit": 2**13,
    },
}
