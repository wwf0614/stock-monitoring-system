@echo off
cd /d "D:\盯盘系统"
rem 注意：不要改回 C:\Users\魏文丰\.workbuddy\binaries\python\versions\3.13.12\python.exe
rem 该解释器未安装 requests，会让所有行情请求短路返回 None（基金全部显示"行情暂不可用"）。
rem 已验证 D:\python\python.exe (3.9.7 + requests 2.32.5) 可完整跑通全部模块。
"D:\python\python.exe" server.py 8765
