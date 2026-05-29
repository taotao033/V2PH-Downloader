import os
import re
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from logging import Logger
from pathlib import Path
from typing import Any, ClassVar, Optional
from urllib.parse import parse_qs, unquote, urlencode, urlparse, urlunparse

from lxml import html

from v2dl.common import Config
from v2dl.common.const import AVAILABLE_LANGUAGES, BASE_URL, normalize_language
from v2dl.common.utils import count_files, enum_to_string
from v2dl.scraper.types import ScrapeType


@dataclass(frozen=True)
class LogKey:
    status: str = "status"
    dest: str = "dest"
    expect_num: str = "expect_num"
    real_num: str = "real_num"


class DownloadStatus(Enum):
    OK = 10
    VIP = 20
    FAIL = 30

    def __lt__(self, other: Any) -> bool:
        if isinstance(other, DownloadStatus):
            return self.value < other.value
        return NotImplemented

    def __le__(self, other: Any) -> bool:
        if isinstance(other, DownloadStatus):
            return self.value <= other.value
        return NotImplemented

    def __gt__(self, other: Any) -> bool:
        if isinstance(other, DownloadStatus):
            return self.value > other.value
        return NotImplemented

    def __ge__(self, other: Any) -> bool:
        if isinstance(other, DownloadStatus):
            return self.value >= other.value
        return NotImplemented

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, DownloadStatus):
            return self.value == other.value
        return NotImplemented

    def __ne__(self, other: Any) -> bool:
        if isinstance(other, DownloadStatus):
            return self.value != other.value
        return NotImplemented


class AlbumTracker:
    """Download log in units of albums."""

    def __init__(self, download_log_path: str):
        self.album_log_path = download_log_path
        self.download_status: dict[str, dict[str, Any]] = {}
        self.keys = LogKey()

    def is_downloaded(self, album_url: str) -> bool:
        if os.path.exists(self.album_log_path):
            with open(self.album_log_path, encoding="utf-8") as f:
                downloaded_albums = f.read().splitlines()
            return album_url in downloaded_albums
        return False

    def log_downloaded(self, album_url: str) -> None:
        album_url = UrlHandler.remove_page_num(album_url)
        if not self.is_downloaded(album_url):
            with open(self.album_log_path, "a", encoding="utf-8") as f:
                f.write(album_url + "\n")

    def update_download_log(self, album_url: str, metadata: dict[str, Any]) -> None:
        album_url = UrlHandler.remove_query_params(album_url)
        if album_url not in self.download_status:
            self.download_status[album_url] = {
                self.keys.status: DownloadStatus.OK,
                self.keys.dest: "",
                self.keys.expect_num: 0,
                self.keys.real_num: 0,
            }

        for key, value in metadata.items():
            if key in self.keys.__dict__.values():
                self.download_status[album_url][key] = value

    def init_download_log(self, album_url: str, **kwargs: Any) -> None:
        album_url = UrlHandler.remove_query_params(album_url)
        default_metadata = {
            self.keys.status: DownloadStatus.OK,
            self.keys.dest: "",
            self.keys.expect_num: 0,
            self.keys.real_num: 0,
        }
        default_metadata.update(kwargs)
        self.download_status[album_url] = default_metadata

    @property
    def get_download_status(self) -> dict[str, dict[str, Any]]:
        return self.download_status


class UrlHandler:
    """Handles URL parsing and management."""

    URL_HANDLERS: ClassVar[dict[str, ScrapeType]] = {
        "album": "album_image",
        "actor": "album_list",
        "company": "album_list",
        "category": "album_list",
        "country": "album_list",
        "search": "album_list",
    }

    @classmethod
    def get_scrape_type(cls, url: str) -> Optional[ScrapeType]:
        """Get the appropriate handler method based on URL path."""
        path_parts, _ = UrlHandler.parse_input_url(url)
        for part in path_parts:
            if part in cls.URL_HANDLERS:
                return cls.URL_HANDLERS[part]
        return None

    @staticmethod
    def load_urls(url: str, url_file: Optional[str]) -> list[str]:
        """Load URLs from config (URL or txt file)."""
        if url_file:
            with open(url_file, encoding="utf-8") as file:
                urls = [line.strip() for line in file if line.strip() and not line.startswith("#")]
        else:
            urls = [url]
        return urls

    @staticmethod
    def mark_processed_url(url_file: str, target_url: str) -> None:
        """Mark URL as processed in the URL file."""
        with open(url_file, "r+", encoding="utf-8") as file:
            lines = file.readlines()
            file.seek(0)

            for line in lines:
                if line.strip().startswith(UrlHandler.remove_query_params(target_url)):
                    file.write(f"# {line}")
                else:
                    file.write(line)

            file.truncate()

    @staticmethod
    def parse_input_url(url: str) -> tuple[list[str], int]:
        """
        Extracts path segments and the starting page number from a URL.

        Args:
            url (str): Input URL.

        Returns:
            tuple[list[str], int]: Path segments and the starting page number.
        """
        parsed_url = urlparse(url)
        path_parts = parsed_url.path.split("/")
        query_params = parse_qs(parsed_url.query)
        start_page: int = int(query_params.get("page", [1])[0])  # default page=1
        return path_parts, start_page

    @staticmethod
    def parse_html(html_content: str, logger: Logger) -> html.HtmlElement | None:
        """Parses HTML content into an HTML element.

        Args:
            html_content (str): HTML content as a string.
            logger (Logger): Logger for error handling.

        Returns:
            html.HtmlElement | None: Parsed HTML element or None if parsing fails.
        """
        if "Failed" in html_content:
            return None

        try:
            return html.fromstring(html_content)
        except Exception as e:
            logger.error("Error parsing HTML content: %s", e)
            return None

    @staticmethod
    def get_max_page(tree: html.HtmlElement) -> int:
        """
        Retrieves the maximum page number from a pagination element.

        Args:
            tree (html.HtmlElement): Parsed HTML tree.

        Returns:
            int: Maximum page number, default is 1 if none found.
        """
        page_links = tree.xpath(
            '//li[@class="page-item"]/a[@class="page-link" and string-length(text()) <= 2]/@href',
        )

        if not page_links:
            return 1

        page_numbers = []
        for link in page_links:
            match = re.search(r"page=(\d+)", link)
            if match:
                page_number = int(match.group(1))
            else:
                page_number = 1
            page_numbers.append(page_number)

        return max(page_numbers)

    @staticmethod
    def add_page_num(url: str, page: int) -> str:
        """
        Adds or updates the page number in a URL.

        Args:
            url (str): Original URL.
            page (int): Page number to add or update.

        Returns:
            str: Updated URL with the specified page number.
        """
        parsed_url = urlparse(url)  # 解析 URL
        query_params = parse_qs(parsed_url.query)  # 解析查詢參數
        query_params["page"] = [str(page)]  # 修改頁碼

        new_query = urlencode(query_params, doseq=True)  # 組合成字串
        new_url = parsed_url._replace(query=new_query)  # 替換頁碼

        # Example
        # url = "https://example.com/search?q=test&sort=asc", page = 3
        # parsed_url: ParseResult(scheme='https', netloc='example.com', path='/search', params='', query='q=test&sort=asc', fragment='')
        # query_params: {'q': ['test'], 'sort': ['asc'], 'page': ['3']}
        # new_query: 'q=test&sort=asc&page=3'
        # new_url: ParseResult(scheme='https', netloc='example.com', path='/search', params='', query='q=test&sort=asc&page=3', fragment='')
        # urlunparse: 'https://example.com/search?q=test&sort=asc&page=3'
        return urlunparse(new_url)

    @staticmethod
    def remove_page_num(url: str) -> str:
        """
        Removes the page parameter from a URL.

        Args:
            url (str): Original URL.

        Returns:
            str: URL without the page parameter.
        """
        parsed_url = urlparse(url)
        query_params = parse_qs(parsed_url.query)

        if "page" in query_params:
            del query_params["page"]

        new_query = urlencode(query_params, doseq=True)

        return urlunparse(parsed_url._replace(query=new_query))

    @staticmethod
    def remove_query_params(url: str) -> str:
        parsed_url = urlparse(url)
        return urlunparse(parsed_url._replace(query=""))

    @staticmethod
    def force_language(url: str, lang: str) -> str:
        """Drop any existing ``?hl=`` and re-apply the configured language.

        Use this for URLs harvested *from* page HTML (e.g. ``<a
        class="media-cover" href>``). The ``hl`` query in those hrefs
        reflects how the *listing* page was rendered, which may itself be
        a server-side fallback to English when the listing was requested
        with an unrecognised code (e.g. ``?hl=zh``). Without scrubbing
        that, every downstream album fetch inherits the wrong language.

        Unlike :meth:`update_language`, this does **not** respect an
        already-present ``hl``; the caller is asserting that the URL is
        scraped output, not user intent.
        """
        parsed_url = urlparse(url)
        query = parse_qs(parsed_url.query)
        query.pop("hl", None)
        canonical = normalize_language(lang)
        if canonical is not None:
            query["hl"] = [canonical]
        updated_query = urlencode(query, doseq=True)
        return urlunparse(parsed_url._replace(query=updated_query))

    @staticmethod
    def update_language(url: str, lang: str) -> str:
        """Apply the configured language to ``url``'s ``?hl=`` query.

        Rules (in priority order):

        1. If the URL already specifies an ``hl`` that v2ph understands,
           leave it alone. This respects user intent for hand-crafted URLs
           like ``/actor/<x>?hl=zh-Hans`` and avoids overwriting it with a
           generic default that may not match (e.g. someone left
           ``language: ja`` in config.yaml but pasted a Chinese URL).
        2. Otherwise, normalize the config-supplied ``lang`` via
           ``normalize_language`` (handles aliases like ``zh`` ->
           ``zh-Hans``, ``zh-TW`` -> ``zh-Hant``) and apply that.
        3. If neither the URL nor the config produces a recognised code,
           drop the ``hl`` query entirely so v2ph picks its own default
           rather than silently falling back to English on an invalid
           ``?hl=zh``.
        """
        parsed_url = urlparse(url)
        query = parse_qs(parsed_url.query)

        existing = (query.get("hl") or [""])[0]
        existing_canonical = normalize_language(existing)
        if existing_canonical is not None:
            query["hl"] = [existing_canonical]
        else:
            canonical = normalize_language(lang)
            if canonical is not None:
                query["hl"] = [canonical]
            else:
                query.pop("hl", None)

        updated_query = urlencode(query, doseq=True)
        return urlunparse(parsed_url._replace(query=updated_query))

    @staticmethod
    def handle_first_page(target_page: int | list[int]) -> tuple[int, bool]:
        scrape_one_page = False
        if isinstance(target_page, list):
            if len(target_page) == 0:
                # '5'
                page = target_page[0]
                scrape_one_page = True
            else:
                # '5-10' or '5:10:20'
                page = target_page[0]
        else:
            page = target_page
        return page, scrape_one_page

    @staticmethod
    def handle_pagination(current_page: int, target_page: int | list[int]) -> int | None:
        """Handle pagination logic including sleep for consecutive pages."""
        if isinstance(target_page, list):
            if len(target_page) == 1:
                # '5'
                next_page = None
            elif len(target_page) == 2:
                # '5-10'
                next_page = current_page + 1
                if next_page > target_page[-1]:
                    next_page = None
            elif len(target_page) == 3:
                # '5:10:20'
                next_page = current_page + target_page[1]
                if next_page > target_page[2]:
                    next_page = None
        else:
            next_page = current_page + 1

        return next_page

    @staticmethod
    def extract_album_name(alts: list[str]) -> str:
        album_name = next((alt for alt in alts if not alt.isdigit()), None)
        if album_name:
            album_name = re.sub(r"\s*\d*$", "", album_name).strip()
        if not album_name:
            album_name = BASE_URL.rstrip("/").split("/")[-1]
        return album_name

    # Listing types whose URL slug (segment right after the type) is the
    # "owning entity" we want to group downloads by (company / actor / etc).
    # ``search`` is intentionally excluded because the slug after /search/
    # is not stable (it's a query string).
    _LISTING_TYPES_WITH_SLUG: ClassVar[tuple[str, ...]] = (
        "company",
        "actor",
        "category",
        "country",
    )

    @classmethod
    def extract_listing_slug(cls, url: str) -> str | None:
        """Return the listing slug from a /company/<X> (or /actor/<X>, etc.) URL.

        The slug is URL-decoded so that sites using percent-encoded Unicode
        (e.g. ``/company/%E7%BD%91%E7%BB%9C%E7%BE%8E%E5%A5%B3``) yield a
        readable name. Returns ``None`` for URLs that are not a listing
        (e.g. ``/album/...``).
        """
        try:
            parsed = urlparse(url)
        except Exception:
            return None
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) < 2:
            return None
        listing_type, slug = parts[0], parts[1]
        if listing_type not in cls._LISTING_TYPES_WITH_SLUG:
            return None
        try:
            slug = unquote(slug)
        except Exception:
            pass
        slug = slug.strip()
        return slug or None

    # Common page-title suffixes on v2ph (the site brand). The site uses
    # multiple writings depending on the ?hl= query (zh-Hans / zh-Hant / en),
    # so we strip them all generically and fall back to a last-resort " - "
    # / " | " split.
    _TITLE_SITE_SUFFIXES: ClassVar[tuple[str, ...]] = (
        " - 微图坊",
        " - 微圖坊",
        " - V2PH",
        " - v2ph",
        " | V2PH",
        " | v2ph",
    )

    @classmethod
    def extract_listing_display_name(cls, tree: html.HtmlElement) -> str | None:
        """Best-effort extraction of the visible name of the current listing.

        Tries the Bootstrap breadcrumb first (most specific - shows just the
        current entity), then falls back to the ``<title>`` element with
        common site-name suffixes stripped. Returns ``None`` if nothing
        usable is found; callers should fall back to the URL slug.
        """
        if tree is None:
            return None

        try:
            crumb_nodes = tree.xpath(
                '//ol[contains(@class,"breadcrumb")]//li[last()]//text()'
            )
            for node in crumb_nodes:
                text = (node or "").strip()
                if text:
                    return text
        except Exception:
            pass

        try:
            title_nodes = tree.xpath('//title/text()')
        except Exception:
            title_nodes = []
        for node in title_nodes:
            title = (node or "").strip()
            if not title:
                continue
            for suffix in cls._TITLE_SITE_SUFFIXES:
                if title.endswith(suffix):
                    title = title[: -len(suffix)].strip()
                    break
            else:
                for sep in (" - ", " | ", " — "):
                    if sep in title:
                        title = title.rsplit(sep, 1)[0].strip()
                        break
            # Drop trailing "第N页" / "Page N" decorations sometimes added by
            # paginated listings so multi-page scrapes don't produce a
            # different folder name on page 2+.
            title = re.sub(r"\s*第\s*\d+\s*[页頁]\s*$", "", title).strip()
            title = re.sub(r"\s*Page\s*\d+\s*$", "", title, flags=re.IGNORECASE).strip()
            if title:
                return title
        return None

    @staticmethod
    def parse_page_range(page_range: str) -> list[int]:
        pattern = r"^(\d+|\d+-\d+|\d+:\d+:\d+)$"
        if not re.match(pattern, page_range):
            raise ValueError("Invalid format. Must be '5', '8-20', or '1:24:3'")

        if "-" in page_range:
            start, end = map(int, page_range.split("-"))
            return [start, end]
        elif ":" in page_range:
            return list(map(int, page_range.split(":")))
        else:
            return [int(page_range)]


class MetadataHandler:
    """Handles metadata operations."""

    def __init__(self, config: Config, album_tracker: AlbumTracker) -> None:
        self.config = config
        self.album_tracker = album_tracker

    def write_metadata(self) -> None:
        """Write metadata to a file."""
        if self.config.static_config.no_metadata:
            return

        download_status = self.album_tracker.get_download_status

        # count real files
        for url, album_status in download_status.items():
            dest = album_status[LogKey.dest]
            real_num = 0 if not dest else count_files(Path(dest))
            self.album_tracker.update_download_log(url, {LogKey.real_num: real_num})

        # write metadata
        if self.config.static_config.metadata_path:
            metadata_dest = Path(self.config.static_config.metadata_path)
        else:
            metadata_name = "metadata_" + str(datetime.now().strftime("%Y%m%d_%H%M%S")) + ".json"
            metadata_dest = Path(self.config.static_config.download_dir) / metadata_name

        metadata_dest.parent.mkdir(parents=True, exist_ok=True)
        with metadata_dest.open("w", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    download_status,
                    indent=4,
                    ensure_ascii=False,
                    default=enum_to_string,
                )
            )
