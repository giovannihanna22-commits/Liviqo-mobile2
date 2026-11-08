@echo off
cd /d "%~dp0"
if not exist .venv (
  py -3 -m venv .venv
)
call .venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
echo.
echo ============================================
echo LIVIQO is starting for phone access.
echo On Windows, run IPCONFIG and find your Wi-Fi IPv4 address.
echo Then open http://YOUR-IP:8000 on your phone.
echo Keep this window open and connect both devices to same Wi-Fi.
echo ============================================
echo.
python -m uvicorn app:app --host 0.0.0.0 --port 8000
pause
