import socket
import zlib
import zstandard as zstd
import struct
import os
import concurrent.futures
import threading
from enum import Enum
from typing import Tuple, Any
from Crypto.Cipher import AES
from colorama import init, Fore, Style, Back

# Initialize colorama for Windows/Linux terminal support
init(autoreset=True)

# ── SDP DATA STRUCTURES (Core Protocol) ──────────────────────────────
class SdpDataType(Enum):
    INTEGER_POSITIVE = 0
    INTEGER_NEGATIVE = 1
    FLOAT = 2
    DOUBLE = 3
    STRING = 4
    LIST = 5
    DICT = 6
    STRUCT_BEGIN = 7
    STRUCT_END = 8

class SdpException(Exception):
    pass

class SdpStruct(dict):
    def __init__(self, data=None):
        super().__init__()
        self.data = b''
        self.offset = 0
        if isinstance(data, bytes):
            self.data = data
            self.offset = 0
            self._unpack_from_binary()
        elif data is not None:
            super().update(data)
            self._pack_to_binary()

    def _pack_to_binary(self):
        self.data = bytes([SdpDataType.STRUCT_BEGIN.value << 4])
        for tag, value in sorted(self.items()):
            self._pack(tag, value)
        self.data += bytes([SdpDataType.STRUCT_END.value << 4])

    def _unpack_from_binary(self):
        if not self.data: return
        if self.data[0] >> 4 == SdpDataType.STRUCT_BEGIN.value:
            self.offset = 1
        while self.offset < len(self.data):
            tag, value = self._unpack()
            if isinstance(value, SdpDataType) and value == SdpDataType.STRUCT_END:
                break
            self[tag] = value

    def _write_number(self, value: int) -> bytes:
        result = bytearray()
        while value >= 0x80:
            result.append((value & 0x7F) | 0x80)
            value >>= 7
        result.append(value & 0x7F)
        return bytes(result)

    def _read_number(self) -> int:
        n = 1
        val = self.data[self.offset] & 0x7F
        while self.data[self.offset + n - 1] >= 0x80:
            val |= (self.data[self.offset + n] & 0x7F) << (7 * n)
            n += 1
        self.offset += n
        return val

    def _pack_header(self, tag: int, data_type: SdpDataType) -> None:
        if tag < 15:
            self.data += bytes([(data_type.value << 4) | tag])
        else:
            self.data += bytes([(data_type.value << 4) | 15])
            self.data += self._write_number(tag)

    def _pack(self, tag: int, value: Any) -> None:
        if isinstance(value, bool):
            self._pack_header(tag, SdpDataType.INTEGER_POSITIVE)
            self.data += self._write_number(1 if value else 0)
        elif isinstance(value, int):
            if value < 0:
                self._pack_header(tag, SdpDataType.INTEGER_NEGATIVE)
                self.data += self._write_number(-value)
            else:
                self._pack_header(tag, SdpDataType.INTEGER_POSITIVE)
                self.data += self._write_number(value)
        elif isinstance(value, float):
            self._pack_header(tag, SdpDataType.DOUBLE)
            packed = struct.pack("<d", value)
            self.data += self._write_number(len(packed))
            self.data += packed
        elif isinstance(value, (str, bytes)):
            self._pack_header(tag, SdpDataType.STRING)
            encoded = value.encode('utf-8') if isinstance(value, str) else value
            self.data += self._write_number(len(encoded))
            self.data += encoded
        elif isinstance(value, list):
            self._pack_header(tag, SdpDataType.LIST)
            self.data += self._write_number(len(value))
            for item in value:
                self._pack(0, item)
        elif isinstance(value, dict):
            if isinstance(value, SdpStruct):
                self._pack_header(tag, SdpDataType.STRUCT_BEGIN)
                for k, v in sorted(value.items()):
                    self._pack(k, v)
                self.data += bytes([SdpDataType.STRUCT_END.value << 4])
            else:
                self._pack_header(tag, SdpDataType.DICT)
                self.data += self._write_number(len(value))
                for k, v in sorted(value.items()):
                    self._pack(0, k)
                    self._pack(0, v)
        else:
            raise SdpException(f"Unsupported type: {type(value)}")

    def _unpack(self) -> Tuple[int, Any]:
        try:
            if self.offset >= len(self.data): return 0, None
            header = self.data[self.offset]
            tag = header & 0xF
            data_type = SdpDataType(header >> 4)
            self.offset += 1
            if tag == 15: tag = self._read_number()

            if data_type == SdpDataType.INTEGER_POSITIVE: return tag, self._read_number()
            elif data_type == SdpDataType.INTEGER_NEGATIVE: return tag, -self._read_number()
            elif data_type == SdpDataType.FLOAT:
                val = self._read_number().to_bytes(4, 'little')
                return tag, struct.unpack("<f", val)[0]
            elif data_type == SdpDataType.DOUBLE:
                val = self._read_number().to_bytes(8, 'little')
                return tag, struct.unpack("<d", val)[0]
            elif data_type == SdpDataType.STRING:
                length = self._read_number()
                try: val = self.data[self.offset:self.offset+length].decode('utf-8')
                except UnicodeDecodeError: val = self.data[self.offset:self.offset+length]
                self.offset += length
                return tag, val
            elif data_type == SdpDataType.LIST:
                length = self._read_number()
                val = []
                for _ in range(length):
                    _, item = self._unpack()
                    val.append(item)
                return tag, val
            elif data_type == SdpDataType.DICT:
                length = self._read_number()
                val = {}
                for _ in range(length):
                    _, k = self._unpack()
                    _, v = self._unpack()
                    val[k] = v
                return tag, val
            elif data_type == SdpDataType.STRUCT_BEGIN:
                struct_data = {}
                while True:
                    sub_tag, sub_value = self._unpack()
                    if isinstance(sub_value, SdpDataType) and sub_value == SdpDataType.STRUCT_END:
                        break
                    struct_data[sub_tag] = sub_value
                return tag, SdpStruct(struct_data)
            elif data_type == SdpDataType.STRUCT_END:
                return tag, SdpDataType.STRUCT_END
            else: raise SdpException("Unknown data type")
        except Exception:
            raise SdpException("Unpack error")

# ── NETWORK CONNECTIONS ──────────────────────────────────────────────
AES_KEY = bytes.fromhex('f5a193d50ade553e9835595f5cd75ddd')
AES_IV = b'\x00' * 16

class BanCheckerConnection:
    def __init__(self, device_id: str):
        self.host = 'login.ml.youngjoygame.com'
        self.port = 30021
        self.sequence = 1
        self.socket = None
        self.queue_data = b''
        self.device_id = device_id
        
        parts = device_id.split('_')
        device_info = parts[1] if len(parts) >= 2 else device_id
        if len(parts) >= 3 and len(device_info) < 32:
            device_info = device_info + "_" + parts[2]

        if len(device_info) >= 32:
            self.imei_md5 = device_info[:32]
            self.android_id = device_info[32:48] if len(device_info) >= 48 else ""
            self.advertising_id = device_info[48:] if len(device_info) > 48 else ""
        else:
            self.imei_md5 = device_id
            self.android_id = ""
            self.advertising_id = ""

        self.channel = 'and_usa'
        self.client_version = '2.2.16.1232.1'
        self.account_id = 0
        self.session_key = ''
        self.zone_id = 0
        self.game_server_host = ''
        self.game_server_port = 0

    def connect(self, host=None, port=None):
        if host: self.host = host
        if port: self.port = port
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.connect((self.host, self.port))
        self.socket.settimeout(5)

    def cleanup(self):
        if self.socket:
            self.socket.close()
            self.sequence = 1
            self.socket = None

    def send_data(self, pkt_id, sdp):
        packet = SdpStruct({
            0: pkt_id,
            1: self.sequence,
            5: sdp.data
        }).data
        buf = zstd.compress(packet)
        flags = (len(buf) + 4) | (16 << 24)
        buf = flags.to_bytes(4, 'big') + buf
        self.socket.send(buf)
        self.sequence += 1

    def recv_data(self):
        try:
            while len(self.queue_data) < 4:
                data = self.socket.recv(4096)
                if not data: return None, None
                self.queue_data += data

            flags = int.from_bytes(self.queue_data[:4], 'big')
            size = flags & 0xFFFFFF
            compression_type = flags >> 24

            while len(self.queue_data) < size:
                data = self.socket.recv(4096)
                if not data: return None, None
                self.queue_data += data

            data = self.queue_data[4:size]
            self.queue_data = self.queue_data[size:]

            if compression_type == 1:
                data = zlib.decompress(data)
            elif compression_type == 16:
                data = zstd.decompress(data)
            elif compression_type in (2, 3, 18):
                cipher = AES.new(AES_KEY, AES.MODE_CBC, iv=AES_IV)
                data = cipher.decrypt(data[:-1] if len(data) % 16 != 0 else data)
                data = data.rstrip(b'\x00')
                if compression_type == 3: data = zlib.decompress(data)
                elif compression_type == 18: data = zstd.decompress(data)

            result = SdpStruct(data)
            pkt_id = result[0]
            if pkt_id is None: return None, None

            res = result.get(6) or result.get(5)
            if not res or not isinstance(res, bytes):
                return pkt_id, None

            return pkt_id, SdpStruct(res)

        except socket.timeout: return -1, None
        except Exception: return None, None

# ── BAN REASON MAPPING & HELPER ──────────────────────────────────────
BAN_REASONS = {
    "21": "Using Plug-in Apps to Compromise Competitive Fairness",
}

def inspect_for_ban(pkt_id, sdp_data):
    is_banned = False
    details = {}

    if sdp_data:
        def scan(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k == 'ban_reason':
                        code_str = str(v)
                        details['ban_code'] = code_str
                        details['reason_name'] = BAN_REASONS.get(code_str, "Using Plug-in Apps to Compromise Competitive Fairness")
                    elif k in ('ban_status', 'ban_time') or (isinstance(k, str) and 'ban' in k.lower()):
                        details[str(k)] = v
                    
                    if k == 'endtime_day': details['endtime_day'] = v
                    if k == 'endtime_hour': details['endtime_hour'] = v
                    if k == 'endtime_min': details['endtime_min'] = v
                    if k == 'endtime_sec': details['endtime_sec'] = v

                    if isinstance(v, (dict, list)): scan(v)
            elif isinstance(obj, list):
                for item in obj: scan(item)

        scan(dict(sdp_data))

    # STRICT CHECK: Banned ONLY if explicit 'endtime_day' exists
    if 'endtime_day' in details and details['endtime_day'] is not None:
        is_banned = True

    return is_banned, details

# ── UI FORMATTING HELPERS ────────────────────────────────────────────
def print_banner(device_id):
    print(f"\n{Fore.MAGENTA}╔══════════════════════════════════════════════════════════════════╗")
    print(f"{Fore.MAGENTA}║ {Fore.CYAN} {Style.BRIGHT}MLBB BAN CHEKER BY POYZZ{Fore.MAGENTA}                                  ║")
    print(f"{Fore.MAGENTA}╠══════════════════════════════════════════════════════════════════╣")
    print(f"{Fore.MAGENTA}║ {Fore.YELLOW}TARGET DEVICE ID:{Fore.MAGENTA}                                                ║")
    print(f"{Fore.MAGENTA}║ {Fore.WHITE}{device_id[:60]:<60} {Fore.MAGENTA}║")
    print(f"{Fore.MAGENTA}╚══════════════════════════════════════════════════════════════════╝\n")

def print_step(step_num, message):
    print(f"{Fore.CYAN}[*] Step {step_num}/3:{Style.RESET_ALL} {message}")

def print_success(message):
    print(f"{Fore.GREEN}[+] {message}{Style.RESET_ALL}")

def print_error(message):
    print(f"{Fore.RED}[-] {message}{Style.RESET_ALL}")

def print_ban_result(ban_info, stage):
    print(f"\n{Back.RED}{Fore.WHITE}{Style.BRIGHT} [!] BAN DETECTED DURING {stage} {Style.RESET_ALL}")
    print(f"{Fore.RED}┌─────────────────────────────────────────────────────────────┐")
    
    reason = ban_info.get('reason_name', 'Using Plug-in Apps to Compromise Competitive Fairness')
    print(f"{Fore.RED}│ {Fore.YELLOW}Reason Name: {Fore.WHITE}{reason}")
    if 'ban_code' in ban_info:
        print(f"{Fore.RED}│ {Fore.YELLOW}Ban Code:    {Fore.WHITE}{ban_info['ban_code']}")
        
    day = ban_info.get('endtime_day', '?')
    hour = ban_info.get('endtime_hour', '00')
    minute = ban_info.get('endtime_min', '00')
    sec = ban_info.get('endtime_sec', '00')
    
    print(f"{Fore.RED}│ {Fore.YELLOW}Duration:    {Fore.WHITE}Day {day}, {hour}:{minute}:{sec}")
        
    print(f"{Fore.RED}└─────────────────────────────────────────────────────────────┘")
    print(f"{Fore.RED}{Style.BRIGHT}>>> RESULT: ACCOUNT / DEVICE IS BANNED. <<<{Style.RESET_ALL}\n")

def print_clean_result(account_id=None, zone_id=None):
    print(f"\n{Back.GREEN}{Fore.WHITE}{Style.BRIGHT} [✓] ROLE LOGIN SUCCESS {Style.RESET_ALL}")
    print(f"{Fore.GREEN}┌─────────────────────────────────────────────────────────────┐")
    print(f"{Fore.GREEN}│ {Fore.WHITE}Status: {Fore.GREEN}CLEAN / ACTIVE                                      {Fore.GREEN}│")
    if account_id is not None:
        print(f"{Fore.GREEN}│ {Fore.WHITE}Account ID: {Fore.CYAN}{account_id}{' ' * (40 - len(str(account_id)))}{Fore.GREEN}│")
    if zone_id is not None:
        print(f"{Fore.GREEN}│ {Fore.WHITE}Zone ID:    {Fore.CYAN}{zone_id}{' ' * (40 - len(str(zone_id)))}{Fore.GREEN}│")
    print(f"{Fore.GREEN}└─────────────────────────────────────────────────────────────┘")
    print(f"{Fore.GREEN}{Style.BRIGHT}>>> RESULT: NO BAN DETECTED FOR THIS DEVICE / ACCOUNT. <<<{Style.RESET_ALL}\n")

# ── DEBUG CHECKER FLOW ────────────────────────────────────────────────
def check_device_ban(device_id: str):
    print_banner(device_id)
    conn = BanCheckerConnection(device_id)

    try:
        # STEP 1
        print_step(1, "Authenticating with Login Server...")
        conn.connect('login.ml.youngjoygame.com', 30021)

        conn.send_data(1, SdpStruct({
            0: conn.device_id,
            1: f'gps_adid={conn.advertising_id}&android_id={conn.android_id}&device_unique_id={conn.imei_md5}',
            2: conn.client_version,
            3: conn.channel,
            4: 'en'
        }))

        pkt_id, res = conn.recv_data()
        banned, ban_info = inspect_for_ban(pkt_id, res)
        
        if banned:
            print_ban_result(ban_info, "LOGIN SERVER")
            return

        if pkt_id == 2 and res:
            conn.account_id = res.get(0)
            conn.session_key = res.get(1)
            zone_data = res.get(2)
            if isinstance(zone_data, dict): conn.zone_id = zone_data.get(0, 0)
            elif isinstance(zone_data, list) and len(zone_data) > 0:
                conn.zone_id = zone_data[0] if not isinstance(zone_data[0], dict) else zone_data[0].get(0, 0)
            else: conn.zone_id = zone_data or 0

            print_success(f"Login Success! Account ID: {conn.account_id} | Zone: {conn.zone_id}")
        else:
            print_error("Login server connection failed.")
            return

        # STEP 2
        print_step(2, "Requesting Game Server Details...")
        conn.send_data(5, SdpStruct({
            0: conn.account_id, 1: conn.session_key, 2: conn.client_version,
            5: conn.zone_id, 6: conn.channel
        }))

        pkt_id, res = conn.recv_data()
        banned, ban_info = inspect_for_ban(pkt_id, res)
        
        if banned:
            print_ban_result(ban_info, "GAME SERVER SELECTION")
            return

        if pkt_id == 6 and res:
            game_server = res[1]
            conn.game_server_host, conn.game_server_port = game_server.split(':')
            conn.game_server_port = int(conn.game_server_port)
            print_success(f"Game Server Assigned: {conn.game_server_host}:{conn.game_server_port}")
        else:
            print_error("Failed to fetch Game Server endpoint.")
            return

        # STEP 3
        print_step(3, "Verifying Device & Role Status...")
        conn.cleanup()
        conn.connect(conn.game_server_host, conn.game_server_port)

        conn.send_data(10001, SdpStruct({
            0: conn.account_id, 1: conn.session_key, 2: conn.zone_id,
            4: conn.client_version, 13: conn.channel, 15: conn.device_id
        }))
        conn.send_data(10101, SdpStruct({0: 0, 2: 2}))

        role_requested = False
        
        while True:
            pkt_id, res = conn.recv_data()
            banned, ban_info = inspect_for_ban(pkt_id, res)
            
            if banned:
                print_ban_result(ban_info, "ROLE VERIFICATION")
                return

            if pkt_id is None:
                print_error("Connection closed by the server.")
                break
            elif pkt_id == 10002 and not role_requested:
                conn.send_data(10003, SdpStruct({
                    0: conn.account_id, 1: conn.session_key, 2: conn.zone_id,
                    3: conn.client_version, 4: conn.channel, 5: conn.device_id
                }))
                role_requested = True
            elif pkt_id in (10004, 10008):
                print_clean_result(conn.account_id, conn.zone_id)
                break
            elif pkt_id == -1:
                print_error("Request timed out.")
                break

    except Exception as e:
        print_error(f"Unexpected error: {e}")
    finally:
        conn.cleanup()

# ── SILENT BULK CHECKER FLOW ──────────────────────────────────────────
def format_ban_string(device_id: str, ban_info: dict) -> str:
    reason = ban_info.get('reason_name', 'Using Plug-in Apps to Compromise Competitive Fairness')
    day = ban_info.get('endtime_day')
    hour = ban_info.get('endtime_hour', '00')
    minute = ban_info.get('endtime_min', '00')
    sec = ban_info.get('endtime_sec', '00')
    return f"{device_id} |  Reason Name: {reason} |  Duration: Day {day}, {hour}:{minute}:{sec}"

def format_clean_string(device_id: str, account_id: int, zone_id: int) -> str:
    return f"Device ID: {device_id}\nAccount ID: {account_id}\nZone ID: {zone_id}\n"

def check_device_ban_silent(device_id: str) -> Tuple[str, str]:
    conn = BanCheckerConnection(device_id)
    try:
        conn.connect('login.ml.youngjoygame.com', 30021)
        conn.send_data(1, SdpStruct({
            0: conn.device_id,
            1: f'gps_adid={conn.advertising_id}&android_id={conn.android_id}&device_unique_id={conn.imei_md5}',
            2: conn.client_version,
            3: conn.channel,
            4: 'en'
        }))

        pkt_id, res = conn.recv_data()
        banned, ban_info = inspect_for_ban(pkt_id, res)
        if banned: return "BANNED", format_ban_string(device_id, ban_info)

        if pkt_id == 2 and res:
            conn.account_id = res.get(0)
            conn.session_key = res.get(1)
            zone_data = res.get(2)
            if isinstance(zone_data, dict): conn.zone_id = zone_data.get(0, 0)
            elif isinstance(zone_data, list) and len(zone_data) > 0:
                conn.zone_id = zone_data[0] if not isinstance(zone_data[0], dict) else zone_data[0].get(0, 0)
            else: conn.zone_id = zone_data or 0
        else:
            return "UNKNOWN", device_id

        conn.send_data(5, SdpStruct({
            0: conn.account_id, 1: conn.session_key, 2: conn.client_version,
            5: conn.zone_id, 6: conn.channel
        }))

        pkt_id, res = conn.recv_data()
        banned, ban_info = inspect_for_ban(pkt_id, res)
        if banned: return "BANNED", format_ban_string(device_id, ban_info)

        if pkt_id == 6 and res:
            game_server = res[1]
            conn.game_server_host, conn.game_server_port = game_server.split(':')
            conn.game_server_port = int(conn.game_server_port)
        else:
            return "UNKNOWN", device_id

        conn.cleanup()
        conn.connect(conn.game_server_host, conn.game_server_port)

        conn.send_data(10001, SdpStruct({
            0: conn.account_id, 1: conn.session_key, 2: conn.zone_id,
            4: conn.client_version, 13: conn.channel, 15: conn.device_id
        }))
        conn.send_data(10101, SdpStruct({0: 0, 2: 2}))

        role_requested = False
        while True:
            pkt_id, res = conn.recv_data()
            banned, ban_info = inspect_for_ban(pkt_id, res)
            
            if banned:
                return "BANNED", format_ban_string(device_id, ban_info)

            if pkt_id is None or pkt_id == -1:
                return "UNKNOWN", device_id
            elif pkt_id == 10002 and not role_requested:
                conn.send_data(10003, SdpStruct({
                    0: conn.account_id, 1: conn.session_key, 2: conn.zone_id,
                    3: conn.client_version, 4: conn.channel, 5: conn.device_id
                }))
                role_requested = True
            elif pkt_id in (10004, 10008):
                return "CLEAN", format_clean_string(device_id, conn.account_id, conn.zone_id)
    except Exception:
        return "UNKNOWN", device_id
    finally:
        conn.cleanup()

# ── THREADING & BULK LOGIC ──────────────────────────────────────────
progress_lock = threading.Lock()
file_lock = threading.Lock()
processed_count = 0
banned_count = 0
clean_count = 0
total_count = 0

def update_progress():
    global processed_count, banned_count, clean_count, total_count
    with progress_lock:
        print(f"\r{Fore.CYAN}proccessing {processed_count}/{total_count} | {Fore.GREEN}NOT BAN 100% : {clean_count} | {Fore.RED}BAN 100% : {banned_count}{Style.RESET_ALL}", end="")

def process_device_worker(device_id: str, ban_filepath: str, clean_filepath: str):
    global processed_count, banned_count, clean_count
    
    status, result_str = check_device_ban_silent(device_id)
    
    with file_lock:
        if status == "BANNED":
            banned_count += 1
            with open(ban_filepath, "a", encoding="utf-8") as f:
                f.write(result_str + "\n")
        elif status == "CLEAN":
            clean_count += 1
            with open(clean_filepath, "a", encoding="utf-8") as f:
                f.write(result_str + "\n")
        elif status == "UNKNOWN":
            with open(os.path.join(os.path.dirname(ban_filepath), "UNKNOWN.txt"), "a", encoding="utf-8") as f:
                f.write(result_str + "\n")
        
        processed_count += 1
    
    update_progress()

def run_bulk_mode():
    global processed_count, banned_count, clean_count, total_count
    processed_count = 0
    banned_count = 0
    clean_count = 0
    
    print(f"\n{Fore.YELLOW}--- BULK CHECK MODE ---{Style.RESET_ALL}")
    
    filepath = input(f"{Fore.CYAN}Enter filepath containing Device IDs: {Style.RESET_ALL}").strip()
    if not os.path.exists(filepath):
        print(f"{Fore.RED}File not found!{Style.RESET_ALL}")
        return

    try:
        threads_input = int(input(f"{Fore.CYAN}Enter number of threads (1-20 max): {Style.RESET_ALL}").strip())
        threads = max(1, min(20, threads_input))
    except ValueError:
        threads = 1
        print(f"{Fore.YELLOW}Invalid input, defaulting to 1 thread.{Style.RESET_ALL}")

    with open(filepath, "r", encoding="utf-8") as f:
        device_ids = [line.strip() for line in f if line.strip()]
    
    total_count = len(device_ids)
    if total_count == 0:
        print(f"{Fore.RED}No valid Device IDs found in file.{Style.RESET_ALL}")
        return

    # Target Directory setup
    # Termux/Android: save bulk results beside this script.
    base_dir = os.path.dirname(os.path.abspath(__file__))
    save_dir = os.path.join(base_dir, "ban_results")
    os.makedirs(save_dir, exist_ok=True)
    ban_file = os.path.join(save_dir, "BAN 100%.txt")
    clean_file = os.path.join(save_dir, "NOT BAN 100%.txt")

    print(f"{Fore.MAGENTA}Starting Bulk Checker with {threads} threads...{Style.RESET_ALL}\n")
    update_progress()

    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
        futures = [
            executor.submit(process_device_worker, d_id, ban_file, clean_file) 
            for d_id in device_ids
        ]
        concurrent.futures.wait(futures)

    print(f"\n\n{Fore.GREEN}[+] Bulk check completed!{Style.RESET_ALL}")
    print(f"Results saved to:\n- {ban_file}\n- {clean_file}\n")

# ── MAIN MENU ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    while True:
        print(f"\n{Fore.MAGENTA}╔══════════════════════════════════════════════════════════════════╗")
        print(f"{Fore.MAGENTA}║ {Fore.CYAN} {Style.BRIGHT}MLBB BAN CHEKER BY POYZZ{Fore.MAGENTA}                                    ║")
        print(f"{Fore.MAGENTA}╠══════════════════════════════════════════════════════════════════╣")
        print(f"{Fore.MAGENTA}║ {Fore.WHITE}1. Debug Version (Check 1 Device Only to see response){Fore.MAGENTA}           ║")
        print(f"{Fore.MAGENTA}║ {Fore.WHITE}2. Bulk Mode{Fore.MAGENTA}                                                     ║")
        print(f"{Fore.MAGENTA}║ {Fore.WHITE}3. Exit{Fore.MAGENTA}                                                          ║")
        print(f"{Fore.MAGENTA}╚══════════════════════════════════════════════════════════════════╝\n")
        
        choice = input(f"{Fore.CYAN}Select option (1-3): {Style.RESET_ALL}").strip()
        
        if choice == '1':
            device_id_input = input(f"\n{Fore.CYAN}Enter Device ID to test: {Style.RESET_ALL}").strip()
            if device_id_input:
                check_device_ban(device_id_input)
            else:
                print(f"{Fore.RED}No Device ID provided.{Style.RESET_ALL}")
        elif choice == '2':
            run_bulk_mode()
        elif choice == '3':
            print("Exiting...")
            break
        else:
            print(f"{Fore.RED}Invalid option selected.{Style.RESET_ALL}")
