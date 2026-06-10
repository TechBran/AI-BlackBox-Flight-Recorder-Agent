import inspect
from Orchestrator.sms.ami_client import AMISMSClient


def test_no_hardcoded_secret_in_source():
    src = inspect.getsource(AMISMSClient.__init__)
    assert "6157Ego8" not in src, "hardcoded AMI secret must be removed"
    assert "192.168.1.200" not in src, "hardcoded default host must be removed"


def test_defaults_are_empty():
    c = AMISMSClient()
    assert c.host == ""
    assert c.secret == ""
    assert c.username == ""
    assert c.port == 5038


def test_explicit_creds_stored():
    c = AMISMSClient(host="10.0.0.9", username="u", secret="s", port=5038)
    assert c.host == "10.0.0.9" and c.username == "u" and c.secret == "s"
