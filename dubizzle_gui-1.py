"""
Dubizzle Property Finder - GUI
-------------------------------
A point-and-click front end for dubizzle_scraper.py. No editing the
script or using a terminal needed.

HOW TO USE:
  1. Click "Log in to Dubizzle". A real browser window opens - log in,
     set your search location and Furnished filter, then click
     "Done logging in" back here (only needed the first time).
  2. Paste the resulting search-results page URL into the box.
  3. Adjust "Max listings" and "Scroll pause" if you want (sensible
     defaults are pre-filled - you never need to edit the script).
  4. Click "Start". A browser window opens and loads the search page.
     If a human-verification challenge ("confirm you're human", a
     captcha, etc.) shows up in that window, solve it there, then
     click "I've done it - Continue" back here.
  5. Watch live progress at the bottom. The "Owner results" tab fills in
     as owners are found (title, owner name, price, phone number):
       - "Copy all" copies every listing's link with its number under it
       - "Clear owner results..." wipes the owner results (it asks first)
     "Open report" shows the same owners as photo cards in your browser.
     There you can tick the listings you've already contacted and click
     "Delete selected", or "Clear all..." - both ask "are you sure?" first.

REQUIREMENTS: dubizzle_scraper.py must sit in this same folder.
    pip install playwright
    playwright install chromium
Then just run this file (python dubizzle_gui.py) - no terminal
interaction needed after that.
"""

import queue
import threading
import webbrowser
from pathlib import Path
from tkinter import Tk, StringVar, IntVar, DoubleVar, messagebox
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

import dubizzle_scraper1 as scraper

STATUS_COLORS = {
    "idle": "#6b7280",
    "running": "#1a7f37",
    "waiting": "#b35900",
    "stopped": "#b30000",
    "error": "#b30000",
}


def _positive_int(value):
    """Parses a whole number > 0 out of a Tk variable's raw value, or
    raises ValueError - used to validate the settings fields without
    trusting whatever's currently typed into the Spinbox."""
    n = int(str(value).strip())
    if n < 1:
        raise ValueError
    return n


def _positive_float(value):
    n = float(str(value).strip())
    if n <= 0:
        raise ValueError
    return n


class DubizzleApp:
    def __init__(self, root):
        self.root = root
        root.title("Dubizzle Property Finder")
        root.geometry("860x760")
        root.minsize(720, 560)

        # Cross-thread signaling. The scraper always runs on a
        # background thread (it blocks on the browser), and these are
        # how button clicks on the main/UI thread talk to it.
        self.log_queue = queue.Queue()
        self.verification_event = threading.Event()
        self.login_done_event = threading.Event()
        self.stop_event = threading.Event()
        self.worker_thread = None
        self.login_thread = None
        self._run_state = "running"  # "running" or "waiting", used by the heartbeat pulse

        self._build_ui()
        self.root.after(150, self._drain_log_queue)
        self.root.after(400, self._pulse_heartbeat)
        self._log_mtime = None
        self.root.after(2000, self._poll_results)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------------------------------------------------- UI --
    def _build_ui(self):
        pad = {"padx": 12, "pady": 8}

        # ---- status bar up top, so the app never looks "dead" ----
        status_bar = ttk.Frame(self.root)
        status_bar.pack(fill="x", padx=12, pady=(10, 0))
        self.heartbeat_label = ttk.Label(status_bar, text="\u25cf", foreground=STATUS_COLORS["idle"])
        self.heartbeat_label.pack(side="left")
        self.status_var = StringVar(value="Idle - paste a URL and click Start")
        ttk.Label(status_bar, textvariable=self.status_var, font=("", 10, "bold")).pack(side="left", padx=(6, 0))

        step1 = ttk.LabelFrame(self.root, text="1. Log in  (only needed the first time)")
        step1.pack(fill="x", **pad)
        row1 = ttk.Frame(step1)
        row1.pack(fill="x", padx=10, pady=10)
        self.login_btn = ttk.Button(row1, text="Log in to Dubizzle", command=self.on_login_clicked)
        self.login_btn.pack(side="left")
        self.login_done_btn = ttk.Button(
            row1, text="Done logging in", command=self.on_login_done_clicked, state="disabled"
        )
        self.login_done_btn.pack(side="left", padx=8)

        step2 = ttk.LabelFrame(self.root, text="2. Paste your Dubizzle search results URL")
        step2.pack(fill="x", **pad)
        self.url_var = StringVar()
        url_entry = ttk.Entry(step2, textvariable=self.url_var)
        url_entry.pack(fill="x", padx=10, pady=10)

        step3 = ttk.LabelFrame(self.root, text="3. Settings  (no need to touch the script)")
        step3.pack(fill="x", **pad)
        row3 = ttk.Frame(step3)
        row3.pack(fill="x", padx=10, pady=(10, 2))

        ttk.Label(row3, text="Max listings:").pack(side="left")
        self.max_listings_var = IntVar(value=scraper.MAX_LISTINGS)
        ttk.Spinbox(row3, from_=1, to=100000, increment=10, width=8,
                    textvariable=self.max_listings_var).pack(side="left", padx=(4, 20))

        ttk.Label(row3, text="Scroll pause (seconds):").pack(side="left")
        self.scroll_pause_var = DoubleVar(value=scraper.SCROLL_PAUSE_SECONDS)
        ttk.Spinbox(row3, from_=0.5, to=30, increment=0.5, width=6,
                    textvariable=self.scroll_pause_var).pack(side="left", padx=(4, 0))

        row3b = ttk.Frame(step3)
        row3b.pack(fill="x", padx=10, pady=(2, 10))
        ttk.Label(
            row3b,
            text="Max listings: stop once this many have been checked. Scroll pause: how "
                 "long to wait after each scroll/page-load while collecting listings - "
                 "raise it on a slow connection.",
            foreground="#6b7280", wraplength=680, justify="left",
        ).pack(fill="x")

        step4 = ttk.LabelFrame(self.root, text="4. Run")
        step4.pack(fill="x", **pad)
        row4 = ttk.Frame(step4)
        row4.pack(fill="x", padx=10, pady=(10, 4))
        self.start_btn = ttk.Button(row4, text="\u25b6  Start", command=self.on_start_clicked)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(row4, text="\u25a0  Stop", command=self.on_stop_clicked, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        self.verify_btn = ttk.Button(
            row4, text="\u2713  I've done human verification - Continue",
            command=self.on_verify_clicked, state="disabled",
        )
        self.verify_btn.pack(side="left", padx=8)
        self.report_btn = ttk.Button(row4, text="Open report", command=self.on_open_report_clicked)
        self.report_btn.pack(side="right")

        progress_row = ttk.Frame(step4)
        progress_row.pack(fill="x", padx=10, pady=(4, 10))
        self.progress = ttk.Progressbar(progress_row, mode="determinate")
        self.progress.pack(fill="x", side="left", expand=True)
        self.progress_label_var = StringVar(value="")
        ttk.Label(progress_row, textvariable=self.progress_label_var, width=14, anchor="e").pack(side="left", padx=(8, 0))

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, **pad)

        log_tab = ttk.Frame(self.notebook)
        self.notebook.add(log_tab, text="Progress log")
        self.log_box = ScrolledText(log_tab, state="disabled", wrap="word", height=10)
        self.log_box.pack(fill="both", expand=True, padx=6, pady=6)

        self.results_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.results_tab, text="Owner results (0)")
        self._build_results_tab(self.results_tab)
        self._refresh_results()

    def _build_results_tab(self, tab):
        bar = ttk.Frame(tab)
        bar.pack(fill="x", padx=6, pady=(8, 4))
        ttk.Button(bar, text="Copy all (link + number)", command=self.on_copy_all_clicked).pack(side="left")
        self.clear_btn = ttk.Button(bar, text="Clear owner results...", command=self.on_clear_clicked)
        self.clear_btn.pack(side="left", padx=8)
        self.results_msg_var = StringVar(value="")
        ttk.Label(bar, textvariable=self.results_msg_var, foreground="#1a7f37").pack(side="left", padx=8)

        table = ttk.Frame(tab)
        table.pack(fill="both", expand=True, padx=6, pady=(0, 2))
        columns = ("title", "owner", "price", "phone")
        self.results_tree = ttk.Treeview(table, columns=columns, show="headings", selectmode="browse")
        for col, text, width in (
            ("title", "Title", 340), ("owner", "Owner", 130),
            ("price", "Price", 110), ("phone", "Phone", 120),
        ):
            self.results_tree.heading(col, text=text)
            self.results_tree.column(col, width=width, anchor="w", stretch=(col == "title"))
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.results_tree.yview)
        self.results_tree.configure(yscrollcommand=scroll.set)
        self.results_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.results_tree.bind("<Double-1>", self.on_result_double_clicked)
        self.results_links = {}
        self.results_rows = []

        ttk.Label(tab, text="Double-click a row to open that listing in your browser.",
                  foreground="#6b7280").pack(anchor="w", padx=8, pady=(0, 6))

    # --------------------------------------------------- status/UX --
    def _set_status(self, text, kind="idle"):
        self.status_var.set(text)
        self._run_state = kind
        self.heartbeat_label.configure(foreground=STATUS_COLORS.get(kind, STATUS_COLORS["idle"]))

    def _pulse_heartbeat(self):
        """Makes the status dot gently pulse while a run is active, so
        the window visibly shows it's alive even between log lines,
        instead of looking frozen while a listing loads."""
        busy = (self.worker_thread and self.worker_thread.is_alive()) or (
            self.login_thread and self.login_thread.is_alive()
        )
        if busy:
            base = STATUS_COLORS.get(self._run_state, STATUS_COLORS["running"])
            dim = "#d8dee3"
            current = self.heartbeat_label.cget("foreground")
            self.heartbeat_label.configure(foreground=dim if current == base else base)
        self.root.after(500, self._pulse_heartbeat)

    # ---------------------------------------------- thread-safe log --
    def log(self, message):
        """Safe to call from the background thread - just queues text;
        the UI thread's _drain_log_queue loop actually writes it."""
        self.log_queue.put(str(message))

    def _drain_log_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log_box.configure(state="normal")
                self.log_box.insert("end", msg + "\n")
                self.log_box.see("end")
                self.log_box.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(150, self._drain_log_queue)

    # --------------------------------------------------- login flow --
    def on_login_clicked(self):
        if self.login_thread and self.login_thread.is_alive():
            return
        self.login_btn.configure(state="disabled")
        self.login_done_event.clear()
        self.login_done_btn.configure(state="normal")
        self._set_status("Waiting for you to log in...", "waiting")
        self.log("Opening a browser window for you to log in...")

        def worker():
            try:
                scraper.run_login_only(log=self.log, wait_for_done=self.login_done_event.wait)
            except Exception as e:
                self.log(f"Login window closed unexpectedly: {e}")
            finally:
                self.root.after(0, self._on_login_finished)

        self.login_thread = threading.Thread(target=worker, daemon=True)
        self.login_thread.start()

    def on_login_done_clicked(self):
        self.login_done_btn.configure(state="disabled")
        self.login_done_event.set()

    def _on_login_finished(self):
        self.login_btn.configure(state="normal")
        self.login_done_btn.configure(state="disabled")
        self._set_status("Logged in - paste your search URL and click Start", "idle")
        self.log("Login window closed. Paste your search URL above and click Start.")

    # -------------------------------------------------- scrape flow --
    def on_start_clicked(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("No URL", "Paste your Dubizzle search results URL first.")
            return
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("Already running", "A run is already in progress.")
            return

        try:
            max_listings = _positive_int(self.max_listings_var.get())
        except (ValueError, TypeError):
            messagebox.showwarning("Invalid max listings", "Max listings must be a whole number of 1 or more.")
            return
        try:
            scroll_pause = _positive_float(self.scroll_pause_var.get())
        except (ValueError, TypeError):
            messagebox.showwarning("Invalid scroll pause", "Scroll pause must be a number greater than 0.")
            return

        self.stop_event.clear()
        self.verification_event.clear()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.progress_label_var.set("finding listings...")
        self._set_status("Loading search results...", "running")
        self.log(f"Starting... loading {url}")
        self.log(f"(max listings: {max_listings}, scroll pause: {scroll_pause}s)")

        def on_verification_wait(label):
            self.root.after(0, lambda: self._set_status("Waiting for human verification...", "waiting"))
            self.log(
                f"\nLoaded {label}. If a human-verification challenge (captcha / "
                "\"confirm you're human\") is showing in the browser window, solve "
                "it now, then click \"I've done human verification - Continue\" below."
            )
            self.root.after(0, lambda: self.verify_btn.configure(state="normal"))
            self.verification_event.wait()
            self.verification_event.clear()
            self.root.after(0, lambda: self.verify_btn.configure(state="disabled"))
            self.root.after(0, lambda: self._set_status("Running...", "running"))

        def on_progress(done, total):
            def update():
                if str(self.progress["mode"]) != "determinate":
                    self.progress.stop()
                    self.progress.configure(mode="determinate", maximum=max(total, 1))
                self.progress["value"] = done
                self.progress_label_var.set(f"{done} / {total}")
                self._set_status(f"Running... {done} of {total} listings checked", "running")
            self.root.after(0, update)

        def worker():
            try:
                scraper.run_scrape(
                    url,
                    log=self.log,
                    on_verification_wait=on_verification_wait,
                    should_stop=self.stop_event.is_set,
                    open_report=True,
                    max_listings=max_listings,
                    scroll_pause_seconds=scroll_pause,
                    on_progress=on_progress,
                    on_result=lambda row: self.root.after(0, self._refresh_results),
                )
            except Exception as e:
                self.log(f"\nThe run stopped because of an unexpected error: {type(e).__name__}: {e}")
                self.log(
                    "If this mentions the page/browser being 'closed', close any other "
                    "Chrome/Chromium window using the dubizzle_profile folder and try again."
                )
                self.root.after(0, lambda: self._set_status("Stopped because of an error", "error"))
            finally:
                self.root.after(0, self._on_run_finished)

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()

    def on_stop_clicked(self):
        self._set_status("Stopping...", "stopped")
        self.log("Stopping after the current listing...")
        self.stop_event.set()
        self.verification_event.set()  # unblock if it's paused waiting on verification
        self.stop_btn.configure(state="disabled")

    def _on_run_finished(self):
        self._refresh_results()
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.verify_btn.configure(state="disabled")
        if str(self.progress["mode"]) != "determinate":
            self.progress.stop()
            self.progress.configure(mode="determinate")
        if self._run_state != "error":
            self._set_status("Finished - check the report", "idle")

    def on_verify_clicked(self):
        self.verify_btn.configure(state="disabled")
        self.verification_event.set()

    # ------------------------------------------------------- report --
    def on_open_report_clicked(self):
        path = Path(scraper.REPORT_HTML).resolve()
        if not path.exists():
            messagebox.showinfo("No report yet", "The report will appear here once a run has found something.")
            return
        # Opens through the app's local report server, so the page's
        # "Delete selected" / "Clear all" buttons work.
        scraper.open_report_in_browser()

    # --------------------------------------------------- owner results --
    def _refresh_results(self):
        """Reloads the Owner results table from the log (newest first)."""
        try:
            rows = scraper.read_owner_rows()
        except Exception:
            return  # log being written at this instant - the next refresh will catch up
        self.results_rows = rows
        self.results_links = {}
        self.results_tree.delete(*self.results_tree.get_children())
        for r in reversed(rows):
            iid = self.results_tree.insert("", "end", values=(
                (r.get("title") or "").strip() or "(no title)",
                (r.get("owner_name") or "").strip() or "-",
                (r.get("price") or "").strip() or "-",
                (r.get("phone") or "").strip() or "-",
            ))
            self.results_links[iid] = r.get("listing_url", "")
        self.notebook.tab(self.results_tab, text=f"Owner results ({len(rows)})")

    def _poll_results(self):
        """Refreshes the table if the log changed on disk (e.g. listings
        deleted from the report page in the browser)."""
        try:
            mtime = Path(scraper.LOG_CSV).stat().st_mtime
        except OSError:
            mtime = None
        if mtime != self._log_mtime:
            self._log_mtime = mtime
            self._refresh_results()
        self.root.after(2000, self._poll_results)

    def on_result_double_clicked(self, event):
        iid = self.results_tree.identify_row(event.y)
        url = self.results_links.get(iid)
        if url:
            webbrowser.open(url)

    def on_copy_all_clicked(self):
        rows = scraper.read_owner_rows()
        text, copied, skipped = scraper.format_copy_text(rows)
        if not copied:
            messagebox.showinfo("Nothing to copy", "No owner listings with a phone number yet.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()  # keeps the clipboard contents after the window loses focus
        msg = f"Copied {copied} listing(s)"
        if skipped:
            msg += f" ({skipped} without a number left out)"
        self.results_msg_var.set(msg)

    def on_clear_clicked(self):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("Run in progress", "Stop the current run before clearing the results.")
            return
        count = len(scraper.read_owner_rows())
        if count == 0:
            messagebox.showinfo("Nothing to clear", "There are no owner results to clear.")
            return
        if not messagebox.askyesno(
            "Clear owner results",
            f"Permanently delete all {count} owner results?\n\n"
            "Broker records are kept. The deleted owner listings will be checked "
            "again the next time you run a search.\n\nThis cannot be undone.",
            icon="warning", default="no", parent=self.root,
        ):
            return
        removed = scraper.clear_owner_results()
        self._refresh_results()
        self.results_msg_var.set(f"Cleared {removed} owner result(s)")

    # -------------------------------------------------------- close --
    def _on_close(self):
        if (self.worker_thread and self.worker_thread.is_alive()) or (
            self.login_thread and self.login_thread.is_alive()
        ):
            if not messagebox.askyesno(
                "Run in progress",
                "A run is still going. Closing now will abruptly stop it "
                "(anything already logged stays saved). Close anyway?",
            ):
                return
        self.root.destroy()


def main():
    root = Tk()
    try:
        ttk.Style().theme_use("clam")
    except Exception:
        pass
    DubizzleApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
