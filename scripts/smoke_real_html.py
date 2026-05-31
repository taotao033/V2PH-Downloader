"""Validate ProfileExtractor against the user's saved real-world HTML.

The user occasionally saves the live actor + album pages from
v2ph.com (via "Save Page As") into ``album_screenshots/*.html``. When
those files are present, this smoke test parses them and asserts the
exact field values we expect for the Miku-Tanaka pages. If the files
are missing the script skips gracefully so it can run in any clone.

Run with:  .venv\\Scripts\\python.exe -X utf8 scripts\\smoke_real_html.py
"""

from __future__ import annotations

import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lxml import html as lxml_html  # noqa: E402

from v2dl.scraper.profiles import ProfileExtractor  # noqa: E402
from v2dl.scraper.tools import UrlHandler  # noqa: E402


HERE = Path(__file__).resolve().parent.parent
SCREEN_DIR = HERE / "album截图"
ACTOR_URL = "https://www.v2ph.com/actor/Miku-Tanaka?hl=zh-Hans"
ALBUM_URL = "https://www.v2ph.com/album/amo7nn4a.html?hl=zh-Hans"


def _nfc(s):
    """Normalise to NFC so saved-from-browser kana (NFD) compares equal."""
    if isinstance(s, str):
        return unicodedata.normalize("NFC", s)
    return s


def _check(label: str, got, want) -> None:
    ng, nw = _nfc(got), _nfc(want)
    ok = ng == nw
    print(f"  [{ 'OK' if ok else 'FAIL' }] {label}: got={got!r} want={want!r}")
    if not ok:
        # Also print code points to help debug NFC/NFD or look-alike confusables.
        if isinstance(got, str) and isinstance(want, str):
            print(f"        got codepoints: {[hex(ord(c)) for c in got[:40]]}")
            print(f"        want codepoints: {[hex(ord(c)) for c in want[:40]]}")
        raise AssertionError(f"{label}: got={got!r} want={want!r}")


def _find_one(pattern: str) -> Path | None:
    matches = sorted(SCREEN_DIR.glob(pattern))
    return matches[0] if matches else None


def validate_actor() -> bool:
    p = _find_one("*Miku Tanaka*高清写真*.html")
    if p is None:
        print("[skip] no actor html found in album_screenshots/")
        return True
    print(f"\n=== actor: {p.name} ===")
    tree = lxml_html.fromstring(p.read_text(encoding="utf-8"))

    display = UrlHandler.extract_listing_display_name(tree)
    _check("listing display_name", display, "田中美久 、 Miku Tanaka")

    profile = ProfileExtractor.extract_actor(tree, ACTOR_URL)
    _check("actor.actor_url", profile.actor_url, "https://www.v2ph.com/actor/Miku-Tanaka")
    _check("actor.actor_slug", profile.actor_slug, "Miku-Tanaka")
    _check("actor.name", profile.name, "田中美久 、 Miku Tanaka")
    _check("actor.birthday", profile.birthday, "2001-09-12")
    _check("actor.height", profile.height, "149")
    _check("actor.from_location", profile.from_location, "日本熊本县")
    _check("actor.zodiac", profile.zodiac, "处女座")
    _check("actor.blood_type", profile.blood_type, "B")
    _check("actor.profession", profile.profession, "偶像、艺人")
    _check("actor.hobbies", profile.hobbies, "电影、唱歌")
    _check("actor.listed_album_count", profile.listed_album_count, 151)
    # Avatar URL is mirrored in <meta property="og:image"> on the live
    # site; "Save Page As" preserves that meta tag verbatim.
    _check(
        "actor.avatar_url",
        profile.avatar_url,
        "https://cdn.v2ph.com/actor/QODhcN7rGsjlxJGy.jpg",
    )
    assert profile.bio and "HKT48" in profile.bio and "熊本县" in profile.bio, (
        f"unexpected bio: {profile.bio!r}"
    )
    print(f"  [OK] actor.bio length={len(profile.bio)} (contains HKT48 + 熊本县)")
    return True


def validate_album() -> bool:
    p = _find_one("*写真集*.html")
    if p is None:
        print("[skip] no album html found in album_screenshots/")
        return True
    print(f"\n=== album: {p.name} ===")
    tree = lxml_html.fromstring(p.read_text(encoding="utf-8"))

    profile = ProfileExtractor.extract_album(tree, ALBUM_URL)
    _check("album.album_url", profile.album_url, "https://www.v2ph.com/album/amo7nn4a.html")
    _check("album.album_slug", profile.album_slug, "amo7nn4a")
    _check("album.title", profile.title, "田中美久写真集 「もっと、ぜんぶ、ほんと」")
    _check("album.release_date", profile.release_date, "2026-04-27")
    _check("album.listed_photo_count", profile.listed_photo_count, 110)

    assert len(profile.models) == 1, f"models={profile.models}"
    assert profile.models[0].name == "田中美久"
    assert profile.models[0].url == "https://www.v2ph.com/actor/Miku-Tanaka?hl=zh-Hans"
    print(f"  [OK] album.models[0]={profile.models[0]}")

    assert len(profile.tags) == 1, f"tags={profile.tags}"
    assert profile.tags[0].name == "日本嫩模"
    assert profile.tags[0].url == "https://www.v2ph.com/category/japanese-girl-models?hl=zh-Hans"
    print(f"  [OK] album.tags[0]={profile.tags[0]}")
    return True


def main() -> int:
    if not SCREEN_DIR.exists():
        print(f"[skip] {SCREEN_DIR} not present")
        return 0
    validate_actor()
    validate_album()
    print("\n[OK] real-html smoke passed (or all sources missing)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
