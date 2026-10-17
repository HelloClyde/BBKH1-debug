# 安装、维护与硬件依据

仅在涉及H1Debug自身变更、安装或故障时读取。详细实现以工具工作区 `h1-debug/{README.md,protocol.md}`、`docs/h1-debug.md` 和当前源码为准。

## 安装与换版

本节是 **H1Debug调试器自身** 的安装/换版流程，不是每次修改目标程序都要执行。目标RAM模块和数据可用现有USB指令更新；新版完整BDA文件和原生启动命令已实现，详见 [deployment.md](deployment.md)。普通目标BDA更新可复用当前兼容的调试器。

已有驻留支持CAPS bit9文件协议时，自身磁盘BDA可用 `file put` 更新；同时支持bit10且位于空闲桌面时，也可 `deploy ... --no-run` 上传并刷新。两种方式都不替换已运行的resident内存，提交成功后仍须关闭、重启H1，再启动新版并核对image_id。不要 `desktop run` 自身当作热升级。当前`4214dbc...`包已通过文件/桌面/后台、重开、再次打开关闭和恢复磁盘的真机验证，具体边界见本页证据记录。普通目标BDA更新不需要拔插USB或重启；这里的重启只用于调试器自身换新驻留版本。

首次安装或旧驻留缺少相应能力时，使用普通磁盘流程：

1. 检查BDA、`dist/H1Debug.build.json`、`dist/H1Debug.bda.sha256`及验证报告绑定同一SHA256。仅安装现有构建时不必重编。
2. 识别普通USB磁盘 `07C4:A4A5`、USB磁盘身份、实际盘符和 `应用/程序` 目录，F:仅为历史值。驻留运行时，按任务需要正常关闭，再请求实体拔插进入磁盘模式；保留用户当前工作的状态。
3. 已授权安装则调用 `powershell -File tools/install_h1_debug.ps1 -DriveLetter <实际盘符字母>`。脚本核对源校验、备份旧包、临时复制、校验、替换、读回。安装成功不等于运行验收。
4. 安全弹出、拔线。**换过曾启动的驻留构建必须重启H1**：退出BDA、关闭调试或换文件均不卸载旧驻留内存。普通同版本启停不必重启。
5. 未连接USB时打开“H1调试”，关闭启动提示回菜单，再连接USB进入CDC。启动器拒绝抢占已连接的普通磁盘会话。
6. 握手/状态检查后，新构建验收用 `test_h1_debug_hardware.py probe` 核对驻留image_id。只比较磁盘文件或协议字符串不能判断实际运行版本。

缺少物理切换时合并给用户明确步骤；不要求用户代做PC能完成的输入/截图。安装脚本末尾提示未涵盖全部换版重启条件，以生命周期规则和当前源码为准。

再次打开同版H1Debug或PC `stop`关闭后台。桌面job只有QUEUED/LOADING阶段阻止STOP；普通payload已RUNNING时可以关闭后台，前台继续运行并能沿原有调用链返回。未完成文件事务需完成取消/回滚，目录游标需清理后才能结束服务。停止会恢复未来派发用的钩子，但保留驻留分配及仍在栈上的bridge/回调，不能释放这些内存或清空活跃返回链。由`desktop run`运行同版H1Debug自身也能关闭，不再等待启动器自己退出。

关闭导致CDC消失符合预期，主机尤其可能来不及观察H1Debug自身的RUNNING状态。保留已知回执，不因断线盲重发stop/run或点击；验证关闭UI时让用户确认提示和返回菜单，验证普通payload中停止时另行观察前台继续运行、退出正常。历史真机需拔插一次恢复磁盘，检查PID_A4A5、卷和文件哈希；“串口消失”不能单独证明成功关闭或磁盘恢复。最终是否停止由任务决定。

## 构建与分层验证

`python tools/build_h1_debug.py` 使用固定SDK及MIPS工具链。Git因沙箱账号不同报dubious ownership时，对实际仓库使用命令/进程级 `safe.directory`，不默认改全局或通配放行。构建器调用SDK子进程，必要时使用 `GIT_CONFIG_COUNT/KEY_0/VALUE_0` 为该进程指定SDK绝对路径。

选择相关检查；最终二进制改变后旧验收不能移植给新SHA256。

|改动|检查入口|
|---|---|
|主机协议/参数/解码|`tests/h1_debug_host.py`|
|驻留RPC与USB|`tests/h1_debug_device.py`|
|活跃窗口/停止时钟/持续流量|`tests/h1_debug_transport.py --existing-build`|
|无损编码/边界|`tests/h1_debug_screen_codec.py`|
|输入/触摸/LCD|`tests/h1_debug_io.py`、`tools/verify_h1_debug_io_firmware.py`|
|文件事务/取消/回滚、列表/删除/改名/目录管理|`tests/h1_debug_files.py`|
|原生桌面派发与BDA入口|`tests/h1_debug_shell.py`|
|IRQ22唤醒|`tests/h1_debug_wake.py`|
|布局/重定位|`tests/h1_debug_relocation.py`，两个地址与独立GNU链接逐字节比较|
|验收辅助工具|`tests/h1_debug_hardware.py`、`tests/h1_debug_preview.py`、`tools/test_h1_debug_foreground_helper.py`|
|完整固件生命周期/触摸自停|`tools/test_h1_debug_native.py`、`tools/test_h1_debug_touch_native.py`|
|FRAME冻结后自然前台进展|`tools/test_h1_debug_native_fairness.py`|

完整固件测试须有用户已有私有V1.41固件和NAND，先看脚本参数，使用隔离实例/端口，只结束自己进程，不停止其它模拟器，不将私有镜像放入发布包或skill。

真实USB回归围绕实际改动：安装读回+image_id、原屏和raw/RLE一致性、输入效果、其他BDA内后台内存/执行、DTR重开、关闭和磁盘恢复。文件/桌面改动需验证完整文件上传读回、事务取消恢复、目录列表/创建/改名/删除与非空目录拒绝、原生桌面标题/图标刷新、V1→V2实际入口和界面变化；不能只凭文件COMMIT或桌面排队ACK通过。STOP改动需分别验证普通BDA继续运行后正常返回，以及同版H1Debug自身关闭。调度改动还需验证持续下载期间前台响应。保留日志、模块、截图、清理回执和构建散列。

仅模型通过不能设置hardware_verified；完整固件和物理验收分别绑定BDA，再构建应重置待验证。归档已测包和元数据以免覆盖。仅有会话stdout则如实记录摘要，不伪造日志。

## 已证实的固件与USB约束

仅适用固件SHA256 `d05786e442f9aad62a8d0a0cb4f6d786bdc7c2fa353a7a2b152c9ed9f01b40ef`，这些地址不通用。

- 后台priority6任务、代码/栈/快照/模块区在全局堆，TCB+4存扩展指针。前台覆盖 `0x83C00020`，不得在此驻留或长期引用其服务表。
- 常规OS tick未启用，单用OSTimeDly会永久睡眠。链式调用原IRQ22后仅唤醒本任务；关闭恢复原回调，不开启全局OS tick。
- 输入队列存指针不复制消息。停止后保留分配和休眠任务复用，重启才回收；不可释放尚有事件引用的内存。shell已进入普通payload时，其bridge/回调也属于活跃返回链，停止只能撤销未来派发，不能销毁现有链。触摸坐标须独立不可变记录，UP消费确认用于触摸自停。
- 触屏查询 `0x80040EBC` 前六条指令是已有BDA签名；钩子从+24 (`0x80040ED4`)开始，保持调用约定和fallback前缀。实体ADC注入测试不能代替虚拟输入钩子测试。
- GUI消息消费任务为priority10，`OSTCBPrioTbl=0x80A6D6F0`，TCB+0x35为优先级、+0x3C为调度计数。priority7不是该消费任务。先验证结构，不硬编码历史TCB地址。
- 新shell替换原桌面窗口+0x88回调，在既有timer消息后由GUI10执行扫描/分类/图标缓存刷新和正常BDA载入。worker不直接跳 `0x83C00020`，也不跨任务写原生无锁PostMessage队列；加载入口确认来自原生loader调用点。busy、前台根窗口、子窗口及皮肤条件都需匹配。
- 文件事务和管理使用原生FS API及已有信号量，不直接写FAT/FTL/NAND，也不启用MSC磁盘驱动。原生seek返回绝对位置而非ISO C的0，rename拒绝覆盖已有目标；备份改名与重读CRC构成可检测的提交/回滚，不是掉电原子替换。目录枚举采用活游标，不承诺快照一致性；DTR变化取消活动文件事务和目录枚举，但不取消桌面job。
- Full-Speed CDC为64字节端点、PIO轮询。115200仅CDC配置，没有UART限速路径。DTR重开保留DATA0/DATA1；reset/重新配置/CLEAR_FEATURE才按协议重置。仅PKTRDY置位时flush，空FIFO flush曾造成真机重开故障。
- 旧版约每25ms仅32次极短轮询，主机尚未取包就睡眠，补包延迟累计，522240字节曾需161.5秒。加速版在实际进展后开启最多两个USB FRAME间隔的固定窗口，不延长deadline，然后让出；tick变化与4096次轮询独立兜底，空闲只一次轮询。
- JZ4740无可用CP0 Count，不能拿模拟器CP0表现给真机限时。SOF可能停止，FRAME不能是唯一退出条件。传输预算不是快照复制或EXEC的强制抢占。
- USB中断/PD6保持已有掩码及所有权约定。改为中断驱动不是局部调参，需另证固件共存和生命周期。
- 本环境QEMU缺USB主机/FIFO，FRAME通常恒0。合成待发送响应只证明有限退出与自然前台进展，不证明真实吞吐或持续USB负载。

## 历史已验收基线

2026-10-16 USB fast r1：SHA256 `90e6c2f34873ac5a218ab520e17a44b205a52d2db03cbc5e958e40074bb169a3`，image_id `7143fbe9`，BDA70,236字节、驻留702,272字节。主证据 `docs/evidence/h1-debug-fast-hardware-20261016.json`，说明 `docs/h1-debug.md`。

480×272 BGRX快照522,240字节，菜单raw18.453/17.704秒、RLE7.000/7.296秒，同token逐字节一致。“时间”RLE14.907秒；菜单7秒不是任何画面的保证。新raw相对旧版约8.9倍，菜单RLE约22.6倍；旧版参考并非同一像素缓冲。历史结束时关闭调试、恢复磁盘，不代表今后开始任务时的实时状态。

这份历史构建的CAPS是 `0x1FF`，没有文件事务、桌面和文件管理命令。上述速度与生命周期结果只属于该SHA256，不能直接标到新版；新版实测范围如下。

## 2026-10-17 文件部署与管理实测

物理H1测试绑定BDA SHA256 `4214dbc11266c59359dac38d3ad6386c3d727e0f534a6520e952c5872cbd8b3d`、驻留image_id `7195598f`、CAPS `0xFFF`。公开汇总报告为H1工具工作区的 `docs/evidence/h1-debug-deploy-hardware-20261017.json`，说明见 `docs/h1-debug.md`，原尺寸截图也在 `docs/evidence/`。报告中的历史 `build/` 路径和原始日志散列是出处记录，原始工作目录不随仓库发布；使用 skill 不要求存在这些日志，更不能伪造或按历史路径创建替代物。公开报告记录了以下结果：

- `deployment`、`file_management`、`background_debug`字段：同一个Client会话、不拔USB，完整普通BDA V1→V2上传/同名替换、设备CRC及下载SHA256核对、原生入口/版本标志和480×272运行截图通过。目录创建、中文文件/目录改名、列表、65537字节及零字节文件传输、单文件删除、空目录删除、非空目录拒绝通过；管理期间前台版本1动画计数从9增至47。后台代码计算、BSS保留、代码替换及512字节专属内存读写/原生可见性通过，内存已恢复释放，测试文件清理完成，USB reset计数不变。
- `deployment`与`screenshots`字段：V1/V2桌面标题和运行画面分别核对，删除测试BDA后刷新并保存画面。桌面逻辑状态可能早于LCD重绘，须给GUI调度机会后采集新快照；立即截图的旧画面不能单独作为刷新失败证据。
- `serial_reopen`字段：10次串口/DTR重开通过；中断目录枚举后游标取消，可再次列表；中断上传后临时文件清理通过。USB reset计数保持不变。
- `background_debug`与`screenshots`字段：PC触摸打开“时间”，保存480×272原屏，后台状态正常，ESC返回桌面并截图确认通过。
- `closing`字段：opcode51重新运行同版H1Debug，收到job11排队ACK，随后CDC消失。用户确认成功关闭并返回菜单后实体拔插USB，PC重新检测到PID_A4A5、内置F:与SD G:卷，读回H1Debug.bda哈希仍为上述`4214dbc...`。盘符仅为本次观测值。

本轮已确认通过再次运行H1Debug关闭并恢复普通磁盘。关闭后USB调试无法再截图，关闭提示与正常返回由用户确认，磁盘恢复由重新枚举及文件哈希补证；不要将CDC消失单独视为完整验收。实体拔插发生在最终关闭后恢复普通磁盘的步骤，完整普通BDA更新本身未拔USB。该结束状态不代表未来任务开始时的设备状态。

本轮离线检查另有`tests/h1_debug_files.py`实际MIPS文件服务模型32项、`tests/h1_debug_host.py`主机协议/CLI97项通过，覆盖GBK边界和丢ACK等错误路径。模型、完整固件与物理测试分层记录，不互相替代；后续重建须重新绑定证据。

基线供比较，不替代本次测量或版本检查；普通业务BDA调试无需重复整套调试器发布验收。
