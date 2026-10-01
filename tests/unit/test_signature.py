from reviewer.github.signature import verify_signature
from tests.helpers import sign

BODY = b'{"action": "opened"}'


def test_valid_signature():
    assert verify_signature("s3cret", BODY, sign(BODY, "s3cret"))


def test_wrong_secret():
    assert not verify_signature("s3cret", BODY, sign(BODY, "other"))


def test_tampered_body():
    assert not verify_signature("s3cret", BODY + b" ", sign(BODY, "s3cret"))


def test_missing_or_malformed_header():
    assert not verify_signature("s3cret", BODY, None)
    assert not verify_signature("s3cret", BODY, "")
    assert not verify_signature("s3cret", BODY, "sha1=abc")
    assert not verify_signature("s3cret", BODY, sign(BODY, "s3cret").removeprefix("sha256="))


def test_non_ascii_header_does_not_raise():
    assert not verify_signature("s3cret", BODY, "sha256=ü")
