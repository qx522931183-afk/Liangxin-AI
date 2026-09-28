@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "LIANXIN_PYTHON=D:\anaconda3\envs\lianxin\python.exe"
set "PLAYWRIGHT_BROWSERS_PATH=D:\anaconda3\envs\lianxin\ms-playwright"
set "HF_HOME=D:\anaconda3\envs\lianxin\cache\huggingface"
set "HF_ENDPOINT=https://hf-mirror.com"
set "HF_HUB_DISABLE_XET=1"
set "TORCH_HOME=D:\anaconda3\envs\lianxin\cache\torch"
set "PATH=D:\anaconda3\envs\lianxin;D:\anaconda3\envs\lianxin\Scripts;D:\anaconda3\envs\lianxin\Library\bin;%PATH%"

if not exist "%LIANXIN_PYTHON%" (
    echo Lianxin Conda environment was not found:
    echo %LIANXIN_PYTHON%
    pause
    exit /b 1
)

"%LIANXIN_PYTHON%" main.py
if errorlevel 1 (
    echo.
    echo Lianxin exited with an error.
    pause
)
