"""FEFO consume: earliest expiry first among positive remaining, clean lots.

data_quality='dirty' lots are bad stock (e.g. freezer-burned dumplings): they
stay visible on the shelf but can never be picked for eating, neither by a
single consume nor by a combo confirmation.
"""

def is_edible(lot: dict) -> bool:
    return (lot.get("status", "on_shelf") == "on_shelf"
            and float(lot.get("qty_remain", 0)) > 0
            and lot.get("data_quality") != "dirty")

def sort_lots_fefo(lots: list[dict]) -> list[dict]:
    return sorted(
        [l for l in lots if is_edible(l)],
        key=lambda l: (l.get("expiry") or "9999-12-31", l.get("id") or 0),
    )

def consume_fefo(lots: list[dict], qty: float) -> dict:
    """Return deductions list and leftover demand. Mutates copies only."""
    need = float(qty)
    if need <= 0:
        return {"ok": False, "reason": "qty_non_positive", "deductions": [], "short": 0.0}
    ordered = sort_lots_fefo(lots)
    deductions = []
    for lot in ordered:
        if need <= 0:
            break
        avail = float(lot["qty_remain"])
        take = min(avail, need)
        deductions.append({"lot_id": lot["id"], "take": take, "expiry": lot.get("expiry")})
        need -= take
    if need > 1e-9:
        return {"ok": False, "reason": "short", "deductions": deductions, "short": round(need, 3)}
    return {"ok": True, "reason": "", "deductions": deductions, "short": 0.0}

def consume_group_fefo(requests: list[dict], lots_by_item: dict, allow_partial: bool = False) -> dict:
    """Plan FEFO deductions for several items at once. Pure: reads snapshots, writes nothing.

    requests: [{item_id, qty}]  qty must be > 0.
    lots_by_item: {item_id: [lot rows as they are now]}; only on-shelf,
                   positive-remain, non-dirty lots are eligible.

    allow_partial=False (all-or-nothing): one item short -> ok=False, no item is
    deductible (deductions is empty). allow_partial=True: items that can be fully
    met get their deductions; short items are reported in `short_items` and
    contribute nothing. Zero eligible stock for a requested item counts as short.
    """
    planned = []
    short_items = []
    for req in requests:
        item_id = int(req["item_id"])
        need = float(req["qty"])
        r = consume_fefo(lots_by_item.get(item_id, []), need)
        if r["ok"]:
            planned.append({"item_id": item_id, "qty": need, "deductions": r["deductions"]})
        else:
            short_items.append({"item_id": item_id, "qty": need, "short": r["short"] if r["reason"] == "short" else need})
            if not allow_partial:
                return {"ok": False, "reason": "short", "allow_partial": False,
                        "deductions": [], "items": [], "short_items": short_items}
    if short_items:
        return {"ok": True, "reason": "partial", "allow_partial": True,
                "deductions": [d for it in planned for d in it["deductions"]],
                "items": planned, "short_items": short_items}
    return {"ok": True, "reason": "", "allow_partial": allow_partial,
            "deductions": [d for it in planned for d in it["deductions"]],
            "items": planned, "short_items": []}

def expire_lots(lots: list[dict], today: str) -> list[int]:
    """Ids that should leave shelf: remaining>0 and expiry < today."""
    out = []
    for l in lots:
        exp = l.get("expiry")
        if exp and exp < today and float(l.get("qty_remain", 0)) > 0:
            out.append(l["id"])
    return out
