import os, re, time, random, json
from urllib.parse import urlparse, parse_qs, unquote
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchWindowException, WebDriverException
import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
SERVICE_ACCOUNT_INFO = json.loads(SERVICE_ACCOUNT_JSON)
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
SHEET_NAME = os.environ.get("SHEET_NAME", "Jeff's Thread Tracker v2")
AMZ_EMAIL = os.environ["AMZ_EMAIL"]
AMZ_PASS = os.environ["AMZ_PASS"]

CHROME_BIN = os.environ.get("CHROME_BIN", "/usr/bin/chromium")
CHROMEDRIVER_PATH = os.environ.get("CHROMEDRIVER_PATH", "/usr/bin/chromedriver")

creds = Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO, scopes=SCOPES)
gc = gspread.authorize(creds)
sheet = gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)

def mark_manual(row):
    sheet.update(f"I{row}", [["MANUAL"]])
    print(f"✍️  Row {row} marked as MANUAL")

def chrome_driver():
    service = Service(CHROMEDRIVER_PATH)
    options = webdriver.ChromeOptions()
    options.binary_location = CHROME_BIN
    options.add_argument("--headless=new")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-features=NetworkServiceInProcess")
    options.add_argument("--log-level=3")
    user_data_dir = os.environ.get("CHROME_USER_DATA_DIR", "/tmp/chrome-profile")
    options.add_argument(f"--user-data-dir={user_data_dir}")
    options.page_load_strategy = "eager"
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(60)
    return driver

def amazon_login(driver, email, password, timeout=30):
    try:
        wait = WebDriverWait(driver, timeout)
        driver.get("https://affiliate-program.amazon.com/home")
        time.sleep(1)
        if "/home" in driver.current_url.lower():
            try:
                if driver.find_elements(By.CSS_SELECTOR, "a.ac-creatorhub-header-item-login-button"):
                    pass
                else:
                    print("✅ Already signed in (home).")
                    return True
            except Exception:
                print("✅ Already signed in (home).")
                return True
        if "signin" in driver.current_url.lower() or driver.find_elements(By.ID, "ap_email"):
            print("🔐 On Amazon sign-in form.")
        else:
            try:
                sign_in_link = wait.until(EC.element_to_be_clickable(
                    (By.CSS_SELECTOR, "a.ac-creatorhub-header-item-login-button")
                ))
                driver.execute_script("arguments[0].click();", sign_in_link)
                print("✅ Clicked Sign In link.")
            except TimeoutException:
                if "/home" in driver.current_url.lower():
                    print("✅ Already signed in (no Sign In button).")
                    return True
        try:
            email_input = wait.until(EC.visibility_of_element_located((By.ID, "ap_email")))
            email_input.clear()
            email_input.send_keys(email)
            print("✅ Entered email.")
            wait.until(EC.element_to_be_clickable((By.ID, "continue"))).click()
            print("✅ Clicked Continue.")
        except TimeoutException:
            pass
        try:
            password_input = wait.until(EC.visibility_of_element_located((By.ID, "ap_password")))
            password_input.clear()
            password_input.send_keys(password)
            print("✅ Entered password.")
            sign_in_btn = wait.until(EC.element_to_be_clickable((By.ID, "signInSubmit")))
            driver.execute_script("arguments[0].click();", sign_in_btn)
            print("✅ Clicked Sign In.")
        except TimeoutException:
            pass
        wait.until(EC.url_contains("/home"))
        print("✅ Login successful. Ready to process rows.")
        return True
    except Exception as e:
        print(f"❌ Error during login automation: {e}")
        return False

def ensure_amazon_session(driver, email, password):
    try:
        driver.get("https://affiliate-program.amazon.com/home")
        time.sleep(1)
        if "signin" in driver.current_url.lower() or driver.find_elements(By.CSS_SELECTOR, "a.ac-creatorhub-header-item-login-button"):
            print("🔐 Amazon session not active, logging in...")
            return amazon_login(driver, email, password, timeout=45)
        print("🔐 Amazon session active.")
        return True
    except Exception:
        print("🔐 Amazon session check failed, attempting login...")
        return amazon_login(driver, email, password, timeout=45)

def js_commission_probe(driver):
    try:
        return driver.execute_script("""
            const base = document.getElementById('amzn-ss-commission-rate-content');
            const bonus = document.getElementById('amzn-ss-cc-rate');
            const baseT = base ? base.textContent.trim() : '';
            const bonusT = bonus ? bonus.textContent.trim() : '';
            return [baseT, bonusT];
        """)
    except Exception:
        return ["",""]

def get_commission_texts(driver, max_wait=45):
    t0 = time.time()
    base_text, bonus_text = "",""
    while time.time() - t0 < max_wait:
        base_text, bonus_text = js_commission_probe(driver)
        if base_text or bonus_text:
            return base_text, bonus_text
        iframes = driver.find_elements(By.CSS_SELECTOR, "iframe")
        for fr in iframes[:6]:
            try:
                driver.switch_to.frame(fr)
                b, c = js_commission_probe(driver)
                driver.switch_to.default_content()
                if b or c:
                    return b, c
            except Exception:
                try:
                    driver.switch_to.default_content()
                except Exception:
                    pass
        time.sleep(1)
    return base_text, bonus_text

def extract_rate(txt):
    m = re.search(r"([\d]+(?:\.\d+)?)", txt or "")
    return float(m.group(1)) if m else 0.0

OUTCLICK_SELECTORS = [
    "a.dealDetailsOutclickButton",
    "a.dealCardCTALink",
    "a[data-role='outclick']",
    "a[data-tracking*='outclick']",
    "a[href*='/f/redirect']",
    "a[href*='amazon.']"
]

def decode_redirect(url):
    try:
        pr = urlparse(url)
        qs = parse_qs(pr.query)
        for key in ("u2", "u"):
            if key in qs and qs[key]:
                return unquote(qs[key][0])
        return url
    except Exception:
        return url

def build_amazon_from_cta(a):
    asin = a.get_attribute("data-aps-asin") or ""
    if not asin:
        return None
    tag = a.get_attribute("data-aps-asc-tag") or ""
    sub = a.get_attribute("data-aps-asc-subtag") or ""
    url = f"https://www.amazon.com/dp/{asin}"
    qs = []
    if tag:
        qs.append(f"tag={tag}")
    if sub and "%ascsubtag%" not in sub:
        qs.append(f"ascsubtag={sub}")
    if qs:
        url += "?" + "&".join(qs)
    return url

def looks_like_product_url(u):
    ul = (u or "").lower()
    if "amazon." not in ul:
        return False
    bad = ("product-reviews", "/review", "customer-reviews", "/ask", "/questions")
    if any(x in ul for x in bad):
        return False
    return True

def find_amazon_url_or_click(driver):
    preferred_ctas = driver.find_elements(
        By.CSS_SELECTOR,
        "a.dealDetailsOutclickButton[data-store-slug*='amazon'], "
        "a.dealDetailsOutclickButton[data-aps-asin], "
        "a.dealDetailsMainBlock__outclickButton[data-store-slug*='amazon'], "
        "a[data-cta='outclick'][data-store-slug*='amazon'], "
        "a[data-qa-ddp-seedeal-button][data-store-slug*='amazon']"
    )
    for a in preferred_ctas:
        try:
            if not a.is_displayed():
                continue
            built = build_amazon_from_cta(a)
            if built and looks_like_product_url(built):
                print(f"✅ Pref Amazon CTA: {built}")
                return built, None
        except Exception:
            continue
    for a in preferred_ctas:
        try:
            if not a.is_displayed():
                continue
            original = driver.current_window_handle
            before = set(driver.window_handles)
            before_url = driver.current_url
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", a)
            time.sleep(0.1)
            driver.execute_script("arguments[0].click();", a)
            try:
                WebDriverWait(driver, 10).until(lambda d: len(d.window_handles) > len(before))
                new_handle = (set(driver.window_handles) - before).pop()
                driver.switch_to.window(new_handle)
                print("🧭 Switched via CTA (new tab)")
                return None, (original, new_handle)
            except TimeoutException:
                try:
                    WebDriverWait(driver, 10).until(lambda d: d.current_url != before_url)
                except TimeoutException:
                    pass
                if looks_like_product_url(driver.current_url):
                    print("🧭 Switched via CTA (same tab)")
                    return None, (original, None)
        except Exception:
            continue
    candidates = []
    sels = [
        "a.dealDetailsOutclickButton",
        "a.dealCardCTALink",
        "a[data-role='outclick']",
        "a[data-tracking*='outclick']",
        "a[href*='/f/redirect']",
        "a[href*='slickdeals.net/click']",
        "a[href*='amazon.']",
    ]
    for sel in sels:
        try:
            for a in driver.find_elements(By.CSS_SELECTOR, sel):
                href = a.get_attribute("href") or ""
                if not href:
                    continue
                decoded = decode_redirect(href)
                if looks_like_product_url(decoded):
                    candidates.append(decoded)
        except Exception:
            continue
    def rank(u):
        ul = u.lower()
        if "/dp/" in ul or "/gp/product/" in ul or "/gp/aw/d/" in ul:
            return 100
        if "/offer-listing/" in ul:
            return 80
        return 50
    if candidates:
        best = sorted(set(candidates), key=rank, reverse=True)[0]
        print(f"✅ Fallback Amazon link: {best}")
        return best, None
    links = []
    for sel in sels:
        try:
            links.extend(driver.find_elements(By.CSS_SELECTOR, sel))
        except Exception:
            pass
    original = driver.current_window_handle
    before = set(driver.window_handles)
    for a in links:
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", a)
            time.sleep(0.1)
            driver.execute_script("arguments[0].click();", a)
            break
        except Exception:
            continue
    try:
        WebDriverWait(driver, 10).until(lambda d: len(d.window_handles) > len(before))
        new_handle = (set(driver.window_handles) - before).pop()
        driver.switch_to.window(new_handle)
        print("🧭 Switched via generic outclick")
        return None, (original, new_handle)
    except TimeoutException:
        return None, (original, None)

def ensure_on_amazon(driver, max_wait=30):
    t0 = time.time()
    while time.time() - t0 < max_wait:
        try:
            if "amazon." in driver.current_url.lower():
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False

def safe_close_extra_tabs(driver, keep_handle):
    try:
        for h in list(driver.window_handles):
            if h != keep_handle:
                try:
                    driver.switch_to.window(h)
                    driver.close()
                except Exception:
                    pass
        driver.switch_to.window(keep_handle)
    except Exception:
        pass

def process_row(driver, row_num, thread_url):
    attempts = 2
    relogin_done = False
    for attempt in range(1, attempts+1):
        try:
            print(f"\n➡ Row {row_num} attempt {attempt} → {thread_url}")
            driver.get(thread_url)
            time.sleep(random.uniform(0.8, 1.6))
            try:
                err = driver.find_element(By.CSS_SELECTOR, "h2.errorPage__headline")
                if "400 Error" in err.text:
                    print("400 Error")
                    return "400 Error"
            except Exception:
                pass
            url_hint, tab_tuple = find_amazon_url_or_click(driver)
            if url_hint:
                if "amazon." not in url_hint.lower():
                    print("ℹ️ Direct outclick is non-Amazon, skipping commission.")
                    return "NON-AMAZON"
                try:
                    driver.set_page_load_timeout(60)
                    driver.get(url_hint)
                except TimeoutException:
                    print("⏱️ Timeout navigating to Amazon direct link")
            else:
                orig, newh = tab_tuple
                if newh:
                    try:
                        driver.switch_to.window(newh)
                    except Exception:
                        pass
                if not ensure_on_amazon(driver, 25):
                    try:
                        cur = driver.current_url.lower()
                    except Exception:
                        cur = ""
                    if "amazon." not in cur and "slickdeals" not in cur and cur:
                        print("ℹ️ Outclick goes to non-Amazon store, skipping commission.")
                        if tab_tuple[0]:
                            safe_close_extra_tabs(driver, tab_tuple[0])
                        return "NON-AMAZON"
                    print("❌ Did not arrive on Amazon after outclick")
                    if tab_tuple[0]:
                        safe_close_extra_tabs(driver, tab_tuple[0])
                    continue
            if not ensure_on_amazon(driver, 10):
                print("❌ Not on Amazon")
                continue
            current_product_url = driver.current_url
            print(f"✅ On Amazon: {current_product_url[:140]}")
            base_text, bonus_text = get_commission_texts(driver, max_wait=50)
            if not base_text and not bonus_text and not relogin_done:
                print("🔄 No commission widgets seen, verifying Amazon session...")
                if ensure_amazon_session(driver, AMZ_EMAIL, AMZ_PASS):
                    relogin_done = True
                    driver.get(current_product_url)
                    base_text, bonus_text = get_commission_texts(driver, max_wait=40)
            if not base_text and not bonus_text:
                print("❌ Commission widgets not found")
                continue
            base_value = extract_rate(base_text)
            bonus_value = extract_rate(bonus_text)
            total = base_value + bonus_value
            print(f"➡ Commission base={base_value:.2f}% bonus={bonus_value:.2f}% total={total:.2f}%")
            return f"{total:.2f}%"
        except (NoSuchWindowException, WebDriverException) as e:
            print(f"💥 Browser error: {e}")
            time.sleep(2)
            continue
        except Exception as e:
            print(f"❌ Unexpected error row {row_num}: {e}")
            time.sleep(2)
            continue
    return None

def retry_manual_rows(driver):
    col_b = sheet.col_values(2)
    col_i = sheet.col_values(9)
    max_len = len(col_b)
    col_i += [""] * (max_len - len(col_i))
    manual_rows = []
    for row_num in range(2, max_len + 1):
        url = (col_b[row_num - 1] or "").strip()
        commission = (col_i[row_num - 1] or "").strip().upper()
        if url and commission == "MANUAL":
            manual_rows.append((row_num, url))
    if not manual_rows:
        print("✅ No MANUAL rows to retry.")
        return
    print(f"🔁 Retrying {len(manual_rows)} MANUAL rows.")
    clear_updates = [{"range": f"I{row_num}", "values": [[""]]} for row_num, _ in manual_rows]
    sheet.batch_update(clear_updates)
    updates = []
    processed = 0
    for row_num, thread_url in manual_rows:
        total_pct = process_row(driver, row_num, thread_url)
        if total_pct is None:
            mark_manual(row_num)
        elif total_pct == "400 Error":
            updates.append({"range": f"I{row_num}", "values": [["400 Error"]]})
        elif total_pct == "NON-AMAZON":
            updates.append({"range": f"I{row_num}", "values": [["NON-AMAZON"]]})
        else:
            updates.append({"range": f"I{row_num}", "values": [[total_pct]]})
        processed += 1
        if processed % 10 == 0 and updates:
            sheet.batch_update(updates)
            print(f"✅ Saved MANUAL retry batch after {processed} threads.")
            updates = []
        time.sleep(random.uniform(0.6, 1.2))
    if updates:
        sheet.batch_update(updates)
        print("✅ Finished retry pass for MANUAL rows.")

if __name__ == "__main__":
    driver = chrome_driver()
    try:
        ok = ensure_amazon_session(driver, AMZ_EMAIL, AMZ_PASS)
        if not ok:
            try:
                driver.quit()
            except Exception:
                pass
            raise SystemExit(1)
    except Exception as e:
        print(f"❌ Error during login automation: {e}")
        try:
            driver.quit()
        except Exception:
            pass
        raise SystemExit(1)
    while True:
        try:
            col_a = sheet.col_values(1)
            while col_a and not col_a[-1].strip():
                col_a.pop()
            bottom_row = len(col_a)
            col_b = sheet.col_values(2)
            col_i = sheet.col_values(9)
            max_len = bottom_row
            col_b += [""] * (max_len - len(col_b))
            col_i += [""] * (max_len - len(col_i))
            rows_to_process = []
            for row_num in range(bottom_row, 1, -1):
                url = col_b[row_num - 1].strip()
                commission = col_i[row_num - 1].strip()
                if url and not commission:
                    rows_to_process.append((row_num, url))
            print(f"\n🔁 Found {len(rows_to_process)} rows needing scraping.")
            updates = []
            processed = 0
            if rows_to_process:
                for row_num, thread_url in rows_to_process:
                    total_pct = process_row(driver, row_num, thread_url)
                    if total_pct is None:
                        mark_manual(row_num)
                    elif total_pct == "400 Error":
                        updates.append({"range": f"I{row_num}", "values": [["400 Error"]]})
                    elif total_pct == "NON-AMAZON":
                        updates.append({"range": f"I{row_num}", "values": [["NON-AMAZON"]]})
                    else:
                        updates.append({"range": f"I{row_num}", "values": [[total_pct]]})
                    processed += 1
                    if processed % 10 == 0 and updates:
                        sheet.batch_update(updates)
                        print(f"✅ Saved batch update after {processed} threads.")
                        updates = []
                    time.sleep(random.uniform(0.6, 1.2))
                if updates:
                    sheet.batch_update(updates)
                    print("✅ Google Sheet updated with commission rates.")
                else:
                    print("✅ No updates needed this round.")
            else:
                print("✅ No new rows to scrape.")
            retry_manual_rows(driver)
        except Exception as e:
            print(f"❌ Fatal loop error: {e}")
            try:
                driver.quit()
            except Exception:
                pass
            time.sleep(5)
            driver = chrome_driver()
            if not ensure_amazon_session(driver, AMZ_EMAIL, AMZ_PASS):
                print("❌ Could not re-establish Amazon session.")
        print("⏳ Sleep 5 minutes...")
        time.sleep(300)
