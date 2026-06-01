# `/data` — 샘플(합성) 데이터 설명

이 프로젝트는 **고정된 외부 데이터셋을 사용하지 않는다.** 모든 센서 데이터는
`src/scenario_player.py`가 실행 시점에 시나리오별로 **합성 생성(synthetic)** 한다.
따라서 이 폴더에 들어 있는 파일은 "원본 데이터셋"이 아니라, 파이프라인이
**실제로 한 번 돌았을 때 남긴 출력의 일부 샘플**이다. 데이터 포맷을 확인하고
README의 판정 로직을 대조해 볼 수 있도록 제공한다.

## 포함된 파일

| 파일 | 내용 | 생성 주체 |
|---|---|---|
| `sample_session.jsonl` | 수신된 패킷 레코드 샘플 (앞 500개). 한 줄에 JSON 1개. | `udp_receiver.py` |
| `sample_event_log.csv` | 판정 상태 전이 로그 샘플 (앞 500행). | `udp_receiver.py` |

## `sample_session.jsonl` 한 줄 예시

```json
{"seq_id": 0, "timestamp": 1779982498.29, "user_id": "U1", "scenario": "normal_idle",
 "acc": {"x": -0.0045, "y": 0.0007, "z": 0.0256},
 "gyro": {"x": -1.941, "y": 0.4871, "z": 1.8388},
 "heart_rate": 71, "skin_contact": 1,
 "acc_magnitude": 0.026, "gyro_magnitude": 2.7177,
 "latency_ms": 610.37, "packet_status": "received", "is_interpolated": false,
 "raw_decision": "NORMAL", "final_decision": "NORMAL", "latched_event": "",
 "motion_score": 0.0, "wear_score": 1.0, "data_quality_score": 1.0,
 "reason": "정상 활동 범위"}
```

- `packet_status` 는 `received` / `missing`(손실) / `interpolated`(선형 보간 복원) 중 하나다.
- `acc_magnitude` 는 **dynamic acceleration magnitude (g, 중력 성분 제거)** 기준이다.
  완전 정지는 0 g 근처이며 1 g 근처가 아니다.

## 런타임에 자동 생성되는 파일 (저장소에 커밋하지 않음 — `.gitignore` 참고)

시스템을 실행하면 다음 파일들이 이 폴더에 자동으로 만들어지거나 갱신된다.

- `runtime_config.json` — 대시보드/송신기/수신기가 공유하는 단일 상태 파일
  (없으면 `src/config.py`의 `DEFAULTS`에서 사용자 0명으로 새로 생성된다)
- `runtime_config.lock` / `.runtime_config_*.tmp` — 파일 락 / atomic write 임시 파일
- `session.jsonl`, `event_log.csv` — 전체 세션 로그 (위 샘플의 원본)
