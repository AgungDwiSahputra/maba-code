@echo off
rem Launcher MABA Code: jalankan dari folder proyek mana pun, mis.  D:\Workspace Agung\MABA\maba.cmd
setlocal
set "PYTHONUTF8=1"
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
python -m maba_code %*
exit /b %ERRORLEVEL%
