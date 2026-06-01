"""
프로세스 A (Multi-user): 다수 사용자의 웨어러블 엣지 노드를 동시에 시뮬레이션한다.

실행:  python src/udp_sender.py

각 사용자(U01, U02, …)는 자신의 StatefulScenarioPlayer를 가지며,
라운드로빈으로 매 send_interval마다 한 명씩 패킷을 전송한다.

설계 의도:
- 다수 사용자 환경(요양원 등)에서 1대의 서버가 N대의 wearable을 모니터링하는
  cloud-centric 아키텍처를 시연한다.
- 사용자별 active_scenario / scenario_version은 runtime_config의 users 딕셔너리에서
  읽어와 동적으로 반영된다. 대시보드에서 특정 사용자의 시나리오만 바꿔도 다른
  사용자는 정상 활동을 계속한다.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

import json
import math
import random
import socket
import time

from scenario_player import StatefulScenarioPlayer
from config import UDP_HOST, UDP_PORT, load_runtime_cfg


def _mag(vec: dict) -> float:
    try:
        return math.sqrt(vec["x"] ** 2 + vec["y"] ** 2 + vec["z"] ** 2)
    except (TypeError, KeyError):
        return 0.0


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    print(f"[UDP 송신기 v3] 시작 — 대상: {UDP_HOST}:{UDP_PORT}")
    print("[UDP 송신기 v3] 다수 사용자 모드 (라운드로빈)")
    print("[UDP 송신기 v3] 사용자 풀이 비어 있어도 대시보드에서 등록할 때까지 대기")
    print("[UDP 송신기 v3] 종료: Ctrl+C")
    print("-" * 60)

    # 빈 사용자 풀로 시작 — 등록되면 다음 사이클에서 자동으로 활성화된다
    players: dict       = {}
    last_versions: dict = {}
    user_seq: dict      = {}
    last_reset_ver      = None
    rr_idx = 0
    waiting_logged      = False    # 빈 풀 안내 메시지 1회 출력용

    try:
        while True:
            cfg = load_runtime_cfg()

            if cfg.get("paused", False):
                time.sleep(0.3)
                continue

            # 전체 리셋 감지
            reset_ver = cfg.get("reset_version", 0)
            if last_reset_ver is None:
                last_reset_ver = reset_ver
            elif last_reset_ver != reset_ver:
                last_reset_ver = reset_ver
                for uid in list(last_versions.keys()):
                    last_versions[uid] = None
                print("[송신기] 전체 리셋 감지 — 모든 플레이어 초기화")

            users_cfg     = cfg.get("users", {})
            current_uids  = list(users_cfg.keys())

            # ── 사용자 풀 동기화 (대시보드에서 add/remove된 경우) ─────────────
            for uid in current_uids:
                if uid not in players:
                    # user_id에서 숫자 부분을 seed로 사용해 시작 위치를 분산시킨다
                    try:
                        seed_off = int("".join(c for c in uid if c.isdigit()) or "0")
                    except ValueError:
                        seed_off = 0
                    players[uid]       = StatefulScenarioPlayer(seed_offset=seed_off * 3)
                    last_versions[uid] = None
                    user_seq[uid]      = 0
                    print(f"[송신기] 새 사용자 등록 → {uid} (seed_off={seed_off * 3})")

            removed = [uid for uid in list(players.keys()) if uid not in current_uids]
            for uid in removed:
                players.pop(uid, None)
                last_versions.pop(uid, None)
                user_seq.pop(uid, None)
                print(f"[송신기] 사용자 제거 → {uid}")

            user_ids = current_uids
            if not user_ids:
                if not waiting_logged:
                    print("[송신기] 사용자 풀이 비어 있음 — 대시보드에서 디바이스를 등록하세요...")
                    waiting_logged = True
                time.sleep(0.5)
                continue
            else:
                if waiting_logged:
                    print("[송신기] 사용자 등록 감지 — 송신 재개")
                    waiting_logged = False

            # 라운드로빈 현재 사용자
            current_uid = user_ids[rr_idx % len(user_ids)]
            rr_idx += 1

            user_state = users_cfg.get(current_uid, {})
            mode       = user_state.get("scenario_mode",  "auto")
            scenario   = user_state.get("active_scenario", "normal_idle")
            version    = user_state.get("scenario_version", 0)

            # 사용자별 시나리오 변경 감지
            player = players[current_uid]
            if last_versions[current_uid] != version:
                player.reset(scenario, version, mode=mode)
                last_versions[current_uid] = version
                mode_tag = "자동" if mode == "auto" else f"수동:{scenario}"
                print(f"[송신기][{current_uid}] 시나리오 모드 변경 → {mode_tag} (v{version})")

            # 패킷 생성
            pkt = player.next_packet()
            pkt["seq_id"]    = user_seq[current_uid]
            pkt["user_id"]   = current_uid
            pkt["timestamp"] = time.time()
            payload = json.dumps(pkt, ensure_ascii=False).encode("utf-8")

            log_seq = user_seq[current_uid]
            user_seq[current_uid] += 1

            # 패킷 손실 — 사용자별 override 우선, 없으면 전역값.
            # user_state["packet_loss_rate"]가 None 이면 전역 cfg["packet_loss_rate"] 사용.
            user_loss = user_state.get("packet_loss_rate")
            loss_rate = user_loss if user_loss is not None else cfg.get("packet_loss_rate", 0.15)
            if random.random() < loss_rate:
                print(
                    f"[{current_uid}-{log_seq:04d}] DROP | {pkt['scenario']:<18} | "
                    f"acc={_mag(pkt['acc']):.3f}g"
                )
            else:
                delay = random.uniform(
                    cfg.get("delay_min", 0.05),
                    cfg.get("delay_max", 0.4),
                )
                time.sleep(delay)
                sock.sendto(payload, (UDP_HOST, UDP_PORT))
                print(
                    f"[{current_uid}-{log_seq:04d}] SEND | {pkt['scenario']:<18} | "
                    f"acc={_mag(pkt['acc']):.3f}g | hr={pkt['heart_rate']} | "
                    f"착용={pkt['skin_contact']} | {delay*1000:.0f}ms"
                )

            time.sleep(cfg.get("send_interval", 0.3))

    except KeyboardInterrupt:
        print("\n[송신기] 사용자 중단 — 종료")
    finally:
        sock.close()


if __name__ == "__main__":
    # 가동 시 이전 데모 세션의 잔재를 비운다. 같은 부트 사이클에서 receiver /
    # dashboard가 먼저 호출했다면 락 안에서 그쪽 결과를 보고 멱등하게 끝난다.
    from config import reset_system_state
    if reset_system_state(boot_token="sender-boot"):
        print("[송신기] 부트 초기화 완료 — 이전 세션 데이터 비움")
    main()
