"""Deterministic HPCS-005 checks. Fake BLE only; no instrument access."""
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

import hpcs310 as h

KEYS = ('frequency_hz', 'flicker_percent', 'flicker_index', 'flicker_cycle_ms')
REFERENCE = (100.041679, 4.497815, 0.003591027, 9.995834)
ZERO = dict.fromkeys(KEYS, 0.0)
VALID = dict(zip(KEYS, REFERENCE))
WAVE = [4000 + i % 100 for i in range(400)]


class DecisionTests(unittest.TestCase):
    def decision(self, metrics=ZERO, samples=WAVE, actual=1, requested=None):
        return h.flicker_decision(metrics, h.flicker_waveform_diagnostics(samples), actual, requested)

    def test_auto_success(self):
        self.assertEqual(self.decision(VALID)['action'], 'accept')

    def test_zero_viable_retries(self):
        result = self.decision()
        self.assertEqual((result['action'], result['next_gain_index']), ('retry', 1))

    def test_saturation_stops_even_with_valid_metrics(self):
        self.assertEqual(self.decision(VALID, WAVE[:-1] + [65535])['action'], 'reject')

    def test_flat_and_incomplete_do_not_retry(self):
        for wave in ([4000] * 400, [], WAVE[:-1]):
            self.assertEqual(self.decision(samples=wave)['action'], 'reject')

    def test_bad_metrics_do_not_retry(self):
        for metrics in (None, dict(VALID, frequency_hz=float('nan')),
                        dict(VALID, flicker_percent=101), dict(VALID, flicker_cycle_ms=5)):
            self.assertEqual(self.decision(metrics)['action'], 'reject')

    def test_no_headroom_stops_before_higher_gain(self):
        self.assertEqual(self.decision(samples=[1000, 60000] * 200, requested=1)['action'], 'reject')

    def test_exhausted_and_unknown_gain_stop(self):
        self.assertEqual(self.decision(actual=3, requested=3)['action'], 'reject')
        self.assertEqual(self.decision(actual=None, requested=1)['action'], 'reject')
        self.assertEqual(self.decision(actual=3, requested=3)['action'], 'reject')

    def test_index_7_metadata(self):
        self.assertEqual(h.flicker_sampling_metadata(7), {
            'sampling_rate_index': 7, 'sample_rate_hz': 20000, 'acquisition_window_ms': 500})
        self.assertIsNone(h.flicker_sampling_metadata(None)['sample_rate_hz'])


class FakeClient:
    def __init__(self, outcomes, fail_wave=False, mismatched_gain=False, firmware=2008,
                 short_start_ack=False, initial_gain=0):
        self.outcomes = outcomes
        self.fail_wave = fail_wave
        self.mismatched_gain = mismatched_gain
        self.firmware = firmware
        self.gain_mode, self.gain, self.rate_status_3f, self.rate = 0, initial_gain, 0, 7
        self.short_start_ack = short_start_ack
        self.commands = []
        self.attempt = -1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def start_notify(self, uuid, callback):
        self.callback = callback

    async def write_gatt_char(self, uuid, frame, response=False):
        self.commands.append(frame)
        cmd = frame[1]
        if cmd == 0xee:
            raw = bytearray(20)
            raw[:2] = frame
            raw[2:10] = b'HPCS310P'
            struct.pack_into('<I', raw, 12, 315015421)
            struct.pack_into('<H', raw, 16, self.firmware)
        elif cmd in (0x37, 0x35, 0x41, 0x3e):
            name = {0x37: 'gain_mode', 0x35: 'gain', 0x3e: 'rate'}.get(cmd)
            if not (cmd == 0x35 and self.mismatched_gain):
                if name:
                    setattr(self, name, frame[2])
            if cmd == 0x37 and frame[2] == 1:
                self.gain = 1
            raw = frame[:2] + b'\x00'
        elif cmd in (0x38, 0x36, 0x3f, 0x3d):
            raw = frame + bytes([{0x38: self.gain_mode, 0x36: self.gain,
                                 0x3f: self.rate_status_3f, 0x3d: self.rate}[cmd]])
        elif cmd == 0x0e:
            self.attempt += 1
            raw = frame[:2] if self.short_start_ack else frame[:2] + b'\x00'
        elif cmd == 0x3b:
            raw = frame + b'\x01'
        elif cmd == 0x3c:
            metrics, _ = self.outcomes[self.attempt]
            raw = frame + struct.pack('<4f', *metrics)
        elif cmd == 0x3a:
            if self.fail_wave:
                raise RuntimeError('simulated disconnect during waveform')
            _, samples = self.outcomes[self.attempt]
            raw = frame + struct.pack('<400H', *samples)
        else:
            raw = frame[:2] + b'\x00'
        # Deliberately fragment 3C as well as 3A.
        width = 7 if cmd in (0x3c, 0x3a) else len(raw)
        for offset in range(0, len(raw), width):
            self.callback(None, raw[offset:offset + width])


class AcquisitionTests(unittest.TestCase):
    def run_capture(self, client, continuous=False, expected_error=None, restored=True):
        async def no_sleep(seconds):
            pass
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(sys.modules, {'bleak': types.SimpleNamespace(BleakClient=lambda *a, **kw: client)}), \
                    patch.object(h.asyncio, 'sleep', no_sleep), contextlib.redirect_stdout(io.StringIO()):
                if expected_error:
                    with self.assertRaises(expected_error):
                        asyncio.run(h._flicker('FAKE', directory, True, continuous))
                    code = None
                else:
                    code = asyncio.run(h._flicker('FAKE', directory, True, continuous))
            root = Path(directory)
            record = json.loads((root / 'flicker.json').read_text())
            for attempt in record['attempts']:
                folder = root / ('attempt_%02d' % attempt['number'])
                self.assertEqual((folder / 'flicker_params.bin').read_bytes().hex(), attempt['raw_3c_hex'])
                self.assertEqual((folder / 'flicker_waveform.bin').read_bytes().hex(), attempt['raw_3a_hex'])
                self.assertEqual(len((folder / 'flicker_waveform.csv').read_text().splitlines()),
                                 len(attempt['waveform']['samples']) + 1)
            self.assertNotIn('sampling_speed_value', json.dumps(record))
            self.assertEqual(record['restoration_verified'], restored)
            self.assertIn(h.CMD_STOP, client.commands)
            return code, record

    def test_auto_success_no_manual_retry(self):
        client = FakeClient([(REFERENCE, WAVE)])
        code, record = self.run_capture(client)
        self.assertEqual(code, 0)
        self.assertEqual(record['accepted_attempt'], 1)
        self.assertEqual(client.attempt, 0)

    def test_exact_two_byte_start_ack_is_accepted(self):
        client = FakeClient([(REFERENCE, WAVE)], short_start_ack=True)
        code, record = self.run_capture(client)
        self.assertEqual(code, 0)
        self.assertEqual(record['attempts'][0]['start_ack_hex'], '8c0e')

    def test_auto_register_x1000_still_retries_at_manual_x10(self):
        client = FakeClient([((0, 0, 0, 0), WAVE), (REFERENCE, WAVE)], initial_gain=3)
        code, record = self.run_capture(client)
        self.assertEqual(code, 0)
        self.assertEqual([a['requested_gain_index'] for a in record['attempts']], [None, 1])

    def test_full_gain_sequence_and_all_evidence(self):
        client = FakeClient([((0, 0, 0, 0), WAVE)] * 3 + [(REFERENCE, WAVE)])
        code, record = self.run_capture(client, continuous=True)
        self.assertEqual(code, 0)
        self.assertEqual([a['requested_gain_index'] for a in record['attempts']], [None, 1, 2, 3])
        self.assertEqual([a['decision']['action'] for a in record['attempts']], ['retry'] * 3 + ['accept'])
        self.assertEqual(record['accepted_attempt'], 4)
        for attempt in record['attempts']:
            self.assertEqual(attempt['sample_rate_hz'], 20000)
            self.assertEqual(attempt['acquisition_window_ms'], 500)
            self.assertEqual(len(bytes.fromhex(attempt['raw_3c_hex'])), 18)
            self.assertEqual(len(bytes.fromhex(attempt['raw_3a_hex'])), 802)
            self.assertEqual(len(attempt['waveform']['samples']), 400)
        self.assertEqual(client.commands.count(h.CMD_FLICK_START_CONTINUOUS), 4)
        self.assertEqual(client.commands.count(h.CMD_BATT), 4)

    def test_saturation_ends_sequence(self):
        client = FakeClient([((0, 0, 0, 0), WAVE), ((0, 0, 0, 0), [65535] * 400)])
        code, record = self.run_capture(client)
        self.assertEqual(code, 6)
        self.assertEqual(len(record['attempts']), 2)
        self.assertIsNone(record['accepted_attempt'])

    def test_disconnect_retains_3c_and_restores(self):
        client = FakeClient([(REFERENCE, WAVE)], fail_wave=True)
        _, record = self.run_capture(client, expected_error=RuntimeError)
        self.assertEqual(record['status'], 'error')
        self.assertEqual(len(bytes.fromhex(record['attempts'][0]['raw_3c_hex'])), 18)
        self.assertEqual(record['attempts'][0]['decision']['action'], 'reject')

    def test_manual_readback_mismatch_aborts_before_capture(self):
        client = FakeClient([((0, 0, 0, 0), WAVE)] * 2, mismatched_gain=True)
        _, record = self.run_capture(client, expected_error=ValueError, restored=False)
        self.assertEqual(client.attempt, 1)
        self.assertIn('readback', record['error'])

    def test_exhaustion_preserves_four_zero_attempts(self):
        code, record = self.run_capture(FakeClient([((0, 0, 0, 0), WAVE)] * 4))
        self.assertEqual(code, 6)
        self.assertEqual(len(record['attempts']), 4)
        self.assertIn('no higher gain', record['attempts'][-1]['decision']['reason'])

    def test_nonfinite_metrics_preserve_raw_and_reject(self):
        code, record = self.run_capture(FakeClient([((float('nan'), 0, 0, 0), WAVE)]))
        self.assertEqual(code, 6)
        self.assertIsNone(record['attempts'][0]['metrics'])
        self.assertIn('metric_decode_error', record['attempts'][0])

    def test_old_firmware_refuses_continuous(self):
        client = FakeClient([], firmware=2006)
        _, record = self.run_capture(client, continuous=True, expected_error=ValueError, restored=False)
        self.assertEqual(record['attempts'], [])
        self.assertNotIn(h.CMD_FLICK_START_CONTINUOUS, client.commands)


if __name__ == '__main__':
    unittest.main()
