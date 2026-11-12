#!/usr/bin/env python3
"""Script untuk membuat akun admin baru DanzDroid di Supabase."""
import urllib.request
import urllib.error
import json

SUPABASE_URL = "https://sxdhwmrtzuzuzdcjxsso.supabase.co"
SUPABASE_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InN4ZGh3bXJ0enV6dXpkY2p4c3NvIiwicm9sZSI6ImFub24iLCJpYXQiOjE3OTQ0MDk4MDksImV4cCI6MjEwOTk4NTgwOX0."
    "nezJvcqLrbw7N3Q9Cvm-qegMeGZZBMqaZISv-4RDimI"
)

NEW_EMAIL = "danu4830@gmail.com"
NEW_PASSWORD = "Droiddanz900$"
NEW_USERNAME = "DanzDroid"

headers = {
    "apikey": SUPABASE_KEY,
    "Content-Type": "application/json",
    "Authorization": f"Bearer {SUPABASE_KEY}",
}

payload = json.dumps({
    "email": NEW_EMAIL,
    "password": NEW_PASSWORD,
    "data": {
        "full_name": NEW_USERNAME,
        "username": NEW_USERNAME,
    },
}).encode("utf-8")

print(f"[*] Mendaftarkan akun admin baru: {NEW_EMAIL} ...")

req = urllib.request.Request(
    f"{SUPABASE_URL}/auth/v1/signup",
    data=payload,
    headers=headers,
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8")
        res = json.loads(raw)
        uid = res.get("user", {}).get("id", "")
        email = res.get("user", {}).get("email", "")
        has_token = bool(res.get("access_token"))
        print(f"[OK] Akun berhasil dibuat!")
        print(f"     User ID : {uid}")
        print(f"     Email   : {email}")
        print(f"     Login langsung (token): {has_token}")
        if uid:
            print(f"\n[INFO] Jalankan SQL ini di Supabase SQL Editor untuk konfirmasi email:")
            print(f"UPDATE auth.users SET email_confirmed_at = now() WHERE email = '{NEW_EMAIL}';")
            print()
            print(f"[INFO] Lalu jalankan SQL ini untuk set role admin di users_profile:")
            print(f"""INSERT INTO public.users_profile (id_user, username, full_name, role, subscription_status)
SELECT id, '{NEW_USERNAME}', '{NEW_USERNAME}', 'admin', 'active'
FROM auth.users WHERE email = '{NEW_EMAIL}'
ON CONFLICT (id_user) DO UPDATE SET role = 'admin', username = '{NEW_USERNAME}', full_name = '{NEW_USERNAME}';""")
            print()
            print(f"[INFO] Lalu hapus admin lama dengan SQL:")
            print(f"DELETE FROM auth.users WHERE email = 'chairilmaulan06@gmail.com';")

except urllib.error.HTTPError as e:
    raw = e.read().decode("utf-8")
    print(f"[ERROR] HTTP {e.code}: {raw}")
    try:
        err = json.loads(raw)
        print(f"  -> {err.get('msg') or err.get('message') or err}")
    except Exception:
        pass
except Exception as e:
    print(f"[ERROR] {e}")
