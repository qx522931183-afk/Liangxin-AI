@echo off
setlocal EnableExtensions
cd /d "%~dp0"

REM Liang - no console window launcher (uses pythonw.exe)
REM NOTE: use run.bat when you need to see error messages.

set "LIANXIN_PY=D:\anaconda3\envs\lianxin\pythonw.exe"
set "PLAYWRIGHT_BROWSERS_PATH=D:\anaconda3\envs\lianxin\ms-playwright"
set "HF_HOME=D:\anaconda3\envs\lianxin\cache\huggingface"
set "HF_ENDPOINT=https://hf-mirror.com"
set "HF_HUB_DISABLE_XET=1"
set "TORCH_HOME=D:\anaconda3\envs\lianxin\cache\torch"
set "PATH=D:\anaconda3\envs\lianxin;D:\anaconda3\envs\lianxin\Scripts;D:\anaconda3\envs\lianxin\Library\bin;%PATH%"

if not exist "%LIANXIN_PY%" (
    echo pythonw.exe not found: %LIANXIN_PY%
    pause
    exit /b 1
)

start "" "%LIANXIN_PY%" main.py
