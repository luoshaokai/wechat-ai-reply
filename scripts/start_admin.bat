@echo off
chcp 936 >nul
setlocal
set PYTHONIOENCODING=gbk
cd /d "%~dp0.."
call :findpy
net session >nul 2>nul
if %errorlevel%==0 (
    call "%~dp0start.bat"
    exit /b %errorlevel%
)
echo 正在请求管理员权限（UAC 弹窗点「是」）…
echo 为什么要点它：不是管理员时，界面发送只能走「坐标 + OCR」，微信窗口一挪就点歪。
powershell -NoProfile -Command "Start-Process -FilePath '%~dp0start_admin.bat' -Verb RunAs"
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
