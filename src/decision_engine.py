"""
다단계 판정 엔진.

최종 판정 종류:
  NORMAL / WARNING / EMERGENCY_FALL /
  DEVICE_REMOVED / DEVICE_THROWN_OR_DROPPED / DATA_UNCERTAIN

v2: 사용자별 분리는 receiver가 담당하고, 본 모듈은 user 무관 — 단순히 window 입력만 받는다.

v3 (학술 근거 보강): 점수 가중치와 임계값 기본값을 다음 문헌에 맞춰 정렬한다.

  - Habib, M. A. et al. (2022). "Fall detection using accelerometer-based
    smartphones: Where do we go from here?" Frontiers in Public Health,
    10:996021. → FAM (First Acceleration Magnitude) 2g / Peak Time (PT) 3g
    의 표준 임계값 쌍을 채택.
  - Bourke, A. K., & Lyons, G. M. (2008). "A threshold-based fall-detection
    algorithm using a bi-axial gyroscope sensor." Medical Engineering &
    Physics, 30(1), 84-90. → 자이로 임계값 ~200 deg/s 의 근거.
  - Abbate, S. et al. (2012). "A smartphone-based fall detection system."
    Pervasive and Mobile Computing, 8(6), 883-899. → fall 의 post-impact
    "free of intentional movement" 구간을 fall 판정의 필요조건으로 사용.
  - Kangas, M. et al. (2008). "Comparison of low-complexity fall detection
    algorithms for body attached accelerometers." Gait & Posture, 28(2),
    285-291. → wrist 위치는 waist/head 보다 정확도가 낮다 (~75%)는 점이
    multi-modal 보조 신호 (wear / quality) 도입 동기.

v7 (시간 순서 모델): motion_score 의 acc / gyro / post-quiet 항목이 v6 까지
서로 독립적으로 OR 합산되어, "충격은 있는데 회전이 없는 경우 (책상 충돌)"
와 "회전만 격한 경우 (손목 비틀기)" 가 fall 과 같은 점수를 받는 문제가 있었다.
v7 에서는 acc spike (= impact) 를 anchor 로 잡고, 그 시점 전후의 gyro 동반
여부와 그 이후의 정지 패턴을 *시간 순서* 로 평가한다.

  - Bagalà, F. et al. (2012). "Evaluation of accelerometer-based fall
    detection algorithms on real-world falls." PLOS ONE, 7(5), e37062.
    → Bourke 의 four-phase fall 모델 (pre-fall / critical / post-fall /
    recovery) 의 표준 정의. 본 시스템의 impact + post-inactivity 시퀀스 검사
    의 직접 근거.
  - Huynh, Q. T. et al. (2015). "Optimization of an accelerometer and
    gyroscope-based fall detection algorithm." Journal of Sensors, 2015,
    452078. → "LFTacc 가 잡힌 후 0.5초 fall window 안에서 UFTacc 와 UFTgyro
    를 동시 확인" 방식의 sequential check. 본 시스템의 co-rotation 가산
    (impact ±2 tick 안의 gyro 동반) 의 근거.
  - Liu, S.-H. et al. (2017). "A novel hierarchical fall detection
    algorithm using a multiphase fall model." Sensors, 17(2), 307.
    → "free fall → impact → rest 의 시간 순서 가 모두 확인되어야 fall 로
    분류" 라는 phase 순서 검사 패러다임의 근거.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

import math

# 사용자별로 같은 post-quiet 판정 기준을 쓰도록 config 의 상수를 그대로 임포트.
# (config 와 decision_engine 두 곳에 같은 값이 존재해서 한 쪽만 바꾸면 silent
# 하게 분기 동작이 달라지는 사고를 막기 위함.)
from config import POST_QUIET_ACC, POST_QUIET_GYRO, POST_QUIET_MIN


def compute_acc_magnitude(row: dict):
    """acc magnitude 반환. missing 패킷이거나 acc 값이 None 이면 None 반환.

    중요: missing 은 "정지" 가 아니라 "모름". 0.0 을 반환하면 max / post-quiet
    판정이 모두 잘못된다 (낙상 직후 일부 패킷이 손실되면 그 0.0 들이 정지로
    인정되어 EMERGENCY_FALL 이 false-positive 트리거됨).
    """
    if row.get("packet_status") == "missing":
        return None
    precomp = row.get("acc_magnitude")
    if precomp is not None:
        try:
            return float(precomp)
        except (TypeError, ValueError):
            pass
    acc = row.get("acc") or {}
    x, y, z = acc.get("x"), acc.get("y"), acc.get("z")
    if x is None or y is None or z is None:
        return None
    return math.sqrt(x ** 2 + y ** 2 + z ** 2)


def compute_gyro_magnitude(row: dict):
    """gyro magnitude 반환. missing 패킷이거나 gyro 값이 None 이면 None 반환."""
    if row.get("packet_status") == "missing":
        return None
    precomp = row.get("gyro_magnitude")
    if precomp is not None:
        try:
            return float(precomp)
        except (TypeError, ValueError):
            pass
    gyro = row.get("gyro") or {}
    x, y, z = gyro.get("x"), gyro.get("y"), gyro.get("z")
    if x is None or y is None or z is None:
        return None
    return math.sqrt(x ** 2 + y ** 2 + z ** 2)


# ── 점수 계산 ──────────────────────────────────────────────────────────────

def compute_motion_score(
    window: list,
    fall_threshold: float = 2.8,
    gyro_threshold: float = 250.0,
) -> float:
    """
    낙상 동작 점수 (0.0 ~ 1.0). v7 시간 순서 fall-phase 모델.

    가중치 (합 1.0):
      [1] +0.30  IMPACT          : acc max >= fall_threshold
      [2] +0.25  CO-ROTATION     : impact 시점 ±2 tick 안에 gyro >= gyro_threshold
      [3] +0.45  POST-INACTIVITY : impact 이후 acc < POST_QUIET_ACC 그리고
                                   gyro < POST_QUIET_GYRO 가 POST_QUIET_MIN 이상

    중요: missing 패킷은 magnitude None 으로 처리되며 max / post-quiet
    계산에서 제외된다. 0.0 처리하면 missing 구간이 정지로 인정되어
    false-positive 가 발생함.
    """
    if not window:
        return 0.0

    # None 을 제외한 magnitude 리스트와 index 매핑
    acc_mags  = [compute_acc_magnitude(p)  for p in window]
    gyro_mags = [compute_gyro_magnitude(p) for p in window]

    # 유효 (None 아님) acc 값 중 임계 넘는 첫 위치
    spike_idx = None
    for i, m in enumerate(acc_mags):
        if m is not None and m >= fall_threshold:
            spike_idx = i
            break

    if spike_idx is None:
        return 0.0

    score = 0.30    # [1] IMPACT

    # ── [2] CO-ROTATION: impact 시점 ±2 tick 안에 gyro 동반 ─────────────
    CO_WINDOW = 2
    lo = max(0, spike_idx - CO_WINDOW)
    hi = min(len(gyro_mags), spike_idx + CO_WINDOW + 1)
    co_window_gyros = [g for g in gyro_mags[lo:hi] if g is not None]
    if co_window_gyros and max(co_window_gyros) >= gyro_threshold:
        score += 0.25

    # ── [3] POST-IMPACT INACTIVITY ────────────────────────────────────
    # impact 이후 패킷 중 acc, gyro 모두 정지 임계 미만이어야 quiet.
    # missing 은 quiet 으로 인정하지 않음 (None 이면 둘 다 미달 조건 불만족).
    quiet_count = 0
    for i in range(spike_idx + 1, len(acc_mags)):
        a = acc_mags[i]
        g = gyro_mags[i]
        if a is None or g is None:
            # missing — quiet 으로 카운트하지 않고 연속성도 끊기지 않음 (보수적)
            # → 다음 valid 패킷이 quiet 이면 다시 카운트 이어감
            continue
        if a < POST_QUIET_ACC and g < POST_QUIET_GYRO:
            quiet_count += 1
        else:
            # 큰 움직임이 다시 나타나면 quiet 카운트 리셋
            quiet_count = 0
        if quiet_count >= POST_QUIET_MIN:
            break

    if quiet_count >= POST_QUIET_MIN:
        score += 0.45

    return min(1.0, max(0.0, score))

    return min(1.0, max(0.0, score))


def compute_wear_score(window: list) -> float:
    """기기 착용 점수: skin_contact + heart_rate 존재 여부."""
    if not window:
        return 0.0
    latest = window[-1]
    score = 0.0
    if latest.get("skin_contact", 0) == 1:
        score += 0.60
    if latest.get("heart_rate") is not None:
        score += 0.40
    return min(1.0, max(0.0, score))


def compute_data_quality_score(window: list) -> tuple:
    """데이터 품질 점수 + loss_rate + consecutive_missing 반환."""
    if not window:
        return 0.0, 0.0, 0

    total = len(window)
    drop_count   = sum(1 for p in window if p.get("packet_status") == "missing")
    interp_count = sum(1 for p in window if p.get("packet_status") == "interpolated")

    loss_rate    = drop_count   / total
    interp_ratio = interp_count / total

    consec = 0
    max_consec = 0
    for p in window:
        if p.get("packet_status") == "missing":
            consec += 1
            max_consec = max(max_consec, consec)
        else:
            consec = 0

    score = max(0.0, 1.0 - 1.2 * loss_rate - 0.4 * interp_ratio)
    if max_consec >= 3:
        score = 0.0

    return min(1.0, score), loss_rate, max_consec


# ── 최종 판정 ──────────────────────────────────────────────────────────────

def classify_event(
    window: list,
    fall_threshold: float = 2.8,
    gyro_threshold: float = 250.0,
) -> dict:
    """
    윈도우 기반 판정. 반환 dict에 final_decision / 3가지 점수 / reason 포함.

    판정 우선순위:
      1. data_quality < 0.5 또는 loss_rate >= 0.4 또는 연속 missing >= 3 → DATA_UNCERTAIN
      2. wear_score < 0.3:
           - motion > 0.5 → DEVICE_THROWN_OR_DROPPED
           - else         → DEVICE_REMOVED
      3. motion >= 0.7 and wear >= 0.6  → EMERGENCY_FALL
      4. motion >= 0.30                 → WARNING
      5. else                           → NORMAL
    """
    motion_score = compute_motion_score(window, fall_threshold, gyro_threshold)
    wear_score   = compute_wear_score(window)
    dq, loss_rate, max_consec = compute_data_quality_score(window)

    # None 제외한 valid magnitude 만으로 max 계산 (missing 패킷 제외)
    acc_vals  = [compute_acc_magnitude(p)  for p in window] if window else []
    gyro_vals = [compute_gyro_magnitude(p) for p in window] if window else []
    acc_valid  = [v for v in acc_vals  if v is not None]
    gyro_valid = [v for v in gyro_vals if v is not None]
    max_acc   = max(acc_valid)  if acc_valid  else 0.0
    max_gyro  = max(gyro_valid) if gyro_valid else 0.0

    # 1. 데이터 품질 부족
    if dq < 0.5 or loss_rate >= 0.4 or max_consec >= 3:
        parts = []
        if loss_rate >= 0.4:
            parts.append(f"손실률 {loss_rate:.0%}")
        if max_consec >= 3:
            parts.append(f"연속 {max_consec}개 손실")
        return {
            "final_decision": "DATA_UNCERTAIN",
            "motion_score": motion_score,
            "wear_score": wear_score,
            "data_quality_score": dq,
            "reason": f"패킷 손실 과다({', '.join(parts) or 'low quality'}) → 데이터 신뢰도 부족",
        }

    # 2. 착용 불량
    if wear_score < 0.3:
        if motion_score > 0.5:
            return {
                "final_decision": "DEVICE_THROWN_OR_DROPPED",
                "motion_score": motion_score,
                "wear_score": wear_score,
                "data_quality_score": dq,
                "reason": (
                    f"가속도 {max_acc:.1f}g 충격, skin_contact=0·심박 없음 "
                    f"→ 기기 투척/탈락 판정"
                ),
            }
        else:
            return {
                "final_decision": "DEVICE_REMOVED",
                "motion_score": motion_score,
                "wear_score": wear_score,
                "data_quality_score": dq,
                "reason": "피부 접촉·심박 신호 없음, 움직임 정상 → 기기 탈착",
            }

    # 3. 실제 낙상
    if motion_score >= 0.7 and wear_score >= 0.6:
        # motion 1.0 = impact + 동반 회전 + 정지 모두 만족
        # motion 0.75 = impact + 정지 (동반 회전 약함, 옆방향/주저앉음 등)
        co_rot_tag = "+동반 회전" if motion_score >= 0.95 else ""
        return {
            "final_decision": "EMERGENCY_FALL",
            "motion_score": motion_score,
            "wear_score": wear_score,
            "data_quality_score": dq,
            "reason": (
                f"가속도 {max_acc:.1f}g 충격{co_rot_tag}, "
                f"충격 후 정지 확인, 착용 정상 → 실제 낙상 판정"
            ),
        }

    # 4. 경고 — impact 는 있지만 fall 의 정지 패턴이 아직/전혀 없음.
    #    예: 책상 충돌, fall-like 손목 흔들기, 격하지만 다시 일어선 동작.
    if motion_score >= 0.30:
        return {
            "final_decision": "WARNING",
            "motion_score": motion_score,
            "wear_score": wear_score,
            "data_quality_score": dq,
            "reason": (
                f"가속도 {max_acc:.1f}g 충격, 충격 후 정지 미확인 "
                f"→ 낙상 후보 경고 (단순 충격일 수 있음)"
            ),
        }

    # 5. 정상 — impact 자체가 없음 (회전만 격한 케이스 포함).
    return {
        "final_decision": "NORMAL",
        "motion_score": motion_score,
        "wear_score": wear_score,
        "data_quality_score": dq,
        "reason": "정상 활동 범위",
    }
