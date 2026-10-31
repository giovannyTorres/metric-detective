@echo off
cd /d "%~dp0"
uv run --locked metric-detective local status
pause
