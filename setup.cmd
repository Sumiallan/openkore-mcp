@echo off
REM Wrapper para o setup.ps1: funciona com duplo-clique, no cmd e no PowerShell,
REM sem precisar mexer na ExecutionPolicy da maquina.
REM
REM   setup.cmd
REM   setup.cmd -SkipEmbeddings
REM   setup.cmd -PythonExe "C:\Python313\python.exe"
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
set RC=%ERRORLEVEL%
endlocal & exit /b %RC%
