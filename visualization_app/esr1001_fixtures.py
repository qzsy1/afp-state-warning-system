"""ESR1001-A Modbus RTU protocol fixtures used by contract tests."""

READ_REQUEST = bytes.fromhex("01 04 00 30 00 08 F1 C3")
MANUAL_RESPONSE = bytes.fromhex(
    "01 04 10 44 16 AD 1D 4B F4 52 3F 4B F4 51 8F 4B F4 52 3C F7 79"
)
ENABLE_FOUR_CHANNELS_RESPONSE = bytes.fromhex("01 06 00 15 00 04 99 CD")
START_RESPONSE = bytes.fromhex("01 05 00 09 FF 00 5C 38")
STOP_RESPONSE = bytes.fromhex("01 05 00 09 00 00 1D C8")
ILLEGAL_ADDRESS_RESPONSE = bytes.fromhex("01 84 02 C2 C1")
ILLEGAL_VALUE_RESPONSE = bytes.fromhex("01 84 03 03 01")
