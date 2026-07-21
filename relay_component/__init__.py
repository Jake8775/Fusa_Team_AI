import os
import streamlit.components.v1 as components

_component_func = components.declare_component(
    "relay_component",
    path=os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend"),
)


def relay_call(request_id: str, engine: str, api_key: str, model: str,
               system_prompt: str, user_message: str):
    """
    브라우저 JS를 통해 localhost:8765 릴레이 EXE에 API 요청을 보내고
    결과를 반환한다. 비동기 — 첫 렌더에서 None, 완료 후 rerun 시 결과 반환.

    반환값: {"request_id": str, "result": str|None, "elapsed": float, "error": str|None}
    """
    return _component_func(
        request_id=request_id,
        engine=engine,
        api_key=api_key,
        model=model,
        system_prompt=system_prompt,
        user_message=user_message,
        default=None,
        key="relay_main",
    )
