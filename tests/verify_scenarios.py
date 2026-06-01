"""
시스템 검증 스크립트 (v8 dynamic-acc 기준).

세 가지 카테고리의 검증을 한 번에 수행한다:

A. 시나리오 분류 분포 (100회 random seed)
   - 7개 시나리오가 의도된 최종 판정으로 분류되는지

B. 시뮬레이션 수치 범위 (GPT 권장 범위와 일치 여부)
   - acc / gyro 값이 dynamic-acc 기준 범위 안에 있는지
   - missing 패킷 처리가 올바른지

C. 시간 순서·동반 회전 케이스 (합성 윈도우)
   - 손목 비틀기 → NORMAL
   - 책상 충돌 → WARNING
   - 완전한 낙상 → EMERGENCY_FALL 등

실행:  python tests/verify_scenarios.py
"""
import sys
import os
import math
import random
import statistics
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
sys.path.insert(0, _SRC)

from scenario_player import StatefulScenarioPlayer          # noqa: E402
from decision_engine import (                                # noqa: E402
    classify_event,
    compute_motion_score,
    compute_acc_magnitude,
    compute_gyro_magnitude,
)
from config import DECISION_WINDOW, POST_QUIET_ACC, POST_QUIET_MIN    # noqa: E402


# ── 헬퍼 ─────────────────────────────────────────────────────────────────────

def _run_scenario(scenario: str, n_pkts: int, seed: int = 0):
    """주어진 시나리오의 패킷 시퀀스 생성."""
    random.seed(seed)
    player = StatefulScenarioPlayer(seed_offset=0)
    player.reset(scenario, version=0, mode="manual")
    pkts = []
    for _ in range(n_pkts):
        pkt = player.next_packet()
        pkt["packet_status"] = "received"
        pkts.append(pkt)
    return pkts


def _acc_mag(pkt):
    a = pkt["acc"]
    return math.sqrt(a["x"]**2 + a["y"]**2 + a["z"]**2)


def _gyro_mag(pkt):
    g = pkt["gyro"]
    return math.sqrt(g["x"]**2 + g["y"]**2 + g["z"]**2)


# ── A. 시나리오 분류 분포 ───────────────────────────────────────────────────

CASES = [
    # (시나리오, 누적 패킷 수, 기대 다수 판정)
    ("normal_idle",       12, "NORMAL"),
    ("walking",           12, "NORMAL"),
    ("running",           12, "NORMAL"),
    ("fall_like_motion",  10, "WARNING"),
    # real_fall: PRE(8) + ROT(1) + SPIKE(2) + POST(가능한 많이) → 윈도우 12에 POST가 충분히 들어와야
    ("real_fall",         14, "EMERGENCY_FALL"),
    # watch_thrown: PRE(6) + REM(1) + SPIKE(2) + POST(약간) → SPIKE 시점이 윈도우 끝에
    ("watch_thrown",      10, "DEVICE_THROWN_OR_DROPPED"),
    ("watch_removed",     12, "DEVICE_REMOVED"),
]


def test_a_scenario_distribution():
    print("[A] 시나리오 분류 분포 (100회 random seed)")
    print("-" * 100)
    print(f"{'시나리오':<22} | {'기대 판정':<28} | 100회 결과")
    print("-" * 100)
    all_ok = True
    for scenario, n_pkts, expected in CASES:
        results: Counter = Counter()
        for seed in range(100):
            pkts = _run_scenario(scenario, n_pkts, seed=seed)
            window = pkts[-DECISION_WINDOW:]
            result = classify_event(window)
            results[result["final_decision"]] += 1
        summary = ", ".join(f"{k}={v}" for k, v in results.most_common())

        # fall_like_motion 은 spike 가 fall_threshold 를 가끔만 넘기는 게 의도된
        # 동작이라 NORMAL 과 WARNING 이 섞임. GPT 요구사항도 "EMERGENCY 만 아니면
        # OK". 따라서 이 시나리오는 EMERGENCY 비율이 0% 인지만 검사한다.
        if scenario == "fall_like_motion":
            ok = (results.get("EMERGENCY_FALL", 0) == 0)
            flag = "✓" if ok else "✗"
            print(f"{scenario:<22} | NORMAL 또는 WARNING (EMERGENCY 0) | {summary} {flag}")
        else:
            top = results.most_common(1)[0][0]
            ok = (top == expected)
            flag = "✓" if ok else "✗"
            print(f"{scenario:<22} | {expected:<28} | {summary} {flag}")
        all_ok = all_ok and ok
    print()
    return all_ok


# ── B. 시뮬레이션 수치 범위 검증 (GPT 요청 1~7 + 8) ──────────────────────────

def test_b_value_ranges():
    print("[B] 시뮬레이션 수치 범위 (GPT 권장 dynamic-acc 기준 일치 여부)")
    print("-" * 100)
    all_ok = True

    # 각 시나리오에서 충분히 많은 패킷을 모아 통계 검증
    def collect(scenario: str, n_pkts: int, n_trials: int = 30):
        all_pkts = []
        for seed in range(n_trials):
            all_pkts.extend(_run_scenario(scenario, n_pkts, seed=seed))
        return all_pkts

    def _check(name, ok, detail):
        nonlocal all_ok
        flag = "✓" if ok else "✗"
        print(f"  {flag}  {name}")
        if detail:
            print(f"      {detail}")
        all_ok = all_ok and ok

    # B1. normal_idle 의 acc 평균이 0.01~0.08g 범위
    pkts = collect("normal_idle", n_pkts=20)
    accs = [_acc_mag(p) for p in pkts]
    mean_acc = statistics.mean(accs)
    _check("normal_idle 평균 acc ∈ [0.01, 0.08] g",
           0.01 <= mean_acc <= 0.08,
           f"평균={mean_acc:.4f}, min={min(accs):.4f}, max={max(accs):.4f}")

    # B2. walking 의 acc 대부분이 0.08~0.45g 범위
    pkts = collect("walking", n_pkts=20)
    accs = [_acc_mag(p) for p in pkts]
    in_range = sum(1 for a in accs if 0.08 <= a <= 0.45)
    ratio = in_range / len(accs)
    _check("walking acc ∈ [0.08, 0.45] g 비율 ≥ 95%",
           ratio >= 0.95,
           f"비율={ratio:.1%}, min={min(accs):.3f}, max={max(accs):.3f}")

    # B3. running 의 acc 대부분이 0.35~1.20g 범위
    pkts = collect("running", n_pkts=20)
    accs = [_acc_mag(p) for p in pkts]
    in_range = sum(1 for a in accs if 0.35 <= a <= 1.20)
    ratio = in_range / len(accs)
    # 부드러운 sin 곡선이라 시작 phase에서 일시 0.35 미만이 있을 수 있으므로 90% 목표
    _check("running acc ∈ [0.35, 1.20] g 비율 ≥ 90%",
           ratio >= 0.90,
           f"비율={ratio:.1%}, min={min(accs):.3f}, max={max(accs):.3f}")

    # B4. watch_removed 가 거의 0g 근처 (≤ 0.05g)
    pkts = collect("watch_removed", n_pkts=20)
    accs = [_acc_mag(p) for p in pkts]
    max_acc = max(accs)
    _check("watch_removed acc max ≤ 0.05 g (거의 0)",
           max_acc <= 0.05,
           f"max={max_acc:.4f}, 평균={statistics.mean(accs):.4f}")

    # B5. real_fall: impact phase 에서만 ≥ 2.8g, post phase 는 ≤ 0.12g
    print()
    print("  [B5] real_fall phase 별 분리 확인 (1회 시뮬, seed=0)")
    pkts = _run_scenario("real_fall", n_pkts=14, seed=0)
    accs = [_acc_mag(p) for p in pkts]
    # phase 구조: 0..7 pre, 8 rot, 9..10 impact, 11.. post
    impact_accs = accs[9:11]
    post_accs   = accs[11:14]
    impact_ok = all(a >= 2.8 for a in impact_accs)
    post_ok   = all(a <= 0.12 for a in post_accs)
    _check("real_fall impact phase 모두 ≥ 2.8 g",
           impact_ok,
           f"impact accs={[round(a,2) for a in impact_accs]}")
    _check("real_fall post phase 모두 ≤ 0.12 g",
           post_ok,
           f"post accs={[round(a,3) for a in post_accs]}")

    # B6. fall_like_motion: spike 있어도 post-quiet 없어 EMERGENCY_FALL 안 됨
    print()
    print("  [B6] fall_like_motion 은 EMERGENCY_FALL 이 되면 안 됨")
    emergency_count = 0
    for seed in range(100):
        pkts = _run_scenario("fall_like_motion", n_pkts=12, seed=seed)
        result = classify_event(pkts[-DECISION_WINDOW:])
        if result["final_decision"] == "EMERGENCY_FALL":
            emergency_count += 1
    _check("fall_like_motion → EMERGENCY_FALL 비율 = 0%",
           emergency_count == 0,
           f"EMERGENCY 발생 횟수 = {emergency_count}/100")

    # B7. watch_thrown 이 DEVICE_THROWN_OR_DROPPED 로 분류
    print()
    print("  [B7] watch_thrown 은 DEVICE_THROWN_OR_DROPPED 로 분류")
    correct = 0
    for seed in range(100):
        pkts = _run_scenario("watch_thrown", n_pkts=10, seed=seed)
        result = classify_event(pkts[-DECISION_WINDOW:])
        if result["final_decision"] == "DEVICE_THROWN_OR_DROPPED":
            correct += 1
    _check("watch_thrown → DEVICE_THROWN_OR_DROPPED 비율 ≥ 95%",
           correct >= 95,
           f"정답 비율 = {correct}/100")

    # B8. missing packet 이 post-quiet 으로 인정되지 않음
    print()
    print("  [B8] missing 패킷이 post-quiet 으로 잘못 인정되지 않음")
    # 충격 spike 직후 missing 만 가득한 윈도우: post-quiet 못 받음
    window = []
    # pre: 보행 수준
    for _ in range(2):
        window.append({"acc": {"x": 0.2, "y": 0.1, "z": 0.0},
                       "gyro": {"x": 30, "y": 20, "z": 0},
                       "heart_rate": 80, "skin_contact": 1,
                       "packet_status": "received"})
    # impact spike 1개
    window.append({"acc": {"x": 3.5, "y": 0, "z": 0},
                   "gyro": {"x": 400, "y": 0, "z": 0},
                   "heart_rate": 90, "skin_contact": 1,
                   "packet_status": "received"})
    # 그 후 missing 만 3개 (실제로는 사용자가 움직이고 있을 수도 있음)
    for _ in range(3):
        window.append({"acc": {"x": None, "y": None, "z": None},
                       "gyro": {"x": None, "y": None, "z": None},
                       "heart_rate": None, "skin_contact": None,
                       "packet_status": "missing"})

    motion = compute_motion_score(window)
    # spike (0.30) 만 받아야 함. post-quiet 0.45 받으면 안 됨.
    # co-rotation 은 받음 (0.25). 즉 ~0.55 이하여야 함.
    _check("spike 후 missing 만 있을 때 post-quiet 미가산 (motion ≤ 0.60)",
           motion <= 0.60,
           f"motion={motion:.2f} (spike+co-rot 0.55 정도면 정상, 1.00 이면 missing 을 정지로 오인)")

    print()
    return all_ok


# ── C. 시간 순서·동반 회전 합성 케이스 ──────────────────────────────────────

def _synth_pkt(acc_mag, gyro_mag, *, skin=1, hr=75):
    return {
        "acc":  {"x": acc_mag,  "y": 0.0, "z": 0.0},
        "gyro": {"x": gyro_mag, "y": 0.0, "z": 0.0},
        "heart_rate": hr,
        "skin_contact": skin,
        "packet_status": "received",
    }


def test_c_temporal_cases():
    print("[C] 시간 순서·동반 회전 합성 케이스")
    print("-" * 100)
    all_ok = True

    def _case(name, window, expected_decision, expected_score_range):
        nonlocal all_ok
        motion = compute_motion_score(window)
        result = classify_event(window)
        decision = result["final_decision"]
        lo, hi = expected_score_range
        score_ok = lo <= motion <= hi
        decision_ok = (decision == expected_decision)
        ok = score_ok and decision_ok
        flag = "✓" if ok else "✗"
        print(f"  {flag}  {name}")
        print(f"      motion={motion:.2f} (기대 {lo:.2f}~{hi:.2f})  →  {decision} (기대 {expected_decision})")
        all_ok = all_ok and ok

    # 1. 손목 비틀기 (회전만)
    win = [_synth_pkt(0.05, 50) for _ in range(8)]
    win += [_synth_pkt(0.10, 350) for _ in range(2)]
    win += [_synth_pkt(0.05, 50) for _ in range(4)]
    _case("회전만 ↑↑ (손목 비틀기) → NORMAL", win, "NORMAL", (0.0, 0.0))

    # 2. 책상 충돌 (충격만, 회전 작음, 즉시 회복)
    win = [_synth_pkt(0.20, 30) for _ in range(8)]
    win += [_synth_pkt(3.5, 150) for _ in range(2)]    # 충격, 회전 250 미달
    win += [_synth_pkt(0.30, 35) for _ in range(4)]    # 즉시 회복 — 정지 아님
    _case("충격만 ↑↑, 회전 작고 즉시 회복 → WARNING", win, "WARNING", (0.25, 0.35))

    # 3. 충격 + 정지, 회전 약함 (옆방향 낙상)
    win = [_synth_pkt(0.20, 30) for _ in range(8)]
    win += [_synth_pkt(3.5, 150) for _ in range(2)]    # 충격, 회전 약함
    win += [_synth_pkt(0.05, 5) for _ in range(4)]     # 정지
    _case("충격 + 정지, 회전 약함 → EMERGENCY (옆방향 낙상)",
          win, "EMERGENCY_FALL", (0.70, 0.80))

    # 4. 완전한 낙상 (회전 → 충격 → 정지)
    win = [_synth_pkt(0.20, 30) for _ in range(7)]
    win += [_synth_pkt(0.5, 300)]                       # pre 회전
    win += [_synth_pkt(4.0, 500) for _ in range(2)]     # 충격 + 동반 회전
    win += [_synth_pkt(0.05, 5) for _ in range(4)]      # 정지
    _case("회전 → 충격 → 정지 → EMERGENCY (motion 1.00)",
          win, "EMERGENCY_FALL", (0.95, 1.00))

    # 5. 충격+회전 동반하지만 일어남 (정지 없음)
    win = [_synth_pkt(0.20, 30) for _ in range(8)]
    win += [_synth_pkt(4.0, 500) for _ in range(2)]     # 충격 + 회전
    win += [_synth_pkt(0.50, 60) for _ in range(4)]     # 다시 보행
    _case("충격+회전 동반, 정지 없음 → WARNING",
          win, "WARNING", (0.50, 0.60))

    print()
    return all_ok


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 100)
    print("v8 dynamic-acc 통합 검증")
    print("=" * 100)
    print()
    ok_a = test_a_scenario_distribution()
    ok_b = test_b_value_ranges()
    ok_c = test_c_temporal_cases()

    print("=" * 100)
    if ok_a and ok_b and ok_c:
        print("✓ 모든 검증 통과")
    else:
        print(f"✗ 일부 실패 — A: {'OK' if ok_a else 'FAIL'}, "
              f"B: {'OK' if ok_b else 'FAIL'}, "
              f"C: {'OK' if ok_c else 'FAIL'}")
        sys.exit(1)


if __name__ == "__main__":
    main()
