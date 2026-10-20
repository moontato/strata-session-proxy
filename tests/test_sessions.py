import asyncio
from unittest.mock import patch

import pytest
from conftest import conversation, infer, settled


async def test_continuation_no_snapshot_writes(harness):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    await infer(client, url)
    await infer(client, url, continuation=True, temperature=0.2)
    await settled(manager)
    assert mock.events == ["inference", "inference"]
    assert manager.store.rows() == []


@pytest.mark.parametrize("tokens,saves", [(200, 0), (299, 0), (300, 1), (400, 1), (900, 1)])
async def test_threshold(harness, tokens, saves):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    mock.next_tokens = tokens
    await infer(client, url)
    await infer(client, url, "beta")
    await settled(manager)
    assert mock.events.count("save") == saves
    assert len(manager.store.rows()) == saves


async def test_switch_restore_and_no_rewrite_of_saved_candidate(harness):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    await infer(client, url)
    await infer(client, url, "beta")
    await infer(client, url, continuation=True)
    await settled(manager)
    assert mock.events == ["inference", "save", "inference", "save", "restore", "inference"]
    report = manager.store.report(manager.config.estimates)
    assert report["counters"]["restores"] == 1
    assert report["counters"]["cache_hits"] == 1
    assert report["estimates_not_measurements"]["prompt_tokens_spared"] == 400
    assert report["estimates_not_measurements"]["electricity_kwh_avoided"] is None


async def test_lru_and_count_limit(harness):
    mock, client, make, _ = harness
    url, manager, _ = await make(max_snapshots=2)
    for name in ["alpha", "beta", "gamma"]:
        await infer(client, url, name)
    await settled(manager)
    alpha = manager.store.rows()[-1]["name"]
    assert len(manager.store.rows()) == 2
    # Switch off gamma to alpha: save gamma evicts alpha unless the incoming candidate is protected.
    await infer(client, url, "delta")
    await settled(manager)
    assert len(manager.store.rows()) == 2
    assert not (mock.directory / alpha).exists()
    assert len(list(mock.directory.glob("ssp-*.bin"))) == 2
    assert manager.store.report(manager.config.estimates)["counters"]["evictions"] == 1


async def test_no_save_when_auto_disabled(harness):
    mock, client, make, _ = harness
    url, manager, _ = await make(auto_save=False)
    await infer(client, url)
    await infer(client, url, "beta")
    await settled(manager)
    assert "save" not in mock.events


async def test_disk_full_skips_save_but_inference_continues(harness):
    mock, client, make, _ = harness
    url, manager, _ = await make(min_free_disk_gib=1)
    await infer(client, url)
    usage = type("Usage", (), {"free": 0})()
    with patch("strata_session_proxy.storage.shutil.disk_usage", return_value=usage):
        await infer(client, url, "beta")
        await settled(manager)
    assert "save" not in mock.events
    assert not manager.store.meta("blocked")


async def test_failed_save_preserves_prior_good(harness):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    await infer(client, url)
    await infer(client, url, "beta")
    await infer(client, url, continuation=True)
    await settled(manager)
    original = {r["name"] for r in manager.store.rows()}
    mock.save_error = (507, {"error": {"kind": "storage"}})
    await infer(client, url, "gamma")
    await settled(manager)
    assert {r["name"] for r in manager.store.rows()} == original
    assert not manager.store.meta("blocked")


async def test_corruption_detected_before_restore(harness):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    await infer(client, url)
    await infer(client, url, "beta")
    await settled(manager)
    row = manager.store.rows()[0]
    p = mock.directory / row["name"]
    p.write_bytes(b"x" * p.stat().st_size)
    await infer(client, url, continuation=True)
    await settled(manager)
    assert "restore" not in mock.events
    assert not p.exists()


async def test_refused_restore_and_fatal_restore(harness):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    await infer(client, url)
    await infer(client, url, "beta")
    mock.restore_error = (400, {"error": {"kind": "invalid"}})
    await infer(client, url, continuation=True)
    await settled(manager)
    assert not manager.store.meta("blocked")
    # beta remains a candidate; fatal transfer is not safe to paper over.
    mock.restore_error = (500, {"error": {"message": "synthetic fatal"}})
    async with client.post(url + "/v1/chat/completions", json=conversation("beta", True)) as r:
        assert r.status == 503
    assert manager.store.meta("blocked") == "uncertain_restore"
    count = mock.events.count("inference")
    async with client.post(url + "/v1/chat/completions", json=conversation("gamma")) as r:
        assert r.status == 503
    assert mock.events.count("inference") == count


async def test_backend_restart_drops_active_but_can_restore_compatible(harness):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    await infer(client, url)
    await infer(client, url, "beta")
    await settled(manager)
    mock.started += 1
    mock.requests = 0
    mock.prompt = 0
    mock.held = None
    await infer(client, url, continuation=True)
    await settled(manager)
    assert mock.events.count("save") == 1  # do not save stale beta under its old identity
    assert mock.events.count("restore") == 1


async def test_incompatible_engine_avoids_restore(harness):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    await infer(client, url)
    await infer(client, url, "beta")
    await settled(manager)
    mock.engine = "synthetic-engine-v2"
    await infer(client, url, continuation=True)
    await settled(manager)
    assert "restore" not in mock.events


async def test_external_inference_invalidates_active_association(harness):
    mock, client, make, backend = harness
    url, manager, _ = await make()
    await infer(client, url)
    await settled(manager)
    await infer(client, backend, "out-of-band-synthetic")
    await infer(client, url, "beta")
    await settled(manager)
    assert "save" not in mock.events
    assert manager.store.report(manager.config.estimates)["counters"]["external_changes"] == 1


async def test_concurrent_switches_slow_hdd_no_overlap(harness):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    mock.delay = 0.04
    mock.inference_delay = 0.02
    await asyncio.gather(*(infer(client, url, name) for name in ["alpha", "beta", "gamma", "alpha"]))
    await settled(manager)
    assert mock.max_busy == 1
    assert len(manager.store.rows()) <= 5
    assert mock.events.count("save") == 3
    assert mock.events.count("restore") == 1


async def test_disconnect_releases_only_after_idle(harness):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    mock.stream_gate = asyncio.Event()
    response = await client.post(url + "/v1/chat/completions", json=conversation(stream=True))
    await response.content.readuntil(b"\n\n")
    response.close()
    await asyncio.sleep(0.05)
    mock.stream_gate.set()
    await settled(manager)
    await infer(client, url, "beta")
    await settled(manager)
    assert mock.max_busy == 1
    assert "save" not in mock.events


async def test_unknown_mutation_invalidates_active(harness):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    await infer(client, url)
    async with client.post(url + "/future-api", data=b"synthetic") as r:
        assert await r.read() == b"synthetic"
    await settled(manager)
    assert manager.active is None


async def test_missing_storage_does_not_create_fallback_directory(harness):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    await infer(client, url)
    await settled(manager)
    moved = mock.directory.with_name("disconnected-storage")
    mock.directory.rename(moved)
    await infer(client, url, "beta")
    await settled(manager)
    assert not mock.directory.exists()
    assert "save" not in mock.events
