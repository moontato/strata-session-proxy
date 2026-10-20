import json
from pathlib import Path

import pytest
from conftest import conversation

from strata_session_proxy.config import Config, load
from strata_session_proxy.identity import identify
from strata_session_proxy.storage import StorageError, Store


def ident(data, **headers):
    return identify(
        "/v1/chat/completions", json.dumps(data).encode(), {"Content-Type": "application/json", **headers}
    )


def test_exact_prefix_and_branches():
    a = ident(conversation())
    continued = conversation(continuation=True)
    b = ident(continued)
    assert a.overlap(b) == 2
    continued["messages"][-1]["content"] = "synthetic branch"
    c = ident(continued)
    assert b.overlap(c) == 3
    assert not a.overlap(ident(conversation("different")))
    assert not a.overlap(ident({"messages": [{"role": "system", "content": "synthetic shared system"}]}))


def test_ordered_content_roles_tools_and_prompt_options():
    a = conversation()
    assert ident(a) == ident({"temperature": 0.9, "max_tokens": 10, **a})
    assert not ident(a).overlap(
        ident({**a, "tools": [{"type": "function", "function": {"name": "synthetic"}}]})
    )
    assert not ident(a).overlap(ident({**a, "reasoning_effort": "low"}))
    assert not ident(a).overlap(ident({**a, "future_prompt_option": True}))
    assert not ident(a).overlap(ident(a, Authorization="Bearer synthetic-other-tenant"))
    x = conversation(continuation=True)
    x["messages"][2]["tool_calls"] = [{"id": "synthetic-a"}]
    y = json.loads(json.dumps(x))
    y["messages"][2]["tool_calls"][0]["id"] = "synthetic-b"
    # Earlier input can still nominate a checkpoint; full-history identities differ.
    assert ident(x).key != ident(y).key


def test_multimodal_mutable_reference_falls_back():
    data = conversation()
    data["messages"][-1]["content"] = [
        {"type": "image_url", "image_url": {"url": "https://example.invalid/synthetic.png"}}
    ]
    assert ident(data) is None
    data["messages"][-1]["content"][0]["image_url"]["url"] = "data:image/png;base64,c3ludGhldGlj"
    assert ident(data) is not None
    assert identify("/v1/responses", b"{}", {"Content-Type": "application/json"}) is None
    assert (
        identify(
            "/v1/chat/completions", b'{"messages":[],"messages":[]}', {"Content-Type": "application/json"}
        )
        is None
    )
    assert ident({**conversation(), "strata_checkpoint": False}) is None


@pytest.mark.parametrize(
    "key,value",
    [
        ("max_snapshots", 0),
        ("save_threshold_percent", -1),
        ("save_threshold_percent", 101),
        ("min_free_disk_gib", float("nan")),
        ("eviction_policy", "random"),
        ("enabled", "yes"),
    ],
)
def test_invalid_config(key, value):
    config = Config()
    setattr(config.snapshots, key, value)
    with pytest.raises(ValueError):
        config.validate()


def test_generic_config_env_and_unknown_keys(tmp_path, monkeypatch):
    config = tmp_path / "generic.local.yaml"
    config.write_text("server:\n  listen_port: 8084\n")
    monkeypatch.setenv("STRATA_PROXY__SERVER__LISTEN_PORT", "8085")
    assert load(str(config)).server.listen_port == 8085
    config.write_text("snapshots:\n  typo: 1\n")
    with pytest.raises(ValueError):
        load(str(config))


def store_config(tmp_path):
    c = Config().snapshots
    c.directory = str(tmp_path / "snapshots")
    c.state_directory = str(tmp_path / "state")
    c.min_free_disk_gib = 0
    Path(c.directory).mkdir(mode=0o700)
    return c


def add(store, identity):
    name = store.begin(identity, "synthetic-compatibility")
    path = store.path(name)
    path.write_bytes(b"synthetic snapshot")
    path.chmod(0o600)
    size, sha = store.file_info(name)
    store.commit(name, 400, size, sha)
    return name


def test_restart_reconcile_missing_and_unrelated_files(tmp_path):
    config = store_config(tmp_path)
    store = Store(config)
    name = add(store, ident(conversation()))
    other = store.directory / "unrelated.txt"
    other.write_text("synthetic unrelated")
    store.path(name).unlink()
    store.close()
    store = Store(config)
    assert store.rows() == []
    assert other.read_text() == "synthetic unrelated"
    store.close()


def test_interrupted_save_persists_block_and_preserves_file(tmp_path):
    config = store_config(tmp_path)
    store = Store(config)
    name = store.begin(ident(conversation()), "synthetic")
    store.path(name).write_bytes(b"partial synthetic")
    store.close()
    store = Store(config)
    assert store.meta("blocked") == "interrupted_snapshot_operation"
    assert store.path(name).exists()
    store.close()


def test_single_process_lock(tmp_path):
    config = store_config(tmp_path)
    store = Store(config)
    try:
        with pytest.raises(StorageError):
            Store(config)
    finally:
        store.close()


def test_symlinks_and_mount_required(tmp_path):
    config = store_config(tmp_path)
    config.required_mount = str(tmp_path)
    with pytest.raises(StorageError):
        Store(config)
    config.required_mount = ""
    store = Store(config)
    name = store.begin(ident(conversation()), "synthetic")
    store.path(name).symlink_to(tmp_path / "not-a-snapshot")
    with pytest.raises(OSError):
        store.file_info(name)
    with pytest.raises(StorageError):
        store.remove(name)
    store.close()


def test_committed_replacement_survives_delete_failure(tmp_path, monkeypatch):
    config = store_config(tmp_path)
    config.max_snapshots = 1
    store = Store(config)
    old = add(store, ident(conversation("alpha")))
    original_remove = store.remove

    def fail_remove(name):
        raise OSError("synthetic filesystem failure")

    monkeypatch.setattr(store, "remove", fail_remove)
    with pytest.raises(OSError):
        add(store, ident(conversation("beta")))
    new = store.rows()[0]["name"]
    assert new != old and store.path(new).exists() and store.path(old).exists()
    monkeypatch.setattr(store, "remove", original_remove)
    store.close()
    store = Store(config)
    assert [r["name"] for r in store.rows()] == [new]
    assert not store.path(old).exists()
    store.close()


def test_lowered_retention_limit_reconciles_on_restart(tmp_path):
    config = store_config(tmp_path)
    store = Store(config)
    first = add(store, ident(conversation("alpha")))
    second = add(store, ident(conversation("beta")))
    store.close()
    config.max_snapshots = 1
    store = Store(config)
    assert [row["name"] for row in store.rows()] == [second]
    assert not store.path(first).exists()
    store.close()


def test_snapshot_directory_has_a_cross_catalog_lock(tmp_path):
    config = store_config(tmp_path)
    store = Store(config)
    config.state_directory = str(tmp_path / "another-state")
    try:
        with pytest.raises(StorageError):
            Store(config)
    finally:
        store.close()


def test_lru_touch_changes_eviction_and_retention_survives_restart(tmp_path):
    config = store_config(tmp_path)
    config.max_snapshots = 2
    store = Store(config)
    a = add(store, ident(conversation("alpha")))
    b = add(store, ident(conversation("beta")))
    store.touch(a)
    c = add(store, ident(conversation("gamma")))
    assert {r["name"] for r in store.rows()} == {a, c}
    assert not store.path(b).exists()
    store.close()
    store = Store(config)
    assert len(store.rows()) == 2
    assert store.candidate(ident(conversation("alpha", True)), "synthetic-compatibility")["name"] == a
    store.close()
