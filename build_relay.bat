@echo off
chcp 65001 > nul
echo HMG_Relay.exe 빌드 시작...
echo.

pip install pyinstaller fastapi uvicorn openai anthropic google-genai

echo.
echo PyInstaller 빌드 중...
pyinstaller --onefile --name HMG_Relay relay.py

echo.
if exist dist\HMG_Relay.exe (
    echo 빌드 완료: dist\HMG_Relay.exe
) else (
    echo 빌드 실패. 오류 메시지를 확인하세요.
)
echo.
pause
