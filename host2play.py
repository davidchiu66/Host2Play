import os
import re
import time
import subprocess
import requests
from botasaurus.browser import browser, Driver


HOST2PLAY_URLS = [
    line.strip()
    for line in os.environ.get("HOST2PLAY_URLS", "").splitlines()
    if line.strip()
]

RENEW_URLS = HOST2PLAY_URLS or [
    "https://host2play.gratis/server/renew?i=03cab61c-8431-4684-a7fa-8a422c7b6e10",
]

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
BUSTER_EXTENSION_PATH = os.environ.get("BUSTER_EXTENSION_PATH", "")
SOCKS5_PROXY = os.environ.get("HOST2PLAY_SOCKS5_PROXY", "").strip() or os.environ.get("SOCKS5_PROXY", "").strip()

SCREENSHOT_NAME = "host2play_status.png"
SCREENSHOT_PATH = os.path.join("output", "screenshots", SCREENSHOT_NAME)
MAX_CAPTCHA_WAIT_SECONDS = 30
POST_SOLVE_WAIT_SECONDS = 18
MAX_RENEW_RETRIES_PER_URL = 3


def log(message: str):
    print(f"[HOST2PLAY] {message}", flush=True)


class CaptchaBlocked(Exception):
    pass


def human_sleep(seconds: int | float):
    time.sleep(seconds)


def normalize_socks5_proxy(proxy_value: str) -> str:
    if not proxy_value:
        return ""

    proxy_value = proxy_value.strip()
    if "://" not in proxy_value:
        return f"socks5://{proxy_value}"
    return proxy_value


def get_requests_proxies() -> dict[str, str] | None:
    proxy = normalize_socks5_proxy(SOCKS5_PROXY)
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def get_browser_proxy() -> str | None:
    proxy = normalize_socks5_proxy(SOCKS5_PROXY)
    return proxy or None


def send_tg_message(text: str, photo_path: str | None = None):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log("Telegram not configured, skipping notification.")
        return

    proxies = get_requests_proxies()
    try:
        if photo_path and os.path.exists(photo_path):
            url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto"
            data = {
                "chat_id": TG_CHAT_ID,
                "caption": text,
                "parse_mode": "HTML",
            }
            with open(photo_path, "rb") as photo_file:
                requests.post(
                    url,
                    data=data,
                    files={"photo": photo_file},
                    timeout=30,
                    proxies=proxies,
                )
        else:
            url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
            data = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}
            requests.post(url, data=data, timeout=30, proxies=proxies)
        log("Telegram notification sent.")
    except Exception as exc:
        log(f"Telegram notification failed: {exc}")


class BusterExtension:
    def __init__(self, extension_path: str):
        self.extension_path = os.path.abspath(extension_path)

    def load(self, with_command_line_option=True):
        if with_command_line_option:
            return f"--load-extension={self.extension_path}"
        return self.extension_path

    @property
    def extension_absolute_path(self):
        return self.extension_path


def get_extensions():
    if BUSTER_EXTENSION_PATH and os.path.exists(BUSTER_EXTENSION_PATH):
        return [BusterExtension(BUSTER_EXTENSION_PATH)]
    return []


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def get_text_by_selector(driver: Driver, selector: str) -> str:
    try:
        return clean_text(
            driver.run_js(
                f"""
                const element = document.querySelector({selector!r});
                return element ? (element.innerText || element.textContent || '') : '';
                """
            )
        )
    except Exception:
        return ""


def get_page_text(driver: Driver) -> str:
    try:
        return clean_text(driver.run_js("return document.body ? document.body.innerText : '';"))
    except Exception:
        return ""


def get_server_name(driver: Driver) -> str:
    selectors = [
        "#serverName",
        "h1",
        "h2",
        ".server-name",
        "[data-server-name]",
    ]
    for selector in selectors:
        text = get_text_by_selector(driver, selector)
        if text:
            return text
    return "Unknown"


def extract_expire_time_from_text(text: str) -> str:
    patterns = [
        r"Expires\s+in\s*:?\s*(.+?)(?:\n|$)",
        r"Deletes\s+on\s*:?\s*(.+?)(?:\n|$)",
        r"Expire(?:s|d)?\s*:?\s*(.+?)(?:\n|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return clean_text(match.group(1))
    return "Unknown"


def get_expire_time(driver: Driver) -> str:
    selectors = [
        "#expireDate",
        "[data-expire-date]",
        ".expire-date",
    ]
    for selector in selectors:
        text = get_text_by_selector(driver, selector)
        if text:
            return text
    return extract_expire_time_from_text(get_page_text(driver))


def save_status_screenshot(driver: Driver):
    try:
        driver.save_screenshot(SCREENSHOT_NAME)
    except Exception as exc:
        log(f"Screenshot failed: {exc}")


def restart_warp() -> bool:
    log("Restarting WARP to rotate IP.")
    try:
        subprocess.run(
            ["sudo", "warp-cli", "--accept-tos", "disconnect"],
            check=False,
            timeout=30,
            capture_output=True,
        )
        human_sleep(3)
        subprocess.run(
            ["sudo", "warp-cli", "--accept-tos", "registration", "delete"],
            check=False,
            timeout=30,
            capture_output=True,
        )
        human_sleep(2)
        subprocess.run(
            ["sudo", "warp-cli", "--accept-tos", "registration", "new"],
            check=True,
            timeout=30,
            capture_output=True,
        )
        human_sleep(2)
        subprocess.run(
            ["sudo", "warp-cli", "--accept-tos", "connect"],
            check=True,
            timeout=30,
            capture_output=True,
        )
        human_sleep(8)
        return True
    except Exception as exc:
        log(f"WARP restart failed: {exc}")
        return False


def log_public_ip():
    proxies = get_requests_proxies()
    try:
        response = requests.get("https://api.ipify.org", timeout=20, proxies=proxies)
        log(f"Current outbound IP: {response.text.strip()}")
    except Exception as exc:
        log(f"Failed to query public IP: {exc}")


def remove_overlays(driver: Driver):
    driver.run_js(
        """
        const selectors = [
            'ins.adsbygoogle',
            'iframe[src*="ads"]',
            '.modal-backdrop',
            '[aria-label="Close ad"]',
            '[id*="cookie"]',
            '[class*="cookie"]'
        ];
        selectors.forEach((selector) => {
            document.querySelectorAll(selector).forEach((node) => node.remove());
        });
        """
    )


def human_pause(driver: Driver, seconds: int = 2):
    driver.sleep(seconds)


def is_recaptcha_blocked(driver: Driver) -> bool:
    if not wait_for_recaptcha_frame(driver, "iframe[src*='recaptcha/api2/bframe']", 5):
        return False

    try:
        challenge = find_recaptcha_challenge_frame(driver)
        blocked = challenge.run_js(
            """
            const blockedHeader = document.querySelector('.rc-doscaptcha-header-text');
            const blockedBody = document.querySelector('.rc-doscaptcha-body-text');
            const pageText = (document.body ? document.body.innerText : '').toLowerCase();

            if (blockedHeader && blockedHeader.innerText.toLowerCase().includes('try again later')) {
                return true;
            }

            if (blockedBody && blockedBody.innerText.toLowerCase().includes('automated queries')) {
                return true;
            }

            return pageText.includes('try again later') || pageText.includes('automated queries');
            """
        )
        return bool(blocked)
    except Exception:
        return False


def click_first_matching_button(driver: Driver, snippets: list[str]) -> bool:
    snippets_js = "[" + ", ".join(repr(item.lower()) for item in snippets) + "]"
    js = f"""
    const snippets = {snippets_js};
    const elements = Array.from(document.querySelectorAll('button, a, [role="button"], input[type="submit"]'));
    for (const element of elements) {{
        const text = (element.innerText || element.textContent || element.value || '').trim().toLowerCase();
        if (!text) continue;
        if (snippets.some((snippet) => text.includes(snippet))) {{
            element.removeAttribute('target');
            element.click();
            return true;
        }}
    }}
    return false;
    """
    return bool(driver.run_js(js))


def open_renew_dialog(driver: Driver) -> bool:
    button_labels = ["renew server", "renew"]
    for _ in range(3):
        if click_first_matching_button(driver, button_labels):
            human_pause(driver, 3)
            return True
        driver.run_js("window.scrollBy(0, 500);")
        human_pause(driver, 2)
    return False


def find_recaptcha_anchor_frame(driver: Driver):
    return driver.select_iframe("iframe[src*='recaptcha/api2/anchor']")


def find_recaptcha_challenge_frame(driver: Driver):
    return driver.select_iframe("iframe[src*='recaptcha/api2/bframe']")


def wait_for_recaptcha_frame(driver: Driver, selector: str, timeout_seconds: int = 20) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if driver.is_element_present(selector):
            return True
        human_pause(driver, 1)
    return False


def is_recaptcha_solved(driver: Driver) -> bool:
    try:
        anchor = find_recaptcha_anchor_frame(driver)
        checked = anchor.run_js(
            "return document.querySelector('#recaptcha-anchor')?.getAttribute('aria-checked') === 'true';"
        )
        if checked:
            return True
    except Exception:
        pass

    try:
        token = driver.run_js(
            "return document.querySelector(\"textarea[name='g-recaptcha-response']\")?.value || '';"
        )
        return bool(token and len(token) > 20)
    except Exception:
        return False


def click_recaptcha_checkbox(driver: Driver) -> bool:
    if not wait_for_recaptcha_frame(driver, "iframe[src*='recaptcha/api2/anchor']", 25):
        return False

    try:
        anchor = find_recaptcha_anchor_frame(driver)
        anchor.click("#recaptcha-anchor")
        human_pause(driver, 3)
        if is_recaptcha_blocked(driver):
            raise CaptchaBlocked("Google reCAPTCHA returned 'Try again later' right after checkbox click.")
        return True
    except Exception as exc:
        if isinstance(exc, CaptchaBlocked):
            raise
        log(f"Failed to click reCAPTCHA checkbox: {exc}")
        return False


def switch_recaptcha_to_audio(driver: Driver) -> bool:
    if not wait_for_recaptcha_frame(driver, "iframe[src*='recaptcha/api2/bframe']", 20):
        log("reCAPTCHA challenge frame not found.")
        return False

    for attempt in range(1, 5):
        try:
            challenge = find_recaptcha_challenge_frame(driver)
            if is_recaptcha_blocked(driver):
                raise CaptchaBlocked("Google reCAPTCHA blocked the current IP before audio mode opened.")
            if challenge.run_js("return !!document.querySelector('#audio-response');"):
                log("reCAPTCHA already in audio mode.")
                return True

            try:
                challenge.click("#recaptcha-audio-button")
                human_pause(driver, 3)
                if is_recaptcha_blocked(driver):
                    raise CaptchaBlocked("Google reCAPTCHA blocked the current IP when switching to audio mode.")
                if challenge.run_js("return !!document.querySelector('#audio-response');"):
                    log(f"Switched reCAPTCHA to audio mode on attempt {attempt}.")
                    return True
            except Exception as exc:
                if isinstance(exc, CaptchaBlocked):
                    raise
                pass

            clicked = challenge.run_js(
                """
                const selectors = [
                    '#recaptcha-audio-button',
                    'button[aria-label*="audio" i]',
                    'button[title*="audio" i]',
                    '.rc-button-audio'
                ];
                for (const selector of selectors) {
                    const button = document.querySelector(selector);
                    if (button) {
                        button.click();
                        return true;
                    }
                }

                const buttons = Array.from(document.querySelectorAll('button'));
                for (const button of buttons) {
                    const text = (button.innerText || button.textContent || '').toLowerCase();
                    if (text.includes('audio')) {
                        button.click();
                        return true;
                    }
                }
                return false;
                """
            )

            human_pause(driver, 3)
            if is_recaptcha_blocked(driver):
                raise CaptchaBlocked("Google reCAPTCHA blocked the current IP after audio-button interaction.")
            if clicked and challenge.run_js("return !!document.querySelector('#audio-response');"):
                log(f"Switched reCAPTCHA to audio mode on attempt {attempt}.")
                return True
        except Exception as exc:
            if isinstance(exc, CaptchaBlocked):
                raise
            log(f"Audio switch attempt {attempt} failed: {exc}")

        human_pause(driver, 2)

    return False


def trigger_buster_solver(driver: Driver) -> bool:
    challenge = find_recaptcha_challenge_frame(driver)

    clicked = False
    for _ in range(5):
        try:
            challenge.click("#solver-button")
            clicked = True
            break
        except Exception:
            clicked = bool(
                challenge.run_js(
                    """
                    const button = document.querySelector('#solver-button');
                    if (button) {
                        button.click();
                        return true;
                    }
                    return false;
                    """
                )
            )
            if clicked:
                break
        human_pause(driver, 2)

    if not clicked:
        log("Buster solver button not found inside challenge frame.")
        return False

    log("Buster solver triggered, waiting for completion.")
    human_pause(driver, POST_SOLVE_WAIT_SECONDS)
    return True


def solve_recaptcha_with_buster(driver: Driver) -> bool:
    if is_recaptcha_solved(driver):
        return True

    if is_recaptcha_blocked(driver):
        raise CaptchaBlocked("Google reCAPTCHA already shows 'Try again later'.")

    if not click_recaptcha_checkbox(driver):
        return False

    human_pause(driver, 3)
    if is_recaptcha_solved(driver):
        log("reCAPTCHA solved from checkbox without challenge.")
        return True

    if not switch_recaptcha_to_audio(driver):
        log("Failed to switch reCAPTCHA to audio mode.")
        return False

    if not trigger_buster_solver(driver):
        return False

    deadline = time.time() + MAX_CAPTCHA_WAIT_SECONDS
    while time.time() < deadline:
        if is_recaptcha_blocked(driver):
            raise CaptchaBlocked("Google reCAPTCHA switched to 'Try again later' while Buster was solving.")
        if is_recaptcha_solved(driver):
            log("reCAPTCHA solved by Buster.")
            return True
        human_pause(driver, 2)

    log("Timed out waiting for Buster to solve reCAPTCHA.")
    return False


def click_final_confirm(driver: Driver) -> bool:
    labels = ["renew", "confirm", "continue"]
    for _ in range(4):
        if click_first_matching_button(driver, labels):
            human_pause(driver, 6)
            return True
        human_pause(driver, 2)
    return False


def detect_success(driver: Driver, old_expire: str) -> tuple[bool, str]:
    new_expire = get_expire_time(driver)
    page_text = get_page_text(driver).lower()

    if new_expire != "Unknown" and new_expire != old_expire:
        return True, new_expire

    success_markers = [
        "successfully renewed",
        "server renewed",
        "renewed successfully",
        "renew successful",
    ]
    if any(marker in page_text for marker in success_markers):
        return True, new_expire

    return False, new_expire


def build_success_message(url: str, server_name: str, old_expire: str, new_expire: str) -> str:
    return (
        "<b>Host2Play renewal succeeded</b>\n\n"
        f"<b>Server:</b> <code>{server_name}</code>\n"
        f"<b>Expire:</b> <code>{old_expire}</code> -> <code>{new_expire}</code>\n"
        f"<b>URL:</b> <code>{url}</code>"
    )


def build_failure_message(url: str, server_name: str, old_expire: str, reason: str) -> str:
    return (
        "<b>Host2Play renewal failed</b>\n\n"
        f"<b>Server:</b> <code>{server_name}</code>\n"
        f"<b>Expire:</b> <code>{old_expire}</code>\n"
        f"<b>URL:</b> <code>{url}</code>\n"
        f"<b>Reason:</b> <code>{reason}</code>"
    )


def renew_single_url(driver: Driver, url: str) -> bool:
    last_reason = "Unknown failure"
    last_server_name = "Unknown"
    last_old_expire = "Unknown"

    for attempt in range(1, MAX_RENEW_RETRIES_PER_URL + 1):
        log(f"Renew attempt {attempt}/{MAX_RENEW_RETRIES_PER_URL} for {url}")

        try:
            log(f"Opening renewal URL: {url}")
            driver.get(url)
            human_pause(driver, 8)

            remove_overlays(driver)
            human_pause(driver, 2)

            server_name = get_server_name(driver)
            old_expire = get_expire_time(driver)
            last_server_name = server_name
            last_old_expire = old_expire

            log(f"Detected server: {server_name}")
            log(f"Expire time before renew: {old_expire}")

            if not open_renew_dialog(driver):
                last_reason = "Renew button not found."
                break

            if driver.is_element_present("iframe[src*='recaptcha/api2/anchor']"):
                log("reCAPTCHA detected, starting audio-mode solve flow.")
                if not solve_recaptcha_with_buster(driver):
                    last_reason = "Failed to solve reCAPTCHA or switch to audio mode."
                    break
            else:
                log("No reCAPTCHA frame detected after opening renew dialog.")

            if not click_final_confirm(driver):
                log("Final confirm button was not clicked, checking page state anyway.")

            success, new_expire = detect_success(driver, old_expire)
            save_status_screenshot(driver)

            if success:
                send_tg_message(
                    build_success_message(url, server_name, old_expire, new_expire),
                    SCREENSHOT_PATH,
                )
                return True

            last_reason = "Renew action finished but no success marker or expire-time change was detected."
            break

        except CaptchaBlocked as exc:
            last_reason = str(exc)
            log(f"reCAPTCHA blocked current IP: {last_reason}")
            save_status_screenshot(driver)
            if attempt < MAX_RENEW_RETRIES_PER_URL and restart_warp():
                human_pause(driver, 6)
                continue
            break
        except Exception as exc:
            last_reason = str(exc)
            save_status_screenshot(driver)
            break

    send_tg_message(
        build_failure_message(url, last_server_name, last_old_expire, last_reason),
        SCREENSHOT_PATH,
    )
    return False


@browser(
    headless=False,
    window_size=(1920, 1080),
    extensions=get_extensions(),
    proxy=get_browser_proxy(),
)
def host2play_renewal_task(driver: Driver, data):
    log("Buster extension loaded through Botasaurus.")
    if get_browser_proxy():
        log(f"SOCKS5 proxy enabled for browser: {get_browser_proxy()}")
    else:
        log("SOCKS5 proxy not configured; using direct network.")
    log_public_ip()
    total = len(RENEW_URLS)
    success_count = 0

    for index, url in enumerate(RENEW_URLS, start=1):
        log(f"Processing {index}/{total}: {url}")
        if renew_single_url(driver, url):
            success_count += 1
        human_pause(driver, 5)

    if success_count != total:
        raise RuntimeError(f"Only {success_count}/{total} Host2Play renewals succeeded.")


if __name__ == "__main__":
    host2play_renewal_task()
