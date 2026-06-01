"""
중앙 모니터링 대시보드 v5 — 단일 페이지·KREAM 스타일·탭 기반 상세 영역.

실행: streamlit run dashboard/app.py

핵심 설계:
- 첫 실행 시 사용자 0명. 사이드바에서 [이름 + MAC]만 입력하면 등록.
- 메인은 미니멀 카드 그리드 — 카드에는 이름·MAC·상태 칩만.
- 카드 클릭 → 같은 페이지에서 아래에 상세 패널이 펼쳐짐.
  상세 패널 안에 두 개 탭:
    Tab 1) 실시간 차트: acc/gyro/HR/skin_contact 시계열 + 정보 + 시나리오 제어
    Tab 2) 워치 화면: Apple Watch 스타일 시뮬레이션 + I'm OK / Emergency SOS
- 디폴트 자동 시퀀스. 관리자(=발표자)가 수동으로 시나리오 강제 가능.
- 카드의 × 버튼으로 사용자 제거.
- 사이드바 등록 폼: 이름 비어 있을 때는 조용히 무시 (빨간 검증 박스 X).
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import json
import time
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import (
    SESSION_FILE,
    EVENT_LOG_FILE,
    DECISION_WINDOW,
    load_runtime_cfg,
    save_runtime_cfg,
    mutate_runtime_cfg,
    get_user_meta,
    add_user,
    remove_user,
    reset_system_state,
)

st.set_page_config(
    page_title="Fall Monitor",
    page_icon="●",
    layout="wide",
    initial_sidebar_state="expanded",
)

# 대시보드 프로세스 가동 시 한 번만 이전 세션 데이터를 비운다.
#
# Streamlit은 매 사용자 인터랙션마다 스크립트 전체를 다시 실행하지만, 모듈
# 글로벌 변수는 같은 Python 프로세스 안에서 rerun을 가로질러 유지된다. 따라서
# `_BOOT_TOKEN`을 모듈 전역으로 두고 reset_system_state의 멱등 가드에 넘기면
# 프로세스 첫 진입에서만 실제 초기화가 수행되고, 이후 rerun에서는 no-op이 된다.
# receiver / sender가 이미 같은 부트 사이클에서 비웠더라도 락 안에서 같은
# 깨끗한 상태 위에 한 번 더 적용되는 것뿐이라 무해하다.
_BOOT_TOKEN = f"dashboard-{os.getpid()}"
if reset_system_state(boot_token=_BOOT_TOKEN):
    # 콘솔(터미널)에만 한 번 표시. UI에는 노출하지 않는다.
    print(f"[대시보드] 부트 초기화 완료 — 이전 세션 데이터 비움 (pid={os.getpid()})")


# ════════════════════════════════════════════════════════════════════════════
# 상수
# ════════════════════════════════════════════════════════════════════════════

DECISION_KR = {
    "NORMAL":                   "정상",
    "WARNING":                  "경고",
    "EMERGENCY_FALL":           "긴급 낙상",
    "DEVICE_REMOVED":           "기기 탈착",
    "DEVICE_THROWN_OR_DROPPED": "기기 투척",
    "DATA_UNCERTAIN":           "데이터 불확실",
}

# 상태 칩: (라벨, 글자색, 배경색, 보더색)
CHIP_STYLE = {
    "NORMAL":                   ("정상",        "#0f0f0f", "#ffffff", "#e5e5e5"),
    "WARNING":                  ("경고",        "#7a5500", "#fff8e1", "#f0d68b"),
    "EMERGENCY_FALL":           ("낙상 감지",   "#ffffff", "#ef2027", "#ef2027"),
    "DEVICE_REMOVED":           ("미착용",      "#4a4a4a", "#f4f4f5", "#e5e5e5"),
    "DEVICE_THROWN_OR_DROPPED": ("이상 신호",   "#ffffff", "#181818", "#181818"),
    "DATA_UNCERTAIN":           ("통신 불안정", "#4a4a4a", "#f4f4f5", "#e5e5e5"),
}

SCENARIO_OPTIONS = [
    "normal_idle",
    "walking",
    "running",
    "real_fall",
    "watch_thrown",
    "watch_removed",
    "fall_like_motion",
]

SCENARIO_KR = {
    "normal_idle":      "정상 대기",
    "walking":          "보행",
    "running":          "달리기",
    "real_fall":        "실제 낙상",
    "watch_thrown":     "기기 투척",
    "watch_removed":    "기기 탈착",
    "fall_like_motion": "낙상 유사 동작",
}


# ════════════════════════════════════════════════════════════════════════════
# 데이터 로딩
# ════════════════════════════════════════════════════════════════════════════

def load_session_data(n_lines: int = 1500, session_start_ts: float = 0.0) -> pd.DataFrame:
    """session.jsonl의 마지막 n줄을 DataFrame으로 로드 (세션 시작 이후만)."""
    if not os.path.exists(SESSION_FILE):
        return pd.DataFrame()
    try:
        with open(SESSION_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()[-n_lines:]
        rows = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        # 세션 초기화 이후의 패킷만 표시. session.jsonl 레코드는 sender가
        # 찍은 'timestamp' 필드를 가진다 (recv_ts가 아님).
        if session_start_ts > 0 and "timestamp" in df.columns:
            df = df[pd.to_numeric(df["timestamp"], errors="coerce") >= session_start_ts]
        # 숫자 컬럼 정규화 (차트 그릴 때 필요)
        for col in ("acc_magnitude", "gyro_magnitude", "heart_rate"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df.reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


def latest_user_state(df: pd.DataFrame, user_id: str) -> dict:
    """특정 사용자의 가장 최근 한 행을 dict로 반환."""
    if df.empty or "user_id" not in df.columns:
        return {}
    sub = df[df["user_id"] == user_id]
    if sub.empty:
        return {}
    return sub.iloc[-1].to_dict()


def user_history(df: pd.DataFrame, user_id: str, n_recent: int = 80) -> pd.DataFrame:
    """특정 사용자의 최근 n개 패킷 시계열."""
    if df.empty or "user_id" not in df.columns:
        return pd.DataFrame()
    sub = df[df["user_id"] == user_id].tail(n_recent).reset_index(drop=True)
    return sub


def load_event_log(n_rows: int = 25, session_start_ts: float = 0.0) -> pd.DataFrame:
    """이벤트 로그 csv에서 최근 n_rows 행을 효율적으로 읽어온다.

    v15부터 매 패킷이 csv에 기록되므로 파일이 매우 커질 수 있다. 이전처럼
    `pd.read_csv(전체 파일)` → `df.tail(25)` 패턴은 매 1초 새로고침마다
    수만 행을 파싱하느라 점점 느려진다. 따라서 파일 끝에서 일정 바이트만
    역방향으로 읽어 마지막 N행만 파싱하는 방식으로 최적화한다.

    파일이 작을 때(약 64KB 이하)는 단순히 전체 읽기를 폴백으로 사용한다.
    """
    if not os.path.exists(EVENT_LOG_FILE):
        return pd.DataFrame()
    try:
        file_size = os.path.getsize(EVENT_LOG_FILE)

        # 작은 파일: 전체 읽기가 빠르고 안전하다 (헤더 처리도 자동).
        SMALL_FILE_THRESHOLD = 64 * 1024  # 64KB
        if file_size <= SMALL_FILE_THRESHOLD:
            df = pd.read_csv(EVENT_LOG_FILE)
        else:
            # 큰 파일: 헤더 한 줄 + 끝에서 일정 바이트만 읽어 마지막 행들 파싱.
            # tail(n_rows)에 충분한 여유 = n_rows * 평균행길이(약 200B) * 4배 안전.
            tail_bytes = max(16 * 1024, n_rows * 200 * 4)
            with open(EVENT_LOG_FILE, "rb") as f:
                header = f.readline()                  # 첫 줄(헤더)
                # 파일 끝에서 tail_bytes만큼 점프
                f.seek(max(len(header), file_size - tail_bytes))
                # 부분 라인은 버리고 다음 줄부터
                f.readline()
                tail_data = f.read()
            import io
            buf = io.BytesIO(header + tail_data)
            df = pd.read_csv(buf)

        # 세션 시작 이전 로그는 화면에서 제외.
        if session_start_ts > 0 and "time" in df.columns:
            df = df[pd.to_numeric(df["time"], errors="coerce") >= session_start_ts]
        return df.tail(n_rows)
    except Exception:
        return pd.DataFrame()


# ════════════════════════════════════════════════════════════════════════════
# 글로벌 스타일 — KREAM 풍 미니멀
# ════════════════════════════════════════════════════════════════════════════

def inject_global_styles():
    st.markdown(
        """
        <style>
        /* ── 베이스 ─────────────────────────────────────────────────────── */
        html, body, [class*="css"] {
            font-family: -apple-system, BlinkMacSystemFont, "Pretendard",
                         "Apple SD Gothic Neo", "Segoe UI", "Helvetica Neue",
                         "맑은 고딕", "Malgun Gothic", sans-serif;
            color: #0f0f0f;
        }
        .main .block-container {
            padding-top: 2.2rem;
            padding-bottom: 6rem;
            max-width: 1280px;
        }
        /* 사이드바 */
        section[data-testid="stSidebar"] {
            background: #fafafa;
            border-right: 1px solid #ececec;
        }
        section[data-testid="stSidebar"] .block-container { padding-top: 1.5rem; }

        /* ── 헤더 ───────────────────────────────────────────────────────── */
        .app-header {
            display: flex; align-items: baseline; gap: 14px;
            margin-bottom: 4px;
        }
        .app-title {
            font-size: 32px; font-weight: 800; letter-spacing: -0.02em;
            color: #0f0f0f;
        }
        .app-subtitle {
            font-size: 13px; color: #9a9a9a; font-weight: 500;
        }
        .app-tagline {
            font-size: 13px; color: #6b6b6b; margin-bottom: 28px;
        }

        /* ── 섹션 라벨 ──────────────────────────────────────────────────── */
        .section-label {
            font-size: 11px; font-weight: 700; letter-spacing: 0.14em;
            text-transform: uppercase; color: #9a9a9a;
            margin: 28px 0 14px 0;
        }

        /* ── 상태 요약 바 ──────────────────────────────────────────────── */
        .summary-bar {
            display: flex; gap: 0; border: 1px solid #ececec;
            border-radius: 4px; overflow: hidden;
            background: #ffffff;
        }
        .summary-cell {
            flex: 1; padding: 14px 16px; border-right: 1px solid #ececec;
        }
        .summary-cell:last-child { border-right: none; }
        .summary-cell-label {
            font-size: 11px; color: #9a9a9a; letter-spacing: 0.06em;
            text-transform: uppercase; margin-bottom: 6px;
        }
        .summary-cell-value {
            font-size: 22px; font-weight: 800; color: #0f0f0f;
        }
        .summary-cell-value.alert { color: #ef2027; }

        /* ── EMERGENCY 배너 ─────────────────────────────────────────────── */
        .emergency-banner {
            background: #ef2027; color: #ffffff;
            padding: 14px 20px;
            border-radius: 4px;
            display: flex; justify-content: space-between; align-items: center;
            margin-bottom: 16px;
            font-weight: 700;
            animation: blink 1.4s ease-in-out infinite;
        }
        @keyframes blink {
            0%, 100% { opacity: 1; }
            50%      { opacity: 0.55; }
        }
        .emergency-banner-label {
            font-size: 11px; letter-spacing: 0.18em; text-transform: uppercase;
            opacity: 0.85; margin-right: 12px;
        }
        .emergency-banner-text {
            font-size: 15px;
        }
        .emergency-banner-count {
            font-size: 28px; font-weight: 800;
        }

        /* ── 디바이스 카드 ──────────────────────────────────────────────── */
        .device-card {
            border: 1px solid #ececec; border-radius: 6px;
            background: #ffffff;
            padding: 22px 22px 20px 22px;
            position: relative;
            transition: border-color 0.15s, box-shadow 0.15s;
            min-height: 132px;
        }
        .device-card:hover {
            border-color: #b9b9b9;
            box-shadow: 0 2px 12px rgba(0,0,0,0.04);
        }
        .device-card.emergency {
            border: 1.5px solid #ef2027;
            box-shadow: 0 0 0 2px rgba(239,32,39,0.08);
        }
        .device-card.warning {
            border-color: #f0d68b;
        }
        .device-card.opened {
            border-color: #0f0f0f;
            box-shadow: 0 2px 16px rgba(0,0,0,0.06);
        }
        .device-card-name {
            font-size: 20px; font-weight: 800;
            color: #0f0f0f; letter-spacing: -0.01em;
            margin-bottom: 4px;
        }
        .device-card-mac {
            font-family: ui-monospace, "SF Mono", Menlo, monospace;
            font-size: 12px; color: #9a9a9a;
            margin-bottom: 16px;
            letter-spacing: 0.02em;
        }
        .chip {
            display: inline-block;
            font-size: 12px; font-weight: 700;
            padding: 5px 11px; border-radius: 999px;
            line-height: 1.2;
        }

        /* ── 빈 상태 (사용자 0명) ────────────────────────────────────── */
        .empty-state {
            border: 1px dashed #d9d9d9; border-radius: 6px;
            padding: 56px 28px;
            text-align: center;
            background: #fafafa;
        }
        .empty-state-icon {
            font-size: 28px; margin-bottom: 12px; color: #c5c5c5;
        }
        .empty-state-title {
            font-size: 16px; font-weight: 700; color: #0f0f0f;
            margin-bottom: 6px;
        }
        .empty-state-desc {
            font-size: 13px; color: #6b6b6b; line-height: 1.5;
        }

        /* ── 상세 패널 (카드 클릭 시 펼쳐짐) ───────────────────────────── */
        .detail-panel {
            border: 1px solid #0f0f0f;
            border-radius: 8px;
            padding: 28px 32px;
            background: #ffffff;
            margin-top: 16px;
        }
        .detail-header {
            display: flex; justify-content: space-between; align-items: baseline;
            margin-bottom: 6px;
        }
        .detail-name {
            font-size: 28px; font-weight: 800;
            color: #0f0f0f; letter-spacing: -0.02em;
        }
        .detail-uid {
            font-size: 13px; color: #9a9a9a; margin-left: 10px;
        }
        .detail-mac {
            font-family: ui-monospace, "SF Mono", Menlo, monospace;
            font-size: 12px; color: #9a9a9a;
            margin-bottom: 22px;
        }

        /* ── 현재 상황 ─────────────────────────────────────────────────── */
        .context-card {
            background: #f8f8f8;
            border-left: 3px solid #0f0f0f;
            padding: 12px 16px;
            border-radius: 0 4px 4px 0;
            margin-bottom: 22px;
        }
        .context-card.emergency {
            background: #fff0f1;
            border-left-color: #ef2027;
        }
        .context-card.warning {
            background: #fff8e1;
            border-left-color: #d4a623;
        }
        .context-card-label {
            font-size: 11px; letter-spacing: 0.12em; text-transform: uppercase;
            color: #9a9a9a; margin-bottom: 6px; font-weight: 700;
        }
        .context-card-text {
            font-size: 14px; color: #0f0f0f; line-height: 1.5;
        }

        /* ── 정보 패널 (DEVICE/SENSORS/JUDGMENT) ─────────────────────── */
        .info-block {
            background: #ffffff;
            border: 1px solid #ececec;
            border-radius: 6px;
            padding: 18px 20px;
        }
        .info-block-title {
            font-size: 11px; font-weight: 700; letter-spacing: 0.14em;
            text-transform: uppercase; color: #9a9a9a;
            margin-bottom: 14px;
        }
        .info-row {
            display: flex; justify-content: space-between;
            padding: 8px 0;
            border-bottom: 1px solid #f3f3f3;
            font-size: 13px;
        }
        .info-row:last-child { border-bottom: none; }
        .info-row-label { color: #6b6b6b; }
        .info-row-value { color: #0f0f0f; font-weight: 600; }

        /* ── 탭 스타일 (Streamlit 기본을 미니멀하게) ─────────────────── */
        .stTabs [data-baseweb="tab-list"] {
            gap: 4px;
            border-bottom: 1px solid #ececec;
        }
        .stTabs [data-baseweb="tab"] {
            font-weight: 700;
            font-size: 13px;
            letter-spacing: 0.04em;
            padding: 10px 18px;
            color: #6b6b6b;
            background: transparent;
            border-bottom: 2px solid transparent;
        }
        .stTabs [aria-selected="true"] {
            color: #0f0f0f !important;
            border-bottom-color: #0f0f0f !important;
            background: transparent !important;
        }

        /* ── 워치 외형 (단순화) + 화면 ────────────────────────────────── */
        .watch-stage {
            display: flex; justify-content: center; align-items: center;
            padding: 12px 0 8px 0;
        }
        .watch-frame {
            position: relative;
            width: 248px; height: 296px;
            background: #1d1d1f;
            border-radius: 46px;
            box-shadow: 0 8px 24px rgba(0,0,0,0.18),
                        inset 0 1px 0 rgba(255,255,255,0.08);
            padding: 14px;
        }
        .watch-frame::before {
            content: ""; position: absolute;
            right: -5px; top: 88px;
            width: 6px; height: 28px;
            background: #2a2a2c;
            border-radius: 2px;
        }
        .watch-frame::after {
            content: ""; position: absolute;
            right: -3px; top: 142px;
            width: 4px; height: 36px;
            background: #2a2a2c;
            border-radius: 2px;
        }
        .watch-screen {
            width: 100%; height: 100%;
            background: #000000;
            border-radius: 32px;
            padding: 14px 14px 12px 14px;
            overflow: hidden;
            color: #ffffff;
            display: flex; flex-direction: column;
            font-family: -apple-system, "SF Pro Display",
                         "Apple SD Gothic Neo", sans-serif;
        }
        .watch-screen.emergency {
            background: #ef2027;
        }

        /* ── 워치 화면 — 상단 상태바 ───────────────────────────────────── */
        .ws-statusbar {
            display: flex; justify-content: space-between; align-items: center;
            font-size: 9.5px; font-weight: 700; letter-spacing: 0.04em;
            opacity: 0.95;
            margin-bottom: 4px;
        }
        .ws-statusbar .dot {
            display: inline-block; width: 6px; height: 6px;
            background: #2cd66b; border-radius: 50%;
            margin-right: 5px; vertical-align: middle;
        }
        .ws-statusbar.emergency .dot { background: #ffffff; }

        /* ── 정상 시계 화면 ─────────────────────────────────────────── */
        .ws-clock {
            font-family: -apple-system, "SF Pro Display", sans-serif;
            font-size: 42px; font-weight: 700;
            letter-spacing: -0.02em;
            line-height: 1;
            margin: 6px 0 2px 0;
            color: #ffd23f;
        }
        .ws-clock .ampm {
            font-size: 18px; color: #ffd23f; margin-left: 4px;
            font-weight: 600;
        }
        .ws-date {
            font-size: 10px; color: #b8b8bd;
            margin-bottom: 10px;
        }
        .ws-widgets {
            display: grid; grid-template-columns: 1fr 1fr; gap: 5px;
            flex: 1;
        }
        .ws-widget {
            background: #1c1c1e; border-radius: 9px;
            padding: 6px 8px;
            display: flex; flex-direction: column; justify-content: center;
        }
        .ws-widget-label {
            font-size: 8.5px; color: #b8b8bd; margin-bottom: 1px;
            letter-spacing: 0.02em;
        }
        .ws-widget-label .ic { margin-right: 3px; }
        .ws-widget-value {
            font-size: 15px; font-weight: 700; color: #ffffff;
            line-height: 1.1;
        }
        .ws-widget-value .unit {
            font-size: 9px; color: #b8b8bd; font-weight: 500; margin-left: 2px;
        }
        .ws-widget.heart .ic { color: #ff453a; }
        .ws-widget.activity .ic { color: #2cd66b; }
        .ws-widget.activity .ws-widget-value { color: #2cd66b; font-size: 13px; }
        .ws-widget.steps .ws-widget-value { color: #ffd23f; }
        .ws-widget.cal .ws-widget-value { color: #ff9f0a; }

        .ws-message {
            background: #1c1c1e; border-radius: 9px;
            padding: 6px 9px;
            margin-top: 5px;
        }
        .ws-message-label {
            font-size: 8.5px; color: #ffd23f; font-weight: 700;
            letter-spacing: 0.04em; margin-bottom: 2px;
        }
        .ws-message-text {
            font-size: 10.5px; color: #ffffff; line-height: 1.35;
        }

        /* ── 경고 시계 화면 ─────────────────────────────────────────── */
        .ws-warn-block {
            background: rgba(255, 204, 0, 0.16);
            border: 1px solid rgba(255, 204, 0, 0.5);
            border-radius: 12px;
            padding: 10px 12px;
            margin-top: 6px;
        }
        .ws-warn-title {
            font-size: 11px; color: #ffcc00; font-weight: 700;
            margin-bottom: 4px; letter-spacing: 0.03em;
        }
        .ws-warn-detail {
            font-size: 10.5px; color: #ffffff; line-height: 1.4;
        }

        /* ── 응급 시계 화면 ─────────────────────────────────────────── */
        .ws-emer-icon {
            font-size: 28px; text-align: center; margin: 14px 0 6px 0;
        }
        .ws-emer-title {
            font-size: 15px; color: #ffffff; font-weight: 800;
            text-align: center; line-height: 1.2; margin-bottom: 6px;
            letter-spacing: -0.01em;
        }
        .ws-emer-sub {
            font-size: 10.5px; color: rgba(255,255,255,0.92);
            text-align: center; line-height: 1.4; margin-bottom: 10px;
        }
        .ws-emer-stat {
            background: rgba(0,0,0,0.22);
            border-radius: 10px;
            padding: 8px 10px;
            text-align: center;
            margin-bottom: 8px;
        }
        .ws-emer-stat-num {
            font-size: 22px; font-weight: 800; color: #ffffff;
            line-height: 1;
        }
        .ws-emer-stat-label {
            font-size: 9px; color: rgba(255,255,255,0.85); margin-top: 2px;
        }
        .ws-emer-hr {
            font-size: 10px; color: #ffffff;
            text-align: center;
        }
        .ws-emer-hr .heart { color: #ffffff; }

        /* ── 워치 화면 — 데이터 대기 ───────────────────────────────── */
        .ws-loading {
            display: flex; flex-direction: column; align-items: center;
            justify-content: center; flex: 1;
            color: #8a8a8e;
        }
        .ws-loading-spinner {
            width: 26px; height: 26px;
            border: 2.5px solid rgba(255,255,255,0.12);
            border-top-color: rgba(255,255,255,0.55);
            border-radius: 50%;
            animation: spin 1s linear infinite;
            margin-bottom: 12px;
        }
        @keyframes spin {
            to { transform: rotate(360deg); }
        }
        .ws-loading-text {
            font-size: 11px; color: #b8b8bd;
        }

        /* ── 디바이스 탈/투척 화면 ──────────────────────────────────── */
        .ws-device-alert {
            display: flex; flex-direction: column; align-items: center;
            justify-content: center; flex: 1; padding: 6px;
        }
        .ws-device-alert .ic {
            font-size: 26px; margin-bottom: 6px;
        }
        .ws-device-alert .ttl {
            font-size: 12px; color: #ffffff; font-weight: 700;
            text-align: center; margin-bottom: 4px;
        }
        .ws-device-alert .sub {
            font-size: 10px; color: #b8b8bd; text-align: center;
            line-height: 1.4;
        }

        /* ── Streamlit 기본 요소 조정 ─────────────────────────────── */
        .stButton > button {
            border-radius: 4px;
            font-weight: 600;
            border: 1px solid #d4d4d4;
            background: #ffffff;
            color: #0f0f0f;
            padding: 9px 14px;
        }
        .stButton > button:hover {
            border-color: #0f0f0f;
            background: #0f0f0f;
            color: #ffffff;
        }
        .stButton > button[kind="primary"] {
            background: #0f0f0f;
            color: #ffffff;
            border-color: #0f0f0f;
        }
        .stButton > button[kind="primary"]:hover {
            background: #2a2a2a;
            border-color: #2a2a2a;
        }
        div[data-testid="stDataFrame"] {
            border: 1px solid #ececec;
            border-radius: 4px;
        }

        /* 푸터 텍스트 */
        .footer-note {
            margin-top: 56px;
            padding-top: 22px;
            border-top: 1px solid #f0f0f0;
            font-size: 11px; color: #c5c5c5;
            text-align: center;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ════════════════════════════════════════════════════════════════════════════
# 헤더 + 사이드바
# ════════════════════════════════════════════════════════════════════════════

def render_header():
    st.markdown(
        '<div class="app-header">'
        '<div class="app-title">Fall Monitor</div>'
        '<div class="app-subtitle">Wearable Healthcare · Multi-User</div>'
        '</div>'
        '<div class="app-tagline">다수 사용자 웨어러블 디바이스 중앙 모니터링 시스템</div>',
        unsafe_allow_html=True,
    )


def render_sidebar(cfg):
    with st.sidebar:
        st.markdown(
            '<div style="font-size:14px;font-weight:800;letter-spacing:-0.01em;'
            'color:#0f0f0f;margin-bottom:4px">FALL MONITOR</div>'
            '<div style="font-size:10px;color:#9a9a9a;letter-spacing:0.08em;'
            'text-transform:uppercase;margin-bottom:24px">'
            'Korea University · ICT 2026</div>',
            unsafe_allow_html=True,
        )

        # ── 디바이스 등록 폼 ──
        st.markdown(
            '<div style="font-size:11px;font-weight:700;letter-spacing:0.12em;'
            'text-transform:uppercase;color:#0f0f0f;margin-bottom:10px">'
            '디바이스 등록</div>',
            unsafe_allow_html=True,
        )
        with st.form("register_device", clear_on_submit=True):
            name = st.text_input("이름", placeholder="예: 김복순", key="form_name")
            mac  = st.text_input("MAC 주소", placeholder="A1:B2:C3:D4:E5:F6",
                                 key="form_mac")
            submitted = st.form_submit_button("디바이스 등록",
                                              use_container_width=True,
                                              type="primary")
            if submitted:
                # 매 submit마다 이전 알림은 지운다 (사용자가 값을 고친 뒤
                # 다시 누르면 이전 에러가 남아있지 않도록).
                st.session_state.pop("form_error", None)
                st.session_state.pop("form_error_ts", None)
                st.session_state.pop("form_success", None)
                st.session_state.pop("form_success_ts", None)

                # 이름과 MAC 둘 다 비어있으면 조용히 무시 (검증 메시지 표시 안 함)
                if not (name or "").strip() and not (mac or "").strip():
                    pass
                else:
                    ok, msg, uid = add_user(name, mac)
                    if ok:
                        # 알림 메시지와 발생 시각을 함께 저장 → 자동 만료에 사용
                        st.session_state["form_success"]    = msg
                        st.session_state["form_success_ts"] = time.time()
                        # 새로 등록된 디바이스를 자동으로 펼침
                        st.session_state["opened_user"] = uid
                        st.rerun()
                    else:
                        # 이름만 비어 있는 경우(빨간 박스 회피 요청): 토스트로
                        if not (name or "").strip():
                            st.toast("이름을 입력하세요", icon="ℹ️")
                        else:
                            st.session_state["form_error"]    = msg
                            st.session_state["form_error_ts"] = time.time()
                        st.rerun()

        # ── 알림 렌더링 ─────────────────────────────────────────────────
        # 등록 결과(성공/실패) 알림은 4초 후 자동으로 사라진다.
        # 자동 새로고침이 1초마다 돌고 있으므로, 사이클마다 만료 여부를
        # 검사한다. 이전 구현(form_success는 1회 노출 후 즉시 pop,
        # form_error는 명시적으로 닫지 않는 한 영구 노출)이 일으키던
        # "알람이 즉시 사라짐" / "알람이 영원히 남음" 양쪽 문제를 함께 해결한다.
        TOAST_LIFETIME_SEC = 4.0
        now_ts = time.time()

        # 성공 메시지
        success_msg = st.session_state.get("form_success")
        success_ts  = st.session_state.get("form_success_ts", 0.0)
        if success_msg:
            if now_ts - success_ts > TOAST_LIFETIME_SEC:
                # 만료 → 정리
                st.session_state.pop("form_success", None)
                st.session_state.pop("form_success_ts", None)
            else:
                st.success(success_msg)

        # 에러 메시지
        error_msg = st.session_state.get("form_error")
        error_ts  = st.session_state.get("form_error_ts", 0.0)
        if error_msg:
            if now_ts - error_ts > TOAST_LIFETIME_SEC:
                st.session_state.pop("form_error", None)
                st.session_state.pop("form_error_ts", None)
            else:
                ec1, ec2 = st.columns([5, 1])
                with ec1:
                    st.error(error_msg)
                with ec2:
                    # 사용자가 직접 닫을 수 있는 X 버튼 (자동 만료를 기다리지
                    # 않고 즉시 닫고 싶을 때 사용)
                    def _dismiss_error():
                        st.session_state.pop("form_error", None)
                        st.session_state.pop("form_error_ts", None)
                    st.button(
                        "✕",
                        key="dismiss_form_error",
                        help="알림 닫기",
                        on_click=_dismiss_error,
                    )

        st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)

        # 자동 새로고침
        st.checkbox("자동 새로고침 (1초)", value=True, key="cb_refresh")

        # ── 학번 푸터 ──
        st.markdown(
            '<div style="position:fixed;bottom:14px;left:18px;font-size:10px;'
            'color:#c5c5c5;letter-spacing:0.06em">2023270692 · Jung Yun Jae</div>',
            unsafe_allow_html=True,
        )


# ════════════════════════════════════════════════════════════════════════════
# 워치 화면 빌더
# ════════════════════════════════════════════════════════════════════════════

def _watch_frame(inner_html: str, emergency: bool = False) -> str:
    """워치 외형(검은 라운드 직사각형) + 화면."""
    flat = " ".join(line.strip() for line in inner_html.splitlines() if line.strip())
    screen_class = "watch-screen emergency" if emergency else "watch-screen"
    return (
        '<div class="watch-stage">'
        f'<div class="watch-frame"><div class="{screen_class}">{flat}</div></div>'
        '</div>'
    )


def _statusbar(label: str = "FALL MONITOR", emergency: bool = False) -> str:
    now = datetime.now()
    cls = "ws-statusbar emergency" if emergency else "ws-statusbar"
    if emergency:
        return (
            f'<div class="{cls}">'
            f'<div>SOS</div>'
            f'<div>EMERGENCY</div>'
            f'</div>'
        )
    return (
        f'<div class="{cls}">'
        f'<div><span class="dot"></span>{label}</div>'
        f'<div>{now.strftime("%H:%M")}</div>'
        f'</div>'
    )


def _render_watch_loading() -> str:
    inner = (
        _statusbar("CONNECTING…")
        + '<div class="ws-loading">'
        + '<div class="ws-loading-spinner"></div>'
        + '<div class="ws-loading-text">데이터 수신 대기 중</div>'
        + '</div>'
    )
    return _watch_frame(inner)


def _render_watch_normal(latest: dict) -> str:
    now = datetime.now()
    time_str = now.strftime("%I:%M").lstrip("0") or "12:00"
    ampm = now.strftime("%p")
    weekday_kr = ["월", "화", "수", "목", "금", "토", "일"][now.weekday()]
    date_str = f"{weekday_kr}요일, {now.month}월 {now.day}일"

    hr = latest.get("heart_rate")
    hr_str = f"{int(hr)}" if hr is not None and pd.notna(hr) else "—"

    scenario = str(latest.get("scenario", "")).lower()
    if "walk" in scenario:
        activity = "WALK"
    elif "run" in scenario:
        activity = "RUN"
    else:
        activity = "REST"

    pseudo_steps = (hash(scenario) % 600) + 200 if pd.notna(hr) else 0
    pseudo_cal   = int(pseudo_steps * 0.45) + 120

    inner = (
        _statusbar()
        + f'<div class="ws-clock">{time_str}<span class="ampm">{ampm}</span></div>'
        + f'<div class="ws-date">{date_str}</div>'
        + '<div class="ws-widgets">'
        +   '<div class="ws-widget heart">'
        +     '<div class="ws-widget-label"><span class="ic">●</span>심박</div>'
        +     f'<div class="ws-widget-value">{hr_str}<span class="unit">bpm</span></div>'
        +   '</div>'
        +   '<div class="ws-widget activity">'
        +     '<div class="ws-widget-label"><span class="ic">▶</span>활동</div>'
        +     f'<div class="ws-widget-value">{activity}</div>'
        +   '</div>'
        +   '<div class="ws-widget steps">'
        +     '<div class="ws-widget-label">걸음</div>'
        +     f'<div class="ws-widget-value">{pseudo_steps}</div>'
        +   '</div>'
        +   '<div class="ws-widget cal">'
        +     '<div class="ws-widget-label">칼로리</div>'
        +     f'<div class="ws-widget-value">{pseudo_cal}<span class="unit">kcal</span></div>'
        +   '</div>'
        + '</div>'
        + '<div class="ws-message">'
        +   '<div class="ws-message-label">메시지 · 보호자</div>'
        +   '<div class="ws-message-text">오늘도 좋은 하루 보내세요 :)</div>'
        + '</div>'
    )
    return _watch_frame(inner)


def _render_watch_warning(latest: dict) -> str:
    now = datetime.now()
    time_str = now.strftime("%I:%M").lstrip("0") or "12:00"
    ampm = now.strftime("%p")
    hr = latest.get("heart_rate")
    hr_str = f"{int(hr)}" if hr is not None and pd.notna(hr) else "—"
    reason = str(latest.get("reason", "")) or "비정상 움직임이 감지되었습니다"

    inner = (
        _statusbar()
        + f'<div class="ws-clock" style="font-size:30px">{time_str}'
        + f'<span class="ampm" style="font-size:14px">{ampm}</span></div>'
        + '<div class="ws-warn-block">'
        +   '<div class="ws-warn-title">⚠ 비정상 움직임</div>'
        +   f'<div class="ws-warn-detail">{reason[:60]}</div>'
        + '</div>'
        + '<div class="ws-widgets" style="margin-top:6px">'
        +   '<div class="ws-widget heart">'
        +     '<div class="ws-widget-label"><span class="ic">●</span>심박</div>'
        +     f'<div class="ws-widget-value">{hr_str}<span class="unit">bpm</span></div>'
        +   '</div>'
        +   '<div class="ws-widget activity">'
        +     '<div class="ws-widget-label">상태</div>'
        +     '<div class="ws-widget-value" style="font-size:11px;color:#ffcc00">관찰중</div>'
        +   '</div>'
        + '</div>'
    )
    return _watch_frame(inner)


def _render_watch_device_alert(decision: str, latest: dict) -> str:
    if decision == "DEVICE_REMOVED":
        icon, title, sub = "⌚", "기기 미착용",   "착용을 확인해주세요"
    elif decision == "DEVICE_THROWN_OR_DROPPED":
        icon, title, sub = "🚫", "이상 동작 감지", "기기 충격이 감지되었습니다"
    else:
        icon, title, sub = "ⓘ", "상태 확인 중", "잠시만 기다려주세요"

    inner = (
        _statusbar("FALL MONITOR")
        + '<div class="ws-device-alert">'
        +   f'<div class="ic">{icon}</div>'
        +   f'<div class="ttl">{title}</div>'
        +   f'<div class="sub">{sub}</div>'
        + '</div>'
    )
    return _watch_frame(inner)


def _render_watch_uncertain(latest: dict) -> str:
    inner = (
        _statusbar("UNSTABLE")
        + '<div class="ws-device-alert">'
        +   '<div class="ic">📶</div>'
        +   '<div class="ttl">통신 불안정</div>'
        +   '<div class="sub">판정을 일시 보류합니다</div>'
        + '</div>'
    )
    return _watch_frame(inner)


def _render_watch_emergency(latest: dict) -> str:
    hr  = latest.get("heart_rate")
    hr_str = f"{int(hr)}" if hr is not None and pd.notna(hr) else "—"

    inner = (
        _statusbar(emergency=True)
        + '<div class="ws-emer-icon">🚨</div>'
        + '<div class="ws-emer-title">낙상이 감지되었습니다</div>'
        + '<div class="ws-emer-sub">반응이 없으면<br>응급 호출이 발송됩니다</div>'
        + f'<div class="ws-emer-hr"><span class="heart">♥</span> {hr_str} bpm</div>'
    )
    return _watch_frame(inner, emergency=True)


def render_watch_for(user_id: str, df: pd.DataFrame) -> str:
    latest = latest_user_state(df, user_id)
    if not latest:
        return _render_watch_loading()
    decision = str(latest.get("final_decision", "NORMAL"))
    latched  = str(latest.get("latched_event", "") or "")

    if latched == "EMERGENCY_FALL" or decision == "EMERGENCY_FALL":
        return _render_watch_emergency(latest)
    if decision in ("DEVICE_REMOVED", "DEVICE_THROWN_OR_DROPPED"):
        return _render_watch_device_alert(decision, latest)
    if decision == "DATA_UNCERTAIN":
        return _render_watch_uncertain(latest)
    if decision == "WARNING":
        return _render_watch_warning(latest)
    return _render_watch_normal(latest)


# ════════════════════════════════════════════════════════════════════════════
# 실시간 차트 빌더 (Plotly)
# ════════════════════════════════════════════════════════════════════════════

# 사용자별 차트 키가 매 rerun마다 충돌하지 않도록 user_id를 키에 포함
def _chart_idx_masks(sub: pd.DataFrame):
    """차트에 표시할 idx 리스트와 missing/interp 마스크 반환."""
    idx = list(range(len(sub)))
    if "packet_status" in sub.columns:
        missing_mask = sub["packet_status"] == "missing"
    else:
        missing_mask = pd.Series([False] * len(sub))
    if "is_interpolated" in sub.columns:
        interp_mask = sub["is_interpolated"].fillna(False).astype(bool)
    else:
        interp_mask = pd.Series([False] * len(sub))
    missing_idx = [i for i, v in enumerate(missing_mask) if v]
    interp_idx  = [i for i, v in enumerate(interp_mask)  if v]
    return idx, missing_idx, interp_idx


def _add_loss_recovery_shapes(fig, missing_idx: list, interp_idx: list,
                              y_min: float, y_max: float):
    """손실/보간 구간을 시각적으로 강조하는 shape 들을 차트에 추가.

    시연 시 "어디서 패킷이 빠졌고 어떻게 복구했는지" 가 한눈에 들어오도록:
      - 손실 패킷: 라인 전체에 빨간 세로 점선
      - 보간 복원: 연속 구간을 회색 반투명 띠로 표시 (단발이면 좁은 띠)

    Plotly 의 add_vline / add_vrect 는 figure-level annotation 이라 여러 개
    그려도 성능 영향이 거의 없다.
    """
    # 1) 손실 위치에 세로 점선 — 라인 위 어디서 빠졌는지 즉시 식별
    for mi in missing_idx:
        fig.add_shape(
            type="line",
            x0=mi, x1=mi, y0=y_min, y1=y_max,
            line=dict(color="rgba(231, 76, 60, 0.45)", width=1.5, dash="dot"),
            layer="below",
        )

    # 2) 보간된 패킷의 연속 구간을 회색 음영 띠로 — 복구된 영역이 한 덩어리로 보임
    if interp_idx:
        # 연속 구간을 그룹화 (예: [3,4,5, 9, 12,13] → [(3,5), (9,9), (12,13)])
        groups = []
        start = prev = interp_idx[0]
        for i in interp_idx[1:]:
            if i == prev + 1:
                prev = i
            else:
                groups.append((start, prev))
                start = prev = i
        groups.append((start, prev))
        for g_start, g_end in groups:
            fig.add_vrect(
                x0=g_start - 0.4, x1=g_end + 0.4,
                fillcolor="rgba(149, 165, 166, 0.18)",
                line_width=0,
                layer="below",
            )


def render_acc_chart(sub: pd.DataFrame, user_id: str, fall_thr: float = 2.8):
    if "acc_magnitude" not in sub.columns:
        return
    idx, missing_idx, interp_idx = _chart_idx_masks(sub)
    acc_series = pd.to_numeric(sub["acc_magnitude"], errors="coerce")
    # 손실 패킷의 ❌ 마커를 0 이 아닌 라인 흐름 위 (전후 유효값) 에 띄워서
    # "어디서 빠졌는지" 가 한눈에 보이게 한다. 0 에 박혀 있으면 멀어서 안 보임.
    acc_filled = acc_series.ffill().bfill()
    default_y = float(acc_filled.mean()) if acc_filled.notna().any() else 1.0

    # y축 고정 범위: fall_thr (3g) 보다 충분히 큰 헤드룸 확보
    # — 시뮬레이터 real_fall spike 가 3.5~5.5g 이므로 6.5g 가 적절
    Y_MAX_ACC = 6.5
    Y_MIN_ACC = 0.0

    fig = go.Figure()

    # 손실/보간 구간 음영 — 라인 트레이스보다 먼저 그려 below 에 깔리도록
    _add_loss_recovery_shapes(fig, missing_idx, interp_idx, Y_MIN_ACC, Y_MAX_ACC)

    fig.add_trace(go.Scatter(
        x=idx, y=acc_series.tolist(),
        mode="lines", name="수신/보간",
        line=dict(color="#3498db", width=2),
        connectgaps=False,
    ))
    if interp_idx:
        fig.add_trace(go.Scatter(
            x=interp_idx,
            y=[float(acc_filled.iloc[i]) if pd.notna(acc_filled.iloc[i]) else default_y
               for i in interp_idx],
            mode="markers", name="보간 복원",
            marker=dict(symbol="diamond", color="#7f8c8d", size=10,
                        line=dict(color="white", width=1.5)),
        ))
    if missing_idx:
        fig.add_trace(go.Scatter(
            x=missing_idx,
            y=[float(acc_filled.iloc[i]) if pd.notna(acc_filled.iloc[i]) else default_y
               for i in missing_idx],
            mode="markers", name="손실 패킷",
            marker=dict(symbol="x", color="#e74c3c", size=12, line=dict(width=2.5)),
        ))
    fig.add_hline(y=1.3, line_dash="dash", line_color="#f39c12",
                  annotation_text="경고 1.3g", annotation_position="right")
    fig.add_hline(y=fall_thr, line_dash="dash", line_color="#e74c3c",
                  annotation_text=f"낙상 {fall_thr}g", annotation_position="right")
    fig.update_layout(
        title="가속도 합력 (g)",
        xaxis_title="패킷 순서", yaxis_title="합력 (g)",
        yaxis=dict(range=[Y_MIN_ACC, Y_MAX_ACC], fixedrange=True),
        height=260, margin=dict(l=50, r=20, t=45, b=50),
        legend=dict(orientation="h", y=-0.35),
        plot_bgcolor="#ffffff", paper_bgcolor="#ffffff",
        font=dict(family="-apple-system, sans-serif", color="#0f0f0f"),
    )
    st.plotly_chart(fig, use_container_width=True, key=f"chart_acc_{user_id}")


def render_gyro_chart(sub: pd.DataFrame, user_id: str):
    if "gyro_magnitude" not in sub.columns:
        return
    idx, missing_idx, interp_idx = _chart_idx_masks(sub)
    gyro_series = pd.to_numeric(sub["gyro_magnitude"], errors="coerce")
    gyro_filled = gyro_series.ffill().bfill()
    default_y = float(gyro_filled.mean()) if gyro_filled.notna().any() else 30.0

    # y축 고정 범위: real_fall spike 가 250~700°/s, watch_thrown 이 400~900°/s
    # 이므로 1000°/s 까지 표시 + 임계 250°/s 가이드 라인이 잘 보이도록.
    Y_MAX_GYRO = 1000.0
    Y_MIN_GYRO = 0.0

    fig = go.Figure()
    _add_loss_recovery_shapes(fig, missing_idx, interp_idx, Y_MIN_GYRO, Y_MAX_GYRO)

    fig.add_trace(go.Scatter(
        x=idx, y=gyro_series.tolist(),
        mode="lines", name="수신/보간",
        line=dict(color="#9b59b6", width=2),
        connectgaps=False,
    ))
    if interp_idx:
        fig.add_trace(go.Scatter(
            x=interp_idx,
            y=[float(gyro_filled.iloc[i]) if pd.notna(gyro_filled.iloc[i]) else default_y
               for i in interp_idx],
            mode="markers", name="보간 복원",
            marker=dict(symbol="diamond", color="#7f8c8d", size=10,
                        line=dict(color="white", width=1.5)),
        ))
    if missing_idx:
        fig.add_trace(go.Scatter(
            x=missing_idx,
            y=[float(gyro_filled.iloc[i]) if pd.notna(gyro_filled.iloc[i]) else default_y
               for i in missing_idx],
            mode="markers", name="손실 패킷",
            marker=dict(symbol="x", color="#e74c3c", size=12, line=dict(width=2.5)),
        ))
    # 자이로 임계값 가이드 라인 (250°/s) — 시연 시 fall spike 가 임계 돌파하는 게 보임
    fig.add_hline(y=250.0, line_dash="dash", line_color="#e74c3c",
                  annotation_text="낙상 250°/s", annotation_position="right")
    fig.update_layout(
        title="자이로 합력 (°/s)",
        xaxis_title="패킷 순서", yaxis_title="합력 (°/s)",
        yaxis=dict(range=[Y_MIN_GYRO, Y_MAX_GYRO], fixedrange=True),
        height=260, margin=dict(l=50, r=20, t=45, b=50),
        legend=dict(orientation="h", y=-0.35),
        plot_bgcolor="#ffffff", paper_bgcolor="#ffffff",
        font=dict(family="-apple-system, sans-serif", color="#0f0f0f"),
    )
    st.plotly_chart(fig, use_container_width=True, key=f"chart_gyro_{user_id}")


def render_hr_chart(sub: pd.DataFrame, user_id: str):
    if "heart_rate" not in sub.columns:
        return
    idx, missing_idx, interp_idx = _chart_idx_masks(sub)
    hr_series = pd.to_numeric(sub["heart_rate"], errors="coerce")
    hr_filled = hr_series.ffill().bfill()
    default_y = float(hr_filled.mean()) if hr_filled.notna().any() else 70.0

    # y축 고정 범위: 안정 ~65, 보행 ~85, 달리기 ~130, fall 직후 ~110 모두 표시
    # 정상 안정 영역 (60~100) 도 옅은 배경 영역으로 표시 → 시청자 가독성 ↑
    Y_MIN_HR = 40.0
    Y_MAX_HR = 150.0

    fig = go.Figure()

    # 정상 심박 영역 (60-100 bpm) 옅은 녹색 배경
    fig.add_hrect(
        y0=60, y1=100,
        fillcolor="rgba(46, 204, 113, 0.08)",
        line_width=0,
        layer="below",
        annotation_text="정상 안정 영역",
        annotation_position="top left",
        annotation=dict(font=dict(size=9, color="#27ae60")),
    )

    _add_loss_recovery_shapes(fig, missing_idx, interp_idx, Y_MIN_HR, Y_MAX_HR)

    fig.add_trace(go.Scatter(
        x=idx, y=hr_series.tolist(),
        mode="lines+markers", name="수신/보간",
        line=dict(color="#e74c3c", width=2),
        marker=dict(size=4),
        connectgaps=False,
    ))
    if interp_idx:
        fig.add_trace(go.Scatter(
            x=interp_idx,
            y=[float(hr_filled.iloc[i]) if pd.notna(hr_filled.iloc[i]) else default_y
               for i in interp_idx],
            mode="markers", name="보간 복원",
            marker=dict(symbol="diamond", color="#7f8c8d", size=10,
                        line=dict(color="white", width=1.5)),
        ))
    if missing_idx:
        fig.add_trace(go.Scatter(
            x=missing_idx,
            y=[float(hr_filled.iloc[i]) if pd.notna(hr_filled.iloc[i]) else default_y
               for i in missing_idx],
            mode="markers", name="손실 패킷",
            marker=dict(symbol="x", color="#e74c3c", size=12, line=dict(width=2.5)),
        ))
    fig.update_layout(
        title="심박수 (bpm)",
        xaxis_title="패킷 순서", yaxis_title="bpm",
        yaxis=dict(range=[Y_MIN_HR, Y_MAX_HR], tickformat="d", fixedrange=True),
        height=260, margin=dict(l=50, r=20, t=45, b=50),
        legend=dict(orientation="h", y=-0.35),
        plot_bgcolor="#ffffff", paper_bgcolor="#ffffff",
        font=dict(family="-apple-system, sans-serif", color="#0f0f0f"),
    )
    st.plotly_chart(fig, use_container_width=True, key=f"chart_hr_{user_id}")


def render_skin_chart(sub: pd.DataFrame, user_id: str):
    if "skin_contact" not in sub.columns:
        return
    idx, missing_idx, interp_idx = _chart_idx_masks(sub)
    skin_series = pd.to_numeric(sub["skin_contact"], errors="coerce")
    skin_filled = skin_series.ffill().bfill()
    default_y = 1.0

    # y축 고정: 0/1 이산값이지만 마커가 위로 0.08 띄워지므로 1.25 까지 확보
    Y_MIN_SKIN = -0.1
    Y_MAX_SKIN = 1.25

    fig = go.Figure()
    _add_loss_recovery_shapes(fig, missing_idx, interp_idx, Y_MIN_SKIN, Y_MAX_SKIN)

    fig.add_trace(go.Scatter(
        x=idx, y=skin_series.tolist(),
        mode="lines", name="수신/보간",
        line=dict(color="#2ecc71", width=2, shape="hv"),
        fill="tozeroy", fillcolor="rgba(46, 204, 113, 0.18)",
        connectgaps=False,
    ))
    if interp_idx:
        fig.add_trace(go.Scatter(
            x=interp_idx,
            y=[(float(skin_filled.iloc[i]) if pd.notna(skin_filled.iloc[i]) else default_y) + 0.08
               for i in interp_idx],
            mode="markers", name="보간 복원",
            marker=dict(symbol="diamond", color="#7f8c8d", size=10,
                        line=dict(color="white", width=1.5)),
        ))
    if missing_idx:
        fig.add_trace(go.Scatter(
            x=missing_idx,
            y=[(float(skin_filled.iloc[i]) if pd.notna(skin_filled.iloc[i]) else default_y) + 0.08
               for i in missing_idx],
            mode="markers", name="손실 패킷",
            marker=dict(symbol="x", color="#e74c3c", size=12, line=dict(width=2.5)),
        ))
    fig.update_layout(
        title="착용 상태 (0=미착용 / 1=착용)",
        xaxis_title="패킷 순서",
        yaxis=dict(tickvals=[0, 1], ticktext=["미착용", "착용"], range=[-0.1, 1.3]),
        height=220, margin=dict(l=50, r=20, t=45, b=50),
        legend=dict(orientation="h", y=-0.35),
        plot_bgcolor="#ffffff", paper_bgcolor="#ffffff",
        font=dict(family="-apple-system, sans-serif", color="#0f0f0f"),
    )
    st.plotly_chart(fig, use_container_width=True, key=f"chart_skin_{user_id}")


# ════════════════════════════════════════════════════════════════════════════
# 사용자 액션 — I'm OK / Emergency SOS / 시나리오 강제
# ════════════════════════════════════════════════════════════════════════════

def send_user_action(user_id: str, action: str):
    # mutate_runtime_cfg로 락을 잡고 read-modify-write를 atomic하게 처리.
    # 이전 구현은 load → modify → save 패턴이라 receiver의 동시 save와
    # race가 발생해 user_action이 손실되는 경우가 있었다.
    with mutate_runtime_cfg() as cfg:
        if user_id not in cfg.get("users", {}):
            cfg.mark_clean()
            return
        cfg["users"][user_id]["user_action"] = action
        cfg["users"][user_id]["user_action_version"] = (
            cfg["users"][user_id].get("user_action_version", 0) + 1
        )


def set_scenario(user_id: str, mode: str, scenario: str = None):
    with mutate_runtime_cfg() as cfg:
        if user_id not in cfg.get("users", {}):
            cfg.mark_clean()
            return
        u = cfg["users"][user_id]
        u["scenario_mode"] = mode
        if scenario is not None:
            u["active_scenario"] = scenario
        u["scenario_version"] = u.get("scenario_version", 0) + 1


def set_user_loss_rate(user_id: str, rate: float | None):
    """사용자별 패킷 손실률을 설정한다.

    rate가 None이면 전역값을 사용하도록 override를 해제한다.
    rate가 0.0~1.0이면 sender가 이 사용자에게만 그 비율로 패킷을 drop한다.

    sender는 매 사이클 cfg를 다시 읽으므로(폴링) 별도 version bump 없이도
    다음 송신 사이클부터 새 손실률이 반영된다. mutate_runtime_cfg()의 락
    덕분에 동시 변경에도 안전하다.
    """
    with mutate_runtime_cfg() as cfg:
        if user_id not in cfg.get("users", {}):
            cfg.mark_clean()
            return
        cfg["users"][user_id]["packet_loss_rate"] = rate


# ════════════════════════════════════════════════════════════════════════════
# 카드 + 상세 패널
# ════════════════════════════════════════════════════════════════════════════

# ── 버튼 처리 패턴 ───────────────────────────────────────────────────────────
#
# 이전 구현은 `on_click=콜백, args=(user_id,)` 패턴이었으나, time.sleep + rerun
# 자동 새로고침 사이클과 사용자 클릭이 빠르게 겹치면 콜백이 stale args로
# 호출되어 "끝단 카드 제거 클릭이 무시됨" / "첫 카드 제거 클릭이 전체를 지움"
# 같은 증상이 발생했다 (streamlit issue #8365 계열).
#
# 새 구현은 모든 버튼 처리를 after-the-fact(`if st.button(): ...`)로 옮겼다.
# user_id 등은 스크립트 실행 시점의 로컬 변수 값으로 평가되므로 stale args
# 문제가 원천 차단된다. 아래 콜백 함수들은 더 이상 직접 호출되지 않지만,
# 의미상 한 곳에 정리해 두기 위해 헬퍼로 남겨둔다 (필요 시 재사용 가능).

def _toggle_opened_user(user_id: str):
    """현재 열린 사용자를 토글. after-the-fact 처리 시 인라인으로도 사용 가능."""
    current = st.session_state.get("opened_user")
    st.session_state["opened_user"] = None if current == user_id else user_id


def _close_detail():
    """상세 패널 닫기."""
    st.session_state["opened_user"] = None


def _remove_user_callback(user_id: str):
    """디바이스 제거 + opened_user 정리."""
    remove_user(user_id)
    if st.session_state.get("opened_user") == user_id:
        st.session_state["opened_user"] = None


def render_device_card(user_id: str, df: pd.DataFrame, is_opened: bool):
    """미니멀 카드 — 이름 · MAC · 상태 칩. 클릭 시 상세 펼침."""
    meta = get_user_meta(user_id)
    name = meta.get("name", "—")
    mac  = meta.get("mac", "—")
    latest = latest_user_state(df, user_id)
    decision = str(latest.get("final_decision", "NORMAL")) if latest else "NORMAL"
    latched  = str(latest.get("latched_event", "") or "") if latest else ""
    effective = latched if latched else decision

    chip_label, chip_fg, chip_bg, chip_border = CHIP_STYLE.get(
        effective, CHIP_STYLE["NORMAL"]
    )

    if effective == "EMERGENCY_FALL":
        card_cls = "device-card emergency"
    elif effective == "WARNING":
        card_cls = "device-card warning"
    else:
        card_cls = "device-card"
    if is_opened:
        card_cls += " opened"

    st.markdown(
        f'<div class="{card_cls}">'
        f'  <div class="device-card-name">{name}</div>'
        f'  <div class="device-card-mac">{mac}</div>'
        f'  <span class="chip" style="color:{chip_fg};background:{chip_bg};'
        f'border:1px solid {chip_border}">{chip_label}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # ── 버튼 처리 패턴: after-the-fact (콜백 args 미사용) ─────────────────
    #
    # 이전 구현은 on_click=콜백, args=(user_id,) 패턴이었다. 그러나
    # `time.sleep + st.rerun` 자동 새로고침 사이클과 사용자 클릭이 빠르게
    # 겹치면, streamlit 내부에서 두 rerun 요청이 빠르게 처리되면서 콜백이
    # *직전 사이클의 위젯 트리에 캡처된 stale args*로 호출되는 race가 있다
    # (streamlit issue #8365 계열). 증상은:
    #   - 끝단 카드의 × 클릭이 무시되는 것처럼 보임 (실제로는 다른 카드의
    #     stale args로 콜백 호출 → 같은 사용자 두 번 제거 시도 → no-op)
    #   - 첫 카드의 × 클릭이 전체 사용자 그리드를 모두 지움 (직전 사이클의
    #     마지막 버튼 args가 잘못 매칭되어 연쇄적으로 다른 사용자 제거)
    #
    # 해결: on_click 콜백을 폐기하고, 버튼의 반환값(현재 사이클에서 클릭됨
    # 여부)을 즉시 분기 처리한다. user_id는 *스크립트 실행 시점*의 로컬
    # 변수 값으로 평가되므로 stale args 가능성이 원천 차단된다.
    c1, c2 = st.columns([3, 1])
    with c1:
        label = "닫기" if is_opened else "상세 보기"
        if st.button(label, key=f"open_{user_id}", use_container_width=True):
            current = st.session_state.get("opened_user")
            st.session_state["opened_user"] = None if current == user_id else user_id
            st.rerun()
    with c2:
        if st.button(
            "×",
            key=f"del_{user_id}",
            use_container_width=True,
            help=f"{name} 디바이스 제거",
        ):
            remove_user(user_id)
            if st.session_state.get("opened_user") == user_id:
                st.session_state["opened_user"] = None
            # 클릭 직후 즉시 새 그리드로 다시 그려지도록 자동 새로고침 sleep
            # 한 사이클을 스킵한다. 위젯 시그니처(컬럼 개수·element 개수)는
            # render_device_grid 의 placeholder 패턴으로 매 사이클 동일하게
            # 유지되므로 카드 DOM 잔상은 발생하지 않는다.
            st.session_state["_skip_sleep_once"] = True
            st.rerun()


def render_detail_panel(user_id: str, df: pd.DataFrame, cfg: dict):
    """
    카드 클릭 시 펼쳐지는 상세 패널의 *본문*.

    헤더(이름·MAC)와 닫기 버튼(✕)은 caller(render_device_grid)에서 그린다.
    이는 닫기 버튼을 본문 *이전*에 위치시켜서, 클릭 시 콜백이 즉시 발동되어
    본문이 같은 사이클에서 아예 그려지지 않도록 하기 위한 설계다.

    탭 구성:
      - Tab 1: 실시간 차트 (acc / gyro / HR / skin_contact) + 정보 + 시나리오 제어
      - Tab 2: 워치 화면 + I'm OK / Emergency SOS 버튼
    """
    latest = latest_user_state(df, user_id)
    decision = str(latest.get("final_decision", "NORMAL")) if latest else "NORMAL"
    decision_kr = DECISION_KR.get(decision, decision)
    latched  = str(latest.get("latched_event", "") or "") if latest else ""
    reason   = str(latest.get("reason", "")) if latest else ""

    user_state = cfg.get("users", {}).get(user_id, {})
    mode = user_state.get("scenario_mode", "auto")
    active_scenario = user_state.get("active_scenario", "normal_idle")

    # 상단 강조 바 — 상태에 따른 색
    if latched == "EMERGENCY_FALL" or decision == "EMERGENCY_FALL":
        accent_color = "#ef2027"
    elif decision == "WARNING":
        accent_color = "#d4a623"
    else:
        accent_color = "#0f0f0f"
    st.markdown(
        f'<div style="height:3px;background:{accent_color};border-radius:2px;'
        f'margin:6px 0 18px 0"></div>',
        unsafe_allow_html=True,
    )

    # ── 현재 상황 카드 ──
    if latched == "EMERGENCY_FALL" or decision == "EMERGENCY_FALL":
        ctx_cls = "context-card emergency"
    elif decision == "WARNING":
        ctx_cls = "context-card warning"
    else:
        ctx_cls = "context-card"
    ctx_text = reason or "정상 활동 범위"
    st.markdown(
        f'<div class="{ctx_cls}">'
        f'<div class="context-card-label">현재 상황</div>'
        f'<div class="context-card-text">{ctx_text}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # ── 탭: 실시간 차트 ↔ 워치 화면 ──────────────────────────────────────
    tab_charts, tab_watch = st.tabs(["📈  실시간 차트", "⌚  워치 화면"])

    with tab_charts:
        _render_charts_tab(user_id, df, cfg, latest, decision_kr, latched, mode,
                           active_scenario)

    with tab_watch:
        _render_watch_tab(user_id, df, latched)


def _render_charts_tab(user_id: str, df: pd.DataFrame, cfg: dict,
                       latest: dict, decision_kr: str, latched: str,
                       mode: str, active_scenario: str):
    """탭 1: 실시간 차트 + 센서/판정 정보 + 시나리오 제어."""
    sub = user_history(df, user_id, n_recent=80)
    fall_thr = float(cfg.get("fall_threshold", 2.8))

    if sub.empty:
        st.info("이 디바이스의 데이터가 아직 수신되지 않았습니다.")
    else:
        # 차트 영역 + 정보 패널 2단 레이아웃
        col_charts, col_info = st.columns([2, 1], gap="large")
        with col_charts:
            render_acc_chart(sub, user_id, fall_thr=fall_thr)
            render_gyro_chart(sub, user_id)
            render_hr_chart(sub, user_id)
            render_skin_chart(sub, user_id)

            # 통계 지표
            total_rec = len(sub)
            interp_count = int(
                sub.get("is_interpolated", pd.Series([False] * len(sub))).fillna(False).sum()
            )
            miss_count = int((sub["packet_status"] == "missing").sum()) \
                if "packet_status" in sub.columns else 0
            loss_pct = miss_count / max(total_rec, 1) * 100

            if "latency_ms" in sub.columns:
                lat_vals = pd.to_numeric(sub["latency_ms"], errors="coerce")
                lat_vals = lat_vals[lat_vals > 0]
                avg_lat  = float(lat_vals.mean()) if len(lat_vals) > 0 else 0.0
                if avg_lat != avg_lat:    # NaN
                    avg_lat = 0.0
            else:
                avg_lat = 0.0

            st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)
            s1, s2, s3, s4 = st.columns(4)
            s1.metric("표시 패킷",    f"{total_rec}")
            s2.metric("보간 복원",    f"{interp_count}")
            s3.metric("패킷 손실률",  f"{loss_pct:.1f}%")
            s4.metric("평균 지연",    f"{avg_lat:.1f} ms")

        with col_info:
            _render_info_blocks(user_id, latest, decision_kr, latched, mode)

    # ── 시나리오 제어 ──────────────────────────────────────────────────
    st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)
    st.markdown(
        '<div style="font-size:11px;font-weight:700;letter-spacing:0.14em;'
        'text-transform:uppercase;color:#9a9a9a;margin-bottom:10px">'
        '시나리오 제어 · 발표자 전용</div>',
        unsafe_allow_html=True,
    )

    sc_cols = st.columns([1, 3])
    with sc_cols[0]:
        mode_label = "자동" if mode == "auto" else "수동"
        if st.button(f"모드: {mode_label} ⟳", key=f"mode_{user_id}",
                     use_container_width=True,
                     help="자동 ↔ 수동 토글"):
            new_mode = "manual" if mode == "auto" else "auto"
            set_scenario(user_id, new_mode)
            st.rerun()
    with sc_cols[1]:
        idx = SCENARIO_OPTIONS.index(active_scenario) \
              if active_scenario in SCENARIO_OPTIONS else 0
        picked = st.selectbox(
            "수동 시나리오",
            SCENARIO_OPTIONS,
            index=idx,
            format_func=lambda s: f"{SCENARIO_KR.get(s, s)}  ({s})",
            key=f"scn_{user_id}",
            label_visibility="collapsed",
            disabled=(mode != "manual"),
        )
        if mode == "manual" and picked != active_scenario:
            set_scenario(user_id, "manual", picked)
            st.rerun()

    # ── 네트워크 제약 (사용자별 손실률) ────────────────────────────────────
    #
    # 이 디바이스에 한정해 sender 측 패킷 손실률을 0~80%로 조절한다.
    # 다른 사용자는 영향 없다 (sender가 사용자별 user_state["packet_loss_rate"]
    # 를 먼저 보고 None이면 전역값으로 fallback).
    #
    # 위젯 시그니처: opened_user 가 동일한 동안 이 슬라이더는 매 사이클 같은
    # key·type 으로 정확히 한 번 등장하므로 잔상이 발생하지 않는다.
    st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)
    st.markdown(
        '<div style="font-size:11px;font-weight:700;letter-spacing:0.14em;'
        'text-transform:uppercase;color:#9a9a9a;margin-bottom:10px">'
        '네트워크 제약 · 발표자 전용</div>',
        unsafe_allow_html=True,
    )

    user_loss = cfg.get("users", {}).get(user_id, {}).get("packet_loss_rate")
    global_loss = float(cfg.get("packet_loss_rate", 0.15))
    # None(전역값 사용) 상태일 때 슬라이더 초기값은 전역값으로 표시.
    current_pct = int(round((user_loss if user_loss is not None else global_loss) * 100))

    loss_cols = st.columns([3, 1])
    with loss_cols[0]:
        picked_pct = st.slider(
            "패킷 손실률",
            min_value=0, max_value=80,
            value=current_pct, step=1,
            format="%d%%",
            key=f"loss_{user_id}",
            label_visibility="collapsed",
            help="이 디바이스에만 적용되는 패킷 손실률 (다른 사용자는 영향 없음)",
        )
    with loss_cols[1]:
        if user_loss is None:
            badge = f"전역 {int(round(global_loss*100))}%"
        else:
            badge = f"개별 {int(round(user_loss*100))}%"
        st.markdown(
            f'<div style="text-align:center;font-size:12px;color:#666;'
            f'padding-top:6px">{badge}</div>',
            unsafe_allow_html=True,
        )

    # 슬라이더 값이 현재 적용값과 다르면 cfg 업데이트.
    # 1% 이내의 라운딩 차이는 무시 (전역값 사용 중일 때 슬라이더가 전역값을
    # 표시 → 같은 값으로 클릭만 해도 매번 override 가 set 되는 것을 방지).
    new_rate = picked_pct / 100.0
    if abs(new_rate - (user_loss if user_loss is not None else global_loss)) >= 0.005:
        set_user_loss_rate(user_id, new_rate)
        st.rerun()


def _render_watch_tab(user_id: str, df: pd.DataFrame, latched: str):
    """탭 2: 워치 화면 + I'm OK / Emergency SOS 버튼.

    ── element shape 일관성 보장 ─────────────────────────────────────────
    latch 전환(WARNING ⇄ EMERGENCY_FALL)이 일어날 때 이 함수의 element
    개수와 종류가 사이클마다 달라지면, 그 위 슬롯(detail_slot)의 element
    tree shape이 흔들려 프론트엔드가 잔상을 남긴다.
    →  **응급 여부와 무관하게 항상 동일한 element 시퀀스**를 그리고,
       응급이 아닐 때는 버튼을 `disabled=True` + 안내 캡션으로 처리한다.
       이렇게 하면 backend 트리 shape이 매 사이클 100% 동일해진다.
    """
    is_emergency = (latched == "EMERGENCY_FALL")

    # 좌우 여백 균형을 위해 중앙에 배치 — 모든 사이클에서 동일 컬럼 구조
    col_l, col_c, col_r = st.columns([1, 2, 1])
    with col_c:
        watch_html = render_watch_for(user_id, df)
        st.markdown(watch_html, unsafe_allow_html=True)

        # 스페이서 — 항상 같은 markdown 1개
        st.markdown("<div style='height:14px'></div>", unsafe_allow_html=True)

        # 응답 버튼 — 항상 동일하게 columns(2) + button×2 구조 유지.
        # 위젯 시그니처(key + type + 개수)가 사이클마다 동일해야 streamlit이
        # 위젯 정체성을 유지하고, 클릭이 올바른 핸들러로 전달된다.
        # 응급이 아닐 때는 disabled 처리 — type/key 등 다른 파라미터는 절대
        # 사이클마다 바꾸지 않는다.
        b1, b2 = st.columns(2)
        with b1:
            if st.button(
                "✓  I'm OK",
                key=f"ok_{user_id}",
                use_container_width=True,
                disabled=not is_emergency,
            ):
                send_user_action(user_id, "im_ok")
                st.toast("정상 응답 전송 — latch가 곧 해제됩니다", icon="✅")
                # 클릭 직후 즉시 rerun — latch 해제가 다음 사이클에 바로
                # 반영되고, disabled 토글 사이의 위젯 상태 불일치 시간을
                # 최소화한다. 이 rerun 이 없으면 자동 새로고침까지 최대
                # 1초간 버튼이 "눌렸지만 화면은 그대로" 보여서 사용자가
                # 두 번 누르거나 SOS 를 잘못 누를 위험이 있다.
                st.rerun()
        with b2:
            # type 은 사이클마다 바뀌면 안 됨 (위젯 정체성 손상).
            # 항상 "primary" 로 고정하고, 비활성 시 disabled 만으로 표현.
            if st.button(
                "🚨  Emergency SOS",
                key=f"sos_{user_id}",
                use_container_width=True,
                type="primary",
                disabled=not is_emergency,
            ):
                send_user_action(user_id, "emergency_confirmed")
                st.toast("응급 호출 확정 — 보호자/응급 서비스 알림 (시뮬레이션)",
                         icon="🚨")
                st.rerun()

        # 안내 캡션 — 항상 같은 위치에 같은 markdown 1개.
        # 텍스트만 응급 여부에 따라 달라진다.
        if is_emergency:
            caption_html = (
                '<div style="text-align:center;font-size:11px;'
                'color:#ef2027;margin-top:10px;font-weight:600">'
                '⚠ 응급 응답 대기 중 — 30초 이내 미응답 시 자동 SOS 송신'
                '</div>'
            )
        else:
            caption_html = (
                '<div style="text-align:center;font-size:11px;'
                'color:#9a9a9a;margin-top:10px">'
                '응급 상태일 때만 I\'m OK / SOS 응답 버튼이 활성화됩니다'
                '</div>'
            )
        st.markdown(caption_html, unsafe_allow_html=True)


def _render_info_blocks(user_id: str, latest: dict, decision_kr: str,
                        latched: str, mode: str):
    """DEVICE / SENSORS / JUDGMENT 3개 정보 블록."""
    meta = get_user_meta(user_id)

    def _row(label, value):
        return (
            f'<div class="info-row">'
            f'<span class="info-row-label">{label}</span>'
            f'<span class="info-row-value">{value}</span>'
            f'</div>'
        )

    def _fmt(v, suffix="", decimals=2):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return "—"
        try:
            return f"{float(v):.{decimals}f}{suffix}"
        except (TypeError, ValueError):
            return str(v)

    if not latest:
        st.markdown(
            '<div class="info-block">'
            '<div style="font-size:13px;color:#9a9a9a">데이터 수신 대기 중…</div>'
            '</div>',
            unsafe_allow_html=True,
        )
        return

    acc  = latest.get("acc_magnitude")
    gyro = latest.get("gyro_magnitude")
    hr   = latest.get("heart_rate")
    skin = latest.get("skin_contact")
    motion_score = latest.get("motion_score")
    wear_score   = latest.get("wear_score")
    dq_score     = latest.get("data_quality_score")
    scenario     = latest.get("scenario", "—")
    skin_kr = "착용" if skin == 1 else ("미착용" if skin == 0 else "—")
    mode_kr = "자동 시퀀스" if mode == "auto" else "수동 강제"
    latch_kr = DECISION_KR.get(latched, latched) if latched else "없음"

    device_html = (
        '<div class="info-block">'
        '<div class="info-block-title">DEVICE</div>'
        + _row("이름", meta.get("name", "—"))
        + _row("User ID", user_id)
        + _row("MAC", meta.get("mac", "—"))
        + _row("모드", mode_kr)
        + '</div>'
    )
    sensors_html = (
        '<div class="info-block" style="margin-top:10px">'
        '<div class="info-block-title">SENSORS</div>'
        + _row("가속도", _fmt(acc, " g", 2))
        + _row("자이로", _fmt(gyro, " °/s", 0))
        + _row("심박수", _fmt(hr, " bpm", 0))
        + _row("착용", skin_kr)
        + '</div>'
    )
    judgment_html = (
        '<div class="info-block" style="margin-top:10px">'
        '<div class="info-block-title">JUDGMENT</div>'
        + _row("현재 시나리오", scenario)
        + _row("동작 점수", _fmt(motion_score, "", 2))
        + _row("착용 점수", _fmt(wear_score, "", 2))
        + _row("품질 점수", _fmt(dq_score, "", 2))
        + _row("판정", decision_kr)
        + _row("Latch", latch_kr)
        + '</div>'
    )
    st.markdown(device_html + sensors_html + judgment_html,
                unsafe_allow_html=True)


# ════════════════════════════════════════════════════════════════════════════
# 메인 뷰
# ════════════════════════════════════════════════════════════════════════════

def render_summary_bar(df: pd.DataFrame, cfg: dict):
    """상단 EMERGENCY 배너 + 상태 요약 6칸."""
    user_ids = list(cfg.get("users", {}).keys())
    counts = {k: 0 for k in CHIP_STYLE.keys()}
    emergency_names = []
    for uid in user_ids:
        latest = latest_user_state(df, uid)
        if not latest:
            counts["NORMAL"] += 1
            continue
        decision = str(latest.get("final_decision", "NORMAL"))
        latched  = str(latest.get("latched_event", "") or "")
        eff = latched if latched else decision
        counts[eff] = counts.get(eff, 0) + 1
        if eff == "EMERGENCY_FALL":
            emergency_names.append(get_user_meta(uid).get("name", uid))

    if emergency_names:
        st.markdown(
            '<div class="emergency-banner">'
            '<div>'
            '<span class="emergency-banner-label">EMERGENCY · 낙상 감지</span>'
            f'<span class="emergency-banner-text">— {", ".join(emergency_names)}</span>'
            '</div>'
            f'<div class="emergency-banner-count">{len(emergency_names)}</div>'
            '</div>',
            unsafe_allow_html=True,
        )

    def _cell(label, n, alert=False):
        val_cls = "summary-cell-value alert" if alert else "summary-cell-value"
        return (
            f'<div class="summary-cell">'
            f'<div class="summary-cell-label">{label}</div>'
            f'<div class="{val_cls}">{n}</div>'
            f'</div>'
        )

    st.markdown(
        '<div class="summary-bar">'
        + _cell("정상", counts.get("NORMAL", 0))
        + _cell("경고", counts.get("WARNING", 0))
        + _cell("긴급 낙상", counts.get("EMERGENCY_FALL", 0),
                alert=counts.get("EMERGENCY_FALL", 0) > 0)
        + _cell("기기 투척", counts.get("DEVICE_THROWN_OR_DROPPED", 0))
        + _cell("미착용", counts.get("DEVICE_REMOVED", 0))
        + _cell("통신 불안정", counts.get("DATA_UNCERTAIN", 0))
        + '</div>',
        unsafe_allow_html=True,
    )


def render_device_grid(df: pd.DataFrame, cfg: dict):
    """디바이스 카드 그리드. 빈 상태면 안내. opened_user는 그리드 아래에 펼침.

    잔상 차단 설계 (v18):
    --------------------
    Streamlit 은 위젯 트리를 *위치(인덱스)* 기반으로 reconcile 한다. 따라서
    "위젯 개수가 사이클마다 변하지 않는다"는 불변식을 지키는 것이 잔상을
    원천적으로 막는 가장 안전한 방법이다. 이를 위해:

      - 카드 그리드는 항상 동일한 row 개수, 동일한 컬럼 개수(CARDS_PER_ROW)
        로 그린다. 사용자가 한 명도 없든 N명이든 컬럼 위젯 *개수* 자체는
        같다. user_id 가 채워지지 않은 컬럼에는 invisible placeholder 를
        출력한다 (이 placeholder 도 st.markdown 1개 — 위젯 개수 시그니처
        보존).
      - detail_slot 도 매 사이클 동일 위치에 단일 `st.empty()` 로 생성.
        opened_user 가 없으면 `detail_slot.empty()` 만 호출 (변수 재할당 X,
        함수 일찍 return 도 X).

    이 불변식 덕분에 사용자 제거 시 백엔드 cfg 변경이 즉시 DOM 에 반영되며,
    이전 사이클의 카드 DOM 이 화면 다른 위치로 떨어지거나 잔상으로 남는
    현상이 발생하지 않는다.
    """
    user_ids = list(cfg.get("users", {}).keys())

    if user_ids:
        st.markdown(
            f'<div class="section-label">'
            f'Devices · 총 {len(user_ids)}대</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div class="section-label">Devices</div>'
            '<div class="empty-state">'
            '<div class="empty-state-icon">⌚</div>'
            '<div class="empty-state-title">등록된 디바이스가 없습니다</div>'
            '<div class="empty-state-desc">'
            '좌측 사이드바에서 이름과 MAC 주소를 입력하여<br>'
            '첫 웨어러블 디바이스를 등록해보세요.'
            '</div></div>',
            unsafe_allow_html=True,
        )
        # 빈 상태에서도 detail_slot 만은 동일 위치에 생성해야 한다.
        # 빈 상태 → 등록 → 카드 그리드 사이클에서 element 개수가 유지된다.
        detail_slot = st.empty()
        detail_slot.empty()
        return

    opened_user = st.session_state.get("opened_user")

    # ── 카드 그리드 ───────────────────────────────────────────────────────────
    #
    # row 개수도 매 사이클 동일하게 유지한다. 사용자가 4명에서 1명으로 줄
    # 어들어 row 가 2개에서 1개로 줄면 row 단위에서 같은 잔상 문제가 발생
    # 할 수 있다. 따라서 *이번 세션에서 본 최대 row 개수* 를 session_state
    # 로 기억하고, 그만큼의 row 슬롯을 항상 그린다. 사용자가 다 빠져나간
    # row 는 invisible placeholder row 로 채워진다.
    CARDS_PER_ROW = 3
    needed_rows = max(1, (len(user_ids) + CARDS_PER_ROW - 1) // CARDS_PER_ROW)
    max_rows = max(needed_rows, st.session_state.get("_grid_max_rows", 0))
    st.session_state["_grid_max_rows"] = max_rows

    for row_idx in range(max_rows):
        row = user_ids[row_idx * CARDS_PER_ROW:(row_idx + 1) * CARDS_PER_ROW]
        cols = st.columns(CARDS_PER_ROW, gap="medium")
        for i in range(CARDS_PER_ROW):
            with cols[i]:
                if i < len(row):
                    uid = row[i]
                    render_device_card(uid, df, is_opened=(uid == opened_user))
                else:
                    # 빈 컬럼 placeholder — 위젯 개수 시그니처 보존용.
                    st.markdown(
                        '<div style="display:none"></div>',
                        unsafe_allow_html=True,
                    )

    # ── 상세 패널 ─────────────────────────────────────────────────────────
    #
    # 설계 원칙 (v16에서 정리)
    # ------------------------------------------------------------------
    # 이전 버전들의 핵심 교훈:
    #
    #   - 슬롯 변수를 재할당하면 위치가 어긋난다 (v9 실패).
    #   - `detail_slot.empty()` 만으로는 하위 트리 깊은 곳의 위젯 잔상이
    #     완전히 차단되지 않는다 (v10~v12 실패).
    #   - `detail_slot.container()` 안에 또 `st.container(key=...)` 를 중첩
    #     하면 element id 계산이 꼬여 *다른 위치의 element 가 상세 패널
    #     안에 끼어든다* (v14~v15 발견된 버그 — 카드 그리드가 상세 패널
    #     본문 안에 또 그려지는 현상). → key 가진 container 중첩 금지.
    #   - 매 사이클마다 위젯의 `type` 이나 element 개수가 바뀌면 streamlit
    #     이 위젯 정체성을 잃어 클릭이 다른 위젯에 전달된다 (v14에서 I'm OK
    #     버튼 클릭 시 오류의 원인). → 위젯 시그니처(key + type + 개수)는
    #     모든 사이클에서 100% 동일해야 한다.
    #
    # 최종 패턴:
    #   1. 단일 `detail_slot = st.empty()` 슬롯을 무조건 생성.
    #   2. opened: `with detail_slot.container():` 만 사용 (중첩 컨테이너 X).
    #   3. closed: `detail_slot.empty()` 만 호출 (변수 재할당 X).
    #   4. 슬롯 내부 모든 위젯의 key 는 user_id 만 기반으로 (latch 미포함).
    #   5. 슬롯 내부 모든 위젯의 type/개수는 latch 와 무관하게 고정.

    # ★ 모든 사이클에서 동일한 위치에 단일 슬롯을 생성한다.
    detail_slot = st.empty()

    opened_user = st.session_state.get("opened_user")

    if opened_user and opened_user in user_ids:
        # 사이클 시작 시 latest 를 한 번만 평가해 스냅샷으로 사용.
        # 하위 모든 컴포넌트가 같은 값을 보도록 보장한다.
        latest_snap = latest_user_state(df, opened_user)
        latched_now = (
            str(latest_snap.get("latched_event", "") or "")
            if latest_snap else ""
        )
        decision_now = (
            str(latest_snap.get("final_decision", "NORMAL"))
            if latest_snap else "NORMAL"
        )

        # ── 패널 상태 시그니처 (panel_sig) ───────────────────────────────
        #
        # 같은 사용자의 패널이라도 *시각적 상태가 전이* 되면 (예: WARNING →
        # EMERGENCY_FALL) 패널 내부의 markdown 컬러 클래스 / context-card
        # 색상 / 상단 강조 바 색상이 모두 바뀐다. 이때 streamlit 의 DOM
        # diff 가 raw HTML 블록을 깨끗하게 교체하지 못하고 *추가로 append*
        # 해버려 "경고 패널 위에 낙상 패널이 또 그려지는" 잔상이 발생한다
        # (v31 까지 재발한 고질 버그).
        #
        # 해결: panel_sig 가 바뀌면 detail_slot 을 강제로 empty() 한 뒤
        # 새 slot 을 다시 만들고, 본문도 새로 그린다. 사용자 시점에서는
        # "경고 패널이 자연스럽게 낙상 패널로 전환" 되는 것처럼 보인다
        # (잔상 0, 두 번째 패널 없음).
        #
        # 시그니처에 decision 까지 포함하는 이유: latched 가 비어 있는
        # 정상↔경고 전이도 같은 메커니즘으로 색상이 바뀌므로 동일 처리.
        sig_key = "_detail_panel_sig"
        if latched_now == "EMERGENCY_FALL":
            curr_sig = (opened_user, "EMERGENCY")
        elif decision_now == "WARNING":
            curr_sig = (opened_user, "WARNING")
        else:
            curr_sig = (opened_user, "NORMAL")
        prev_sig = st.session_state.get(sig_key)
        if prev_sig is not None and prev_sig != curr_sig:
            # 시각적 상태 전이 → 이전 사이클 DOM 즉시 폐기.
            detail_slot.empty()
            # 새 placeholder 로 깨끗한 슬롯에서 본문을 다시 그린다.
            # (.empty() 만으로는 깊은 트리의 raw HTML 잔상이 남을 수
            # 있어 새 슬롯을 함께 만들어준다.)
            detail_slot = st.empty()
        st.session_state[sig_key] = curr_sig

        # 단일 컨테이너 — 중첩 금지 (key 가진 container 중첩이 v15 버그 원인).
        with detail_slot.container():
            # 잔상 방지용 마커 div (CSS hook 용)
            st.markdown(
                f'<div class="detail-wrapper detail-{latched_now or "normal"}" '
                f'data-uid="{opened_user}"></div>',
                unsafe_allow_html=True,
            )

            # 헤더 + 닫기 버튼
            st.markdown('<div style="height:8px"></div>',
                        unsafe_allow_html=True)
            meta = get_user_meta(opened_user)
            h_left, h_right = st.columns([5, 1])
            with h_right:
                if st.button(
                    "✕  닫기",
                    key=f"close_detail__{opened_user}",
                    use_container_width=True,
                    help="상세 패널 닫기",
                ):
                    st.session_state["opened_user"] = None
                    st.rerun()
            with h_left:
                st.markdown(
                    f'<div class="detail-header">'
                    f'<div><span class="detail-name">{meta.get("name", "—")}</span>'
                    f'<span class="detail-uid">· {opened_user}</span></div>'
                    f'</div>'
                    f'<div class="detail-mac">{meta.get("mac", "—")}</div>',
                    unsafe_allow_html=True,
                )

            # 본문 (탭 · 차트 · 정보 카드 · 시나리오 제어)
            render_detail_panel(opened_user, df, cfg)
    else:
        # opened_user 가 없을 때는 슬롯을 명시적으로 비우고
        # 다음 펼침이 깨끗한 시그니처에서 시작하도록 키를 제거한다.
        detail_slot.empty()
        st.session_state.pop("_detail_panel_sig", None)


def render_event_log_section(cfg: dict):
    # 등록된 디바이스가 한 대도 없으면 로그를 표시하지 않는다.
    # (이전 세션에서 남은 파일이 있어도 사용자가 등록을 시작하기 전에는 노출하지 않음)
    if not cfg.get("users"):
        return
    session_start_ts = cfg.get("session_start_ts", 0.0)
    df = load_event_log(25, session_start_ts=session_start_ts)
    if df.empty:
        return
    st.markdown('<div class="section-label">Event Log</div>',
                unsafe_allow_html=True)
    display = df.copy()
    if "final_decision" in display.columns:
        display["판정"] = display["final_decision"].map(
            lambda d: DECISION_KR.get(str(d), str(d)))
    rename_map = {
        "time":          "시각",
        "user_id":       "사용자",
        "scenario":      "시나리오",
        "acc_magnitude": "acc(g)",
        "heart_rate":    "심박",
        "motion_score":  "동작점수",
        "wear_score":    "착용점수",
        "reason":        "판정이유",
    }
    display = display.rename(columns=rename_map)
    if "사용자" in display.columns:
        display["사용자"] = display["사용자"].map(
            lambda uid: get_user_meta(uid).get("name", uid))
    if "시나리오" in display.columns:
        display["시나리오"] = display["시나리오"].map(
            lambda s: SCENARIO_KR.get(str(s), str(s)))
    if "시각" in display.columns:
        try:
            display["시각"] = pd.to_datetime(display["시각"], unit="s").dt.strftime("%H:%M:%S")
        except Exception:
            pass
    keep_cols = ["시각", "사용자", "시나리오", "acc(g)", "심박",
                 "동작점수", "착용점수", "판정", "판정이유"]
    display = display[[c for c in keep_cols if c in display.columns]]
    for col, dec in [("acc(g)", 2), ("동작점수", 2), ("착용점수", 2)]:
        if col in display.columns:
            display[col] = pd.to_numeric(display[col], errors="coerce").round(dec)
    # 가장 최근 로그를 표의 상단에 배치 (load_event_log는 오래된→최신 순으로
    # tail(n_rows)를 반환하므로 표시 직전에 역순으로 뒤집는다). reset_index로
    # Streamlit dataframe이 hide_index=True 상태에서도 정렬된 순서가 유지되도록 한다.
    display = display.iloc[::-1].reset_index(drop=True)
    st.dataframe(display, use_container_width=True, hide_index=True,
                 height=min(320, 44 + 36 * len(display)))


# ════════════════════════════════════════════════════════════════════════════
# main
# ════════════════════════════════════════════════════════════════════════════

def main():
    inject_global_styles()
    render_header()

    cfg = load_runtime_cfg()
    render_sidebar(cfg)

    if "opened_user" not in st.session_state:
        st.session_state["opened_user"] = None

    session_start_ts = cfg.get("session_start_ts", 0.0)
    df = load_session_data(n_lines=1500, session_start_ts=session_start_ts)

    render_summary_bar(df, cfg)
    render_device_grid(df, cfg)
    render_event_log_section(cfg)

    st.markdown(
        '<div class="footer-note">Fall Monitor · ICT 응용기술 · 2026</div>',
        unsafe_allow_html=True,
    )

    # 자동 새로고침
    #
    # Streamlit은 메인 스크립트가 time.sleep()으로 차단되는 동안 발생한
    # 사용자 클릭을 별도 스레드에서 큐잉했다가 sleep이 끝나고 rerun이
    # 호출된 직후 다음 사이클의 prefix로 콜백을 실행한다. 따라서 sleep이
    # 길수록 사용자가 닫기·등록·시나리오 변경 같은 인터랙션 후 화면이
    # 갱신되기까지 더 오래 기다린다.
    #
    # 0.5초로 줄여 평균 인터랙션 지연을 약 0.25초로 낮추되, CPU 부담은
    # 여전히 충분히 낮은 수준으로 유지한다 (시각적 데이터 갱신 빈도는
    # send_interval=0.3초보다 살짝 빠르거나 같은 수준이면 충분).
    #
    # × 클릭으로 디바이스가 제거된 직후처럼 즉각 반영이 필요한 사이클에는
    # `_skip_sleep_once` 플래그로 sleep 한 사이클을 건너뛴다. 그래야 잔상이
    # 사용자에게 시각적으로 보이는 시간이 거의 0에 가까워진다.
    if st.session_state.get("cb_refresh", True):
        if not st.session_state.pop("_skip_sleep_once", False):
            time.sleep(0.5)
        st.rerun()


# streamlit run으로 실행되든 다른 컨텍스트에서 임포트되든 main()이
# 정확히 한 번 실행되도록 단일 호출로 단순화. 이전 if/else 두 분기 모두
# main()을 호출하던 패턴은 의미가 없을 뿐 아니라, 향후 누가 한쪽 분기만
# 수정해서 의도치 않은 중복 호출이 생길 위험이 있었다.
main()
