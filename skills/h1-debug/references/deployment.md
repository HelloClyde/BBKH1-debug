# 完整 BDA 与文件管理

本节对应工具工作区当前 `tools/h1_debug.py`、`h1-debug/files.c`、`h1-debug/shell.c`。2026-10-17的`4214dbc...`构建、image_id `7195598f`已在物理H1验证不拔USB的完整BDA V1→V2替换、桌面标题刷新和原生运行，以及文件管理、后台测试、DTR重开、再次打开H1Debug关闭和实体拔插后的磁盘恢复。完整散列与证据位置见 [maintenance.md](maintenance.md)，不同构建不能沿用这些验收结论。

## 前置判断

- `caps`的bit9 (`0x200`)表示文件事务，bit10 (`0x400`)表示桌面刷新/运行，bit11 (`0x800`)表示目录列表及文件管理；全部具备为`0xFFF`，旧USB加速版`0x1FF`不支持。身份字符串不变，因此仅`ping`成功不能证明有新能力。
- 设备路径为 `A:\` 或 `B:\`，不是PC挂载盘符。使用严格GBK，完整路径最多240字节；上传父目录须已存在，可按需用`file mkdir`逐级创建。拒绝`.`、`..`、尾随点/空格组件，上传还须为同目录临时文件名留出空间，客户端会检查。
- 原生桌面仅支持 `A:\应用\程序\NAME.bda` 的直接子文件和已验证网格皮肤2～4。先用当前应用正常退出方式回到桌面，检查 `desktop status` 的 `menu_idle=1`；模态对话框、子窗口、菜单过渡或正在运行的BDA都不是空闲桌面。客户端不会自动按ESC，也不会抢占前台程序。
- 一条传输在同一个 `Client` 中完成；不要把BEGIN/WRITE/COMMIT拆成独立CLI进程。全局 `--port`、`--timeout`、`--report` 写在子命令前，命令自己的 `--wait-seconds` 是异步完成等待时间。

## 普通 BDA 更新并运行

```powershell
python tools/h1_debug.py desktop status
python tools/h1_debug.py deploy build/MyApp.bda 'A:\应用\程序\MyApp.bda' --receipt build/myapp-deploy.json
python tools/h1_debug.py screenshot build/myapp-after.png
```

`deploy`先冻结本地文件并按SDK校验正常BDA头和资源范围，再上传/提交、等待设备读回CRC成功、刷新桌面，最后等待原生payload确实进入。运行命令成功表示程序入口已执行，不保证业务界面已绘制或程序功能正确；继续用截图、目标版本标志或行为测试确认。新程序若快速返回，可能只看到DONE，客户端仍会检查入口计数比请求前增加。

`deploy --no-run`上传并刷新桌面，但不打开程序；它也要求空闲桌面。普通目标BDA可反复执行“正常退出→deploy新版→测试”，无需重装H1Debug或重新启动设备。目标程序资源清理仍由其正常退出路径负责。

需要拆开步骤、或文件已在设备上时：

```powershell
python tools/h1_debug.py file put build/MyApp.bda 'A:\应用\程序\MyApp.bda' --receipt build/myapp-upload.json
python tools/h1_debug.py desktop refresh
python tools/h1_debug.py desktop run 'A:\应用\程序\MyApp.bda'
python tools/h1_debug.py desktop status
```

`file put`只传文件，不负责BDA校验、菜单刷新或启动。`desktop run`不会替你更新文件或扫描新目录；新文件/新图标/新标题先刷新。排队ACK不是运行证明：状态0IDLE、1QUEUED、2LOADING、3RUNNING、4DONE、5FAILED；刷新DONE才完成，运行RUNNING或DONE且累计 `entry_count` 增加才证明进入。RUN的DONE表示程序已返回。

桌面刷新DONE或应用退出后的`menu_idle=1`表示逻辑状态就绪，可能先于屏幕重绘。真机曾在此时立即截到上一程序画面；先短暂让GUI调度，再采集新快照确认标题/图标和页面。等待时间不是通用完成证明，仍需查看截图；不要因旧画面盲目重发刷新、运行或点击。

QUEUED/LOADING期间设备阻止冲突的文件提交/新上传、EXEC载入/运行及STOP。普通BDA进入RUNNING后可继续一般后台RAM/模块测试与文件操作，也可STOP关闭后台而保留前台运行；第二个桌面job仍为BUSY，需先正常退出当前应用。停止时保留驻留分配与已有调用链所用bridge，使前台之后能正常返回。桌面job不因DTR关闭而取消；查询状态后再决定后续操作，超时不要再次盲发run或refresh。

## 目录列表、删除、改名与目录创建

```powershell
python tools/h1_debug.py file list 'A:\'
python tools/h1_debug.py file mkdir 'B:\data'
python tools/h1_debug.py file list 'B:\data'
python tools/h1_debug.py file rename 'B:\data\old.bin' 'B:\data\new.bin'
python tools/h1_debug.py file delete 'B:\data\new.bin'
python tools/h1_debug.py file rmdir 'B:\data'
```

`file put`已经支持新增和覆盖同名文件。`list`列出一个目录的名称、大小、FAT属性和目录标志；根路径`A:\`/`B:\`允许`list`和`stat`读取，不允许delete/rename/mkdir/rmdir等修改。`rename`可改名或移动文件/目录，只支持同卷、目标不存在，不能移入自身子目录。`delete`只删除单个文件；`mkdir`只创建一级目录且要求父目录已存在；`rmdir`拒绝非空目录。客户端没有递归删除或自动跨卷复制，不为处理拒绝擅自扩大删除范围。覆盖更新用`file put`的校验与备份回滚流程。

列表在同一会话内打开、逐项读取、关闭原生游标；它不是目录快照，前台同时改目录可能改变顺序或集合。LISTING状态11与上传、下载、变更互斥；DTR变化、断线、STOP及空闲过期会请求清理游标。不要跨CLI持有token，也不要把推进游标的NEXT当可随意重试的普通读取。默认最多10000项，超过时报告错误及部分结果，可按实际需要提高`--max-entries`再重新枚举；不能把部分结果说成完整列表。

这些操作不自动刷新桌面，改动应用目录后在桌面空闲时执行`desktop refresh`。同一Python会话可用`file_list(path)`、`file_rename(source,target)`、`file_delete(path)`、`file_mkdir(path)`、`file_rmdir(path)`。变更成功ACK表示原生调用完成；丢ACK/失步或原生IO失败的`file_management.outcome_unknown`需保留，重连后用stat/list核对，不能盲重发。文件错误名称是主因，`fs_errno`为辅助信息，逻辑拒绝时可能残留上次原生调用值。

## 文件读取、核对和事务回执

```powershell
python tools/h1_debug.py file stat 'A:\应用\程序\MyApp.bda'
python tools/h1_debug.py --report build/myapp-get.json file get 'A:\应用\程序\MyApp.bda' build/myapp-readback.bda
python tools/h1_debug.py file status
python tools/h1_debug.py file paths 123
```

示例123必须替换为实际回执或STATUS中的token。`file stat`返回存在性、字节数、是否目录；`file paths TOKEN`返回该事务的target、temporary、backup路径。PATH中的名称可能对应已删除文件，存在性要结合flags与STAT。

`file put`和`deploy`未指定 `--receipt` 时，CLI默认把回执存入 `build/h1-debug-transfers/<时间戳>.json`；部署还保存上传子回执。保留路径、token、文件长度、CRC/SHA256、最终状态以及设备构建信息。`file get`写本地临时文件，完整读取和关闭后才替换输出；客户端下载CRC/SHA256只描述收到的字节，没有设备整文件CRC证明。需要比对时与本地待传文件SHA256比较。

上传的COMMIT只是异步请求。设备关闭并重读临时文件CRC，记录旧文件CRC，将旧目标改名为备份，再将临时文件改为目标，重读目标CRC后才报告DONE。备份清理失败可能仍DONE并留BACKUP_PRESENT，回执应保留这一事实。状态5DONE、7ABORTED、8FAILED、9RECOVERY为终态；RECOVERY会阻止新的BEGIN。

## 取消、断线与结果未知

```powershell
python tools/h1_debug.py file status
python tools/h1_debug.py file paths 123
python tools/h1_debug.py file abort 123 --wait-seconds 30
```

`abort`用于已识别的当前事务，不是自动清除RECOVERY的恢复命令。DTR变化、USB复位/取消配置/拔线、超时或STOP会请求取消未完成的文件事务或目录枚举；worker继续关闭句柄/游标、清理临时文件并在需要时回滚旧目标。取消ACK不等于回滚已完成。文件事务token、终态和路径跨串口重开保留到下一BEGIN，先查询再行动。

传输/提交超时或CRC/序号失步后保存失败回执，停止自动重发写入和COMMIT。只在重新识别设备及事务后查询状态。无法确认或回滚失败进入RECOVERY时，保留备份/临时/目标文件及路径证据，查明状态再恢复；不要删除备份或重启来掩盖未解决的事务。该方案使用原生文件API和备份改名，**不承诺掉电原子性**；断电后不能假定resident RAM中的事务状态还存在。

## 更新调试器自身

兼容新文件协议的驻留器可以 `file put` 更新自己的磁盘BDA，或在空闲桌面使用：

```powershell
python tools/h1_debug.py deploy dist/H1Debug.bda 'A:\应用\程序\H1Debug.bda' --no-run --receipt build/debugger-update.json
```

此处`--no-run`仍会刷新菜单，**不会替换当前resident内存**。完成提交后正常关闭并按维护流程重启H1，启动新版后核对image_id。运行同版H1Debug自身具有关闭开关语义，可用正常菜单或`desktop run`触发，但不能冒充热升级；其关闭会让CDC消失，主机可能来不及确认RUNNING，不能据此盲重发或再次点击。若旧驻留缺bit9/10，第一次升级仍需普通USB磁盘安装；详见 [maintenance.md](maintenance.md)。
