@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
pushd "%~dp0"
if errorlevel 1 (
    echo [エラー] アプリのフォルダーを開けません。
    pause
    exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
    echo [エラー] Python 環境が見つかりません。先に SETUP.bat を実行してください。
    popd
    pause
    exit /b 1
)

set "PYTHONUTF8=1"
".venv\Scripts\python.exe" "src\main.py" %*
set "APP_EXIT_CODE=%ERRORLEVEL%"
popd
if not "%APP_EXIT_CODE%"=="0" (
    echo [エラー] 起動または実行に失敗しました。上のエラー内容を確認してください。
    pause
)
exit /b %APP_EXIT_CODE%
