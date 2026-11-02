"""生成 scripts/*.bat（GBK + CRLF）。

为什么用脚本生成：cmd 读 UTF-8 的 bat 会把中文注释读成乱码，甚至报「语法不
正确」（实测过）。.bat 必须存 GBK、行尾 CRLF，所以这里统一由 Python 写出来，
别在编辑器里手改这些文件（要改逻辑就改 wxbridge/ctl.py）。

为什么额外设 PYTHONIOENCODING=gbk：控制台代码页是 936，而 Python 用 -X utf8
跑（文件读写必须是 UTF-8，否则 config.yaml 和日志里的中文会坏）。这两套凑在
一起，print 出来的中文在 cmd 窗口里就是一堆乱码 —— 让 stdio 单独走 GBK，界面
才正常，文件那边不受影响。
"""
from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "scripts"

FIND_PY = """
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
"""

HEADER = """@echo off
chcp 936 >nul
setlocal
set PYTHONIOENCODING=gbk
cd /d "%~dp0.."
call :findpy
"""

SCRIPTS = {
    "install.bat": """echo [1/3] 升级 pip…
%PY% -m pip install --upgrade pip

echo [2/3] 装依赖（websockets + 微信读写库，要联网）…
%PY% -m pip install -r requirements.txt
if %errorlevel%==0 goto :ok

echo [注意] 在线装失败了。仓库 vendor\\ 里有同一个版本的离线包，改用这条：
echo        %PY% -m pip install vendor\\wechatauto_replica-1.2.8.1.tar.gz websockets
pause
exit /b 1

:ok
echo [3/3] 体检一下环境…
%PY% -X utf8 -u wxbridge\\ctl.py doctor
echo.
echo 装好了。下一步：
echo   1) 用记事本打开 config.yaml，填模型地址、模型名、密钥、人设
echo   2) 双击 start_admin.bat（推荐：发送要管理员权限才稳）
pause
exit /b 0
""",

    "start.bat": """echo 先体检，再启动…
%PY% -X utf8 -u wxbridge\\ctl.py doctor
if %errorlevel%==1 (
    echo.
    echo [不行] 体检没通过，先按上面的提示补齐再启动。
    pause
    exit /b 1
)
echo.
%PY% -X utf8 -u wxbridge\\ctl.py start
if %errorlevel%==1 ( pause & exit /b 1 )
start "" http://127.0.0.1:8766/
echo.
echo 控制台已经在浏览器里打开了（http://127.0.0.1:8766/）。
echo 不想要自动回复了：双击 stop.bat。情况不对：立刻双击 panic.bat。
pause
exit /b 0
""",

    "start_admin.bat": """net session >nul 2>nul
if %errorlevel%==0 (
    call "%~dp0start.bat"
    exit /b %errorlevel%
)
echo 正在请求管理员权限（UAC 弹窗点「是」）…
echo 为什么要点它：不是管理员时，界面发送只能走「坐标 + OCR」，微信窗口一挪就点歪。
powershell -NoProfile -Command "Start-Process -FilePath '%~dp0start_admin.bat' -Verb RunAs"
exit /b 0
""",

    "stop.bat": """%PY% -X utf8 -u wxbridge\\ctl.py stop
pause
exit /b 0
""",

    "restart.bat": """%PY% -X utf8 -u wxbridge\\ctl.py restart
pause
exit /b 0
""",

    "status.bat": """%PY% -X utf8 -u wxbridge\\ctl.py status
pause
exit /b 0
""",

    "doctor.bat": """%PY% -X utf8 -u wxbridge\\ctl.py doctor
pause
exit /b 0
""",

    "console.bat": """%PY% -X utf8 -u wxbridge\\ctl.py console
exit /b 0
""",

    "panic.bat": """%PY% -X utf8 -u wxbridge\\ctl.py panic
echo 想恢复自动回复：双击 resume.bat
pause
exit /b 0
""",

    "resume.bat": """%PY% -X utf8 -u wxbridge\\ctl.py panic --resume
pause
exit /b 0
""",

    "demo.bat": """echo 不连微信，用假消息把「消息 → 大脑 → 回复」跑一遍（Ctrl+C 退出）…
%PY% -X utf8 -u wxbridge\\ctl.py demo
exit /b 0
""",
}

NOT_ADMIN = """
net session >nul 2>nul
if %errorlevel%==0 goto :admin
echo [注意] 现在不是管理员：发送会退化成「坐标 + OCR」，微信窗口一动就废。
echo        建议改用 start_admin.bat（会弹 UAC）。还是要继续的话等 5 秒…
timeout /t 5 >nul
goto :go
:admin
echo [OK] 管理员权限：有
:go
"""


def build() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, body in SCRIPTS.items():
        text = HEADER + body
        if name == "start.bat":
            text = text.replace(
                'echo 先体检，再启动…\n', NOT_ADMIN + "\necho 先体检，再启动…\n")
        text += FIND_PY
        path = OUT / name
        path.write_bytes(text.replace("\n", "\r\n").encode("gbk"))
        print("写了", path.name, path.stat().st_size, "字节")


if __name__ == "__main__":
    build()
