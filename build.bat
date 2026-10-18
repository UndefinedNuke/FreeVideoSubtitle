@echo off
cd /d "%~dp0"

echo === Checking Python ===
python --version
if errorlevel 1 (
  echo.
  echo Python is not installed or not added to PATH.
  goto fail
)

echo === Cleaning old build ===
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist FuckingVideo.spec del /q FuckingVideo.spec

echo === Creating clean environment ===
if exist venv rmdir /s /q venv
python -m venv venv
if errorlevel 1 goto fail
call venv\Scripts\activate.bat

echo === Installing packages (this takes a while) ===
python -m pip install -U pip
pip install -U openai-whisper customtkinter pyinstaller pyinstaller-hooks-contrib
if errorlevel 1 goto fail

echo === Building EXE ===
pyinstaller --noconfirm --clean --onedir --windowed --name FuckingVideo --collect-all whisper --collect-all customtkinter --hidden-import tiktoken_ext --hidden-import tiktoken_ext.openai_public subtitle_app.py
if errorlevel 1 goto fail

if exist ffmpeg.exe copy /y ffmpeg.exe dist\FuckingVideo\ >nul

echo.
echo === DONE ===
echo Program: dist\FuckingVideo\FuckingVideo.exe
if not exist dist\FuckingVideo\ffmpeg.exe echo REMINDER: put ffmpeg.exe into dist\FuckingVideo\
pause
exit /b 0

:fail
echo.
echo === ERROR. Send me the text above. ===
pause
exit /b 1