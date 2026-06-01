"""선형 보간 기반 결측 데이터 복구."""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

import math


MAX_GAP_SECONDS = 0.5


def interpolate_gap(
    before: dict,
    after: dict,
    gap_size: int,
    max_gap_seconds: float = MAX_GAP_SECONDS,
) -> list:
    """
    before ~ after 사이 gap_size개 패킷을 선형 보간한다.

    보간 가능 조건:
      - 두 패킷 타임스탬프 차이가 max_gap_seconds 이하
    불가능하면 빈 리스트 반환 (DATA_UNCERTAIN 처리는 decision_engine이 담당).

    보간 패킷은 is_interpolated=True, packet_status="interpolated"로 표시한다.
    """
    t_before = before.get("timestamp", 0.0)
    t_after  = after.get("timestamp", 0.0)
    time_gap = t_after - t_before

    if time_gap <= 0 or time_gap > max_gap_seconds:
        return []

    result = []
    for i in range(1, gap_size + 1):
        alpha = i / (gap_size + 1)
        ts = t_before + alpha * time_gap

        acc = {
            axis: _lerp(before["acc"][axis], after["acc"][axis], alpha)
            for axis in ("x", "y", "z")
        }
        gyro = {
            axis: _lerp(before["gyro"][axis], after["gyro"][axis], alpha)
            for axis in ("x", "y", "z")
        }

        # heart_rate 보간
        hr_b = before.get("heart_rate")
        hr_a = after.get("heart_rate")
        if hr_b is None and hr_a is None:
            hr = None
        elif hr_b is None:
            hr = hr_a
        elif hr_a is None:
            hr = hr_b
        else:
            hr = int(round(_lerp(float(hr_b), float(hr_a), alpha)))

        skin_contact = before.get("skin_contact", 0)

        pkt = {
            "seq_id":          0,
            "timestamp":       round(ts, 6),
            "user_id":         before.get("user_id", ""),
            "scenario":        before.get("scenario", "unknown"),
            "acc":             {k: round(v, 4) for k, v in acc.items()},
            "gyro":            {k: round(v, 4) for k, v in gyro.items()},
            "heart_rate":      hr,
            "skin_contact":    skin_contact,
            "is_interpolated": True,
            "packet_status":   "interpolated",
        }
        result.append(pkt)

    return result


def _lerp(a: float, b: float, t: float) -> float:
    return a + t * (b - a)


def compute_magnitude(vec: dict) -> float:
    """{"x": float, "y": float, "z": float} → 합력 반환."""
    x = vec.get("x") or 0.0
    y = vec.get("y") or 0.0
    z = vec.get("z") or 0.0
    return math.sqrt(x ** 2 + y ** 2 + z ** 2)
