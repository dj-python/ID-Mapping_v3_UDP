import base64
import socket
import threading
from PySide6.QtCore import QThread, Signal
import util_base
import xml.etree.ElementTree as ET
import os
import time
import inspect

class TCPServer(QThread):
    """
    변경 사항:
    - 통신 방식을 TCP에서 UDP로 변경. (소켓 타입만 UDP로 변경하고, 메서드/변수명은 최대한 동일하게 유지)
    - 이 서버는 Write Card 1~4만 직접 관리합니다.
    - Bank 5(IO Board)는 등록된 핸들러(attach 모드)로 위임합니다.
      * UDP 특성상 '연결' 개념이 없으므로, Bank5의 경우 수신된 각 datagram마다 핸들러가 호출될 수 있습니다.
      * 기존 2-인자(handler(sock, addr)) 핸들러도 지원하며(최초 감지 시 1회 호출),
        3-인자(handler(sock, addr, data)) 형태를 등록하면 datagram 데이터까지 함께 전달합니다.
    """
    signalMessage = Signal(str, str, dict)

    def __init__(self, objName=None, baseDir=None, mainWindow=None):
        super().__init__()
        self.ip = None
        self.port = None
        self.timeout = None
        self.baseDir = baseDir
        self.setObjectName(objName)
        self._running = True
        self.connected_clients = 0
        self.lock = threading.Lock()
        self.client_condition = threading.Condition(self.lock)
        self.sock = None
        self.pMainWindow = mainWindow
        # UDP에서는 실제 '클라이언트 소켓'이 없으므로 Bank1~4에 대한 마지막 송신지 주소를 저장합니다.
        # 예: {"Bank1": ("192.168.0.11", 6000), ...}
        self.client_sockets = {}

        # 분리: IO 보드 소켓 핸들러(attach)
        # UDP 변경에 따라, 필요 시 (sock, addr, data) 3-인자도 허용합니다.
        self._bank5_socket_handler = None  # callable(...)

        # UDP용: Bank별 버퍼/타임스탬프 관리
        self._udp_buffers = {}       # { "Bank1": "partial text...", ... }
        self._udp_last_data_ts = {}  # { "Bank1": last_ts, ... }
        self._known_banks = set()    # 최초 감지된 Bank 추적 (연결 카운트 유사 기능)

        # UDP용: Bank5 최초 감지 여부 (2-인자 핸들러 하위호환용)
        self._bank5_seen_addrs = set()


    def set_bank5_socket_handler(self, handler):
        """
        IO Board(Bank 5) 처리를 외부 모듈로 위임하기 위한 핸들러 등록.
        - UDP 변경사항:
          * handler(sock, addr, data) 형태를 지원합니다. (권장)
          * 기존 handler(sock, addr) 2-인자 핸들러도 지원하며, 이 경우 Bank5를 처음 감지했을 때 1회 호출됩니다.
        """
        self._bank5_socket_handler = handler

    def run(self):
        server_info = util_base.get_xml_info(self.baseDir, 'server')
        if server_info is None:
            raise Exception("Server info could not be retrieved")
        ip_address = server_info['ipAddress']
        port_number = int(server_info['portNumber'])
        self.startCom(ip=ip_address, port=port_number, mainwindow=self.pMainWindow)

    def startCom(self, mainwindow, ip, port, timeout=20):
        self.pMainWindow = mainwindow
        self.ip = ip
        self.port = port
        self.timeout = timeout

        if not self.validate_ip(self.ip):
            raise ValueError(f"Invalid IP address: {self.ip}")

        try:
            # UDP 소켓 생성
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind((self.ip, self.port))
            # 논블로킹 종료를 위해 타임아웃 설정
            self.sock.settimeout(1.0)

            print(f"Binded(UDP) to IP: {self.ip}, Port: {self.port}")
            self.signalMessage.emit(self.objectName(), 'connection',
                                    {'where': 'startCom', 'msg': f"Binding(UDP) to IP: {self.ip}, Port: {self.port}"})

            # UDP에서는 listen/accept가 없습니다. 바로 수신 대기 루프로 진입합니다.
            self.wait_for_client()

        except Exception as e:
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
            self.signalMessage.emit(self.objectName(), 'connection',
                                    dict(where='TCPServer', msg=f"Error in UDP server: {str(e)}"))
        finally:
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass

    def get_bank_number_by_ip(self, ip):
        """
        sysInfo.xml 파일을 파싱하여 주어진 IP 주소에 해당하는 bank number를 반환
        """
        try:
            tree = ET.parse(os.path.join(self.baseDir, 'sysInfo.xml'))
            root = tree.getroot()

            clients = root.find('clients')
            if clients is not None:
                for bank in clients.findall('bank'):
                    if bank.get('ip') == ip:
                        return bank.get('number')
            return None
        except Exception as e:
            print(f"Error parsing sysInfo.xml: {e}")
            return None

    def get_connected_sockets(self, clients_info):
        """
        연결된 클라이언트만 포함하는 client_sockets 딕셔너리를 반환합니다.
        - UDP에서는 Bank1~4의 마지막 송신지 주소를 반환합니다.
        """
        client_sockets = {
            f"Bank{client['number']}": self.client_sockets.get(f"Bank{client['number']}")
            for client in clients_info
            if str(client.get('number')) in ("1", "2", "3", "4") and f"Bank{client['number']}" in self.client_sockets
        }
        return client_sockets


    def wait_for_client(self):
        self.signalMessage.emit(self.objectName(), 'connection',
                                {'where': 'wait_for_client', 'msg': 'UDP 서버 대기 중...'})

        writecard_connected = set()

        def process_line(bank_number: str, client_addr, line_text: str, now_ts: float):
            if line_text is None:
                return
            line_text = line_text.rstrip("\r").strip()
            if not line_text:
                return

            # ping 관련 분기 제거: 모든 메시지를 동일하게 처리
            print(f"Received data from {client_addr}: {line_text}")

            where_msg = f"Write Card {bank_number}"
            self.signalMessage.emit(self.objectName(), 'job', dict(where=where_msg, msg=line_text))
            if line_text.startswith("Script save finished"):
                self.signalMessage.emit(self.objectName(), 'client',
                                        {'where': f'Bank {bank_number}', 'msg': line_text})

        while self._running:
            now = time.time()
            try:
                data, client_addr = self.sock.recvfrom(4096)
                if not data:
                    continue

                client_ip = client_addr[0]
                bank_number = self.get_bank_number_by_ip(client_ip)
                if not bank_number:
                    bank_number = "Unknown"

                # IO 보드(5)는 외부 핸들러로 위임
                if str(bank_number) == "5":
                    if self._bank5_socket_handler:
                        try:
                            sig = inspect.signature(self._bank5_socket_handler)
                            params = sig.parameters
                            if len(params) >= 3:
                                # (sock, addr, data) 지원 핸들러
                                self._bank5_socket_handler(self.sock, client_addr, data)
                            else:
                                # (sock, addr) 구형 핸들러: 최초 감지 시 1회 호출
                                if client_addr not in self._bank5_seen_addrs:
                                    self._bank5_seen_addrs.add(client_addr)
                                    self._bank5_socket_handler(self.sock, client_addr)
                        except Exception as e:
                            print(f"[Bank5 handler error] {e}")
                    continue

                # Write Card 1~4만 관리
                if str(bank_number) not in ("1", "2", "3", "4"):
                    continue

                client_name = f"Bank{bank_number}"

                # 최초 감지 시 '연결' 유사 처리
                first_seen_for_bank = False
                with self.lock:
                    prev_addr = self.client_sockets.get(client_name)
                    if prev_addr != client_addr:
                        self.client_sockets[client_name] = client_addr
                        first_seen_for_bank = client_name not in self._known_banks
                        self._known_banks.add(client_name)

                        if first_seen_for_bank:
                            self.connected_clients += 1
                            client_type = f"writecard {bank_number}"
                            self.signalMessage.emit(self.objectName(), 'connection',
                                                    {'where': f'Bank {bank_number}',
                                                     'msg': f'Client connected: {client_type}'})

                            writecard_connected.add(str(bank_number))
                            if len(writecard_connected) == 4:
                                self.signalMessage.emit(self.objectName(), 'connection',
                                                        {'where': 'Client', 'msg': 'All Write Cards connected.'})

                # Bank별 버퍼 초기화 보장
                if client_name not in self._udp_buffers:
                    self._udp_buffers[client_name] = ""
                if client_name not in self._udp_last_data_ts:
                    self._udp_last_data_ts[client_name] = now

                # 디코드 및 라인 처리
                try:
                    text = data.decode()
                except UnicodeDecodeError as e:
                    print(f"Decode error: {e}")
                    continue

                self._udp_last_data_ts[client_name] = now
                self._udp_buffers[client_name] += text

                # 누적 버퍼에서 개행 단위로 처리
                while True:
                    idx = self._udp_buffers[client_name].find("\n")
                    if idx == -1:
                        break
                    line, rest = self._udp_buffers[client_name][:idx], self._udp_buffers[client_name][idx + 1:]
                    self._udp_buffers[client_name] = rest
                    process_line(bank_number, client_addr, line, now)

            except socket.timeout:
                # 각 Bank별로 일정 시간동안 추가 데이터가 없으면 버퍼 플러시
                flush_after = 0.3
                to_flush = []
                for bank_key, buf in list(self._udp_buffers.items()):
                    if buf:
                        last_ts = self._udp_last_data_ts.get(bank_key, 0)
                        if now - last_ts > flush_after:
                            to_flush.append(bank_key)
                for bank_key in to_flush:
                    bank_number = bank_key.replace("Bank", "")
                    buf = self._udp_buffers.get(bank_key, "").strip()
                    if buf:
                        client_addr = self.client_sockets.get(bank_key)
                        process_line(bank_number, client_addr, buf, now)
                        self._udp_buffers[bank_key] = ""
                continue

            except OSError as e:
                if not self._running:
                    break
                print(f"[UDP Error] {str(e)}")
                self.signalMessage.emit(self.objectName(), 'connection',
                                        dict(where='TCPServer', msg=f'UDP socket error: {str(e)}'))
                continue

            except Exception as e:
                print(f"[Error] UDP receive loop - Exception: {str(e)}")
                self.signalMessage.emit(self.objectName(), 'connection',
                                        dict(where='TCPServer', msg=f'Error receiving datagram: {str(e)}'))
                continue

        # 루프 종료 시 정리 로그
        with self.lock:
            banks = list(self.client_sockets.keys())
            if banks:
                print(f"[Debug] UDP server stopping. Known banks: {banks}")


    # (참고) TCP 전용 클라이언트 스레드 함수는 UDP에선 사용하지 않습니다.
    # 기존 코드 호환을 위해 메서드를 유지하되 미사용 처리합니다.
    def handle_client(self, client_socket, client_addr):
        # UDP 모드에서는 사용되지 않습니다.
        pass

    def stop(self):
        self._running = False
        with self.lock:
            # UDP에서는 별도의 클라이언트 소켓이 없으므로 주소 맵만 정리
            self.client_sockets.clear()
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        self.signalMessage.emit(self.objectName(), 'sys',
                                dict(where='TCPServer', msg='UDP 서버 종료'))





    def send_data(self, client_socket, data) -> bool:
        """
        UDP 송신:
        - client_socket 인자에는 다음 중 하나를 전달할 수 있습니다.
          * ('ip', port) 형태의 주소 튜플
          * "BankN" 문자열 (예: "Bank1") -> 내부에 저장된 마지막 주소로 송신
        - bytes 또는 str 모두 지원(개행 자동 부착 로직은 기존 유지)
        """
        if self.sock is None:
            self.signalMessage.emit(self.objectName(), 'data',
                                    dict(where='TCPServer', msg="Error: UDP socket is not initialized"))
            return False

        # 주소 해석
        addr = None
        if isinstance(client_socket, tuple) and len(client_socket) == 2:
            addr = client_socket
        elif isinstance(client_socket, str) and client_socket.startswith("Bank"):
            addr = self.client_sockets.get(client_socket)
        else:
            # 하위호환: 기존에 실제 소켓 객체가 올 수 있으나, UDP에선 지원하지 않음
            self.signalMessage.emit(self.objectName(), 'data',
                                    dict(where='TCPServer', msg=f"Error: Unsupported client handle for UDP: {type(client_socket)}"))
            return False

        if addr is None:
            self.signalMessage.emit(self.objectName(), 'data',
                                    dict(where='TCPServer', msg="Error: Destination address is unknown"))
            return False

        try:
            if isinstance(data, bytes):
                payload = data
                dbg_kind = "bytes"
            else:
                text = str(data)
                if not text.endswith("\n"):
                    text = text + "\n"
                payload = text.encode('utf-8')
                dbg_kind = "text"
            sent = self.sock.sendto(payload, addr)
            ok = (sent == len(payload))
            print(f"[UDP TX] to {addr} {dbg_kind} {len(payload)} bytes (ok={ok})")
            return ok
        except Exception as e:
            self.signalMessage.emit(self.objectName(), 'data',
                                    dict(where='TCPServer', msg=f"Error: {str(e)}"))
            return False



    def send_chunk_to_clients(self, client_socket, chunk) -> bool:
        if client_socket is None:
            print("[Error] 클라이언트 주소가 None 입니다.")
            return False

        if not isinstance(chunk, (bytes, bytearray)):
            if isinstance(chunk, str):
                chunk = chunk.encode('utf-8')
            else:
                print(f"[Error] Chunk 데이터 타입이 올바르지 않습니다 : {type(chunk)}")
                return False

        b64 = base64.b64encode(chunk).decode('ascii')
        line = f"SCRIPT_CHUNK {len(chunk)} {b64}"  # 개행은 send_data가 붙여 줌
        ok = self.send_data(client_socket=client_socket, data=line)
        if ok:
            print(f"[UDPServer] CHUNK sent (raw {len(chunk)} bytes, b64 {len(b64)} chars)")
            return True

        print("[Error] 클라이언트 데이터 전송 실패")
        return False

    # def send_chunk_to_clients(self, client_socket, chunk) -> bool:
    #     if client_socket is None:
    #         print("[Error] 클라이언트 주소가 None 입니다.")
    #         return False
    #
    #     if isinstance(chunk, str):
    #         chunk = chunk.encode('utf-8')
    #     elif not isinstance(chunk, bytes):
    #         print(f"[Error] Chunk 데이터 타입이 올바르지 않습니다 : {type(chunk)}")
    #         return False
    #
    #     ok = self.send_data(client_socket=client_socket, data=chunk)
    #     if ok:
    #         print("[UDPServer] 데이터 전송 완료")
    #         return True
    #
    #     print("[Error] 클라이언트 데이터 전송 실패")
    #     return False

    def is_socket_connected(self, client_socket):
        """
        UDP에서는 연결 상태를 점검할 수 없습니다.
        - ('ip', port) 또는 "BankN"이 등록되어 있으면 True로 간주합니다.
        """
        try:
            if isinstance(client_socket, tuple) and len(client_socket) == 2:
                return client_socket in self.client_sockets.values()
            if isinstance(client_socket, str) and client_socket.startswith("Bank"):
                return client_socket in self.client_sockets
            # 기타 타입은 미지원
            return False
        except Exception:
            return False

    @staticmethod
    def validate_ip(ip):
        try:
            socket.inet_aton(ip)
            return True
        except socket.error:
            return False
