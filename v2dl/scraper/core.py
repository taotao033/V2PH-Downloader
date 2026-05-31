import os
import asyncio
import json
import time
import urllib.request
from abc import ABC, abstractmethod
from datetime import datetime
from mimetypes import guess_extension
from pathlib import Path
from typing import Any, Generic

import httpx
from lxml import html
from pathvalidate import sanitize_filename

from v2dl.common import Config
from v2dl.common.const import BASE_URL, HEADERS, IMAGE_PER_PAGE, VALID_EXTENSIONS
from v2dl.scraper.downloader import DirectoryCache, DownloadPathTool
from v2dl.scraper.profiles import ActorProfile, AlbumProfile, ProfileExtractor
from v2dl.scraper.tools import AlbumTracker, DownloadStatus, LogKey, UrlHandler
from v2dl.scraper.types import AlbumResult, ImageResult, PageResultType

# Hidden JSON sidecar written to each album folder. Records which
# album_url owns the folder so future runs can detect cross-URL name
# collisions (two different albums sharing a "title" alt text in the
# DOM) and route the second one to ``<name> (2)`` / ``(3)`` instead
# of silently overwriting the first one's images.
ALBUM_SIDECAR_NAME = ".v2dl_album.json"


def _read_album_sidecar(album_dir: Path) -> dict[str, Any] | None:
    """Read the album sidecar if present, else None.

    Corrupt JSON / unreadable file -> None (treated as if missing).
    Callers should never trust an unparseable sidecar.
    """
    sidecar = album_dir / ALBUM_SIDECAR_NAME
    if not sidecar.is_file():
        return None
    try:
        return json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_album_sidecar(
    album_dir: Path,
    album_url: str,
    title: str | None,
) -> None:
    """Persist (or refresh) the album sidecar that claims ``album_dir``.

    ``first_seen_at`` is preserved if a prior sidecar exists, so the
    history of when this folder was first created stays intact across
    re-runs.

    Failures are intentionally swallowed: the sidecar is an
    optimisation for *future* collision detection, never a hard
    requirement for the current download to proceed.
    """
    now = datetime.utcnow().isoformat(timespec="seconds")
    existing = _read_album_sidecar(album_dir)
    first_seen_at = (existing or {}).get("first_seen_at") or now

    payload = {
        "album_url": album_url,
        "title": title,
        "first_seen_at": first_seen_at,
        "last_updated_at": now,
    }
    try:
        album_dir.mkdir(parents=True, exist_ok=True)
        (album_dir / ALBUM_SIDECAR_NAME).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass

# cdn.v2ph.com is fronted by Cloudflare and now returns 403 (with a
# "Just a moment..." challenge body) for vanilla httpx requests because the
# TLS / HTTP fingerprint does not look like a real browser. curl-cffi lets
# us replay an actual Chrome TLS fingerprint (JA3/JA4 + HTTP/2 settings),
# which is enough to clear the challenge as long as the request also carries
# the browser session cookies.
try:
    from curl_cffi.requests import AsyncSession as _CurlAsyncSession

    HAS_CURL_CFFI = True
except ImportError:  # pragma: no cover - optional dep
    _CurlAsyncSession = None  # type: ignore[assignment,misc]
    HAS_CURL_CFFI = False

# Chrome build whose TLS / HTTP fingerprint curl-cffi will impersonate. Keep
# in sync (loosely) with DEFAULT_USER_AGENT in common.const to avoid an
# obvious UA / fingerprint mismatch.
CURL_IMPERSONATE_TARGET = "chrome131"

# libcurl option numbers (kept as literals so we do not need to import from
# curl_cffi.const, which moved across minor versions). The values encode the
# argument type in the leading digits: LONG opts are <10000, STRINGPOINT opts
# are 10000+offset, OBJECTPOINT opts are 10000+offset, OFF_T opts are
# 30000+offset.
#   CURLOPT_IPRESOLVE   = 113    (LONG)
#   CURL_IPRESOLVE_V4   = 1
#   CURLOPT_DOH_URL     = 10279  (STRINGPOINT, libcurl >= 7.62)
_CURLOPT_IPRESOLVE = 113
_CURL_IPRESOLVE_V4 = 1
_CURLOPT_DOH_URL = 10279

# DoH endpoint used to bypass local UDP DNS poisoning (common on Chinese ISPs:
# cdn.v2ph.com resolves to bogus Facebook / Twitter ranges via plain DNS even
# when querying 1.1.1.1 over UDP, because the poisoning happens on the wire).
# We default to AliDNS because:
#   * The endpoint is an IP literal, so libcurl does not need a bootstrap DNS
#     lookup of its own.
#   * It is reachable from both mainland-China and overseas networks
#     (1.1.1.1 / 8.8.8.8 are often blocked or throttled inside China).
# Override via the V2DL_DOH_URL env var if a different resolver is needed.
DEFAULT_DOH_URL = "https://223.5.5.5/dns-query"


def _get_doh_url() -> str:
    """Allow overriding the DoH endpoint without editing the source.

    Setting ``V2DL_DOH_URL`` to an empty string disables DoH and falls back
    to libcurl's normal (UDP) DNS lookup.
    """
    env = os.environ.get("V2DL_DOH_URL")
    if env is None:
        return DEFAULT_DOH_URL
    return env


def _detect_proxy() -> str | None:
    """Return the most relevant proxy URL for outbound HTTPS, if any.

    libcurl only inspects ``HTTP_PROXY`` / ``HTTPS_PROXY`` env vars, but on
    Windows the user's Clash / V2Ray / etc is usually exposed only through
    the WinINET registry settings (HKCU\\...\\Internet Settings\\ProxyServer).
    ``urllib.request.getproxies()`` knows how to consult both, so we delegate
    to it and pick the most specific entry available.
    """
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        proxies = {}

    for scheme in ("https", "http", "all"):
        value = proxies.get(scheme)
        if value:
            return _normalize_proxy_url(value)

    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                 "ALL_PROXY", "all_proxy"):
        value = os.environ.get(name)
        if value:
            return _normalize_proxy_url(value)
    return None


def _normalize_proxy_url(value: str) -> str:
    """Ensure the proxy URL has a scheme; libcurl requires e.g. ``http://``.

    Windows WinINET stores entries like ``127.0.0.1:7890`` without a scheme,
    which libcurl rejects with a generic ``couldn't resolve proxy name``.
    """
    if "://" in value:
        return value
    return f"http://{value}"


class BaseScraper(Generic[PageResultType], ABC):
    """Abstract base class for different scraping strategies."""

    def __init__(
        self,
        config: Config,
        album_tracker: AlbumTracker,
    ) -> None:
        self.config = config
        self.runtime_config = config.runtime_config
        self.config = config
        self.album_tracker = album_tracker
        self.logger = config.runtime_config.logger

    @abstractmethod
    def get_xpath(self) -> str:
        """Return xpath of the target ."""

    @abstractmethod
    async def process_page_links(
        self,
        url: str,
        page_links: list[str],
        page_result: list[PageResultType],
        tree: html.HtmlElement,
        page_num: int,
        **kwargs: Any,
    ) -> None:
        """Process links found on the page.

        Note that different strategy has different types of page_result.

        Args:
            page_links (list[str]): The pre-processed result list, determined by get_xpath, used for page_result
            page_result (list[LinkType]): The real result of scraping.
            tree (html.HtmlElement): The xpath tree of the current page.
            page_num (int): The page number of the current URL.
        """

    def is_vip_page(self, tree: html.HtmlElement) -> bool:
        return bool(
            tree.xpath(
                '//div[contains(@class, "alert") and contains(@class, "alert-warning")]//a[contains(@href, "/user/upgrade")]',
            ),
        )


class AlbumScraper(BaseScraper[AlbumResult]):
    """Strategy for scraping album list pages."""

    XPATH_ALBUM_LIST = '//a[@class="media-cover"]/@href'

    def __init__(self, config: Config, album_tracker: AlbumTracker) -> None:
        super().__init__(config, album_tracker)
        # Best-effort name of the current listing (e.g. an actor's display
        # name from the breadcrumb/title). Populated on the first page of
        # a listing so ``ScrapeManager`` can prefer it over the raw URL
        # slug when grouping downloads. The manager resets this between
        # listings because the strategy instance is shared.
        self.last_display_name: str | None = None
        # Most recently captured actor profile (only populated when the
        # listing URL is /actor/<slug>). The manager reads + clears this
        # after ``scrape_all_pages`` completes so it can persist the
        # profile and link subsequent album rows to the actor.
        self.last_actor_profile: ActorProfile | None = None
        self._last_listing_url: str | None = None

    def get_xpath(self) -> str:
        return self.XPATH_ALBUM_LIST

    async def process_page_links(
        self,
        url: str,
        page_links: list[str],
        page_result: list[AlbumResult],
        tree: html.HtmlElement,
        page_num: int,
        **kwargs: Any,
    ) -> None:
        # First page wins: pagination on the same listing should not churn
        # the captured name (page 2+ sometimes has a "第N页" decoration that
        # ``extract_listing_display_name`` already strips, but a fresh probe
        # is still wasted work).
        if self.last_display_name is None:
            try:
                self.last_display_name = UrlHandler.extract_listing_display_name(tree)
            except Exception:
                self.last_display_name = None

        # Capture the actor profile once, on the first page of an /actor/
        # listing. Other listing types (company / category / country /
        # search) intentionally fall through with ``last_actor_profile``
        # left as None so the manager doesn't try to upsert them as
        # actors. Wrapped in a broad ``except`` because profile parsing
        # is opportunistic - it must never break the album-link scrape.
        if self.last_actor_profile is None and "/actor/" in url:
            try:
                self.last_actor_profile = ProfileExtractor.extract_actor(tree, url)
                self._last_listing_url = url
            except Exception as e:
                self.logger.debug("Actor profile extraction failed for %s: %s", url, e)

        page_result.extend([BASE_URL + album_link for album_link in page_links])
        self.logger.info("Found %d albums on page %d", len(page_links), page_num)


class ImageScraper(BaseScraper[ImageResult]):
    """Strategy for scraping album image pages."""

    # v2ph swapped a JS lazyload (URL kept in ``data-src`` until the
    # image scrolled into view) for native ``loading="lazy"`` /
    # ``decoding="async"`` on a real ``src`` attribute, so we now read
    # ``@src`` directly. Both the wrapper div AND the inner img carry
    # an ``album-photo`` class, so we match the wrapper as a whole-token
    # class to avoid picking up unrelated ``album-photo-*`` variants.
    # The ``http``-starts-with filter excludes the few site assets that
    # still ship as relative paths (e.g. ``/img/logo-ja.svg``).
    _XPATH_IMG = (
        '//div[contains(concat(" ", normalize-space(@class), " "),'
        ' " album-photo ")]/img[starts-with(@src, "http")]'
    )
    XPATH_ALBUM = f'{_XPATH_IMG}/@src'
    XPATH_ALTS = f'{_XPATH_IMG}/@alt'
    XPATH_VIP = ""

    def __init__(self, config: Config, album_tracker: AlbumTracker) -> None:
        super().__init__(config, album_tracker)
        self.cache = DirectoryCache()
        self._semaphore = asyncio.Semaphore(config.static_config.max_worker)
        self._warned_no_curl_cffi = False
        self._proxy_logged = False
        self._doh_logged = False
        self._browser_lock = asyncio.Lock()
        self._prefer_browser_only = False
        self._browser_path_logged = False
        # Optional per-listing subdirectory under ``download_dir``. Set
        # by ``ScrapeManager`` around an ``album_list`` scrape so every
        # album collected from the same listing (e.g. one actor / one
        # company) ends up grouped together. ``None`` means no grouping.
        self._parent_slug: str | None = None
        # Album profile of the album currently being scraped (captured
        # on page 1, read by ``ScrapeManager`` after the album finishes).
        # Reset by the manager between albums.
        self.last_album_profile: AlbumProfile | None = None
        # Successful image download count for the current album,
        # accumulated across pages so the manager can write an accurate
        # ``scraped_photo_count`` to the DB. Reset by the manager
        # between albums.
        self.last_album_success_count: int = 0
        # Filesystem destination of the current album (parent dir of
        # the downloaded images). Captured on page 1 so the manager
        # can record it even when later pages fail.
        self.last_album_dest: str | None = None
        # Resolved (collision-disambiguated) album folder for the
        # current album. Computed once on page 1 by
        # ``_resolve_album_dir`` and reused for all subsequent pages
        # so every image of the same album lands in the same folder
        # even when the predicted name was already claimed by a
        # different album_url. Reset by ``reset_album_state``.
        self._resolved_album_dir: Path | None = None

    def set_parent_slug(self, slug: str | None) -> None:
        """Configure (or clear) the per-listing subdirectory under which
        subsequent album downloads should be placed.

        Pass ``None`` to remove the grouping. The slug is sanitised at
        path-construction time, so the caller can pass raw site strings
        (incl. non-ASCII / spaces).
        """
        slug = (slug or "").strip()
        self._parent_slug = slug or None

    def reset_album_state(self) -> None:
        """Clear per-album scratch state (profile / counters / dest).

        Called by ``ScrapeManager`` immediately before each
        ``scrape_album`` so state from a previous album cannot leak
        into the current one (the strategy instance is reused across
        every album in a run).
        """
        self.last_album_profile = None
        self.last_album_success_count = 0
        self.last_album_dest = None
        self._resolved_album_dir = None

    def _resolve_album_dir(
        self,
        parent_dir: Path,
        album_name: str,
        album_url: str,
    ) -> Path:
        """Pick a collision-free on-disk folder for ``album_url``.

        Resolution rules (first match wins, evaluated in order):

        1. **Predicted name is free** -> use it. The folder will be
           created on the first image write; the sidecar is written
           by the caller right after.
        2. **Predicted exists with our sidecar** (``album_url``
           matches) -> reuse. This is the re-run / continuation case.
        3. **Predicted exists with NO sidecar** -> adopt for the
           current URL. This is the legacy case (folder created by
           an older v2dl build that did not write sidecars). First
           caller wins; the second caller will then see a foreign
           sidecar at step 4 and bump.
        4. **Predicted exists with a foreign sidecar** -> bump the
           suffix (`<name> (2)`, `<name> (3)`, ...) and recheck. The
           first n in [2..99] that's free or ours wins.

        The defensive 100-attempt cap is there in case a pathological
        layout (e.g. the user manually created lots of `name (N)`
        decoy folders) loops forever; we fall back to a timestamped
        unique name so the download still goes somewhere.
        """
        base = sanitize_filename(album_name) or "album"
        candidate = parent_dir / base

        if not candidate.exists():
            return candidate
        sidecar = _read_album_sidecar(candidate)
        if sidecar is None:
            return candidate  # legacy adopt
        if sidecar.get("album_url") == album_url:
            return candidate  # ours

        # Foreign-owned. Walk numeric suffixes.
        for n in range(2, 100):
            bumped_name = sanitize_filename(f"{album_name} ({n})") or f"{base}_{n}"
            candidate = parent_dir / bumped_name
            if not candidate.exists():
                return candidate
            sidecar = _read_album_sidecar(candidate)
            if sidecar is None:
                # Foreign legacy folder at this suffix - keep walking
                # rather than adopting, because once the predicted
                # name is foreign-owned we can no longer assume "no
                # sidecar means it's ours".
                continue
            if sidecar.get("album_url") == album_url:
                return candidate

        # Pathological collision count -> last-resort timestamped name.
        unique = f"{base}_collision_{int(time.time())}"
        self.logger.warning(
            "Album folder name '%s' collided too many times under %s; "
            "falling back to '%s'.",
            base, parent_dir, unique,
        )
        return parent_dir / unique

    def get_xpath(self) -> str:
        return self.XPATH_ALBUM

    async def download_file(
        self,
        url: str,
        dest: Path,
        cookies: dict[str, str] | None = None,
        web_bot: Any = None,
    ) -> bool:
        if DownloadPathTool.is_file_exists(
            dest,
            self.config.static_config.force_download,
            self.cache,
            self.logger,
        ):
            self.logger.info("File exists: '%s'", dest)
            return True

        # Merge so default HEADERS (correct Referer / sec-fetch-* for v2ph CDN)
        # win over user-supplied custom_headers, while still allowing the user
        # to add extra headers that are not in HEADERS.
        custom_headers = self.config.static_config.custom_headers or {}
        headers = {**custom_headers, **HEADERS}

        try:
            DownloadPathTool.mkdir(dest.parent)
        except Exception as e:
            self.logger.error("Error creating directory '%s': %s", dest.parent, e)
            return False

        # Path 1: download through the browser's own warmed cdn.v2ph.com tab.
        # This is by far the most reliable route on networks where Cloudflare
        # rejects curl-cffi (the request is same-origin to cdn.v2ph.com and
        # uses the exact TLS / cookie context that already cleared CF). When
        # the bot has no warmed tab (e.g. ``ensure_cdn_warmed`` failed or the
        # caller is not the image scraper), ``browser_fetch`` returns ``None``
        # and we transparently fall through to curl-cffi.
        if web_bot is not None:
            outcome = await self._download_with_browser_fetch(url, dest, web_bot)
            if outcome is True:
                return True
            if outcome is False:
                # The browser itself talked to the CDN and got back a real
                # HTTP error (403 / 404 / VIP-only etc). curl-cffi can only
                # do worse from this network, so don't waste a request on it.
                return False
            # outcome is None: no warmed tab or transient JS failure - try
            # curl-cffi as a fallback so a missing browser path doesn't
            # silently halt downloads.

        if self._prefer_browser_only:
            # We've previously committed to the browser path because
            # curl-cffi was blanket-blocked. Don't reopen that wound; just
            # report the failure for this image.
            self.logger.error(
                "Skipping curl-cffi for %s: browser-routed downloads are"
                " required on this network and the warmed CDN tab is"
                " currently unavailable.",
                url,
            )
            return False

        if HAS_CURL_CFFI:
            return await self._download_with_curl_cffi(url, dest, headers, cookies)

        if not self._warned_no_curl_cffi:
            self.logger.warning(
                "curl-cffi is not installed; falling back to httpx, which is"
                " blocked by Cloudflare on cdn.v2ph.com. Run"
                " 'pip install curl-cffi' to enable real-browser TLS"
                " fingerprinting."
            )
            self._warned_no_curl_cffi = True
        return await self._download_with_httpx(url, dest, headers, cookies)

    async def _download_with_browser_fetch(
        self,
        url: str,
        dest: Path,
        web_bot: Any,
    ) -> bool | None:
        """Download ``url`` via the warmed cdn.v2ph.com browser tab.

        Returns:
            True  - downloaded and written to ``dest`` successfully.
            False - the browser reached the CDN but the CDN returned an
                    HTTP error (403 / 404 / VIP-only). The caller should
                    NOT fall back to curl-cffi.
            None  - the bot has no warmed CDN tab, the tab has died, or
                    the in-page fetch raised a JS-level error before
                    reaching the network. The caller may fall back to
                    curl-cffi.
        """
        fetch = getattr(web_bot, "browser_fetch", None)
        if fetch is None:
            return None

        loop = asyncio.get_running_loop()
        # Serialise: the warmed tab is a single shared resource, and CDP
        # ``run_js`` calls cannot be safely interleaved against the same
        # target.
        async with self._browser_lock:
            try:
                result = await loop.run_in_executor(None, fetch, url)
            except Exception as e:
                self.logger.debug("browser_fetch raised for %s: %s", url, e)
                return None

        if result is None:
            return None

        try:
            status_code, body = result
        except Exception:
            self.logger.debug("Unexpected browser_fetch result for %s: %r", url, result)
            return None

        # status_code == 0 is the JS-side "request never made it onto the
        # network" sentinel (e.g. NetworkError, tab discarded). Treat as a
        # transient miss and let the caller fall back.
        if status_code == 0:
            return None

        if status_code >= 400:
            hint = ""
            if status_code == 403:
                hint = (
                    " (Cloudflare blocked even the same-origin in-page fetch;"
                    " the asset may be VIP-only, removed, or the cf_clearance"
                    " has just expired - try re-running)"
                )
            elif status_code == 404:
                hint = " (asset no longer on the CDN)"
            self.logger.error(
                "browser_fetch HTTP %d for %s%s",
                status_code,
                url,
                hint,
            )
            return False

        if not body:
            self.logger.warning(
                "browser_fetch returned status=%d but empty body for %s",
                status_code,
                url,
            )
            return None

        ext = "." + DownloadPathTool.get_image_ext(url, "jpg", VALID_EXTENSIONS)
        dest = dest.with_suffix(ext)

        try:
            await self._write_bytes(dest, body)
        except Exception as e:
            self.logger.error("Error writing '%s': %s", dest, e)
            return False

        if not self._browser_path_logged:
            self.logger.info(
                "Routing image downloads through the warmed cdn.v2ph.com"
                " browser tab (same-origin fetch); curl-cffi will only be"
                " used as a fallback."
            )
            self._browser_path_logged = True
            # Sticky flag: if curl-cffi later 403s and the browser path is
            # working, lock in the browser-only behaviour.
            self._prefer_browser_only = True

        self.logger.info("Downloaded: '%s'", dest)
        return True

    async def _write_bytes(self, dest: Path, body: bytes) -> None:
        """Write ``body`` to ``dest`` honouring ``rate_limit`` (KB/s).

        We do the actual file IO in the default executor so we don't block
        the event loop for big images, then sleep to enforce the configured
        bandwidth ceiling.
        """
        loop = asyncio.get_running_loop()

        def _write() -> None:
            with open(dest, "wb") as f:
                f.write(body)

        await loop.run_in_executor(None, _write)

        speed_limit_kbps = self.config.static_config.rate_limit
        if speed_limit_kbps:
            expected_time = len(body) / (speed_limit_kbps * 1024)
            if expected_time > 0:
                await asyncio.sleep(expected_time)

    async def _download_with_curl_cffi(
        self,
        url: str,
        dest: Path,
        headers: dict[str, str],
        cookies: dict[str, str] | None,
    ) -> bool:
        """Download an image using curl-cffi with a Chrome TLS fingerprint."""
        assert _CurlAsyncSession is not None  # guaranteed by HAS_CURL_CFFI

        # Force IPv4: cdn.v2ph.com publishes both AAAA (often an unreachable
        # Teredo tunnel address like 2001::xxxx) and A records. libcurl's
        # happy-eyeballs sometimes hangs for ~21s on the dead AAAA before
        # falling back, which surfaces as ``curl: (28) Failed to connect ...
        # after 21043 ms``. We pin to IPv4 to avoid that timeout.
        curl_options: dict[int, Any] = {
            _CURLOPT_IPRESOLVE: _CURL_IPRESOLVE_V4,
        }

        proxy = _detect_proxy()
        if proxy:
            # When the user is behind a system proxy (Clash / V2Ray etc on
            # Windows), let the proxy handle DNS as well - it knows how to
            # reach cdn.v2ph.com. Local DoH would only get in the way.
            if not self._proxy_logged:
                self.logger.info("Routing image downloads via system proxy: %s", proxy)
                self._proxy_logged = True
        else:
            # No proxy: resolve through DoH to dodge local UDP-DNS poisoning
            # (common on mainland-China ISPs - cdn.v2ph.com is sinkholed to
            # Facebook / Twitter IP ranges via plain DNS).
            doh_url = _get_doh_url()
            if doh_url:
                curl_options[_CURLOPT_DOH_URL] = doh_url
                if not self._doh_logged:
                    self.logger.info("Resolving cdn.v2ph.com via DoH: %s", doh_url)
                    self._doh_logged = True

        session_kwargs: dict[str, Any] = {
            "impersonate": CURL_IMPERSONATE_TARGET,
            "headers": headers,
            "cookies": cookies or None,
            "timeout": 30,
            "curl_options": curl_options,
        }
        if proxy:
            session_kwargs["proxy"] = proxy

        try:
            async with self._semaphore:
                async with _CurlAsyncSession(**session_kwargs) as session:
                    response = await session.get(url, stream=True, allow_redirects=True)
                    try:
                        if response.status_code >= 400:
                            await self._log_curl_cffi_error(response, url)
                            return False

                        ext = "." + self._guess_ext_from_curl(response, url)
                        dest = dest.with_suffix(ext)

                        await self._write_stream(
                            dest, response.aiter_content(chunk_size=8192)
                        )
                    finally:
                        try:
                            await response.aclose()
                        except Exception:
                            pass

            self.logger.info("Downloaded: '%s'", dest)
            return True
        except Exception as e:
            self.logger.error("Error downloading '%s': %s", dest, e)
            return False

    async def _download_with_httpx(
        self,
        url: str,
        dest: Path,
        headers: dict[str, str],
        cookies: dict[str, str] | None,
    ) -> bool:
        """Fallback path for when curl-cffi is not installed."""
        try:
            limits = httpx.Limits(
                max_keepalive_connections=self.config.static_config.max_worker,
                max_connections=self.config.static_config.max_worker * 2,
            )
            async with self._semaphore:
                async with httpx.AsyncClient(
                    headers=headers,
                    cookies=cookies or None,
                    http2=True,
                    timeout=httpx.Timeout(30.0),
                    follow_redirects=True,
                    limits=limits,
                ) as client:
                    async with client.stream("GET", url) as response:
                        if response.status_code >= 400:
                            await self._log_httpx_error(response, url)
                            response.raise_for_status()
                        ext = "." + DownloadPathTool.get_ext(response)
                        dest = dest.with_suffix(ext)

                        await self._write_stream(dest, response.aiter_bytes(8192))

            self.logger.info("Downloaded: '%s'", dest)
            return True
        except Exception as e:
            self.logger.error("Error downloading '%s': %s", dest, e)
            return False

    async def _write_stream(self, dest: Path, chunks: Any) -> None:
        """Write an async byte iterator to ``dest`` honouring ``rate_limit``."""
        speed_limit_kbps = self.config.static_config.rate_limit
        total_bytes = 0
        start_time = asyncio.get_running_loop().time()

        with open(dest, "wb") as f:
            async for chunk in chunks:
                if not chunk:
                    continue
                f.write(chunk)

                if speed_limit_kbps:
                    total_bytes += len(chunk)
                    expected_time = total_bytes / (speed_limit_kbps * 1024)
                    elapsed_time = abs(asyncio.get_running_loop().time() - start_time)
                    if elapsed_time < expected_time:
                        await asyncio.sleep(expected_time - elapsed_time)

    def _guess_ext_from_curl(self, response: Any, url: str) -> str:
        """Mimic ``DownloadPathTool.get_ext`` for a curl-cffi response."""
        content_type = ""
        try:
            content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip()
        except Exception:
            pass
        if content_type:
            ext = guess_extension(content_type)
            if ext:
                return ext.lstrip(".")
        return DownloadPathTool.get_image_ext(url, "jpg", VALID_EXTENSIONS)

    async def _log_httpx_error(self, response: httpx.Response, url: str) -> None:
        """Log a short, actionable diagnostic for httpx 4xx/5xx responses."""
        try:
            body = await response.aread()
        except Exception:
            body = b""
        self._log_error_common(response.status_code, dict(response.headers), body, url)

    async def _log_curl_cffi_error(self, response: Any, url: str) -> None:
        """Log a short, actionable diagnostic for curl-cffi 4xx/5xx responses."""
        try:
            body = await response.acontent()
        except Exception:
            try:
                body = response.content or b""
            except Exception:
                body = b""

        # curl-cffi exposes headers as a Headers mapping; normalize to dict for
        # the shared formatter.
        try:
            headers_dict = {k.lower(): v for k, v in response.headers.items()}
        except Exception:
            headers_dict = {}
        self._log_error_common(response.status_code, headers_dict, body, url)

    def _log_error_common(
        self,
        status_code: int,
        headers: dict[str, str],
        body: bytes,
        url: str,
    ) -> None:
        """Format and emit the actual error log line (shared by both backends).

        Surfacing ``server`` / ``cf-ray`` / the first bytes of the body lets the
        user tell apart a Cloudflare bot block from plain hotlink-protection
        or VIP-only assets.
        """
        snippet = body[:200].decode("utf-8", errors="replace").replace("\n", " ").strip()
        server = headers.get("server") or headers.get("Server") or "?"
        cf_ray = headers.get("cf-ray") or headers.get("Cf-Ray") or ""

        lower = snippet.lower()
        hint = ""
        if "cloudflare" in lower or "cf-ray" in lower or cf_ray:
            hint = (
                " (Cloudflare block; even with Chrome TLS fingerprint the"
                " challenge was not cleared. Re-open the album in the bot"
                " browser to refresh cf_clearance / __cf_bm, or temporarily"
                " disable VPN/proxy)"
            )
        self.logger.error(
            "HTTP %d for %s [server=%s cf-ray=%s]%s body=%r",
            status_code,
            url,
            server,
            cf_ray,
            hint,
            snippet,
        )

    async def process_page_links(
        self,
        url: str,
        page_links: list[str],
        page_result: list[ImageResult],
        tree: html.HtmlElement,
        page_num: int,
        **kwargs: Any,
    ) -> None:
        """The input `url` is the album's url, not image url"""
        is_VIP = False
        alts: list[str] = tree.xpath(self.XPATH_ALTS)
        page_result.extend(zip(page_links, alts, strict=False))

        # Capture the album profile on page 1 (subsequent pages of the
        # same album re-render the same card, so re-extracting would
        # be wasted work). Wrapped broadly because parsing failures
        # must NOT block image downloads - profile capture is a
        # best-effort feature.
        if page_num == 1 and self.last_album_profile is None:
            try:
                self.last_album_profile = ProfileExtractor.extract_album(tree, url)
            except Exception as e:
                self.logger.debug("Album profile extraction failed for %s: %s", url, e)

        available_images = self.get_available_images(tree)
        idx = (page_num - 1) * IMAGE_PER_PAGE + 1

        album_name = UrlHandler.extract_album_name(alts)
        dir_ = self.config.static_config.download_dir
        # When the manager set a per-listing slug (e.g. the actor's
        # display name), nest album folders under it so a listing scrape
        # produces ``<download_dir>/<listing>/<album>/...`` instead of
        # mixing every album under the global download root. Sanitised
        # here because ``download_root`` is not sanitised by
        # ``DownloadPathTool.get_file_dest`` (only the album name is).
        if self._parent_slug:
            dir_ = str(Path(dir_) / sanitize_filename(self._parent_slug))

        # Resolve the album folder ONCE per album (page 1 wins; later
        # pages reuse the cached path). This prevents two distinct
        # album_urls that happen to share the same ``<img alt>``-derived
        # name from silently overwriting each other - the second album
        # gets bumped to ``<name> (2)`` based on the sidecar at
        # ``<name>/.v2dl_album.json``. See ``_resolve_album_dir`` for
        # the full ruleset.
        clean_album_url = UrlHandler.remove_query_params(url)
        if self._resolved_album_dir is None:
            self._resolved_album_dir = self._resolve_album_dir(
                Path(dir_), album_name, clean_album_url
            )
            try:
                self._resolved_album_dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                self.logger.error(
                    "Failed to create album dir %s: %s",
                    self._resolved_album_dir, e,
                )
            # Claim the folder for this URL. Title is taken from the
            # captured profile when available, else falls back to the
            # alt-derived ``album_name`` so even a profile-extraction
            # failure still produces a useful sidecar.
            sidecar_title: str | None = None
            if self.last_album_profile is not None:
                sidecar_title = self.last_album_profile.title
            if not sidecar_title:
                sidecar_title = album_name
            _write_album_sidecar(
                self._resolved_album_dir, clean_album_url, sidecar_title
            )

        album_dir = self._resolved_album_dir

        # Cookies snapshotted from the live browser session in PageScraper.
        # Forwarded so cdn.v2ph.com sees the same authenticated session and
        # does not return 403 Forbidden via Cloudflare hotlink protection.
        cookies = kwargs.get("cookies") or None
        # The bot is forwarded so ``download_file`` can route through the
        # warmed cdn.v2ph.com tab via ``browser_fetch`` - the only path
        # that reliably clears Cloudflare on networks where curl-cffi's
        # TLS impersonation is no longer enough.
        web_bot = kwargs.get("web_bot")

        download_tasks = []
        download_paths = []
        page_link_ctr = 0
        for i, available in enumerate(available_images):
            if not available:
                is_VIP = True
                continue
            if page_link_ctr >= len(page_links):
                break
            image_url = page_links[page_link_ctr]
            page_link_ctr += 1

            filename = f"{(idx + i):03d}"
            # Bypass DownloadPathTool.get_file_dest: it would rebuild
            # the folder path from the (possibly colliding) album_name
            # and undo the resolution we just did. Build the dest
            # directly inside the resolved album folder. The downloader
            # appends the actual extension from the response.
            dest = album_dir / sanitize_filename(filename)
            download_tasks.append(
                self.download_file(image_url, dest, cookies=cookies, web_bot=web_bot)
            )
            download_paths.append(dest)

        if download_tasks:
            download_results = await asyncio.gather(*download_tasks)

            successful_downloads = sum(1 for result in download_results if result)
            failed_downloads = len(download_results) - successful_downloads

            if failed_downloads > 0:
                self.logger.warning("Failed to download %d images", failed_downloads)

            # Accumulate per-album success count across pages so the
            # manager can persist an accurate ``scraped_photo_count``
            # to the profile DB. Reset by ``reset_album_state``.
            self.last_album_success_count += successful_downloads

        self.logger.info("Found %d images on page %d", len(page_links), page_num)

        # Always report the resolved album folder (whether we wrote
        # any images this page or not - skipped/VIP pages still want
        # a valid dest for the profile DB row).
        destination = album_dir
        if self.last_album_dest is None:
            self.last_album_dest = str(destination)

        album_status = DownloadStatus.VIP if is_VIP else DownloadStatus.OK
        clean_url = UrlHandler.remove_query_params(url)
        self.album_tracker.update_download_log(
            clean_url, {LogKey.status: album_status, LogKey.dest: str(destination)}
        )

    def get_available_images(self, tree: html.HtmlElement) -> list[bool]:
        album_photos = tree.xpath(
            '//div[contains(concat(" ", normalize-space(@class), " "),'
            ' " album-photo ")][.//img[starts-with(@src, "http")]]'
        )
        return [True] * len(album_photos)
