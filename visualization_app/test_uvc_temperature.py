import struct
import unittest
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent))

from uvc_temperature import (TemperatureFrameError, SourceFrameTracker,
                             decode_temperature_matrix, parse_temperature_yuv, validate_roi)


def frame(sequence=1000, value=6401, **fields):
    header = bytearray(4636)
    w, h = 4, 3
    for offset, val in {0:0x70827773, 4:len(header), 8:8, 12:4+w*h*4, 16:1,
                        20:1, 24:64, 28:0, 32:0, 36:2, 40:sequence,
                        64:w, 68:h, 72:w*h*2, 76:25, 80:1,
                        84:sequence, 88:w, 92:h, 96:w*h*2, **fields}.items():
        struct.pack_into('<I', header, int(offset), val)
    return bytes(header) + bytes(4) + struct.pack('<12H', *([value]*12)) + bytes(w*h*2)


class TemperatureTests(unittest.TestCase):
    def parse(self, raw, conversion='raw64_minus50'):
        return parse_temperature_yuv(raw, conversion=conversion, expected_magic=0x70827773,
                                     expected_dimensions=(4, 3))

    def test_independent_roi_and_full_precision(self):
        raw=bytearray(frame())
        struct.pack_into('<4H', raw, 4640, 6400, 6401, 6402, 6403)
        result=self.parse(raw)
        self.assertEqual(result.roi_mean((0,0,2,1)), 50.0078125)
        self.assertEqual(result.raw_matrix[:8], struct.pack('<4H',6400,6401,6402,6403))
        self.assertEqual(result.evidence['source_frame_counter'],1000)

    def test_formulas_are_explicit_and_never_substituted(self):
        self.assertEqual(self.parse(frame()).matrix[0], 50.015625)
        self.assertAlmostEqual(self.parse(frame(), 'header_kelvin').matrix[0], -173.134375)
        with self.assertRaisesRegex(TemperatureFrameError,'unconfirmed_conversion'):
            self.parse(frame(), 'auto')

    def test_bad_structures_and_frozen_frames(self):
        cases=[({8:9},'not_temperature'),({32:1},'frozen'),({36:1},'not_temperature'),
               ({64:256},'unexpected_dimensions'),({72:23},'inconsistent_payload'),
               ({84:1002},'inconsistent_source'),({4:99999},'invalid_frame_length')]
        for fields, error in cases:
            with self.subTest(fields=fields), self.assertRaisesRegex(TemperatureFrameError,error):
                self.parse(frame(**{str(k):v for k,v in fields.items()}))
        with self.assertRaises(TemperatureFrameError): self.parse(frame()[:-1])
        with self.assertRaises(TemperatureFrameError): self.parse(b'video')

    def test_invalid_pixels_and_float32(self):
        for raw in [bytes(24),struct.pack('<f',float('nan'))*12,struct.pack('<f',-274)*12]:
            with self.assertRaises(TemperatureFrameError):
                decode_temperature_matrix(raw,width=4,height=3,
                                          encoding='uint16_le' if len(raw)==24 else 'float32_le',
                                          conversion='raw64_minus50' if len(raw)==24 else 'celsius_float32')
        self.assertEqual(decode_temperature_matrix(struct.pack('<f',20.25)*12,width=4,height=3,
                        encoding='float32_le',conversion='celsius_float32')[0],20.25)

    def test_roi_rejected_instead_of_clamped(self):
        for roi in [(-1,0,2,2),(0,0,5,2),(1,1,1,2),(0,0,2.5,2),[],(True,0,2,2)]:
            with self.assertRaises(TemperatureFrameError):validate_roi(roi,4,3)

    def test_source_step_two_duplicates_wrap_and_reset(self):
        tracker=SourceFrameTracker()
        self.assertTrue(tracker.accept(0xfffffffc))
        self.assertTrue(tracker.accept(0xfffffffe))
        self.assertTrue(tracker.accept(0))
        self.assertFalse(tracker.accept(0))
        self.assertEqual(tracker.observations,3)
        self.assertEqual(tracker.duplicates,1)
        with self.assertRaisesRegex(TemperatureFrameError,'reset_or_out_of_order'): tracker.accept(0xfffffffe)


if __name__ == '__main__': unittest.main()
