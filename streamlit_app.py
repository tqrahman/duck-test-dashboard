"""Streamlit entrypoint for reviewing Duck test message activity."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard_data import (
    CsvValidationError,
    filter_messages,
    format_duration,
    last_message_by_device,
    load_csv,
)


st.set_page_config(
    page_title="Duck Test Message Dashboard",
    page_icon="🦆",
    layout="wide",
)


@st.cache_data(show_spinner=False)
def cached_load(csv_bytes: bytes):
    return load_csv(csv_bytes)


def display_timestamp(value: pd.Timestamp) -> str:
    return value.strftime("%b %d, %Y · %H:%M:%S UTC")


st.title("Duck Test Message Dashboard")
st.caption(
    "Upload a DMS CSV to find each Duck's last received message and its topic. "
    "Times are normalized to UTC."
)

uploaded_file = st.file_uploader(
    "Upload test data",
    type=["csv"],
    help=(
        "Accepts OWL export columns (Date, Event Type, Device ID) or API-style "
        "columns (timestamp, eventType, DeviceID/payload)."
    ),
)

if uploaded_file is None:
    st.info("Upload a CSV to begin. The file is processed in memory and is not saved by this app.")
    st.stop()

try:
    result = cached_load(uploaded_file.getvalue())
except CsvValidationError as exc:
    st.error(str(exc))
    st.stop()

data = result.data
test_start = result.test_start
test_end = result.test_end
test_duration = test_end - test_start

quality_notes = []
if result.invalid_timestamp_rows:
    quality_notes.append(f"{result.invalid_timestamp_rows} row(s) had invalid timestamps")
if result.missing_device_rows:
    quality_notes.append(f"{result.missing_device_rows} row(s) had no usable DeviceID")
if quality_notes:
    st.warning("Excluded from analysis: " + "; ".join(quality_notes) + ".")

all_devices = sorted(data["DeviceID"].unique().tolist())
all_events = sorted(data["eventType"].unique().tolist())

with st.sidebar:
    st.header("Filters")
    selected_devices = st.multiselect(
        "DeviceID",
        options=all_devices,
        default=all_devices,
        placeholder="Choose Ducks",
    )
    selected_events = st.multiselect(
        "Event type / topic",
        options=all_events,
        default=all_events,
        placeholder="Choose topics",
    )
    st.caption("The test window always uses the complete uploaded CSV.")

filtered = filter_messages(data, selected_devices, selected_events)

window_col, duration_col, device_col, message_col = st.columns(4)
window_col.metric("Test began", display_timestamp(test_start))
duration_col.metric("Test ended", display_timestamp(test_end))
device_col.metric("Ducks in selection", filtered["DeviceID"].nunique())
message_col.metric("Selected messages", f"{len(filtered):,}")
st.caption(f"Test duration: {format_duration(test_duration)}")

if filtered.empty:
    st.warning("No messages match the selected DeviceID and event type filters.")
    st.stop()

latest = filtered.loc[filtered["timestamp"].idxmax()]
last_col, topic_col, gap_col = st.columns(3)
last_col.metric("Last selected message", display_timestamp(latest["timestamp"]))
topic_col.metric("Last topic", latest["eventType"])
gap_col.metric(
    "Before test ended",
    format_duration(test_end - latest["timestamp"]),
)

st.subheader("Last message by Duck")
summary = last_message_by_device(filtered, test_end)
display_summary = summary.copy()
display_summary["last_message"] = display_summary["last_message"].map(display_timestamp)
display_summary["time_before_test_end"] = display_summary["time_before_test_end"].map(
    format_duration
)
display_summary = display_summary.rename(
    columns={
        "DeviceID": "DeviceID",
        "last_message": "Last message (UTC)",
        "last_topic": "Last topic",
        "messages": "Messages",
        "time_before_test_end": "Before test ended",
    }
)
st.dataframe(display_summary, hide_index=True, width="stretch")

st.subheader("Message activity")
st.caption("Each point is one received message; color represents its topic.")
chart_data = filtered[["timestamp", "DeviceID", "eventType"]].rename(
    columns={"eventType": "Topic"}
)
st.scatter_chart(
    chart_data,
    x="timestamp",
    y="DeviceID",
    color="Topic",
    height=max(280, min(650, 80 + 42 * filtered["DeviceID"].nunique())),
)

with st.expander("View selected message records"):
    detail_columns = [
        column
        for column in [
            "timestamp",
            "DeviceID",
            "eventType",
            "receiverDeviceID",
            "payload",
        ]
        if column in filtered.columns
    ]
    st.dataframe(
        filtered[detail_columns].sort_values("timestamp", ascending=False),
        hide_index=True,
        width="stretch",
    )
