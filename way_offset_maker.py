"""Simutrans way addon image offset maker.

Splits a way-addon source image into paksize x paksize tiles, lets the user
assign a compass direction to each tile, then shifts/clips each tile's pixel
content according to the direction so the addon renders with correct offset
in-game.
"""

import csv
import os

import numpy as np
from PIL import Image, ImageTk, ImageDraw
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# Directions grouped by how they must be shifted/clipped.
DIR_NS = ("N", "S", "NS", "NSW", "NSE")
DIR_EW = ("E", "W", "EW", "NEW", "SEW")
DIR_DIAMOND_NE_SW = ("NE", "SW")
DIR_DIAMOND_NW_SE = ("NW", "SE")
DIR_NONE = ("NSEW", "none")

ALL_DIRECTIONS = (
    "none", "N", "S", "E", "W", "NS", "EW",
    "NE", "NW", "SE", "SW",
    "NEW", "NSW", "NSE", "SEW", "NSEW",
)

PAKSIZE_PRESETS = ("32", "64", "96", "128", "192", "256")


def diamond_mask(paksize):
    """Boolean (paksize, paksize) mask matching TILE_CUTTER_RGBA's N_Hight==0
    ground-diamond clip (mode 0 branch): a rhombus occupying the lower half
    of the tile, narrowest at the left/right edges, widest at the centre."""
    p = paksize
    mask = np.zeros((p, p), dtype=bool)
    center = p - p // 4
    half = p // 2
    for ix in range(p):
        w = (ix // 2) if ix < half else ((p - 1 - ix) // 2)
        lo = max(center - w, 0)
        hi = min(center + w, p - 1)
        if lo <= hi:
            mask[lo:hi + 1, ix] = True
    return mask


def shift_tile(tile, dx, dy):
    """Shift RGBA tile content by (dx, dy) pixels (right/down positive).
    Pixels that would come from outside the tile become transparent."""
    p_h, p_w = tile.shape[0], tile.shape[1]
    out = np.zeros_like(tile)

    src_x0 = max(0, -dx)
    src_x1 = min(p_w, p_w - dx)
    src_y0 = max(0, -dy)
    src_y1 = min(p_h, p_h - dy)
    if src_x0 >= src_x1 or src_y0 >= src_y1:
        return out

    dst_x0 = src_x0 + dx
    dst_y0 = src_y0 + dy
    dst_x1 = src_x1 + dx
    dst_y1 = src_y1 + dy

    out[dst_y0:dst_y1, dst_x0:dst_x1] = tile[src_y0:src_y1, src_x0:src_x1]
    return out


def half(value):
    return int(round(value / 2))


def process_tile(tile, direction, offset, mask_cache):
    """Return the shifted (and possibly diamond-clipped) tile for `direction`."""
    if direction in DIR_NONE or direction not in ALL_DIRECTIONS:
        return tile.copy()

    if direction in DIR_NS:
        dx, dy, clip = offset, half(offset), False
    elif direction in DIR_EW:
        dx, dy, clip = -offset, half(offset), False
    elif direction in DIR_DIAMOND_NE_SW:
        dx, dy, clip = -offset, 0, True
    elif direction in DIR_DIAMOND_NW_SE:
        dx, dy, clip = 0, half(offset), True
    else:
        dx, dy, clip = 0, 0, False

    shifted = shift_tile(tile, dx, dy)

    if clip:
        p = tile.shape[0]
        mask = mask_cache.get(p)
        if mask is None:
            mask = diamond_mask(p)
            mask_cache[p] = mask
        shifted = shifted.copy()
        shifted[~mask] = 0

    return shifted


class WayOffsetApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Way Addon Offset Maker")
        self.root.geometry("900x650")

        self.image = None          # PIL Image, RGBA
        self.array = None          # numpy array HxWx4
        self.rows = 0
        self.cols = 0
        self.paksize = 0
        self.direction_vars = {}   # (r, c) -> tk.StringVar
        self.preview_photo = None  # keep reference alive

        self._build_widgets()

    # ---------------------------------------------------------- UI build --
    def _build_widgets(self):
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill=tk.X)

        ttk.Label(top, text="Source image:").grid(row=0, column=0, sticky=tk.W, pady=4)
        self.file_path_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.file_path_var, width=60).grid(
            row=0, column=1, columnspan=3, sticky=tk.EW, padx=5)
        ttk.Button(top, text="Browse...", command=self.on_browse).grid(row=0, column=4, padx=5)

        ttk.Label(top, text="Pak size:").grid(row=1, column=0, sticky=tk.W, pady=4)
        self.paksize_var = tk.StringVar(value="128")
        paksize_combo = ttk.Combobox(
            top, textvariable=self.paksize_var, values=PAKSIZE_PRESETS, width=10)
        paksize_combo.grid(row=1, column=1, sticky=tk.W, padx=5)

        ttk.Button(top, text="Analyze image", command=self.on_analyze).grid(
            row=1, column=2, sticky=tk.W, padx=5)

        self.info_var = tk.StringVar(value="Select an image and pak size, then Analyze.")
        ttk.Label(top, textvariable=self.info_var, foreground="#555").grid(
            row=2, column=0, columnspan=5, sticky=tk.W, pady=(4, 0))

        top.columnconfigure(1, weight=1)

        # Bulk assign row
        bulk = ttk.Frame(self.root, padding=(10, 0))
        bulk.pack(fill=tk.X)
        ttk.Label(bulk, text="Set all regions to:").pack(side=tk.LEFT)
        self.bulk_dir_var = tk.StringVar(value="none")
        ttk.Combobox(bulk, textvariable=self.bulk_dir_var, values=ALL_DIRECTIONS,
                     width=8, state="readonly").pack(side=tk.LEFT, padx=5)
        ttk.Button(bulk, text="Apply to all", command=self.on_bulk_apply).pack(side=tk.LEFT)
        ttk.Button(bulk, text="Load preset...", command=self.on_load_preset).pack(
            side=tk.RIGHT)
        ttk.Button(bulk, text="Save preset...", command=self.on_save_preset).pack(
            side=tk.RIGHT, padx=5)

        # Preview (left) + region grid (right), side by side
        mid = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        mid.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # ---- Left: preview scroll area ----
        left = ttk.Frame(mid)
        mid.add(left, weight=1)
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(left, borderwidth=0, background="#ffffff")
        vscroll = ttk.Scrollbar(left, orient="vertical", command=self.canvas.yview)
        hscroll = ttk.Scrollbar(left, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=vscroll.set,xscrollcommand=hscroll.set)
        self.canvas.grid(row=0, column=0, sticky=tk.NSEW)
        vscroll.grid(row=0, column=1, sticky=tk.NS)
        hscroll.grid(row=1, column=0, sticky=tk.EW)

        self.scroll_frame = ttk.Frame(self.canvas)
        self.scroll_frame_id = self.canvas.create_window(
            (0, 0), window=self.scroll_frame, anchor="nw")
        self.scroll_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))

        self.preview_label = ttk.Label(self.scroll_frame)
        self.preview_label.pack(anchor="nw")

        # ---- Right: region grid scroll area ----
        right = ttk.Frame(mid)
        mid.add(right, weight=1)

        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        self.region_canvas = tk.Canvas(right, borderwidth=0, background="#ffffff")
        region_vscroll = ttk.Scrollbar(
            right, orient="vertical", command=self.region_canvas.yview)
        region_hscroll = ttk.Scrollbar(
            right, orient="horizontal", command=self.region_canvas.xview)
        self.region_canvas.configure(
            yscrollcommand=region_vscroll.set,
            xscrollcommand=region_hscroll.set)

        self.region_canvas.grid(row=0, column=0, sticky=tk.NSEW)
        region_vscroll.grid(row=0, column=1, sticky=tk.NS)
        region_hscroll.grid(row=1, column=0, sticky=tk.EW)

        self.region_frame = ttk.Frame(self.region_canvas)
        self.region_frame_id = self.region_canvas.create_window(
            (0, 0), window=self.region_frame, anchor="nw")
        # 横幅は内容に合わせる(横スクロールを効かせるため幅は固定しない)
        self.region_frame.bind(
            "<Configure>",
            lambda e: self.region_canvas.configure(
                scrollregion=self.region_canvas.bbox("all")))

        # Bottom: offset + apply
        bottom = ttk.Frame(self.root, padding=10)
        bottom.pack(fill=tk.X)
        ttk.Label(bottom, text="Offset (pixels, integer):").pack(side=tk.LEFT)
        self.offset_var = tk.StringVar(value="0")
        ttk.Entry(bottom, textvariable=self.offset_var, width=10).pack(side=tk.LEFT, padx=5)
        ttk.Button(bottom, text="Apply", command=self.on_apply).pack(side=tk.LEFT, padx=10)

    # -------------------------------------------------------------- events --
    def on_browse(self):
        path = filedialog.askopenfilename(
            title="Select way addon source image",
            filetypes=[("PNG image", "*.png"), ("All files", "*.*")])
        if path:
            self.file_path_var.set(path)

    def on_analyze(self):
        path = self.file_path_var.get().strip()
        if not path or not os.path.isfile(path):
            messagebox.showerror("Error", "Please select a valid image file.")
            return

        try:
            paksize = int(self.paksize_var.get())
        except ValueError:
            messagebox.showerror("Error", "Pak size must be an integer.")
            return
        if paksize <= 0 or paksize % 4 != 0:
            messagebox.showerror("Error", "Pak size must be a positive multiple of 4.")
            return

        try:
            img = Image.open(path)
        except Exception as exc:
            messagebox.showerror("Error", f"Could not open image:\n{exc}")
            return

        if img.mode not in ("RGB", "RGBA"):
            messagebox.showerror("Error", f"Unsupported image mode: {img.mode}\n"
                                           "Only RGB or RGBA PNG images are supported.")
            return

        img = img.convert("RGBA")
        w, h = img.size
        cols = w // paksize
        rows = h // paksize

        if w % paksize != 0 or h % paksize != 0:
            messagebox.showwarning(
                "Size mismatch",
                f"Image size {w}x{h} is not an exact multiple of pak size {paksize}.\n"
                f"Only the top-left {cols * paksize}x{rows * paksize} area will be divided "
                f"into {cols}x{rows} regions; leftover edges are copied through unchanged.")

        if cols == 0 or rows == 0:
            messagebox.showerror("Error", "Image is smaller than the pak size.")
            return

        self.image = img
        self.array = np.array(img)
        self.paksize = paksize
        self.cols = cols
        self.rows = rows
        self.info_var.set(f"Image {w}x{h}  ->  {cols}x{rows} regions of {paksize}px")

        self._build_region_grid()

    def _build_region_grid(self):
        for child in self.region_frame.winfo_children():
            child.destroy()
        self.direction_vars = {}

        # --- プレビュー画像(左側)は変更なし ---
        w, h = self.image.size
        max_w = 640
        scale = min(1.0, max_w / w)
        prev_w, prev_h = max(1, int(w * scale)), max(1, int(h * scale))
        preview = self.image.convert("RGB").resize((prev_w, prev_h), Image.NEAREST)
        draw = ImageDraw.Draw(preview)
        p_scaled = self.paksize * scale
        for c in range(self.cols + 1):
            x = round(c * p_scaled)
            draw.line([(x, 0), (x, self.rows * p_scaled)], fill=(255, 0, 0), width=1)
        for r in range(self.rows + 1):
            y = round(r * p_scaled)
            draw.line([(0, y), (self.cols * p_scaled, y)], fill=(255, 0, 0), width=1)
        self.preview_photo = ImageTk.PhotoImage(preview)
        self.preview_label.configure(image=self.preview_photo)

        # --- header と cell を新しい右側フレームへ ---
        header = ttk.Label(self.region_frame,
                           text="Assign a direction to each region (row, col):")
        header.grid(row=0, column=0, columnspan=max(self.cols, 1),
                    sticky=tk.W, pady=(5, 5))

        for r in range(self.rows):
            for c in range(self.cols):
                var = tk.StringVar(value="none")
                self.direction_vars[(r, c)] = var
                cell = ttk.Frame(self.region_frame, padding=2)
                cell.grid(row=1 + r, column=c, sticky=tk.W)
                ttk.Label(cell, text=f"({r},{c})", font=("", 8)).pack()
                ttk.Combobox(cell, textvariable=var, values=ALL_DIRECTIONS,
                             width=7, state="readonly").pack()

        # 再構築後にスクロール位置をリセット
        self.region_canvas.xview_moveto(0)
        self.region_canvas.yview_moveto(0)

    def on_bulk_apply(self):
        if not self.direction_vars:
            return
        value = self.bulk_dir_var.get()
        for var in self.direction_vars.values():
            var.set(value)

    def on_save_preset(self):
        """Save the current direction grid as CSV (one CSV row per region row)."""
        if not self.direction_vars:
            messagebox.showerror("Error", "Please analyze an image first.")
            return

        path = filedialog.asksaveasfilename(
            title="Save direction preset",
            defaultextension=".csv",
            filetypes=[("CSV file", "*.csv")])
        if not path:
            return

        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                for r in range(self.rows):
                    writer.writerow(
                        self.direction_vars[(r, c)].get() for c in range(self.cols))
        except Exception as exc:
            messagebox.showerror("Error", f"Could not save preset:\n{exc}")
            return

        messagebox.showinfo("Done", f"Saved preset:\n{path}")

    def on_load_preset(self):
        """Load a direction grid from CSV. Cells outside the CSV's range, empty
        cells and unknown directions become "none"; extra CSV cells are ignored."""
        if not self.direction_vars:
            messagebox.showerror("Error", "Please analyze an image first.")
            return

        path = filedialog.askopenfilename(
            title="Load direction preset",
            filetypes=[("CSV file", "*.csv"), ("All files", "*.*")])
        if not path:
            return

        try:
            with open(path, newline="", encoding="utf-8-sig") as f:
                data = [row for row in csv.reader(f)]
        except Exception as exc:
            messagebox.showerror("Error", f"Could not load preset:\n{exc}")
            return

        csv_rows = len(data)
        csv_cols = max((len(row) for row in data), default=0)
        for (r, c), var in self.direction_vars.items():
            value = data[r][c].strip() if r < csv_rows and c < len(data[r]) else ""
            var.set(value if value in ALL_DIRECTIONS else "none")

        if (csv_rows, csv_cols) != (self.rows, self.cols):
            messagebox.showwarning(
                "Size mismatch",
                f"Preset is {csv_cols}x{csv_rows} but image has {self.cols}x{self.rows} "
                f"regions.\nUnrecorded cells were set to \"none\".")

    def on_apply(self):
        if self.array is None:
            messagebox.showerror("Error", "Please analyze an image first.")
            return

        try:
            offset = int(self.offset_var.get())
        except ValueError:
            messagebox.showerror("Error", "Offset must be an integer.")
            return

        out_path = filedialog.asksaveasfilename(
            title="Save offset image",
            defaultextension=".png",
            filetypes=[("PNG image", "*.png")])
        if not out_path:
            return

        result = self.array.copy()
        p = self.paksize
        mask_cache = {}

        for (r, c), var in self.direction_vars.items():
            direction = var.get()
            y0, y1 = r * p, (r + 1) * p
            x0, x1 = c * p, (c + 1) * p
            tile = self.array[y0:y1, x0:x1]
            result[y0:y1, x0:x1] = process_tile(tile, direction, offset, mask_cache)

        try:
            Image.fromarray(result, mode="RGBA").save(out_path)
        except Exception as exc:
            messagebox.showerror("Error", f"Could not save image:\n{exc}")
            return

        messagebox.showinfo("Done", f"Saved:\n{out_path}")


def main():
    root = tk.Tk()
    WayOffsetApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
