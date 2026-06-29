import pytest
from pydantic import ValidationError
from courier.models import RegisterAgent, SendMessage


def test_register_requires_name_and_address():
    m = RegisterAgent(name="Claude", address="claude")
    assert m.address == "claude"
    with pytest.raises(ValidationError):
        RegisterAgent(name="x")  # missing address


def test_send_message_defaults():
    m = SendMessage(to="cursor", body="hi")
    assert m.content_type == "text/plain"
    assert m.subject is None
    assert m.in_reply_to is None
