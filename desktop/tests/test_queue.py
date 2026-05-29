import pytest

from pet_queue import QueueDB


@pytest.fixture
def db(tmp_path):
    q = QueueDB(tmp_path / "q.sqlite", tmp_path / "frames")
    yield q
    q.close()


def test_queue_and_get_with_frame(db, tmp_path):
    qid = db.queue_question(
        "object_identification", "what is that?", "a plant", "low confidence",
        pose={"mode": "idle"}, excerpt=[("user", "what is that?")], frame_jpeg=b"\xff\xd8jpeg",
    )
    assert qid == 1
    assert db.count_pending() == 1
    rec = db.get_question(qid)
    assert rec["category"] == "object_identification"
    assert rec["frame_path"] == "1.jpg"
    assert (tmp_path / "frames" / "1.jpg").read_bytes() == b"\xff\xd8jpeg"
    assert rec["pose"] == {"mode": "idle"}
    assert rec["excerpt"] == [["user", "what is that?"]]


def test_resolve_shares_and_returns_fact(db):
    qid = db.queue_question("reasoning", "why?", "because", "too many steps")
    fact = db.resolve_question(qid, "the real answer", share_with_robot=True)
    assert fact is not None
    assert fact.resolution == "the real answer"
    assert fact.category == "reasoning"
    assert db.count_pending() == 0
    assert db.get_question(qid)["status"] == "resolved"


def test_resolve_unshared_returns_none(db):
    qid = db.queue_question("opinion", "thoughts?", "dunno", "judgment")
    assert db.resolve_question(qid, "answer", share_with_robot=False) is None
    assert db.load_recent_resolutions() == []  # nothing learned


def test_resolve_missing_returns_none(db):
    assert db.resolve_question(999, "x", True) is None


def test_dismiss(db):
    qid = db.queue_question("novelty", "huh?", "guess", "unfamiliar")
    assert db.dismiss_question(qid, "irrelevant") is True
    assert db.dismiss_question(999, "nope") is False
    assert db.count_pending() == 0


def test_recent_resolutions_order_oldest_first(db):
    for i in range(3):
        qid = db.queue_question("reasoning", f"q{i}", "g", "w")
        db.resolve_question(qid, f"answer{i}", True)
    facts = db.load_recent_resolutions()
    assert [f.resolution for f in facts] == ["answer0", "answer1", "answer2"]


def test_summary(db):
    assert "No pending" in db.summarize_queue()
    db.queue_question("reasoning", "q", "g", "w")
    db.queue_question("reasoning", "q2", "g", "w")
    db.queue_question("opinion", "q3", "g", "w")
    s = db.summarize_queue()
    assert "3 pending" in s and "2 reasoning" in s
