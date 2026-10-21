"""Synthetic history rewrites; no client-specific compaction recognition or markers."""

import copy
import json

import pytest
from conftest import conversation, settled
from test_branch_switches import identity, send

SYSTEM = {"role": "system", "content": "synthetic system instructions"}
FIRST = {"role": "user", "content": "synthetic original task"}
RECENT = [
    {"role": "user", "content": "synthetic recent question"},
    {
        "role": "assistant",
        "content": None,
        "reasoning_content": "synthetic reasoning",
        "tool_calls": [
            {
                "id": "synthetic-call",
                "type": "function",
                "function": {"name": "synthetic-tool", "arguments": "{}"},
            }
        ],
    },
    {"role": "tool", "tool_call_id": "synthetic-call", "content": "synthetic retained result"},
    {
        "role": "user",
        "content": [
            {"type": "text", "text": "synthetic follow-up"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,c3ludGhldGlj"}},
        ],
    },
]


def original():
    return {
        "messages": copy.deepcopy(
            [
                SYSTEM,
                FIRST,
                {"role": "assistant", "content": "synthetic original answer"},
                {"role": "user", "content": "synthetic old history to summarize"},
                {"role": "assistant", "content": "synthetic old answer"},
                *RECENT,
            ]
        )
    }


def compacted(revision=1, keep_first=False, stream=False):
    # Some generic rewrites retain an initial user instruction too; standard summary-first
    # compaction does not. Test both, rather than infer a particular client's payload.
    prefix = [SYSTEM, FIRST] if keep_first else [SYSTEM]
    return {
        "messages": copy.deepcopy(
            [*prefix, {"role": "user", "content": f"synthetic summary revision {revision}"}, *RECENT]
        ),
        "stream": stream,
    }


def extend(payload, label="next"):
    result = copy.deepcopy(payload)
    result["messages"] += [
        {"role": "assistant", "content": "synthetic"},
        {"role": "user", "content": f"synthetic {label}"},
    ]
    return result


@pytest.mark.parametrize("keep_first", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_long_history_compacts_then_grows_without_obsolete_restores(
    harness, keep_first, stream, monkeypatch
):
    mock, client, make, _ = harness
    url, manager, config = await make()
    mock.context = 262144
    assert config.snapshots.save_threshold_percent == 30
    assert config.snapshots.max_snapshots == 5
    long = original()
    await send(client, url, manager, mock, long, tokens=230000)
    await send(client, url, manager, mock, conversation("different"), tokens=500)
    saved_original = manager.store.rows()[0]["name"]
    growing = extend(long)
    await send(client, url, manager, mock, growing, tokens=234000)
    assert mock.snapshot_actions[-1] == ("restore", saved_original)
    restores = mock.events.count("restore")

    short = compacted(keep_first=keep_first, stream=stream)
    assert short["messages"][-len(RECENT) :] == long["messages"][-len(RECENT) :]
    assert identity(short).overlap(identity(growing)) == (2 if keep_first else 0)
    previous_key = manager.active.key
    await send(client, url, manager, mock, short, tokens=20000)
    assert manager.active.key != previous_key
    assert len(manager.active.chain) == len(short["messages"])
    assert manager.active.roles == tuple(m["role"] for m in short["messages"])
    assert mock.events.count("save") == 2  # preserve outgoing 234K, not the short summary
    assert mock.events.count("restore") == restores
    assert mock.prompt == 20000
    context = json.loads(manager.store.meta("context"))
    assert context["prompt_tokens_lower_bound"] == 20000

    def unexpected_io(*args, **kwargs):
        raise AssertionError("ordinary compacted continuation must not read or write snapshots")

    monkeypatch.setattr(manager.store, "file_info", unexpected_io)
    monkeypatch.setattr(manager.backend, "action", unexpected_io)
    for n in range(4):
        short = extend(short, f"turn {n}")
        await send(client, url, manager, mock, short, tokens=21000 + n * 1000)
    # A further compaction changes the summary and message placement again.
    short = compacted(revision=2, keep_first=keep_first, stream=stream)
    await send(client, url, manager, mock, short, tokens=12000)
    await send(client, url, manager, mock, short, tokens=12000)  # identical retry, no obsolete restore
    await send(client, url, manager, mock, extend(short), tokens=13000)
    assert mock.events.count("save") == 2
    assert mock.events.count("restore") == restores
    assert len(manager.store.rows()) == 2


def test_chain_never_matches_moved_suffix_or_changed_summary():
    long, short = identity(original()), identity(compacted())
    assert not short.overlap(long)
    assert not short.reusable_prefix(long)
    assert short.chain[-1] != long.chain[-1]
    v2 = identity(compacted(revision=2))
    assert not v2.overlap(short)
    assert not v2.reusable_prefix(short)
    # Same user-bearing prefix is a relationship, NOT proof of a saved checkpoint.
    weak = identity(compacted(keep_first=True))
    assert weak.overlap(long) == 2
    assert not weak.reusable_prefix(long)
    assert not long.reusable_prefix(weak)


async def test_actual_full_prefix_reuse_including_small_context_and_older_branch(harness):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    # A genuinely short saved input, not a global minimum-overlap threshold.
    root = conversation("synthetic short root")
    await send(client, url, manager, mock, root, tokens=300)
    await send(client, url, manager, mock, conversation("other"), tokens=400)
    root_file = manager.store.rows()[0]["name"]
    a = extend(root, "branch A")
    await send(client, url, manager, mock, a)
    assert mock.snapshot_actions[-1] == ("restore", root_file)
    await send(client, url, manager, mock, conversation("another"))
    saved_a = manager.store.rows()[0]["name"]
    # Diverge AFTER the whole older root input, BEFORE A's deeper input checkpoint.
    b = extend(root, "branch B")
    assert identity(b).reusable_prefix(identity(root)) == 2
    assert identity(b).reusable_prefix(identity(a)) == 0
    await send(client, url, manager, mock, b)
    assert mock.snapshot_actions[-1] == ("restore", root_file)
    await send(client, url, manager, mock, extend(a))
    assert mock.snapshot_actions[-1] == ("restore", saved_a)


async def test_repeated_zero_reuse_is_suppressed_before_hdd_read_but_not_permanently(harness, monkeypatch):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    mock.context = 262144
    await send(client, url, manager, mock, original(), tokens=230000)
    await send(client, url, manager, mock, conversation("other"), tokens=500)
    name = manager.store.rows()[0]["name"]
    mock.cache_override = 0
    reads = []
    real_read = manager.store.file_info

    def counted(filename):
        reads.append(filename)
        return real_read(filename)

    monkeypatch.setattr(manager.store, "file_info", counted)
    # The preserved request prefix nominates a file but tokenization/engine state may
    # still reject reuse. Use below-threshold prompts so repeats don't create new files.
    request = extend(original())
    await send(client, url, manager, mock, request, tokens=20000)
    assert reads == [name]
    assert manager.store.report(manager.config.estimates)["counters"]["restore_without_reported_hit"] == 1
    first_deadlines = dict(manager.restore_misses)
    for _ in range(3):
        await send(client, url, manager, mock, conversation("other"), tokens=500)
        await send(client, url, manager, mock, request, tokens=20000)
    assert reads == [name]
    assert mock.events.count("restore") == 1
    assert len(manager.restore_misses) == 1
    assert dict(manager.restore_misses) == first_deadlines  # skips do not extend suppression
    assert manager.store.report(manager.config.estimates)["counters"]["restore_suppressed"] == 3
    # A changed incoming history is a different pair, even with the same snapshot.
    await send(client, url, manager, mock, conversation("other"), tokens=500)
    await send(client, url, manager, mock, extend(request), tokens=21000)
    assert reads == [name, name]
    # Expire misses without sleeps; original history is eligible again.
    for key in manager.restore_misses:
        manager.restore_misses[key] = 0
    await send(client, url, manager, mock, conversation("other"), tokens=500)
    mock.cache_override = 12000
    await send(client, url, manager, mock, request, tokens=20000)
    assert reads == [name, name, name]
    assert mock.events.count("restore") == 3


@pytest.mark.parametrize(
    "telemetry",
    [
        None,
        {},
        {"cache_n": 0},
        {"cache_n": None, "prompt_n": 20000},
        {"cache_n": -1, "prompt_n": 20001},
        {"cache_n": True, "prompt_n": 19999},
        {"cache_n": 230000, "prompt_n": 0},
        {"cache_n": 0, "prompt_n": 230000},
    ],
)
async def test_unknown_ambiguous_or_stale_reuse_telemetry_is_not_a_confirmed_miss(harness, telemetry):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    mock.context = 262144
    await send(client, url, manager, mock, original(), tokens=230000)
    await send(client, url, manager, mock, conversation("other"), tokens=500)
    mock.timings_override = telemetry
    for _ in range(2):
        await send(client, url, manager, mock, extend(original()), tokens=20000)
        await send(client, url, manager, mock, conversation("other"), tokens=500)
    assert mock.events.count("restore") == 2
    assert not manager.restore_misses
    counts = manager.store.report(manager.config.estimates)["counters"]
    assert counts["restore_reuse_unknown"] == 2
    assert not counts.get("restore_without_reported_hit")
    assert not counts.get("cache_hits")


async def test_active_replay_preserves_but_does_not_restore_older_copy(harness):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    mock.context = 262144
    long = original()
    await send(client, url, manager, mock, long, tokens=230000)
    await send(client, url, manager, mock, conversation("other"), tokens=500)
    await send(client, url, manager, mock, long, tokens=230000)
    mock.cache_override = 0
    restores = mock.events.count("restore")
    for _ in range(3):
        await send(client, url, manager, mock, long, tokens=230000)
    assert mock.events.count("restore") == restores
    assert mock.events.count("save") == 4  # retain branch fix: preserve regenerated tails
    assert len(manager.store.rows()) == 1


async def test_negative_cache_bound_and_scope(harness):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    mock.context = 262144
    for n in range(manager.MAX_RESTORE_MISSES + 20):
        manager.remember_miss(("synthetic", n))
    assert len(manager.restore_misses) == manager.MAX_RESTORE_MISSES
    assert ("synthetic", 0) not in manager.restore_misses
    await send(client, url, manager, mock, original(), tokens=230000)
    await send(client, url, manager, mock, conversation("other"), tokens=500)
    mock.cache_override = 0
    await send(client, url, manager, mock, extend(original()), tokens=20000)
    obs = manager.observed
    row = manager.store.rows()[0]
    key = manager.restore_key(row, manager.active, obs)
    assert manager.suppressed(key)
    new = dict(row)
    new["sha256"] = "synthetic-new-file-digest"
    assert not manager.suppressed(manager.restore_key(new, manager.active, obs))
    new["compatibility"] = "synthetic-new-config"
    assert not manager.suppressed(manager.restore_key(new, manager.active, obs))
    from dataclasses import replace

    obs = replace(obs, boot=(200, *obs.boot[1:]))
    assert not manager.suppressed(manager.restore_key(row, manager.active, obs))


async def test_small_nonzero_reuse_is_not_arbitrarily_suppressed(harness):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    mock.context = 262144
    await send(client, url, manager, mock, original(), tokens=230000)
    await send(client, url, manager, mock, conversation("other"), tokens=500)
    mock.cache_override = 1
    for _ in range(2):
        await send(client, url, manager, mock, extend(original()), tokens=20000)
        await send(client, url, manager, mock, conversation("other"), tokens=500)
    assert mock.events.count("restore") == 2
    assert not manager.restore_misses
    counts = manager.store.report(manager.config.estimates)["counters"]
    assert counts["cache_hits"] == 2  # reported reuse, NOT claimed useful net disk benefit
    assert counts["estimated_prompt_tokens_spared"] == 2


async def test_after_explicit_mock_restore_short_prompt_usage_is_not_old_file_occupancy(harness):
    mock, client, make, backend = harness
    url, manager, _ = await make()
    mock.context = 262144
    await send(client, url, manager, mock, original(), tokens=230000)
    await send(client, url, manager, mock, conversation("other"), tokens=500)
    name = manager.store.rows()[0]["name"]
    # Mock-only manual operation, not a live probe. It demonstrates the accounting
    # contract the source review found, not numerical qualification of the engine.
    async with client.post(backend + "/slots/0?action=restore", json={"filename": name}) as r:
        assert (await r.json())["n_restored"] == 230005
    assert mock.held["tokens"] == 230005 and mock.prompt == 0
    mock.next_tokens = 18000
    mock.include_usage = True
    short = compacted()
    raw = json.dumps(short, indent=3).encode()
    async with client.post(
        url + "/v1/chat/completions", data=raw, headers={"Content-Type": "application/json"}
    ) as r:
        body = await r.read()
        assert r.status == 200
    await settled(manager)
    usage = json.loads(body)["usage"]
    assert usage == {
        "prompt_tokens": 18000,
        "completion_tokens": 5,
        "total_tokens": 18005,
        "prompt_tokens_details": {"cached_tokens": 0},
    }
    assert mock.held["tokens"] == 18005 and mock.prompt == 18000
    assert mock.received[-1][3] == raw
    async with client.get(url + "/slots") as r:
        assert (await r.json())[0]["n_prompt_tokens"] == 18000
    # Reset to the same mock initial state for a fair direct/proxy comparison.
    async with client.post(backend + "/slots/0?action=restore", json={"filename": name}) as r:
        assert r.status == 200
        await r.read()
    # Direct/proxied response bytes agree; no context-limit/usage rewriting.
    async with client.post(backend + "/v1/chat/completions", data=raw) as r:
        assert await r.read() == body


async def test_suppressed_deeper_candidate_can_fall_back_to_known_shorter_checkpoint(harness):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    root = conversation("synthetic root")
    await send(client, url, manager, mock, root, tokens=300)
    await send(client, url, manager, mock, conversation("other"), tokens=100)
    root_file = manager.store.rows()[0]["name"]
    deep = extend(root)
    await send(client, url, manager, mock, deep, tokens=400)
    await send(client, url, manager, mock, conversation("other"), tokens=100)
    deep_file = manager.store.rows()[0]["name"]
    incoming = extend(deep)
    mock.cache_override = 0
    await send(client, url, manager, mock, incoming, tokens=100)
    assert mock.snapshot_actions[-1] == ("restore", deep_file)
    await send(client, url, manager, mock, conversation("other"), tokens=100)
    mock.cache_override = 300
    await send(client, url, manager, mock, incoming, tokens=500)
    assert mock.snapshot_actions[-1] == ("restore", root_file)


async def test_compaction_preservation_and_lru_threshold_remain_correct(harness):
    mock, client, make, _ = harness
    url, manager, _ = await make(max_snapshots=2)
    mock.context = 262144
    await send(client, url, manager, mock, original(), tokens=230000)
    await send(client, url, manager, mock, compacted(), tokens=20000)
    first = manager.store.rows()[0]["name"]
    await send(client, url, manager, mock, compacted(2), tokens=10000)
    assert mock.events.count("save") == 1
    await send(client, url, manager, mock, conversation("other"), tokens=200000)
    await send(client, url, manager, mock, conversation("another"), tokens=210000)
    await send(client, url, manager, mock, conversation("last"), tokens=1000)
    assert len(manager.store.rows()) == 2
    assert not (mock.directory / first).exists()
    assert manager.store.report(manager.config.estimates)["counters"]["evictions"] == 1
