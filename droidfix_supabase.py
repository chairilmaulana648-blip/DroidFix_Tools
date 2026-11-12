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

    # ---- Token Aktivasi Admin ----
    def verify_token(self, token_code):
        """Periksa apakah token dari admin valid dan belum dipakai."""
        token_code = token_code.strip()
        if not token_code:
            return False, "Token aktivasi wajib diisi!"

        url_enc = urllib.parse.quote(token_code)
        status, res = self._request(f"/rest/v1/activation_tokens?token_code=eq.{url_enc}&select=*")
        if status == 200 and isinstance(res, list):
            if len(res) == 0:
                return False, "Token aktivasi tidak terdaftar! Minta token ke Admin."
            tok = res[0]
            if tok.get("is_used"):
                return False, f"Token ini sudah digunakan pada {str(tok.get('used_at', ''))[:10]} oleh {tok.get('used_by', 'pengguna lain')}."
            return True, "Token valid!"
        return False, "Gagal memverifikasi token ke database Supabase."

    def consume_token(self, token_code, email):
        """Tandai token sebagai sudah terpakai."""
        try:
            url_enc = urllib.parse.quote(token_code.strip())
            payload = {
                "is_used": True,
                "used_by": email,
                "used_at": datetime.utcnow().isoformat() + "Z",
            }
            self._request(f"/rest/v1/activation_tokens?token_code=eq.{url_enc}", method="PATCH", data=payload)
        except Exception:
            pass

    # ---- Autentikasi ----
    def login(self, email, password):
        email = email.strip()
        endpoint = "/auth/v1/token?grant_type=password"
        payload = {"email": email, "password": password}
        status, res = self._request(endpoint, method="POST", data=payload)
        if status == 200:
            user_id = res.get("user", {}).get("id")
            # Cek status lisensi jika bukan akun admin
            if email.lower() != "danu4830@gmail.com" and user_id:
                prof_st, prof_res = self._request(f"/rest/v1/users_profile?id_user=eq.{user_id}&select=*")
                if prof_st == 200 and isinstance(prof_res, list) and len(prof_res) > 0:
                    sub_st = prof_res[0].get("subscription_status", "active")
                    if sub_st in ("banned", "suspended"):
                        return False, "Akun Anda telah DIBLOKIR oleh Admin! Hubungi Admin untuk aktivasi."
                    elif sub_st in ("pending", "expired"):
                        return False, "Akun Anda belum aktif/masa aktif habis. Hubungi Admin untuk token aktivasi."

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

    def signup(self, email, password, token_code="", full_name="", username=""):
        email = email.strip()
        is_admin = (email.lower() == "danu4830@gmail.com")

        # Jika bukan akun admin, token aktivasi WAJIB diverifikasi
        if not is_admin:
            if not token_code.strip():
                return False, "Pendaftaran akun baru wajib menggunakan Token Aktivasi dari Admin!"
            ok, msg = self.verify_token(token_code.strip())
            if not ok:
                return False, msg

        endpoint = "/auth/v1/signup"
        payload = {
            "email": email,
            "password": password,
            "data": {
                "full_name": full_name or email.split("@")[0],
                "username": username or email.split("@")[0],
            },
        }
        status, res = self._request(endpoint, method="POST", data=payload)
        if status in (200, 201):
            # Hanguskan token setelah pendaftaran berhasil
            if not is_admin and token_code.strip():
                self.consume_token(token_code.strip(), email)

            if res.get("access_token"):
                self.session = res
                self.save_session(res)
                threading.Thread(target=self.sync_client, daemon=True).start()
                return True, "Aktivasi berhasil! Akun Anda aktif dan langsung login."
            return True, "Pendaftaran berhasil! Akun Anda aktif dan siap digunakan."
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
        email = self.get_user_email()
        if not user_id:
            return

        # 1. Pastikan profil user tersimpan di users_profile
        try:
            uname = email.split("@")[0] if email else "technician"
            profile_payload = {
                "id_user": user_id,
                "username": uname,
                "full_name": uname.capitalize(),
                "role": "technician",
                "subscription_status": "active",
            }
            self._request("/rest/v1/users_profile?on_conflict=id_user", method="POST", data=profile_payload)
        except Exception:
            pass

        # 2. Upsert client ke table app_clients
        payload = {
            "hwid": self.hwid,
            "id_user": user_id,
            "computer_name": platform.node(),
            "os_version": f"{platform.system()} {platform.release()}",
            "app_version": app_version,
            "is_active": True,
            "last_seen_at": datetime.utcnow().isoformat() + "Z",
        }
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
# GUI DIALOG REGISTRASI & AKTIVASI TOKEN ADMIN
# ==============================================================================
class RegisterDialog:
    def __init__(self, parent, client: SupabaseClient, on_success_callback):
        self.client = client
        self.on_success_callback = on_success_callback

        self.win = tk.Toplevel(parent)
        self.win.title("DroidFix - Aktivasi Akun Baru")
        self.win.geometry("440x510")
        self.win.resizable(False, False)
        self.win.configure(bg=BG)
        self.win.transient(parent)
        self.win.grab_set()

        # Posisi di tengah parent
        self.win.update_idletasks()
        w, h = 440, 510
        x = parent.winfo_x() + (parent.winfo_width() // 2) - (w // 2)
        y = parent.winfo_y() + (parent.winfo_height() // 2) - (h // 2)
        self.win.geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")

        self._setup_ui()

    def _setup_ui(self):
        pad = {"padx": 24, "pady": 4}

        # Header
        lbl_head = tk.Label(self.win, text="REGISTRASI & AKTIVASI LISENSI", font=("Segoe UI", 13, "bold"), fg=ACCENT, bg=BG)
        lbl_head.pack(pady=(20, 2))
        lbl_sub = tk.Label(self.win, text="Wajib Memasukkan Token Verifikasi dari Admin", font=("Segoe UI", 8), fg=MUTED, bg=BG)
        lbl_sub.pack(pady=(0, 14))

        # Email
        tk.Label(self.win, text="Email Teknisi:", font=("Segoe UI", 9, "bold"), fg=FG, bg=BG, anchor="w").pack(fill="x", **pad)
        self.ent_email = tk.Entry(self.win, font=("Segoe UI", 10), bg=BTN, fg=FG, insertbackground=FG, relief="flat", highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT)
        self.ent_email.pack(fill="x", ipady=5, **pad)

        # Password
        tk.Label(self.win, text="Password (minimal 6 karakter):", font=("Segoe UI", 9, "bold"), fg=FG, bg=BG, anchor="w").pack(fill="x", **pad)
        self.ent_pass = tk.Entry(self.win, show="*", font=("Segoe UI", 10), bg=BTN, fg=FG, insertbackground=FG, relief="flat", highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT)
        self.ent_pass.pack(fill="x", ipady=5, **pad)

        # Token Aktivasi Admin
        tk.Label(self.win, text="Token Aktivasi Lisensi (dari Admin):", font=("Segoe UI", 9, "bold"), fg=AMBER, bg=BG, anchor="w").pack(fill="x", **pad)
        self.ent_token = tk.Entry(self.win, font=("Consolas", 11, "bold"), bg=BTN, fg=AMBER, insertbackground=FG, relief="flat", highlightthickness=1, highlightbackground=AMBER, highlightcolor=GREEN)
        self.ent_token.pack(fill="x", ipady=6, **pad)

        # Help info
        info_frame = tk.Frame(self.win, bg="#1a202c", padx=10, pady=8, highlightthickness=1, highlightbackground=BORDER)
        info_frame.pack(fill="x", padx=24, pady=(8, 4))
        tk.Label(info_frame, text="ℹ️ Belum memiliki token aktivasi?\nSilakan hubungi Admin DanzDroid untuk mendapatkan token lisensi sebelum mendaftar.",
                 font=("Segoe UI", 8), fg=MUTED, bg="#1a202c", justify="left").pack(anchor="w")

        # Status Label
        self.lbl_msg = tk.Label(self.win, text="", font=("Segoe UI", 8), fg=AMBER, bg=BG, wraplength=380)
        self.lbl_msg.pack(pady=(6, 2))

        # Buttons
        btn_frame = tk.Frame(self.win, bg=BG)
        btn_frame.pack(fill="x", pady=12, padx=24)

        self.btn_submit = tk.Button(btn_frame, text="Verifikasi Token & Aktifkan Akun", font=("Segoe UI", 10, "bold"),
                                    bg=ACCENT, fg="#0b0f19", activebackground="#26b3a2", relief="flat", cursor="hand2",
                                    command=self._do_register)
        self.btn_submit.pack(fill="x", ipady=6, pady=(0, 6))

        tk.Button(btn_frame, text="Batal / Kembali ke Login", font=("Segoe UI", 9), bg=BTN, fg=FG,
                  activebackground=BTN_H, relief="flat", cursor="hand2", command=self.win.destroy).pack(fill="x", ipady=4)

    def _do_register(self):
        email = self.ent_email.get().strip()
        pwd = self.ent_pass.get()
        token = self.ent_token.get().strip()

        if not email or len(pwd) < 6:
            self.lbl_msg.config(text="Email valid dan password minimal 6 karakter wajib diisi!", fg=RED)
            return
        if not token and email.lower() != "danu4830@gmail.com":
            self.lbl_msg.config(text="Token Aktivasi dari Admin wajib diisi!", fg=RED)
            return

        self.btn_submit.config(state="disabled", text="Memverifikasi ke Admin...")
        self.lbl_msg.config(text="Memeriksa token ke Supabase...", fg=ACCENT)
        self.win.update_idletasks()

        def _task():
            ok, res = self.client.signup(email, pwd, token_code=token)
            self.win.after(0, lambda: self._on_finish(ok, res, email))

        threading.Thread(target=_task, daemon=True).start()

    def _on_finish(self, ok, res, email):
        self.btn_submit.config(state="normal", text="Verifikasi Token & Aktifkan Akun")
        if ok:
            messagebox.showinfo("Aktivasi Sukses", f"{res}\n\nSilakan langsung login menggunakan email dan password Anda.", parent=self.win)
            if self.on_success_callback:
                self.on_success_callback(email)
            self.win.destroy()
        else:
            self.lbl_msg.config(text=f"Aktivasi Gagal: {res}", fg=RED)


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
        self.ent_email.insert(0, "")

        # Password
        lbl_pass = tk.Label(self.root, text="Password (minimal 6 karakter):", font=("Segoe UI", 9, "bold"), fg=FG, bg=BG, anchor="w")
        lbl_pass.pack(fill="x", **pad)
        self.ent_pass = tk.Entry(self.root, show="*", font=("Segoe UI", 10), bg=BTN, fg=FG, insertbackground=FG, relief="flat", highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT)
        self.ent_pass.pack(fill="x", ipady=5, **pad)
        self.ent_pass.insert(0, "")

        # Status / Error Label
        self.lbl_msg = tk.Label(self.root, text="", font=("Segoe UI", 8), fg=AMBER, bg=BG, wraplength=370)
        self.lbl_msg.pack(pady=(6, 2))

        # Tombol Login & Register
        btn_frame = tk.Frame(self.root, bg=BG)
        btn_frame.pack(fill="x", pady=15, padx=24)

        self.btn_login = tk.Button(btn_frame, text="Masuk (Login)", font=("Segoe UI", 10, "bold"), bg=ACCENT, fg="#0b0f19", activebackground="#26b3a2", relief="flat", cursor="hand2", command=self._do_login)
        self.btn_login.pack(fill="x", ipady=6, pady=(0, 8))

        self.btn_reg = tk.Button(btn_frame, text="Daftar Akun Baru (Minta Token Admin)", font=("Segoe UI", 9), bg=BTN, fg=FG, activebackground=BTN_H, relief="flat", cursor="hand2", command=self._open_register_dialog)
        self.btn_reg.pack(fill="x", ipady=4)

        # Footer HWID
        lbl_hwid = tk.Label(self.root, text=f"HWID: {self.client.hwid[:16]}...", font=("Consolas", 8), fg=MUTED, bg=BG)
        lbl_hwid.pack(side="bottom", pady=8)

        # Enter key triggers login
        self.root.bind("<Return>", lambda e: self._do_login())

    def _open_register_dialog(self):
        RegisterDialog(self.root, self.client, on_success_callback=self._on_user_registered)

    def _on_user_registered(self, email):
        self.ent_email.delete(0, "end")
        self.ent_email.insert(0, email)
        self.ent_pass.delete(0, "end")
        self.lbl_msg.config(text="Akun berhasil diaktivasi! Silakan login.", fg=GREEN)

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
