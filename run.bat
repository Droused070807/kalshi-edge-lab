@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
if not exist .venv (
  echo Creating virtual environment...
  %PY% -m venv .venv || (echo Python 3.11+ is required: https://www.python.org/downloads/ & pause & exit /b 1)
)
call .venv\Scripts\activate.bat
python -m pip install -q --disable-pip-version-check -r requirements.txt || (pause & exit /b 1)
if not exist .env (
  copy .env.example .env >nul
  echo Created .env - add your ODDS_API_KEY to it, then run this again.
  notepad .env
  exit /b 0
)
start "" http://127.0.0.1:8000
python -m app
