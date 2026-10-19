# H1Debug v1.0.2

修复CPU/总线调频后USB发送失步需要手工拔插的问题：观察到时钟变化且IN持续卡住时，
在EXEC、文件事务和桌面加载结束后，以软件方式重新枚举USB。原后台驻留服务、DONE模块输出
和故障记录保留，不修改CPU频率，也不重复执行RPC。

重新枚举会使PC旧串口句柄失效，需要重新发现设备并握手，再读取原执行结果；
这不是变频期间串口始终连续，也不能自动重发结果未知的执行、内存写入、文件提交或点击。

- 新增`usb-diag`与CAPS bit12/13，保留首故障、时钟/USB寄存器、服务启停和软件重连计数。
- 修正双缓冲IN FIFO的可写判断与末尾ZLP排队，保留现有DATA序号和USB保护检查。
- 同步PC客户端、诊断说明和`h1-debug` skill，包括重连后读取保留结果的流程。
- 继续提供480×272无损截图、输入控制、RAM/原生模块调试、完整BDA部署及文件管理。

物理H1通过桌面、动画BDA前台各一轮384MHz短测试：每轮32次32KiB RAM校验无错、
CPU校验一致且恢复336MHz；软件重连计数0→1→2，驻留启动次数保持1，每轮EXEC_RUN仅发送一次，
测试全程未实体拔插USB。恢复后前台动画、截图、后台内存恢复释放、代码/BSS测试、
ESC返回、文件清理和10次串口重开通过。

本版未重跑完整固件模拟器及物理关闭/普通磁盘恢复；模型回归与历史真机记录分别标注，
不沿用旧SHA的通过标记。完整范围见仓库的`docs/usb-diagnostics.md`及本版真机证据。

```text
SHA256: bf2934e44ae4d3bdc191acb10f61ec7f3c8d5b866bb4d4f2042dd6479dc4d412
image_id: 71a0983b
SDK: 067fe072477861dfc8949d7b1a55279fb92d2548
```

`H1Debug-install.zip`为设备安装包；`h1-debug-skill.zip`为同步更新的独立skill。
`H1Debug-v1.0.2-source.zip`包含源码、PC工具、skill、固定SDK快照和完整Git bundle，
解压后先运行`python scripts/prepare_source_archive.py`。公开CI构建、运行156项离线测试及2个独立重定位比较、
从实际源码ZIP重建并核对确切BDA；它不连接真机或分发私有固件。资产校验见`SHA256SUMS.txt`。
