@echo off
chcp 65001 >nul
set IMAGEIO_FFMPEG_EXE=C:\Program Files\Python38\lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win64-v4.2.2.exe
cd /d G:\ZAOANDIANTAI
python generate_video.py %*
if %ERRORLEVEL% EQU 0 (
    start "" "G:\ZAOANDIANTAI\output\morning_radio.mp4"
)
pause
