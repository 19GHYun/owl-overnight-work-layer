"""OWL 디스패처(데스크톱): 워커에 작업을 보내고 결과를 판정."""
import json
import urllib.error
import urllib.request

# LAN 주소가 시스템 프록시로 새지 않도록 프록시를 쓰지 않는다.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class DispatchError(Exception):
    """워커에 닿지 못했거나 응답을 해석하지 못한 경우."""


def call(method: str, base_url: str, path: str, token: str, body=None, timeout=10):
    """(HTTP 상태 코드, 응답 JSON)을 돌려준다. 연결 자체가 실패하면 DispatchError."""
    url = base_url.rstrip("/") + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        status = e.code
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise DispatchError(f"{url} 연결 실패: {type(e).__name__}: {reason}") from e
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError as e:
        raise DispatchError(f"{url} 응답이 JSON이 아님(HTTP {status}): {raw[:200]}") from e


def judge(state: dict):
    """워커가 돌려준 작업 상태로 (accepted 여부, 사유)를 정한다."""
    if state.get("status") == "failed":
        err = state.get("error") or {}
        return False, f"{err.get('step', '?')} 단계 실패: {err.get('reason', '사유 없음')}"
    result = state.get("result") or {}
    if result.get("agent_exit_code") != 0:
        return False, f"에이전트 종료 코드 {result.get('agent_exit_code')}"
    if result.get("test_exit_code") != 0:
        return False, f"테스트 실패(exit {result.get('test_exit_code')})"
    if not result.get("changed_files"):
        return False, "바뀐 파일 없음 — 에이전트가 수정하지 않음"
    return True, f"테스트 통과, 바뀐 파일 {len(result['changed_files'])}개"
