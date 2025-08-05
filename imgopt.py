#!/usr/bin/env python
"""
Image Bulk Optimizer – GUI Edition
Version 1.0.0 – 2025‑08‑05

A cross‑platform drag‑and‑drop desktop tool that recursively scans a folder for JPEG/PNG/WebP files,
strips EXIF/ICC metadata, and saves optimized copies either in‑place or to a chosen output folder.

Pack as a single Windows/Linux/macOS executable via PyInstaller:
    pip install pillow pyinstaller
    pyinstaller --onefile --noconsole --name imgopt image_bulk_optimizer_gui.py

If the lossless PNG compressor *oxipng* is available on PATH, it will be invoked automatically for
extra compression.

© 2025 – MIT License
"""
from __future__ import annotations

import hashlib
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import List, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, UnidentifiedImageError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SUPPORTED_EXTS: Tuple[str, ...] = ('.jpg', '.jpeg', '.png', '.webp')
MAX_WORKERS = max(4, (os.cpu_count() or 4))


# ---------------------------------------------------------------------------
# Stats helper
# ---------------------------------------------------------------------------
class Stats:
    """Collects runtime statistics."""

    def __init__(self) -> None:
        self.total_files = 0
        self.optimized_files = 0
        self.total_original_bytes = 0
        self.total_optimized_bytes = 0
        self.failures: List[str] = []

    @property
    def percent_saved(self) -> float:
        if self.total_original_bytes == 0:
            return 0.0
        saved = self.total_original_bytes - self.total_optimized_bytes
        return 100 * saved / self.total_original_bytes

    def add(self, original_size: int, new_size: int) -> None:
        self.total_files += 1
        self.optimized_files += 1
        self.total_original_bytes += original_size
        self.total_optimized_bytes += new_size

    def as_summary(self) -> str:
        saved_mb = (self.total_original_bytes - self.total_optimized_bytes) / 1_048_576
        return (
            f'Optimized {self.optimized_files}/{self.total_files} images · '
            f'Saved {saved_mb:.2f} MiB (↓ {self.percent_saved:.1f}%)'
        )


# ---------------------------------------------------------------------------
# Image processing helpers
# ---------------------------------------------------------------------------

def strip_exif(img: Image.Image) -> Image.Image:
    """Return a copy of *img* with all EXIF/ICC metadata stripped."""
    pixels = list(img.getdata())
    cleaned = Image.new(img.mode, img.size)
    cleaned.putdata(pixels)
    return cleaned


def slug_for(src: Path, img: Image.Image) -> str:
    stem = src.stem
    ext = src.suffix.lower().lstrip('.')
    h = hashlib.blake2b(digest_size=4)
    h.update(img.tobytes()[:32_768])  # hash first 32 KiB for speed
    digest = h.hexdigest()
    return f'{stem}-{img.width}x{img.height}-{digest}.{ext}'


def call_oxipng(png_path: Path) -> None:
    """Invoke oxipng in‑place if available; silently ignore errors."""
    try:
        subprocess.run(
            ['oxipng', '--strip', 'all', '--opt', 'max', '--preserve', str(png_path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass


def optimize_image(
    src: Path,
    dest_dir: Path,
    quality: int,
    convert_png_to_webp: bool,
    stats: Stats,
    log_queue: 'queue.Queue[str | tuple]'
) -> None:
    """Optimize *src* and write to *dest_dir* (can equal src.parent)."""
    try:
        with Image.open(src) as im:
            im = strip_exif(im)
            out_ext = src.suffix.lower()
            save_kwargs = {'optimize': True}

            if out_ext in {'.jpg', '.jpeg'}:
                save_kwargs.update({'quality': quality, 'progressive': True})
            elif out_ext == '.png':
                if convert_png_to_webp:
                    out_ext = '.webp'
                    save_kwargs.update({'quality': quality, 'lossless': False})
                else:
                    save_kwargs.update({'compress_level': 9})
            elif out_ext == '.webp':
                save_kwargs.update({'quality': quality})

            slug_name = slug_for(src, im)
            dest_path = dest_dir / slug_name
            if out_ext != src.suffix.lower():
                dest_path = dest_path.with_suffix(out_ext)
            if dest_path.exists():
                dest_path = dest_path.with_stem(dest_path.stem + '-dup')

            dest_path.parent.mkdir(parents=True, exist_ok=True)
            im.save(dest_path, **save_kwargs)

            if dest_path.suffix.lower() == '.png' and not convert_png_to_webp:
                call_oxipng(dest_path)

            original_size = src.stat().st_size
            new_size = dest_path.stat().st_size
            stats.add(original_size, new_size)
            log_queue.put(f'✓ {src.name} → {dest_path.name} ({original_size/1024:.1f} KiB → {new_size/1024:.1f} KiB)')
    except UnidentifiedImageError:
        log_queue.put(f'✗ Skipped (not an image): {src}')
        stats.failures.append(str(src))
    except Exception as exc:
        log_queue.put(f'✗ Error processing {src}: {exc}')
        stats.failures.append(str(src))


# ---------------------------------------------------------------------------
# File walking
# ---------------------------------------------------------------------------

def walk_images(folder: Path) -> List[Path]:
    return [p for p in folder.rglob('*') if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS]


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title('Image Bulk Optimizer')
        self.geometry('700x500')
        self.minsize(600, 400)

        self.src_folder: Path | None = None
        self.dest_folder: Path | None = None

        self._build_ui()
        self.log_queue: queue.Queue[str | tuple] = queue.Queue()
        self.after(100, self._drain_log_queue)

    # UI widgets ------------------------------------------------------------
    def _build_ui(self) -> None:
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill=tk.BOTH, expand=True)

        # Source selector
        ttk.Button(frm, text='Choose source folder…', command=self._choose_src).grid(row=0, column=0, sticky='w')
        self.src_label = ttk.Label(frm, text='—')
        self.src_label.grid(row=0, column=1, sticky='w')

        # Destination selector
        ttk.Button(frm, text='Choose destination folder…', command=self._choose_dest).grid(row=1, column=0, sticky='w')
        self.dest_label = ttk.Label(frm, text='(in‑place)', foreground='gray')
        self.dest_label.grid(row=1, column=1, sticky='w')

        # Quality slider
        ttk.Label(frm, text='Quality (JPEG / WebP) %').grid(row=2, column=0, sticky='w', pady=(10, 0))
        self.quality_var = tk.IntVar(value=85)
        ttk.Scale(frm, from_=40, to=100, orient=tk.HORIZONTAL, variable=self.quality_var).grid(row=2, column=1, sticky='we', pady=(10, 0))

        # Convert PNG checkbox
        self.png_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text='Convert PNG → WebP (lossy)', variable=self.png_var).grid(row=3, column=1, sticky='w', pady=(5, 5))

        # Start button
        ttk.Button(frm, text='Start optimization', command=self._start).grid(row=4, column=0, columnspan=2, pady=(10, 0))

        # Progress bar
        self.progress = ttk.Progressbar(frm, mode='determinate')
        self.progress.grid(row=5, column=0, columnspan=2, sticky='we', pady=(10, 0))

        # Log text box
        self.log_txt = tk.Text(frm, height=12, state='disabled', wrap='none')
        self.log_txt.grid(row=6, column=0, columnspan=2, sticky='nsew', pady=(10, 0))

        # Summary label
        self.summary_var = tk.StringVar(value='')
        ttk.Label(frm, textvariable=self.summary_var, font=('TkDefaultFont', 10, 'bold')).grid(row=7, column=0, columnspan=2, sticky='w', pady=(10, 0))

        # Layout weights
        frm.columnconfigure(1, weight=1)
        frm.rowconfigure(6, weight=1)

    # Event handlers --------------------------------------------------------
    def _choose_src(self) -> None:
        folder = filedialog.askdirectory(title='Select source folder')
        if folder:
            self.src_folder = Path(folder)
            self.src_label.configure(text=str(self.src_folder))

    def _choose_dest(self) -> None:
        folder = filedialog.askdirectory(title='Select destination folder')
        if folder:
            self.dest_folder = Path(folder)
            self.dest_label.configure(text=str(self.dest_folder), foreground='black')
        else:
            self.dest_folder = None
            self.dest_label.configure(text='(in‑place)', foreground='gray')

    def _start(self) -> None:
        if not self.src_folder:
            messagebox.showerror('No source folder', 'Please select a source folder first')
            return
        files = walk_images(self.src_folder)
        if not files:
            messagebox.showinfo('No images found', 'No supported images were found')
            return

        # Reset UI
        self.progress['maximum'] = len(files)
        self.progress['value'] = 0
        self._clear_log()
        self.summary_var.set('')

        stats = Stats()
        threading.Thread(target=self._worker, args=(files, stats), daemon=True).start()

    def _worker(self, files: List[Path], stats: Stats) -> None:
        dest_dir = self.dest_folder or self.src_folder
        quality = self.quality_var.get()
        convert_png = self.png_var.get()

        for idx, fpath in enumerate(files, 1):
            optimize_image(fpath, dest_dir, quality, convert_png, stats, self.log_queue)
            self.log_queue.put(('PROGRESS', idx))

        self.log_queue.put(('DONE', stats))

    def _drain_log_queue(self) -> None:
        try:
            while True:
                item = self.log_queue.get_nowait()
                if isinstance(item, tuple):
                    tag, payload = item
                    if tag == 'PROGRESS':
                        self.progress['value'] = payload
                    elif tag == 'DONE':
                        stats: Stats = payload
                        self.summary_var.set(stats.as_summary())
                else:
                    self._append_log(item + '\n')
        except queue.Empty:
            pass
        finally:
            self.after(100, self._drain_log_queue)

    # Logging helpers -------------------------------------------------------
    def _clear_log(self) -> None:
        self.log_txt.configure(state='normal')
        self.log_txt.delete('1.0', tk.END)
        self.log_txt.configure(state='disabled')

    def _append_log(self, text: str) -> None:
        self.log_txt.configure(state='normal')
        self.log_txt.insert(tk.END, text)
        self.log_txt.see(tk.END)
        self.log_txt.configure(state='disabled')


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == '__main__':
    main()
