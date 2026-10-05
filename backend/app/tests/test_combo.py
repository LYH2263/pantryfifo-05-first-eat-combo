import json
import threading
from datetime import date, timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import seed
from app.db import connect
from app.main import (ComboDemand, ComboIn, ConsumeIn, app, combo_confirm,
                      combo_preview, consume, expire_sweep)
from app.modules.recipe_suggest import combinable, plan_combo


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seed.init_db()
    return tmp_path


def lot_rows():
    c = connect()
    rows = [dict(r) for r in c.execute("SELECT * FROM lots ORDER BY id")]
    c.close()
    return rows


def shelf_qty(item_id):
    c = connect()
    q = c.execute(
        "SELECT COALESCE(SUM(qty_remain),0) s FROM lots WHERE item_id=? AND status='on_shelf'",
        (item_id,)).fetchone()["s"]
    c.close()
    return q


def consumption_rows():
    c = connect()
    rows = [dict(r) for r in c.execute("SELECT * FROM consumptions")]
    c.close()
    return rows


# ---------- pure planning ----------

def test_combinable_filters_dirty_and_nonpositive():
    lots = [
        {"id": 1, "qty_remain": 2, "data_quality": "clean"},
        {"id": 2, "qty_remain": 1, "data_quality": "dirty"},
        {"id": 3, "qty_remain": -3, "data_quality": "clean"},
        {"id": 4, "qty_remain": 0, "data_quality": "clean"},
    ]
    assert [l["id"] for l in combinable(lots)] == [1]


def test_plan_all_or_nothing():
    lots_by = {
        1: [{"id": 11, "qty_remain": 2, "expiry": "2026-01-01", "data_quality": "clean"}],
        2: [{"id": 12, "qty_remain": 1, "expiry": "2026-01-01", "data_quality": "clean"}],
    }
    plan = plan_combo(lots_by, [{"item_id": 1, "qty": 2}, {"item_id": 2, "qty": 5}])
    assert plan["ok"] is False
    ok_item, short_item = plan["items"]
    assert ok_item["ok"] and ok_item["deductions"][0]["take"] == 2
    assert not short_item["ok"] and short_item["short"] == 4


# ---------- preview ----------

def test_preview_does_not_write(db):
    before = lot_rows()
    plan = combo_preview(ComboIn(items=[ComboDemand(item_id=1, qty=2)]))
    assert plan["ok"] and plan["preview"] is True
    # FEFO: lot#2 (exp 2026-09-28) first, then lot#1 (exp 2026-10-01)
    assert [(d["lot_id"], d["take"]) for d in plan["items"][0]["deductions"]] == [(2, 1.0), (1, 1.0)]
    assert lot_rows() == before
    assert consumption_rows() == []


def test_nonpositive_or_empty_demand_rejected(db):
    with pytest.raises(HTTPException) as ei:
        combo_preview(ComboIn(items=[ComboDemand(item_id=1, qty=0)]))
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException) as ei:
        combo_confirm(ComboIn(items=[ComboDemand(item_id=1, qty=-2)]))
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException) as ei:
        combo_confirm(ComboIn(items=[]))
    assert ei.value.status_code == 400


# ---------- confirm ----------

def test_confirm_writes_group_snapshot_and_remainders(db):
    snap = combo_confirm(ComboIn(items=[ComboDemand(item_id=1, qty=2)], note="早餐"))
    assert snap["kind"] == "combo" and snap["warn_days"] == 3
    takes = {d["lot_id"]: d["take"] for d in snap["items"][0]["deductions"]}
    assert takes == {2: 1.0, 1: 1.0}
    # layer-page remainders dropped by exactly the response takes
    lots = {l["id"]: l for l in lot_rows()}
    assert lots[2]["qty_remain"] == 0 and lots[2]["status"] == "consumed"
    assert lots[1]["qty_remain"] == 1 and lots[1]["status"] == "on_shelf"
    rows = consumption_rows()
    assert len(rows) == 1
    stored = json.loads(rows[0]["result_json"])
    assert stored["warn_days"] == 3 and stored["items"] == snap["items"]


def test_dirty_lot_never_enters_combo(db):
    # 冻饺 only has a dirty lot on shelf → nothing clean to plan from
    with pytest.raises(HTTPException) as ei:
        combo_confirm(ComboIn(items=[ComboDemand(item_id=3, qty=1)]))
    assert ei.value.status_code == 409
    plan = ei.value.detail
    assert plan["ok"] is False and plan["items"][0]["deductions"] == []
    assert consumption_rows() == []


def test_one_short_item_voids_whole_group(db):
    before = lot_rows()
    with pytest.raises(HTTPException) as ei:
        combo_confirm(ComboIn(items=[ComboDemand(item_id=1, qty=1),
                                     ComboDemand(item_id=2, qty=99)]))
    assert ei.value.status_code == 409
    # 牛奶 line alone was satisfiable, but the group is atomic: zero writes
    assert lot_rows() == before
    assert consumption_rows() == []


# ---------- warn_days ----------

def test_warn_days_change_keeps_snapshot_alerts_follow_live(db):
    exp10 = (date.today() + timedelta(days=10)).isoformat()
    c = connect()
    c.execute("INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality)"
              " VALUES (2,5,5,?,'on_shelf','clean')", (exp10,))
    c.commit(); c.close()
    with TestClient(app) as client:
        snap = client.post("/api/combo/confirm",
                           json={"items": [{"item_id": 1, "qty": 1}]}).json()
        cid = snap["consumption_id"]
        assert snap["warn_days"] == 3
        # warn_days=3: the today+10 lot is not yet on the live alert bar
        assert not any(a["expiry"] == exp10 for a in client.get("/api/alerts").json())

        assert client.put("/api/settings", json={"warn_days": 30}).status_code == 200

        # confirmed snapshot is immutable: still the old warn_days, same deductions
        rec = [x for x in client.get("/api/consumptions").json() if x["id"] == cid][0]
        assert rec["result"]["warn_days"] == 3
        assert rec["result"]["items"] == snap["items"]
        # the alert bar is computed live and follows the new warn_days
        assert any(a["expiry"] == exp10 for a in client.get("/api/alerts").json())


# ---------- concurrency: combo vs bypass ----------

def _race(fn_a, fn_b):
    barrier = threading.Barrier(2)
    out = {}

    def run(name, fn):
        barrier.wait()
        try:
            out[name] = ("ok", fn())
        except HTTPException as e:
            out[name] = ("err", e.status_code)

    ts = [threading.Thread(target=run, args=("a", fn_a)),
          threading.Thread(target=run, args=("b", fn_b))]
    for t in ts: t.start()
    for t in ts: t.join()
    return out


def test_combo_and_single_consume_mutually_exclusive(db):
    before = shelf_qty(2)  # 鸡蛋: 12 clean + (-3) dirty = 9 on the layer page
    out = _race(
        lambda: combo_confirm(ComboIn(items=[ComboDemand(item_id=2, qty=10)])),
        lambda: consume(ConsumeIn(item_id=2, qty=10)),
    )
    # exactly one side wins; the loser gets a clean 409, never a half-write
    assert sorted(v[0] for v in out.values()) == ["err", "ok"]
    assert [v[1] for v in out.values() if v[0] == "err"] == [409]
    # layer-page remainder dropped by exactly the winner's takes, nothing double-counted
    assert before - shelf_qty(2) == 10
    assert len(consumption_rows()) == 1
    winner = [v[1] for v in out.values() if v[0] == "ok"][0]
    ded = winner["items"][0]["deductions"] if winner.get("kind") == "combo" else winner["deductions"]
    assert sum(d["take"] for d in ded) == 10
    # no deduction drove a usable lot negative (the seeded dirty -3 stays as-is)
    assert all(l["qty_remain"] >= 0 for l in lot_rows() if l["data_quality"] == "clean")


def test_combo_and_expire_sweep_mutually_exclusive(db):
    c = connect()
    c.execute("INSERT INTO items(id,name,layer,unit) VALUES (99,'临期奶','upper','盒')")
    cur = c.execute("INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality)"
                    " VALUES (99,2,2,'2020-01-01','on_shelf','clean')")
    lid = cur.lastrowid
    c.commit(); c.close()

    out = _race(
        lambda: combo_confirm(ComboIn(items=[ComboDemand(item_id=99, qty=2)])),
        lambda: expire_sweep(),
    )
    lot = [l for l in lot_rows() if l["id"] == lid][0]
    combo = out["a"]
    if combo[0] == "ok":
        # combo committed → lot fully consumed; the sweep must not also expire it
        assert lot["status"] == "consumed" and lot["qty_remain"] == 0
        assert lid not in out["b"][1]["expired_ids"]
    else:
        # sweep committed first → combo saw the lot gone and wrote nothing
        assert combo[1] == 409
        assert lot["status"] == "expired" and lot["qty_remain"] == 2
        assert lid in out["b"][1]["expired_ids"]
