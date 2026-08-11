@echo off
REM Launch the web UI. Requires the `claude` CLI on PATH (or set GATEWAY_CLAUDE_CMD).
python "%~dp0..\scripts\ui.py" %*
