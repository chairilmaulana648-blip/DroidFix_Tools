@echo off
chcp 65001 >nul
title Build DroidFix Flash Tool
echo ==============================================
echo   Build DroidFixFlashTool.exe
echo ==============================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo Python belum terpasang atau belum masuk PATH.
    echo Install dari https://www.python.org/downloads/ dan centang "Add python.exe to PATH".
    pause
    exit /b 1
)

if not exist droidfix.ico (
    echo File droidfix.ico tidak ditemukan. Taruh di folder yang sama dengan build.bat.
    pause
    exit /b 1
)
if not exist droidfix_assets.py (
    echo File droidfix_assets.py tidak ditemukan. Taruh di folder yang sama dengan build.bat.
    pause
    exit /b 1
)

echo [1/2] Memasang PyInstaller...
python -m pip install --upgrade pyinstaller
if errorlevel 1 (
    echo Gagal memasang PyInstaller. Cek koneksi internet.
    pause
    exit /b 1
)

echo.
echo [2/2] Membuat exe...
python -m PyInstaller --noconfirm --clean --onefile --noconsole --name DroidFixFlashTool --icon droidfix.ico --hidden-import droidfix_assets --hidden-import droidfix_supabase droidfix_flash_tool.py
if errorlevel 1 (
    echo Build gagal. Lihat pesan error di atas.
    pause
    exit /b 1
)

echo.
echo Selesai. File exe ada di: dist\DroidFixFlashTool.exe
echo.
echo Langkah terakhir: taruh folder "platform-tools" dan "scrcpy" di sebelah exe,
echo atau klik tombol "Folder tools" di dalam aplikasi untuk menunjuk lokasinya.
echo.
echo Jika ikon exe masih tampil lama di Explorer, itu cache Windows: restart Explorer
echo atau pindahkan exe ke folder lain.
echo.
pause
