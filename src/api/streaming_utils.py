#!/usr/bin/env python3
"""
Streaming Event Emitter — 实时流式事件推送基础设施
=============================================
为 SSE (Server-Sent Events) 提供结构化的事件发射接口。

SSE 格式：
    event: <event_type>\n
    data: <json_payload>\n\n

支持的事件类型：
    phase      — 任务阶段开始（start, l2_retrieval, l3_retrieval, iteration, complete）
    event      — 单个原子事件（cot_generated, code_extracted, compile_result, etc.）
    error      — 错误
    done       — 最终结果
"""

from __future__ import annotations

import json
import time
import threading
from dataclasses import dataclass, field, asdict
from typing import Optional, Callable, Generator, Dict, Any, List
from enum import Enum
from queue import Queue, Empty


class EventType(str, Enum):
    """流式事件类型"""
    # 阶段事件
    PHASE           = "phase"
    # 原子事件
    COT_GENERATED   = "cot_generated"
    CODE_EXTRACTED  = "code_extracted"
    COMPILE_RESULT  = "compile_result"
    LARGE_MODEL_FEEDBACK = "large_model_feedback"
    RUFF_CHECK      = "ruff_check"
    ITERATION_START = "iteration_start"
    ITERATION_END    = "iteration_end"
    MEMORY_HIT       = "memory_hit"
    # 完成事件
    DONE            = "done"
    ERROR           = "error"
    HEARTBEAT       = "heartbeat"


@dataclass
class StreamEvent:
    """流式事件"""
    type: str           # EventType value
    iteration: int = 0  # 当前迭代轮次
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    seq: int = 0        # 递增序号

    def to_sse(self) -> str:
        """序列化为 SSE 格式"""
        lines = [
            f"event: {self.type}",
            f"data: {json.dumps(self.data, ensure_ascii=False)}",
            "",  # SSE 消息以空行结束
        ]
        return "\n".join(lines)

    def to_dict(self) -> Dict:
        d = asdict(self)
        d["type"] = self.type
        return d


class StreamingEmitter:
    """
    线程安全的流式事件发射器。

    用法：
        emitter = StreamingEmitter()
        # 后台线程生成事件
        def worker():
            emitter.emit("phase", {"phase": "start"})
            emitter.emit("cot_generated", {"cot": "...", "iteration": 1})
            emitter.finish()

        # 前端通过 generator 消费
        for event in emitter.stream():
            print(event.type, event.data)
    """

    def __init__(self, queue_size: int = 200):
        self._queue: Queue = Queue(maxsize=queue_size)
        self._done = threading.Event()
        self._done.clear()
        self._seq = 0
        self._lock = threading.Lock()

    def emit(
        self,
        event_type: str,
        data: Dict[str, Any],
        iteration: int = 0,
    ) -> StreamEvent:
        """发射一个事件（线程安全）"""
        with self._lock:
            self._seq += 1
            event = StreamEvent(
                type=event_type,
                iteration=iteration,
                data=data,
                timestamp=time.time(),
                seq=self._seq,
            )
        try:
            self._queue.put_nowait(event)
        except Exception:
            # 队列满时丢弃最旧的事件
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(event)
            except Exception:
                pass
        return event

    def emit_sse(self, event_type: str, data: Dict[str, Any], iteration: int = 0) -> str:
        """发射并直接返回 SSE 字符串"""
        event = self.emit(event_type, data, iteration)
        return event.to_sse()

    def finish(self):
        """标记流结束"""
        self._done.set()
        self._queue.put(None)  # 哨兵

    def stream(self) -> Generator[StreamEvent, None, None]:
        """生成器：持续 yield 事件直到 finish()"""
        while not self._done.is_set() or not self._queue.empty():
            try:
                event = self._queue.get(timeout=0.5)
                if event is None:
                    break
                yield event
            except Empty:
                # 超时但未结束 → 发心跳
                with self._lock:
                    self._seq += 1
                    yield StreamEvent(
                        type=EventType.HEARTBEAT.value,
                        data={"ts": time.time()},
                        seq=self._seq,
                    )
                continue
        # 消费剩余事件
        while True:
            try:
                event = self._queue.get_nowait()
                if event is None:
                    break
                yield event
            except Empty:
                break

    def sse_stream(self) -> Generator[str, None, None]:
        """生成器：持续 yield SSE 字符串直到 finish()"""
        for event in self.stream():
            if event.type == EventType.HEARTBEAT.value:
                continue  # 心跳不发给前端
            yield event.to_sse()


# ==================== 便捷发射函数 ====================
# 这些函数直接创建 SSE 格式字符串，便于在 API 层使用

def sse_event(event_type: str, data: Dict[str, Any], iteration: int = 0) -> str:
    """直接创建 SSE 字符串（无需实例化）"""
    with threading.Lock():
        seq = int(time.time() * 1000)  # 用时间戳做简单序号
    event = StreamEvent(type=event_type, data=data, iteration=iteration, seq=seq)
    return event.to_sse()


def sse_phase(phase: str, iteration: int = 0, **kwargs) -> str:
    """发射阶段事件"""
    return sse_event(EventType.PHASE.value, {"phase": phase, **kwargs}, iteration)


def sse_done(result: Dict[str, Any]) -> str:
    """发射最终结果"""
    return sse_event(EventType.DONE.value, result)


def sse_error(message: str, iteration: int = 0) -> str:
    """发射错误"""
    return sse_event(EventType.ERROR.value, {"message": message}, iteration)


def sse_heartbeat() -> str:
    """发射心跳（keep-alive）"""
    return f"event: {EventType.HEARTBEAT.value}\ndata: {{}}\n\n"
