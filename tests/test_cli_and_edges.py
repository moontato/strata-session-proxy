import asyncio
import json
import sys

import pytest
import yaml
from conftest import conversation, infer, settled

from strata_session_proxy.cli import main
from strata_session_proxy.config import load


def test_yaml_errors_do_not_include_source_lines(tmp_path):
    path = tmp_path / "invalid.local.yaml"
    path.write_text("server: [synthetic-private-value\n")
    with pytest.raises(ValueError) as exc:
        load(str(path))
    assert "synthetic-private-value" not in str(exc.value)


@pytest.mark.parametrize(
    "origin", ["http://localhost:bad", "http://localhost:99999", " http://localhost", "http://localhost/path"]
)
def test_bad_origin_is_rejected(tmp_path, origin):
    path = tmp_path / "invalid.local.yaml"
    path.write_text(yaml.safe_dump({"server": {"backend_url": origin}}))
    with pytest.raises(ValueError):
        load(str(path))


async def test_cli_status_is_local_and_hides_payloads(harness, tmp_path, monkeypatch, capsys):
    mock, client, make, _ = harness
    url, manager, config = await make()
    assert mock.control_calls == []  # even managed startup is network-free
    await infer(client, url)
    await infer(client, url, "beta")
    await settled(manager)
    path = tmp_path / "cli.local.yaml"
    path.write_text(yaml.safe_dump({"snapshots": {"state_directory": config.snapshots.state_directory}}))
    calls = list(mock.control_calls)
    monkeypatch.setattr(sys, "argv", ["strata-session-proxy", "--config", str(path), "status"])
    main()
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["snapshot_count"] == 1
    assert "synthetic shared system" not in output and "synthetic-model" not in output
    assert mock.control_calls == calls


async def test_offline_recovery_preserves_completed_snapshots(harness, tmp_path, monkeypatch, capsys):
    from strata_session_proxy.storage import Store

    mock, client, make, _ = harness
    url, manager, config = await make()
    await infer(client, url)
    await infer(client, url, "beta")
    await settled(manager)
    good = manager.store.rows()[0]["name"]
    pending = manager.store.begin(manager.active, "synthetic-interrupted")
    manager.store.path(pending).write_bytes(b"synthetic partial snapshot")
    manager.store.meta("blocked", "save_in_progress")
    manager.store.close()
    path = tmp_path / "recover.local.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "snapshots": {
                    "directory": config.snapshots.directory,
                    "state_directory": config.snapshots.state_directory,
                }
            }
        )
    )
    calls = list(mock.control_calls)
    monkeypatch.setattr(
        sys,
        "argv",
        ["strata-session-proxy", "--config", str(path), "recover", "--acknowledge-backend-quiescent"],
    )
    main()
    assert "no backend requests" in capsys.readouterr().out
    assert mock.control_calls == calls
    store = Store(config.snapshots)
    assert [row["name"] for row in store.rows()] == [good]
    assert not store.path(pending).exists()
    assert not store.meta("blocked")
    store.close()


async def test_disconnect_during_save_does_not_start_abandoned_inference(harness):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    await infer(client, url)
    await settled(manager)
    mock.delay = 0.1
    task = asyncio.create_task(client.post(url + "/v1/chat/completions", json=conversation("beta")))
    async with asyncio.timeout(1):
        while "save" not in mock.events:
            await asyncio.sleep(0.005)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await settled(manager)
    assert mock.events == ["inference", "save"]
    assert len(manager.store.rows()) == 1
    assert not manager.store.meta("blocked")
    await infer(client, url, "gamma")
    await settled(manager)
    assert mock.events[-1] == "inference"


async def test_missing_candidate_retired_without_stale_record(harness):
    mock, client, make, _ = harness
    url, manager, _ = await make(auto_save=True)
    await infer(client, url)
    await infer(client, url, "beta")
    await settled(manager)
    name = manager.store.rows()[0]["name"]
    (mock.directory / name).unlink()
    await infer(client, url, continuation=True)
    await settled(manager)
    assert name not in [r["name"] for r in manager.store.rows()]
    assert "restore" not in mock.events


async def test_engine_snapshot_support_disabled_is_safe_fallback(harness):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    mock.save_error = (501, {"error": {"message": "synthetic unsupported"}})
    await infer(client, url)
    await infer(client, url, "beta")
    await settled(manager)
    assert not manager.store.meta("blocked")
    assert manager.store.rows() == []


async def test_energy_estimates_are_configured_not_measured(harness):
    _, client, make, _ = harness
    url, manager, config = await make()
    config.estimates.prefill_tokens_per_second = 200
    config.estimates.power_watts = 100
    config.estimates.electricity_price_per_kwh = 0.5
    await infer(client, url)
    await infer(client, url, "beta")
    await infer(client, url, continuation=True)
    await settled(manager)
    e = manager.store.report(config.estimates)["estimates_not_measurements"]
    assert e["compute_seconds_avoided"] == 2
    assert e["electricity_kwh_avoided"] == pytest.approx(200 / 3_600_000)
    assert e["electricity_cost_avoided"] == pytest.approx(100 / 3_600_000)
