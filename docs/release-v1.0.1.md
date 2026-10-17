# H1Debug v1.0.1

修复v1.0.0完整源码ZIP的SDK bundle缺少历史对象问题。CI现在取得完整SDK历史，
发布前从实际源码ZIP全新解压，仅允许本地Git传输恢复SDK，再重建并核对BDA。
原v1.0.0 Git标签保持不变；需要源码ZIP时请下载此修正版。

BDA、PC调试功能和skill与v1.0.0保持一致；BDA仍是2026-10-17实际H1验收的确切文件。

- PC按键、单点触摸、480×272无损截图、RAM读写和原生测试模块。
- 不拔USB上传/覆盖完整BDA、刷新桌面并经原生载入器运行。
- 文件上传/下载/列表/状态、改名或同卷移动、删除、创建/删除空目录。
- 再次打开H1调试关闭；已通过后台调试、10次串口重开及普通U盘恢复。

```text
SHA256: 4214dbc11266c59359dac38d3ad6386c3d727e0f534a6520e952c5872cbd8b3d
image_id: 7195598f
SDK: 067fe072477861dfc8949d7b1a55279fb92d2548
```

`H1Debug-install.zip`用于设备安装，`h1-debug-skill.zip`为独立skill，
`H1Debug-v1.0.1-source.zip`包含完整项目、PC工具、skill和固定SDK快照/bundle。
skill需要项目工具目录；源码包解压后先运行`scripts/prepare_source_archive.py`。

公开CI执行139项离线测试、2个独立GNU链接比较及源码ZIP重建；不连接真机，
已有硬件验收由精确SHA256匹配绑定。资产校验见`SHA256SUMS.txt`。
