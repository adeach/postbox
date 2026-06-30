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


def test_observer_models():
    from postbox.models import ThreadSummary, SendAs, CreateIdentity, MessageView
    s = ThreadSummary(thread_id="t1", subject="hi", members=["a", "b"],
                      last={"from": "a", "text": "yo", "at": "t"},
                      message_count=2, unread={"b": 1})
    assert s.members == ["a", "b"] and s.unread["b"] == 1
    assert SendAs(**{"from": "a", "to": "b", "body": "x"}).from_ == "a"
    assert CreateIdentity(name="adam").name == "adam"
    m = MessageView(id="m1", from_="a", to=["b"], subject=None, body="x",
                    content_type="text/plain", created_at="t", read_by=[])
    assert m.from_ == "a"
