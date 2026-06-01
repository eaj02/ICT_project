# TROUBLESHOOTING — v2 개발 중 만난 주요 문제와 해결

본 문서는 v1에서 v2(다수 사용자 + 시계 화면 + 동적 디바이스 관리)로 확장하며
실제로 부딪쳤던 설계·구현 문제와 그 해결을 기록한다. 발표 Q&A 대비용이며,
ICT 과목이 강조하는 "system designer의 사고"의 흔적이다.

---

## 1. 라운드로빈 송신 시 사용자별 손실률이 70%로 잘못 측정되던 문제

### 증상
v2 초기 구현에서 receiver가 모든 패킷을 `DATA_UNCERTAIN`으로 판정했다. 통계상으로는
손실률이 65~70%로 표시되었지만 실제 sender 로그에서는 15% 손실률만 발생했다.

### 원인
초기 sender는 전역 단일 `seq_id` 카운터를 사용했다. 라운드로빈으로 4명의 사용자가
번갈아 송신하면 같은 사용자의 연속 패킷 사이 seq 간격은 항상 4가 되지만, receiver는
**사용자별** 마지막 seq를 비교하므로 매 패킷마다 "3개 손실"로 잘못 인식했다.

```
실제 송신: U01-0 U02-0 U03-0 U04-0 U01-1 U02-1 …
receiver의 U01 관점: seq 0 → seq 4 → seq 8 … (매번 3개 gap!)
```

### 해결
sender에서 사용자별로 독립된 seq 카운터(`user_seq: dict[str, int]`)를 유지한다.
각 패킷의 `seq_id`는 그 사용자의 카운터 값을 사용한다.

```python
# 수정 후
pkt["seq_id"] = user_seq[current_uid]
user_seq[current_uid] += 1
```

이렇게 하면 같은 사용자의 연속 패킷은 seq 차이가 1이 되어 정상 손실(15%)만 잡힌다.

---

## 2. EMERGENCY_FALL 응답 후에도 latch가 즉시 재발생하는 race condition

### 증상
사용자가 시계 화면에서 "I'm OK"를 누르면 latch가 해제되어 normal_idle로 전환되지만,
1초 후 다시 EMERGENCY_FALL이 latch되는 현상이 발생했다.

### 원인
초기 구현은 "I'm OK" 응답을 받으면 `latched_event = None`으로 풀고 active_scenario만
바꿨다. 하지만 receiver의 링버퍼(최근 12개 패킷 윈도우)에 직전 spike 패킷이 남아 있어서,
다음 패킷이 들어오면 motion_score가 다시 0.7 이상으로 계산되어 latch가 재걸렸다.

### 해결
"I'm OK" 응답 처리 시 `slot.reset()`을 호출해 해당 사용자의 링버퍼 자체를 비운다.
그렇게 하면 새 normal 패킷들이 들어오는 동안 윈도우가 깨끗하게 채워진다.
시나리오 변경(대시보드의 적용 버튼) 시에도 동일하게 reset하도록 통일했다.

---

## 3. 사용자를 제거해도 자동으로 부활하던 문제

### 증상
대시보드에서 U04를 제거하면 `remove_user`가 "성공"으로 반환하지만, 다음 cycle에서
sender가 다시 U04를 송신했다. `load_runtime_cfg()`를 호출하면 U04가 다시 보였다.

### 원인
v1에서 상속받은 `load_runtime_cfg()` 로직이 "DEFAULTS의 4명 중 cfg에 없는 사용자는
자동 복원"하도록 작성되어 있었다. 동적 add/remove를 지원하려면 이 자동 복원이
오히려 방해가 된다.

```python
# v1 (문제)
for uid in DEFAULTS["users"]:
    if uid not in users:
        users[uid] = _default_user_state()    # ← 매번 자동 복원
```

### 해결
파일이 존재하면 그 안의 users 딕셔너리를 그대로 신뢰하고, DEFAULTS 사용자를 자동
복원하지 않는다. DEFAULTS는 **최초 실행 시 cfg 파일 자체가 없을 때**만 시드 역할을 한다.
이렇게 하면 명시적인 add/remove 의도가 보존된다.

---

## 4. 사용자별 시나리오 변경이 다른 사용자에게 전파되던 문제

### 증상
v1에서 시나리오 변경은 전역 단일 `active_scenario` 키로 관리했다.
v2에서 멀티유저로 확장할 때 단순히 sender가 전역 키를 모든 사용자에게 적용해버려서,
한 사용자에게 real_fall을 적용하면 4명 모두 동시에 낙상이 발생하는 버그가 생겼다.

### 해결
`runtime_config.json`의 스키마를 다음으로 변경:

```json
{
  "users": {
    "U01": { "active_scenario": "normal_idle", "scenario_version": 0, "user_action": null, ... },
    "U02": { "active_scenario": "real_fall",   "scenario_version": 3, ... },
    ...
  }
}
```

sender는 매 사이클마다 현재 송신 대상 사용자의 user_state를 읽어 해당 사용자의 player만
업데이트한다. receiver의 latch와 처리도 사용자별로 독립이다.

---

## 5. 시계 페이지에서 빈 사용자 풀 처리

### 증상
모든 사용자를 제거한 직후 watch 페이지를 열면 `st.selectbox`가 빈 리스트에 대해
에러를 던졌다.

### 해결
페이지 진입 시 `user_ids`가 비어 있으면 `st.warning`과 `st.stop()`으로 깔끔히 중단한다.
`remove_user`도 최소 1명을 유지하도록 가드를 두었다 (`if len(users) <= 1: return False`).

---

## 6. 파일 기반 IPC의 race condition 우려

### 우려 사항
대시보드(streamlit)와 sender, receiver가 모두 같은 `runtime_config.json`을 읽고/쓴다.
세 프로세스가 동시에 write할 때 파일이 깨질 수 있다.

### 실측 결과 및 완화
실제 데모 부하(0.3s 송신 주기, 1s 대시보드 폴링)에서는 문제가 나타나지 않았다.
write는 모두 `json.dump`로 atomic하게 일어나고, 사용자 액션(I'm OK 등)은 user별
`user_action_version`을 증가시켜 receiver가 monotonic counter로 처리한다 — 이전 액션을
중복 처리하지 않는다.

근본적 해결은 file lock(fcntl)이나 별도 메시지 큐(Redis 등) 도입이지만, 본 프로젝트의
프로토타입 단순성을 위해 의도적으로 도입하지 않았다. 실제 배포 시 필요한 변경 사항으로
README §7에 future work로 명시했다.

---

## 7. CSS 키프레임 애니메이션이 Streamlit에서 일관되게 동작하는지

### 우려 사항
응급 카드의 빨간 깜빡임과 시계 화면의 진동(`@keyframes shake`)을 inline `<style>`로
주입했다. Streamlit이 매 rerun마다 DOM을 갈아치우면 애니메이션이 끊길 수 있다는 우려.

### 실측 결과
`@st.fragment(run_every=1.0)`을 사용하면 fragment 내부만 재렌더링되고 CSS 클래스가
있는 한 애니메이션이 자연스럽게 유지된다. CSS 자체는 head에 inject되며 streamlit의
rerun 영향을 받지 않는다.

---

## 8. UDP 패킷 순서 보장 부재

### 한계 (해결되지 않은 부분)
UDP는 패킷 순서를 보장하지 않는다. 본 receiver는 같은 사용자의 패킷이 seq 오름차순으로
도착한다고 가정한다. 로컬호스트 환경에서는 거의 발생하지 않지만, 실제 무선 네트워크에서는
out-of-order delivery가 일어날 수 있다.

### 실제 배포 시 필요한 변경
- seq 기반 reorder buffer (작은 window) 도입
- 또는 MQTT/CoAP 같은 IoT 표준 프로토콜로 대체

이는 발표 Q&A 답변용으로 명시했고, README §7에 future work로 기록했다.

---

## 9. 발표 시연 시간이 부족할 경우 대비

낙상 spike는 4명 라운드로빈에서 약 15~18초 후 발생한다(_FALL_PRE=8 × 1.2s ≒ 10s
+ spike 도달까지). 발표 도중 기다리기 어렵다면:

- `src/scenario_player.py`의 `_FALL_PRE = 8`을 `4`로 줄여 7~8초 안에 spike 발생
- 또는 `src/config.py`의 `send_interval` 기본값 0.3을 0.2로 줄임

본 코드는 데모 가독성과 자연스러움의 균형을 위해 8 tick으로 두었다.

---

## 10. v5 → v6 UI 버그 수정 (대시보드 잔상·알림 문제)

발표 직전 점검 중 발견된 대시보드 UI 버그 세 가지와 해결 기록.

### 10.1 등록 알림이 화면에서 사라지지 않는 문제

#### 증상
사이드바 "디바이스 등록" 폼에서 잘못된 MAC을 입력하면 빨간 에러 알림이
표시되는데, 이후 자동 새로고침(1초)이 계속 돌아도 알림이 영원히 남아 있었다.
반대로 성공 알림은 표시되자마자 1초 사이클에 즉시 사라져서 사용자가 인지하기
전에 사라지는 문제도 있었다.

#### 원인
이전 구현은 두 종류 알림을 비대칭으로 다뤘다:

```python
# 성공: 표시 직후 즉시 pop → 자동 새로고침에 너무 빨리 사라짐
if st.session_state.get("form_success"):
    st.success(...)
    st.session_state.pop("form_success", None)

# 에러: ✕ 버튼이나 다음 submit 전까지 영구 노출
if st.session_state.get("form_error"):
    st.error(...)
    # pop 없음 — 자동 새로고침마다 매번 다시 그려짐
```

자동 새로고침이 1초마다 도는 환경에서 "한 사이클만 표시" 정책은 사실상
"전혀 보이지 않음"과 같고, "영구 표시" 정책은 사실상 "영원히 안 사라짐"과
같다. 두 정책 모두 사용자 경험을 망쳤다.

#### 해결
알림 메시지에 timestamp를 함께 저장하고, **`TOAST_LIFETIME_SEC = 4.0`초**가
지나면 자동으로 만료되도록 통일했다. ✕ 버튼은 즉시 닫기 옵션으로 유지.

```python
if submitted and ok:
    st.session_state["form_success"]    = msg
    st.session_state["form_success_ts"] = time.time()

# 매 사이클마다 만료 검사
if success_msg:
    if now_ts - success_ts > TOAST_LIFETIME_SEC:
        st.session_state.pop("form_success", None)
        st.session_state.pop("form_success_ts", None)
    else:
        st.success(success_msg)
```

자동 새로고침 주기와 알림 수명을 분리한 덕에 자동 새로고침 주기를 바꿔도
알림은 항상 4초간 표시된다.

### 10.2 "닫기" 후에도 detail panel 헤더가 화면에 남는 문제

#### 증상
디바이스 카드의 "상세 보기"로 detail panel을 펼친 뒤 panel 안의 "✕ 닫기"
버튼을 누르면, 차트와 탭은 사라지지만 그 위쪽의 헤더(이름·UID·MAC)와
강조 색 바가 화면 하단에 잔존했다. 자동 새로고침이 도는 다음 사이클에야
완전히 사라졌고, 자동 새로고침을 끈 상태에서는 사실상 영원히 남았다.

#### 원인
`on_click` 콜백은 위젯 생성 시점에 실행된다. 이전 구현에서 닫기 버튼은
`render_detail_panel` 함수 내부의 헤더 영역에 있었다:

```python
def render_detail_panel(...):
    st.markdown('<div>...강조바...</div>')         # ← 이미 화면에 그려짐
    st.markdown('<div class="detail-header">...')  # ← 이미 화면에 그려짐
    with hcol2:
        st.button("✕  닫기", on_click=_close_detail)  # ← 콜백 발동 (너무 늦음)

    if st.session_state.get("opened_user") != user_id:
        return    # 차트는 안 그려지지만, 위의 헤더는 이미 화면에 표시됨
```

닫기 콜백이 발동되어 `opened_user`가 `None`이 되더라도, 그 *위*에 이미
그려진 헤더와 강조바는 현재 사이클 화면에 반영된 상태로 남는다.
자동 새로고침(1초)이 와야 헤더가 사라진다.

#### 해결
**닫기 버튼을 panel 본문보다 먼저 위젯으로 등록**하도록 구조를 바꿨다.
헤더와 닫기 버튼은 `st.empty()` placeholder 컨테이너 안의 같은 columns 행에
배치하되, **위젯 등록 순서를 닫기 버튼 → 헤더 markdown** 순으로 두어 콜백이
markdown보다 먼저 발동되게 했다. 콜백이 `opened_user`를 `None`으로 만들면
바로 다음 줄에서 `header_ph.empty()`로 placeholder 자체를 비워서, 헤더와
닫기 버튼까지 깨끗하게 사라진다.

```python
header_ph = st.empty()
with header_ph.container():
    h_left, h_right = st.columns([5, 1])
    with h_right:    # ← 위젯 등록을 먼저 (콜백 트리거)
        st.button("✕  닫기", on_click=_close_detail, ...)
    with h_left:     # ← markdown은 그 다음
        st.markdown(...헤더...)

if st.session_state.get("opened_user") != opened_user:
    header_ph.empty()    # 헤더+닫기 버튼까지 모두 깨끗이 제거
    return

render_detail_panel(opened_user, df, cfg)    # 본문(강조바·탭·차트)
```

동시에 `render_detail_panel`에서는 내부의 헤더·닫기 버튼 코드를 제거하고
강조 바와 본문(현재상황·탭)만 그리도록 정리했다.

### 10.3 "디폴트 화면에서 최근 로그 밑에 또 최근 로그" 잔상

#### 증상
detail panel을 열었다가 닫는 동작 직후 잠시 동안 Event Log 위쪽에 사라져야
할 헤더 영역이 잔존해서, 마치 Event Log 섹션이 두 번 나오는 듯 보였다.

#### 원인
10.2의 잔상이 Event Log 위쪽에 한 사이클 동안 보이는 것이 직접 원인이었다.
또한 `app.py` 끝의 비정상 패턴

```python
if __name__ == "__main__":
    main()
else:
    main()
```

은 동작상 한 번만 실행되지만 의도가 불분명해서, 한쪽 분기를 누가 수정하면
중복 실행이 생길 위험이 있었다.

#### 해결
- 10.2 수정으로 detail panel 잔상이 같은 사이클에서 즉시 사라지므로 Event Log
  위쪽에 무엇도 남지 않는다.
- `if/else` 두 분기를 단순 `main()` 호출 한 줄로 정리해 향후 회귀를 예방했다.
