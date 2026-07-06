"""Data loading and aggregation helpers for the Duck message dashboard."""

from __future__ import annotations

import ast
import io
import json
from dataclasses import dataclass

import pandas as pd


REQUIRED_COLUMNS = {"timestamp", "eventType"}

# Canonical names used by the dashboard and aliases used by OWL's CSV export UI.
COLUMN_ALIASES = {
    "timestamp": ("Date",),
    "eventType": ("Event Type",),
    "DeviceID": ("Device ID",),
    "receiverDeviceID": ("Gateway ID",),
    "gatewayName": ("Gateway Name",),
    "MessageID": ("Message ID",),
    "payload": ("Payload",),
    "hops": ("# of Hops",),
}


class CsvValidationError(ValueError):
    """Raised when an uploaded CSV cannot support the dashboard."""


@dataclass(frozen=True)
class LoadResult:
    data: pd.DataFrame
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    invalid_timestamp_rows: int
    missing_device_rows: int


def _payload_dict(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if pd.isna(value) or not isinstance(value, str) or not value.strip():
        return {}

    for parser in (json.loads, ast.literal_eval):
        try:
            parsed = parser(value)
        except (ValueError, SyntaxError, TypeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _counter_from_payload(value: object) -> object:
    payload = _payload_dict(value)
    for key in ("C", "c"):
        if key in payload:
            return payload[key]

    # API-style exports wrap the device payload in an outer payload object.
    inner = _payload_dict(payload.get("Payload"))
    for key in ("C", "c"):
        if key in inner:
            return inner[key]
    return pd.NA


def load_csv(csv_bytes: bytes) -> LoadResult:
    """Load a DMS CSV and normalize its message identity and timestamps."""
    try:
        frame = pd.read_csv(io.BytesIO(csv_bytes))
    except (pd.errors.ParserError, UnicodeDecodeError, ValueError) as exc:
        raise CsvValidationError(f"The file could not be read as CSV: {exc}") from exc

    if frame.empty:
        raise CsvValidationError("The CSV contains headers but no message rows.")

    frame.columns = [str(column).strip() for column in frame.columns]
    rename_map: dict[str, str] = {}
    for canonical, aliases in COLUMN_ALIASES.items():
        if canonical in frame.columns:
            continue
        alias = next((candidate for candidate in aliases if candidate in frame.columns), None)
        if alias is not None:
            rename_map[alias] = canonical
    frame = frame.rename(columns=rename_map)

    missing = sorted(REQUIRED_COLUMNS - set(frame.columns))
    if missing:
        raise CsvValidationError(
            "Missing required column(s): " + ", ".join(missing)
        )

    normalized = frame.copy()
    parsed_timestamps = pd.to_datetime(
        normalized["timestamp"], errors="coerce", utc=True, format="mixed"
    )
    invalid_timestamp_rows = int(parsed_timestamps.isna().sum())
    normalized["timestamp"] = parsed_timestamps
    valid_timestamps = parsed_timestamps.dropna()
    if valid_timestamps.empty:
        raise CsvValidationError("The CSV does not contain any valid timestamps.")
    test_start = valid_timestamps.min()
    test_end = valid_timestamps.max()

    if "payload" in normalized.columns:
        payloads = normalized["payload"].map(_payload_dict)
        payload_devices = payloads.map(lambda item: item.get("DeviceID"))
        normalized["counter"] = pd.to_numeric(
            normalized["payload"].map(_counter_from_payload), errors="coerce"
        )
    else:
        payload_devices = pd.Series(pd.NA, index=normalized.index, dtype="object")
        normalized["counter"] = pd.Series(pd.NA, index=normalized.index, dtype="Float64")

    explicit_devices = (
        normalized["DeviceID"]
        if "DeviceID" in normalized.columns
        else pd.Series(pd.NA, index=normalized.index, dtype="object")
    )
    if "receiverDeviceID" in normalized.columns:
        receiver_devices = normalized["receiverDeviceID"]
    elif "deviceId" in normalized.columns:
        receiver_devices = normalized["deviceId"]
    else:
        receiver_devices = pd.Series(pd.NA, index=normalized.index, dtype="object")

    # Prefer an explicit DeviceID column, then the sender embedded in payload.
    # The top-level deviceId is a receiver in DMS exports, so it is only a fallback.
    normalized["DeviceID"] = (
        explicit_devices.combine_first(payload_devices).combine_first(receiver_devices)
    )
    normalized["receiverDeviceID"] = receiver_devices

    normalized["DeviceID"] = normalized["DeviceID"].map(
        lambda value: str(value).strip() if pd.notna(value) else pd.NA
    )
    normalized["eventType"] = normalized["eventType"].map(
        lambda value: str(value).strip() if pd.notna(value) else "Unknown"
    )

    missing_device_rows = int(normalized["DeviceID"].isna().sum())
    normalized = normalized.dropna(subset=["timestamp", "DeviceID"])
    normalized = normalized.sort_values("timestamp").reset_index(drop=True)

    if normalized.empty:
        raise CsvValidationError(
            "No usable rows remain after removing invalid timestamps and missing DeviceIDs."
        )

    return LoadResult(
        data=normalized,
        test_start=test_start,
        test_end=test_end,
        invalid_timestamp_rows=invalid_timestamp_rows,
        missing_device_rows=missing_device_rows,
    )


def filter_messages(
    frame: pd.DataFrame, device_ids: list[str], event_types: list[str]
) -> pd.DataFrame:
    return frame.loc[
        frame["DeviceID"].isin(device_ids)
        & frame["eventType"].isin(event_types)
    ].copy()


def last_message_by_device(frame: pd.DataFrame, test_end: pd.Timestamp) -> pd.DataFrame:
    """Return the last selected message and topic for each selected Duck."""
    if frame.empty:
        return pd.DataFrame(
            columns=[
                "DeviceID",
                "last_message",
                "last_topic",
                "messages",
                "time_before_test_end",
            ]
        )

    ordered = frame.sort_values("timestamp")
    last_rows = ordered.groupby("DeviceID", as_index=False).tail(1)
    counts = ordered.groupby("DeviceID").size().rename("messages")
    summary = last_rows[["DeviceID", "timestamp", "eventType"]].rename(
        columns={"timestamp": "last_message", "eventType": "last_topic"}
    )
    summary = summary.join(counts, on="DeviceID")
    summary["time_before_test_end"] = test_end - summary["last_message"]
    return summary.sort_values("last_message", ascending=False).reset_index(drop=True)


def packet_loss_by_topic(frame: pd.DataFrame) -> pd.DataFrame:
    """Estimate packet loss from per-device, per-topic counter gaps.

    This follows the existing notebook definition: after sorting each DeviceID/topic
    series by time, a positive counter difference greater than one contributes
    ``difference - 1`` missing packets. Duplicate counters and backward moves are
    reported but do not count as missing packets.
    """
    columns = [
        "eventType",
        "received_rows",
        "counter_messages",
        "missing_packets",
        "expected_packets",
        "packet_loss_pct",
        "duplicate_counters",
        "counter_resets",
    ]
    if frame.empty:
        return pd.DataFrame(columns=columns)

    working = frame.copy()
    if "counter" not in working.columns:
        if "payload" in working.columns:
            working["counter"] = pd.to_numeric(
                working["payload"].map(_counter_from_payload), errors="coerce"
            )
        else:
            working["counter"] = pd.Series(
                pd.NA, index=working.index, dtype="Float64"
            )

    # Establish temporal order before taking differences within each
    # DeviceID + eventType counter sequence.
    working = working.sort_values("timestamp", kind="mergesort")
    working["counter"] = pd.to_numeric(working["counter"], errors="coerce")
    counter_rows = working.dropna(subset=["counter"]).copy()

    if not counter_rows.empty:
        counter_rows["counter_diff"] = counter_rows.groupby(
            ["DeviceID", "eventType"], sort=False
        )["counter"].diff()
        counter_rows["missing_packets"] = (
            counter_rows["counter_diff"].where(counter_rows["counter_diff"] > 1, 1) - 1
        ).fillna(0)
        counter_rows["duplicate_counter"] = counter_rows["counter_diff"].eq(0)
        counter_rows["counter_reset"] = counter_rows["counter_diff"].lt(0)

        counter_summary = counter_rows.groupby("eventType", as_index=False).agg(
            counter_messages=("counter", "size"),
            missing_packets=("missing_packets", "sum"),
            duplicate_counters=("duplicate_counter", "sum"),
            counter_resets=("counter_reset", "sum"),
        )
    else:
        counter_summary = pd.DataFrame(
            columns=[
                "eventType",
                "counter_messages",
                "missing_packets",
                "duplicate_counters",
                "counter_resets",
            ]
        )

    received = working.groupby("eventType", as_index=False).size().rename(
        columns={"size": "received_rows"}
    )
    summary = received.merge(counter_summary, on="eventType", how="left")
    calculable = summary["counter_messages"].notna()
    summary.loc[calculable, "expected_packets"] = (
        summary.loc[calculable, "counter_messages"]
        + summary.loc[calculable, "missing_packets"]
    )
    summary.loc[calculable, "packet_loss_pct"] = (
        summary.loc[calculable, "missing_packets"]
        / summary.loc[calculable, "expected_packets"]
        * 100
    )

    for column in (
        "received_rows",
        "counter_messages",
        "missing_packets",
        "expected_packets",
        "duplicate_counters",
        "counter_resets",
    ):
        summary[column] = summary[column].astype("Int64")

    return summary[columns].sort_values(
        ["packet_loss_pct", "eventType"], ascending=[False, True], na_position="last"
    ).reset_index(drop=True)


def format_duration(value: pd.Timedelta) -> str:
    total_seconds = max(0, int(value.total_seconds()))
    days, remainder = divmod(total_seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)
