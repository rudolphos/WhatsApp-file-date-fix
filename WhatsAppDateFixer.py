"""
WhatsApp Media Date Fixer
=========================
Fixes file timestamps (and EXIF for JPEGs) based on WhatsApp filename conventions.

Supported filename patterns:
  IMG-YYYYMMDD-WAxxxx.jpg/png/webp       (Android received images)
  VID-YYYYMMDD-WAxxxx.mp4/3gp            (Android received videos)
  PTT-YYYYMMDD-WAxxxx.opus               (voice messages)
  AUD-YYYYMMDD-WAxxxx.m4a/mp3/ogg        (audio)
  DOC-YYYYMMDD-WAxxxx.*                  (documents)
  PHOTO-YYYY-MM-DD-HH-MM-SS.jpg          (iPhone-style, has time)
  WhatsApp Image YYYY-MM-DD at HH.MM.SS  (newer naming)

What it does per file:
  - Sets Windows file creation + modified time from filename date
  - Writes EXIF DateTimeOriginal into JPEG/PNG (via piexif)
  - Embeds creation_time into MP4/video via ffmpeg (optional, needs ffmpeg on PATH)

Dependencies:
  pip install tkinterdnd2 piexif pillow
  ffmpeg on PATH for video metadata embedding (optional)
"""

import os
import re
import ctypes
import ctypes.wintypes
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from datetime import datetime, timezone
import threading
import queue
import traceback

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except ImportError:
    HAS_DND = False

try:
    import piexif
    HAS_PIEXIF = True
except ImportError:
    HAS_PIEXIF = False

try:
    import subprocess
    _ffmpeg = subprocess.run(["ffmpeg", "-version"], capture_output=True)
    HAS_FFMPEG = _ffmpeg.returncode == 0
except Exception:
    HAS_FFMPEG = False

#CONFIG
DEFAULT_TIME = "12:00:00"   # fallback time when filename has no time component
RECURSE_SUBDIRS = True       # process subdirectories
DRY_RUN = False              # set True to preview without changing anything


# Regex patterns: each yields (year, month, day, hour, minute, second) or partial
PATTERNS = [
    # PHOTO-2023-11-05-14-30-22.jpg  (iPhone with time)
    (re.compile(r'^(?:PHOTO|IMG|VID)-(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})', re.I),
     lambda m: (int(m[0]), int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5]))),

    # WhatsApp Image 2023-11-05 at 14.30.22.jpg
    (re.compile(r'(\d{4})-(\d{2})-(\d{2}) at (\d{2})\.(\d{2})\.(\d{2})', re.I),
     lambda m: (int(m[0]), int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5]))),

    # IMG-20231105-WA0003.jpg  (standard Android, date only)
    (re.compile(r'^(?:IMG|VID|PTT|AUD|DOC|STK|GIF)-(\d{4})(\d{2})(\d{2})-WA\d+', re.I),
     lambda m: (int(m[0]), int(m[1]), int(m[2]), None, None, None)),

    # Fallback: any YYYYMMDD or YYYY-MM-DD anywhere in filename
    (re.compile(r'(\d{4})[_-]?(\d{2})[_-]?(\d{2})'),
     lambda m: (int(m[0]), int(m[1]), int(m[2]), None, None, None)),
]

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp'}
VIDEO_EXTS = {'.mp4', '.3gp', '.mkv', '.mov', '.avi'}


def parse_filename_date(filename: str) -> datetime | None:
    stem = os.path.splitext(filename)[0]
    for pattern, extractor in PATTERNS:
        m = pattern.search(stem)
        if m:
            try:
                yr, mo, dy, hr, mn, sc = extractor(m.groups())
                if hr is None:
                    dft = [int(x) for x in DEFAULT_TIME.split(':')]
                    hr, mn, sc = dft
                return datetime(yr, mo, dy, hr, mn, sc)
            except (ValueError, TypeError):
                continue
    return None


def set_timestamps_ctypes(path: str, dt: datetime):
    """Set creation, access, and modified time on Windows via ctypes (no pywin32 needed)."""
    import time as _time

    # FILETIME = 100-nanosecond intervals since 1601-01-01 00:00:00 UTC
    EPOCH_AS_FILETIME = 116444736000000000
    ts = int(_time.mktime(dt.timetuple()) * 10_000_000) + EPOCH_AS_FILETIME

    # Use a c_uint64 to avoid signed 32-bit truncation in wintypes.FILETIME
    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", ctypes.c_uint32),
                    ("dwHighDateTime", ctypes.c_uint32)]

    ft = FILETIME(dwLowDateTime=ts & 0xFFFFFFFF, dwHighDateTime=ts >> 32)

    kernel32 = ctypes.windll.kernel32
    kernel32.SetFileTime.restype = ctypes.wintypes.BOOL

    GENERIC_WRITE = 0x40000000
    FILE_SHARE_ALL = 0x7
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000

    handle = kernel32.CreateFileW(
        path, GENERIC_WRITE, FILE_SHARE_ALL, None,
        OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL | FILE_FLAG_BACKUP_SEMANTICS, None
    )
    if handle == ctypes.wintypes.HANDLE(-1).value:
        raise OSError(f"CreateFileW failed (err={kernel32.GetLastError()}) for {path}")
    try:
        ok = kernel32.SetFileTime(handle, ctypes.byref(ft), ctypes.byref(ft), ctypes.byref(ft))
        if not ok:
            raise OSError(f"SetFileTime failed (err={kernel32.GetLastError()}) for {path}")
    finally:
        kernel32.CloseHandle(handle)


def write_jpeg_exif(path: str, dt: datetime) -> bool:
    if not HAS_PIEXIF:
        return False
    dt_str = dt.strftime("%Y:%m:%d %H:%M:%S").encode()
    try:
        try:
            exif_dict = piexif.load(path)
        except Exception:
            exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}
        exif_dict.setdefault("Exif", {})
        exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = dt_str
        exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = dt_str
        exif_dict.setdefault("0th", {})
        exif_dict["0th"][piexif.ImageIFD.DateTime] = dt_str
        piexif.insert(piexif.dump(exif_dict), path)
        # piexif.insert() rewrites the file and resets mtime — re-stamp immediately
        set_timestamps_ctypes(path, dt)
        return True
    except Exception:
        return False


def embed_video_date_ffmpeg(path: str, dt: datetime) -> bool:  # dt already in signature
    """Re-mux video with creation_time metadata via ffmpeg (lossless for MP4)."""
    if not HAS_FFMPEG:
        return False
    ext = os.path.splitext(path)[1]
    tmp = path + ".tmp_wa" + ext
    dt_str = dt.strftime("%Y-%m-%dT%H:%M:%S")
    cmd = [
        "ffmpeg", "-y", "-i", path,
        "-c", "copy",
        "-metadata", f"creation_time={dt_str}",
        tmp
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=60)
        if r.returncode == 0 and os.path.exists(tmp):
            os.replace(tmp, path)
            set_timestamps_ctypes(path, dt)
            return True
        if os.path.exists(tmp):
            os.remove(tmp)
    except Exception:
        pass
    return False


def process_file(path: str) -> dict:
    filename = os.path.basename(path)
    ext = os.path.splitext(filename)[1].lower()
    result = {"path": path, "filename": filename, "status": "skipped", "date": None, "details": ""}

    dt = parse_filename_date(filename)
    if dt is None:
        result["status"] = "no_date"
        result["details"] = "No date found in filename"
        return result

    result["date"] = dt.strftime("%Y-%m-%d %H:%M:%S")

    if DRY_RUN:
        result["status"] = "dry_run"
        result["details"] = f"Would set → {result['date']}"
        return result

    actions = []
    try:
        set_timestamps_ctypes(path, dt)
        actions.append("timestamps")
    except Exception as e:
        actions.append(f"timestamps FAILED: {e}")

    if ext in IMAGE_EXTS and ext in ('.jpg', '.jpeg'):
        ok = write_jpeg_exif(path, dt)
        actions.append("EXIF" if ok else "EXIF skipped (piexif?)")

    if ext in VIDEO_EXTS:
        ok = embed_video_date_ffmpeg(path, dt)
        actions.append("video-meta" if ok else "video-meta skipped (ffmpeg?)")

    result["status"] = "ok"
    result["details"] = ", ".join(actions)
    return result


def collect_files(folder: str) -> list[str]:
    files = []
    if RECURSE_SUBDIRS:
        for root, _, fnames in os.walk(folder):
            for f in fnames:
                files.append(os.path.join(root, f))
    else:
        files = [os.path.join(folder, f) for f in os.listdir(folder)
                 if os.path.isfile(os.path.join(folder, f))]
    return files


#GUI

class App(TkinterDnD.Tk if HAS_DND else tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("WhatsApp Media Date Fixer")
        self.geometry("820x580")
        self.resizable(True, True)
        self.folder = tk.StringVar()
        self.dry_run_var = tk.BooleanVar(value=DRY_RUN)
        self.recurse_var = tk.BooleanVar(value=RECURSE_SUBDIRS)
        self.q = queue.Queue()
        self._file_queue: list[str] = []   # explicit file list (files dropped/picked directly)
        self._build_ui()
        self._poll()

    def _build_ui(self):
        pad = dict(padx=8, pady=4)

        # Folder row
        fr = tk.Frame(self)
        fr.pack(fill="x", **pad)
        tk.Label(fr, text="Folder:").pack(side="left")
        e = tk.Entry(fr, textvariable=self.folder, width=55)
        e.pack(side="left", fill="x", expand=True, padx=4)
        tk.Button(fr, text="Browse…", command=self._browse).pack(side="left")
        tk.Button(fr, text="Files…", command=self._browse_files).pack(side="left", padx=(4, 0))

        # Drop target
        if HAS_DND:
            drop_lbl = tk.Label(self, text="◀  Drag & drop files or a folder here  ▶",
                                relief="groove", height=2, fg="#555",
                                font=("Segoe UI", 9, "italic"))
            drop_lbl.pack(fill="x", padx=8, pady=2)
            drop_lbl.drop_target_register(DND_FILES)
            drop_lbl.dnd_bind("<<Drop>>", self._on_drop)

        # Queued files label
        self.queue_lbl = tk.Label(self, text="No files queued.", anchor="w", fg="#444")
        self.queue_lbl.pack(fill="x", padx=8)

        # Options row
        opt = tk.Frame(self)
        opt.pack(fill="x", **pad)
        tk.Checkbutton(opt, text="Dry run (preview only)", variable=self.dry_run_var).pack(side="left")
        tk.Checkbutton(opt, text="Recurse subdirectories", variable=self.recurse_var).pack(side="left", padx=16)
        tk.Button(opt, text="Clear queue", command=self._clear_queue).pack(side="right")

        # Info bar
        caps = []
        if HAS_PIEXIF: caps.append("EXIF ✓")
        else: caps.append("EXIF ✗ (pip install piexif)")
        if HAS_FFMPEG: caps.append("ffmpeg ✓")
        else: caps.append("ffmpeg ✗ (not on PATH)")
        if HAS_DND: caps.append("DnD ✓")
        tk.Label(self, text="  |  ".join(caps), fg="#666", font=("Segoe UI", 8)).pack(anchor="w", padx=8)

        # Run button
        self.run_btn = tk.Button(self, text="▶  Fix Dates", bg="#2a7", fg="white",
                                  font=("Segoe UI", 10, "bold"),
                                  command=self._run)
        self.run_btn.pack(fill="x", padx=8, pady=4)

        # Progress
        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill="x", padx=8, pady=2)
        self.status_lbl = tk.Label(self, text="Ready.", anchor="w")
        self.status_lbl.pack(fill="x", padx=8)

        # Results table
        cols = ("filename", "date", "status", "details")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=18)
        self.tree.heading("filename", text="Filename")
        self.tree.heading("date", text="Detected Date")
        self.tree.heading("status", text="Status")
        self.tree.heading("details", text="Details")
        self.tree.column("filename", width=260)
        self.tree.column("date", width=140)
        self.tree.column("status", width=80)
        self.tree.column("details", width=260)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=4)
        sb.pack(side="right", fill="y", pady=4, padx=(0, 6))

        # Tag colors
        self.tree.tag_configure("ok", foreground="#1a7a1a")
        self.tree.tag_configure("dry_run", foreground="#1a1a9a")
        self.tree.tag_configure("no_date", foreground="#999")
        self.tree.tag_configure("skipped", foreground="#aaa")
        self.tree.tag_configure("error", foreground="#cc0000")

    def _browse(self):
        d = filedialog.askdirectory()
        if d:
            self.folder.set(d)
            self._file_queue.clear()
            self._update_queue_label()

    def _browse_files(self):
        files = filedialog.askopenfilenames(
            title="Select files",
            filetypes=[("Media files", "*.jpg *.jpeg *.png *.webp *.mp4 *.3gp *.mkv *.mov *.avi *.opus *.m4a *.mp3 *.ogg"), ("All files", "*.*")]
        )
        if files:
            self._file_queue.extend(f for f in files if f not in self._file_queue)
            self.folder.set("")
            self._update_queue_label()

    def _clear_queue(self):
        self._file_queue.clear()
        self.folder.set("")
        self._update_queue_label()

    def _update_queue_label(self):
        if self._file_queue:
            self.queue_lbl.config(text=f"{len(self._file_queue)} file(s) queued individually.")
        elif self.folder.get():
            self.queue_lbl.config(text=f"Folder: {self.folder.get()}")
        else:
            self.queue_lbl.config(text="No files queued.")

    def _on_drop(self, event):
        raw = event.data.strip()
        # tkinterdnd2 wraps paths with spaces in braces: {C:\my path\file.jpg} {C:\other.jpg}
        paths = []
        while raw:
            if raw.startswith("{"):
                end = raw.index("}")
                paths.append(raw[1:end])
                raw = raw[end + 1:].strip()
            else:
                parts = raw.split(None, 1)
                paths.append(parts[0])
                raw = parts[1].strip() if len(parts) > 1 else ""

        folders = [p for p in paths if os.path.isdir(p)]
        files   = [p for p in paths if os.path.isfile(p)]

        if files:
            self._file_queue.extend(f for f in files if f not in self._file_queue)
            if not folders:
                self.folder.set("")
        if folders:
            # If a folder is dropped, use it as the folder source (first one wins)
            if not self._file_queue:
                self.folder.set(folders[0])
            else:
                # Expand folder into file queue too
                for fd in folders:
                    for root, _, fnames in os.walk(fd):
                        for fn in fnames:
                            fp = os.path.join(root, fn)
                            if fp not in self._file_queue:
                                self._file_queue.append(fp)
        self._update_queue_label()

    def _run(self):
        has_queue = bool(self._file_queue)
        folder = self.folder.get().strip()
        if not has_queue and (not folder or not os.path.isdir(folder)):
            messagebox.showerror("Error", "Please select a folder or drop/pick files first.")
            return
        global DRY_RUN, RECURSE_SUBDIRS
        DRY_RUN = self.dry_run_var.get()
        RECURSE_SUBDIRS = self.recurse_var.get()

        self.tree.delete(*self.tree.get_children())
        self.run_btn.config(state="disabled")
        self.status_lbl.config(text="Scanning…")

        if has_queue:
            files = list(self._file_queue)
        else:
            files = None  # worker will call collect_files(folder)

        threading.Thread(target=self._worker, args=(folder, files), daemon=True).start()

    def _worker(self, folder, files):
        try:
            if files is None:
                files = collect_files(folder)
            total = len(files)
            self.q.put(("total", total))
            counts = {"ok": 0, "dry_run": 0, "no_date": 0, "error": 0}
            for i, path in enumerate(files):
                try:
                    r = process_file(path)
                except Exception as e:
                    r = {"path": path, "filename": os.path.basename(path),
                         "status": "error", "date": "", "details": str(e)}
                counts[r["status"]] = counts.get(r["status"], 0) + 1
                self.q.put(("row", r, i + 1, total))
            self.q.put(("done", counts, total))
        except Exception:
            self.q.put(("crash", traceback.format_exc()))

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "total":
                    self.progress["maximum"] = msg[1]
                    self.progress["value"] = 0
                elif msg[0] == "row":
                    _, r, done, total = msg
                    tag = r["status"]
                    self.tree.insert("", "end", values=(
                        r["filename"], r.get("date", ""), r["status"], r["details"]
                    ), tags=(tag,))
                    self.tree.yview_moveto(1)
                    self.progress["value"] = done
                    self.status_lbl.config(text=f"Processing {done}/{total}: {r['filename']}")
                elif msg[0] == "done":
                    _, counts, total = msg
                    verb = "Would fix" if DRY_RUN else "Fixed"
                    fixed = counts.get("ok", 0) + counts.get("dry_run", 0)
                    self.status_lbl.config(
                        text=f"Done. {total} files — {verb} {fixed}, "
                             f"no-date {counts.get('no_date',0)}, "
                             f"errors {counts.get('error',0)}"
                    )
                    self.run_btn.config(state="normal")
                    # Only clear the queue on a real (non-dry-run) pass so a
                    # dry run can still be reviewed and then actually run.
                    if not DRY_RUN:
                        self._file_queue.clear()
                        self.folder.set("")
                        self._update_queue_label()
                elif msg[0] == "crash":
                    messagebox.showerror("Crash", msg[1])
                    self.run_btn.config(state="normal")
        except queue.Empty:
            pass
        self.after(50, self._poll)


if __name__ == "__main__":
    app = App()
    app.mainloop()