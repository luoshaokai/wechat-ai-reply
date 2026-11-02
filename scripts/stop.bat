@echo off
chcp 936 >nul
setlocal
set PYTHONIOENCODING=gbk
cd /d "%~dp0.."
call :findpy
%PY% -X utf8 -u wxbridge\ctl.py stop
pause
exit /b 0

:findpy
set "PY="
where py.exe >nul 2>nul
if %errorlevel%==0 set "PY=py -3"
if defined PY exit /b
where python.exe >nul 2>nul
if %errorlevel%==0 set "PY=python"
if defined PY exit /b
echo [不行] 没找到 Python。先装 3.9 以上的 Windows 版，装的时候勾上 Add python.exe to PATH。
echo        下载：https://www.python.org/downloads/windows/
pause
exit /b 1
