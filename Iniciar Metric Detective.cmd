@echo off
cd /d "%~dp0"
where uv >nul 2>nul
if errorlevel 1 (
  echo Instala uv o usa el inicio Docker descrito en README.es.md.
  pause
  exit /b 1
)
uv run --locked metric-detective local start --open
if errorlevel 1 pause
