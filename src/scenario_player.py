"""
StatefulScenarioPlayer — 상태 기반 시나리오 플레이어 (v8 사람-움직임 모델)

매 tick마다 active_scenario에 맞는 패킷 1개를 생성한다.

v8 핵심 변경:

(1) acc_magnitude 기준 통일: **dynamic / user acceleration magnitude (g 단위,
    중력 성분 제거됨)**. 완전 정지는 0 g 근처, 1 g 근처 아님.
    이전 버전들은 단위 약속이 모호해 시뮬레이터 값과 임계값이 같은 척도 위에
    있지 않았다 (예: walking 0.7~1.7 g vs fall_threshold 3 g). v8 부터는
    config.DEFAULTS 의 모든 임계값과 본 모듈의 시뮬 값이 동일한 dynamic-acc
    축 위에서 비교된다.

(2) 3축 방향에 state 유지: 이전 버전은 매 tick 마다 _rand_direction 으로
    완전히 랜덤한 단위 벡터를 뽑아서, x/y/z 개별 축이 사람 움직임과 다르게
    난수처럼 흩어졌다. v8 에서는 각 시나리오의 player 인스턴스가 acc / gyro
    방향을 보존하고 작은 jitter 만 적용한다. magnitude 의 시간적 흐름은
    sin wave (보행/주행) 또는 phase-based envelope (낙상) 으로 자연스럽게.

(3) 시나리오별 권장 범위 (dynamic acc g, deg/s):

    [normal_idle]      acc 0.01-0.08  / gyro 0.5-6     / hr 65-75  / skin=1
    [walking]          acc 0.08-0.45  / gyro 15-80     / hr 78-100 / skin=1   (sin 주기성)
    [running]          acc 0.35-1.20  / gyro 70-180    / hr 110-150/ skin=1   (더 빠른 sin)
    [fall_like_motion] pre walking → spike acc 1.3-2.6 / gyro 100-260 → walking 복귀
                       (post-quiet 없음 → WARNING 또는 NORMAL)
    [real_fall]        pre walking → 회전 먼저 → impact acc 2.8-4.8 + gyro 250-700
                       → post acc 0.01-0.08 + gyro 0.5-12, hr 80-110 서서히 상승
    [watch_removed]    acc 0.00-0.04  / gyro 0.0-2.0   / hr=None   / skin=0
    [watch_thrown]     pre 착용 walking → removal (skin=0, hr=None) →
                       throw spike acc 2.5-6.0 + gyro 400-900 (사람이 던지는
                       회전 범위 — GPT 권장 1200 은 야구 투수급이라 축소) →
                       post acc 0.00-0.05 + gyro 0.0-5.0
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

import math
import random


# ── 방향 state 헬퍼 ──────────────────────────────────────────────────────────

def _rand_unit_vec() -> tuple:
    """무작위 단위 벡터 (구면 균등 분포)."""
    theta = random.uniform(0, 2 * math.pi)
    phi   = random.uniform(0, math.pi)
    return (math.sin(phi) * math.cos(theta),
            math.sin(phi) * math.sin(theta),
            math.cos(phi))


def _normalize(v: tuple) -> tuple:
    """벡터 정규화. 0벡터면 임의 방향 반환."""
    n = math.sqrt(v[0]**2 + v[1]**2 + v[2]**2)
    if n < 1e-9:
        return _rand_unit_vec()
    return (v[0] / n, v[1] / n, v[2] / n)


def _jitter_direction(prev_dir: tuple, jitter_strength: float) -> tuple:
    """이전 방향에 작은 jitter 를 더한 새 방향 (정규화 결과).

    jitter_strength:
      0.02 ~ 0.05 : normal_idle 수준 (거의 안 흔들림)
      0.10 ~ 0.20 : walking 수준 (자연스러운 흔들림)
      0.25 ~ 0.40 : running 수준 (적당히 격함)
      0.80 ~ 1.50 : impact spike (방향 거의 새로 잡힘)
    """
    dx = random.gauss(0, jitter_strength)
    dy = random.gauss(0, jitter_strength)
    dz = random.gauss(0, jitter_strength)
    return _normalize((prev_dir[0] + dx, prev_dir[1] + dy, prev_dir[2] + dz))


def _scale(d: tuple, magnitude: float) -> tuple:
    return (d[0] * magnitude, d[1] * magnitude, d[2] * magnitude)


# ── 패킷 빌더 ────────────────────────────────────────────────────────────────

def _pkt(scenario, acc_vec, gyro_vec, hr, skin) -> dict:
    ax, ay, az = acc_vec
    gx, gy, gz = gyro_vec
    return {
        "seq_id":       0,
        "timestamp":    0.0,
        "user_id":      "",          # sender가 채운다
        "scenario":     scenario,
        "acc":          {"x": round(ax, 4), "y": round(ay, 4), "z": round(az, 4)},
        "gyro":         {"x": round(gx, 4), "y": round(gy, 4), "z": round(gz, 4)},
        "heart_rate":   hr,
        "skin_contact": skin,
    }


# ── 심박수 drift 시뮬레이터 ──────────────────────────────────────────────────

class _HR:
    """심박수 drift 시뮬레이터. 목표값에 점진적으로 수렴."""
    def __init__(self, init: float = 72.0):
        self._v = init

    def next(self, lo: int, hi: int, smoothing: float = 0.25) -> int:
        target = random.uniform(lo, hi)
        self._v += (target - self._v) * smoothing + random.uniform(-1.0, 1.0)
        self._v = max(45.0, min(200.0, self._v))
        return int(round(self._v))

    def step_toward(self, target: float, rate: float = 0.15) -> int:
        """특정 목표값에 천천히 수렴 (real_fall 의 hr 서서히 상승용)."""
        self._v += (target - self._v) * rate + random.uniform(-0.5, 0.5)
        self._v = max(45.0, min(200.0, self._v))
        return int(round(self._v))


# 시나리오 phase tick 설정
_FALL_PRE    = 8    # real_fall pre-normal (보행)
_FALL_ROT    = 1    # real_fall 낙하 중 회전 phase (자이로 먼저 튐)
_THROWN_PRE  = 6    # watch_thrown pre-worn (착용 중 보행)
_THROWN_REM  = 1    # watch_thrown removal transition (skin/hr 꺼짐)
_LIKE_PRE    = 6    # fall_like_motion pre-walking
_SPIKE_TICKS = 2    # impact spike 길이


# ── auto 시퀀스 프로그램 ─────────────────────────────────────────────────────

def _make_auto_program(seed_offset: int = 0) -> list:
    """자동 시퀀스 — (scenario, duration_ticks) 튜플 리스트."""
    base = [
        ("normal_idle",      12),
        ("walking",           8),
        ("normal_idle",       6),
        ("walking",           6),
        ("fall_like_motion", 10),    # 손목 흔들기 — WARNING
        ("walking",           4),
        ("normal_idle",      10),
        ("running",           4),
        ("normal_idle",       8),
        ("real_fall",        14),    # EMERGENCY_FALL (관리자 응답 대기)
        ("normal_idle",      12),
        ("walking",           5),
        ("watch_removed",     6),    # DEVICE_REMOVED
        ("normal_idle",       8),
        ("watch_thrown",     12),    # DEVICE_THROWN_OR_DROPPED
        ("normal_idle",      10),
    ]
    n = len(base)
    offset = seed_offset % n
    return base[offset:] + base[:offset]


# ── 메인 플레이어 ────────────────────────────────────────────────────────────

class StatefulScenarioPlayer:
    """
    시나리오별 player. 매 tick 패킷 1개를 생성하며 acc/gyro 방향 state 를 보존.
    """

    def __init__(self, seed_offset: int = 0):
        self.active_scenario = "normal_idle"
        self.version         = -1
        self.tick            = 0
        self.event_done      = False
        self._hr             = _HR(init=72.0)

        # 방향 state — 시나리오가 바뀌어도 보존되어 자연스러운 연속성
        self._acc_dir  = _rand_unit_vec()
        self._gyro_dir = _rand_unit_vec()

        # 보행/주행 sin 위상
        self._walk_phase = 0.0
        self._run_phase  = 0.0

        self.mode            = "auto"
        self._auto_seq_idx   = 0
        self._auto_step_tick = 0
        self._auto_program   = _make_auto_program(seed_offset)

    def reset(self, scenario: str, version: int, mode: str = "manual"):
        self.active_scenario = scenario
        self.version         = version
        self.tick            = 0
        self.event_done      = False
        self._walk_phase     = 0.0
        self._run_phase      = 0.0
        self.mode            = mode
        if mode == "auto":
            self._auto_seq_idx   = 0
            self._auto_step_tick = 0

    def next_packet(self) -> dict:
        if self.mode == "auto":
            pkt = self._auto_next()
        else:
            pkt = self._dispatch()
        self.tick += 1
        return pkt

    def _auto_next(self) -> dict:
        if self._auto_seq_idx >= len(self._auto_program):
            self._auto_seq_idx = 0

        step_scenario, step_duration = self._auto_program[self._auto_seq_idx]

        if self._auto_step_tick == 0:
            self.active_scenario = step_scenario
            self.event_done      = False
            self.tick            = 0    # event 시작 시 tick 리셋 (phase 계산용)

        pkt = self._dispatch()
        self._auto_step_tick += 1

        if self._auto_step_tick >= step_duration:
            self._auto_seq_idx  += 1
            self._auto_step_tick = 0

        return pkt

    def _dispatch(self) -> dict:
        s = self.active_scenario
        if   s == "normal_idle":      return self._normal()
        elif s == "walking":          return self._walking()
        elif s == "running":          return self._running()
        elif s == "real_fall":        return self._real_fall()
        elif s == "watch_thrown":     return self._watch_thrown()
        elif s == "watch_removed":    return self._watch_removed()
        elif s == "fall_like_motion": return self._fall_like()
        else:                         return self._normal()

    # ── 지속형 시나리오 ───────────────────────────────────────────────────

    def _normal(self) -> dict:
        # acc 0.01-0.08 g, gyro 0.5-6 deg/s — 거의 정지, 작은 호흡·미세 움직임
        self._acc_dir  = _jitter_direction(self._acc_dir, 0.05)
        self._gyro_dir = _jitter_direction(self._gyro_dir, 0.05)
        mag_a = random.uniform(0.01, 0.08)
        mag_g = random.uniform(0.5, 6.0)
        hr = self._hr.next(65, 75, smoothing=0.15)
        return _pkt("normal_idle",
                    _scale(self._acc_dir, mag_a),
                    _scale(self._gyro_dir, mag_g),
                    hr, 1)

    def _walking(self) -> dict:
        # 보행 주기: ~1Hz step. send_interval 0.3s 기준 phase 증가 ≒ 1.9 rad/tick
        # acc 는 sin 위에 작은 jitter — 0.08-0.45 g
        self._walk_phase += 1.9
        # 보행 step 의 위상 (0..1) 따라 acc 가 부드럽게 진동
        wave = abs(math.sin(self._walk_phase))    # 0..1
        mag_a = 0.10 + 0.32 * wave + random.uniform(-0.02, 0.03)
        mag_a = max(0.08, min(0.45, mag_a))
        # gyro 도 보행 phase 와 약하게 동조 (팔 흔들림)
        mag_g = 18.0 + 55.0 * wave + random.uniform(-3.0, 5.0)
        mag_g = max(15.0, min(80.0, mag_g))

        self._acc_dir  = _jitter_direction(self._acc_dir, 0.15)
        self._gyro_dir = _jitter_direction(self._gyro_dir, 0.18)

        hr = self._hr.next(78, 100, smoothing=0.15)
        return _pkt("walking",
                    _scale(self._acc_dir, mag_a),
                    _scale(self._gyro_dir, mag_g),
                    hr, 1)

    def _running(self) -> dict:
        # 주행 주기: ~2-3Hz, sin 더 빠르게
        self._run_phase += 2.7
        wave = abs(math.sin(self._run_phase))
        mag_a = 0.40 + 0.75 * wave + random.uniform(-0.04, 0.05)
        mag_a = max(0.35, min(1.20, mag_a))
        mag_g = 75.0 + 100.0 * wave + random.uniform(-5.0, 8.0)
        mag_g = max(70.0, min(180.0, mag_g))

        self._acc_dir  = _jitter_direction(self._acc_dir, 0.25)
        self._gyro_dir = _jitter_direction(self._gyro_dir, 0.28)

        hr = self._hr.next(110, 150, smoothing=0.12)
        return _pkt("running",
                    _scale(self._acc_dir, mag_a),
                    _scale(self._gyro_dir, mag_g),
                    hr, 1)

    def _watch_removed(self) -> dict:
        # 책상 위 정지 — acc 0.00-0.04, gyro 0.0-2.0, hr=None, skin=0
        # 방향은 사실상 거의 안 바뀜
        self._acc_dir  = _jitter_direction(self._acc_dir, 0.02)
        self._gyro_dir = _jitter_direction(self._gyro_dir, 0.02)
        mag_a = random.uniform(0.00, 0.04)
        mag_g = random.uniform(0.0, 2.0)
        return _pkt("watch_removed",
                    _scale(self._acc_dir, mag_a),
                    _scale(self._gyro_dir, mag_g),
                    None, 0)

    # ── 이벤트형 시나리오 ─────────────────────────────────────────────────

    def _real_fall(self) -> dict:
        """
        real_fall phase 구조 (총 _FALL_PRE + _FALL_ROT + _SPIKE_TICKS + ... ticks):
          0 .. _FALL_PRE-1                                : 보행 (pre-fall)
          _FALL_PRE                                       : 회전 phase (gyro 먼저 튐)
          _FALL_PRE+_FALL_ROT .. +_FALL_ROT+_SPIKE_TICKS-1: impact (acc + gyro 동시)
          그 이후                                          : post-fall quiet, hr 서서히 상승
        """
        t = self.tick
        rot_start    = _FALL_PRE
        impact_start = _FALL_PRE + _FALL_ROT
        impact_end   = impact_start + _SPIKE_TICKS    # exclusive

        if t < rot_start:
            # pre-fall: 보행 패턴 (낮은 acc)
            self._walk_phase += 1.9
            wave = abs(math.sin(self._walk_phase))
            mag_a = 0.10 + 0.30 * wave + random.uniform(-0.02, 0.03)
            mag_a = max(0.08, min(0.50, mag_a))
            mag_g = 15.0 + 40.0 * wave + random.uniform(-3.0, 5.0)
            mag_g = max(10.0, min(60.0, mag_g))
            self._acc_dir  = _jitter_direction(self._acc_dir, 0.15)
            self._gyro_dir = _jitter_direction(self._gyro_dir, 0.18)
            hr = self._hr.next(70, 82, smoothing=0.15)
            return _pkt("real_fall",
                        _scale(self._acc_dir, mag_a),
                        _scale(self._gyro_dir, mag_g),
                        hr, 1)

        elif t < impact_start:
            # 낙하 중 회전 phase: 자이로가 먼저 큰 값으로 튐, acc 는 약간만
            mag_a = random.uniform(0.30, 0.80)
            mag_g = random.uniform(180.0, 320.0)
            # 회전 방향은 새로 잡힘
            self._acc_dir  = _jitter_direction(self._acc_dir, 0.5)
            self._gyro_dir = _jitter_direction(self._gyro_dir, 1.0)
            hr = self._hr.next(78, 90, smoothing=0.25)
            return _pkt("real_fall",
                        _scale(self._acc_dir, mag_a),
                        _scale(self._gyro_dir, mag_g),
                        hr, 1)

        elif t < impact_end and not self.event_done:
            # impact spike: acc 2.8-4.8 + gyro 250-700 동시
            mag_a = random.uniform(2.8, 4.8)
            mag_g = random.uniform(250.0, 700.0)
            # impact 시 방향이 크게 바뀜 (충돌 방향)
            self._acc_dir  = _jitter_direction(self._acc_dir, 1.2)
            self._gyro_dir = _jitter_direction(self._gyro_dir, 1.2)
            hr = self._hr.step_toward(95.0, rate=0.20)
            if t == impact_end - 1:
                self.event_done = True
            return _pkt("real_fall",
                        _scale(self._acc_dir, mag_a),
                        _scale(self._gyro_dir, mag_g),
                        hr, 1)

        else:
            # post-fall quiet: 거의 정지, hr 80-110 서서히 상승
            self.event_done = True
            self._acc_dir  = _jitter_direction(self._acc_dir, 0.05)
            self._gyro_dir = _jitter_direction(self._gyro_dir, 0.05)
            mag_a = random.uniform(0.01, 0.08)
            mag_g = random.uniform(0.5, 12.0)
            # 충격 후 hr 은 천천히 110 까지 상승
            hr = self._hr.step_toward(108.0, rate=0.08)
            return _pkt("real_fall",
                        _scale(self._acc_dir, mag_a),
                        _scale(self._gyro_dir, mag_g),
                        hr, 1)

    def _watch_thrown(self) -> dict:
        """
        watch_thrown phase 구조:
          0 .. _THROWN_PRE-1                                       : 착용 중 보행
          _THROWN_PRE                                              : removal (skin=0, hr=None)
          _THROWN_PRE+_THROWN_REM .. +_THROWN_REM+_SPIKE_TICKS-1  : throw + 바닥 충돌 spike
          그 이후                                                   : 바닥에 정지
        """
        t = self.tick
        rem_start    = _THROWN_PRE
        impact_start = _THROWN_PRE + _THROWN_REM
        impact_end   = impact_start + _SPIKE_TICKS

        if t < rem_start:
            # 착용 중 보행
            self._walk_phase += 1.9
            wave = abs(math.sin(self._walk_phase))
            mag_a = 0.10 + 0.30 * wave + random.uniform(-0.02, 0.03)
            mag_a = max(0.08, min(0.50, mag_a))
            mag_g = 15.0 + 40.0 * wave + random.uniform(-3.0, 5.0)
            mag_g = max(10.0, min(60.0, mag_g))
            self._acc_dir  = _jitter_direction(self._acc_dir, 0.15)
            self._gyro_dir = _jitter_direction(self._gyro_dir, 0.18)
            hr = self._hr.next(68, 78, smoothing=0.15)
            return _pkt("watch_thrown",
                        _scale(self._acc_dir, mag_a),
                        _scale(self._gyro_dir, mag_g),
                        hr, 1)

        elif t < impact_start:
            # removal: 시계 벗는 순간 — skin=0, hr=None, 약한 움직임만
            mag_a = random.uniform(0.20, 0.60)
            mag_g = random.uniform(30.0, 120.0)
            self._acc_dir  = _jitter_direction(self._acc_dir, 0.3)
            self._gyro_dir = _jitter_direction(self._gyro_dir, 0.4)
            return _pkt("watch_thrown",
                        _scale(self._acc_dir, mag_a),
                        _scale(self._gyro_dir, mag_g),
                        None, 0)

        elif t < impact_end and not self.event_done:
            # throw impact: acc 2.5-6.0 + gyro 400-900
            # (실제 사람이 던지는 회전 — 야구 투수급 1200 deg/s 가 아닌 일상 던지기)
            mag_a = random.uniform(2.5, 6.0)
            mag_g = random.uniform(400.0, 900.0)
            self._acc_dir  = _jitter_direction(self._acc_dir, 1.2)
            self._gyro_dir = _jitter_direction(self._gyro_dir, 1.5)
            if t == impact_end - 1:
                self.event_done = True
            return _pkt("watch_thrown",
                        _scale(self._acc_dir, mag_a),
                        _scale(self._gyro_dir, mag_g),
                        None, 0)

        else:
            # 바닥에 떨어져 정지
            self.event_done = True
            self._acc_dir  = _jitter_direction(self._acc_dir, 0.02)
            self._gyro_dir = _jitter_direction(self._gyro_dir, 0.02)
            mag_a = random.uniform(0.00, 0.05)
            mag_g = random.uniform(0.0, 5.0)
            return _pkt("watch_thrown",
                        _scale(self._acc_dir, mag_a),
                        _scale(self._gyro_dir, mag_g),
                        None, 0)

    def _fall_like(self) -> dict:
        """
        fall_like_motion: 강한 손목 흔들기, 의자에 털썩 앉기 같은 fall-유사 동작.
        spike 후 곧바로 보행 복귀 → post-quiet 없음 → WARNING 또는 NORMAL.
        """
        t = self.tick

        if t < _LIKE_PRE:
            # pre: 보행
            self._walk_phase += 1.9
            wave = abs(math.sin(self._walk_phase))
            mag_a = 0.10 + 0.30 * wave + random.uniform(-0.02, 0.03)
            mag_a = max(0.08, min(0.50, mag_a))
            mag_g = 15.0 + 40.0 * wave + random.uniform(-3.0, 5.0)
            mag_g = max(15.0, min(80.0, mag_g))
            self._acc_dir  = _jitter_direction(self._acc_dir, 0.15)
            self._gyro_dir = _jitter_direction(self._gyro_dir, 0.18)
            hr = self._hr.next(72, 88, smoothing=0.15)
            return _pkt("fall_like_motion",
                        _scale(self._acc_dir, mag_a),
                        _scale(self._gyro_dir, mag_g),
                        hr, 1)

        elif t < _LIKE_PRE + _SPIKE_TICKS:
            # spike: acc 1.3-3.2 g (warn 1.3 g 는 항상 넘기고 fall 2.8 g 는
            # 약 30~40% 확률로 넘김), gyro 100-260 deg/s (gyro 임계 250 도 가끔)
            # → 임계를 넘기는 시행에서는 impact +0.30 받아 WARNING.
            # → post-quiet 은 다음 phase 가 walking 으로 곧장 복귀하므로 없음.
            #    EMERGENCY_FALL 로는 절대 가지 않음.
            mag_a = random.uniform(1.3, 3.2)
            mag_g = random.uniform(100.0, 260.0)
            self._acc_dir  = _jitter_direction(self._acc_dir, 0.8)
            self._gyro_dir = _jitter_direction(self._gyro_dir, 0.8)
            hr = self._hr.next(82, 95, smoothing=0.20)
            return _pkt("fall_like_motion",
                        _scale(self._acc_dir, mag_a),
                        _scale(self._gyro_dir, mag_g),
                        hr, 1)

        else:
            # post: 곧바로 walking 수준 복귀 — post-quiet 없음
            self._walk_phase += 1.9
            wave = abs(math.sin(self._walk_phase))
            mag_a = 0.10 + 0.30 * wave + random.uniform(-0.02, 0.05)
            mag_a = max(0.08, min(0.45, mag_a))
            mag_g = 18.0 + 55.0 * wave + random.uniform(-3.0, 5.0)
            mag_g = max(15.0, min(80.0, mag_g))
            self._acc_dir  = _jitter_direction(self._acc_dir, 0.15)
            self._gyro_dir = _jitter_direction(self._gyro_dir, 0.18)
            hr = self._hr.next(82, 95, smoothing=0.15)
            return _pkt("fall_like_motion",
                        _scale(self._acc_dir, mag_a),
                        _scale(self._gyro_dir, mag_g),
                        hr, 1)
