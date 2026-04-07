"""
TTS Tool Calling 模块
为 LLM 注册语音合成工具，实现 Agentic TTS 架构
"""

import asyncio
import base64
import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from py.tts_adapter import tts_adapter
from py.tts_policy import tts_policy_manager
from py.tts_streaming import streaming_manager, stream_audio_chunks

logger = logging.getLogger(__name__)

_tts_manager = None

def set_tts_manager(manager):
    """设置全局 TTS Manager，用于推送音频到前端"""
    global _tts_manager
    _tts_manager = manager


TTS_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "voice_speak",
        "description": """语音合成工具 - 将文本转换为语音播放。

重要使用场景：
1. 回复用户时需要朗读内容
2. 播报重要信息、通知、提醒
3. 讲故事、讲笑话、朗读诗歌
4. 需要口语化表达的场合

使用限制：
- 只朗读纯文本，不要包含 Markdown 符号
- 单次不超过 220 字符
- 代码、表格、长段落在大多数场景下不朗读
- 连续调用需间隔 0.5 秒以上

建议：
- 保持语音内容简洁明了
- 将长文本拆分成多次调用
- 根据语言选择合适的音色""",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "要朗读的纯文本内容（无Markdown，不超过220字符）",
                },
                "voice_id": {
                    "type": "string",
                    "description": "音色ID，如不指定则使用默认音色。常用：xiaoyi(中文女声), jenny(英文女声), nanami(日语女声)",
                    "default": "default"
                },
                "language": {
                    "type": "string",
                    "description": "语言代码",
                    "enum": ["zh", "en", "ja", "ko", "yue"],
                    "default": "zh"
                }
            },
            "required": ["text"]
        }
    }
}


TTS_TOOL_SCHEMA_STREAM = {
    "type": "function",
    "function": {
        "name": "voice_speak_stream",
        "description": """流式语音合成工具 - 实时流式输出音频，适合较长的文本。

使用场景：
- 较长的文本内容（超过220字符）
- 需要实时播放的场合
- 与 VRM/Live2D 口型同步

特点：
- 流式传输，无需等待完整音频
- 支持 WebSocket 实时推送
- 可与其他任务并行执行""",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "要朗读的文本内容",
                },
                "voice_id": {
                    "type": "string",
                    "description": "音色ID",
                    "default": "default"
                },
                "language": {
                    "type": "string",
                    "description": "语言代码",
                    "enum": ["zh", "en", "ja", "ko", "yue"],
                    "default": "zh"
                },
                "session_id": {
                    "type": "string",
                    "description": "流式会话ID，用于追踪和管理",
                    "default": ""
                }
            },
            "required": ["text"]
        }
    }
}


async def get_tts_tool(settings: dict) -> Optional[dict]:
    """
    获取 TTS 工具配置

    Args:
        settings: 应用设置

    Returns:
        TTS 工具 schema 或 None（如果未启用）
    """
    tts_settings = settings.get("ttsSettings", {})
    if not tts_settings.get("enabled", False):
        return None

    return TTS_TOOL_SCHEMA


async def get_tts_tool_stream(settings: dict) -> Optional[dict]:
    """
    获取流式 TTS 工具配置

    Args:
        settings: 应用设置

    Returns:
        流式 TTS 工具 schema 或 None
    """
    tts_settings = settings.get("ttsSettings", {})
    if not tts_settings.get("enabled", False):
        return None

    return TTS_TOOL_SCHEMA_STREAM


async def handle_tts_tool_call(
    tool_name: str,
    tool_params: dict,
    settings: dict
) -> dict:
    """
    处理 TTS 工具调用（后台异步模式）

    立即返回成功，音频在后台异步合成并推送到前端

    Args:
        tool_name: 工具名称
        tool_params: 工具参数 {text, voice_id, language}
        settings: 应用设置

    Returns:
        立即返回成功结果，音频在后台播放
    """
    text = tool_params.get("text", "")
    voice_id = tool_params.get("voice_id", "default")
    language = tool_params.get("language", "zh")

    if not text or not text.strip():
        return {
            "success": False,
            "error": "文本为空"
        }

    session_id = "default"
    policy = tts_policy_manager.get_policy(session_id)
    is_allowed, reason = policy.check(text, voice_id)

    if not is_allowed:
        logger.warning(f"TTS policy blocked: {reason}, text: {text[:50]}...")
        return {
            "success": False,
            "error": f"TTS 被策略拦截: {reason}",
            "policy_info": policy.get_stats()
        }

    async def background_synthesize():
        """后台异步合成音频"""
        tts_settings = settings.get("ttsSettings", {})
        engine = tts_settings.get("engine", "edge")

        try:
            if engine == "edge" or engine == "edgetts":
                audio_iter = tts_adapter.synthesize_stream(
                    text=text,
                    engine="edge",
                    voice_id=voice_id,
                    language=language,
                    rate=tts_settings.get("edgettsRate", "+0%"),
                )
            elif engine == "openai":
                audio_iter = tts_adapter.synthesize_stream(
                    text=text,
                    engine="openai",
                    voice_id=voice_id,
                    language=language,
                    api_key=tts_settings.get("api_key", ""),
                    base_url=tts_settings.get("base_url", "https://api.openai.com/v1"),
                    model=tts_settings.get("model", "tts-1"),
                    speed=tts_settings.get("speed", 1.0),
                )
            elif engine == "dashscope":
                audio_iter = tts_adapter.synthesize_stream(
                    text=text,
                    engine="dashscope",
                    voice_id=voice_id,
                    language=language,
                    api_key=tts_settings.get("api_key", ""),
                    model=tts_settings.get("model", "qwen3-tts-vc-2026-01-22"),
                )
            elif engine == "gsv" or engine == "gpt-sovits":
                audio_iter = tts_adapter.synthesize_stream(
                    text=text,
                    engine="gsv",
                    voice_id=voice_id,
                    language=language,
                    ref_audio_path=tts_settings.get("gsvRefAudioPath", ""),
                    ref_text=tts_settings.get("gsvPromptText", ""),
                    speed_factor=tts_settings.get("gsvRate", 1.0),
                )
            elif engine == "kokoro":
                audio_iter = tts_adapter.synthesize_stream(
                    text=text,
                    engine="kokoro",
                    voice_id=voice_id,
                    language=language,
                    model_path=tts_settings.get("kokoroModelPath", ""),
                    voices_path=tts_settings.get("kokoroVoicesPath", ""),
                    speed=tts_settings.get("speed", 1.0),
                )
<<<<<<< HEAD
            elif engine == "volcengine" or engine == "volcano":
                audio_iter = tts_adapter.synthesize_stream(
                    text=text,
                    engine="volcengine",
                    voice_id=tts_settings.get("volcVoice", "zh_female_vv_uranus_bigtts"),
                    language=language,
                    app_id=tts_settings.get("volcAppId", ""),
                    access_key=tts_settings.get("volcAccessKey", ""),
                    resource_id=tts_settings.get("volcResourceId", "seed-tts-2.0"),
                    speed_ratio=float(tts_settings.get("volcRate", 1.0)),
                )
=======
>>>>>>> b355e12ba0fea89a5e6bda58856669df1b807470
            else:
                audio_iter = tts_adapter.synthesize_stream(
                    text=text,
                    engine="edge",
                    voice_id=voice_id,
                    language=language,
                )

            chunk_index = 0
            total_bytes = 0
            import time

            async for audio_chunk in audio_iter:
                audio_b64 = base64.b64encode(audio_chunk).decode("utf-8")
                total_bytes += len(audio_chunk)

                if _tts_manager is not None:
                    message = json.dumps({
                        "type": "tts_audio",
                        "text": text,
                        "voice_id": voice_id,
                        "language": language,
                        "audio": audio_b64,
                        "format": "mp3",
                        "chunk_index": chunk_index,
                        "is_streaming": True,
                        "timestamp": time.time(),
                    })
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            loop.create_task(_tts_manager.send_to_main(message))
                        else:
                            asyncio.create_task(_tts_manager.send_to_main(message))
                    except Exception as e:
                        logger.error(f"Failed to send TTS audio chunk: {e}")

                chunk_index += 1

            if _tts_manager is not None:
                complete_msg = json.dumps({
                    "type": "tts_complete",
                    "success": True,
                    "text": text,
                    "voice_id": voice_id,
                    "language": language,
                    "total_chunks": chunk_index,
                    "total_bytes": total_bytes,
                })
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        loop.create_task(_tts_manager.send_to_main(complete_msg))
                    else:
                        asyncio.create_task(_tts_manager.send_to_main(complete_msg))
                except Exception as e:
                    logger.error(f"Failed to send TTS complete: {e}")

        except Exception as e:
            logger.error(f"TTS synthesis failed: {e}")
            if _tts_manager is not None:
                error_msg = json.dumps({
                    "type": "tts_error",
                    "error": str(e),
                })
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        loop.create_task(_tts_manager.send_to_main(error_msg))
                    else:
                        asyncio.create_task(_tts_manager.send_to_main(error_msg))
                except Exception:
                    pass

    asyncio.create_task(background_synthesize())

    return {
        "success": True,
        "message": "语音合成已开始，音频将实时推送",
        "text": text,
        "voice_id": voice_id,
        "language": language,
    }


async def handle_tts_stream_tool_call(
    tool_name: str,
    tool_params: dict,
    settings: dict
) -> dict:
    """
    处理流式 TTS 工具调用

    Args:
        tool_name: 工具名称
        tool_params: 工具参数
        settings: 应用设置

    Returns:
        流式会话信息
    """
    text = tool_params.get("text", "")
    voice_id = tool_params.get("voice_id", "default")
    language = tool_params.get("language", "zh")
    stream_session_id = tool_params.get("session_id", "default")

    if not text or not text.strip():
        return {
            "success": False,
            "error": "文本为空"
        }

    policy = tts_policy_manager.get_policy(stream_session_id)
    is_allowed, reason = policy.check(text, voice_id)

    if not is_allowed:
        return {
            "success": False,
            "error": f"TTS 被策略拦截: {reason}"
        }

    return {
        "success": True,
        "session_id": stream_session_id,
        "text": text,
        "voice_id": voice_id,
        "language": language,
        "message": "请使用 /ws/tts/stream/{session_id} WebSocket 端点获取音频流"
    }


async def tts_stream_audio(
    websocket,
    session_id: str,
    text: str,
    voice_id: str,
    language: str,
    settings: dict,
):
    """
    通过 WebSocket 流式传输音频

    Args:
        websocket: WebSocket 连接
        session_id: 会话 ID
        text: 文本内容
        voice_id: 音色 ID
        language: 语言
        settings: 应用设置
    """
    tts_settings = settings.get("ttsSettings", {})
    engine = tts_settings.get("engine", "edge")

    try:
        if engine == "edge" or engine == "edgetts":
            audio_iter = tts_adapter.synthesize_stream(
                text=text,
                engine="edge",
                voice_id=voice_id,
                language=language,
            )
        else:
            audio_data = await tts_adapter.synthesize(
                text=text,
                engine=engine,
                voice_id=voice_id,
                language=language,
            )

            async def audio_chunk_iter():
                chunk_size = 4096
                for i in range(0, len(audio_data), chunk_size):
                    yield audio_data[i:i + chunk_size]

            audio_iter = audio_chunk_iter()

        await stream_audio_chunks(audio_iter, websocket, session_id)

    except Exception as e:
        logger.error(f"流式 TTS 失败: {e}")
        await websocket.send_json({
            "type": "error",
            "session_id": session_id,
            "error": str(e)
        })


def get_all_tts_tools(settings: dict) -> List[dict]:
    """
    获取所有 TTS 工具

    Args:
        settings: 应用设置

    Returns:
        TTS 工具列表
    """
    tools = []

    tts_tool = asyncio.run(get_tts_tool(settings))
    if tts_tool:
        tools.append(tts_tool)

    tts_stream_tool = asyncio.run(get_tts_tool_stream(settings))
    if tts_stream_tool:
        tools.append(tts_stream_tool)

    return tools
