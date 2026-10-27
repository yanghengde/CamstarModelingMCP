import os

from agent import memory


def setup_store(monkeypatch, tmp_path):
    monkeypatch.setattr(memory, "SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(memory, "MEMORY_FILE", str(tmp_path / "legacy.json"))
    monkeypatch.setattr(memory, "user_memories", {})


def test_restart_preserves_new_messages_active_session_and_imported_reference(monkeypatch, tmp_path):
    setup_store(monkeypatch, tmp_path)
    original_reference = memory.user_memories
    old = memory.create_session("test")
    new = memory.create_session("test")
    messages = memory.get_user_messages("test", new)
    messages.extend([{"role": "user", "content": "新问题"}, {"role": "assistant", "content": "新回答"}])
    memory.save_user_session("test", new)
    path = tmp_path / "sessions" / "test" / f"{new}.json"
    before = path.stat().st_mtime_ns
    memory.init_memory()
    assert memory.user_memories is original_reference
    assert memory.get_active_session("test") == new
    assert memory.get_user_messages("test", new)[-1]["content"] == "新回答"
    assert path.stat().st_mtime_ns == before
    assert {s["id"] for s in memory.get_sessions("test")} == {old, new}


def test_recent_order_is_by_content_update_not_filename_or_history_read(monkeypatch, tmp_path):
    setup_store(monkeypatch, tmp_path)
    clock = iter([10, 20, 30])
    monkeypatch.setattr(memory.time, "time", lambda: next(clock))
    old = memory.create_session("test")
    new = memory.create_session("test")
    memory.get_user_messages("test", old).append({"role": "user", "content": "继续旧对话"})
    memory.save_user_session("test", old)
    memory.init_memory()
    memory.set_active_session("test", new)
    memory.get_user_messages("test", new)
    assert [s["id"] for s in memory.get_sessions("test")] == [new, old]


def test_legacy_session_uses_file_time_without_rewriting(monkeypatch, tmp_path):
    import json
    setup_store(monkeypatch, tmp_path)
    directory = tmp_path / "sessions" / "test"
    directory.mkdir(parents=True)
    path = directory / "legacy.json"
    path.write_text(json.dumps({"id": "legacy", "messages": []}), encoding="utf-8")
    os.utime(path, (1000, 1000))
    memory.init_memory()
    assert memory.get_sessions("test")[0]["updated_at"] == 1000
    assert path.stat().st_mtime == 1000
