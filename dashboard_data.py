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
    else:
        payload_devices = pd.Series(pd.NA, index=normalized.index, dtype="object")

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
