"""Re-fetch page 1 of already-downloaded albums and fill in the new fields
(company_id, volume_number, description) that were not collected by earlier
versions of v2dl.

Strategy
--------
* Source A – the profile DB: every album row where the new fields are all
  NULL is treated as "needs backfill". Use ``--force`` to re-process every
  row regardless.
* Source B – ``downloaded_albums.txt``: URLs that appear in the log but
  have NO DB row at all (the user ran v2dl before the profile DB feature
  existed, or before the DB path was configured).

For each candidate the script:
  1. Fetches page 1 of the album URL through the v2dl web bot (same
     Cloudflare-aware pipeline the main downloader uses).
  2. Calls ``ProfileExtractor.extract_album`` to parse company/volume/
     description (plus any other fields still missing).
  3. Upserts the company row (if a 机构 link was found) then the album row.
     ``upsert_album`` uses COALESCE for nullable fields so it never
     overwrites data that was already correct.
  4. **Preserves** the existing ``scraped_photo_count``, ``download_dest``,
     and ``actor_id`` by reading them from the DB and copying them onto the
     freshly-parsed profile before the upsert.

Typical usage::

    # Dry-run: show what would be done without touching the DB
    python scripts/backfill_album_profiles.py -d D:/v2ph_archive --dry-run

    # Backfill the first 100 albums (polite rate-limit)
    python scripts/backfill_album_profiles.py -d D:/v2ph_archive --limit 100

    # Full backfill of all 3000+ rows (will take a while)
    python scripts/backfill_album_profiles.py -d D:/v2ph_archive

    # Re-process even rows that already look complete
    python scripts/backfill_album_profiles.py -d D:/v2ph_archive --force
"""

from __future__ import annotations

import sys
import time
import asyncio
import argparse
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
for _p in (REPO_ROOT, SCRIPTS_DIR):
    sp = str(_p)
    if sp not in sys.path:
        sys.path.insert(0, sp)

from pathvalidate import sanitize_filename  # noqa: E402
from lxml import html as lxml_html  # noqa: E402

from v2dl import V2DLApp  # noqa: E402
from v2dl.cli import parse_arguments  # noqa: E402
from v2dl.scraper.core import ImageScraper  # noqa: E402
from v2dl.scraper.downloader import DownloadPathTool  # noqa: E402
from v2dl.scraper.profiles import (  # noqa: E402
    AlbumProfile,
    CompanyProfile,
    ProfileDB,
    ProfileExtractor,
    _strip_query,
    _url_segment,
)

# Sleep between page fetches to stay polite (seconds).
SLEEP_BETWEEN_ALBUMS = 3


# --------------------------------------------------------------------------- #
# argument parsing
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Backfill new profile fields (company, volume_number, description) "
            "for albums already in the v2dl profile DB or downloaded_albums.txt."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--destination", "-d",
        type=Path,
        required=True,
        help=(
            "Archive root. Profile DB defaults to "
            "<destination>/v2ph_profiles.sqlite3; "
            "download log defaults to <destination>/downloaded_albums.txt."
        ),
    )
    p.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Override the profile DB path.",
    )
    p.add_argument(
        "--log",
        type=Path,
        default=None,
        help="Override the downloaded_albums.txt path.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Cap how many albums to process this run (0 = no cap).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help=(
            "Re-fetch and re-upsert even albums whose new fields are "
            "already populated. Safe: upsert_album uses COALESCE so "
            "existing non-null values are never overwritten."
        ),
    )
    p.add_argument(
        "--sleep",
        type=float,
        default=SLEEP_BETWEEN_ALBUMS,
        help=f"Seconds between album page fetches (default {SLEEP_BETWEEN_ALBUMS}).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify candidates and print counts; do not open Chrome or touch the DB.",
    )
    p.add_argument(
        "--skip-log",
        action="store_true",
        help="Skip Source B (downloaded_albums.txt). Only process DB rows.",
    )
    p.add_argument(
        "--cover-dir",
        type=Path,
        default=None,
        help=(
            "Directory to save album cover images. "
            "Defaults to <destination>/_covers. "
            "Pass an empty string to disable cover download."
        ),
    )
    p.add_argument(
        "--no-cover",
        action="store_true",
        help="Skip cover image download entirely (only update text fields).",
    )
    p.add_argument(
        "--cover-only",
        action="store_true",
        help=(
            "Download covers for albums that already have cover_url in the DB "
            "but no cover_local_path. Skips page re-fetch entirely — useful when "
            "some album pages are Cloudflare-blocked but the CDN image is still "
            "accessible via curl-cffi. "
            "Incompatible with --no-cover."
        ),
    )
    return p


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _resolve_db_path(args: argparse.Namespace) -> Path:
    return Path(args.db) if args.db else Path(args.destination) / "v2ph_profiles.sqlite3"


def _resolve_log_path(args: argparse.Namespace) -> Path:
    return Path(args.log) if args.log else Path(args.destination) / "downloaded_albums.txt"


def _needs_backfill(row: dict[str, Any]) -> bool:
    """True when any of the three new fields is still NULL."""
    return (
        row.get("company_id") is None
        or row.get("volume_number") is None
        or row.get("description") is None
    )


def _load_db_candidates(
    db: ProfileDB, *, force: bool, cover_only: bool = False
) -> list[dict[str, Any]]:
    """Return album rows that need backfill.

    When ``cover_only`` is True, returns only rows that have a ``cover_url``
    but no ``cover_local_path`` (cover was not yet downloaded).
    """
    import sqlite3
    conn = sqlite3.connect(db.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        if force:
            rows = conn.execute("SELECT * FROM albums ORDER BY id").fetchall()
        elif cover_only:
            rows = conn.execute(
                """
                SELECT * FROM albums
                WHERE cover_url IS NOT NULL
                  AND cover_local_path IS NULL
                ORDER BY id
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM albums
                WHERE (company_id IS NULL
                       AND volume_number IS NULL
                       AND description IS NULL)
                   OR cover_url IS NULL
                ORDER BY id
                """
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _load_log_candidates(log_path: Path, db: ProfileDB) -> list[str]:
    """Return album URLs from downloaded_albums.txt that are NOT in the DB."""
    if not log_path.exists():
        return []
    candidates: list[str] = []
    try:
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # The log may contain "# " prefixed (processed) lines - skip.
            if line.startswith("# "):
                continue
            clean = _strip_query(line)
            if "/album/" not in clean:
                continue
            if db.get_album_by_url(clean) is None:
                candidates.append(clean)
    except OSError:
        pass
    return candidates


# --------------------------------------------------------------------------- #
# web-bot session (same as sync_actors_profile._ProfileSession)
# --------------------------------------------------------------------------- #
class _BackfillSession:
    def __init__(self, destination: Path) -> None:
        self.destination = destination
        self.app: V2DLApp | None = None
        self._cdn_warmed: bool = False

    async def __aenter__(self) -> "_BackfillSession":
        argv = ["-d", str(self.destination), "https://www.v2ph.com/"]
        args = parse_arguments(argv)
        args.terminate = True
        self.app = V2DLApp()
        await self.app.init(args)
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self.app is not None:
            try:
                self.app.bot.close_driver()
            except Exception:
                pass

    async def fetch_page1(self, album_url: str) -> str:
        assert self.app is not None
        from v2dl.scraper.tools import UrlHandler
        page1_url = UrlHandler.add_page_num(album_url, 1)
        return await self.app.bot.auto_page_scroll(page1_url, page_sleep=0)

    @property
    def image_strategy(self) -> ImageScraper:
        assert self.app is not None
        s = self.app.scraper.strategies.get("album_image")
        if not isinstance(s, ImageScraper):
            raise RuntimeError("album_image strategy not found")
        return s

    def _ensure_cdn_warmed(self, url: str) -> None:
        if self._cdn_warmed:
            return
        assert self.app is not None
        try:
            self.app.bot.ensure_cdn_warmed(url)
            self._cdn_warmed = True
        except Exception:
            pass

    async def download_cover(self, cover_url: str, dest: Path) -> Path | None:
        """Download a cover image; returns the actual written path or None."""
        assert self.app is not None
        self._ensure_cdn_warmed(cover_url)
        try:
            cookies = self.app.bot.get_cookies()
        except Exception:
            cookies = {}
        try:
            ok = await self.image_strategy.download_file(
                cover_url,
                dest,
                cookies=cookies,
                web_bot=self.app.bot,
            )
        except Exception as e:
            print(f"  [WARN] cover download raised: {e}", file=sys.stderr)
            return None
        if not ok:
            return None
        # download_file may rewrite suffix; locate whatever was written.
        if dest.exists():
            return dest
        for sibling in dest.parent.glob(f"{dest.stem}.*"):
            if sibling.is_file():
                return sibling
        return None


# --------------------------------------------------------------------------- #
# per-album processing
# --------------------------------------------------------------------------- #
def _upsert_with_preserved_counts(
    db: ProfileDB,
    profile: "ProfileExtractor.__class__",  # actually AlbumProfile
    existing_row: dict[str, Any] | None,
) -> int:
    """Upsert the profile while keeping existing scraped_photo_count /
    download_dest / actor_id when the freshly-parsed profile lacks them."""
    from v2dl.scraper.profiles import AlbumProfile

    if existing_row is not None:
        # Never let a fresh page-1 parse zero out the on-disk count.
        if profile.scraped_photo_count == 0 and existing_row.get("scraped_photo_count"):
            profile.scraped_photo_count = int(existing_row["scraped_photo_count"])
        if profile.download_dest is None and existing_row.get("download_dest"):
            profile.download_dest = str(existing_row["download_dest"])
        if profile.actor_id is None and existing_row.get("actor_id"):
            profile.actor_id = int(existing_row["actor_id"])
        if profile.company_id is None and existing_row.get("company_id"):
            profile.company_id = int(existing_row["company_id"])

    return db.upsert_album(profile)


def _cover_dest(album_slug: str | None, cover_dir: Path) -> Path:
    slug = album_slug or "album"
    safe = sanitize_filename(slug) or "album"
    return cover_dir / f"{safe}.jpg"


async def _process_album(
    session: _BackfillSession,
    db: ProfileDB,
    album_url: str,
    existing_row: dict[str, Any] | None,
    *,
    label: str,
    cover_dir: Path | None = None,
) -> bool:
    """Fetch page 1, upsert profile, optionally download cover. Returns True on success."""
    print(f"  [fetch] {label}")
    try:
        html_content = await session.fetch_page1(album_url)
    except Exception as e:
        print(f"  [ERROR] fetch failed: {e}", file=sys.stderr)
        return False

    if not html_content or "Just a moment" in html_content[:2000]:
        print("  [WARN] Cloudflare interstitial or empty body; skipping", file=sys.stderr)
        return False

    try:
        tree = lxml_html.fromstring(html_content)
    except Exception as e:
        print(f"  [ERROR] HTML parse failed: {e}", file=sys.stderr)
        return False

    try:
        profile = ProfileExtractor.extract_album(tree, album_url)
    except Exception as e:
        print(f"  [ERROR] extract_album failed: {e}", file=sys.stderr)
        return False

    # Preserve existing cover_local_path so we don't overwrite with None.
    if existing_row and existing_row.get("cover_local_path"):
        profile.cover_local_path = str(existing_row["cover_local_path"])
    if existing_row and existing_row.get("cover_url") and not profile.cover_url:
        profile.cover_url = str(existing_row["cover_url"])

    # Upsert company first, then set FK on the album profile.
    if profile.company is not None and profile.company.url:
        try:
            company_clean = _strip_query(profile.company.url)
            cp = CompanyProfile(
                company_url=company_clean,
                company_slug=_url_segment(company_clean, "company"),
                name=profile.company.name or None,
            )
            profile.company_id = db.upsert_company(cp)
        except Exception as e:
            print(f"  [WARN] company upsert failed: {e}", file=sys.stderr)

    try:
        album_id = _upsert_with_preserved_counts(db, profile, existing_row)
    except Exception as e:
        print(f"  [ERROR] album upsert failed: {e}", file=sys.stderr)
        return False

    bits = []
    if profile.company:
        bits.append(f"company={profile.company.name!r}")
    if profile.volume_number:
        bits.append(f"vol={profile.volume_number!r}")
    if profile.description:
        bits.append(f"desc={profile.description[:30]!r}…")
    if profile.cover_url:
        bits.append(f"cover_url=✓")

    # Download cover image if requested and not already on disk.
    if cover_dir is not None and profile.cover_url and not profile.cover_local_path:
        dest = _cover_dest(profile.album_slug, cover_dir)
        # Skip if any extension variant already exists.
        existing = dest if dest.exists() else next(
            (s for s in dest.parent.glob(f"{dest.stem}.*") if s.is_file()), None
        )
        if existing:
            try:
                db.update_album_cover_path(album_id, str(existing))
            except Exception:
                pass
            bits.append("cover=cached")
        else:
            actual = await session.download_cover(profile.cover_url, dest)
            if actual:
                try:
                    db.update_album_cover_path(album_id, str(actual))
                    bits.append(f"cover=saved")
                except Exception as e:
                    print(f"  [WARN] cover path update failed: {e}", file=sys.stderr)
            else:
                bits.append("cover=failed")

    print(f"  [OK] id={album_id} {' '.join(bits) or '(no new fields on page)'}")
    return True


async def _download_cover_only(
    session: _BackfillSession,
    db: ProfileDB,
    cover_dir: Path,
    *,
    limit: int = 0,
    sleep: float = SLEEP_BETWEEN_ALBUMS,
) -> tuple[int, int]:
    """Download covers for DB rows that have cover_url but no cover_local_path.

    Does NOT re-fetch the album page. Works even when album pages are
    Cloudflare-blocked, because the CDN image is fetched via curl-cffi
    with browser impersonation (same mechanism as regular photo downloads).

    Returns (succeeded, failed).
    """
    import sqlite3
    conn = sqlite3.connect(db.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, album_slug, cover_url, cover_local_path
        FROM albums
        WHERE cover_url IS NOT NULL
          AND (cover_local_path IS NULL OR cover_local_path = '')
        ORDER BY id
        """
    ).fetchall()
    conn.close()

    rows = [dict(r) for r in rows]
    if limit:
        rows = rows[:limit]

    total = len(rows)
    print(f"[cover-only] albums needing cover download: {total}")
    if total == 0:
        return 0, 0

    succeeded = failed = 0
    for i, row in enumerate(rows, 1):
        album_id = int(row["id"])
        slug = row.get("album_slug") or "album"
        cover_url = str(row["cover_url"])
        dest = _cover_dest(slug, cover_dir)

        # Check if already present on disk under any extension.
        existing = dest if dest.exists() else next(
            (s for s in dest.parent.glob(f"{dest.stem}.*") if s.is_file()), None
        )
        if existing:
            try:
                db.update_album_cover_path(album_id, str(existing))
            except Exception:
                pass
            print(f"  [{i}/{total}] cached  {slug}: {existing.name}")
            succeeded += 1
            continue

        print(f"  [{i}/{total}] download {slug}: {cover_url}")
        actual = await session.download_cover(cover_url, dest)
        if actual:
            try:
                db.update_album_cover_path(album_id, str(actual))
            except Exception as e:
                print(f"  [WARN] cover path update failed: {e}", file=sys.stderr)
            print(f"  [{i}/{total}] saved   → {actual.name}")
            succeeded += 1
        else:
            print(f"  [{i}/{total}] FAILED  {cover_url}", file=sys.stderr)
            failed += 1

        if i < total:
            time.sleep(sleep)

    return succeeded, failed


# --------------------------------------------------------------------------- #
# main loop
# --------------------------------------------------------------------------- #
def _resolve_cover_dir(args: argparse.Namespace) -> Path | None:
    """Return the cover directory, or None if download is disabled."""
    if args.no_cover:
        return None
    if args.cover_dir is not None:
        p = Path(args.cover_dir)
    else:
        p = Path(args.destination) / "_covers"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"[backfill] WARNING: cannot create cover dir {p}: {e}", file=sys.stderr)
        return None
    return p


async def _run(args: argparse.Namespace) -> int:
    if getattr(args, "cover_only", False) and getattr(args, "no_cover", False):
        raise SystemExit("[backfill] --cover-only and --no-cover are mutually exclusive.")

    db_path = _resolve_db_path(args)
    log_path = _resolve_log_path(args)

    if not db_path.exists():
        raise SystemExit(
            f"[backfill] Profile DB not found: {db_path}\n"
            "Run v2dl at least once with a profile_db_path configured, or "
            "run `sync_companies_profile.py` first to initialise the DB."
        )

    db = ProfileDB(db_path)
    cover_dir = _resolve_cover_dir(args)

    # ------------------------------------------------------------------ #
    # --cover-only branch: download covers for rows that already have
    # cover_url in the DB, bypassing the album-page fetch entirely.
    # ------------------------------------------------------------------ #
    if getattr(args, "cover_only", False):
        if cover_dir is None:
            raise SystemExit("[backfill] --cover-only requires a valid cover dir (not --no-cover).")
        print(f"[backfill] profile DB:  {db_path}")
        print(f"[backfill] cover dir:   {cover_dir}")
        print("[backfill] mode: COVER-ONLY (no page re-fetch)")
        if args.dry_run:
            import sqlite3
            conn = sqlite3.connect(db.db_path, timeout=30)
            n = conn.execute(
                "SELECT COUNT(*) FROM albums WHERE cover_url IS NOT NULL "
                "AND (cover_local_path IS NULL OR cover_local_path = '')"
            ).fetchone()[0]
            conn.close()
            print(f"[backfill] DRY RUN — {n} covers would be downloaded")
            return 0
        async with _BackfillSession(args.destination) as session:
            ok, fail = await _download_cover_only(
                session, db, cover_dir, limit=args.limit, sleep=args.sleep
            )
        print(f"\n[backfill] done. succeeded={ok} failed={fail}")
        return 0 if fail == 0 else 1

    # -- Source A: DB rows needing backfill --
    db_candidates = _load_db_candidates(db, force=args.force)

    # -- Source B: logged URLs not yet in DB --
    log_candidates: list[str] = []
    if not args.skip_log:
        log_candidates = _load_log_candidates(log_path, db)

    total = len(db_candidates) + len(log_candidates)
    print(f"[backfill] profile DB:   {db_path}")
    print(f"[backfill] download log: {log_path}")
    print(
        f"[backfill] candidates:   {len(db_candidates)} DB rows + "
        f"{len(log_candidates)} log-only URLs  =  {total} total"
    )
    if cover_dir:
        print(f"[backfill] cover dir:    {cover_dir}")
    else:
        print("[backfill] cover download: disabled (--no-cover)")

    if args.force:
        print("[backfill] --force: re-processing all DB rows")
    if args.dry_run:
        print("[backfill] DRY RUN — no Chrome, no DB writes")
        if args.limit:
            print(f"[backfill] (would cap to {args.limit})")
        return 0

    if total == 0:
        print("[backfill] nothing to do.")
        return 0

    # Apply limit
    if args.limit and total > args.limit:
        budget = args.limit
        if budget <= len(log_candidates):
            log_candidates = log_candidates[:budget]
            db_candidates = []
        else:
            log_candidates = log_candidates[:budget]
            db_candidates = db_candidates[:budget - len(log_candidates)]
        print(f"[backfill] capped to {args.limit} albums")

    succeeded = failed = 0
    processed = 0
    grand_total = len(db_candidates) + len(log_candidates)

    async with _BackfillSession(args.destination) as session:
        # DB rows first
        for i, row in enumerate(db_candidates, 1):
            url = str(row.get("album_url") or "")
            title = str(row.get("title") or url)[:60]
            label = f"DB row {i}/{len(db_candidates)}: {title}"
            ok = await _process_album(session, db, url, row, label=label, cover_dir=cover_dir)
            if ok:
                succeeded += 1
            else:
                failed += 1
            processed += 1
            if processed < grand_total:
                time.sleep(args.sleep)

        # Log-only URLs
        for i, url in enumerate(log_candidates, 1):
            label = f"log-only {i}/{len(log_candidates)}: {url}"
            ok = await _process_album(session, db, url, None, label=label, cover_dir=cover_dir)
            if ok:
                succeeded += 1
            else:
                failed += 1
            processed += 1
            if processed < grand_total:
                time.sleep(args.sleep)

    print(f"\n[backfill] done. succeeded={succeeded} failed={failed}")
    return 0 if failed == 0 else 1


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\n[backfill] interrupted by user", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
