# modules/memory.py

import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional
from pydantic import BaseModel


# Optional fallback logger
try:
    from agent import log
except ImportError:
    import datetime
    def log(stage: str, msg: str):
        now = datetime.datetime.now().strftime("%H:%M:%S")
        print(f"[{now}] [{stage}] {msg}")

class MemoryItem(BaseModel):
    """Represents a single memory entry for a session."""
    timestamp: float
    type: str  # run_metadata, tool_call, tool_output, final_answer
    text: str
    tool_name: Optional[str] = None
    tool_args: Optional[dict] = None
    tool_result: Optional[dict] = None
    final_answer: Optional[str] = None
    tags: Optional[List[str]] = []
    success: Optional[bool] = None
    metadata: Optional[Dict] = None  # ✅ ADD THIS LINE BACK


class MemoryManager:
    """Manages session memory (read/write/append)."""

    def __init__(self, session_id: str, memory_dir: str = "memory"):
        self.session_id = session_id
        self.memory_dir = memory_dir
        self.memory_path = os.path.join('memory', session_id.split('-')[0], session_id.split('-')[1], session_id.split('-')[2], f'session-{session_id}.json')
        self.items: List[MemoryItem] = []

        if not os.path.exists(self.memory_dir):
            os.makedirs(self.memory_dir)

        self.load()

    def load(self):
        if os.path.exists(self.memory_path):
            with open(self.memory_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
                self.items = [MemoryItem(**item) for item in raw]
        else:
            self.items = []

    def save(self):
        # Before opening the file for writing
        os.makedirs(os.path.dirname(self.memory_path), exist_ok=True)
        with open(self.memory_path, "w", encoding="utf-8") as f:
            raw = [item.dict() for item in self.items]
            json.dump(raw, f, indent=2)

    def add(self, item: MemoryItem):
        self.items.append(item)
        self.save()

    def add_tool_call(
        self, tool_name: str, tool_args: dict, tags: Optional[List[str]] = None
    ):
        item = MemoryItem(
            timestamp=time.time(),
            type="tool_call",
            text=f"Called {tool_name} with {tool_args}",
            tool_name=tool_name,
            tool_args=tool_args,
            tags=tags or [],
        )
        self.add(item)

    def add_tool_output(
        self, tool_name: str, tool_args: dict, tool_result: dict, success: bool, tags: Optional[List[str]] = None
    ):
        item = MemoryItem(
            timestamp=time.time(),
            type="tool_output",
            text=f"Output of {tool_name}: {tool_result}",
            tool_name=tool_name,
            tool_args=tool_args,
            tool_result=tool_result,
            success=success,  # 🆕 Track success!
            tags=tags or [],
        )
        self.add(item)

    def add_final_answer(self, text: str, question: Optional[str] = None):
        item = MemoryItem(
            timestamp=time.time(),
            type="final_answer",
            text=text,
            final_answer=text,
            metadata={"question": question} if question else None,
        )
        self.add(item)

    def find_recent_successes(self, limit: int = 5) -> List[str]:
        """Find tool names which succeeded recently."""
        tool_successes = []

        # Search from newest to oldest
        for item in reversed(self.items):
            if item.type == "tool_output" and item.success:
                if item.tool_name and item.tool_name not in tool_successes:
                    tool_successes.append(item.tool_name)
            if len(tool_successes) >= limit:
                break

        return tool_successes

    def add_tool_success(self, tool_name: str, success: bool):
        """Patch last tool call or output for a given tool with success=True/False."""

        # Search backwards for latest matching tool call/output
        for item in reversed(self.items):
            if item.tool_name == tool_name and item.type in {"tool_call", "tool_output"}:
                item.success = success
                log("memory", f"✅ Marked {tool_name} as success={success}")
                self.save()
                return

        log("memory", f"⚠️ Tried to mark {tool_name} as success={success} but no matching memory found.")

    def get_session_items(self) -> List[MemoryItem]:
        """
        Return all memory items for current session.
        """
        return self.items


class QAMemory:
    """Global cache of recent Q&A pairs for instant responses."""

    def __init__(self, memory_dir: str = "memory", limit: int = 10):
        self.memory_dir = memory_dir
        self.limit = max(1, limit)
        self.cache_path = os.path.join(self.memory_dir, "qa_cache.json")
        if not os.path.exists(self.memory_dir):
            os.makedirs(self.memory_dir)
        self.entries: List[dict] = []
        self.load()

    def load(self):
        if os.path.exists(self.cache_path):
            with open(self.cache_path, "r", encoding="utf-8") as f:
                try:
                    self.entries = json.load(f)
                except json.JSONDecodeError:
                    self.entries = []
        else:
            self.entries = []

    def save(self):
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump(self.entries[-self.limit :], f, indent=2)

    @staticmethod
    def _normalize(question: str) -> str:
        return " ".join(question.strip().lower().split())

    def lookup(self, question: str) -> Optional[str]:
        normalized = self._normalize(question)
        for entry in reversed(self.entries):
            if entry.get("normalized_question") == normalized:
                return entry.get("answer")
        return None

    def add(self, question: str, answer: str):
        normalized = self._normalize(question)
        timestamp = time.time()
        # Remove previous instances of the same normalized question
        self.entries = [
            entry for entry in self.entries
            if entry.get("normalized_question") != normalized
        ]
        self.entries.append(
            {
                "question": question,
                "normalized_question": normalized,
                "answer": answer,
                "timestamp": timestamp,
            }
        )
        if len(self.entries) > self.limit:
            self.entries = self.entries[-self.limit :]
        self.save()


class QAHistoricIndex:
    """Persistent index of historical Q&A pairs loaded from memory logs."""

    def __init__(self, memory_dir: str = "memory", index_filename: str = "qa_index.json", limit: int = 200):
        self.memory_dir = Path(memory_dir)
        self.limit = max(1, limit)
        self.index_path = self.memory_dir / index_filename
        self.entries: Dict[str, Dict] = {}
        if not self.memory_dir.exists():
            self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.load()

    def load(self):
        if self.index_path.exists():
            try:
                with open(self.index_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        self.entries = data
            except json.JSONDecodeError:
                self.entries = {}
        else:
            self.entries = {}

    def save(self):
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.index_path, "w", encoding="utf-8") as f:
            json.dump(self.entries, f, indent=2)

    @staticmethod
    def _normalize(question: str) -> str:
        return " ".join(question.strip().lower().split())

    def lookup(self, question: str) -> Optional[str]:
        normalized = self._normalize(question)
        entry = self.entries.get(normalized)
        if entry:
            return entry.get("answer")
        return None

    def _truncate(self):
        if len(self.entries) <= self.limit:
            return
        sorted_items = sorted(
            self.entries.items(),
            key=lambda item: item[1].get("timestamp", 0)
        )
        self.entries = dict(sorted_items[-self.limit :])

    def add(self, question: str, answer: str, timestamp: Optional[float] = None):
        normalized = self._normalize(question)
        self.entries[normalized] = {
            "question": question,
            "answer": answer,
            "timestamp": timestamp or time.time(),
        }
        self._truncate()
        self.save()

    def refresh(self):
        aggregated: Dict[str, Dict] = {}
        if not self.memory_dir.exists():
            return

        for session_path in self.memory_dir.rglob("session-*.json"):
            try:
                with open(session_path, "r", encoding="utf-8") as f:
                    session_items = json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                continue

            for item in session_items:
                if item.get("type") != "final_answer":
                    continue
                metadata = item.get("metadata") or {}
                question = metadata.get("question")
                answer = item.get("final_answer") or item.get("text")
                if not question or not answer:
                    continue
                normalized = self._normalize(question)
                timestamp = item.get("timestamp", time.time())
                aggregated[normalized] = {
                    "question": question,
                    "answer": answer,
                    "timestamp": timestamp,
                }

        if aggregated:
            self.entries.update(aggregated)
            self._truncate()
            self.save()
