#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DroidFix Flash Tool - Supabase Integration Module
Pure Python standard library (no pip install required).
"""
import hashlib
import json
import os
import platform
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

SUPABASE_URL = os.environ.get(
    "SUPABASE_URL", "https://sxdhwmrtzuzuzdcjxsso.supabase.co"
).rstrip("/")
SUPABASE_KEY = os.environ.get(
    "SUPABASE_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InN4ZGh3bXJ0enV6dXpkY2p4c3NvIiwicm9sZSI6ImFub24iLCJpYXQiOjE3OTQ0MDk4MDksImV4cCI6MjEwOTk4NTgwOX0."
    "nezJvcqLrbw7N3Q9Cvm-qegMeGZZBMqaZISv-4RDimI",
)

SESSION_FILE = Path.home() / ".droidfix_session.json"

BG, LOGBG = "#161a22", "#0e1117"
FG, MUTED = "#e6e9ef", "#8a93a6"
BORDER, BTN, BTN_H = "#2a3140", "#252c3b", "#303a50"
ACCENT, GREEN, AMBER, RED = "#2dd4bf", "#34d399", "#f5a524", "#f0506e"


def get_hwid():
    """Dapatkan HWID unik mesin Windows / Linux."""
    try:
        if os.name == "nt":
            # Coba ambil UUID motherboard via PowerShell
            cmd = ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_ComputerSystemProduct).UUID"]
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            hwid = p.stdout.strip()
            if hwid and len(hwid) > 10 and "error" not in hwid.lower():
                return hwid
    except Exception:
        pass
    # Fallback berbasis node & platform
    raw = f"{platform.node()}:{platform.machine()}:{platform.processor()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32].upper()


class SupabaseClient:
    def __init__(self, url=SUPABASE_URL, key=SUPABASE_KEY):
        self.url = url
        self.key = key
        self.session = self.load_session()
        self.hwid = get_hwid()

    def _headers(self, token=None):
        h = {
            "apikey": self.key,
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        tok = token or (self.session.get("access_token") if self.session else None)
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        else:
            h["Authorization"] = f"Bearer {self.key}"
        return h

    def _request(self, endpoint, method="GET", data=None, token=None):
        url = f"{self.url}{endpoint}"
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(url, data=body, headers=self._headers(token), method=method)
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8")
            try:
                err_json = json.loads(raw)
            except Exception:
                err_json = {"message": raw or str(e)}
            return e.code, err_json
        except Exception as e:
            return -1, {"message": str(e)}

    # ---- Autentikasi ----
    def login(self, email, password):
        endpoint = "/auth/v1/token?grant_type=password"
        payload = {"email": email.strip(), "password": password}
        status, res = self._request(endpoint, method="POST", data=payload)
        if status == 200:
            self.session = res
            self.save_session(res)
            # Sync client PC
            threading.Thread(target=self.sync_client, daemon=True).start()
            return True, res
        msg = res.get("msg") or res.get("message") or res.get("error_description") or "Login gagal"
        if "email_not_confirmed" in str(res):
            msg = "Email belum dikonfirmasi di Supabase! Harap verifikasi email Anda atau nonaktifkan 'Confirm email' di Supabase."
        elif "invalid_credentials" in str(res):
            msg = "Email atau password salah!"
        return False, msg

    def signup(self, email, password, full_name="", username=""):
        endpoint = "/auth/v1/signup"
        payload = {
            "email": email.strip(),
            "password": password,
            "data": {
                "full_name": full_name or email.split("@")[0],
                "username": username or email.split("@")[0],
            },
        }
        status, res = self._request(endpoint, method="POST", data=payload)
        if status in (200, 201):
            if res.get("access_token"):
                self.session = res
                self.save_session(res)
                return True, "Pendaftaran berhasil & langsung login!"
            return True, "Pendaftaran berhasil! Cek email untuk konfirmasi akun (atau login jika konfirmasi dinonaktifkan)."
        msg = res.get("msg") or res.get("message") or "Pendaftaran gagal"
        if "weak_password" in str(res):
            msg = "Password terlalu pendek (minimal 6 karakter)!"
        elif "already registered" in str(res):
            msg = "Email ini sudah terdaftar. Silakan langsung login."
        return False, msg

    def logout(self):
        self.session = None
        if SESSION_FILE.exists():
            try:
                SESSION_FILE.unlink()
            except Exception:
                pass

    def save_session(self, session_data):
        try:
            with open(SESSION_FILE, "w", encoding="utf-8") as f:
                json.dump(session_data, f, indent=2)
        except Exception:
            pass

    def load_session(self):
        if not SESSION_FILE.exists():
            return None
        try:
            with open(SESSION_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("access_token") and data.get("user"):
                    return data
        except Exception:
            pass
        return None

    def get_user_id(self):
        if self.session and self.session.get("user"):
            return self.session["user"].get("id")
        return None

    def get_user_email(self):
        if self.session and self.session.get("user"):
            return self.session["user"].get("email")
        return ""

    # ---- Sync PC & Log ----
    def sync_client(self, app_version="1.0.0"):
        user_id = self.get_user_id()
        if not user_id:
            return
        payload = {
            "hwid": self.hwid,
            "id_user": user_id,
            "computer_name": platform.node(),
            "os_version": f"{platform.system()} {platform.release()}",
            "app_version": app_version,
            "is_active": True,
            "last_seen_at": datetime.utcnow().isoformat() + "Z",
        }
        # Upsert client ke table app_clients
        endpoint = "/rest/v1/app_clients?on_conflict=hwid"
        self._request(endpoint, method="POST", data=payload)

    def log_flash(self, tool_type, mode, command, status="SUCCESS", duration=0, target_part="", fw_name="", err="", log_text=""):
        """Kirim log eksekusi flash ke Supabase secara background."""
        def _worker():
            user_id = self.get_user_id()
            payload = {
                "tool_type": str(tool_type),
                "mode": str(mode),
                "command_executed": str(command)[:500],
                "target_partition": str(target_part) if target_part else None,
                "firmware_name": str(fw_name) if fw_name else None,
                "status": status,
                "duration_seconds": int(duration),
                "error_message": str(err)[:500] if err else None,
                "log_output": str(log_text)[:3000] if log_text else None,
            }
            if user_id:
                payload["id_user"] = user_id
            endpoint = "/rest/v1/flash_logs"
            self._request(endpoint, method="POST", data=payload)

        threading.Thread(target=_worker, daemon=True).start()

    # ---- Katalog Xiaomi (Cloud Sync) ----
    def get_xiaomi_models(self):
        """Ambil katalog model Xiaomi dari Supabase, dengan fallback offline."""
        try:
            status, res = self._request("/rest/v1/xiaomi_models?select=*&order=series,model_name")
            if status == 200 and isinstance(res, list) and len(res) > 0:
                return res
        except Exception:
            pass
        return DEFAULT_XIAOMI_MODELS

    def get_xiaomi_tools(self):
        """Ambil daftar aksi tool Xiaomi dari Supabase."""
        try:
            status, res = self._request("/rest/v1/xiaomi_tools?select=*&order=sort_order")
            if status == 200 and isinstance(res, list) and len(res) > 0:
                return res
        except Exception:
            pass
        return [
            {"id": "ubl", "tool_name": "Unlock Bootloader (UBL Instan)", "operation_type": "ubl", "command_args": "da seccfg unlock", "description": "Buka UBL tanpa nunggu 7 hari"},
            {"id": "relock", "tool_name": "Relock Bootloader (Kunci UBL)", "operation_type": "relock", "command_args": "da seccfg lock", "description": "Kunci kembali bootloader Xiaomi MTK"},
            {"id": "bypass", "tool_name": "Bypass Auth (BROM Exploit)", "operation_type": "bypass", "command_args": "payload", "description": "Bypass SLA & DAA auth proteksi MediaTek"},
        ]


DEFAULT_XIAOMI_MODELS = [
    # Redmi Note Series
    {"series": "Redmi Note Series", "model_name": "Redmi Note 3", "chipset": "Helio X10"},
    {"series": "Redmi Note Series", "model_name": "Redmi Note 4", "chipset": "Helio X20"},
    {"series": "Redmi Note Series", "model_name": "Redmi Note 8 Pro", "chipset": "Helio G90T"},
    {"series": "Redmi Note Series", "model_name": "Redmi Note 8 2021", "chipset": "Helio G85"},
    {"series": "Redmi Note Series", "model_name": "Redmi Note 9 | 10X 4G", "chipset": "Helio G85"},
    {"series": "Redmi Note Series", "model_name": "Redmi Note 9 5G", "chipset": "Dimensity 800U"},
    {"series": "Redmi Note Series", "model_name": "Redmi Note 10T 5G", "chipset": "Dimensity 700"},
    {"series": "Redmi Note Series", "model_name": "Redmi Note 10 5G", "chipset": "Dimensity 700"},
    {"series": "Redmi Note Series", "model_name": "Redmi Note 10 Pro 5G", "chipset": "Dimensity 1100"},
    {"series": "Redmi Note Series", "model_name": "Redmi Note 10S 4G", "chipset": "Helio G95"},
    # Redmi Series
    {"series": "Redmi Series", "model_name": "Mi Play", "chipset": "Helio P35"},
    {"series": "Redmi Series", "model_name": "Redmi 6A", "chipset": "Helio A22"},
    {"series": "Redmi Series", "model_name": "Redmi 6", "chipset": "Helio P22"},
    {"series": "Redmi Series", "model_name": "Redmi 9", "chipset": "Helio G80"},
    {"series": "Redmi Series", "model_name": "Redmi 9 Prime", "chipset": "Helio G80"},
    {"series": "Redmi Series", "model_name": "Redmi 9A / 9T / 9i", "chipset": "Helio G25"},
    {"series": "Redmi Series", "model_name": "Redmi 9C NFC", "chipset": "Helio G35"},
    {"series": "Redmi Series", "model_name": "Redmi 9C | Poco C3", "chipset": "Helio G35"},
    {"series": "Redmi Series", "model_name": "Redmi 10 | 10 Prime", "chipset": "Helio G88"},
    {"series": "Redmi Series", "model_name": "Redmi 10X 5G", "chipset": "Dimensity 820"},
    {"series": "Redmi Series", "model_name": "Redmi 10X Pro 5G", "chipset": "Dimensity 820"},
    # Mi & Poco Series
    {"series": "Mi & Poco Series", "model_name": "Xiaomi 11T", "chipset": "Dimensity 1200"},
    {"series": "Mi & Poco Series", "model_name": "Redmi K30 Ultra", "chipset": "Dimensity 1000+"},
    {"series": "Mi & Poco Series", "model_name": "Redmi K40 Gaming", "chipset": "Dimensity 1200"},
    {"series": "Mi & Poco Series", "model_name": "Poco M2", "chipset": "Helio G80"},
    {"series": "Mi & Poco Series", "model_name": "Poco M3 Pro 5G", "chipset": "Dimensity 700"},
    {"series": "Mi & Poco Series", "model_name": "Poco F3 GT Gaming", "chipset": "Dimensity 1200"},
    {"series": "Mi & Poco Series", "model_name": "Poco X3 GT", "chipset": "Dimensity 1100"},
    {"series": "Mi & Poco Series", "model_name": "Poco C31", "chipset": "Helio G35"},
]


# ==============================================================================
# GUI DIALOG LOGIN DROIDFIX
# ==============================================================================
class LoginDialog:
    def __init__(self, client: SupabaseClient, on_success):
        self.client = client
        self.on_success = on_success

        self.root = tk.Tk()
        self.root.title("DroidFix - Login Teknisi")
        self.root.geometry("420x460")
        self.root.resizable(False, False)
        self.root.configure(bg=BG)

        # Letakkan di tengah layar
        self.root.update_idletasks()
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        x = (self.root.winfo_screenwidth() // 2) - (w // 2)
        y = (self.root.winfo_screenheight() // 2) - (h // 2)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        self._setup_ui()

    def _setup_ui(self):
        pad = {"padx": 24, "pady": 6}

        # Header Title
        lbl_head = tk.Label(self.root, text="DROIDFIX FLASH TOOL", font=("Segoe UI", 16, "bold"), fg=ACCENT, bg=BG)
        lbl_head.pack(pady=(25, 4))
        lbl_sub = tk.Label(self.root, text="Koneksi Cloud Supabase Aktif", font=("Segoe UI", 9), fg=MUTED, bg=BG)
        lbl_sub.pack(pady=(0, 20))

        # Email
        lbl_email = tk.Label(self.root, text="Email / Akun Teknisi:", font=("Segoe UI", 9, "bold"), fg=FG, bg=BG, anchor="w")
        lbl_email.pack(fill="x", **pad)
        self.ent_email = tk.Entry(self.root, font=("Segoe UI", 10), bg=BTN, fg=FG, insertbackground=FG, relief="flat", highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT)
        self.ent_email.pack(fill="x", ipady=5, **pad)
        self.ent_email.insert(0, "chairilmaulan06@gmail.com")

        # Password
        lbl_pass = tk.Label(self.root, text="Password (minimal 6 karakter):", font=("Segoe UI", 9, "bold"), fg=FG, bg=BG, anchor="w")
        lbl_pass.pack(fill="x", **pad)
        self.ent_pass = tk.Entry(self.root, show="*", font=("Segoe UI", 10), bg=BTN, fg=FG, insertbackground=FG, relief="flat", highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT)
        self.ent_pass.pack(fill="x", ipady=5, **pad)
        self.ent_pass.insert(0, "123456")

        # Status / Error Label
        self.lbl_msg = tk.Label(self.root, text="", font=("Segoe UI", 8), fg=AMBER, bg=BG, wraplength=370)
        self.lbl_msg.pack(pady=(6, 2))

        # Tombol Login & Register
        btn_frame = tk.Frame(self.root, bg=BG)
        btn_frame.pack(fill="x", pady=15, padx=24)

        self.btn_login = tk.Button(btn_frame, text="Masuk (Login)", font=("Segoe UI", 10, "bold"), bg=ACCENT, fg="#0b0f19", activebackground="#26b3a2", relief="flat", cursor="hand2", command=self._do_login)
        self.btn_login.pack(fill="x", ipady=6, pady=(0, 8))

        self.btn_reg = tk.Button(btn_frame, text="Daftar Akun Baru (Register)", font=("Segoe UI", 9), bg=BTN, fg=FG, activebackground=BTN_H, relief="flat", cursor="hand2", command=self._do_signup)
        self.btn_reg.pack(fill="x", ipady=4)

        # Footer HWID
        lbl_hwid = tk.Label(self.root, text=f"HWID: {self.client.hwid[:16]}...", font=("Consolas", 8), fg=MUTED, bg=BG)
        lbl_hwid.pack(side="bottom", pady=8)

        # Enter key triggers login
        self.root.bind("<Return>", lambda e: self._do_login())

    def _do_login(self):
        email = self.ent_email.get().strip()
        pwd = self.ent_pass.get()
        if not email or not pwd:
            self.lbl_msg.config(text="Email dan password wajib diisi!", fg=RED)
            return

        self.btn_login.config(state="disabled", text="Memverifikasi...")
        self.lbl_msg.config(text="Menghubungi Supabase...", fg=ACCENT)
        self.root.update_idletasks()

        def _task():
            ok, res = self.client.login(email, pwd)
            self.root.after(0, lambda: self._on_login_finish(ok, res))

        threading.Thread(target=_task, daemon=True).start()

    def _on_login_finish(self, ok, res):
        self.btn_login.config(state="normal", text="Masuk (Login)")
        if ok:
            self.lbl_msg.config(text="Login Berhasil!", fg=GREEN)
            self.root.after(400, self._proceed)
        else:
            self.lbl_msg.config(text=f"Error: {res}", fg=RED)

    def _do_signup(self):
        email = self.ent_email.get().strip()
        pwd = self.ent_pass.get()
        if not email or len(pwd) < 6:
            self.lbl_msg.config(text="Password minimal 6 karakter!", fg=RED)
            return

        self.btn_reg.config(state="disabled", text="Mendaftar...")
        self.lbl_msg.config(text="Mendaftarkan akun ke Supabase...", fg=ACCENT)
        self.root.update_idletasks()

        def _task():
            ok, res = self.client.signup(email, pwd)
            self.root.after(0, lambda: self._on_signup_finish(ok, res))

        threading.Thread(target=_task, daemon=True).start()

    def _on_signup_finish(self, ok, res):
        self.btn_reg.config(state="normal", text="Daftar Akun Baru (Register)")
        if ok:
            messagebox.showinfo("Supabase Auth", res, parent=self.root)
            self.lbl_msg.config(text=res, fg=GREEN)
        else:
            self.lbl_msg.config(text=f"Gagal: {res}", fg=RED)

    def _proceed(self):
        self.root.destroy()
        self.on_success()

    def run(self):
        self.root.mainloop()


def start_with_auth(main_app_starter):
    """Jalankan flow auth sebelum membuka aplikasi utama."""
    client = SupabaseClient()
    # Jika sudah punya sesi tersimpan dan masih valid
    if client.session and client.get_user_id():
        # Validasi ringan di background
        threading.Thread(target=client.sync_client, daemon=True).start()
        main_app_starter(client)
    else:
        dlg = LoginDialog(client, on_success=lambda: main_app_starter(client))
        dlg.run()
