# H1DEBUG/1 协议

传输复用 H1 CDC 的实验 VID/PID `07C4:A4A6`，身份独立。每帧为 `<4sBBHHH`：`H1DB`、版本1、opcode、sequence、payload长度、status，随后payload和LE32 IEEE CRC32（覆盖头+payload）。payload最大512字节，应答opcode=request|0x80，sequence相同。一次仅一个未完成请求，不自动重试写入/执行。64字节整包结尾发送ZLP。

整数均为小端32位，除特别注明。status0成功、1非法参数/不支持、2当前状态或资源暂不可用、3设备/接口错误。字段名以 `tools/h1_debug.py` 的 `*_FIELDS` 为准。

|opcode|命令|请求|返回|
|---|---|---|---|
|1|PING|空|ASCII `H1DEBUG/1 V1.41 RESIDENT`|
|2|STATUS|空|16字：版本、状态、USB配置、USB错误、复位次数、命令数、坏帧数、tick、poll数、last poll tick、驻留地址/长度、按键low/high、执行状态、last error|
|3|READ|address,count≤512|原始RAM字节|
|4|WRITE|address+1～508字节|空|
|5|KEYS|mask low,mask high,lease ticks|空，键N对应bit N−1；全0释放|
|6|SCREEN_CAPTURE|空|token,width,height,stride,format,total,source,tick|
|7|SCREEN_READ|token,offset,wanted≤500|token,offset,total+数据|
|8|SCREEN_RELEASE|token|空|
|9|CAPS|空|版本、feature bits、512、RAM起点/长度、模块ABI、强制抢占0、tick Hz未知0|
|10|STOP|空|空；应答后开始异步关闭|
|11|TOUCH_SET|x,y,pressed,lease ticks|空；pressed=1按下/移动/续租，pressed=0抬起|
|12|TOUCH_STATUS|空|x,y,pressed,lease remaining，4字|
|13|SCREEN_READ_RLE|token,offset,wanted≤16384|token,offset,total,decoded count+≤496编码字节|
|26|EXEC_INFO|空|原生模块ABI、地址、容量及状态等16字|
|27|EXEC_BEGIN|image size,memory size,entry offset,CRC32,ABI|空|
|28|EXEC_WRITE|连续offset+1～508字节|空|
|29|EXEC_COMMIT|空|空；验证长度/CRC，清零BSS并同步cache|
|30|EXEC_RUN|opcode,budget ticks+≤256参数字节|空；应答后执行|
|31|EXEC_RESULT|空|15字，末字signed返回值|
|32|EXEC_CANCEL|空|空；设置合作取消标记|
|33|EXEC_READ|offset:u32,wanted:u16≤504|offset,total+数据|
|34|EXEC_RESET|空|空；运行时拒绝|
|40|FILE_STATUS|空|16字，见下文|
|41|FILE_PUT_BEGIN|size,CRC32+GBK路径|token,next offset=0|
|42|FILE_WRITE|token,offset+1～504文件字节|next offset|
|43|FILE_COMMIT|token|空；仅接收异步提交请求|
|44|FILE_ABORT|token|空；仅设置取消标记|
|45|FILE_GET_BEGIN|GBK路径|token,size|
|46|FILE_READ|token,offset,count=1～512|原始文件字节|
|47|FILE_GET_CLOSE|token|空|
|48|FILE_STAT|GBK路径|exists,size,is directory|
|49|FILE_PATH|token,kind=0目标/1临时/2备份|GBK路径，无NUL；未使用的路径可为空|
|50|DESKTOP_REFRESH|空|job id；仅确认已排队|
|51|DESKTOP_RUN|GBK完整BDA路径|job id；仅确认已排队|
|52|DESKTOP_STATUS|空|job id,kind,state,error,menu idle,app index,entry count,return code|
|60|FILE_LIST_BEGIN|GBK目录路径|token|
|61|FILE_LIST_NEXT|token|done,size,FAT attrs,name length+GBK basename，无NUL|
|62|FILE_LIST_CLOSE|token|空|
|63|FILE_DELETE|GBK文件路径|空；原生操作已完成|
|64|FILE_RENAME|source length,GBK源路径,GBK目标路径|空；同卷、目标不存在|
|65|FILE_MKDIR|GBK目录路径|空；只创建一级目录|
|66|FILE_RMDIR|GBK目录路径|空；只删除空目录|

CAPS feature bits0～8分别标识读RAM、写RAM、按键、截图、原生执行、后台任务、停止、触屏、无损截图RLE；bit9文件事务，bit10桌面刷新/启动，bit11文件管理60～66。支持全部扩展时返回 `0xFFF`；仅有文件事务和桌面扩展为`0x7FF`，此前USB加速版是 `0x1FF`；旧触屏构建 `0xFF` 使用原始截图命令7，旧构建 `0x7F` 还不支持opcode11/12。客户端按位协商，STATUS仍为原有16字。驻留状态0初始化、1运行、2关闭中、3关闭、4故障；模块状态0EMPTY、1LOADING、2READY、3PENDING、4RUNNING、5DONE。

文件路径为设备绝对路径 `A:\` 或 `B:\`，严格GBK编码，不带NUL，最长240字节；不是PC的F:/G:挂载盘符。拒绝`.`、`..`及尾随点/空格组件，GBK双字节尾部的0x5C不是路径分隔符。上传父目录必须存在，并为15字节临时文件名留出路径空间。文件大小0～0x7FFFFFFF；支持零字节文件。WRITE只接受当前连续offset，重发相同offset也报错。COMMIT ACK后应轮询STATUS；客户端不得把ACK当成功或自动重发BEGIN/WRITE/COMMIT。GET每次范围必须在打开时取得的长度内；GET没有设备整文件CRC，客户端下载CRC/SHA256仅表示收到的字节。

FILE_STATUS字段顺序为version,state,token,size,received,expected CRC,actual CRC,verify offset,error,FS errno,flags,old size,old CRC,last activity tick,step count,reserved。version=1。state为0IDLE、1UPLOADING、2VERIFYING_TEMP、3COMMITTING、4VERIFYING_TARGET、5DONE、6ABORTING、7ABORTED、8FAILED、9RECOVERY、10READING、11LISTING。flags为1OLD_EXISTS、2TEMP_PRESENT、4BACKUP_PRESENT、8TARGET_NEW、16ABORT_REQUEST、32RECOVERY。GET的actual CRC为0；verify offset可能属于旧文件校验，不能按新文件size解释。LISTING的size/received为0，不代表目录条目数。tick未校准；step count用于观察进展。

文件命令40～49使用自己的错误码：0OK、1INVALID、2BUSY、3BINDING、4IO、5CRC、6OFFSET、7TOKEN、8OPEN_FILE、9NEEDS_RECOVERY、10NOT_FOUND、11CANCELLED、12TRANSPORT、13EXPIRED。异步错误放在STATUS.error，FS errno保留首因。提交依次关闭并重读临时文件CRC、保存旧目标CRC、将旧目标改名为唯一备份、临时文件改名为目标、重读目标CRC，最后删除备份。备份删除失败仍可DONE，flags和FS errno报告残留。失败尝试回滚，无法恢复则RECOVERY阻止新BEGIN并保留路径；不承诺断电原子性。PATH可能仍记录已删除文件的名字，存在性以flags/STAT为准。USB会话变化只发布abort意图，worker完成关闭/清理；token、终态和路径跨串口重开保留到下一BEGIN，不能自动清掉RECOVERY。

文件管理60～66沿用上述文件错误码，新增14EXISTS、15NOT_DIRECTORY、16IS_DIRECTORY、17NOT_EMPTY、18CROSS_VOLUME；错误回复payload为单个u32原生FS errno。文件管理变更的错误不会覆盖此前上传事务保留的STATUS恢复证据。LIST_BEGIN创建非零token；LIST_NEXT一次只返回一个basename，FAT attrs的0x10表示目录，名称占1～496字节；列出的名称可能长于后续操作允许的240字节完整路径。done=1表示枚举完成，此时size/attrs/name length均为0且无名称。NEXT会推进游标，丢失响应后不能重发并假定仍是同一项；应结束该次列表或等会话清理后重新开始。完成或提前退出时LIST_CLOSE；断线、STOP和空闲过期也请求清理。LISTING与上传、下载、变更互斥，列表不保证前台同时改目录时的快照一致性。

LIST允许卷根`A:\`/`B:\`和子目录，可将一个末尾分隔符规范化；其余变更拒绝卷根。DELETE只接受普通文件，RENAME支持文件/目录但要求同卷、目标不存在且不能是源目录的子目录；MKDIR要求已有父目录，RMDIR只删除空目录。不存在默认递归删除。所有路径按前述GBK组件规则校验；RENAME两条路径各自最长240字节，source length不包含u32长度字段。60～66不自动刷新桌面。变更返回成功表示该次原生调用完成，传输异常则结果未知，不自动重试；重连后通过STAT/LIST核对结果。

桌面操作只支持 `A:\应用\程序\NAME.bda` 直接子文件，`.bda`不区分大小写，路径同样最多240 GBK字节。kind为1刷新、2运行；state为0IDLE、1QUEUED、2LOADING、3RUNNING、4DONE、5FAILED。error为0OK、1INVALID、2BUSY、3BINDING、4NOT_FOUND、5BDA_INVALID、6LOAD_FAILED、7EXPIRED、8CONTEXT、9IO。menu idle要求固件桌面空闲、根窗口在前台、没有活动子窗口或过渡、受支持的网格皮肤2～4及没有活动桌面job；其他皮肤或已打开BDA返回BUSY，客户端不自动按ESC。RUNNING由原生载入器实际进入payload时确认，entry count为累计进入次数；程序可能快速退出，只观察到DONE，此时也必须确认entry count比请求前增加，不能仅凭排队ACK宣称运行。refresh完成为DONE；run的DONE表示程序已返回，return code保留入口返回值的32位模式。DTR关闭不取消桌面job。QUEUED/LOADING期间阻止冲突的文件BEGIN/COMMIT及EXEC_BEGIN/RUN；RUNNING时恢复一般后台调试，并允许STOP关闭后台而让前台继续；仅QUEUED/LOADING阶段阻止STOP。第二个桌面job仍BUSY，需先退出前台应用。停止保留驻留分配及bridge返回链，正在运行的BDA随后仍可正常返回。桌面请求超时后，服务仍可连接时查询job状态，不盲目重发；运行H1Debug自身会关闭服务，此时通过实体提示核对结果，不能再依赖USB查询。

触屏坐标使用480×272显示像素：左上角 `(0,0)`，x为0～479，y为0～271，单触点。`pressed`只能为0/1；按下时lease为1～4000原始tick，相同坐标只续租、新坐标移动；抬起时lease为0，设备忽略请求坐标并使用最后成功接受的位置。TOUCH_STATUS的lease remaining在已抬起时为0，坐标保留最后位置。主机每点等待ACK，不批量塞入多条RPC；点击和滑动最后都显式抬起，队列暂不可用时也尝试发送抬起。传输已失步则不重试写命令，由设备有限租期和断线释放处理。

GUI触屏事件为DOWN=11、MOVE=12、UP=8，每个入队事件绑定自己的像素坐标。该接口模拟GUI事件和坐标查询，不改写实体触屏ADC状态。

滑动由PC在两端坐标之间插值，不增加设备专用动画协议。`--seconds`是PC调度目标，实际持续时间包含USB往返；慢速通信会跳过过期中间点并保留终点。不得把原始tick租期按固定频率换算成秒。

SCREEN的format1=RGB565LE，format2=BGRX8888；source是LCD表面对应的CPU非缓存地址。复制中换缓冲返回错误；画面内容变化不保证原子快照。

SCREEN_READ_RLE与SCREEN_READ读取同一个已保存的token，不重新采集画面。RLE不改变宽高、步幅、颜色深度或任何像素字节，BGRX的X字节也原样保留。`offset`和`wanted`必须按format的像素字节数（2或4）对齐；wanted为1～16384的最大解压字节数。返回的decoded count是正数且像素对齐，不超过wanted和剩余快照长度。受496字节编码容量限制，回复可只覆盖所请求范围的前缀；下一请求从offset+decoded count继续。

编码由连续记录构成：tag为单字节。tag<128表示随后是tag+1个原始像素；tag≥128表示随后一个像素重复(tag&127)+2次。像素按原格式存储，每条记录不可跨回复拆开。主机校验token/offset/total、正向进度、请求边界和像素对齐，编码必须恰好解码为decoded count字节，截断、溢出和多余数据均报错。错误不会静默退回原始传输。

主机`capture_screen(codec="auto")`按CAPS bit8选择RLE或原始传输；`codec="raw"`强制opcode7，`codec="rle"`要求设备支持。两种方式完成或失败后都释放token（传输已失步时不再发送命令）。`read_screen(meta, codec=..., progress=None)`仅下载已有token，由调用者释放；可在每个完整RPC后调用`progress(done_bytes,total_bytes)`，此时能够串行发送其他命令。计时包含回调耗时，因此纯传输测速不设置回调。

执行时允许PING/STATUS/READ/CAPS/STOP/TOUCH_STATUS/EXEC_INFO/RESULT/CANCEL，其余返回状态2。原生模块没有强制终止。STOP成功应答仅确认请求接收，完成时串口断开；没有承诺正在运行的任意代码都能停下。

USB复位、取消配置、拔线清空未完成传输并释放按键和触点。CDC DTR变化清空残缺RPC/截图并请求取消模块，便于PC进程失败后重新打开；DTR关闭不缩短已经确认的有限输入租期，所以独立CLI的 `press` 在进程退出后仍会维持到指定期限。
