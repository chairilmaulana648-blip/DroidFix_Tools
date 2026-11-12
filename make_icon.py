#!/usr/bin/env python3
"""Convert logo.jpeg menjadi droidfix.ico untuk build PyInstaller."""
from PIL import Image
import os

src = "logo.jpeg"
dst = "droidfix.ico"

if not os.path.exists(src):
    print(f"[ERROR] File {src} tidak ditemukan!")
    exit(1)

img = Image.open(src).convert("RGBA")

# ICO multi-size: 16, 32, 48, 64, 128, 256
sizes = [(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)]

# Buat thumbnail square dengan background transparan
def make_square(im, size):
    im2 = im.copy()
    im2.thumbnail(size, Image.LANCZOS)
    bg = Image.new("RGBA", size, (0, 0, 0, 0))
    offset = ((size[0] - im2.width) // 2, (size[1] - im2.height) // 2)
    bg.paste(im2, offset)
    return bg

icons = [make_square(img, s) for s in sizes]
icons[0].save(dst, format="ICO", sizes=sizes, append_images=icons[1:])
print(f"[OK] {dst} berhasil dibuat dari {src}")
print(f"     Ukuran: {os.path.getsize(dst):,} bytes")
