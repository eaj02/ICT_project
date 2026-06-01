# 웨어러블 기반 실시간 낙상 감지 시스템
### Wearable-based Real-time Fall Detection over a Constrained Network

> **Course:** Korea University DCSS 405 — ICT Application Technology (2026 Spring)
> **Instructor:** Byungjin Cho
> **Student:** 정윤재 (Jeong Yunjae, 2023270692)
> **Type:** Individual Project — Module 4 Capstone


## 1. 한 줄 요약

웨어러블 센서가 만든 데이터를 **손실이 있는 UDP 네트워크**로 서버에 보내고,
서버가 데이터를 **수집·복구·판단**하여 사용자별 상태(정상 / 경고 / 낙상 / 기기 투척 /
기기 탈착 / 데이터 불확실)를 **실시간 웹 대시보드**에 표시하는 cloud-centric 파이프라인.

이 프로젝트의 초점은 "화려한 AI"가 아니라, **열악한 네트워크 환경에서도 견디고
오류를 처리하며 유의미한 최종 판단을 내리는 시스템 설계**다.

---

## 2. 문제 정의

WHO에 따르면 65세 이상 고령자의 약 28~35%가 매년 낙상을 경험하며, 낙상은 의도하지
않은 부상으로 인한 사망의 주요 원인 중 하나다. 낙상은 단순한 움직임 문제가 아니라
**빠르게 감지하고 대응해야 하는 실시간 안전 문제**다.

상용 제품(예: Apple Watch)은 센서 수집과 판단이 **기기 내부에서** 이루어지는
edge-computing 모델에 가깝다. 본 시스템은 이를 대체하려는 것이 아니라, **여러
웨어러블이 원시 데이터를 중앙 서버로 보내고 서버가 다수 사용자를 동시에
수집·복구·판단하는 cloud-centric 구조**를 보여주는 데 목적이 있다.

가장 핵심적인 설계 질문은 다음과 같다:

> **실제 낙상과, 시계를 벗어 던지는 상황을 어떻게 구분하는가?**
> 가속도만 보면 둘 다 큰 충격이 발생한다. 그래서 충격 하나가 아니라
> **여러 신호를 시간 순서와 함께** 본다.

---

## 3. 6단계 ICT 파이프라인 (과제 요구사항 매핑)

| 단계 | 과제 요구 | 구현 위치 |
|---|---|---|
| ① 데이터 생성 (Edge/Source) | RQ1 | `src/scenario_player.py` + `src/udp_sender.py` (Process A) |
| ② 제약이 포함된 전송 | RQ2 | UDP 전송 + **15% 패킷 손실 + 50~400ms 지연** 의도적 주입 |
| ③ 데이터 수집 (Central) | RQ3 | `src/udp_receiver.py` (Process B), 사용자별 슬롯 분리 |
| ④ AI 처리 / 복구 | RQ4 | `src/recovery.py`(선형 보간) + `src/decision_engine.py`(multi-modal scoring) |
| ⑤ 최종 판단 / 액션 | RQ5 | `classify_event` 6단계 상태 + latch + `I'm OK`/`SOS` |
| ⑥ 웹 시각화 | RQ6 | `dashboard/app.py` (Streamlit 라이브 대시보드) |

두 프로세스(생성 노드 ↔ 수집/처리 노드)는 **논리적으로 분리된 별개 프로세스**이며,
로컬 머신 내에서 UDP 소켓과 파일 기반 IPC로 통신한다.

---

## 4. 시스템 아키텍처

```
┌──────────────────────────────────┐   UDP    ┌──────────────────────────────────┐
│   Process A — 송신 (Edge 시뮬)     │ ───────▶ │   Process B — 수신 + 판정 (Server) │
│   udp_sender.py                   │  :9999   │   udp_receiver.py                 │
│                                   │          │                                   │
│   ┌──────┐ ┌──────┐  N players    │          │   ┌──── MultiUserStateStore ───┐ │
│   │ U1   │ │ U2   │  (사용자별     │          │   │  U1 슬롯 (ring/latch/통계)  │ │
│   │player│ │player│  scenario)    │          │   │  U2 슬롯 ...               │ │
│   └──────┘ └──────┘               │          │   └────────────────────────────┘ │
│   라운드로빈 매 0.3초 1패킷 송신     │          │                                   │
│   + 15% 손실 / 50~400ms 지연 주입   │          │   사용자별 보간 복구 → 판정 → latch │
└──────────────────────────────────┘          └───────────────┬───────────────────┘
                                                              │ session.jsonl / event_log.csv
                                                              ▼
                                   ┌──────────────────────────────────────────────┐
                                   │   Streamlit 대시보드 (dashboard/app.py)         │
                                   │   · 사용자 카드 그리드 (응급 시 빨강 깜빡임)        │
                                   │   · 카드 클릭 → 상세 탭 2개                       │
                                   │       Tab1 실시간 차트 (acc/gyro/HR/skin)        │
                                   │       Tab2 워치 화면 (I'm OK / Emergency SOS)    │
                                   └──────────────────────────────────────────────┘
                                                       ▲  파일 IPC (runtime_config.json)
                                                       │  시나리오 적용 / 사용자 응답 양방향
```

세 프로세스는 `data/runtime_config.json`(단일 source of truth)을 파일 락 + atomic
write로 공유한다. 별도 메시지 브로커(Redis/MQTT)를 도입하지 않은 것은 프로토타입
단순화와 "다중 프로세스 통신" 학습 목표에 맞춘 의도적 결정이다.

---

## 5. 핵심 판단 로직 — Multi-modal Scoring

단일 가속도 임계값 하나로는 낙상과 기기 투척을 구분할 수 없다. 그래서 **3가지 점수를
함께** 본다. (모든 임계값은 `src/config.py`에 학술 인용과 함께 정의되어 있다.)

### (1) `motion_score` — 시간 순서 기반 fall-phase 모델 (0.0 ~ 1.0)

실제 낙상은 **회전(pre-impact) → 충격(impact) → 정지(post-impact)** 의 시간 순서를
따른다. acc 충격(impact)을 anchor로 잡고 그 전후를 시간적으로 연관해 평가한다.

| 구성 요소 | 가중치 | 조건 | 근거 |
|---|---|---|---|
| `[1] IMPACT` | +0.30 | acc max ≥ **2.8 g** (`fall_threshold`) | Bourke 2007, Bagalà 2012 |
| `[2] CO-ROTATION` | +0.25 | impact ±2 tick 안에 gyro ≥ **250 °/s** (`gyro_threshold`) | Huynh 2015 "fall window" |
| `[3] POST-INACTIVITY` | +0.45 | impact 이후 acc < **0.12 g** & gyro < **20 °/s** 가 **2 패킷** 이상 | Abbate 2012, Bagalà 2012 |

> 핵심: **impact 자체가 없으면 회전이 아무리 격해도 0점.** 손목 비틀기는 NORMAL.

### (2) `wear_score` — 착용 여부 (0.0 ~ 1.0)

| 신호 | 가중치 |
|---|---|
| `skin_contact == 1` (피부 접촉) | +0.60 |
| `heart_rate` 존재 | +0.40 |

### (3) `data_quality_score` — 데이터 신뢰도 (0.0 ~ 1.0)

`score = 1 − 1.2·(손실률) − 0.4·(보간 비율)`, 단 **연속 3패킷 이상 손실이면 0**.
복구된 데이터를 무조건 정상 데이터처럼 믿지 않기 위한 안전장치다.

### 최종 판정 (`classify_event`, 우선순위 순서)

| 순위 | 조건 | 판정 (`final_decision`) |
|---|---|---|
| 1 | `data_quality < 0.5` **또는** 손실률 ≥ 0.4 **또는** 연속 손실 ≥ 3 | `DATA_UNCERTAIN` |
| 2 | `wear_score < 0.3` **&** `motion_score > 0.5` | `DEVICE_THROWN_OR_DROPPED` |
| 2 | `wear_score < 0.3` **&** `motion_score ≤ 0.5` | `DEVICE_REMOVED` |
| 3 | `motion_score ≥ 0.7` **&** `wear_score ≥ 0.6` | `EMERGENCY_FALL` |
| 4 | `motion_score ≥ 0.30` | `WARNING` |
| 5 | 그 외 | `NORMAL` |

### 세 상황의 구분 (이 시스템의 핵심)

| 상황 | 착용 | 충격 | 회전 | 이후 정지 | 판정 |
|---|---|---|---|---|---|
| **실제 낙상** | ⭕ | ⭕ | ⭕ | ⭕ | `EMERGENCY_FALL` |
| **기기 투척/탈락** | ❌ | ⭕ (큼) | — | — | `DEVICE_THROWN_OR_DROPPED` |
| **기기 단순 탈착** | ❌ | ❌ | ❌ | — | `DEVICE_REMOVED` |

`EMERGENCY_FALL`은 센서값이 다시 안정되어도 자동으로 NORMAL로 돌아가지 않는다.
낙상 후 사용자가 움직이지 못할 수 있기 때문에, 응급 상태는 사용자가 워치 화면에서
**`I'm OK`** 를 누를 때까지 **latch** 로 유지된다.

---

## 6. 네트워크 제약과 복구

- **전송:** UDP (`127.0.0.1:9999`). 순서/도착을 보장하지 않는 비신뢰 전송.
- **의도적 제약:** 송신 시 **15% 확률로 패킷 드롭**, 도착 패킷마다 **50~400ms 랜덤 지연**.
- **손실 표시:** 수신 측은 seq 간격으로 손실을 감지해 해당 구간을 `missing`으로 표시.
- **복구:** 인접한 두 유효 패킷의 시간 간격이 `interp_max_gap`(기본 **1.2초**) 이하일 때만
  **선형 보간(interpolated)** 으로 복원. 그보다 길면 복구하지 않는다.
- **신뢰도 반영:** 복구 데이터는 `data_quality_score`에 패널티로 반영되며, 품질이 낮으면
  판정 자체를 보류(`DATA_UNCERTAIN`)한다 — **불확실한 데이터로는 응급 호출을 하지 않는다.**

---

## 7. 디렉터리 구조

```
ICT_project_v33/
├── README.md                  # (이 문서) 시스템 개요·로직·영상 링크
├── TROUBLESHOOTING.md         # 개발 중 만난 주요 버그와 해결
├── requirements.txt
├── .gitignore
├── src/                       # 모든 소스 코드
│   ├── config.py              # 전역 설정·임계값(학술 인용 포함)·runtime cfg I/O
│   ├── scenario_player.py     # 사용자별 stateful 센서 시나리오 생성기
│   ├── udp_sender.py          # Process A — N명 라운드로빈 송신 + 손실/지연 주입
│   ├── udp_receiver.py        # Process B — 수신·복구·판정·latch·액션 처리
│   ├── recovery.py            # 선형 보간 기반 결손 복구
│   ├── decision_engine.py     # multi-modal scoring + 6단계 최종 판정
│   └── state_store.py         # MultiUserStateStore — 사용자별 링버퍼/통계/latch
├── dashboard/                 # 웹 GUI
│   └── app.py                 # Streamlit 단일 페이지 대시보드 (탭 2개)
├── data/                      # 샘플(합성) 데이터 — data/README.md 참고
│   ├── README.md
│   ├── sample_session.jsonl   # 수신 패킷 레코드 샘플
│   └── sample_event_log.csv   # 판정 상태 전이 로그 샘플
├── docs/
│   └── 발표대본_v7.md          # 최종 발표 대본
└── tests/
    └── verify_scenarios.py    # 시나리오 분류 분포 + 합성 윈도우 케이스 검증
```

> 런타임 파일(`runtime_config.json`, `session.jsonl`, `event_log.csv` 등)은 실행 시
> 자동 생성되며 `.gitignore`로 저장소에서 제외된다.

---

## 8. 실행 방법

세 개의 터미널이 필요하다. 모두 프로젝트 루트(`ICT_project_v33/`)에서 실행한다.

```bash
# 0) 의존성 설치 (최초 1회)
pip install -r requirements.txt
```

```bash
# 터미널 1 — 서버 (수신 + 판정 엔진)
python src/udp_receiver.py
```

```bash
# 터미널 2 — 송신기 (웨어러블 엣지 시뮬레이션)
python src/udp_sender.py
```

```bash
# 터미널 3 — 대시보드
streamlit run dashboard/app.py
```

브라우저가 자동으로 열린다. 처음에는 **사용자 0명**이며, 사이드바에서 `[이름 + MAC]`을
입력해 디바이스를 직접 등록하면서 시연한다.

```bash
# (선택) 판정 로직 검증
python tests/verify_scenarios.py
```

**요구 환경:** Python 3.10+ / `pandas` `numpy` `streamlit` `plotly` (버전은 `requirements.txt` 참고)

---

## 9. 데모 시나리오 (발표 흐름)

1. **사용자 등록** — 사이드바에서 `Alice` 등록 → 카드 생성, 클릭 시 상세 탭 표시.
2. **정상 상태** — `normal_idle`: acc/gyro 안정, 착용 신호 유지 → `NORMAL`.
3. **실제 낙상** — `real_fall`: 충격 + 회전 + 이후 정지 + 착용 유지 → `EMERGENCY_FALL`(latch).
4. **기기 투척** — (`I'm OK` 후) `watch_thrown`: 큰 충격이지만 심박·접촉 사라짐 → `DEVICE_THROWN_OR_DROPPED`.
5. **기기 탈착** — `watch_removed`: 착용 신호만 사라지고 충격 없음 → `DEVICE_REMOVED`.
6. **패킷 손실/복구** — 손실 구간 `missing` 표시 + 선형 보간, 품질 낮으면 `DATA_UNCERTAIN`.
7. **다중 사용자** — `Bob` 추가 후 Alice=낙상 / Bob=정상 → 사용자별 상태가 **독립적으로** 관리됨(cloud-centric).

시연 가능한 시나리오: `normal_idle`, `walking`, `running`, `fall_like_motion`,
`real_fall`, `watch_thrown`, `watch_removed`.

---

## 10. 검증

`tests/verify_scenarios.py`는 세 가지를 한 번에 검증한다.

- **A. 시나리오 분류 분포** — 각 시나리오를 100회(random seed) 돌려 의도한 판정으로
  분류되는지 확인 (`real_fall` → EMERGENCY_FALL, `watch_thrown` → THROWN 등).
- **B. 시뮬레이션 수치 범위** — acc/gyro 값이 dynamic-acc 기준 범위 안에 있는지,
  missing 패킷이 올바르게 처리되는지.
- **C. 시간 순서 케이스** — 손목 비틀기 → NORMAL, 책상 충돌 → WARNING,
  완전한 낙상 → EMERGENCY_FALL 등 합성 윈도우 단위 검증.

---

## 11. 한계와 후속 작업

- 검증은 합성 데이터 기반. SisFall / MobiFall 등 **공개 데이터셋 검증은 향후 작업**.
- 다수 사용자가 라운드로빈으로 매 0.3초에 한 명씩 송신 → 사용자당 실제 샘플링
  주기 ≈ 1.2초로, 실제 wearable(25~50Hz)보다 매우 느림 (데모 가독성 우선 설계).
- `Emergency SOS` 응답은 현재 콘솔 로그로만 시뮬레이션 (실제 알림 시스템 미연동).
- UDP는 순서를 보장하지 않으나 본 시스템은 in-order delivery를 가정한다 (실무에서는
  seq 기반 reorder buffer 필요 — `TROUBLESHOOTING.md` 참고).
- 파일 기반 IPC는 프로토타입 규모에서 충분하나, 대규모에서는 Redis/MQTT 권장.

---

## 12. 참고 문헌

### 임계값·판정 로직 근거
- **Bourke, A. K., O'Brien, J. V., & Lyons, G. M. (2007).** *Evaluation of a threshold-based tri-axial accelerometer fall detection algorithm.* Gait & Posture, 26(2), 194–199.
- **Bourke, A. K., & Lyons, G. M. (2008).** *A threshold-based fall-detection algorithm using a bi-axial gyroscope sensor.* Medical Engineering & Physics, 30(1), 84–90. — gyro 임계 근거.
- **Abbate, S. et al. (2012).** *A smartphone-based fall detection system.* Pervasive and Mobile Computing, 8(6), 883–899. — post-impact inactivity 근거.
- **Habib, M. A. et al. (2022).** *Fall detection using accelerometer-based smartphones: Where do we go from here?* Frontiers in Public Health, 10:996021. — FAM/PT 임계값 쌍.
- **Kangas, M. et al. (2008).** *Comparison of low-complexity fall detection algorithms for body attached accelerometers.* Gait & Posture, 28(2), 285–291. — wrist 정확도 한계 → multi-modal 도입 동기.

### 시간 순서 fall-phase 모델 근거
- **Bagalà, F. et al. (2012).** *Evaluation of accelerometer-based fall detection algorithms on real-world falls.* PLOS ONE, 7(5), e37062. — four-phase fall 모델.
- **Huynh, Q. T. et al. (2015).** *Optimization of an accelerometer and gyroscope-based fall detection algorithm.* Journal of Sensors, 2015, 452078. — co-rotation "fall window".
- **Liu, S.-H. et al. (2017).** *A novel hierarchical fall detection algorithm using a multiphase fall model.* Sensors, 17(2), 307. — phase 순서 검사.

### 데이터셋 / 비교 연구
- **Sucerquia, A. et al. (2017).** *SisFall: A fall and movement dataset.* Sensors, 17(1), 198.
- **Vavoulas, G. et al. (2013).** *The MobiFall dataset.* IEEE BIBE 2013.
- **Ruiz-Garcia, J. C. et al. (2023).** *CareFall: Automatic Fall Detection through Wearable Devices and AI Methods.* arXiv:2307.05275.
- **Zhang, J. et al. (2024).** *An Effective Deep Learning Framework for Fall Detection.* JMIR, 26:e56750.

---

## 부록 — 버전 이력 (요약)

개발 과정에서 다음과 같이 발전했다. 상세 변경 사항과 디버깅 기록은
`TROUBLESHOOTING.md`에 있다.

- **v2:** 단일 사용자 → 다수 사용자 중앙 모니터링(cloud-centric), 워치 화면 + 사용자 응답 버튼, 동적 디바이스 추가/제거.
- **v4~v5:** 워치 페이지를 메인 단일 페이지로 통합, 미니멀 카드 그리드 + 상세 탭(차트/워치) UI.
- **v6:** 모든 임계값·시뮬레이션 분포를 fall detection 표준 문헌에 맞춰 정렬하고 코드 주석에 인용 명시.
- **v7:** `motion_score`를 **시간 순서 fall-phase 모델**(impact anchor + co-rotation + post-inactivity)로 재설계. 회전만으로는 낙상으로 판정하지 않도록 개선.

> 현재 코드에 반영된 임계값(`src/config.py`): `fall_threshold = 2.8 g`,
> `warn_threshold = 1.3 g`, `gyro_threshold = 250 °/s`. 본 README의 모든 수치는
> 실제 코드 값과 일치한다.
