# H1Debug 安装

仅适用于步步高 H1 / Y100 V1.41。

1. H1处于普通USB磁盘模式时，把安装包中的 `应用/程序/H1Debug.bda`
   复制到H1内置存储同名目录。
2. 如果替换的是已经启动过的旧调试器，先重启H1，卸载旧驻留代码。
3. 安全弹出磁盘并拔线，在H1打开“H1调试”，关闭启动提示回桌面。
4. 接上USB，电脑应出现 `07C4:A4A6` CDC串口。

PC工具与skill在仓库/完整源码包中。安装Python依赖后运行：

```powershell
python -m pip install -r requirements-client.txt
python tools/h1_debug.py list
python tools/h1_debug.py screenshot build/screen.png
python tools/h1_debug.py deploy dist/MyApp.bda 'A:\应用\程序\MyApp.bda'
```

普通目标BDA可保持USB连接更新；先正常退出该程序回到空闲网格桌面。
再次打开“H1调试”可关闭后台。关闭后若普通磁盘未自动恢复，拔插一次USB。
更换调试器自身的驻留版本仍需重启；`--no-run`只更新磁盘文件。

v1.0.2调频恢复会短暂重新枚举USB，旧串口句柄失效时关闭该句柄，重新发现设备并握手，
用`usb-diag`和`exec result`读取保留状态。不要重新执行结果未知的程序或写入命令。
自动恢复不会重启驻留服务，也不会更改CPU频率。

完整说明与发布包：<https://github.com/HelloClyde/BBKH1-debug>
