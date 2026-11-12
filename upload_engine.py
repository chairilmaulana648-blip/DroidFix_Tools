#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Upload Engine Xiaomi MTK ke Supabase Storage Bucket 'engines'
"""
import urllib.request
import urllib.error
import json
from pathlib import Path

SUPABASE_URL = "https://sxdhwmrtzuzuzdcjxsso.supabase.co"
SUPABASE_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InN4ZGh3bXJ0enV6dXpkY2p4c3NvIiwicm9sZSI6ImFub24iLCJpYXQiOjE3OTQ0MDk4MDksImV4cCI6MjEwOTk4NTgwOX0."
    "nezJvcqLrbw7N3Q9Cvm-qegMeGZZBMqaZISv-4RDimI"
)

ZIP_PATH = Path(__file__).resolve().parent / "xiaomi_mtk_engine.zip"


def upload_to_supabase():
    if not ZIP_PATH.is_file():
        print(f"File tidak ditemukan: {ZIP_PATH}")
        return False

    url = f"{SUPABASE_URL}/storage/v1/object/engines/xiaomi_mtk_engine.zip"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/zip",
        "x-upsert": "true",
    }

    print(f"Membaca {ZIP_PATH.name} ({ZIP_PATH.stat().st_size / (1024*1024):.2f} MB)...")
    with open(ZIP_PATH, "rb") as f:
        file_bytes = f.read()

    print(f"Mengunggah ke {url}...")
    req = urllib.request.Request(url, data=file_bytes, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            print(f"Upload SUKSES! Status: {resp.status}")
            print(resp.read().decode())
            public_url = f"{SUPABASE_URL}/storage/v1/object/public/engines/xiaomi_mtk_engine.zip"
            print(f"Public Download URL: {public_url}")
            return True
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        print(f"Upload GAGAL (HTTP {e.code}): {err}")
        return False
    except Exception as e:
        print(f"Error: {e}")
        return False


if __name__ == "__main__":
    upload_to_supabase()
