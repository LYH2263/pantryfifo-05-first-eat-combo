import json
import sqlite3
from datetime import date, datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from app import seed
from app.db import connect
from app.engines.fefo import consume_fefo, expire_lots
from app.modules.recipe_suggest import plan_combo

app = FastAPI(title="Pantryfifo", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def _startup(): seed.init_db()

@app.get("/api/health")
def health(): return {"ok": True, "project": "pantryfifo"}

@app.get("/api/items")
def items():
    c = connect(); rows = [dict(r) for r in c.execute("SELECT * FROM items")]; c.close(); return rows

@app.get("/api/fridge")
def fridge(layer: str | None = None):
    c = connect()
    q = """SELECT lots.*, items.name, items.layer, items.unit FROM lots
           JOIN items ON items.id=lots.item_id WHERE lots.status='on_shelf'"""
    args = []
    if layer:
        q += " AND items.layer=?"; args.append(layer)
    rows = [dict(r) for r in c.execute(q, args)]; c.close(); return rows

@app.get("/api/alerts")
def alerts():
    # computed live on every call: follows the current warn_days setting
    c = connect()
    warn = int(c.execute("SELECT value FROM settings WHERE key='warn_days'").fetchone()["value"])
    today = date.today().isoformat()
    rows = [dict(r) for r in c.execute(
        """SELECT lots.*, items.name, items.layer FROM lots JOIN items ON items.id=lots.item_id
           WHERE status='on_shelf' AND qty_remain>0 AND expiry IS NOT NULL""")]
    c.close()
    out = []
    for r in rows:
        if r["expiry"] <= today:
            r["level"] = "expired"
            out.append(r)
        else:
            # simple day diff via fromisoformat
            delta = (date.fromisoformat(r["expiry"]) - date.today()).days
            if delta <= warn:
                r["level"] = "soon"; r["days_left"] = delta; out.append(r)
    return out

class LotIn(BaseModel):
    item_id: int
    qty: float
    expiry: str

@app.post("/api/lots")
def inbound(body: LotIn):
    c = connect()
    item = c.execute("SELECT id FROM items WHERE id=?", (body.item_id,)).fetchone()
    if not item: c.close(); raise HTTPException(404, "item")
    cur = c.execute(
        "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
        (body.item_id, body.qty, body.qty, body.expiry, "on_shelf", "clean"))
    c.commit(); lid = cur.lastrowid; c.close(); return {"id": lid}

def _apply_deductions(c, deductions: list[dict]):
    """Apply takes inside the caller's transaction. The guarded UPDATE
    (status/qty_remain re-checked per row) keeps the response takes and the
    layer-page remainders from ever diverging; any mismatch rolls back."""
    for d in deductions:
        cur = c.execute(
            "UPDATE lots SET qty_remain = qty_remain - ? "
            "WHERE id=? AND status='on_shelf' AND qty_remain >= ?",
            (d["take"], d["lot_id"], d["take"]))
        if cur.rowcount != 1:
            c.rollback()
            raise HTTPException(409, {"reason": "lot_changed", "lot_id": d["lot_id"]})
        rem = c.execute("SELECT qty_remain FROM lots WHERE id=?", (d["lot_id"],)).fetchone()["qty_remain"]
        if rem <= 0:
            c.execute("UPDATE lots SET status='consumed', qty_remain=0 WHERE id=?", (d["lot_id"],))

class ConsumeIn(BaseModel):
    item_id: int
    qty: float
    note: str = ""

@app.post("/api/consume")
def consume(body: ConsumeIn):
    c = connect()
    try:
        # BEGIN IMMEDIATE takes the write lock up front, so the re-read below
        # is serialized against combo confirms and expire sweeps hitting the
        # same lots: exactly one side of the race observes each lot state.
        c.execute("BEGIN IMMEDIATE")
        lots = [dict(r) for r in c.execute(
            "SELECT * FROM lots WHERE item_id=? AND status='on_shelf' AND qty_remain>0", (body.item_id,))]
        result = consume_fefo(lots, body.qty)
        if not result["ok"]:
            c.rollback()
            if result["reason"] == "qty_non_positive":
                raise HTTPException(400, result["reason"])
            raise HTTPException(409, result)
        _apply_deductions(c, result["deductions"])
        c.execute("INSERT INTO consumptions(note,result_json,created_at) VALUES (?,?,?)",
                  (body.note, json.dumps(result), datetime.now(timezone.utc).isoformat()))
        c.commit(); return result
    except HTTPException:
        raise
    except sqlite3.OperationalError as e:
        c.rollback(); raise HTTPException(409, f"busy: {e}")
    finally:
        c.close()

def _warn_days(c) -> int:
    return int(c.execute("SELECT value FROM settings WHERE key='warn_days'").fetchone()["value"])

def _lots_by_item(c, item_ids: list[int]) -> dict:
    marks = ",".join("?" for _ in item_ids)
    rows = [dict(r) for r in c.execute(
        f"SELECT * FROM lots WHERE status='on_shelf' AND item_id IN ({marks})", item_ids)]
    out: dict[int, list[dict]] = {}
    for r in rows:
        out.setdefault(r["item_id"], []).append(r)
    return out

def _merged_demands(parts: list["ComboDemand"]) -> list[dict]:
    """Validate and merge duplicate item lines into one demand per item."""
    merged: dict[int, float] = {}
    for p in parts:
        if float(p.qty) <= 0:
            raise HTTPException(400, "qty_non_positive")
        merged[p.item_id] = merged.get(p.item_id, 0.0) + float(p.qty)
    if not merged:
        raise HTTPException(400, "empty_combo")
    return [{"item_id": i, "qty": q} for i, q in merged.items()]

class ComboDemand(BaseModel):
    item_id: int
    qty: float

class ComboIn(BaseModel):
    items: list[ComboDemand]
    note: str = ""

@app.post("/api/combo/preview")
def combo_preview(body: ComboIn):
    """Planned takes against current on-shelf positive clean remainders.
    Preview only — reads, never writes lots."""
    demands = _merged_demands(body.items)
    c = connect()
    plan = plan_combo(_lots_by_item(c, [d["item_id"] for d in demands]), demands)
    plan["warn_days"] = _warn_days(c)
    c.close()
    plan["preview"] = True
    return plan

@app.post("/api/combo/confirm")
def combo_confirm(body: ComboIn):
    """Atomic all-or-nothing combo write. Re-plans inside BEGIN IMMEDIATE
    (never trusts the preview), so a concurrent single consume or expire
    sweep on the same lots settles into exactly one outcome: either this
    group commits whole, or it rolls back with 409 and writes nothing."""
    demands = _merged_demands(body.items)
    c = connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        plan = plan_combo(_lots_by_item(c, [d["item_id"] for d in demands]), demands)
        if not plan["ok"]:
            c.rollback()
            raise HTTPException(409, plan)
        warn = _warn_days(c)
        for it in plan["items"]:
            _apply_deductions(c, it["deductions"])
        now = datetime.now(timezone.utc).isoformat()
        # snapshot is written once and never rewritten — later warn_days
        # edits must not mutate this confirmed group's deduction record
        snapshot = {"kind": "combo", "ok": True, "note": body.note,
                    "warn_days": warn, "created_at": now, "items": plan["items"]}
        cur = c.execute("INSERT INTO consumptions(note,result_json,created_at) VALUES (?,?,?)",
                        (body.note, json.dumps(snapshot), now))
        snapshot["consumption_id"] = cur.lastrowid
        c.commit(); return snapshot
    except HTTPException:
        raise
    except sqlite3.OperationalError as e:
        c.rollback(); raise HTTPException(409, f"busy: {e}")
    finally:
        c.close()

@app.get("/api/consumptions")
def consumptions():
    c = connect()
    rows = [dict(r) for r in c.execute("SELECT * FROM consumptions ORDER BY id DESC")]
    c.close()
    for r in rows:
        r["result"] = json.loads(r.pop("result_json"))
    return rows

@app.post("/api/expire-sweep")
def expire_sweep():
    c = connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        lots = [dict(r) for r in c.execute("SELECT * FROM lots WHERE status='on_shelf'")]
        ids = expire_lots(lots, date.today().isoformat())
        for i in ids:
            c.execute("UPDATE lots SET status='expired' WHERE id=? AND status='on_shelf'", (i,))
        c.commit(); return {"expired_ids": ids}
    except sqlite3.OperationalError as e:
        c.rollback(); raise HTTPException(409, f"busy: {e}")
    finally:
        c.close()

@app.get("/api/settings")
def settings():
    c = connect(); rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}; c.close(); return rows

class SettingsIn(BaseModel):
    warn_days: int

@app.put("/api/settings")
def put_settings(body: SettingsIn):
    # touches only the settings row; confirmed combo snapshots keep the
    # warn_days they were confirmed with, the alert bar recomputes live
    if body.warn_days < 0:
        raise HTTPException(400, "warn_days_negative")
    c = connect()
    c.execute("INSERT INTO settings(key,value) VALUES ('warn_days',?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(body.warn_days),))
    c.commit(); c.close(); return {"warn_days": body.warn_days}
