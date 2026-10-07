@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
pushd "%~dp0"
if errorlevel 1 goto location_failed

set "SETUP_PAUSE=1"
if /i "%~1"=="--no-pause" set "SETUP_PAUSE=0"
set "SETUP_PYTHON=%CD%\.venv\Scripts\python.exe"
set "PYTHONUTF8=1"
set "PIPENV_VENV_IN_PROJECT=1"
set "PIPENV_IGNORE_VIRTUALENVS=1"

echo GijirokuStudio 初期導入
echo 録音・日英文字起こし・英日翻訳の環境とモデルを導入します。
echo 初回はインターネット接続が必要です。導入済みモデルは再利用します。
echo.

set "SETUP_STEP=必要ファイルの確認"
if not exist "Pipfile" goto failed
if not exist "Pipfile.lock" goto failed
if not exist "src\main.py" goto failed
if not exist "src\setup_fast_asr.py" goto failed
if not exist "src\setup_translation.py" goto failed
if not exist "ffmpeg.exe" goto ffmpeg_missing
"%CD%\ffmpeg.exe" -version >nul 2>&1
if errorlevel 1 goto ffmpeg_invalid

echo [1/4] Python 3.13 とPipenvを準備
set "SETUP_STEP=Python・Pipenvの確認"
py -3.13 -c "import sys, struct; sys.exit(0 if sys.version_info[:2] == (3, 13) and struct.calcsize('P') == 8 else 1)" >nul 2>&1
if not errorlevel 1 goto use_launcher
python -c "import sys, struct; sys.exit(0 if sys.version_info[:2] == (3, 13) and struct.calcsize('P') == 8 else 1)" >nul 2>&1
if errorlevel 1 goto python_missing
set "SETUP_HOST=python"
goto check_pipenv

:use_launcher
set "SETUP_HOST=py -3.13"

:check_pipenv
if exist "%SETUP_PYTHON%" goto check_existing_python
goto ensure_pipenv

:check_existing_python
"%SETUP_PYTHON%" -c "import sys, struct; sys.exit(0 if sys.version_info[:2] == (3, 13) and struct.calcsize('P') == 8 else 1)" >nul 2>&1
if errorlevel 1 goto python_invalid

:ensure_pipenv
%SETUP_HOST% -m pipenv --version >nul 2>&1
if not errorlevel 1 goto sync_dependencies
%SETUP_HOST% -m pip install --user "pipenv==2025.0.4"
if errorlevel 1 goto failed

:sync_dependencies
echo [2/4] Pipfile.lockから実行用ライブラリを導入
set "SETUP_STEP=Pipenvでの仮想環境・実行用ライブラリの導入"
%SETUP_HOST% -m pipenv sync --python 3.13 --categories "default local"
if errorlevel 1 goto failed
"%SETUP_PYTHON%" -c "import sys, struct; sys.exit(0 if sys.version_info[:2] == (3, 13) and struct.calcsize('P') == 8 else 1)" >nul 2>&1
if errorlevel 1 goto python_invalid

echo [3/4] 日英文字起こしモデルを導入
set "SETUP_STEP=日英文字起こしモデルの導入"
%SETUP_HOST% -m pipenv run python "src\setup_fast_asr.py"
if errorlevel 1 goto failed

echo [4/4] 省電力の英日翻訳モデルを導入
set "SETUP_STEP=英日翻訳モデルの導入"
%SETUP_HOST% -m pipenv run python "src\setup_translation.py" --model elan-tiny
if errorlevel 1 goto failed

set "SETUP_STEP=依存関係の確認"
%SETUP_HOST% -m pipenv run python -m pip check
if errorlevel 1 goto failed

echo.
echo 初期導入が完了しました。
echo RUN_EXE.bat をダブルクリックするとアプリが起動します。
echo 翻訳はアプリの「英語 → 日本語の翻訳を使う」でONにできます。
if "%SETUP_PAUSE%"=="1" pause
popd
endlocal
exit /b 0

:python_missing
echo [エラー] Python 3.13 の64bit版が見つかりません。
echo Python 3.13 の64bit版をインストールして、このbatを再実行してください。
goto failed

:python_invalid
echo [エラー] 既存の .venv がPython 3.13の64bit版ではないか、起動できません。
echo .venv を別名に退避してから再実行してください。
goto failed

:ffmpeg_missing
echo [エラー] このbatと同じフォルダに ffmpeg.exe を配置してください。
goto failed

:ffmpeg_invalid
echo [エラー] ffmpeg.exe を起動できません。Windows用の実行ファイルを確認してください。
goto failed

:failed
echo.
echo 初期導入を中止しました: %SETUP_STEP%
echo 上のエラーを確認してから再実行してください。
if "%SETUP_PAUSE%"=="1" pause
popd
endlocal
exit /b 1

:location_failed
echo [エラー] アプリのフォルダを開けません。
pause
endlocal
exit /b 1
