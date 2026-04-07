"""
TTS Policy 检查层
负责内容过滤、频率限制、冷却时间等保护机制
"""

import time
import re
from dataclasses import dataclass, field
from typing import List, Optional, Set
from functools import wraps


@dataclass
class TTSPolicyConfig:
    """TTS 策略配置"""
    max_chars: int = 220
    min_chars: int = 1
    cooldown_seconds: float = 0.5
    max_per_minute: int = 10
    max_per_hour: int = 100
    blocked_patterns: List[str] = field(default_factory=lambda: [
        r"```[\s\S]*?```",
        r"```python[\s\S]*?```",
        r"```javascript[\s\S]*?```",
        r"```html[\s\S]*?```",
        r"```css[\s\S]*?```",
        r"```json[\s\S]*?```",
        r"```yaml[\s\S]*?```",
        r"```sql[\s\S]*?```",
        r"\|.*\|.*\|",
        r"^\s*[-*]\s.*\|",
        r"^\s*\d+\.\s.*\|",
        r"```bash[\s\S]*?```",
        r"```shell[\s\S]*?```",
        r"`{3,}",
        r"^\s*#\s+\w+\s*=\s*",
        r"function\s+\w+\s*\(",
        r"def\s+\w+\s*\(",
        r"class\s+\w+\s*[:\{]",
        r"import\s+\w+",
        r"from\s+\w+\s+import",
        r"<\w+[^>]*>[\s\S]*?</\w+>",
        r"{\w+\s*:\s*\w+}",
        r"\[\d+\s+\d+\s+\d+\]",
    ])
    allow_live2d: bool = True
    allow_markdown: bool = False


@dataclass
class TTSPolicyStats:
    """TTS 策略统计"""
    total_calls: int = 0
    blocked_calls: int = 0
    last_call_time: float = 0.0
    call_timestamps: List[float] = field(default_factory=list)
    blocked_history: List[str] = field(default_factory=list)


class TTSPolicy:
    """
    TTS Policy 保护层

    检查项：
    1. 文本长度限制
    2. 频率限制（每分钟/每小时）
    3. 内容模式过滤（代码、表格等）
    4. 冷却时间
    5. 重复文本检测
    """

    def __init__(self, config: Optional[TTSPolicyConfig] = None):
        self.config = config or TTSPolicyConfig()
        self.stats = TTSPolicyStats()
        self._blocked_set: Set[str] = set()

    def check(self, text: str, voice_id: str = "default") -> tuple[bool, Optional[str]]:
        """
        检查文本是否可以通过 TTS 策略

        Args:
            text: 要检查的文本
            voice_id: 音色 ID

        Returns:
            (is_allowed, reason): 是否允许，失败原因
        """
        if not text or not text.strip():
            return False, "文本为空"

        text_stripped = text.strip()

        if len(text_stripped) < self.config.min_chars:
            return False, f"文本太短（最少 {self.config.min_chars} 字符）"

        if len(text_stripped) > self.config.max_chars:
            return False, f"文本超长（最多 {self.config.max_chars} 字符，当前 {len(text_stripped)}）"

        if not self._check_cooldown():
            return False, f"冷却中（需等待 {self.config.cooldown_seconds} 秒）"

        if not self._check_frequency():
            return False, f"频率超限（每分钟最多 {self.config.max_per_minute} 次）"

        pattern_result = self._check_patterns(text_stripped)
        if not pattern_result[0]:
            return pattern_result

        if not self._check_repetition(text_stripped):
            return False, "重复文本"

        self._record_call()
        return True, None

    def _check_cooldown(self) -> bool:
        """检查冷却时间"""
        current_time = time.time()
        elapsed = current_time - self.stats.last_call_time
        return elapsed >= self.config.cooldown_seconds

    def _check_frequency(self) -> bool:
        """检查频率限制"""
        current_time = time.time()
        one_minute_ago = current_time - 60
        one_hour_ago = current_time - 3600

        recent_calls = [t for t in self.stats.call_timestamps if t > one_minute_ago]
        hour_calls = [t for t in self.stats.call_timestamps if t > one_hour_ago]

        if len(recent_calls) >= self.config.max_per_minute:
            return False
        if len(hour_calls) >= self.config.max_per_hour:
            return False

        return True

    def _check_patterns(self, text: str) -> tuple[bool, Optional[str]]:
        """检查内容模式"""
        for pattern in self.config.blocked_patterns:
            if re.search(pattern, text, re.MULTILINE | re.IGNORECASE):
                self._record_blocked(text, f"模式匹配: {pattern[:30]}...")
                return False, f"内容包含禁止模式（代码/表格/特殊格式）"

        if not self.config.allow_markdown:
            md_patterns = [
                r"\*\*.+?\*\*",
                r"\*.+?\*",
                r"__.+?__",
                r"_.+?_",
                r"~~.+?~~",
                r"\[.+?\]\(.+?\)",
                r"!\[\]\(.+?\)",
                r"^#+\s",
                r"^>\s",
                r"^\s*[-*+]\s",
                r"^\s*\d+\.\s",
            ]
            for pattern in md_patterns:
                if re.search(pattern, text, re.MULTILINE):
                    self._record_blocked(text, f"Markdown 模式: {pattern}")
                    return False, "内容包含 Markdown 格式"

        return True, None

    def _check_repetition(self, text: str) -> bool:
        """检查重复文本"""
        if text in self._blocked_set:
            return False

        similarity_threshold = 0.8
        for cached in list(self._blocked_set)[:10]:
            if self._calculate_similarity(text, cached) > similarity_threshold:
                return False

        return True

    def _calculate_similarity(self, text1: str, text2: str) -> float:
        """计算两段文本的相似度"""
        if text1 == text2:
            return 1.0

        len1, len2 = len(text1), len(text2)
        if len1 == 0 or len2 == 0:
            return 0.0

        max_len = max(len1, len2)
        distance = abs(len1 - len2) / max_len

        min_len = min(len1, len2)
        for i in range(min_len):
            if text1[i] != text2[i]:
                distance += 1

        return 1.0 - (distance / max_len)

    def _record_call(self):
        """记录一次成功调用"""
        current_time = time.time()
        self.stats.total_calls += 1
        self.stats.last_call_time = current_time
        self.stats.call_timestamps.append(current_time)

        one_hour_ago = current_time - 3600
        self.stats.call_timestamps = [
            t for t in self.stats.call_timestamps if t > one_hour_ago
        ]

        self._blocked_set.add(self.stats.call_timestamps[-1] if self.stats.call_timestamps else "")

    def _record_blocked(self, text: str, reason: str):
        """记录一次被拦截的调用"""
        self.stats.blocked_calls += 1
        preview = text[:50] + "..." if len(text) > 50 else text
        self.stats.blocked_history.append(f"[{time.strftime('%H:%M:%S')}] {reason}: {preview}")

        if len(self.stats.blocked_history) > 20:
            self.stats.blocked_history = self.stats.blocked_history[-20:]

    def reset(self):
        """重置策略状态"""
        self.stats = TTSPolicyStats()
        self._blocked_set.clear()

    def get_stats(self) -> dict:
        """获取策略统计信息"""
        current_time = time.time()
        one_minute_ago = current_time - 60

        recent_calls = len([
            t for t in self.stats.call_timestamps if t > one_minute_ago
        ])

        return {
            "total_calls": self.stats.total_calls,
            "blocked_calls": self.stats.blocked_calls,
            "calls_last_minute": recent_calls,
            "max_per_minute": self.config.max_per_minute,
            "cooldown_remaining": max(
                0,
                self.config.cooldown_seconds - (current_time - self.stats.last_call_time)
            ),
            "last_call_time": self.stats.last_call_time,
        }


class TTSPolicyManager:
    """TTS 策略管理器（支持多用户/多会话）"""

    def __init__(self):
        self._policies = {}

    def get_policy(self, session_id: str = "default") -> TTSPolicy:
        """获取指定会话的策略"""
        if session_id not in self._policies:
            self._policies[session_id] = TTSPolicy()
        return self._policies[session_id]

    def check(
        self,
        text: str,
        session_id: str = "default",
        voice_id: str = "default"
    ) -> tuple[bool, Optional[str]]:
        """检查文本是否可以通过"""
        policy = self.get_policy(session_id)
        return policy.check(text, voice_id)

    def reset_session(self, session_id: str):
        """重置指定会话的策略"""
        if session_id in self._policies:
            self._policies[session_id].reset()

    def cleanup_old_sessions(self, max_age_seconds: int = 3600):
        """清理旧会话"""
        current_time = time.time()
        stale_sessions = [
            sid for sid, policy in self._policies.items()
            if current_time - policy.stats.last_call_time > max_age_seconds
        ]
        for sid in stale_sessions:
            del self._policies[sid]


tts_policy_manager = TTSPolicyManager()


def with_policy_check(session_id: str = "default"):
    """装饰器：为 TTS 函数添加策略检查"""
    def decorator(func):
        @wraps(func)
        async def wrapper(text: str, *args, **kwargs):
            policy = tts_policy_manager.get_policy(session_id)
            is_allowed, reason = policy.check(text)

            if not is_allowed:
                raise PermissionError(f"TTS 被策略拦截: {reason}")

            return await func(text, *args, **kwargs)
        return wrapper
    return decorator
