import yaml
import time
import queue
import re
from threading import Thread
from pprint import pformat, pprint
from sys import argv
from datetime import datetime, timedelta
from pathlib import Path
from netmiko import ConnectHandler
from netmiko.ssh_exception import NetMikoTimeoutException, SSHException

# решение проблемы при подключении к IOS XR
import logging
logging.getLogger('paramiko.transport').disabled = True 


#######################################################################################
# ------------------------------ classes part ----------------------------------------#
#######################################################################################


class CellSiteGateway:
    def __init__(self, ip, host):
        self.hostname = host
        self.ip_address = ip
        self.ssh_conn = None
        self.os_type = "cisco_ios"

        self.connection_status = True       # failed connection status, False if connection fails
        self.connection_error_msg = None    # connection error message

        self.log_count_per_day = None
        self.logs_formatted = {}
        self.logs_formatted_count = {}

    def show_commands(self):
        self.show_log = self.ssh_conn.send_command(r"show logging")

    def parse(self, dev):
        log_parse(dev)

    def reset(self):
        self.log_count_per_day = None
        self.logs_formatted = {}
        self.logs_formatted_count = {}


class PaggXR(CellSiteGateway):

    def __init__(self, ip, host):
        CellSiteGateway.__init__(self, ip, host)
        self.os_type = "cisco_xr"

    def parse(self, dev):
        xr_log_parse(dev)
        
class PaggXE(CellSiteGateway):

    def __init__(self, ip, host):
        CellSiteGateway.__init__(self, ip, host)
        self.os_type = "cisco_xe"


#######################################################################################
# ------------------------------ def function part -----------------------------------#
#######################################################################################


def get_arguments(arguments):
    settings = {"maxth": 20, "conf": False}
    mt_pattern = re.compile(r"mt([0-9]+)")
    for arg in arguments:
        if "mt" in arg:
            match = re.search(mt_pattern, arg)
            if match and int(match[1]) <= 100:
                settings["maxth"] = int(match[1])
        elif arg == "cfg" or arg == "CFG" or arg == "conf":
            settings["conf"] = True
    
    print("\n"
          f"max threads:...................{settings['maxth']}\n"
          f"config mode:...................{settings['conf']}\n"
          )
    return settings


def get_user_pw():
    with open("psw.yaml") as file:
        user_psw = yaml.load(file, yaml.SafeLoader)

    return user_psw[0], user_psw[1]


def get_device_info(csv_file):
    devs = []
    with open(csv_file, "r") as file:
        for line in file:
            if "#" not in line and len(line) > 5:
                hostname, ip_address, ios = line.strip().split(",")

                if ios in ["ios", "cisco_ios"]:
                    dev = CellSiteGateway(ip=ip_address, host=hostname)
                    devs.append(dev)
                elif ios in ["ios xr", "cisco_xr", "xr"]:
                    dev = PaggXR(ip=ip_address, host=hostname)
                    devs.append(dev)
                elif ios in ["ios xe", "cisco_xe", "xe"]:
                    dev = PaggXE(ip=ip_address, host=hostname)
                    devs.append(dev)
                else:
                    print(f"wrong ios: {ios}")

    return devs


def write_logs(devices, current_time, log_folder, settings):
    failed_conn_count = 0
    unavailable_device = []

    conn_msg = log_folder / f"{current_time}_connection_error_msg.txt"
    device_info = log_folder / f"{current_time}_device_info.txt"
    log_count_per_day = log_folder / f"{current_time}_logs_per_day_summary.txt"
    logs = log_folder / f"{current_time}_logs_per_day_brief.txt"
    last_logs_summary = log_folder / f"{current_time}_last_logs_summary.txt"
    severity_logs_summ = log_folder / f"{current_time}_last_severity_logs_summary.txt"

    conn_msg_file = open(conn_msg, "w")
    device_info_file = open(device_info, "w")
    log_count_per_day_file = open(log_count_per_day, "w")
    logs_file = open(logs, "w")
    last_logs_summary_file = open(last_logs_summary, "w")
    severity_logs_summ_file = open(severity_logs_summ, "w")

    period = 21     # last 21 days period
    last_days, year = generate_last_days_list(period)   
    last_logs_summary_file.write(f"summary logs for last {period} days period\n\n\n")
    severity_logs_summ_file.write(f"summary severity logs for last {period} days period\n\n\n")
    last_logs_summary_file.write(f"hostname,{','.join(last_days)},summary\n")
    severity_logs_summ_file.write(f"hostname,{','.join(last_days)},summary\n")

    logs_summary_all_devs_per_day = []

    for device in devices:
        if device.connection_status:
            export_device_info(device, device_info_file)  # export device info: show, status, etc
            logs_summary_per_day_str, logs_summary_per_day_int, output_severity = export_last_logs_summary(device, last_days, year)
            last_logs_summary_file.write(f"{device.hostname},{logs_summary_per_day_str}\n")
            severity_logs_summ_file.write(f"{device.hostname},{output_severity}\n")
            logs_summary_all_devs_per_day.append(logs_summary_per_day_int)

            for d, d_count in device.log_count_per_day.items():
                log_count_per_day_file.write(f"{device.hostname},{d},{str(d_count)}\n")
            
            for year, year_value in device.logs_formatted_count.items():
                for day, day_value in year_value.items():
                    for log, log_value in day_value.items():
                        logs_file.write(f"{device.hostname},{year},{day},{log},{str(log_value)}\n")
                        
        else:
            failed_conn_count += 1
            conn_msg_file.write("-" * 80 + "\n")
            conn_msg_file.write(f"### {device.hostname} : {device.ip_address} ###\n\n")
            conn_msg_file.write(f"{device.connection_error_msg}\n")
            unavailable_device.append(f"{device.hostname} : {device.ip_address}")
    
    summary_all = summary_all_devs_per_day(logs_summary_all_devs_per_day, period)
    last_logs_summary_file.write(f"summary:,{summary_all}\n")     

    conn_msg_file.close()
    device_info_file.close()
    log_count_per_day_file.close()
    logs_file.close()
    last_logs_summary_file.close()
    severity_logs_summ_file.close()

    if all([dev.connection_status is True for dev in devices]):
        conn_msg.unlink()

    return failed_conn_count


def export_device_info(dev, export_file):
    export_file.write("#" * 80 + "\n")
    export_file.write(f"### {dev.hostname} : {dev.ip_address} ###\n\n")


    export_file.write("-" * 80 + "\n")
    export_file.write("device.show_log\n\n")
    export_file.write(dev.show_log)
    export_file.write("\n\n")

    export_file.write("-" * 80 + "\n")
    export_file.write("device.log_count_per_day\n\n")
    export_file.write(pformat(dev.log_count_per_day))
    export_file.write("\n\n")
    
    export_file.write("-" * 80 + "\n")
    export_file.write("device.logs_formatted\n\n")
    export_file.write(pformat(dev.logs_formatted))
    export_file.write("\n\n")

    export_file.write("-" * 80 + "\n")
    export_file.write("device.logs_formatted_count\n\n")
    export_file.write(pformat(dev.logs_formatted_count))
    export_file.write("\n\n")


#######################################################################################
# ------------------------------ get bs port -----------------------------------------#
#######################################################################################

def log_parse(device):
    pattern = re.compile(r"(\w{3} +\d{1,2}) +(\d{4}) +(\d{2}:\d{2}:\d{2}.\d+)(?: \w*)?: +(%\S+)")
        # 000054: (May  6) (2022) (16:57:09.303): (%SSH-5-ENABLED): SSH 2.0 has been enabled
        # 000136: (Jul 11) (2021) (02:35:56.665) ALA: (%LINEPROTO-5-UPDOWN): Line protocol on In
    pattern2 = re.compile(r"(\w{3} +\d{1,2}) +(\d{4}) +(\d{2}:\d{2}:\d{2}.\d+)(?: \w*)?: +(.*)")
        # 000070: (Apr 21) (2022) (02:37:19.435) ALA: (The VLAN 4093 will be internally used for this clock port.)
    pattern_without_year = re.compile(r"(\w{3} +\d{1,2}) +(\d{2}:\d{2}:\d{2}.\d+)(?: \w*)?: +(.*)")
        # *(Jan  2) (00:00:03.503): (LIC: :License level: AdvancedMetroIPAccess  License type: Permanent)

    logs = {}               # {year : {day : {time : log}}}
    logs_per_day = {}       # day : logs-count
    day_logs_count = {}     # year {day: {log : count}}
    log_count = 0
    
    for line in device.show_log.splitlines():
        match = re.search(pattern, line)
        match2 = re.search(pattern2, line)
        match_without_year = re.search(pattern_without_year, line)

        if match:
            log_count += 1
            
            day = match[1]
            year = match[2]
            time = match[3]
            log = match[4]
            
            logs_to_dict(year, day, time, log, logs)
            logs_per_day_count_summary(day, logs_per_day)   
            logs_per_day_count_brief(year, day, log, day_logs_count)   

        elif match2:
            log_count += 1
            
            day = match2[1]
            year = match2[2]
            time = match2[3]
            log = match2[4]
            
            logs_to_dict(year, day, time, log, logs)
            logs_per_day_count_summary(day, logs_per_day)   
            logs_per_day_count_brief(year, day, log, day_logs_count)   

        elif match_without_year:
            log_count += 1

            day = match_without_year[1]
            year = "unknown"
            time = match_without_year[2]
            log = match_without_year[3]

            logs_to_dict(year, day, time, log, logs)
            logs_per_day_count_summary(day, logs_per_day)   
            logs_per_day_count_brief(year, day, log, day_logs_count)      

    logs_quantity = check_logs_quantity(device)
    
    if log_count == 0:
        print(f"{device.hostname:23}{device.ip_address:16}[ERROR] log_count = 0")
    if logs_quantity - log_count > 6:
        print(f"{device.hostname:23}{device.ip_address:16}[ERROR] log match error: {logs_quantity}-{log_count}= {logs_quantity - log_count}")
        
    device.logs_formatted = logs
    device.logs_formatted_count = day_logs_count
    device.log_count_per_day = logs_per_day


def xr_log_parse(device):
    pattern = re.compile(r"(\d{4}) +(\w{3} +\d{1,2}) +(\d{2}:\d{2}:\d{2}.\d+)(?: \w*)?: \S+ (%\S+)")
    # RP/0/RSP0/CPU0:(2021) (Aug  3) (11:32:55.412) ALA: pwr_mgmt[392]: (%PLATFORM-PWR_MGMT-4-MODULE_WARNING) : Power-module 0/PS0/M0/SP warning condition cleared
    pattern_without_year = re.compile(r"(\w{3} +\d{1,2}) +(\d{2}:\d{2}:\d{2}.\d+)(?: \w*)?: +(.*)")
    # *(Jan  2) (00:00:03.503): (LIC: :License level: AdvancedMetroIPAccess  License type: Permanent)
    
    logs = {}               # {year : {day : {time : log}}}
    logs_per_day = {}       # day : logs-count
    day_logs_count = {}     # year {day: {log : count}}
    log_count = 0
    
    for line in device.show_log.splitlines():
        match = re.search(pattern, line)
        match_without_year = re.search(pattern_without_year, line)

        if match:
            log_count += 1
            
            day = match[2]
            year = match[1]
            time = match[3]
            log = match[4]

            logs_to_dict(year, day, time, log, logs)
            logs_per_day_count_summary(day, logs_per_day)   
            logs_per_day_count_brief(year, day, log, day_logs_count)           

        elif match_without_year:
            log_count += 1

            day = match_without_year[1]
            year = "unknown"
            time = match_without_year[2]
            log = match_without_year[3]

            logs_to_dict(year, day, time, log, logs)
            logs_per_day_count_summary(day, logs_per_day)   
            logs_per_day_count_brief(year, day, log, day_logs_count)  

    
    logs_quantity = check_logs_quantity(device)

    if log_count == 0:
        print(f"{device.hostname:23}{device.ip_address:16}[ERROR] log_count = 0")
    if logs_quantity - log_count > 1:
        print(f"{device.hostname:23}{device.ip_address:16}[ERROR] log match error: {logs_quantity}-{log_count}= {logs_quantity - log_count}")
        
    device.logs_formatted = logs
    device.logs_formatted_count = day_logs_count
    device.log_count_per_day = logs_per_day


def logs_to_dict(yr, dy, tm, lg, lgs):

    if lgs.get(yr):
        if lgs[yr].get(dy):
            if lgs[yr][dy].get(tm):
                i = 1
                while True:
                    tm_final = f"{tm}-{i}"    # 18:45:24.699-1
                    if lgs[yr][dy].get(tm_final):
                        i += 1
                    else:
                        lgs[yr][dy][tm_final] = lg
                        break
            else:
                lgs[yr][dy][tm] = lg
        else:
            lgs[yr][dy] = {tm: lg}
    else:
        lgs[yr] = {dy: {tm: lg}}    


def logs_per_day_count_summary(dy, lgs):

    if lgs.get(dy):
        lgs[dy] += 1
    else:
        lgs[dy] = 1


def logs_per_day_count_brief(yr, dy, lg, lgs):   

    if lgs.get(yr):
        if lgs[yr].get(dy):
            if lgs[yr][dy].get(lg):
                lgs[yr][dy][lg] += 1
            else:
                lgs[yr][dy][lg] = 1
        else:
            lgs[yr][dy] = {lg: 1}                
    else:
        lgs[yr] = {dy: {lg: 1}}


def export_last_logs_summary(dv, dys, yr):
    output = [] # dy1, dy2, ..., summary
    output_severity = []
    logs_summary = 0
    logs_sev_summary = 0
    log_alarm = {}  # alarm log for high severity

    for dy in dys:    
        if yr in dv.logs_formatted:
            if dy in dv.logs_formatted[yr]:
                log_quantity =  len(dv.logs_formatted[yr][dy])
                severity_quantity, dy_log_alarm = define_high_severity(dv, yr, dy)
                if dy_log_alarm:
                    for i in dy_log_alarm:
                        if log_alarm.get(i):
                            log_alarm[i].append(dy)
                        else:
                            log_alarm[i] = [dy]

                logs_summary += log_quantity
                logs_sev_summary += severity_quantity

                output.append(log_quantity)
                output_severity.append(severity_quantity)
            else:
                output.append(0)
                output_severity.append(0)

    if log_alarm:
        for k,v in log_alarm.items():
            print(f"{dv.hostname:23}{dv.ip_address:16}high severity {k}:  {', '.join(v)}")

    output.append(logs_summary)
    output_severity.append(logs_sev_summary)
    output_str = ",".join((str(i) for i in output))
    output_severity_str = ",".join((str(i) for i in output_severity))
    
    return output_str, output, output_severity_str


def define_high_severity(dv, yr, dy):

    severity_high = ("-1-", "-2-")
    severity = ("-3-")
    severity_count = 0
    lg_alarm = []

    for lg in dv.logs_formatted[yr][dy].values():
        if any(i in lg for i in severity_high):
            severity_count += 1
            if lg not in lg_alarm:
                lg_alarm.append(lg)
        elif any(i in lg for i in severity):
            severity_count += 1
    
    return severity_count, lg_alarm


def generate_last_days_list(xdys):
    # last xdys days period: 21 days
    now = datetime.now()
    year = now.strftime("%Y")   # 2022
    day_period = []     # days list in cisco format May  1, May 20

    for i in reversed(range(xdys)):
        dy = now - timedelta(days = i)
        dy1 = dy.strftime("%b %d")
        dy2 = dy1.split() 
        dy3 = f"{dy2[0]}{dy2[1].lstrip('0'):>3}"
        
        day_period.append(dy3)

    return day_period, year


def summary_all_devs_per_day(logs_summary_lists, pr):
    output = [0 for a in range(pr)]
    for i in range(pr):
        for logs_list in logs_summary_lists:
            output[i] += logs_list[i]

    output_str = ",".join((str(i) for i in output))
    return output_str


def check_logs_quantity(dv):
    lgs_quantity = 0
    for line in dv.show_log.splitlines():
        if line != "\n" and line != "":
            lgs_quantity += 1

        if "Log Buffer" in line:  # Log Buffer (1024000 bytes):
            lgs_quantity = 0

    return lgs_quantity


#######################################################################################
# ------------------------------              ----------------------------------------#
#######################################################################################

def connect_device(my_username, my_password, dev_queue, settings):
    while True:
        dev = dev_queue.get()
        i = 0
        while True:
            try:
                dev.ssh_conn = ConnectHandler(device_type=dev.os_type, ip=dev.ip_address,
                                              username=my_username, password=my_password)
                dev.show_commands()
                dev.parse(dev)

                dev.ssh_conn.disconnect()
                dev_queue.task_done()
                break

            except NetMikoTimeoutException as err_msg:
                dev.connection_status = False
                dev.connection_error_msg = str(err_msg)
                print(f"{dev.hostname:23}{dev.ip_address:16}timeout")
                dev_queue.task_done()
                break
                 
            except Exception as err_msg:
                if i == 2:  # tries
                    dev.connection_status = False
                    dev.connection_error_msg = str(err_msg)
                    print(f"{dev.hostname:23}{dev.ip_address:16}{'BREAK connection failed':20} i={i}")
                    dev_queue.task_done()
                    break
                else:
                    i += 1
                    dev.reset()
                    # print(f"{dev.hostname:23}{dev.ip_address:16}ERROR connection failed i={i}")
                    time.sleep(5)


#######################################################################################
# ------------------------------ main part -------------------------------------------#
#######################################################################################

start_time = datetime.now()
current_date = start_time.strftime("%Y.%m.%d")
current_time = start_time.strftime("%H.%M")

log_folder = Path(f"{Path.cwd()}/logs/{current_date}/")  # current dir / logs / date /
log_folder.mkdir(exist_ok=True)

q = queue.Queue()

settings = get_arguments(argv)
username, password = get_user_pw()
devices = get_device_info("devices.csv")

total_devices = len(devices)

print(
    "\n"
    f"Total devices: {total_devices}\n"
    "-------------------------------------------------------------------------------------------------------\n"
    "hostname               ip address      comment\n"
    "---------------------- --------------- ----------------------------------------------------------------\n"
)

for i in range(settings["maxth"]):
    thread = Thread(target=connect_device, args=(username, password, q, settings))
    thread.daemon = True
    thread.start()

for device in devices:
    q.put(device)

q.join()

failed_connection_count = write_logs(devices, current_time, log_folder, settings)
duration = datetime.now() - start_time
duration_time = timedelta(seconds=duration.seconds)

print("\n"
      "-------------------------------------------------------------------------------------------------------\n"
      f"failed connection:.....{failed_connection_count}\n"
      f"elapsed time:..........{duration_time}\n"
      "-------------------------------------------------------------------------------------------------------")
