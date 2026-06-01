"""
v2: Multi-user 상태 저장소.

- 사용자별로 별도 링버퍼(deque)와 통계를 관리한다.
- 모든 사용자의 패킷은 단일 session.jsonl과 event_log.csv에 기록되지만
  각 레코드에 user_id가 포함되어 대시보드가 필터링할 수 있다.
- 사용자별 latch 상태(EMERGENCY_FALL 등)를 보관한다.
- user_action_version을 기록해서 시계 페이지의 'I'm OK' 버튼이
  새로운 액션을 보냈는지 receiver가 감지할 수 있다.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

import csv
import json
import threading
from collections import deque

from config import (
    RING_BUFFER_SIZE,
    SESSION_FILE,
    EVENT_LOG_FILE,
    DATA_DIR,
)


class _UserSlot:
    """단일 사용자의 상태."""

    def __init__(self, user_id: str):
        self.user_id            = user_id
        self.buf: deque         = deque(maxlen=RING_BUFFER_SIZE)
        self.last_seq: int      = -1
        self.last_pkt: dict     = {}
        self.latched_event      = None    # "EMERGENCY_FALL" | "DEVICE_THROWN_OR_DROPPED" | None
        self.last_scenario      = None    # 직전 사이클의 active_scenario — 자동 모드 전환 감지용
        self.prev_display       = "NORMAL"
        self.last_action_ver    = 0       # 마지막으로 처리한 user_action_version
        self.last_scenario_ver  = 0       # 마지막으로 처리한 scenario_version
        self.stats = {
            "received":         0,
            "dropped":          0,
            "interpolated":     0,
            "total_latency_ms": 0.0,
        }

    def push(self, record: dict):
        self.buf.append(record)
        status = record.get("packet_status", "received")
        if status == "missing":
            self.stats["dropped"] += 1
        elif status == "interpolated":
            self.stats["interpolated"] += 1
        else:
            self.stats["received"] += 1
        self.stats["total_latency_ms"] += record.get("latency_ms", 0.0)

    def snapshot(self) -> list:
        return list(self.buf)

    def reset(self):
        self.buf.clear()
        self.last_seq = -1
        self.last_pkt = {}
        self.latched_event = None
        self.last_scenario = None
        self.prev_display  = "NORMAL"
        self.stats = {
            "received":         0,
            "dropped":          0,
            "interpolated":     0,
            "total_latency_ms": 0.0,
        }


class MultiUserStateStore:
    """모든 사용자의 상태를 관리하는 store."""

    def __init__(self):
        self._slots: dict = {}    # user_id → _UserSlot
        self._lock = threading.Lock()
        os.makedirs(DATA_DIR, exist_ok=True)
        # session.jsonl과 event_log.csv 모두 lazy하게 처리한다. 모듈 import
        # 시점에 미리 만들어 두면 부트 초기화(config.reset_system_state)가
        # 파일을 비우려 할 때 우리가 잡고 있는 핸들 때문에 race가 발생한다.
        self._session_file = None

    def _ensure_event_log_header(self):
        """event_log.csv가 없거나 비어 있으면 헤더를 작성한다 (멱등)."""
        if os.path.exists(EVENT_LOG_FILE) and os.path.getsize(EVENT_LOG_FILE) > 0:
            return
        with open(EVENT_LOG_FILE, "w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerow([
                "time", "user_id", "seq_id", "scenario",
                "acc_magnitude", "gyro_magnitude",
                "skin_contact", "heart_rate",
                "packet_status", "is_interpolated",
                "motion_score", "wear_score", "data_quality_score",
                "raw_decision", "final_decision", "latched_event", "reason",
            ])

    def get_slot(self, user_id: str) -> _UserSlot:
        with self._lock:
            if user_id not in self._slots:
                self._slots[user_id] = _UserSlot(user_id)
            return self._slots[user_id]

    def all_user_ids(self) -> list:
        with self._lock:
            return list(self._slots.keys())

    def push(self, record: dict):
        """레코드를 push — 사용자별 slot에도 저장하고 session.jsonl에도 기록."""
        user_id = record.get("user_id", "unknown")
        slot = self.get_slot(user_id)
        slot.push(record)
        # session.jsonl 핸들을 lazy하게 연다 (부트 초기화가 끝난 뒤 첫 쓰기에서).
        if self._session_file is None:
            self._session_file = open(SESSION_FILE, "a", encoding="utf-8")
        self._session_file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._session_file.flush()

    def log_event(self, record: dict, decision: dict):
        """판정 결과를 event_log.csv에 추가 (상태 변화 시에만 호출)."""
        # 부트 초기화가 파일을 삭제한 뒤 처음 호출되는 경우 헤더를 보장.
        self._ensure_event_log_header()
        hr = record.get("heart_rate")
        acc_mag  = record.get("acc_magnitude")
        gyro_mag = record.get("gyro_magnitude")
        with open(EVENT_LOG_FILE, "a", encoding="utf-8", newline="") as f:
            csv.writer(f).writerow([
                record.get("timestamp", ""),
                record.get("user_id", ""),
                record.get("seq_id", ""),
                record.get("scenario", ""),
                f"{acc_mag:.4f}"  if acc_mag  is not None else "",
                f"{gyro_mag:.4f}" if gyro_mag is not None else "",
                record.get("skin_contact", ""),
                int(hr) if hr is not None else "",
                record.get("packet_status", ""),
                record.get("is_interpolated", False),
                f"{decision['motion_score']:.2f}",
                f"{decision['wear_score']:.2f}",
                f"{decision['data_quality_score']:.2f}",
                record.get("raw_decision",   decision.get("final_decision", "")),
                record.get("final_decision", decision.get("final_decision", "")),
                record.get("latched_event",  ""),
                record.get("reason",         decision.get("reason", "")),
            ])

    def reset_user(self, user_id: str):
        """특정 사용자의 상태만 초기화."""
        slot = self.get_slot(user_id)
        slot.reset()

    def reset_all(self):
        """모든 사용자 상태 초기화."""
        with self._lock:
            for slot in self._slots.values():
                slot.reset()

    def close(self):
        if self._session_file is not None:
            self._session_file.close()
            self._session_file = None


# 전역 인스턴스 (Process B 프로세스 내에서만 사용)
store = MultiUserStateStore()
