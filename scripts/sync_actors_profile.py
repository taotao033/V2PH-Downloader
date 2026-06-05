"""Populate the actor profile DB (+ avatars) from ``data/sync/actors.xlsx``.

Standalone helper meant for **personal, offline archival only**. For each
row in ``data/sync/actors.xlsx`` (the watch list produced by
``sync_local.py discover actors``):

* Look up the actor URL in the SQLite profile DB
  (``<destination>/v2ph_profiles.sqlite3`` by default).
* **Skip** the row when the DB already has a complete entry
  (actor row + ``avatar_local_path`` on disk). Already-fetched actors
  never trigger another Chrome page load.
* **Avatar-only resume** when the DB has the actor row + cached
  ``avatar_url`` but the avatar binary never landed (e.g. the previous
  run was Ctrl-C'd, or Cloudflare 403'd the CDN once). We skip the
  HTML fetch entirely and just retry the image download.
* **Full fetch** otherwise: pull the actor profile page through the
  v2dl web bot, parse with :class:`ProfileExtractor`, upsert the
  :class:`ActorProfile`, then download the avatar via the same
  ``ImageScraper`` pipeline the main downloader uses (browser-fetch
  with Cloudflare clearance, falling back to curl-cffi / httpx).

The avatar is the whole point: the user reviews ``<destination>/_avatars``
after a run and decides which actors are worth a full album sync. So
the script optimises for "as many avatars on disk as possible per run"
rather than "fastest first row to DB". Concretely:

* One Chrome instance for the whole run (per-fetch boot costs 5-10s
  each and would dominate).
* CDN tab pre-warmed lazily on the first avatar URL so subsequent
  downloads reuse the same TLS / Cloudflare cookie context.
* :data:`SLEEP_BETWEEN_ACTORS` between actor *page* fetches (no extra
  sleep for avatar-only resume rows - that's just one CDN GET).
* Per-actor failures are caught and logged; the rest of the run
  continues.

Typical usage::

    # First bulk pass: every actor row in the xlsx, downloads avatars to
    #   D:\\v2ph_archive\\_avatars\\<actor_slug>.<ext>
    # and writes profiles to
    #   D:\\v2ph_archive\\v2ph_profiles.sqlite3
    python scripts/sync_actors_profile.py --destination "D:\\v2ph_archive"

    # Only the rows you've already ticked (是否采集 == 1)
    python scripts/sync_actors_profile.py -d "D:\\v2ph_archive" --only-selected

    # Bite-sized chunk: avoid blowing the daily Cloudflare quota
    python scripts/sync_actors_profile.py -d "D:\\v2ph_archive" --limit 50

    # Skip avatars entirely (text-only profile collection)
    python scripts/sync_actors_profile.py -d "D:\\v2ph_archive" --no-avatar

    # Override the avatar dir without changing the archive root
    python scripts/sync_actors_profile.py -d "D:\\v2ph_archive" \\
        --avatar-dir "D:\\actor_thumbnails"
"""

from __future__ import annotations

import sys
import time
import asyncio
import argparse
from pathlib import Path
from typing import Any

# Repo root + scripts/ both need to be on sys.path so we can import
# v2dl.* and the private helpers in sync_local.py.
REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
for _p in (REPO_ROOT, SCRIPTS_DIR):
    sp = str(_p)
    if sp not in sys.path:
        sys.path.insert(0, sp)

from lxml import html  # noqa: E402

from v2dl import V2DLApp  # noqa: E402
from v2dl.cli import parse_arguments  # noqa: E402
from v2dl.scraper.core import ImageScraper  # noqa: E402
from v2dl.scraper.profiles import (  # noqa: E402
    ActorProfile,
    ProfileDB,
    ProfileExtractor,
    _strip_query,
    _url_segment,
)

from sync_local import (  # noqa: E402
    DEFAULT_ACTORS_FILE,
    INTER_URL_SLEEP_SECONDS,
    _load_urls_from_xlsx,
    _load_urls_from_xlsx_all,
)


# Sleep between actor *page* fetches (HTML). Mirrors what
# ``sync_local.py discover actors`` uses between companies - one
# Chrome page load + one DB upsert per actor is the same order of
# magnitude. Avatar-only resume rows don't pay this cost (they're a
# single CDN GET, no HTML scrape).
SLEEP_BETWEEN_ACTORS = INTER_URL_SLEEP_SECONDS


# --------------------------------------------------------------------------- #
# argument parsing
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Read data/sync/actors.xlsx, fetch each actor's profile page "
            "(skipping rows already complete in the DB), upsert the basic "
            "info into the v2dl profile SQLite, and download each actor's "
            "avatar through the same Cloudflare-aware pipeline the main "
            "downloader uses."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_ACTORS_FILE,
        help=(
            "actors watch list. Default: data/sync/actors.xlsx. "
            "Must be the xlsx layout produced by "
            "`sync_local.py discover actors` (columns: url, name, total, "
            "是否采集(0/1))."
        ),
    )
    p.add_argument(
        "--destination",
        "-d",
        type=Path,
        required=True,
        help=(
            "Archive root. The profile DB defaults to "
            "<destination>/v2ph_profiles.sqlite3 and avatars land in "
            "<destination>/_avatars/<actor_slug>.<ext>. Either "
            "--db / --avatar-dir can override individually."
        ),
    )
    p.add_argument(
        "--db",
        type=Path,
        default=None,
        help=(
            "Override the profile DB path (default: "
            "<destination>/v2ph_profiles.sqlite3)."
        ),
    )
    p.add_argument(
        "--avatar-dir",
        type=Path,
        default=None,
        help=(
            "Override the avatar output dir (default: "
            "<destination>/_avatars). Ignored when --no-avatar is set."
        ),
    )
    p.add_argument(
        "--no-avatar",
        action="store_true",
        help=(
            "Skip avatar downloads entirely. ``avatar_url`` is still "
            "captured to the DB so a later run can fill in just the "
            "images without re-fetching the HTML."
        ),
    )
    p.add_argument(
        "--only-selected",
        action="store_true",
        help=(
            "Only process rows whose 是否采集 column is 1 in the xlsx. "
            "By default every row is processed (the profile DB is "
            "cheap to grow, and unticked actors get cached for free)."
        ),
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help=(
            "Cap how many actors to *process* this run (0 = no cap). "
            "Counts both full-fetch and avatar-only-resume actors; "
            "fully-skipped (already complete) rows do not consume the "
            "budget."
        ),
    )
    p.add_argument(
        "--force",
        action="store_true",
        help=(
            "Re-fetch the HTML and re-download the avatar even when "
            "the actor row + avatar already look complete. "
            "``upsert_actor`` merges via COALESCE so this is safe."
        ),
    )
    p.add_argument(
        "--sleep",
        type=float,
        default=SLEEP_BETWEEN_ACTORS,
        help=(
            f"Seconds to sleep between actor *page* fetches "
            f"(default {SLEEP_BETWEEN_ACTORS}). Avatar-only resume "
            "rows do not trigger this sleep."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Resolve inputs, classify each row (skip / avatar-only / "
            "full-fetch), then exit without opening Chrome or touching "
            "the DB."
        ),
    )
    return p


# --------------------------------------------------------------------------- #
# session: bot + scraper bundle
# --------------------------------------------------------------------------- #
class _ProfileSession:
    """Wrap a fully-initialised V2DLApp for HTML fetch + avatar download.

    The session lives for one ``async with`` block so the underlying
    Chromium boots exactly once. ``app.scraper.strategies['album_image']``
    is an :class:`ImageScraper` configured with the exact same headers /
    semaphore / curl-cffi fallback the main downloader uses, so avatars
    benefit from the same Cloudflare bypass.

    Notable side-effects on entry:

    * Sets ``download_dir`` from ``destination`` so
      ``ScrapeManager._avatar_dest_for`` resolves a writable path.
    * Optionally overrides ``avatar_dir`` from CLI.
    """

    def __init__(
        self,
        destination: Path,
        avatar_dir: Path | None = None,
    ) -> None:
        self.destination = destination
        self.avatar_dir_override = avatar_dir
        self.app: V2DLApp | None = None
        self._cdn_warmed: bool = False

    async def __aenter__(self) -> "_ProfileSession":
        # Build a v2dl argument Namespace just like sync_local._BotSession,
        # but with ``-d <destination>`` so download_dir is populated.
        # The stub URL satisfies the argparser; we never feed it to the
        # scraper (we drive ``bot.auto_page_scroll`` and
        # ``ImageScraper.download_file`` directly).
        argv = ["-d", str(self.destination), "https://www.v2ph.com/"]
        args = parse_arguments(argv)
        args.terminate = True

        self.app = V2DLApp()
        await self.app.init(args)

        if self.avatar_dir_override is not None:
            self.app.config.static_config.avatar_dir = str(self.avatar_dir_override)
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.app is not None:
            try:
                self.app.bot.close_driver()
            except Exception:
                pass

    # -- HTML fetch ---------------------------------------------------------
    async def fetch(self, url: str) -> str:
        if self.app is None:
            raise RuntimeError("_ProfileSession used outside async with block")
        return await self.app.bot.auto_page_scroll(url, page_sleep=0)

    # -- avatar download ----------------------------------------------------
    @property
    def image_strategy(self) -> ImageScraper:
        assert self.app is not None
        strategy = self.app.scraper.strategies["album_image"]
        assert isinstance(strategy, ImageScraper)
        return strategy

    def _avatar_dest(self, actor: ActorProfile) -> Path | None:
        """Delegate to ScrapeManager._avatar_dest_for (returns None when
        avatar_dir is not resolvable)."""
        assert self.app is not None
        try:
            return self.app.scraper._avatar_dest_for(actor)
        except Exception:
            return None

    def _ensure_cdn_warmed(self, avatar_url: str) -> None:
        """Open one cdn.v2ph.com tab so browser_fetch can same-origin it.

        Idempotent across the session (the bot itself short-circuits
        after the first success). Failures fall through to curl-cffi
        on a per-download basis - we don't propagate.
        """
        if self._cdn_warmed:
            return
        assert self.app is not None
        try:
            ok = self.app.bot.ensure_cdn_warmed(avatar_url)
        except Exception as e:
            print(f"[sync-actors]   CDN warmup raised: {e}", file=sys.stderr)
            return
        if ok:
            self._cdn_warmed = True

    async def download_avatar(self, actor: ActorProfile) -> Path | None:
        """Download ``actor.avatar_url`` to the configured avatar dir.

        Returns the on-disk path that actually got written (the
        ImageScraper rewrites the suffix based on the response MIME, so
        the final path may differ from the predicted one) or ``None``
        on any failure.
        """
        assert self.app is not None
        if not actor.avatar_url:
            return None
        dest = self._avatar_dest(actor)
        if dest is None:
            return None

        self._ensure_cdn_warmed(actor.avatar_url)

        try:
            cookies = self.app.bot.get_cookies()
        except Exception:
            cookies = {}
        try:
            ok = await self.image_strategy.download_file(
                actor.avatar_url,
                dest,
                cookies=cookies,
                web_bot=self.app.bot,
            )
        except Exception as e:
            print(f"[sync-actors]   avatar download raised: {e}", file=sys.stderr)
            return None
        if not ok:
            return None

        # download_file may have rewritten the suffix; resolve the
        # actual file on disk via the same helper ScrapeManager uses.
        try:
            actual = self.app.scraper._find_written_file(dest)
        except Exception:
            actual = None
        return actual if actual is not None else (dest if dest.exists() else None)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _resolve_db_path(args: argparse.Namespace) -> Path:
    if args.db:
        return Path(args.db)
    return Path(args.destination) / "v2ph_profiles.sqlite3"


def _load_actor_rows(input_path: Path, only_selected: bool) -> list[tuple[str, str]]:
    if not input_path.exists():
        raise SystemExit(
            f"[sync-actors] actors watch list not found: {input_path}\n"
            "[sync-actors] run `python scripts/sync_local.py discover actors` first."
        )
    if input_path.suffix.lower() != ".xlsx":
        raise SystemExit(
            f"[sync-actors] {input_path} is not an .xlsx file. "
            "This script only consumes the xlsx layout produced by "
            "`sync_local.py discover actors`."
        )

    if only_selected:
        rows = _load_urls_from_xlsx(input_path)
        label = "selected (是否采集 == 1)"
    else:
        rows = _load_urls_from_xlsx_all(input_path)
        label = "all"

    if not rows:
        raise SystemExit(
            f"[sync-actors] no actor rows to process from {input_path} "
            f"(filter: {label})."
        )
    print(f"[sync-actors] loaded {len(rows)} {label} actor rows from {input_path.name}")
    return rows


def _classify_row(
    db: ProfileDB,
    url: str,
    *,
    force: bool,
    skip_avatar: bool,
) -> tuple[str, dict[str, Any] | None]:
    """Decide what to do with one xlsx row.

    Returns ``(action, db_row)`` where ``action`` is one of:

    * ``"skip"`` - already complete, do nothing.
    * ``"avatar"`` - DB row exists with ``avatar_url`` but no
      ``avatar_local_path``; just download the image.
    * ``"full"`` - need to fetch the HTML page (either no DB row or
      forced).
    """
    if force:
        return ("full", None)

    try:
        row = db.get_actor_by_url(_strip_query(url))
    except Exception as e:
        print(
            f"[sync-actors]   WARN: DB lookup failed for {url}: {e}; treating as not-present",
            file=sys.stderr,
        )
        return ("full", None)

    if row is None:
        return ("full", None)
    if skip_avatar:
        return ("skip", row)

    has_local = bool((row.get("avatar_local_path") or "").strip())
    if has_local and Path(str(row["avatar_local_path"])).exists():
        return ("skip", row)

    has_remote = bool((row.get("avatar_url") or "").strip())
    if has_remote:
        return ("avatar", row)

    # In DB but neither avatar_url nor avatar_local_path - extraction
    # probably failed last time. Re-fetch the page to try again.
    return ("full", row)


def _profile_from_row(row: dict[str, Any]) -> ActorProfile:
    """Reconstruct an ActorProfile from a DB row (for the avatar-only path).

    ``_avatar_dest_for`` only reads ``actor_slug`` / ``name``, but we
    populate the obvious fields anyway in case future helpers grow more
    dependencies. ``avatar_url`` is the one field that actually matters
    for the download itself.
    """
    actor_url = str(row.get("actor_url") or "")
    return ActorProfile(
        actor_url=actor_url,
        actor_slug=(row.get("actor_slug") or _url_segment(actor_url, "actor")),
        name=row.get("name"),
        avatar_url=row.get("avatar_url"),
        avatar_local_path=row.get("avatar_local_path"),
    )


def _format_actor_summary(profile: ActorProfile) -> str:
    bits: list[str] = []
    for attr in ("name", "birthday", "height", "from_location", "listed_album_count"):
        val = getattr(profile, attr, None)
        if val in (None, ""):
            continue
        bits.append(f"{attr}={val}")
    return ", ".join(bits) if bits else "<no fields extracted>"


# --------------------------------------------------------------------------- #
# per-row handlers
# --------------------------------------------------------------------------- #
async def _process_full(
    session: _ProfileSession,
    db: ProfileDB,
    url: str,
    *,
    skip_avatar: bool,
) -> tuple[bool, bool]:
    """Full path: fetch page -> upsert profile -> (optional) avatar.

    Returns ``(profile_saved, avatar_saved)``.
    """
    print(f"[sync-actors]   fetching {url}")
    try:
        html_content = await session.fetch(url)
    except Exception as e:
        print(f"[sync-actors]   fetch error: {e}", file=sys.stderr)
        return (False, False)

    if not html_content or "Just a moment" in html_content[:2000]:
        print(
            "[sync-actors]   Cloudflare interstitial or empty body; skipping",
            file=sys.stderr,
        )
        return (False, False)

    try:
        tree = html.fromstring(html_content)
    except Exception as e:
        print(f"[sync-actors]   parse error: {e}", file=sys.stderr)
        return (False, False)

    try:
        profile = ProfileExtractor.extract_actor(tree, url)
        actor_id = db.upsert_actor(profile)
        print(
            f"[sync-actors]   saved actor id={actor_id} "
            f"({_format_actor_summary(profile)})"
        )
    except Exception as e:
        print(f"[sync-actors]   extract/upsert error: {e}", file=sys.stderr)
        return (False, False)

    if skip_avatar or not profile.avatar_url:
        if not profile.avatar_url:
            print("[sync-actors]   no avatar_url on page; skipping avatar")
        return (True, False)

    written = await session.download_avatar(profile)
    if written is None:
        print("[sync-actors]   avatar download failed; profile row still saved")
        return (True, False)
    try:
        db.update_actor_avatar_path(actor_id, str(written))
        print(f"[sync-actors]   avatar -> {written}")
        return (True, True)
    except Exception as e:
        print(f"[sync-actors]   avatar path update failed: {e}", file=sys.stderr)
        return (True, False)


async def _process_avatar_only(
    session: _ProfileSession,
    db: ProfileDB,
    row: dict[str, Any],
) -> bool:
    """Resume path: DB already has the actor + ``avatar_url``, but the
    image never landed. One CDN GET; no HTML scrape.
    """
    profile = _profile_from_row(row)
    actor_id = int(row["id"])
    if not profile.avatar_url:
        return False
    print(f"[sync-actors]   avatar resume: {profile.avatar_url}")
    written = await session.download_avatar(profile)
    if written is None:
        print("[sync-actors]   avatar download failed")
        return False
    try:
        db.update_actor_avatar_path(actor_id, str(written))
        print(f"[sync-actors]   avatar -> {written}")
        return True
    except Exception as e:
        print(f"[sync-actors]   avatar path update failed: {e}", file=sys.stderr)
        return False


# --------------------------------------------------------------------------- #
# main loop
# --------------------------------------------------------------------------- #
async def _run(args: argparse.Namespace) -> int:
    db_path = _resolve_db_path(args)
    rows = _load_actor_rows(args.input, args.only_selected)

    avatar_dir_display: str
    if args.no_avatar:
        avatar_dir_display = "(disabled, --no-avatar)"
    elif args.avatar_dir is not None:
        avatar_dir_display = str(args.avatar_dir)
    else:
        avatar_dir_display = str(Path(args.destination) / "_avatars")

    print(f"[sync-actors] profile DB:  {db_path}")
    print(f"[sync-actors] avatar dir:  {avatar_dir_display}")
    if args.dry_run:
        print("[sync-actors] DRY RUN: no Chrome boot, no DB writes.")

    db = ProfileDB(db_path)

    # Pre-flight classification so the progress log lines up with the
    # actual workload (full vs avatar-only vs skip).
    full_pending: list[tuple[int, str, str]] = []  # (row_idx, url, name)
    avatar_pending: list[tuple[int, str, str, dict[str, Any]]] = []
    skipped = 0
    for i, (url, name) in enumerate(rows, 1):
        action, row = _classify_row(
            db, url,
            force=args.force,
            skip_avatar=args.no_avatar,
        )
        if action == "skip":
            skipped += 1
        elif action == "avatar":
            assert row is not None
            avatar_pending.append((i, url, name, row))
        else:
            full_pending.append((i, url, name))

    total_pending = len(full_pending) + len(avatar_pending)
    print(
        f"[sync-actors] {skipped} already complete (skipped), "
        f"{len(full_pending)} need full fetch, "
        f"{len(avatar_pending)} need avatar-only resume"
        + (f" (capped to {args.limit})" if args.limit and total_pending > args.limit else "")
    )

    # Apply the --limit cap: avatar-only resume is cheaper (no HTML
    # scrape), so prioritise those - they're more "free wins" per CF
    # quota point.
    if args.limit and total_pending > args.limit:
        budget = args.limit
        if budget <= len(avatar_pending):
            avatar_pending = avatar_pending[:budget]
            full_pending = []
        else:
            full_pending = full_pending[: budget - len(avatar_pending)]

    if args.dry_run:
        shown = 0
        for i, url, name, _row in avatar_pending[:25]:
            label = name or url
            print(f"[sync-actors]   [avatar] row {i}: {label}")
            shown += 1
        for i, url, name in full_pending[:25]:
            label = name or url
            print(f"[sync-actors]   [full]   row {i}: {label}")
            shown += 1
        remaining = (len(avatar_pending) + len(full_pending)) - shown
        if remaining > 0:
            print(f"[sync-actors]   ... and {remaining} more")
        return 0

    if not (full_pending or avatar_pending):
        print("[sync-actors] nothing to do.")
        return 0

    avatars_saved = 0
    profiles_saved = 0
    failed = 0

    # Single Chrome instance for the whole run.
    async with _ProfileSession(
        destination=args.destination,
        avatar_dir=args.avatar_dir,
    ) as session:
        # ---- avatar-only resume rows first ----
        for n, (row_idx, url, name, row) in enumerate(avatar_pending, 1):
            label = name or url
            print(
                f"[sync-actors] [avatar {n}/{len(avatar_pending)}] "
                f"row {row_idx}: {label}"
            )
            ok = await _process_avatar_only(session, db, row)
            if ok:
                avatars_saved += 1
            else:
                failed += 1
            # No inter-row sleep here - we're hitting cdn.v2ph.com via
            # the warmed tab, not the www. origin / page-fetch path.

        # ---- full fetches ----
        for n, (row_idx, url, name) in enumerate(full_pending, 1):
            label = name or url
            print(
                f"[sync-actors] [full {n}/{len(full_pending)}] "
                f"row {row_idx}: {label}"
            )
            profile_ok, avatar_ok = await _process_full(
                session, db, url,
                skip_avatar=args.no_avatar,
            )
            if profile_ok:
                profiles_saved += 1
            else:
                failed += 1
            if avatar_ok:
                avatars_saved += 1

            if n < len(full_pending):
                time.sleep(args.sleep)

    print(
        f"[sync-actors] done. "
        f"profiles_saved={profiles_saved} avatars_saved={avatars_saved} "
        f"failed={failed} pre-skipped={skipped}"
    )
    return 0 if failed == 0 else 1


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\n[sync-actors] interrupted by user", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
