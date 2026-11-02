"""Study Buddy: live study timer with friends and a leaderboard.

Setup: paste your Firebase Realtime Database URL below, then build.
Friends only type a name tag the first time they open the app.
"""
import json
import os
import queue
import re
import threading
import time
import uuid
from pathlib import Path

import customtkinter as ctk
import requests

# ======= PASTE YOUR FIREBASE URL HERE (no slash at the end) =======
FIREBASE_URL = "https://rmdn-f2330-default-rtdb.firebaseio.com"
# ==================================================================

POLL_SECONDS = 5          # how often we check friends
HEARTBEAT_SECONDS = 15    # how often we say "I'm still here"
ONLINE_WINDOW_MS = 45_000  # no heartbeat for this long = offline

STATUSES = {
    "studying": ("Studying", "#2ecc71"),
    "break": ("On break", "#f1c40f"),
    "away": ("Away", "#e67e22"),
    "idle": ("Online", "#5dade2"),
    "offline": ("Offline", "#6b6b6b"),
}

CONFIG_DIR = Path(os.getenv("APPDATA") or Path.home()) / "StudyBuddy"
CONFIG_FILE = CONFIG_DIR / "config.json"
NAME_RE = re.compile(r"^(?=.*[A-Za-z])[A-Za-z0-9_-]{2,20}$")


def fmt(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg), encoding="utf-8")
    except Exception:
        pass


class Api:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.http = requests.Session()

    def get(self, path):
        r = self.http.get(f"{self.base}/{path}.json", timeout=8)
        r.raise_for_status()
        return r.json()

    def patch(self, path, data):
        r = self.http.patch(f"{self.base}/{path}.json", json=data, timeout=8)
        r.raise_for_status()


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.title("Study Buddy")
        self.geometry("420x660")
        self.minsize(380, 520)

        self.cfg = load_config()
        self.cfg.setdefault("uid", uuid.uuid4().hex)
        self.cfg.setdefault("friends", [])
        self.api = Api(FIREBASE_URL)

        self.key = None
        self.name = None
        self.pending_name = None
        self.me = {"status": "idle", "since": None, "total": 0}
        self.users = {}
        self.offset_ms = 0
        self.hb_local_ms = 0
        self.seen_own_ls = None
        self.ready = False
        self.loops_started = False
        self.row_order = []
        self.rows = {}

        self.jobs = queue.Queue()
        self.results = queue.Queue()
        threading.Thread(target=self.worker, daemon=True).start()

        self.build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(150, self.startup)
        self.after(200, self.drain)
        self.tick()

    # ---------------- UI ----------------
    def build_ui(self):
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=16, pady=(14, 0))
        self.name_label = ctk.CTkLabel(top, text="...", font=("Segoe UI", 16, "bold"))
        self.name_label.pack(side="left")

        self.session_label = ctk.CTkLabel(self, text="0:00:00", font=("Consolas", 54, "bold"))
        self.session_label.pack(pady=(8, 0))
        ctk.CTkLabel(self, text="current session", text_color="#888").pack()

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(pady=12)
        self.btns = {}
        for st, text, color in [
            ("studying", "Study", "#27ae60"),
            ("break", "Break", "#b7950b"),
            ("away", "Away", "#d35400"),
            ("idle", "Stop", "#5d6d7e"),
        ]:
            b = ctk.CTkButton(
                btn_row, text=text, width=80, fg_color=color, hover_color=color,
                border_color="white", border_width=0,
                command=lambda s=st: self.set_status(s),
            )
            b.pack(side="left", padx=4)
            self.btns[st] = b
        self.highlight()

        ctk.CTkLabel(self, text="Leaderboard", font=("Segoe UI", 14, "bold"),
                     anchor="w").pack(fill="x", padx=18, pady=(6, 0))
        self.list_frame = ctk.CTkScrollableFrame(self)
        self.list_frame.pack(fill="both", expand=True, padx=12, pady=6)

        add = ctk.CTkFrame(self, fg_color="transparent")
        add.pack(fill="x", padx=14, pady=(0, 4))
        self.friend_entry = ctk.CTkEntry(add, placeholder_text="Add a friend's name tag")
        self.friend_entry.pack(side="left", fill="x", expand=True)
        self.friend_entry.bind("<Return>", lambda e: self.add_friend())
        ctk.CTkButton(add, text="Add", width=60, command=self.add_friend).pack(side="left", padx=(8, 0))

        self.banner = ctk.CTkLabel(self, text="", text_color="#e74c3c", wraplength=380)
        self.banner.pack(pady=(0, 8))

    def set_banner(self, text):
        self.banner.configure(text=text)

    def highlight(self):
        cur = self.me["status"]
        for st, b in self.btns.items():
            b.configure(border_width=3 if st == cur else 0)

    # ---------------- background worker ----------------
    def worker(self):
        while True:
            job = self.jobs.get()
            try:
                job()
            except Exception as e:
                self.results.put(("error", str(e)))

    def drain(self):
        try:
            while True:
                kind, payload = self.results.get_nowait()
                try:
                    getattr(self, "on_" + kind)(payload)
                except Exception as e:
                    print("handler error:", e)
        except queue.Empty:
            pass
        self.after(200, self.drain)

    def on_error(self, msg):
        if "401" in msg or "403" in msg:
            self.set_banner("The database rules are blocking access. Re-check the Rules step.")
        else:
            self.set_banner("Can't reach the server. Retrying...")
        if not self.ready and self.pending_name:
            self.after(5000, lambda: self.claim(self.pending_name))

    # ---------------- startup / name tag ----------------
    def startup(self):
        if "YOUR-PROJECT" in FIREBASE_URL:
            self.set_banner("Open study_buddy.py and paste your Firebase URL at the top.")
            return
        name = self.cfg.get("name")
        if name:
            self.claim(name)
        else:
            self.ask_name()

    def ask_name(self, message="Pick your name tag (letters, numbers, _ or -):"):
        dlg = ctk.CTkInputDialog(text=message, title="Study Buddy")
        name = (dlg.get_input() or "").strip()
        if not name:
            self.destroy()
            return
        if not NAME_RE.match(name):
            self.ask_name("Use 2-20 letters, numbers, _ or - (with at least one letter):")
            return
        self.claim(name)

    def claim(self, name):
        self.pending_name = name
        key = name.lower()

        def job():
            rec = self.api.get(f"users/{key}")
            self.results.put(("claim", (name, key, rec)))

        self.jobs.put(job)

    def on_claim(self, payload):
        if self.ready:
            return
        name, key, rec = payload
        if isinstance(rec, dict) and rec.get("uid") and rec["uid"] != self.cfg["uid"]:
            self.cfg.pop("name", None)
            self.ask_name(f"'{name}' is already taken. Pick another name tag:")
            return
        rec = rec if isinstance(rec, dict) else {}
        total = rec.get("total", 0) or 0
        # bank time from a session that ended without a clean close
        if rec.get("status") == "studying" and rec.get("since") and rec.get("last_seen"):
            total += max(0, (rec["last_seen"] - rec["since"]) / 1000)
        self.name, self.key = name, key
        self.cfg["name"] = name
        save_config(self.cfg)
        self.me = {"status": "idle", "since": None, "total": int(total)}
        self.ready = True
        self.name_label.configure(text=name)
        self.highlight()
        self.push_me()
        self.poll_now()
        if not self.loops_started:
            self.loops_started = True
            self.after(POLL_SECONDS * 1000, self.poll_loop)
            self.after(HEARTBEAT_SECONDS * 1000, self.hb_loop)

    # ---------------- network loops ----------------
    def poll_loop(self):
        self.poll_now()
        self.after(POLL_SECONDS * 1000, self.poll_loop)

    def hb_loop(self):
        self.push_me()
        self.after(HEARTBEAT_SECONDS * 1000, self.hb_loop)

    def push_me(self):
        if not self.ready:
            return
        data = {
            "name": self.name,
            "uid": self.cfg["uid"],
            "status": self.me["status"],
            "since": self.me["since"],
            "total": int(self.me["total"]),
        }

        def job():
            self.hb_local_ms = time.time() * 1000
            self.api.patch(f"users/{self.key}", dict(data, last_seen={".sv": "timestamp"}))

        self.jobs.put(job)

    def poll_now(self):
        if not self.ready:
            return

        def job():
            hb = self.hb_local_ms
            users = self.api.get("users") or {}
            self.results.put(("poll", (users, hb)))

        self.jobs.put(job)

    def on_poll(self, payload):
        users, hb = payload
        if not isinstance(users, dict):
            users = {}
        self.users = users
        own = users.get(self.key) or {}
        ls = own.get("last_seen")
        if ls and hb and ls != self.seen_own_ls:
            # learn the difference between this PC's clock and the server's
            self.offset_ms = ls - hb
            self.seen_own_ls = ls
        self.set_banner("")
        self.refresh_rows()

    # ---------------- logic ----------------
    def server_now(self):
        return time.time() * 1000 + self.offset_ms

    def set_status(self, new):
        if not self.ready or self.me["status"] == new:
            return
        now = self.server_now()
        if self.me["status"] == "studying" and self.me["since"]:
            self.me["total"] += max(0, (now - self.me["since"]) / 1000)
            self.me["since"] = None
        if new == "studying":
            self.me["since"] = int(now)
        self.me["status"] = new
        self.push_me()
        self.highlight()
        self.refresh_rows()

    def entry(self, key):
        now = self.server_now()
        if key == self.key:
            rec, online, name = self.me, True, self.name
        else:
            rec = self.users.get(key)
            if not isinstance(rec, dict):
                return {"key": key, "name": key, "status": "offline", "total": 0, "missing": True}
            online = now - (rec.get("last_seen") or 0) < ONLINE_WINDOW_MS
            name = rec.get("name", key)
        status = rec.get("status", "idle")
        total = rec.get("total", 0) or 0
        since = rec.get("since")
        if status == "studying" and since:
            end = now if online else (rec.get("last_seen") or since)
            total += max(0, (end - since) / 1000)
        shown = status if online else "offline"
        if shown not in STATUSES:
            shown = "idle"
        return {"key": key, "name": name, "status": shown, "total": total}

    def add_friend(self):
        raw = self.friend_entry.get().strip().lower()
        self.friend_entry.delete(0, "end")
        if not raw:
            return
        if not NAME_RE.match(raw) or raw == self.key or raw in self.cfg["friends"]:
            self.set_banner("That name tag is invalid or already added.")
            return
        self.cfg["friends"].append(raw)
        save_config(self.cfg)
        self.set_banner("")
        self.refresh_rows()
        self.poll_now()

    def remove_friend(self, key):
        if key in self.cfg["friends"]:
            self.cfg["friends"].remove(key)
            save_config(self.cfg)
            self.refresh_rows()

    # ---------------- leaderboard drawing ----------------
    def refresh_rows(self):
        if not self.ready:
            return
        keys = [self.key] + [f for f in self.cfg["friends"] if f != self.key]
        entries = [self.entry(k) for k in keys]
        entries.sort(key=lambda e: -e["total"])
        order = [e["key"] for e in entries]
        if order != self.row_order:
            self.build_rows(entries)
            self.row_order = order
        self.update_rows(entries)

    def build_rows(self, entries):
        for w in self.list_frame.winfo_children():
            w.destroy()
        self.rows = {}
        for i, e in enumerate(entries, 1):
            is_me = e["key"] == self.key
            row = ctk.CTkFrame(self.list_frame, corner_radius=10,
                               fg_color="#2b3a4f" if is_me else "#2a2a2e")
            row.pack(fill="x", pady=3, padx=2)
            row.grid_columnconfigure(2, weight=1)
            ctk.CTkLabel(row, text=str(i), width=24, text_color="#888").grid(
                row=0, column=0, rowspan=2, padx=(8, 0))
            dot = ctk.CTkLabel(row, text="\u25cf", width=20, font=("Segoe UI", 18))
            dot.grid(row=0, column=1, rowspan=2, padx=4)
            ctk.CTkLabel(row, text=e["name"], anchor="w", font=("Segoe UI", 15, "bold")).grid(
                row=0, column=2, sticky="w", pady=(6, 0))
            status = ctk.CTkLabel(row, text="", anchor="w", text_color="#aaa")
            status.grid(row=1, column=2, sticky="w", pady=(0, 6))
            total = ctk.CTkLabel(row, text="", font=("Consolas", 15))
            total.grid(row=0, column=3, rowspan=2, padx=8)
            if not is_me:
                ctk.CTkButton(
                    row, text="\u2715", width=28, fg_color="transparent", hover_color="#552222",
                    command=lambda k=e["key"]: self.after(150, lambda: self.remove_friend(k)),
                ).grid(row=0, column=4, rowspan=2, padx=(0, 6))
            self.rows[e["key"]] = {"dot": dot, "status": status, "total": total, "last": None}

    def update_rows(self, entries):
        for e in entries:
            r = self.rows.get(e["key"])
            if not r:
                continue
            label, color = STATUSES[e["status"]]
            if e.get("missing"):
                label = "Not found yet"
            vals = (label, color, fmt(e["total"]))
            if r["last"] == vals:
                continue
            r["last"] = vals
            r["dot"].configure(text_color=color)
            r["status"].configure(text=label)
            r["total"].configure(text=vals[2])

    def tick(self):
        try:
            if self.ready:
                st = self.me["status"]
                if st == "studying" and self.me["since"]:
                    sess = (self.server_now() - self.me["since"]) / 1000
                else:
                    sess = 0
                color = STATUSES[st][1] if st in ("studying", "break", "away") else "#cfcfcf"
                self.session_label.configure(text=fmt(sess), text_color=color)
                self.refresh_rows()
        except Exception as e:
            print("tick error:", e)
        self.after(1000, self.tick)

    # ---------------- closing ----------------
    def on_close(self):
        if self.ready:
            total = self.me["total"]
            if self.me["status"] == "studying" and self.me["since"]:
                total += max(0, (self.server_now() - self.me["since"]) / 1000)
            try:
                requests.patch(
                    f"{FIREBASE_URL.rstrip('/')}/users/{self.key}.json",
                    json={"status": "offline", "since": None, "total": int(total),
                          "last_seen": {".sv": "timestamp"}},
                    timeout=3,
                )
            except Exception:
                pass
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
