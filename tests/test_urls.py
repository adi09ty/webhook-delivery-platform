import pytest

from app.urls import UnsafeTargetError, validate_target_url


@pytest.mark.parametrize("url", ["http://localhost:9000/hook", "https://hooks.example.test/in", "http://10.0.0.5/hook"])
def test_anything_http_is_fine_when_private_targets_are_allowed(url):
    assert validate_target_url(url, allow_private=True) == url


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/hook",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "example.com/hook",
        "https://",
        "https://user:pass@example.com/hook",
        "https://example.com/" + "a" * 2100,
    ],
)
def test_malformed_or_odd_urls_are_rejected(url):
    with pytest.raises(ValueError):
        validate_target_url(url, allow_private=True)


def test_plain_http_is_refused_in_production_mode():
    with pytest.raises(ValueError, match="https"):
        validate_target_url("http://93.184.216.34/hook", allow_private=False)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/hook",
        "https://10.0.0.5/hook",
        "https://192.168.1.10/hook",
        "https://172.16.0.1/hook",
        "https://169.254.169.254/latest/meta-data",  # the cloud metadata address
        "https://[::1]/hook",
        "https://localhost/hook",
    ],
)
def test_private_and_loopback_addresses_are_refused_in_production_mode(url):
    with pytest.raises(UnsafeTargetError):
        validate_target_url(url, allow_private=False)


def test_a_public_address_is_accepted_in_production_mode():
    url = "https://93.184.216.34/hook"
    assert validate_target_url(url, allow_private=False) == url
