@echo off
REM One-command start for Windows: creates a virtualenv, installs, runs.
cd /d "%~dp0"

if not exist .venv (
  echo Creating virtual environment...
  python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install -q --upgrade pip
pip install -q -r requirements.txt

echo.
echo   Time Off is running at  http://localhost:8000
echo   API docs:               http://localhost:8000/docs
echo.
uvicorn app.main:app --reload --port 8000
