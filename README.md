# WhatsApp Media Date Fixer

Fixes file dates on WhatsApp media after a phone change or backup restore.

## Problem

When WhatsApp media is restored from a backup (new phone, reinstall, Google
Drive/local backup restore), Android resets the file's modified date to the
restore time instead of the original date the photo/video/voice note was
received. Gallery apps that sort by "date modified" then show everything
under today's date instead of the real timeline.

The original date is still recoverable — it's embedded in the filename
(`IMG-20231105-WA0003.jpg`, `WhatsApp Image 2023-11-05 at 14.30.22.jpg`,
etc.). Both tools parse that date and write it back to the file.

## Two tools, two situations

| | `WhatsAppDateFixer.py` | `WhatsAppDateFixADB.py` |
|---|---|---|
| Runs against | Files already copied to your PC | Files still on the phone |
| Connection | None | USB, via ADB |
| File types | Images, video, audio, docs | JPEG (metadata write); any type (date-only fix) |
| Use when | You've pulled the WhatsApp folder off the phone already | You want to fix it directly on-device, no manual copy step |

Use the desktop version if you already have (or plan to copy) the media to
your computer. Use the ADB version if you'd rather fix it in place on the
phone and not deal with a copy round-trip.

## Requirements (both tools)

- Python 3.10 or newer — the scripts use `X | None` type hints, which
  fail on older versions. Get it from
  [python.org](https://www.python.org/downloads/). On Windows, tick
  "Add python.exe to PATH" during install.
- `tkinter` — included with the standard Windows/macOS Python installer.
  On Linux it's usually a separate package (`sudo apt install
  python3-tk` on Debian/Ubuntu).
- Install the pip packages listed under each tool below:
  `pip install tkinterdnd2 piexif pillow`

---

## `WhatsAppDateFixer.py` (desktop)

Point it at a folder or drop files/folders onto it. For each file:

- Sets Windows file creation + modified time from the date in the filename
- Writes EXIF `DateTimeOriginal` into JPEGs (needs `piexif`)
- Embeds `creation_time` into video containers via ffmpeg remux, lossless
  (needs `ffmpeg` on PATH)

### Requirements

Same as above, plus:

`ffmpeg` on PATH is optional — without it, video files still get their
filesystem timestamp fixed, just not the embedded container metadata.

### Usage

Run the script, then either:
- drag files or a folder onto the window, or
- use `Browse…` for a folder, or `Files…` to pick specific files, or
- type a folder path directly into the field

Check **Dry run** first to preview what would change before writing
anything. Click **Fix Dates**.

### Notes

- Recognizes: `IMG-`, `VID-`, `PTT-`, `AUD-`, `DOC-` Android naming,
  `PHOTO-YYYY-MM-DD-HH-MM-SS` (iPhone-style), and `WhatsApp Image
  YYYY-MM-DD at HH.MM.SS`. Falls back to any `YYYYMMDD` sequence found in
  the filename if none of those match.
- No time in the filename → defaults to 12:00:00.
- The file-picker queue (`Files…` / drag-drop) clears itself after a
  completed (non-dry-run) pass. Folder mode does not clear the folder
  field, since re-running against the same folder is a normal use case
  there.

---

## `WhatsAppDateFixADB.py` (on-device via ADB)

Runs from your PC, operates on the phone over USB — no manual copying,
no Termux.

Per file:

1. Parse the date from the filename.
2. Read the file's current mtime from the device.
3. If filename date and mtime agree (same day) → mtime already has a real
   time component, trust it, don't touch the file.
4. If they disagree → mtime was reset by the restore. Fix mtime via
   `touch -t`. For JPEGs, also pull the file, patch EXIF locally, and push
   it back.

The pull/patch/push path never touches the original file directly: it
patches a local copy, pushes to a temp path on the device, verifies the
MD5 matches at each step, then does an atomic rename over the original
only after everything checks out. If any step fails, the original file
is untouched.

### Requirements

Same as above, plus:

Android Platform Tools (`adb`) on PATH. USB debugging enabled on the
phone, and you'll need to accept the RSA authorization prompt on first
connect.

### Usage

Run the script. It checks for a connected device automatically. Folder
list defaults to the standard Android 11+ WhatsApp media paths — edit the
list in the text box if your device uses different paths (older Android,
WhatsApp Business, custom storage location).

Check **Dry run** first — it's the default. Click **Scan & Fix via ADB**.

### Limitations

- Only JPEG gets metadata patched. Video files get the mtime fix but not
  embedded container metadata — see below.
- Video files pulled/pushed over USB for a metadata fix would be far
  slower than JPEGs given typical WhatsApp video sizes; not currently
  implemented. Check whether your videos even carry a `creation_time`
  container tag before deciding you need this (`ffprobe -v quiet
  -show_entries format_tags=creation_time -of default=nw=1 file.mp4` on a
  pulled sample) — if they don't, the mtime fix alone is sufficient for
  anything that sorts by filesystem date.
- Large file transfers may hit the default ADB command timeout.

---

## Back up first

Both tools write directly to your files — EXIF gets rewritten in place,
video containers get remuxed, and the ADB tool overwrites files on your
phone. There is no undo. Back up the media before running either tool,
and run with dry run on first to check the results table before
committing to a real pass.

Neither tool does deduplication, file renaming, or date recovery when
the filename no longer matches any of the known patterns.
