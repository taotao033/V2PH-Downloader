import asyncio
import re
from logging import Logger
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generic

from pathvalidate import sanitize_filename

from v2dl.common import Config, RuntimeConfig, ScrapeError
from v2dl.common.utils import count_files
from v2dl.scraper.core import (
    AlbumScraper,
    BaseScraper,
    ImageScraper,
)
from v2dl.scraper.downloader import DownloadPathTool
from v2dl.scraper.profiles import (
    ActorProfile,
    AlbumProfile,
    ProfileDB,
)
from v2dl.scraper.tools import AlbumTracker, DownloadStatus, LogKey, MetadataHandler, UrlHandler
from v2dl.scraper.types import PageResultType, ScrapeType

if TYPE_CHECKING:
    from v2dl.web_bot.base import BaseBot


class ScrapeManager:
    """Manage the starting and ending of the scraper."""

    def __init__(
        self,
        config: Config,
        web_bot: "BaseBot",
    ) -> None:
        self.config = config
        self.runtime_config = config.runtime_config
        self.web_bot = web_bot
        self.logger = config.runtime_config.logger

        self.no_log = False  # flag to not log download status

        self.album_tracker = AlbumTracker(config.static_config.download_log_path)
        self.strategies: dict[ScrapeType, BaseScraper[Any]] = {
            "album_list": AlbumScraper(
                config,
                self.album_tracker,
            ),
            "album_image": ImageScraper(
                config,
                self.album_tracker,
            ),
        }

        self.metadata_handler = MetadataHandler(config, self.album_tracker)
        self.processed_urls: set[str] = set()

        # Profile DB (actor / album metadata). Lazily created when the
        # config supplies a path. ``self._current_actor_id`` is set
        # while a /actor/ listing is being scraped so that each album
        # processed inside it can be linked back via FK; cleared once
        # the listing finishes.
        self.profile_db: ProfileDB | None = self._init_profile_db()
        self._current_actor_id: int | None = None

    async def start_scraping(self) -> bool:
        """Start scraping based on URL type."""
        try:
            urls = UrlHandler.load_urls(self.runtime_config.url, self.runtime_config.url_file)
            if self.__check_early_return(urls):
                return False

            for url in urls:
                url = UrlHandler.update_language(url, self.config.static_config.language)
                self.runtime_config.url = url
                self.update_runtime_config(self.runtime_config)
                await self.scrape(url)

                if self.runtime_config.url_file:
                    UrlHandler.mark_processed_url(self.runtime_config.url_file, url)

        except ScrapeError as e:
            self.logger.exception("Scraping error: '%s'", e)
            return False
        finally:
            if self.config.static_config.terminate:
                self.web_bot.close_driver()
        return True

    def __check_early_return(self, urls: list[str]) -> bool:
        if not urls:
            if self.runtime_config.url:
                source = self.runtime_config.url
            else:
                source = self.runtime_config.url_file
            self.logger.info(f"No valid urls found in {source}")
            self.no_log = True
            return True
        return False

    async def scrape(self, url: str) -> None:
        """Main entry point for scraping operations."""
        scrape_type = UrlHandler.get_scrape_type(url)
        if scrape_type is None:
            raise KeyError(
                "Unsupported link type. Please report this issue at https://github.com/ZhenShuo2021/V2PH-Downloader/issues."
            )

        target_page: int | list[int]
        _, target_page = UrlHandler.parse_input_url(url)
        if self.config.static_config.page_range is not None:
            target_page = UrlHandler.parse_page_range(self.config.static_config.page_range)

        self.processed_urls.add(UrlHandler.remove_query_params(url))

        if scrape_type == "album_list":
            await self.scrape_album_list(url, target_page)
        else:
            await self.scrape_album(url, target_page)

    async def scrape_album_list(self, url: str, target_page: int | list[int]) -> None:
        """Handle scraping of album lists."""
        strategy = self.strategies["album_list"]
        # Reset the last-seen display name so this listing's name doesn't
        # leak across consecutive scrape_album_list calls (the strategy
        # instance is shared across all URLs in a sync run).
        if isinstance(strategy, AlbumScraper):
            strategy.last_display_name = None
            strategy.last_actor_profile = None
        scraper = PageScraper(self.web_bot, strategy, self.logger)

        album_links = await scraper.scrape_all_pages(url, target_page)
        self.logger.info("A total of %d albums found for %s", len(album_links), url)

        # Pick the most user-friendly name for the per-listing subdirectory:
        # prefer the page-visible name (e.g. "网络美女") captured by
        # AlbumScraper, fall back to the URL slug (e.g. "Beautyleg").
        parent_slug: str | None = None
        if isinstance(strategy, AlbumScraper):
            parent_slug = strategy.last_display_name
        if not parent_slug:
            parent_slug = UrlHandler.extract_listing_slug(url)

        image_strategy = self.strategies["album_image"]
        if isinstance(image_strategy, ImageScraper):
            image_strategy.set_parent_slug(parent_slug)
            if parent_slug:
                self.logger.info(
                    "Grouping downloads from %s under subdirectory '%s'",
                    UrlHandler.remove_query_params(url),
                    parent_slug,
                )

        # Persist the captured actor profile (if any) BEFORE iterating
        # over album URLs, so that each album's row can reference the
        # actor via FK as it's inserted. Avatar download is best-effort
        # and never blocks album scraping.
        if isinstance(strategy, AlbumScraper) and strategy.last_actor_profile is not None:
            await self._persist_actor_profile(
                strategy.last_actor_profile,
                image_strategy if isinstance(image_strategy, ImageScraper) else None,
            )

        temp_original_url = self.runtime_config.url
        success_count = 0
        fail_count = 0

        try:
            for album_url in album_links:
                try:
                    self.runtime_config.url = album_url
                    self.update_runtime_config(self.runtime_config)
                    await self.scrape_album(album_url, 1)
                    self.processed_urls.add(UrlHandler.remove_query_params(album_url))
                    success_count += 1
                except Exception as e:
                    self.logger.error(
                        "Error processing album %s: %s. Skipping to next album.",
                        album_url,
                        str(e),
                    )
                    fail_count += 1
                    continue
        finally:
            if isinstance(image_strategy, ImageScraper):
                image_strategy.set_parent_slug(None)
            # Update the actor's ``scraped_album_count`` once the
            # listing is fully iterated. Done in ``finally`` so a
            # mid-listing crash still produces an accurate partial
            # number.
            if self.profile_db is not None and self._current_actor_id is not None:
                try:
                    self.profile_db.update_actor_scraped_album_count(
                        self._current_actor_id, success_count
                    )
                except Exception as e:
                    self.logger.debug(
                        "Failed to persist scraped_album_count for actor_id=%s: %s",
                        self._current_actor_id, e,
                    )
                self._current_actor_id = None

        self.logger.info(
            "Album list processing completed: %d successful, %d failed",
            success_count,
            fail_count,
        )
        self.runtime_config.url = temp_original_url
        self.update_runtime_config(self.runtime_config)

    async def scrape_album(self, album_url: str, target_page: int | list[int]) -> None:
        """Handle scraping of a single album page.

        Three modes:

        * **Normal** - URL not in ``downloaded_albums.txt``: fetch all
          pages and download images.
        * **Already downloaded + profile already in DB**: full skip.
        * **Already downloaded + profile NOT yet in DB**: cheap
          *profile-only backfill* - fetch only page 1 to capture the
          album card, count files already on disk for
          ``scraped_photo_count``, then upsert. This closes the gap
          where a user with a populated ``downloaded_albums.txt``
          (from earlier runs that pre-date the profile DB) would
          otherwise have their old albums permanently absent from
          ``v2ph_profiles.sqlite3``.
        """
        clean_url = UrlHandler.remove_query_params(album_url)
        already_downloaded = (
            self.album_tracker.is_downloaded(clean_url)
            and not self.config.static_config.force_download
        )

        if already_downloaded:
            # No DB configured -> classic skip behaviour (no backfill
            # to perform).
            if self.profile_db is None:
                self.logger.info("Album %s already downloaded, skipping.", album_url)
                return
            try:
                existing = self.profile_db.get_album_by_url(clean_url)
            except Exception as e:
                self.logger.debug("Profile DB lookup failed for %s: %s", clean_url, e)
                existing = None
            if existing is not None:
                self.logger.info(
                    "Album %s already downloaded and profile recorded, skipping.",
                    album_url,
                )
                return
            self.logger.info(
                "Album %s already downloaded but profile not yet recorded - "
                "fetching page 1 only to backfill profile.",
                album_url,
            )
            await self._backfill_album_profile(album_url, clean_url)
            return

        try:
            strategy = self.strategies["album_image"]
            # Wipe previous album's scratch state (profile / counters /
            # dest) so it can't bleed into this one. Safe even when
            # the previous album crashed mid-page.
            if isinstance(strategy, ImageScraper):
                strategy.reset_album_state()
            scraper = PageScraper(self.web_bot, strategy, self.logger)

            image_links = await scraper.scrape_all_pages(album_url, target_page)
            self.album_tracker.update_download_log(
                album_url,  # 使用專輯 URL 而不是 runtime_config.url
                {LogKey.expect_num: len(image_links)},
            )
            if not image_links:
                self.logger.warning("No images found for album %s", album_url)
                # Still upsert the album profile (if captured) so we
                # have a record that we tried, even if VIP / blocked.
                if isinstance(strategy, ImageScraper):
                    self._refresh_album_count_from_disk(strategy)
                    self._persist_album_profile(strategy, clean_url)
                return

            album_name = re.sub(r"\s*\d+$", "", image_links[0][1]) if image_links else "Unknown Album"
            self.logger.info("Found %d images in album %s", len(image_links), album_name)
            self.album_tracker.log_downloaded(clean_url)

            if isinstance(strategy, ImageScraper):
                # Prefer ground-truth on-disk file count over the
                # per-run success accumulator: the two should agree,
                # but ``count_files`` survives partial reruns where
                # half the images are cache-hits and half are fresh
                # downloads.
                self._refresh_album_count_from_disk(strategy)
                self._persist_album_profile(strategy, clean_url)
        except Exception as e:
            self.logger.error(
                "Error scraping album %s: %s. Skipping to next album.",
                album_url,
                str(e),
            )
            # Update download log with failure status
            self.album_tracker.update_download_log(
                clean_url,
                {LogKey.status: DownloadStatus.FAIL},
            )

    async def _backfill_album_profile(self, album_url: str, clean_url: str) -> None:
        """Profile-only backfill for albums already in ``downloaded_albums.txt``.

        We fetch ONLY page 1 of the album because:
          * The album card (title / release date / models / tags /
            listed photo count) is identical on every page of the
            album, so page 1 is sufficient.
          * Image GETs short-circuit on the file cache anyway, but
            each *page* fetch still goes through Cloudflare via
            ``auto_page_scroll`` - those add up fast on a 100-album
            backlog. Fetching page 1 only gives us a 5-10x speedup
            over a full-album rescan.

        For ``scraped_photo_count`` we use ``count_files`` on the
        predicted destination directory (computed by ImageScraper
        during the page-1 pass) instead of the per-run success
        counter, since the latter would only see the page-1 cache
        hits.
        """
        strategy = self.strategies["album_image"]
        if not isinstance(strategy, ImageScraper):
            return
        strategy.reset_album_state()
        scraper = PageScraper(self.web_bot, strategy, self.logger)
        try:
            await scraper.scrape_all_pages(album_url, [1])
        except Exception as e:
            self.logger.warning("Profile backfill for %s failed: %s.", album_url, e)
            return

        if strategy.last_album_profile is None:
            self.logger.warning(
                "Profile backfill for %s captured no profile (page may be "
                "VIP / login-redirected / layout-changed).",
                album_url,
            )
            return

        self._refresh_album_count_from_disk(strategy)
        self._persist_album_profile(strategy, clean_url)

    @staticmethod
    def _refresh_album_count_from_disk(strategy: ImageScraper) -> None:
        """Set ``last_album_success_count`` from the actual file count.

        Replaces the per-run accumulator with whatever the filesystem
        currently shows in ``last_album_dest``. This is the user-
        meaningful "actually got these many files" number that ends
        up in ``albums.scraped_photo_count``.
        """
        dest = strategy.last_album_dest
        if not dest:
            return
        try:
            dest_path = Path(dest)
            if dest_path.is_dir():
                strategy.last_album_success_count = count_files(dest_path)
        except Exception:
            # Stay quiet: a missing dir just means no images on disk
            # yet (e.g. profile was captured but VIP-blocked the
            # downloads).
            pass

    def update_runtime_config(self, runtime_config: RuntimeConfig) -> None:
        if not isinstance(runtime_config, RuntimeConfig):
            raise TypeError(f"Expected a RuntimeConfig object, got {type(runtime_config).__name__}")
        self.runtime_config = runtime_config

        for strategy in self.strategies.values():
            strategy.runtime_config = runtime_config

    # ------------------------------------------------------------------ #
    # Profile DB helpers
    # ------------------------------------------------------------------ #
    def _init_profile_db(self) -> ProfileDB | None:
        """Open the profile DB if a path is configured, else return None.

        Failures are non-fatal: profile collection is opportunistic, and
        the user can always still scrape images. We just log and move
        on.
        """
        path = (self.config.static_config.profile_db_path or "").strip()
        if not path:
            return None
        try:
            return ProfileDB(path)
        except Exception as e:
            self.logger.warning(
                "Failed to open profile DB at %s (%s); profile collection disabled.",
                path, e,
            )
            return None

    async def _persist_actor_profile(
        self,
        actor: ActorProfile,
        image_strategy: ImageScraper | None,
    ) -> None:
        """Persist an actor profile and (best-effort) download the avatar.

        ``self._current_actor_id`` is set to the inserted PK so each
        album processed inside the listing can FK back to it. Avatar
        download routes through the same browser/curl-cffi pipeline
        that ``ImageScraper`` uses for album images, so it benefits
        from the same Cloudflare bypass.
        """
        if self.profile_db is None:
            return
        try:
            actor_id = self.profile_db.upsert_actor(actor)
        except Exception as e:
            self.logger.warning("Failed to upsert actor profile %s: %s", actor.actor_url, e)
            return

        self._current_actor_id = actor_id
        self.logger.info(
            "Captured actor profile: %s (id=%d, listed_albums=%s)",
            actor.name or actor.actor_slug or actor.actor_url,
            actor_id,
            actor.listed_album_count,
        )

        if not actor.avatar_url or image_strategy is None:
            return

        avatar_path = self._avatar_dest_for(actor)
        if avatar_path is None:
            return
        try:
            cookies = self.web_bot.get_cookies()
        except Exception:
            cookies = {}
        try:
            ok = await image_strategy.download_file(
                actor.avatar_url,
                avatar_path,
                cookies=cookies,
                web_bot=self.web_bot,
            )
        except Exception as e:
            self.logger.debug("Avatar download raised for %s: %s", actor.actor_url, e)
            ok = False

        if ok:
            # ``download_file`` rewrites the suffix based on the actual
            # MIME type, so the file on disk may not match
            # ``avatar_path``. Look up whatever variant was written.
            actual = self._find_written_file(avatar_path)
            if actual is not None:
                try:
                    self.profile_db.update_actor_avatar_path(actor_id, str(actual))
                except Exception as e:
                    self.logger.debug("Failed to record avatar path: %s", e)
        else:
            self.logger.info(
                "Avatar download failed for %s; profile row still saved.",
                actor.actor_url,
            )

    def _avatar_dest_for(self, actor: ActorProfile) -> Path | None:
        """Build a sanitised filesystem path for the actor's avatar.

        Falls back to ``<download_dir>/_avatars`` when the dedicated
        ``avatar_dir`` is not set. The slug is sanitised because
        users have actor pages whose slugs contain forward slashes /
        spaces (e.g. ``/actor/Some Name``).
        """
        avatar_dir = (self.config.static_config.avatar_dir or "").strip()
        if not avatar_dir:
            avatar_dir = str(Path(self.config.static_config.download_dir or "") / "_avatars")
        if not avatar_dir or avatar_dir == "_avatars":
            return None

        slug = actor.actor_slug or actor.name or "actor"
        safe = sanitize_filename(slug) or "actor"
        # Suffix is provisional: ``ImageScraper.download_file`` rewrites
        # it based on the response Content-Type / URL extension. We
        # pass ``.jpg`` as a placeholder so the parent dir is created
        # correctly.
        dest = Path(avatar_dir) / f"{safe}.jpg"
        try:
            DownloadPathTool.mkdir(dest.parent)
        except Exception as e:
            self.logger.debug("Failed to create avatar dir %s: %s", dest.parent, e)
            return None
        return dest

    @staticmethod
    def _find_written_file(reference: Path) -> Path | None:
        """Find whichever extension variant of ``reference`` exists on disk.

        ``ImageScraper.download_file`` re-suffixes based on the actual
        response (e.g. .jpg -> .png), so the post-download path may
        differ from what we supplied. We just glob the directory for
        anything sharing the stem.
        """
        if reference.exists():
            return reference
        try:
            for sibling in reference.parent.glob(f"{reference.stem}.*"):
                if sibling.is_file():
                    return sibling
        except Exception:
            return None
        return None

    def _persist_album_profile(self, strategy: ImageScraper, clean_url: str) -> None:
        """Upsert the captured album profile + counts to the profile DB."""
        if self.profile_db is None:
            return
        profile = strategy.last_album_profile
        if profile is None:
            return

        profile.actor_id = self._current_actor_id
        profile.scraped_photo_count = strategy.last_album_success_count
        profile.download_dest = strategy.last_album_dest
        try:
            album_id = self.profile_db.upsert_album(profile)
            self.logger.info(
                "Persisted album profile: %s (id=%d, scraped=%d/%s, models=%d, tags=%d)",
                profile.title or profile.album_slug or profile.album_url,
                album_id,
                profile.scraped_photo_count,
                profile.listed_photo_count,
                len(profile.models),
                len(profile.tags),
            )
        except Exception as e:
            self.logger.warning("Failed to upsert album profile %s: %s", clean_url, e)

    def log_final_status(self) -> None:
        download_status = self.album_tracker.get_download_status
        if self.no_log or not download_status:
            return

        self.logger.info("Download finished, showing download status")
        for url in self.processed_urls:
            if url in download_status:
                album_status = download_status[url]
                if album_status[LogKey.status] == DownloadStatus.FAIL:
                    self.logger.error(f"{url}: Unexpected error")
                elif album_status[LogKey.status] == DownloadStatus.VIP:
                    self.logger.warning(f"{url}: VIP images found")
                else:
                    self.logger.info(f"{url}: Download successful")

    def write_metadata(self) -> None:
        self.metadata_handler.write_metadata()


class PageScraper(Generic[PageResultType]):
    """Handles the scraping of individual pages."""

    def __init__(
        self,
        web_bot: "BaseBot",
        strategy: BaseScraper[PageResultType],
        logger: Logger,
    ) -> None:
        self.web_bot = web_bot
        self.strategy = strategy
        self.logger = logger

    async def scrape_all_pages(self, url: str, target_page: int | list[int]) -> list[Any]:
        """Scrape multiple pages according to target configuration."""
        all_results: list[Any] = []
        page: int | list[int] | None
        page, scrape_one_page = UrlHandler.handle_first_page(target_page)

        scrape_type = "album" if isinstance(self.strategy, AlbumScraper) else "image"
        self.logger.info(
            "Starting to scrape %s links from %s",
            scrape_type,
            url,
        )

        consecutive_failures = 0
        max_consecutive_failures = 3

        while True:
            try:
                page_results, should_continue = await self.scrape_page(url, page)
                all_results.extend(page_results)

                # Reset consecutive failures counter on success
                if page_results or should_continue:
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1

                # If we've had too many consecutive failures, stop
                if consecutive_failures >= max_consecutive_failures:
                    self.logger.error(
                        "Too many consecutive page failures (%d), stopping scraping for %s",
                        consecutive_failures,
                        url,
                    )
                    break

                page = UrlHandler.handle_pagination(page, target_page)
                if not should_continue or scrape_one_page or page is None:
                    break
            except Exception as e:
                self.logger.error(
                    "Unexpected error while scraping page %s from %s: %s. Continuing to next page.",
                    page,
                    url,
                    str(e),
                )
                consecutive_failures += 1
                if consecutive_failures >= max_consecutive_failures:
                    self.logger.error(
                        "Too many consecutive errors (%d), stopping scraping for %s",
                        consecutive_failures,
                        url,
                    )
                    break
                page = UrlHandler.handle_pagination(page, target_page)
                if scrape_one_page or page is None:
                    break

        return all_results

    async def scrape_page(self, url: str, page: int) -> tuple[list[PageResultType], bool]:
        """Scrape a single page and return results and continuation flag."""
        full_url = UrlHandler.add_page_num(url, page)
        try:
            html_content = await self.web_bot.auto_page_scroll(full_url, page_sleep=0)
            tree = UrlHandler.parse_html(html_content, self.logger)

            if tree is None:
                self.logger.warning("Failed to parse HTML for page %d, skipping", page)
                return [], False

            if self.strategy.is_vip_page(tree):
                _url = UrlHandler.remove_query_params(full_url)
                self.strategy.album_tracker.update_download_log(
                    _url, {LogKey.status: DownloadStatus.VIP}
                )
                return [], False

            self.logger.info("Fetching content from %s", full_url)
            page_links = tree.xpath(self.strategy.get_xpath())

            scrape_type = "album_list" if isinstance(self.strategy, AlbumScraper) else "album_image"
            if not page_links:
                self.logger.info(
                    "No more %s found on page %d",
                    "albums" if scrape_type == "album_list" else "images",
                    page,
                )
                if scrape_type == "album_image" and page == 1:
                    self._log_empty_image_page_diagnostics(full_url, html_content, tree)
                return [], False

            page_result: list[PageResultType] = []
            # For image albums, kick the browser through Cloudflare's
            # bot-management handshake for cdn.v2ph.com BEFORE we
            # snapshot cookies. The CDN is a separate CF zone, so its
            # ``__cf_bm`` / ``cf_clearance`` only appear in the jar
            # after the browser has actually loaded a CDN URL. Without
            # this, the cookies forwarded to curl-cffi cover only
            # www.v2ph.com and the CDN returns 403 "Just a moment...".
            # ``ensure_cdn_warmed`` is idempotent across the session.
            if scrape_type == "album_image" and page_links:
                try:
                    self.web_bot.ensure_cdn_warmed(page_links[0])
                except Exception as e:
                    self.logger.debug("CDN warmup raised: %s", e)

            # Snapshot browser session cookies so ImageScraper can reuse them
            # for cdn.v2ph.com downloads (Cloudflare blocks raw httpx requests).
            try:
                browser_cookies = self.web_bot.get_cookies()
            except Exception as e:
                self.logger.debug("Unable to snapshot browser cookies: %s", e)
                browser_cookies = {}
            await self.strategy.process_page_links(
                url,
                page_links,
                page_result,
                tree,
                page,
                cookies=browser_cookies,
                web_bot=self.web_bot,
            )

            # Check if we've reached the last page
            should_continue = page < UrlHandler.get_max_page(tree)
            if not should_continue:
                self.logger.info("Reach last page, stopping")

            return page_result, should_continue
        except Exception as e:
            self.logger.error(
                "Error scraping page %d from %s: %s. Skipping this page.",
                page,
                full_url,
                str(e),
            )
            return [], False

    def _log_empty_image_page_diagnostics(
        self,
        full_url: str,
        html_content: str,
        tree: Any,
    ) -> None:
        """One-shot diagnostic for the "Fetching content ... -> No more
        images found on page 1" case.

        Cloudflare-cleared but image-less is almost always one of:
        captcha (image / Turnstile re-check), login redirect, VIP
        upgrade card, language redirect, or a layout change. We probe
        the parsed tree for each signal and log a single, actionable
        line so the user knows what to fix instead of staring at an
        empty result. The raw HTML is dumped to a file (capped) so
        layout-change cases can be reported upstream without losing
        the evidence.
        """
        try:
            page_url = ""
            try:
                page_url = getattr(self.web_bot, "page", None).url or ""  # type: ignore[union-attr]
            except Exception:
                page_url = ""

            page_title = ""
            try:
                page_title = (getattr(self.web_bot, "page", None).title or "")[:120]  # type: ignore[union-attr]
            except Exception:
                page_title = ""

            html_len = len(html_content) if html_content else 0
            lower_html = (html_content or "").lower()

            # Probe order: most specific signal first.
            signals: list[str] = []

            # Cloudflare interstitial / challenge still on the page.
            # ``cdn-cgi/challenge`` and ``challenges.cloudflare.com`` are
            # NOT used here even though they look CF-y: they appear in
            # preconnect <link> hints / CSP / residual scripts on EVERY
            # CF-fronted page even after the challenge cleared, and
            # would produce false positives on real album pages.
            if (
                "just a moment" in lower_html
                or 'name="cf-chl-bypass"' in lower_html
                or "__cf_chl_" in lower_html
                or "checking your browser" in lower_html
                or "正在进行安全验证" in (html_content or "")
                or "正在進行安全驗證" in (html_content or "")
            ):
                signals.append(
                    "Cloudflare challenge HTML still present (CF cleared "
                    "the interstitial visually but the album page itself "
                    "served a fresh challenge). Try re-running once the "
                    "browser cookie store has new cf_clearance / __cf_bm."
                )

            # v2ph image captcha (the 4-character coloured-noise card).
            if (
                'id="album-captcha-form"' in lower_html
                or 'id="captcha-image"' in lower_html
                or 'id="captcha_code"' in lower_html
                or 'class="captcha-container' in lower_html
            ):
                signals.append(
                    "v2ph image captcha is on the album page and was NOT "
                    "auto-solved. Install ddddocr (`pip install v2dl[ocr]`) "
                    "or solve the captcha manually in the open browser, "
                    "then re-run."
                )

            # Login / read-limit interception.
            if (
                'class="login-box-msg"' in lower_html
                or 'name="email"' in lower_html and 'name="password"' in lower_html
            ):
                signals.append(
                    "The album URL redirected to the login page. Check "
                    "that your session cookies are valid (or run "
                    "`v2dl --bot drissionpage <album-url>` once "
                    "interactively to refresh the cookie file)."
                )

            # VIP upgrade card. We re-use the same xpath the strategy
            # uses, but since the page wasn't recognised as VIP earlier
            # this catches the looser variant.
            try:
                if tree is not None and tree.xpath(
                    '//a[contains(@href, "/user/upgrade")]'
                ):
                    signals.append(
                        "Page contains a 'Upgrade to VIP' link - the "
                        "album is likely VIP-only on this account."
                    )
            except Exception:
                pass

            # Language / locale redirect: the URL we asked for had a
            # specific ``hl=`` and the browser ended up on a different
            # one. v2ph silently downgrades unknown codes to en, which
            # also strips the album body for some legacy URLs.
            if "hl=" in full_url and "hl=" in page_url and full_url != page_url:
                if "hl=" in page_url and "hl=" in full_url:
                    asked = full_url.split("hl=", 1)[1].split("&", 1)[0]
                    got = page_url.split("hl=", 1)[1].split("&", 1)[0]
                    if asked.lower() != got.lower():
                        signals.append(
                            f"Language redirect: requested hl={asked} but "
                            f"the browser settled on hl={got}. v2ph maps "
                            "unknown codes to English which can blank "
                            "the album body - try a canonical code from "
                            "AVAILABLE_LANGUAGES."
                        )

            # Bare layout-change fallback: the body has v2ph chrome but
            # neither album-photo nor any of the above markers. Likely
            # an HTML structure change.
            if not signals:
                has_v2ph_chrome = "v2ph" in lower_html or "微图坊" in (html_content or "")
                if has_v2ph_chrome:
                    signals.append(
                        "Album page rendered but contains no "
                        "<div class='album-photo'> elements and no "
                        "captcha / login / VIP / CF markers. The HTML "
                        "structure may have changed upstream - dumping "
                        "the page so you can grep it / file an issue."
                    )
                else:
                    signals.append(
                        "Page HTML does not look like a v2ph album "
                        "(no v2ph chrome, no images). The browser may "
                        "be on a different URL than expected."
                    )

            self.logger.warning(
                "Empty-album diagnostic for %s: browser_url=%r title=%r "
                "html_len=%d :: %s",
                full_url,
                page_url,
                page_title,
                html_len,
                " | ".join(signals),
            )

            self._log_html_layout_samples(html_content)
            self._dump_empty_album_html(full_url, html_content)
        except Exception as e:
            self.logger.warning("Empty-album diagnostic itself failed: %s", e)

    def _log_html_layout_samples(self, html_content: str) -> None:
        """Inline HTML fragments that actually matter for figuring out
        the new image markup. Always logged at warning level so they
        survive default log config (info+); we don't rely on the disk
        dump because filesystem encoding / permissions can swallow it
        silently.
        """
        if not html_content:
            return

        snippet_max = 240

        def _trim(s: str) -> str:
            s = re.sub(r"\s+", " ", s).strip()
            return (s[: snippet_max - 3] + "...") if len(s) > snippet_max else s

        photo_divs = re.findall(
            r"<div[^>]*(?:photo|album|gallery)[^>]*>", html_content, re.IGNORECASE
        )
        if photo_divs:
            self.logger.warning(
                "Layout sample - first 'photo/album/gallery' divs (%d total):",
                len(photo_divs),
            )
            for tag in photo_divs[:5]:
                self.logger.warning("  %s", _trim(tag))
        else:
            self.logger.warning(
                "Layout sample - NO div tags mentioning photo/album/gallery"
                " in attributes."
            )

        imgs = re.findall(r"<img[^>]*>", html_content, re.IGNORECASE)
        if imgs:
            self.logger.warning(
                "Layout sample - first <img> tags (%d total):", len(imgs)
            )
            for tag in imgs[:6]:
                self.logger.warning("  %s", _trim(tag))
        else:
            self.logger.warning(
                "Layout sample - NO <img> tags at all (album body did not"
                " render)."
            )

        dsrc_count = len(re.findall(r"\bdata-src\s*=", html_content, re.IGNORECASE))
        self.logger.warning(
            "Layout sample - data-src attribute count=%d", dsrc_count
        )

        class_names: set[str] = set()
        for m in re.finditer(
            r'class\s*=\s*"([^"]+)"', html_content, re.IGNORECASE
        ):
            for cls in m.group(1).split():
                low = cls.lower()
                if any(
                    needle in low
                    for needle in ("photo", "album", "gallery", "image", "thumb")
                ):
                    class_names.add(cls)
        if class_names:
            self.logger.warning(
                "Layout sample - candidate wrapper classes: %s",
                ", ".join(sorted(class_names)[:20]),
            )

    def _dump_empty_album_html(self, full_url: str, html_content: str) -> None:
        """Persist the offending HTML so we have raw evidence. Tries
        several fallback locations because the configured download_dir
        often contains non-ASCII characters that can trip mkdir on some
        filesystems / locales.
        """
        if not html_content:
            return

        from pathlib import Path
        from tempfile import gettempdir

        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", full_url)[-80:]
        filename = f"v2dl_empty_album_{slug}.html"
        data = html_content[: 256 * 1024]

        candidates: list[Path] = []
        # ``self`` is a ``PageScraper`` which doesn't own the Config
        # directly - the Config lives on the strategy. Be defensive in
        # case future strategies don't expose it either.
        static_config = getattr(getattr(self, "strategy", None), "config", None)
        static_config = getattr(static_config, "static_config", None)
        log_path = getattr(static_config, "system_log_path", "") or ""
        if log_path:
            candidates.append(Path(log_path).parent)
        download_dir = getattr(static_config, "download_dir", "") or ""
        if download_dir:
            candidates.append(Path(download_dir))
        candidates.append(Path(gettempdir()))
        candidates.append(Path.cwd())

        last_err: Exception | None = None
        for target_dir in candidates:
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                dump_path = target_dir / filename
                dump_path.write_text(data, encoding="utf-8", errors="replace")
                self.logger.warning(
                    "Wrote first %d bytes of the offending album HTML to %s",
                    len(data),
                    dump_path,
                )
                return
            except Exception as e:
                last_err = e
                continue

        self.logger.warning(
            "Failed to dump empty-album HTML to any of %s: %s",
            [str(p) for p in candidates],
            last_err,
        )
