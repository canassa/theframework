import theframework


def test_version_returns_integer() -> None:
    assert theframework.version() == 1
