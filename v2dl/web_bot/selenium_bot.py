import sys
import time
import base64
import random
import asyncio
from datetime import datetime
from logging import Logger
from subprocess import Popen
from typing import TYPE_CHECKING, Any

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from v2dl.common import BotError
from v2dl.common.cookies import load_cookies
from v2dl.web_bot.base import (
    BaseBehavior,
    BaseBot,
    BaseScroll,
    cf_hard_blocked,
    cf_simple_blocked,
)

# Optional ONNX-based local OCR for v2ph's 4-character image captcha.
# Used to auto-solve the captcha a few times before falling back to
# the manual-input wait loop. Install with ``pip install v2dl[ocr]``
# or ``pip install ddddocr``. Without it the auto-solve step is just
# skipped and the user is asked to type the captcha manually.
try:
    import ddddocr  # type: ignore[import-not-found]

    _HAS_DDDDOCR = True
except Exception:
    ddddocr = None  # type: ignore[assignment]
    _HAS_DDDDOCR = False

if TYPE_CHECKING:
    from v2dl.common import Config
    from v2dl.security import AccountManager, KeyManager

DEFAULT_BOT_OPT = [
    "--remote-debugging-port=9222",
    "--disable-gpu",
    "--disable-infobars",
    "--disable-extensions",
    "--disable-dev-shm-usage",
    "--start-maximized",
]


class SeleniumBot(BaseBot):
    def __init__(
        self,
        config: "Config",
        key_manager: "KeyManager",
        account_manager: "AccountManager",
    ) -> None:
        super().__init__(config, key_manager, account_manager)
        self.init_driver()
        self.scroller = SelScroll(self.driver, self.config, self.logger)
        self.cloudflare = SelCloudflareHandler(self.driver, self.logger)
        # Persistent Selenium window handle parked on cdn.v2ph.com once
        # it has passed Cloudflare. Reused for browser-routed downloads
        # via fetch() so that the request is first-party / same-origin
        # to the CDN.
        self._cdn_window_handle: str | None = None
        # Lazily-initialised ddddocr instance and an init-failure latch.
        # We don't load the ONNX model until the first captcha actually
        # fires (~200 ms warm-up + a few MB of RAM).
        self._ocr: Any = None
        self._ocr_init_failed: bool = False

    def init_driver(self) -> None:
        self.driver: WebDriver
        options = Options()
        options.add_argument("--no-exit")
        chrome_path = [self.config.static_config.chrome_exec_path]

        # commands for running subprocess
        subprocess_cmd = chrome_path + (DEFAULT_BOT_OPT)

        if not self.config.static_config.use_default_chrome_profile:
            user_data_dir = self.prepare_chrome_profile()
            subprocess_cmd.append(f"--user-data-dir={user_data_dir}")

        for arg in subprocess_cmd:
            options.add_argument(arg)

        # additional args for webdriver.Chrome to takeover the control of created browser
        options.add_experimental_option("debuggerAddress", "127.0.0.1:9222")
        try:
            self.chrome_process = Popen(subprocess_cmd)  # subprocess.run fails
            self.driver = webdriver.Chrome(service=Service(), options=options)
        except Exception as e:
            self.logger.error("Unable to start Selenium WebDriver: %s", e)
            sys.exit("Unable to start Selenium WebDriver")

    def close_driver(self) -> None:
        if self._cdn_window_handle is not None:
            try:
                # Switch to the parked CDN window and close just that
                # window before quitting the driver. This is cosmetic;
                # ``driver.quit()`` would close everything anyway, but
                # being explicit avoids a stray "tab is closing" race
                # if the user runs with ``terminate: false``.
                self.driver.switch_to.window(self._cdn_window_handle)
                self.driver.close()
            except Exception:
                pass
            self._cdn_window_handle = None
        self.driver.quit()
        self.chrome_process.terminate()

    def get_cookies(self) -> dict[str, str]:
        """Snapshot the live cookies of the Selenium browser session.

        Mirrors ``DrissionBot.get_cookies``; uses CDP to read cookies
        across ALL domains. Selenium's ``driver.get_cookies()`` only
        returns cookies for the current tab's domain, which leaves
        cdn.v2ph.com without its own ``__cf_bm`` / ``cf_clearance``
        and produces 403 "Just a moment..." on the CDN downloads.
        """
        raw: list[dict[str, Any]] = []
        try:
            # CDP works on Chromium-based drivers and gives us the
            # entire cookie jar in one shot.
            cdp_result = self.driver.execute_cdp_cmd("Network.getAllCookies", {})
            raw = cdp_result.get("cookies", []) if isinstance(cdp_result, dict) else []
        except Exception as e:
            self.logger.debug("CDP Network.getAllCookies failed (%s); falling back", e)
            try:
                raw = list(self.driver.get_cookies() or [])
            except Exception as e2:
                self.logger.warning("Failed to read browser cookies: %s", e2)
                return {}

        preferred_domain = "cdn.v2ph.com"
        staged: dict[str, tuple[str, str]] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            value = item.get("value")
            domain = (item.get("domain") or "").lstrip(".")
            if not name or value is None:
                continue
            key = str(name)
            if key not in staged:
                staged[key] = (str(domain), str(value))
            else:
                current_domain, _ = staged[key]
                if domain == preferred_domain and current_domain != preferred_domain:
                    staged[key] = (str(domain), str(value))
        return {k: v for k, (_, v) in staged.items()}

    def ensure_cdn_warmed(self, url: str) -> bool:
        """Open ``url`` in a background window and wait for Cloudflare
        to clear. The window is **kept open** afterwards so
        ``browser_fetch`` can run ``fetch()`` from it (same-origin to
        cdn.v2ph.com).
        """
        if self._cdn_window_handle is not None:
            return True
        if getattr(self, "_cdn_warmed_failed", False):
            return False
        if not url:
            return False

        original_handle: str | None = None
        new_handle: str | None = None
        keep_window = False
        last_ct = ""
        warmup_timeout = 25.0
        try:
            original_handle = self.driver.current_window_handle
            existing = set(self.driver.window_handles)
            self.driver.execute_script("window.open(arguments[0], '_blank');", url)
            for handle in self.driver.window_handles:
                if handle not in existing:
                    new_handle = handle
                    break

            if new_handle is None:
                self.logger.warning("CDN warmup could not open a new window")
                self._cdn_warmed_failed = True
                return False

            self.driver.switch_to.window(new_handle)
            deadline = time.time() + warmup_timeout
            while time.time() < deadline:
                try:
                    ct = self.driver.execute_script(
                        "return document.contentType || ''"
                    )
                except Exception:
                    ct = ""
                last_ct = str(ct or "")
                if last_ct.lower().startswith("image/"):
                    time.sleep(0.3)
                    self._cdn_window_handle = new_handle
                    keep_window = True
                    self.logger.info(
                        "CDN warmup ok (contentType=%s); keeping the "
                        "window open for browser-routed downloads",
                        last_ct,
                    )
                    return True
                time.sleep(0.5)

            self.logger.warning(
                "CDN warmup did not reach an image response within %.0fs "
                "(last document.contentType=%r). The browser itself "
                "could not pass Cloudflare for %s; try a different "
                "VPN exit node or disable the proxy.",
                warmup_timeout,
                last_ct,
                url,
            )
            self._cdn_warmed_failed = True
            return False
        except Exception as e:
            self.logger.warning("CDN warmup failed for %s: %s", url, e)
            return False
        finally:
            if new_handle is not None and not keep_window:
                try:
                    self.driver.switch_to.window(new_handle)
                    self.driver.close()
                except Exception:
                    pass
            if original_handle is not None:
                try:
                    self.driver.switch_to.window(original_handle)
                except Exception:
                    pass

    _CDN_FETCH_JS = """
const targetUrl = arguments[0];
const done = arguments[arguments.length - 1];
(async () => {
    try {
        const resp = await fetch(targetUrl, {
            credentials: 'include',
            cache: 'default',
            referrerPolicy: 'strict-origin-when-cross-origin'
        });
        const status = resp.status;
        if (!resp.ok) {
            done({status: status, ok: false, error: 'http_' + status});
            return;
        }
        const buf = await resp.arrayBuffer();
        const bytes = new Uint8Array(buf);
        const chunk = 32768;
        let bin = '';
        for (let i = 0; i < bytes.length; i += chunk) {
            bin += String.fromCharCode.apply(
                null, bytes.subarray(i, i + chunk)
            );
        }
        done({status: status, ok: true, data: btoa(bin)});
    } catch (e) {
        done({status: 0, ok: false, error: String(e)});
    }
})();
"""

    def browser_fetch(self, url: str) -> tuple[int, bytes] | None:
        """Fetch ``url`` via ``fetch()`` inside the parked cdn.v2ph.com
        window. Same-origin request, so Cloudflare cannot tell our fetch
        apart from a regular in-page image load.

        Synchronous - the caller must run it in an executor and
        serialize browser access (the asyncio.Lock in ImageScraper).
        """
        if self._cdn_window_handle is None:
            return None

        original_handle: str | None = None
        try:
            try:
                original_handle = self.driver.current_window_handle
            except Exception:
                original_handle = None
            try:
                self.driver.switch_to.window(self._cdn_window_handle)
            except Exception as e:
                self.logger.debug(
                    "Lost CDN window handle (%s); will give up on browser_fetch",
                    e,
                )
                self._cdn_window_handle = None
                return None

            # ``execute_async_script`` requires a script timeout; size it
            # generously since CDN response + base64 encode can take a
            # few seconds for big images.
            self.driver.set_script_timeout(60)
            result = self.driver.execute_async_script(self._CDN_FETCH_JS, url)
        except Exception as e:
            self.logger.debug("browser_fetch failed for %s: %s", url, e)
            return None
        finally:
            if original_handle is not None:
                try:
                    self.driver.switch_to.window(original_handle)
                except Exception:
                    pass

        if not isinstance(result, dict):
            return None

        status_code = int(result.get("status", 0) or 0)
        if result.get("ok") and "data" in result:
            try:
                body = base64.b64decode(result["data"])
            except Exception as e:
                self.logger.debug("base64 decode failed for %s: %s", url, e)
                return (status_code, b"")
            return (status_code, body)

        err = result.get("error")
        if err:
            self.logger.debug("browser_fetch JS error for %s: %s", url, err)
        return (status_code, b"")

    async def auto_page_scroll(
        self,
        url: str,
        max_retry: int = 3,
        page_sleep: int = 5,
    ) -> str:
        response: str = ""
        self.url = url

        for attempt in range(max_retry):
            try:
                self.driver.get(url)
                SelBehavior.random_sleep(0.1, 0.5)

                if not self.handle_redirection_fail(url, max_retry, page_sleep):
                    self.logger.error(
                        "Reconnection fail for URL %s. Please check your network status.",
                        url,
                    )
                    break

                if self.cloudflare.handle_simple_block(attempt, max_retry):
                    continue

                # main business
                self.handle_login()
                self.handle_read_limit()
                self.handle_image_captcha()
                if self._is_image_captcha_page():
                    self.logger.warning(
                        "Captcha still blocking %s after solve handler - "
                        "retrying page load",
                        url,
                    )
                    continue
                self.driver.execute_script("document.body.style.zoom='50%'")
                await self.scroller.scroll_to_bottom()
                SelBehavior.random_sleep(5, 15)

                response = self.driver.page_source
                break

            except Exception as e:
                self.logger.exception(
                    "Request failed for URL %s - Attempt %d/%d. Error: %s",
                    url,
                    attempt + 1,
                    max_retry,
                    e,
                )

            self.logger.debug("Scrolling finished, pausing to avoid blocking")
            SelBehavior.random_sleep(page_sleep, page_sleep + 5)

        if not response:
            error_template = "Failed to retrieve URL after {} attempts: '{}'"
            error_msg = error_template.format(max_retry, url)
            self.logger.error(error_msg)
            return error_msg
        return response

    def handle_redirection_fail(self, url: str, max_retry: int, sleep_time: int) -> bool:
        if self.driver.current_url == url:
            return True
        retry = 1
        while retry <= max_retry:
            self.logger.warning("Connection failed - Attempt %d/%d", retry, max_retry)
            SelBehavior.random_sleep(sleep_time, sleep_time + 5 * random.uniform(1, retry * 5))

            if self.cloudflare.handle_simple_block(retry, max_retry):
                self.logger.warning("Failed to solve Cloudflare turnstile challenge")
                continue

            self.driver.get(url)
            retry += 1
            if self.driver.current_url == url:
                return True

        return self.driver.current_url == url

    def handle_login(self) -> bool:
        success = False
        if self.driver.find_elements(By.XPATH, "//h1[contains(@class, 'login-box-msg')]"):
            self.logger.info("Login page detected - Starting login process")
            try:
                accounts = self.account_manager.get_all_accounts()
                for _ in accounts:
                    # if no any available account, `AccountManager.random_pick` will execute sys.exit
                    self.account = self.account_manager.random_pick()

                    # this will update cookies_valid
                    if self.cookies_login():
                        return True

                    email_field = self.driver.find_element(By.ID, "email")
                    password_field = self.driver.find_element(By.ID, "password")
                    BaseBehavior.random_sleep(0.5, 1)

                    password_field.send_keys(Keys.SHIFT, Keys.TAB)  # replace alt+A
                    BaseBehavior.random_sleep(0.5, 1)
                    SelBehavior.human_like_type(email_field, self.account)
                    SelBehavior.random_sleep(0.01, 0.3)

                    email_field.send_keys(Keys.TAB)
                    SelBehavior.human_like_type(
                        password_field, self.account_manager.get_pw(self.account, self.private_key)
                    )
                    SelBehavior.random_sleep(0.01, 0.5)

                    try:
                        self.cloudflare.handle_cloudflare_recaptcha()
                    except Exception as e:
                        self.logger.exception("Error handling Cloudflare reCAPTCHA: %s", e)

                    self.driver.find_element(
                        By.XPATH,
                        '//button[@type="submit" and @class="btn btn-primary"]',
                    ).click()

                    SelBehavior.random_sleep(3, 5)

                    if not self.driver.find_elements(
                        By.XPATH,
                        "//h1[@class='h4 text-secondary mb-4 login-box-msg']",
                    ):
                        success = True
                        self.logger.info("Login successful")
                        return success
                    else:
                        self.logger.info("Login failed - Checking for error messages")
                        self.account_manager.update_runtime_state(
                            self.account,
                            "password_valid",
                            False,
                        )
                        self.check_login_errors()
                        return success

            except NoSuchElementException as e:
                self.logger.error("Login form element not found: %s", e)
                raise
            except TimeoutException as e:
                self.logger.error("Timeout waiting for element: %s", e)
                raise
            except Exception as e:
                self.logger.error("Unexpected error during login: %s", e)
                raise
        else:
            return True
        if not success:
            self.logger.info("Automated login failed. Please login yourself.")
            sys.exit("Automated login failed.")
        return False

    # See ``DrissionBot._CAPTCHA_DETECT_JS`` for rationale. Kept in sync
    # with the DrissionPage variant: primary signal is the unique
    # ``#album-captcha-form`` / ``#captcha-image`` / ``#captcha_code``
    # IDs that v2ph reuses across both the legacy inline form and the
    # newer full-page card, with class fragments and localised heading
    # text in all 10 languages from the bottom switcher as fallbacks.
    _CAPTCHA_DETECT_JS = """
return (function () {
    try {
        if (document.querySelector(
            '#album-captcha-form, #captcha-image, #captcha_code, '
            + '[class*="captcha-container"], [class*="captcha-box"], '
            + 'form[action*="captcha"], img[src*="captcha"]'
        )) return true;
    } catch (e) {}
    var bodyText = '';
    try { bodyText = (document.body && document.body.innerText) || ''; } catch (e) {}
    var hints = [
        'Please complete the verification',
        'captcha verification',
        '\u8bf7\u5b8c\u6210\u9a8c\u8bc1',
        '\u9a8c\u8bc1\u7801\u9a8c\u8bc1',
        '\u8acb\u5b8c\u6210\u9a57\u8b49',
        '\u9a57\u8b49\u78bc\u9a57\u8b49',
        '\u8a8d\u8a3c\u3092\u5b8c\u4e86',
        '\u30ad\u30e3\u30d7\u30c1\u30e3\u8a8d\u8a3c',
        '\uc778\uc99d\uc744 \uc644\ub8cc',
        '\uce90\ud2b8\ucc28 \uc778\uc99d',
        'completa la verificaci\u00f3n',
        'completa el captcha',
        'compl\u00e9ter la v\u00e9rification',
        'compl\u00e9ter le captcha',
        'schlie\u00dfen Sie die Verifizierung',
        'Captcha-Verifizierung',
        '\u043f\u0440\u043e\u0439\u0434\u0438\u0442\u0435 \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0443',
        '\u043a\u0430\u043f\u0447',
        '\u0625\u0643\u0645\u0627\u0644 \u0627\u0644\u062a\u062d\u0642\u0642',
    ];
    for (var i = 0; i < hints.length; i++) {
        if (bodyText.indexOf(hints[i]) !== -1) return true;
    }
    return false;
})();
"""

    _ALBUM_PHOTO_DETECT_JS = """
return (function () {
    try {
        return !!document.querySelector(
            'div.album-photo img[src^="http"], '
            + 'div.album-photo-small img[src^="http"]'
        );
    } catch (e) { return false; }
})();
"""

    def _is_image_captcha_page(self) -> bool:
        """Return True when the current page is v2ph's image-captcha
        interception, regardless of which layout is served.
        """
        try:
            result = self.driver.execute_script(self._CAPTCHA_DETECT_JS)
        except Exception:
            return False
        return bool(result)

    def _has_album_photo_content(self) -> bool:
        """Return True when album thumbnail images are on screen."""
        try:
            result = self.driver.execute_script(self._ALBUM_PHOTO_DETECT_JS)
        except Exception:
            return False
        return bool(result)

    def _is_captcha_solved(self) -> bool:
        """Return True only when captcha UI is gone and album photos show."""
        if self._is_image_captcha_page():
            return False
        return self._has_album_photo_content()

    # See ``DrissionBot._CAPTCHA_AUTO_ATTEMPTS`` for rationale.
    _CAPTCHA_AUTO_ATTEMPTS = 50
    _CAPTCHA_SUBMIT_WAIT = 6.0
    _CAPTCHA_REFRESH_WAIT = 5.0
    _CAPTCHA_PRE_OCR_REFRESH_COUNT = 3

    def _get_ocr(self) -> Any:
        """Return a lazily-initialised ``ddddocr.DdddOcr`` instance, or
        ``None`` if ddddocr isn't installed or initialisation
        previously failed.
        """
        if self._ocr_init_failed or not _HAS_DDDDOCR or ddddocr is None:
            return None
        if self._ocr is not None:
            return self._ocr
        try:
            self._ocr = ddddocr.DdddOcr(show_ad=False)
        except Exception as e:
            self.logger.warning(
                "ddddocr failed to initialise (%s) - skipping captcha "
                "auto-solve and falling back to manual input",
                e,
            )
            self._ocr_init_failed = True
            return None
        return self._ocr

    def _read_captcha_image_bytes(self) -> bytes | None:
        """Pull the bytes of the current captcha image out of the
        ``<img id="captcha-image" src="data:image/png;base64,...">``
        tag. Returns ``None`` if the element / src is missing or the
        base64 decode fails.
        """
        try:
            src = self.driver.execute_script(
                "var el = document.getElementById('captcha-image'); "
                "return el ? el.src : '';"
            )
        except Exception as e:
            self.logger.debug("captcha image src read failed: %s", e)
            return None
        if not isinstance(src, str) or "," not in src:
            return None
        _, _, b64 = src.partition(",")
        try:
            return base64.b64decode(b64)
        except Exception as e:
            self.logger.debug("captcha image base64 decode failed: %s", e)
            return None

    def _refresh_captcha_image(self) -> None:
        """Reload the page to fetch a fresh captcha challenge.

        See :py:meth:`DrissionBot._refresh_captcha_image` for why we
        no longer simulate a click on ``#captcha-image``.
        """
        try:
            old_src = self.driver.execute_script(
                "var el = document.getElementById('captcha-image'); "
                "return el ? el.src : '';"
            )
        except Exception:
            old_src = ""

        try:
            self.driver.refresh()
        except Exception as e:
            self.logger.debug("captcha image refresh failed: %s", e)
            return

        deadline = time.time() + self._CAPTCHA_REFRESH_WAIT
        while time.time() < deadline:
            time.sleep(0.3)
            try:
                if self._is_captcha_solved():
                    return
                if not self._is_image_captcha_page():
                    continue
                new_src = self.driver.execute_script(
                    "var el = document.getElementById('captcha-image'); "
                    "return el ? el.src : '';"
                )
                if isinstance(new_src, str) and new_src and new_src != old_src:
                    return
            except Exception:
                continue

    def _refresh_captcha_before_ocr(self, attempt: int) -> None:
        """Reload the captcha page several times before OCR."""
        count = self._CAPTCHA_PRE_OCR_REFRESH_COUNT
        if count <= 0:
            return
        self.logger.info(
            "Captcha auto-solve attempt %d/%d: refreshing %d time(s) "
            "before OCR",
            attempt, self._CAPTCHA_AUTO_ATTEMPTS, count,
        )
        for _ in range(count):
            self._refresh_captcha_image()

    def _try_auto_solve_captcha_once(self, attempt: int) -> bool:
        """Run a single OCR-and-submit cycle. Returns True iff the
        captcha page is no longer present after the attempt.
        """
        ocr = self._get_ocr()
        if ocr is None:
            return False

        self._refresh_captcha_before_ocr(attempt)

        img_bytes = self._read_captcha_image_bytes()
        if not img_bytes:
            return False

        try:
            prediction = ocr.classification(img_bytes)
        except Exception as e:
            self.logger.debug("ddddocr inference failed: %s", e)
            return False

        text = prediction.strip() if isinstance(prediction, str) else ""
        # v2ph's captcha is always 4 ASCII alphanumeric characters.
        # If OCR returns punctuation / whitespace / non-ASCII it's
        # garbage; refresh and skip the submit.
        if (
            not text
            or len(text) < 3
            or len(text) > 8
            or not text.isascii()
            or not text.isalnum()
        ):
            self.logger.info(
                "Captcha auto-solve attempt %d/%d: OCR returned %r, "
                "refreshing image",
                attempt, self._CAPTCHA_AUTO_ATTEMPTS, text,
            )
            self._refresh_captcha_image()
            return False

        self.logger.info(
            "Captcha auto-solve attempt %d/%d: submitting OCR prediction %r",
            attempt, self._CAPTCHA_AUTO_ATTEMPTS, text,
        )

        try:
            input_field = self.driver.find_element(By.ID, "captcha_code")
            submit_btn = self.driver.find_element(By.ID, "submit")
        except NoSuchElementException:
            self.logger.debug("Captcha input or submit element not found")
            return False

        try:
            input_field.clear()
            SelBehavior.human_like_type(input_field, text)
            time.sleep(random.uniform(0.2, 0.6))
            submit_btn.click()
        except Exception as e:
            self.logger.debug("Captcha submit failed: %s", e)
            return False

        deadline = time.time() + self._CAPTCHA_SUBMIT_WAIT
        while time.time() < deadline:
            time.sleep(0.4)
            try:
                if self._is_captcha_solved():
                    return True
            except Exception:
                pass
        return False

    def handle_image_captcha(self) -> bool:
        """Detect v2ph's image captcha and clear it.

        Strategy mirrors :py:meth:`DrissionBot.handle_image_captcha`:
        try ddddocr-based auto-solve up to
        :py:attr:`_CAPTCHA_AUTO_ATTEMPTS` times, then fall back to the
        manual-input wait loop. v2ph reuses the same
        ``#album-captcha-form`` markup for both the legacy inline
        layout and the newer full-page card, so the auto-solver works
        against either unchanged.
        """
        if not self._is_image_captcha_page():
            self.logger.debug("No image captcha detected")
            return True

        if _HAS_DDDDOCR:
            self.logger.info(
                "Image captcha detected - attempting auto-solve via "
                "ddddocr (up to %d attempts)",
                self._CAPTCHA_AUTO_ATTEMPTS,
            )
            for attempt in range(1, self._CAPTCHA_AUTO_ATTEMPTS + 1):
                try:
                    if self._try_auto_solve_captcha_once(attempt):
                        self.logger.info(
                            "Captcha auto-solved on attempt %d - "
                            "continuing process",
                            attempt,
                        )
                        return True
                except Exception as e:
                    self.logger.debug(
                        "Captcha auto-solve attempt %d errored: %s",
                        attempt, e,
                    )
                try:
                    if self._is_captcha_solved():
                        self.logger.info(
                            "Captcha cleared on attempt %d - "
                            "continuing process",
                            attempt,
                        )
                        return True
                except Exception as e:
                    self.logger.debug(
                        "Captcha solve check failed on attempt %d: %s",
                        attempt, e,
                    )
                if attempt < self._CAPTCHA_AUTO_ATTEMPTS:
                    self.logger.info(
                        "Captcha auto-solve attempt %d/%d did not clear "
                        "the gate - retrying",
                        attempt, self._CAPTCHA_AUTO_ATTEMPTS,
                    )

            self.logger.warning(
                "Captcha auto-solve gave up after %d attempts - please "
                "solve the captcha in the opened browser (use F5 to "
                "refresh the image if needed). The download will resume "
                "automatically once verified.",
                self._CAPTCHA_AUTO_ATTEMPTS,
            )
        else:
            self.logger.warning(
                "Image captcha detected - please solve the captcha in "
                "the opened browser (use F5 to refresh the image if "
                "needed). (Install `ddddocr` for automatic solving: "
                "`pip install v2dl[ocr]`.) The download will resume "
                "automatically once verified."
            )

        while True:
            try:
                if self._is_captcha_solved():
                    self.logger.info("Captcha completed - continuing process")
                    return True
            except Exception as e:
                self.logger.debug("Captcha solve check failed while waiting: %s", e)
            time.sleep(random.uniform(1.0, 2.0))

    def cookies_login(self) -> bool:
        account_info = self.account_manager.read(self.account)
        if account_info is None:
            raise BotError("Unexpected error while reading account '%s'", account_info)

        cookies_path = account_info.get("cookies")
        if cookies_path:
            cookies = load_cookies(cookies_path)
            self.driver.delete_all_cookies()
            for k, v in cookies.items():
                self.driver.add_cookie({"name": k, "value": v})
            SelBehavior.random_sleep(0, 3)
            self.driver.refresh()
            SelBehavior.random_sleep(0, 3)
            self.driver.get(self.url)

        if not self.driver.find_element('//a[@href="/site/recovery-password"]'):
            self.logger.info("Account %s login successful with cookies", self.account)
            return True

        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        self.account_manager.update_runtime_state(self.account, "cookies_valid", False)
        self.account_manager.update_account(self.account, "exceed_time", now)
        return False

    def check_login_errors(self) -> None:
        error_messages = self.driver.find_elements(By.CLASS_NAME, "errorMessage")
        if error_messages:
            for message in error_messages:
                self.logger.error("Login error: %s", message.text)
        else:
            self.logger.error("Login failed for unknown reasons")
            sys.exit(1)

    def handle_read_limit(self) -> None:
        if self.check_read_limit():
            # click logout
            logout_button = self.driver.find_element(
                By.XPATH,
                '//ul[@class="nav justify-content-end"]//a[contains(@href, "/user/logout")]',
            )
            logout_button.click()
            now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            self.account_manager.update_runtime_state(self.account, "exceed_quota", True)
            self.account_manager.update_account(self.account, "exceed_quota", True)
            self.account_manager.update_account(self.account, "exceed_time", now)
            self.account = self.account_manager.random_pick()

    def check_read_limit(self) -> bool:
        return "https://www.v2ph.com/user/upgrade" in self.driver.current_url


class SelCloudflareHandler:
    """Handles Cloudflare protection detection and bypass attempts.

    Includes methods for dealing with various Cloudflare challenges.
    """

    def __init__(self, driver: WebDriver, logger: Logger):
        self.driver = driver
        self.logger = logger

    def handle_simple_block(self, attempt: int, retries: int) -> bool:
        """check and handle Cloudflare challenge."""
        blocked = False
        if self.is_simple_blocked():
            self.logger.info(
                "Detected Cloudflare challenge, attempting to solve... Attempt %d/%d",
                attempt + 1,
                retries,
            )
            self.handle_cloudflare_turnstile()
            blocked = True
        return blocked

    def handle_hard_block(self) -> bool:
        """Check whether blocked or not.

        This is a cloudflare WAF full page block.
        """
        blocked = False
        if self.is_hard_block():
            self.logger.warning("Hard block detected by Cloudflare - Unable to proceed")
            blocked = True
        return blocked

    def is_simple_blocked(self) -> bool:
        try:
            title = self.driver.title or ""
        except Exception:
            title = ""
        try:
            html = self.driver.page_source or ""
        except Exception:
            html = ""
        return cf_simple_blocked(title, html)

    def is_hard_block(self) -> bool:
        try:
            title = self.driver.title or ""
        except Exception:
            title = ""
        is_blocked = cf_hard_blocked(title)
        if is_blocked:
            self.logger.warning("Cloudflare hard block detected")
        return is_blocked

    def handle_cloudflare_turnstile(self) -> None:
        """Try to solve the Turnstile interstitial; fall back to
        waiting for a human if the automated path fails.

        Cloudflare's checkbox sits inside a cross-origin iframe; the
        long-standing iframe-switch + click trick still works on most
        builds, but when CF mutates the DOM faster than we can switch
        we just yield to the user and poll for the page to clear.
        """
        auto_solved = False
        try:
            iframe = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((
                    By.XPATH,
                    "//iframe[contains(@src, 'challenges.cloudflare.com')]",
                )),
            )
            self.driver.switch_to.frame(iframe)

            checkbox = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.ID, "cf-turnstile-response")),
            )
            SelBehavior.human_like_click(self.driver, checkbox)
            auto_solved = True

            if "Select all squares with" in self.driver.page_source:
                self.solve_image_captcha()
        except (TimeoutException, NoSuchElementException):
            self.logger.warning(
                "Automated Cloudflare turnstile click failed - "
                "falling back to manual solve"
            )
        except NotImplementedError:
            self.logger.warning(
                "Cloudflare image captcha requires manual solving"
            )
        finally:
            try:
                self.driver.switch_to.default_content()
            except Exception:
                pass

        self._wait_for_clear(timeout=120.0, log_manual_hint=not auto_solved)

    def _wait_for_clear(self, timeout: float = 120.0, log_manual_hint: bool = False) -> bool:
        """Poll until the Cloudflare interstitial is no longer present
        or until ``timeout`` elapses; lets a human solve the captcha
        in the opened browser window."""
        if log_manual_hint:
            self.logger.warning(
                "Cloudflare interstitial is still on screen - please "
                "click the captcha in the browser within %.0fs. The "
                "download will resume automatically once verified.",
                timeout,
            )
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.is_simple_blocked():
                self.logger.info("Cloudflare interstitial cleared")
                return True
            time.sleep(1.0)

        self.logger.warning(
            "Cloudflare interstitial did not clear within %.0fs - "
            "the outer retry loop will re-fetch the URL.",
            timeout,
        )
        return False

    def handle_cloudflare_recaptcha(self) -> None:
        try:
            recaptcha_checkbox = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "//input[@type='checkbox']")),
            )
            SelBehavior.human_like_click(self.driver, recaptcha_checkbox)

            verify_button = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "//span[contains(text(), '驗證您是人類')]")),
            )
            SelBehavior.human_like_click(self.driver, verify_button)

            SelBehavior.random_sleep(3, 5)
        except (TimeoutException, NoSuchElementException) as e:
            self.logger.warning(
                "reCAPTCHA checkbox or verify button not found or unable to interact: %s",
                e,
            )

    def solve_image_captcha(self) -> None:
        raise NotImplementedError


class SelBehavior(BaseBehavior):
    @staticmethod
    def human_like_mouse_movement(driver: Any, element: Any) -> None:
        action = ActionChains(driver)
        action.move_by_offset(random.randint(-100, 100), random.randint(-100, 100))
        action.move_to_element_with_offset(
            element,
            random.randint(-10, 10),
            random.randint(-10, 10),
        )
        action.pause(random.uniform(0.1, 0.3))
        action.move_to_element(element)
        action.perform()
        SelBehavior.random_sleep(*BaseBehavior.pause_time)

    @staticmethod
    def human_like_click(driver: Any, element: Any) -> None:
        SelBehavior.human_like_mouse_movement(driver, element)
        action = ActionChains(driver)
        action.click()
        action.perform()
        SelBehavior.random_sleep(*BaseBehavior.pause_time)

    @staticmethod
    def human_like_type(element: Any, text: str) -> None:
        for char in text:
            element.send_keys(char)
            time.sleep(random.uniform(0.001, 0.2))
        SelBehavior.random_sleep(*BaseBehavior.pause_time)


class SelScroll(BaseScroll):
    def __init__(self, driver: WebDriver, config: "Config", logger: Logger):
        super().__init__(config, logger)
        self.driver = driver

    async def scroll_to_bottom(self) -> None:
        max_attempts = 10
        attempts = 0
        last_position = -123459
        wait_time = (1, 2)
        scroll_length = lambda: random.randint(
            self.config.static_config.min_scroll_distance,
            self.config.static_config.max_scroll_distance,
        )

        while attempts < max_attempts:
            scroll = scroll_length()
            self.logger.debug(
                "Current position: %d, scrolling down by %d pixels",
                last_position,
                scroll,
            )
            self.driver.execute_script(f"window.scrollBy({{top: {scroll}, behavior: 'smooth'}});")
            new_position = self.driver.execute_script("return window.pageYOffset;")
            await asyncio.sleep(random.uniform(*wait_time))
            if new_position == last_position:
                break
            last_position = new_position
            attempts += 1

    def old_scroll_to_bottom(self) -> None:
        self.logger.info("Start scrolling the page")
        scroll_attempts = 0
        max_attempts = 45

        scroll_pos_init = self.driver.execute_script("return window.pageYOffset;")
        step_scroll = lambda: random.randint(
            self.config.static_config.min_scroll_distance,
            self.config.static_config.max_scroll_distance,
        )

        while scroll_attempts < max_attempts:
            scroll_attempts += 1

            self.driver.execute_script(f"window.scrollBy(0, {step_scroll});")
            scroll_pos_end = self.driver.execute_script("return window.pageYOffset;")
            time.sleep(0.75)

            if scroll_pos_init >= scroll_pos_end:
                self.logger.debug("Reached the bottom of the page")
                break

            scroll_pos_init = scroll_pos_end

            step_scroll = lambda: random.randint(
                self.config.static_config.min_scroll_distance,
                self.config.static_config.max_scroll_distance,
            )

            self.wait_for_content_load()

            self.successive_scroll_count += 1
            if self.successive_scroll_count >= self.max_successive_scrolls:
                pause_time = random.uniform(3, 7)
                self.logger.debug(
                    "Scrolled %d times, pausing for %.2f seconds",
                    self.successive_scroll_count,
                    pause_time,
                )
                time.sleep(pause_time)
                self.successive_scroll_count = 0
                self.max_successive_scrolls = random.randint(3, 7)

        if scroll_attempts == max_attempts:
            self.logger.info(
                "Reached maximum attempts (%d), scrolling finished, may not have fully reached the bottom",
                max_attempts,
            )
        else:
            self.logger.info("Page scroll completed")

    def perform_scroll_action(self) -> None:
        action = random.choices(
            ["scroll_down", "scroll_up", "pause", "jump"],
            weights=[0.7, 0.1, 0.1, 0.1],
        )[0]

        current_position = self.get_scroll_position()

        if action == "scroll_down":
            scroll_length = random.randint(
                self.config.static_config.min_scroll_distance,
                self.config.static_config.max_scroll_distance,
            )
            target_position = current_position + scroll_length
            self.logger.debug("Trying to scroll down %d pixels", scroll_length)
            actual_position = self.safe_scroll(target_position)
            self.logger.debug("Actually scrolled to %d pixels", actual_position)
            time.sleep(random.uniform(*BaseBehavior.pause_time))
        elif action == "scroll_up":
            scroll_length = random.randint(
                self.config.static_config.min_scroll_distance,
                self.config.static_config.max_scroll_distance,
            )
            target_position = max(0, current_position - scroll_length)
            self.logger.debug("Trying to scroll up %d pixels", scroll_length)
            actual_position = self.safe_scroll(target_position)
            self.logger.debug("Actually scrolled to %d pixels", actual_position)
        elif action == "pause":
            pause_time = random.uniform(1, 3)
            self.logger.debug("Pausing for %.2f seconds", pause_time)
            time.sleep(pause_time)
        elif action == "jump":
            jump_position = current_position + random.randint(100, 500)
            self.logger.debug("Trying to jump to position %d", jump_position)
            actual_position = self.safe_scroll(jump_position)
            self.logger.debug("Actually jumped to %d", actual_position)

    def safe_scroll(self, target_position: float) -> float:
        current_position = self.get_scroll_position()
        step = random.randint(
            self.config.static_config.min_scroll_step,
            self.config.static_config.max_scroll_step,
        )
        # step = 50 if target_position > current_position else -50
        # while abs(current_position - target_position) > abs(step):

        while abs(current_position - target_position) > step:
            self.driver.execute_script(f"window.scrollTo(0, {current_position + step});")
            time.sleep(random.uniform(0.01, 0.2))
            new_position = self.get_scroll_position()
            if new_position == current_position:
                self.logger.debug(
                    "Cannot continue scrolling, target: %d, current: %d",
                    target_position,
                    current_position,
                )
                break
            current_position = new_position
        self.driver.execute_script(f"window.scrollTo(0, {target_position});")
        return self.get_scroll_position()

    def get_scroll_position(self) -> float:
        return self.driver.execute_script("return window.pageYOffset")

    def get_page_height(self) -> float:
        return self.driver.execute_script("return document.body.scrollHeight")

    def wait_for_content_load(self) -> None:
        try:
            WebDriverWait(self.driver, 5).until(
                lambda d: d.execute_script("return document.readyState") == "complete",
            )
        except TimeoutException:
            self.logger.warning("Timeout waiting for new content to load")
