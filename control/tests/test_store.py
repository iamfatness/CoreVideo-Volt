from volt.store import HoldError, NotClearedError, Store


def test_mint_does_not_wait_for_transcode():
    store = Store()
    asset = store.mint_asset(owner="desk", show_name="final-four", camera="iso-3")
    assert asset.owner == "desk"
    assert asset.legal_hold is False
    assert asset.cleared is False
    essence = store.add_essence(asset.id, role="hi-res", location="quarantine/card.mxf", open_file=True)
    assert essence
    assert store.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0] == 1


def test_proxy_is_not_a_second_asset():
    store = Store()
    asset = store.mint_asset(owner="desk")
    store.add_essence(asset.id, role="hi-res", location="media/a.mxf", open_file=True)
    store.add_essence(asset.id, role="proxy", location="proxy/a.mp4", open_file=True)
    assert store.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM essence").fetchone()[0] == 2


def test_replay_does_not_mint():
    store = Store()
    asset = store.mint_asset(owner="desk")
    job = store.enqueue(asset.id, "proxy", "proxy-v1")
    store.fail_job(job.id, "encoder died")
    again = store.enqueue(asset.id, "proxy", "proxy-v1")
    assert again.id == job.id
    assert again.attempt == 2
    assert again.status == "queued"
    assert store.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 1


def test_hold_blocks_delete_and_uncleared_blocks_publish():
    store = Store()
    asset = store.mint_asset(owner="desk")
    store.set_hold(asset.id, True)
    try:
        store.delete_asset(asset.id)
        raise AssertionError("hold should block delete")
    except HoldError:
        pass
    try:
        store.assert_publishable(asset.id)
        raise AssertionError("uncleared should block publish")
    except NotClearedError:
        pass
    store.set_hold(asset.id, False)
    store.set_cleared(asset.id, True)
    store.assert_publishable(asset.id)
    job_id = store.delete_asset(asset.id)
    assert job_id


def test_span_holds_timecode_not_a_path():
    store = Store()
    asset = store.mint_asset(owner="desk")
    span_id = store.add_span(asset.id, "transcript", "01:12:08:00", "01:12:09:12", text="steal")
    row = store.conn.execute("SELECT * FROM span WHERE id = ?", (span_id,)).fetchone()
    assert row["asset_id"] == asset.id
    assert row["text"] == "steal"
    assert "location" not in row.keys()
