@echo off
setlocal
set PROJECT_DIR=%~dp0..
wsl -d Ubuntu-22.04 --cd "%PROJECT_DIR%" -- /home/ttt/.local/bin/muse %*
