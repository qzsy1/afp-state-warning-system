from __future__ import annotations

from copy import deepcopy


_INTERFACES: list[dict[str, object]] = [
    {
        "id": "thermocouple_8ch",
        "sensor_name": "SMRF八通道热电偶",
        "label": "八通道热电偶",
        "role": "thermocouple",
        "driver": "smrf_hid",
        "protocol": "USB HID",
        "endpoint": "SMRFCT08B",
        "channels": [f"温度{index}" for index in range(1, 9)],
        "processing": "HID帧校验后按K型热电偶换算",
    },
    {
        "id": "plc_process",
        "sensor_name": "松下PLC过程传感器",
        "label": "松下 PLC",
        "role": "plc",
        "driver": "modbus_tcp",
        "protocol": "Modbus TCP FC03",
        "endpoint": "192.168.125.5:502",
        "channels": ["温度", "压力", "张力"],
        "processing": "寄存器读取并按float32低字在前解析",
    },
    {
        "id": "uvc_temperature",
        "sensor_name": "BSV UVC测温热像仪",
        "label": "BSV UVC 热像仪",
        "role": "thermal_uvc",
        "driver": "uvc_thermal",
        "protocol": "USB WinUSB",
        "endpoint": "BSV UVC (WinUSB)",
        "channels": ["ROI平均温度"],
        "processing": "温度矩阵换算后计算ROI均值",
    },
    {
        "id": "abb_motion",
        "sensor_name": "ABB机器人",
        "label": "ABB 机器人",
        "role": "robot",
        "driver": "abb_robot",
        "protocol": "ABB RWS",
        "endpoint": "192.168.125.1",
        "channels": ["ABB_X", "ABB_Y", "ABB_Z", "线速度"],
        "processing": "读取robtarget并由相邻位置与时间计算线速度",
    },
    {
        "id": "m3232_pressure",
        "sensor_name": "M3232薄膜压力",
        "label": "M3232 薄膜压力",
        "role": "pressure",
        "driver": "m3232_pressure",
        "protocol": "专用串口矩阵",
        "endpoint": "COM8",
        "baudrate": 115200,
        "channels": ["薄膜压力"],
        "processing": "矩阵尺寸识别、有效像素合计和中值滤波",
    },
]


def build_interface_catalog() -> list[dict[str, object]]:
    """Return a caller-safe copy of the v2.0.6 five-interface contract."""

    return deepcopy(_INTERFACES)
