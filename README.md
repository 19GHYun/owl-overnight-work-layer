# OWL — Overnight Work Layer

퇴근 전에 지시하면 사무실의 남는 PC들이 밤새 나눠 일하고, 검수까지 마친 결과를 아침에 받아보는
설치형 AI 개발 자동화 시스템을 만드는 프로젝트입니다.

## 왜 만드는가

AI 코딩 에이전트 하나로도 생산성은 오르지만, 작업이 늘수록 무엇을 시킬지 쪼개고, 결과를 하나하나
검수하고, 틀린 것을 다시 시키는 관리 일이 사람에게 쌓입니다. 에이전트를 여러 개 쓰고 싶어도 관리하는
사람이 한 명이면 늘릴 수 없습니다. OWL은 이 관리 계층(작업 분해, 배분, 검수, 재지시)을 자동화하고,
퇴근 후 비어 있는 PC를 실행 노드로 씁니다.

## 목표 구조

- 관리 PC 1대가 작업을 나눠 실행 PC N대에 배분
- 실행 PC는 각자 격리된 작업 공간에서 코드 수정·빌드·테스트
- 빌드·테스트·지시 범위 검사를 통과한 결과만 사람에게 전달, 실패하면 사유와 함께 재지시
- 계층별로 다른 모델(상용 API 또는 로컬 LLM) 지정
- 설치형: 작업 이력, 산출물, 검수 로그가 조직 내부 PC에 남음

## 지금 어디까지 왔나

| 단계 | 내용 | 상태 |
| --- | --- | --- |
| 0 | PC 2대 직접 연결, 공통 로그 형식 (이 저장소 v0.1.1) | 테스트 중 |
| 1 | 자체 관리 루프, 모델 어댑터, 작업 공간 격리, 연결 끊김 처리 | 예정 |
| 2 | 무인 실행 통제: 재시도 상한, 무진전 감지, 야간 예산 상한, 아침 보고서 | 예정 |
| 3 | 실행 PC 3대, 설치·등록 스크립트, 예약 기동·종료 | 예정 |
| 4 | 폐쇄망 모드: 로컬 LLM만으로 끝까지 실행 | 예정 |
| 5 | 고정 과업으로 단일 에이전트 대비 측정 | 예정 |

## 개발 원칙

- 실제로 동작하는 코드만 넣습니다. 연결이 안 될 때 목업 데이터로 채우는 시뮬레이터는 만들지 않습니다.
- 기능이 실패하면 무엇이 문제인지 로그로 판단할 수 있게 기록합니다 (단계, 사유, 원본 출력 위치).
- 시간, 비용, 통과율 같은 수치는 로그에서 계산한 실측값만 씁니다.

---

# 이번 버전: 직접 연결 테스트 (v0.1.1)

메인 PC(디스패처)가 같은 네트워크의 미니PC(워커)로 작업을 보내면, 미니PC가
git 작업 폴더 생성 → Claude Code 헤드리스 실행 → 테스트 → 브랜치 push까지 하고 결과를 돌려줍니다.
두 PC 모두 같은 `OWL.exe` 하나를 쓰고, 실행할 때 역할을 고릅니다.

## 1. exe 만들기 (메인 PC에서 한 번)

1. Python 3.11 이상 설치 (설치 화면에서 "Add python.exe to PATH" 체크)
2. 이 폴더에서 `build.bat` 더블클릭
3. `dist\OWL.exe`가 생기면 완료

PyInstaller 단일 파일 exe는 서명이 없어서 Windows Defender나 SmartScreen 경고가 뜰 수 있습니다.
내 PC 두 대에서 테스트하는 동안에는 예외로 허용하면 됩니다.

## 2. 두 PC에 놓기

각 PC에 빈 폴더를 하나 만들고 `OWL.exe`를 복사합니다 (예: `C:\owl\OWL.exe`).
실행하면 exe 옆에 아래가 생깁니다.

| 이름 | 내용 |
| --- | --- |
| `logs\minipc-YYYYMMDD.jsonl` | 워커 로그 (미니PC) |
| `logs\desktop-YYYYMMDD.jsonl` | 디스패처 로그 (메인 PC) |
| `results\<작업 ID>\` | 워커: Claude Code 출력 원본, 테스트 출력, result.json |
| `results\<작업 ID>.json` | 디스패처: 받은 결과 + 판정 |
| `tasks\` | 디스패처에서 저장한 작업 정의 |
| `owl_config.json` | 마지막 설정 (토큰이 평문으로 저장됨, LAN 테스트용) |
| `logs\owl-gui-internal.log` | 프로그램 자체가 예상 못 한 오류로 멈췄을 때의 기록 |

## 3. 미니PC 준비 (워커 시작 전에)

- [ ] 공유기에서 미니PC IP 고정 (DHCP 예약)
- [ ] 네트워크 프로필 '개인' 확인: PowerShell `Get-NetConnectionProfile`
- [ ] 두 PC 시간 동기화: 관리자 PowerShell `w32tm /resync`
- [ ] Claude Code 헤드리스 확인: PowerShell `claude -p "hello" --output-format json` 이 JSON을 바로 출력
- [ ] 커밋 작성자: 워커 창의 '커밋 작성자 (이름 / 메일)'에 입력합니다. 미니PC에 git 전역 설정이
  있으면 자동으로 채워지고, 없으면 직접 입력하면 됩니다. 이 값은 OWL이 만드는 커밋에만 쓰이고
  git 전역 설정은 바꾸지 않습니다. 비어 있거나 메일 형식이 틀리면 워커가 시작되지 않습니다.
- [ ] 테스트용 저장소를 미니PC에서 clone·push 할 수 있는지 한 번 손으로 확인
- [ ] Docker Desktop 실행, 쓸 테스트 명령을 PowerShell에서 손으로 한 번 실행
- [ ] 방화벽 (관리자 PowerShell):
  `New-NetFirewallRule -DisplayName "OWL worker" -Direction Inbound -Protocol TCP -LocalPort 8765 -Action Allow -Profile Private`
- [ ] `OWL.exe`를 처음 워커로 시작할 때 방화벽 창이 뜨면 **개인 네트워크 허용**.
  여기서 취소하면 차단 규칙이 생기고, 차단 규칙은 위 허용 규칙보다 우선합니다.
- [ ] 테스트하는 동안 미니PC 절전 끄기

워커는 받은 지시를 그대로 실행합니다. 공유기 포트포워딩은 하지 마세요.

## 4. 실행

**미니PC — 워커**

1. `OWL.exe` 실행 → "워커" 선택
2. 받을 주소: 목록에서 미니PC의 LAN IP (예: 192.168.0.23)
3. 포트 8765, 토큰 아무 문자열 (메인 PC와 같게), 작업 폴더 (기본 `%USERPROFILE%\owl_work`)
4. 커밋 작성자 이름/메일 확인 ('git 전역 설정 불러오기'로 다시 채울 수 있음)
5. '필요한 프로그램'에 claude, git 경로가 보이는지 확인 → **워커 시작**

**메인 PC — 디스패처**

1. `OWL.exe` 실행 → "디스패처" 선택
2. 워커 주소 `http://<미니PC IP>:8765`, 같은 토큰 → **연결 확인**
   (워커의 호스트 이름과 claude/git/docker 경로가 뜨면 연결 성공)
3. 저장소, 기준 브랜치, 지시문, 테스트 명령, 제한 시간 입력 → **작업 보내기**
4. 5초마다 자동으로 상태를 조회하고, 끝나면 판정(accepted / rejected)과 사유가 표에 뜹니다.
   행을 고르면 아래에 워커가 돌려준 결과 전체가 보입니다.

테스트 명령은 미니PC의 작업 폴더에서 `cmd.exe`로 실행됩니다. Docker로 그 폴더를 넘길 때는
`%cd%`를 씁니다. 예:

```
docker run --rm -v "%cd%":/app -w /app python:3.12-slim python -m unittest -v
```

## 5. 테스트 시나리오

| 작업 | 내용 | 기대 결과 |
| --- | --- | --- |
| t-0001 | 작은 함수 추가 + 테스트 작성 지시 | accepted, 원격에 `owl/<작업 ID>` 브랜치 |
| t-0002 | 통과할 수 없는 테스트가 있는 상태에서 무관한 수정 지시 | rejected, 사유에 테스트 실패 |
| t-0003 (선택) | 작업 도중 워커 창 닫기 | 디스패처에 `dispatch_error`, 1분 뒤 '연결 끊김' |

완료 확인: 두 PC의 `logs\*.jsonl`을 합쳐 작업 ID와 시각으로 정렬했을 때, t-0001과 t-0002의
전 과정(실패하면 멈춘 단계와 사유 포함)이 로그만으로 이어지면 됩니다.

## 6. 로그 이벤트

| PC | 이벤트 |
| --- | --- |
| 미니PC | `worker_started`, `worker_stopped`, `received`, `auth_failed`, `git_synced`, `agent_started`, `agent_finished`, `test_started`, `test_finished`, `pushed`, `done`, `error` |
| 메인 PC | `dispatched`, `result_received`, `accepted`, `rejected`, `dispatch_error` |

공통 필드: `ts`, `task_id`(작업과 무관하면 `-`), `pc`, `host`, `event`.
필요할 때만: `step`, `step_ms`, `exit_code`, `reason`, `cost_usd`(Claude Code 출력에 값이 없으면 `null`).

## 7. 판정 기준 (메인 PC)

- 워커 단계 실패 → rejected (사유: 멈춘 단계와 원인)
- Claude Code 종료 코드가 0이 아님 → rejected
- 테스트 종료 코드가 0이 아님 → rejected
- 바뀐 파일이 없음 → rejected (수정 권한 또는 지시문 확인)
- 그 외 → accepted

## 8. 알려진 제약 (v0.1.0)

- 워커는 작업을 한 번에 하나씩 순서대로 실행합니다.
- 같은 작업 ID는 다시 쓸 수 없습니다. 원격에 `owl/<작업 ID>` 브랜치가 남아 있으면 git 단계에서 실패합니다.
- 작업 폴더(`owl_work\worktrees\<작업 ID>`)는 자동으로 지우지 않습니다. 결과 확인 후
  `git worktree remove`로 정리하세요.
- 워커 중지 버튼은 새 접수만 막습니다. 실행 중인 작업은 끝까지 갑니다.
- 결과 커밋은 `git -c user.name=… -c user.email=… commit`으로 만들어, 미니PC의 git 전역 설정을 건드리지 않습니다.
- Claude Code 실행 시 `--permission-mode acceptEdits`로 파일 수정만 허용합니다. 테스트는 워커가 직접 실행합니다.
- 재지시, 재시도 상한, 비용 상한은 아직 없습니다 (체크리스트 2단계).

## 9. 막혔을 때

해당 작업의 `error` 로그 줄, 그리고 미니PC `results\<작업 ID>\` 폴더의
`agent_output.json`, `agent_stderr.txt`, `test_output.txt`를 같이 보면 원인이 드러납니다.
