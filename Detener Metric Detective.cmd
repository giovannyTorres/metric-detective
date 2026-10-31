@echo off
cd /d "%~dp0"
uv run --locked metric-detective local stop
if errorlevel 1 pause
