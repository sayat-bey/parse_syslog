import yaml
import time
import queue
import re
from threading import Thread
from pprint import pformat, pprint
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

        self.logs_dict = {} # day: {timestamp: log}
        self.bad_logs_qnt = 0
        self.all_logs_qnt = 0

    def show_commands(self):
        self.show_log = self.ssh_conn.send_command(r"show logging")
        self.show_timestamps = self.ssh_conn.send_command(r"show running-config | include service timestamps log")

    def reset(self):
        self.logs_dict = {} # day: {timestamp: log}
        self.bad_logs_qnt = 0
        self.all_logs_qnt = 0


class PaggXR(CellSiteGateway):

    def __init__(self, ip, host):
        CellSiteGateway.__init__(self, ip, host)
        self.os_type = "cisco_xr"


class PaggXE(CellSiteGateway):

    def __init__(self, ip, host):
        CellSiteGateway.__init__(self, ip, host)
        self.os_type = "cisco_xe"


#######################################################################################
# ------------------------------ def function part -----------------------------------#
#######################################################################################

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


def write_logs(devices, current_time, log_folder, xdays, year, period):
    failed_conn_count = 0
    unavailable_device = []

    conn_msg = log_folder / f"{current_time}_connection_error_msg.txt"
    device_info = log_folder / f"{current_time}_device_info.txt"
    last_logs_summary = log_folder / f"{current_time}_last_{period}_days_log_summary.txt"
    logs_sfp = log_folder / f"{current_time}_sfp_removed_logs.txt"

    conn_msg_file = open(conn_msg, "w")
    device_info_file = open(device_info, "w")
    last_logs_summary_file = open(last_logs_summary, "w")
    logs_sfp_file = open(logs_sfp, "w")

    last_logs_summary_file.write(f"summary logs for last {period} days period\n\n\n")
    last_logs_summary_file.write(f"hostname,{','.join(xdays)},summary\n")
    logs_sfp_file.write(f"logs when sfp removed for last {period} days period\n\n\n")

    for device in devices:
        if device.connection_status:
            export_device_info(device, device_info_file)  # export device info: show, status, etc
            xdays_summ_str, xdays_summ_int, xdays_severity = export_last_logs_summary(device, xdays, year)
            last_logs_summary_file.write(f"{device.hostname},{xdays_summ_str}\n")
            severity_logs_summ_file.write(f"{device.hostname},{xdays_severity}\n")
            xdays_summ_last_row.append(xdays_summ_int)

            for year, year_value in device.logs_formatted_brief.items():
                for day, day_value in year_value.items():
                    for log, log_value in day_value.items():
                        logs_file.write(f"{device.hostname},{year},{day},{log},{str(log_value)}\n")    

            logs_sfp = check_logs_sfp(device, xdays, year)
            if logs_sfp:
                for i in logs_sfp:
                    logs_sfp_file.write(f"{i}\n")

            count_mismatched_logs(device, xdays, period)
            
        else:
            failed_conn_count += 1
            conn_msg_file.write("-" * 80 + "\n")
            conn_msg_file.write(f"### {device.hostname} : {device.ip_address} ###\n\n")
            conn_msg_file.write(f"{device.connection_error_msg}\n")
            unavailable_device.append(f"{device.hostname} : {device.ip_address}")
    
    conn_msg_file.close()
    device_info_file.close()
    last_logs_summary_file.close()
    logs_sfp_file.close()

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
    export_file.write("device.logs_formatted\n\n")
    export_file.write(pformat(dev.logs_formatted))
    export_file.write("\n\n")

    export_file.write("-" * 80 + "\n")
    export_file.write("device.logs_formatted_brief\n\n")
    export_file.write(pformat(dev.logs_formatted_brief))
    export_file.write("\n\n")


#######################################################################################
# ------------------------------ get bs port -----------------------------------------#
#######################################################################################

def fn_parse_logs(device, xdays, year):

    pattern = re.compile(r"(\w{3} +\d{1,2}) +(\d{4}) +(\d{2}:\d{2}:\d{2}.\d+)(?: \w*)?: +(.*)")
        # 000070: (Apr 21) (2022) (02:37:19.435) ALA: (The VLAN 4093 will be internally used for this clock port.)
    pattern_xr = re.compile(r"(\d{4}) +(\w{3} +\d{1,2}) +(\d{2}:\d{2}:\d{2}.\d+)(?: \w*)?: +(.*)")
        # RP/0/RSP0/CPU0:(2021) (Aug  3) (11:32:55.412) ALA: (pwr_mgmt[392]: %PLATFORM-PWR_MGMT-4-MODULE_WARNING : Power-module warning)

    log_count = 0

    for line in device.show_log.splitlines():
        match = re.search(pattern, line)
        match_xr = re.search(pattern_xr, line)

        if match:
            log_count += 1
            date = match[1]
            log_year = match[2]
            timestamp = match[3]
            log = match[4]
            
            fn_logs_to_dict(device, xdays, year, date, log_year, timestamp, log)

        elif match_xr:
            log_count += 1
            date = match[2]
            log_year = match[1]
            timestamp = match[3]
            log = match[4]           

            fn_logs_to_dict(device, xdays, year, date, log_year, timestamp, log)
    
    if log_count == 0:
        print(f"{device.hostname:23}{device.ip_address:16}[NOTE] log_count = 0")


def fn_logs_to_dict(device, xdays, year, date, log_year, timestamp, log):
    
    for i in xdays:
        device.logs_dict[i] = {}  # day: {timestamp: log}

    if year == log_year:
        if date in xdays:
            if device.logs_dict[date].get(timestamp):
                i = 1
                while True:
                    tm_final = f"{timestamp}-{i}"    # 18:45:24.699-1
                    if device.logs_dict[date].get(tm_final):
                        i += 1
                    else:
                        device.logs_dict[date][tm_final] = log
                        break
            else:
                device.logs_dict[date][timestamp] = log


def fn_count_bad_logs(device, xdays, year, date, log_year):

    pattern = re.compile(r"(\w{3} +\d{1,2}) +(\d{4}) +(\d{2}:\d{2}:\d{2}.\d+)(?: \w*)?: +(.*)")
        # 000070: (Apr 21) (2022) (02:37:19.435) ALA: (The VLAN 4093 will be internally used for this clock port.)
    pattern_xr = re.compile(r"(\d{4}) +(\w{3} +\d{1,2}) +(\d{2}:\d{2}:\d{2}.\d+)(?: \w*)?: +(.*)")
        # RP/0/RSP0/CPU0:(2021) (Aug  3) (11:32:55.412) ALA: (pwr_mgmt[392]: %PLATFORM-PWR_MGMT-4-MODULE_WARNING : Power-module warning)

    day_matched = False

    for line in device.show_log.splitlines():
        match = re.search(pattern, line)
        match_xr = re.search(pattern_xr, line)

        if match:
            date = match[1]
            log_year = match[2]

            if log_year == year and not day_matched and date in xdays:
                day_matched = True

        elif match_xr:
            date = match[2]
            log_year = match[1]
          
            if log_year == year and not day_matched and date in xdays:
                day_matched = True

        else:
            if day_matched:
                device.bad_logs_qnt += 1


def fn_export_last_logs_summary(device):
    output = [] # dy1, dy2, ..., summary
    summary = 0

    for day_dict in device.logs_dict.values():
        output.append(len(day_dict))
        summary += len(day_dict)

    output.append(summary)
    output_str = ",".join((str(i) for i in output))

    return output_str, output, 


def fn_define_high_severity(device):
    severity_high = ("-1-", "-2-", "-3-")
    output = []

    for day, day_dict in device.logs_dict.items():
        for tms, log in day_dict.items():
            if any(i in log for i in severity_high):
                output.append(f"{device.hostname},{day},{tms},{log}")

    if output:
        print(f"{device.hostname:23}{device.ip_address:16}[NOTE] xFP is removed (see attached file)")


def fn_last_days(period):
    # period: last days period: 21 days
    now = datetime.now()
    year = now.strftime("%Y")   # 2022
    xdays = []                  # day list in cisco format May  1, May 2,... May N

    for i in reversed(range(period)):
        day = now - timedelta(days = i)
        dayi = day.strftime("%b %d")    # May 01 or May 22
        dayii = dayi.split()             # May, 01
        dayiii = f"{dayii[0]}{dayii[1].lstrip('0'):>3}"     # May  1 or May 22
        
        xdays.append(dayiii)

    return xdays, year


def fn_count_logs(device):
    buffer_matched = False  # Log Buffer (1024000 bytes):

    for line in device.show_log.splitlines():
        if buffer_matched:
            if line != "\n" and line != "":
                device.all_logs_qnt += 1
        else:
            if "Log Buffer" in line: 
                buffer_matched = True


def fn_check_timestamps(device):
    tmstmp = "service timestamps log datetime msec localtime show-timezone year"
    tmstmpxr = "service timestamps log datetime localtime msec show-timezone year"

    if device.os_type == "cisco_ios" or device.os_type == "cisco_xe":
        if tmstmp not in device.show_timestamps.splitlines():
            print(f"{device.hostname:23}{device.ip_address:16}[ERROR] check timestamp")
    
    elif device.os_type == "cisco_xr":
        if tmstmpxr not in device.show_timestamps.splitlines():
            print(f"{device.hostname:23}{device.ip_address:16}[ERROR] check timestamp")
    

def fn_check_logs_sfp(device):
    output = []
    
    for day, day_dict in device.logs_dict.items():
        for tms, log in day_dict.items():
            if "Transceiver module removed" in log:
                output.append(f"{device.hostname},{day},{tms},{log}")
            if "is removed" in log and "xFP" in log:
                output.append(f"{device.hostname},{day},{tms},{log}")

    if output:
        print(f"{device.hostname:23}{device.ip_address:16}[NOTE] xFP is removed (see attached file)")

    return output


#######################################################################################
# ------------------------------              ----------------------------------------#
#######################################################################################

def connect_device(my_username, my_password, dev_queue, xdays, year):
    while True:
        dev = dev_queue.get()
        i = 0
        while True:
            try:
                dev.ssh_conn = ConnectHandler(device_type=dev.os_type, ip=dev.ip_address,
                                              username=my_username, password=my_password)
                dev.show_commands()
                fn_parse_logs(device, xdays, year)
                check_timestamps(dev)

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
                    time.sleep(5)


#######################################################################################
# ------------------------------ main part -------------------------------------------#
#######################################################################################

start_time = datetime.now()
current_date = start_time.strftime("%Y.%m.%d")
current_time = start_time.strftime("%H.%M")

period = 21     # last 21 days period
xdays, year = fn_last_days(period) 

log_folder = Path(f"{Path.cwd()}/logs/{current_date}/")  # current dir / logs / date /
log_folder.mkdir(exist_ok=True)

q = queue.Queue()

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

for i in range(20):
    thread = Thread(target=connect_device, args=(username, password, q, xdays, year))
    thread.daemon = True
    thread.start()

for device in devices:
    q.put(device)

q.join()

failed_connection_count = write_logs(devices, current_time, log_folder, xdays, year, period)
duration = datetime.now() - start_time
duration_time = timedelta(seconds=duration.seconds)

print("\n"
      "-------------------------------------------------------------------------------------------------------\n"
      f"failed connection:.....{failed_connection_count}\n"
      f"elapsed time:..........{duration_time}\n"
      "-------------------------------------------------------------------------------------------------------")
