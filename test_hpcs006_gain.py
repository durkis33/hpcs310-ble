"""No-hardware tests for HPCS-006 evidence and fixed-gain sequencing."""
import asyncio
import contextlib
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import hpcs006_gain as subject


WAVES = [[4000 + gain * 500 + (sample % 17) for sample in range(400)] for gain in range(4)]
METRICS = [(0, 0, 0, 0), (0, 0, 0, 0), (0, 0, 0, 0),
           (100.0, 4.5, 0.0036, 10.0)]


class FakeClient:
    def __init__(self, mismatch=False):
        self.gain_mode, self.gain, self.rate_status_3f, self.rate = 1, 3, 1, 7
        self.mismatch = mismatch
        self.attempt = -1
        self.commands = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def start_notify(self, uuid, callback):
        self.callback = callback

    async def write_gatt_char(self, uuid, frame, response=False):
        self.commands.append(frame)
        command = frame[1]
        if command == 0xee:
            raw = bytearray(20)
            raw[:2] = frame[:2]
            raw[2:10] = b'HPCS310P'
            struct.pack_into('<I', raw, 12, 315015421)
            struct.pack_into('<H', raw, 16, 2008)
        elif command in (0x37, 0x35, 0x41, 0x3e):
            if not (command == 0x35 and self.mismatch):
                # Physical HPCS-006 showed 3F stays 01 after 4100; it is
                # retained as opaque status rather than simulated as a mode echo.
                if command != 0x41:
                    setattr(self, {0x37: 'gain_mode', 0x35: 'gain', 0x3e: 'rate'}[command], frame[2])
            raw = frame[:2] + b'\x00'
        elif command in (0x38, 0x36, 0x3f, 0x3d):
            values = {0x38: self.gain_mode, 0x36: self.gain, 0x3f: self.rate_status_3f, 0x3d: self.rate}
            raw = frame[:2] + bytes((values[command],))
        elif command == 0x0e:
            self.attempt += 1
            raw = frame[:2]  # Physical 310P start ACK shape.
        elif command == 0x3b:
            raw = frame[:2] + b'\x01'
        elif command == 0x3c:
            raw = frame[:2] + struct.pack('<4f', *METRICS[self.attempt])
        elif command == 0x3a:
            raw = frame[:2] + struct.pack('<400H', *WAVES[self.attempt])
        else:
            raw = frame[:2] + b'\x00'
        width = 11 if command in (0x3a, 0x3c) else len(raw)
        for offset in range(0, len(raw), width):
            self.callback(None, raw[offset:offset + width])


class HPCS006Tests(unittest.TestCase):
    def run_experiment(self, client, error=None):
        async def no_sleep(seconds):
            pass
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(sys.modules, {'bleak': types.SimpleNamespace(BleakClient=lambda *a, **kw: client)}), \
                    patch.object(subject.asyncio, 'sleep', no_sleep), contextlib.redirect_stdout(io.StringIO()):
                if error:
                    with self.assertRaises(error):
                        asyncio.run(subject.run('FAKE', temp))
                else:
                    self.assertEqual(asyncio.run(subject.run('FAKE', temp)), 0)
            record = json.loads((Path(temp) / 'hpcs006.json').read_text())
            evidence = {entry['gain_multiplier']: {
                'capture_json': (Path(temp) / ('gain_x%d' % entry['gain_multiplier']) / 'capture.json').exists(),
                'waveform_bytes': (Path(temp) / ('gain_x%d' % entry['gain_multiplier']) / 'flicker_waveform.bin').stat().st_size,
            } for entry in record['captures']}
            return record, evidence

    def test_fixed_rate_all_gains_and_evidence(self):
        record, evidence_by_gain = self.run_experiment(FakeClient())
        self.assertEqual(record['status'], 'complete')
        self.assertEqual([entry['gain_multiplier'] for entry in record['captures']], [1, 10, 100, 1000])
        self.assertEqual(record['analysis']['first_instrument_detection'], 1000)
        self.assertTrue(record['captures'][-1]['instrument_3c_detected'])
        for entry in record['captures']:
            self.assertEqual((entry['sample_rate_hz'], entry['acquisition_window_ms']), (20000, 500))
            self.assertEqual(entry['config_before']['gain_mode'], 0)
            self.assertEqual(entry['config_before']['rate_status_3f'], 1)
            self.assertEqual(entry['config_before']['rate_index'], 7)
            self.assertEqual(len(bytes.fromhex(entry['raw_3c_hex'])), 18)
            self.assertEqual(len(bytes.fromhex(entry['raw_3a_hex'])), 802)
            self.assertEqual(len(entry['waveform']['samples']), 400)
            self.assertIn('digital_headroom', entry['waveform'])
            self.assertIn('noise_floor_status', entry['waveform'])
            evidence = evidence_by_gain[entry['gain_multiplier']]
            self.assertTrue(evidence['capture_json'])
            self.assertEqual(evidence['waveform_bytes'], 802)
        self.assertEqual(record['manual_fixed_config']['rate_configuration_confirmation']['rate_index_8c3d'], 7)
        self.assertEqual(record['manual_fixed_config']['rate_configuration_confirmation']['rate_status_8c3f'], 1)
        self.assertTrue(record['restoration_verified'])

    def test_opaque_3f_status_does_not_block_confirmed_rate_index(self):
        record, _ = self.run_experiment(FakeClient())
        self.assertEqual(record['status'], 'complete')
        self.assertEqual(record['initial_config']['rate_status_3f'], 1)
        self.assertEqual(record['manual_fixed_config']['rate_index'], 7)

    def test_gain_readback_failure_preserves_partial_record(self):
        record, _ = self.run_experiment(FakeClient(mismatch=True), ValueError)
        self.assertEqual(record['status'], 'error')
        self.assertEqual(len(record['captures']), 1)
        self.assertIn('readback mismatch', record['error'])

    def test_diagnostics_and_limits_are_explicit(self):
        wave = subject.sensitivity_diagnostics([1000 + i for i in range(400)])
        self.assertEqual(wave['dc_mean_counts'], 1199.5)
        self.assertEqual(wave['ac_peak_to_peak_counts'], 399)
        self.assertIn('not calibrated', wave['noise_floor_status'])
        summary = subject.characterisation_summary([])
        self.assertIsNone(summary['practical_lowest_reliably_distinguishable_flicker']['value'])
        self.assertIn('not established', summary['practical_lowest_reliably_distinguishable_flicker']['status'])


if __name__ == '__main__':
    unittest.main()
