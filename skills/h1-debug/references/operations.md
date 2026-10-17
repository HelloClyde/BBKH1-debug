# 日常调试与测试

以下相对路径均以找到的 **H1工具工作区** 为根。先看实际CLI `--help`；Python命令替换成当前可用解释器。可从该工作区执行示例，或将脚本与示例源路径改成实际工具根下的绝对路径；目标BDA及输出路径仍按本次项目选择。

## 环境与连接

- `tools/h1_debug.py` 自动优先导入 `.tools/usb-deps` 的pyserial。单独 `import serial` 失败不证明客户端不可用。截图需要Pillow；构建另依赖pyelftools，MIPS模型测试需要Unicorn。
- 编译器默认为 `.tools/toolchain/bin/mipsel-none-elf-gcc.exe`；先确认存在。SDK在 `vendor/h1-sdk`。
- `list`只枚举匹配的USB调试端口；普通磁盘模式下没有COM正常。
- 可用后先 `ping`、`caps`、`status`，核对 `state=1`、`usb_fault=0`、`last_error=0`。CAPS bit7为触屏、bit8为RLE、bit9为文件事务、bit10为桌面控制、bit11为文件管理；旧版可能缺失。
- 客户端全局参数 `--port`、`--timeout`、`--report` 放在子命令前。只有一个匹配设备时可省略端口。

```powershell
python tools/h1_debug.py list
python tools/h1_debug.py ping
python tools/h1_debug.py caps
python tools/h1_debug.py status
python tools/h1_debug.py --report build/h1-status.json status
```

首次启动或换版所需物理操作见maintenance.md。调试已经运行并且能力满足时直接使用。

## 输入与原屏确认

```powershell
python tools/h1_debug.py screenshot build/h1-before.png
python tools/h1_debug.py press ESCAPE --ticks 8
python tools/h1_debug.py release
python tools/h1_debug.py tap 105 52
python tools/h1_debug.py touch 200 100 --ticks 80
python tools/h1_debug.py touch 240 100 --ticks 80
python tools/h1_debug.py release-touch
python tools/h1_debug.py touch-status
python tools/h1_debug.py swipe 320 150 180 150 --seconds 0.25 --steps 4
```

坐标仅适用于已确认相同布局的页面。原生工具页历史位置：“时间”约(105,52)、“H1调试”约(41,135)，后者再次打开会关闭调试。长滑动可能连续翻多页，确认当前页再点图标。

坐标x=0..479、y=0..271，单触点；支持按下、移动、抬起和有限租期。输入使用固件队列和查询钩子，直接读实体触屏ADC的应用不一定响应。触摸默认80原始tick、按键默认8个，不能假定80Hz。下一组 `press` 替换按住集合；`release`释放按键，`release-touch`释放触点。`tap`/`swipe`尽力显式抬起，断线/复位/停止也释放输入；普通DTR关闭不截断有限租期。

`screenshot`默认完整480×272 PNG，无损RLE不丢颜色，也保留BGRX的X字节。单token的像素固定，下载中切换前台不换图；抓取时前台仍可能改画面而撕裂，LCD换缓冲可检测并报错。低分辨率预览模块不是该路径。

连续操作优先一个会话，例如从已确认工具页打开时间：

```python
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path('tools').resolve()))
from h1_debug import Client

with Client() as client:
    print(client.status())
    client.tap(105, 52)
    time.sleep(0.5)  # 示例前台调度间隔，不是通用完成证明
    print(client.screenshot(Path('build/time.png')))
```

查看PNG确认页面，不能用文件存在替代实际检查。若捕获输入前画面，在正常会话中重新取新快照确认，不无判断地再次点击。按游戏控制定义选择可观察状态；完整截图下载可能错过瞬时动作，后图无变化不能单独证明按键失败。

桌面刷新DONE、应用返回后的`menu_idle=1`也可能早于LCD重绘。先短暂让前台调度，再重新截图；立即截到旧程序画面不等于刷新/退出失败，不据此盲目重发命令。

## 完整用户BDA与测试模块

USB调试协议传输命令和字节，更新代码属于正常用途。根据对象选择已实现的接口：

- **RAM模块更新**：`exec run SOURCE`已经会编译、上传、校验、提交并执行新代码，不需要退出调试或重启H1。
- **目标RAM代码/数据更新**：`write`已经支持向已确认的目标地址写入并维护D/I cache。热补丁需选择目标允许修改的时机；普通BDA文件有自己的装载/入口约定，不等同于直接覆盖任意执行地址。
- **完整BDA文件更新和重新启动**：当前源码/客户端已有 `file put/get/stat/status/paths`、`desktop refresh/run/status` 及 `deploy`。具有相应CAPS的驻留版本可通过CDC文件事务上传完整BDA，再由GUI任务调用原生载入器运行；操作流程、取消/恢复和验收边界见 [deployment.md](deployment.md)。
- **设备文件管理**：具有CAPS bit11时可用`file list/delete/rename/mkdir/rmdir`；目录列表持有同一会话游标，DTR重开会请求清理。根路径允许list/stat读取，变更必须指定非根路径；删除只针对单文件或空目录。具体改名/目录约束也见 [deployment.md](deployment.md)。

用户要求“不拔线上传并重测完整BDA”时，优先使用这些命令，核对当前驻留是否支持。旧版不支持时需要一次升级调试器；之后更新普通目标BDA可复用服务，无需每次拔线或重装。`4214dbc...`构建已在真机同一串口会话完成V1→V2更新/读回/运行、文件管理和后台内存/代码测试，另完成10次DTR重开、触摸打开时间/ESC返回、再次打开H1Debug关闭及实体拔插后磁盘恢复，证据见维护页。普通目标更新不要求该次拔插，只有调试器自身换新驻留版本需要重启。`exec upload`仍只接收其模块ABI，不是普通BDA安装入口。

用户要求保留当前模块/分配时不覆盖；用户要求替换它时，先正常清理属于旧模块的资源，再上传新版本。H1Debug自身换版须卸载旧驻留内存，与目标模块更新不是一回事。

EXEC ABI在 `experiments/h1-usb-runner/module_abi.h`，入口：
`int h1_module_entry(const h1_exec_api *, h1_exec_call *)`。
按任务读取 `h1-debug/examples/compute.c`、`cooperative.c` 或 `benchmark.c`。

```powershell
python tools/h1_debug.py exec info
python tools/h1_debug.py exec run h1-debug/examples/compute.c --directory build/h1-compute --opcode 9 --args-hex 616263
python tools/h1_debug.py exec run --opcode 11 --args-hex 78797a
python tools/h1_debug.py exec result
python tools/h1_debug.py exec output --output build/module-output.bin
```

带源文件的 `exec run` 获取实际模块基址、编译、上传、CRC提交再执行；无源文件则运行当前模块。不沿用重启前的地址。新上传清零模块BSS/scratch，同模块重复运行保留它们。代码+BSS容量64KiB、栈32KiB、scratch16KiB、输出4KiB、参数256字节；以 `exec info` 和当前ABI校验为准。

RAM命令：`read ADDRESS COUNT --output FILE`，`write ADDRESS FILE` / `write ADDRESS --hex HEX`。支持缓存RAM范围 `0x80000000..0x83FFFFFF`，拒绝MMIO、溢出和调试器自身分配；这不意味着任意地址都可覆盖。地址来自实际加载版本、符号/分配返回值及读回确认。

通用验收有可恢复的内存所有权记录：

```powershell
python tools/test_h1_debug_hardware.py suite --directory build/h1-acceptance --stages probe compute memory
```

`probe`核对设备image_id与本地构建；本地已更新而设备仍旧版时会失败，不能忽略并宣称测了新版。`compute`验证PC参考结果、BSS保留和代码替换；`memory`新分配512字节、验证PC/设备均看到修改、恢复后释放。两者会替换当前EXEC模块，先处理原模块活分配。清理失败保留 `memory-ownership.json`，用 `cleanup --state ...` 核对归属后清理，不直接向旧地址重发free。

## 性能测量

### 截图/USB传输

只用下面的快照比较工具，**不替换EXEC，也不要求释放当前模块的分配**。用户要求保留模块时，不运行带源文件的计算benchmark、preview、reset或清理其分配。

```powershell
python tools/h1_debug_screen_benchmark.py build/h1-screen-comparison
```

已有会话调用 `h1_debug_screen_benchmark.compare_snapshot(client, directory)`，可用前后 `exec_info()` 核对模块身份/状态保持。该比较工具要求CAPS bit8；旧版不支持RLE时，用 `client.screenshot(Path('build/h1-screen.png'), codec='raw')` 交付完整截图及其传输统计，注明未做RLE对比，不为测速擅自升级驻留器。

模块仅装载或已返回时可以截图；**EXEC仍在PENDING/RUNNING时，驻留器拒绝截图采集/下载命令**。即使复用同一Client也不例外，需等待正常执行结束再截；不为截图擅自取消用户任务。另开CLI还会改变DTR并请求取消活动EXEC。

截图比较工具同token下载raw和RLE并逐字节比较。报告尺寸、解码/编码字节数、下载耗时及是否包括采集/PNG；速度取决于场景。`read_screen(meta, codec=..., progress=...)`回调在完整RPC后，可串行交错输入；含回调耗时不是纯测速。

`tools/h1_debug_foreground.py`用于已确认原生工具页的调试器验收：下载25%处点时间、检查加载区变化及真实GUI priority10调度、同token复读、ESCAPE返回。若时间BDA此前已加载，窗口前缀可能已经是时间，不能盲套“前缀必须改变”；业务BDA使用自己的状态/画面证据。

### 模块计算

只在任务要求模块计算测速且允许替换当前EXEC时使用带源文件的benchmark；先处理旧模块的活分配。用户要求保留现有模块时，改用它已有且已知语义的测试入口，不能上传示例覆盖。

```powershell
python tools/h1_debug.py calibrate --seconds 2
python tools/h1_debug.py benchmark h1-debug/examples/benchmark.c --directory build/h1-benchmark --repeat 3 --calibrate-seconds 2
```

设备tick与主机时间分开报告。历史真机约40 tick/s、分辨率约25ms，当前重新校准；零tick表示低于分辨率。有限纯计算benchmark故意不在计时循环内让出，会暂停低优先级前台，不能用它证明前台公平。持续任务用协作式poll，把调试/让出成本计入口径。

## 结果未知或断开

CRC/序号/超时错误会将会话标记失步。保存命令、状态和所有权记录，关闭损坏会话；重新识别设备和状态后再决定下一步。写/EXEC超时不等于未执行，不自动重试。非协作代码可能只能用户重启；`cancel_requested`不是已经停止。

线级日志复用 `test_h1_debug_hardware.connect(port, timeout, wire_path)`，不让它与另一个Client竞争端口。

需要结束调试时，桌面job的QUEUED/LOADING阶段会拒绝STOP；普通BDA已RUNNING时允许停止后台，前台应继续运行并能正常返回。停止可能先等待文件取消/回滚或游标清理完成。正常重新运行同版H1Debug也会关闭，不代表更新驻留版本。关闭时CDC消失属于预期现象，主机无法再通过它截图或轮询；保存已知结果，不盲目重发stop、run或点击，实际关闭提示/前台存活由用户或另一路观察确认。换版与恢复磁盘见 [maintenance.md](maintenance.md)。
