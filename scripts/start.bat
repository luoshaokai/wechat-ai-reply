@echo off
chcp 936 >nul
setlocal
set PYTHONIOENCODING=gbk
cd /d "%~dp0.."
call :findpy

net session >nul 2>nul
if %errorlevel%==0 goto :admin
echo [注意] 现在不是管理员：发送会退化成「坐标 + OCR」，微信窗口一动就废。
echo        建议改用 start_admin.bat（会弹 UAC）。还是要继续的话等 5 秒…
timeout /t 5 >nul
goto :go
:admin
echo [OK] 管理员权限：有
:go

echo 先体检，再启动…
%PY% -X utf8 -u wxbridge\ctl.py doctor
if %errorlevel%==1 (
    echo.
    echo [不行] 体检没通过，先按上面的提示补齐再启动。
    pause
    exit /b 1
)
echo.
%PY% -X utf8 -u wxbridge\ctl.py start
if %errorlevel%==1 ( pause & exit /b 1 )
start "" http://127.0.0.1:8766/
echo.
echo 控制台已经在浏览器里打开了（http://127.0.0.1:8766/）。
echo 不想要自动回复了：双击 stop.bat。情况不对：立刻双击 panic.bat。
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
