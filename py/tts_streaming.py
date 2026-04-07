"""
TTS 流式播放模块
支持 WebSocket 流式传输、Web Audio API 兼容格式输出
"""

import asyncio
import base64
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import AsyncIterator, Callable, Dict, List, Optional, Set

import websockets

logger = logging.getLogger(__name__)


class StreamFormat(Enum):
    """流式音频格式"""
    PCM = "pcm"
    MP3 = "mp3"
    OPUS = "opus"
    OGG = "ogg"


@dataclass
class AudioChunk:
    """音频分块"""
    data: bytes
    chunk_index: int
    is_final: bool = False
    timestamp: float = 0.0
    text: str = ""


@dataclass
class StreamSession:
    """流式会话"""
    session_id: str
    voice_id: str
    language: str
    format: StreamFormat
    created_at: float
    audio_buffer: List[AudioChunk] = None
    is_active: bool = True

    def __post_init__(self):
        if self.audio_buffer is None:
            self.audio_buffer = []


class AudioStreamerBase(ABC):
    """音频流处理器基类"""

    @abstractmethod
    async def send_chunk(self, chunk: AudioChunk) -> bool:
        """发送音频分块"""
        pass

    @abstractmethod
    async def send_complete(self):
        """发送完成信号"""
        pass

    @abstractmethod
    async def send_error(self, error: str):
        """发送错误信息"""
        pass


class WebSocketAudioStreamer(AudioStreamerBase):
    """WebSocket 音频流处理器"""

    def __init__(self, websocket, session: StreamSession):
        self.websocket = websocket
        self.session = session
        self._sequence = 0

    async def send_chunk(self, chunk: AudioChunk) -> bool:
        """发送音频分块到 WebSocket"""
        try:
            if self.session.format == StreamFormat.PCM:
                audio_b64 = base64.b64encode(chunk.data).decode("utf-8")
            else:
                audio_b64 = base64.b64encode(chunk.data).decode("utf-8")

            message = {
                "type": "audio_chunk",
                "session_id": self.session.session_id,
                "audio": audio_b64,
                "format": self.session.format.value,
                "chunk_index": chunk.chunk_index,
                "is_final": chunk.is_final,
                "timestamp": chunk.timestamp,
                "text": chunk.text,
            }

            await self.websocket.send_json(message)
            self._sequence += 1
            return True

        except Exception as e:
            logger.error(f"发送音频分块失败: {e}")
            return False

    async def send_complete(self):
        """发送完成信号"""
        try:
            await self.websocket.send_json({
                "type": "audio_complete",
                "session_id": self.session.session_id,
                "total_chunks": self._sequence,
            })
        except Exception as e:
            logger.error(f"发送完成信号失败: {e}")

    async def send_error(self, error: str):
        """发送错误信息"""
        try:
            await self.websocket.send_json({
                "type": "error",
                "session_id": self.session.session_id,
                "error": error,
            })
        except Exception as e:
            logger.error(f"发送错误信息失败: {e}")


class DashScopeRealtimeClient:
    """
    阿里云 DashScope 实时语音合成客户端
    支持 WebSocket 流式传输
    """

    def __init__(
        self,
        api_key: str,
        model: str = "qwen3-tts-vc-2026-01-22",
        voice_id: str = "default",
        language: str = "zh-CN",
        sample_rate: int = 24000,
    ):
        self.api_key = api_key
        self.model = model
        self.voice_id = voice_id
        self.language = language
        self.sample_rate = sample_rate
        self._websocket = None
        self._receive_task = None
        self._is_connected = False

    async def connect(self) -> bool:
        """建立 WebSocket 连接"""
        try:
            import dashscope
            dashscope.api_key = self.api_key

            from dashscope.api_entity import DingtalkConfig
            ws_url = DingtalkConfig.get_tts_wss_url()

            self._websocket = await websockets.connect(
                ws_url,
                extra_headers={"Authorization": f"Bearer {self.api_key}"}
            )
            self._is_connected = True

            session_config = {
                "session": {
                    "model": self.model,
                    "voice": self.voice_id,
                    "audio_format": "pcm",
                    "sample_rate": self.sample_rate,
                    "language": self.language,
                }
            }
            await self._websocket.send(json.dumps(session_config))

            return True

        except Exception as e:
            logger.error(f"DashScope WebSocket 连接失败: {e}")
            self._is_connected = False
            return False

    async def synthesize_stream(
        self,
        text: str,
        callback: Callable[[bytes], None],
    ) -> AsyncIterator[AudioChunk]:
        """
        流式合成文本

        Args:
            text: 要合成的文本
            callback: 音频数据回调

        Yields:
            AudioChunk: 音频分块
        """
        if not self._is_connected:
            raise RuntimeError("WebSocket 未连接")

        chunk_index = 0
        try:
            text_message = {
                "input": {
                    "text": text,
                }
            }
            await self._websocket.send(json.dumps(text_message))

            while self._is_connected:
                try:
                    message = await asyncio.wait_for(
                        self._websocket.recv(),
                        timeout=30.0
                    )

                    if isinstance(message, str):
                        data = json.loads(message)

                        if data.get("type") == "response.audio.delta":
                            audio_b64 = data["audio"]["delta"]
                            audio_data = base64.b64decode(audio_b64)

                            chunk = AudioChunk(
                                data=audio_data,
                                chunk_index=chunk_index,
                                is_final=False,
                                timestamp=time.time(),
                                text=text,
                            )
                            chunk_index += 1

                            callback(audio_data)
                            yield chunk

                        elif data.get("type") == "response.audio.done":
                            final_chunk = AudioChunk(
                                data=b"",
                                chunk_index=chunk_index,
                                is_final=True,
                                timestamp=time.time(),
                                text=text,
                            )
                            yield final_chunk
                            break

                        elif data.get("type") == "error":
                            error_msg = data.get("error", {}).get("message", "Unknown error")
                            raise RuntimeError(f"DashScope error: {error_msg}")

                except asyncio.TimeoutError:
                    continue

        except Exception as e:
            logger.error(f"流式合成失败: {e}")
            raise

        finally:
            chunk_index = 0

    async def close(self):
        """关闭连接"""
        self._is_connected = False
        if self._websocket:
            await self._websocket.close()


class TTSServerStreamingManager:
    """
    TTS 服务器端流式管理
    管理多个 WebSocket 连接和流式会话
    """

    def __init__(self):
        self._sessions: Dict[str, StreamSession] = {}
        self._connections: Dict[str, websockets.WebSocketServerProtocol] = {}
        self._streamers: Dict[str, WebSocketAudioStreamer] = {}
        self._active_streams: Dict[str, asyncio.Task] = {}

    async def create_session(
        self,
        websocket: websockets.WebSocketServerProtocol,
        session_id: str,
        voice_id: str = "default",
        language: str = "zh",
        format: str = "pcm",
    ) -> StreamSession:
        """创建新的流式会话"""
        session = StreamSession(
            session_id=session_id,
            voice_id=voice_id,
            language=language,
            format=StreamFormat(format),
            created_at=time.time(),
        )

        self._sessions[session_id] = session
        self._connections[session_id] = websocket
        self._streamers[session_id] = WebSocketAudioStreamer(websocket, session)

        return session

    def get_session(self, session_id: str) -> Optional[StreamSession]:
        """获取会话"""
        return self._sessions.get(session_id)

    def get_streamer(self, session_id: str) -> Optional[WebSocketAudioStreamer]:
        """获取流处理器"""
        return self._streamers.get(session_id)

    async def close_session(self, session_id: str):
        """关闭会话"""
        if session_id in self._active_streams:
            task = self._active_streams[session_id]
            if not task.done():
                task.cancel()
            del self._active_streams[session_id]

        if session_id in self._streamers:
            del self._streamers[session_id]

        if session_id in self._connections:
            del self._connections[session_id]

        if session_id in self._sessions:
            self._sessions[session_id].is_active = False
            del self._sessions[session_id]

    async def broadcast_to_session(
        self,
        session_id: str,
        audio_data: bytes,
        chunk_index: int,
        is_final: bool = False,
        text: str = "",
    ):
        """向指定会话发送音频数据"""
        streamer = self.get_streamer(session_id)
        if not streamer:
            return

        chunk = AudioChunk(
            data=audio_data,
            chunk_index=chunk_index,
            is_final=is_final,
            timestamp=time.time(),
            text=text,
        )

        await streamer.send_chunk(chunk)

        if is_final:
            await streamer.send_complete()

    def get_active_session_count(self) -> int:
        """获取活跃会话数"""
        return len([s for s in self._sessions.values() if s.is_active])

    def get_session_ids(self) -> List[str]:
        """获取所有会话 ID"""
        return list(self._sessions.keys())


streaming_manager = TTSServerStreamingManager()


async def stream_audio_chunks(
    audio_iterator: AsyncIterator[bytes],
    websocket: websockets.WebSocketServerProtocol,
    session_id: str,
    chunk_size: int = 4096,
) -> int:
    """
    通用音频流式传输函数

    Args:
        audio_iterator: 音频数据迭代器
        websocket: WebSocket 连接
        session_id: 会话 ID
        chunk_size: 分块大小

    Returns:
        int: 发送的分块数量
    """
    chunk_index = 0
    total_bytes = 0

    try:
        async for audio_data in audio_iterator:
            if isinstance(audio_data, str):
                audio_data = audio_data.encode("utf-8")

            chunk = AudioChunk(
                data=audio_data,
                chunk_index=chunk_index,
                is_final=False,
                timestamp=time.time(),
            )

            await websocket.send_json({
                "type": "audio_chunk",
                "session_id": session_id,
                "audio": base64.b64encode(audio_data).decode("utf-8"),
                "chunk_index": chunk_index,
                "is_final": False,
                "timestamp": chunk.timestamp,
                "bytes_sent": len(audio_data),
            })

            chunk_index += 1
            total_bytes += len(audio_data)

        await websocket.send_json({
            "type": "audio_complete",
            "session_id": session_id,
            "total_chunks": chunk_index,
            "total_bytes": total_bytes,
        })

    except Exception as e:
        logger.error(f"流式传输失败: {e}")
        await websocket.send_json({
            "type": "error",
            "session_id": session_id,
            "error": str(e),
        })

    return chunk_index
