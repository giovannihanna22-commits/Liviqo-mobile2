"""
Dubizzle Property Finder
-------------------------------------
Automates the tedious parts of finding owner-listed furnished properties
on Dubizzle. This script only FINDS and CLASSIFIES listings - it never
opens a chat, types a message, or contacts anyone. It hands you a clean,
photo-backed list of owner listings (with title, price, owner name and
phone number) so you can reach out yourself, however and whenever you
choose.

WHAT THIS DOES AUTOMATICALLY:
  - Loads your saved, filtered Dubizzle search (see SETUP below)
  - Pauses once at the start so you can clear any human-verification /
    bot-check screen by hand
  - Walks every page of results using the site's own "Next" pagination
    (not URL guessing), scrolling each page first in case it lazy-loads
  - Opens each listing and inspects its contact options
  - Classifies it as OWNER (has an internal Dubizzle Chat button) or
    BROKER (Call/Show Phone Number/WhatsApp only, no internal Chat)
  - For OWNER listings only: reads the title, price and owner name, and
    clicks the listing's "Show Phone Number" button to read the number
  - Grabs the listing's photos for the report
  - Tracks which listings it has already looked at, so re-runs don't
    redo work (owners saved without a number are re-checked)
  - Logs everything to a CSV file AND a live HTML report (one card per
    owner listing, with a "Copy all" button)

WHAT IT NEVER DOES:
  - It never opens the internal chat, types anything, or clicks Send.
    No message is ever composed or sent to anyone. The only thing it
    clicks on a listing is the "Show Phone Number" button.
  - It does not solve human-verification / captcha challenges. It
    pauses and waits for you to solve them by hand in the visible
    browser window.

SETUP (one-time):
  1. pip install playwright
     playwright install chromium
  2. Run this script once with RUN_LOGIN_ONLY = True (below). It opens
     a real browser window - log into your Dubizzle account there, and
     manually set your search location + Furnished = Yes filter exactly
     how you want it. Your login session is saved locally in
     ./dubizzle_profile, so you only do this once.
  3. Copy the resulting search-results page URL into SEARCH_URL below.
  4. Set RUN_LOGIN_ONLY = False and run again.

IMPORTANT - SELECTORS MAY NEED ADJUSTING:
  The CSS/text selectors for classification, title, price, owner name
  and the phone button are best-effort guesses based on typical Dubizzle
  markup; they can't be tested against the live site from where this was
  written. If a field comes back blank, open a listing, right-click the
  thing -> Inspect, and put a matching selector into the override hooks
  in CONFIG (CONTACT_CARD_SELECTOR, TITLE_SELECTOR, PRICE_SELECTOR,
  OWNER_NAME_SELECTOR, NEXT_BUTTON_SELECTOR). When details are missing the tool also saves a
  few listing pages into ./debug_pages so the cause can be diagnosed.

A NOTE ON TERMS OF SERVICE:
  Automated interaction with Dubizzle (scraping, scripted browsing) is
  very likely against its Terms of Service, and could get your account
  limited or banned if used heavily. Revealing phone numbers in bulk is
  the kind of activity most likely to be noticed - keep volumes
  reasonable and use the numbers responsibly.
"""

import csv
import hashlib
import json
import os
import mimetypes
import re
import secrets
import threading
import time
import webbrowser
from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from playwright.sync_api import sync_playwright

# ============ CONFIG - edit these ============
RUN_LOGIN_ONLY = False   # True the first time: just opens a browser for you to log in & set filters
SEARCH_URL = "https://www.dubizzle.com.eg/en/properties/apartments-duplex-for-rent/madinaty/q-furnished-apartments/"          # Paste your filtered search-results URL here after setup step 3
PROFILE_DIR = os.environ.get("LIVIQO_PROFILE_DIR", "/data/dubizzle_profile" if os.environ.get("RAILWAY_ENVIRONMENT") else "./dubizzle_profile")
PROCESSED_FILE = "./processed_listings.json"
MAX_LISTINGS = 1000     # safety cap so a run can't go forever unattended
MAX_PAGES = 500         # safety cap on how many result pages are walked
NEXT_BUTTON_SELECTOR = None    # e.g. "a[aria-label='Next']"; None = try a few common patterns
SCROLL_PAUSE_SECONDS = 3.5

# --- Human verification ---
# On first load, you'll always get a chance to clear a captcha/bot-check
# by hand (up to VERIFICATION_WAIT_SECONDS, but you can continue sooner
# by pressing Enter). The script also auto-detects common challenge
# pages before each new results page and pauses again if it sees one.
VERIFICATION_WAIT_SECONDS = 30

# --- Logging / review ---
LOG_CSV = "./outreach_log.csv"
REPORT_HTML = "./outreach_report.html"
PHOTOS_DIR = "./outreach_photos"   # local screenshot files the report's photos point to
OPEN_REPORT_AUTOMATICALLY = True   # opens REPORT_HTML in your default browser once, at the start

# --- Selector overrides (fill these in after inspecting the live page
# if the defaults misfire - see docstring above). None = use the built-in guesses. ---
CONTACT_CARD_SELECTOR = None   # e.g. "div[data-testid='contact-card']"; None = scan the whole listing page
TITLE_SELECTOR = None          # e.g. "h1"
PRICE_SELECTOR = None          # e.g. "[aria-label='Price']"
OWNER_NAME_SELECTOR = None     # element that holds the seller's name

# --- Troubleshooting ---
SAVE_DEBUG_HTML = True         # save a few listing pages whose details couldn't all be read
DEBUG_DIR = "./debug_pages"
DEBUG_MAX_FILES = 5
# ==============================================


# ---------- processed-listings tracking ----------

def load_processed():
    p = Path(PROCESSED_FILE)
    if p.exists():
        return set(json.loads(p.read_text()))
    return set()


def save_processed(processed):
    Path(PROCESSED_FILE).write_text(json.dumps(sorted(processed)))


# ---------- logging / HTML report ----------

# Guards the CSV / processed-file read-modify-write cycles, because the scraper
# thread and the report page's delete buttons can touch them at the same time.
_LOG_LOCK = threading.RLock()
_RUN_ACTIVE = threading.Event()   # set while run_scrape is running

LOG_FIELDS = [
    "timestamp", "listing_url", "classification", "action",
    "title", "owner_name", "price", "phone", "image_urls",
]


def read_log_rows():
    """All rows of the CSV log as dicts (older logs without the newer
    title/owner/price/phone columns still load - those keys just come
    back missing and are treated as empty)."""
    p = Path(LOG_CSV)
    if not p.exists():
        return []
    with open(p, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_log_rows(rows):
    """Rewrites the whole CSV with the current column layout. This is
    also what quietly upgrades an old log file to the new columns."""
    with open(LOG_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS, extrasaction="ignore", restval="")
        writer.writeheader()
        writer.writerows(rows)


def read_owner_rows():
    return [r for r in read_log_rows() if r.get("classification") == "owner"]


def log_result(listing_url, classification, action, image_urls=None, details=None):
    """
    Records one listing. If the listing is already in the log it is
    UPDATED in place (so re-checking an owner listing to fetch its
    phone number doesn't create a duplicate row). Returns the row.
    """
    details = details or {}
    new_row = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "listing_url": listing_url,
        "classification": classification,
        "action": action,
        "title": details.get("title", ""),
        "owner_name": details.get("owner_name", ""),
        "price": details.get("price", ""),
        "phone": details.get("phone", ""),
        "image_urls": json.dumps(image_urls or []),
    }

    with _LOG_LOCK:
        rows = read_log_rows()
        for i, old in enumerate(rows):
            if old.get("listing_url") == listing_url:
                # keep anything we found last time that this pass didn't
                for key in ("title", "owner_name", "price", "phone"):
                    if not new_row[key] and old.get(key):
                        new_row[key] = old[key]
                rows[i] = new_row
                break
        else:
            rows.append(new_row)

        write_log_rows(rows)
        write_html_report()
    return new_row


def format_copy_text(rows):
    """
    The 'copy all' text: each listing's link, with its phone number on
    the line right under it, a blank line between listings.
    Listings without a number are left out (nothing to contact them
    with). Returns (text, copied_count, skipped_count).
    """
    blocks, skipped = [], 0
    for r in rows:
        link = (r.get("listing_url") or "").strip()
        phone = (r.get("phone") or "").strip()
        if not link:
            continue
        if not phone:
            skipped += 1
            continue
        blocks.append(f"{link}\n{phone}")
    return "\n\n".join(blocks), len(blocks), skipped


def _parse_images(raw):
    try:
        urls = json.loads(raw or "[]")
        return urls if isinstance(urls, list) else []
    except Exception:
        return []


def delete_owner_results(urls=None, forget=False):
    """
    Deletes OWNER rows from the log (broker rows are never touched) and
    rebuilds the report. urls=None means every owner row; otherwise only
    the owner rows whose listing_url is in `urls`.

    forget=False: the listings stay in the 'processed' list, so future
        runs will NOT bring them back (use this for "already contacted").
    forget=True: they are removed from 'processed' too, so the next run
        checks them afresh.

    Screenshot files made for the deleted listings are removed as well.
    Returns how many owner rows were removed.
    """
    with _LOG_LOCK:
        rows = read_log_rows()
        wanted = None if urls is None else set(urls)
        gone = [r for r in rows if r.get("classification") == "owner"
                and (wanted is None or r.get("listing_url") in wanted)]
        if not gone:
            return 0
        gone_ids = {id(r) for r in gone}
        gone_urls = {r.get("listing_url") for r in gone}
        keep = [r for r in rows if id(r) not in gone_ids]

        for r in gone:
            for u in _parse_images(r.get("image_urls")):
                if isinstance(u, str) and not u.startswith("http") and u.startswith(PHOTOS_DIR):
                    try:
                        Path(u).unlink(missing_ok=True)
                    except Exception:
                        pass

        write_log_rows(keep)
        processed = load_processed()
        if forget:
            processed -= gone_urls
        else:
            processed |= gone_urls
        save_processed(processed)
        write_html_report()
        return len(gone)


def clear_owner_results(forget=True):
    """Deletes every owner row (the app's 'Clear owner results...' button)."""
    return delete_owner_results(None, forget=forget)


REPORT_CSS = """
  :root { --owner: #1a7f37; --broker: #b35900; --error: #b30000; --muted: #6b7280; }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
    margin: 0; padding: 2rem clamp(1rem, 4vw, 3rem);
    background: #f4f5f7; color: #1a1a1a;
  }
  header h2 { margin: 0 0 0.25rem 0; font-size: 1.5rem; }
  header p { margin: 0 0 1rem 0; color: var(--muted); font-size: 14px; max-width: 70ch; }
  .toolbar { display: flex; align-items: center; gap: 12px; margin-bottom: 1.25rem; }
  .btn {
    background: #0b57d0; color: #fff; border: 0; border-radius: 8px;
    padding: 8px 14px; font-size: 14px; font-weight: 600; cursor: pointer;
  }
  .btn:hover { background: #0a49ad; }
  #copyMsg { color: var(--owner); font-size: 13px; font-weight: 600; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; }
  .card {
    background: #fff; border-radius: 12px; overflow: hidden;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08), 0 1px 2px rgba(0,0,0,0.06);
    display: flex; flex-direction: column;
    transition: transform 0.12s ease, box-shadow 0.12s ease;
  }
  .card:hover { transform: translateY(-2px); box-shadow: 0 6px 16px rgba(0,0,0,0.12); }
  .thumb {
    width: 100%; aspect-ratio: 4 / 3; background: #e9eaed;
    display: flex; align-items: center; justify-content: center;
    overflow: hidden; position: relative;
  }
  .thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .thumb__count {
    position: absolute; bottom: 8px; right: 8px;
    background: rgba(0,0,0,0.6); color: #fff; font-size: 11px;
    font-weight: 600; padding: 2px 8px; border-radius: 999px;
  }
  .thumb--empty span, .thumb--broken span { color: #9aa0a6; font-size: 13px; font-weight: 500; }
  .thumb--broken img { display: none; }
  .filmstrip { display: flex; gap: 4px; padding: 6px; overflow-x: auto; background: #f0f1f3; }
  .filmstrip img {
    width: 46px; height: 36px; object-fit: cover; border-radius: 5px;
    flex: 0 0 auto; cursor: pointer; opacity: 0.75;
    border: 2px solid transparent; transition: opacity 0.1s, border-color 0.1s;
  }
  .filmstrip img:hover { opacity: 1; border-color: #0b57d0; }
  .card__body { padding: 12px 14px 14px; display: flex; flex-direction: column; gap: 6px; }
  .card__meta { display: flex; align-items: center; justify-content: space-between; font-size: 12px; }
  .card__meta time { color: var(--muted); }
  .card__title {
    font-size: 15px; font-weight: 600; color: #1a1a1a; text-decoration: none;
    line-height: 1.35; overflow-wrap: anywhere;
  }
  .card__title:hover { text-decoration: underline; color: #0b57d0; }
  .card__price { font-size: 15px; font-weight: 700; color: var(--owner); }
  .card__row { font-size: 13px; display: flex; gap: 6px; }
  .card__row .lbl { color: var(--muted); min-width: 48px; }
  .card__row a { color: #0b57d0; text-decoration: none; font-weight: 600; }
  .card__row a:hover { text-decoration: underline; }
  .card__link { font-size: 11px; color: var(--muted); text-decoration: none; word-break: break-all; }
  .card__link:hover { text-decoration: underline; }
  .badge {
    display: inline-block; padding: 2px 9px; border-radius: 999px;
    font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.03em; color: #fff;
  }
  .badge--found { background: var(--owner); }
  .badge--skipped { background: var(--muted); }
  .badge--error { background: var(--error); }
  .empty-state { color: var(--muted); padding: 3rem 0; text-align: center; }
"""

REPORT_CSS_EXTRA = """
  .card { position: relative; }
  .pick-wrap {
    position: absolute; top: 8px; left: 8px; z-index: 2;
    background: rgba(255,255,255,0.92); border-radius: 6px; padding: 5px;
    display: flex; cursor: pointer; box-shadow: 0 1px 3px rgba(0,0,0,0.25);
  }
  .pick { width: 20px; height: 20px; margin: 0; cursor: pointer; accent-color: #b30000; }
  .card--picked { outline: 3px solid #b30000; outline-offset: -3px; }
  .toolbar { flex-wrap: wrap; }
  .selall { display: flex; align-items: center; gap: 6px; font-size: 14px; font-weight: 600; cursor: pointer; }
  .selall input { width: 18px; height: 18px; margin: 0; cursor: pointer; }
  .btn:disabled { background: #b8bdc7; cursor: not-allowed; }
  .btn--danger { background: #b30000; }
  .btn--danger:hover:not(:disabled) { background: #8f0000; }
  .btn--outline { background: #fff; color: #b30000; border: 2px solid #b30000; padding: 6px 12px; }
  .btn--outline:hover:not(:disabled) { background: #fdecec; }
  .btn--outline:disabled { background: #fff; color: #b8bdc7; border-color: #d5d8de; }
  .btn--ghost { background: #e9eaed; color: #1a1a1a; }
  .btn--ghost:hover { background: #d9dbe0; }
  .msg { color: var(--owner); font-size: 13px; font-weight: 600; }
  .msg--bad { color: var(--error); }
  .offline {
    background: #fff4e5; color: #8a4b00; border: 1px solid #f0c78a;
    border-radius: 8px; padding: 8px 12px; font-size: 13px; margin-bottom: 1rem; max-width: 70ch;
  }
  .modal[hidden], .empty-state[hidden], .offline[hidden], .modal__extra[hidden] { display: none; }
  .modal {
    position: fixed; inset: 0; background: rgba(0,0,0,0.45); z-index: 10;
    display: flex; align-items: center; justify-content: center; padding: 1rem;
  }
  .modal__box { background: #fff; border-radius: 12px; padding: 20px 22px; width: min(440px, 100%); box-shadow: 0 10px 40px rgba(0,0,0,0.3); }
  .modal__box h3 { margin: 0 0 8px 0; font-size: 18px; }
  .modal__box p { margin: 0 0 14px 0; font-size: 14px; line-height: 1.45; color: #333; }
  .modal__extra { display: flex; gap: 8px; align-items: flex-start; font-size: 13px; margin-bottom: 14px; cursor: pointer; }
  .modal__extra input { margin-top: 2px; }
  .modal__btns { display: flex; justify-content: flex-end; gap: 10px; }
"""

REPORT_JS = """
const API_TOKEN = "__REPORT_TOKEN__";
const LIVE = location.protocol === 'http:' || location.protocol === 'https:';
const $ = (id) => document.getElementById(id);
const cards = () => Array.from(document.querySelectorAll('.card'));
const selectedCards = () => cards().filter(c => c.querySelector('.pick').checked);

let msgTimer;
function flash(msg, bad) {
  const el = $('msg');
  el.textContent = msg;
  el.className = bad ? 'msg msg--bad' : 'msg';
  clearTimeout(msgTimer);
  msgTimer = setTimeout(() => { el.textContent = ''; }, 5000);
}

function refreshUI() {
  const all = cards(), sel = selectedCards();
  $('ownerCount').textContent = all.length;
  $('delSel').textContent = 'Delete selected (' + sel.length + ')';
  $('delSel').disabled = !LIVE || sel.length === 0;
  $('clearAll').disabled = !LIVE || all.length === 0;
  $('selAll').disabled = !LIVE || all.length === 0;
  $('selAll').checked = all.length > 0 && sel.length === all.length;
  $('emptyState').hidden = all.length > 0;
  $('offline').hidden = LIVE;
  all.forEach(c => c.classList.toggle('card--picked', c.querySelector('.pick').checked));
}

document.addEventListener('change', (e) => {
  if (e.target.classList && e.target.classList.contains('pick')) refreshUI();
});
$('selAll').addEventListener('change', (e) => {
  cards().forEach(c => { c.querySelector('.pick').checked = e.target.checked; });
  refreshUI();
});

function legacyCopy(text) {
  const ta = document.createElement('textarea');
  ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.appendChild(ta); ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch (e) {}
  document.body.removeChild(ta);
  return ok;
}
$('copyAll').addEventListener('click', async () => {
  const blocks = []; let skipped = 0;
  cards().reverse().forEach(c => {          // oldest first, same as before
    const url = c.dataset.url, phone = c.dataset.phone;
    if (!url) return;
    if (!phone) { skipped++; return; }
    blocks.push(url + '\\n' + phone);
  });
  if (!blocks.length) { flash('No owner listings with a phone number yet', true); return; }
  const text = blocks.join('\\n\\n');
  let ok = false;
  try { await navigator.clipboard.writeText(text); ok = true; }
  catch (e) { ok = legacyCopy(text); }
  flash(ok ? 'Copied ' + blocks.length + ' listing(s)' : 'Copy failed - select the text manually', !ok);
});

// ---- the "are you sure?" popup ----
function confirmModal(opts) {
  return new Promise((resolve) => {
    const modal = $('modal');
    $('mTitle').textContent = opts.title;
    $('mText').textContent = opts.text;
    $('mOk').textContent = opts.okLabel;
    $('mExtra').hidden = !opts.extraLabel;
    $('mExtraText').textContent = opts.extraLabel || '';
    $('mForget').checked = false;
    modal.hidden = false;
    $('mCancel').focus();                    // Cancel is the default, on purpose

    function finish(ok) {
      modal.hidden = true;
      const extra = $('mForget').checked;
      $('mOk').removeEventListener('click', onOk);
      $('mCancel').removeEventListener('click', onCancel);
      modal.removeEventListener('click', onBackdrop);
      document.removeEventListener('keydown', onKey);
      resolve({ ok: ok, extra: extra });
    }
    const onOk = () => finish(true);
    const onCancel = () => finish(false);
    const onBackdrop = (e) => { if (e.target === modal) finish(false); };
    const onKey = (e) => { if (e.key === 'Escape') finish(false); };
    $('mOk').addEventListener('click', onOk);
    $('mCancel').addEventListener('click', onCancel);
    modal.addEventListener('click', onBackdrop);
    document.addEventListener('keydown', onKey);
  });
}

async function deleteListings(targets, forget) {
  let res;
  try {
    res = await fetch('/api/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Report-Token': API_TOKEN },
      body: JSON.stringify({ urls: targets.map(c => c.dataset.url), forget: !!forget }),
    });
  } catch (e) {
    throw new Error("Can't reach the app - is it still running? Reopen this page with its Open report button.");
  }
  let data = {};
  try { data = await res.json(); } catch (e) {}
  if (!res.ok) throw new Error(data.error || ('The app answered with an error (' + res.status + ')'));
  targets.forEach(c => c.remove());
  refreshUI();
  flash('Deleted ' + data.removed + ' listing(s)');
}

$('delSel').addEventListener('click', async () => {
  const sel = selectedCards();
  if (!sel.length) return;
  const n = sel.length;
  const r = await confirmModal({
    title: 'Delete ' + n + ' selected listing' + (n === 1 ? '' : 's') + '?',
    text: 'They will be removed from this page and from the log. They are remembered as already handled, ' +
          'so future runs will not bring them back. This cannot be undone.',
    okLabel: 'Yes, delete ' + n,
  });
  if (!r.ok) return;
  try { await deleteListings(sel, false); } catch (e) { flash(e.message, true); }
});

$('clearAll').addEventListener('click', async () => {
  const all = cards();
  if (!all.length) return;
  const n = all.length;
  const r = await confirmModal({
    title: 'Clear ALL ' + n + ' owner listing' + (n === 1 ? '' : 's') + '?',
    text: 'Every owner listing shown on this page will be permanently deleted. This cannot be undone.',
    extraLabel: 'Also forget them, so the next run checks these listings again (leave unticked if you already contacted them)',
    okLabel: 'Yes, clear all ' + n,
  });
  if (!r.ok) return;
  try { await deleteListings(all, r.extra); } catch (e) { flash(e.message, true); }
});

refreshUI();
"""


def _photo_block(image_urls, card_idx):
    if not image_urls:
        return '<div class="thumb thumb--empty"><span>No photo found</span></div>'

    main_id = f"main-{card_idx}"
    safe_main = escape(image_urls[0], quote=True)
    main_img = (
        f'<div class="thumb">'
        f'<img id="{main_id}" src="{safe_main}" alt="Listing photo" loading="lazy" '
        f'onerror="this.closest(\'.thumb\').classList.add(\'thumb--broken\')">'
        f'<span class="thumb__count">{len(image_urls)} photo{"s" if len(image_urls) != 1 else ""}</span>'
        f'</div>'
    )
    if len(image_urls) == 1:
        return main_img

    strip_items = "\n".join(
        f'<img src="{escape(u, quote=True)}" alt="" loading="lazy" '
        f'onclick="document.getElementById(\'{main_id}\').src=this.src" '
        f'onerror="this.style.display=\'none\'">'
        for u in image_urls[:10]
    )
    return main_img + f'<div class="filmstrip">{strip_items}</div>'


def write_html_report():
    """
    Rewritten after every listing. Only OWNER listings are shown
    (brokers stay in outreach_log.csv). Each card shows the listing's
    title, price, owner name and phone number, plus every photo found,
    and a checkbox. When the page is opened through the app's local
    report server (the 'Open report' button) you can tick listings and
    delete them, or clear everything - each behind an 'are you sure?'
    popup. Nothing here contacts anyone.
    """
    with _LOG_LOCK:
        rows = read_log_rows()
        owner_rows = [r for r in rows if r.get("classification") == "owner"]
        skipped = sum(1 for r in owner_rows if not (r.get("phone") or "").strip())

        def card(idx, r):
            url = r.get("listing_url", "")
            title = (r.get("title") or "").strip() or url
            price = (r.get("price") or "").strip()
            owner = (r.get("owner_name") or "").strip()
            phone = (r.get("phone") or "").strip()
            tel = re.sub(r"[^\d+]", "", phone)

            price_html = f'<div class="card__price">{escape(price)}</div>' if price else ""
            owner_html = (
                f'<div class="card__row"><span class="lbl">Owner</span><span>{escape(owner)}</span></div>'
                if owner else ""
            )
            phone_html = (
                f'<div class="card__row"><span class="lbl">Phone</span>'
                f'<a href="tel:{escape(tel, quote=True)}">{escape(phone)}</a></div>'
                if phone else
                '<div class="card__row"><span class="lbl">Phone</span><span>not found</span></div>'
            )
            return f"""<article class="card" data-url="{escape(url, quote=True)}" data-phone="{escape(phone, quote=True)}">
  <label class="pick-wrap" title="Select this listing"><input type="checkbox" class="pick" aria-label="Select this listing"></label>
  {_photo_block(_parse_images(r.get('image_urls')), idx)}
  <div class="card__body">
    <div class="card__meta">
      <span class="badge badge--{escape(r.get('action', 'found'))}">{escape(r.get('action', 'found'))}</span>
      <time>{escape(r.get('timestamp', ''))}</time>
    </div>
    <a class="card__title" href="{escape(url, quote=True)}" target="_blank" rel="noopener">{escape(title)}</a>
    {price_html}
    {owner_html}
    {phone_html}
    <a class="card__link" href="{escape(url, quote=True)}" target="_blank" rel="noopener">{escape(url)}</a>
  </div>
</article>"""

        cards = "\n".join(card(idx, r) for idx, r in enumerate(reversed(owner_rows)))
        note = f" {skipped} without a phone number are left out of the copy." if skipped else ""

        html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Dubizzle Outreach Log</title>
<style>{REPORT_CSS}{REPORT_CSS_EXTRA}</style>
</head>
<body>
<header>
  <h2>Dubizzle Outreach Log</h2>
  <p><span id="ownerCount">{len(owner_rows)}</span> owner listings shown. Brokers are still recorded
  in outreach_log.csv, just filtered out of this view. This page is rewritten after every
  listing - refresh to see progress. Tick listings you have already contacted and delete them.{escape(note)}</p>
  <div class="offline" id="offline" hidden>Selecting and deleting are switched off because this page was
  opened as a plain file. Open it with the <b>Open report</b> button in the app to turn them on.</div>
  <div class="toolbar">
    <label class="selall"><input type="checkbox" id="selAll"> Select all</label>
    <button class="btn" id="copyAll" type="button">Copy all (link + number)</button>
    <button class="btn btn--danger" id="delSel" type="button" disabled>Delete selected (0)</button>
    <button class="btn btn--outline" id="clearAll" type="button">Clear all...</button>
    <span id="msg" class="msg"></span>
  </div>
</header>
<div class="grid">{cards}</div>
<div class="empty-state" id="emptyState"{" hidden" if owner_rows else ""}>No owner listings logged yet.</div>

<div class="modal" id="modal" hidden>
  <div class="modal__box" role="alertdialog" aria-modal="true" aria-labelledby="mTitle" aria-describedby="mText">
    <h3 id="mTitle"></h3>
    <p id="mText"></p>
    <label class="modal__extra" id="mExtra" hidden><input type="checkbox" id="mForget"><span id="mExtraText"></span></label>
    <div class="modal__btns">
      <button class="btn btn--ghost" id="mCancel" type="button">Cancel</button>
      <button class="btn btn--danger" id="mOk" type="button">Delete</button>
    </div>
  </div>
</div>
<script>{REPORT_JS}</script>
</body>
</html>"""
        Path(REPORT_HTML).write_text(html, encoding="utf-8")


# ---------- local report server (makes the report's delete buttons work) ----------
#
# A plain .html file can't edit your CSV, so the app serves the report from a
# tiny web server that only listens on THIS computer (127.0.0.1). Deleting is
# protected by a random per-run token plus a Host check, so other websites
# open in your browser can't poke it.

REPORT_HOST = "127.0.0.1"
REPORT_PORT = 8765          # falls back to any free port if this one is taken
_REPORT_TOKEN = secrets.token_urlsafe(24)
_report_server = None
_report_server_lock = threading.Lock()


class _ReportHandler(BaseHTTPRequestHandler):
    server_version = "DubizzleReport"

    def log_message(self, *args):      # keep the terminal quiet
        pass

    def _host_ok(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost")

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json; charset=utf-8")

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, "Forbidden", "text/plain")
        path = unquote(self.path.split("?", 1)[0])
        if path in ("/", "/" + Path(REPORT_HTML).name):
            with _LOG_LOCK:
                write_html_report()
                page = Path(REPORT_HTML).read_text(encoding="utf-8")
            return self._send(200, page.replace("__REPORT_TOKEN__", _REPORT_TOKEN))
        prefix = "/" + Path(PHOTOS_DIR).name + "/"
        if path.startswith(prefix):
            root = Path(PHOTOS_DIR).resolve()
            target = (root / path[len(prefix):]).resolve()
            if root in target.parents and target.is_file():
                ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
                return self._send(200, target.read_bytes(), ctype)
        self._send(404, "Not found", "text/plain")

    def do_POST(self):
        if not self._host_ok():
            return self._json(403, {"error": "Forbidden"})
        if self.path != "/api/delete":
            return self._json(404, {"error": "Not found"})
        sent = (self.headers.get("X-Report-Token") or "").encode("utf-8")
        if not secrets.compare_digest(sent, _REPORT_TOKEN.encode("utf-8")):
            return self._json(403, {"error": "This page is out of date - reload it and try again."})
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._json(400, {"error": "Bad request"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 5_000_000:
                return self._json(413, {"error": "Request too large"})
            data = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._json(400, {"error": "Bad request"})
        urls = data.get("urls")
        if not isinstance(urls, list) or not urls or not all(isinstance(u, str) for u in urls):
            return self._json(400, {"error": "Nothing to delete"})
        forget = bool(data.get("forget"))
        if forget and _RUN_ACTIVE.is_set():
            return self._json(409, {"error": "A run is in progress. Stop it first, or untick "
                                             "\"forget\" (deleting without it is always safe)."})
        removed = delete_owner_results(urls, forget=forget)
        self._json(200, {"removed": removed})


def start_report_server():
    """Starts the local report server once (safe to call repeatedly) and
    returns its URL, or None if it couldn't start."""
    global _report_server
    with _report_server_lock:
        if _report_server is None:
            srv = None
            for port in (REPORT_PORT, 0):
                try:
                    srv = ThreadingHTTPServer((REPORT_HOST, port), _ReportHandler)
                    break
                except OSError:
                    srv = None
            if srv is None:
                return None
            srv.daemon_threads = True
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            _report_server = srv
        return f"http://{REPORT_HOST}:{_report_server.server_address[1]}/"


def open_report_in_browser():
    """Opens the report through the local server (so delete works). Falls
    back to opening the plain file if the server can't start."""
    url = start_report_server()
    if url:
        webbrowser.open(url)
        return url
    webbrowser.open(Path(REPORT_HTML).resolve().as_uri())
    return None


# ---------- human verification ----------

def wait_for_human_check(page, label, first_time=False, on_wait=None):
    """
    Dubizzle (and the CDN in front of it) occasionally shows a
    human-verification / bot challenge (Cloudflare, reCAPTCHA, etc.)
    on first load. We can't solve these programmatically, so on the
    very first page load only, we pause so you can solve it by hand.

    on_wait, when given (the GUI passes one), is called instead of the
    terminal countdown/input below - it's the GUI's job to show a
    "Continue" button, block until it's clicked, then return. Running
    from the terminal (on_wait=None) falls back to the original
    countdown-or-press-Enter behavior.

    NOTE: this is intentionally a one-shot pause, not a mid-run
    detector. Content-based challenge detection was tried before and
    produced false positives on ordinary listing/search pages
    (e.g. pages that happen to mention "captcha" or similar wording
    in unrelated text), which would silently derail an otherwise fine
    run. If you hit a real challenge mid-run, the safest fix is to
    stop the run, clear it manually in the visible browser window,
    then start again (already-processed listings are skipped).
    """
    if not first_time:
        return

    if on_wait is not None:
        on_wait(label)
        safe_wait_for_load(page)
        return

    print(f"\nLoaded {label}. If a human-verification challenge is showing in the")
    print(f"browser window, solve it now - you have up to {VERIFICATION_WAIT_SECONDS}s,")
    print("or just press Enter as soon as you're ready to continue.")
    _countdown_or_enter(VERIFICATION_WAIT_SECONDS)
    safe_wait_for_load(page)


def safe_wait_for_load(page, timeout_ms=15000):
    """
    Waits for the page to finish loading. Deliberately does NOT use
    Playwright's 'networkidle' state - ad-heavy sites like Dubizzle keep
    background requests (ads, analytics, chat widgets) running more or
    less forever, so 'networkidle' can sit and time out even once the
    page is fully usable. 'domcontentloaded' plus a short fixed settle
    pause is slower to "prove" the page is ready but far more reliable
    in practice, and this never raises - a slow/odd page just gets the
    fixed pause instead of crashing the run.
    """
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except Exception:
        pass
    page.wait_for_timeout(1500)


def _countdown_or_enter(seconds):
    """Waits up to `seconds`, but returns immediately if Enter is pressed."""
    import sys

    if sys.platform.startswith("win"):
        # select() doesn't support stdin on Windows, so there's no reliable
        # way to show a live countdown that's also interruptible by Enter.
        # Just block on Enter instead - you can still take as long as you need.
        input(f"(no live countdown on Windows - press Enter once you're ready, "
              f"up to ~{seconds}s is plenty of time)... ")
        return

    import select

    print(f"(waiting up to {seconds}s - press Enter to continue sooner)")
    remaining = seconds
    while remaining > 0:
        ready, _, _ = select.select([sys.stdin], [], [], 1)
        if ready:
            sys.stdin.readline()
            return
        remaining -= 1


# ---------- classification ----------

PHONE_PATTERNS = ["call", "show phone", "show number", "phone number"]
WHATSAPP_PATTERNS = ["whatsapp", "wa.me"]
CHAT_PATTERNS = ["chat"]
CHAT_EXCLUDE_PATTERNS = ["support", "help center", "customer service", "live chat support"]

# Whole-word match for CHAT_PATTERNS, so e.g. the site's own top-nav
# "Chats" (inbox) link does NOT match "chat" as a substring the way
# `"chat" in "chats"` would. This was misclassifying every listing as
# "owner", because that nav link is present on every single page,
# listing content aside.
_CHAT_PATTERN_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(p) for p in CHAT_PATTERNS) + r")\b"
)


def classify_listing(page):
    """
    Returns (kind, chat_element) where kind is 'owner', 'broker', or
    'unknown' based on the contact buttons present on a listing page.

    Based on the sample owner listing: owner listings show a
    "Show Phone Number" button plus a "Chat" button (Dubizzle's
    internal messaging). Broker/agent listings typically show
    Call/WhatsApp buttons only, with no internal Chat button.

    chat_element is the exact locator for the matched chat control
    when kind == 'owner', otherwise None. This script never clicks it -
    it's returned only in case a caller wants to know exactly which
    element triggered the 'owner' classification (e.g. for debugging
    a misclassification).

    Elements inside <header>, <nav>, <footer>, or anything with
    role="navigation" are skipped entirely - site-wide chrome (like a
    "Chats" inbox link in the top bar) should never count as a
    listing's own contact option, no matter its wording.

    If CONTACT_CARD_SELECTOR is set, only elements inside that
    container are checked - tighter and less likely to pick up an
    unrelated "chat" widget elsewhere on the page (e.g. a support
    bubble). Otherwise the whole page is scanned with exclusions
    for common support-widget wording.
    """
    scope = page.locator(CONTACT_CARD_SELECTOR) if CONTACT_CARD_SELECTOR else page
    elements = scope.locator("a, button")

    has_call = False
    has_whatsapp = False
    chat_element = None

    count = elements.count()
    for i in range(count):
        el = elements.nth(i)

        try:
            if el.evaluate(
                "el => !!el.closest('header, nav, footer, [role=\"navigation\"]')"
            ):
                continue
        except Exception:
            pass

        try:
            text = (el.inner_text() or "").strip().lower()
        except Exception:
            text = ""
        try:
            href = (el.get_attribute("href") or "").lower()
        except Exception:
            href = ""

        if not text and not href:
            continue

        if any(p in href for p in WHATSAPP_PATTERNS) or any(p in text for p in WHATSAPP_PATTERNS):
            has_whatsapp = True
            continue

        if href.startswith("tel:") or any(p in text for p in PHONE_PATTERNS):
            has_call = True
            continue

        if _CHAT_PATTERN_RE.search(text) and not any(p in text for p in CHAT_EXCLUDE_PATTERNS):
            chat_element = el
            continue

    if chat_element is not None:
        return "owner", chat_element
    elif has_call or has_whatsapp:
        return "broker", None
    return "unknown", None


_BAD_IMAGE_HINTS = (
    "logo", "icon", "sprite", "avatar", "placeholder", "favicon",
    "badge", "pixel.", "spacer", "blank.gif", "1x1",
)


def _looks_like_real_photo(src, width=None, height=None):
    """
    Filters out the site chrome (logos, icons, ad pixels, avatar
    placeholders) that a generic "first <img> on the page" search
    tends to pick up before it ever reaches an actual listing photo.
    """
    if not src or src.startswith("data:"):
        return False
    low = src.lower()
    if any(hint in low for hint in _BAD_IMAGE_HINTS):
        return False
    if not low.startswith("http"):
        return False
    try:
        if width is not None and height is not None:
            w, h = int(width), int(height)
            if w and h and (w < 80 or h < 80):
                return False
    except (TypeError, ValueError):
        pass
    return True


def _best_url_from_srcset(srcset):
    """Picks the largest/last candidate out of a srcset attribute."""
    if not srcset:
        return ""
    candidates = [c.strip().split(" ")[0] for c in srcset.split(",") if c.strip()]
    return candidates[-1] if candidates else ""


_LISTING_ID_RE = re.compile(r"[-/]ID(\d{5,})\.html", re.IGNORECASE)


def _extract_listing_id(listing_url):
    """Pulls the numeric ad ID out of a Dubizzle listing URL, e.g.
    '...-ID209070953.html' -> '209070953'. Used to scope JSON-based
    photo extraction to just this ad, not the whole page's data (which
    also carries "similar listings" / "you may also like" ads)."""
    m = _LISTING_ID_RE.search(listing_url or "")
    return m.group(1) if m else None


def get_listing_images(page, listing_url="", max_images=12):
    """
    Grabs every usable photo URL for the listing, for a proper gallery
    in the HTML report - not just one thumbnail. Tries several sources
    and merges what they find, since no single one is reliable across
    every listing template:

      1. Embedded page data: Dubizzle's page is a React/Next.js app, so
         the full listing payload (including every photo URL) is often
         sitting in a <script id="__NEXT_DATA__"> block, already loaded
         with the page - no lazy-loading or gallery-click needed to see
         it. IMPORTANT: this blob also carries data for unrelated
         "similar listings" carousels elsewhere on the same page, so
         this is scoped to just the JSON sub-tree whose own id field
         matches this ad's ID (pulled from the URL) - otherwise photos
         from completely different apartments end up mixed into one
         listing's gallery. If the ad's ID can't be found in the URL,
         or can't be matched inside the JSON, this step is skipped
         entirely rather than falling back to an unscoped whole-page
         scan, since that's exactly what causes the mixing.
      2. JSON-LD structured data (schema.org "image" field) - this
         describes only the page's one primary entity by convention,
         so it doesn't need the same ID-matching scope as __NEXT_DATA__.
      3. og:image / twitter:image meta tags (usually just one photo,
         but a solid fallback if the embedded-data search finds none).
      4. The visible photo gallery in the DOM - scrolled into view
         first, since gallery images are frequently lazy-loaded and
         have no real src until they've been scrolled to. Scoped to
         the FIRST matching gallery-like container only (not every
         element on the page matching that selector), for the same
         "don't mix in a different section's photos" reason as above.
      5. Any other sizeable <img> on the page, as a last resort.

    Returns a de-duplicated list of up to `max_images` URLs, in the
    order found (empty list if nothing usable is found - never raises).
    """
    found = []
    seen = set()

    def add(url):
        url = (url or "").strip()
        if url and url not in seen and _looks_like_real_photo(url):
            seen.add(url)
            found.append(url)

    listing_id = _extract_listing_id(listing_url)

    # 1. __NEXT_DATA__ (or similar) embedded JSON blob - scoped to this ad only
    if listing_id:
        try:
            scripts = page.locator("script#__NEXT_DATA__, script[type='application/json']")
            count = min(scripts.count(), 5)
            for i in range(count):
                try:
                    raw = scripts.nth(i).inner_text()
                    data = json.loads(raw)
                except Exception:
                    continue
                own_node = _find_own_ad_node(data, listing_id)
                if own_node is not None:
                    _walk_json_for_images(own_node, add)
                if len(found) >= max_images:
                    return found[:max_images]
        except Exception:
            pass

    # 2. JSON-LD structured data (schema.org "image" field) - describes
    # only the page's main entity by convention, no ID-scoping needed.
    try:
        scripts = page.locator("script[type='application/ld+json']")
        count = scripts.count()
        for i in range(count):
            try:
                raw = scripts.nth(i).inner_text()
                data = json.loads(raw)
            except Exception:
                continue
            for item in (data if isinstance(data, list) else [data]):
                if not isinstance(item, dict):
                    continue
                img = item.get("image")
                if isinstance(img, str):
                    add(img)
                elif isinstance(img, list):
                    for u in img:
                        if isinstance(u, str):
                            add(u)
    except Exception:
        pass

    if len(found) >= max_images:
        return found[:max_images]

    # 3. Meta tags
    try:
        for meta_prop in ("meta[property='og:image']", "meta[name='twitter:image']"):
            tag = page.locator(meta_prop).first
            if tag.count() > 0:
                add(tag.get_attribute("content"))
    except Exception:
        pass

    # 4. Visible gallery, after nudging the page so lazy images resolve.
    # Scoped to the first matching container only, so a "similar
    # listings" carousel elsewhere on the page (which can easily match
    # the same broad [class*='gallery']-style selector) never gets its
    # images merged in with this listing's own gallery.
    try:
        page.mouse.wheel(0, 700)
        page.wait_for_timeout(600)
        page.mouse.wheel(0, -700)
        page.wait_for_timeout(300)
    except Exception:
        pass

    gallery_container_selectors = [
        "[data-testid*='gallery']",
        "[data-testid*='image']",
        "[data-testid*='photo']",
        "[class*='gallery']",
        "[class*='carousel']",
        "[class*='slider']",
        "[class*='swiper']",
    ]
    for sel in gallery_container_selectors:
        try:
            container = page.locator(sel).first
            if container.count() == 0 or not container.is_visible():
                continue
            images = container.locator("img")
            count = min(images.count(), max_images)
            if count == 0:
                continue
            for i in range(count):
                img = images.nth(i)
                for attr in ("src", "data-src", "data-lazy-src", "data-original"):
                    add(img.get_attribute(attr))
                add(_best_url_from_srcset(img.get_attribute("srcset") or img.get_attribute("data-srcset")))
            # Found a working gallery container - stop here rather than
            # also checking the remaining selectors, which is exactly
            # what let a second, unrelated container's photos leak in.
            if found:
                return found[:max_images]
        except Exception:
            continue

    if found:
        return found[:max_images]

    # 5. Last resort: any sizeable image anywhere on the page
    try:
        images = page.locator("img")
        count = min(images.count(), 60)
        for i in range(count):
            img = images.nth(i)
            for attr in ("src", "data-src"):
                src = (img.get_attribute(attr) or "").strip()
                if _looks_like_real_photo(src, img.get_attribute("width"), img.get_attribute("height")):
                    add(src)
                    break
            if len(found) >= max_images:
                break
    except Exception:
        pass

    return found[:max_images]


_IMAGE_EXT_RE = re.compile(r"\.(jpg|jpeg|png|webp)(\?|$)", re.IGNORECASE)


def _find_own_ad_node(obj, listing_id, _depth=0):
    """
    Searches a parsed JSON blob for the specific dict that represents
    THIS ad, identified by an id-like field whose value matches
    listing_id - and returns that dict (so callers can then pull
    images from just its subtree). Checks a node's own keys before
    descending into children (depth-first), so it returns the
    outermost/most complete matching node rather than some deeply
    nested duplicate of the same id.

    This existing purely to avoid what a plain "find every image
    string in the whole JSON tree" scan does: on a page that also
    embeds data for "similar listings" carousels, that pulls in
    photos from several different, unrelated apartments into one
    listing's gallery. Scoping to the one node whose own id matches
    the ad in the URL keeps it to just this listing's photos.
    """
    if _depth > 20:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (str, int)) and "id" in k.lower() and str(v) == listing_id:
                return obj
        for v in obj.values():
            found = _find_own_ad_node(v, listing_id, _depth + 1)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_own_ad_node(item, listing_id, _depth + 1)
            if found is not None:
                return found
    return None


def _walk_json_for_images(obj, add_fn, _depth=0):
    """
    Recursively scans a parsed JSON blob for strings that look like
    photo URLs and hands each one to add_fn. Used against Next.js's
    __NEXT_DATA__ payload, which nests the listing's photo list
    somewhere inside a big tree of props - the exact key path isn't
    worth hard-coding since it shifts with site updates, so this just
    finds every image-shaped URL in the whole tree.
    """
    if _depth > 12:
        return
    if isinstance(obj, dict):
        for v in obj.values():
            _walk_json_for_images(v, add_fn, _depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _walk_json_for_images(item, add_fn, _depth + 1)
    elif isinstance(obj, str):
        if obj.startswith("http") and _IMAGE_EXT_RE.search(obj):
            add_fn(obj)


def capture_listing_photo(page, listing_url):
    """
    Guaranteed fallback for when URL-based extraction finds nothing
    (e.g. this site's markup/CDN patterns don't match anything
    get_listing_images() knows to look for). Instead of hunting for
    the "right" <img> src - which depends on guessing a moving-target
    site structure - this just takes an actual screenshot of whatever
    photo is rendered on screen and saves it as a local file. It
    can't be defeated by a class name or CDN URL changing, because it
    doesn't look at either.

    Tries to screenshot just the photo/gallery element for a clean
    crop; if none is found (or it's too small to be a real photo),
    falls back to a screenshot of the top of the page, since a
    listing's main photo is almost always the first thing shown.

    Returns a relative path (under PHOTOS_DIR, alongside REPORT_HTML)
    to use directly as an <img src=...>, or "" if even a screenshot
    couldn't be taken (e.g. the page errored out). Never raises.
    """
    try:
        Path(PHOTOS_DIR).mkdir(parents=True, exist_ok=True)
    except Exception:
        return ""

    fname = hashlib.md5(listing_url.encode("utf-8")).hexdigest() + ".jpg"
    full_path = Path(PHOTOS_DIR) / fname

    try:
        page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass
    page.wait_for_timeout(500)  # let images actually paint before capturing

    candidates = [
        "[data-testid*='gallery']", "[data-testid*='image']", "[data-testid*='photo']",
        "[class*='gallery']", "[class*='carousel']", "[class*='slider']", "[class*='swiper']",
        "picture",
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0 or not loc.is_visible():
                continue
            box = loc.bounding_box()
            if not box or box["width"] < 200 or box["height"] < 150:
                continue
            loc.screenshot(path=str(full_path), type="jpeg", quality=75)
            return f"{PHOTOS_DIR}/{fname}"
        except Exception:
            continue

    try:
        # Last resort: whatever's currently in the viewport. On a
        # freshly-loaded, scrolled-to-top listing page this is, in
        # practice, the photo gallery - real estate sites put it first.
        page.screenshot(path=str(full_path), type="jpeg", quality=75)
        return f"{PHOTOS_DIR}/{fname}"
    except Exception:
        return ""


# ---------- listing details (title / price / owner / phone) ----------

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def _clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def _first_text(page, selectors, must_have=None):
    """First non-empty visible text among the given selectors (checks the
    first few matches of each). must_have: optional regex the text has to
    contain, e.g. a digit for prices."""
    for sel in selectors:
        if not sel:
            continue
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 5)):
                el = loc.nth(i)
                if not el.is_visible():
                    continue
                text = _clean(el.inner_text())
                if text and (must_have is None or re.search(must_have, text)):
                    return text
        except Exception:
            continue
    return ""


def _load_json_blobs(page, limit=6):
    blobs = []
    try:
        scripts = page.locator("script#__NEXT_DATA__, script[type='application/json']")
        for i in range(min(scripts.count(), limit)):
            try:
                blobs.append(json.loads(scripts.nth(i).text_content() or ""))
            except Exception:
                continue
    except Exception:
        pass
    return blobs


def _load_jsonld(page):
    items = []
    try:
        scripts = page.locator("script[type='application/ld+json']")
        for i in range(scripts.count()):
            try:
                data = json.loads(scripts.nth(i).text_content() or "")
            except Exception:
                continue
            items.extend(data if isinstance(data, list) else [data])
    except Exception:
        pass
    return [d for d in items if isinstance(d, dict)]


_OWNER_PARENT_KEYS = {
    "contactinfo", "contact_info", "seller", "user", "owner",
    "agent", "advertiser", "poster", "agency",
}
_NAME_KEYS = ("name", "fullName", "full_name", "displayName", "display_name", "userName")


def _find_owner_name_in_json(node, depth=0):
    """Looks for {seller/user/owner/contactInfo...: {name: ...}} inside THIS
    ad's own JSON node only (never the whole page's data - that can contain
    the logged-in account's own name)."""
    if depth > 14:
        return ""
    if isinstance(node, dict):
        for k, v in node.items():
            if str(k).lower() in _OWNER_PARENT_KEYS and isinstance(v, dict):
                for nk in _NAME_KEYS:
                    val = v.get(nk)
                    if isinstance(val, str) and _clean(val):
                        return _clean(val)
        for v in node.values():
            found = _find_owner_name_in_json(v, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_owner_name_in_json(item, depth + 1)
            if found:
                return found
    return ""


def _find_in_json(node, key_names, depth=0):
    """First value stored under any of key_names (case-insensitive), searched
    depth-first through a parsed JSON node."""
    if depth > 10:
        return None
    wanted = {k.lower() for k in key_names}
    if isinstance(node, dict):
        for k, v in node.items():
            if str(k).lower() in wanted and v not in (None, "", [], {}):
                return v
        for v in node.values():
            found = _find_in_json(v, key_names, depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_in_json(item, key_names, depth + 1)
            if found is not None:
                return found
    return None


_GENERIC_NAME_LINES = re.compile(
    r"^(posted by|listed by|seller|owner|advertiser|member since|view profile|see all|"
    r"all ads|ads?$|call|chat|show|whatsapp|rating|verified|message|reply|report|share|save|follow)",
    re.IGNORECASE,
)


def _pick_name_from_text(text):
    for line in (text or "").splitlines():
        line = _clean(line)
        if not line or len(line) > 60 or not re.search(r"[^\W\d_]", line):
            continue
        if _GENERIC_NAME_LINES.match(line):
            continue
        return line
    return ""


def _slug_title(listing_url):
    """Last-resort title built from the URL slug, e.g.
    '.../ad/furnished-studio-new-cairo-ID123.html' -> 'Furnished studio new cairo'."""
    try:
        slug = unquote(listing_url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1])
        slug = re.sub(r"[-_]?ID\d+\.html$", "", slug, flags=re.IGNORECASE)
        slug = re.sub(r"\.html$", "", slug, flags=re.IGNORECASE)
        title = _clean(slug.replace("-", " ").replace("_", " "))
        return title[:1].upper() + title[1:] if title else ""
    except Exception:
        return ""


def _price_from_json(value, currency="EGP"):
    if isinstance(value, dict):
        for k in ("formatted", "formattedPrice", "display", "value", "amount", "price"):
            if k in value:
                return _price_from_json(value[k], currency)
        return ""
    if isinstance(value, (int, float)):
        return f"{currency} {value:,.0f}"
    if isinstance(value, str) and re.search(r"\d", value):
        return _clean(value)
    return ""


def get_listing_details(page, listing_url):
    """
    Reads title, price and owner name from an open listing page. Every
    field tries several sources and falls back to the next, because the
    page markup can't be checked live from here: the on-screen text
    first (what you'd see), then the page's embedded data scoped to THIS
    ad, then meta tags / the URL. TITLE_SELECTOR, PRICE_SELECTOR and
    OWNER_NAME_SELECTOR (config at the top) override the guesses.
    Returns {"title", "price", "owner_name"} - blank when not found.
    """
    listing_id = _extract_listing_id(listing_url)
    own_nodes = []
    if listing_id:
        for blob in _load_json_blobs(page):
            node = _find_own_ad_node(blob, listing_id)
            if node is not None:
                own_nodes.append(node)
    jsonld = _load_jsonld(page)

    # ---- title ----
    title = _first_text(page, [TITLE_SELECTOR] if TITLE_SELECTOR else ["h1"])
    if not title:
        try:
            og = page.locator("meta[property='og:title']").first
            if og.count() > 0:
                title = _clean(og.get_attribute("content"))
                title = re.sub(r"\s*[|\-–]\s*dubizzle.*$", "", title, flags=re.IGNORECASE)
        except Exception:
            pass
    if not title:
        for item in jsonld:
            if isinstance(item.get("name"), str) and _clean(item["name"]):
                title = _clean(item["name"])
                break
    if not title:
        for node in own_nodes:
            val = _find_in_json(node, ["title", "name"])
            if isinstance(val, str) and _clean(val):
                title = _clean(val)
                break
    if not title:
        title = _slug_title(listing_url)

    # ---- price ----
    price_selectors = [PRICE_SELECTOR] if PRICE_SELECTOR else [
        "[aria-label='Price']", "[data-testid*='price' i]", "[class*='price' i]",
    ]
    price = _first_text(page, price_selectors, must_have=r"\d")
    if not price:
        for item in jsonld:
            offers = item.get("offers")
            if isinstance(offers, list) and offers:
                offers = offers[0]
            if isinstance(offers, dict) and offers.get("price") not in (None, ""):
                price = _price_from_json(offers.get("price"), offers.get("priceCurrency") or "EGP")
                if price:
                    break
    if not price:
        for node in own_nodes:
            val = _find_in_json(node, ["formattedPrice", "formatted_price", "price"])
            price = _price_from_json(val) if val is not None else ""
            if price:
                break
    if not price:
        try:
            body = page.inner_text("body")
            m = re.search(r"(?:EGP|E£|LE)\s*[\d,]{3,}(?:\s*/\s*\w+)?|[\d,]{3,}\s*EGP", body)
            if m:
                price = _clean(m.group(0))
        except Exception:
            pass

    # ---- owner name ----
    owner = ""
    if OWNER_NAME_SELECTOR:
        owner = _pick_name_from_text(_first_text(page, [OWNER_NAME_SELECTOR]))
    if not owner:
        for node in own_nodes:
            owner = _find_owner_name_in_json(node)
            if owner:
                break
    if not owner:
        for sel in (
            "[data-testid*='seller' i]", "[data-testid*='poster' i]", "[data-testid*='owner' i]",
            "[data-testid*='agent' i]", "[data-testid*='user-name' i]", "[data-testid*='profile' i]",
            "[class*='seller' i]", "[class*='posted-by' i]",
        ):
            try:
                loc = page.locator(sel)
                for i in range(min(loc.count(), 3)):
                    el = loc.nth(i)
                    if el.evaluate("el => !!el.closest('header, nav, footer')"):
                        continue
                    owner = _pick_name_from_text(el.inner_text())
                    if owner:
                        break
            except Exception:
                continue
            if owner:
                break
    if not owner:
        try:
            body = page.inner_text("body")
            m = re.search(r"(?:Posted by|Listed by|Advertiser|Seller)\s*[:\-]?\s*\n?\s*([^\n]{2,60})", body, re.IGNORECASE)
            if m:
                owner = _pick_name_from_text(m.group(1))
        except Exception:
            pass

    return {"title": title, "price": price, "owner_name": owner}


# ---------- phone number reveal ----------

_PHONE_CANDIDATE_RE = re.compile(r"\+?\d[\d\s\-().]{7,18}\d")
_EG_MOBILE_RE = re.compile(r"(?<!\d)(?:\+?20|0)?1[0125]\d{8}(?!\d)")
_REVEAL_PATTERNS = ("show phone", "show number", "show contact", "phone number", "reveal")


def _normalize_phone(raw):
    s = (raw or "").translate(_ARABIC_DIGITS).strip()
    digits = re.sub(r"\D", "", s)
    if not 10 <= len(digits) <= 13:
        return ""
    return ("+" if s.startswith("+") else "") + digits


def find_phone_in_text(text):
    """First phone-number-looking value in some on-screen text ('' if none)."""
    for m in _PHONE_CANDIDATE_RE.finditer((text or "").translate(_ARABIC_DIGITS)):
        phone = _normalize_phone(m.group(0))
        if phone:
            return phone
    return ""


def _phone_from_response_text(text):
    """Phone number inside a JSON/text network response. Stricter than the
    on-screen version (must look like an Egyptian number, or sit under a
    phone-ish key) because response bodies are full of long IDs/timestamps."""
    text = (text or "").translate(_ARABIC_DIGITS)
    m = re.search(r'"(?:phone|mobile|phone_?number|phoneNumber|contact_?number)[^"]*"\s*:\s*"([^"]+)"',
                  text, re.IGNORECASE)
    if m:
        phone = _normalize_phone(m.group(1))
        if phone:
            return phone
    m = _EG_MOBILE_RE.search(text)
    return m.group(0) if m else ""


def _phone_from_tel_links(page):
    try:
        hrefs = page.eval_on_selector_all(
            "a[href^='tel:']",
            "els => els.filter(e => !e.closest('header, nav, footer')).map(e => e.getAttribute('href'))",
        )
    except Exception:
        return ""
    for href in hrefs or []:
        phone = _normalize_phone(unquote((href or "")[4:]))
        if phone:
            return phone
    return ""


def make_response_collector(bucket):
    """Returns a handler for page.on('response', ...) that remembers only
    responses whose URL looks phone/contact related, in case the site
    delivers the number through an API call instead of the page text."""
    def handler(resp):
        try:
            low = resp.url.lower()
            if any(k in low for k in ("phone", "contact", "reveal")):
                bucket.append(resp)
        except Exception:
            pass
    return handler


# Phone numbers: find the "Show Phone Number" button, click it, then report
# only a number that APPEARED because of the click (so a hotline in an ad, a
# footer, or the description can never be mistaken for the owner's number).

_REVEAL_STRONG_RE = re.compile(r"show\s*(?:phone|number|contact)|reveal|إظهار|اظهار|عرض\s*رقم", re.IGNORECASE)
_REVEAL_WEAK_RE = re.compile(r"phone\s*number|call\s*seller", re.IGNORECASE)
_PHONE_LINE_RE = re.compile(r"\+?\d[\d \u00a0\-().]{7,18}\d")      # one line at a time, so digits
_EG_PHONE_DIGITS_RE = re.compile(r"(?:20|0)?1[0125]\d{8}")          # from separate lines never join


def _is_eg_mobile(phone):
    return bool(_EG_PHONE_DIGITS_RE.fullmatch(re.sub(r"\D", "", phone or "")))


def _phones_in_text(text):
    found = []
    for line in (text or "").translate(_ARABIC_DIGITS).splitlines():
        for m in _PHONE_LINE_RE.finditer(line):
            p = _normalize_phone(m.group(0))
            if p and p not in found:
                found.append(p)
    return found


def _visible_phone_snapshot(page):
    """{digits: number} for every phone-looking value on screen right now
    (page text + tel: links). Taken before and after the click."""
    snap = {}
    try:
        text = page.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception:
        text = ""
    for p in _phones_in_text(text):
        snap.setdefault(re.sub(r"\D", "", p), p)
    try:
        hrefs = page.eval_on_selector_all(
            "a[href^='tel:']", "els => els.map(e => e.getAttribute('href'))"
        )
    except Exception:
        hrefs = []
    for href in hrefs or []:
        p = _normalize_phone(unquote((href or "")[4:]))
        if p:
            snap.setdefault(re.sub(r"\D", "", p), p)
    return snap


def _pick_best_phone(candidates):
    for p in candidates:
        if _is_eg_mobile(p):
            return p
    return candidates[0] if candidates else ""


def _find_reveal_button(page):
    """The visible 'Show Phone Number' control - whatever tag it is (button,
    link, or a plain div), found by its text. Skips the site header/footer."""
    scope = page.locator(CONTACT_CARD_SELECTOR).first if CONTACT_CARD_SELECTOR else page
    for pattern in (_REVEAL_STRONG_RE, _REVEAL_WEAK_RE):
        try:
            matches = scope.get_by_text(pattern)
            count = min(matches.count(), 15)
        except Exception:
            continue
        for i in range(count):
            el = matches.nth(i)
            try:
                if not el.is_visible():
                    continue
                if el.evaluate("el => !!el.closest('header, nav, footer, [role=\"navigation\"]')"):
                    continue
                if len((el.inner_text() or "").strip()) > 40:
                    continue      # a paragraph that merely mentions it
            except Exception:
                continue
            return el
    return None


def _click_robustly(el):
    try:
        el.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    for attempt in (
        lambda: el.click(timeout=5000),
        lambda: el.click(force=True, timeout=3000),
        lambda: el.evaluate("e => e.click()"),
    ):
        try:
            attempt()
            return True
        except Exception:
            continue
    return False


def reveal_phone_number(page, responses=None, log=None):
    """
    Clicks the listing's 'Show Phone Number' button and returns the number
    that appears as a result ('' if none). Never raises.
    """
    say = log or (lambda msg: None)
    button = _find_reveal_button(page)
    if button is None:
        say("   Couldn't find a 'Show Phone Number' button on this listing.")
        return ""

    before = _visible_phone_snapshot(page)
    resp_start = len(responses) if responses is not None else 0
    if not _click_robustly(button):
        say("   Found the 'Show Phone Number' button but couldn't click it.")
        return ""

    for _ in range(16):                       # watch up to ~5 seconds
        page.wait_for_timeout(300)
        after = _visible_phone_snapshot(page)
        new = [p for digits, p in after.items() if digits not in before]
        if new:
            return _pick_best_phone(new)

    # Number may have been on screen already (e.g. a previous reveal): look
    # at the button's own surroundings, Egyptian mobile numbers only.
    try:
        texts = button.evaluate(
            "el => { const out = []; let n = el; "
            "for (let i = 0; i < 3 && n; i++) { out.push(n.innerText || ''); n = n.parentElement; } "
            "return out; }"
        )
        for t in texts or []:
            eg = [p for p in _phones_in_text(t) if _is_eg_mobile(p)]
            if eg:
                return eg[0]
    except Exception:
        pass

    # Or the site sent it through a network call made by the click.
    for resp in list(responses or [])[resp_start:]:
        try:
            phone = _phone_from_response_text(resp.text())
        except Exception:
            continue
        if phone:
            return phone

    try:
        if page.locator("[role='dialog'], [aria-modal='true']").first.is_visible():
            say("   A pop-up appeared instead of a number (possibly a login prompt - "
                "use the 'Log in to Dubizzle' step first).")
    except Exception:
        pass
    say("   Clicked 'Show Phone Number' but no new number appeared on the page.")
    return ""


def _save_debug_html(page, listing_url, log):
    """Saves the listing page's HTML (after the phone click) so a field
    that couldn't be read can be diagnosed. Capped at DEBUG_MAX_FILES."""
    try:
        Path(DEBUG_DIR).mkdir(parents=True, exist_ok=True)
        if len(list(Path(DEBUG_DIR).glob("*.html"))) >= DEBUG_MAX_FILES:
            return
        name = _extract_listing_id(listing_url) or hashlib.md5(listing_url.encode("utf-8")).hexdigest()[:10]
        path = Path(DEBUG_DIR) / f"{name}.html"
        path.write_text(page.content(), encoding="utf-8")
        try:
            page.screenshot(path=str(path.with_suffix(".png")))
        except Exception:
            pass
        log(f"   (some details missing - saved the page to {path} for troubleshooting)")
    except Exception:
        pass


def read_owner_details(page, listing_url, responses, log=print):
    """Title, price, owner name and phone for an OWNER listing."""
    details = {"title": "", "price": "", "owner_name": "", "phone": ""}
    try:
        details.update(get_listing_details(page, listing_url))
    except Exception as e:
        log(f"   Couldn't read title/price/owner ({e!s})")
    try:
        details["phone"] = reveal_phone_number(page, responses, log)
    except Exception as e:
        log(f"   Couldn't read the phone number ({e!s})")
    if SAVE_DEBUG_HTML and not all(details.values()):
        _save_debug_html(page, listing_url, log)
    return details


def _already_done(listing_url, processed, log_rows):
    """Skip a listing only if it was fully handled before. Owner listings
    logged without a phone number (e.g. by an older version of this tool)
    are looked at again so the number can be picked up."""
    if listing_url not in processed:
        return False
    row = log_rows.get(listing_url)
    if row and row.get("classification") == "owner" and not (row.get("phone") or "").strip():
        return False
    return True


# ---------- pagination ----------

def _find_next_button(page):
    if NEXT_BUTTON_SELECTOR:
        candidates = [NEXT_BUTTON_SELECTOR]
    else:
        candidates = [
            "a[aria-label='Next']",
            "a[aria-label='Next Page']",
            "a[aria-label='Go to next page']",
            "button[aria-label='Next']",
            "button[aria-label='Next Page']",
            "button[aria-label='Go to next page']",
            "a[rel='next']",
            "li.pagination-next a",
            "li.ant-pagination-next a",
            "li.ant-pagination-next button",
            "a:has-text('Next')",
            "button:has-text('Next')",
            "a[data-testid='page-next']",
            "button[data-testid='page-next']",
            "a[data-testid*='next']",
            "button[data-testid*='next']",
            "a[class*='next']",
            "button[class*='next']",
        ]
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                return loc
        except Exception:
            continue

    # Fallback 1: numbered pagination (1 2 3 ...) instead of an explicit
    # "Next" control - find the current-page marker and click whatever
    # comes right after it.
    found = _find_next_numbered_page_link(page)
    if found is not None:
        return found

    # Fallback 2: a bare arrow icon with no text/label at all next to a
    # row of page-number links (e.g. "< 1 2 3 ... 199 >") - locate the
    # numbered links, then take the element immediately after the last
    # one in the same row/container.
    return _find_next_arrow_by_pagination_container(page)


def _find_next_numbered_page_link(page):
    current_candidates = [
        "[aria-current='page']",
        ".pagination .active a",
        ".pagination li.active a",
        "a.active[href]",
        "span.active",
    ]
    for sel in current_candidates:
        try:
            current = page.locator(sel).first
            if current.count() == 0:
                continue
            current_text = (current.inner_text() or "").strip()
            if not current_text.isdigit():
                continue
            next_num = str(int(current_text) + 1)
            candidate = page.locator(f"a:text-is('{next_num}')").first
            if candidate.count() > 0:
                return candidate
        except Exception:
            continue
    return None


def _find_next_arrow_by_pagination_container(page):
    """
    Heuristic fallback for pagination that's just a row of digit
    page-links plus a bare arrow icon and no 'Next' text or
    aria-label anywhere - e.g. "< 1 2 3 ... 199 >". Finds the visible
    page-number links, takes the shared row/container they sit in,
    and returns whichever button/link comes right after the last
    digit link in that same container (the right-arrow control).
    Returns None if that can't be determined confidently.
    """
    try:
        digit_links = page.locator("a, button").filter(has_text=re.compile(r"^\d+$"))
        count = digit_links.count()
        if count < 2:
            return None

        last_digit = digit_links.nth(count - 1)
        container = last_digit.locator(
            "xpath=ancestor::*[self::nav or self::ul or self::div][1]"
        )
        if container.count() == 0:
            return None

        children = container.first.locator("a, button")
        child_count = children.count()
        if child_count == 0:
            return None

        candidate = children.nth(child_count - 1)
        text = (candidate.inner_text() or "").strip()
        if text.isdigit():
            # Last child in the row is itself a page number, not an
            # arrow after it - nothing to click here.
            return None
        return candidate
    except Exception:
        return None


def _is_control_disabled(locator):
    """
    Checks several common ways a pagination control signals it's
    greyed-out / at the end (native disabled attribute, aria-disabled,
    a "disabled" class on the element itself or its parent <li>).
    """
    try:
        if locator.get_attribute("disabled") is not None:
            return True
    except Exception:
        pass
    try:
        if (locator.get_attribute("aria-disabled") or "").lower() == "true":
            return True
    except Exception:
        pass
    try:
        own_class = (locator.get_attribute("class") or "").lower()
        if "disabled" in own_class:
            return True
    except Exception:
        pass
    try:
        if locator.evaluate(
            "el => !!el.closest('[disabled], [aria-disabled=\"true\"], "
            "[class*=\"disabled\"]')"
        ):
            return True
    except Exception:
        pass
    return False


def _find_load_more_button(page):
    """
    Some result pages use a 'Load more' / 'Show more' button (infinite
    scroll) instead of page-by-page navigation. Checked first, before
    a 'Next' link, on each pass.
    """
    candidates = [
        "button:has-text('Load more')",
        "button:has-text('Show more')",
        "a:has-text('Load more')",
        "a:has-text('Show more')",
        "button:has-text('See more')",
        "a:has-text('See more')",
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


def collect_all_listings(page, max_listings=None, scroll_pause_seconds=None, log=print):
    """
    Walks every results page via the site's own pagination ("Next"),
    scrolling each page first in case it lazy-loads content, and
    returns the de-duplicated list of listing URLs found.

    max_listings / scroll_pause_seconds fall back to the module-level
    MAX_LISTINGS / SCROLL_PAUSE_SECONDS when not given, so this still
    works unchanged when called from the terminal entry point; a GUI
    passes its own values straight from the on-screen fields instead
    of requiring anyone to edit this file.
    """
    max_listings = MAX_LISTINGS if max_listings is None else max_listings
    scroll_pause_seconds = SCROLL_PAUSE_SECONDS if scroll_pause_seconds is None else scroll_pause_seconds
    scroll_pause_ms = int(scroll_pause_seconds * 1000)

    seen_links = []
    page_num = 1
    stale_pages = 0

    while len(seen_links) < max_listings:
        # scroll this page to the bottom a few times in case it lazy-loads
        stable_rounds = 0
        prev_count = -1
        while stable_rounds < 3:
            links = page.locator("a[href*='/ad/']").all()
            hrefs = {l.get_attribute("href") for l in links if l.get_attribute("href")}
            count = len(hrefs)
            stable_rounds = stable_rounds + 1 if count == prev_count else 0
            prev_count = count
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(scroll_pause_ms)

        links = page.locator("a[href*='/ad/']").all()
        hrefs = {l.get_attribute("href") for l in links if l.get_attribute("href")}
        new_count = 0
        for href in hrefs:
            if href not in seen_links:
                seen_links.append(href)
                new_count += 1

        log(f"Page {page_num}: {len(hrefs)} listings found ({new_count} new). "
            f"{len(seen_links)} total so far.")

        if len(seen_links) >= max_listings:
            break

        # safety nets (never needed when paging works): stop if several pages
        # in a row add nothing new, or after MAX_PAGES pages
        stale_pages = stale_pages + 1 if new_count == 0 else 0
        if stale_pages >= 3:
            log("Three pages in a row had nothing new - stopping here.")
            break
        if page_num >= MAX_PAGES:
            log(f"Hit the safety limit of {MAX_PAGES} pages - stopping.")
            break

        load_more = _find_load_more_button(page)
        if load_more is not None:
            try:
                load_more.scroll_into_view_if_needed()
                load_more.click()
                page.wait_for_timeout(scroll_pause_ms)
                page_num += 1
                continue
            except Exception as e:
                log(f"Couldn't click 'Load more' ({e!s}) - falling back to pagination check.")

        next_button = _find_next_button(page)
        if next_button is None:
            log("No 'Next'/'Load more' control found - assuming this is the last page. "
                "If the site actually has more results, the selectors in NEXT_BUTTON_SELECTOR "
                "or _find_load_more_button() likely need updating for this page's markup "
                "(right-click the pagination control in your browser -> Inspect).")
            break

        if _is_control_disabled(next_button):
            log(f"Page {page_num}: next-page control looks disabled/greyed-out - "
                "this is the last page.")
            break

        try:
            next_button.scroll_into_view_if_needed()
            next_button.click()
            safe_wait_for_load(page)
        except Exception as e:
            log(f"Couldn't click 'Next' ({e}) - stopping pagination here.")
            break

        page_num += 1

    return seen_links[:max_listings]


# ---------- main ----------

def run_login_only(log=print, wait_for_done=None):
    """
    Opens a real, visible browser window against the persistent profile
    so you can log into Dubizzle and set your search location +
    Furnished filter by hand, then closes it again.

    wait_for_done, when given (the GUI passes one - typically a
    threading.Event's .wait method), is called instead of the
    terminal's input() below and should block until you've told the
    GUI you're done. Running from the terminal (wait_for_done=None)
    falls back to pressing Enter.
    """
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(PROFILE_DIR, headless=os.environ.get("LIVIQO_HEADLESS", "false").lower() == "true")
        page = context.new_page()
        log("A browser window will open. Log into Dubizzle, set your location")
        log("and Furnished = Yes filter, then copy the results page URL for next time.")
        page.goto("https://www.dubizzle.com.eg/")
        if wait_for_done is not None:
            wait_for_done()
        else:
            input("Press Enter here once you're done (this will close the browser)...")
        context.close()
        log("Login/setup browser closed.")


def run_scrape(*args, **kwargs):
    """Runs the scrape, flagging that a run is active (the report page uses
    this to refuse 'forget' deletes that the run would silently undo)."""
    _RUN_ACTIVE.set()
    try:
        return _run_scrape_impl(*args, **kwargs)
    finally:
        _RUN_ACTIVE.clear()


def _run_scrape_impl(search_url, log=print, on_verification_wait=None, should_stop=None,
                open_report=True, max_listings=None, scroll_pause_seconds=None,
                on_progress=None, on_result=None):
    """
    The whole find-and-classify run, extracted out of a plain script
    body so both the terminal entry point below and a GUI can drive it
    with their own logging and their own way of pausing for human
    verification, without duplicating any of this logic.

    log: called with each progress line (defaults to print).
    on_verification_wait: passed straight through to
        wait_for_human_check - see that function's docstring.
    should_stop: called before each listing; if it returns True the
        run finishes up cleanly after the current listing instead of
        continuing (a GUI wires this to a "Stop" button).
    open_report: whether to open the live HTML report in a browser
        tab automatically when the run starts (mirrors
        OPEN_REPORT_AUTOMATICALLY for the terminal entry point; a GUI
        may prefer to pass False and use its own "Open report" button).
    max_listings: stop once this many listings have been found, across
        all results pages (falls back to MAX_LISTINGS when None - a
        GUI passes its own value straight from an on-screen field
        instead of requiring anyone to edit this file).
    scroll_pause_seconds: how long to pause after each scroll/"Load
        more" click while paging through results, to give a lazy-
        loading page time to render more listings (falls back to
        SCROLL_PAUSE_SECONDS when None; same idea as max_listings -
        a GUI field instead of an edit to this file).
    on_progress: optional callback(done, total) fired after every
        listing is processed, so a GUI can drive a progress bar/counter.
    on_result: optional callback(row) fired after each listing is written to
        the log, so a GUI can refresh its results table live.
    """
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(PROFILE_DIR, headless=os.environ.get("LIVIQO_HEADLESS", "false").lower() == "true")
        page = context.new_page()

        if not search_url:
            log("No search URL was given.")
            context.close()
            return

        if open_report:
            write_html_report()
            url = open_report_in_browser()
            log(f"Live report opened in your browser: {url or Path(REPORT_HTML).resolve()}")

        processed = load_processed()
        log_rows = {r.get("listing_url"): r for r in read_log_rows()}
        page.goto(search_url)
        wait_for_human_check(page, "the search results page", first_time=True, on_wait=on_verification_wait)

        seen_links = collect_all_listings(
            page, max_listings=max_listings,
            scroll_pause_seconds=scroll_pause_seconds, log=log,
        )
        total = len(seen_links)
        log(f"\nFound {total} listings across all pages.\n")

        done = 0
        for href in seen_links:
            if should_stop is not None and should_stop():
                log("Stopped.")
                break

            listing_url = href if href.startswith("http") else f"https://www.dubizzle.com.eg{href}"
            if _already_done(listing_url, processed, log_rows):
                done += 1
                if on_progress is not None:
                    on_progress(done, total)
                continue

            listing_page = context.new_page()
            captured = []
            listing_page.on("response", make_response_collector(captured))
            try:
                details = {}
                try:
                    listing_page.goto(listing_url, timeout=30000)
                    safe_wait_for_load(listing_page)
                    kind, chat_element = classify_listing(listing_page)
                    image_urls = get_listing_images(listing_page, listing_url)
                    if not image_urls:
                        # URL-based extraction found nothing usable on
                        # this listing's markup - fall back to a real
                        # screenshot so the report never comes up blank.
                        shot = capture_listing_photo(listing_page, listing_url)
                        if shot:
                            image_urls = [shot]
                    if kind == "owner":
                        details = read_owner_details(listing_page, listing_url, captured, log)
                except Exception as e:
                    log(f"Couldn't load/classify {listing_url} ({e!s}) - skipping this one, continuing run.")
                    row = log_result(listing_url, "unknown", "error")
                    log_rows[listing_url] = row
                    if on_result is not None:
                        on_result(row)
                    continue
                finally:
                    done += 1
                    if on_progress is not None:
                        on_progress(done, total)

                if kind == "owner":
                    log(f"[OWNER] {details.get('title') or '(no title)'} | "
                        f"{details.get('owner_name') or '(no name)'} | "
                        f"{details.get('price') or '(no price)'} | "
                        f"{details.get('phone') or '(no number)'}")
                    log(f"        {listing_url}")
                else:
                    log(f"[{kind.upper()}] {listing_url}")

                # This script only finds and classifies listings - it
                # never opens the chat or sends anything to anyone. Owners
                # are logged (with photo, title, price, name and number)
                # so you can reach out yourself; brokers are logged too,
                # for the record, and skipped either way.
                action = "found" if kind == "owner" else "skipped"
                row = log_result(listing_url, kind, action, image_urls, details)
                log_rows[listing_url] = row
                processed.add(listing_url)
                save_processed(processed)
                if on_result is not None:
                    on_result(row)
            finally:
                listing_page.close()

        context.close()
        log("Done.")
        log(f"Full log: {Path(LOG_CSV).resolve()}")
        log(f"Live report: {Path(REPORT_HTML).resolve()}")
        log(f"Photos: {Path(PHOTOS_DIR).resolve()} (keep this folder next to the "
            "report if you move it, so the photos keep showing)")


def main():
    if RUN_LOGIN_ONLY:
        run_login_only()
    else:
        run_scrape(SEARCH_URL, open_report=OPEN_REPORT_AUTOMATICALLY)
        if _report_server is not None:
            input("\nThe report page stays live (delete buttons work) until you press Enter here... ")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Catches things like a Playwright TargetClosedError - e.g. if
        # the browser window got closed (manually, by another process,
        # or because another script/browser instance was already using
        # PROFILE_DIR) partway through a run - and explains it instead
        # of just dumping a raw traceback.
        print(f"\nThe run stopped because of an unexpected error: {type(e).__name__}: {e}")
        print("\nIf this mentions the page/context/browser being 'closed', the most common causes are:")
        print("  - The browser window got closed manually while the script was still running")
        print(f"  - Another Chrome/Chromium window is already using the profile folder ({PROFILE_DIR})")
        print("  - A previous run of this script is still running in another terminal")
        print("Close any other windows/processes using that profile folder, then run the script again.")
