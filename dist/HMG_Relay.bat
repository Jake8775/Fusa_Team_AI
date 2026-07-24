@echo off
chcp 65001 > nul
title HMG Relay Server
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765 "') do taskkill /PID %%a /F >nul 2>&1
HMG_Relay.exe
