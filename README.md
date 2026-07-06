# Duck Test Message Dashboard

A Streamlit dashboard for finding the last message received from each Duck during a test.

## CSV format

The dashboard accepts both OWL web exports and API-style DMS exports.

OWL web-export columns:

- `Date`
- `Event Type`
- `Device ID`

Equivalent API-style columns:

- `timestamp`
- `eventType`
- A Duck identifier in either `DeviceID`, `payload.DeviceID`, or (as a fallback) `deviceId`

For raw DMS exports, `payload.DeviceID` is treated as the sending Duck and the top-level
`deviceId` is retained as `receiverDeviceID`.

## Run locally

```bash
python3 -m pip install -r requirements.txt
python3 -m streamlit run streamlit_app.py
```

The dashboard normalizes timestamps to UTC. The test start and end are calculated from
the earliest and latest usable timestamps in the complete uploaded CSV. Device and topic
filters do not change that test window.

## Deploy

Deploy `streamlit_app.py` from this repository on Streamlit Community Cloud. No secrets
are required for the CSV-only version. Configure the deployed app as private before
sharing test data with colleagues.
