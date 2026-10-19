# USB掉线与CPU调频诊断

诊断构建新增CAPS bit12和`usb-diag`，软件恢复构建另有bit13。它们区分USB保护触发、
数据通道失步与软件重枚举。本次已复现传输卡住，保护没有触发；没有放宽USB所有权保护。
2026-10-18原384MHz短测试的保留输出显示32轮RAM校验无错、CPU校验一致并恢复336MHz，
但当时的USB原因已在重新初始化中清掉，不能追溯补出直接证据。

```powershell
python tools/h1_debug.py --report build/usb-before.json usb-diag
# 调频后立即保存；bit13构建失步后等待软件重新枚举，重开PC串口再读。
python tools/h1_debug.py --report build/usb-after.json usb-diag
```

文中的`build/`路径及原始日志散列记录本地验收出处，不随发布包分发。
公开汇总见本页链接的`docs/evidence/h1-debug-clock-usb-hardware-20261019.json`，
使用本版不要求存在这些历史工作目录。

首个USB故障被锁存，同一会话后续轮询不会覆盖。正常关闭再启动同版调试器时，
当前USB状态会初始化，但保留上次故障、累计故障数、服务启动代次及停止原因。
**H1整机重启会清除记录**。换装诊断构建本身需要首次重启；之后调查掉线时应先保留记录。
同一驻留分配的下一次新故障更新`last_fault`，故应尽早保存。

`last_fault_open`关联发生故障的服务代次；判断本次操作是否出现新故障，应与操作前的
`fault_count`、`open_count`和原始tick比较。旧的`last_fault_valid=true`不意味着当前操作又失败。
没有故障记录也不能证明CPU、USB或调度稳定：记录只覆盖驱动实际观察到并触发的保护条件。

保存CPCCR、CPPCR、CLKGR、OPCR、CP0 Status、INTC/GPIO掩码、USB POWER、
端点与总线中断使能、FRAME、地址、端点索引及传输计数。诊断快照不读取会清除事件的
INTRUSB/INTRTX/INTRRX，也不写时钟、控制器或FIFO。故障的`events`来自驱动此前已读取的事件；
当前快照`events=0`表示未采样该清除型寄存器，不表示总线上没有事件。

CPU/总线分频变化本身不属于停止条件。仍检查USB原生服务中断、PD6、端点IRQ、
高速位和USB门控/PHY状态；这些条件异常时仍停止服务。`fault_reason=128`是合并保护分支，
必须结合快照的CLKGR、OPCR及中断状态进一步判断，不能仅据该位认定调频是根因。

主机执行超时现在只报告结果未知，保留重连和诊断的下一步，不再把无USB应答直接写成
“原生代码没有轮询”。不会自动重复执行调频、RAM写入或其他结果未知的命令。

## USB_DIAG布局（64个LE32）

字0～7为：版本（1或2）、字数64、累计成功启动次数、累计故障次数、上次故障有效、
上次故障启动代次、快照字数24、软件重连次数（版本1此字保留0，版本2使用）。

字8～31是当前24字快照，字32～55是保留的故障快照。各快照顺序：
`tick, fault_reason, events, cpccr, cppcr, clkgr, opcr, cp0_status, intc_imr,
pd_mask, pd_pin, pd_flag, power, intr_txe, intr_rxe, intr_usbe, frame, address,
index, configured, resets, session_epoch, rx_packets, tx_packets`。

字56～63为：当前驻留state、stop_request、last_error、累计完成停止次数、
上次完成停止的error、tick、累计poll数、last_poll_tick。STATUS原有16字布局保持不变。

fault_reason各位：1端点TX IRQ、2端点RX IRQ、4总线IRQ、8原生USB IRQ未屏蔽、
16原生插线IRQ未屏蔽、32高速位、64复位等待超时、128所有权/门控/PHY合并保护。

## 2026-10-19 保留现场与FIFO修正候选

诊断版`962e7422…05c9f`（image_id `626249f4`）在物理H1复现了384MHz短测试无应答。
测试输出保留在模块自有BSS：32轮32KiB RAM校验无错，CPU校验前后均为`0x33c657f5`，
返回值0，CPCCR/CPPCR恢复`0x40662220/0x1b000520`。USB故障和停止计数未增加，
中断所有权、USB门控/PHY未变化。第二次复现仅拔插USB就恢复，同一驻留分配和启动代次未变；
这证明本次恢复不需要重开服务，不能把此前“重开后恢复”当作保护触发的证明。

嵌入的35条快照显示：USB FRAME持续推进，poll持续调用，但76字节EXEC_RESULT回复在
`tx_offset=64`处停住，EP1 TXCSR持续`0x2002`。记录在工作区
`build/h1-debug-usb-diag-20261019/clock-384-instrumented/after-replug.json`，
原始输出和USB线级读写分块日志同目录保存。这里的日志是主机串口字节记录，并非电气USB抓包。

[Ingenic JZ4740编程手册](https://storage.googleapis.com/google-code-archive-downloads/v2/code.google.com/dingoo-sdk/JZ4740_pm.pdf)
§21.4.3.15、§21.9.1.2规定双缓冲中`InPktRdy=0`也可能表示第二个槽可写，
`FIFONotEmpty=1`不等于禁止装入下一包。Linux的
[MUSB PIO发送](https://raw.githubusercontent.com/torvalds/linux/master/drivers/usb/musb/musb_gadget.c)
以InPktRdy判断可写，
[寄存器定义](https://raw.githubusercontent.com/torvalds/linux/master/drivers/usb/musb/musb_regs.h)
把FIFONotEmpty列为写0清除、写1保留位。

FIFO修正候选`d3a61a92…8c163`（image_id `8c258a46`）据此区分“可排队”与“完全排空”：
装包只等待InPktRdy/SendStall，提交包时保留FIFONotEmpty；完整回复末尾ZLP可排到第二槽，
完成应答/启动模块仍等待所有数据排空。不刷新FIFO、不重置DATA0/DATA1，也不放宽保护检查。
模型增加64+12字节和64+ZLP双槽、延迟ACK及STALL回归；将编译代码就绪掩码恢复为旧`&3`时
该回归按预期失败。101项主机、28项实际编译MIPS驻留/USB、13项传输、2个独立重定位比较通过。
修正候选已写入设备并读回一致，但真机仍无应答：两包已排入FIFO，`tx_offset=76`、TXCSR持续
`0x2003`。因此FIFO修正合理且通过回归，却**没有单独修复调频掉线**，不能称为成功修复版。

随后Windows UCX请求头跟踪显示EP0控制请求与bulk OUT成功，bulk IN请求保持挂起、0字节，
到本工具关闭端口才以取消结束；没有取得最初失步瞬间的主机错误码。不能写成已证实Windows pipe HALT。
串口`IOCTL_SERIAL_RESET_DEVICE`返回成功也未恢复数据，它不等于USB端口复位。

### 关闭串口的对照（已通过）

`fifo-r1/quiesce-r2/result.json`记录一轮严格有界对照：两次变频前主动关闭COM，USB线保持连接；
在384MHz稳定段重开COM，USB_DIAG核对运行CPCCR/CPPCR为`0x40673330/0x1f000520`，
48/60/112字节RAM回复各两次及EXEC_RESULT正常。模块完成160轮32KiB RAM校验、0错误，
固定160个raw tick后无条件恢复336MHz。服务open_count、USB reset计数均未变化。
这支持故障与变频期间USB事务重叠有关，不证明已经掌握硅片内部的失步机制。

现有协议将DTR会话变化记为EXEC_USB_FAULT（4），即使USB保护fault_count为0。
这个专用诊断模块仅在设备仍配置、无ownership_fault且有固定恢复期限时容许该预期标志；
不得把“忽略flags=4”推广到一般模块。首次对照因预期标志不匹配在变频前返回-20，未改变时钟。

### 软件重连版（已通过针对性真机回归）

`bf2934e4…4d412`（image_id `71a0983b`）新增CAPS bit13。观察到CPCCR/CPPCR改变后，
若bulk IN持续占用80个raw tick且EXEC、文件事务、桌面加载均已结束，才撤下SOFTCONN，
12个raw tick后恢复连接，让主机重新枚举。它不修改CPU时钟、不重新执行RPC、不重启驻留服务；
DONE模块输出及故障记录保留。正常ACK排空会解除观察；无时钟变化的普通超时不触发重连。
USB所有权/门控/PHY异常仍按原保护停止，不进入自动争用或重连循环。

这是软件重新枚举，PC旧串口句柄会失效，调用者需重新发现设备、握手，再读取保留结果，
不能宣称变频期间串口始终连续。不得因为掉线自动重发执行、写内存、文件提交或点击。
USB_DIAG版本2把第7字用于`clock_link_reconnects`；客户端兼容版本1（该字必须为0）。
当前通过103项主机、33项编译MIPS驻留/USB、13项传输、2个独立重定位比较。

新构建的两轮物理H1测试（桌面、动画BDA前台各一轮）均复现失步并由软件恢复，
各轮EXEC_RUN在线路记录中都只有一次。每轮32次32KiB RAM校验无错、CPU校验一致、
恢复336MHz；open_count仍为1，completed_stops仍为0，软件重连计数0→1→2。
全程未实体拔插，也未关闭/重开H1Debug。旧串口失效后仅用新句柄读取同一次DONE结果。

前台BDA动画计数1→190→198，后台512字节专属RAM写回/原生可见性、恢复并释放、
两版模块代码替换/BSS连续执行、480×272截图及10次DTR重开通过。ESC正常返回桌面，
测试BDA文件删除并刷新完成；10次重开USB reset计数不变。恢复后STOP及硬件掩码还原有
编译MIPS模型回归；**本SHA未再次进行物理关闭/磁盘恢复，也未重跑完整固件模拟器**。
汇总见[本次真机记录](evidence/h1-debug-clock-usb-hardware-20261019.json)。

测试结束保持调试运行、空闲桌面、CPU336MHz。此结果不证明任意频率、任意调频代码或
不合作死循环均可恢复，也不是长期超频稳定性认证。若两次变频都发生在轮询间隔内，
本观察器可能看不到寄存器变化；未观察到变化时不会贸然复位USB。

## 诊断候选的离线验证

诊断候选版通过101项主机、26项实际编译MIPS驻留/USB模型、13项传输调度和
2个独立GNU链接重定位比较。模型验证时钟/总线寄存器改变且USB所有权不变时RPC继续，
故障首因不会被后续状态覆盖，正常停止/启动保留记录，诊断读取不确认USB中断事件。
这些不是实际调频USB波形验证。本候选版的硬件和完整固件标记保持未验证，不能沿用旧SHA的通过状态。
