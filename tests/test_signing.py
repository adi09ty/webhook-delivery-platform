import base64
import hashlib
import hmac

from app import signing

SECRET = "whsec_test_secret"
BODY = b'{"id":"evt_1","type":"payment.completed","data":{"amount":100}}'


def sign(timestamp=1_700_000_000, message_id="evt_1", body=BODY, secret=SECRET):
    return signing.build_signature_headers(secret, message_id, timestamp, body)


def test_signature_matches_the_documented_recipe():
    headers = sign()
    expected = hmac.new(SECRET.encode(), b"evt_1.1700000000." + BODY, hashlib.sha256).digest()

    assert headers["webhook-id"] == "evt_1"
    assert headers["webhook-timestamp"] == "1700000000"
    assert headers["webhook-signature"] == "v1," + base64.b64encode(expected).decode()


def verify(headers, body=BODY, secret=SECRET, now=1_700_000_000, **kwargs):
    return signing.verify_signature(
        secret,
        headers["webhook-id"],
        headers["webhook-timestamp"],
        headers["webhook-signature"],
        body,
        now=now,
        **kwargs,
    )


def test_a_genuine_message_verifies():
    assert verify(sign())


def test_a_changed_body_is_rejected():
    assert not verify(sign(), body=BODY.replace(b"100", b"999"))


def test_the_wrong_secret_is_rejected():
    assert not verify(sign(), secret="whsec_somebody_else")


def test_a_changed_message_id_is_rejected():
    headers = sign()
    headers["webhook-id"] = "evt_2"
    assert not verify(headers)


def test_an_old_message_is_rejected_to_stop_replays():
    assert not verify(sign(), now=1_700_000_000 + 301)
    assert verify(sign(), now=1_700_000_000 + 299)


def test_a_timestamp_from_the_future_is_also_rejected():
    assert not verify(sign(timestamp=1_700_000_000 + 1000))


def test_a_garbage_timestamp_is_rejected_not_crashed_on():
    headers = sign()
    headers["webhook-timestamp"] = "yesterday"
    assert not verify(headers)


def test_several_signatures_are_accepted_if_one_matches():
    """Used while rotating a secret: the sender can include old and new signatures."""
    headers = sign()
    headers["webhook-signature"] = "v1,AAAA " + headers["webhook-signature"]
    assert verify(headers)


def test_a_missing_signature_is_rejected():
    headers = sign()
    headers["webhook-signature"] = ""
    assert not verify(headers)
