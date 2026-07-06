import unittest

import pandas as pd

from dashboard_data import (
    CsvValidationError,
    filter_messages,
    format_duration,
    last_message_by_device,
    load_csv,
    packet_loss_by_topic,
)


CSV = b'''deviceId,timestamp,eventType,payload
RECEIVER,2026-07-01 14:00:00+00:00,health,"{'DeviceID': 'DUCK-1', 'MessageID': 'A'}"
RECEIVER,2026-07-01 14:05:00+00:00,gps,"{'DeviceID': 'DUCK-2', 'MessageID': 'B'}"
RECEIVER,2026-07-01 14:08:00+00:00,boot,"{'DeviceID': 'DUCK-1', 'MessageID': 'C'}"
'''


class DashboardDataTests(unittest.TestCase):
    def test_loads_sender_from_payload_and_preserves_receiver(self):
        result = load_csv(CSV)
        self.assertEqual(result.data["DeviceID"].tolist(), ["DUCK-1", "DUCK-2", "DUCK-1"])
        self.assertEqual(result.data["receiverDeviceID"].unique().tolist(), ["RECEIVER"])
        self.assertEqual(str(result.data["timestamp"].dt.tz), "UTC")
        self.assertEqual(result.test_start, pd.Timestamp("2026-07-01 14:00:00+00:00"))
        self.assertEqual(result.test_end, pd.Timestamp("2026-07-01 14:08:00+00:00"))

    def test_filters_and_summarizes_last_message(self):
        frame = load_csv(CSV).data
        selected = filter_messages(frame, ["DUCK-1"], ["health", "boot"])
        summary = last_message_by_device(selected, frame["timestamp"].max())
        self.assertEqual(len(selected), 2)
        self.assertEqual(summary.loc[0, "last_topic"], "boot")
        self.assertEqual(summary.loc[0, "messages"], 2)
        self.assertEqual(summary.loc[0, "time_before_test_end"], pd.Timedelta(0))

    def test_explicit_device_id_takes_precedence(self):
        csv_bytes = b'''DeviceID,timestamp,eventType,payload\nEXPLICIT,2026-07-01T14:00:00Z,health,"{'DeviceID': 'EMBEDDED'}"\n'''
        frame = load_csv(csv_bytes).data
        self.assertEqual(frame.loc[0, "DeviceID"], "EXPLICIT")

    def test_loads_owl_web_export_columns(self):
        csv_bytes = b'''Gateway ID,Gateway Name,Device ID,Event Type,Date,Message ID,Payload,# of Hops\nFJXS9HIF,TRPAPAPZ,SHTDuck1,gps,2026-07-06T03:10:18.258+00:00,59OJ,"{""C"":15}",1\n'''
        result = load_csv(csv_bytes)
        row = result.data.iloc[0]
        self.assertEqual(row["DeviceID"], "SHTDuck1")
        self.assertEqual(row["eventType"], "gps")
        self.assertEqual(row["receiverDeviceID"], "FJXS9HIF")
        self.assertEqual(row["MessageID"], "59OJ")
        self.assertEqual(row["hops"], 1)

    def test_rejects_missing_required_columns(self):
        with self.assertRaises(CsvValidationError):
            load_csv(b"timestamp,DeviceID\n2026-07-01T14:00:00Z,DUCK-1\n")

    def test_test_window_includes_valid_rows_without_device_id(self):
        csv_bytes = b'''timestamp,eventType,DeviceID\n2026-07-01T14:00:00Z,health,DUCK-1\n2026-07-01T15:00:00Z,system,\n'''
        result = load_csv(csv_bytes)
        self.assertEqual(result.test_end, pd.Timestamp("2026-07-01 15:00:00+00:00"))
        self.assertEqual(result.missing_device_rows, 1)

    def test_duration_format(self):
        self.assertEqual(format_duration(pd.Timedelta(seconds=3661)), "1h 1m 1s")

    def test_packet_loss_is_calculated_per_device_and_topic(self):
        csv_bytes = b'''DeviceID,timestamp,eventType,payload
DUCK-1,2026-07-01T14:00:00Z,gps,"{""C"":1}"
DUCK-1,2026-07-01T14:01:00Z,gps,"{""C"":3}"
DUCK-2,2026-07-01T14:00:00Z,gps,"{""C"":5}"
DUCK-2,2026-07-01T14:01:00Z,gps,"{""C"":8}"
DUCK-1,2026-07-01T14:02:00Z,health,"{""c"":1}"
DUCK-1,2026-07-01T14:03:00Z,health,"{""c"":1}"
DUCK-1,2026-07-01T14:04:00Z,boot,"{""message"":""started""}"
'''
        summary = packet_loss_by_topic(load_csv(csv_bytes).data).set_index("eventType")

        self.assertEqual(summary.loc["gps", "missing_packets"], 3)
        self.assertEqual(summary.loc["gps", "expected_packets"], 7)
        self.assertAlmostEqual(summary.loc["gps", "packet_loss_pct"], 3 / 7 * 100)
        self.assertEqual(summary.loc["health", "duplicate_counters"], 1)
        self.assertEqual(summary.loc["health", "packet_loss_pct"], 0)
        self.assertTrue(pd.isna(summary.loc["boot", "packet_loss_pct"]))

    def test_packet_loss_reads_nested_api_payload_counter(self):
        csv_bytes = b'''DeviceID,timestamp,eventType,payload
DUCK-1,2026-07-01T14:00:00Z,gps,"{'Payload': '{""C"": 2}'}"
DUCK-1,2026-07-01T14:01:00Z,gps,"{'Payload': '{""C"": 5}'}"
'''
        summary = packet_loss_by_topic(load_csv(csv_bytes).data)
        self.assertEqual(summary.loc[0, "missing_packets"], 2)


if __name__ == "__main__":
    unittest.main()
