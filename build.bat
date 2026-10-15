@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>nul || (echo [ERROR] python not found in PATH. Install Python 3.11+ and check "Add to PATH". & exit /b 1)
python -m pip install -r requirements.txt || (echo [ERROR] pip install failed. See messages above. & exit /b 1)
python -m PyInstaller --noconfirm --clean --onefile --windowed --name OWL owl_gui.py || (echo [ERROR] PyInstaller build failed. See messages above. & exit /b 1)
echo.
echo [OK] Built: %~dp0dist\OWL.exe
endlocal
