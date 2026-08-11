@echo off
REM Zero-Trust GitLab CLI shim -> forwards to the guarded glab wrapper.
python "%~dp0..\scripts\glab_gw.py" %*
