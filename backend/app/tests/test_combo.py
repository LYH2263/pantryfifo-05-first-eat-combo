import threading

import pytest

from app.engines.fefo import consume_group_fefo


def clean_lot(lot_id, item_id, qty, expiry="2026-12-01", dq="clean"):
    return {"id": lot_id, "item_id": item_id, "qty_remain": qty,
            "expiry": expiry, "status": "on_shelf", "data_quality": dq}


# ---------- pure engine semantics ----------

def test_group_all_or_nothing_blocks_whole_group():
    lots = {
        1: [clean_lot(10, 1, 5)],
        2: [clean_lot(20, 2, 1)],
    }
    r = consume_group_fefo([{"item_id": 1, "qty": 5}, {"item_id": 2, "qty": 3}], lots)
    assert r["ok"] is False and r["reason"] == "short"
    # one item short -> the WHOLE group carries no deductible snapshot
    assert r["deductions"] == [] and r["items"] == []
    assert r["short_items"][0]["item_id"] == 2 and r["short_items"][0]["short"] == 2


def test_group_partial_skips_short_items_only():
    lots = {1: [clean_lot(10, 1, 5)], 2: [clean_lot(20, 2, 1)]}
    r = consume_group_fefo(
        [{"item_id": 1, "qty": 5}, {"item_id": 2, "qty": 3}], lots, allow_partial=True)
    assert r["ok"] is True and r["reason"] == "partial"
    assert [it["item_id"] for it in r["items"]] == [1]
    assert r["items"][0]["deductions"][0]["take"] == 5
    assert r["short_items"][0]["item_id"] == 2
    assert [d["lot_id"] for d in r["deductions"]] == [10]


def test_group_dirty_lot_never_deducted():
    # 冻饺 freezer-burned: positive remain but dirty -> cannot eat it in a combo
    lots = {3: [clean_lot(30, 3, 1, expiry="2025-01-01", dq="dirty")]}
    r = consume_group_fefo([{"item_id": 3, "qty": 1}], lots)
    assert r["ok"] is False and r["short_items"][0]["short"] == 1
    assert r["deductions"] == []


def test_group_non_positive_and_off_shelf_ignored():
    lots = {1: [
        clean_lot(10, 1, 0),
        {**clean_lot(11, 1, 2), "status": "consumed"},
        clean_lot(12, 1, 2),
    ]}
    r = consume_group_fefo([{"item_id": 1, "qty": 2}], lots)
    assert r["ok"] is True
    assert [d["lot_id"] for d in r["deductions"]] == [12]


def test_group_fefo_order_per_item():
    lots = {1: [clean_lot(11, 1, 3, "2026-11-01"), clean_lot(10, 1, 3, "2026-10-20")]}
    r = consume_group_fefo([{"item_id": 1, "qty": 4}], lots)
    d = r["deductions"]
    assert d[0]["lot_id"] == 10 and d[0]["take"] == 3
    assert d[1]["lot_id"] == 11 and d[1]["take"] == 1


# ---------- endpoint / real-sqlite behaviour ----------

@pytest.fixture()
def client_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app import seed
    from app import main
    seed.init_db()
    c = main.connect()
    # Seed lots 1/2 are expired-but-on-shelf milk; park them so item 1 stock is
    # deterministic: lot 6 = 6 (2026-12-10), lot 7 = 4 (2026-12-20).
    # lot 3 = 12 clean eggs; lot 4 = dirty 冻饺 x1; lot 5 = dirty egg lot qty -3.
    c.execute("UPDATE lots SET status='consumed' WHERE id IN (1,2)")
    c.execute("INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (1,6,6,'2026-12-10','on_shelf','clean')")
    c.execute("INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (1,4,4,'2026-12-20','on_shelf','clean')")
    c.execute("INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (3,2,2,'2026-12-15','on_shelf','clean')")
    c.commit(); c.close()
    return main


def _rem(main, lot_id):
    c = main.connect()
    row = dict(c.execute("SELECT qty_remain,status FROM lots WHERE id=?", (lot_id,)).fetchone())
    c.close(); return row


def test_preview_does_not_touch_lots(client_env):
    main = client_env
    before = [_rem(main, i) for i in (6, 7, 8)]
    main.combo_preview(main.ComboIn(items=[main.ComboItemIn(item_id=1, qty=5),
                                           main.ComboItemIn(item_id=3, qty=2)]))
    assert [_rem(main, i) for i in (6, 7, 8)] == before


def test_confirm_rejects_non_positive(client_env):
    main = client_env
    with pytest.raises(main.HTTPException) as ei:
        main.combo_confirm(main.ComboIn(items=[main.ComboItemIn(item_id=1, qty=0)]))
    assert ei.value.status_code == 400


def test_all_or_nothing_rolls_back_entire_group(client_env):
    main = client_env
    body = main.ComboIn(items=[main.ComboItemIn(item_id=1, qty=5),   # available
                               main.ComboItemIn(item_id=2, qty=99)])  # only 12 eggs
    with pytest.raises(main.HTTPException) as ei:
        main.combo_confirm(body)
    assert ei.value.status_code == 409
    # nothing deducted anywhere
    assert _rem(main, 6)["qty_remain"] == 6 and _rem(main, 7)["qty_remain"] == 4
    c = main.connect()
    assert c.execute("SELECT COUNT(*) n FROM combos").fetchone()["n"] == 0
    c.close()


def test_partial_confirm_response_matches_layer_quantities(client_env):
    main = client_env
    r = main.combo_confirm(main.ComboIn(
        items=[main.ComboItemIn(item_id=1, qty=6), main.ComboItemIn(item_id=2, qty=99)],
        allow_partial=True))
    assert r["reason"] == "partial"
    # lot 6 fully taken (consumed), lot 7 untouched
    assert _rem(main, 6) == {"qty_remain": 0, "status": "consumed"}
    assert _rem(main, 7)["qty_remain"] == 4
    # every returned take matches what actually left the shelf
    for d in r["deductions"]:
        assert r["lots_remaining"][d["lot_id"]]["qty_remain"] == _rem(main, d["lot_id"])["qty_remain"]
    assert [s["item_id"] for s in r["short_items"]] == [2]


def test_dirty_dumpling_never_enters_deductions(client_env):
    main = client_env
    # lot 4 is the seeded dirty 冻饺 (qty 1, 2025-01-01); lot 8 is clean 冻饺 x2
    with pytest.raises(main.HTTPException):
        main.combo_confirm(main.ComboIn(items=[main.ComboItemIn(item_id=3, qty=3)]))
    assert _rem(main, 4)["qty_remain"] == 1 and _rem(main, 4)["status"] == "on_shelf"
    # partial group: item1 fully met, 冻饺 short as a whole -> that item writes nothing
    r = main.combo_confirm(main.ComboIn(
        items=[main.ComboItemIn(item_id=1, qty=2), main.ComboItemIn(item_id=3, qty=3)],
        allow_partial=True))
    assert r["reason"] == "partial"
    assert [it["item_id"] for it in r["items"]] == [1]
    assert 4 not in [d["lot_id"] for d in r["deductions"]]
    assert _rem(main, 8)["qty_remain"] == 2 and _rem(main, 8)["status"] == "on_shelf"
    assert _rem(main, 4)["qty_remain"] == 1
    # clean dumpling stock alone can satisfy 2; dirty lot (earlier expiry!) must not be FEFO-picked
    r2 = main.combo_confirm(main.ComboIn(items=[main.ComboItemIn(item_id=3, qty=2)]))
    assert [d["lot_id"] for d in r2["deductions"]] == [8]
    assert _rem(main, 4)["qty_remain"] == 1 and _rem(main, 4)["status"] == "on_shelf"


def test_partial_with_no_fulfillable_item_writes_nothing(client_env):
    main = client_env
    with pytest.raises(main.HTTPException) as ei:
        main.combo_confirm(main.ComboIn(
            items=[main.ComboItemIn(item_id=2, qty=99)], allow_partial=True))
    assert ei.value.status_code == 409
    c = main.connect()
    assert c.execute("SELECT COUNT(*) n FROM combos").fetchone()["n"] == 0
    assert _rem(main, 3)["qty_remain"] == 12  # clean egg lot untouched
    c.close()


def test_warn_days_change_preserves_confirmed_snapshot(client_env):
    main = client_env
    r = main.combo_confirm(main.ComboIn(items=[main.ComboItemIn(item_id=3, qty=2)]))
    cid = r["combo_id"]
    assert r["warn_days"] == 3
    main.update_settings(main.SettingsIn(warn_days=10))
    combos = main.combos()
    snap = next(x for x in combos if x["id"] == cid)
    assert snap["warn_days"] == 3                      # frozen at confirm time
    assert snap["result"]["warn_days"] == 3
    assert main.settings()["warn_days"] == "10"        # top bar recomputes live


def test_combo_vs_single_consume_are_mutex_to_one_outcome(client_env):
    main = client_env
    barrier = threading.Barrier(2)
    outcomes = {}

    def do_combo():
        barrier.wait()
        try:
            outcomes["combo"] = main.combo_confirm(main.ComboIn(
                items=[main.ComboItemIn(item_id=1, qty=5),
                       main.ComboItemIn(item_id=3, qty=2)]))
        except main.HTTPException as e:
            outcomes["combo"] = ("409", e.status_code)

    def do_consume():
        barrier.wait()
        try:
            outcomes["consume"] = main.consume(main.ConsumeIn(item_id=1, qty=6))
        except main.HTTPException as e:
            outcomes["consume"] = ("409", e.status_code)

    t1, t2 = threading.Thread(target=do_combo), threading.Thread(target=do_consume)
    t1.start(); t2.start(); t1.join(); t2.join()

    final6, final7, final8 = (_rem(main, i)["qty_remain"] for i in (6, 7, 8))
    # item1 stock 10, contested demands 5 (combo) + 6 (consume) = 11 -> exactly one loses
    winners = [k for k, v in outcomes.items() if v != ("409", 409)]
    losers = [k for k, v in outcomes.items() if v == ("409", 409)]
    assert len(winners) == 1 and len(losers) == 1
    assert final6 >= 0 and final7 >= 0 and final8 >= 0  # never over-deducted

    # what the winner reported as take must equal the actual shelf reduction
    if "combo" in winners:
        took1 = sum(d["take"] for d in outcomes["combo"]["deductions"] if _item_of(main, d["lot_id"]) == 1)
        assert final6 + final7 + took1 == 10
        assert final8 == 0  # 冻饺 clean lot consumed as part of the group
    else:
        took1 = sum(d["take"] for d in outcomes["consume"]["deductions"])
        assert final6 + final7 + took1 == 10
        assert final8 == 2  # all-or-nothing group lost -> 冻饺 untouched


def _item_of(main, lot_id):
    c = main.connect()
    item_id = c.execute("SELECT item_id FROM lots WHERE id=?", (lot_id,)).fetchone()[0]
    c.close(); return item_id


def test_combo_vs_off_shelf_sweep_leave_one_consistent_result(client_env):
    main = client_env
    c = main.connect()
    # expired but still on shelf, clean, positive
    c.execute("INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (2,3,3,'2020-01-01','on_shelf','clean')")
    c.commit()
    expired_lot = c.execute("SELECT MAX(id) m FROM lots").fetchone()["m"]
    c.close()

    barrier = threading.Barrier(2)
    outcomes = {}

    def do_combo():
        barrier.wait()
        try:
            # 13 eggs: 3 on the expired front lot + 12 clean. Sweep-first leaves 12 -> short.
            outcomes["combo"] = main.combo_confirm(main.ComboIn(
                items=[main.ComboItemIn(item_id=2, qty=13),
                       main.ComboItemIn(item_id=1, qty=1)]))
        except main.HTTPException as e:
            outcomes["combo"] = ("409", e.status_code)

    def do_sweep():
        barrier.wait()
        outcomes["sweep"] = main.expire_sweep()

    # NOTE: combo wants item2; the expired lot is item2's FEFO-front lot, so the
    # sweep directly removes stock the combo planned to take.
    t1, t2 = threading.Thread(target=do_combo), threading.Thread(target=do_sweep)
    t1.start(); t2.start(); t1.join(); t2.join()

    row = _rem(main, expired_lot)
    if outcomes.get("combo") == ("409", 409):
        # sweep landed first: lot left the shelf, whole all-or-nothing group aborted
        assert row["status"] == "expired" and row["qty_remain"] == 3
        assert _rem(main, 6)["qty_remain"] == 6   # item1 of the group untouched
        assert _rem(main, 3)["qty_remain"] == 12  # clean egg lot untouched
    else:
        # combo landed first: expired front lot got consumed, no 409 split-brain
        assert row["status"] == "consumed" and row["qty_remain"] == 0
