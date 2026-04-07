"""
TTS 适配器核心模块
支持多种 TTS 引擎，通过统一接口调用
"""

import asyncio
import base64
import io
import logging
from abc import ABC, abstractmethod
from typing import AsyncIterator, Literal, Optional, Tuple

logger = logging.getLogger(__name__)


class TTSEngineBase(ABC):
    """TTS 引擎基类"""

    @abstractmethod
    async def synthesize(
        self,
        text: str,
        voice_id: str = "default",
        language: str = "zh",
        **kwargs
    ) -> bytes:
        """同步合成音频，返回原始音频数据"""
        pass

    async def synthesize_stream(
        self,
        text: str,
        voice_id: str = "default",
        language: str = "zh",
        **kwargs
    ) -> AsyncIterator[bytes]:
        """流式合成音频，分块返回"""
        audio = await self.synthesize(text, voice_id, language, **kwargs)
        chunk_size = kwargs.get("chunk_size", 4096)
        for i in range(0, len(audio), chunk_size):
            yield audio[i:i + chunk_size]


class EdgeTTSEngine(TTSEngineBase):
    """微软 EdgeTTS 引擎"""

    def __init__(self):
        self._communicate = None

    async def synthesize(
        self,
        text: str,
        voice_id: str = "default",
        language: str = "zh",
        **kwargs
    ) -> bytes:
        try:
            import edge_tts
        except ImportError:
            raise RuntimeError("edge-tts 库未安装，请运行: pip install edge-tts")

        rate = kwargs.get("rate", "+0%")
        pitch = kwargs.get("pitch", "+0Hz")

        voice_map = {
            "default": f"{language}-XiaoyiNeural",
            "zh-CN-XiaoxiaoNeural": "zh-CN-XiaoxiaoNeural",
            "zh-CN-YunxiNeural": "zh-CN-YunxiNeural",
            "zh-CN-YunyangNeural": "zh-CN-YunyangNeural",
            "ja-JP-NanamiNeural": "ja-JP-NanamiNeural",
            "en-US-JennyNeural": "en-US-JennyNeural",
            "en-US-GuyNeural": "en-US-GuyNeural",
        }

        full_voice = voice_map.get(voice_id, voice_id)
        if voice_id == "default" and language == "zh":
            full_voice = "zh-CN-XiaoyiNeural"
        elif voice_id == "default" and language == "en":
            full_voice = "en-US-JennyNeural"
        elif voice_id == "default":
            full_voice = f"{language}-XiaoyiNeural"

        communicate = edge_tts.Communicate(text, full_voice, rate=rate, pitch=pitch)

        audio_chunks = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_chunks.append(chunk["data"])

        return b"".join(audio_chunks)


class OpenAITTSEngine(TTSEngineBase):
    """OpenAI TTS 引擎"""

    def __init__(self, api_key: str = "", base_url: str = "https://api.openai.com/v1"):
        self.api_key = api_key
        self.base_url = base_url
        self._client = None

    async def _get_client(self):
        if self._client is None:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)
        return self._client

    async def synthesize(
        self,
        text: str,
        voice_id: str = "default",
        language: str = "zh",
        **kwargs
    ) -> bytes:
        client = await self._get_client()

        voice = voice_id if voice_id != "default" else "alloy"
        model = kwargs.get("model", "tts-1")
        speed = kwargs.get("speed", 1.0)
        response_format = kwargs.get("response_format", "mp3")

        response = await client.audio.speech.create(
            model=model,
            voice=voice,
            input=text,
            speed=max(0.25, min(4.0, speed)),
            response_format=response_format
        )
        return await response.aread()


class KokoroTTSEngine(TTSEngineBase):
    """Kokoro-ONNX 开源 TTS 引擎"""

    def __init__(self, model_path: str = "", voices_path: str = ""):
        self.model_path = model_path
        self.voices_path = voices_path
        self._model = None
        self._pipeline = None

    async def synthesize(
        self,
        text: str,
        voice_id: str = "default",
        language: str = "zh",
        **kwargs
    ) -> bytes:
        try:
            from kokoro import KPipeline
        except ImportError:
            raise RuntimeError("kokoro 库未安装，请参考: https://github.com/thewh1teagle/kokoro-onnx")

        if self._pipeline is None:
            lang = "zh" if language in ["zh", "yue"] else language
            self._pipeline = KPipeline(
                model_path=self.model_path,
                voices_path=self.voices_path,
                lang=lang
            )

        generator = self._pipeline(
            text,
            voice=voice_id if voice_id != "default" else "default",
            speed=kwargs.get("speed", 1.0)
        )

        audio_samples = []
        for _, audio in generator:
            audio_samples.append(audio)

        if audio_samples:
            import numpy as np
            full_audio = np.concatenate(audio_samples)
            return (full_audio * 32767).astype(np.int16).tobytes()

        return b""


class DashScopeTTSEngine(TTSEngineBase):
    """阿里云 DashScope TTS 引擎"""

    def __init__(self, api_key: str = ""):
        self.api_key = api_key
        self._client = None

    async def synthesize(
        self,
        text: str,
        voice_id: str = "default",
        language: str = "zh",
        **kwargs
    ) -> bytes:
        try:
            import dashscope
            from dashscope.audio.tts_v3 import SpeechSynthesizer
        except ImportError:
            raise RuntimeError("dashscope 库未安装，请运行: pip install dashscope")

        dashscope.api_key = self.api_key

        model = kwargs.get("model", "qwen3-tts-vc-2026-01-22")

        lang_map = {
            "zh": "zh-CN",
            "en": "en-US",
            "ja": "jp",
            "ko": "spa",
            "yue": "yue-CN"
        }

        result = SpeechSynthesizer.call(
            model=model,
            voice=voice_id if voice_id != "default" else None,
            text=text,
            language_type=lang_map.get(language, "zh-CN")
        )

        if result.output.get("audio", {}).get("url"):
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.get(result.output["audio"]["url"], timeout=30.0)
                resp.raise_for_status()
                return resp.content

        return b""


class GSVTTTSEngine(TTSEngineBase):
    """GPT-SoVITS TTS 引擎"""

    def __init__(self, server_url: str = "http://127.0.0.1:9880"):
        self.server_url = server_url

    async def synthesize(
        self,
        text: str,
        voice_id: str = "default",
        language: str = "zh",
        **kwargs
    ) -> bytes:
        import httpx

        ref_audio_path = kwargs.get("ref_audio_path", "")
        ref_text = kwargs.get("ref_text", "")
        speed_factor = kwargs.get("speed_factor", 1.0)

        params = {
            "text": text,
            "text_lang": language,
            "ref_audio_path": ref_audio_path,
            "prompt_lang": language,
            "prompt_text": ref_text,
            "speed_factor": speed_factor,
            "streaming_mode": False,
            "media_type": "ogg"
        }

        safe_url = f"{self.server_url.rstrip('/')}/tts"

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(safe_url, json=params)
            response.raise_for_status()
            return response.content


<<<<<<< HEAD
class VolcengineTTSEngine(TTSEngineBase):
    """火山引擎 Volcengine TTS 引擎"""
    
    # 默认音色配置
    DEFAULT_VOICE = "zh_female_vv_uranus_bigtts"

    def __init__(
        self,
        app_id: str = "",
        access_key: str = "",
        resource_id: str = "seed-tts-2.0"
    ):
        self.app_id = app_id
        self.access_key = access_key
        self.resource_id = resource_id
        
        # 验证必要参数
        if not self.app_id or not self.access_key:
            raise ValueError("火山引擎TTS需要app_id和access_key参数")

    async def synthesize(
        self,
        text: str,
        voice_id: str = "default",
        language: str = "zh",
        **kwargs
    ) -> bytes:
        import httpx
        import json
        import base64

        voice = voice_id if voice_id != "default" else self.DEFAULT_VOICE
        speed_ratio = float(kwargs.get("speed_ratio", 1.0))
        audio_format = kwargs.get("format", "mp3")
        sample_rate = kwargs.get("sample_rate", 24000)

        url = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
        headers = {
            "X-Api-App-Id": self.app_id,
            "X-Api-Access-Key": self.access_key,
            "X-Api-Resource-Id": self.resource_id,
            "Content-Type": "application/json"
        }
        payload = {
            "user": {"uid": "123456"},
            "req_params": {
                "text": text,
                "speaker": voice,
                "speed_ratio": speed_ratio,
                "audio_params": {"format": audio_format, "sample_rate": sample_rate},
                "additions": json.dumps({"disable_markdown_filter": True})
            }
        }

        collected_audio = bytearray()
        
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        if data.get("code", 0) not in [0, 20000000]:
                            logger.error(f'火山引擎TTS错误: {data}')
                            raise Exception(f'TTS合成失败: {data.get("message", "未知错误")}')
                        if "data" in data and data["data"]:
                            chunk_audio = base64.b64decode(data["data"])
                            collected_audio.extend(chunk_audio)
                    except json.JSONDecodeError:
                        continue

        return bytes(collected_audio)


=======
>>>>>>> b355e12ba0fea89a5e6bda58856669df1b807470
class TTSAdapter:
    """
    TTS 适配器主类
    统一管理多种 TTS 引擎，提供一致接口
    """

    ENGINE_MAPPING = {
        "edge": EdgeTTSEngine,
        "edgetts": EdgeTTSEngine,
        "openai": OpenAITTSEngine,
        "kokoro": KokoroTTSEngine,
        "dashscope": DashScopeTTSEngine,
        "gsv": GSVTTTSEngine,
        "gpt-sovits": GSVTTTSEngine,
<<<<<<< HEAD
        "volcengine": VolcengineTTSEngine,
        "volcano": VolcengineTTSEngine,
=======
>>>>>>> b355e12ba0fea89a5e6bda58856669df1b807470
    }

    def __init__(self, default_engine: str = "edge", **default_kwargs):
        self.default_engine = default_engine
        self.default_kwargs = default_kwargs
        self._engines = {}
        self._streaming_engines = {}

    def get_engine(self, engine: str = "edge", **kwargs) -> TTSEngineBase:
        """获取或创建 TTS 引擎实例"""
        if engine not in self.ENGINE_MAPPING:
            raise ValueError(f"不支持的 TTS 引擎: {engine}，支持的引擎: {list(self.ENGINE_MAPPING.keys())}")

        cache_key = (engine, tuple(sorted(kwargs.items())))
        if cache_key not in self._engines:
            engine_class = self.ENGINE_MAPPING[engine]
            merged_kwargs = {**self.default_kwargs, **kwargs}
            self._engines[cache_key] = engine_class(**merged_kwargs)

        return self._engines[cache_key]

    async def synthesize(
        self,
        text: str,
        engine: str = "edge",
        voice_id: str = "default",
        language: str = "zh",
        **kwargs
    ) -> bytes:
        """
        合成音频（同步模式）

        Args:
            text: 要合成的文本
            engine: TTS 引擎类型
            voice_id: 音色 ID
            language: 语言代码 (zh/en/ja/ko/yue)
            **kwargs: 引擎特定参数

        Returns:
            bytes: 音频数据
        """
        engine_obj = self.get_engine(engine, **kwargs)
        return await engine_obj.synthesize(text, voice_id, language, **kwargs)

    async def synthesize_to_opus(
        self,
        text: str,
        engine: str = "edge",
        voice_id: str = "default",
        language: str = "zh",
        **kwargs
    ) -> bytes:
        """合成音频并转换为 opus 格式"""
        from py.get_setting import convert_to_opus_simple

        audio_data = await self.synthesize(text, engine, voice_id, language, **kwargs)

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            convert_to_opus_simple,
            audio_data
        )

        if isinstance(result, tuple):
            return result[0]
        return result

    async def synthesize_stream(
        self,
        text: str,
        engine: str = "edge",
        voice_id: str = "default",
        language: str = "zh",
        **kwargs
    ) -> AsyncIterator[bytes]:
        """
        流式合成音频

        Args:
            text: 要合成的文本
            engine: TTS 引擎类型
            voice_id: 音色 ID
            language: 语言代码
            **kwargs: 引擎特定参数

        Yields:
            bytes: 音频数据分块
        """
        engine_obj = self.get_engine(engine, **kwargs)

        if hasattr(engine_obj, "synthesize_stream"):
            async for chunk in engine_obj.synthesize_stream(text, voice_id, language, **kwargs):
                yield chunk
        else:
            audio = await engine_obj.synthesize(text, voice_id, language, **kwargs)
            chunk_size = kwargs.get("chunk_size", 4096)
            for i in range(0, len(audio), chunk_size):
                yield audio[i:i + chunk_size]

    def register_engine(self, name: str, engine_class: type):
        """注册新的 TTS 引擎"""
        if not issubclass(engine_class, TTSEngineBase):
            raise TypeError("引擎类必须继承自 TTSEngineBase")
        self.ENGINE_MAPPING[name] = engine_class


tts_adapter = TTSAdapter()
