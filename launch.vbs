On Error Resume Next
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "D:\盯盘系统"
Dim py, script
' 注意：不要改回 C:\Users\魏文丰\.workbuddy\binaries\python\versions\3.13.12\python.exe
' 该解释器未安装 requests，会让所有行情请求短路返回 None（基金全部显示"行情暂不可用"）。
' 已验证 D:\python\python.exe (3.9.7 + requests 2.32.5) 可完整跑通全部模块。
py = "D:\python\pythonw.exe"   ' 无控制台: 日志经 _RotatingLog 落 server.log, 与开机任务一致
script = "D:\盯盘系统\server.py"
WshShell.Run """" & py & """ """ & script & """ 8765", 0, False
