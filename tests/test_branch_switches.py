"""Regression: sharing an earlier input prefix must not discard a branch's later KV."""

import json

import pytest
from conftest import conversation, settled

from strata_session_proxy.identity import Identity, identify


def branch(name):
    return {
        "messages": [
            {"role": "system", "content": "synthetic shared system"},
            {"role": "user", "content": "synthetic shared turn one"},
            {"role": "assistant", "content": "synthetic shared answer one"},
            {"role": "user", "content": "synthetic shared turn two"},
            {"role": "assistant", "content": "synthetic shared answer two"},
            {"role": "user", "content": f"synthetic unique branch {name}"},
        ]
    }


def identity(body):
    return identify("/v1/chat/completions", json.dumps(body).encode(), {"Content-Type": "application/json"})


async def send(client, url, manager, mock, payload, tokens=400):
    # Counts are reported by the synthetic backend, not inferred from fixture text length.
    mock.next_tokens = tokens
    raw = json.dumps(payload, indent=3, ensure_ascii=False).encode()
    async with client.post(
        url + "/v1/chat/completions", data=raw, headers={"Content-Type": "application/json"}
    ) as response:
        assert response.status == 200, await response.read()
        await response.read()
    await settled(manager)
    assert mock.received[-1][3] == raw


@pytest.mark.parametrize("stream", [False, True])
async def test_long_sibling_switch_preserves_unique_state_and_restores_exact_branch(harness, stream):
    mock, client, make, _ = harness
    url, manager, config = await make()
    assert config.snapshots.save_threshold_percent == 30
    assert config.snapshots.max_snapshots == 5
    mock.context = 262144
    a, b = branch("A"), branch("B")
    a["stream"] = b["stream"] = stream
    # Synthetic accounting: common prefix 180K, A adds 20K, B adds a different 15K.
    await send(client, url, manager, mock, a, tokens=180000 + 20000)
    await send(client, url, manager, mock, b, tokens=180000 + 15000)
    assert mock.events == ["inference", "save", "inference"]
    saved_a = manager.store.rows()[0]
    data = json.loads((mock.directory / saved_a["name"]).read_bytes())
    assert data["held"]["messages"] == a["messages"]
    assert saved_a["tokens"] == 200005  # includes mock consumed assistant tokens

    await send(client, url, manager, mock, a, tokens=200000)
    assert mock.events == ["inference", "save", "inference", "save", "restore", "inference"]
    assert mock.snapshot_actions[-1] == ("restore", saved_a["name"])
    # The preserved unique A input, not merely the shared prefix or B, produced the cache hit.
    assert mock.cached == 200000
    continued = {
        **a,
        "messages": a["messages"]
        + [
            {"role": "assistant", "content": "synthetic"},
            {"role": "user", "content": "synthetic follow-up on A"},
        ],
        "temperature": 0.2,
    }
    actions = list(mock.snapshot_actions)
    await send(client, url, manager, mock, continued, tokens=201000)
    assert mock.snapshot_actions == actions


async def test_multiple_siblings_choose_longest_candidate_not_recent_sibling(harness):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    await send(client, url, manager, mock, branch("A"))
    await send(client, url, manager, mock, branch("B"))
    saved_a = manager.store.rows()[0]["name"]
    await send(client, url, manager, mock, branch("C"))
    saved_b = next(r["name"] for r in manager.store.rows() if r["name"] != saved_a)
    # A can help prefill C's shared prefix, but neither overlap establishes continuation.
    await send(client, url, manager, mock, branch("A"))
    assert mock.snapshot_actions[-1] == ("restore", saved_a)
    await send(client, url, manager, mock, branch("B"))
    assert mock.snapshot_actions[-1] == ("restore", saved_b)
    assert mock.events.count("save") == 4


@pytest.mark.parametrize(
    "shared",
    [
        [],
        [{"role": "user", "content": "synthetic shared question"}],
        [
            {"role": "user", "content": "synthetic shared question"},
            {"role": "assistant", "content": "synthetic shared answer"},
        ],
    ],
)
async def test_short_prefixes_and_system_only_are_switches(harness, shared):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    base = [{"role": "system", "content": "synthetic shared system"}, *shared]
    a = {"messages": base + [{"role": "user", "content": "synthetic A"}]}
    b = {"messages": base + [{"role": "user", "content": "synthetic B"}]}
    await send(client, url, manager, mock, a)
    await send(client, url, manager, mock, b)
    assert mock.events == ["inference", "save", "inference"]
    assert not identity(b).continues(identity(a))
    assert bool(identity(a).overlap(identity(b))) == bool(shared)


def test_continuation_is_directional_and_request_boundary_aware():
    a, b = identity(branch("A")), identity(branch("B"))
    assert a.overlap(b) == 5
    assert not a.continues(b) and not b.continues(a)
    followup = branch("A")
    followup["messages"] += [
        {"role": "assistant", "content": "synthetic response"},
        {"role": "user", "content": "synthetic next question"},
    ]
    c = identity(followup)
    assert c.continues(a)
    assert not a.continues(c)  # rewind
    assert not a.continues(a)  # replay/regeneration may replace generated assistant state
    assert not c.continues(Identity.unpack(a.packed()))  # stored candidates are not active observations
    followup["messages"][-1] = {"role": "assistant", "content": "synthetic partial prefill"}
    assert not identity(followup).continues(a)


@pytest.mark.parametrize(
    "kind",
    ["replay", "rewind", "no-response-boundary", "assistant-prefill", "invalid-role", "system", "developer"],
)
async def test_ambiguous_or_nonextending_history_preserves_outgoing(harness, kind):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    a = branch("A")
    await send(client, url, manager, mock, a)
    incoming = branch("A")
    if kind == "rewind":
        incoming["messages"] = incoming["messages"][:2]
    elif kind == "no-response-boundary":
        incoming["messages"].append({"role": "user", "content": "synthetic new input"})
    elif kind == "assistant-prefill":
        incoming["messages"].append({"role": "assistant", "content": "synthetic partial response"})
    elif kind == "invalid-role":
        incoming["messages"].append({"role": "unknown-synthetic-role", "content": "synthetic"})
    elif kind in {"system", "developer"}:
        incoming["messages"] += [
            {"role": "assistant", "content": "synthetic"},
            {"role": kind, "content": "synthetic new instruction"},
            {"role": "user", "content": "synthetic follow-up"},
        ]
    await send(client, url, manager, mock, incoming)
    assert mock.events[:3] == ["inference", "save", "inference"]
    assert len(manager.store.rows()) == 1


async def test_snapshot_digest_reads_remain_once_per_save_and_before_each_restore(harness, monkeypatch):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    reads = []
    original = manager.store.file_info

    def counted(name):
        reads.append(name)
        return original(name)

    monkeypatch.setattr(manager.store, "file_info", counted)
    await send(client, url, manager, mock, branch("A"))
    await send(client, url, manager, mock, branch("B"))
    saved_a = manager.store.rows()[0]["name"]
    assert reads == [saved_a]
    await send(client, url, manager, mock, branch("A"))
    saved_b = next(r["name"] for r in manager.store.rows() if r["name"] != saved_a)
    assert reads == [saved_a, saved_b, saved_a]
    continued = branch("A")
    continued["messages"] += [
        {"role": "assistant", "content": "synthetic"},
        {"role": "user", "content": "synthetic continuation"},
    ]
    await send(client, url, manager, mock, continued)
    assert reads == [saved_a, saved_b, saved_a]


async def test_normal_tool_turn_extension_keeps_active_slot(harness):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    a = conversation()
    await send(client, url, manager, mock, a)
    a["messages"] += [
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "synthetic reasoning",
            "tool_calls": [
                {"id": "synthetic-call", "function": {"name": "synthetic-tool", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "synthetic-call", "content": "synthetic result"},
    ]
    await send(client, url, manager, mock, a)
    assert mock.events == ["inference", "inference"]
