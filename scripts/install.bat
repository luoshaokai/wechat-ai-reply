@echo off
chcp 936 >nul
setlocal
set PYTHONIOENCODING=gbk
cd /d "%~dp0.."
call :findpy
echo [1/3] 升级 pip…
%PY% -m pip install --upgrade pip

echo [2/3] 装依赖（websockets + 微信读写库，要联网）…
%PY% -m pip install -r requirements.txt
if %errorlevel%==0 goto :ok

echo [注意] 在线装失败了。仓库 vendor\ 里有同一个版本的离线包，改用这条：
echo        %PY% -m pip install vendor\wechatauto_replica-1.2.8.1.tar.gz websockets
pause
exit /b 1

:ok
echo [3/3] 体检一下环境…
%PY% -X utf8 -u wxbridge\ctl.py doctor
echo.
echo 装好了。下一步：
echo   1) 用记事本打开 config.yaml，填模型地址、模型名、密钥、人设
echo   2) 双击 start_admin.bat（推荐：发送要管理员权限才稳）
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
