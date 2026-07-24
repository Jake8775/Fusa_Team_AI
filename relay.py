"""
HMG 사내 API 릴레이 서버
사용자 PC(사내망)에서 실행 — localhost:8765 리스닝
SDK 없이 httpx 직접 호출로 번들 크기 최소화
"""
import os
os.environ.setdefault("PYTHONUTF8", "1")

import asyncio
import base64
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
    images        = body.get("images", [])  # [{mime, b64}]

    start = time.time()
    try:
        loop = asyncio.get_event_loop()
        if engine == "GPT":
            result = await loop.run_in_executor(
                None, _call_gpt, api_key, model, system_prompt, user_message, images
            )
        elif engine == "Claude":
            result = await loop.run_in_executor(
                None, _call_claude, api_key, model, system_prompt, user_message, images
            )
        elif engine == "Gemini":
            result = await loop.run_in_executor(
                None, _call_gemini, api_key, model, system_prompt, user_message, images
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

def _call_gpt(api_key, model, system_prompt, user_message, images=None):
    url = (
        f"{BASE_URL}/openai/deployments/{model}/chat/completions"
        f"?api-version={GPT_API_VERSION}"
    )
    messages = []
    if system_prompt.strip():
        messages.append({"role": "system", "content": system_prompt})

    if images:
        content = []
        for img in images:
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{img['mime']};base64,{img['b64']}"},
            })
        content.append({"type": "text", "text": user_message})
    else:
        content = user_message

    messages.append({"role": "user", "content": content})

    resp = httpx.post(
        url,
        headers={"api-key": api_key, "Content-Type": "application/json"},
        json={"messages": messages, "temperature": 0.9, "max_completion_tokens": 4096},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"] or ""


def _call_claude(api_key, model, system_prompt, user_message, images=None):
    url = f"{BASE_URL}/v1/messages"

    if images:
        content = []
        for img in images:
            content.append({
                "type": "image",
                "source": {
                    "type":       "base64",
                    "media_type": img["mime"],
                    "data":       img["b64"],
                },
            })
        content.append({"type": "text", "text": user_message})
    else:
        content = user_message

    body = {
        "model":       model,
        "messages":    [{"role": "user", "content": content}],
        "max_tokens":  8192,
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


def _call_gemini(api_key, model, system_prompt, user_message, images=None):
    from google import genai
    from google.genai import types as _gtypes
    from google.genai.types import HttpOptions, GenerateContentConfig
    GEMINI_BASE_URL = "https://internal-apigw-kr.hmg-corp.io/hchat-in/api/v3?key="
    base = GEMINI_BASE_URL + api_key
    client = genai.Client(
        api_key=api_key,
        http_options=HttpOptions(api_version="v1", base_url=base),
    )
    config = GenerateContentConfig(temperature=0.9, max_output_tokens=8192)
    if system_prompt.strip():
        config.system_instruction = system_prompt

    if images:
        parts = []
        for img in images:
            parts.append(
                _gtypes.Part.from_bytes(
                    data=base64.b64decode(img["b64"]),
                    mime_type=img["mime"],
                )
            )
        parts.append(user_message)
        contents = parts
    else:
        contents = user_message

    resp = client.models.generate_content(model=model, contents=contents, config=config)
    return resp.text or ""


if __name__ == "__main__":
    print(f"HMG 릴레이 서버 시작 중... (localhost:{PORT})")
    print("이 창을 닫으면 서버가 종료됩니다.")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="error")
