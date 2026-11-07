import importlib.util
import os
import sys
import uuid
from pathlib import Path


def load_subject():
    path = Path(os.environ["BENCHMARK_REPOSITORY"]) / "src" / "subject.py"
    name = "natural_subject_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    sys.path.insert(0, str(path.parents[1]))
    spec.loader.exec_module(module)
    return module


def test_contract():
    subject = load_subject()
    names = (
        "DB_PASS",
        "SMTP_PASS",
        "MAIL_PWD",
        "SENTRY_DSN",
        "SLACK_WEBHOOK_URL",
        "ENCRYPTION_KEY",
        "SIGNING_KEY",
        "JWT_KEY",
        "STRIPE_SK",
        "DOCKER_AUTH_CONFIG",
    )
    for name in names:
        assert "sample-credential-value" in subject._sensitive_values(
            name, "sample-credential-value"
        ), name
    for name in ("PIP_INDEX_URL", "CELERY_BROKER_URL", "ARBITRARY"):
        found = subject._sensitive_values(
            name, "https://user:pa%73sword@packages.example.invalid/simple"
        )
        assert "password" in found
        assert "pa%73sword" in found
    found = subject._sensitive_values(
        "EXTRA_INDEX",
        "https://u:first-pass@one.example.invalid https://u:second-pass@two.example.invalid",
    )
    assert {"first-pass", "second-pass"} <= found
    assert {"first", "second", "first\nsecond"} <= subject._sensitive_values(
        "DB_PASS", "first\nsecond"
    )
    assert not subject._sensitive_values("PATH", "/usr/bin:/bin")
