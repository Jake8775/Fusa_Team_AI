@echo off
chcp 65001 > nul
title HMG Relay Server

:: 기존 8765 포트 점유 프로세스 종료
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765 "') do (
    taskkill /PID %%a /F >nul 2>&1
)

echo HMG 릴레이 서버 시작 중... (localhost:8765)
echo 이 창을 닫으면 서버가 종료됩니다.
echo.
python relay.py
pause
