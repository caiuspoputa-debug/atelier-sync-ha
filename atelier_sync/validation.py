"""Portable validation for the version-1 Atelier agenda, with no clock checks.

Historic offline work is valid data. Availability and data-shape checks belong
here; the phone/tablet UI decides whether a newly entered start is in the past.
"""
from __future__ import annotations

import copy
import datetime as dt
import math
import re


class ValidationError(ValueError):
    pass


STATUSES = {"pending", "confirmed", "completed", "cancelled", "no-show"}
POLICY = "daily-10-18-v1"
JS_WHITESPACE = "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"


def fail(message="Datele nu respectă formatul agendei Atelier, versiunea 1."):
    raise ValidationError(message)


def obj(value):
    if not isinstance(value, dict):
        fail()
    return value


def text(value, limit=200):
    if not isinstance(value, str):
        fail()
    try:
        value.encode("utf-8")
        if len(value.encode("utf-16-le")) // 2 > limit:
            fail()
    except UnicodeError:
        fail()
    return value


def identifier(value):
    if not text(value, 100):
        fail()
    return value


def number(value, low, high, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high or not math.isfinite(value):
        fail()
    if integer and value != int(value):
        fail()
    return value


def array(value, limit):
    if not isinstance(value, list) or len(value) > limit:
        fail()
    return value


def unique(items, key="id"):
    if len({x[key] for x in items}) != len(items):
        fail("Există identificatori duplicați în datele agendei.")


def date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value, re.ASCII):
        fail()
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError:
        fail()
    # The existing Android validator uses a four-digit, unpadded local year.
    if parsed.year < 1000:
        fail()
    return value


def clock(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value, re.ASCII):
        fail()
    return int(value[:2]) * 60 + int(value[3:])


def timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", value, re.ASCII):
        fail()
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        fail()
    if parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z") != value:
        fail()
    return value


def color(value):
    if not isinstance(value, str) or not re.fullmatch(r"#[a-fA-F0-9]{6}", value):
        fail()
    return value


def optional_text(source, key, limit):
    return text(source[key], limit) if key in source else ""


def legacy_text(source, key, limit):
    value = source.get(key, "")
    if value is None or value is False or value == 0:
        value = ""
    return text(value, limit)


def normalize_phone(value):
    value = re.sub(r"[^\d+]", "", value, flags=re.ASCII)
    value = value[:1] + value[1:].replace("+", "")
    if value.startswith("00"):
        value = "+" + value[2:]
    if re.fullmatch(r"0\d{9}", value, re.ASCII):
        value = "+40" + value[1:]
    return value


def legacy_duration(source, result):
    if "duration" in source:
        result["duration"] = number(source["duration"], 5, 720, True)
    return result


def default_workspace():
    return {"salonName": "atelier", "accent": "#79536e", "defaultView": "auto", "density": "comfortable", "textSize": "normal", "showStats": True, "showIntro": True, "visibleStart": "10:00", "visibleEnd": "18:00"}


def validate_settings(raw):
    r = obj(raw)
    if r.get("slotMinutes") not in (15, 30, 60) or isinstance(r.get("slotMinutes"), bool) or r.get("theme") not in ("light", "dark"):
        fail()
    result = {"slotMinutes": r["slotMinutes"], "theme": r["theme"]}
    if "bookingEndIsLastStart" in r:
        if not isinstance(r["bookingEndIsLastStart"], bool):
            fail()
        result["bookingEndIsLastStart"] = r["bookingEndIsLastStart"]
    if "schedulePolicy" in r:
        if r["schedulePolicy"] != POLICY:
            fail()
        result["schedulePolicy"] = r["schedulePolicy"]

    def rule(source, override=False):
        source = obj(source)
        if not isinstance(source.get("closed"), bool):
            fail()
        start, end = clock(source.get("start")), clock(source.get("end"))
        if end <= start:
            fail()
        bs, be = legacy_text(source, "breakStart", 5), legacy_text(source, "breakEnd", 5)
        if bs or be:
            if not start <= clock(bs) < clock(be) <= end:
                fail()
        row = {"closed": source["closed"], "start": source["start"], "end": source["end"], "breakStart": bs, "breakEnd": be}
        if override:
            row.update(id=identifier(source.get("id")), date=date(source.get("date")))
        else:
            row["day"] = number(source.get("day"), 0, 6, True)
        return row

    schedule = [rule(x) for x in array(r.get("schedule"), 7)]
    if len(schedule) != 7 or len({x["day"] for x in schedule}) != 7:
        fail()
    overrides = [rule(x, True) for x in array(r.get("overrides"), 3000)]
    unique(overrides)
    unique(overrides, "date")
    result.update(schedule=schedule, overrides=overrides)
    w = default_workspace() if "workspace" not in r else obj(r["workspace"])
    if w.get("defaultView") not in ("auto", "day", "week", "month") or w.get("density") not in ("compact", "comfortable", "large") or w.get("textSize") not in ("normal", "large") or not isinstance(w.get("showStats"), bool) or not isinstance(w.get("showIntro"), bool):
        fail()
    if clock(w.get("visibleEnd")) <= clock(w.get("visibleStart")):
        fail()
    name = text(w.get("salonName"), 60).strip(JS_WHITESPACE)
    if not name:
        fail()
    result["workspace"] = {"salonName": name, "accent": color(w.get("accent")), **{k: w[k] for k in ("defaultView", "density", "textSize", "showStats", "showIntro", "visibleStart", "visibleEnd")}}
    if "agendaRanges" in r:
        ranges = []
        for x in array(r["agendaRanges"], 1000):
            x = obj(x)
            if clock(x.get("lastStart")) < clock(x.get("start")):
                fail()
            ranges.append({"id": identifier(x.get("id")), "effectiveFrom": date(x.get("effectiveFrom")), "start": x["start"], "lastStart": x["lastStart"], "createdAt": timestamp(x.get("createdAt"))})
        unique(ranges)
        result["agendaRanges"] = ranges
    return result


def business_snapshot(a):
    keys = ("clientId", "date", "start", "duration", "price", "status", "notes", "services")
    result = {k: copy.deepcopy(a[k]) for k in keys}
    if "color" in a:
        result["color"] = a["color"]
    return result


def semantic_equal(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(semantic_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(semantic_equal(x, y) for x, y in zip(a, b))
    return a == b


def appointment_changed(a, b):
    a, b = business_snapshot(a), business_snapshot(b)
    for snapshot in (a, b):
        if "color" in snapshot:
            snapshot["color"] = snapshot["color"].lower()
        for service in snapshot["services"]:
            service["color"] = service["color"].lower()
    return not semantic_equal(a, b)


def validate_state(raw):
    raw = obj(raw)
    if raw.get("version") != 1 or isinstance(raw.get("version"), bool):
        fail()
    settings = validate_settings(raw.get("settings"))
    clients = []
    for c in array(raw.get("clients"), 100000):
        c = obj(c)
        name = text(c.get("name"), 150).strip(JS_WHITESPACE)
        if not name:
            fail()
        clients.append({"id": identifier(c.get("id")), "name": name, "phone": normalize_phone(legacy_text(c, "phone", 40)), "notes": legacy_text(c, "notes", 5000), "archived": bool(c.get("archived", False))})
    unique(clients)
    client_ids = {c["id"] for c in clients}
    services = []
    for s in array(raw.get("services"), 10000):
        s = obj(s)
        name = text(s.get("name"), 150).strip(JS_WHITESPACE)
        if not name:
            fail()
        services.append(legacy_duration(s, {"id": identifier(s.get("id")), "name": name, "category": legacy_text(s, "category", 100), "price": number(s.get("price"), 0, 1000000), "color": color(s.get("color")), "active": bool(s.get("active", False))}))
    unique(services)

    def snapshot(a, strict=False):
        a = obj(a)
        day, start = date(a.get("date")), clock(a.get("start"))
        duration = number(a.get("duration"), 5, 720, True)
        if start + duration > 1440 or a.get("status") not in STATUSES:
            fail()
        client = identifier(a.get("clientId"))
        if client not in client_ids:
            fail("O programare face referire la un client inexistent.")
        items = []
        for s in array(a.get("services"), 100):
            s = obj(s)
            items.append(legacy_duration(s, {"serviceId": identifier(s.get("serviceId")), "name": text(s.get("name"), 150), "price": number(s.get("price"), 0, 1000000), "color": color(s.get("color"))}))
        if not items:
            fail()
        result = {"clientId": client, "date": day, "start": a["start"], "duration": duration, "price": number(a.get("price"), 0, 1000000), "status": a["status"], "notes": optional_text(a, "notes", 5000) if strict else legacy_text(a, "notes", 5000), "services": items}
        if "color" in a:
            result["color"] = color(a["color"])
        return result

    appointments = []
    for a in array(raw.get("appointments"), 200000):
        a = obj(a)
        result = {"id": identifier(a.get("id")), **snapshot(a), "createdAt": legacy_text(a, "createdAt", 50), "updatedAt": legacy_text(a, "updatedAt", 50)}
        if "corrections" in a:
            history = []
            for event in array(a["corrections"], 1000):
                event = obj(event)
                if "corrections" in obj(event.get("before")) or "corrections" in obj(event.get("after")):
                    fail()
                before, after = snapshot(event["before"], True), snapshot(event["after"], True)
                if not appointment_changed(before, after):
                    fail()
                entry = {"at": timestamp(event.get("at")), "before": before, "after": after}
                if "reason" in event:
                    entry["reason"] = text(event["reason"], 1000)
                history.append(entry)
            result["corrections"] = history
        appointments.append(result)
    unique(appointments)
    suppliers = []
    for supplier in array(raw.get("suppliers", []), 10000):
        supplier = obj(supplier)
        name = text(supplier.get("name"), 150).strip(JS_WHITESPACE)
        if not name:
            fail("Numele furnizorului este obligatoriu.")
        suppliers.append({"id": identifier(supplier.get("id")), "name": name})
    unique(suppliers)
    supplier_ids = {supplier["id"] for supplier in suppliers}
    expenses = []
    for x in array(raw.get("expenses", []), 200000):
        x = obj(x)
        amount = number(x.get("amount"), .01, 1000000)
        if abs(amount * 100 - round(amount * 100)) > .000001:
            fail()
        result = {"id": identifier(x.get("id")), "date": date(x.get("date")), "amount": amount, "description": optional_text(x, "description", 500), "createdAt": optional_text(x, "createdAt", 50), "updatedAt": optional_text(x, "updatedAt", 50)}
        if ("supplierId" in x) != ("supplierName" in x):
            fail("Furnizorul cheltuielii trebuie să conțină identificatorul și numele salvat.")
        if "supplierId" in x:
            supplier_id = identifier(x["supplierId"])
            supplier_name = text(x["supplierName"], 150).strip(JS_WHITESPACE)
            if not supplier_name:
                fail("Numele furnizorului cheltuielii este obligatoriu.")
            if supplier_id not in supplier_ids:
                fail("O cheltuială face referire la un furnizor inexistent.")
            result.update(supplierId=supplier_id, supplierName=supplier_name)
        expenses.append(result)
    unique(expenses)
    return {"version": 1, "demo": bool(raw.get("demo", False)), "settings": settings, "clients": clients, "services": services, "appointments": appointments, "expenses": expenses, "suppliers": suppliers}


def reject_overlaps(state):
    """Sweep by day; touching endpoints and cancelled records are allowed."""
    rows = sorted((a["date"], clock(a["start"]), clock(a["start"]) + a["duration"]) for a in state["appointments"] if a["status"] != "cancelled")
    day, end = None, -1
    for current, start, finish in rows:
        if current == day and start < end:
            fail("Programările se suprapun. Verifică intervalele înainte de sincronizare.")
        day, end = current, finish


def retain_audit(previous, candidate):
    """Retained records cannot lose history or bypass completed-record auditing."""
    originals = {a["id"]: a for a in previous["appointments"]}
    for after in candidate["appointments"]:
        before = originals.get(after["id"])
        if before is None:
            continue
        old_history, history = before.get("corrections", []), after.get("corrections", [])
        if len(history) < len(old_history) or not semantic_equal(history[:len(old_history)], old_history):
            fail("Istoricul corecțiilor unei programări nu poate fi înlocuit.")
        changed = appointment_changed(before, after)
        added = len(history) - len(old_history)
        if changed:
            required = before["status"] == "completed" or bool(old_history)
            if added not in (0, 1) or required and added != 1:
                fail("Modificarea programării finalizate necesită o corecție în istoric.")
            if added:
                event = history[-1]
                if not semantic_equal(event["before"], business_snapshot(before)) or not semantic_equal(event["after"], business_snapshot(after)):
                    fail("Valorile corecției nu corespund modificării programării.")
        elif added:
            fail("Istoricul corecțiilor nu poate fi modificat fără schimbarea programării.")
