@echo off
REM Run the Director. Requires the `claude` CLI on PATH (or set GATEWAY_CLAUDE_CMD
REM to however you launch Claude Code).
python "%~dp0..\scripts\director.py" %*
