"""
프로세스 B (Multi-user): UDP 수신 + 사용자별 판정 엔진.

실행:  python src/udp_receiver.py

설계:
- 단일 UDP 포트에서 모든 사용자의 패킷을 수신한다.
- 각 패킷의 user_id로 store의 해당 slot을 찾아 처리한다.
- 사용자별 event latch를 관리한다 (EMERGENCY_FALL이 한 번 latch되면
  사용자가 시계 화면에서 'I'm OK' 누르거나 시나리오가 바뀔 때까지 유지).
- user_action_version이 갱신되면 시계 페이지의 사용자 응답을 처리한다:
    "im_ok"               → latch 해제, active_scenario를 normal_idle로 변경
    "emergency_confirmed" → latch 유지 (응급 확정 — 실제로는 응급 호출이 발생)
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

import json
import socket
import threading
import time
from collections import deque

from config import (
    UDP_HOST, UDP_PORT,
    DECISION_WINDOW,
    load_runtime_cfg,
    mutate_runtime_cfg,
)
from recovery import interpolate_gap, compute_magnitude
from decision_engine import classify_event
from state_store import store


# ── 수신 큐 (스레드 간 공유) ──────────────────────────────────────────────────
_recv_queue: deque = deque()
_recv_lock = threading.Lock()


def _udp_listener(sock: socket.socket):
    """UDP 패킷을 수신해 큐에 넣는 백그라운드 스레드."""
    while True:
        try:
            data, _ = sock.recvfrom(65535)
            pkt = json.loads(data.decode("utf-8"))
            with _recv_lock:
                _recv_queue.append(pkt)
        except Exception as e:
            print(f"[수신 스레드] 오류: {e}")


def _apply_latch(slot, raw_decision: str, raw_reason: str,
                 active_scenario: str, scenario_mode: str = "auto") -> tuple:
    """
    사용자별 event latch를 적용해 (display_decision, reason)을 반환.

    규칙:
    - raw_decision == EMERGENCY_FALL → latch 설정
    - raw_decision == DEVICE_THROWN_OR_DROPPED (and scenario==watch_thrown) → latch 설정
    - latch가 걸려 있으면 display는 latch 상태 유지
    - **자동 모드(scenario_mode=="auto")에서 시나리오가 자동 전환되면 latch 자동 해제**
      (시연용 — 자동 시퀀스가 real_fall → watch_thrown 등으로 넘어가는데 이전
       시나리오의 latch 가 sticky 하게 남아 화면이 "긴급 낙상"으로 굳어버리는
       문제를 해결. 현실 시스템에서는 수동 응답을 기다려야 하지만, 시뮬레이션
       에서는 시나리오 전환이 곧 "새 상황 시작" 으로 간주됨.)
    """
    # ── 자동 모드 시나리오 전환 감지 → latch 자동 해제 ─────────────────────
    if scenario_mode == "auto" and slot.last_scenario is not None:
        if slot.last_scenario != active_scenario:
            # 자동 시퀀스가 다음 시나리오로 넘어갔다. 이전 시나리오의 latch 는
            # 데모 흐름상 더 이상 유효하지 않으므로 정리한다.
            if slot.latched_event is not None:
                slot.latched_event = None
    slot.last_scenario = active_scenario

    if raw_decision == "EMERGENCY_FALL":
        slot.latched_event = "EMERGENCY_FALL"
    elif raw_decision == "DEVICE_THROWN_OR_DROPPED" and active_scenario == "watch_thrown":
        slot.latched_event = "DEVICE_THROWN_OR_DROPPED"

    if slot.latched_event == "EMERGENCY_FALL":
        display = "EMERGENCY_FALL"
        reason  = (
            "낙상 이벤트 확정됨 — 시계의 'I'm OK' 또는 'Emergency SOS' 응답 대기"
            if raw_decision != "EMERGENCY_FALL" else raw_reason
        )
    elif slot.latched_event == "DEVICE_THROWN_OR_DROPPED":
        display = "DEVICE_THROWN_OR_DROPPED"
        reason  = (
            "기기 투척 이벤트 확정됨 — 리셋 또는 시나리오 변경 전까지 유지"
            if raw_decision != "DEVICE_THROWN_OR_DROPPED" else raw_reason
        )
    else:
        display = raw_decision
        reason  = raw_reason

    return display, reason


def _handle_user_actions(cfg: dict) -> bool:
    """
    runtime_config.users 딕셔너리에서 각 사용자의 user_action을 확인하고
    필요한 처리를 수행한다 (latch 해제, 시나리오 전환).

    주의: 이 함수는 cfg를 **in-place로 수정**한다. 호출자는 반드시
    mutate_runtime_cfg() 같이 락이 잡힌 critical section 안에서 호출하고,
    수정이 발생했는지를 반환값으로 판단해 저장 여부를 결정해야 한다.

    이전 버전은 cfg를 통째로 받아 통째로 save하던 패턴이라, 락 밖에서
    호출되면 dashboard가 추가한 새 사용자를 stale 스냅샷으로 덮어써서
    사용자 등록이 사라지는 race를 일으켰다.

    반환: cfg가 수정되었는지 여부 (bool)
    """
    cfg_changed = False
    users_cfg   = cfg.get("users", {})

    for user_id, ustate in users_cfg.items():
        action_ver = ustate.get("user_action_version", 0)
        slot = store.get_slot(user_id)

        if action_ver > slot.last_action_ver:
            action = ustate.get("user_action")
            slot.last_action_ver = action_ver

            if action == "im_ok":
                # 사용자가 'I'm OK' 응답 → latch 해제 + 링버퍼 초기화 + 시나리오 흐름 리셋
                print(f"[수신기][{user_id}] 사용자 응답: I'm OK → latch 해제 + 링버퍼 초기화")
                slot.reset()
                # mode가 auto면 auto 흐름을 처음부터 재시작, manual이면 normal_idle로 전환
                cur_mode = ustate.get("scenario_mode", "auto")
                if cur_mode == "manual":
                    ustate["active_scenario"] = "normal_idle"
                ustate["scenario_version"] = ustate.get("scenario_version", 0) + 1
                ustate["user_action"]      = None
                cfg_changed = True

            elif action == "emergency_confirmed":
                # 사용자가 'Emergency SOS' 확정 → latch 유지하며 응급 호출 발생
                print(f"[수신기][{user_id}] 사용자 응답: 응급 확정 (Emergency SOS) — 알림 전송됨")
                ustate["user_action"] = None
                cfg_changed = True

    return cfg_changed


def _process_packet(pkt: dict, cfg: dict) -> list:
    """
    수신 패킷을 user별 slot에 push하고 판정한다.
    """
    user_id = pkt.get("user_id", "unknown")
    slot    = store.get_slot(user_id)

    records   = []
    recv_time = time.time()
    seq       = pkt.get("seq_id", 0)
    latency_ms = (recv_time - pkt.get("timestamp", recv_time)) * 1000.0

    # ── 갭(loss) 감지 — 단, sender의 seq는 모든 사용자 공통이므로
    # 사용자별로는 last_seq가 같은 user_id의 직전 패킷이 된다.
    # 따라서 gap 계산은 user별로 진행한다 (서로 다른 사용자의 seq는 무시).
    if slot.last_seq >= 0:
        gap_size = seq - slot.last_seq - 1
        # gap_size가 너무 크면 (다른 사용자 패킷들 사이) 보간하지 않는다.
        # 실용적으로 같은 사용자 패킷 간 gap은 적당히 작아야 하므로,
        # 4 이상이면 다른 사용자가 끼어든 것으로 보고 보간 안 함
        if 0 < gap_size <= 4:
            max_gap = cfg.get("interp_max_gap", 1.2)
            interp_pkts = interpolate_gap(slot.last_pkt, pkt, gap_size, max_gap)
            if interp_pkts:
                for i, ip in enumerate(interp_pkts, start=1):
                    ip["seq_id"]            = slot.last_seq + i
                    ip["acc_magnitude"]     = round(compute_magnitude(ip["acc"]), 4)
                    ip["gyro_magnitude"]    = round(compute_magnitude(ip["gyro"]), 4)
                    ip["latency_ms"]        = 0.0
                    ip["raw_decision"]      = "NORMAL"
                    ip["final_decision"]    = "NORMAL"
                    ip["latched_event"]     = slot.latched_event or ""
                    ip["motion_score"]      = 0.0
                    ip["wear_score"]        = 0.0
                    ip["data_quality_score"] = 0.8
                    ip["reason"]            = "선형 보간 복원 패킷"
                    records.append(ip)
                    store.push(ip)
            else:
                # 보간 불가 → missing 마커 삽입
                t_before = slot.last_pkt.get("timestamp", recv_time)
                t_after  = pkt.get("timestamp", recv_time)
                for j, miss_seq in enumerate(range(slot.last_seq + 1, seq), start=1):
                    frac   = j / (gap_size + 1)
                    est_ts = t_before + frac * (t_after - t_before)
                    missing_pkt = {
                        "seq_id":          miss_seq,
                        "timestamp":       round(est_ts, 6),
                        "user_id":         user_id,
                        "scenario":        pkt.get("scenario", "unknown"),
                        "acc":             {"x": None, "y": None, "z": None},
                        "gyro":            {"x": None, "y": None, "z": None},
                        "heart_rate":      None,
                        "skin_contact":    None,
                        "acc_magnitude":   None,
                        "gyro_magnitude":  None,
                        "latency_ms":      0.0,
                        "packet_status":   "missing",
                        "is_interpolated": False,
                        "raw_decision":    "DATA_UNCERTAIN",
                        "final_decision":  "DATA_UNCERTAIN",
                        "latched_event":   slot.latched_event or "",
                        "motion_score":    0.0,
                        "wear_score":      0.0,
                        "data_quality_score": 0.0,
                        "reason":          f"seq {miss_seq} 패킷 손실 — 복구 불가 (갭 {gap_size}개)",
                    }
                    records.append(missing_pkt)
                    store.push(missing_pkt)

    # ── 현재 패킷 처리 ──
    acc_mag  = compute_magnitude(pkt.get("acc",  {"x": 0, "y": 0, "z": 0}))
    gyro_mag = compute_magnitude(pkt.get("gyro", {"x": 0, "y": 0, "z": 0}))

    temp_record = {
        **pkt,
        "acc_magnitude":   round(acc_mag, 4),
        "gyro_magnitude":  round(gyro_mag, 4),
        "latency_ms":      round(latency_ms, 2),
        "packet_status":   "received",
        "is_interpolated": False,
    }

    # 판정
    fall_thr = cfg.get("fall_threshold", 2.5)
    gyro_thr = cfg.get("gyro_threshold", 150.0)
    window   = (slot.snapshot() + [temp_record])[-DECISION_WINDOW:]
    decision = classify_event(window, fall_thr, gyro_thr)

    raw_decision    = decision["final_decision"]
    user_state      = cfg.get("users", {}).get(user_id, {})
    active_scenario = user_state.get("active_scenario", "normal_idle")
    scenario_mode   = user_state.get("scenario_mode", "auto")

    display_decision, display_reason = _apply_latch(
        slot, raw_decision, decision["reason"], active_scenario, scenario_mode
    )

    record = {
        **temp_record,
        "raw_decision":       raw_decision,
        "final_decision":     display_decision,
        "latched_event":      slot.latched_event or "",
        "motion_score":       decision["motion_score"],
        "wear_score":         decision["wear_score"],
        "data_quality_score": decision["data_quality_score"],
        "reason":             display_reason,
    }

    records.append(record)
    store.push(record)

    # ── 이벤트 로그 기록 정책 ─────────────────────────────────────────────
    # v15부터: **모든 패킷을 매번 기록** (완전 실시간 모드).
    #
    # 이전(v14까지): 상태 전환 시(`display_decision != slot.prev_display`)에만
    # 한 줄 기록 → 정상 상태가 길게 유지되면 사용자 입장에서 "로그가 멈춘 듯"
    # 보이는 문제.
    #
    # 트레이드오프:
    # - 장점: 진짜 실시간성. 매 0.3~0.5초마다 새 행이 csv에 추가되어 시연
    #   에서 "살아 움직이는" 로그를 보여줄 수 있다.
    # - 단점: event_log.csv가 빠르게 커진다(분당 ~150행 × 등록 사용자 수).
    #   대시보드는 tail(25)로 최근 25행만 읽어 그리므로 화면은 정신없지
    #   않다. 다만 디스크 증가는 별도 관리가 필요할 수 있다.
    #   → 세션 초기화(고급 메뉴 - 전체 세션 초기화)가 csv도 비워주는지
    #     확인되어 있다. 장시간 운영 시에는 외부 logrotate 등을 권장.
    store.log_event(record, decision)
    slot.prev_display = display_decision  # 다른 코드가 참조할 수 있어 유지

    slot.last_seq = seq
    slot.last_pkt = pkt
    return records


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((UDP_HOST, UDP_PORT))
    print(f"[UDP 수신기 v2] 시작 — 수신 대기: {UDP_HOST}:{UDP_PORT}")
    print("[UDP 수신기 v2] 다수 사용자 모드 — 사용자별 분리 처리")
    print("[UDP 수신기 v2] 종료: Ctrl+C")
    print("-" * 60)

    listener = threading.Thread(target=_udp_listener, args=(sock,), daemon=True)
    listener.start()

    last_stat_time = time.time()
    last_reset_ver = None

    try:
        while True:
            # 사용자 액션이 발생할 수 있는 read-modify-write는 락 안에서
            # 단일 critical section으로 묶는다. 그 외 read-only 사용은 락 밖에서
            # 가벼운 load_runtime_cfg로 충분하다.
            #
            # ⚠ 락 holding time을 짧게 유지하기 위해 패킷 처리 같은 무거운 작업은
            # 락 밖에서 수행한다 (cfg snapshot만 들고 나간다).
            with mutate_runtime_cfg() as cfg:
                # 기본은 read-only 진입. 실제 cfg 수정이 일어난 경우에만
                # mark_dirty로 저장을 활성화한다.
                cfg.mark_clean()

                # 전체 리셋 감지
                reset_ver = cfg.get("reset_version", 0)
                if last_reset_ver is None:
                    last_reset_ver = reset_ver
                elif last_reset_ver != reset_ver:
                    last_reset_ver = reset_ver
                    store.reset_all()
                    print("[수신기] 전체 리셋 감지 — 모든 사용자 상태 초기화")

                # 사용자 액션 처리 (I'm OK / Emergency SOS) — cfg in-place 수정
                if _handle_user_actions(cfg):
                    cfg.mark_dirty()

                # 사용자별 시나리오 변경 시 latch + 링버퍼 초기화 (새 시나리오가
                # 이전 spike의 잔재에 의해 잘못 판정되지 않도록).
                # 이 부분은 cfg를 변경하지 않으므로 store 갱신만 수행.
                for user_id, ustate in cfg.get("users", {}).items():
                    slot = store.get_slot(user_id)
                    sver = ustate.get("scenario_version", 0)
                    if slot.last_scenario_ver != sver:
                        if slot.last_scenario_ver != 0:    # 첫 부팅 제외
                            slot.reset()
                            print(f"[수신기][{user_id}] 시나리오 변경 → 상태 초기화")
                        slot.last_scenario_ver = sver

                # 락 안에서 cfg snapshot을 빼내 락 밖에서 사용.
                # _process_packet은 cfg를 read-only로만 쓰므로 같은 dict를
                # 참조해도 안전하다 (이후 다른 프로세스가 디스크의 cfg를
                # 바꿔도 우리가 들고 있는 인메모리 dict에는 영향 없음).
                cfg_snapshot = cfg

            # 큐에서 패킷 꺼내 처리 (락 밖)
            with _recv_lock:
                pkt = _recv_queue.popleft() if _recv_queue else None

            if pkt is not None:
                records = _process_packet(pkt, cfg_snapshot)

                if records:
                    r = records[-1]
                    KR = {
                        "NORMAL":                   "정상",
                        "WARNING":                  "경고",
                        "EMERGENCY_FALL":           "긴급낙상",
                        "DEVICE_REMOVED":           "기기탈착",
                        "DEVICE_THROWN_OR_DROPPED": "기기투척",
                        "DATA_UNCERTAIN":           "데이터불확실",
                    }
                    fin = KR.get(r.get("final_decision", ""), r.get("final_decision", ""))
                    acc_s = f"{r['acc_magnitude']:.3f}g" if r["acc_magnitude"] is not None else "—"
                    print(
                        f"[{r.get('seq_id','?'):05}] {r.get('user_id','?')} | "
                        f"{r.get('scenario','?'):<18} | "
                        f"{fin:<10} | acc={acc_s} | {r['latency_ms']:.1f}ms"
                    )
            else:
                time.sleep(0.01)

            # 10초마다 통계
            now = time.time()
            if now - last_stat_time >= 10.0:
                lines = []
                for uid in store.all_user_ids():
                    s = store.get_slot(uid).stats
                    total = max(s["received"] + s["dropped"] + s["interpolated"], 1)
                    lines.append(
                        f"  {uid}: 수신 {s['received']} / 손실 {s['dropped']} / "
                        f"보간 {s['interpolated']} / 손실률 {s['dropped']/total*100:.1f}%"
                    )
                print(f"\n{'='*60}\n[통계]")
                for line in lines:
                    print(line)
                print(f"{'='*60}\n")
                last_stat_time = now

    except KeyboardInterrupt:
        print("\n[수신기] 사용자 중단 — 종료")
    finally:
        sock.close()
        store.close()


if __name__ == "__main__":
    # 가동 시 이전 데모 세션의 잔재(이벤트 로그, 패킷 로그, 등록 사용자)를 모두
    # 비워서 깨끗한 상태에서 시작한다. 락 안에서 멱등하게 수행되므로 sender /
    # dashboard와 거의 동시에 켜져도 안전하다.
    from config import reset_system_state
    if reset_system_state(boot_token="receiver-boot"):
        print("[수신기] 부트 초기화 완료 — 이전 세션 데이터 비움")
    main()
