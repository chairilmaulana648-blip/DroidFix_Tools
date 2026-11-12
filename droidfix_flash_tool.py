#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DroidFix Flash Tool
GUI Tkinter murni (tanpa library tambahan). Bisa dibungkus jadi .exe dengan build.bat.

Taruh folder `platform-tools` dan `scrcpy` di sebelah program ini (atau pakai
tombol "Folder tools" untuk menunjuk lokasinya).
"""
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

APP_TITLE = "DroidFix Flash Tool"
IS_WIN = os.name == "nt"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
BASE_DIR = (Path(sys.executable).parent if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parent)
CONFIG_FILE = Path.home() / ".droidfix_flash_tool.json"
OLD_CONFIG_FILE = Path.home() / ".adb_fastboot_toolkit.json"

UI_FONT = "Segoe UI" if IS_WIN else "DejaVu Sans"
MONO_FONT = "Consolas" if IS_WIN else "DejaVu Sans Mono"

# Palet: slate gelap, satu aksen teal; warna lain hanya untuk makna status.
BG, LOGBG = "#161a22", "#0e1117"
FG, MUTED = "#e6e9ef", "#8a93a6"
BORDER, BTN, BTN_H = "#2a3140", "#252c3b", "#303a50"
ACCENT, GREEN, AMBER, RED, BLUE, GREY = (
    "#2dd4bf", "#34d399", "#f5a524", "#f0506e", "#60a5fa", "#4b5468")

# USB mode khusus yang tidak muncul di adb/fastboot
# (id Windows, id lsusb, status, nama)
USB_SPECIAL = [
    ("VID_05C6&PID_9008", "05c6:9008", "edl", "Qualcomm HS-USB QDLoader 9008"),
    ("VID_0E8D&PID_0003", "0e8d:0003", "brom", "MediaTek BROM"),
]


# --------------------------------------------------------------------------
# Fungsi bantu (tanpa GUI, mudah diuji)
# --------------------------------------------------------------------------
def exe_name(name):
    return name + ".exe" if IS_WIN else name


def search_dir(root, filename, depth, skip_prefix=()):
    root = Path(root)
    if not root.is_dir():
        return None
    if (root / filename).is_file():
        return str(root / filename)
    if depth <= 0:
        return None
    try:
        for child in sorted(root.iterdir()):
            if child.is_dir() and not child.name.lower().startswith(tuple(skip_prefix)):
                found = search_dir(child, filename, depth - 1, skip_prefix)
                if found:
                    return found
    except OSError:
        pass
    return None


def find_tool(name, user_dirs):
    """Cari adb/fastboot/scrcpy: folder pilihan user, sebelah program, lalu PATH."""
    fn = exe_name(name)
    # scrcpy membawa adb sendiri; untuk adb/fastboot utamakan platform-tools.
    skip = ("scrcpy",) if name in ("adb", "fastboot") else ()
    roots = [(Path(d), 3) for d in user_dirs]
    roots += [(BASE_DIR / "platform-tools", 1), (BASE_DIR, 2)]
    if hasattr(sys, "_MEIPASS"):
        roots.append((Path(sys._MEIPASS), 2))
    for root, depth in roots:
        found = search_dir(root, fn, depth, skip)
        if found:
            return found
    return shutil.which(name)


def capture(args, timeout=15):
    """Jalankan perintah, kembalikan (returncode, output gabungan)."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, creationflags=NO_WINDOW,
                           stdin=subprocess.DEVNULL)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except Exception as exc:  # noqa: BLE001
        return -2, str(exc)


PROP_RE = re.compile(r"^\[(.+?)\]: \[(.*)\]\s*$")


def parse_props(text):
    props = {}
    for line in text.splitlines():
        m = PROP_RE.match(line.strip())
        if m:
            props[m.group(1)] = m.group(2)
    return props


def parse_getvar(text):
    """Ubah keluaran `fastboot getvar all` jadi dict (kemunculan pertama dipakai)."""
    out = {}
    for line in text.splitlines():
        line = re.sub(r"^\(bootloader\)\s*", "", line.strip())
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        out.setdefault(key.strip(), val.strip())
    return out


class Device:
    """tool: 'adb' | 'fastboot' | 'usb'   state: device/recovery/sideload/fastboot/edl/..."""

    def __init__(self, tool, serial, state, extra=""):
        self.tool, self.serial, self.state, self.extra = tool, serial, state, extra

    @property
    def key(self):
        return (self.tool, self.serial, self.state)

    def label(self):
        if self.tool == "adb":
            return f"{self.serial}  [ADB · {self.state}]"
        if self.tool == "fastboot":
            return f"{self.serial}  [fastboot]"
        return f"{self.extra}  [USB]"


def parse_adb_devices(text):
    devs, started = [], False
    for line in text.splitlines():
        if line.startswith("List of devices"):
            started = True
            continue
        if not started or not line.strip() or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            m = re.search(r"model:(\S+)", line)
            devs.append(Device("adb", parts[0], parts[1], m.group(1) if m else ""))
    return devs


def parse_fastboot_devices(text):
    devs = []
    for line in text.splitlines():
        line = line.strip()
        if not line or "waiting" in line.lower() or line.startswith("*"):
            continue
        parts = line.split()
        devs.append(Device("fastboot", parts[0], parts[1] if len(parts) > 1 else "fastboot"))
    return devs


# Partisi yang TIDAK dicentang otomatis saat memuat scatter / rawprogram:
# tabel partisi (GPT), bootloader awal, data kalibrasi / IMEI, dan data pengguna.
# Semuanya tetap bisa dicentang manual.
SKIP_DEFAULT = {
    "preloader", "pgpt", "sgpt", "primarygpt", "backupgpt",
    "nvram", "nvdata", "nvcfg", "proinfo", "protect1", "protect2", "persist",
    "seccfg", "otp", "expdb", "frp", "para", "boot_para",
    "modemst1", "modemst2", "fsg", "fsc", "ssd", "ddr", "devinfo", "sec",
    "userdata", "metadata", "cache",
}
PART_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def human_size(n):
    if n <= 0:
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def parse_scatter(path):
    """Baca scatter MediaTek (format YAML-like). Kembalikan (label, daftar partisi)."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    plat = re.search(r"(?m)^\s*platform:\s*(\S+)", text)
    store = re.search(r"(?m)^\s*storage:\s*(\S+)", text)
    entries = []
    for block in re.split(r"(?m)^\s*-\s*partition_index:", text)[1:]:
        name = re.search(r"(?m)^\s*partition_name:\s*(\S+)", block)
        fname = re.search(r"(?m)^\s*file_name:\s*(\S+)", block)
        dl = re.search(r"(?m)^\s*is_download:\s*(\w+)", block)
        if not name or not fname:
            continue
        if dl and dl.group(1).lower() != "true":
            continue
        if fname.group(1).upper() == "NONE":
            continue
        entries.append({"part": name.group(1), "file": fname.group(1)})
    label = "MediaTek scatter"
    if plat:
        label += f" · {plat.group(1)}"
    if store:
        label += f" · {store.group(1)}"
    return label, entries


def parse_rawprogram(paths):
    """Baca seluruh entri <program> dari satu/lebih rawprogram XML Qualcomm.

    Entri tanpa filename tetap ditampilkan sebagai baris XML (mis. operasi kosong/
    erase pada paket tertentu), tetapi tidak dapat dieksekusi oleh fastboot flasher.
    """
    import xml.etree.ElementTree as ET
    entries, used = [], 0
    for p in paths:
        root = ET.parse(p).getroot()
        found = 0
        for el in root.iter():
            if str(el.tag).split("}")[-1].lower() != "program":
                continue
            attrs = {str(k).lower(): (v or "").strip() for k, v in el.attrib.items()}
            label = attrs.get("label", "")
            fname = attrs.get("filename", "")
            if not label:
                # Keep a visible row for every program node, even if metadata is incomplete.
                label = attrs.get("physical_partition_number", "")
                label = f"partition_{label}" if label else f"program_{len(entries) + 1}"
            entries.append({
                "part": label,
                "file": fname,
                "xml_only": not bool(fname) or fname.upper() == "NONE",
                "physical_partition": attrs.get("physical_partition_number", ""),
                "start_sector": attrs.get("start_sector", ""),
                "num_sectors": attrs.get("num_partition_sectors", ""),
                "sector_size": attrs.get("SECTOR_SIZE_IN_BYTES", attrs.get("sector_size_in_bytes", "")),
            })
            found += 1
        if found:
            used += 1
    return f"Qualcomm rawprogram · {used} file XML", entries


def default_skip(entry):
    name, fname = entry["part"].lower(), entry["file"].lower()
    return (name in SKIP_DEFAULT or name.startswith(("gpt", "pgpt", "sgpt"))
            or fname.startswith(("gpt_main", "gpt_backup", "gpt_both")))


def resolve_image(base_dir, filename):
    """Cari file image relatif terhadap folder scatter/XML (juga subfolder images)."""
    base = Path(base_dir)
    fn = filename.replace("\\", "/")
    for cand in (base / fn, base / Path(fn).name, base / "images" / Path(fn).name):
        if cand.is_file():
            return str(cand)
    return search_dir(base, Path(fn).name, 3)


# --------------------------------------------------------------------------
# Aplikasi
# --------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self, supabase=None):
        super().__init__()
        self.supabase = supabase
        self.title(APP_TITLE)
        self.set_app_icon()
        self.scale = max(1.0, self.winfo_fpixels("1i") / 96.0)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = min(int(1180 * self.scale), sw - 40)
        h = min(int(940 * self.scale), sh - 90)
        self.geometry(f"{w}x{h}+{max((sw - w) // 2, 0)}+{max((sh - h) // 2 - 20, 0)}")
        self.minsize(min(int(1000 * self.scale), w), min(int(640 * self.scale), h))
        self.configure(bg=BG)

        self.cfg = self.load_config()
        self.devices = []
        self.sel_key = None
        self.details = {}
        self.is_busy = False
        self.closing = False
        self.scrcpy_proc = None
        self.adb = self.fastboot = self.scrcpy = self.odin4 = self.odin3 = None
        self.act_buttons = []
        self.fl_entries = []
        self.pb_ind = False
        self.cancel_flag = threading.Event()

        self.uiq = queue.Queue()
        self.refresh_now = threading.Event()

        self.setup_style()
        self.build_ui()
        self.locate_tools(initial=True)

        self.after(60, self.drain_ui_queue)
        threading.Thread(target=self.poll_loop, daemon=True).start()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------------- konfigurasi ----------------
    def load_config(self):
        for f in (CONFIG_FILE, OLD_CONFIG_FILE):
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
        return {"tool_dirs": []}

    def save_config(self):
        try:
            CONFIG_FILE.write_text(json.dumps(self.cfg, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    # ---------------- gaya ----------------
    def setup_style(self):
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure(".", background=BG, foreground=FG, font=(UI_FONT, 10),
                     bordercolor=BORDER, focuscolor=BG)
        st.configure("TFrame", background=BG)
        st.configure("TLabel", background=BG, foreground=FG)
        st.configure("Muted.TLabel", foreground=MUTED)
        st.configure("Title.TLabel", font=(UI_FONT, 15, "bold"))
        st.configure("TLabelframe", background=BG, bordercolor=BORDER, relief="solid")
        st.configure("TLabelframe.Label", background=BG, foreground=MUTED,
                     font=(UI_FONT, 10, "bold"))
        st.configure("TButton", background=BTN, foreground=FG, padding=(10, 7),
                     borderwidth=1, focuscolor=BTN)
        st.map("TButton",
               background=[("disabled", "#1d222d"), ("pressed", "#3b4660"), ("active", BTN_H)],
               foreground=[("disabled", "#59627a")])
        st.configure("Accent.TButton", background="#14b8a6", foreground="#04201c",
                     font=(UI_FONT, 10, "bold"))
        st.map("Accent.TButton",
               background=[("disabled", "#1d222d"), ("active", ACCENT)],
               foreground=[("disabled", "#59627a")])
        st.configure("Danger.TButton", background="#7f2a3b", foreground="white")
        st.map("Danger.TButton",
               background=[("disabled", "#1d222d"), ("active", "#a83650")],
               foreground=[("disabled", "#59627a")])
        st.configure("TCheckbutton", background=BG, foreground=FG)
        st.map("TCheckbutton", background=[("active", BG)],
               indicatorcolor=[("selected", ACCENT), ("!selected", "#262c3b")])
        st.configure("TCombobox", fieldbackground=BTN, background=BTN, foreground=FG,
                     arrowcolor=FG, selectbackground=BTN, selectforeground=FG)
        st.map("TCombobox", fieldbackground=[("readonly", BTN)],
               foreground=[("readonly", FG)])
        st.configure("Vertical.TScrollbar", background=BTN, troughcolor=LOGBG,
                     bordercolor=LOGBG, arrowcolor=MUTED)
        st.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0))
        st.configure("TNotebook.Tab", background=BTN, foreground=MUTED, padding=(14, 7), borderwidth=0)
        st.map("TNotebook.Tab", background=[("selected", "#202838"), ("active", BTN_H)],
               foreground=[("selected", ACCENT)])
        st.configure("TEntry", fieldbackground=BTN, foreground=FG, insertcolor=FG, bordercolor=BORDER)
        st.map("TEntry", fieldbackground=[("readonly", BTN)], foreground=[("readonly", FG)])
        st.configure("Treeview", background=LOGBG, fieldbackground=LOGBG, foreground=FG,
                     rowheight=24, borderwidth=0)
        st.configure("Treeview.Heading", background=BTN, foreground=MUTED, relief="flat",
                     font=(UI_FONT, 10, "bold"))
        st.map("Treeview", background=[("selected", "#24324a")], foreground=[("selected", FG)])
        st.map("Treeview.Heading", background=[("active", BTN_H)])
        st.configure("Horizontal.TProgressbar", troughcolor=LOGBG, background="#14b8a6",
                     bordercolor=BORDER, lightcolor="#14b8a6", darkcolor="#14b8a6", thickness=16)
        st.configure("DFT.Horizontal.TProgressbar", troughcolor="#1b2130", background=ACCENT,
                     bordercolor="#1b2130", lightcolor=ACCENT, darkcolor=ACCENT, thickness=7)
        st.configure("TPanedwindow", background=BG)
        st.configure("Sash", sashthickness=8, gripcount=0, background=BORDER)
        self.option_add("*TCombobox*Listbox.background", BTN)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        self.option_add("*TCombobox*Listbox.selectBackground", "#14b8a6")

    def set_app_icon(self):
        # ikon jendela / taskbar dari aset yang ditanam (droidfix_assets.py)
        try:
            import droidfix_assets as assets
            self._icon_img = tk.PhotoImage(data=assets.ICON_128)
            self.iconphoto(True, self._icon_img)
        except Exception:  # noqa: BLE001
            pass

    def load_logo(self):
        try:
            import droidfix_assets as assets
            data = assets.LOGO_60 if self.scale >= 1.4 else assets.LOGO_40
            self._logo_img = tk.PhotoImage(data=data)
            return self._logo_img
        except Exception:  # noqa: BLE001
            return None

    def px(self, n):
        return int(n * self.scale)

    def _resize_sidebar(self, event):
        # Lebar sidebar = porsi tetap dari lebar jendela, dibatasi minimum (agar status/log
        # terbaca) dan maksimum (agar area tab di kanan tetap muat). Konsisten lintas DPI.
        total = event.width
        if total <= 1:
            return
        lo = self.px(340)                     # minimal supaya panel kiri tidak terlalu sempit
        hi = max(lo, total - self.px(560))    # sisakan ruang untuk tab fastboot 2 kolom
        want = int(total * self.sidebar_ratio)
        width = max(lo, min(want, hi))
        if getattr(self, "_last_sidebar_w", None) != width:
            self._last_sidebar_w = width
            self.left_sidebar.configure(width=width)

    # ---------------- tata letak ----------------
    def mkbtn(self, parent, text, cmd, style="TButton", track=True):
        b = ttk.Button(parent, text=text, command=cmd, style=style)
        if track:
            self.act_buttons.append(b)
        return b

    def build_ui(self):
        self.var_slot = tk.StringVar(value="-")
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)

        hdr = ttk.Frame(outer)
        hdr.pack(fill="x")
        logo = self.load_logo()
        if logo:
            tk.Label(hdr, image=logo, bg=BG, bd=0).pack(side="left", padx=(0, 10))
        ttk.Label(hdr, text="DroidFix Flash Tool", style="Title.TLabel").pack(side="left")

        # Indikator user Supabase & logout
        if getattr(self, "supabase", None) and self.supabase.get_user_email():
            u_email = self.supabase.get_user_email()
            tk.Button(hdr, text="Keluar (Logout)", font=(UI_FONT, 8), bg=BTN, fg=FG,
                      activebackground=BTN_H, relief="flat", cursor="hand2",
                      command=self.do_user_logout).pack(side="right", padx=(8, 0))
            tk.Label(hdr, text=f"👤 {u_email}", bg=BG, fg=ACCENT, font=(UI_FONT, 9, "bold")).pack(side="right", padx=(10, 0))

        ttk.Button(hdr, text="Folder tools…", command=self.choose_tools).pack(side="right")
        self.lbl_tools = tk.Label(hdr, bg=BG, fg=MUTED, font=(UI_FONT, 10))
        self.lbl_tools.pack(side="right", padx=12)

        self.build_statusbar(outer)

        # Tata letak gaya DFT: sidebar kiri (status + log) tidak bisa digeser; lebarnya
        # PROPORSIONAL terhadap lebar jendela supaya tampak sama di layar/DPI mana pun
        # (tidak lagi memakai angka px tetap yang berubah-ubah mengikuti scaling Windows).
        content = tk.Frame(outer, bg=BG)
        content.pack(fill="both", expand=True, pady=(10, 0))
        self.content_frame = content

        left = tk.Frame(content, bg=BG)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)  # lebar diatur manual lewat _resize_sidebar
        self.left_sidebar = left
        self.build_status(left)
        self.build_log(left)

        right = ttk.Frame(content, padding=(10, 0, 0, 0))
        right.pack(side="left", fill="both", expand=True)

        # porsi lebar sidebar terhadap jendela; batas kanan dijaga agar tab tetap muat
        self.sidebar_ratio = 0.45
        content.bind("<Configure>", self._resize_sidebar)

        # Menu brand sebagai baris tombol horizontal di atas; tab muncul di bawahnya.
        menu_row = ttk.Frame(right)
        menu_row.pack(fill="x", pady=(0, 10))
        self.var_brand = tk.StringVar(value="")
        self.brand_buttons = {}
        for name in self.BRANDS:
            b = ttk.Button(menu_row, text=name, command=lambda n=name: self.on_brand(n))
            b.pack(side="left", padx=(0, 6))
            self.brand_buttons[name] = b

        # Wadah tempat tab brand digambar; diisi ulang setiap ganti brand.
        self.brand_area = ttk.Frame(right)
        self.brand_area.pack(fill="both", expand=True)
        self.brand_hint = ttk.Label(
            self.brand_area, style="Muted.TLabel",
            text="Pilih menu di atas untuk menampilkan perintahnya.")
        self.brand_hint.pack(anchor="w", pady=4)

        self.render_status()

    # ---- pemilih brand ----
    BRANDS = ("Universal", "Xiaomi", "Samsung", "Qualcomm (EDL)", "MediaTek")

    def on_brand(self, brand):
        self.var_brand.set(brand)
        for name, btn in self.brand_buttons.items():
            btn.configure(style="Accent.TButton" if name == brand else "TButton")
        for child in self.brand_area.winfo_children():
            child.destroy()
        nb = ttk.Notebook(self.brand_area)
        nb.pack(fill="both", expand=True)
        if brand == "Universal":
            tabs = (("ADB & Fastboot", self.tab_commands),
                    ("Flash partisi", self.tab_partitions),
                    ("Flasher scatter / rawprogram", self.tab_flasher),
                    ("scrcpy", self.tab_scrcpy))
            for title, builder in tabs:
                page = ttk.Frame(nb, padding=10)
                nb.add(page, text=title)
                builder(page)
        elif brand == "Samsung":
            page = ttk.Frame(nb, padding=10)
            nb.add(page, text="Odin (odin4)")
            self.tab_samsung(page)
        elif brand == "Xiaomi":
            page = ttk.Frame(nb, padding=10)
            nb.add(page, text="Xiaomi MediaTek UBL")
            self.tab_xiaomi(page)
        else:
            page = ttk.Frame(nb, padding=16)
            nb.add(page, text=brand)
            ttk.Label(page, text=f"Belum ada fitur khusus untuk {brand}.",
                      font=(UI_FONT, 11, "bold")).pack(anchor="w")
            ttk.Label(page, style="Muted.TLabel", justify="left", wraplength=self.px(560),
                      text="Untuk saat ini gunakan menu Universal — semua perintah ADB, fastboot, "
                           "flash partisi, flasher scatter/rawprogram, dan scrcpy ada di sana dan "
                           "bekerja lintas brand.").pack(anchor="w", pady=(6, 0))

    # ---- panel status (kiri atas, dibuat padat supaya log dapat ruang lebih besar) ----
    def build_status(self, parent):
        sf = ttk.LabelFrame(parent, text=" Status perangkat ", padding=(10, 6))
        sf.pack(fill="x")
        self.badge = tk.Label(sf, text="Tidak ada perangkat", bg=GREY, fg="white",
                              font=(UI_FONT, 11, "bold"), pady=5)
        self.badge.pack(fill="x")

        row = ttk.Frame(sf)
        row.pack(fill="x", pady=(6, 4))
        self.cb = ttk.Combobox(row, state="readonly")
        self.cb.pack(side="left", fill="x", expand=True)
        self.cb.bind("<<ComboboxSelected>>", self.on_pick)
        ttk.Button(row, text="Segarkan", command=self.refresh_now.set).pack(side="left", padx=(6, 0))

        code_row = ttk.Frame(sf)
        code_row.pack(fill="x")
        self.var_code = tk.StringVar(value="-")
        ttk.Label(code_row, textvariable=self.var_code, font=(MONO_FONT, 13, "bold")).pack(side="left")
        ttk.Button(code_row, text="Salin", width=7, command=self.copy_serial).pack(side="right")

        self.var_summary = tk.StringVar(value="")
        ttk.Label(sf, textvariable=self.var_summary, style="Muted.TLabel", justify="left",
                 wraplength=self.px(340)).pack(fill="x", anchor="w", pady=(2, 0))

    # ---- tab 1: perintah ADB & fastboot ----
    def tab_commands(self, page):
        page.columnconfigure(0, weight=2, uniform="c")
        page.columnconfigure(1, weight=3, uniform="c")
        af = ttk.LabelFrame(page, text=" ADB ", padding=10)
        af.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        for text, cmd in (
            ("Quick info ADB", lambda: self.task(self.do_adb_info)),
            ("ADB reboot system", lambda: self.tool_task("adb", ["reboot"])),
            ("ADB reboot bootloader", lambda: self.tool_task("adb", ["reboot", "bootloader"])),
            ("ADB reboot recovery", lambda: self.tool_task("adb", ["reboot", "recovery"])),
            ("ADB sideload…", self.adb_sideload),
        ):
            self.mkbtn(af, text, cmd).pack(fill="x", pady=3)

        ff = ttk.LabelFrame(page, text=" Fastboot ", padding=10)
        ff.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        ff.columnconfigure((0, 1), weight=1, uniform="fb")
        items = [
            ("Quick info fastboot", lambda: self.task(self.do_fb_info), 0, 0, 2, "TButton"),
            ("Fastboot reboot", lambda: self.tool_task("fastboot", ["reboot"]), 1, 0, 1, "TButton"),
            ("Reboot bootloader", lambda: self.tool_task("fastboot", ["reboot", "bootloader"]), 1, 1, 1, "TButton"),
            ("Reboot recovery", lambda: self.tool_task(
                "fastboot", ["reboot", "recovery"], fallback=["oem", "reboot-recovery"]), 2, 0, 1, "TButton"),
            ("Reboot EDL", self.fb_edl, 2, 1, 1, "TButton"),
            ("Set slot A", lambda: self.tool_task("fastboot", ["--set-active=a"]), 3, 0, 1, "TButton"),
            ("Set slot B", lambda: self.tool_task("fastboot", ["--set-active=b"]), 3, 1, 1, "TButton"),
            ("Cek slot aktif", lambda: self.task(self.do_check_slot), 4, 0, 2, "TButton"),
            ("Flashing unlock…", lambda: self.fb_flashing("unlock"), 5, 0, 1, "Danger.TButton"),
            ("Flashing lock…", lambda: self.fb_flashing("lock"), 5, 1, 1, "Danger.TButton"),
            ("Fastboot wipe (hapus data)…", self.fb_wipe, 6, 0, 2, "Danger.TButton"),
        ]
        for text, cmd, r, c, span, style in items:
            self.mkbtn(ff, text, cmd, style).grid(row=r, column=c, columnspan=span,
                                                   sticky="ew", padx=2, pady=3)

    # ---- tab 2: flash partisi satuan ----
    def tab_partitions(self, page):
        top = ttk.Frame(page)
        top.pack(fill="x")
        ttk.Label(top, text="Slot aktif:", style="Muted.TLabel").pack(side="left")
        ttk.Label(top, textvariable=self.var_slot, font=(MONO_FONT, 13, "bold")).pack(
            side="left", padx=(8, 14))
        self.mkbtn(top, "Cek slot aktif", lambda: self.task(self.do_check_slot)).pack(side="left")
        ttk.Label(page, text="Flash = tanpa akhiran, fastboot memakai slot aktif.  "
                             "Flash _a / _b = langsung ke slot yang dipilih.",
                  style="Muted.TLabel").pack(anchor="w", pady=(8, 8))

        grid = ttk.Frame(page)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)
        rows = [("boot", ("boot", "boot_a", "boot_b")),
                ("init_boot", ("init_boot", "init_boot_a", "init_boot_b")),
                ("vendor_boot", ("vendor_boot", "vendor_boot_a", "vendor_boot_b")),
                ("recovery", ("recovery", "recovery_a", "recovery_b")),
                ("system", ("system", "system_a", "system_b")),
                ("super", ("super", "super_a", "super_b"))]
        for r, (title, parts) in enumerate(rows):
            var = tk.StringVar()
            ttk.Label(grid, text=title, width=13, font=(MONO_FONT, 10, "bold")).grid(
                row=r, column=0, sticky="w", pady=4)
            ttk.Entry(grid, textvariable=var, state="readonly").grid(row=r, column=1, sticky="ew", padx=6)
            ttk.Button(grid, text="Pilih file…",
                       command=lambda v=var, t=title: self.browse_image(v, t)).grid(
                row=r, column=2, padx=(0, 6))
            for c, part in enumerate(parts):
                text = "Flash" if c == 0 else "Flash " + part[len(title):]
                self.mkbtn(grid, text, lambda p=part, v=var: self.flash_part(p, v)).grid(
                    row=r, column=3 + c, padx=2, sticky="ew")

        # baris bebas: nama partisi lain (vbmeta, dtbo, dst.)
        r = len(rows)
        self.var_custom = tk.StringVar()
        cvar = tk.StringVar()
        ttk.Entry(grid, textvariable=self.var_custom, width=13).grid(row=r, column=0, sticky="w", pady=4)
        ttk.Entry(grid, textvariable=cvar, state="readonly").grid(row=r, column=1, sticky="ew", padx=6)
        ttk.Button(grid, text="Pilih file…",
                   command=lambda: self.browse_image(cvar, "partisi lain")).grid(row=r, column=2, padx=(0, 6))
        self.mkbtn(grid, "Flash", lambda: self.flash_part(self.var_custom.get().strip(), cvar)).grid(
            row=r, column=3, padx=2, sticky="ew")
        ttk.Label(page, text="Baris paling bawah: ketik nama partisi lain (mis. vbmeta, dtbo) lalu pilih filenya.",
                  style="Muted.TLabel").pack(anchor="w", pady=(6, 0))
        ttk.Label(page, text="Catatan: system & super adalah partisi dinamis — flash-nya butuh mode "
                             "fastbootd (reboot fastboot). super umumnya tunggal (slotless), jadi "
                             "pakai tombol Flash, bukan _a / _b, kecuali perangkat Anda memang punya super_a/super_b.",
                  style="Muted.TLabel", wraplength=self.px(600), justify="left").pack(anchor="w", pady=(4, 0))

    # ---- tab 3: flasher scatter / rawprogram ----
    # ---- tab Samsung (gaya Odin, memanggil odin4) ----
    # Slot file Odin -> flag odin4 yang setara. odin4 adalah utilitas flashing Samsung
    # untuk Linux/Windows yang beredar resmi; tool ini hanya memanggilnya, bukan
    # mengimplementasikan protokol Odin/LOKE milik Samsung.
    ODIN_SLOTS = (("BL", "-b", "Bootloader (sboot, dll)"),
                  ("AP", "-a", "Android Platform (image utama, besar)"),
                  ("CP", "-c", "Modem / baseband"),
                  ("CSC", "-s", "Country/Carrier (CSC = wipe, HOME_CSC = simpan data)"),
                  ("USERDATA", "-u", "Userdata (opsional)"))

    def tab_samsung(self, page):
        head = ttk.Frame(page)
        head.pack(fill="x")
        ttk.Label(head, text="Flashing Samsung (gaya Odin)", font=(UI_FONT, 11, "bold")).pack(side="left")
        ttk.Button(head, text="Cek tool", command=lambda: self.task(self.do_odin_check)).pack(side="right")
        ttk.Label(page, style="Muted.TLabel", justify="left", wraplength=self.px(640),
                  text="Masukkan file firmware resmi Samsung (.tar / .tar.md5) ke slot yang sesuai, "
                       "lalu HP harus berada di mode Download. Di Linux (atau odin4 build Windows) "
                       "tool menjalankan flash lewat odin4 otomatis; di Windows tanpa odin4, tombol "
                       "Start membuka Odin3 dan Anda flash manual di sana.").pack(anchor="w", pady=(6, 10))

        grid = ttk.Frame(page)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)
        self.odin_vars = {}
        for r, (slot, flag, desc) in enumerate(self.ODIN_SLOTS):
            ttk.Label(grid, text=slot, width=10, font=(MONO_FONT, 10, "bold")).grid(
                row=r, column=0, sticky="w", pady=4)
            var = tk.StringVar()
            self.odin_vars[slot] = var
            ttk.Entry(grid, textvariable=var, state="readonly").grid(row=r, column=1, sticky="ew", padx=6)
            ttk.Button(grid, text="Pilih…",
                       command=lambda v=var, s=slot: self.browse_odin(v, s)).grid(row=r, column=2, padx=(0, 4))
            ttk.Button(grid, text="Hapus",
                       command=lambda v=var: v.set("")).grid(row=r, column=3)
            ttk.Label(grid, text=desc, style="Muted.TLabel").grid(row=r, column=4, sticky="w", padx=(8, 0))

        opt = ttk.LabelFrame(page, text=" Opsi ", padding=10)
        opt.pack(fill="x", pady=(12, 0))
        self.odin_reboot = tk.BooleanVar(value=True)
        self.odin_home_csc = tk.BooleanVar(value=True)
        self.odin_nand_erase = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="Auto reboot setelah selesai", variable=self.odin_reboot).grid(
            row=0, column=0, sticky="w", padx=(0, 18), pady=2)
        ttk.Checkbutton(opt, text="HOME_CSC (simpan data; jangan wipe)", variable=self.odin_home_csc).grid(
            row=0, column=1, sticky="w", pady=2)
        ttk.Checkbutton(opt, text="Nand erase", variable=self.odin_nand_erase).grid(
            row=1, column=0, sticky="w", padx=(0, 18), pady=2)
        ttk.Label(opt, style="Muted.TLabel", wraplength=self.px(620), justify="left",
                  text="Catatan: CSC akan menghapus data; pilih HOME_CSC untuk mempertahankannya "
                       "(hanya berlaku bila firmware Anda menyertakan HOME_CSC). Re-partition penuh "
                       "ikut paket PIT firmware — tidak disetel manual di sini.").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))

        run = ttk.Frame(page)
        run.pack(fill="x", pady=(12, 0))
        self.mkbtn(run, "Start", self.odin_start, "Accent.TButton").pack(side="left")
        self.btn_open_odin3 = self.mkbtn(run, "Buka Odin3", self.open_odin3)
        self.btn_open_odin3.pack(side="left", padx=(6, 0))
        ttk.Label(run, style="Muted.TLabel",
                  text="HP harus di mode Download. FRP / Reactivation Lock tidak disentuh tool ini.").pack(
            side="left", padx=12)
        self.var_odin_engine = tk.StringVar(value="")
        ttk.Label(page, textvariable=self.var_odin_engine, style="Muted.TLabel").pack(
            anchor="w", pady=(8, 0))
        self.update_odin_engine_label()

    def browse_odin(self, var, slot):
        path = filedialog.askopenfilename(
            title=f"Pilih file untuk slot {slot}",
            filetypes=[("Firmware Odin", "*.tar *.tar.md5 *.md5"), ("Semua file", "*.*")])
        if path:
            var.set(path)

    @staticmethod
    def find_odin3(user_dirs):
        """Cari Odin3 GUI untuk Windows. Nama filenya bervariasi antar rilis."""
        import glob
        names = ["Odin3.exe", "Odin3_v3.14.4.exe", "Odin3_v3.14.1.exe", "Odin3 v3.14.4.exe",
                 "Odin.exe"]
        roots = list(user_dirs) + [str(BASE_DIR), str(BASE_DIR / "Odin3"), str(BASE_DIR / "odin3")]
        for root in roots:
            r = Path(root)
            for n in names:
                if (r / n).is_file():
                    return str(r / n)
            # pola Odin3*.exe di folder tersebut dan satu tingkat di bawahnya
            for pat in (str(r / "Odin3*.exe"), str(r / "*" / "Odin3*.exe")):
                hits = sorted(glob.glob(pat))
                if hits:
                    return hits[0]
        return None

    def update_odin_engine_label(self):
        if self.odin4:
            txt = f"Mesin flash: odin4 otomatis  ({self.odin4})"
        elif self.odin3:
            txt = f"Mesin flash: Odin3 (manual)  ({self.odin3})"
        else:
            txt = "Mesin flash: belum ada (taruh odin4 atau Odin3 di folder tools)."
        self.var_odin_engine.set(txt)
        if hasattr(self, "btn_open_odin3"):
            self.btn_open_odin3.state(["!disabled"] if self.odin3 else ["disabled"])

    def do_odin_check(self):
        found = False
        if self.odin4:
            found = True
            self.log(f"odin4: {self.odin4}", "info")
            rc, _ = self.run_stream([self.odin4, "-v"])
            if rc != 0:
                self.run_stream([self.odin4])  # sebagian build tak punya -v
        if self.odin3:
            found = True
            self.log(f"Odin3 (GUI Windows): {self.odin3}", "info")
        if not found:
            self.log("Belum ada odin4 maupun Odin3. Di Windows, taruh Odin3 (mis. Odin3.exe) di "
                     "folder tools lalu klik 'Folder tools…'.", "err")
        else:
            self.log("Tool Samsung siap. Gunakan tombol Start.", "ok")
        self.update_odin_engine_label()

    def open_odin3(self):
        if not self.odin3:
            self.log("Odin3 tidak ditemukan. Taruh Odin3 (mis. Odin3.exe) di folder tools lalu klik "
                     "'Folder tools…'.", "err")
            return
        self.log(f"Membuka Odin3: {self.odin3}", "info")
        try:
            subprocess.Popen([self.odin3], cwd=str(Path(self.odin3).parent), creationflags=NO_WINDOW)
            self.log("Odin3 dibuka. Isikan slot BL/AP/CP/CSC di jendela Odin3 lalu tekan Start di sana. "
                     "Path file yang Anda pilih di tab ini bisa dijadikan acuan.", "ok")
        except OSError as exc:
            self.log(f"Gagal membuka Odin3: {exc}", "err")

    def odin_start(self):
        # Windows tanpa odin4: alihkan ke Odin3 (GUI, flash manual).
        if not self.odin4:
            if self.odin3:
                self.log("odin4 tidak ada — membuka Odin3 untuk flashing manual.", "info")
                self.open_odin3()
            else:
                self.log("Belum ada mesin flash Samsung. Taruh odin4 atau Odin3 di folder tools, "
                         "lalu klik 'Folder tools…'.", "err")
            return
        files = {slot: var.get().strip() for slot, var in self.odin_vars.items()}
        chosen = {s: p for s, p in files.items() if p}
        if not chosen:
            self.log("Belum ada file firmware yang dipilih untuk slot mana pun.", "warn")
            return
        for slot, path in chosen.items():
            if not Path(path).is_file():
                self.log(f"File slot {slot} tidak ditemukan: {path}", "err")
                return
        lines = [f"{slot}: {Path(p).name}" for slot, p in chosen.items()]
        lines.append("")
        lines.append("Auto reboot : " + ("ya" if self.odin_reboot.get() else "tidak"))
        if "CSC" in chosen:
            lines.append("CSC mode    : " + ("HOME_CSC (simpan data)" if self.odin_home_csc.get()
                                            else "CSC (WIPE data)"))
        if self.odin_nand_erase.get():
            lines.append("Nand erase  : YA")
        if not messagebox.askyesno("Konfirmasi flash Samsung",
                                   "Flash firmware berikut lewat odin4?\n\n" + "\n".join(lines) +
                                   "\n\nPastikan HP di mode Download. Lanjutkan?"):
            return
        self.task(self.do_odin_flash, chosen)

    def do_odin_flash(self, chosen):
        exe = self.odin4
        cmd = [exe]
        # peta slot -> flag (lihat ODIN_SLOTS). CSC: pakai HOME_CSC bila diminta & tersedia.
        flag_of = {slot: flag for slot, flag, _ in self.ODIN_SLOTS}
        for slot, path in chosen.items():
            cmd += [flag_of[slot], path]
        # odin4 memakai argumen posisi untuk file; opsi tambahan bergantung versi odin4.
        # Hanya tambahkan flag yang umum didukung; selain itu biarkan default odin4.
        if not self.odin_reboot.get():
            cmd.append("--no-reboot")  # diabaikan bila versi odin4 tidak mengenalnya
        self.log("Mulai flashing Samsung (odin4). Jangan cabut kabel.", "ok")
        rc, _ = self.run_stream(cmd)
        if rc == 0:
            self.log("✓ odin4 selesai.", "ok")
        else:
            self.log(f"✗ odin4 gagal (kode {rc}). Pastikan HP di mode Download, driver terpasang, "
                     f"dan file firmware benar.", "err")

    # ---------------- Xiaomi MediaTek Tool (Solusi 2: Cloud Auto-Download) ----------------
    XIAOMI_ENGINE_URL = "https://sxdhwmrtzuzuzdcjxsso.supabase.co/storage/v1/object/public/engines/xiaomi_mtk_engine.zip"

    def find_xiaomi_mtk_engine(self):
        """Cari Python & script mtkclient di sistem atau folder tools/xiaomi (Solusi 2)."""
        roots = [
            BASE_DIR / "tools" / "xiaomi",
            BASE_DIR / "tools" / "mtk",
            BASE_DIR / "data",
        ]
        for r in roots:
            py = r / "python.exe" if IS_WIN else r / "python3"
            mtk = r / "mtk"
            if py.is_file() and mtk.is_file():
                return str(py), str(mtk), str(r)

        sys_py = shutil.which("python") or shutil.which("python3")
        for r in roots:
            mtk = r / "mtk"
            if sys_py and mtk.is_file():
                return sys_py, str(mtk), str(r)

        return None, None, None

    def update_xiaomi_engine_ui(self):
        py_exe, mtk_script, mtk_dir = self.find_xiaomi_mtk_engine()
        if hasattr(self, "lbl_engine_info") and hasattr(self, "btn_dl_engine"):
            if py_exe:
                self.lbl_engine_info.config(text=f"✓ Engine Xiaomi MTK aktif & siap digunakan ({mtk_dir}).", foreground=GREEN)
                self.btn_dl_engine.config(text="🔄 Unduh Ulang Engine", style="TButton")
                self.ec_prog_frame.pack_forget()
            else:
                self.lbl_engine_info.config(text="⚠️ Engine Xiaomi MTK belum diunduh (±29 MB). Unduh otomatis dari Cloud Supabase sekali saja.", foreground=AMBER)
                self.btn_dl_engine.config(text="📥 Unduh & Pasang Engine Xiaomi", style="Accent.TButton")

    def start_download_engine(self):
        self.btn_dl_engine.config(state="disabled", text="Mengunduh...")
        self.ec_prog_frame.pack(fill="x", pady=(6, 2))
        self.pb_engine["value"] = 0
        self.lbl_engine_percent.config(text="Memulai unduhan...")
        self.log("Memulai pengunduhan Engine Xiaomi MTK dari Cloud Supabase...", "info")

        def _worker():
            tools_dir = BASE_DIR / "tools"
            tools_dir.mkdir(parents=True, exist_ok=True)
            temp_zip = tools_dir / "temp_xiaomi_engine.zip"
            dest_dir = tools_dir / "xiaomi"

            try:
                req = urllib.request.Request(self.XIAOMI_ENGINE_URL, headers={"User-Agent": "DroidFix-Flash-Tool/1.0"})
                with urllib.request.urlopen(req, timeout=40) as resp:
                    total_bytes = int(resp.headers.get("content-length", 0))
                    downloaded = 0
                    with open(temp_zip, "wb") as f:
                        while True:
                            chunk = resp.read(64 * 1024)
                            if not chunk:
                                break
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total_bytes > 0:
                                pct = int((downloaded / total_bytes) * 100)
                                dl_mb = downloaded / (1024 * 1024)
                                tot_mb = total_bytes / (1024 * 1024)
                                txt = f"{pct}% ({dl_mb:.1f} / {tot_mb:.1f} MB)"
                                self.ui(lambda p=pct, t=txt: self._update_dl_prog(p, t))

                self.ui(lambda: self.lbl_engine_percent.config(text="Mengekstrak file engine..."))
                self.log("Unduhan selesai. Mengekstrak file engine ke tools/xiaomi...", "ok")
                with zipfile.ZipFile(temp_zip, "r") as zf:
                    zf.extractall(dest_dir)

                if temp_zip.exists():
                    try:
                        temp_zip.unlink()
                    except Exception:
                        pass

                self.ui(lambda: self._on_dl_success())
            except Exception as exc:
                self.ui(lambda e=str(exc): self._on_dl_failed(e))

        threading.Thread(target=_worker, daemon=True).start()

    def _update_dl_prog(self, percent, text):
        self.pb_engine["value"] = percent
        self.lbl_engine_percent.config(text=text)

    def _on_dl_success(self):
        self.btn_dl_engine.config(state="normal")
        self.update_xiaomi_engine_ui()
        self.log("✓ Engine Xiaomi MTK berhasil dipasang dari Cloud dan siap digunakan!", "ok")
        messagebox.showinfo("Setup Engine Sukses", "Engine Xiaomi MTK berhasil diunduh dan dipasang secara otomatis!\nFitur UBL dan Bypass siap digunakan.", parent=self)

    def _on_dl_failed(self, err_msg):
        self.btn_dl_engine.config(state="normal", text="📥 Coba Unduh Lagi", style="Accent.TButton")
        self.lbl_engine_percent.config(text="Gagal mengunduh!")
        self.log(f"Gagal mengunduh engine dari cloud: {err_msg}", "err")
        messagebox.showerror("Download Gagal", f"Gagal mengunduh engine dari Cloud Supabase:\n{err_msg}\n\nPastikan bucket 'engines' sudah dibuat dan file engine sudah di-upload ke Supabase Storage.", parent=self)

    def tab_xiaomi(self, page):
        head = ttk.Frame(page)
        head.pack(fill="x")
        ttk.Label(head, text="Xiaomi MediaTek Bootloader Tool", font=(UI_FONT, 11, "bold")).pack(side="left")

        # Petunjuk koneksi
        info_frame = ttk.Frame(page, padding=6)
        info_frame.pack(fill="x", pady=(4, 6))
        ttk.Label(info_frame,
                  text="Petunjuk BROM: Matikan HP -> Tahan Vol(+) & Vol(-) bersamaan -> Colok kabel USB ke PC.",
                  foreground=ACCENT, font=(UI_FONT, 9, "bold")).pack(anchor="w")

        # Setup Engine Cloud (Solusi 2)
        self.engine_card = ttk.LabelFrame(page, text=" Setup Engine Xiaomi MTK (Cloud Auto-Download) ", padding=8)
        self.engine_card.pack(fill="x", pady=(0, 8))

        ec_top = ttk.Frame(self.engine_card)
        ec_top.pack(fill="x")
        self.lbl_engine_info = ttk.Label(ec_top, text="")
        self.lbl_engine_info.pack(side="left")
        self.btn_dl_engine = ttk.Button(ec_top, text="📥 Unduh Engine MTK", command=self.start_download_engine, style="Accent.TButton")
        self.btn_dl_engine.pack(side="right")

        self.ec_prog_frame = ttk.Frame(self.engine_card)
        self.pb_engine = ttk.Progressbar(self.ec_prog_frame, orient="horizontal", mode="determinate")
        self.pb_engine.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.lbl_engine_percent = ttk.Label(self.ec_prog_frame, text="", style="Muted.TLabel")
        self.lbl_engine_percent.pack(side="right")

        self.update_xiaomi_engine_ui()

        # Tombol Aksi Utama
        act_box = ttk.LabelFrame(page, text=" Operasi Bootloader Xiaomi (MediaTek) ", padding=10)
        act_box.pack(fill="x", pady=(0, 8))

        row1 = ttk.Frame(act_box)
        row1.pack(fill="x", pady=2)

        self.mkbtn(row1, "Unlock Bootloader (UBL Instan)",
                   lambda: self.task(self.do_xiaomi_action, "ubl"), style="Accent.TButton").pack(side="left", padx=(0, 8))
        self.mkbtn(row1, "Relock Bootloader (Kunci UBL)",
                   lambda: self.task(self.do_xiaomi_action, "relock")).pack(side="left", padx=(0, 8))
        self.mkbtn(row1, "Bypass Auth (SLA/DAA BROM)",
                   lambda: self.task(self.do_xiaomi_action, "bypass")).pack(side="left", padx=(0, 8))

        row2 = ttk.Frame(act_box)
        row2.pack(fill="x", pady=(6, 2))
        self.mkbtn(row2, "Device Manager", self.open_dev_mgmt).pack(side="left", padx=(0, 8))
        self.mkbtn(row2, "Scan Port COM / USB", lambda: self.task(self.scan_mtk_ports)).pack(side="left", padx=(0, 8))
        self.mkbtn(row2, "Stop Proses", self.stop_xiaomi_process).pack(side="left")

        # Daftar Model yang Didukung (Cloud Sync via Supabase)
        model_box = ttk.LabelFrame(page, text=" Model Xiaomi MediaTek yang Didukung (Tersinkronisasi Cloud) ", padding=8)
        model_box.pack(fill="both", expand=True)

        ctrl_row = ttk.Frame(model_box)
        ctrl_row.pack(fill="x", pady=(0, 6))

        ttk.Label(ctrl_row, text="Filter Seri:", style="Muted.TLabel").pack(side="left", padx=(0, 6))
        self.var_series_filter = tk.StringVar(value="Semua Seri")
        cb_series = ttk.Combobox(ctrl_row, textvariable=self.var_series_filter, state="readonly", width=20)
        cb_series["values"] = ("Semua Seri", "Redmi Note Series", "Redmi Series", "Mi & Poco Series")
        cb_series.pack(side="left", padx=(0, 10))
        cb_series.bind("<<ComboboxSelected>>", lambda e: self.filter_xiaomi_models())

        self.mkbtn(ctrl_row, "Sinkronkan dari Supabase", self.load_xiaomi_models_cloud).pack(side="right")
        self.lbl_model_count = ttk.Label(ctrl_row, text="", style="Muted.TLabel")
        self.lbl_model_count.pack(side="right", padx=10)

        # Treeview Model
        tree_frame = ttk.Frame(model_box)
        tree_frame.pack(fill="both", expand=True)

        cols = ("model", "series", "chipset", "status")
        self.tree_models = ttk.Treeview(tree_frame, columns=cols, show="headings", height=6, selectmode="browse")
        self.tree_models.heading("model", text="Model HP")
        self.tree_models.heading("series", text="Seri")
        self.tree_models.heading("chipset", text="Chipset / Prosesor")
        self.tree_models.heading("status", text="Dukungan UBL")

        self.tree_models.column("model", width=self.px(220), anchor="w")
        self.tree_models.column("series", width=self.px(170), anchor="w")
        self.tree_models.column("chipset", width=self.px(150), anchor="w")
        self.tree_models.column("status", width=self.px(110), anchor="center")

        sb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree_models.yview)
        self.tree_models.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree_models.pack(side="left", fill="both", expand=True)

        self.xiaomi_models_data = []
        self.load_xiaomi_models_cloud()

    def open_dev_mgmt(self):
        try:
            subprocess.Popen(["cmd", "/c", "start", "devmgmt.msc"], shell=True)
            self.log("Membuka Device Manager Windows...", "info")
        except Exception as e:
            self.log(f"Gagal membuka Device Manager: {e}", "err")

    def scan_mtk_ports(self):
        self.log("Memindai port COM / USB untuk MediaTek...", "cmd")
        try:
            p = subprocess.run(["powershell", "-NoProfile", "-Command",
                                "Get-CimInstance Win32_PnPEntity | Where-Object { $_.Name -match 'MediaTek|VCOM|Preloader|USB Serial' } | Select-Object -ExpandProperty Name"],
                               capture_output=True, text=True, timeout=8, creationflags=NO_WINDOW)
            res = p.stdout.strip()
            if res:
                for line in res.splitlines():
                    if line.strip():
                        self.log(f"Port Terdeteksi: {line.strip()}", "ok")
            else:
                self.log("Tidak ada port MediaTek aktif. Pastikan HP terhubung di mode BROM/Preloader.", "warn")
        except Exception as e:
            self.log(f"Gagal scan port: {e}", "err")

    def stop_xiaomi_process(self):
        try:
            subprocess.run(["taskkill", "/F", "/IM", "python.exe"], capture_output=True, creationflags=NO_WINDOW)
            self.log("Semua proses python background dihentikan.", "warn")
        except Exception:
            pass

    def do_xiaomi_action(self, action):
        py_exe, mtk_script, mtk_dir = self.find_xiaomi_mtk_engine()
        if not py_exe or not mtk_script:
            if messagebox.askyesno("Engine Belum Terpasang",
                                   "Engine Xiaomi MTK belum terpasang di laptop ini.\n\nUnduh otomatis dari Cloud Supabase sekarang (±29 MB)?"):
                self.start_download_engine()
            return

        cmd = [py_exe, mtk_script]
        action_name = "Unlock Bootloader"
        if action == "ubl":
            cmd += ["da", "seccfg", "unlock"]
            action_name = "Unlock Bootloader (UBL Instan)"
        elif action == "relock":
            cmd += ["da", "seccfg", "lock"]
            action_name = "Relock Bootloader"
        elif action == "bypass":
            cmd += ["payload"]
            action_name = "Bypass Auth (BROM Exploit)"

        self.log(f"=== Memulai Operasi Xiaomi: {action_name} ===", "info")
        self.log("Menunggu perangkat MediaTek dihubungkan (Tahan Vol+ & Vol- lalu colok USB)...", "warn")

        t0 = time.time()
        try:
            p = subprocess.Popen(cmd, cwd=mtk_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                                 errors="replace", bufsize=1, creationflags=NO_WINDOW)
        except Exception as e:
            self.log(f"Gagal memulai eksekusi: {e}", "err")
            return

        lines = []
        for line in p.stdout:
            line = line.rstrip()
            if line:
                lines.append(line)
                bad = re.search(r"FAILED|error|fail", line, re.I)
                self.log(line, "err" if bad else "out")
        rc = p.wait()
        dur = int(time.time() - t0)
        self.report(rc)

        if getattr(self, "supabase", None):
            self.supabase.log_flash(
                tool_type="xiaomi_mtk",
                mode=action,
                command=" ".join(cmd),
                status="SUCCESS" if rc == 0 else "FAILED",
                duration=dur,
                log_text="\n".join(lines[-20:])
            )

    def load_xiaomi_models_cloud(self):
        try:
            if getattr(self, "supabase", None):
                models = self.supabase.get_xiaomi_models()
            else:
                import droidfix_supabase
                models = droidfix_supabase.DEFAULT_XIAOMI_MODELS
            self.xiaomi_models_data = models or []
            self.filter_xiaomi_models()
            self.log(f"Katalog model Xiaomi berhasil disinkronkan ({len(self.xiaomi_models_data)} model).", "ok")
        except Exception as e:
            self.log(f"Gagal memuat katalog model: {e}", "err")

    def filter_xiaomi_models(self):
        if not hasattr(self, "tree_models"):
            return
        for item in self.tree_models.get_children():
            self.tree_models.delete(item)

        selected_series = self.var_series_filter.get()
        count = 0
        for item in self.xiaomi_models_data:
            series = item.get("series", "")
            if selected_series != "Semua Seri" and series != selected_series:
                continue
            model_name = item.get("model_name", "")
            chipset = item.get("chipset", "MediaTek")
            status = "Didukung (Instan)"
            self.tree_models.insert("", "end", values=(model_name, series, chipset, status))
            count += 1

        if hasattr(self, "lbl_model_count"):
            self.lbl_model_count.config(text=f"Total: {count} Model")

    def tab_flasher(self, page):
        top = ttk.Frame(page)
        top.pack(fill="x")
        self.mkbtn(top, "Pilih scatter (MediaTek)…", lambda: self.load_flash_file("scatter")).pack(side="left")
        self.mkbtn(top, "Pilih rawprogram XML (Qualcomm)…", lambda: self.load_flash_file("xml")).pack(
            side="left", padx=6)
        self.var_flfile = tk.StringVar(value="Belum ada file dimuat.")
        ttk.Label(page, textvariable=self.var_flfile, style="Muted.TLabel").pack(anchor="w", pady=(6, 4))

        box = ttk.Frame(page)
        box.pack(fill="both", expand=True)
        cols = ("sel", "part", "file", "size", "status")
        self.tree = ttk.Treeview(box, columns=cols, show="headings", height=6, selectmode="extended")
        for col, text, width, anchor in (("sel", "", 34, "center"), ("part", "Partisi", 110, "w"),
                                         ("file", "File", 230, "w"), ("size", "Ukuran", 80, "e"),
                                         ("status", "Status", 130, "w")):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=self.px(width), anchor=anchor, stretch=(col == "file"))
        sb = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.tag_configure("missing", foreground=RED)
        self.tree.tag_configure("skipped", foreground=MUTED)
        self.tree.bind("<Button-1>", self.on_tree_click)
        self.tree.bind("<space>", self.on_tree_space)

        sel = ttk.Frame(page)
        sel.pack(fill="x", pady=(6, 0))
        ttk.Button(sel, text="Pilih semua", command=lambda: self.set_all_checked("all")).pack(side="left")
        ttk.Button(sel, text="Kosongkan", command=lambda: self.set_all_checked("none")).pack(side="left", padx=6)
        ttk.Button(sel, text="Pilihan aman", command=lambda: self.set_all_checked("safe")).pack(side="left")
        self.var_flsummary = tk.StringVar(value="")
        ttk.Label(sel, textvariable=self.var_flsummary, style="Muted.TLabel").pack(side="right")

        self.var_wipe = tk.BooleanVar(value=False)
        self.var_reboot = tk.BooleanVar(value=True)
        self.var_keepgoing = tk.BooleanVar(value=False)
        o1 = ttk.Frame(page)
        o1.pack(fill="x", pady=(8, 0))
        ttk.Checkbutton(o1, text="Hapus data (userdata + metadata)", variable=self.var_wipe).pack(
            side="left", padx=(0, 14))
        ttk.Checkbutton(o1, text="Reboot di akhir", variable=self.var_reboot).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(o1, text="Lanjut walau gagal", variable=self.var_keepgoing).pack(side="left")

        run = ttk.Frame(page)
        run.pack(fill="x", pady=(8, 0))
        self.mkbtn(run, "Flash all (tanpa relock bootloader)", self.flash_all, "Accent.TButton").pack(side="left")
        ttk.Label(run, text="Bootloader tidak di-relock. Progres ada di bawah log.",
                  style="Muted.TLabel").pack(side="left", padx=12)

    # ---- tab 4: scrcpy ----
    def tab_scrcpy(self, page):
        ttk.Label(page, text="Mirroring layar HP ke laptop. HP harus menyala dengan USB debugging aktif "
                             "(status: ADB · Android nyala).", style="Muted.TLabel",
                  wraplength=self.px(640)).pack(anchor="w", pady=(0, 10))
        r1 = ttk.Frame(page)
        r1.pack(fill="x")
        ttk.Label(r1, text="Resolusi maks").pack(side="left")
        self.var_size = tk.StringVar(value="1280")
        ttk.Combobox(r1, textvariable=self.var_size, width=9, state="readonly",
                     values=["Original", "1920", "1600", "1280", "1024", "800"]).pack(
            side="left", padx=(6, 16))
        ttk.Label(r1, text="Bitrate").pack(side="left")
        self.var_bitrate = tk.StringVar(value="8M")
        ttk.Combobox(r1, textvariable=self.var_bitrate, width=6, state="readonly",
                     values=["2M", "4M", "8M", "12M", "16M", "24M"]).pack(side="left", padx=(6, 0))

        self.var_awake = tk.BooleanVar(value=True)
        self.var_screenoff = tk.BooleanVar(value=False)
        self.var_ontop = tk.BooleanVar(value=False)
        self.var_touches = tk.BooleanVar(value=False)
        self.var_noaudio = tk.BooleanVar(value=False)
        r2 = ttk.Frame(page)
        r2.pack(fill="x", pady=(12, 0))
        for i, (text, var) in enumerate((("Layar HP tetap nyala", self.var_awake),
                                         ("Matikan layar HP", self.var_screenoff),
                                         ("Jendela selalu di atas", self.var_ontop),
                                         ("Tampilkan sentuhan", self.var_touches),
                                         ("Tanpa audio", self.var_noaudio))):
            ttk.Checkbutton(r2, text=text, variable=var).grid(row=i // 3, column=i % 3, sticky="w",
                                                              padx=(0, 20), pady=3)
        r3 = ttk.Frame(page)
        r3.pack(fill="x", pady=(14, 0))
        self.btn_start = ttk.Button(r3, text="Mulai mirroring", style="Accent.TButton",
                                    command=self.start_scrcpy)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(r3, text="Stop", command=self.stop_scrcpy, state="disabled")
        self.btn_stop.pack(side="left", padx=6)

    # ---- log (bawah) ----
    def build_statusbar(self, parent):
        """Strip status/progres selebar jendela, menempel di tepi bawah (gaya DFT)."""
        bar = tk.Frame(parent, bg=LOGBG, highlightthickness=1, highlightbackground=BORDER)
        bar.pack(side="bottom", fill="x", pady=(8, 0))
        row = tk.Frame(bar, bg=LOGBG)
        row.pack(fill="x", padx=10, pady=(6, 4))
        self.var_prog = tk.StringVar(value="Siap")
        tk.Label(row, textvariable=self.var_prog, bg=LOGBG, fg=MUTED,
                 font=(MONO_FONT, 9), anchor="w").pack(side="left")
        self.btn_cancel = ttk.Button(row, text="Batalkan", command=self.cancel_flash, state="disabled")
        self.btn_cancel.pack(side="right")
        self.pb = ttk.Progressbar(bar, mode="determinate", style="DFT.Horizontal.TProgressbar")
        self.pb.pack(fill="x", side="bottom")

    def build_log(self, parent):
        lf = ttk.LabelFrame(parent, text=" Log ", padding=8)
        lf.pack(fill="both", expand=True)
        bar = ttk.Frame(lf)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text="Bersihkan", command=self.clear_log).pack(side="left")
        ttk.Button(bar, text="Simpan log…", command=self.save_log).pack(side="left", padx=6)
        self.var_autoscroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Gulir otomatis", variable=self.var_autoscroll).pack(side="left", padx=10)

        body = ttk.Frame(lf)
        body.pack(fill="both", expand=True)
        sb = ttk.Scrollbar(body, orient="vertical")
        sb.pack(side="right", fill="y")
        self.txt = tk.Text(body, bg=LOGBG, fg=FG, insertbackground=FG, relief="flat",
                           font=(MONO_FONT, 11), wrap="word", padx=12, pady=10, height=12,
                           spacing1=1, spacing3=2, state="disabled", yscrollcommand=sb.set)
        self.txt.pack(side="left", fill="both", expand=True)
        sb.config(command=self.txt.yview)
        self.txt.tag_config("time", foreground="#586178")
        self.txt.tag_config("cmd", foreground="#7dd3fc", font=(MONO_FONT, 11, "bold"))
        self.txt.tag_config("out", foreground=FG)
        self.txt.tag_config("ok", foreground=GREEN)
        self.txt.tag_config("warn", foreground=AMBER)
        self.txt.tag_config("err", foreground=RED)
        self.txt.tag_config("info", foreground="#a5b4fc")

    def pb_indeterminate(self, on):
        if on and not self.pb_ind:
            self.pb_ind = True
            self.pb.configure(mode="indeterminate")
            self.pb.start(14)
        elif not on and self.pb_ind:
            self.pb_ind = False
            self.pb.stop()
            self.pb.configure(mode="determinate")

    # ---------------- antrean UI / log ----------------
    def ui(self, fn, *args):
        self.uiq.put((fn, args))

    def drain_ui_queue(self):
        try:
            for _ in range(400):
                fn, args = self.uiq.get_nowait()
                try:
                    fn(*args)
                except Exception:  # noqa: BLE001
                    pass
        except queue.Empty:
            pass
        if not self.closing:
            self.after(60, self.drain_ui_queue)

    def log(self, msg, tag="out"):
        self.ui(self._log_ui, msg, tag)

    def _log_ui(self, msg, tag):
        ts = datetime.now().strftime("%H:%M:%S")
        self.txt.configure(state="normal")
        for line in (str(msg).splitlines() or [""]):
            self.txt.insert("end", f"[{ts}] ", "time")
            self.txt.insert("end", line + "\n", tag)
        if int(self.txt.index("end-1c").split(".")[0]) > 6000:
            self.txt.delete("1.0", "1500.0")
        self.txt.configure(state="disabled")
        if self.var_autoscroll.get():
            self.txt.see("end")

    def clear_log(self):
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")

    def save_log(self):
        name = datetime.now().strftime("droidfix_log_%Y%m%d_%H%M%S.txt")
        path = filedialog.asksaveasfilename(title="Simpan log", initialfile=name,
                                            defaultextension=".txt",
                                            filetypes=[("Teks", "*.txt"), ("Semua file", "*.*")])
        if path:
            Path(path).write_text(self.txt.get("1.0", "end"), encoding="utf-8")
            self.log(f"Log disimpan ke {path}", "ok")

    # ---------------- lokasi tools ----------------
    def locate_tools(self, initial=False):
        dirs = self.cfg.get("tool_dirs", [])
        self.adb = find_tool("adb", dirs)
        self.fastboot = find_tool("fastboot", dirs)
        self.scrcpy = find_tool("scrcpy", dirs)
        self.odin4 = find_tool("odin4", dirs)
        self.odin3 = self.find_odin3(dirs)
        parts, ok_all = [], True
        # odin4 opsional: tidak memengaruhi status ok_all (hanya dipakai di tab Samsung)
        for name, path in (("adb", self.adb), ("fastboot", self.fastboot), ("scrcpy", self.scrcpy)):
            parts.append(f"{name} {'✓' if path else '✗'}")
            ok_all &= bool(path)
            if path:
                self.log(f"{name}: {path}", "info")
            else:
                self.log(f"{exe_name(name)} tidak ditemukan. Taruh foldernya di sebelah program "
                         f"ini, atau klik 'Folder tools…'.", "warn")
        if self.odin4:
            self.log(f"odin4: {self.odin4}", "info")
        if self.odin3:
            self.log(f"Odin3: {self.odin3}", "info")
        if not self.odin4 and not self.odin3:
            self.log("odin4 / Odin3 belum ada (opsional, untuk tab Samsung). Di Windows taruh "
                     "Odin3 (mis. Odin3.exe) di folder tools bila ingin flashing Samsung.", "info")
        self.lbl_tools.config(text="   ".join(parts), fg=GREEN if ok_all else AMBER)
        if initial:
            self.log("Siap. Colok HP lewat USB; status akan muncul otomatis.", "ok")
        self.refresh_now.set()

    def choose_tools(self):
        d = filedialog.askdirectory(title="Pilih folder yang berisi adb / fastboot / scrcpy")
        if d:
            dirs = [x for x in self.cfg.get("tool_dirs", []) if x != d]
            self.cfg["tool_dirs"] = [d] + dirs
            self.save_config()
            self.locate_tools()

    # ---------------- polling status perangkat ----------------
    def scan_adb(self):
        if not self.adb:
            return []
        rc, out = capture([self.adb, "devices", "-l"], timeout=20)
        return parse_adb_devices(out) if rc == 0 else []

    def scan_fastboot(self):
        if not self.fastboot:
            return []
        rc, out = capture([self.fastboot, "devices"], timeout=10)
        return parse_fastboot_devices(out) if rc == 0 else []

    def scan_usb(self):
        """Deteksi EDL 9008 / MTK BROM lewat daftar perangkat USB sistem."""
        if IS_WIN:
            ps = ("Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | "
                  "Where-Object { $_.InstanceId -match 'VID_05C6&PID_9008|VID_0E8D&PID_0003' } | "
                  "ForEach-Object { $_.InstanceId }")
            rc, out = capture(["powershell", "-NoProfile", "-NonInteractive",
                               "-ExecutionPolicy", "Bypass", "-Command", ps], timeout=12)
            text = out.upper()
        else:
            rc, out = capture(["lsusb"], timeout=5)
            text = out.lower()
        found = []
        for win_id, lsusb_id, state, name in USB_SPECIAL:
            if (win_id in text) if IS_WIN else (lsusb_id in text):
                found.append(Device("usb", state, state, name))
        return found

    def fb_getvar(self, serial, var):
        rc, out = capture([self.fastboot, "-s", serial, "getvar", var], timeout=10)
        m = re.search(rf"^{re.escape(var)}:\s*(.+)$", out, re.M)
        return m.group(1).strip() if m else ""

    def fetch_details(self, d):
        """Ambil info singkat perangkat. None = gagal, coba lagi di polling berikutnya."""
        if d.tool == "adb" and d.state in ("device", "recovery"):
            rc, out = capture([self.adb, "-s", d.serial, "shell", "getprop"], timeout=10)
            if rc != 0:
                return None
            p = parse_props(out)
            return {
                "brand": p.get("ro.product.brand", ""),
                "model": p.get("ro.product.model", ""),
                "android": p.get("ro.build.version.release", ""),
                "sdk": p.get("ro.build.version.sdk", ""),
                "patch": p.get("ro.build.version.security_patch", ""),
                "slot": p.get("ro.boot.slot_suffix", "").lstrip("_"),
            }
        if d.tool == "fastboot" and self.fastboot:
            return {v: self.fb_getvar(d.serial, v)
                    for v in ("product", "current-slot", "unlocked", "is-userspace")}
        return {}

    def poll_loop(self):
        usb, last_usb = [], 0.0
        while not self.closing:
            if not self.is_busy:
                try:
                    devs = self.scan_adb() + self.scan_fastboot()
                    if devs:
                        usb = []
                    else:
                        if time.time() - last_usb > 3:
                            usb, last_usb = self.scan_usb(), time.time()
                        devs = list(usb)
                    for d in devs:
                        if d.key not in self.details:
                            info = self.fetch_details(d)
                            if info is not None:
                                self.details[d.key] = info
                    alive = {d.key for d in devs}
                    for k in list(self.details):
                        if k not in alive:
                            self.details.pop(k, None)
                    self.ui(self.apply_devices, devs)
                except Exception as exc:  # noqa: BLE001
                    self.log(f"Polling error: {exc}", "err")
            self.refresh_now.wait(2.0)
            self.refresh_now.clear()

    def apply_devices(self, devs):
        old = {d.key: d for d in self.devices}
        new = {d.key: d for d in devs}
        for k, d in new.items():
            if k not in old:
                self.log(f"Terhubung: {self.describe(d)[0]} · {d.serial or d.extra}", "ok")
        for k, d in old.items():
            if k not in new:
                self.log(f"Terputus: {d.label()}", "warn")
        self.devices = devs
        keys = [d.key for d in devs]
        if self.sel_key not in keys:
            self.sel_key = keys[0] if keys else None
        self.render_status()

    def on_pick(self, _event=None):
        i = self.cb.current()
        if 0 <= i < len(self.devices):
            self.sel_key = self.devices[i].key
            self.render_status()

    def selected(self):
        for d in self.devices:
            if d.key == self.sel_key:
                return d
        return None

    def pick(self, tool):
        """Pilih perangkat untuk tool tertentu: yang dipilih user, kalau tidak ada yang pertama."""
        devs = [d for d in self.devices if d.tool == tool]
        for d in devs:
            if d.key == self.sel_key:
                return d
        return devs[0] if devs else None

    # ---------------- tampilan status ----------------
    def describe(self, d):
        info = self.details.get(d.key) or {}
        na = "-"
        if d.tool == "adb":
            title, color = {
                "device": ("ADB · Android nyala", GREEN),
                "recovery": ("Recovery · ADB", AMBER),
                "sideload": ("Sideload mode", BLUE),
                "unauthorized": ("ADB · belum diizinkan", AMBER),
                "offline": ("ADB · offline", RED),
            }.get(d.state, (f"ADB · {d.state}", GREY))
            if d.state == "unauthorized":
                rows = [("Serial", d.serial),
                        ("Catatan", "Lihat layar HP, lalu izinkan USB debugging.")]
            else:
                model = " ".join(x for x in (info.get("brand"), info.get("model")) if x) or d.extra or na
                android = info.get("android") or na
                if info.get("sdk") and android != na:
                    android += f"  (SDK {info['sdk']})"
                rows = [("Serial", d.serial), ("Model", model), ("Android", android),
                        ("Security patch", info.get("patch") or na),
                        ("Slot", info.get("slot") or na)]
        elif d.tool == "fastboot":
            userspace = info.get("is-userspace") == "yes"
            title, color = ("Fastbootd", AMBER) if userspace else ("Fastboot", AMBER)
            unl = (info.get("unlocked") or "").lower()
            bl = "Unlocked" if unl == "yes" else "Locked" if unl == "no" else (unl or na)
            rows = [("Fastboot code", d.serial),
                    ("Mode", "fastbootd (userspace)" if userspace else "bootloader"),
                    ("Product", info.get("product") or na),
                    ("Slot aktif", info.get("current-slot") or na),
                    ("Bootloader", bl)]
        elif d.state == "edl":
            title, color = "EDL · Qualcomm 9008", RED
            rows = [("Port USB", d.extra),
                    ("Catatan", "Flash lewat QFIL, MiFlash, atau edl.py.")]
        else:
            title, color = "MediaTek BROM", RED
            rows = [("Port USB", d.extra),
                    ("Catatan", "Flash lewat SP Flash Tool atau mtkclient.")]
        return title, color, rows

    def render_status(self):
        d = self.selected()
        if d is None:
            self.badge.config(text="Tidak ada perangkat", bg=GREY)
            self.cb.config(values=["(belum ada perangkat)"])
            self.cb.current(0)
            self.var_code.set("-")
            self.var_summary.set("Colok HP lewat USB, lalu nyalakan USB debugging atau masuk "
                                 "ke mode fastboot / recovery.")
        else:
            title, color, rows = self.describe(d)
            self.badge.config(text=title, bg=color, fg="#10141b" if color in (AMBER, GREEN) else "white")
            self.cb.config(values=[x.label() for x in self.devices])
            self.cb.current(self.devices.index(d))
            _, code_val = rows[0] if rows else ("", "")
            self.var_code.set(code_val or "-")
            self.var_summary.set(" · ".join(f"{k}: {v}" for k, v in rows[1:] if v))
        slot = ""
        if d is not None:
            info = self.details.get(d.key) or {}
            slot = (info.get("current-slot") or info.get("slot") or "").lstrip("_")
        self.var_slot.set(slot.upper() if slot else "-")

    def copy_serial(self):
        d = self.selected()
        if d and d.serial:
            self.clipboard_clear()
            self.clipboard_append(d.serial)
            self.log(f"Disalin ke clipboard: {d.serial}", "info")

    # ---------------- menjalankan perintah ----------------
    def set_busy_ui(self, busy):
        for b in self.act_buttons:
            b.state(["disabled"] if busy else ["!disabled"])
        if busy:
            self.var_prog.set("Bekerja…")
            self.pb_indeterminate(True)
        elif self.pb_ind:  # perintah biasa selesai; hasil flash all tetap ditampilkan
            self.pb_indeterminate(False)
            self.pb["value"] = 0
            self.var_prog.set("Siap")

    def task(self, fn, *args):
        if self.is_busy:
            self.log("Masih ada perintah yang berjalan. Tunggu sampai selesai.", "warn")
            return
        self.is_busy = True
        self.set_busy_ui(True)

        def run():
            try:
                fn(*args)
            except Exception as exc:  # noqa: BLE001
                self.log(f"Error: {exc}", "err")
            finally:
                self.is_busy = False
                self.details.clear()
                self.ui(self.set_busy_ui, False)
                self.refresh_now.set()

        threading.Thread(target=run, daemon=True).start()

    def run_stream(self, args):
        """Jalankan perintah dan tampilkan keluarannya baris demi baris di log."""
        self.log("$ " + subprocess.list2cmdline(args), "cmd")
        try:
            p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                                 errors="replace", bufsize=1, creationflags=NO_WINDOW)
        except OSError as exc:
            self.log(f"Tidak bisa menjalankan perintah: {exc}", "err")
            return -2, ""
        t0 = time.time()
        lines = []
        for line in p.stdout:
            line = line.rstrip()
            if line:
                lines.append(line)
                bad = re.search(r"FAILED|error|fail", line, re.I)
                self.log(line, "err" if bad else "out")
        rc = p.wait()
        dur = int(time.time() - t0)
        full_out = "\n".join(lines)
        if getattr(self, "supabase", None):
            try:
                tool_name = Path(args[0]).stem.lower() if args else "tool"
                self.supabase.log_flash(
                    tool_type=tool_name,
                    mode="exec",
                    command=subprocess.list2cmdline(args),
                    status="SUCCESS" if rc == 0 else "FAILED",
                    duration=dur,
                    log_text=full_out[-2000:] if full_out else ""
                )
            except Exception:
                pass
        return rc, full_out

    def resolve(self, tool):
        """Kembalikan (exe, serial) atau None bila belum siap; alasan ditulis ke log."""
        exe = self.adb if tool == "adb" else self.fastboot
        if not exe:
            self.log(f"{exe_name(tool)} tidak ditemukan. Klik 'Folder tools…' untuk menunjuk lokasinya.", "err")
            return None
        dev = self.pick(tool)
        if not dev:
            hint = ("Pastikan USB debugging aktif." if tool == "adb"
                    else "Pastikan HP sudah ada di mode fastboot.")
            self.log(f"Tidak ada perangkat {tool} yang terdeteksi. {hint}", "warn")
            return None
        return exe, dev.serial

    def report(self, rc):
        if rc == 0:
            self.log("✓ Selesai", "ok")
        else:
            self.log(f"✗ Gagal (kode {rc})", "err")

    def tool_task(self, tool, args, fallback=None):
        self.task(self._tool_cmd, tool, args, fallback)

    def _tool_cmd(self, tool, args, fallback=None):
        r = self.resolve(tool)
        if not r:
            return
        exe, serial = r
        rc, _ = self.run_stream([exe, "-s", serial] + args)
        if rc != 0 and fallback:
            self.log("Perintah utama gagal, mencoba alternatif…", "warn")
            rc, _ = self.run_stream([exe, "-s", serial] + fallback)
        self.report(rc)

    # ---------------- ADB ----------------
    def do_adb_info(self):
        r = self.resolve("adb")
        if not r:
            return
        exe, serial = r
        self.log("$ adb shell getprop  (ringkasan)", "cmd")
        rc, out = capture([exe, "-s", serial, "shell", "getprop"], timeout=20)
        if rc != 0:
            self.log(out.strip() or "Gagal membaca properti HP.", "err")
            self.report(rc)
            return
        p = parse_props(out)
        fields = [
            ("Brand", "ro.product.brand"), ("Model", "ro.product.model"),
            ("Codename", "ro.product.device"), ("Android", "ro.build.version.release"),
            ("SDK", "ro.build.version.sdk"), ("Build", "ro.build.display.id"),
            ("Security patch", "ro.build.version.security_patch"),
            ("CPU ABI", "ro.product.cpu.abi"), ("Platform", "ro.board.platform"),
            ("Bootloader", "ro.bootloader"), ("Slot", "ro.boot.slot_suffix"),
            ("Verified boot", "ro.boot.verifiedbootstate"),
            ("Flash locked", "ro.boot.flash.locked"), ("Serial", "ro.serialno"),
            ("Fingerprint", "ro.build.fingerprint"),
        ]
        for label, key in fields:
            if p.get(key):
                self.log(f"{label:<15}: {p[key]}", "info")
        rc2, bat = capture([exe, "-s", serial, "shell", "dumpsys", "battery"], timeout=10)
        if rc2 == 0:
            lvl = re.search(r"level:\s*(\d+)", bat)
            tmp = re.search(r"temperature:\s*(\d+)", bat)
            if lvl:
                extra = f"  ({int(tmp.group(1)) / 10:.1f}°C)" if tmp else ""
                self.log(f"{'Baterai':<15}: {lvl.group(1)}%{extra}", "info")
        self.report(0)

    def adb_sideload(self):
        path = filedialog.askopenfilename(title="Pilih file ZIP untuk sideload",
                                          filetypes=[("ZIP", "*.zip"), ("Semua file", "*.*")])
        if not path:
            return
        d = self.pick("adb")
        if d and d.state != "sideload":
            if not messagebox.askyesno(
                    "HP belum di mode sideload",
                    "Status HP saat ini bukan 'sideload'.\n\nDi recovery, pilih "
                    "'Apply update from ADB' dulu supaya sideload bisa berjalan.\n\nLanjutkan?"):
                return
        self.tool_task("adb", ["sideload", path])

    # ---------------- fastboot ----------------
    def do_fb_info(self):
        r = self.resolve("fastboot")
        if not r:
            return
        exe, serial = r
        rc, out = self.run_stream([exe, "-s", serial, "getvar", "all"])
        v = parse_getvar(out)
        self.log("Ringkasan fastboot:", "ok")
        fields = [("Product", "product"), ("Variant", "variant"), ("Serial no", "serialno"),
                  ("Slot aktif", "current-slot"), ("Jumlah slot", "slot-count"),
                  ("Unlocked", "unlocked"), ("Secure", "secure"),
                  ("Fastbootd", "is-userspace"), ("Versi bootloader", "version-bootloader"),
                  ("Versi baseband", "version-baseband"), ("Max download", "max-download-size"),
                  ("Voltase baterai", "battery-voltage")]
        for label, key in fields:
            if v.get(key):
                self.log(f"{label:<16}: {v[key]}", "info")
        self.report(rc)

    def fb_edl(self):
        if not messagebox.askyesno("Reboot ke EDL",
                                   "HP akan masuk mode EDL (layar mati, tidak ada tampilan).\n\n"
                                   "Lanjutkan?"):
            return
        self.tool_task("fastboot", ["reboot", "edl"], fallback=["oem", "edl"])

    def fb_flashing(self, action):
        # action: "unlock" atau "lock" (membuka / mengunci bootloader)
        if action == "unlock":
            msg = ("'fastboot flashing unlock' MEMBUKA bootloader dan akan MENGHAPUS SEMUA DATA "
                   "di HP. Di banyak perangkat Anda juga harus menekan tombol konfirmasi di layar "
                   "HP setelah ini.\n\nKetik UNLOCK untuk melanjutkan:")
            keyword = "UNLOCK"
        else:
            msg = ("'fastboot flashing lock' MENGUNCI bootloader dan akan MENGHAPUS SEMUA DATA. "
                   "Mengunci dengan boot/ROM yang tidak cocok bisa membuat HP tidak bisa booting "
                   "(brick). Pastikan perangkat dalam keadaan bootable penuh.\n\n"
                   "Ketik LOCK untuk melanjutkan:")
            keyword = "LOCK"
        ans = simpledialog.askstring(f"Konfirmasi {action}", msg, parent=self)
        if not ans or ans.strip().upper() != keyword:
            self.log(f"Flashing {action} dibatalkan.", "warn")
            return
        # 'flashing unlock/lock' adalah perintah baru; sebagian perangkat lama pakai 'oem unlock/lock'
        self.tool_task("fastboot", ["flashing", action], fallback=["oem", action])

    def fb_wipe(self):
        ans = simpledialog.askstring(
            "Konfirmasi wipe",
            "PERINGATAN: 'fastboot -w' menghapus SEMUA DATA di HP (userdata dan cache).\n"
            "Tindakan ini tidak bisa dibatalkan.\n\nKetik WIPE untuk melanjutkan:",
            parent=self)
        if ans and ans.strip().upper() == "WIPE":
            self.tool_task("fastboot", ["-w"])
        else:
            self.log("Wipe dibatalkan.", "warn")

    # ---------------- cek slot & flash partisi satuan ----------------
    def do_check_slot(self):
        sel = self.selected()
        if sel and sel.tool in ("adb", "fastboot"):
            tool = sel.tool
        else:
            tool = "fastboot" if self.pick("fastboot") else "adb"
        r = self.resolve(tool)
        if not r:
            return
        exe, serial = r
        if tool == "fastboot":
            rc, out = self.run_stream([exe, "-s", serial, "getvar", "current-slot"])
            m = re.search(r"current-slot:\s*(\S+)", out)
            slot = m.group(1) if m else ""
        else:
            self.log("$ adb shell getprop ro.boot.slot_suffix", "cmd")
            rc, out = capture([exe, "-s", serial, "shell", "getprop", "ro.boot.slot_suffix"], timeout=10)
            slot = out.strip() if rc == 0 else ""
        slot = slot.lstrip("_")
        if slot.lower() in ("a", "b"):
            self.log(f"Slot aktif: {slot.upper()}", "ok")
            self.ui(self.var_slot.set, slot.upper())
        else:
            self.log("Perangkat tidak melaporkan slot aktif (kemungkinan bukan A/B).", "warn")
            self.ui(self.var_slot.set, "-")

    def browse_image(self, var, name):
        path = filedialog.askopenfilename(
            title=f"Pilih file image untuk {name}",
            filetypes=[("Image", "*.img *.bin *.mbn"), ("Semua file", "*.*")])
        if path:
            var.set(path)

    def flash_part(self, part, var_path):
        if not part or not PART_NAME_RE.match(part):
            self.log("Nama partisi kosong atau tidak valid.", "warn")
            return
        path = var_path.get().strip()
        if not path:
            self.browse_image(var_path, part)
            path = var_path.get().strip()
            if not path:
                return
        if not Path(path).is_file():
            self.log(f"File tidak ditemukan: {path}", "err")
            return
        if not messagebox.askyesno("Konfirmasi flash",
                                   f"Flash partisi '{part}' dengan file:\n{path}\n\nLanjutkan?"):
            return
        self.tool_task("fastboot", ["flash", part, path])

    # ---------------- flasher scatter / rawprogram ----------------
    def load_flash_file(self, kind):
        if kind == "scatter":
            paths = [filedialog.askopenfilename(
                title="Pilih file scatter (MediaTek)",
                filetypes=[("Scatter / teks", "*.txt"), ("Semua file", "*.*")])]
        else:
            paths = list(filedialog.askopenfilenames(
                title="Pilih rawprogram XML (Qualcomm)",
                filetypes=[("XML", "*.xml"), ("Semua file", "*.*")]))
        paths = [p for p in paths if p]
        if not paths:
            return
        try:
            label, entries = parse_scatter(paths[0]) if kind == "scatter" else parse_rawprogram(paths)
        except Exception as exc:  # noqa: BLE001
            self.log(f"Gagal membaca file: {exc}", "err")
            return
        if not entries:
            self.log("Tidak ada partisi yang bisa di-flash di file itu. Pastikan ini scatter versi baru "
                     "(ada partition_name dan file_name) atau rawprogram*.xml, bukan patch*.xml.", "warn")
            return
        base = Path(paths[0]).parent
        for e in entries:
            e["path"] = resolve_image(base, e["file"]) if e.get("file") and not e.get("xml_only") else None
            e["size"] = os.path.getsize(e["path"]) if e["path"] else 0
            skip = default_skip(e)
            e["checked"] = bool(e["path"]) and not skip
            if e.get("xml_only"):
                e["status"] = "XML saja · tidak ada image"
            else:
                e["status"] = ("File tidak ada" if not e["path"]
                               else "Dilewati (default)" if skip else "Siap")
        self.fl_entries = entries
        missing = sum(1 for e in entries if not e["path"])
        self.var_flfile.set(f"{label} · {len(entries)} partisi · {Path(paths[0]).name}")
        self.log(f"Dimuat: {label} ({len(entries)} partisi, {missing} file tidak ditemukan).", "ok")
        if missing:
            self.log("Pastikan file image ada di folder yang sama dengan scatter/XML "
                     "(atau di subfolder 'images').", "warn")
        self.refresh_tree()

    def refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        for i, e in enumerate(self.fl_entries):
            tags = ("missing",) if not e["path"] and not e.get("xml_only") else (
                ("skipped",) if e["status"].startswith(("Dilewati", "XML saja")) else ())
            filename = e["file"] or "(tanpa image)"
            detail = e["status"]
            if e.get("start_sector"):
                detail += f" · LBA {e['start_sector']}"
            self.tree.insert("", "end", iid=str(i), tags=tags,
                             values=("☑" if e["checked"] else "☐", e["part"], filename,
                                     human_size(e["size"]), detail))
        self.update_flash_summary()

    def update_flash_summary(self):
        chosen = [e for e in self.fl_entries if e["checked"]]
        total = sum(e["size"] for e in chosen)
        text = f"{len(chosen)} dari {len(self.fl_entries)} dipilih"
        if total:
            text += f" · {human_size(total)}"
        self.var_flsummary.set(text)

    def toggle_entry(self, i):
        e = self.fl_entries[i]
        if self.is_busy or not e["path"]:
            return
        e["checked"] = not e["checked"]
        self.tree.set(str(i), "sel", "☑" if e["checked"] else "☐")
        self.update_flash_summary()

    def on_tree_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) != "#1":
            return
        iid = self.tree.identify_row(event.y)
        if iid:
            self.toggle_entry(int(iid))

    def on_tree_space(self, _event):
        for iid in self.tree.selection():
            self.toggle_entry(int(iid))
        return "break"

    def set_all_checked(self, mode):
        if self.is_busy:
            return
        for e in self.fl_entries:
            if mode == "all":
                e["checked"] = bool(e["path"])
            elif mode == "none":
                e["checked"] = False
            else:
                e["checked"] = bool(e["path"]) and not default_skip(e)
        self.refresh_tree()

    def flash_all(self):
        chosen = [e for e in self.fl_entries if e["checked"] and e["path"]]
        if not chosen:
            self.log("Belum ada partisi yang dipilih. Muat scatter atau rawprogram XML dulu.", "warn")
            return
        if not self.resolve("fastboot"):
            return
        dev = self.pick("fastboot")
        info = self.details.get(dev.key) or {}
        wipe, reboot, keep_going = self.var_wipe.get(), self.var_reboot.get(), self.var_keepgoing.get()
        lines = [f"Perangkat : {dev.serial}",
                 f"Partisi   : {len(chosen)} dipilih",
                 "Relock bootloader : TIDAK (flash all only)",
                 "Hapus data        : " + ("YA, semua data pengguna akan hilang" if wipe else "tidak"),
                 "Reboot di akhir   : " + ("ya" if reboot else "tidak")]
        if (info.get("unlocked") or "").lower() == "no":
            lines.append("")
            lines.append("PERHATIAN: bootloader terkunci, flash kemungkinan ditolak.")
        if not messagebox.askyesno("Konfirmasi flash all", "\n".join(lines) + "\n\nLanjutkan?"):
            return
        self.cancel_flag.clear()
        self.task(self.do_flash_all, chosen, wipe, reboot, keep_going)

    def do_flash_all(self, entries, wipe, reboot, keep_going):
        r = self.resolve("fastboot")
        if not r:
            return
        exe, serial = r
        steps = len(entries) + (2 if wipe else 0) + (1 if reboot else 0)
        done, failed, aborted = 0, [], False
        self.log(f"=== Flash all dimulai: {len(entries)} partisi (bootloader tidak di-relock) ===", "ok")
        self.ui(self.set_flash_ui, True)
        try:
            for i, e in enumerate(entries, 1):
                if self.cancel_flag.is_set():
                    self.log("Dibatalkan oleh pengguna.", "warn")
                    aborted = True
                    break
                self.ui(self.set_progress, done, steps, f"[{i}/{len(entries)}] {e['part']}")
                self.log(f"[{i}/{len(entries)}] {e['part']}  <-  {e['file']}", "info")
                rc, _ = self.run_stream([exe, "-s", serial, "flash", e["part"], e["path"]])
                done += 1
                if rc != 0:
                    failed.append(e["part"])
                    self.log(f"✗ {e['part']} gagal (kode {rc})", "err")
                    if not keep_going:
                        self.log("Flash dihentikan karena ada partisi yang gagal.", "err")
                        aborted = True
                        break
            if not aborted and wipe:
                for part in ("userdata", "metadata"):
                    self.ui(self.set_progress, done, steps, f"Menghapus {part}")
                    rc, _ = self.run_stream([exe, "-s", serial, "erase", part])
                    done += 1
                    if rc != 0:
                        self.log(f"Catatan: erase {part} gagal (kode {rc}); partisinya mungkin tidak ada.", "warn")
            if not aborted and reboot:
                if failed:
                    self.log("Tidak reboot otomatis karena ada partisi yang gagal.", "warn")
                else:
                    self.ui(self.set_progress, done, steps, "Reboot")
                    self.run_stream([exe, "-s", serial, "reboot"])
                    done += 1
        finally:
            self.ui(self.set_flash_ui, False)
        if failed:
            self.log(f"Selesai dengan {len(failed)} kegagalan: {', '.join(failed)}", "err")
            text = "Selesai dengan kegagalan"
        elif aborted:
            self.log("Flash all tidak selesai.", "warn")
            text = "Tidak selesai"
        else:
            self.log("✓ Flash all selesai. Bootloader tidak di-relock.", "ok")
            text = "Selesai · bootloader tidak di-relock"
        self.ui(self.set_progress, steps if not (failed or aborted) else done, steps, text)

    def cancel_flash(self):
        self.cancel_flag.set()
        self.log("Pembatalan diminta: berhenti setelah partisi yang sedang berjalan selesai.", "warn")

    def set_flash_ui(self, running):
        self.btn_cancel.state(["!disabled"] if running else ["disabled"])

    def set_progress(self, done, total, text):
        self.pb_indeterminate(False)
        self.pb["maximum"] = max(total, 1)
        self.pb["value"] = done
        self.var_prog.set(text)

    # ---------------- scrcpy ----------------
    def start_scrcpy(self):
        if self.scrcpy_proc and self.scrcpy_proc.poll() is None:
            self.log("scrcpy sudah berjalan.", "warn")
            return
        if not self.scrcpy:
            self.log("scrcpy.exe tidak ditemukan. Taruh folder 'scrcpy' di sebelah program ini "
                     "atau klik 'Folder tools…'.", "err")
            return
        dev = self.pick("adb")
        if not dev or dev.state != "device":
            self.log("Mirroring butuh HP yang menyala dengan USB debugging aktif "
                     "(status: ADB · Android nyala).", "warn")
            return
        cmd = [self.scrcpy, "-s", dev.serial, "-b", self.var_bitrate.get()]
        if self.var_size.get() != "Original":
            cmd += ["-m", self.var_size.get()]
        if self.var_awake.get():
            cmd.append("--stay-awake")
        if self.var_screenoff.get():
            cmd.append("--turn-screen-off")
        if self.var_ontop.get():
            cmd.append("--always-on-top")
        if self.var_touches.get():
            cmd.append("--show-touches")
        if self.var_noaudio.get():
            cmd.append("--no-audio")
        env = os.environ.copy()
        if self.adb:
            env["ADB"] = self.adb  # pakai adb yang sama supaya server tidak bentrok
        self.log("$ " + subprocess.list2cmdline(cmd), "cmd")
        try:
            self.scrcpy_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", env=env,
                cwd=str(Path(self.scrcpy).parent), creationflags=NO_WINDOW)
        except OSError as exc:
            self.log(f"Gagal menjalankan scrcpy: {exc}", "err")
            return
        self.set_scrcpy_ui(True)
        threading.Thread(target=self.pump_scrcpy, args=(self.scrcpy_proc,), daemon=True).start()

    def pump_scrcpy(self, proc):
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                self.log("[scrcpy] " + line, "info")
        rc = proc.wait()
        self.log("scrcpy berhenti." if rc == 0 else f"scrcpy berhenti (kode {rc}).",
                 "info" if rc == 0 else "warn")
        self.ui(self.set_scrcpy_ui, False)

    def stop_scrcpy(self):
        if self.scrcpy_proc and self.scrcpy_proc.poll() is None:
            self.scrcpy_proc.terminate()

    def set_scrcpy_ui(self, running):
        self.btn_start.state(["disabled"] if running else ["!disabled"])
        self.btn_stop.state(["!disabled"] if running else ["disabled"])

    def do_user_logout(self):
        if messagebox.askyesno("Logout", "Apakah Anda yakin ingin keluar dari akun ini?"):
            if getattr(self, "supabase", None):
                self.supabase.logout()
            self.on_close()
            try:
                import droidfix_supabase
                droidfix_supabase.start_with_auth(lambda client: App(supabase=client).mainloop())
            except Exception:
                pass

    # ---------------- keluar ----------------
    def on_close(self):
        self.closing = True
        self.stop_scrcpy()
        self.destroy()


def main():
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # teks tajam di layar HiDPI
        except Exception:  # noqa: BLE001
            pass
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DroidFix.FlashTool")
        except Exception:  # noqa: BLE001
            pass
    try:
        import droidfix_supabase
        droidfix_supabase.start_with_auth(lambda client: App(supabase=client).mainloop())
    except Exception:
        App().mainloop()


if __name__ == "__main__":
    main()
