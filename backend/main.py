from __future__ import annotations

import json
import os
import sqlite3
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from interconnect_backend.api_simulator import run_api_simulation

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = BASE_DIR / "data"
DB_PATH = Path(
    os.environ.get(
        "INTERCONNECTSENTRY_DB_PATH",
        str(DEFAULT_DATA_DIR / "api_state.sqlite3"),
    )
).expanduser()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="InterconnectSentry API", version="1.0.0")
CORS_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        "INTERCONNECTSENTRY_CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173,http://127.0.0.1:4173",
    ).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def connect_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    return db

def init_db() -> None:
    with connect_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS frontend_experiments (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL,
                input_json TEXT NOT NULL,
                record_json TEXT NOT NULL,
                events_json TEXT NOT NULL DEFAULT '[]'
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL
            )
        """)

init_db()

def default_topology() -> dict[str, Any]:
    endpoints = [
        {"id": "A", "name": "Chiplet A", "x": 100, "y": 120},
        {"id": "B", "name": "Chiplet B", "x": 300, "y": 120},
        {"id": "C", "name": "Chiplet C", "x": 100, "y": 280},
        {"id": "D", "name": "Chiplet D", "x": 300, "y": 280},
    ]
    pairs = [("A", "B"), ("A", "C"), ("A", "D"), ("B", "C"), ("B", "D"), ("C", "D")]
    links = [
        {"id": f"L{i + 1}", "source": source, "destination": destination, "widthBits": 32, "clockMHz": 500}
        for i, (source, destination) in enumerate(pairs)
    ]
    return {"schemaVersion": 1, "source": "backend", "endpoints": endpoints, "links": links}

def read_topology() -> dict[str, Any]:
    with connect_db() as db:
        row = db.execute("SELECT value_json FROM app_settings WHERE key='topology'").fetchone()
    return json.loads(row["value_json"]) if row else default_topology()

def write_topology(topology: dict[str, Any]) -> dict[str, Any]:
    topology = dict(topology)
    topology["source"] = "backend"
    with connect_db() as db:
        db.execute(
            "INSERT INTO app_settings(key,value_json) VALUES('topology',?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
            (json.dumps(topology),),
        )
    return topology

def get_record(experiment_id: str) -> dict[str, Any] | None:
    with connect_db() as db:
        row = db.execute(
            "SELECT record_json FROM frontend_experiments WHERE id=?", (experiment_id,)
        ).fetchone()
    return json.loads(row["record_json"]) if row else None

def save_record(record: dict[str, Any], events: list[dict[str, Any]] | None = None) -> None:
    with connect_db() as db:
        db.execute(
            """INSERT INTO frontend_experiments(id,created_at,status,input_json,record_json,events_json)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET status=excluded.status,
                 record_json=excluded.record_json, events_json=excluded.events_json""",
            (
                record["id"], record["createdAt"], record["status"],
                json.dumps(record["input"]), json.dumps(record),
                json.dumps(events if events is not None else []),
            ),
        )

def get_saved_events(experiment_id: str) -> list[dict[str, Any]]:
    with connect_db() as db:
        row = db.execute(
            "SELECT events_json FROM frontend_experiments WHERE id=?", (experiment_id,)
        ).fetchone()
    return json.loads(row["events_json"]) if row else []

def as_text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)

class TopologyPayload(BaseModel):
    schemaVersion: int = 1
    source: str = "backend"
    endpoints: list[dict[str, Any]]
    links: list[dict[str, Any]]

class HardwareStartPayload(BaseModel):
    workload: int = Field(default=100, ge=1, le=65535)
    target_link: int = Field(default=0, ge=0, le=15)
    fault_type: int = Field(default=0, ge=0, le=3)
    fault_enable: bool = False
    fault_mask: int = Field(default=0, ge=0, le=0xFFFFFFFF)
    delay_cycles: int = Field(default=0, ge=0, le=255)
    drop_interval: int = Field(default=0, ge=0, le=255)


class ExperimentPayload(BaseModel):
    name: str
    targetLinkId: str | None = None
    fault: dict[str, Any] = {}
    faults: list[dict[str, Any]] | None = None
    seed: int
    transferCount: int
    testDurationCycles: int
    notes: str | None = None

FPGA_BRIDGE_URL = os.environ.get(
    "INTERCONNECTSENTRY_FPGA_BRIDGE_URL",
    "http://127.0.0.1:8765",
).rstrip("/")

CF_ACCESS_CLIENT_ID = os.environ.get("CF_ACCESS_CLIENT_ID", "")
CF_ACCESS_CLIENT_SECRET = os.environ.get("CF_ACCESS_CLIENT_SECRET", "")


def call_fpga_bridge(path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        FPGA_BRIDGE_URL + path,
        data=body,
        headers={
            "Content-Type": "application/json",
            **(
                {
                    "CF-Access-Client-Id": CF_ACCESS_CLIENT_ID,
                    "CF-Access-Client-Secret": CF_ACCESS_CLIENT_SECRET,
                }
                if CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET
                else {}
            ),
        },
        method="POST" if payload is not None else "GET",
    )
    try:
        with urlopen(request, timeout=12) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail=f"FPGA bridge unavailable: {exc}",
        ) from exc

    if not isinstance(result, dict) or not result.get("ok"):
        detail = result.get("error", "FPGA bridge returned an unsuccessful response") if isinstance(result, dict) else "Invalid FPGA bridge response"
        raise HTTPException(status_code=503, detail=detail)

    return {
        **result,
        "source": "hardware",
        "device": "PYNQ-Z2",
        "timestamp": now_iso(),
    }


@app.get("/api/v1/hardware/status")
def hardware_status() -> dict[str, Any]:
    return call_fpga_bridge("/api/status")


@app.post("/api/v1/hardware/start")
def hardware_start(payload: HardwareStartPayload) -> dict[str, Any]:
    return call_fpga_bridge("/api/start", payload.model_dump())


@app.post("/api/v1/hardware/stop")
def hardware_stop() -> dict[str, Any]:
    return call_fpga_bridge("/api/stop", {})


@app.get("/api/v1/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": app.version}

@app.get("/api/v1/topology")
def get_topology() -> dict[str, Any]:
    return read_topology()

@app.put("/api/v1/topology")
def put_topology(payload: TopologyPayload) -> dict[str, Any]:
    # The frontend validates topology details with Zod; keep the API defensive too.
    topology = payload.model_dump()
    topology["source"] = "backend"
    endpoint_ids = [endpoint.get("id") for endpoint in topology["endpoints"]]
    if len(endpoint_ids) != len(set(endpoint_ids)):
        raise HTTPException(status_code=422, detail="Endpoint IDs must be unique")
    endpoint_set = set(endpoint_ids)
    link_ids = [link.get("id") for link in topology["links"]]
    if len(link_ids) != len(set(link_ids)):
        raise HTTPException(status_code=422, detail="Link IDs must be unique")
    for link in topology["links"]:
        if link.get("source") not in endpoint_set or link.get("destination") not in endpoint_set:
            raise HTTPException(status_code=422, detail="Every link must reference existing endpoints")
        if link.get("source") == link.get("destination"):
            raise HTTPException(status_code=422, detail="A link cannot connect an endpoint to itself")
    return write_topology(topology)

@app.get("/api/v1/experiments")
def list_experiments() -> list[dict[str, Any]]:
    with connect_db() as db:
        rows = db.execute(
            "SELECT record_json FROM frontend_experiments ORDER BY created_at DESC"
        ).fetchall()
    return [json.loads(row["record_json"]) for row in rows]

@app.post("/api/v1/experiments")
def create_experiment(payload: ExperimentPayload) -> dict[str, Any]:
    topology = read_topology()
    valid_link_ids = {link["id"] for link in topology["links"]}

    if payload.faults is None:
        if not payload.targetLinkId or payload.targetLinkId not in valid_link_ids:
            raise HTTPException(
                status_code=422,
                detail="targetLinkId does not exist in the saved topology"
            )
    else:
        if not payload.faults:
            raise HTTPException(
                status_code=422,
                detail="faults must contain at least one fault configuration"
            )

        fault_targets = [item.get("targetLinkId") for item in payload.faults]

        if any(target not in valid_link_ids for target in fault_targets):
            raise HTTPException(
                status_code=422,
                detail="Every fault target must exist in the saved topology"
            )

        if len(fault_targets) != len(set(fault_targets)):
            raise HTTPException(
                status_code=422,
                detail="Each link can have only one fault configuration per experiment"
            )
    if payload.transferCount < 1 or payload.transferCount > 10000:
        raise HTTPException(status_code=422, detail="transferCount must be between 1 and 10000")
    if payload.testDurationCycles < 1 or payload.testDurationCycles > 1_000_000:
        raise HTTPException(status_code=422, detail="testDurationCycles is out of range")
    if payload.seed < 0 or payload.seed > 0xFFFFFFFF:
        raise HTTPException(status_code=422, detail="seed is out of range")

    experiment_id = f"api-exp-{uuid4().hex[:12]}"
    record = {
        "schemaVersion": 1,
        "id": experiment_id,
        "source": "backend",
        "createdAt": now_iso(),
        "status": "ready",
        "input": payload.model_dump(),
        "outcome": None,
    }
    save_record(record, [])
    return record

def choose_engine_scenario(fault: dict[str, Any]) -> str:
    # The current Python engine exposes six preset scenarios rather than arbitrary fault injection.
    fault_type = fault.get("type")
    return {
        "bit_corruption": "bit",
        "transfer_delay": "delay",
        "dropped_transfer": "drop",
    }.get(fault_type, "healthy")

def convert_engine_events(experiment_id: str, engine_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for sequence, event in enumerate(engine_events):
        event_type = event.get("type", "protocol_violation")
        if event_type == "latency_excursion":
            event_type = "latency_sample"
        if event_type not in {"data_mismatch", "dropped_transfer", "latency_sample", "protocol_violation"}:
            event_type = "protocol_violation"
        severity = event.get("sev", "warning")
        if severity == "critical":
            severity = "error"
        if severity not in {"info", "warning", "error"}:
            severity = "warning"
        converted.append({
            "id": str(event.get("id", f"{experiment_id}-event-{sequence}")),
            "experimentId": experiment_id,
            "source": "backend",
            "sequence": sequence,
            "cycle": max(0, int(event.get("cyc") or 0)),
            "hardwareCycle": None,
            "linkId": str(event.get("link") or "L1"),
            "type": event_type,
            "severity": severity,
            "expected": as_text(event.get("exp")),
            "observed": as_text(event.get("obs")),
            "latencyCycles": max(0, float(event["lat"])) if event.get("lat") is not None else None,
        })
    return converted

@app.post("/api/v1/experiments/{experiment_id}/run")
def run_experiment(experiment_id: str) -> dict[str, Any]:
    record = get_record(experiment_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Experiment not found")
    if record["status"] == "cancelled":
        return record
    if record["status"] == "completed":
        return record

    input_data = record["input"]
    fault = input_data.get("fault", {})

    try:
        engine_result = run_api_simulation(
            topology=read_topology(),
            target_link_id=input_data.get("targetLinkId"),
            fault=fault,
            seed=int(input_data["seed"]),
            transfer_count=int(input_data["transferCount"]),
            duration_cycles=int(input_data["testDurationCycles"]),
            faults=input_data.get("faults"),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Simulation failed: {exc}"
        ) from exc

    events = convert_engine_events(
        experiment_id,
        engine_result["events"]
    )

    record["status"] = "completed"
    record["outcome"] = {
        "localizedLinkId": engine_result["localizedLinkId"],
        "localizedLinkIds": engine_result["localizedLinkIds"],
        "injectedLinkId": engine_result["injectedLinkId"],
        "injectedLinkIds": engine_result["injectedLinkIds"],
        "localizationCorrect": engine_result["localizationCorrect"],
        "anomalyCountsByLink": engine_result["anomalyCountsByLink"],
        "transfersByLink": engine_result["transfersByLink"],
        "transfers": engine_result["transfersExecuted"],
        "requestedTransfers": engine_result["requestedTransfers"],
        "durationCycles": engine_result["durationCycles"],
        "mismatches": engine_result["mismatches"],
        "droppedTransfers": engine_result["droppedTransfers"],
        "avgLatencyCycles": engine_result["avgLatencyCycles"],
        "maxLatencyCycles": engine_result["maxLatencyCycles"],
    }

    save_record(record, events)
    return record

@app.post("/api/v1/experiments/{experiment_id}/cancel")
def cancel_experiment(experiment_id: str) -> dict[str, Any]:
    record = get_record(experiment_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Experiment not found")
    if record["status"] not in {"completed", "failed"}:
        record["status"] = "cancelled"
        save_record(record, get_saved_events(experiment_id))
    return record

@app.get("/api/v1/experiments/{experiment_id}/events")
def experiment_events(experiment_id: str) -> list[dict[str, Any]]:
    if get_record(experiment_id) is None:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return get_saved_events(experiment_id)
