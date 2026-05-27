# boil-ippanel
登录 boil 然后更换指定机器的IP

下载本仓库的 boil_ippanel.py

上传到/root/boil_ippanel.py

`python3 boil_ippanel.py`测试运行

添加定时任务：`crontab -e`

`0 5 * * * python3 /root/boil_ippanel.py >/dev/null 2>&1`

会在 /root 目录记录日志
