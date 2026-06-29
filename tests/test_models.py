import pytest
from postbox.models import RegisterAgent, SendMessage


def test_register_with_name_and_address():
    m = RegisterAgent(name="Claude", address="claude")
    assert m.address == "claude"


def test_send_message_defaults():
    m = SendMessage(to="cursor", body="hi")
    assert m.content_type == "text/plain"
    assert m.subject is None
    assert m.in_reply_to is None


def test_wakeup_model_and_register_defaults():
    from postbox.models import RegisterAgent, Wakeup
    m = RegisterAgent(wakeup=Wakeup(kind="tmux", target="%5"))
    assert m.name is None                 # name optional in v2 (server defaults it)
    assert m.wakeup.kind == "tmux" and m.wakeup.target == "%5"
    m2 = RegisterAgent()
    assert m2.wakeup.kind == "none"       # default wakeup


def test_set_name_model():
    from postbox.models import SetName
    assert SetName(name="alice").name == "alice"
