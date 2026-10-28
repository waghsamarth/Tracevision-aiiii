@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Creating virtual environment...
  python -m venv .venv || (echo Python 3 is not installed or not on PATH. & pause & exit /b 1)
)
call .venv\Scripts\activate.bat
python -m pip install -q -r requirements.txt || (echo Package install failed. Check your internet connection. & pause & exit /b 1)
start "" http://127.0.0.1:5000
python app.py
pause
