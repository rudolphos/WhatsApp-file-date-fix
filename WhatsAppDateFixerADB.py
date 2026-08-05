"""
WhatsApp Media Date Fixer — ADB (runs on Windows, fixes Android)
================================================================
Runs entirely from your PC via ADB. No Termux, no phone interaction.

Heuristic per file:
  - Parse YYYYMMDD from filename
  - Read mtime from device via adb shell stat
  - If filename YMD == mtime YMD  → mtime is trustworthy (has real H:M:S)
      → only write EXIF with mtime value, no touch needed
  - If mismatch → mtime was clobbered
      → set mtime via touch -t, write EXIF from filename date + 12:00:00

EXIF is written by pulling the file, patching locally, pushing back.
Only done for JPEG files, and only when a date change is needed.

Requirements:
  pip install tkinterdnd2 piexif pillow
  adb on PATH (Android Platform Tools)

USB Debugging must be enabled on phone.
"""

import os
import re
import subprocess
import tempfile
import threading
import queue
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime
from pathlib import Path

try:
    from tkinterdnd2 import TkinterDnD
    BASE = TkinterDnD.Tk
except ImportError:
    BASE = tk.Tk

try:
    import piexif
    HAS_PIEXIF = True
except ImportError:
    HAS_PIEXIF = False

#conf
DEFAULT_FOLDERS = [
    # Android 11+ paths (current)
    "/sdcard/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images",
    "/sdcard/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Video",
    "/sdcard/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Audio",
    "/sdcard/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Voice Notes",
    "/sdcard/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Documents",
    # WhatsApp Business (same structure, different package)
    # "/sdcard/Android/media/com.whatsapp.w4b/WhatsApp/Media/WhatsApp Images",
    # Legacy path (Android 10 and below — uncomment if needed)
    # "/sdcard/WhatsApp/Media/WhatsApp Images",
]
FALLBACK_TIME   = (12, 0, 0)
WRITE_EXIF      = True   # pull/patch/push JPEG when date changes
RECURSE         = True

PATTERNS = [
    (re.compile(r'(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})'),
     lambda g: datetime(int(g[0]),int(g[1]),int(g[2]),int(g[3]),int(g[4]),int(g[5]))),
    (re.compile(r'(\d{4})-(\d{2})-(\d{2}) at (\d{2})\.(\d{2})\.(\d{2})'),
     lambda g: datetime(int(g[0]),int(g[1]),int(g[2]),int(g[3]),int(g[4]),int(g[5]))),
    (re.compile(r'[A-Z]{2,4}-(\d{4})(\d{2})(\d{2})-WA\d+', re.I),
     lambda g: datetime(int(g[0]),int(g[1]),int(g[2]),*FALLBACK_TIME)),
    (re.compile(r'(\d{4})(\d{2})(\d{2})'),
     lambda g: datetime(int(g[0]),int(g[1]),int(g[2]),*FALLBACK_TIME)),
]
IMAGE_EXTS = {'.jpg', '.jpeg'}


def adb(args: list, capture=True, timeout=60) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            ["adb"] + args,
            capture_output=capture,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None


def adb_check() -> tuple[bool, str]:
    try:
        r = adb(["devices"])
        lines = [l.strip() for l in r.stdout.splitlines() if l.strip()
                 and not l.startswith("List")]
        if not lines:
            return False, "No device found. Enable USB Debugging and connect phone."
        if "unauthorized" in lines[0]:
            return False, "Device unauthorized — accept the RSA prompt on your phone."
        if "offline" in lines[0]:
            return False, "Device offline — try unplugging and reconnecting."
        return True, lines[0].split()[0]
    except FileNotFoundError:
        return False, "adb not found. Add Android Platform Tools to PATH."


def list_files_adb(folder: str, recurse: bool) -> list[str]:
    if recurse:
        r = adb(["shell", f"find '{folder}' -type f 2>/dev/null"], timeout=120)
    else:
        r = adb(["shell", f"ls -1 '{folder}' 2>/dev/null"], timeout=60)
    if r is None or r.returncode != 0 or not r.stdout or not r.stdout.strip():
        return []
    lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
    if not recurse:
        lines = [f"{folder}/{l}" for l in lines]
    return lines


def get_mtime_adb(path: str) -> datetime | None:
    r = adb(["shell", f"stat -c %Y '{path}'"])
    if r is None or r.returncode != 0 or not r.stdout or not r.stdout.strip():
        return None
    try:
        ts = int(r.stdout.strip())
        return datetime.fromtimestamp(ts)
    except (ValueError, OSError):
        return None


def set_mtime_adb(path: str, dt: datetime):
    t = dt.strftime("%Y%m%d%H%M.%S")
    adb(["shell", f"touch -t {t} '{path}'"])


def md5_device(remote_path: str) -> str | None:
    r = adb(["shell", f"md5sum '{remote_path}'"])
    if r is None or r.returncode != 0 or not r.stdout or not r.stdout.strip():
        return None
    return r.stdout.strip().split()[0].lower()


def parse_filename_date(name: str) -> datetime | None:
    stem = Path(name).stem
    for pat, builder in PATTERNS:
        m = pat.search(stem)
        if m:
            try:
                return builder(m.groups())
            except ValueError:
                continue
    return None


def md5_local(path: str) -> str:
    import hashlib
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def md5_device(remote_path: str) -> str | None:
    """md5sum is available on all Android devices."""
    r = adb(["shell", f"md5sum '{remote_path}'"])
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return r.stdout.strip().split()[0].lower()


def is_valid_jpeg(path: str) -> bool:
    """Check JPEG SOI and EOI markers — catches truncation/corruption."""
    try:
        with open(path, "rb") as f:
            header = f.read(2)
            if header != b'\xff\xd8':
                return False
            f.seek(-2, 2)
            return f.read(2) == b'\xff\xd9'
    except Exception:
        return False


def write_exif_local(local_path: str, dt: datetime) -> bool:
    if not HAS_PIEXIF:
        return False
    dt_str = dt.strftime("%Y:%m:%d %H:%M:%S").encode()
    try:
        try:
            exif = piexif.load(local_path)
        except Exception:
            exif = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}
        exif.setdefault("Exif", {})[piexif.ExifIFD.DateTimeOriginal]  = dt_str
        exif["Exif"][piexif.ExifIFD.DateTimeDigitized]                 = dt_str
        exif.setdefault("0th", {})[piexif.ImageIFD.DateTime]          = dt_str
        piexif.insert(piexif.dump(exif), local_path)
        return True
    except Exception:
        return False


def process_file_adb(remote_path: str, dry_run: bool) -> dict:
    name = remote_path.split("/")[-1]
    ext  = Path(name).suffix.lower()
    res  = {"name": name, "path": remote_path, "status": "ok",
            "date": "", "source": "", "details": ""}

    fname_dt = parse_filename_date(name)
    if fname_dt is None:
        res["status"] = "no_date"
        res["details"] = "no date in filename"
        return res

    mtime_dt = get_mtime_adb(remote_path)
    if mtime_dt is None:
        res["status"] = "error"
        res["details"] = "stat failed"
        return res

    same_day = fname_dt.date() == mtime_dt.date()

    if same_day:
        use_dt = mtime_dt
        res["source"] = "mtime"
        res["date"]   = use_dt.strftime("%Y-%m-%d %H:%M:%S")
    else:
        use_dt = fname_dt
        res["source"] = "filename"
        res["date"]   = use_dt.strftime("%Y-%m-%d %H:%M:%S")

    # Skip if already fixed: for filename-source files, check if mtime already
    # matches the filename date (within 60s tolerance — touch precision)
    if not same_day:
        delta = abs((mtime_dt - use_dt).total_seconds())
        if delta < 60:
            res["status"]  = "skipped"
            res["details"] = "already fixed"
            return res
    # For mtime-source files: if EXIF write is disabled or not a JPEG, nothing
    # to do either — mtime is already correct by definition (same_day passed)
    elif ext not in IMAGE_EXTS or not WRITE_EXIF or not HAS_PIEXIF:
        res["status"]  = "skipped"
        res["details"] = "already correct"
        return res

    if dry_run:
        res["status"]  = "dry_run"
        res["details"] = f"would use {res['source']}"
        return res

    actions = []

    # Set mtime only if filename won (mtime was clobbered)
    if not same_day:
        set_mtime_adb(remote_path, use_dt)
        actions.append("mtime✓")

    # Write EXIF: safe pull → verify → patch → verify → push to temp → verify → atomic rename
    if WRITE_EXIF and ext in IMAGE_EXTS and HAS_PIEXIF:
        remote_tmp = remote_path + ".wa_tmp"
        with tempfile.TemporaryDirectory() as tmp:
            local = os.path.join(tmp, name)

            # 1. Pull
            pull = adb(["pull", remote_path, local])
            if pull.returncode != 0 or not os.path.exists(local):
                actions.append("pull_failed"); goto_cleanup = True
            else:
                # 2. Verify pull integrity: hash must match device
                hash_before_device = md5_device(remote_path)
                hash_pulled         = md5_local(local)
                if hash_before_device != hash_pulled:
                    actions.append(f"pull_hash_mismatch"); goto_cleanup = True
                elif not is_valid_jpeg(local):
                    actions.append("pulled_jpeg_invalid"); goto_cleanup = True
                else:
                    goto_cleanup = False

            if not goto_cleanup:
                # 3. Patch EXIF on local copy
                ok = write_exif_local(local, use_dt)
                if not ok:
                    actions.append("exif_patch_failed")
                elif not is_valid_jpeg(local):
                    # piexif produced a broken file — do NOT push
                    actions.append("patched_jpeg_invalid")
                else:
                    # 4. Push to TEMP path (never touching original yet)
                    push = adb(["push", local, remote_tmp])
                    if push.returncode != 0:
                        actions.append("push_tmp_failed")
                        adb(["shell", f"rm -f '{remote_tmp}'"])
                    else:
                        # 5. Verify pushed file hash matches local patched file
                        hash_patched_local  = md5_local(local)
                        hash_patched_device = md5_device(remote_tmp)
                        if hash_patched_local != hash_patched_device:
                            actions.append("push_hash_mismatch")
                            adb(["shell", f"rm -f '{remote_tmp}'"])
                        else:
                            # 6. Atomic rename temp → original (original only replaced NOW)
                            mv = adb(["shell", f"mv '{remote_tmp}' '{remote_path}'"])
                            if mv.returncode != 0:
                                actions.append("rename_failed")
                                adb(["shell", f"rm -f '{remote_tmp}'"])
                            else:
                                # 7. Final verify
                                hash_final = md5_device(remote_path)
                                if hash_final != hash_patched_local:
                                    actions.append("final_verify_failed⚠")
                                else:
                                    actions.append("EXIF✓")

    res["details"] = ", ".join(actions) if actions else "timestamps ok"
    return res


class App(BASE):
    def __init__(self):
        super().__init__()
        self.title("WhatsApp ADB Date Fixer")
        self.geometry("900x620")
        self.resizable(True, True)
        self.dry_run_var  = tk.BooleanVar(value=True)   # safe default
        self.exif_var     = tk.BooleanVar(value=WRITE_EXIF)
        self.recurse_var  = tk.BooleanVar(value=RECURSE)
        self.q = queue.Queue()
        self._stop_flag = threading.Event()
        self._build_ui()
        self._poll()
        self.after(200, self._check_adb)

    def _build_ui(self):
        pad = dict(padx=8, pady=3)

        # ADB status bar
        self.adb_lbl = tk.Label(self, text="Checking ADB...", fg="gray",
                                font=("Segoe UI", 9), anchor="w")
        self.adb_lbl.pack(fill="x", padx=8, pady=(6,0))

        ttk.Separator(self).pack(fill="x", padx=8, pady=4)

        # Folders list
        fl = tk.Frame(self)
        fl.pack(fill="x", **pad)
        tk.Label(fl, text="Android folders to process:", anchor="w").pack(fill="x")
        self.folders_text = tk.Text(fl, height=5, font=("Consolas", 9), wrap="none")
        self.folders_text.pack(fill="x")
        self.folders_text.insert("end", "\n".join(DEFAULT_FOLDERS))
        sb = ttk.Scrollbar(fl, orient="horizontal", command=self.folders_text.xview)
        self.folders_text.configure(xscrollcommand=sb.set)
        sb.pack(fill="x")

        # Options
        opt = tk.Frame(self)
        opt.pack(fill="x", **pad)
        tk.Checkbutton(opt, text="Dry run (preview only — safe to test first)",
                       variable=self.dry_run_var).pack(side="left")
        tk.Checkbutton(opt, text="Write EXIF into JPEGs (pull/push)",
                       variable=self.exif_var).pack(side="left", padx=16)
        tk.Checkbutton(opt, text="Recurse subdirs",
                       variable=self.recurse_var).pack(side="left", padx=8)

        # Caps bar
        caps = []
        caps.append("piexif ✓" if HAS_PIEXIF else "piexif ✗ (pip install piexif pillow)")
        tk.Label(self, text="  ".join(caps), fg="#666",
                 font=("Segoe UI", 8), anchor="w").pack(fill="x", padx=8)

        # Run
        btn_row = tk.Frame(self)
        btn_row.pack(fill="x", padx=8, pady=4)
        self.run_btn = tk.Button(btn_row, text="▶  Scan & Fix via ADB",
                                  bg="#2a7", fg="white",
                                  font=("Segoe UI", 10, "bold"),
                                  state="disabled", command=self._run)
        self.run_btn.pack(side="left", fill="x", expand=True)
        self.stop_btn = tk.Button(btn_row, text="■  Stop",
                                   bg="#c33", fg="white",
                                   font=("Segoe UI", 10, "bold"),
                                   state="disabled", command=self._stop,
                                   width=10)
        self.stop_btn.pack(side="left", padx=(6, 0))

        # Progress
        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill="x", padx=8)
        self.status_lbl = tk.Label(self, text="Waiting for ADB...", anchor="w")
        self.status_lbl.pack(fill="x", padx=8)

        # Results table
        cols = ("filename", "source", "date", "status", "details")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=16)
        self.tree.heading("filename", text="Filename")
        self.tree.heading("source",   text="Source")
        self.tree.heading("date",     text="Date Used")
        self.tree.heading("status",   text="Status")
        self.tree.heading("details",  text="Details")
        self.tree.column("filename", width=240)
        self.tree.column("source",   width=70)
        self.tree.column("date",     width=140)
        self.tree.column("status",   width=70)
        self.tree.column("details",  width=220)
        sb2 = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb2.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(8,0), pady=4)
        sb2.pack(side="right", fill="y", padx=(0,6), pady=4)

        self.tree.tag_configure("ok",      foreground="#1a7a1a")
        self.tree.tag_configure("dry_run", foreground="#1a1a9a")
        self.tree.tag_configure("no_date", foreground="#aaa")
        self.tree.tag_configure("skipped", foreground="#666")
        self.tree.tag_configure("error",   foreground="#cc0000")

    def _check_adb(self):
        ok, msg = adb_check()
        if ok:
            self.adb_lbl.config(text=f"ADB ✓  Device: {msg}", fg="#1a7a1a")
            self.run_btn.config(state="normal")
            self.status_lbl.config(text="Ready.")
        else:
            self.adb_lbl.config(text=f"ADB ✗  {msg}", fg="#cc0000")
            self.run_btn.config(state="disabled")
            self.status_lbl.config(text="ADB not ready.")
        self.after(3000, self._check_adb)

    def _get_folders(self) -> list[str]:
        raw = self.folders_text.get("1.0", "end").strip()
        return [l.strip() for l in raw.splitlines() if l.strip()]

    def _stop(self):
        self._stop_flag.set()
        self.stop_btn.config(state="disabled")
        self.status_lbl.config(text="Stopping after current file...")

    def _run(self):
        self.tree.delete(*self.tree.get_children())
        self._stop_flag.clear()
        self.run_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.tree.delete(*self.tree.get_children())
        self.run_btn.config(state="disabled")
        global WRITE_EXIF, RECURSE
        WRITE_EXIF = self.exif_var.get()
        RECURSE    = self.recurse_var.get()
        dry = self.dry_run_var.get()
        folders = self._get_folders()
        threading.Thread(target=self._worker, args=(folders, dry), daemon=True).start()

    def _worker(self, folders, dry_run):
        import traceback
        try:
            all_files = []
            self.q.put(("status", "Listing files on device..."))
            for folder in folders:
                files = list_files_adb(folder, RECURSE)
                all_files.extend(files)

            total = len(all_files)
            self.q.put(("total", total))
            self.q.put(("status", f"Found {total} files. Processing..."))

            counts = {}
            for i, path in enumerate(all_files):
                if self._stop_flag.is_set():
                    self.q.put(("done", counts, i, dry_run, True))
                    return
                try:
                    r = process_file_adb(path, dry_run)
                except Exception as e:
                    r = {"name": path.split("/")[-1], "path": path,
                         "status": "error", "source": "", "date": "",
                         "details": str(e)}
                counts[r["status"]] = counts.get(r["status"], 0) + 1
                self.q.put(("row", r, i+1, total))

            self.q.put(("done", counts, total, dry_run, False))
        except Exception:
            self.q.put(("crash", traceback.format_exc()))

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "total":
                    self.progress["maximum"] = max(msg[1], 1)
                    self.progress["value"] = 0
                elif msg[0] == "status":
                    self.status_lbl.config(text=msg[1])
                elif msg[0] == "row":
                    _, r, done, total = msg
                    self.tree.insert("", "end",
                        values=(r["name"], r["source"], r["date"],
                                r["status"], r["details"]),
                        tags=(r["status"],))
                    self.tree.yview_moveto(1)
                    self.progress["value"] = done
                    self.status_lbl.config(
                        text=f"{done}/{total}: {r['name']}")
                elif msg[0] == "done":
                    _, counts, total, dry, stopped = msg
                    verb = "Stopped" if stopped else ("Preview" if dry else "Done")
                    self.status_lbl.config(
                        text=f"{verb}. {total} files — "
                             f"ok:{counts.get('ok',0)}  "
                             f"dry:{counts.get('dry_run',0)}  "
                             f"no-date:{counts.get('no_date',0)}  "
                             f"errors:{counts.get('error',0)}"
                    )
                    self.run_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                elif msg[0] == "crash":
                    messagebox.showerror("Error", msg[1])
                    self.run_btn.config(state="normal")
        except queue.Empty:
            pass
        self.after(60, self._poll)


if __name__ == "__main__":
    app = App()
    app.mainloop()
