import sys
import time
import base64
import random
import asyncio
from datetime import datetime
from logging import Logger
from typing import TYPE_CHECKING, Any

from DrissionPage import ChromiumOptions, ChromiumPage
from DrissionPage.common import wait_until
from DrissionPage.errors import ContextLostError, ElementNotFoundError, WaitTimeoutError

# Optional OS-level mouse driver used to defeat Cloudflare Turnstile's
# ``isTrusted`` check. CDP/JS-injected clicks set ``event.isTrusted``
# to ``false`` and CF rejects them; pyautogui synthesises Windows
# ``SendInput`` events which the kernel marks trusted, so CF cannot
# distinguish them from a real user. Install with ``pip install
# v2dl[bypass]`` or ``pip install pyautogui``.
try:
    import pyautogui  # type: ignore[import-not-found]

    _HAS_PYAUTOGUI = True
    # pyautogui's built-in 0.1s pause is multiplied across every call
    # (move, click, etc.) and ends up making the click look robotic.
    pyautogui.PAUSE = 0  # type: ignore[attr-defined]
    pyautogui.FAILSAFE = False  # type: ignore[attr-defined]
except Exception:
    pyautogui = None  # type: ignore[assignment]
    _HAS_PYAUTOGUI = False

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

from v2dl.common.const import BASE_URL
from v2dl.common.cookies import load_cookies
from v2dl.common.error import BotError
from v2dl.web_bot.base import (
    BaseBehavior,
    BaseBot,
    BaseScroll,
    cf_hard_blocked,
    cf_simple_blocked,
)

if TYPE_CHECKING:
    from v2dl.common import Config
    from v2dl.security import AccountManager, KeyManager


class DrissionBot(BaseBot):
    def __init__(
        self,
        config: "Config",
        key_manager: "KeyManager",
        account_manager: "AccountManager",
    ) -> None:
        super().__init__(config, key_manager, account_manager)
        self.config = config
        self.init_driver()
        self.cloudflare = DriCloudflareHandler(self.page, self.logger)
        # Persistent ChromiumTab parked on cdn.v2ph.com once it has
        # passed Cloudflare. Reused for browser-routed image downloads
        # via JS fetch (same-origin to the CDN, so the request looks
        # exactly like a normal in-page image load - no third-party /
        # cross-frame penalty from CF).
        self._cdn_tab: Any = None
        # Last URL the CDN tab was successfully warmed with. Stashed
        # so ``browser_fetch`` can transparently re-warm a dead tab
        # without losing the original origin context.
        self._cdn_warmup_url: str | None = None
        # One-shot flag for the "CDN tab run_js raised" warning so we
        # don't spam the log once Chromium has discarded the tab.
        self._cdn_runjs_warning_logged: bool = False
        # Lazily-initialised ddddocr instance and an init-failure
        # latch. We don't load the ONNX model until the first captcha
        # actually fires (~200 ms warm-up + a few MB of RAM).
        self._ocr: Any = None
        self._ocr_init_failed: bool = False

    def init_driver(self) -> None:
        co = ChromiumOptions()

        # --- Anti-fingerprinting baseline -----------------------------
        # DrissionPage 4.x ships ``--test-type`` in its default
        # ``ChromiumOptions``. That single flag is sufficient for
        # Cloudflare's bot-management orchestrate script to flag the
        # session as automation and refuse to clear the managed
        # challenge regardless of what we click. Strip it before any
        # user-supplied args are applied so the user can still
        # re-enable it explicitly via ``chrome_args`` if they need it.
        co.remove_argument("--test-type")

        # IMPORTANT: do NOT pass ``--disable-blink-features=
        # AutomationControlled`` here. From Chrome 130+, that flag is
        # on Chrome's "sensitive flag" deny-list and triggers a yellow
        # info-bar at the top of the window ("您使用的不支持的命令行标记...").
        # That info-bar (a) shrinks the viewport by ~37 CSS px - which
        # throws off every coordinate the OS-click code computes for
        # the Turnstile widget - and (b) is itself a strong fingerprint
        # that CF / Turnstile pick up via parent-frame measurement.
        # The ``navigator.webdriver`` property is overridden at the
        # JS level by ``_inject_stealth_scripts`` instead, which is
        # both undetectable from the page and immune to Chrome's flag
        # filtering.

        # Disable a few feature flags that bias Chrome toward
        # automation telemetry / experimental UI which CF heuristics
        # weigh negatively. Keep this list conservative; aggressive
        # ``--disable-features`` lists themselves can be a fingerprint.
        co.set_argument(
            "--disable-features",
            "Translate,OptimizationHints,PrivacySandboxSettings4",
        )

        args = self.parse_chrome_args()
        if len(args) > 0:
            for arg in args:
                co.set_argument(*arg)
                self.logger.info(f"Apply custom chrome args: {arg}")

        # Do NOT use preset user_agent for drissionpage
        if self.config.static_config.custom_user_agent:
            self.logger.info(
                f"Apply custom user agent: {self.config.static_config.custom_user_agent}"
            )
            co.set_user_agent(user_agent=self.config.static_config.custom_user_agent)

        if not self.config.static_config.use_default_chrome_profile:
            user_data_dir = self.prepare_chrome_profile()
            co.set_user_data_path(user_data_dir)
        else:
            co.use_system_user_path()

        self.page = ChromiumPage(addr_or_opts=co, timeout=0.8)  # type: ignore
        self.page.set.scroll.smooth(on_off=True)
        self.page.set.scroll.wait_complete(on_off=True)

        # Inject the stealth patches before any navigation runs so the
        # very first document the user-agent loads (incl. the CF
        # interstitial) sees a clean ``navigator.webdriver`` etc.
        self._inject_stealth_scripts()

        self.scroller = DriScroll(self.page, self.config, self.logger)

    def _inject_stealth_scripts(self) -> None:
        """Patch the most-fingerprinted ``navigator`` / ``window``
        surfaces *before* every document is created.

        Uses ``Page.addScriptToEvaluateOnNewDocument`` (CDP) which
        runs the script ahead of any page script, including
        Cloudflare's challenge orchestrate bundle. Without this:

        * ``navigator.webdriver`` is ``true`` whenever a CDP client
          (DrissionPage) is attached to Chromium - the loudest single
          bot signal there is.
        * ``navigator.plugins`` is empty, which CF cross-checks
          against the UA: a real desktop Chrome always reports a
          handful of built-in PDF viewer plugins.
        * ``window.chrome`` is missing some sub-objects that real
          Chrome exposes (``runtime``, ``app``, ``csi``,
          ``loadTimes``); CF's "browser sanity" checks look for them.

        Best-effort: a CDP failure is logged but does not raise so
        startup still succeeds on Chromium builds where this CDP
        method is not available.
        """
        script = r"""
        try {
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined,
                configurable: true,
            });
        } catch (e) {}
        try {
            if (!navigator.languages || navigator.languages.length === 0) {
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['zh-CN', 'zh', 'en'],
                    configurable: true,
                });
            }
        } catch (e) {}
        try {
            Object.defineProperty(navigator, 'plugins', {
                get: () => ([
                    {name: 'PDF Viewer', filename: 'internal-pdf-viewer'},
                    {name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer'},
                    {name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer'},
                    {name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer'},
                    {name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer'},
                ]),
                configurable: true,
            });
        } catch (e) {}
        try {
            if (typeof window.chrome === 'undefined') {
                window.chrome = {};
            }
            if (typeof window.chrome.runtime === 'undefined') {
                window.chrome.runtime = {};
            }
            if (typeof window.chrome.app === 'undefined') {
                window.chrome.app = {InstallState: {}, RunningState: {}, getDetails: function(){}};
            }
            if (typeof window.chrome.csi === 'undefined') {
                window.chrome.csi = function(){};
            }
            if (typeof window.chrome.loadTimes === 'undefined') {
                window.chrome.loadTimes = function(){};
            }
        } catch (e) {}
        """
        try:
            self.page.run_cdp(
                "Page.addScriptToEvaluateOnNewDocument",
                source=script,
            )
            self.logger.info("Stealth patches installed via CDP addScriptToEvaluateOnNewDocument")
        except Exception as e:
            self.logger.warning(
                "Failed to inject stealth script via CDP (%s); "
                "Cloudflare managed challenges may not auto-clear",
                e,
            )

    def close_driver(self) -> None:
        if self._cdn_tab is not None:
            try:
                self._cdn_tab.close()
            except Exception:
                pass
            self._cdn_tab = None
        self.page.quit()

    def get_cookies(self) -> dict[str, str]:
        """Snapshot the live cookies of the DrissionPage browser session.

        Cloudflare's ``cf_clearance`` / ``__cf_bm`` cookies are scoped per
        zone, so the ones obtained for www.v2ph.com do NOT clear requests
        to cdn.v2ph.com (a separate CF zone). We therefore need every
        cookie the browser knows about - DrissionPage's default
        ``page.cookies()`` only returns cookies for the current page's
        domain, which is exactly the bug that left curl-cffi without the
        CDN clearance cookies and produced 403 "Just a moment..." pages.
        """
        try:
            raw = self.page.cookies(all_domains=True)
        except TypeError:
            # Older DrissionPage builds expose ``all_urls`` instead of
            # ``all_domains``; fall back transparently rather than break.
            try:
                raw = self.page.cookies(all_urls=True)  # type: ignore[call-arg]
            except Exception as e:
                self.logger.warning("Failed to read browser cookies: %s", e)
                return {}
        except Exception as e:
            self.logger.warning("Failed to read browser cookies: %s", e)
            return {}

        cookies: dict[str, str] = {}
        if isinstance(raw, dict):
            for name, value in raw.items():
                if value is None:
                    continue
                cookies[str(name)] = str(value)
        else:
            # When multiple domains contribute a cookie with the same
            # ``name`` (e.g. CF sets ``__cf_bm`` on both v2ph.com and
            # cdn.v2ph.com), prefer the CDN value so the downloader gets
            # the clearance that actually matters for cdn.v2ph.com.
            preferred_domain = "cdn.v2ph.com"
            staged: dict[str, tuple[str, str]] = {}
            for item in raw or []:
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
            cookies = {k: v for k, (_, v) in staged.items()}
        return cookies

    def ensure_cdn_warmed(self, url: str) -> bool:
        """Open ``url`` in a background tab and wait for Cloudflare to
        clear. The tab is **kept open** afterwards so ``browser_fetch``
        can run ``fetch()`` inside it (same-origin to cdn.v2ph.com).

        ``page.wait.load_complete`` only waits for the *interstitial*
        HTML, not for the JS challenge to actually run. We instead poll
        ``document.contentType`` until it is an image MIME (CF served
        the real bytes) or until a timeout. If we time out the browser
        itself cannot pass CF for cdn.v2ph.com, so no client-side
        workaround will help and we log a loud warning.

        Idempotent across a session.
        """
        if self._cdn_tab is not None:
            return True
        if getattr(self, "_cdn_warmed_failed", False):
            return False
        if not url:
            return False

        new_tab = None
        keep_tab = False
        last_ct = ""
        warmup_timeout = 25.0
        try:
            new_tab = self.page.new_tab(url)
            deadline = time.time() + warmup_timeout
            while time.time() < deadline:
                try:
                    ct = new_tab.run_js("return document.contentType || ''")
                except Exception:
                    ct = ""
                last_ct = str(ct or "")
                if last_ct.lower().startswith("image/"):
                    # Tiny grace for any trailing Set-Cookie to commit.
                    time.sleep(0.3)
                    self._cdn_tab = new_tab
                    self._cdn_warmup_url = url
                    keep_tab = True
                    self.logger.info(
                        "CDN warmup ok (contentType=%s); keeping the tab "
                        "open for browser-routed downloads",
                        last_ct,
                    )
                    return True
                time.sleep(0.5)

            self.logger.warning(
                "CDN warmup did not reach an image response within %.0fs "
                "(last document.contentType=%r). The Chromium tab itself "
                "could not pass Cloudflare for %s. Try a different VPN "
                "exit node or disable the proxy, then re-run.",
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
            if new_tab is not None and not keep_tab:
                try:
                    new_tab.close()
                except Exception:
                    pass

    # JS executed inside the parked cdn.v2ph.com tab. Same-origin
    # to the URL, so CF treats the request as a normal in-page image
    # load and serves it without challenging. The body is returned as
    # base64 because CDP cannot ferry arbitrary binary back to Python.
    _CDN_FETCH_JS = """
const targetUrl = arguments[0];
return (async () => {
    try {
        const resp = await fetch(targetUrl, {
            credentials: 'include',
            cache: 'default',
            referrerPolicy: 'strict-origin-when-cross-origin',
        });
        const status = resp.status;
        if (!resp.ok) {
            return {status: status, ok: false, error: 'http_' + status};
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
        return {status: status, ok: true, data: btoa(bin)};
    } catch (e) {
        return {status: 0, ok: false, error: String(e)};
    }
})();
"""

    def _run_cdn_fetch_js(self, url: str) -> Any:
        """Run :pyattr:`_CDN_FETCH_JS` against the parked CDN tab,
        transparently re-warming the tab once if ``run_js`` raises
        (Chromium occasionally discards a long-idle background tab,
        and DrissionPage then surfaces a ``ContextLostError`` /
        ``WebSocketException`` on the next call).

        Returns the JS result dict, or ``None`` if the tab is dead and
        cannot be re-warmed.
        """
        if self._cdn_tab is None:
            return None

        try:
            return self._cdn_tab.run_js(self._CDN_FETCH_JS, url)
        except Exception as e:
            # First time we see this we want a *visible* signal; the
            # caller in ``core.py`` only logs a generic "no result"
            # error which obscures the real cause. After the first
            # warning we drop back to debug to avoid spamming the log
            # for every image until the re-warm succeeds.
            if not self._cdn_runjs_warning_logged:
                self.logger.warning(
                    "CDN tab run_js raised: %s. The parked "
                    "cdn.v2ph.com tab appears to have been discarded "
                    "by Chromium; attempting to re-warm it before "
                    "retrying the fetch.",
                    e,
                )
                self._cdn_runjs_warning_logged = True
            else:
                self.logger.debug(
                    "CDN tab run_js raised for %s: %s", url, e
                )

        # Re-warm path: discard the dead tab and bring up a fresh one
        # using the last known good warmup URL (or ``url`` as a
        # last-resort seed).
        rewarm_url = self._cdn_warmup_url or url
        try:
            if self._cdn_tab is not None:
                self._cdn_tab.close()
        except Exception:
            pass
        self._cdn_tab = None
        # Reset both latches so ``ensure_cdn_warmed`` retries cleanly
        # rather than short-circuiting on a stale "permanently
        # failed" state from a previous session.
        self._cdn_warmed_failed = False

        if not self.ensure_cdn_warmed(rewarm_url) or self._cdn_tab is None:
            return None

        # New tab acquired; reset the one-shot warning so a *future*
        # death (e.g. cf_bm rotation hours later) is also surfaced.
        self._cdn_runjs_warning_logged = False
        try:
            return self._cdn_tab.run_js(self._CDN_FETCH_JS, url)
        except Exception as e:
            self.logger.warning(
                "CDN tab run_js still failing after re-warm for %s: %s",
                url, e,
            )
            return None

    def browser_fetch(self, url: str) -> tuple[int, bytes] | None:
        """Fetch ``url`` via ``fetch()`` inside the parked cdn.v2ph.com
        tab.

        Why this works where curl-cffi (and CDP
        ``Network.loadNetworkResource``) don't: Cloudflare's bot
        management binds clearance to the original TLS / origin context.
        Executing ``fetch()`` from a tab that's *already on*
        cdn.v2ph.com makes the request first-party, same-origin and
        indistinguishable from a normal in-page image load.

        Synchronous - the caller must run it in an executor and
        serialize browser access (the asyncio.Lock in ImageScraper).

        If the parked tab dies between calls (Chromium occasionally
        evicts long-idle background tabs to reclaim memory) we
        transparently re-warm it once and retry; see
        :pymeth:`_run_cdn_fetch_js`.
        """
        if self._cdn_tab is None:
            return None

        result = self._run_cdn_fetch_js(url)

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
        self.url = url

        for attempt in range(max_retry):
            try:
                self.page.get(url)

                # handle page redirection fail
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
                self.page.run_js("document.body.style.zoom='50%'")
                await self.scroller.scroll_to_bottom()

                # Sleep to avoid Cloudflare blocking
                self.logger.debug("Scrolling finished, pausing to avoid blocking")
                # DriBehavior.random_sleep(page_sleep, page_sleep + 5)
                break

            except ContextLostError:
                self.handle_login()

            except Exception as e:
                self.logger.exception(
                    "Request failed for URL %s - Attempt %d/%d. Error: %s",
                    url,
                    attempt + 1,
                    max_retry,
                    e,
                )
                # DriBehavior.random_sleep(page_sleep, page_sleep + 5)

        if not self.page.html:
            error_template = "Failed to retrieve URL after {} attempts: '{}'"
            error_msg = error_template.format(max_retry, url)
            self.logger.error(error_msg)
            return error_msg
        elif self.simple_blockage_check(self.page.html):
            raise RuntimeError(
                f"Unexpected error: Base URL '{BASE_URL}' not found in the HTML result.\n"
                "This indicates the request was blocked by an anti-bot check. Suggested actions:\n"
                "  - Change the header using: v2dl <url> --user-agent '<custom user agent>'\n"
                "  - Run in a clean internet environment\n"
                "  - Turn off the VPN if enabled"
            )
        else:
            return self.page.html

    def handle_redirection_fail(self, url: str, max_retry: int, sleep_time: int) -> bool:
        # If read limit exceed, not a redirection fail.
        # If not exceed read limit, check url.
        if self.check_read_limit() or (self.page.url == url and self.page.states.is_alive):
            return True
        retry = 1
        while retry <= max_retry:
            self.logger.warning(
                "Redirection handle failed for URL %s - Attempt %d/%d.",
                url,
                retry,
                max_retry,
            )
            DriBehavior.random_sleep(sleep_time, sleep_time + 5 * random.uniform(1, retry * 5))

            if self.cloudflare.handle_simple_block(retry, max_retry):
                self.logger.warning("Failed to solve Cloudflare turnstile challenge")
                continue

            self.page.get(url)
            retry += 1
            if self.page.url == url and self.page.states.is_alive:
                return True

        return self.page.url == url and self.page.states.is_alive

    def handle_login(self) -> bool:
        """handle login and return the bool represents if login success or not"""
        success = False
        if self.page("xpath=//h1[contains(@class, 'login-box-msg')]"):
            DriBehavior.random_sleep()
            self.logger.info("Login page detected - Starting login process")
            try:
                accounts = self.account_manager.get_all_accounts()
                for _ in accounts:
                    # if no any available account, `AccountManager.random_pick` will execute sys.exit
                    self.account = self.account_manager.random_pick()

                    # this will update cookies_valid
                    if self.cookies_login():
                        return True

                    email_field = self.page("#email")
                    password_field = self.page("#password")

                    # self.handle_cloudflare_recaptcha()
                    email_field.clear(True)
                    password_field.clear(True)

                    DriBehavior.human_like_type(email_field, self.account)
                    DriBehavior.random_sleep(0.01, 0.3)
                    DriBehavior.human_like_type(
                        password_field, self.account_manager.get_pw(self.account, self.private_key)
                    )
                    DriBehavior.random_sleep(0.01, 0.5)

                    login_button = self.page(
                        'xpath=//button[@type="submit" and @class="btn btn-primary"]',
                    )
                    login_button.click()

                    if not self.page(
                        'xpath=//div[contains(@class, "alert-danger") and @role="alert"]',
                        timeout=0.5,
                    ):
                        success = True
                        self.logger.info("Account %s login successful with password", self.account)
                        return success
                    else:
                        self.logger.info(
                            "Account %s Login failed. Checking error messages",
                            self.account,
                        )
                        self.account_manager.update_runtime_state(
                            self.account,
                            "password_valid",
                            False,
                        )
                        self.check_login_errors()
                        return success

            except ElementNotFoundError as e:
                self.logger.error("Login form element not found: %s", e)
                raise
            except WaitTimeoutError as e:
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

    # Heuristic JS used to detect a v2ph image-captcha interception in
    # either the legacy inline form or the newer full-page "Please
    # complete the verification" card. v2ph re-uses the same
    # ``#album-captcha-form`` markup in both layouts (with the same
    # ``#captcha-image``/``#captcha_code`` IDs), so an ID match is
    # the cheapest and most specific signal. We additionally check
    # ``[class*="captcha-container"]`` (covers the outer
    # ``col-md-6 captcha-container card p-3`` div *and* the inner
    # wrapper around the image), and fall back to localised heading
    # text in all 10 languages exposed by the bottom switcher
    # (zh-Hans / zh-Hant / en / ja / ko / es / fr / ru / de / ar) for
    # the unlikely case the markup is overhauled. Any single match is
    # sufficient.
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

    def _is_image_captcha_page(self) -> bool:
        """Return True when the current page is v2ph's image-captcha
        interception, regardless of whether it uses the legacy
        ``col-md-6 captcha-container`` markup or the newer centred
        "Please complete the verification" card.
        """
        try:
            result = self.page.run_js(self._CAPTCHA_DETECT_JS)
        except Exception:
            return False
        return bool(result)

    # Number of automated solve attempts before falling back to the
    # manual-input wait loop. ddddocr clears v2ph's 4-character colour
    # noise captcha at ~85-90% per attempt on a single shot, and v2ph
    # rolls a fresh captcha after every failed POST, so 5 attempts
    # gives well over 99% cumulative success when ddddocr is installed.
    _CAPTCHA_AUTO_ATTEMPTS = 5

    # Per-attempt deadline for the page to navigate away from the
    # captcha after we click Submit. v2ph normally responds in <1 s
    # but slow networks / VPNs can push it past 3 s.
    _CAPTCHA_SUBMIT_WAIT = 6.0

    def _get_ocr(self) -> Any:
        """Return a lazily-initialised ``ddddocr.DdddOcr`` instance, or
        ``None`` if the dependency isn't installed or initialisation
        previously failed. The ONNX model load is ~200 ms + a few MB
        of RAM, so we don't pay it for albums that never trigger a
        captcha.
        """
        if self._ocr_init_failed or not _HAS_DDDDOCR or ddddocr is None:
            return None
        if self._ocr is not None:
            return self._ocr
        try:
            # ``show_ad=False`` suppresses ddddocr's startup banner.
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
            src = self.page.run_js(
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
        """Click the captcha image to trigger v2ph's built-in
        ``location.reload()`` handler so the next attempt sees a
        fresh challenge. Used when OCR returns garbage and we don't
        want to waste a Submit round-trip on it.
        """
        try:
            self.page.run_js(
                "var el = document.getElementById('captcha-image'); "
                "if (el) el.click();"
            )
        except Exception as e:
            self.logger.debug("captcha image refresh failed: %s", e)

    def _try_auto_solve_captcha_once(self, attempt: int) -> bool:
        """Run a single OCR-and-submit cycle. Returns True iff the
        captcha page is no longer present after the attempt.

        On garbage OCR output (wrong length / non-alphanumeric) we
        refresh the image instead of submitting, since v2ph's captcha
        is always 4 ASCII alphanumeric chars and submitting noise
        just burns a server-side rate-limit token.
        """
        ocr = self._get_ocr()
        if ocr is None:
            return False

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
        # If OCR produces punctuation, whitespace or non-ASCII glyphs
        # (e.g. it picked up a noise line as a Chinese radical) it's
        # garbage; refresh and skip the submit to save a server-side
        # rate-limit token.
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
            DriBehavior.random_sleep(0.5, 1.0)
            return False

        self.logger.info(
            "Captcha auto-solve attempt %d/%d: submitting OCR prediction %r",
            attempt, self._CAPTCHA_AUTO_ATTEMPTS, text,
        )

        input_field = self.page("#captcha_code")
        submit_btn = self.page("#submit")
        if not input_field or not submit_btn:
            self.logger.debug("Captcha input or submit element not found")
            return False

        try:
            input_field.clear(True)
            DriBehavior.human_like_type(input_field, text)
            DriBehavior.random_sleep(0.2, 0.6)
            submit_btn.click()
        except Exception as e:
            self.logger.debug("Captcha submit failed: %s", e)
            return False

        # Wait for the form POST to complete and the page to either
        # re-render the captcha (failure) or render the real album
        # (success). We poll the JS detector rather than wait for a
        # specific URL change because v2ph submits to the same album
        # URL in either case.
        deadline = time.time() + self._CAPTCHA_SUBMIT_WAIT
        while time.time() < deadline:
            time.sleep(0.4)
            try:
                if not self._is_image_captcha_page():
                    return True
            except Exception:
                return True
        return False

    def handle_image_captcha(self) -> bool:
        """Detect v2ph's image captcha and clear it.

        Strategy:

        1. If ``ddddocr`` is installed, run up to
           :pyattr:`_CAPTCHA_AUTO_ATTEMPTS` auto-solve cycles. Each
           cycle reads the inline ``data:image/png;base64,...`` from
           ``#captcha-image``, runs OCR, types the prediction into
           ``#captcha_code`` and clicks ``#submit``. v2ph re-rolls
           the captcha after every failed POST so subsequent attempts
           see a fresh challenge.
        2. If ddddocr isn't installed, or every auto-solve attempt
           failed, fall back to the manual-input wait loop: log a
           warning and poll the JS detector until the user solves the
           captcha by hand in the open Chromium window.

        The captcha currently ships in two flavours:

        * Legacy ``<div class="col-md-6 captcha-container card p-3">``
          inline form on the album page.
        * Newer full-page card with a localised heading ("Please
          complete the verification" / "请完成验证" / etc.) served as
          an interception page at the album URL.

        Both reuse the same ``#album-captcha-form`` markup so the
        auto-solver works against either layout unchanged.
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
                # Confirm we're still on the captcha page before
                # spending another attempt. If the user solved it
                # manually mid-loop, exit early.
                try:
                    if not self._is_image_captcha_page():
                        self.logger.info(
                            "Captcha cleared (likely solved manually) - "
                            "continuing process"
                        )
                        return True
                except Exception:
                    return True

            self.logger.warning(
                "Captcha auto-solve gave up after %d attempts - please "
                "solve the captcha in the opened browser. The download "
                "will resume automatically once verified.",
                self._CAPTCHA_AUTO_ATTEMPTS,
            )
        else:
            self.logger.warning(
                "Image captcha detected - please solve the captcha in "
                "the opened browser. (Install `ddddocr` for automatic "
                "solving: `pip install v2dl[ocr]`.) The download will "
                "resume automatically once verified."
            )

        while True:
            try:
                if not self._is_image_captcha_page():
                    self.logger.info("Captcha completed - continuing process")
                    return True
            except Exception:
                # Page likely navigated mid-check (form submit raced
                # with our JS read). Treat as solved and let the outer
                # loop re-check.
                self.logger.info("Captcha completed - continuing process")
                return True
            DriBehavior.random_sleep(1.0, 2.0)

    def cookies_login(self) -> bool:
        account_info = self.account_manager.read(self.account)
        if account_info is None:
            raise BotError("Unexpected error while reading account '%s'", account_info)

        cookies_path = account_info.get("cookies")
        if cookies_path:
            cookies = load_cookies(cookies_path)
            self.page.set.cookies.clear()
            self.page.set.cookies(cookies)
            DriBehavior.random_sleep(0, 3)
            self.page.refresh()
            DriBehavior.random_sleep(0, 3)
            self.page.get(self.url)

        if not self.page(
            'xpath=//div[contains(@class, "alert-danger") and @role="alert"]', timeout=0.5
        ):
            self.logger.info("Account %s login successful with cookies", self.account)
            return True

        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        self.account_manager.update_runtime_state(self.account, "cookies_valid", False)
        self.account_manager.update_account(self.account, "exceed_time", now)
        return False

    def check_login_errors(self) -> None:
        error_message = self.page('xpath=//div[@class="errorMessage"]')
        if error_message:
            self.logger.error("Login error: %s", error_message.text)
        else:
            self.logger.error("Login failed for unknown reasons")
            sys.exit(1)

    def handle_read_limit(self) -> None:
        if self.check_read_limit():
            # click logout
            self.page(
                'xpath=//ul[@class="nav justify-content-end"]//a[contains(@href, "/user/logout")]'
            ).click()
            now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            self.account_manager.update_runtime_state(self.account, "exceed_quota", True)
            self.account_manager.update_account(self.account, "exceed_quota", True)
            self.account_manager.update_account(self.account, "exceed_time", now)
            self.account = self.account_manager.random_pick()

    def check_read_limit(self) -> bool:
        return "https://www.v2ph.com/user/upgrade" in self.page.url

    def click_logout(self) -> None:
        self.page.ele("@href=/user/logout").click()

    def simple_blockage_check(self, html_content: str) -> bool:
        """Return True if the base URL is not in the HTML content, indicating blockage."""
        return BASE_URL not in html_content


class DriCloudflareHandler:
    """Handles Cloudflare protection detection and bypass attempts.

    Includes methods for dealing with various Cloudflare challenges.
    """

    def __init__(self, page: ChromiumPage, logger: Logger) -> None:
        self.page = page
        self.logger = logger

    def handle_simple_block(self, attempt: int, retries: int) -> bool:
        """Check, handle, and return whether blocked or not."""
        blocked = False
        if self.is_simple_blocked():
            self.logger.info(
                "Cloudflare challenge detected - Solve attempt %d/%d "
                "(OS-level click %s)",
                attempt + 1,
                retries,
                "ENABLED via pyautogui" if _HAS_PYAUTOGUI
                else "DISABLED - install `pyautogui` for reliable bypass",
            )
            blocked = self.handle_cloudflare_turnstile()
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
            title = self.page.title or ""
        except Exception:
            title = ""
        try:
            html = self.page.html or ""
        except Exception:
            html = ""
        return cf_simple_blocked(title, html)

    def is_hard_block(self) -> bool:
        try:
            title = self.page.title or ""
        except Exception:
            title = ""
        is_blocked = cf_hard_blocked(title)
        if is_blocked:
            self.logger.warning("Cloudflare hard block detected")
        return is_blocked

    # Candidate locators for the Turnstile widget. Ordered most -> least
    # specific. iframes give us the exact widget rect when present;
    # ``.cf-turnstile`` is the inner container CF sizes to fit the
    # widget exactly so the "left+30px" offset still lands on the
    # checkbox. The outer ``#challenge-stage`` / ``#turnstile-wrapper``
    # are deliberately last because they can be much wider than the
    # widget itself (full content column), which would push the offset
    # click into empty space.
    # Locators for *visible* Turnstile widget elements. Hidden inputs
    # (``<input type="hidden" id="cf-chl-widget-XXX_response">``) are
    # deliberately excluded - they exist in the main DOM but have no
    # rect and are useless as click targets.
    _TURNSTILE_LOCATORS: tuple[str, ...] = (
        # iframe selectors: when CF doesn't isolate via shadow DOM the
        # widget iframe lives in the main document and gives us the
        # exact widget rect.
        "xpath://iframe[contains(@src, 'challenges.cloudflare.com')]",
        "xpath://iframe[contains(@src, 'cdn-cgi/challenge')]",
        "xpath://iframe[contains(@src, 'turnstile')]",
        "xpath://iframe[starts-with(@id, 'cf-chl-widget') and not(@type='hidden')]",
        "xpath://iframe[contains(translate(@title, 'CLOUDFRARE', 'cloudfrare'), 'cloudflare')]",
        "xpath://iframe",
        # Visible widget container (sized to fit the checkbox card).
        "css:.cf-turnstile",
        "css:#cf-turnstile",
        "css:.turnstile-box",
        "css:#turnstile_box",
        "xpath://*[@data-sitekey and not(self::input)]",
        "xpath://div[starts-with(@id, 'cf-chl-widget')]",
        # Outer wrappers - large, used as last resort for offset click.
        "css:#turnstile-wrapper",
        "css:#challenge-stage",
    )

    # Anchor locators used to find a stable, *visible* element on the
    # CF challenge page so we can compute the widget's screen position
    # by offset. Headings are reliable because they appear in the
    # body's innerText regardless of whether the actual Turnstile
    # widget is rendered in a (closed) shadow DOM that the main
    # document's queries cannot reach.
    _ANCHOR_LOCATORS: tuple[str, ...] = (
        "xpath://h2[contains(text(), '安全验证') or contains(text(), '安全驗證')]",
        "xpath://h2[contains(text(), 'Verify') or contains(text(), 'Verifying')]",
        "xpath://h2[contains(text(), '人類') or contains(text(), '人间')]",
        "xpath://h1[contains(text(), 'v2ph') or contains(text(), 'V2PH')]",
        "xpath://h1",
        "xpath://h2",
    )

    def handle_cloudflare_turnstile(self) -> bool:
        """Attempt to solve a Cloudflare Turnstile interstitial.

        Modern CF challenge pages render the Turnstile widget inside a
        **closed** shadow root (or an OOP iframe whose element is not
        exposed to the main document), so ``document.getElementsByTagName
        ('iframe')`` returns 0 even though the checkbox is visually on
        the page. DOM-based clicks therefore cannot reach the widget.

        Our only reliable lever is an OS-level click at the widget's
        screen position. We use a visible heading (``<h1>`` / ``<h2>``)
        as a layout anchor to compute that position - it's always in
        the main DOM regardless of shadow encapsulation. If anchors
        also fail, we fall back to a fixed viewport-relative position
        that matches CF's standard interstitial layout.

        Returns True whenever an interstitial was observed. The outer
        retry loop will re-fetch the URL on True; CF clearance cookies
        persist across that re-fetch.
        """
        # Give CF a beat to fully paint the widget. Without this the
        # heading anchor may be in the DOM but the widget canvas hasn't
        # yet been drawn under it, so even a perfect click is ignored.
        self.random_sleep(1.0, 1.8)

        # If CF's challenge bundle exposes ``cType: 'managed'`` (a
        # backend-driven challenge with no visible widget) clicking
        # anywhere on the page is at best wasted work and at worst an
        # extra "non-human behaviour" signal. Skip the click loop and
        # just give CF's background JS time to validate the session.
        challenge_type = self._detect_cf_challenge_type()
        if challenge_type == "managed":
            self.logger.info(
                "Cloudflare 'managed' challenge detected (cType=managed, "
                "no widget to click); waiting for CF's background JS to "
                "validate the session - OS click loop skipped"
            )
            self._wait_for_clear(
                timeout=120.0,
                log_manual_hint=False,
                retry_click_every=0.0,
            )
            return True

        # Try OS-level anchor-based click first - this is the only
        # method that defeats closed-shadow-DOM + isTrusted checks.
        clicked = False
        if _HAS_PYAUTOGUI:
            clicked = self._try_anchor_os_click()

        if not clicked:
            # Either pyautogui is missing or no anchor was located.
            # Fall through to the legacy DOM-driven path; it usually
            # fails on modern CF but covers older challenge variants.
            clicked = self._try_auto_click(initial_wait=15.0)

        if clicked:
            self.logger.info(
                "Cloudflare turnstile click dispatched, waiting for clearance"
            )
        else:
            self.logger.info(
                "Could not auto-click the Cloudflare widget; "
                "will keep retrying while waiting for clearance"
            )

        self._wait_for_clear(
            timeout=120.0,
            log_manual_hint=not clicked,
            retry_click_every=6.0,
        )
        return True

    def _detect_cf_challenge_type(self) -> str:
        """Return CF's self-reported challenge type.

        Cloudflare's interstitial bundle assigns ``window._cf_chl_opt``
        on the page; its ``cType`` member is one of:

        * ``'managed'``  - fully backend-driven, no widget. The page
          shows just a heading + spinner; CF validates the browser via
          JS and HTTP-2/TLS fingerprints. Clicking does nothing.
        * ``'interactive'`` / ``'jschal'`` / ``'chl_api_*'`` - a
          Turnstile widget is (or will be) rendered and we should try
          to click it.
        * Empty / missing - either CF's bundle hasn't loaded yet or
          we're not actually on a CF page; treated the same as
          ``'unknown'``.
        """
        try:
            ctype = self.page.run_js(
                "return (window._cf_chl_opt && window._cf_chl_opt.cType) || ''"
            )
        except Exception:
            return "unknown"
        if not ctype:
            return "unknown"
        return str(ctype).strip().lower()

    def _try_anchor_os_click(self) -> bool:
        """OS-click the Turnstile checkbox.

        Strategy in priority order:

        1. **Widget direct hit** - if the Turnstile iframe / container
           is in the main DOM, use its real ``viewport_location`` and
           click 30 CSS px in from its left edge, vertically centred.
           This is precise to within a couple of pixels.
        2. **Heading-offset fallback** - if no widget element is
           reachable from the main document (rare on v2ph; mostly
           happens on shadow-DOM-only managed challenges, which we
           now skip earlier anyway), fall back to the legacy
           "H2 + 70 px" geometry guess.

        Returns True iff a click was dispatched.
        """
        if not _HAS_PYAUTOGUI or pyautogui is None:
            return False

        # --- Priority 1: locate the actual Turnstile widget rect ----
        for locator in self._TURNSTILE_LOCATORS:
            try:
                ele = self.page.ele(locator, timeout=0)
            except Exception:
                ele = None
            if not ele:
                continue
            try:
                loc = ele.rect.viewport_location
                size = ele.rect.size
                wx, wy = float(loc[0]), float(loc[1])
                ww, wh = float(size[0]), float(size[1])
            except Exception:
                continue
            # Reject zero-size matches (hidden <input> etc.) and the
            # full-page wrappers like ``#challenge-stage`` whose rect
            # spans the whole viewport - clicking their centre would
            # land somewhere far above the actual widget.
            if ww <= 30.0 or wh <= 30.0:
                continue
            # Standard Turnstile cards are ~300x65 CSS px. ``#turnstile-
            # wrapper`` and similar outer containers can be much taller
            # than the card itself; if so, click ~32 px down from the
            # top edge (where the checkbox sits in CF's layout) rather
            # than the geometric centre.
            click_vx = wx + min(30.0, max(15.0, ww * 0.08))
            if wh <= 100.0:
                click_vy = wy + wh / 2.0
            else:
                click_vy = wy + 32.0

            self.logger.info(
                "CF Turnstile widget located via %s at viewport "
                "(%.0f, %.0f) size %.0fx%.0f; targeting checkbox at "
                "viewport (%.0f, %.0f)",
                locator, wx, wy, ww, wh, click_vx, click_vy,
            )
            self._log_element_under_viewport_point(click_vx, click_vy)
            return self._pyautogui_click_viewport(click_vx, click_vy)

        # --- Priority 2: legacy heading-offset geometry guess --------
        anchor = None
        anchor_locator = ""
        anchor_rect: tuple[float, float, float, float] | None = None
        for locator in self._ANCHOR_LOCATORS:
            try:
                ele = self.page.ele(locator, timeout=0)
            except Exception:
                ele = None
            if not ele:
                continue
            try:
                loc = ele.rect.viewport_location
                size = ele.rect.size
                ax, ay = float(loc[0]), float(loc[1])
                aw, ah = float(size[0]), float(size[1])
            except Exception:
                continue
            if aw <= 1 or ah <= 1:
                continue
            anchor = ele
            anchor_locator = locator
            anchor_rect = (ax, ay, aw, ah)
            break

        if anchor is None or anchor_rect is None:
            self.logger.info(
                "No CF widget or heading anchor found; cannot compute "
                "widget screen position"
            )
            return False

        ax, ay, aw, ah = anchor_rect
        # CF's layout: the Turnstile card now sits ~110 CSS px below
        # the H2 heading bottom (was ~70 in older versions; v2ph's
        # current interstitial added an extra description line).
        # Empirically v2ph today places the checkbox row at viewport
        # y ≈ 325 with the H2 at y=186-216, so an offset of ~110 from
        # H2 bottom hits the checkbox centre.
        click_vx = ax + 22.0
        if "h1" in anchor_locator:
            click_vy = ay + ah + 180.0
        else:
            click_vy = ay + ah + 110.0

        self.logger.info(
            "CF heading anchor (no widget element visible) located via "
            "%s at viewport (%.0f, %.0f) size %.0fx%.0f; geometry-"
            "guessing widget at viewport (%.0f, %.0f)",
            anchor_locator, ax, ay, aw, ah, click_vx, click_vy,
        )
        self._log_element_under_viewport_point(click_vx, click_vy)
        return self._pyautogui_click_viewport(click_vx, click_vy)

    def _log_element_under_viewport_point(
        self, vx: float, vy: float
    ) -> None:
        """Diagnostic: log what DOM element is under the click point.

        Helps tell apart "we clicked the wrong pixel" from "we clicked
        the right pixel but Turnstile rejected the press". A real
        click on the checkbox should report something like
        ``IFRAME#cf-chl-widget-..`` or a child of ``.cf-turnstile``;
        if it instead reports the body / a heading we know our
        coordinates are off.
        """
        try:
            info = self.page.run_js(
                "const el = document.elementFromPoint(arguments[0], arguments[1]); "
                "if (!el) return 'no-element'; "
                "return (el.tagName || '?') + "
                "(el.id ? '#' + el.id : '') + "
                "(el.className && typeof el.className === 'string' ? "
                "  '.' + el.className.replace(/\\s+/g, '.') : '');",
                vx, vy,
            )
        except Exception as e:
            self.logger.debug("elementFromPoint diagnostic failed: %s", e)
            return
        self.logger.info(
            "Element under click target (viewport %.1f, %.1f): %s",
            vx, vy, info,
        )

    # Standard Chrome top-chrome height (title bar + tab strip +
    # URL bar) in CSS pixels. Used as a fallback when JS-reported
    # ``window.outerHeight - window.innerHeight`` is implausible.
    _STANDARD_CHROME_H_CSS: float = 90.0
    # Side border width on a maximised Chrome on Windows 10+ is 0,
    # but a non-maximised window can have ~8 px. We treat anything
    # under 30 px as plausible and fall back to 0 otherwise.
    _STANDARD_BORDER_W_CSS: float = 0.0

    def _bring_chromium_to_front(self) -> None:
        """Best-effort: ensure the Chromium window owning ``self.page``
        is the foreground window before we synthesise an OS click.

        Without this, ``pyautogui.click()`` will move the mouse to the
        right physical pixel but Windows may dispatch the resulting
        click to whatever window is currently on top (Cursor / IDE /
        terminal), so the Turnstile checkbox never sees the press.

        Uses CDP ``Browser.bringToFront`` which Chromium also routes
        through Win32 ``SetForegroundWindow``; failure is non-fatal,
        the click will still be attempted.
        """
        try:
            self.page.run_cdp("Page.bringToFront")
        except Exception:
            # ``Page.bringToFront`` is the most universally supported
            # variant and works on every reasonably modern Chromium;
            # ``Browser.bringToFront`` exists but isn't always exposed
            # on the page-target session DrissionPage hands us.
            try:
                self.page.run_cdp("Browser.bringToFront")
            except Exception as e:
                self.logger.debug("bringToFront failed: %s", e)

    def _pyautogui_click_viewport(self, click_vx: float, click_vy: float) -> bool:
        """Translate viewport coordinates ``(click_vx, click_vy)`` to
        screen coordinates and dispatch a real OS-level click.

        Coordinate gotchas this implementation handles:

        * Chrome counts a docked DevTools panel as part of
          ``outerHeight``, inflating ``outerHeight - innerHeight`` far
          above the real top-chrome height. We sanity-clamp the
          reported value and fall back to a 90 px standard.
        * ``pyautogui`` on Windows uses *physical* pixels when DPI
          aware, but JS values are in *CSS* pixels. We multiply the
          final coordinates by ``devicePixelRatio`` to convert.
        * Cloudflare's Turnstile fingerprints repeat-click locations:
          if every retry hits the same physical pixel that's a
          dead-giveaway for a bot. We therefore add ±6 CSS px of
          jitter around the requested point on every call.
        """
        if not _HAS_PYAUTOGUI or pyautogui is None:
            return False

        # Bring the Chromium window to the foreground first so that
        # the click event is routed to it and not to whichever window
        # currently has focus (IDE, terminal, etc.). Idempotent and
        # cheap to call on every retry.
        self._bring_chromium_to_front()

        try:
            info = self.page.run_js("""
return {
    sx: window.screenX,
    sy: window.screenY,
    outerW: window.outerWidth,
    outerH: window.outerHeight,
    innerW: window.innerWidth,
    innerH: window.innerHeight,
    dpr: window.devicePixelRatio || 1
};
""")
        except Exception as e:
            self.logger.debug("window position JS read failed: %s", e)
            return False

        if not isinstance(info, dict):
            return False

        try:
            screen_x = float(info["sx"])
            screen_y = float(info["sy"])
            outer_w = float(info["outerW"])
            outer_h = float(info["outerH"])
            inner_w = float(info["innerW"])
            inner_h = float(info["innerH"])
            dpr = float(info.get("dpr", 1) or 1)
            if dpr <= 0:
                dpr = 1.0
        except (KeyError, TypeError, ValueError) as e:
            self.logger.debug("window info parse failed: %s", e)
            return False

        # Bail out fast on obviously broken window state. Chromium
        # reports outerWidth/outerHeight as a tiny iconified rect
        # (~160x28) when the OS window is minimized, and Win32 places
        # such windows at the magic ``(-32000, -32000)`` location.
        # Either signal means an OS click would land somewhere in
        # the void - which the recent log shows happening at
        # ``(-47679, -47436)``.
        if outer_w < 200.0 or outer_h < 200.0:
            self.logger.warning(
                "Browser window appears minimized or torn off "
                "(outer=%.0fx%.0f); skipping OS click. Restore the "
                "v2dl Chrome window so it is visible on the primary "
                "display, then leave it alone while the scrape runs.",
                outer_w, outer_h,
            )
            return False
        if screen_x < -10000.0 or screen_y < -10000.0:
            self.logger.warning(
                "Browser window is iconified (screenX=%.0f, screenY=%.0f); "
                "skipping OS click until the window is restored.",
                screen_x, screen_y,
            )
            return False

        # ``window.outerWidth`` / ``outerHeight`` on Windows Chrome
        # with per-monitor-v2 DPI awareness are reported in *physical*
        # pixels, while ``innerWidth`` / ``innerHeight`` stay in CSS
        # pixels. Naively subtracting them produces the absurd 405 px
        # "chrome height" we used to log. Detect the mismatch and use
        # the dpr-corrected value instead.
        chrome_h_same_unit = outer_h - inner_h
        chrome_h_dpr_corrected = outer_h / dpr - inner_h
        border_w_same_unit = (outer_w - inner_w) / 2.0
        border_w_dpr_corrected = (outer_w / dpr - inner_w) / 2.0

        # Plausible Chrome top-chrome height range, in CSS px. 30-200
        # covers maximised windows (~58 CSS without bookmarks bar) up
        # through tall windows with docked extension toolbars.
        def _plausible_h(v: float) -> bool:
            return 30.0 <= v <= 200.0

        if _plausible_h(chrome_h_same_unit):
            chrome_h = chrome_h_same_unit
            chrome_h_source = "outer-inner"
        elif _plausible_h(chrome_h_dpr_corrected):
            chrome_h = chrome_h_dpr_corrected
            chrome_h_source = "outer-physical-corrected"
        else:
            chrome_h = self._STANDARD_CHROME_H_CSS
            chrome_h_source = "standard-fallback"
            self.logger.warning(
                "Browser chrome height %.0f px (outer %.0fx%.0f, "
                "inner %.0fx%.0f, dpr=%.2f) and dpr-corrected %.1f "
                "are both outside the plausible 30-200 px range. "
                "Using %.0f px fallback. Close any docked DevTools "
                "panel and remove bookmarks/extension bars for "
                "accurate OS clicks.",
                chrome_h_same_unit, outer_w, outer_h, inner_w, inner_h, dpr,
                chrome_h_dpr_corrected, chrome_h,
            )

        if 0.0 <= border_w_same_unit <= 30.0:
            border_w = border_w_same_unit
        elif 0.0 <= border_w_dpr_corrected <= 30.0:
            border_w = border_w_dpr_corrected
        else:
            border_w = self._STANDARD_BORDER_W_CSS

        # Add per-call jitter so that consecutive retries don't hit
        # the exact same physical pixel - Turnstile flags repeat-pixel
        # clicks as automation. ±6 CSS px keeps us well within the
        # Turnstile checkbox (~24 CSS px wide) while looking organic.
        jitter_vx = random.uniform(-6.0, 6.0)
        jitter_vy = random.uniform(-5.0, 5.0)

        # CSS-pixel coordinate of the click target on the OS desktop.
        css_x = screen_x + border_w + click_vx + jitter_vx
        css_y = screen_y + chrome_h + click_vy + jitter_vy

        # pyautogui on Windows with per-monitor-v2 DPI awareness uses
        # physical pixels. Scale up by devicePixelRatio.
        final_x = int(round(css_x * dpr))
        final_y = int(round(css_y * dpr))

        try:
            screen_w, screen_h = pyautogui.size()  # type: ignore[attr-defined]
        except Exception:
            screen_w = screen_h = 0
        if screen_w and screen_h:
            if not (0 <= final_x < screen_w and 0 <= final_y < screen_h):
                self.logger.warning(
                    "OS click (%d, %d) outside screen %dx%d; the browser "
                    "may be on another monitor / minimised. Make sure "
                    "the V2PH browser window is visible on the primary "
                    "display.",
                    final_x, final_y, screen_w, screen_h,
                )
                return False

        self.logger.info(
            "Dispatching OS click at physical screen (%d, %d) "
            "[CSS (%.0f, %.0f), jitter=(%+.1f,%+.1f) -> phys via dpr=%.2f; "
            "window @ (%.0f,%.0f), chrome_h=%.0f (%s), border_w=%.0f]",
            final_x, final_y, css_x, css_y, jitter_vx, jitter_vy, dpr,
            screen_x, screen_y, chrome_h, chrome_h_source, border_w,
        )
        try:
            # First glide to a point slightly off the target
            # (simulating a human approaching the checkbox), then
            # finish the move on the actual target. This produces a
            # non-linear mouse trajectory plus a small hover before
            # the click - both of which Turnstile uses as positive
            # human-behaviour signals.
            try:
                cur_x, cur_y = pyautogui.position()  # type: ignore[attr-defined]
            except Exception:
                cur_x, cur_y = final_x - 100, final_y - 100
            stage1_x = int(round(final_x + random.uniform(-30, 30)))
            stage1_y = int(round(final_y + random.uniform(-30, 30)))
            # If the cursor is already near the target there's no
            # point in a two-stage move - just go straight to it.
            if abs(cur_x - final_x) > 40 or abs(cur_y - final_y) > 40:
                pyautogui.moveTo(  # type: ignore[attr-defined]
                    stage1_x,
                    stage1_y,
                    duration=random.uniform(0.25, 0.5),
                )
                time.sleep(random.uniform(0.05, 0.15))
            pyautogui.moveTo(  # type: ignore[attr-defined]
                final_x,
                final_y,
                duration=random.uniform(0.18, 0.35),
            )
            # Verify the OS actually accepted the move. If pyautogui
            # is in a DPI-virtualised process or the foreground window
            # rejected the input, the cursor will be somewhere else
            # entirely - in that case the upcoming click hits whatever
            # is under the *real* cursor position, not our target.
            try:
                actual_x, actual_y = pyautogui.position()  # type: ignore[attr-defined]
                if abs(actual_x - final_x) > 3 or abs(actual_y - final_y) > 3:
                    self.logger.warning(
                        "Cursor ended up at (%d, %d) instead of (%d, %d) "
                        "after pyautogui.moveTo - the OS or another "
                        "process is intercepting input. The Turnstile "
                        "checkbox will not be clicked.",
                        actual_x, actual_y, final_x, final_y,
                    )
                else:
                    self.logger.debug(
                        "Cursor confirmed at (%d, %d) after moveTo",
                        actual_x, actual_y,
                    )
            except Exception:
                pass
            time.sleep(random.uniform(0.12, 0.28))
            pyautogui.click()  # type: ignore[attr-defined]
            return True
        except Exception as e:
            self.logger.warning("pyautogui click raised: %s", e)
            return False

    def _try_auto_click(self, initial_wait: float) -> bool:
        """Poll for a Turnstile target up to ``initial_wait`` seconds
        and click it. Returns True iff a click was actually dispatched.
        """
        deadline = time.time() + initial_wait
        last_logged_signature = ""
        already_logged_waiting = False
        dom_snapshot_logged = False
        heuristic_attempted = False
        # If selector-based location fails for this long, try a blind
        # OS-level click at the typical CF widget screen position.
        heuristic_after = initial_wait * 0.6
        start = time.time()

        while time.time() < deadline:
            target, locator = self._find_turnstile_target()
            if target is not None:
                self.logger.info(
                    "Turnstile target located via %s, attempting click",
                    locator,
                )
                self.random_sleep(0.3, 0.8)
                if self._click_turnstile_checkbox(target, locator):
                    return True
            else:
                if not already_logged_waiting:
                    self.logger.info(
                        "Cloudflare interstitial detected; waiting for "
                        "the Turnstile widget to appear in the DOM..."
                    )
                    already_logged_waiting = True

                signature = self._iframe_signature()
                if signature != last_logged_signature:
                    if signature:
                        self.logger.info("Current iframes on page: %s", signature)
                    else:
                        self.logger.info(
                            "No iframes currently in the DOM - CF may "
                            "still be running its background JS check, "
                            "or the widget is hosted in a shadow root."
                        )
                    last_logged_signature = signature

                # First time we miss, dump a snippet of the body HTML
                # to help diagnose what CF is actually rendering.
                if not dom_snapshot_logged:
                    self._dump_dom_snapshot()
                    dom_snapshot_logged = True

                # After ~60% of the wait window, fall back to a blind
                # OS-level click at the screen position where CF's
                # challenge widget is rendered (it's always centered
                # horizontally near the top of the viewport).
                if (
                    not heuristic_attempted
                    and _HAS_PYAUTOGUI
                    and (time.time() - start) >= heuristic_after
                ):
                    heuristic_attempted = True
                    if self._try_heuristic_os_click():
                        return True
            time.sleep(0.5)
        return False

    def _dump_dom_snapshot(self) -> None:
        """Log a compact snapshot of the current page so the user can
        see what CF is actually rendering when no selector matches."""
        try:
            url = self.page.url
        except Exception:
            url = "<unknown>"
        try:
            title = self.page.title
        except Exception:
            title = "<unknown>"
        try:
            html = self.page.html or ""
        except Exception:
            html = ""
        try:
            body_text = self.page.run_js(
                "return (document.body && document.body.innerText) || ''"
            ) or ""
        except Exception:
            body_text = ""
        try:
            counts = self.page.run_js("""
return {
    iframes: document.getElementsByTagName('iframe').length,
    forms: document.getElementsByTagName('form').length,
    cf_turnstile: document.getElementsByClassName('cf-turnstile').length,
    challenge_stage: document.getElementById('challenge-stage') ? 1 : 0,
    shadow_roots: Array.from(document.querySelectorAll('*'))
        .filter(e => e.shadowRoot).length,
}
""")
        except Exception:
            counts = {}

        self.logger.info(
            "CF page snapshot: url=%s title=%r counts=%s body_text=%r html_len=%d",
            url,
            title,
            counts,
            body_text[:160],
            len(html),
        )

    def _try_heuristic_os_click(self) -> bool:
        """Blindly click where CF's challenge widget *usually* renders.

        Used as a last-ditch fallback when no anchor heading is found
        and no DOM widget could be located. CF's v2ph challenge page
        is left-aligned so we target the absolute viewport position
        rather than the centre.
        """
        if not _HAS_PYAUTOGUI or pyautogui is None:
            return False

        try:
            inner_h = self.page.run_js("return window.innerHeight || 600")
        except Exception:
            inner_h = 600
        try:
            inner_h_f = float(inner_h)
        except (TypeError, ValueError):
            inner_h_f = 600.0

        # v2ph's CF page anchors content at ~155 px from the left edge;
        # the checkbox sits ~30 px in from there. Vertical position is
        # ~285 px from viewport top on a normal 700-800 px tall window.
        click_vx = 185.0
        click_vy = min(inner_h_f * 0.40, 320.0)

        self.logger.info(
            "Blind heuristic OS click at viewport (%.0f, %.0f) "
            "(no anchor found - CF's layout assumed left-aligned)",
            click_vx, click_vy,
        )
        return self._pyautogui_click_viewport(click_vx, click_vy)

    def _find_turnstile_target(self) -> tuple[Any, str]:
        """Return ``(element, locator_string)`` for the first matching
        Turnstile target, or ``(None, '')`` if none of the candidate
        locators resolves. Uses ``timeout=0`` so the lookup is an
        immediate single-shot check - the outer loop handles waiting.

        Hidden / zero-size matches are rejected outright; otherwise a
        hidden ``<input type="hidden" id="cf-chl-widget-XXX_response">``
        would be returned and the click would silently no-op.
        """
        for locator in self._TURNSTILE_LOCATORS:
            try:
                ele = self.page.ele(locator, timeout=0)
            except Exception:
                ele = None
            if not ele:
                continue
            # Strict size check: if we cannot read the rect, treat it
            # as unusable (likely a hidden input).
            try:
                size = ele.rect.size
                w = float(size[0]) if size and len(size) >= 1 else 0.0
                h = float(size[1]) if size and len(size) >= 2 else 0.0
            except Exception:
                continue
            if w <= 1 or h <= 1:
                continue
            return ele, locator
        return None, ""

    def _iframe_signature(self) -> str:
        """Compact diagnostic listing of all iframes on the page."""
        try:
            iframes = self.page.eles("xpath://iframe", timeout=0)
        except Exception:
            return ""
        parts: list[str] = []
        for idx, frame in enumerate(iframes):
            try:
                src = frame.attr("src") or ""
            except Exception:
                src = ""
            try:
                title = frame.attr("title") or ""
            except Exception:
                title = ""
            try:
                fid = frame.attr("id") or ""
            except Exception:
                fid = ""
            parts.append(
                f"[{idx}] id={fid[:30]!r} src={src[:80]!r} title={title[:40]!r}"
            )
            if idx >= 5:
                parts.append("...")
                break
        return "; ".join(parts)

    def _click_turnstile_checkbox(self, target: Any, locator: str) -> bool:
        """Click the Turnstile checkbox at the left edge of ``target``.

        Cloudflare renders the checkbox roughly 30 px in from the left
        edge of the widget rect, vertically centred. Modern CF
        Turnstile inspects ``event.isTrusted`` and silently discards
        any click that wasn't generated by the operating system, which
        means CDP / JS clicks land on the right pixel but achieve
        nothing. We therefore try strategies in this order:

        1. ``pyautogui`` - OS-level ``SendInput`` (Windows) / ``CGEvent``
           (macOS) / ``XTest`` (X11). The resulting click event has
           ``isTrusted: true`` and CF cannot tell it apart from a
           human's hand.
        2. ``ele.click.at(offset_x, offset_y)`` - CDP real-mouse with
           an element-relative offset (useful on older CF flows that
           don't enforce the isTrusted check, and for headless runs
           where pyautogui has no visible window).
        3. ``ele.click(offset_x=..., offset_y=...)`` - same as above
           on older DrissionPage builds.
        4. ``ele.click()`` - last resort, clicks the element's centre.
        """
        try:
            size = target.rect.size  # (w, h)
        except Exception as e:
            self.logger.debug(
                "Could not read target size via locator %s: %s", locator, e
            )
            size = None

        if not size or len(size) < 2:
            return self._click_target_centre(target, locator)

        try:
            w = float(size[0])
            h = float(size[1])
        except (TypeError, ValueError):
            w = h = 0.0

        if w <= 1 or h <= 1:
            self.logger.debug(
                "Turnstile target via %s has zero size (w=%.1f h=%.1f); skipping",
                locator, w, h,
            )
            return False

        offset_x = int(min(30.0, max(8.0, w * 0.1)))
        offset_y = int(h / 2)

        if self._try_os_click(target, offset_x, offset_y, locator, w, h):
            return True

        # CDP-injected clicks (``isTrusted: false``). CF often rejects
        # these, but keep them as a fallback for older CF flows / the
        # case where pyautogui is not installed.
        click_methods = (
            ("click.at", lambda: target.click.at(offset_x, offset_y)),
            ("click(offset)", lambda: target.click(offset_x=offset_x, offset_y=offset_y)),
        )
        for name, fn in click_methods:
            try:
                fn()
                self.logger.info(
                    "Turnstile %s dispatched at offset (%d, %d) "
                    "via locator %s [w=%.0f h=%.0f] (CDP click; CF "
                    "may reject due to isTrusted=false)",
                    name, offset_x, offset_y, locator, w, h,
                )
                return True
            except TypeError:
                continue
            except Exception as e:
                self.logger.debug(
                    "%s failed for locator %s: %s", name, locator, e
                )

        return self._click_target_centre(target, locator)

    def _click_target_centre(self, target: Any, locator: str) -> bool:
        try:
            target.click()
            self.logger.info(
                "Turnstile centre click dispatched via locator %s "
                "(no offset support - may miss the checkbox)",
                locator,
            )
            return True
        except Exception as e:
            self.logger.debug(
                "Centre click also failed for locator %s: %s", locator, e
            )
            return False

    def _try_os_click(
        self,
        target: Any,
        offset_x: int,
        offset_y: int,
        locator: str,
        w: float,
        h: float,
    ) -> bool:
        """Real OS-level mouse click via pyautogui.

        Converts the target's viewport coordinates into screen
        coordinates (browser window position + chrome height) and
        synthesises a Windows ``SendInput`` mouse event, which the
        OS marks ``isTrusted: true``. This is what CF actually wants
        to see.
        """
        if not _HAS_PYAUTOGUI or pyautogui is None:
            self.logger.debug(
                "pyautogui not installed; skipping OS-level click "
                "(install via `pip install v2dl[bypass]` for reliable "
                "Cloudflare Turnstile bypass)"
            )
            return False

        try:
            viewport_loc = target.rect.viewport_location  # (x, y) in viewport
        except Exception as e:
            self.logger.debug("viewport_location read failed: %s", e)
            return False

        if not viewport_loc or len(viewport_loc) < 2:
            return False

        try:
            vx, vy = float(viewport_loc[0]), float(viewport_loc[1])
        except (TypeError, ValueError):
            return False

        click_vx = vx + offset_x
        click_vy = vy + offset_y

        # Resolve viewport -> screen coordinates. ``window.screenX/Y``
        # give the window's top-left; subtracting outerHeight-innerHeight
        # yields the address-bar + tab-strip + title-bar height that
        # sits between the window top and the viewport top.
        try:
            info = self.page.run_js("""
return {
    sx: window.screenX,
    sy: window.screenY,
    outerW: window.outerWidth,
    outerH: window.outerHeight,
    innerW: window.innerWidth,
    innerH: window.innerHeight,
    dpr: window.devicePixelRatio || 1
};
""")
        except Exception as e:
            self.logger.debug("window position JS read failed: %s", e)
            return False

        if not isinstance(info, dict):
            return False
        try:
            screen_x = float(info["sx"])
            screen_y = float(info["sy"])
            outer_w = float(info["outerW"])
            outer_h = float(info["outerH"])
            inner_w = float(info["innerW"])
            inner_h = float(info["innerH"])
        except (TypeError, KeyError, ValueError) as e:
            self.logger.debug("window info parse failed: %s", e)
            return False

        chrome_h = max(0.0, outer_h - inner_h)
        border_w = max(0.0, (outer_w - inner_w) / 2.0)
        viewport_screen_x = screen_x + border_w
        viewport_screen_y = screen_y + chrome_h

        final_x = int(viewport_screen_x + click_vx)
        final_y = int(viewport_screen_y + click_vy)

        # Sanity check: the click must land within the visible screen
        # area or pyautogui will silently noop / move off-screen.
        try:
            screen_w, screen_h = pyautogui.size()  # type: ignore[attr-defined]
        except Exception:
            screen_w = screen_h = 0
        if screen_w and screen_h:
            if not (0 <= final_x < screen_w and 0 <= final_y < screen_h):
                self.logger.warning(
                    "OS-level click target (%d, %d) is outside the "
                    "primary screen %dx%d; browser may be on another "
                    "monitor or minimised. Falling back to CDP click.",
                    final_x, final_y, screen_w, screen_h,
                )
                return False

        self.logger.info(
            "Turnstile OS-level click via pyautogui at screen (%d, %d) "
            "via locator %s [viewport offset (%d, %d), widget w=%.0f h=%.0f]",
            final_x, final_y, locator, offset_x, offset_y, w, h,
        )

        try:
            # Move with a short randomised duration so the cursor
            # actually travels - some CF flows correlate click events
            # with preceding mousemove events.
            pyautogui.moveTo(  # type: ignore[attr-defined]
                final_x,
                final_y,
                duration=random.uniform(0.25, 0.6),
            )
            time.sleep(random.uniform(0.08, 0.2))
            pyautogui.click()  # type: ignore[attr-defined]
            return True
        except Exception as e:
            self.logger.warning("pyautogui click raised: %s", e)
            return False

    def _wait_for_clear(
        self,
        timeout: float = 120.0,
        log_manual_hint: bool = False,
        retry_click_every: float = 0.0,
    ) -> bool:
        """Poll until ``is_simple_blocked`` reports the interstitial is
        gone, or until ``timeout`` elapses. Doubles as the manual-solve
        window: the user can tick the checkbox themselves in the
        opened browser and the scraper will resume automatically.

        If ``retry_click_every > 0``, we periodically retry the
        automated click - useful when the widget renders only after
        CF's initial JS challenge fails (so it wasn't in the DOM
        during the initial click attempt).
        """
        if log_manual_hint:
            self.logger.warning(
                "Cloudflare interstitial is still on screen - please "
                "click the captcha in the browser within %.0fs. The "
                "download will resume automatically once verified.",
                timeout,
            )
        deadline = time.time() + timeout
        next_retry = time.time() + retry_click_every if retry_click_every > 0 else float("inf")
        managed_logged = False
        while time.time() < deadline:
            if not self.is_simple_blocked():
                self.logger.info("Cloudflare interstitial cleared")
                return True
            if time.time() >= next_retry:
                # Re-check cType every retry tick: CF can downgrade
                # from interactive back to managed (or vice versa)
                # while the page is sitting on the interstitial. If
                # we're now on a managed challenge there's nothing
                # to click, so silently extend the next-retry window
                # rather than spamming OS clicks into empty space.
                if self._detect_cf_challenge_type() == "managed":
                    if not managed_logged:
                        self.logger.info(
                            "Challenge re-classified as 'managed' "
                            "mid-wait; suppressing further OS click "
                            "retries until clearance or timeout"
                        )
                        managed_logged = True
                    next_retry = time.time() + max(retry_click_every, 5.0)
                else:
                    self.logger.debug("Retrying automated turnstile click")
                    # Anchor-based OS click first - the only thing that
                    # works on modern (closed shadow DOM) CF widgets.
                    retried = False
                    if _HAS_PYAUTOGUI:
                        retried = self._try_anchor_os_click()
                    if not retried:
                        target, locator = self._find_turnstile_target()
                        if target is not None:
                            self._click_turnstile_checkbox(target, locator)
                    next_retry = time.time() + retry_click_every
            time.sleep(1.0)

        self.logger.warning(
            "Cloudflare interstitial did not clear within %.0fs - "
            "the outer retry loop will re-fetch the URL.",
            timeout,
        )
        return False

    def random_sleep(self, min_time: float, max_time: float) -> None:
        time.sleep(random.uniform(min_time, max_time))


class DriBehavior(BaseBehavior):
    @staticmethod
    def human_like_mouse_movement(page: Any, element: Any) -> None:
        # Get the element's position
        rect = element.rect
        action_x = random.randint(-100, 100)
        action_y = random.randint(-100, 100)

        # Move by offset and then to element
        page.mouse.move_to(x=rect["x"] + action_x, y=rect["y"] + action_y)
        page.mouse.move_to(rect["x"], rect["y"])
        DriBehavior.random_sleep(*BaseBehavior.pause_time)

    @staticmethod
    def human_like_click(page: Any, element: Any) -> None:
        DriBehavior.human_like_mouse_movement(page, element)
        page.mouse.click()
        DriBehavior.random_sleep(*BaseBehavior.pause_time)

    @staticmethod
    def human_like_type(element: Any, text: str) -> None:
        for char in text:
            element.input(char)
            time.sleep(random.uniform(0.001, 0.2))
        DriBehavior.random_sleep(*BaseBehavior.pause_time)


class DriScroll(BaseScroll):
    def __init__(self, page: ChromiumPage, config: "Config", logger: Logger) -> None:
        super().__init__(config, logger)
        self.page = page
        self.page.set.scroll.smooth(on_off=True)

    async def scroll_to_bottom(self) -> None:
        attempts = 0
        max_attempts = 10
        wait_time = (1, 2)
        last_position = -123459
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
            self.page.run_js(f"window.scrollBy({{top: {scroll}, behavior: 'smooth'}});")
            await asyncio.sleep(random.uniform(*wait_time))

            new_position = self.page.run_js("return window.pageYOffset;")
            if new_position == last_position:
                break
            last_position = new_position
            attempts += 1

    def old_scroll_to_bottom(self) -> None:
        self.logger.info("Start scrolling the page")
        scroll_attempts = 0
        max_attempts = 45
        same_position_count = 0
        max_same_position_count = 3
        last_position = 0.0
        scrolled_up = False

        while scroll_attempts < max_attempts:
            scroll_attempts += 1

            current_position = self.get_current_position()

            if current_position == last_position:
                same_position_count += 1
                if same_position_count >= max_same_position_count:
                    self.logger.debug(
                        "Detected the same position three times in a row, stopping scrolling. Scrolled a total of %d times",
                        scroll_attempts,
                    )
                    break
            else:
                same_position_count = 0

            last_position = current_position

            scrolled_up = self.perform_scroll_action(scrolled_up)

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
                scrolled_up = False
                self.successive_scroll_count = 0
                self.max_successive_scrolls = random.randint(3, 7)

        if scroll_attempts == max_attempts:
            self.logger.info(
                "Reached maximum attempts (%d), scrolling finished, may not have fully reached the bottom",
                max_attempts,
            )
        else:
            self.logger.info("Page scroll completed")

    def perform_scroll_action(self, scrolled_up: bool) -> bool:
        while True:
            action = random.choices(
                ["scroll_down", "scroll_up", "pause", "jump"],
                weights=[0.9, 0.1, 0.1, 0.01],
            )[0]

            if (
                action != "scroll_up" or not scrolled_up
            ):  # 連續捲動時，只要往上捲動過一次就不要再選擇往上
                break

        if action == "scroll_down":
            scroll_length = random.randint(
                self.config.static_config.min_scroll_step,
                self.config.static_config.max_scroll_step,
            )
            self.logger.debug("Trying to scroll down %d", scroll_length)
            self.page.scroll.down(pixel=scroll_length)
            time.sleep(random.uniform(*BaseBehavior.pause_time))
        elif action == "scroll_up":
            scroll_length = random.randint(
                self.config.static_config.min_scroll_step,
                self.config.static_config.max_scroll_step,
            )
            self.logger.debug("Trying to scroll up %d", scroll_length)
            self.page.scroll.up(pixel=scroll_length)
        elif action == "pause":
            pause_time = random.uniform(1, 3)
            self.logger.debug("Pausing for %.2f seconds", pause_time)
            time.sleep(pause_time)
        elif action == "jump":
            self.logger.debug("Jumping to the bottom of the page")
            self.page.scroll.to_bottom()
            # self.page.scroll.to_see("@class=album-photo my-2")

        return action == "scrolled_up"

    # def safe_scroll(self, target_position):
    #     """DrissionPage does not need safe_scroll."""
    #     current_position = self.get_current_position()
    #     step = random.uniform(
    #         self.config.download.min_scroll_step,
    #         self.config.download.max_scroll_step,
    #     )

    #     while abs(current_position - target_position) > step:
    #         self.page.run_js(f"window.scrollTo(0, {current_position + step});")
    #         time.sleep(random.uniform(0.005, 0.1))
    #         new_position = self.get_current_position()
    #         if new_position == current_position:
    #             self.logger.debug(
    #                 "Cannot continue scrolling, target: %d, current: %d", target_position, current_position
    #             )
    #             break
    #         current_position = new_position
    #     self.page.run_js(f"window.scrollTo(0, {target_position});")
    #     return self.get_current_position()

    def get_current_position(self) -> float:
        page_location: float = self.page.run_js("return window.pageYOffset;")
        self.logger.debug("Current vertical position %d", page_location)
        return page_location

    def get_page_height(self) -> float:
        page_height: float = self.page.run_js("return document.body.scrollHeight;")
        self.logger.debug("Total page height %d", page_height)
        return page_height

    def wait_for_content_load(self) -> None:
        try:
            # wait until the callable return true. default time_out=10
            wait_until(lambda: self.page.states.ready_state == "complete", timeout=5)
        except TimeoutError:
            self.logger.warning("Timeout waiting for new content to load")
