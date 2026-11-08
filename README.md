# LIVIQO Dubizzle Owner Finder

## Windows setup
1. Install Python 3.10+ (enable "Add Python to PATH").
2. Double-click `START-WINDOWS.bat`.
3. Wait for dependencies and Chromium to install.
4. Open `http://127.0.0.1:8000` in your browser.

## First use
- Click **Open Dubizzle login**. A separate Playwright Chromium window opens.
- Log in manually and set your search filters. Click **Done login** in the app.
- Paste the exact Dubizzle search-results URL and click **Start scan**.
- If human verification appears, complete it in that same Chromium window and click **Continue same task**.

## Important limitations
- The supplied scraper uses best-effort Dubizzle selectors. Website markup changes may require selector updates.
- The original scraper pauses for human verification at initial search-page load. This app keeps that same scraper thread and browser context alive while waiting. The original engine does not implement automatic mid-run Cloudflare detection; a challenge arising later may require stopping and restarting after manual resolution.
- Owner classification means "Owner Candidate" based on contact-option evidence; it is not proof of legal ownership.
- This app does not bypass CAPTCHA, Cloudflare, login, or access controls.
- No sample listings, fake prices, or generated property photos are inserted. Results come from the original scraper's CSV log.
- Automated scraping may be restricted by Dubizzle's terms. Use reasonable volumes and check the site's rules.

## Data files
The original scraper writes `outreach_log.csv`, `processed_listings.json`, `outreach_photos/`, `outreach_report.html`, and `dubizzle_profile/` beside the app. Back these up before replacing the project.

## Troubleshooting
- If Chromium fails to launch, run `python -m playwright install chromium` inside the app's virtual environment.
- If the site blocks access, complete any verification manually. This app does not defeat site protections.
- If results are unrelated or fields are blank, inspect `debug_pages/` and update the original scraper's selectors only after inspecting the actual page DOM.


## فتح لوحة التحكم من الموبايل
التطبيق الحالي يعمل كمحرك على الكمبيوتر وواجهة ويب يمكن فتحها من الموبايل:
- شغّل `START-WINDOWS.bat`.
- اجعل الكمبيوتر والموبايل على نفس شبكة Wi-Fi.
- نفّذ `ipconfig` على الكمبيوتر وخذ قيمة `IPv4 Address` الخاصة بالـ Wi-Fi.
- افتح على الموبايل `http://IP-ADDRESS:8000` مع استبدال العنوان بعنوان جهازك.
- اسمح لـ Python عبر Windows Firewall على الشبكات الخاصة فقط عند ظهور التنبيه.
- لا تغلق الكمبيوتر أو نافذة التشغيل أثناء العمل.

هذا ليس APK مستقلاً؛ الكمبيوتر يشغّل Playwright والمتصفح. تحقق Cloudflare/CAPTCHA، عند ظهوره، يتم يدوياً على الكمبيوتر.


## Mobile-first website / installable PWA
The dashboard is now responsive and includes a PWA manifest/service worker. Once served over HTTPS from a deployed server, open the site on Android Chrome and choose **Add to Home screen** / **Install app**.

### Important hosting requirement
This is not a static-only website: the Python API and Playwright browser engine must run on an always-on server with persistent storage for the browser profile and result files. A static website host alone cannot run the scraper. The current scraper launches a visible Chromium window (`headless=False`) for manual login/verification, so a normal headless cloud deployment is not yet a complete mobile-only deployment. A VPS plus remote-browser/verification access is required to preserve the manual Cloudflare step; do not treat a deployment as tested until Dubizzle is tested from that server.

The dashboard can be installed like an app, but the backend must remain online. The original scraper's owner/broker classification is only a candidate signal, not proof of ownership.


## Railway cloud deployment
This repository includes a Dockerfile and railway.json for a first cloud deployment. Attach a Railway Volume at `/data` to persist the Playwright profile and generated data. The browser runs headless in Railway. This build does **not** provide a remote visible browser, so interactive login and manual CAPTCHA/Cloudflare completion are not available yet. Treat this as a backend deployment test, not a fully validated production scraper.
