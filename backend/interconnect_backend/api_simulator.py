"""Configurable simulator for backend Live experiments.

Separate from engine.py so the original Demo Lab parity model stays unchanged.
All timing values are simulated cycles, not hardware measurements.
"""

from typing import Any
from .engine import Rng, hx

CYCLES_PER_TRANSFER = 8
VALID_FAULTS = {
    "healthy",
    "bit_corruption",
    "transfer_delay",
    "dropped_transfer",
}


def run_api_simulation(
    topology: dict[str, Any],
    target_link_id: str | None,
    fault: dict[str, Any],
    seed: int,
    transfer_count: int,
    duration_cycles: int,
    faults: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:

    links = topology.get("links", [])
    if not links:
        raise ValueError("Topology must contain at least one link")

    link_ids = [str(link["id"]) for link in links]

    if len(link_ids) != len(set(link_ids)):
        raise ValueError("Topology contains duplicate link IDs")

    if not 1 <= transfer_count <= 10000:
        raise ValueError("transferCount must be between 1 and 10000")

    if not 1 <= duration_cycles <= 1_000_000:
        raise ValueError("testDurationCycles is out of range")

    # Existing single-fault API remains supported.
    if faults is None:
        if target_link_id is None:
            raise ValueError("targetLinkId is required")
        fault_configs = [{
            "targetLinkId": target_link_id,
            **fault,
        }]
    else:
        if not faults:
            raise ValueError("At least one fault configuration is required")
        fault_configs = faults

    validated_faults = {}

    for config in fault_configs:
        target = config.get("targetLinkId")

        if target not in link_ids:
            raise ValueError(
                f"Target link '{target}' does not exist"
            )

        if target in validated_faults:
            raise ValueError(f"Duplicate fault target '{target}'")

        fault_type = config.get("type", "healthy")

        if fault_type not in VALID_FAULTS:
            raise ValueError(f"Unsupported fault type '{fault_type}'")

        bit_index = int(config.get("bitIndex", 3))
        delay_cycles = int(config.get("delayCycles", 14))
        drop_every = int(config.get("dropEvery", 7))
        injection_start = int(config.get("startCycle", 0))
        injection_duration = int(
            config.get("durationCycles", duration_cycles)
        )

        if not 0 <= bit_index <= 31:
            raise ValueError("bitIndex must be between 0 and 31")

        if not 1 <= delay_cycles <= 32:
            raise ValueError("delayCycles must be between 1 and 32")

        if not 1 <= drop_every <= 10000:
            raise ValueError("dropEvery must be between 1 and 10000")

        if not 0 <= injection_start < duration_cycles:
            raise ValueError("startCycle must be inside experiment duration")

        if injection_duration < 1:
            raise ValueError("Fault duration must be at least one cycle")

        validated_faults[target] = {
            "type": fault_type,
            "bitIndex": bit_index,
            "delayCycles": delay_cycles,
            "dropEvery": drop_every,
            "startCycle": injection_start,
            "durationCycles": injection_duration,
        }

    rng = Rng(seed)
    events = []
    anomaly_counts = {link_id: 0 for link_id in link_ids}
    latency_values = []
    link_transfer_counts = {link_id: 0 for link_id in link_ids}

    executed = 0
    dropped = 0
    mismatches = 0

    max_by_duration = duration_cycles // CYCLES_PER_TRANSFER
    execution_limit = min(transfer_count, max_by_duration)

    for index in range(execution_limit):
        cycle = index * CYCLES_PER_TRANSFER

        # Distribute traffic across the saved topology.
        link = links[index % len(links)]
        link_id = str(link["id"])

        link_transfer_counts[link_id] += 1
        target_transfer_number = link_transfer_counts[link_id]

        expected = int(rng() * 4294967296)

        base_latency = 4
        latency = base_latency + int(rng() * 3)

        executed += 1

        config = validated_faults.get(link_id)

        active_fault = False
        if config:
            active_fault = (
                config["type"] != "healthy"
                and config["startCycle"]
                <= cycle
                < config["startCycle"] + config["durationCycles"]
            )

        event_type = None
        severity = "warning"
        observed = None
        is_dropped = False

        if active_fault:
            fault_type = config["type"]

            if fault_type == "bit_corruption":
                if rng() < 0.6:
                    observed = expected ^ (1 << config["bitIndex"])
                    event_type = "data_mismatch"
                    severity = "error"
                    mismatches += 1

            elif fault_type == "transfer_delay":
                latency += config["delayCycles"]

                if latency > base_latency + 4:
                    event_type = (
                        "protocol_violation" if latency > 32
                        else "latency_excursion"
                    )
                    severity = "critical" if latency > 32 else "warning"

            elif fault_type == "dropped_transfer":
                if target_transfer_number % config["dropEvery"] == 0:
                    is_dropped = True
                    event_type = "dropped_transfer"
                    severity = "error"
                    dropped += 1

        if not is_dropped:
            latency_values.append(latency)

        if event_type:
            anomaly_counts[link_id] += 1
            events.append({
                "cyc": cycle,
                "link": link_id,
                "type": event_type,
                "sev": severity,
                "exp": hx(expected),
                "obs": "—" if observed is None else hx(observed),
                "lat": None if is_dropped else latency,
            })

    # Localize only from observed anomaly evidence.
    localized_links = [
        link_id
        for link_id, count in anomaly_counts.items()
        if count > 0
    ]

    injected_links = [
        link_id
        for link_id, config in validated_faults.items()
        if config["type"] != "healthy"
    ]

    localized_set = set(localized_links)
    injected_set = set(injected_links)

    return {
        "transfersExecuted": executed,
        "requestedTransfers": transfer_count,
        "durationCycles": duration_cycles,
        "mismatches": mismatches,
        "droppedTransfers": dropped,
        "localizedLinkId": (
            localized_links[0] if len(localized_links) == 1 else None
        ),
        "localizedLinkIds": localized_links,
        "injectedLinkId": (
            injected_links[0] if len(injected_links) == 1 else None
        ),
        "injectedLinkIds": injected_links,
        "localizationCorrect": (
            localized_set == injected_set if injected_set else None
        ),
        "anomalyCountsByLink": anomaly_counts,
        "transfersByLink": link_transfer_counts,
        "avgLatencyCycles": (
            sum(latency_values) / len(latency_values)
            if latency_values else None
        ),
        "maxLatencyCycles": (
            max(latency_values) if latency_values else None
        ),
        "events": events,
    }
