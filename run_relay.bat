@echo off
chcp 65001 > nul
title HMG Relay Server
echo HMG 릴레이 서버 시작 중... (localhost:8765)
echo 이 창을 닫으면 서버가 종료됩니다.
echo.
python relay.py
pause
