# FlexiTac 原始 ADC 数据采集

先关闭热力图和测速程序，确保没有其他程序读取该串口。
运行环境只需要 Python 3 和 `pyserial`（`python3 -m pip install pyserial`）。

```bash
python3 -u /home/pby/code/VLA/flexitac_mini/FlexiTac/capture_raw.py \
  --port /dev/ttyACM0 --baud 2000000 \
  --duration 60 --output ~/flexitac_data/trial_001
```

省略 `--duration` 时持续记录，Ctrl+C 正常结束。输出目录必须是新目录，防止覆盖。
Windows 使用 `--port COM3` 等实际端口。默认采用 Mini 扩展协议；原版
Arduino 的 AA55+512 字节协议可显式指定 `--protocol legacy`，它不提供 CRC、
节点 ID 或板端时间戳，无法可靠区分混合节点或检测载荷损坏。

每次记录包含：

- `raw_adc.bin`：连续帧，按行存储，形状 16×32。根据固件自动检测 `uint8`
  （ADC8，每帧512字节）或小端 `uint16`（ADC12，每帧1024字节）。
  不进行基线、阈值、归一化、平滑或颜色转换。多节点的帧按到达顺序混合存储。
- `frames.csv`：每帧索引、十六进制节点 ID、节点内帧索引、板端毫秒时间戳、
  主机 Unix/monotonic 纳秒时间、二进制偏移、是否通过 CRC 校验。
  索引从 0 开始，与 `raw_adc.bin` 一一对应。
- `serial_stream.bin`：本次串口读取到的全部原始字节，包括协议头、坏帧和末尾不完整帧。
- `metadata.json`：运行参数、记录时间、逐节点帧数、CRC/帧头错误候选数及未解析字节数。

Mini 默认只把 CRC 合法的帧载荷写入 `raw_adc.bin`。支持原版 Mini ADC8 与新固件 ADC12；
同一记录中的所有节点必须使用相同格式，程序检测到格式混用会报错。启动时可能从半帧开始，
丢弃的未对齐字节不等于丢帧。CRC 错误统计为搜索过程中的候选帧数，不是精确丢帧计数。
主机时间在每次 read 后采集，批量到达的帧可能共用一个主机时间，不能作为精确采样时刻。
板端时间戳是各板启动后的 uint32 毫秒数，会回绕/复位，各板之间没有时钟同步。

文件持续写入并约每秒刷新缓冲区，正常结束会关闭文件并更新元数据；异常断电不是
原子写入，恢复时应核对 `raw_adc.bin` 长度为 512（ADC8）或1024（ADC12）的整数倍，
并与 CSV 帧数一致。
运行时没有有效帧则返回退出码 2；串口/文件错误返回 1。

ADC8 固件的值是 12 位 ADC 右移4位后的结果，不能恢复低4位。ADC12 固件传输原始12位
计数（小端 uint16），但仍不是以 N 为单位的力值。ADC12 帧比 ADC8 帧大一倍。
每块板的板间 UART 已改为2 Mbps，级联时所有相连板须刷入相同固件版本和协议。
新固件的单板二进制为 `Firmware/FlexiTac_16x32_adc12.bin`。先离线检查烧录流程：

```bash
python3 /home/pby/code/VLA/flexitac_mini/tools/flash_flexitac.py \
  --flash --dry-run \
  --firmware /home/pby/code/VLA/flexitac_mini/FlexiTac/Firmware/FlexiTac_16x32_adc12.bin
```

确认为目标板并准备好擦除后，运行实际烧录命令（会整片擦除 128 KiB 主 Flash）：

```bash
python3 /home/pby/code/VLA/flexitac_mini/tools/flash_flexitac.py \
  --flash --yes \
  --firmware /home/pby/code/VLA/flexitac_mini/FlexiTac/Firmware/FlexiTac_16x32_adc12.bin
```

## Python 读取（安装 numpy）

```python
from pathlib import Path
import csv
import json
import numpy as np

directory = Path.home() / "flexitac_data/trial_001"
meta = json.loads((directory / "metadata.json").read_text())
dtype = np.dtype("<u2") if meta.get("adc_bits") == 12 else np.dtype("u1")
adc = np.fromfile(directory / "raw_adc.bin", dtype=dtype).reshape(-1, 16, 32)
with (directory / "frames.csv").open(newline="", encoding="utf-8") as file:
    records = list(csv.DictReader(file))
assert len(adc) == len(records), "帧数据与索引数量不一致"
print(adc.shape, adc.dtype)
if len(adc):
    print("第一帧：", records[0]["node_id"], adc[0])

# 若两块板的 ID 是 C086、C097，可分别提取：
for node in sorted({row["node_id"] for row in records}):
    select = np.array([row["node_id"] == node for row in records], dtype=bool)
    node_adc = adc[select]
    np.save(directory / f"{node}_adc.npy", node_adc)
    print(node, node_adc.shape)
```
