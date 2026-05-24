import os
import asyncio
import urllib.request
from abc import ABC, abstractmethod
from mimetypes import guess_extension
from pathlib import Path
from typing import Any, Generic

import httpx
from lxml import html

from v2dl.common import Config
from v2dl.common.const import BASE_URL, HEADERS, IMAGE_PER_PAGE, VALID_EXTENSIONS
from v2dl.scraper.downloader import DirectoryCache, DownloadPathTool
from v2dl.scraper.tools import AlbumTracker, DownloadStatus, LogKey, UrlHandler
from v2dl.scraper.types import AlbumResult, ImageResult, PageResultType

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
        page_result.extend([BASE_URL + album_link for album_link in page_links])
        self.logger.info("Found %d albums on page %d", len(page_links), page_num)


class ImageScraper(BaseScraper[ImageResult]):
    """Strategy for scraping album image pages."""

    XPATH_ALBUM = '//div[contains(@class,"album-photo")]/img/@data-src'
    XPATH_ALTS = '//div[contains(@class,"album-photo")]/img/@alt'
    XPATH_VIP = ""

    def __init__(self, config: Config, album_tracker: AlbumTracker) -> None:
        super().__init__(config, album_tracker)
        self.cache = DirectoryCache()
        self._semaphore = asyncio.Semaphore(config.static_config.max_worker)
        self._warned_no_curl_cffi = False
        self._proxy_logged = False
        self._doh_logged = False

    def get_xpath(self) -> str:
        return self.XPATH_ALBUM

    async def download_file(
        self,
        url: str,
        dest: Path,
        cookies: dict[str, str] | None = None,
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

        available_images = self.get_available_images(tree)
        idx = (page_num - 1) * IMAGE_PER_PAGE + 1

        album_name = UrlHandler.extract_album_name(alts)
        dir_ = self.config.static_config.download_dir

        # Cookies snapshotted from the live browser session in PageScraper.
        # Forwarded so cdn.v2ph.com sees the same authenticated session and
        # does not return 403 Forbidden via Cloudflare hotlink protection.
        cookies = kwargs.get("cookies") or None

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
            dest = DownloadPathTool.get_file_dest(dir_, album_name, filename)
            download_tasks.append(self.download_file(image_url, dest, cookies=cookies))
            download_paths.append(dest)

        if download_tasks:
            download_results = await asyncio.gather(*download_tasks)

            successful_downloads = sum(1 for result in download_results if result)
            failed_downloads = len(download_results) - successful_downloads

            if failed_downloads > 0:
                self.logger.warning("Failed to download %d images", failed_downloads)

        self.logger.info("Found %d images on page %d", len(page_links), page_num)

        destination = download_paths[0].parent if download_paths else Path(dir_) / album_name

        album_status = DownloadStatus.VIP if is_VIP else DownloadStatus.OK
        clean_url = UrlHandler.remove_query_params(url)
        self.album_tracker.update_download_log(
            clean_url, {LogKey.status: album_status, LogKey.dest: str(destination)}
        )

    def get_available_images(self, tree: html.HtmlElement) -> list[bool]:
        album_photos = tree.xpath('//div[contains(@class,"album-photo")][.//img[@data-src]]')
        return [True] * len(album_photos)
