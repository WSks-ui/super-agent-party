"""
TTS WebSocket 端点模块
提供 FastAPI 路由注册，支持独立的 TTS 流式服务
"""

import asyncio
import base64
import json
import logging
import uuid
from typing import Optional

import websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import JSONResponse

from py.tts_adapter import tts_adapter
from py.tts_policy import tts_policy_manager
from py.tts_streaming import streaming_manager, stream_audio_chunks

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ws/tts", tags=["tts"])


@router.websocket("/stream")
async def tts_stream_websocket(websocket: WebSocket):
    """
    TTS 流式 WebSocket 端点

    协议：
    1. 客户端发送 {"type": "init", "voice_id": "xxx", "language": "zh"}
    2. 客户端发送 {"type": "speak", "text": "xxx"}
    3. 服务端发送 {"type": "audio_chunk", "audio": "base64...", "chunk_index": 0}
    4. 服务端发送 {"type": "audio_complete", "total_chunks": N}
    """
    session_id = str(uuid.uuid4())
    voice_id = "default"
    language = "zh"
    settings = None

    await websocket.accept()

    try:
        async for message in websocket.iter_json():
            msg_type = message.get("type")

            if msg_type == "init":
                voice_id = message.get("voice_id", "default")
                language = message.get("language", "zh")
                session_id = message.get("session_id", session_id)

                await websocket.send_json({
                    "type": "init_response",
                    "session_id": session_id,
                    "status": "ready"
                })

            elif msg_type == "speak":
                text = message.get("text", "")
                if not text:
                    await websocket.send_json({
                        "type": "error",
                        "error": "文本为空"
                    })
                    continue

                policy = tts_policy_manager.get_policy(session_id)
                is_allowed, reason = policy.check(text, voice_id)

                if not is_allowed:
                    await websocket.send_json({
                        "type": "error",
                        "error": f"策略拦截: {reason}",
                        "policy_info": policy.get_stats()
                    })
                    continue

                engine = "edge"
                if settings:
                    tts_settings = settings.get("ttsSettings", {})
                    engine = tts_settings.get("engine", "edge")

                try:
                    audio_iter = tts_adapter.synthesize_stream(
                        text=text,
                        engine=engine,
                        voice_id=voice_id,
                        language=language,
                    )

                    await stream_audio_chunks(audio_iter, websocket, session_id)

                except Exception as e:
                    logger.error(f"TTS 流式合成失败: {e}")
                    await websocket.send_json({
                        "type": "error",
                        "error": str(e)
                    })

            elif msg_type == "get_status":
                stats = policy.get_stats()
                await websocket.send_json({
                    "type": "status",
                    "session_id": session_id,
                    "stats": stats
                })

    except WebSocketDisconnect:
        logger.info(f"TTS WebSocket 连接断开: {session_id}")
    except Exception as e:
        logger.error(f"TTS WebSocket 错误: {e}")
    finally:
        await streaming_manager.close_session(session_id)


@router.websocket("/stream/{session_id}")
async def tts_stream_with_session(
    websocket: WebSocket,
    session_id: str
):
    """
    带 session_id 的 TTS 流式 WebSocket 端点

    用于已有的流式会话续传
    """
    await websocket.accept()

    session = streaming_manager.get_session(session_id)
    if session:
        streamer = streaming_manager.get_streamer(session_id)
    else:
        await websocket.send_json({
            "type": "error",
            "error": "会话不存在"
        })
        await websocket.close()
        return

    try:
        async for message in websocket.iter_json():
            if message.get("type") == "speak":
                text = message.get("text", "")
                if text:
                    policy = tts_policy_manager.get_policy(session_id)
                    is_allowed, reason = policy.check(text, session.voice_id)

                    if is_allowed:
                        audio_iter = tts_adapter.synthesize_stream(
                            text=text,
                            engine="edge",
                            voice_id=session.voice_id,
                            language=session.language,
                        )
                        await stream_audio_chunks(audio_iter, websocket, session_id)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"TTS 流式错误: {e}")
    finally:
        await streaming_manager.close_session(session_id)


def register_tts_routes(app):
    """
    注册 TTS 路由到 FastAPI 应用

    使用方法：
    ```python
    from py.tts_routes import register_tts_routes

    # 在 server.py 的 lifespan 或 startup 中调用
    register_tts_routes(app)
    ```
    """
    from fastapi import FastAPI

    if isinstance(app, FastAPI):
        app.include_router(router)
        logger.info("TTS 路由已注册到 FastAPI 应用")
    else:
        raise TypeError("app 必须是 FastAPI 实例")


def get_tts_system_prompt() -> str:
    """
    获取 TTS 系统的 prompt

    在 LLM system prompt 中注入此内容，强制 LLM 使用 TTS 工具
    """
    return """【语音合成规则】

你拥有 voice_speak 工具，可以将文本转换为语音。

使用时机：
- 回复用户时，重要的信息用语音播报
- 讲故事、讲笑话、朗读诗歌
- 需要口语化表达的场合

使用限制：
- 只朗读纯文本，不要包含 Markdown
- 单次不超过 220 字符
- 代码、表格、长段落不朗读

使用示例：
用户：给我讲个笑话
你应调用：voice_speak(text="有一天...")

注意：
- voice_speak 是异步的，调用后会立即返回
- 可以和其他回复内容并行调用
- 保持语音内容简洁明了"""


def create_tts_agent_system_prompt(
    persona: str = "",
    voice_rules: str = ""
) -> str:
    """
    创建完整的 TTS Agent system prompt

    Args:
        persona: 人格描述
        voice_rules: 自定义语音规则

    Returns:
        完整的 system prompt
    """
    base_rules = get_tts_system_prompt()

    if voice_rules:
        return f"{persona}\n\n{voice_rules}\n\n{base_rules}"
    else:
        return f"{persona}\n\n{base_rules}"


async def tts_http_endpoint(request: Request):
    """
    HTTP TTS 接口（简化版，不依赖原有 /tts）

    相比原有接口，更注重 Tool Calling 架构的集成
    """
    from py.get_setting import load_settings

    try:
        data = await request.json()
        text = data.get("text", "")
        voice_id = data.get("voice_id", "default")
        language = data.get("language", "zh")
        session_id = data.get("session_id", "default")
        stream = data.get("stream", False)

        if not text:
            return JSONResponse(status_code=400, content={"error": "文本为空"})

        policy = tts_policy_manager.get_policy(session_id)
        is_allowed, reason = policy.check(text, voice_id)

        if not is_allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "error": f"策略拦截: {reason}",
                    "policy_info": policy.get_stats()
                }
            )

        settings = await load_settings()

        async def generate():
            try:
                audio_iter = tts_adapter.synthesize_stream(
                    text=text,
                    engine="edge",
                    voice_id=voice_id,
                    language=language,
                )

                async for chunk in audio_iter:
                    yield chunk

            except Exception as e:
                logger.error(f"TTS 生成失败: {e}")
                yield b""

        if stream:
            from fastapi.responses import StreamingResponse
            return StreamingResponse(
                generate(),
                media_type="audio/mpeg",
                headers={
                    "X-Session-ID": session_id,
                    "X-Voice-ID": voice_id,
                    "X-Language": language
                }
            )
        else:
            audio_data = await tts_adapter.synthesize(
                text=text,
                engine="edge",
                voice_id=voice_id,
                language=language,
            )
            audio_b64 = base64.b64encode(audio_data).decode("utf-8")
            return JSONResponse(content={
                "success": True,
                "audio": audio_b64,
                "format": "mp3",
                "bytes": len(audio_data),
                "policy_info": policy.get_stats()
            })

    except Exception as e:
        logger.error(f"TTS HTTP 端点错误: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})
