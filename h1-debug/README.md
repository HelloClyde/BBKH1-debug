# H1 Debug：后台 USB 调试器

独立应用 [`dist/H1Debug.bda`](../dist/H1Debug.bda)，菜单标题 **H1调试**，仅支持 H1 / Y100 V1.41。

v1.0.2新增保留USB诊断与调频失步后的软件重连（CAPS `0x3FFF`），已安装并通过2026-10-19针对性真机回归。重连会短暂重新枚举，PC须重开串口再读取原结果，不会重复执行程序。新SHA为`bf2934e4…4d412`、image_id `71a0983b`，见[调频/USB说明](../docs/usb-diagnostics.md)和[本次验收](../docs/evidence/h1-debug-clock-usb-hardware-20261019.json)。下文完整部署/关闭验收仍属于所列历史SHA。

2026-10-17 的文件部署版已安装并完成真机验收：不拔USB更新完整BDA、刷新桌面名称/图标、运行V1→V2、文件和目录管理、480×272截图、输入、后台内存/代码执行、10次串口重开及再次打开关闭均通过。最后恢复普通U盘并读回核对文件。该历史版SHA256为 `4214dbc1…bd8b3d`、image_id为 `7195598f`，见 [本版真机报告](../docs/evidence/h1-debug-deploy-hardware-20261017.json) 和 [各版验收记录](../docs/h1-debug.md)。此前USB加速版 `90e6c2f3…bb169a3` 的菜单RLE为7.000/7.296秒；这些历史测速不冒充当前构建的新测量。

第一次打开启动后台调试并返回菜单；可以继续打开其他 BDA。再次打开停止调试，恢复输入查询和 USB 控制器。PC 也可以发送 `stop`。停止后的下次打开重新启动。

应用图标使用 H1 原生风格的蓝色圆角高光底板、银色显示器及 USB 插头，由内置 imagegen 工具生成，再用 SDK 转成四份原生菜单资源。源图为 [`assets/icon.png`](assets/icon.png)，生成提示词与来源见 [`assets/icon.provenance.json`](assets/icon.provenance.json)。

本程序不依赖前台程序主动接入调试库。通过固件真实任务接口运行，代码、栈、USB 状态、截图和模块加载区一起移到全局堆；其他 BDA 覆盖 `0x83C00020` 不会覆盖调试器。

## 安装与连接

1. 在普通 USB 磁盘模式下，将 `dist/H1Debug.bda` 复制到 H1 的 `应用/程序`。本工作区可用 `powershell -File tools/install_h1_debug.ps1`；安装器核对 BBK 磁盘身份并备份同名旧文件。
2. **安全移除并拔掉 USB**，在 H1 上打开“H1调试”，关闭启动提示，回到菜单。
3. 再插 USB。Windows 应出现 CDC 串口；调试期间不提供 U 盘接口。
4. 在项目根目录执行以下命令。Python 3.10+，串口需要 `pyserial`，保存 PNG 需要 `Pillow`。本工作区已有 `.tools/usb-deps`，客户端会自动查找。

如果替换的是已运行过的旧版 H1Debug，复制新版后需重启 H1，再拔线启动调试器；仅关闭调试不会卸载旧驻留代码。

```powershell
python -m pip install pyserial Pillow
python tools/h1_debug.py list
python tools/h1_debug.py ping
python tools/h1_debug.py status
python tools/h1_debug.py screenshot build/h1-screen.png

# 按住“右+D+K”8个原始tick；到期由设备自行松开
python tools/h1_debug.py press RIGHT D K --ticks 8
python tools/h1_debug.py release

# 读取前台 BDA 入口；只接受缓存 RAM 地址
python tools/h1_debug.py read 0x83c00020 256 --output build/bda-entry.bin

# 停止调试。恢复磁盘若未重新枚举，拔插一次 USB
python tools/h1_debug.py stop
```

有多个匹配设备时用 `--port COM4` 指定端口，放在子命令前。客户端只打开 VID `07C4`、PID `A4A6` 的端口，必须收到 `H1DEBUG/1 V1.41 RESIDENT` 才会继续；旧“USB调试”“USB执行器”不能冒充这个服务。

## 通过 USB 更新完整 BDA

支持文件事务和桌面控制的新驻留版本提供 `file`、`desktop` 与 `deploy`，CAPS bit9/10分别表示这两项能力。此前 `0x1FF` 的USB加速版需要先更新调试器并重启；客户端不会对旧版本发送未支持的写入命令。此处是普通完整 `.bda` 文件部署，包含文件头、图标和程序，使用固件原生载入器运行；`exec run`仍用于独立RAM测试模块。

```powershell
# 先关闭前台应用，回到网格桌面。上传、校验、刷新，再确认进入新版程序
python tools/h1_debug.py deploy dist/MyApp.bda 'A:\应用\程序\MyApp.bda'
# 只安装并刷新，暂不运行
python tools/h1_debug.py deploy dist/MyApp.bda 'A:\应用\程序\MyApp.bda' --no-run

# 单独文件操作：不限BDA扩展名，不触发桌面刷新或运行
python tools/h1_debug.py file put build/data.bin 'B:\data.bin'
python tools/h1_debug.py file stat 'B:\data.bin'
python tools/h1_debug.py file get 'B:\data.bin' build/downloaded-data.bin
python tools/h1_debug.py desktop refresh
python tools/h1_debug.py desktop run 'A:\应用\程序\MyApp.bda'
python tools/h1_debug.py desktop status

# 上传失败/超时后先查事务；TOKEN替换为状态或回执中的整数
python tools/h1_debug.py file status
python tools/h1_debug.py file paths TOKEN
python tools/h1_debug.py file abort TOKEN
```

路径使用设备自己的 `A:\`（内置存储）和 `B:\`（SD），不是Windows挂载的F:/G:。主机严格转换GBK，最多240编码字节，父目录须已存在；上传目录还须容纳临时文件名。`file put/get`流式分块，支持零字节文件。下载先写本地唯一`.part-*`文件，完整读取并关闭后才替换输出，失败保留原输出和部分文件。下载SHA256/CRC32校验主机收到的数据，不冒充设备提供的整文件CRC。

`deploy`先冻结本地构建并用SDK校验BDA包，再上传临时文件。设备关闭并重读临时文件核对CRC，保存旧文件备份，替换目标，最后再次重读目标核对CRC。只有确认DONE及校验一致才刷新桌面；刷新完成后再排队运行。运行结果必须观察到原生payload进入计数增加；快速退出的程序可直接报告DONE。排队ACK不能证明程序已运行。桌面控制要求空闲的网格皮肤2～4且无弹窗/过渡；忙时返回错误，不自动关闭用户正在运行的应用。前台应用未退出时，第二个桌面任务返回BUSY；STOP仅在QUEUED/LOADING阶段被拒绝，RUNNING时可以关闭后台，前台仍能继续并正常返回。刷新DONE或menu_idle可能早于LCD重绘，截图验收前需短暂让前台调度。

成功提交会清理旧文件备份；清理失败时返回残留路径和flags。替换失败会尝试回滚，无法恢复则保留RECOVERY状态及目标/临时/备份路径，阻止新上传；不承诺断电原子性。所有写入、提交、刷新和启动都不自动重试。传输超时须重开连接查询token/job状态；开始新的上传会覆盖上一笔终态信息，应先保存回执和恢复路径。`file paths`可返回已删除文件的历史名字，存在性以flags或`file stat`为准。

`file put`和`deploy`默认保存JSON回执到 `build/h1-debug-transfers/`，可用 `--receipt PATH`指定；全局`--report PATH`也会保存成功或失败结果。`--wait-seconds`控制异步提交/桌面任务的等待上限，不是单次USB超时（单次由全局`--timeout`控制）。Python在已有会话中可调用 `client.file_put(source,target)`、`file_get(source,output)`、`file_stat(path)`、`file_status()`、`file_paths(token)`、`desktop_refresh()`、`desktop_run(path)` 或 `deploy(source,target,run=True)`；不会另开串口。

更新调试器自身时用 `file put` 或 `deploy --no-run`，随后重启H1才能加载新驻留版本。`desktop run`及默认`deploy`面向普通BDA；运行“H1调试”自身有开关语义，不能把它当作无重启热升级。同版自身可远程运行以关闭调试；CDC随之消失，主机可能来不及确认RUNNING而报告结果未知。此时核对实体成功提示，不盲重发；服务停止后也无法查询桌面状态或截屏。

## USB 文件管理

CAPS bit11 表示目录与文件管理能力。已有 `file put` 能新增或替换同名文件；以下命令补充目录列表、改名/移动、删除和目录创建，均通过当前调试USB连接操作设备文件系统：

```powershell
python tools/h1_debug.py file list 'A:\'
python tools/h1_debug.py file list 'B:\data'
python tools/h1_debug.py file mkdir 'B:\data'
python tools/h1_debug.py file rename 'B:\data\old.bin' 'B:\data\new.bin'
python tools/h1_debug.py file delete 'B:\data\new.bin'
python tools/h1_debug.py file rmdir 'B:\data'
# 修改应用目录后，在桌面空闲时重新扫描图标和名称
python tools/h1_debug.py desktop refresh
```

`list`逐项读取一个目录，返回GBK文件名、大小、FAT属性和目录标记；允许根目录 `A:\` / `B:\`，不递归遍历。默认最多10000项，可用`--max-entries`调整；超过限制会返回错误和已读条目，不把截断结果当完整列表。目录枚举持有一个游标，与上传、下载和文件变更互斥，完成时关闭，断线或空闲过期也会清理。前台程序可继续运行；列表不是文件系统的原子快照。

`rename`可以改名或移动文件/目录，要求同一卷且目标不存在，不移动到自身子目录。`delete`只删除一个文件；`mkdir`只创建一级目录，父目录须存在；`rmdir`只删除空目录。所有变更拒绝卷根目录，不提供隐式覆盖、递归删除或跨卷复制。已有同名文件需要替换时使用带校验与回滚的`file put`。这些命令不自动刷新桌面或启动程序；需要时显式执行`desktop refresh`或使用`deploy`。

删除等原生操作完成后才返回成功ACK，不自动重试。若连接失步或ACK丢失，JSON中的`file_management.outcome_unknown`为true；重连后用`file stat/list`核对实际结果，再决定下一步。设备明确拒绝时保留错误名称与原生`fs_errno`，不会改用其他删除方式。Python同一会话可使用`client.file_list(path)`、`file_rename(source,target)`、`file_delete(path)`、`file_mkdir(path)`、`file_rmdir(path)`。

## 完整分辨率截图和测速

`screenshot`保存原始480×272尺寸PNG，自动协商设备支持的无损RLE传输。RLE不降采样、不减少颜色，解压后的RGB565或BGRX字节与原始快照完全一致，包括BGRX的X字节。旧设备没有CAPS bit8时自动使用原始分块传输；压缩数据校验失败会报错并释放快照，不静默重试另一种格式。

```powershell
# 自动选择无损RLE；PNG仍为480×272
python tools/h1_debug.py screenshot build/h1-screen.png
# 强制原始传输，供速度对比
python tools/h1_debug.py screenshot build/h1-screen-raw.png --raw
# 同一个快照令牌分别下载raw/RLE，逐字节验证并保存测速证据
python tools/h1_debug_screen_benchmark.py build/h1-screen-comparison --port COM4
```

截图JSON包含`codec`、`transfer`和`capture_elapsed_seconds`。transfer统计解压后字节数、编码字节数、读请求数、协议收发字节及下载时间；协议字节包含H1DB帧，不含USB包开销。capture_elapsed_seconds另包括采集和释放，不含PNG编码。2026-10-16真机菜单两轮纯下载：raw为18.453/17.704秒，RLE为7.000/7.296秒，每轮同一快照逐字节相同；“时间”画面RLE为14.907秒。压缩率取决于画面，噪声或照片可能压缩不明显。

比较工具只采集一次，先后下载的内容不会随前台变化。输出保留`screen.raw`、`screen.rle-decoded.raw`、原尺寸`screen.png`、`comparison.json`和原始通信日志；JSON报告两个SHA256、逐字节比较结果和纯下载速度比。已有串口会话可调用`h1_debug_screen_benchmark.compare_snapshot(client, directory)`，不会另开串口。普通Python调用是`client.capture_screen(codec="auto")`，返回`(metadata, raw_bytes)`；也可将codec指定为`"raw"`或`"rle"`。

USB服务在有数据时持续处理最多两个USB帧计数间隔，然后让出CPU；另有原生tick与4096次轮询上限，防止USB帧计数停止后无限忙等。空闲时只轮询一次。这个预算约束传输轮询，不是对单条截图复制或用户EXEC代码的强制抢占。真机在原始整屏下载的25%处接受点击并进入“时间”，GUI任务持续调度，后台快照、内存和执行功能均通过回归。

## 模拟触屏

支持单点点击、按住、移动和滑动。坐标对应480×272显示屏，左上为 `(0,0)`，x范围0～479、y范围0～271。客户端先检查CAPS的触屏位；旧驻留版本需要安装新版并重启后才能使用。

```powershell
# 点击；按下约0.12秒后主动抬起
python tools/h1_debug.py tap 106 52

# 长按1秒，同坐标续租，结束时抬起
python tools/h1_debug.py tap 240 136 --seconds 1

# 按住/移动到指定坐标，最多维持80个原始tick
python tools/h1_debug.py touch 200 100 --ticks 80
python tools/h1_debug.py touch 240 100 --ticks 80
python tools/h1_debug.py touch-status
python tools/h1_debug.py release-touch

# PC插值滑动，最后主动抬起
python tools/h1_debug.py swipe 60 180 420 180 --seconds 0.8 --steps 16
```

`touch`在已按住时移动或续租；`tap`和`swipe`无论成功还是队列返回错误，都尝试发送抬起。每次按下/移动使用有限租期，默认80原始tick，允许1～4000；断线或设备异常时由设备释放。`--seconds`使用PC时钟，USB响应延迟会影响实际动作时间；滑动最多120个插值间隔、目标时长0.05～10秒，慢速通信跳过过期中间点。长按和滑动点间的较长等待会约每0.25秒同坐标续租，不假定原始tick的固定频率；过短租期或超长通信延迟仍可能先触发自动抬起。

Python可在一个连续串口会话中调用 `client.touch(x,y)`、`client.tap(x,y,hold_seconds=1)`、`client.swipe(x0,y0,x1,y1,duration_seconds=.8)`、`client.touch_status()` 和 `client.release_touch()`。触屏状态使用独立命令，现有STATUS字段不变。

模拟输入进入固件GUI事件及坐标查询；直接读取实体触屏ADC的个别手写路径不保证响应。每个排队的触屏事件保留自己的坐标，快速滑动不会把早先事件误配为最后一个点。

## 内存、数据和代码

`read ADDRESS COUNT` / `write ADDRESS FILE` / `write ADDRESS --hex HEX` 自动分块。设备接受 `0x80000000..0x83FFFFFF` 的缓存 RAM 地址，拒绝范围溢出、MMIO 和写入调试器自己的分配块。数据写入后维护 D/I cache，可用于目标程序的数据修改和机器码补丁。需要先确认目标地址、内容和运行时机；写错地址可以破坏当前程序或系统。该接口不写 NAND，也不直接安装 BDA 文件。

完整 C 测试代码通过单独的 RAM 模块入口执行：

```powershell
python tools/h1_debug.py exec info
python tools/h1_debug.py exec run h1-debug/examples/compute.c --directory build/h1-compute --opcode 9 --args-hex 616263
python tools/h1_debug.py exec run --opcode 11 --args-hex 78797a
python tools/h1_debug.py benchmark h1-debug/examples/benchmark.c --directory build/h1-bench --repeat 10 --calibrate-seconds 2
python tools/h1_debug.py exec run h1-debug/examples/cooperative.c --directory build/h1-cancel --budget-ticks 20
```

编译器默认 `.tools/toolchain/bin/mipsel-none-elf-gcc.exe`。模块链接到设备实时返回的 RAM 地址，上传全部字节并核对 CRC32 后才允许执行；新模块清零 BSS/scratch，再维护指令缓存。连续运行同一个模块会保留 `.data/.bss` 和 scratch。结果为 JSON，可供 AI 或测试脚本直接处理，执行目录保存 `module.json`、`module.bin`、`execution.json`。

ABI 见 [`module_abi.h`](../experiments/h1-usb-runner/module_abi.h)：

- `int h1_module_entry(const h1_exec_api *, h1_exec_call *)`，MIPS32 / O32 / 小端 / soft-float。
- 代码+BSS 64 KiB，独立栈32 KiB，scratch16 KiB，输出4 KiB，参数256字节。
- 预算1～400原始 tick。循环应调用 `api->poll_cancel()`；它处理 USB、检查预算并让前台运行。
- `api->runtime_table()` 返回启动时保存的服务表，不依赖被其他 BDA 改写的前缀；`device_command()` 在此调试器中不提供旧射频命令。
- **没有强制抢占恢复或内存隔离。** 无限循环、不让出的模块、错误内存访问可能使设备卡住；这时需要重启。关闭调试不是任意代码的强制终止器。

`tick_80hz` 是沿用的 ABI 名称，实际频率不能按名字推断。`calibrate --seconds 2` 和 benchmark 的校准通过 PC 墙钟估计当前频率，包含调度及 USB 误差；`elapsed_ticks=0` 代表低于计数器分辨率，不代表零耗时。需要精细计时时让同一操作在模块内重复足够次数。调试轮询、截图和内存下载会消耗 CPU，性能比较应保留同样的调试条件。

## 后台生命周期与边界

- 使用经过签名检查的 V1.41 `OSTaskCreateExt`，优先级6必须空闲；TCB 扩展指针保存驻留块，重新打开时据此发现，拒绝占用未知任务。固件没有启用常规 OS tick，因此在运行期间链式接入原 IRQ22，保留原时钟/键盘处理，再只唤醒本调试任务；关闭时恢复原中断回调。
- 前台 BDA 只负责安装/开关，不常驻加载窗口。按键查询入口只在运行期间作 RAM 临时跳转，关闭恢复原指令；固件文件未改写。
- 按键同时提供固件消息队列的 down/up 事件和游戏查询的按住状态。支持矩阵键1～42及命名键，组合键分别松开；期限1～4000原始tick。断线、USB复位和关闭触发松键，队列满时继续重试释放。
- 截图读取 LCD DMA 描述符指向的当前显示数据，支持480×272 RGB565及18/32bpp存储；一份快照分块下载时使用同一 token。复制时应用仍可能改画面，因此可能撕裂；检测到描述符换帧会报错，可重新截图。
- 当前部署版关闭后仍保留约 **708 KiB** RAM（725,472字节，另有分配对齐开销）和一个休眠任务，供下次复用；实际大小以构建摘要为准。固件事件队列和前台返回链可能仍引用该块，因此只在重启时完全回收。重复开关不会重复分配；更换不同构建后须重启再使用。
- 启动时拒绝接管已连接的磁盘会话。关闭恢复原 USB 配置；上一轮真机需要拔插一次才恢复磁盘枚举。其他程序若抢占 USB，会触发所有权错误并停止调试。
- 本轮真机关闭后需要拔插一次USB才恢复磁盘枚举，F:内置存储和G:SD均已验证恢复。
- 没有后台刷机、断点单步、异常恢复、远程重启或强制结束任意 BDA。系统睡眠、全局关中断和高优先级死循环也可能暂停 USB 服务。

## 构建和验证

```powershell
python -m pip install pyelftools unicorn Pillow
python tools/build_h1_debug.py
python tests/h1_debug_relocation.py
python tests/h1_debug_host.py
python tests/h1_debug_io.py
python tests/h1_debug_device.py
python tests/h1_debug_files.py
python tests/h1_debug_shell.py
python tests/h1_debug_transport.py --existing-build
python tests/h1_debug_screen_codec.py
python tests/h1_debug_wake.py
python tools/verify_h1_debug_io_firmware.py
python tests/h1_debug_hardware.py
python tests/h1_debug_preview.py
```

构建使用已锁定的 H1 SDK，不改 SDK、旧 USB 调试器或游戏模拟器。链接生成 ELF relocation，再由 BDA 启动器在实际堆地址应用；测试把重定位结果与两个独立地址的 GNU 链接结果逐字节比较。

主机测试覆盖帧校验、短读、序号、分块和非法参数。IO 测试执行实际 MIPS 按键钩子/跳板和像素复制；设备测试执行实际驻留镜像与 USB 控制器模型。完整固件和真机验收分别记录，不能由这些模型测试推导硬件 USB、缓存、稳定性或性能通过。当前构建摘要及验证状态见 `dist/H1Debug.build.json`，详细本轮结果见 [`docs/h1-debug.md`](../docs/h1-debug.md)。

协议见 [`protocol.md`](protocol.md)。

真机验收工具可在同一个串口会话中记录身份、截图、C 运算、专用内存块读写和计时：

```powershell
python tools/test_h1_debug_hardware.py suite --port COM4 --directory build/h1-debug/hardware-live
```

其中内存测试由模块单独分配512字节，验证主机写入与设备读取一致，恢复内容后释放；不会选择未确认用途的地址。输出目录保留原始 USB 通信、截图、模块结果和内存清理凭据。按键实际效果、跨 BDA 和再次打开关闭需要另外导航并检查；不能仅凭该命令通过就宣布全部真机验收通过。

此前版本的完整原始截图耗时约162秒，96×54预览约3.4秒；这些不是新版USB/RLE的测速结果。可选预览会替换当前EXEC模块，使用前先完成该模块工作并释放其内存分配。各块是实时采样，可能来自不同时刻；常规`screenshot`保持480×272并且不替换EXEC模块：

```python
import sys
from pathlib import Path
sys.path.insert(0, "tools")
from h1_debug import Client
from h1_debug_preview import preview

with Client("COM4") as client:
    preview(client, Path("build/h1-preview"))
```
