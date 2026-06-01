"""전역 설정 — 모든 모듈이 이 파일에서 상수를 가져온다.

v3 변경 사항:
- 초기 사용자 풀이 비어 있음 (실무 시나리오: 관리자가 직접 디바이스 등록)
- 사용자 식별: 자동 부여 ID(U1, U2, …) + MAC 주소
- 사용자 메타: 이름 + MAC만
- 등록 시 기본 모드는 'auto' — 자동 시퀀스 진행

v4 변경 사항 (race condition 수정):
- runtime_config.json은 3개 프로세스(대시보드/송신기/수신기)가 동시에
  read-modify-write 한다. 락 없이 작업하면 stale snapshot 덮어쓰기로
  - 사용자 ID 중복 부여 (Carol과 Dave가 같은 U3)
  - 새로 등록한 사용자가 사라짐
  - 제거 시 이전 사용자 이름이 다른 슬롯을 덮어씀
  같은 데이터 정합성 문제가 발생한다.
- 해결: 모든 read-modify-write를 mutate_runtime_cfg() critical section으로
  묶고, OS의 파일 락(POSIX flock / Windows msvcrt)으로 직렬화한다.
- 쓰기는 항상 임시 파일 → os.replace로 atomic하게 수행해서 partial-write
  상태의 JSON을 다른 프로세스가 읽는 일이 없도록 한다.
"""
import os
import json
import re
import sys
import tempfile
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")

UDP_HOST = "127.0.0.1"
UDP_PORT = 9999

SESSION_FILE     = os.path.join(DATA_DIR, "session.jsonl")
EVENT_LOG_FILE   = os.path.join(DATA_DIR, "event_log.csv")
RUNTIME_CFG_FILE = os.path.join(DATA_DIR, "runtime_config.json")
# 락 전용 파일 — runtime_config.json 자체를 락 대상으로 쓰면 atomic
# rename(임시 파일 → 원본) 시점에 락 파일이 교체되어 다른 프로세스가 잡고
# 있던 fd의 락이 의미를 잃을 수 있다. 별도 파일을 두면 안전하다.
RUNTIME_LOCK_FILE = os.path.join(DATA_DIR, "runtime_config.lock")


# ── 사용자별 기본 상태 ───────────────────────────────────────────────────────

def _default_user_state() -> dict:
    """새로 등록된 사용자의 초기 상태.

    scenario_mode:
      'auto'   — 자동 시퀀스 진행 (sender가 시간 흐름에 따라 다양한 상황 생성)
      'manual' — 관리자가 active_scenario를 지정한 상태

    기본값은 'manual' + 'normal_idle'. 발표자가 직접 시나리오를 선택할
    때까지 사용자는 NORMAL 상태로만 대기한다. (v8 이전 기본 'auto' 는
    시연 중 의도하지 않은 fall_like_motion / watch_thrown 등이 자동으로
    돌아서 "내가 누르지도 않았는데 경고가 뜬다" 는 문제를 만들었다.)

    packet_loss_rate:
      None  — 전역 packet_loss_rate (DEFAULTS) 사용
      0.0~1.0 — 이 사용자만의 손실률로 override
    """
    return {
        "scenario_mode":         "manual",          # 기본: 수동 (NORMAL 대기)
        "active_scenario":       "normal_idle",
        "scenario_version":      0,
        "user_action":           None,             # "im_ok" | "emergency_confirmed"
        "user_action_version":   0,
        "packet_loss_rate":      None,             # None → 전역값 사용
    }


DEFAULTS = {
    # ── 전역 네트워크 설정 ─────────────────────────────────────────────────
    "packet_loss_rate": 0.15,
    "delay_min": 0.05,
    "delay_max": 0.4,
    "send_interval": 0.3,

    # ── 전역 판정 임계값 ──────────────────────────────────────────────────
    #
    # 본 시스템의 acc_magnitude 는 **dynamic / user acceleration magnitude**
    # 기준으로 통일되어 있다 (단위 g, 중력 성분 제거됨).
    # 즉 완전 정지 상태는 0g 근처. 1g 근처가 아님.
    # 이 약속에 따라 모든 시뮬레이터 / 임계값 / 차트 가이드 라인이 같은
    # 기준을 공유한다.
    #
    # warn_threshold = 1.3 g (WARNING 진입)
    #   근거: Bourke et al. (Gait & Posture 2007) 의 dynamic acc 기준 LFT
    #   0.73 g 와 UFT 1.79 g 의 중간값. wrist 위치에서 일상 동작 (running,
    #   강한 손목 흔들기) 와 fall 후보를 분리하는 1차 게이트.
    #
    # fall_threshold = 2.8 g (EMERGENCY 후보)
    #   근거: Bourke 2007 UFT 1.79 g, Bagalà et al. (PLOS ONE 2012) UFT
    #   2.8 g (real-world fall 평가에서 보고된 값). 본 시스템은 wrist 위치를
    #   가정하므로 보수적으로 2.8 g 채택.
    #
    # gyro_threshold = 250 deg/s
    #   근거: Bourke & Lyons (Med Eng Phys 2008) 의 wrist 자이로 임계 ~190
    #   deg/s 보다 보수적인 값. wrist 위치에서 walking 시 자이로가 가끔
    #   100 deg/s 까지 튀는 점을 고려.
    "fall_threshold": 2.8,
    "warn_threshold": 1.3,
    "gyro_threshold": 250.0,
    "interp_max_gap": 1.2,

    # ── 전역 제어 ──────────────────────────────────────────────────────────
    "reset_version": 0,
    "session_start_ts": 0.0,
    "paused": False,

    # ── 사용자 풀 — 초기에는 비어 있다. 관리자가 대시보드에서 등록한다. ──
    "users": {},
    "user_meta": {},
    # 다음에 부여할 user_id 카운터 (U1, U2, U3, …)
    "next_user_num": 1,
}

RING_BUFFER_SIZE = 200

# ── 판정 윈도우 / post-impact 정지 판정 ────────────────────────────────────
#
# DECISION_WINDOW = 12 패킷
#   사용자당 송신 주기 ≒ 1.2초 → 12 패킷 ≒ 14.4초 윈도우.
#   Zhang et al. (J Med Internet Res 2024, DSCS) 의 12초 / 8초 세그먼트와
#   같은 자릿수.
#
# POST_QUIET_ACC = 0.12 g (post-impact 정지 임계값, dynamic acc 기준)
#   근거: Abbate et al. (Pervasive Mobile Computing 2012) 의 post-impact
#   "free of intentional movement" 구간을 dynamic acc 로 환산. 중력 성분을
#   제거한 상태에서 사람이 의식 잃고 누운 상태는 acc magnitude 가 0.05~0.10 g
#   수준으로 거의 0 에 수렴한다. 보수적으로 0.12 g 미만을 정지로 본다.
#
# POST_QUIET_GYRO = 20 deg/s (post-impact 회전 정지 임계값)
#   acc 가 일시적으로 작아도 자이로가 크면 아직 움직이는 중이다. 두 신호
#   모두 정지 임계 미만이어야 "정지" 로 인정.
#
# POST_QUIET_MIN = 2 패킷 (≒ 2.4초 정지 지속)
#   근거: Bourke et al. (Gait & Posture 2010), Abbate 2012. post-fall
#   inactivity ≥ 2초 가 fall 판정의 필요조건.
DECISION_WINDOW  = 12
POST_QUIET_ACC   = 0.12
POST_QUIET_GYRO  = 20.0
POST_QUIET_MIN   = 2


# ── MAC 주소 검증 ────────────────────────────────────────────────────────────

_MAC_PATTERN = re.compile(r"^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$")


def is_valid_mac(mac: str) -> bool:
    """6옥텟 16진수 MAC 주소 형식 검증 (XX:XX:XX:XX:XX:XX 또는 XX-XX-XX-XX-XX-XX)."""
    return bool(_MAC_PATTERN.match((mac or "").strip()))


def normalize_mac(mac: str) -> str:
    """MAC을 대문자 + 콜론 구분 형태로 정규화."""
    s = (mac or "").strip().upper().replace("-", ":")
    return s


# ── 파일 락 (크로스플랫폼) ────────────────────────────────────────────────────
#
# 대시보드 / 송신기 / 수신기 세 프로세스가 동시에 runtime_config.json을
# read-modify-write 한다. 락이 없으면 다음 같은 race가 자주 발생한다:
#
#   T0  receiver: load cfg   (users={U1,U2,U3}, next=4)
#   T1  dashboard: load cfg  (users={U1,U2,U3}, next=4)
#   T2  dashboard: add U4    (users={U1,U2,U3,U4}, next=5) → save
#   T3  receiver: scenario_version++ on its stale snapshot → save
#       → 결과: U4 사라짐, next_user_num도 4로 되돌아감
#
# 다음 등록 사이클에서 next=4가 다시 부여되어 새 사용자가 U4가 되고,
# 사용자 보기에는 "동일한 ID가 두 명에게 부여된 것처럼" 보인다.
# 이걸 막으려면 read-modify-write 전체를 단일 critical section으로 직렬화해야 한다.

if sys.platform == "win32":
    import msvcrt

    def _acquire_lock(fd):
        # Windows: 1바이트 영역 락(블로킹). 같은 파일에 동시에 잡으면 대기.
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)

    def _release_lock(fd):
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _acquire_lock(fd):
        fcntl.flock(fd, fcntl.LOCK_EX)

    def _release_lock(fd):
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


@contextmanager
def _cfg_file_lock():
    """runtime_config 작업의 critical section.

    별도 락 파일에 OS 락을 잡는다. 락 파일은 처음 진입 시 자동 생성되며
    한 번 만들어진 뒤에는 fd만 잠그므로 atomic rename 같은 동시 작업에도
    안전하다.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    # Windows에서는 잠그려는 파일 영역에 데이터가 실제로 존재해야 LK_LOCK이
    # 성공한다. 처음 만들 때 1바이트를 써둔다.
    if not os.path.exists(RUNTIME_LOCK_FILE):
        with open(RUNTIME_LOCK_FILE, "wb") as _init:
            _init.write(b"\0")
    fd = os.open(RUNTIME_LOCK_FILE, os.O_RDWR)
    try:
        _acquire_lock(fd)
        yield
    finally:
        _release_lock(fd)
        os.close(fd)


# ── 파일 I/O (락 없이 호출되는 저수준 함수) ───────────────────────────────────
#
# 외부에서는 절대 직접 호출하지 말 것. 항상 load_runtime_cfg() /
# save_runtime_cfg() / mutate_runtime_cfg()를 통해 락을 거쳐 들어와야 한다.

def _load_unlocked() -> dict:
    """락을 잡지 않고 디스크에서 cfg를 읽는다. 호출자가 락을 보장해야 한다."""
    try:
        with open(RUNTIME_CFG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 누락된 최상위 키 보완 (users / user_meta는 파일 값을 신뢰)
        for k, v in DEFAULTS.items():
            if k not in data:
                if k in ("users", "user_meta"):
                    data[k] = {}
                elif isinstance(v, dict):
                    data[k] = dict(v)
                else:
                    data[k] = v
        # 각 사용자의 내부 키 보완
        for uid in list(data["users"].keys()):
            for k, v in _default_user_state().items():
                data["users"][uid].setdefault(k, v)
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return _deepcopy_defaults()


def _save_unlocked(cfg: dict):
    """락을 잡지 않고 디스크에 atomic하게 쓴다. 호출자가 락을 보장해야 한다.

    같은 디렉터리의 임시 파일에 전체를 쓴 뒤 os.replace로 교체한다.
    os.replace는 같은 파일시스템 내에서 atomic이므로, 다른 프로세스가
    partial JSON을 읽는 문제가 발생하지 않는다.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".runtime_config_", suffix=".tmp", dir=DATA_DIR
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, RUNTIME_CFG_FILE)
    except Exception:
        # 임시 파일 정리 (replace 실패 시)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _deepcopy_defaults() -> dict:
    return json.loads(json.dumps(DEFAULTS))


# ── 공개 I/O API (모두 락 안에서 동작) ────────────────────────────────────────

def load_runtime_cfg() -> dict:
    """런타임 설정을 읽어 dict로 반환 (락을 잡고 일관된 스냅샷 보장)."""
    with _cfg_file_lock():
        return _load_unlocked()


def save_runtime_cfg(cfg: dict):
    """전체 cfg를 통째로 덮어쓴다.

    ⚠ 이 함수는 read-modify-write를 하지 않으므로, 호출자가 직전에 읽은
    cfg가 stale일 수 있다. 외부 변경과 충돌할 가능성이 있는 경우에는
    save_runtime_cfg 대신 mutate_runtime_cfg()를 사용할 것.
    """
    with _cfg_file_lock():
        _save_unlocked(cfg)


# dict는 built-in이라 attribute monkey-patch가 불가능하다.
# 호환을 위한 가벼운 서브클래스로 dict 인터페이스는 유지하면서
# mark_dirty / mark_clean 메서드만 추가한다.
class _MutableCfg(dict):
    __slots__ = ("_dirty",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._dirty = True    # 기본은 dirty (호환성: 명시 안 하면 항상 저장)

    def mark_dirty(self):
        self._dirty = True

    def mark_clean(self):
        self._dirty = False

    def is_dirty(self) -> bool:
        return self._dirty


@contextmanager
def mutate_runtime_cfg():
    """read-modify-write를 단일 critical section으로 묶는 컨텍스트 매니저.

    사용 패턴 1 — 단순 수정 (블록 종료 시 항상 저장):
        with mutate_runtime_cfg() as cfg:
            cfg["users"][user_id] = ...

    사용 패턴 2 — 조건부 저장 (변경이 없으면 디스크 쓰기 생략):
        with mutate_runtime_cfg() as cfg:
            cfg.mark_clean()             # 기본 dirty를 끄고 시작
            if 어떤_조건:
                cfg["..."] = ...
                cfg.mark_dirty()         # 명시적으로 변경 표시
        # mark_dirty가 호출되지 않으면 save를 건너뛴다.

    수신기는 매 ~0.3초마다 이 컨텍스트를 진입하므로, 변경이 없을 때
    무조건 save하면 락 경합과 디스크 I/O가 누적된다. mark_clean →
    조건부 mark_dirty 패턴으로 실제 변경 사이클에만 저장한다.

    예외가 발생하면 저장하지 않고 락만 해제한다.
    """
    with _cfg_file_lock():
        raw = _load_unlocked()
        cfg = _MutableCfg(raw)
        yield cfg
        if cfg.is_dirty():
            # 저장 직전에 dict 인터페이스만 남긴 plain dict로 변환 (json.dump가
            # 서브클래스도 처리하지만, 보수적으로 plain dict로 직렬화).
            _save_unlocked(dict(cfg))


# ── 공개 API ──────────────────────────────────────────────────────────────────

def list_user_ids() -> list:
    cfg = load_runtime_cfg()
    return list(cfg.get("users", {}).keys())


def get_user_meta(user_id: str) -> dict:
    cfg = load_runtime_cfg()
    return cfg.get("user_meta", {}).get(user_id, {"name": user_id, "mac": "—"})


def add_user(name: str, mac: str) -> tuple:
    """
    새 사용자(웨어러블 디바이스)를 등록한다.

    인자:
      name — 사용자 이름
      mac  — 디바이스 MAC 주소 (XX:XX:XX:XX:XX:XX)

    반환: (성공 여부 bool, 메시지 str, 부여된 user_id 또는 None)

    내부적으로 mutate_runtime_cfg()로 락을 잡고 read-modify-write를 수행해
    동시에 호출되어도 ID 중복 부여가 발생하지 않는다.
    """
    name = (name or "").strip()
    if not name:
        return False, "이름을 입력하세요", None

    if not is_valid_mac(mac):
        return False, "MAC 주소 형식이 올바르지 않습니다 (예: A1:B2:C3:D4:E5:F6)", None

    mac_norm = normalize_mac(mac)

    with mutate_runtime_cfg() as cfg:
        # MAC 중복 검사 — 락 안에서 수행되므로 두 동시 호출자가 같은 MAC을
        # 등록하려 해도 한 쪽만 성공한다.
        for uid, meta in cfg.get("user_meta", {}).items():
            if meta.get("mac") == mac_norm:
                # 변경이 없으므로 디스크 쓰기 생략 (락만 해제)
                cfg.mark_clean()
                return False, f"이미 등록된 MAC 주소입니다 ({uid})", None

        # 새 user_id 부여
        num = cfg.get("next_user_num", 1)
        user_id = f"U{num}"
        cfg["next_user_num"] = num + 1

        cfg["users"][user_id]     = _default_user_state()
        cfg["user_meta"][user_id] = {"name": name, "mac": mac_norm}

    return True, f"디바이스 등록 완료: {user_id} ({name})", user_id


def remove_user(user_id: str) -> tuple:
    """사용자 제거. 반환: (성공 여부, 메시지)

    add_user와 동일한 락 보호 하에서 동작한다. receiver의 stale snapshot이
    이미 제거된 사용자를 다시 살리거나 user_meta를 덮어쓰는 race를
    방지한다.
    """
    with mutate_runtime_cfg() as cfg:
        if user_id not in cfg.get("users", {}):
            cfg.mark_clean()
            return False, f"존재하지 않는 사용자: {user_id}"
        cfg["users"].pop(user_id, None)
        cfg.get("user_meta", {}).pop(user_id, None)
    return True, f"디바이스 제거 완료: {user_id}"


# ── 시스템 부트 초기화 ────────────────────────────────────────────────────────
#
# 시스템(receiver / sender / dashboard) 각 프로세스가 가동되는 시점에 호출되어
# 이전 데모 세션의 잔재(이벤트 로그, 패킷 로그, 등록된 사용자, 사용자별 상태)를
# 모두 깨끗하게 비운다. 데모 시작 시 항상 빈 슬레이트에서 출발하기 위한 함수다.
#
# 동시 실행 안전성:
#   세 프로세스가 거의 동시에 시작되어도 _cfg_file_lock으로 직렬화되며, 작업은
#   멱등하다 (빈 상태에서 한 번 더 비우는 것은 무해). 락 안에서 파일 truncate를
#   수행하므로 다른 프로세스가 부분적으로 쓰여진 파일을 보는 일도 없다.
#
# 멱등성:
#   같은 부트 토큰(boot_token)으로 두 번 호출되면 두 번째는 no-op. Streamlit
#   대시보드처럼 한 프로세스 안에서 스크립트가 여러 번 rerun되는 환경에서도
#   안전하게 호출할 수 있다.

def reset_system_state(boot_token: str | None = None) -> bool:
    """이전 세션 데이터를 모두 비우고 시스템을 깨끗한 상태로 초기화한다.

    인자:
      boot_token — 이 프로세스의 부트 식별자. None이면 매번 초기화를 수행한다.
                   같은 token으로 다시 호출되면 멱등하게 건너뛴다.

    반환:
      실제로 초기화가 수행되었으면 True, 건너뛰면 False.

    중복 호출 방지 (두 단계):
      1) 프로세스 내 — _LAST_RESET_TOKEN: 같은 Python 프로세스 안에서 같은
         boot_token으로 두 번째 호출은 no-op (Streamlit rerun 등에서 매번
         호출되어도 안전).
      2) 프로세스 간 — 부트 윈도우: receiver / sender / dashboard가 거의
         동시에 시작될 때, 다른 프로세스가 _BOOT_WINDOW_SEC 이내에 이미
         초기화했다면 같은 "부트 사이클"로 간주하고 건너뛴다. 이렇게 하면
         첫 프로세스 reset 직후 두 번째가 들어와 데이터를 또 비우는 일이
         없다. 데모 도중 한 프로세스만 재시작하더라도(예: dashboard 재기동)
         최근 부트 윈도우 밖이라면 의도대로 reset이 다시 한 번 수행된다.

    초기화 대상:
      - data/event_log.csv : 비움
      - data/session.jsonl : 비움
      - runtime_config.json: 디폴트로 리셋 (users / user_meta 비움 등),
                             reset_version은 in-memory 상태 트리거용으로 갱신
    """
    global _LAST_RESET_TOKEN
    if boot_token is not None and _LAST_RESET_TOKEN == boot_token:
        return False    # 같은 부트 사이클 내 두 번째 호출 — 건너뛰기

    import time as _time
    with _cfg_file_lock():
        # 부트 윈도우 가드: 다른 프로세스가 방금 비웠다면 건너뛴다.
        try:
            mtime = os.path.getmtime(_BOOT_MARKER_FILE)
            if _time.time() - mtime < _BOOT_WINDOW_SEC:
                _LAST_RESET_TOKEN = boot_token
                return False
        except OSError:
            pass    # 마커 없음 → 첫 부트

        # 1) 데이터 파일 비우기
        for path in (EVENT_LOG_FILE, SESSION_FILE):
            try:
                if os.path.exists(path):
                    if sys.platform == "win32":
                        with open(path, "w", encoding="utf-8") as f:
                            f.truncate(0)
                    else:
                        os.unlink(path)
            except OSError:
                try:
                    with open(path, "w", encoding="utf-8") as f:
                        f.truncate(0)
                except OSError:
                    pass

        # 2) runtime_config.json 디폴트로 초기화
        cfg = _deepcopy_defaults()
        cfg["reset_version"] = int(_time.time())
        cfg["session_start_ts"] = 0.0
        _save_unlocked(cfg)

        # 3) 부트 마커 갱신 — 곧이어 들어오는 다른 프로세스가 윈도우 안에서
        #    중복 reset을 시도하지 않도록.
        try:
            with open(_BOOT_MARKER_FILE, "w") as f:
                f.write(str(_time.time()))
        except OSError:
            pass

    _LAST_RESET_TOKEN = boot_token
    return True


# 같은 부트 사이클로 간주하는 시간 윈도우 (초). 보통 receiver → sender →
# dashboard를 차례로 켜는 데 몇 초면 충분하다. 윈도우가 너무 짧으면 의도된
# 부트 동기화가 깨지고, 너무 길면 데모 도중 의도적으로 재시작했을 때 reset이
# 건너뛰어진다. 30초가 적절한 절충.
_BOOT_WINDOW_SEC = 30.0
_BOOT_MARKER_FILE = os.path.join(DATA_DIR, ".boot_marker")

# 부트 토큰 — 같은 Python 프로세스 안에서 reset_system_state가 두 번
# 호출되어도 두 번째는 멱등하게 건너뛰도록 추적한다.
_LAST_RESET_TOKEN: str | None = None
