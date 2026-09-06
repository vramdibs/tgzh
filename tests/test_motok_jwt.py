from motok_jwt import HubJwtError, issue_homework_jwt, verify_homework_jwt


def test_jwt_roundtrip() -> None:
    token = issue_homework_jwt("pid-9", secret="hub-secret", ttl_s=60)
    payload = verify_homework_jwt(token, secret="hub-secret")
    assert payload["sub"] == "pid-9"
    assert "homework" in payload["modules"]


def test_jwt_bad_secret() -> None:
    token = issue_homework_jwt("pid-9", secret="hub-secret", ttl_s=60)
    try:
        verify_homework_jwt(token, secret="other")
    except HubJwtError:
        return
    raise AssertionError("expected HubJwtError")
