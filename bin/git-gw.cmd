@echo off
REM Zero-Trust git shim -> forwards to the guarded git wrapper.
python "%~dp0..\scripts\git_gw.py" %*
