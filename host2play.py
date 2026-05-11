import os
import re
import time
import subprocess
import tempfile
import requests
from botasaurus.browser import browser, Driver

try:
    import speech_recognition as sr
    from pydub import AudioSegment
except ImportError:
    sr = None
    AudioSegment = None


HOST2PLAY_URLS = [
    line.strip()
    for line in os.environ.get("HOST2PLAY_URLS", "").splitlines()
    if line.strip()
]

RENEW_URLS = HOST2PLAY_URLS or [
    "",
]

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
BUSTER_EXTENSION_PATH = os.environ.get("BUSTER_EXTENSION_PATH", "")
RAW_PROXY_CONFIG = os.environ.get("HOST2PLAY_SOCKS5_PROXIES", "").strip() or os.environ.get(
    "HOST2PLAY_SOCKS5_PROXY", ""
).strip() or os.environ.get("SOCKS5_PROXY", "").strip()

SCREENSHOT_NAME = "host2play_status.png"
SCREENSHOT_PATH = os.path.join("output", "screenshots", SCREENSHOT_NAME)
ROOT_SCREENSHOT_PATH = SCREENSHOT_NAME
MAX_CAPTCHA_WAIT_SECONDS = 30
POST_SOLVE_WAIT_SECONDS = 18
MAX_RENEW_RETRIES_PER_URL = 3
CURRENT_TASK_PROXY = ""


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
    if proxy_value.startswith("socks://"):
        return "socks5://" + proxy_value[len("socks://") :]
    if "://" not in proxy_value:
        return f"socks5://{proxy_value}"
    return proxy_value


def get_proxy_pool() -> list[str]:
    proxies = []
    for line in RAW_PROXY_CONFIG.splitlines():
        proxy = normalize_socks5_proxy(line)
        if proxy:
            proxies.append(proxy)
    if not proxies:
        single = normalize_socks5_proxy(RAW_PROXY_CONFIG)
        if single:
            proxies.append(single)
    return proxies


def get_proxy_from_data(data) -> str | None:
    if isinstance(data, dict):
        proxy = normalize_socks5_proxy(data.get("proxy", ""))
        return proxy or None
    return None


def set_task_proxy(proxy: str):
    global CURRENT_TASK_PROXY
    CURRENT_TASK_PROXY = proxy


def get_current_task_proxy() -> str:
    if CURRENT_TASK_PROXY:
        return CURRENT_TASK_PROXY
    pool = get_proxy_pool()
    return pool[0] if pool else ""


def get_requests_proxies() -> dict[str, str] | None:
    proxy = get_current_task_proxy()
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def get_browser_proxy() -> str | None:
    proxy = get_current_task_proxy()
    return proxy or None


def is_proxy_enabled() -> bool:
    return bool(get_browser_proxy())


def can_use_proxy(proxy: str) -> bool:
    proxy = normalize_socks5_proxy(proxy)
    if not proxy:
        return True

    try:
        response = requests.get(
            "https://api.ipify.org",
            timeout=8,
            proxies={"http": proxy, "https": proxy},
        )
        return response.ok and bool(response.text.strip())
    except Exception as exc:
        log(f"Skipping unavailable proxy {proxy}: {exc}")
        return False


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
        os.makedirs(os.path.dirname(SCREENSHOT_PATH), exist_ok=True)
        driver.save_screenshot(SCREENSHOT_PATH)
        try:
            driver.save_screenshot(ROOT_SCREENSHOT_PATH)
        except Exception:
            pass
    except Exception as exc:
        log(f"Screenshot failed: {exc}")


def restart_warp() -> bool:
    if is_proxy_enabled():
        log("SOCKS5 proxy is enabled, skipping WARP restart.")
        return False

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


def has_recaptcha_challenge_frame(driver: Driver) -> bool:
    return wait_for_recaptcha_frame(driver, "iframe[src*='recaptcha/api2/bframe']", 8)


def is_audio_challenge_visible(driver: Driver) -> bool:
    if not has_recaptcha_challenge_frame(driver):
        return False
    try:
        challenge = find_recaptcha_challenge_frame(driver)
        return bool(
            challenge.run_js(
                """
                const input = document.querySelector('#audio-response');
                return !!input;
                """
            )
        )
    except Exception:
        return False


def is_visual_challenge_visible(driver: Driver) -> bool:
    if not has_recaptcha_challenge_frame(driver):
        return False
    try:
        challenge = find_recaptcha_challenge_frame(driver)
        return bool(
            challenge.run_js(
                """
                const imageSelectors = [
                    '.rc-imageselect-instructions',
                    '.rc-imageselect-desc-wrapper',
                    '.rc-imageselect-target',
                    '.rc-image-tile-wrapper',
                    '#rc-imageselect'
                ];
                return imageSelectors.some((selector) => !!document.querySelector(selector));
                """
            )
        )
    except Exception:
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
            if is_audio_challenge_visible(driver):
                log("reCAPTCHA already in audio mode.")
                return True

            if not is_visual_challenge_visible(driver):
                log(f"Challenge frame detected but visual challenge is not ready yet on attempt {attempt}.")
                human_pause(driver, 2)
                continue

            try:
                challenge.click("#recaptcha-audio-button")
                human_pause(driver, 3)
                if is_recaptcha_blocked(driver):
                    raise CaptchaBlocked("Google reCAPTCHA blocked the current IP when switching to audio mode.")
                if is_audio_challenge_visible(driver):
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
            if clicked and is_audio_challenge_visible(driver):
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
    for attempt in range(1, 9):
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
                    const buttonByTitle = document.querySelector('[title*="Buster"], [aria-label*="Buster"]');
                    if (buttonByTitle) {
                        buttonByTitle.click();
                        return true;
                    }
                    return false;
                    """
                )
            )
            if clicked:
                break
        log(f"Buster solver button not ready on attempt {attempt}.")
        human_pause(driver, 2)

    if not clicked:
        log("Buster solver button not found inside challenge frame.")
        return False

    log("Buster solver triggered, waiting for completion.")
    human_pause(driver, POST_SOLVE_WAIT_SECONDS)
    return True


def get_audio_download_url(driver: Driver) -> str | None:
    if not has_recaptcha_challenge_frame(driver):
        return None
    try:
        challenge = find_recaptcha_challenge_frame(driver)
        return challenge.run_js(
            """
            const candidates = [
                '.rc-audiochallenge-tdownload-link',
                '.rc-audiochallenge-ndownload-link',
                '#audio-source'
            ];
            for (const selector of candidates) {
                const element = document.querySelector(selector);
                if (!element) continue;
                const value = element.href || element.src || element.getAttribute('href') || element.getAttribute('src');
                if (value) return value;
            }
            return null;
            """
        )
    except Exception:
        return None


def download_audio_file(url: str) -> str | None:
    proxies = get_requests_proxies()
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.google.com/",
    }

    candidate_urls = [url]
    if "google.com" in url:
        candidate_urls.append(url.replace("google.com", "recaptcha.net"))
    elif "recaptcha.net" in url:
        candidate_urls.append(url.replace("recaptcha.net", "google.com"))

    for candidate in candidate_urls:
        try:
            response = requests.get(candidate, headers=headers, timeout=30, proxies=proxies)
            response.raise_for_status()
            if len(response.content) < 512:
                continue
            path = tempfile.mktemp(suffix=".mp3")
            with open(path, "wb") as file_obj:
                file_obj.write(response.content)
            return path
        except Exception:
            continue
    return None


def recognize_audio_file(mp3_path: str) -> str | None:
    if not sr or not AudioSegment:
        log("SpeechRecognition or pydub is not installed; audio fallback unavailable.")
        return None

    wav_path = mp3_path.replace(".mp3", ".wav")
    try:
        AudioSegment.from_mp3(mp3_path).export(wav_path, format="wav")
        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio_data = recognizer.record(source)
        text = recognizer.recognize_google(audio_data)
        return clean_text(text)
    except Exception as exc:
        log(f"Audio recognition failed: {exc}")
        return None
    finally:
        for path in [wav_path, mp3_path]:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass


def submit_audio_response(driver: Driver, answer: str) -> bool:
    if not has_recaptcha_challenge_frame(driver):
        return False

    try:
        challenge = find_recaptcha_challenge_frame(driver)
        challenge.run_js(
            """
            const input = document.querySelector('#audio-response');
            if (input) {
                input.value = '';
            }
            """
        )
        challenge.type("#audio-response", answer)
        human_pause(driver, 1)
        try:
            challenge.click("#recaptcha-verify-button")
        except Exception:
            challenge.run_js(
                """
                const button = document.querySelector('#recaptcha-verify-button');
                if (button) {
                    button.click();
                    return true;
                }
                return false;
                """
            )
        return True
    except Exception as exc:
        log(f"Submitting audio response failed: {exc}")
        return False


def solve_audio_challenge_locally(driver: Driver) -> bool:
    for attempt in range(1, 4):
        audio_url = get_audio_download_url(driver)
        if not audio_url:
            log(f"Audio URL not found on attempt {attempt}.")
            human_pause(driver, 2)
            continue

        mp3_path = download_audio_file(audio_url)
        if not mp3_path:
            log(f"Audio download failed on attempt {attempt}.")
            human_pause(driver, 2)
            continue

        answer = recognize_audio_file(mp3_path)
        if not answer:
            log(f"Audio recognition returned empty result on attempt {attempt}.")
            human_pause(driver, 2)
            continue

        log(f"Audio recognition result: {answer}")
        if not submit_audio_response(driver, answer):
            human_pause(driver, 2)
            continue

        human_pause(driver, 5)
        if is_recaptcha_solved(driver):
            log("reCAPTCHA solved by local audio fallback.")
            return True

    return False


def solve_recaptcha_with_buster(driver: Driver) -> bool:
    if is_recaptcha_solved(driver):
        return True

    if is_recaptcha_blocked(driver):
        raise CaptchaBlocked("Google reCAPTCHA already shows 'Try again later'.")

    if not click_recaptcha_checkbox(driver):
        return False

    human_pause(driver, 4)
    if is_recaptcha_solved(driver):
        log("reCAPTCHA solved from checkbox without challenge.")
        return True

    if not has_recaptcha_challenge_frame(driver):
        log("No challenge frame appeared after checkbox click; proceeding without audio switch.")
        return is_recaptcha_solved(driver)

    if is_visual_challenge_visible(driver):
        log("Visual reCAPTCHA challenge detected, switching to audio mode.")
        if not switch_recaptcha_to_audio(driver):
            log("Failed to switch visual challenge to audio mode.")
            return False
    elif is_audio_challenge_visible(driver):
        log("Audio challenge is already visible.")
    else:
        log("Challenge frame appeared, but neither visual nor audio challenge is ready yet.")
        human_pause(driver, 3)
        if is_visual_challenge_visible(driver):
            if not switch_recaptcha_to_audio(driver):
                log("Failed to switch delayed visual challenge to audio mode.")
                return False
        elif not is_audio_challenge_visible(driver):
            log("Challenge type could not be determined.")
            return False

    if trigger_buster_solver(driver):
        deadline = time.time() + MAX_CAPTCHA_WAIT_SECONDS
        while time.time() < deadline:
            if is_recaptcha_blocked(driver):
                raise CaptchaBlocked("Google reCAPTCHA switched to 'Try again later' while Buster was solving.")
            if is_recaptcha_solved(driver):
                log("reCAPTCHA solved by Buster.")
                return True
            human_pause(driver, 2)
        log("Timed out waiting for Buster to solve reCAPTCHA.")
    else:
        log("Buster solver was unavailable, falling back to local audio recognition.")

    return solve_audio_challenge_locally(driver)


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


def renew_single_attempt(driver: Driver, payload: dict) -> dict:
    url = payload["url"]
    attempt = payload["attempt"]
    proxy = payload.get("proxy", "")
    set_task_proxy(proxy)

    server_name = "Unknown"
    old_expire = "Unknown"

    if proxy:
        log(f"Using SOCKS5 proxy for attempt {attempt}: {proxy}")
    else:
        log(f"Using direct network for attempt {attempt}.")

    log(f"Renew attempt {attempt}/{MAX_RENEW_RETRIES_PER_URL} for {url}")

    try:
        log(f"Opening renewal URL: {url}")
        driver.get(url)
        human_pause(driver, 8)

        remove_overlays(driver)
        human_pause(driver, 2)

        server_name = get_server_name(driver)
        old_expire = get_expire_time(driver)

        log(f"Detected server: {server_name}")
        log(f"Expire time before renew: {old_expire}")

        if not open_renew_dialog(driver):
            save_status_screenshot(driver)
            return {
                "success": False,
                "reason": "Renew button not found.",
                "server_name": server_name,
                "old_expire": old_expire,
            }

        if driver.is_element_present("iframe[src*='recaptcha/api2/anchor']"):
            log("reCAPTCHA detected, starting audio-mode solve flow.")
            if not solve_recaptcha_with_buster(driver):
                save_status_screenshot(driver)
                return {
                    "success": False,
                    "reason": "Failed to solve reCAPTCHA or switch to audio mode.",
                    "server_name": server_name,
                    "old_expire": old_expire,
                }
        else:
            log("No reCAPTCHA frame detected after opening renew dialog.")

        if not click_final_confirm(driver):
            log("Final confirm button was not clicked, checking page state anyway.")

        success, new_expire = detect_success(driver, old_expire)
        save_status_screenshot(driver)

        if success:
            return {
                "success": True,
                "server_name": server_name,
                "old_expire": old_expire,
                "new_expire": new_expire,
            }

        return {
            "success": False,
            "reason": "Renew action finished but no success marker or expire-time change was detected.",
            "server_name": server_name,
            "old_expire": old_expire,
        }

    except CaptchaBlocked as exc:
        save_status_screenshot(driver)
        return {
            "success": False,
            "blocked": True,
            "reason": str(exc),
            "server_name": server_name,
            "old_expire": old_expire,
        }
    except Exception as exc:
        save_status_screenshot(driver)
        return {
            "success": False,
            "reason": str(exc),
            "server_name": server_name,
            "old_expire": old_expire,
        }


@browser(
    headless=False,
    window_size=(1920, 1080),
    extensions=get_extensions(),
    proxy=get_proxy_from_data,
)
def run_host2play_attempt(driver: Driver, data):
    set_task_proxy(data.get("proxy", ""))
    log("Buster extension loaded through Botasaurus.")
    if get_browser_proxy():
        log(f"SOCKS5 proxy enabled for browser: {get_browser_proxy()}")
    else:
        log("SOCKS5 proxy not configured; using direct network.")
    log_public_ip()
    return renew_single_attempt(driver, data)


def host2play_renewal_task():
    total = len(RENEW_URLS)
    success_count = 0
    proxy_pool = get_proxy_pool()
    max_attempts = max(MAX_RENEW_RETRIES_PER_URL, len(proxy_pool)) if proxy_pool else MAX_RENEW_RETRIES_PER_URL

    for index, url in enumerate(RENEW_URLS, start=1):
        log(f"Processing {index}/{total}: {url}")

        last_result = {
            "success": False,
            "reason": "Unknown failure",
            "server_name": "Unknown",
            "old_expire": "Unknown",
        }

        for attempt in range(1, max_attempts + 1):
            proxy = proxy_pool[(attempt - 1) % len(proxy_pool)] if proxy_pool else ""
            if proxy and not can_use_proxy(proxy):
                last_result = {
                    "success": False,
                    "blocked": True,
                    "reason": f"Proxy health check failed: {proxy}",
                    "server_name": "Unknown",
                    "old_expire": "Unknown",
                }
                if attempt < max_attempts:
                    log("Trying the next configured proxy because the current one failed health checks.")
                    continue
                break

            result = run_host2play_attempt(
                {
                    "url": url,
                    "attempt": attempt,
                    "proxy": proxy,
                }
            )
            last_result = result

            if result.get("success"):
                send_tg_message(
                    build_success_message(
                        url,
                        result["server_name"],
                        result["old_expire"],
                        result["new_expire"],
                    ),
                    SCREENSHOT_PATH,
                )
                success_count += 1
                break

            if result.get("blocked") and attempt < max_attempts:
                if proxy_pool and len(proxy_pool) > 1:
                    log("Current proxy was blocked; next retry will rotate to the next configured proxy.")
                elif not proxy_pool and restart_warp():
                    human_sleep(6)
                else:
                    log("Proxy/IP was blocked and no alternate proxy is configured.")
                continue

            if not result.get("blocked"):
                break

        if not last_result.get("success"):
            send_tg_message(
                build_failure_message(
                    url,
                    last_result.get("server_name", "Unknown"),
                    last_result.get("old_expire", "Unknown"),
                    last_result.get("reason", "Unknown failure"),
                ),
                SCREENSHOT_PATH,
            )

        human_sleep(5)

    if success_count != total:
        raise RuntimeError(f"Only {success_count}/{total} Host2Play renewals succeeded.")


if __name__ == "__main__":
    host2play_renewal_task()
