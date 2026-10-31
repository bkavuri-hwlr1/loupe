from src import subject


def test_smoke():
    assert "sample-token" in subject._sensitive_values("ACCESS_TOKEN", "sample-token")
    assert not subject._sensitive_values("LANG", "en_US.UTF-8")
