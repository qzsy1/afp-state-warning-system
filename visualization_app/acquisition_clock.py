"""采集主机时间域：来源、调度、缓存、帧截止和显示传输共用 QPC。

单调时间仅在所属主机上相减；helper 的帧与来源一起传输，服务端
不得用本机时钟减去 helper 时间。墙钟只用于离线对照，不是设备采样时间。
协议周期、设备控制和原始测量值不由该时钟改变。
"""

from time import perf_counter
import time


ACQUISITION_CLOCK_DOMAIN = "host_perf_counter"


def monotonic() -> float:
    """返回高精度单调秒数；不与 GetTickCount64 时间直接相减。"""
    return perf_counter()


def wait_interruptible(stop_event, seconds: float, *, clock=monotonic) -> bool:
    """长等待用事件唤醒，末段用高精度 sleep 校验截止，不忙等或提前发起读取。"""
    deadline = clock() + max(0.0, seconds)
    while not stop_event.is_set():
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        if remaining > 0.02:
            stop_event.wait(remaining - 0.02)
        else:
            time.sleep(min(remaining, 0.001))
    return True
