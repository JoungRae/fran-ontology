@echo off
chcp 65001 >nul
cd /d "%~dp0"
set BASE=%1
if "%BASE%"=="" set BASE=지하1층_pit
"D:\Python_test\fran_consist_cad_json\.venv\Scripts\python.exe" fire_server.py %BASE%
pause
