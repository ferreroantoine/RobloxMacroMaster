@echo off
rem Reconstruit MacroRoblox.exe (dans ce dossier) a partir de macro_gui.py + roblox_macro.py.
cd /d "%~dp0"
python -m pip install --upgrade pyinstaller opencv-python numpy mss pillow || goto :error
python -m PyInstaller --noconfirm --onefile --windowed --name MacroRoblox ^
    --distpath . --workpath build --specpath build macro_gui.py || goto :error
echo.
echo OK : MacroRoblox.exe est pret.
pause
exit /b 0

:error
echo.
echo Echec de la construction.
pause
exit /b 1
