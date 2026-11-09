@echo off
cd /d "%~dp0"
py -3 -m venv .venv
if errorlevel 1 python -m venv .venv
call .venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
if not exist .env copy .env.example .env
echo.
echo Setup finished. Edit .env with your Cloudflare credentials, then run run.bat.
pause
