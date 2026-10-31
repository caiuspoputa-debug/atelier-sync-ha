#!/usr/bin/env python3
"""Atelier Sync: independent persistent coordinator for phones and tablets.

Only Python's standard library is required. No Home Assistant API is accessed.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from validation import (ValidationError, identifier, obj, text, timestamp,
                        reject_overlaps, retain_audit, semantic_equal,
                        validate_client_transitions, validate_state)

PROTOCOL = 1
CAPABILITIES = ("suppliers-v1", "client-deletion-v1")
MAX_BODY = 32 * 1024 * 1024
MAX_STATE = 30 * 1024 * 1024
MAX_OPERATIONS = 50
MAX_REVISION = 9007199254740991
ENTITY_TYPES = ("clients", "services", "appointments", "expenses", "suppliers")
LOGGER = logging.getLogger("atelier_sync")


class APIError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def payload_hash(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def ensure_state_size(state):
    if len(canonical(state).encode("utf-8")) > MAX_STATE:
        raise ValidationError("Agenda depășește limita de 30 MiB a acestei versiuni. Datele anterioare au fost păstrate.")


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def flatten(state):
    result = {"settings": copy.deepcopy(state["settings"]), "meta": {"demo": state["demo"]}}
    for kind in ENTITY_TYPES:
        for record in state.get(kind, []):
            result[kind + "/" + record["id"]] = copy.deepcopy(record)
    return result


def unflatten(entities):
    if "settings" not in entities or "meta" not in entities:
        raise ValidationError("Setările și informațiile agendei nu pot fi șterse.")
    meta = obj(entities["meta"])
    if set(meta) != {"demo"} or not isinstance(meta["demo"], bool):
        raise ValidationError("Informațiile agendei nu sunt valide.")
    result = {"version": 1, "demo": meta["demo"], "settings": copy.deepcopy(entities["settings"]), **{kind: [] for kind in ENTITY_TYPES}}
    for key, value in entities.items():
        if key in ("settings", "meta"):
            continue
        kind, sep, record_id = key.partition("/")
        if not sep or kind not in ENTITY_TYPES or not record_id or not isinstance(value, dict) or value.get("id") != record_id:
            raise ValidationError("Identificatorul înregistrării nu corespunde cheii operației.")
        result[kind].append(copy.deepcopy(value))
    return result


class Store:
    def __init__(self, database, pairing_key):
        if not isinstance(pairing_key, str) or len(pairing_key) < 16:
            raise ValueError("Cheia de asociere trebuie să aibă minimum 16 caractere.")
        self.database = str(database)
        self.pairing_key = pairing_key
        Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS devices(
                    device_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
                    token_nonce TEXT NOT NULL, role TEXT NOT NULL,
                    name TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS entities(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS receipts(
                    operation_id TEXT PRIMARY KEY, device_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL);
            """)
            db.execute("BEGIN IMMEDIATE")
            for key, value in (("workspace_id", secrets.token_urlsafe(24)), ("auth_secret", secrets.token_hex(32)), ("initialized", "0"), ("revision", "0")):
                db.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES(?,?)", (key, value))
            db.commit()
        try:
            os.chmod(self.database, 0o600)
        except OSError:
            pass

    @contextlib.contextmanager
    def connection(self):
        db = sqlite3.connect(self.database, timeout=20, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA busy_timeout=20000")
        try:
            yield db
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _meta(db, key):
        return db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()[0]

    @staticmethod
    def _set_meta(db, key, value):
        db.execute("INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def status(self):
        with self.connection() as db:
            return {"protocol": PROTOCOL, "capabilities": list(CAPABILITIES), "workspaceId": self._meta(db, "workspace_id"), "initialized": self._meta(db, "initialized") == "1"}

    def register(self, body):
        try:
            body = obj(body)
            device_id = identifier(body.get("deviceId"))
            name = text(body.get("name"), 150).strip()
            role = body.get("role")
            if role not in ("master", "secondary") or not name:
                raise ValidationError()
        except (ValidationError, TypeError):
            raise APIError(400, "invalid_registration", "Datele dispozitivului nu sunt valide.")
        supplied = body.get("pairingKey")
        if not isinstance(supplied, str) or not hmac.compare_digest(supplied.encode("utf-8"), self.pairing_key.encode("utf-8")):
            raise APIError(401, "invalid_pairing_key", "Cheia de asociere nu este corectă.")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
            secret = bytes.fromhex(self._meta(db, "auth_secret"))
            nonce = existing["token_nonce"] if existing else secrets.token_hex(16)
            token = hmac.new(secret, (device_id + "\0" + nonce).encode("utf-8"), hashlib.sha256).hexdigest()
            digest = hashlib.sha256(token.encode("ascii")).hexdigest()
            db.execute("INSERT INTO devices(device_id,token_hash,token_nonce,role,name,created_at) VALUES(?,?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET name=excluded.name,role=excluded.role", (device_id, digest, nonce, role, name, utc_now()))
            result = {"protocol": PROTOCOL, "capabilities": list(CAPABILITIES), "workspaceId": self._meta(db, "workspace_id"), "token": token, "initialized": self._meta(db, "initialized") == "1"}
            db.commit()
            return result

    def _authenticate(self, db, token, device_id):
        if not isinstance(token, str) or len(token) != 64:
            raise APIError(401, "unauthorized", "Dispozitivul nu este autentificat.")
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        device = db.execute("SELECT * FROM devices WHERE token_hash=?", (digest,)).fetchone()
        if device is None:
            raise APIError(401, "unauthorized", "Dispozitivul nu este autentificat.")
        if device["device_id"] != device_id:
            raise APIError(403, "device_mismatch", "Tokenul nu aparține acestui dispozitiv.")
        return device

    @staticmethod
    def _entities(db):
        return {row["key"]: json.loads(row["value"]) for row in db.execute("SELECT key,value FROM entities ORDER BY rowid")}

    @staticmethod
    def _write_entities(db, entities):
        old = {row["key"]: row["value"] for row in db.execute("SELECT key,value FROM entities")}
        for key in old.keys() - entities.keys():
            db.execute("DELETE FROM entities WHERE key=?", (key,))
        for key, value in entities.items():
            encoded = canonical(value)
            if old.get(key) != encoded:
                db.execute("INSERT INTO entities(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, encoded))

    def _bootstrap(self, db, device, body):
        if "bootstrap" not in body:
            return
        try:
            bootstrap_id = identifier(body.get("bootstrapId"))
            digest = payload_hash(body["bootstrap"])
        except (ValidationError, TypeError, ValueError):
            raise APIError(400, "invalid_bootstrap", "Inițializarea agendei nu este validă.")
        if self._meta(db, "initialized") == "1":
            old_id = db.execute("SELECT value FROM metadata WHERE key='bootstrap_id'").fetchone()
            if old_id and old_id[0] == bootstrap_id:
                same = self._meta(db, "bootstrap_hash") == digest and self._meta(db, "bootstrap_device") == device["device_id"]
                if same:
                    return
                raise APIError(409, "bootstrap_id_reused", "Identificatorul inițializării a fost deja folosit cu alte date.")
            raise APIError(409, "already_initialized", "Serverul are deja o agendă. Inițializarea nu o poate înlocui.")
        if device["role"] != "master":
            raise APIError(403, "master_required", "Prima inițializare trebuie făcută de pe tabletă, cu rol principal.")
        try:
            state = validate_state(body["bootstrap"])
            ensure_state_size(state)
            reject_overlaps(state)
        except (ValidationError, TypeError, KeyError, ValueError) as exc:
            raise APIError(400, "invalid_bootstrap", str(exc) if isinstance(exc, ValidationError) else "Agenda pentru inițializare nu este validă.")
        self._write_entities(db, flatten(state))
        for key, value in (("initialized", "1"), ("revision", "1"), ("bootstrap_id", bootstrap_id), ("bootstrap_hash", digest), ("bootstrap_device", device["device_id"])):
            self._set_meta(db, key, value)

    def _operation(self, db, device_id, operation, live):
        try:
            operation = obj(operation)
            operation_id = identifier(operation.get("id"))
            digest = payload_hash(operation)
        except (ValidationError, TypeError, ValueError):
            raise APIError(400, "invalid_operation", "O operație nu are un identificator sau un conținut valid.")
        receipt = db.execute("SELECT * FROM receipts WHERE operation_id=?", (operation_id,)).fetchone()
        if receipt:
            if receipt["payload_hash"] == digest and receipt["device_id"] == device_id:
                return json.loads(receipt["result"]), live
            return {"id": operation_id, "status": "rejected", "reason": "Identificatorul operației a fost deja folosit cu alte date sau pe alt dispozitiv.", "keys": []}, live

        keys = []
        result = {"id": operation_id, "status": "rejected", "reason": "Operația nu este validă.", "keys": keys}
        next_live = live
        try:
            timestamp(operation.get("createdAt"))
            changes = operation.get("changes")
            if not isinstance(changes, list) or not 1 <= len(changes) <= 500000:
                raise ValidationError("Operația trebuie să conțină modificări de înregistrări.")
            for change in changes:
                change = obj(change)
                key = text(change.get("key"), 120)
                kind, sep, record_id = key.partition("/")
                if key not in ("settings", "meta") and (kind not in ENTITY_TYPES or not sep or not record_id):
                    raise ValidationError("Tipul înregistrării nu este acceptat pentru sincronizare.")
                if key in keys or "before" not in change or "after" not in change or any(value is not None and not isinstance(value, dict) for value in (change["before"], change["after"])):
                    raise ValidationError("Operația conține modificări duplicate sau invalide.")
                keys.append(key)
                if key in ("settings", "meta") and change["after"] is None:
                    raise ValidationError("Setările și informațiile agendei nu pot fi șterse.")
            mismatches = [c["key"] for c in changes if not semantic_equal(c["before"], live.get(c["key"]))]
            if mismatches:
                result.update(status="conflict", reason="Datele au fost modificate pe alt dispozitiv. Verifică diferențele înainte de a continua.")
            else:
                candidate = copy.deepcopy(live)
                for change in changes:
                    if change["after"] is None:
                        candidate.pop(change["key"], None)
                    else:
                        candidate[change["key"]] = copy.deepcopy(change["after"])
                previous = validate_state(unflatten(live))
                state = validate_state(unflatten(candidate))
                validate_client_transitions(previous, state)
                ensure_state_size(state)
                retain_audit(previous, state)
                try:
                    reject_overlaps(state)
                except ValidationError as exc:
                    result.update(status="conflict", reason=str(exc))
                else:
                    revision = int(self._meta(db, "revision"))
                    if revision >= MAX_REVISION:
                        raise ValidationError("Limita reviziilor serverului a fost atinsă.")
                    next_live = flatten(state)
                    self._write_entities(db, next_live)
                    self._set_meta(db, "revision", revision + 1)
                    result.update(status="applied", reason="Modificările au fost salvate pe server.")
        except (ValidationError, TypeError, KeyError, ValueError) as exc:
            result.update(status="rejected", reason=str(exc) if isinstance(exc, ValidationError) else "Datele operației nu respectă formatul agendei.")
        db.execute("INSERT INTO receipts(operation_id,device_id,payload_hash,result,created_at) VALUES(?,?,?,?,?)", (operation_id, device_id, digest, canonical(result), utc_now()))
        return result, next_live

    def sync(self, token, body):
        try:
            body = obj(body)
            device_id = identifier(body.get("deviceId"))
            workspace = identifier(body.get("workspaceId"))
            operations = body.get("operations")
            if not isinstance(operations, list) or len(operations) > MAX_OPERATIONS:
                raise ValidationError()
        except (ValidationError, TypeError):
            raise APIError(400, "invalid_request", "Cererea de sincronizare nu este validă sau depășește 50 de operații.")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            device = self._authenticate(db, token, device_id)
            if workspace != self._meta(db, "workspace_id"):
                raise APIError(409, "workspace_mismatch", "Acest server are o altă agendă. Verifică asocierea dispozitivului.")
            self._bootstrap(db, device, body)
            if self._meta(db, "initialized") != "1":
                raise APIError(409, "needs_initialization", "Agenda trebuie inițializată mai întâi de pe tabletă.")
            live = self._entities(db)
            results = []
            for operation in operations:
                result, live = self._operation(db, device_id, operation, live)
                results.append(result)
            response = {"protocol": PROTOCOL, "capabilities": list(CAPABILITIES), "workspaceId": workspace, "initialized": True, "serverRevision": int(self._meta(db, "revision")), "results": results, "state": validate_state(unflatten(live))}
            db.commit()
            return response

    def revoke_device(self, device_id):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            removed = db.execute("DELETE FROM devices WHERE device_id=?", (device_id,)).rowcount
            db.commit()
            return bool(removed)

    def backup(self, destination):
        """SQLite backup API includes WAL safely; do not copy a live .sqlite file."""
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(destination)
        try:
            with self.connection() as source:
                source.backup(target)
        finally:
            target.close()
        try:
            os.chmod(destination, 0o600)
        except OSError:
            pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "AtelierSync/0.3.0"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, _format, *args):
        # URLs, bodies, pairing keys, bearer tokens and client data are never logged.
        pass

    def _send(self, status, value, content_type="application/json; charset=utf-8"):
        payload = (canonical(value) if isinstance(value, (dict, list)) else value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if urlsplit(self.path).path != "/":
            self._send(404, {"protocol": 1, "code": "not_found", "error": "Adresa nu există."})
            return
        self._send(200, "<!doctype html><html lang='ro'><meta charset='utf-8'><meta name='viewport' content='width=device-width'><title>Atelier Sync</title><h1>Atelier Sync</h1><p>Serviciul de sincronizare funcționează. Asociază dispozitivele din aplicația Atelier.</p><p>Această pagină nu afișează datele agendei.</p></html>", "text/html; charset=utf-8")

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            raise APIError(400, "invalid_body", "Formatul cererii nu este acceptat.")
        if self.headers.get_content_type() != "application/json":
            raise APIError(415, "content_type", "Cererea trebuie să folosească formatul JSON.")
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if length < 0:
            raise APIError(411, "length_required", "Lungimea cererii este obligatorie.")
        if length > MAX_BODY:
            raise APIError(413, "body_too_large", "Cererea depășește limita de 32 MiB.")
        data = self.rfile.read(length)
        if len(data) != length:
            raise APIError(400, "invalid_body", "Cererea este incompletă.")
        try:
            def invalid_constant(_value):
                raise ValueError("nonfinite")
            return json.loads(data.decode("utf-8"), parse_constant=invalid_constant)
        except (UnicodeError, ValueError, RecursionError):
            raise APIError(400, "invalid_json", "Conținutul JSON nu este valid.")

    def do_POST(self):
        try:
            body = self._body()
            route = urlsplit(self.path).path
            store = self.server.store
            if route == "/v1/status":
                result = store.status()
            elif route == "/v1/register":
                result = store.register(body)
            elif route == "/v1/sync":
                auth = self.headers.get("Authorization", "")
                token = auth[7:] if auth.startswith("Bearer ") else ""
                result = store.sync(token, body)
            else:
                raise APIError(404, "not_found", "Adresa nu există.")
            self._send(200, result)
        except APIError as exc:
            self._send(exc.status, {"protocol": PROTOCOL, "code": exc.code, "error": exc.message})
        except (ConnectionError, TimeoutError):
            return
        except Exception:
            # Do not persist a receipt for a transient database/server failure.
            LOGGER.error("Cererea nu a putut fi procesată; datele neconfirmate pot fi retrimise.")
            self._send(500, {"protocol": PROTOCOL, "code": "server_error", "error": "Serverul nu a putut salva cererea. Reîncearcă sincronizarea."})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address, store):
        self.store = store
        super().__init__(address, Handler)


def runtime_options(options, pairing_key_override=None):
    if not isinstance(options, dict):
        raise ValueError("Configurația aplicației nu este validă.")
    key = pairing_key_override if pairing_key_override is not None else options.get("pairing_key")
    if not isinstance(key, str) or len(key) < 16:
        raise ValueError("Configurează pairing_key cu minimum 16 caractere înainte de pornire.")
    interval = options.get("backup_interval_hours", 6)
    retention = options.get("backup_retention_days", 14)
    if type(interval) is not int or not 1 <= interval <= 24 or type(retention) is not int or not 1 <= retention <= 90:
        raise ValueError("Intervalul copiilor trebuie să fie 1–24 ore, iar păstrarea 1–90 zile.")
    return {"pairing_key": key, "backup_interval_hours": interval, "backup_retention_days": retention}


def automatic_backups(store, stop, directory, interval_hours=6, retention_days=14):
    directory = Path(directory)
    while not stop.is_set():
        try:
            today = dt.datetime.now(dt.timezone.utc).date()
            destination = directory / ("atelier-sync-" + today.isoformat() + ".sqlite3")
            store.backup(destination)
            cutoff = today - dt.timedelta(days=retention_days - 1)
            for old in directory.glob("atelier-sync-????-??-??.sqlite3"):
                try:
                    day = dt.date.fromisoformat(old.name[len("atelier-sync-"):-len(".sqlite3")])
                except ValueError:
                    continue
                if day < cutoff:
                    old.unlink()
        except Exception:
            LOGGER.error("Copia automată nu a putut fi creată. Verifică spațiul din stocarea aplicației.")
        stop.wait(interval_hours * 60 * 60)


def main():
    parser = argparse.ArgumentParser(description="Atelier Sync")
    parser.add_argument("--database", default=os.environ.get("ATELIER_DATABASE", "/data/atelier-sync.sqlite3"))
    parser.add_argument("--host", default=os.environ.get("ATELIER_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("ATELIER_PORT", "8787")))
    parser.add_argument("--options", default="/data/options.json")
    parser.add_argument("--backup")
    parser.add_argument("--revoke-device")
    args = parser.parse_args()
    try:
        options = {}
        if Path(args.options).is_file():
            with open(args.options, encoding="utf-8") as handle:
                options = json.load(handle)
        options = runtime_options(options, os.environ.get("ATELIER_PAIRING_KEY"))
        store = Store(args.database, options["pairing_key"])
    except (ValueError, TypeError, OSError) as exc:
        raise SystemExit(str(exc) if isinstance(exc, ValueError) else "Configurația aplicației nu a putut fi citită.")
    if args.backup:
        store.backup(args.backup)
        return
    if args.revoke_device:
        store.revoke_device(args.revoke_device)
        return
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    LOGGER.info("Atelier Sync pornește pe portul %d. Datele sunt păstrate local în baza persistentă.", args.port)
    stop = threading.Event()
    backup_thread = threading.Thread(target=automatic_backups, args=(store, stop, Path(args.database).parent / "backups", options["backup_interval_hours"], options["backup_retention_days"]), daemon=True)
    backup_thread.start()
    server = Server((args.host, args.port), store)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        backup_thread.join(timeout=5)
        server.server_close()


if __name__ == "__main__":
    main()
