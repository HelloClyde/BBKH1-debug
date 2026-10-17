# 测试范围

测试分为公开离线测试、需要用户自备固件的模型/完整固件测试，以及真机验收。前两种不能代替物理 USB 和设备验证。普通 CI 仅运行下列公开离线测试，不下载或分发 BBK 固件、NAND、原机 BDA。

## 无需固件的公开离线测试

需要 Python 3.10+、Pillow、pyelftools、Unicorn 2.1.x，以及构建说明中固定的 Windows MIPS GNU 工具链。工具链放在 `.tools/toolchain/bin/`，SDK 子模块位于 `vendor/h1-sdk/`。先构建当前源码，再执行：

```powershell
python tools/build_h1_debug.py
python tests/h1_debug_host.py
python tests/h1_debug_relocation.py
python tests/h1_debug_device.py
python tests/h1_debug_transport.py --existing-build
python tests/h1_debug_hardware.py
python tools/test_h1_debug_foreground_helper.py
```

- `h1_debug_host.py`：主机协议、参数、文件事务、目录管理和桌面任务；BDA 包样例通过固定 SDK 生成。串口和设备均为测试替身。
- `h1_debug_relocation.py`：把实际驻留镜像的运行时重定位与独立地址的 GNU 链接结果比较；需要上一条构建的 ELF、对象、映射及 relocation 文件。
- `h1_debug_device.py`：Unicorn 执行构建出的 MIPS 驻留代码，USB 控制器、调度及固件接口是明确的模型。
- `h1_debug_transport.py --existing-build`：在同一构建上检查 USB 服务时间预算及让出调度。去掉参数会另外编译专用测试镜像。
- `h1_debug_hardware.py`：这是**离线**的验收工具测试，验证专用堆块分配、恢复、释放及异常处理；不会打开 USB。
- `test_h1_debug_foreground_helper.py`：前台检查脚本的测试替身验证。

不要使用 `unittest discover` 混跑整个目录：其他测试需要私有输入，而 `tools/test_h1_debug_*` 中还有会操作真机或指定模拟器的验收脚本。

## 需要自备固件的 MIPS 模型测试

以下脚本要求用户合法持有的 H1/Y100 V1.41 内核文件位于：

```text
build/minigpt-native/project.bin
SHA-256 d05786e442f9aad62a8d0a0cb4f6d786bdc7c2fa353a7a2b152c9ed9f01b40ef
```

该目录名沿用原验收工具的历史路径，不需要 MiniGPT 程序或模型。仓库不提供此内核。测试读取原生接口签名或指令，但部分文件系统、输入和任务行为仍由模型实现：

```powershell
python tools/verify_h1_debug_io_firmware.py
python tests/h1_debug_io.py
python tests/h1_debug_files.py
python tests/h1_debug_shell.py
python tests/h1_debug_wake.py
python tests/h1_debug_screen_codec.py
python tests/h1_debug_preview.py
```

其中 `screen_codec` 和 `preview` 复用了 `h1_debug_io.VM`，也会读取该私有内核；不能仅因测试名称是编码或预览就把它们放进无固件 CI。`verify_h1_debug_io_firmware.py` 支持 `--firmware PATH`，其他上述模型脚本目前使用固定相对路径。

## 完整固件验收

`tools/test_h1_debug_native*.py` 和 `tools/test_h1_debug_touch_native.py` 面向用户已准备好的隔离 H1 模拟器、私有 NAND 与内核。它们不是镜像安装器，也不会准备公开固件。需要单独准备：

- 私有工作目录中的 `project.bin`、`h1-system.raw` 和 `image-build.json`；内核与上列 SHA-256 一致。
- 与该私有镜像中 H1Debug 一致的 `tested-build.json`（原生文件/部署脚本使用），以及仓库当前构建的 `dist/H1Debug.build.json` 和 `build/h1-debug/resident.elf`。
- 原生菜单中被替换的“黑白子”槽位含 H1Debug；具体脚本还要求相应菜单或驻留状态，务必先读其开头说明及参数。
- 独立的模拟器 HTTP/GDB 端口。原部署/关闭验收使用 `8857/8858`，旧生命周期/触摸脚本默认 `8847/8848`。脚本核对私有 NAND/内核路径，不能指向正在进行其他工作的实例。
- 完整 BDA 更新验收需先运行 `python tools/build_h1_debug_update_probe.py`，生成 `build/h1-debug-update-probes/probes.json` 与 V1/V2 BDA。

原生脚本共用 `tools/h1_debug_gdb.py`；这个独立传输模块不会在导入时读取其他项目的 ELF 或连接模拟器。`test_h1_debug_native_stop_deploy.py` 另外核对 GDB 监听进程与选定 QEMU PID 一致，并限定私有端口 `8857/8858`。

这些测试通过模拟器 GDB 进入实际驻留分发器，可验证原生文件系统、桌面和返回流程；它们没有经过物理 CDC 链路，不能据此声明 USB 真机测试通过。

## 物理 H1 验收

串口需要 pyserial，保存截图需要 Pillow。运行前需完成调试器安装并识别实际设备，独占串口，保留原始通信及回执。工具会操作输入、创建和删除专用测试文件或关闭调试服务；先读脚本说明和当前设备状态。

```powershell
python tools/test_h1_debug_hardware.py suite --directory build/hardware
python tools/build_h1_debug_update_probe.py
python tools/test_h1_debug_deploy_hardware.py --directory build/deploy-hardware
python tools/test_h1_debug_deploy_visual.py --directory build/deploy-visual
python tools/test_h1_debug_deploy_reopen.py --directory build/deploy-reopen
python tools/test_h1_debug_deploy_close.py input --directory build/deploy-close
python tools/test_h1_debug_deploy_close.py close --directory build/deploy-close
```

这些命令是分阶段验收入口，不是一条无人值守流程。关闭阶段只能记录启动 ACK 与串口消失；成功关闭提示、返回桌面及必要的实体 USB 拔插仍需人确认。原始日志、内存内容和设备文件保留在本地 `build/`，不要自动随源码发布。
