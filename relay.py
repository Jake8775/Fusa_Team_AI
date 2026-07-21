"""
HMG 사내 API 릴레이 서버
사용자 PC(사내망)에서 실행 — localhost:8765 리스닝
SDK 없이 httpx 직접 호출로 번들 크기 최소화
"""
import os
os.environ.setdefault("PYTHONUTF8", "1")

import asyncio
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

BASE_URL        = "https://internal-apigw-kr.hmg-corp.io/hchat-in/api/v3"
GPT_API_VERSION = "2025-04-01-preview"
PORT            = 8765
TIMEOUT         = httpx.Timeout(connect=30, read=120, write=60, pool=30)

_CORS = {
    "Access-Control-Allow-Origin":          "*",
    "Access-Control-Allow-Methods":         "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers":         "*",
    "Access-Control-Allow-Private-Network": "true",
}

app = FastAPI()


@app.options("/{path:path}")
async def preflight(path: str):
    return JSONResponse({}, headers=_CORS)


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"}, headers=_CORS)


@app.post("/relay")
async def relay(request: Request):
    body          = await request.json()
    engine        = body["engine"]
    api_key       = body["api_key"]
    model         = body["model"]
    system_prompt = body.get("system_prompt", "")
    user_message  = body["user_message"]

    start = time.time()
    try:
        loop = asyncio.get_event_loop()
        if engine == "GPT":
            result = await loop.run_in_executor(
                None, _call_gpt, api_key, model, system_prompt, user_message
            )
        elif engine == "Claude":
            result = await loop.run_in_executor(
                None, _call_claude, api_key, model, system_prompt, user_message
            )
        elif engine == "Gemini":
            result = await loop.run_in_executor(
                None, _call_gemini, api_key, model, system_prompt, user_message
            )
        else:
            raise ValueError(f"Unknown engine: {engine}")

        return JSONResponse(
            {"result": result, "elapsed": time.time() - start},
            headers=_CORS,
        )
    except Exception as e:
        return JSONResponse(
            {"error": str(e), "elapsed": time.time() - start},
            status_code=500,
            headers=_CORS,
        )


# ── 직접 HTTP 호출 (SDK 없음) ─────────────────────────────────────────────────

def _call_gpt(api_key, model, system_prompt, user_message):
    url = (
        f"{BASE_URL}/openai/deployments/{model}/chat/completions"
        f"?api-version={GPT_API_VERSION}"
    )
    messages = []
    if system_prompt.strip():
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_message})

    resp = httpx.post(
        url,
        headers={"api-key": api_key, "Content-Type": "application/json"},
        json={"messages": messages, "temperature": 0.9, "max_completion_tokens": 4096},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"] or ""


def _call_claude(api_key, model, system_prompt, user_message):
    url = f"{BASE_URL}/v1/messages"
    body = {
        "model":     model,
        "messages":  [{"role": "user", "content": user_message}],
        "max_tokens": 8192,
        "temperature": 0.9,
    }
    if system_prompt.strip():
        body["system"] = system_prompt

    resp = httpx.post(
        url,
        headers={
            "x-api-key":         api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type":      "application/json",
        },
        json=body,
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["content"][0]["text"]


def _call_gemini(api_key, model, system_prompt, user_message):
    # NOTE: HMG 게이트웨이 Gemini URL 패턴 — 실제 동작 확인 필요
    url = f"{BASE_URL}/v1/models/{model}:generateContent?key={api_key}"
    body = {
        "contents": [{"parts": [{"text": user_message}]}],
        "generationConfig": {"temperature": 0.9, "maxOutputTokens": 8192},
    }
    if system_prompt.strip():
        body["systemInstruction"] = {"parts": [{"text": system_prompt}]}

    resp = httpx.post(
        url,
        headers={"Content-Type": "application/json"},
        json=body,
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["candidates"][0]["content"]["parts"][0]["text"]


if __name__ == "__main__":
    print(f"HMG 릴레이 서버 시작 중... (localhost:{PORT})")
    print("이 창을 닫으면 서버가 종료됩니다.")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="error")
