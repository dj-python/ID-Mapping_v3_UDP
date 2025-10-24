import threading, re, traceback, os, time, ast
import c_udp_server, util_base, c_udp_ioboard
from PySide6 import QtCore
import xml.etree.ElementTree as ET
from typing import Dict, Iterable, Optional

class JobController(QtCore.QObject):
    EXIT_THREAD = False
    signalMessage = QtCore.Signal(str, str, dict)

    def __init__(self, baseDir, objName='', mainWindow=None):
        QtCore.QObject.__init__(self)
        self.logger = util_base.setuplogger(self.__class__.__name__)
        self.setObjectName(objName)
        self.p_mainWindow = mainWindow
        self.baseDir = baseDir

        try:
            self.clients_info = util_base.get_xml_info(self.baseDir, 'clients') or []
        except Exception:
            self.clients_info = []
        self.io_socket = None

        self.clsInfo = {'is_examine': False, 'Writecard 1 Ready': False, 'Writecard 2 Ready': False,
                        'Writecard 3 Ready': False, 'Writecard 4 Ready': False,
                        'Left_Recon_Script': False, 'Right_Recon_Script': False,
                        'IOBoard_Ready': False, 'is_abortTest': False,
                        'writeCard_states': {}, 'left_capture_done': False, 'right_capture_done': False,
                        '1st_image_scan': False,
                        '1st_left_barcode': False, '1st_right_barcode': False, '2nd_barcode': False,
                        '3rd_shot': False, '3rd_left_barcode': False, '3rd_right_barcode': False,
                        'sensor_data1': False, 'sensor_data2': False, 'sensor_data3': False, 'sensor_data4': False,
                        'barcode_data1': False, 'barcode_data2': False, 'barcode_data3': False, 'barcode_data4': False,
                        'pusher back': False, 'c_save': False, 'sensor_dict update': False, '2nd show update': False,
                        'barcode_stop': False, 'pusher_down_started': False, 'pusher_down_finished': False,
                        'button_unpushed': False,
                        'pusher_down_tes': None, 'button_unpushed_ts': None, 'pusher_sequence_decided': False,
                        'early_button_unpushed': False,
                        'force_abort': False}
        self.job_modules_Left = {}
        self.job_modules_Right = {}
        self.left_ready_printed = False
        self.right_ready_printed = False
        self.settings_xml_info = os.path.join(self.baseDir, 'models', self.p_mainWindow.cb_customerName.currentText(),
                                              self.p_mainWindow.cb_selectedModel.currentText(), 'settings.xml')
        self._test_thread = None
        self._barcode_read_requested = False

        try:
            # Write Card 1~4 서버
            self.writeCard = c_udp_server.TCPServer(objName='writeCard', baseDir=self.baseDir,
                                                    mainWindow=self.p_mainWindow)
            self.writeCard.signalMessage.connect(self.slotParse, type=QtCore.Qt.ConnectionType.DirectConnection)

            # IO Board 서버(attach 모드)
            self.ioBoard = c_udp_ioboard.IOBoardServer(objName='ioBoard', baseDir=self.baseDir,
                                                       mainWindow=self.p_mainWindow)
            self.ioBoard.signalMessage.connect(self.slotParse, type=QtCore.Qt.ConnectionType.DirectConnection)
            self.ioBoard.start()  # attach 모드여도 스레드를 살아있게 유지(옵션)

            # writeCard 서버가 IO 보드 소켓을 위임할 핸들러 등록
            self.writeCard.set_bank5_socket_handler(self.ioBoard.attach_socket)

            self.writeCard.start()

            self.clsInfo['is_initialized'] = True
            self.logger.info(f'Initialization Successful > ObjectName: {self.objectName()}')

        except OSError as e:
            self.server = None
            print(traceback.format_exc())
            self.logger.error(f'Initialization Failed > ObjectName: {self.objectName()}, Detail:{e}')


        self._pending_script_by_wc: Dict[object, bytes] = {}
        self._pending_lock = threading.Lock()



    @QtCore.Slot()
    def slotManualTestStart(self):
        """
        UI의 'Manual Test' 버튼에서 호출되어 do_test 메인 루프를 시작한다.
        - 이미 실행 중이면 무시
        - 물리 버튼 이벤트와 무관하게 동작
        """
        # 이미 테스트 쓰레드가 돌고 있으면 무시
        if self.clsInfo.get('is_examine') or (
                getattr(self, '_test_thread', None) is not None and self._test_thread.is_alive()
        ):
            print("테스트가 이미 실행중입니다. (ignore UI Manual Test)")
            return

        # 필요한 최소 초기화 (물리 버튼 관련 강제 중단 로직은 사용하지 않으므로 단순화)
        self.clsInfo['sensorID'] = str()
        self.clsInfo['is_examine'] = True
        self.clsInfo['is_abortTest'] = False

        self._test_thread = threading.Thread(target=self.do_test, daemon=True)
        self._test_thread.start()
        print("수동 테스트 시작됨 (UI 버튼 통해)")

    @QtCore.Slot(str, str, dict)
    def slotParse(self, objName, msgType, values):
        try:
            if 'msg' not in values:
                values['msg'] = "No msg provided"
        except Exception as e:
            self.logger.error(f"Error in slotParse: {str(e)}")

        # 1) Write Card 1~4 처리
        if objName.startswith('writeCard'):
            try:
                # UDP 전환: 연결 이벤트 사용 안 함
                if msgType == 'job':
                    msg = values.get('msg', '')

                    if msg.startswith("Script save"):
                        self.update_job_module_status(values)

                    elif msg.startswith("sensor_ID"):
                        dict_str = values['msg'][len("sensor_ID: "):].strip()
                        try:
                            sensor_ID_dict = ast.literal_eval(dict_str)
                        except Exception as e:
                            print(f"Failed to parse sensor_ID: {e}")
                            sensor_ID_dict = {}
                        self.update_sensorID_from_client(Writecard_num=values['where'], sensor_ID=sensor_ID_dict)

                    elif msg.startswith("barcode_info"):
                        prefix = "barcode_info: OrderedDict("
                        if values['msg'].startswith(prefix):
                            dict_str = values['msg'][len(prefix):-1].strip()
                        else:
                            dict_str = values['msg'][len("barcode_info: "):].strip()
                        try:
                            barcode_info_dict = ast.literal_eval(dict_str)
                        except Exception as e:
                            print(f"Failed to parse barcode_info: {e}")
                            barcode_info_dict = {}
                        self.update_barcode_from_client(Writecard_num=values['where'], barcode_info=barcode_info_dict)

                    elif msg == 'Scan Stop':
                        # 카메라/클라이언트에서 오는 스캔 중단
                        self.clsInfo['barcode_stop'] = True
                        print('[slotParse] barcode_stop latched (Scan Stop)')

            except Exception as e:
                self.logger.error(f"Error in slotParse: {str(e)}")
            return

        # 2) IO Board 처리
        if objName.startswith('ioBoard'):
            try:
                # UDP 전환: 연결 이벤트 사용 안 함
                if msgType == 'job':
                    msg = values.get('msg', '')

                    if msg == 'Mapping start':
                        # 테스트가 이미 진행 중이면 무시
                        if self.clsInfo.get('is_examine') or (
                                getattr(self, '_test_thread', None) is not None and self._test_thread.is_alive()):
                            print("테스트가 이미 실행중입니다. (ignore 'Mapping start')")
                            return

                        self.clsInfo['pusher_down_finished'] = False
                        self.clsInfo['button_unpushed'] = False
                        self.clsInfo['pusher_down_ts'] = None
                        self.clsInfo['button_unpushed_ts'] = None
                        self.clsInfo['pusher_sequence_decided'] = False
                        self.clsInfo['early_button_unpushed'] = False
                        self.clsInfo['force_abort'] = False
                        self.clsInfo['pusher back'] = False

                        if not self.clsInfo['is_examine']:
                            self.clsInfo['sensorID'] = str()
                            self.clsInfo['is_examine'] = True
                            self.clsInfo['is_abortTest'] = False
                            self._test_thread = threading.Thread(target=self.do_test, daemon=True)
                            self._test_thread.start()

                    elif msg == 'Pusher down finished':
                        if not self.clsInfo.get('pusher_down_finished'):
                            self.clsInfo['pusher_down_finished'] = True
                            self.clsInfo['pusher_down_ts'] = time.perf_counter()
                            print('[slotParse] Pusher down finished (first)')
                        self.clsInfo['early_button_unpushed'] = False
                        self.clsInfo['force_abort'] = False

                    elif msg == 'Pusher back finished':
                        self.clsInfo['pusher back'] = True

                    elif msg == 'Button unpushed':
                        if not self.clsInfo.get('button_unpushed'):
                            self.clsInfo['button_unpushed'] = True
                            self.clsInfo['button_unpushed_ts'] = time.perf_counter()

                        if not self.clsInfo.get('pusher_down_finished'):
                            print('[slotParse] Button came before PusherDown → request abort')
                            self.clsInfo['early_button_unpushed'] = True
                            self.clsInfo['force_abort'] = True
                            # [변경] UDP 주소 해석 → 직접 전송
                            try:
                                addr = None
                                try:
                                    sockets = self.ioBoard.get_connected_sockets()
                                    addr = sockets.get('Bank5')
                                except Exception:
                                    addr = None
                                if not addr:
                                    addr = self._get_bank_addr_from_config(5)

                                if addr:
                                    self.writeCard.send_data(client_socket=addr, data='Pusher back')
                                    print("[slotParse] Sent 'Pusher back' (early button, UDP addr)")
                                else:
                                    print("[slotParse] Bank5 address not found; cannot send 'Pusher back'")
                            except Exception as e:
                                print(f"[slotParse] Failed to send 'Pusher back': {e}")

                    elif msg == 'Scan Stop':
                        self.clsInfo['barcode_stop'] = True
                        print('[slotParse] barcode_stop latched (Scan Stop)')

            except Exception as e:
                self.logger.error(f"Error in slotParse(ioBoard): {str(e)}")
            return





    # @QtCore.Slot(str, str, dict)
    # def slotParse(self, objName, msgType, values):
    #     try:
    #         if 'msg' not in values:
    #             values['msg'] = "No msg provided"
    #     except Exception as e:
    #         self.logger.error(f"Error in slotParse: {str(e)}")
    #
    #     # 1) Write Card 1~4 처리
    #     if objName.startswith('writeCard'):
    #         try:
    #             # UDP 전환: 연결 이벤트 사용 안 함
    #             if msgType == 'job':
    #                 msg = values.get('msg', '')
    #
    #                 if msg.startswith("Script save"):
    #                     self.update_job_module_status(values)
    #
    #                 elif msg.startswith("sensor_ID"):
    #                     dict_str = values['msg'][len("sensor_ID: "):].strip()
    #                     try:
    #                         sensor_ID_dict = ast.literal_eval(dict_str)
    #                     except Exception as e:
    #                         print(f"Failed to parse sensor_ID: {e}")
    #                         sensor_ID_dict = {}
    #                     self.update_sensorID_from_client(Writecard_num=values['where'], sensor_ID=sensor_ID_dict)
    #
    #                 elif msg.startswith("barcode_info"):
    #                     prefix = "barcode_info: OrderedDict("
    #                     if values['msg'].startswith(prefix):
    #                         dict_str = values['msg'][len(prefix):-1].strip()
    #                     else:
    #                         dict_str = values['msg'][len("barcode_info: "):].strip()
    #                     try:
    #                         barcode_info_dict = ast.literal_eval(dict_str)
    #                     except Exception as e:
    #                         print(f"Failed to parse barcode_info: {e}")
    #                         barcode_info_dict = {}
    #                     self.update_barcode_from_client(Writecard_num=values['where'], barcode_info=barcode_info_dict)
    #
    #                 elif msg == 'Scan Stop':
    #                     # 카메라/클라이언트에서 오는 스캔 중단
    #                     self.clsInfo['barcode_stop'] = True
    #                     print('[slotParse] barcode_stop latched (Scan Stop)')
    #
    #         except Exception as e:
    #             self.logger.error(f"Error in slotParse: {str(e)}")
    #         return
    #
    #     # 2) IO Board 처리
    #     if objName.startswith('ioBoard'):
    #         try:
    #             # UDP 전환: 연결 이벤트 사용 안 함
    #             if msgType == 'job':
    #                 msg = values.get('msg', '')
    #
    #                 if msg == 'Mapping start':
    #                     # 테스트가 이미 진행 중이면 플래그를 건드리지 않고 무시
    #                     if self.clsInfo.get('is_examine') or (
    #                             getattr(self, '_test_thread', None) is not None and self._test_thread.is_alive()):
    #                         print("테스트가 이미 실행중입니다. (ignore 'Mapping start')")
    #                         return
    #
    #                     self.clsInfo['pusher_down_finished'] = False
    #                     self.clsInfo['button_unpushed'] = False
    #                     self.clsInfo['pusher_down_ts'] = None
    #                     self.clsInfo['button_unpushed_ts'] = None
    #                     self.clsInfo['pusher_sequence_decided'] = False
    #                     self.clsInfo['early_button_unpushed'] = False
    #                     self.clsInfo['force_abort'] = False
    #                     self.clsInfo['pusher back'] = False
    #
    #                     if not self.clsInfo['is_examine']:
    #                         self.clsInfo['sensorID'] = str()
    #                         self.clsInfo['is_examine'] = True
    #                         self.clsInfo['is_abortTest'] = False
    #                         self._test_thread = threading.Thread(target=self.do_test, daemon=True)
    #                         self._test_thread.start()
    #
    #                 elif msg == 'Pusher down finished':
    #                     if not self.clsInfo.get('pusher_down_finished'):
    #                         self.clsInfo['pusher_down_finished'] = True
    #                         self.clsInfo['pusher_down_ts'] = time.perf_counter()
    #                         print('[slotParse] Pusher down finished (first)')
    #                     self.clsInfo['early_button_unpushed'] = False
    #                     self.clsInfo['force_abort'] = False
    #
    #                 elif msg == 'Pusher back finished':
    #                     self.clsInfo['pusher back'] = True
    #
    #                 elif msg == 'Button unpushed':
    #                     if not self.clsInfo.get('button_unpushed'):
    #                         self.clsInfo['button_unpushed'] = True
    #                         self.clsInfo['button_unpushed_ts'] = time.perf_counter()
    #
    #                     if not self.clsInfo.get('pusher_down_finished'):
    #                         print('[slotParse] Button came before PusherDown → request abort')
    #                         self.clsInfo['early_button_unpushed'] = True
    #                         self.clsInfo['force_abort'] = True
    #                         try:
    #                             if self.io_socket:
    #                                 self.ioBoard.send_data(data='Pusher back')
    #                                 print("[slotParse] Sent 'Pusher back' (early button, cached socket)")
    #                             else:
    #                                 sockets = self.ioBoard.get_connected_sockets()
    #                                 io_socket = sockets.get('Bank5')
    #                                 if io_socket:
    #                                     self.ioBoard.send_data(client_socket=io_socket, data='Pusher back')
    #                                     print("[slotParse] Sent 'Pusher back' (early button, fallback)")
    #                         except Exception as e:
    #                             print(f"[slotParse] Failed to send 'Pusher back': {e}")
    #
    #                 elif msg == 'Scan Stop':
    #                     # 카메라/클라이언트에서 오는 스캔 중단
    #                     self.clsInfo['barcode_stop'] = True
    #                     print('[slotParse] barcode_stop latched (Scan Stop)')
    #
    #         except Exception as e:
    #             self.logger.error(f"Error in slotParse(ioBoard): {str(e)}")
    #         return


    def _handle_client_disconnect(self, where: str | None, msg: str | None = None):
        """
        c_tcp_server.py -> signalMessage(sender, 'connection', {'where': 'Bank N', 'msg': 'Client socket closed: ...'})
        를 받아, Bank 번호에 따라 clsInfo의 Ready 플래그를 False로 내립니다.
          - Bank 1~4: 'Writecard N Ready' = False
          - Bank 5  : 'IOBoard_Ready'    = False
        """
        if not where:
            return
        # where는 'Bank 1' 같은 형태를 가정
        bank_no = None
        try:
            import re
            m = re.search(r'Bank\s*(\d+)', str(where))
            if m:
                bank_no = int(m.group(1))
        except Exception:
            bank_no = None

        if bank_no is None:
            return

        if 1 <= bank_no <= 4:
            key = f'Writecard {bank_no} Ready'
            if key in self.clsInfo:
                self.clsInfo[key] = False
                print(f"[JobControl] {where} disconnected → {key}=False", flush=True)
            else:
                # 키가 없으면 생성해도 됨(옵션)
                self.clsInfo[key] = False
                print(f"[JobControl] {where} disconnected → {key} created=False", flush=True)
        elif bank_no == 5:
            self.clsInfo['IOBoard_Ready'] = False
            print(f"[JobControl] {where} disconnected → IOBoard_Ready=False", flush=True)



    def _banks_ready(self) -> bool:
        """
        UDP 전환에 따라 TCP 연결 플래그 기반의 Ready 검사 로직을 제거.
        항상 True를 반환해 테스트 흐름이 연결 이벤트에 의해 막히지 않도록 함.
        """
        return True



    def make_dictionary(self, baseDir, selected_family, selected_model):
        # 1) settings.xml 경로
        settings_path = os.path.join(baseDir, "models", selected_family, selected_model, "settings.xml")
        if not os.path.exists(settings_path):
            print(f"[ERROR] settings.xml 파일이 존재하지 않습니다: {settings_path}")
            return

        # [ADD] 새 사이클 시작: Recon 관련 게이트/플래그 초기화
        #  - 'Left/Right Recon Script Loaded'를 다음 사이클에서 다시 emit하기 위함
        self.left_ready_printed = False
        self.right_ready_printed = False
        self.clsInfo['Left_Recon_Script'] = False
        self.clsInfo['Right_Recon_Script'] = False

        # 2) writecard1~4 qty 파싱 (파싱 실패 시 0)
        writecard1_qty = 0
        writecard2_qty = 0
        writecard3_qty = 0
        writecard4_qty = 0

        attrs = util_base.parse_settings_xml(settings_path, "writecard1")
        if attrs:
            writecard1_qty = int(attrs.get("qty", 0))
        attrs = util_base.parse_settings_xml(settings_path, "writecard2")
        if attrs:
            writecard2_qty = int(attrs.get("qty", 0))
        attrs = util_base.parse_settings_xml(settings_path, "writecard3")
        if attrs:
            writecard3_qty = int(attrs.get("qty", 0))
        attrs = util_base.parse_settings_xml(settings_path, "writecard4")
        if attrs:
            writecard4_qty = int(attrs.get("qty", 0))

        # 3) carrier columns 파싱 (기본값 2로 가정)
        carrier_columns = 2
        try:
            c_attrs = util_base.parse_settings_xml(settings_path, "carrier")
            if c_attrs and "columns" in c_attrs:
                carrier_columns = int(c_attrs.get("columns"))
            else:
                tree = ET.parse(settings_path)
                root = tree.getroot()
                node = root.find(".//carrier")
                if node is not None and node.get("columns") is not None:
                    carrier_columns = int(node.get("columns"))
        except Exception:
            pass

        # 4) 딕셔너리 초기화
        self.job_modules_Left.clear()
        self.job_modules_Right.clear()

        # 5) columns 조건에 따라 분배
        if carrier_columns == 1:
            module_idx = 1
            for qty, wc_name in [
                (writecard1_qty, 'writecard1'),
                (writecard2_qty, 'writecard2'),
                (writecard3_qty, 'writecard3'),
                (writecard4_qty, 'writecard4'),
            ]:
                for _ in range(qty):
                    self.job_modules_Left[f'Module{module_idx}'] = [False, wc_name, None, None]
                    module_idx += 1

            print(f"[INFO] (columns=1) job_modules_Left: {self.job_modules_Left}")
            print(f"[INFO] (columns=1) job_modules_Right: {self.job_modules_Right}")

        else:
            # 기존 로직: Left ← writecard1,2 / Right ← writecard3,4
            module_idx = 1
            for qty, wc_name in [(writecard1_qty, "writecard1"), (writecard2_qty, "writecard2")]:
                for _ in range(qty):
                    self.job_modules_Left[f'Module{module_idx}'] = [False, wc_name, None, None]
                    module_idx += 1

            module_idx = 1
            for qty, wc_name in [(writecard3_qty, "writecard3"), (writecard4_qty, "writecard4")]:
                for _ in range(qty):
                    self.job_modules_Right[f'Module{module_idx}'] = [False, wc_name, None, None]
                    module_idx += 1

            print("[INFO] job_modules_Left:", self.job_modules_Left)
            print("[INFO] job_modules_Right:", self.job_modules_Right)

        # 6) 스크립트 전송
        self.send_scripts_to_clients()







    def send_scripts_to_clients(self):
        """
        연결된 클라이언트에게 스크립트를 전송 (UDP 버전)
        - Bank1~4의 주소를 sysInfo.xml에서 읽어와 직접 UDP 전송
        - 'Script send' 알림 → 파일 청크 전송(1024 bytes) → 'EOF' 전송 순서
        """
        try:
            family_name = util_base.get_xml_info(self.baseDir, 'recent/familyName')
            model_name = util_base.get_xml_info(self.baseDir, 'recent/modelName')

            if not family_name or not model_name:
                self.signalMessage.emit(self.objectName(), 'ui',
                                        {'msg': "[Error] familyName 또는 modelName 정보를 가져오지 못했습니다."})
                return

            settings_file_path = os.path.join(self.baseDir, 'models', family_name, model_name, 'settings.xml')
            if not os.path.exists(settings_file_path):
                self.signalMessage.emit(self.objectName(), 'ui', {'msg': f"[Error] settings.xml 파일을 찾을 수 없습니다."})
                self.signalMessage.emit(self.objectName(), 'ui', {'msg': f"[Error] {settings_file_path}"})
                return

            try:
                tree = ET.parse(settings_file_path)
                root = tree.getroot()

                script_element = root.find(".//script")
                if script_element is None or 'filePath' not in script_element.attrib:
                    self.signalMessage.emit(self.objectName(), 'ui',
                                            {'msg': "[Error] settings.xml에서 script 태그 또는 filePath키를 찾을 수 없습니다."})
                    return

                script_file_path = os.path.join(self.baseDir, 'models', family_name, model_name,
                                                f"{model_name}_script.txt")
                # script_file_path = os.path.join(self.baseDir, 'models', family_name, model_name,
                #                                 script_element.get('filePath'))
                if not os.path.exists(script_file_path):
                    self.signalMessage.emit(self.objectName(), 'ui',
                                            {'msg': f"[Error] 스크립트 파일을 찾을 수 없습니다. : {script_file_path}"})
                    return
            except ET.ParseError as e:
                self.signalMessage.emit(self.objectName(), 'ui',
                                        {'msg': f"[Error] settings.xml 파일을 파싱하는 중 오류 발생: {str(e)}"})
                return

            # Bank1~4 주소 수집
            bank_addrs = {}
            for bank_no in (1, 2, 3, 4):
                addr = self._get_bank_addr_from_config(bank_no)
                if addr:
                    bank_addrs[f"Bank{bank_no}"] = addr
                else:
                    self.signalMessage.emit(self.objectName(), 'ui',
                                            {'msg': f"[Warning] Bank{bank_no} address not found. Skip script send."})

            if not bank_addrs:
                self.signalMessage.emit(self.objectName(), 'ui', {'msg': "[Error] 전송 가능한 Bank 주소가 없습니다."})
                return

            # 1) 사전 알림: "Script send"
            for bank_name, addr in bank_addrs.items():
                try:
                    ok = self.writeCard.send_data(client_socket=addr, data="Script send")
                    time.sleep(0.05)
                    if ok:
                        self.signalMessage.emit(self.objectName(), 'ui',
                                                {'msg': f"{bank_name} 'Script send' 메시지 전송 완료"})
                    else:
                        self.signalMessage.emit(self.objectName(), 'ui',
                                                {'msg': f"[Error] {bank_name} 'Script send' 전송 실패"})
                except Exception:
                    self.signalMessage.emit(self.objectName(), 'ui',
                                            {'msg': f"[Error] {bank_name} 'Script send' 전송 중 예외 발생"})

            # 2) 스크립트 파일 청크 전송
            chunks = []
            with open(script_file_path, 'rb') as script_file:
                while True:
                    chunk = script_file.read(768)
                    if not chunk:
                        break
                    chunks.append(chunk)
            total_chunks = len(chunks)

            failed_banks = set()
            for idx, chunk in enumerate(chunks, 1):
                for bank_name, addr in list(bank_addrs.items()):
                    if bank_name in failed_banks:
                        continue
                    try:
                        ok = self.writeCard.send_chunk_to_clients(client_socket=addr, chunk=chunk)
                        print(f'chunk: {chunk}')
                        time.sleep(0.05)
                        if ok:
                            self.signalMessage.emit(self.objectName(), 'ui',
                                                    {'msg': f"{bank_name} 스크립트 ({idx}/{total_chunks}) 전송 완료"})
                        else:
                            failed_banks.add(bank_name)
                            self.signalMessage.emit(self.objectName(), 'ui',
                                                    {'msg': f"[Error] {bank_name} 데이터 전송 실패"})
                    except Exception:
                        failed_banks.add(bank_name)
                        self.signalMessage.emit(self.objectName(), 'ui',
                                                {'msg': f"[Error] {bank_name} 데이터 전송 중 예외 발생"})

            # 3) EOF 전송
            for bank_name, addr in bank_addrs.items():
                if bank_name in failed_banks:
                    continue
                ok = self.writeCard.send_data(client_socket=addr, data="EOF")
                if ok:
                    print(f"[UDPServer] {bank_name}에 종료 시그널 전송 완료")
                else:
                    self.signalMessage.emit(self.objectName(), 'ui',
                                            {'msg': f"[Error] {bank_name} 종료 시그널 전송 실패"})

        except Exception as e:
            self.signalMessage.emit(self.objectName(), 'ui', {'msg': f"[Error] 스크립트 전송 중 오류 발생: {str(e)}"})
            print(f"[JobController Error] 스크립트 전송 중 오류 발생: {e}")







    # def send_scripts_to_clients(self):
    #     """
    #     연결된 클라이언트에게 스크립트를 전송
    #     """
    #     send_chunks = set()
    #
    #     try:
    #         family_name = util_base.get_xml_info(self.baseDir, 'recent/familyName')
    #         model_name = util_base.get_xml_info(self.baseDir, 'recent/modelName')
    #
    #         if not family_name or not model_name:
    #             self.signalMessage.emit(self.objectName(), 'ui',
    #                                     {'msg': "[Error] familyName 또는 modelName 정보를 가져오지 못했습니다."})
    #             return
    #
    #         settings_file_path = os.path.join(self.baseDir, 'models', family_name, model_name, 'settings.xml')
    #         if not os.path.exists(settings_file_path):
    #             self.signalMessage.emit(self.objectName(), 'ui', {'msg': f"[Error] settings.xml 파일을 찾을 수 없습니다."})
    #             self.signalMessage.emit(self.objectName(), 'ui', {'msg': f"[Error] {settings_file_path}"})
    #
    #         try:
    #             tree = ET.parse(settings_file_path)
    #             root = tree.getroot()
    #
    #             script_element = root.find(".//script")
    #             if script_element is None or 'filePath' not in script_element.attrib:
    #                 self.signalMessage.emit(self.objectName(), 'ui',
    #                                         {'msg': "[Error] settings.xml에서 script 태그 또는 filePath키를 찾을 수 없습니다."})
    #                 return
    #
    #             script_file_path = os.path.join(self.baseDir, 'models', family_name, model_name,
    #                                             script_element.get('filePath'))
    #             if not os.path.exists(script_file_path):
    #                 self.signalMessage.emit(self.objectName(), 'ui',
    #                                         {'msg': f"[Error] 스크립트 파일을 찾을 수 없습니다. : {script_file_path}"})
    #                 return
    #         except ET.ParseError as e:
    #             self.signalMessage.emit(self.objectName(), 'ui',
    #                                     {'msg': f"[Error] settings.xml 파일을 파싱하는 중 오류 발생: {str(e)}"})
    #             return
    #
    #         clients_info = util_base.get_xml_info(self.baseDir, 'clients')
    #         connected_sockets = self.writeCard.get_connected_sockets(clients_info)
    #
    #         for client_name, client_socket in connected_sockets.items():
    #             try:
    #                 if client_socket:
    #                     self.writeCard.send_data(client_socket=client_socket, data="Script send")
    #                     self.signalMessage.emit(self.objectName(), 'ui',
    #                                             {'msg': f"클라이언트 {client_name} 'Script send' 메시지 전송 완료"})
    #                 else:
    #                     self.signalMessage.emit(self.objectName(), 'ui',
    #                                             {'msg': f"[Error] 클라이언트 {client_name}의 IP 또는 포트 정보가 누락되었습니다."})
    #             except Exception as e:
    #                 self.signalMessage.emit(self.objectName(), 'ui',
    #                                         {'msg': f"[Error] 클라이언트에게 'Script send' 메시지 전송 중 오류가 발생하였습니다."})
    #
    #         chunks = []
    #         with open(script_file_path, 'rb') as script_file:
    #             while chunk := script_file.read(1024):
    #                 chunks.append(chunk)
    #             total_chunks = len(chunks)
    #
    #             failed_clients = set()
    #
    #             for idx, chunk in enumerate(chunks, 1):
    #                 for client_name, client_socket in list(connected_sockets.items()):
    #                     if client_name in failed_clients:
    #                         continue
    #                     try:
    #                         if client_socket:
    #                             ok = self.writeCard.send_chunk_to_clients(client_socket=client_socket, chunk=chunk)
    #                             if ok:
    #                                 self.signalMessage.emit(self.objectName(), 'ui', {
    #                                     'msg': f"클라이언트 {client_name} 스크립트 ({idx}/{total_chunks}) 전송 완료"
    #                                 })
    #                             else:
    #                                 failed_clients.add(client_name)
    #                                 self.signalMessage.emit(self.objectName(), 'ui', {
    #                                     'msg': f"[Error] 클라이언트 {client_name} 데이터 전송 실패(연결 끊김)"
    #                                 })
    #                         else:
    #                             failed_clients.add(client_name)
    #                             self.signalMessage.emit(self.objectName(), 'ui', {
    #                                 'msg': f"[Error] 클라이언트 {client_name} 의 IP 또는 포트 정보가 누락되었습니다."
    #                             })
    #                     except Exception:
    #                         failed_clients.add(client_name)
    #                         self.signalMessage.emit(self.objectName(), 'ui', {
    #                             'msg': f"[Error] 클라이언트 {client_name} 데이터 전송 중 예외 발생"
    #                         })
    #
    #             for client_name, client_socket in connected_sockets.items():
    #                 if client_name in failed_clients:
    #                     continue
    #                 ok = self.writeCard.send_data(client_socket, b"EOF")
    #                 if ok:
    #                     print(f"[TCPServer] 클라이언트 {client_name}에 종료 시그널 전송 완료")
    #                 else:
    #                     self.signalMessage.emit(self.objectName(), 'ui', {
    #                         'msg': f"[Error] 클라이언트 {client_name} 종료 시그널 전송 실패(연결 끊김)"
    #                     })
    #
    #     except Exception as e:
    #         self.signalMessage.emit(self.objectName(), 'ui', {'msg': f"[Error] 스크립트 전송 중 오류 발생: {str(e)}"})
    #         print(f"[JobController Error] 스크립트 전송 중 오류 발생: {e}")



    def _get_carrier_columns(self) -> int:
        """
        settings.xml의 <carrier columns="..."> 값을 반환.
        - 파싱 실패 시 기본값 2를 반환.
        """
        settings_path = getattr(self, "settings_xml_info", None)
        if not settings_path or not os.path.exists(settings_path):
            return 2

        # 1차: util_base.parse_settings_xml 사용
        try:
            attrs = util_base.parse_settings_xml(settings_path, "carrier")
            if attrs and "columns" in attrs:
                return int(attrs.get("columns"))
        except Exception:
            pass

        # 2차: XPath로 직접 파싱
        try:
            tree = ET.parse(settings_path)
            root = tree.getroot()
            node = root.find(".//carrier")
            if node is not None and node.get("columns") is not None:
                return int(node.get("columns"))
        except Exception:
            pass

        return 2

    def update_job_module_status(self, values):
        """
        'Script save finished: MCU n' 메시지에 따라 job_modules_Left/Right의 모듈 준비 상태를 갱신.
        - <carrier columns="1"> 인 경우: writecard1~4 모두 Left 딕셔너리(self.job_modules_Left)에 매핑.
        - <carrier columns="2"> (또는 기본): Left=writecard1/2, Right=writecard3/4로 매핑.
        - 전체 준비 완료 시:
          - columns=1 → self.clsInfo['Left_Recon_Script'] = True
          - columns>=2 → Left/Right 각각에 대해 해당 플래그 True
        """
        msg = values.get('msg', '')
        where = values.get('where', '')

        if msg.startswith('Script save Failed: MCU'):
            print(msg)
            return

        if msg.startswith('Script save finished: MCU') and where.startswith('Write Card '):
            # MCU 번호 파싱
            try:
                mcu_part = msg.split('MCU')[1]
                mcu_num = int(mcu_part.strip().replace(',', ''))
            except Exception as e:
                self.logger.error(f"MCU 파싱 실패: {msg} : {e}")
                return

            # Write Card 번호 파싱
            try:
                writecard_num = int(where.replace('Write Card', '').strip())
            except Exception as e:
                self.logger.error(f"Write Card 파싱 실패: {where} : {e}")
                return

            # carrier columns 확인
            carrier_cols = self._get_carrier_columns()

            # 타깃 dict/플래그 결정
            if carrier_cols == 1:
                # 모든 writecard(1~4)를 Left dict로 처리
                target_dict = self.job_modules_Left
                writecard = f'writecard{writecard_num}'
                ready_flag_attr = 'left_ready_printed'
            else:
                # 기본(기존): 1,2 → Left / 3,4 → Right
                if writecard_num in [1, 2]:
                    target_dict = self.job_modules_Left
                    writecard = f'writecard{writecard_num}'
                    ready_flag_attr = 'left_ready_printed'
                elif writecard_num in [3, 4]:
                    target_dict = self.job_modules_Right
                    writecard = f'writecard{writecard_num}'
                    ready_flag_attr = 'right_ready_printed'
                else:
                    self.logger.error(f"잘못된 Write Card 번호: {writecard_num}")
                    return

            # writecard별 MCU → ModuleN 매핑
            def get_module_key_by_writecard_and_mcu(target_dict, writecard, mcu_num):
                module_keys = [k for k, v in target_dict.items() if v[1] == writecard]
                module_keys.sort(key=lambda x: int(x.replace('Module', '')))
                if 0 < mcu_num <= len(module_keys):
                    return module_keys[mcu_num - 1]
                return None

            module_key = get_module_key_by_writecard_and_mcu(target_dict, writecard, mcu_num)
            if module_key and module_key in target_dict:
                entry = target_dict[module_key]
                entry[0] = True  # ready 플래그 True
                target_dict[module_key] = entry
                side = 'Left' if (carrier_cols == 1 or writecard_num in [1, 2]) else 'Right'
                self.logger.info(f"Updated {entry[1]}-{module_key} in {side} to True")
                print(
                    f"job_Left: {self.job_modules_Left}' if side == 'Left' else f'job_Right: {self.job_modules_Right}")
            else:
                self.logger.warning(
                    f"Write Card/MCU 매칭 실패: Write Card {writecard}, MCU {mcu_num} → 해당 Module 없음 (ignored)")

            # 전체 준비 완료 판정 및 clsInfo 갱신
            all_ready = all(val[0] is True for val in target_dict.values()) and len(target_dict) > 0
            if all_ready and not getattr(self, ready_flag_attr):
                if carrier_cols == 1:
                    # 단일 컬럼: Left만 존재 → Left_Recon_Script True
                    self.clsInfo['Left_Recon_Script'] = True
                    print(f"Left recon script status : {self.clsInfo['Left_Recon_Script']}")
                    setattr(self, ready_flag_attr, True)
                    self.signalMessage.emit(self.objectName(), 'ui', {'msg':'Left Recon Script Loaded'})
                else:
                    if writecard_num in [1, 2]:
                        self.clsInfo['Left_Recon_Script'] = True
                        print(f"Left recon script status : {self.clsInfo['Left_Recon_Script']}")
                        self.signalMessage.emit(self.objectName(), 'ui', {'msg':"Left Recon Script Loaded"})
                    else:
                        self.clsInfo['Right_Recon_Script'] = True
                        print(f"Left recon script status : {self.clsInfo['Right_Recon_Script']}")
                        self.signalMessage.emit(self.objectName(), 'ui', {'msg':"Right Recon Script Loaded"})
                    setattr(self, ready_flag_attr, True)
            if self.clsInfo['Left_Recon_Script'] and self.clsInfo['Right_Recon_Script'] == True:
                self.signalMessage.emit(self.objectName(), 'job', {'msg':'Script all loaded'})

    def _get_io_socket_cached_or_lookup(self):
        """
        IO 보드 소켓을 캐시에서 우선 반환하고, 없으면 ioBoard 모듈에서 조회.
        """
        if self.io_socket:
            return self.io_socket
        try:
            sockets = self.ioBoard.get_connected_sockets()
            return sockets.get('Bank5')
        except Exception:
            return None

    def get_writecard_qty(self, settings_path, card_number):
        # card_number: int (1~4)
        tag = f'writecard{card_number}'
        try:
            tree = ET.parse(settings_path)
            root = tree.getroot()
            qty_tag = root.find(f".//{tag}")
            if qty_tag is not None and qty_tag.attrib.get('qty') is not None:
                return int(qty_tag.attrib['qty'])
        except Exception as e:
            print(f"settings.xml 파싱 실패: {e}")
        return 0

    def do_test(self):
        TIME_INTERVAL = 0.1
        TIME_OUT_SEC = 10
        idx_examine = 0
        cnt_timeOut = 0
        finalResult = None
        reasonOfFail = None

        family_name = util_base.get_xml_info(self.baseDir, 'recent/familyName')
        model_name = util_base.get_xml_info(self.baseDir, 'recent/modelName')
        settings_path = os.path.join(self.baseDir, "models", family_name, model_name, "settings.xml")

        self._barcode_read_requested = False
        self.clsInfo['barcode_stop'] = False

        # carrier columns 파싱
        try:
            tree = ET.parse(settings_path)
            root = tree.getroot()
            carrier_el = root.find(".//carrier")
            carrier_columns = int((carrier_el.attrib.get('columns') or carrier_el.attrib.get('column') or 2))
        except Exception:
            carrier_columns = 2

        # Bank5 주소 해석(캐시 우선, 없으면 sysInfo.xml)
        def _resolve_bank5_addr():
            try:
                sockets = self.ioBoard.get_connected_sockets()
                addr = sockets.get('Bank5')
                if addr:
                    return addr
            except Exception:
                pass
            return self._get_bank_addr_from_config(5)

        while self.clsInfo['is_examine']:
            # 스캔 중단 즉시 복귀
            if bool(self.clsInfo.get('barcode_stop')):
                addr = _resolve_bank5_addr()
                if addr:
                    self.writeCard.send_data(client_socket=addr, data='Pusher back')
                    print("[do_test] Sent 'Pusher back' due to barcode_stop")
                if finalResult is None:
                    finalResult = 'Fail'
                if not reasonOfFail:
                    reasonOfFail = 'Scan Failed'
                idx_examine = 100
                self.clsInfo['barcode_stop'] = False
                continue

            if idx_examine == 0:
                try:
                    rt_left = bool(self.p_mainWindow.clsInfo.get('RealTimeLeft'))
                    rt_right = bool(self.p_mainWindow.clsInfo.get('RealTimeRight'))
                    if rt_left or rt_right:
                        print("[do_test] RealTimeLeft/Right detected → terminate test immediately")
                        idx_examine = 100
                        continue
                except Exception as e:
                    print(f"[do_test] RealTime flags check error: {e}")

                if self.p_mainWindow.clsInfo['qty pass'] == False:
                    reasonOfFail = f'Module quantity error: Please check settings.xml'
                    finalResult = 'Fail'
                    idx_examine = 100
                else:
                    self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': 'STStart'})
                    idx_examine += 1

            if idx_examine == 1:
                # any_fail = False
                # for i in range(1, 5):
                #     ready_key = f'Writecard {i} Ready'
                #     qty = self.get_writecard_qty(settings_path, i)
                #     if qty != 0 and self.clsInfo[ready_key] == False:
                #         reasonOfFail = f'Writecard {i} is not connected'
                #         finalResult = 'Fail'
                #         idx_examine = 100
                #         any_fail = True
                #         break
                # if any_fail:
                #     time.sleep(TIME_INTERVAL)
                #     continue
                self.signalMessage.emit(self.objectName(), 'job',
                                        {'where': 'do_test', 'msg': 'Write cards connection OK'})
                idx_examine += 1

            elif idx_examine == 2:
                if carrier_columns == 1:
                    if self.clsInfo['Left_Recon_Script'] is True:
                        self.signalMessage.emit(self.objectName(), 'job',
                                                {'where': 'do_test', 'msg': 'Script loaded OK'})
                        idx_examine += 1
                    else:
                        reasonOfFail = 'Left Recon Script is not loaded'
                        finalResult = 'Fail'
                        idx_examine = 100
                else:
                    if self.clsInfo['Left_Recon_Script'] is True and self.clsInfo['Right_Recon_Script'] is True:
                        self.signalMessage.emit(self.objectName(), 'job',
                                                {'where': 'do_test', 'msg': 'Script loaded OK'})
                        idx_examine += 1
                    elif self.clsInfo['Left_Recon_Script'] is False:
                        reasonOfFail = 'Left Recon Script is not loaded'
                        finalResult = 'Fail'
                        idx_examine = 100
                    elif self.clsInfo['Right_Recon_Script'] is False:
                        reasonOfFail = 'Right Recon Script is not loaded'
                        finalResult = 'Fail'
                        idx_examine = 100

            elif idx_examine == 3:
                self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': 'Mapping start'})
                idx_examine += 1

            elif idx_examine == 4:
                if not self._barcode_read_requested:
                    self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': 'Barcode read'})
                    self._barcode_read_requested = True
                if self.clsInfo.get('1st_image_scan') is True:
                    cnt_timeOut = 0
                    idx_examine += 1
                    self._barcode_read_requested = False

            elif idx_examine == 5:
                # 파일 정리
                barcode_dir = os.path.join(self.baseDir, "barcode")
                for p in (
                        os.path.join(barcode_dir, "Left.txt"),
                        os.path.join(barcode_dir, "Right.txt"),
                        os.path.join(barcode_dir, "Left_Recon.txt"),
                        os.path.join(barcode_dir, "Right_Recon.txt"),
                ):
                    if os.path.exists(p):
                        try:
                            with open(p, 'w') as f:
                                f.write("")
                            print(f"Cleared content of file: {p}")
                        except Exception as e:
                            print(f"[Error] Unable to clear content of {p}: {e}")

                # IO 보드로 'Pusher front' 전송(UDP)
                addr = _resolve_bank5_addr()
                if addr and self.writeCard.send_data(client_socket=addr, data='Pusher front'):
                    self.signalMessage.emit(self.objectName(), 'ui',
                                            {'msg': "Sent 'Pusher front' to IO Board (Bank5)"})
                    self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': 'Pusher front'})
                    cnt_timeOut = 0
                    idx_examine += 1
                else:
                    self.signalMessage.emit(self.objectName(), 'ui',
                                            {
                                                'msg': "[Warning] IO Board (Bank5) is not connected. Could not send 'Pusher front'."})
                    reasonOfFail = 'Cannot send message to IO board'
                    finalResult = 'Fail'
                    idx_examine = 100

            elif idx_examine == 6:
                if not self.clsInfo.get('1st_left_barcode'):
                    time.sleep(TIME_INTERVAL)
                    continue

                def _all_scanned(modules_dict: dict) -> bool:
                    for v in modules_dict.values():
                        try:
                            if bool(v[0]) and v[2] is None:
                                return False
                        except Exception:
                            return False
                    return True

                left_done = _all_scanned(self.p_mainWindow.modules_Left)
                if not left_done:
                    time.sleep(TIME_INTERVAL)
                    continue

                settings_path2 = os.path.join(
                    self.baseDir, "models",
                    self.p_mainWindow.cb_customerName.currentText(),
                    self.p_mainWindow.cb_selectedModel.currentText(),
                    "settings.xml"
                )
                barcode_length = None
                try:
                    tree = ET.parse(settings_path2)
                    root = tree.getroot()
                    barcode_tag = root.find(".//barcode")
                    if barcode_tag is not None and barcode_tag.attrib.get('length') is not None:
                        barcode_length = int(barcode_tag.attrib['length'])
                except Exception as e:
                    print(f"settings.xml barcode 길이 파싱 실패: {e}")

                def check_left(modules_dict, barcode_length):
                    for v in modules_dict.values():
                        if v[0] is True:
                            if v[2] is None:
                                return 'Barcode reading error'
                            if isinstance(v[2], str):
                                if v[2] != 'Scan Failed' and barcode_length is not None and len(v[2]) != barcode_length:
                                    return 'Barcode reading error'
                            if v[2] == 'Scan Failed':
                                return 'Scan Failed'
                    return None

                left_result = check_left(self.p_mainWindow.modules_Left, barcode_length)
                print(f'job (Left only at idx 6): {self.p_mainWindow.modules_Left}')

                if left_result == 'Barcode reading error':
                    reasonOfFail = 'Barcode reading error'
                    finalResult = 'Fail'
                    idx_examine = 100
                    addr = _resolve_bank5_addr()
                    if addr:
                        self.writeCard.send_data(client_socket=addr, data='go_init')
                    continue
                elif left_result == 'Scan Failed':
                    reasonOfFail = 'Scan Failed'
                    finalResult = 'Fail'
                    idx_examine = 100
                    addr = _resolve_bank5_addr()
                    if addr:
                        self.writeCard.send_data(client_socket=addr, data='go_init')
                    continue
                else:
                    idx_examine += 1
                    cnt_timeOut = 0
                    self.signalMessage.emit(
                        self.objectName(), 'job',
                        {'where': 'do_test', 'msg': '1st_left_barcode_OK'}
                    )
                    continue

            elif idx_examine == 7:
                if not self.clsInfo.get('1st_right_barcode'):
                    time.sleep(TIME_INTERVAL)
                    continue

                def _all_scanned(modules_dict: dict) -> bool:
                    for v in modules_dict.values():
                        try:
                            if bool(v[0]) and v[2] is None:
                                return False
                        except Exception:
                            return False
                    return True

                right_done = _all_scanned(self.p_mainWindow.modules_Right)
                if not right_done:
                    time.sleep(TIME_INTERVAL)
                    continue

                settings_path2 = os.path.join(
                    self.baseDir, "models",
                    self.p_mainWindow.cb_customerName.currentText(),
                    self.p_mainWindow.cb_selectedModel.currentText(),
                    "settings.xml"
                )
                barcode_length = None
                try:
                    tree = ET.parse(settings_path2)
                    root = tree.getroot()
                    barcode_tag = root.find(".//barcode")
                    if barcode_tag is not None and barcode_tag.attrib.get('length') is not None:
                        barcode_length = int(barcode_tag.attrib['length'])
                except Exception as e:
                    print(f"settings.xml barcode 길이 파싱 실패: {e}")

                def check_right(modules_dict, barcode_length):
                    for v in modules_dict.values():
                        if v[0] is True:
                            if v[2] is None:
                                return 'Barcode reading error'
                            if isinstance(v[2], str):
                                if v[2] != 'Scan Failed' and barcode_length is not None and len(v[2]) != barcode_length:
                                    return 'Barcode reading error'
                            if v[2] == 'Scan Failed':
                                return 'Scan Failed'
                    return None

                right_result = check_right(self.p_mainWindow.modules_Right, barcode_length)
                print(f'job (Right only at idx 7): {self.p_mainWindow.modules_Right}')

                if right_result == 'Barcode reading error':
                    reasonOfFail = 'Barcode reading error'
                    finalResult = 'Fail'
                    idx_examine = 100
                    addr = _resolve_bank5_addr()
                    if addr:
                        self.writeCard.send_data(client_socket=addr, data='go_init')
                    continue
                elif right_result == 'Scan Failed':
                    reasonOfFail = 'Scan Failed'
                    finalResult = 'Fail'
                    idx_examine = 100
                    addr = _resolve_bank5_addr()
                    if addr:
                        self.writeCard.send_data(client_socket=addr, data='go_init')
                    continue
                else:
                    self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': '1st Scan OK'})
                    self.send_barcodes_to_clients()
                    print('send_barcodes_clients executed')
                    idx_examine += 1
                    cnt_timeOut = 0
                    self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': '1st_Scan_OK'})
                    continue

            elif idx_examine == 8:
                idx_examine += 1

            elif idx_examine == 9:
                self.send_signal_to_clients(msg='barcode sending finished')
                idx_examine += 1

            elif idx_examine == 10:
                if self.clsInfo['sensor_data1'] == True and self.clsInfo['sensor_data2'] == True and self.clsInfo[
                    'sensor_data3'] == True and self.clsInfo['sensor_data4'] == True:
                    self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': 'sensor ID recieved'})
                    idx_examine += 1
                    cnt_timeOut = 0

            elif idx_examine == 11:
                if (self.clsInfo['barcode_data1'] and self.clsInfo['barcode_data2']
                        and self.clsInfo['barcode_data3'] and self.clsInfo['barcode_data4']):
                    self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': '2nd barcode OK'})
                    addr = _resolve_bank5_addr()
                    if addr and self.writeCard.send_data(client_socket=addr, data='Pusher back'):
                        self.clsInfo['pusher back'] = True
                        self.signalMessage.emit(self.objectName(), 'ui',
                                                {'msg': "Sent 'Pusher back' to IO Board (Bank5)"})
                    else:
                        self.signalMessage.emit(self.objectName(), 'ui',
                                                {
                                                    'msg': "[Warning] IO Board (Bank5) is not connected. Could not send 'Pusher back'."})
                    idx_examine += 1

            elif idx_examine == 12:
                if self.clsInfo['c_save'] == True:
                    self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': 'c_save finished'})
                    idx_examine += 1
                    cnt_timeOut = 0

            elif idx_examine == 13:
                if self.clsInfo['sensor_dict update'] == True:
                    self.signalMessage.emit(self.objectName(), 'job',
                                            {'where': 'do_test', 'msg': 'sensor_dict updated'})
                    idx_examine += 1
                    cnt_timeOut = 0

            elif idx_examine == 14:
                def _has_pairwise_mismatch(modules_dict: dict) -> bool:
                    for v in modules_dict.values():
                        try:
                            if v[0] is True:
                                a = v[2] if len(v) > 2 else None
                                b = v[4] if len(v) > 4 else None
                                if a is not None and b is not None and str(a) != str(b):
                                    return True
                        except Exception:
                            continue
                    return False

                try:
                    left_mismatch = _has_pairwise_mismatch(self.p_mainWindow.modules_Left)
                    right_mismatch = _has_pairwise_mismatch(self.p_mainWindow.modules_Right)
                except Exception as e:
                    print(f"[do_test] pairwise compare error: {e}")
                    left_mismatch = right_mismatch = True

                if left_mismatch or right_mismatch:
                    finalResult = finalResult or 'Fail'
                    reasonOfFail = reasonOfFail or 'Barcode mismatch (col3 vs col5)'
                    idx_examine = 100
                    continue

                idx_examine += 1
                cnt_timeOut = 0

            elif idx_examine == 15:
                if self.clsInfo['2nd show update'] == True and self.clsInfo['pusher back'] == True:
                    self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': '3rd Barcode shot'})

                    # 파일 정리
                    barcode_dir = os.path.join(self.baseDir, "barcode")
                    for p in [
                        os.path.join(barcode_dir, "Left.txt"),
                        os.path.join(barcode_dir, "Right.txt"),
                        os.path.join(barcode_dir, "Left_Recon.txt"),
                        os.path.join(barcode_dir, "Right_Recon.txt"),
                    ]:
                        if os.path.exists(p):
                            try:
                                with open(p, 'w') as f:
                                    f.write("")
                                print(f"Cleared content of file: {p}")
                            except Exception as e:
                                print(f"[Error] Unable to clear content of {p}: {e}")

                    idx_examine += 1
                    cnt_timeOut = 0

            elif idx_examine == 16:
                if self.clsInfo['3rd_shot'] == True:
                    self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': '3rd left barcode'})
                    idx_examine += 1
                    cnt_timeOut = 0

            elif idx_examine == 17:
                if self.clsInfo['3rd_left_barcode'] == True:
                    settings_path2 = os.path.join(self.baseDir, "models",
                                                  self.p_mainWindow.cb_customerName.currentText(),
                                                  self.p_mainWindow.cb_selectedModel.currentText(), "settings.xml")
                    barcode_length = None
                    try:
                        tree = ET.parse(settings_path2)
                        root = tree.getroot()
                        barcode_tag = root.find(".//barcode")
                        if barcode_tag is not None and barcode_tag.attrib.get('length') is not None:
                            barcode_length = int(barcode_tag.attrib['length'])
                    except Exception as e:
                        print(f"settings.xml barcode 길이 파싱 실패: {e}")

                    def check_barcode_length(modules_dict, barcode_length):
                        for v in modules_dict.values():
                            if v[0] is True and v[5] is not None and isinstance(v[5], str):
                                if v[2] != 'Scan Failed' and barcode_length is not None and len(v[5]) != barcode_length:
                                    return 'Barcode reading error'
                            if v[0] is True and v[5] == 'Scan Failed':
                                return 'Scan Failed'
                        return None

                    left_result = check_barcode_length(self.p_mainWindow.modules_Left, barcode_length)
                    print(f'do_test (3rd, Left only at idx 15) : modules_Left : {self.p_mainWindow.modules_Left}')

                    if left_result == 'Barcode reading error':
                        reasonOfFail = 'Barcode reading error'
                        finalResult = 'Fail'
                        idx_examine = 100
                    elif left_result == 'Scan Failed':
                        reasonOfFail = 'Scan Failed'
                        finalResult = 'Fail'
                        idx_examine = 100
                    else:
                        idx_examine += 1
                        cnt_timeOut = 0
                        self.signalMessage.emit(self.objectName(), 'job',
                                                {'where': 'do_test', 'msg': '3rd_left_barcode_OK'})

            elif idx_examine == 18:
                if self.clsInfo['3rd_right_barcode'] == True:
                    settings_path2 = os.path.join(self.baseDir, "models",
                                                  self.p_mainWindow.cb_customerName.currentText(),
                                                  self.p_mainWindow.cb_selectedModel.currentText(), "settings.xml")
                    barcode_length = None
                    try:
                        tree = ET.parse(settings_path2)
                        root = tree.getroot()
                        barcode_tag = root.find(".//barcode")
                        if barcode_tag is not None and barcode_tag.attrib.get('length') is not None:
                            barcode_length = int(barcode_tag.attrib['length'])
                    except Exception as e:
                        print(f"settings.xml barcode 길이 파싱 실패: {e}")

                    def check_barcode_length(modules_dict, barcode_length):
                        for v in modules_dict.values():
                            if v[0] is True and v[5] is not None and isinstance(v[5], str):
                                if v[2] != 'Scan Failed' and barcode_length is not None and len(v[5]) != barcode_length:
                                    return 'Barcode reading error'
                            if v[0] is True and v[5] == 'Scan Failed':
                                return 'Scan Failed'
                        return None

                    right_result = check_barcode_length(self.p_mainWindow.modules_Right, barcode_length)
                    print(f'do_test (3rd, Right only at idx 16) : modules_Right : {self.p_mainWindow.modules_Right}')

                    if right_result == 'Barcode reading error':
                        reasonOfFail = 'Barcode reading error'
                        finalResult = 'Fail'
                        idx_examine = 100
                    elif right_result == 'Scan Failed':
                        reasonOfFail = 'Scan Failed'
                        finalResult = 'Fail'
                        idx_examine = 100
                    else:
                        self.signalMessage.emit(self.objectName(), 'job',
                                                {'here': 'do_test', 'msg': 'examine finished'})
                        idx_examine += 1
                        cnt_timeOut = 0

            elif idx_examine == 19:
                print('this message only should be shown when all OK')
                idx_examine = 100

            elif idx_examine == 100:
                self.signalMessage.emit(self.objectName(), 'job',
                                        {'where': 'do_test', 'finalResult': finalResult, 'reasonOfFail': reasonOfFail})
                self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': 'STStop'})

                # 종료 시 IO 보드 초기화(UDP)
                addr = _resolve_bank5_addr()
                if addr:
                    self.writeCard.send_data(client_socket=addr, data='ManualPusherInitial')

                self.clsInfo['is_examine'] = False
                self._test_thread = None
                print('idx_examine = 100. test finished')

                # 플래그 리셋
                self.clsInfo['barcode_stop'] = False
                self._barcode_read_requested = False
                self.clsInfo['1st_image_scan'] = False
                self.clsInfo['1st_left_barcode'] = False
                self.clsInfo['1st_right_barcode'] = False
                self.clsInfo['sensor data'] = False
                self.clsInfo['2nd_barcode'] = False
                self.clsInfo['3rd_shot'] = False
                self.clsInfo['3rd_left_barcode'] = False
                self.clsInfo['3rd_right_barcode'] = False
                self.clsInfo['c_save'] = False
                self.clsInfo['sensor_dic update'] = False
                self.clsInfo['sensor_data1'] = False
                self.clsInfo['sensor_data2'] = False
                self.clsInfo['sensor_data3'] = False
                self.clsInfo['sensor_data4'] = False
                self.clsInfo['barcode_data1'] = False
                self.clsInfo['barcode_data2'] = False
                self.clsInfo['barcode_data3'] = False
                self.clsInfo['barcode_data4'] = False
                self.clsInfo['2nd show update'] = False
                self.clsInfo['3rd_shot'] = False
                self.clsInfo['1st_image_scan'] = False
                self.clsInfo['pusher back'] = False
                self.clsInfo['Scan Stop'] = False
                self.clsInfo['pusher_down_started'] = False
                self.clsInfo['pusher_down_finished'] = False
                self.clsInfo['button_unpushed'] = False
                self.clsInfo['pusher_down_ts'] = None
                self.clsInfo['button_unpushed_ts'] = None
                self.clsInfo['pusher_sequence_decided'] = False
                self.clsInfo['early_button_unpushed'] = False

                for v in self.job_modules_Left.values():
                    v[0] = False
                    v[2] = None
                    v[3] = None
                for v in self.job_modules_Right.values():
                    v[0] = False
                    v[2] = None
                    v[3] = None
                return

            # Abort Test
            if self.clsInfo['is_abortTest']:
                finalResult = 'Fail'
                reasonOfFail = 'Abort Test'
                idx_examine = 100

            # Test TimeOut
            cnt_timeOut += 1
            if cnt_timeOut > (TIME_OUT_SEC / TIME_INTERVAL):
                finalResult = 'Fail'
                reasonOfFail = 'Timeout'
                idx_examine = 100

            time.sleep(TIME_INTERVAL)



    # def do_test(self):
    #     TIME_INTERVAL = 0.1
    #     TIME_OUT_SEC = 10
    #     idx_examine = 0
    #     cnt_timeOut = 0
    #     finalResult = None
    #     reasonOfFail = None
    #
    #     family_name = util_base.get_xml_info(self.baseDir, 'recent/familyName')
    #     model_name = util_base.get_xml_info(self.baseDir, 'recent/modelName')
    #     settings_path = os.path.join(self.baseDir, "models", family_name, model_name, "settings.xml")
    #
    #     self._barcode_read_requested = False
    #
    #     def get_io_socket():
    #         return self._get_io_socket_cached_or_lookup()
    #
    #     self.clsInfo['barcode_stop'] = False
    #
    #     # carrier columns 파싱(기존 동일)
    #     try:
    #         tree = ET.parse(settings_path)
    #         root = tree.getroot()
    #         carrier_el = root.find(".//carrier")
    #         carrier_columns = int((carrier_el.attrib.get('columns') or carrier_el.attrib.get('column') or 2))
    #     except Exception:
    #         carrier_columns = 2
    #
    #     while self.clsInfo['is_examine']:
    #         if bool(self.clsInfo.get('barcode_stop')):
    #             try:
    #                 io_socket = get_io_socket()
    #                 if io_socket:
    #                     self.ioBoard.send_data(client_socket=io_socket, data='Pusher back')
    #                     print("[do_test] Sent 'Pusher back' due to barcode_stop")
    #             except Exception as e:
    #                 print(f"[do_test] Failed to send 'Pusher back' on barcode_stop: {e}")
    #
    #             if finalResult is None:
    #                 finalResult = 'Fail'
    #             if not reasonOfFail:
    #                 reasonOfFail = 'Scan Failed'
    #             idx_examine = 100
    #             self.clsInfo['barcode_stop'] = False
    #             continue
    #
    #         if idx_examine == 0:
    #             try:
    #                 rt_left = bool(self.p_mainWindow.clsInfo.get('RealTimeLeft'))
    #                 rt_right = bool(self.p_mainWindow.clsInfo.get('RealTimeRight'))
    #                 if rt_left or rt_right:
    #                     print("[do_test] RealTimeLeft/Right detected → terminate test immediately")
    #                     idx_examine = 100
    #                     continue
    #             except Exception as e:
    #                 print(f"[do_test] RealTime flags check error: {e}")
    #
    #             if self.p_mainWindow.clsInfo['qty pass'] == False:
    #                 reasonOfFail = f'Module quantity error: Please check settings.xml'
    #                 finalResult = 'Fail'
    #                 idx_examine = 100
    #             else:
    #                 self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': 'STStart'})
    #                 idx_examine += 1
    #
    #         if idx_examine == 1:
    #             any_fail = False
    #             for i in range(1, 5):
    #                 ready_key = f'Writecard {i} Ready'
    #                 qty = self.get_writecard_qty(settings_path, i)
    #                 if qty != 0 and self.clsInfo[ready_key] == False:
    #                     reasonOfFail = f'Writecard {i} is not connected'
    #                     finalResult = 'Fail'
    #                     idx_examine = 100
    #                     any_fail = True
    #                     break
    #             if any_fail:
    #                 time.sleep(TIME_INTERVAL)
    #                 continue
    #             self.signalMessage.emit(self.objectName(), 'job',
    #                                     {'where': 'do_test', 'msg': 'Write cards connection OK'})
    #             idx_examine += 1
    #
    #         elif idx_examine == 2:
    #             if carrier_columns == 1:
    #                 if self.clsInfo['Left_Recon_Script'] is True:
    #                     self.signalMessage.emit(self.objectName(), 'job',
    #                                             {'where': 'do_test', 'msg': 'Script loaded OK'})
    #                     idx_examine += 1
    #                 else:
    #                     reasonOfFail = 'Left Recon Script is not loaded'
    #                     finalResult = 'Fail'
    #                     idx_examine = 100
    #             else:
    #                 if self.clsInfo['Left_Recon_Script'] is True and self.clsInfo['Right_Recon_Script'] is True:
    #                     self.signalMessage.emit(self.objectName(), 'job',
    #                                             {'where': 'do_test', 'msg': 'Script loaded OK'})
    #                     idx_examine += 1
    #                 elif self.clsInfo['Left_Recon_Script'] is False:
    #                     reasonOfFail = 'Left Recon Script is not loaded'
    #                     finalResult = 'Fail'
    #                     idx_examine = 100
    #                 elif self.clsInfo['Right_Recon_Script'] is False:
    #                     reasonOfFail = 'Right Recon Script is not loaded'
    #                     finalResult = 'Fail'
    #                     idx_examine = 100
    #
    #         elif idx_examine == 3:
    #             self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': 'Mapping start'})
    #             idx_examine += 1
    #
    #         elif idx_examine == 4:
    #             if not self._barcode_read_requested:
    #                 self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': 'Barcode read'})
    #                 self._barcode_read_requested = True
    #             if self.clsInfo.get('1st_image_scan') is True:
    #                 cnt_timeOut = 0
    #                 idx_examine += 1
    #                 self._barcode_read_requested = False
    #
    #         elif idx_examine == 5:
    #             # [NEW] Bank5(IO Board)로 'Pusher front' 전송 (안전 조회)
    #             # Left.txt와 Right.txt 파일 내용 삭제
    #             barcode_dir = os.path.join(self.baseDir, "barcode")
    #             left_file_path = os.path.join(barcode_dir, "Left.txt")
    #             right_file_path = os.path.join(barcode_dir, "Right.txt")
    #             left_recon_path = os.path.join(barcode_dir, "Left_Recon.txt")
    #             right_recon_path = os.path.join(barcode_dir, "Right_Recon.txt")
    #
    #             # Left.txt 내용 삭제
    #             if os.path.exists(left_file_path):
    #                 try:
    #                     with open(left_file_path, 'w') as file:
    #                         file.write("")  # 파일 내용을 비움
    #                     print(f"Cleared content of file: {left_file_path}")
    #                 except Exception as e:
    #                     print(f"[Error] Unable to clear content of {left_file_path}: {e}")
    #
    #             # Right.txt 내용 삭제
    #             if os.path.exists(right_file_path):
    #                 try:
    #                     with open(right_file_path, 'w') as file:
    #                         file.write("")  # 파일 내용을 비움
    #                     print(f"Cleared content of file: {right_file_path}")
    #                 except Exception as e:
    #                     print(f"[Error] Unable to clear content of {right_file_path}: {e}")
    #
    #                 # NEW: Left_Recon.txt 내용 삭제
    #             if os.path.exists(left_recon_path):
    #                 try:
    #                     with open(left_recon_path, 'w') as file:
    #                         file.write("")  # 파일 내용을 비움
    #                     print(f"Cleared content of file: {left_recon_path}")
    #                 except Exception as e:
    #                     print(f"[Error] Unable to clear content of {left_recon_path}: {e}")
    #
    #                 # NEW: Right_Recon.txt 내용 삭제
    #             if os.path.exists(right_recon_path):
    #                 try:
    #                     with open(right_recon_path, 'w') as file:
    #                         file.write("")  # 파일 내용을 비움
    #                     print(f"Cleared content of file: {right_recon_path}")
    #                 except Exception as e:
    #                     print(f"[Error] Unable to clear content of {right_recon_path}: {e}")
    #
    #             try:
    #                 io_socket = get_io_socket()
    #                 if io_socket:
    #                     self.ioBoard.send_data(client_socket=io_socket, data='Pusher front')
    #                     self.signalMessage.emit(self.objectName(), 'ui',
    #                                             {'msg': "Sent 'Pusher front' to IO Board (Bank5)"})
    #                     self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': 'Pusher front'})
    #                     cnt_timeOut = 0
    #                     idx_examine += 1
    #                 else:
    #                     self.signalMessage.emit(self.objectName(), 'ui',
    #                                             {
    #                                                 'msg': "[Warning] IO Board (Bank5) is not connected. Could not send 'Pusher front'."})
    #                     reasonOfFail = 'Cannot send message to IO board'
    #                     finalResult = 'Fail'
    #                     idx_examine = 100
    #             except Exception as e:
    #                 self.signalMessage.emit(self.objectName(), 'ui',
    #                                         {'msg': f"[Error] Failed to send 'Pusher front' to IO Board: {e}"})
    #                 reasonOfFail = 'Cannot send message to IO board'
    #                 finalResult = 'Fail'
    #                 idx_examine = 100
    #
    #         elif idx_examine == 6:
    #             if not self.clsInfo.get('1st_left_barcode'):
    #                 time.sleep(TIME_INTERVAL)
    #                 continue
    #
    #             # 2) Left 스캔(1차) 완료 플래그가 아직 아니면 대기
    #             if not self.clsInfo.get('1st_left_barcode'):
    #                 time.sleep(TIME_INTERVAL)
    #                 continue
    #
    #             # 3) Left 전체 모듈 스캔 완료 여부 확인
    #             def _all_scanned(modules_dict: dict) -> bool:
    #                 for v in modules_dict.values():
    #                     try:
    #                         if bool(v[0]) and v[2] is None:
    #                             return False
    #                     except Exception:
    #                         return False
    #                 return True
    #
    #             left_done = _all_scanned(self.p_mainWindow.modules_Left)
    #             if not left_done:
    #                 time.sleep(TIME_INTERVAL)
    #                 continue
    #
    #             # 4) 길이/실패 검사 준비
    #             settings_path = os.path.join(
    #                 self.baseDir, "models",
    #                 self.p_mainWindow.cb_customerName.currentText(),
    #                 self.p_mainWindow.cb_selectedModel.currentText(),
    #                 "settings.xml"
    #             )
    #             barcode_length = None
    #             try:
    #                 tree = ET.parse(settings_path)
    #                 root = tree.getroot()
    #                 barcode_tag = root.find(".//barcode")
    #                 if barcode_tag is not None and barcode_tag.attrib.get('length') is not None:
    #                     barcode_length = int(barcode_tag.attrib['length'])
    #             except Exception as e:
    #                 print(f"settings.xml barcode 길이 파싱 실패: {e}")
    #
    #             def check_left(modules_dict, barcode_length):
    #                 for v in modules_dict.values():
    #                     if v[0] is True:
    #                         if v[2] is None:
    #                             return 'Barcode reading error'
    #                         if isinstance(v[2], str):
    #                             if v[2] != 'Scan Failed' and barcode_length is not None and len(v[2]) != barcode_length:
    #                                 return 'Barcode reading error'
    #                         if v[2] == 'Scan Failed':
    #                             return 'Scan Failed'
    #                 return None
    #
    #             left_result = check_left(self.p_mainWindow.modules_Left, barcode_length)
    #             print(f'job (Left only at idx 6): {self.p_mainWindow.modules_Left}')
    #
    #             if left_result == 'Barcode reading error':
    #                 reasonOfFail = 'Barcode reading error'
    #                 finalResult = 'Fail'
    #                 idx_examine = 100
    #                 io_socket = get_io_socket()
    #                 if io_socket:
    #                     self.writeCard.send_data(client_socket=io_socket, data='go_init')
    #                 continue  # 종료 루프 처리 대기
    #             elif left_result == 'Scan Failed':
    #                 reasonOfFail = 'Scan Failed'
    #                 finalResult = 'Fail'
    #                 idx_examine = 100
    #                 io_socket = get_io_socket()
    #                 if io_socket:
    #                     self.writeCard.send_data(client_socket=io_socket, data='go_init')
    #                 continue
    #             else:
    #                 # 정상 → 다음 단계
    #                 idx_examine += 1
    #                 cnt_timeOut = 0
    #                 self.signalMessage.emit(
    #                     self.objectName(), 'job',
    #                     {'where': 'do_test', 'msg': '1st_left_barcode_OK'}
    #                 )
    #                 continue  # 명시적 continue (가독성)
    #
    #         elif idx_examine == 7:
    #             if not self.clsInfo.get('1st_right_barcode'):
    #                 time.sleep(TIME_INTERVAL)
    #                 continue
    #
    #             # 2) Right 1차 바코드 완료 플래그가 아직 False면 대기
    #             if not self.clsInfo.get('1st_right_barcode'):
    #                 time.sleep(TIME_INTERVAL)
    #                 continue
    #
    #             # 3) Right 전체 모듈 스캔 완료 여부 확인
    #             def _all_scanned(modules_dict: dict) -> bool:
    #                 for v in modules_dict.values():
    #                     try:
    #                         if bool(v[0]) and v[2] is None:
    #                             return False
    #                     except Exception:
    #                         return False
    #                 return True
    #
    #             right_done = _all_scanned(self.p_mainWindow.modules_Right)
    #             if not right_done:
    #                 time.sleep(TIME_INTERVAL)
    #                 continue
    #
    #             # 4) 길이/실패 검사 준비
    #             settings_path = os.path.join(
    #                 self.baseDir, "models",
    #                 self.p_mainWindow.cb_customerName.currentText(),
    #                 self.p_mainWindow.cb_selectedModel.currentText(),
    #                 "settings.xml"
    #             )
    #             barcode_length = None
    #             try:
    #                 tree = ET.parse(settings_path)
    #                 root = tree.getroot()
    #                 barcode_tag = root.find(".//barcode")
    #                 if barcode_tag is not None and barcode_tag.attrib.get('length') is not None:
    #                     barcode_length = int(barcode_tag.attrib['length'])
    #             except Exception as e:
    #                 print(f"settings.xml barcode 길이 파싱 실패: {e}")
    #
    #             def check_right(modules_dict, barcode_length):
    #                 for v in modules_dict.values():
    #                     if v[0] is True:
    #                         if v[2] is None:
    #                             return 'Barcode reading error'
    #                         if isinstance(v[2], str):
    #                             if v[2] != 'Scan Failed' and barcode_length is not None and len(v[2]) != barcode_length:
    #                                 return 'Barcode reading error'
    #                         if v[2] == 'Scan Failed':
    #                             return 'Scan Failed'
    #                 return None
    #
    #             right_result = check_right(self.p_mainWindow.modules_Right, barcode_length)
    #             print(f'job (Right only at idx 7): {self.p_mainWindow.modules_Right}')
    #
    #             if right_result == 'Barcode reading error':
    #                 reasonOfFail = 'Barcode reading error'
    #                 finalResult = 'Fail'
    #                 idx_examine = 100
    #                 io_socket = get_io_socket()
    #                 if io_socket:
    #                     self.writeCard.send_data(client_socket=io_socket, data='go_init')
    #                 continue
    #             elif right_result == 'Scan Failed':
    #                 reasonOfFail = 'Scan Failed'
    #                 finalResult = 'Fail'
    #                 idx_examine = 100
    #                 io_socket = get_io_socket()
    #                 if io_socket:
    #                     self.writeCard.send_data(client_socket=io_socket, data='go_init')
    #                 continue
    #             else:
    #                 # Left(6) OK + Right(7) OK → 메시지 송신 후 다음 단계
    #                 self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': '1st Scan OK'})
    #                 self.send_barcodes_to_clients()
    #                 print('send_barcodes_clients executed')
    #                 idx_examine += 1
    #                 cnt_timeOut = 0
    #                 self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': '1st_Scan_OK'})
    #                 continue  # 명시적 continue
    #
    #         elif idx_examine == 8:
    #             idx_examine += 1
    #
    #         elif idx_examine == 9:
    #             self.send_signal_to_clients(msg='barcode sending finished')
    #             idx_examine += 1
    #
    #         elif idx_examine == 10:
    #             if self.clsInfo['sensor_data1'] == True and self.clsInfo['sensor_data2'] == True and self.clsInfo[
    #                 'sensor_data3'] == True and self.clsInfo['sensor_data4'] == True:
    #                 self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': 'sensor ID recieved'})
    #                 idx_examine += 1
    #                 cnt_timeOut = 0
    #
    #         elif idx_examine == 11:
    #             if (self.clsInfo['barcode_data1'] and self.clsInfo['barcode_data2']
    #                     and self.clsInfo['barcode_data3'] and self.clsInfo['barcode_data4']):
    #                 self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': '2nd barcode OK'})
    #                 try:
    #                     io_socket = get_io_socket()
    #                     if io_socket:
    #                         self.clsInfo['pusher back'] = False
    #                         self.ioBoard.send_data(client_socket=io_socket, data='Pusher back')
    #                         self.signalMessage.emit(self.objectName(), 'ui',
    #                                                 {'msg': "Sent 'Pusher back' to IO Board (Bank5)"})
    #                     else:
    #                         self.signalMessage.emit(self.objectName(), 'ui',
    #                                                 {
    #                                                     'msg': "[Warning] IO Board (Bank5) is not connected. Could not send 'Pusher back'."})
    #                 except Exception as e:
    #                     self.signalMessage.emit(self.objectName(), 'ui',
    #                                             {'msg': f"[Error] Failed to send 'Pusher back' to IO Board: {e}"})
    #                 idx_examine += 1
    #
    #         elif idx_examine == 12:
    #             if self.clsInfo['c_save'] == True:
    #                 self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': 'c_save finished'})
    #                 idx_examine += 1
    #                 cnt_timeOut = 0
    #
    #         elif idx_examine == 13:
    #             if self.clsInfo['sensor_dict update'] == True:
    #                 self.signalMessage.emit(self.objectName(), 'job',
    #                                         {'where': 'do_test', 'msg': 'sensor_dict updated'})
    #                 idx_examine += 1
    #                 cnt_timeOut = 0
    #
    #         # [NEW] 14단계: 3열(인덱스2) vs 5열(인덱스4) 비교. 불일치 시 즉시 종료로 전환.
    #         elif idx_examine == 14:
    #             def _has_pairwise_mismatch(modules_dict: dict) -> bool:
    #                 for v in modules_dict.values():
    #                     try:
    #                         if v[0] is True:
    #                             a = v[2] if len(v) > 2 else None
    #                             b = v[4] if len(v) > 4 else None
    #                             # 둘 다 값이 있을 때만 비교해 불일치 판정
    #                             if a is not None and b is not None:
    #                                 if str(a) != str(b):
    #                                     return True
    #                     except Exception:
    #                         # 방어적 무시
    #                         continue
    #                 return False
    #
    #             try:
    #                 left_mismatch = _has_pairwise_mismatch(self.p_mainWindow.modules_Left)
    #                 right_mismatch = _has_pairwise_mismatch(self.p_mainWindow.modules_Right)
    #             except Exception as e:
    #                 print(f"[do_test] pairwise compare error: {e}")
    #                 left_mismatch = right_mismatch = True  # 예외 시 보수적으로 종료
    #
    #             if left_mismatch or right_mismatch:
    #                 finalResult = finalResult or 'Fail'
    #                 reasonOfFail = reasonOfFail or 'Barcode mismatch (col3 vs col5)'
    #                 idx_examine = 100
    #                 continue
    #
    #             # 불일치가 없으면 다음 단계로 진행
    #             idx_examine += 1
    #             cnt_timeOut = 0
    #
    #         elif idx_examine == 15:
    #             if self.clsInfo['2nd show update'] == True and self.clsInfo['pusher back'] == True:
    #                 self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': '3rd Barcode shot'})
    #
    #                 # Left.txt와 Right.txt 파일 내용 삭제
    #                 barcode_dir = os.path.join(self.baseDir, "barcode")
    #                 left_file_path = os.path.join(barcode_dir, "Left.txt")
    #                 right_file_path = os.path.join(barcode_dir, "Right.txt")
    #                 left_recon_path = os.path.join(barcode_dir, "Left_Recon.txt")
    #                 right_recon_path = os.path.join(barcode_dir, "Right_Recon.txt")
    #
    #                 # Left.txt 내용 삭제
    #                 if os.path.exists(left_file_path):
    #                     try:
    #                         with open(left_file_path, 'w') as file:
    #                             file.write("")  # 파일 내용을 비움
    #                         print(f"Cleared content of file: {left_file_path}")
    #                     except Exception as e:
    #                         print(f"[Error] Unable to clear content of {left_file_path}: {e}")
    #
    #                 # Right.txt 내용 삭제
    #                 if os.path.exists(right_file_path):
    #                     try:
    #                         with open(right_file_path, 'w') as file:
    #                             file.write("")  # 파일 내용을 비움
    #                         print(f"Cleared content of file: {right_file_path}")
    #                     except Exception as e:
    #                         print(f"[Error] Unable to clear content of {right_file_path}: {e}")
    #
    #                     # NEW: Left_Recon.txt 내용 삭제
    #                 if os.path.exists(left_recon_path):
    #                     try:
    #                         with open(left_recon_path, 'w') as file:
    #                             file.write("")  # 파일 내용을 비움
    #                         print(f"Cleared content of file: {left_recon_path}")
    #                     except Exception as e:
    #                         print(f"[Error] Unable to clear content of {left_recon_path}: {e}")
    #
    #                     # NEW: Right_Recon.txt 내용 삭제
    #                 if os.path.exists(right_recon_path):
    #                     try:
    #                         with open(right_recon_path, 'w') as file:
    #                             file.write("")  # 파일 내용을 비움
    #                         print(f"Cleared content of file: {right_recon_path}")
    #                     except Exception as e:
    #                         print(f"[Error] Unable to clear content of {right_recon_path}: {e}")
    #
    #                 idx_examine += 1
    #                 cnt_timeOut = 0
    #
    #         elif idx_examine == 16:
    #             if self.clsInfo['3rd_shot'] == True:
    #                 self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': '3rd left barcode'})
    #                 idx_examine += 1
    #                 cnt_timeOut = 0
    #
    #         elif idx_examine == 17:
    #             # 3rd barcode 수신 이후, Left만 검사
    #             if self.clsInfo['3rd_left_barcode'] == True:
    #                 settings_path = os.path.join(self.baseDir, "models",
    #                                              self.p_mainWindow.cb_customerName.currentText(),
    #                                              self.p_mainWindow.cb_selectedModel.currentText(), "settings.xml")
    #                 barcode_length = None
    #                 try:
    #                     tree = ET.parse(settings_path)
    #                     root = tree.getroot()
    #                     barcode_tag = root.find(".//barcode")
    #                     if barcode_tag is not None and barcode_tag.attrib.get('length') is not None:
    #                         barcode_length = int(barcode_tag.attrib['length'])
    #                 except Exception as e:
    #                     print(f"settings.xml barcode 길이 파싱 실패: {e}")
    #
    #                 # 검사 함수 (기존 15 단계의 검사 로직을 좌측만 대상으로 사용)
    #                 def check_barcode_length(modules_dict, barcode_length):
    #                     for v in modules_dict.values():
    #                         # 1. Barcode 길이 에러: 첫번째 값이 True일 때만 체크
    #                         if v[0] is True and v[5] is not None and isinstance(v[5], str):
    #                             # 'Scan failed' case는 아래에서 따로 처리
    #                             if v[2] != 'Scan Failed' and barcode_length is not None and len(v[5]) != barcode_length:
    #                                 return 'Barcode reading error'
    #                         # 2. 'Scan failed' 에러: 첫번째 값이 True일 때만 체크
    #                         if v[0] is True and v[5] == 'Scan Failed':
    #                             return 'Scan Failed'
    #                     return None
    #
    #                 # Left만 검사
    #                 left_result = check_barcode_length(self.p_mainWindow.modules_Left, barcode_length)
    #                 print(f'do_test (3rd, Left only at idx 15) : modules_Left : {self.p_mainWindow.modules_Left}')
    #
    #                 if left_result == 'Barcode reading error':
    #                     reasonOfFail = 'Barcode reading error'
    #                     finalResult = 'Fail'
    #                     idx_examine = 100
    #                 elif left_result == 'Scan Failed':
    #                     reasonOfFail = 'Scan Failed'
    #                     finalResult = 'Fail'
    #                     idx_examine = 100
    #                 else:
    #                     # Left OK → 다음 단계에서 Right 검사
    #                     idx_examine += 1
    #                     cnt_timeOut = 0
    #
    #                     # 필요 시 Left OK 신호를 별도로 보낼 수 있음
    #                     self.signalMessage.emit(self.objectName(), 'job',
    #                                             {'where': 'do_test', 'msg': '3rd_left_barcode_OK'})
    #
    #         elif idx_examine == 18:
    #             # 3rd barcode 수신 이후, Right만 검사
    #             if self.clsInfo['3rd_right_barcode'] == True:
    #                 settings_path = os.path.join(self.baseDir, "models",
    #                                              self.p_mainWindow.cb_customerName.currentText(),
    #                                              self.p_mainWindow.cb_selectedModel.currentText(), "settings.xml")
    #                 barcode_length = None
    #                 try:
    #                     tree = ET.parse(settings_path)
    #                     root = tree.getroot()
    #                     barcode_tag = root.find(".//barcode")
    #                     if barcode_tag is not None and barcode_tag.attrib.get('length') is not None:
    #                         barcode_length = int(barcode_tag.attrib['length'])
    #                 except Exception as e:
    #                     print(f"settings.xml barcode 길이 파싱 실패: {e}")
    #
    #                 # 검사 함수 (동일 로직을 Right 대상으로 사용)
    #                 def check_barcode_length(modules_dict, barcode_length):
    #                     for v in modules_dict.values():
    #                         # 1. Barcode 길이 에러: 첫번째 값이 True일 때만 체크
    #                         if v[0] is True and v[5] is not None and isinstance(v[5], str):
    #                             # 'Scan failed' case는 아래에서 따로 처리
    #                             if v[2] != 'Scan Failed' and barcode_length is not None and len(v[5]) != barcode_length:
    #                                 return 'Barcode reading error'
    #                         # 2. 'Scan failed' 에러: 첫번째 값이 True일 때만 체크
    #                         if v[0] is True and v[5] == 'Scan Failed':
    #                             return 'Scan Failed'
    #                     return None
    #
    #                 # Right만 검사
    #                 right_result = check_barcode_length(self.p_mainWindow.modules_Right, barcode_length)
    #                 print(f'do_test (3rd, Right only at idx 16) : modules_Right : {self.p_mainWindow.modules_Right}')
    #
    #                 if right_result == 'Barcode reading error':
    #                     reasonOfFail = 'Barcode reading error'
    #                     finalResult = 'Fail'
    #                     idx_examine = 100
    #                 elif right_result == 'Scan Failed':
    #                     reasonOfFail = 'Scan Failed'
    #                     finalResult = 'Fail'
    #                     idx_examine = 100
    #                 else:
    #                     # Right OK → 3rd 전체 OK 처리 후 종료 단계로
    #                     self.signalMessage.emit(self.objectName(), 'job',
    #                                             {'here': 'do_test', 'msg': 'examine finished'})
    #                     idx_examine += 1
    #                     cnt_timeOut = 0
    #
    #         elif idx_examine == 19:
    #             print('this message only should be shown when all OK')
    #             idx_examine = 100
    #
    #         elif idx_examine == 100:
    #             self.signalMessage.emit(self.objectName(), 'job',
    #                                     {'where': 'do_test', 'finalResult': finalResult, 'reasonOfFail': reasonOfFail})
    #             self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': 'STStop'})
    #
    #             try:
    #                 io_socket = get_io_socket()
    #                 if io_socket:
    #                     self.ioBoard.send_data(client_socket=io_socket, data='ManualPusherInitial')
    #                 else:
    #                     print("[do_test] Warning: IO Board not connected.")
    #             except Exception as e:
    #                 print(f"[do_test] Error while sending 'ManualPusherInitial': {e}")
    #
    #             self.clsInfo['is_examine'] = False
    #             self._test_thread = None
    #             print('idx_examine = 100. test finished')
    #
    #             # [ADD] 종료 시 바코드 중단 플래그 리셋
    #             self.clsInfo['barcode_stop'] = False
    #
    #             self._barcode_read_requested = False
    #             self.clsInfo['1st_image_scan'] = False
    #             self.clsInfo['1st_left_barcode'] = False
    #             self.clsInfo['1st_right_barcode'] = False
    #             self.clsInfo['sensor data'] = False
    #             self.clsInfo['2nd_barcode'] = False
    #             self.clsInfo['3rd_shot'] = False
    #             self.clsInfo['3rd_left_barcode'] = False
    #             self.clsInfo['3rd_right_barcode'] = False
    #             self.clsInfo['c_save'] = False
    #             self.clsInfo['sensor_dic update'] = False
    #             self.clsInfo['sensor_data1'] = False
    #             self.clsInfo['sensor_data2'] = False
    #             self.clsInfo['sensor_data3'] = False
    #             self.clsInfo['sensor_data4'] = False
    #             self.clsInfo['barcode_data1'] = False
    #             self.clsInfo['barcode_data2'] = False
    #             self.clsInfo['barcode_data3'] = False
    #             self.clsInfo['barcode_data4'] = False
    #             self.clsInfo['2nd show update'] = False
    #             self.clsInfo['3rd_shot'] = False
    #             self.clsInfo['1st_image_scan'] = False
    #             self.clsInfo['pusher back'] = False
    #             self.clsInfo['Scan Stop'] = False
    #             self.clsInfo['pusher_down_started'] = False
    #             self.clsInfo['pusher_down_finished'] = False
    #             self.clsInfo['button_unpushed'] = False
    #             self.clsInfo['pusher_down_ts'] = None
    #             self.clsInfo['button_unpushed_ts'] = None
    #             self.clsInfo['pusher_sequence_decided'] = False
    #             self.clsInfo['early_button_unpushed'] = False
    #
    #             for v in self.job_modules_Left.values():
    #                 v[0] = False
    #                 v[2] = None
    #                 v[3] = None
    #                 # v[4] = None
    #                 # v[5] = None
    #                 # v[6] = None
    #             for v in self.job_modules_Right.values():
    #                 v[0] = False
    #                 v[2] = None
    #                 v[3] = None
    #                 # v[4] = None
    #                 # v[5] = None
    #                 # v[6] = None
    #             return
    #
    #         # Abort Test
    #         if self.clsInfo['is_abortTest']:
    #             finalResult = 'Fail'
    #             reasonOfFail = 'Abort Test'
    #             idx_examine = 100
    #
    #         # Test TimeOut
    #         cnt_timeOut += 1
    #         if cnt_timeOut > (TIME_OUT_SEC / TIME_INTERVAL):
    #             finalResult = 'Fail'
    #             reasonOfFail = 'Timeout'
    #             idx_examine = 100
    #
    #         time.sleep(TIME_INTERVAL)

    def send_barcodes_to_clients(self):
        """
        p_mainWindow.modules_Left/Right 자료를 파싱해 각 writecard(1~4)별로
        바코드 정보를 '한 번에' 딕셔너리로 전송한다.

        변경 사항(UDP):
        - 연결/소켓 캐시 확인 없이 sysInfo.xml 기반 주소 해석 함수(_get_bank_addr_from_config)를 사용해
          각 Bank(1~4)의 (ip, port)로 직접 UDP 전송한다.
        """
        # writecard → Bank 번호 매핑
        writecard_bank_no = {
            'writecard1': 1,
            'writecard2': 2,
            'writecard3': 3,
            'writecard4': 4,
        }

        # Left/Right에서 수집한 항목을 writecard 단위로 합치기
        combined_by_writecard: dict[str, list[tuple[int, object]]] = {}

        def collect_from_modules(modules_dict):
            for module_key, v in modules_dict.items():
                try:
                    # v: [ready_flag, writecardN, <여기(2)에 값>, ...]
                    if not isinstance(v, (list, tuple)) or len(v) < 3:
                        continue
                    writecard = v[1]
                    val_3rd = v[2]  # 3번째 값(index 2) 전송 요구사항
                    # 원본 모듈 번호를 파싱해서 정렬 기준으로 사용
                    mk_num = 999999
                    try:
                        mk_str = str(module_key)
                        if mk_str.startswith('Module'):
                            mk_num = int(mk_str.replace('Module', '').strip())
                    except Exception:
                        mk_num = 999999
                    combined_by_writecard.setdefault(writecard, []).append((mk_num, val_3rd))
                except Exception:
                    # 안전장치: 한 모듈에서 문제가 생겨도 전체 전송은 계속
                    continue

        # Left/Right 모두 수집
        try:
            collect_from_modules(self.p_mainWindow.modules_Left)
        except Exception:
            pass
        try:
            collect_from_modules(self.p_mainWindow.modules_Right)
        except Exception:
            pass

        # 각 writecard 그룹에 대해 한 번씩 딕셔너리 전송
        for writecard, items in combined_by_writecard.items():
            bank_no = writecard_bank_no.get(writecard)
            if not bank_no:
                continue

            # sysInfo.xml 기반으로 Bank 주소 해석
            addr = self._get_bank_addr_from_config(bank_no)
            if not addr:
                self.signalMessage.emit(self.objectName(), 'ui', {
                    'msg': f"[Warning] Bank{bank_no} address not found. Skip barcode send."
                })
                continue

            # 원본 모듈 번호 기준 정렬 후, Bank마다 'Module1'부터 재번호
            items.sort(key=lambda x: x[0])
            payload = {}
            for idx, (_orig_mk_num, val) in enumerate(items, 1):
                payload[f"Module{idx}"] = val

            # 요구 형식: "barcode_info: { ... }"
            msg = f"barcode_info: {payload}"

            # UDP 전송
            ok = self.writeCard.send_data(client_socket=addr, data=msg)
            if ok:
                print(f"[send_barcodes_to_clients] Sent to Bank{bank_no} ({writecard}) @ {addr}: {msg}")
                self.signalMessage.emit(self.objectName(), 'ui', {
                    'msg': f"Sent barcode_info to Bank{bank_no} ({addr[0]}:{addr[1]})"
                })
            else:
                print(f"[send_barcodes_to_clients] Failed to send to Bank{bank_no} ({writecard}) @ {addr}")
                self.signalMessage.emit(self.objectName(), 'ui', {
                    'msg': f"[Error] Failed to send barcode_info to Bank{bank_no} ({addr[0]}:{addr[1]})"
                })


    # def send_barcodes_to_clients(self):
    #     """
    #     p_mainWindow.modules_Left/Right 자료를 파싱해 각 writecard(1~4)별로
    #     바코드 정보를 '한 번에' 딕셔너리로 전송한다.
    #
    #     변경 사항:
    #     - Bank(=writecard)마다 전송 키는 항상 'Module1'부터 시작하여 재번호 부여한다.
    #       (원본 모듈 키가 Module6~10이어도, Bank2로 보내는 데이터는 'Module1'부터 시작)
    #     - value는 각 모듈 value 리스트의 3번째 값(index 2)을 사용한다.
    #     - 메시지 형식: "barcode_info: { 'Module1': <v[2]>, 'Module2': <v[2]>, ... }"
    #     - 클라이언트 매핑은 기존과 동일:
    #         writecard1 -> Bank1
    #         writecard2 -> Bank2
    #         writecard3 -> Bank3
    #         writecard4 -> Bank4
    #     """
    #     # 기존과 동일한 writecard -> Bank 매핑
    #     writecard_bank_map = {
    #         'writecard1': 'Bank1',
    #         'writecard2': 'Bank2',
    #         'writecard3': 'Bank3',
    #         'writecard4': 'Bank4',
    #     }
    #
    #     # 연결 소켓 조회
    #     clients_info = util_base.get_xml_info(self.baseDir, 'clients')
    #     sockets = self.writeCard.get_connected_sockets(clients_info)  # { 'Bank1': socket, ... }
    #
    #     # Left/Right에서 수집한 항목을 writecard 단위로 합치기
    #     combined_by_writecard: dict[str, list[tuple[int, object]]] = {}
    #
    #     def collect_from_modules(modules_dict):
    #         for module_key, v in modules_dict.items():
    #             try:
    #                 # v: [ready_flag, writecardN, <여기(2)에 값>, ...]
    #                 if not isinstance(v, (list, tuple)) or len(v) < 3:
    #                     continue
    #                 writecard = v[1]
    #                 val_3rd = v[2]  # 3번째 값(index 2) 전송 요구사항
    #                 # 원본 모듈 번호를 파싱해서 정렬 기준으로 사용
    #                 mk_num = 999999
    #                 try:
    #                     mk_str = str(module_key)
    #                     if mk_str.startswith('Module'):
    #                         mk_num = int(mk_str.replace('Module', '').strip())
    #                 except Exception:
    #                     mk_num = 999999
    #                 combined_by_writecard.setdefault(writecard, []).append((mk_num, val_3rd))
    #             except Exception:
    #                 # 안전장치: 한 모듈에서 문제가 생겨도 전체 전송은 계속
    #                 continue
    #
    #     # Left/Right 모두 수집
    #     try:
    #         collect_from_modules(self.p_mainWindow.modules_Left)
    #     except Exception:
    #         pass
    #     try:
    #         collect_from_modules(self.p_mainWindow.modules_Right)
    #     except Exception:
    #         pass
    #
    #     # 각 writecard 그룹에 대해 한 번씩 딕셔너리 전송
    #     for writecard, items in combined_by_writecard.items():
    #         bank = writecard_bank_map.get(writecard)
    #         if not bank:
    #             continue
    #         client_socket = sockets.get(bank)
    #         if not client_socket:
    #             continue
    #
    #         # 원본 모듈 번호 기준 정렬 후, Bank마다 'Module1'부터 재번호
    #         items.sort(key=lambda x: x[0])
    #         payload = {}
    #         for idx, (_orig_mk_num, val) in enumerate(items, 1):
    #             payload[f"Module{idx}"] = val
    #
    #         # 요구 형식: "barcode_info: { ... }"
    #         msg = f"barcode_info: {payload}"
    #
    #         # 한 writecard(=한 Bank)에 대해 한 번만 전송
    #         self.writeCard.send_data(client_socket, msg)
    #         print(f'Send barcode: {client_socket}, {msg}')
    #
    #         # 디버그 로그
    #         try:
    #             print(f"[send_barcodes_to_clients] Sent to {bank} ({writecard}): {msg}")
    #         except Exception:
    #             pass

    def send_signal_to_clients(self, msg=None):
        """
        Bank1~4에 동일한 신호(msg)를 전송.
        - UDP: _get_bank_addr_from_config(bank_no)로 각 Bank의 (ip, port)를 구해 직접 전송
        """
        if msg is None:
            return

        for bank_no in (1, 2, 3, 4):
            addr = self._get_bank_addr_from_config(bank_no)
            if not addr:
                self.signalMessage.emit(self.objectName(), 'ui',
                                        {'msg': f"[Warning] Bank{bank_no} address not found. Could not send '{msg}'."})
                continue

            ok = self.writeCard.send_data(client_socket=addr, data=msg)
            if ok:
                print(f"[send_signal_to_clients] Sent '{msg}' to Bank{bank_no} @ {addr}")
            else:
                self.signalMessage.emit(self.objectName(), 'ui',
                                        {
                                            'msg': f"[Error] Failed to send '{msg}' to Bank{bank_no} ({addr[0]}:{addr[1]})"})



    # def send_signal_to_clients(self, msg=None):
    #     bank_names = ['Bank1', 'Bank2', 'Bank3', 'Bank4']
    #     clients_info = util_base.get_xml_info(self.baseDir, 'clients')
    #     sockets = self.writeCard.get_connected_sockets(clients_info)  # {Bank1: socket, ...}
    #
    #     for bank in bank_names:
    #         client_socket = sockets.get(bank)
    #         if client_socket:
    #             self.writeCard.send_data(client_socket, msg)
    #             print(f"sent '{msg}' to {bank}")

    def _get_bank_addr_from_config(self, bank_number: int):
        """
        sysInfo.xml에서 지정한 Bank(1~5)의 ip/port를 읽어 ('ip', port) 형태로 반환.
        - udpPort/port가 없으면 서버의 portNumber를 최후의 수단으로 폴백(경고 메시지 출력).
        - 어떤 경우에도 포트를 특정할 수 없으면 None.
        """
        try:
            sysinfo_path = os.path.join(self.baseDir, 'sysInfo.xml')
            tree = ET.parse(sysinfo_path)
            root = tree.getroot()
            clients = root.find('clients')
            if clients is None:
                return None

            ip, port = None, None
            for bank in clients.findall('bank'):
                if str(bank.get('number')) == str(bank_number):
                    ip = bank.get('ip')
                    port_str = bank.get('udpPort') or bank.get('port') or bank.get('udp_port')
                    if port_str and str(port_str).isdigit():
                        port = int(port_str)
                    break

            if not ip:
                return None

            # 포트가 없으면 서버 포트로 폴백(주의: 대상 장치가 해당 포트에서 수신 중이어야 유효)
            if port is None:
                try:
                    server_info = util_base.get_xml_info(self.baseDir, 'server')
                    fallback_port = int(server_info.get('portNumber')) if server_info and server_info.get(
                        'portNumber') else None
                except Exception:
                    fallback_port = None

                if fallback_port is not None:
                    # 경고: 폴백 사용
                    try:
                        self.signalMessage.emit(self.objectName(), 'ui',
                                                {
                                                    'msg': f"[Warning] Bank{bank_number} udpPort/port 미설정 → 서버 포트({fallback_port})로 폴백 전송 시도"})
                    except Exception:
                        pass
                    return (ip, fallback_port)
                else:
                    return None

            return (ip, port)

        except Exception as e:
            print(f"[JobControl] Failed to read Bank{bank_number} from sysInfo.xml: {e}")
            return None

    def send_manual_to_bank5(self, msg: str):
        """
        Manual handling UI에서 전달된 msg 값을 Bank5(IO Board)로 전송.
        - 1순위: ioBoard가 이미 서버로 패킷을 보냈다면, 그 주소 캐시로 전송(등록 방식).
        - 2순위: sysInfo.xml의 Bank5 ip/port(없으면 서버 포트 폴백)로 전송.
        - 실제 송신은 self.writeCard.send_data(UDP 소켓 보유 주체)를 사용.
        """
        try:
            # 1) 등록 주소(수신 캐시) 우선
            addr = None
            try:
                sockets = self.ioBoard.get_connected_sockets()
                addr = sockets.get('Bank5')
            except Exception:
                addr = None

            # 2) 캐시가 없으면 구성 파일에서 주소 확보(필요 시 폴백 포함)
            if not addr:
                addr = self._get_bank_addr_from_config(5)

            if not addr:
                self.signalMessage.emit(self.objectName(), 'ui',
                                        {
                                            'msg': f"[Warning] IO Board (Bank5) address not found. Could not send '{msg}'."})
                return

            ok = self.writeCard.send_data(client_socket=addr, data=msg)
            if ok:
                self.signalMessage.emit(self.objectName(), 'ui',
                                        {'msg': f"Sent manual command '{msg}' to IO Board (Bank5) via UDP"})
            else:
                self.signalMessage.emit(self.objectName(), 'ui',
                                        {'msg': f"[Error] Failed to send '{msg}' to IO Board (Bank5) via UDP"})

        except Exception as e:
            self.signalMessage.emit(self.objectName(), 'ui',
                                    {'msg': f"[Error] Failed to send manual command '{msg}' to IO Board: {e}"})

    # def send_manual_to_bank5(self, msg: str):
    #     """
    #     Manual handling UI에서 전달된 msg 값을 Bank5(IO Board)로 전송.
    #     - UDP에서는 '연결' 개념 없이 설정된 목적지로 바로 송신 가능.
    #     - 우선 캐시된 주소(ioBoard.get_connected_sockets())를 사용, 없으면 sysInfo.xml의 Bank5 주소로 송신.
    #     - 실제 송신은 UDP 서버(self.writeCard.send_data)를 사용(소켓 보유 주체).
    #     """
    #     try:
    #         # 1) 캐시 주소 시도(있으면 최우선)
    #         addr = None
    #         try:
    #             sockets = self.ioBoard.get_connected_sockets()
    #             addr = sockets.get('Bank5')
    #         except Exception:
    #             addr = None
    #
    #         # 2) 캐시가 없으면 sysInfo.xml의 Bank5 주소 사용
    #         if not addr:
    #             addr = self._get_bank5_addr_from_config()
    #
    #         if not addr:
    #             # 목적지 해석 불가
    #             self.signalMessage.emit(self.objectName(), 'ui',
    #                                     {
    #                                         'msg': f"[Warning] IO Board (Bank5) address not found. Could not send '{msg}'."})
    #             return
    #
    #         # 3) UDP 송신: IOBoardServer는 소켓 참조가 없을 수 있으므로, 실제 UDP 소켓을 가진 writeCard 쪽으로 전송
    #         ok = self.writeCard.send_data(client_socket=addr, data=msg)
    #         if ok:
    #             self.signalMessage.emit(self.objectName(), 'ui',
    #                                     {'msg': f"Sent manual command '{msg}' to IO Board (Bank5) via UDP"})
    #         else:
    #             self.signalMessage.emit(self.objectName(), 'ui',
    #                                     {'msg': f"[Error] Failed to send '{msg}' to IO Board (Bank5) via UDP"})
    #
    #     except Exception as e:
    #         self.signalMessage.emit(self.objectName(), 'ui',
    #                                 {'msg': f"[Error] Failed to send manual command '{msg}' to IO Board: {e}"})




    # def send_manual_to_bank5(self, msg: str):
    #     """
    #     Manual handling UI에서 전달된 msg 값을 Bank5(IO Board)로 그대로 전송한다.
    #     - msg 예: 'ManualPusherDown', 'ManualPusherUp', 'ManualPusherFront', 'ManualPusherBack'
    #     - 연결이 없으면 UI에 경고 메시지 출력
    #     """
    #     try:
    #         io_socket = self._get_io_socket_cached_or_lookup()
    #         if io_socket:
    #             self.ioBoard.send_data(client_socket=io_socket, data=msg)
    #             self.signalMessage.emit(self.objectName(), 'ui',
    #                                     {'msg': f"Sent manual command '{msg}' to IO Board (Bank5)"})
    #         else:
    #             self.signalMessage.emit(self.objectName(), 'ui',
    #                                     {
    #                                         'msg': f"[Warning] IO Board (Bank5) is not connected. Could not send '{msg}'."})
    #     except Exception as e:
    #         self.signalMessage.emit(self.objectName(), 'ui',
    #                                 {'msg': f"[Error] Failed to send manual command '{msg}' to IO Board: {e}"})

    def update_sensorID_from_client(self, Writecard_num, sensor_ID):
        """
        클라이언트(writecardN)에서 수신한 sensor_ID(dict)를 job_modules_Left/Right에 반영한다.

        - settings.xml의 <carrier columns="...">가 1이면:
          모든 writecard(1~4)의 데이터를 job_modules_Left에 순서대로 채운다.
          (make_dictionary에서 writecard1→2→3→4 순서대로 job_modules_Left에 섹션이 이미 구성되어 있음)

        - columns가 2 이상이면(기본):
          Left ← writecard1,2 / Right ← writecard3,4 에 매핑한다.

        - 클라이언트가 보내는 딕셔너리의 키는 'ModuleN' 또는 'sensorN' 형태 모두 지원.
          키의 숫자 오름차순으로 정렬하여 해당 writecard 섹션의 모듈에 순서대로 매핑한다.

        - 매핑 위치:
          각 모듈 value 리스트의 세 번째 인덱스(2)에 센서(FAKEID) 문자열 저장.

        - 각 카드 데이터 도착 시 self.clsInfo['sensor_dataN'] flag를 True로 설정.
        """
        # 1) carrier columns 확인
        try:
            carrier_cols = self._get_carrier_columns()
        except Exception:
            carrier_cols = 2

        # 2) 대상 dict, card-key, 플래그 결정
        if Writecard_num == 'Write Card 1':
            target_dict = self.job_modules_Left if carrier_cols == 1 else self.job_modules_Left
            card_key = 'writecard1'
            flag_key = 'sensor_data1'
        elif Writecard_num == 'Write Card 2':
            target_dict = self.job_modules_Left if carrier_cols == 1 else self.job_modules_Left
            card_key = 'writecard2'
            flag_key = 'sensor_data2'
        elif Writecard_num == 'Write Card 3':
            target_dict = self.job_modules_Left if carrier_cols == 1 else self.job_modules_Right
            card_key = 'writecard3'
            flag_key = 'sensor_data3'
        elif Writecard_num == 'Write Card 4':
            target_dict = self.job_modules_Left if carrier_cols == 1 else self.job_modules_Right
            card_key = 'writecard4'
            flag_key = 'sensor_data4'
        else:
            raise ValueError(f"알 수 없는 Writecard_num: {Writecard_num}")

        # 3) 해당 writecard 섹션의 모듈 키만 추출 후 Module 번호 기준 정렬
        module_keys = [k for k, v in target_dict.items() if len(v) > 1 and v[1] == card_key]
        try:
            module_keys.sort(key=lambda mk: int(mk.replace('Module', '')) if mk.startswith('Module') else 9999)
        except Exception:
            # 문제가 생겨도 순서 유지
            pass

        # 4) 수신 데이터 정렬: 'ModuleN' 또는 'sensorN' 키 모두 지원
        def _index_from_key(s: str) -> int:
            try:
                if s.startswith('Module'):
                    suf = s[6:]
                    return int(suf) if suf.isdigit() else 9999
                if s.lower().startswith('sensor'):
                    suf = s[len('sensor'):]
                    return int(suf) if suf.isdigit() else 9999
            except Exception:
                pass
            return 9999

        try:
            sorted_sensor_items = sorted(sensor_ID.items(), key=lambda kv: _index_from_key(str(kv[0])))
        except Exception:
            # 정렬 실패 시, 기존 순서 그대로 사용(OrderedDict이면 입력 순서 보존)
            sorted_sensor_items = list(sensor_ID.items())

        # 5) 매칭 저장: writecard 섹션(module_keys) ↔ 수신 항목(sorted_sensor_items) 순서대로
        for module_key, (recv_key, sensor_val) in zip(module_keys, sorted_sensor_items):
            target_dict[module_key][2] = sensor_val  # 세 번째 값에 저장

        # 6) 플래그 설정
        self.clsInfo[flag_key] = True

        # 7) 디버그 출력
        print(f'self.job_modules_Left: {self.job_modules_Left}')
        print(f'self.job_modules_Right: {self.job_modules_Right}')
        print(f"self.clsInfo flags: {[self.clsInfo.get(f'sensor_data{i}') for i in range(1, 5)]}")


    def update_barcode_from_client(self, Writecard_num, barcode_info):
        """
        클라이언트(writecardN)에서 수신한 barcode_info를 job_modules_Left/Right에 반영한다.

        - settings.xml의 <carrier columns="...">가 1이면:
          모든 writecard(1~4)의 데이터를 job_modules_Left에 순서대로 채운다.
          (make_dictionary에서 writecard1→2→3→4 순서대로 job_modules_Left에 섹션이 이미 구성되어 있음)

        - columns가 2 이상이면(기본):
          Left ← writecard1,2 / Right ← writecard3,4 에 매핑한다.

        - 클라이언트가 보내는 딕셔너리의 키는 'ModuleN' 또는 'barcodeN' 형태 모두 지원.
          키의 숫자 오름차순으로 정렬하여 해당 writecard 섹션의 모듈에 순서대로 매핑한다.

        - 매핑 위치:
          각 모듈 value 리스트의 네 번째 인덱스(3)에 바코드 문자열 저장.

        - 각 카드 데이터 도착 시 self.clsInfo['barcode_dataN'] flag를 True로 설정.
        """
        # 1) carrier columns 확인
        try:
            carrier_cols = self._get_carrier_columns()
        except Exception:
            carrier_cols = 2

        # 2) 대상 dict, card-key, 플래그 결정
        if Writecard_num == 'Write Card 1':
            target_dict = self.job_modules_Left if carrier_cols == 1 else self.job_modules_Left
            card_key = 'writecard1'
            flag_key = 'barcode_data1'
        elif Writecard_num == 'Write Card 2':
            target_dict = self.job_modules_Left if carrier_cols == 1 else self.job_modules_Left
            card_key = 'writecard2'
            flag_key = 'barcode_data2'
        elif Writecard_num == 'Write Card 3':
            target_dict = self.job_modules_Left if carrier_cols == 1 else self.job_modules_Right
            card_key = 'writecard3'
            flag_key = 'barcode_data3'
        elif Writecard_num == 'Write Card 4':
            target_dict = self.job_modules_Left if carrier_cols == 1 else self.job_modules_Right
            card_key = 'writecard4'
            flag_key = 'barcode_data4'
        else:
            raise ValueError(f"알 수 없는 Writecard_num: {Writecard_num}")

        # 3) 해당 writecard 섹션의 모듈 키만 추출 후 Module 번호 기준 정렬
        module_keys = [k for k, v in target_dict.items() if len(v) > 1 and v[1] == card_key]
        try:
            module_keys.sort(key=lambda mk: int(mk.replace('Module', '')) if mk.startswith('Module') else 9999)
        except Exception:
            # 문제가 생겨도 순서 유지
            pass

        # 4) 수신 데이터 정렬: 'ModuleN' 또는 'barcodeN' 키 모두 지원
        def _index_from_key(s: str) -> int:
            try:
                if s.startswith('Module'):
                    suf = s[6:]
                    return int(suf) if suf.isdigit() else 9999
                if s.startswith('barcode'):
                    suf = s[7:]
                    return int(suf) if suf.isdigit() else 9999
            except Exception:
                pass
            return 9999

        try:
            sorted_barcode_items = sorted(barcode_info.items(), key=lambda kv: _index_from_key(str(kv[0])))
        except Exception:
            # 정렬 실패 시, 기존 순서 그대로 사용(OrderedDict이면 입력 순서 보존)
            sorted_barcode_items = list(barcode_info.items())

        # 5) 매칭 저장: writecard 섹션(module_keys) ↔ 수신 항목(sorted_barcode_items) 순서대로
        for module_key, (recv_key, barcode_val) in zip(module_keys, sorted_barcode_items):
            target_dict[module_key][3] = barcode_val  # 네 번째 값에 저장

        # 6) 플래그 설정
        self.clsInfo[flag_key] = True

        # 7) 디버그 출력
        print(f'self.job_modules_Left: {self.job_modules_Left}')
        print(f'self.job_modules_Right: {self.job_modules_Right}')
        print(f"self.clsInfo barcode flags: {[self.clsInfo.get(f'barcode_data{i}') for i in range(1, 5)]}")

    def reset_job_context(self, reason: str | None = None):
        """
        모델/고객사 변경 등으로 JobController 상태 초기화.
        IO 보드 초기화 전송을 UDP 주소 해석 → self.writeCard.send_data로 수행.
        """
        try:
            if getattr(self, "_test_thread", None) is not None and self._test_thread.is_alive():
                self.clsInfo["force_abort"] = True
                self.clsInfo["is_abortTest"] = True
                self.clsInfo["is_examine"] = False
                self.clsInfo["barcode_stop"] = True

                # [변경] 소켓 캐시 대신 주소 해석 후 직접 송신
                try:
                    addr = None
                    try:
                        sockets = self.ioBoard.get_connected_sockets()
                        addr = sockets.get('Bank5')
                    except Exception:
                        addr = None
                    if not addr:
                        addr = self._get_bank_addr_from_config(5)

                    if addr:
                        self.writeCard.send_data(client_socket=addr, data="ManualPusherInitial")
                except Exception:
                    pass

                self._test_thread.join(timeout=1.0)
        except Exception:
            pass
        finally:
            self._test_thread = None

        try:
            self.job_modules_Left.clear()
            self.job_modules_Right.clear()
        except Exception:
            self.job_modules_Left = {}
            self.job_modules_Right = {}

        self.left_ready_printed = False
        self.right_ready_printed = False
        self._barcode_read_requested = False
        self.io_socket = None  # IO 캐시 제거

        was_initialized = bool(self.clsInfo.get("is_initialized"))
        keys = list(self.clsInfo.keys())
        for k in keys:
            v = self.clsInfo.get(k)
            if k in ("pusher_down_ts", "button_unpushed_ts"):
                self.clsInfo[k] = None
            elif isinstance(v, bool):
                self.clsInfo[k] = False
            elif isinstance(v, (int, float)):
                self.clsInfo[k] = 0
            elif isinstance(v, dict):
                self.clsInfo[k] = {}
            else:
                self.clsInfo[k] = False

        force_false_keys = [
            "is_examine", "is_abortTest",
            "Writecard 1 Ready", "Writecard 2 Ready", "Writecard 3 Ready", "Writecard 4 Ready",
            "Left_Recon_Script", "Right_Recon_Script",
            "IOBoard_Ready",
            "1st_image_scan", "1st_left_barcode", "1st_right_barcode",
            "2nd_barcode",
            "3rd_shot", "3rd_left_barcode", "3rd_right_barcode",
            "sensor_data1", "sensor_data2", "sensor_data3", "sensor_data4",
            "barcode_data1", "barcode_data2", "barcode_data3", "barcode_data4",
            "pusher back", "pusher_down_started", "pusher_down_finished",
            "button_unpushed", "pusher_sequence_decided", "early_button_unpushed",
            "barcode_stop", "force_abort",
            "Scan Stop",
            "2nd show update",
            "sensor_dict update", "sensor_dic update",
        ]
        for k in force_false_keys:
            self.clsInfo[k] = False

        self.clsInfo["writeCard_states"] = {}
        self.clsInfo["pusher_down_ts"] = None
        self.clsInfo["button_unpushed_ts"] = None

        if was_initialized:
            self.clsInfo["is_initialized"] = True

        try:
            if self.p_mainWindow:
                self.settings_xml_info = os.path.join(
                    self.baseDir, "models",
                    self.p_mainWindow.cb_customerName.currentText(),
                    self.p_mainWindow.cb_selectedModel.currentText(),
                    "settings.xml"
                )
        except Exception:
            self.settings_xml_info = None

        try:
            msg_txt = f"[JobControl] Reset job context ({reason})" if reason else "[JobControl] Reset job context"
            self.signalMessage.emit(self.objectName(), "ui", {"msg": msg_txt})
        except Exception:
            pass


    # def reset_job_context(self, reason: str | None = None):
    #     """
    #     모델/고객사 변경 등으로 JobController 상태 초기화.
    #     IO 보드 초기화 전송 경로를 self.ioBoard로 변경.
    #     """
    #     try:
    #         if getattr(self, "_test_thread", None) is not None and self._test_thread.is_alive():
    #             self.clsInfo["force_abort"] = True
    #             self.clsInfo["is_abortTest"] = True
    #             self.clsInfo["is_examine"] = False
    #             self.clsInfo["barcode_stop"] = True
    #
    #             try:
    #                 io_socket = self._get_io_socket_cached_or_lookup()
    #                 if io_socket:
    #                     self.ioBoard.send_data(client_socket=io_socket, data="ManualPusherInitial")
    #             except Exception:
    #                 pass
    #
    #             self._test_thread.join(timeout=1.0)
    #     except Exception:
    #         pass
    #     finally:
    #         self._test_thread = None
    #
    #     # job 모듈 딕셔너리 초기화
    #     try:
    #         self.job_modules_Left.clear()
    #         self.job_modules_Right.clear()
    #     except Exception:
    #         self.job_modules_Left = {}
    #         self.job_modules_Right = {}
    #
    #     self.left_ready_printed = False
    #     self.right_ready_printed = False
    #     self._barcode_read_requested = False
    #     self.io_socket = None  # IO 캐시 제거
    #
    #     was_initialized = bool(self.clsInfo.get("is_initialized"))
    #     keys = list(self.clsInfo.keys())
    #     for k in keys:
    #         v = self.clsInfo.get(k)
    #         if k in ("pusher_down_ts", "button_unpushed_ts"):
    #             self.clsInfo[k] = None
    #         elif isinstance(v, bool):
    #             self.clsInfo[k] = False
    #         elif isinstance(v, (int, float)):
    #             self.clsInfo[k] = 0
    #         elif isinstance(v, dict):
    #             self.clsInfo[k] = {}
    #         else:
    #             self.clsInfo[k] = False
    #
    #     force_false_keys = [
    #         "is_examine", "is_abortTest",
    #         "Writecard 1 Ready", "Writecard 2 Ready", "Writecard 3 Ready", "Writecard 4 Ready",
    #         "Left_Recon_Script", "Right_Recon_Script",
    #         "IOBoard_Ready",
    #         "1st_image_scan", "1st_left_barcode", "1st_right_barcode",
    #         "2nd_barcode",
    #         "3rd_shot", "3rd_left_barcode", "3rd_right_barcode",
    #         "sensor_data1", "sensor_data2", "sensor_data3", "sensor_data4",
    #         "barcode_data1", "barcode_data2", "barcode_data3", "barcode_data4",
    #         "pusher back", "pusher_down_started", "pusher_down_finished",
    #         "button_unpushed", "pusher_sequence_decided", "early_button_unpushed",
    #         "barcode_stop", "force_abort",
    #         "Scan Stop",
    #         "2nd show update",
    #         "sensor_dict update", "sensor_dic update",
    #     ]
    #     for k in force_false_keys:
    #         self.clsInfo[k] = False
    #
    #     self.clsInfo["writeCard_states"] = {}
    #     self.clsInfo["pusher_down_ts"] = None
    #     self.clsInfo["button_unpushed_ts"] = None
    #
    #     if was_initialized:
    #         self.clsInfo["is_initialized"] = True
    #
    #     try:
    #         if self.p_mainWindow:
    #             self.settings_xml_info = os.path.join(
    #                 self.baseDir, "models",
    #                 self.p_mainWindow.cb_customerName.currentText(),
    #                 self.p_mainWindow.cb_selectedModel.currentText(),
    #                 "settings.xml"
    #             )
    #     except Exception:
    #         self.settings_xml_info = None
    #
    #     try:
    #         msg_txt = f"[JobControl] Reset job context ({reason})" if reason else "[JobControl] Reset job context"
    #         self.signalMessage.emit(self.objectName(), "ui", {"msg": msg_txt})
    #     except Exception:
    #         pass
    #

    def UDPTest(self):
        """
        Send 'UDPTest' to Writecard1~4 (Bank1~4) via UDP.
        Uses sysInfo.xml configuration through send_signal_to_clients.
        """
        try:
            self.send_signal_to_clients('UDPTest')
            self.signalMessage.emit(self.objectName(), 'ui', {'msg': "Sent 'UDPTest' to Banks 1-4"})
        except Exception as e:
            try:
                self.signalMessage.emit(self.objectName(), 'ui', {'msg': f"[Error] UDPTest failed: {e}"})
            except Exception:
                pass




# import threading, re, traceback, os, time, ast
# import c_udp_server, util_base, c_udp_ioboard
# from PySide6 import QtCore
# import xml.etree.ElementTree as ET
# from typing import Dict, Iterable, Optional
#
# class JobController(QtCore.QObject):
#     EXIT_THREAD = False
#     signalMessage = QtCore.Signal(str, str, dict)
#
#     def __init__(self, baseDir, objName='', mainWindow=None):
#         QtCore.QObject.__init__(self)
#         self.logger = util_base.setuplogger(self.__class__.__name__)
#         self.setObjectName(objName)
#         self.p_mainWindow = mainWindow
#         self.baseDir = baseDir
#
#         try:
#             self.clients_info = util_base.get_xml_info(self.baseDir, 'clients') or []
#         except Exception:
#             self.clients_info = []
#         self.io_socket = None
#
#         self.clsInfo = {'is_examine': False, 'Writecard 1 Ready': False, 'Writecard 2 Ready': False,
#                         'Writecard 3 Ready': False, 'Writecard 4 Ready': False,
#                         'Left_Recon_Script': False, 'Right_Recon_Script': False,
#                         'IOBoard_Ready': False, 'is_abortTest': False,
#                         'writeCard_states': {}, 'left_capture_done': False, 'right_capture_done': False,
#                         '1st_image_scan': False,
#                         '1st_left_barcode': False, '1st_right_barcode': False, '2nd_barcode': False,
#                         '3rd_shot': False, '3rd_left_barcode': False, '3rd_right_barcode': False,
#                         'sensor_data1': False, 'sensor_data2': False, 'sensor_data3': False, 'sensor_data4': False,
#                         'barcode_data1': False, 'barcode_data2': False, 'barcode_data3': False, 'barcode_data4': False,
#                         'pusher back': False, 'c_save': False, 'sensor_dict update': False, '2nd show update': False,
#                         'barcode_stop': False, 'pusher_down_started': False, 'pusher_down_finished': False,
#                         'button_unpushed': False,
#                         'pusher_down_tes': None, 'button_unpushed_ts': None, 'pusher_sequence_decided': False,
#                         'early_button_unpushed': False,
#                         'force_abort': False}
#         self.job_modules_Left = {}
#         self.job_modules_Right = {}
#         self.left_ready_printed = False
#         self.right_ready_printed = False
#         self.settings_xml_info = os.path.join(self.baseDir, 'models', self.p_mainWindow.cb_customerName.currentText(),
#                                               self.p_mainWindow.cb_selectedModel.currentText(), 'settings.xml')
#         self._test_thread = None
#         self._barcode_read_requested = False
#
#         try:
#             # Write Card 1~4 서버
#             self.writeCard = c_udp_server.TCPServer(objName='writeCard', baseDir=self.baseDir,
#                                                     mainWindow=self.p_mainWindow)
#             self.writeCard.signalMessage.connect(self.slotParse, type=QtCore.Qt.ConnectionType.DirectConnection)
#
#             # IO Board 서버(attach 모드)
#             self.ioBoard = c_udp_ioboard.IOBoardServer(objName='ioBoard', baseDir=self.baseDir,
#                                                        mainWindow=self.p_mainWindow)
#             self.ioBoard.signalMessage.connect(self.slotParse, type=QtCore.Qt.ConnectionType.DirectConnection)
#             self.ioBoard.start()  # attach 모드여도 스레드를 살아있게 유지(옵션)
#
#             # writeCard 서버가 IO 보드 소켓을 위임할 핸들러 등록
#             self.writeCard.set_bank5_socket_handler(self.ioBoard.attach_socket)
#
#             self.writeCard.start()
#
#             self.clsInfo['is_initialized'] = True
#             self.logger.info(f'Initialization Successful > ObjectName: {self.objectName()}')
#
#         except OSError as e:
#             self.server = None
#             print(traceback.format_exc())
#             self.logger.error(f'Initialization Failed > ObjectName: {self.objectName()}, Detail:{e}')
#
#
#     @QtCore.Slot()
#     def slotManualTestStart(self):
#         """
#         UI의 'Manual Test' 버튼에서 호출되어 do_test 메인 루프를 시작한다.
#         - 이미 실행 중이면 무시
#         - 물리 버튼 이벤트와 무관하게 동작
#         """
#         # 이미 테스트 쓰레드가 돌고 있으면 무시
#         if self.clsInfo.get('is_examine') or (
#                 getattr(self, '_test_thread', None) is not None and self._test_thread.is_alive()
#         ):
#             print("테스트가 이미 실행중입니다. (ignore UI Manual Test)")
#             return
#
#         # 필요한 최소 초기화 (물리 버튼 관련 강제 중단 로직은 사용하지 않으므로 단순화)
#         self.clsInfo['sensorID'] = str()
#         self.clsInfo['is_examine'] = True
#         self.clsInfo['is_abortTest'] = False
#
#         self._test_thread = threading.Thread(target=self.do_test, daemon=True)
#         self._test_thread.start()
#         print("수동 테스트 시작됨 (UI 버튼 통해)")
#
#     @QtCore.Slot(str, str, dict)
#     def slotParse(self, objName, msgType, values):
#         try:
#             if 'msg' not in values:
#                 values['msg'] = "No msg provided"
#         except Exception as e:
#             self.logger.error(f"Error in slotParse: {str(e)}")
#
#         # 1) Write Card 1~4 처리
#         if objName.startswith('writeCard'):
#             try:
#                 if msgType == 'connection':
#                     self.signalMessage.emit(self.objectName(), msgType, values)
#
#                     where = str(values.get('where', '')).strip()
#                     msg = str(values.get('msg', '')).strip()
#
#                     # Writecard 연결 상태 반영
#                     for i in range(1, 5):
#                         if msg == f'Client connected: writecard {i}':
#                             self.clsInfo[f'Writecard {i} Ready'] = True
#                         elif msg == f'Client disconnected: writecard {i}':
#                             self.clsInfo[f'Writecard {i} Ready'] = False
#
#                     # Bank 번호 기반 해제 처리
#                     m = re.search(r'Bank\s*(\d+)', where) if where else None
#                     if m and 'Client socket closed' in msg:
#                         bank_no = int(m.group(1))
#                         if 1 <= bank_no <= 4:
#                             key = f'Writecard {bank_no} Ready'
#                             self.clsInfo[key] = False
#                 elif msgType == 'job':
#                     msg = values.get('msg', '')
#
#                     if msg.startswith("Script save"):
#                         self.update_job_module_status(values)
#
#                     elif msg.startswith("sensor_ID"):
#                         dict_str = values['msg'][len("sensor_ID: "):].strip()
#                         try:
#                             sensor_ID_dict = ast.literal_eval(dict_str)
#                         except Exception as e:
#                             print(f"Failed to parse sensor_ID: {e}")
#                             sensor_ID_dict = {}
#                         self.update_sensorID_from_client(Writecard_num=values['where'], sensor_ID=sensor_ID_dict)
#
#                     elif msg.startswith("barcode_info"):
#                         prefix = "barcode_info: OrderedDict("
#                         if values['msg'].startswith(prefix):
#                             dict_str = values['msg'][len(prefix):-1].strip()
#                         else:
#                             dict_str = values['msg'][len("barcode_info: "):].strip()
#                         try:
#                             barcode_info_dict = ast.literal_eval(dict_str)
#                         except Exception as e:
#                             print(f"Failed to parse barcode_info: {e}")
#                             barcode_info_dict = {}
#                         self.update_barcode_from_client(Writecard_num=values['where'], barcode_info=barcode_info_dict)
#
#                     elif msg == 'Scan Stop':
#                         # 카메라/클라이언트에서 오는 스캔 중단
#                         self.clsInfo['barcode_stop'] = True
#                         print('[slotParse] barcode_stop latched (Scan Stop)')
#
#             except Exception as e:
#                 self.logger.error(f"Error in slotParse: {str(e)}")
#             return
#
#         # 2) IO Board 처리
#         if objName.startswith('ioBoard'):
#             try:
#                 if msgType == 'connection':
#                     self.signalMessage.emit(self.objectName(), msgType, values)
#
#                     msg = str(values.get('msg', '')).strip()
#                     if msg == 'Client connected: IO Board':
#                         sockets = self.ioBoard.get_connected_sockets()
#                         self.io_socket = sockets.get('Bank5')
#                         self.clsInfo['IOBoard_Ready'] = True
#                         if self.io_socket:
#                             self.ioBoard.send_data(data='ManualPusherInitial')
#                     elif 'Client socket closed' in msg or msg == 'Client disconnected: IO Board':
#                         self.clsInfo['IOBoard_Ready'] = False
#                         self.io_socket = None
#                 elif msgType == 'job':
#                     msg = values.get('msg', '')
#
#                     if msg == 'Mapping start':
#                         # 테스트가 이미 진행 중이면 무시
#                         if self.clsInfo.get('is_examine') or (
#                                 getattr(self, '_test_thread', None) is not None and self._test_thread.is_alive()):
#                             print("테스트가 이미 실행중입니다. (ignore 'Mapping start')")
#                             return
#
#                         self.clsInfo['pusher_down_finished'] = False
#                         self.clsInfo['button_unpushed'] = False
#                         self.clsInfo['pusher_down_ts'] = None
#                         self.clsInfo['button_unpushed_ts'] = None
#                         self.clsInfo['pusher_sequence_decided'] = False
#                         self.clsInfo['early_button_unpushed'] = False
#                         self.clsInfo['force_abort'] = False
#                         self.clsInfo['pusher back'] = False
#
#                         if not self.clsInfo['is_examine']:
#                             self.clsInfo['sensorID'] = str()
#                             self.clsInfo['is_examine'] = True
#                             self.clsInfo['is_abortTest'] = False
#                             self.start_do_test_thread()
#                             # self._test_thread = threading.Thread(target=self.do_test, daemon=True)
#                             # self._test_thread.start()
#
#                     elif msg == 'Pusher down finished':
#                         if not self.clsInfo.get('pusher_down_finished'):
#                             self.clsInfo['pusher_down_finished'] = True
#                             self.clsInfo['pusher_down_ts'] = time.perf_counter()
#                             print('[slotParse] Pusher down finished (first)')
#                         self.clsInfo['early_button_unpushed'] = False
#                         self.clsInfo['force_abort'] = False
#
#                     elif msg == 'Pusher back finished':
#                         self.clsInfo['pusher back'] = True
#
#                     elif msg == 'Button unpushed':
#                         if not self.clsInfo.get('button_unpushed'):
#                             self.clsInfo['button_unpushed'] = True
#                             self.clsInfo['button_unpushed_ts'] = time.perf_counter()
#
#                         if not self.clsInfo.get('pusher_down_finished'):
#                             print('[slotParse] Button came before PusherDown → request abort')
#                             self.clsInfo['early_button_unpushed'] = True
#                             self.clsInfo['force_abort'] = True
#                             # [변경] UDP 주소 해석 → 직접 전송
#                             try:
#                                 addr = None
#                                 try:
#                                     sockets = self.ioBoard.get_connected_sockets()
#                                     addr = sockets.get('Bank5')
#                                 except Exception:
#                                     addr = None
#                                 if not addr:
#                                     addr = self._get_bank_addr_from_config(5)
#
#                                 if addr:
#                                     self.writeCard.send_data(client_socket=addr, data='Pusher back')
#                                     print("[slotParse] Sent 'Pusher back' (early button, UDP addr)")
#                                 else:
#                                     print("[slotParse] Bank5 address not found; cannot send 'Pusher back'")
#                             except Exception as e:
#                                 print(f"[slotParse] Failed to send 'Pusher back': {e}")
#
#                     elif msg == 'Scan Stop':
#                         self.clsInfo['barcode_stop'] = True
#                         print('[slotParse] barcode_stop latched (Scan Stop)')
#
#             except Exception as e:
#                 self.logger.error(f"Error in slotParse(ioBoard): {str(e)}")
#             return
#
#
#
#
#
#     # @QtCore.Slot(str, str, dict)
#     # def slotParse(self, objName, msgType, values):
#     #     try:
#     #         if 'msg' not in values:
#     #             values['msg'] = "No msg provided"
#     #     except Exception as e:
#     #         self.logger.error(f"Error in slotParse: {str(e)}")
#     #
#     #     # 1) Write Card 1~4 처리
#     #     if objName.startswith('writeCard'):
#     #         try:
#     #             # UDP 전환: 연결 이벤트 사용 안 함
#     #             if msgType == 'job':
#     #                 msg = values.get('msg', '')
#     #
#     #                 if msg.startswith("Script save"):
#     #                     self.update_job_module_status(values)
#     #
#     #                 elif msg.startswith("sensor_ID"):
#     #                     dict_str = values['msg'][len("sensor_ID: "):].strip()
#     #                     try:
#     #                         sensor_ID_dict = ast.literal_eval(dict_str)
#     #                     except Exception as e:
#     #                         print(f"Failed to parse sensor_ID: {e}")
#     #                         sensor_ID_dict = {}
#     #                     self.update_sensorID_from_client(Writecard_num=values['where'], sensor_ID=sensor_ID_dict)
#     #
#     #                 elif msg.startswith("barcode_info"):
#     #                     prefix = "barcode_info: OrderedDict("
#     #                     if values['msg'].startswith(prefix):
#     #                         dict_str = values['msg'][len(prefix):-1].strip()
#     #                     else:
#     #                         dict_str = values['msg'][len("barcode_info: "):].strip()
#     #                     try:
#     #                         barcode_info_dict = ast.literal_eval(dict_str)
#     #                     except Exception as e:
#     #                         print(f"Failed to parse barcode_info: {e}")
#     #                         barcode_info_dict = {}
#     #                     self.update_barcode_from_client(Writecard_num=values['where'], barcode_info=barcode_info_dict)
#     #
#     #                 elif msg == 'Scan Stop':
#     #                     # 카메라/클라이언트에서 오는 스캔 중단
#     #                     self.clsInfo['barcode_stop'] = True
#     #                     print('[slotParse] barcode_stop latched (Scan Stop)')
#     #
#     #         except Exception as e:
#     #             self.logger.error(f"Error in slotParse: {str(e)}")
#     #         return
#     #
#     #     # 2) IO Board 처리
#     #     if objName.startswith('ioBoard'):
#     #         try:
#     #             # UDP 전환: 연결 이벤트 사용 안 함
#     #             if msgType == 'job':
#     #                 msg = values.get('msg', '')
#     #
#     #                 if msg == 'Mapping start':
#     #                     # 테스트가 이미 진행 중이면 플래그를 건드리지 않고 무시
#     #                     if self.clsInfo.get('is_examine') or (
#     #                             getattr(self, '_test_thread', None) is not None and self._test_thread.is_alive()):
#     #                         print("테스트가 이미 실행중입니다. (ignore 'Mapping start')")
#     #                         return
#     #
#     #                     self.clsInfo['pusher_down_finished'] = False
#     #                     self.clsInfo['button_unpushed'] = False
#     #                     self.clsInfo['pusher_down_ts'] = None
#     #                     self.clsInfo['button_unpushed_ts'] = None
#     #                     self.clsInfo['pusher_sequence_decided'] = False
#     #                     self.clsInfo['early_button_unpushed'] = False
#     #                     self.clsInfo['force_abort'] = False
#     #                     self.clsInfo['pusher back'] = False
#     #
#     #                     if not self.clsInfo['is_examine']:
#     #                         self.clsInfo['sensorID'] = str()
#     #                         self.clsInfo['is_examine'] = True
#     #                         self.clsInfo['is_abortTest'] = False
#     #                         self._test_thread = threading.Thread(target=self.do_test, daemon=True)
#     #                         self._test_thread.start()
#     #
#     #                 elif msg == 'Pusher down finished':
#     #                     if not self.clsInfo.get('pusher_down_finished'):
#     #                         self.clsInfo['pusher_down_finished'] = True
#     #                         self.clsInfo['pusher_down_ts'] = time.perf_counter()
#     #                         print('[slotParse] Pusher down finished (first)')
#     #                     self.clsInfo['early_button_unpushed'] = False
#     #                     self.clsInfo['force_abort'] = False
#     #
#     #                 elif msg == 'Pusher back finished':
#     #                     self.clsInfo['pusher back'] = True
#     #
#     #                 elif msg == 'Button unpushed':
#     #                     if not self.clsInfo.get('button_unpushed'):
#     #                         self.clsInfo['button_unpushed'] = True
#     #                         self.clsInfo['button_unpushed_ts'] = time.perf_counter()
#     #
#     #                     if not self.clsInfo.get('pusher_down_finished'):
#     #                         print('[slotParse] Button came before PusherDown → request abort')
#     #                         self.clsInfo['early_button_unpushed'] = True
#     #                         self.clsInfo['force_abort'] = True
#     #                         try:
#     #                             if self.io_socket:
#     #                                 self.ioBoard.send_data(data='Pusher back')
#     #                                 print("[slotParse] Sent 'Pusher back' (early button, cached socket)")
#     #                             else:
#     #                                 sockets = self.ioBoard.get_connected_sockets()
#     #                                 io_socket = sockets.get('Bank5')
#     #                                 if io_socket:
#     #                                     self.ioBoard.send_data(client_socket=io_socket, data='Pusher back')
#     #                                     print("[slotParse] Sent 'Pusher back' (early button, fallback)")
#     #                         except Exception as e:
#     #                             print(f"[slotParse] Failed to send 'Pusher back': {e}")
#     #
#     #                 elif msg == 'Scan Stop':
#     #                     # 카메라/클라이언트에서 오는 스캔 중단
#     #                     self.clsInfo['barcode_stop'] = True
#     #                     print('[slotParse] barcode_stop latched (Scan Stop)')
#     #
#     #         except Exception as e:
#     #             self.logger.error(f"Error in slotParse(ioBoard): {str(e)}")
#     #         return
#
#
#     def _handle_client_disconnect(self, where: str | None, msg: str | None = None):
#         """
#         c_tcp_server.py -> signalMessage(sender, 'connection', {'where': 'Bank N', 'msg': 'Client socket closed: ...'})
#         를 받아, Bank 번호에 따라 clsInfo의 Ready 플래그를 False로 내립니다.
#           - Bank 1~4: 'Writecard N Ready' = False
#           - Bank 5  : 'IOBoard_Ready'    = False
#         """
#         if not where:
#             return
#         # where는 'Bank 1' 같은 형태를 가정
#         bank_no = None
#         try:
#             import re
#             m = re.search(r'Bank\s*(\d+)', str(where))
#             if m:
#                 bank_no = int(m.group(1))
#         except Exception:
#             bank_no = None
#
#         if bank_no is None:
#             return
#
#         if 1 <= bank_no <= 4:
#             key = f'Writecard {bank_no} Ready'
#             if key in self.clsInfo:
#                 self.clsInfo[key] = False
#                 print(f"[JobControl] {where} disconnected → {key}=False", flush=True)
#             else:
#                 # 키가 없으면 생성해도 됨(옵션)
#                 self.clsInfo[key] = False
#                 print(f"[JobControl] {where} disconnected → {key} created=False", flush=True)
#         elif bank_no == 5:
#             self.clsInfo['IOBoard_Ready'] = False
#             print(f"[JobControl] {where} disconnected → IOBoard_Ready=False", flush=True)
#
#
#
#     def _banks_ready(self) -> bool:
#         """
#         UDP 전환에 따라 TCP 연결 플래그 기반의 Ready 검사 로직을 제거.
#         항상 True를 반환해 테스트 흐름이 연결 이벤트에 의해 막히지 않도록 함.
#         """
#         return True
#
#
#
#     def make_dictionary(self, baseDir, selected_family, selected_model):
#         # 1) settings.xml 경로
#         settings_path = os.path.join(baseDir, "models", selected_family, selected_model, "settings.xml")
#         if not os.path.exists(settings_path):
#             print(f"[ERROR] settings.xml 파일이 존재하지 않습니다: {settings_path}")
#             return
#
#         # [ADD] 새 사이클 시작: Recon 관련 게이트/플래그 초기화
#         #  - 'Left/Right Recon Script Loaded'를 다음 사이클에서 다시 emit하기 위함
#         self.left_ready_printed = False
#         self.right_ready_printed = False
#         self.clsInfo['Left_Recon_Script'] = False
#         self.clsInfo['Right_Recon_Script'] = False
#
#         # 2) writecard1~4 qty 파싱 (파싱 실패 시 0)
#         writecard1_qty = 0
#         writecard2_qty = 0
#         writecard3_qty = 0
#         writecard4_qty = 0
#
#         attrs = util_base.parse_settings_xml(settings_path, "writecard1")
#         if attrs:
#             writecard1_qty = int(attrs.get("qty", 0))
#         attrs = util_base.parse_settings_xml(settings_path, "writecard2")
#         if attrs:
#             writecard2_qty = int(attrs.get("qty", 0))
#         attrs = util_base.parse_settings_xml(settings_path, "writecard3")
#         if attrs:
#             writecard3_qty = int(attrs.get("qty", 0))
#         attrs = util_base.parse_settings_xml(settings_path, "writecard4")
#         if attrs:
#             writecard4_qty = int(attrs.get("qty", 0))
#
#         # 3) carrier columns 파싱 (기본값 2로 가정)
#         carrier_columns = 2
#         try:
#             c_attrs = util_base.parse_settings_xml(settings_path, "carrier")
#             if c_attrs and "columns" in c_attrs:
#                 carrier_columns = int(c_attrs.get("columns"))
#             else:
#                 tree = ET.parse(settings_path)
#                 root = tree.getroot()
#                 node = root.find(".//carrier")
#                 if node is not None and node.get("columns") is not None:
#                     carrier_columns = int(node.get("columns"))
#         except Exception:
#             pass
#
#         # 4) 딕셔너리 초기화
#         self.job_modules_Left.clear()
#         self.job_modules_Right.clear()
#
#         # 5) columns 조건에 따라 분배
#         if carrier_columns == 1:
#             module_idx = 1
#             for qty, wc_name in [
#                 (writecard1_qty, 'writecard1'),
#                 (writecard2_qty, 'writecard2'),
#                 (writecard3_qty, 'writecard3'),
#                 (writecard4_qty, 'writecard4'),
#             ]:
#                 for _ in range(qty):
#                     self.job_modules_Left[f'Module{module_idx}'] = [False, wc_name, None, None]
#                     module_idx += 1
#
#             print(f"[INFO] (columns=1) job_modules_Left: {self.job_modules_Left}")
#             print(f"[INFO] (columns=1) job_modules_Right: {self.job_modules_Right}")
#
#         else:
#             # 기존 로직: Left ← writecard1,2 / Right ← writecard3,4
#             module_idx = 1
#             for qty, wc_name in [(writecard1_qty, "writecard1"), (writecard2_qty, "writecard2")]:
#                 for _ in range(qty):
#                     self.job_modules_Left[f'Module{module_idx}'] = [False, wc_name, None, None]
#                     module_idx += 1
#
#             module_idx = 1
#             for qty, wc_name in [(writecard3_qty, "writecard3"), (writecard4_qty, "writecard4")]:
#                 for _ in range(qty):
#                     self.job_modules_Right[f'Module{module_idx}'] = [False, wc_name, None, None]
#                     module_idx += 1
#
#             print("[INFO] job_modules_Left:", self.job_modules_Left)
#             print("[INFO] job_modules_Right:", self.job_modules_Right)
#
#         # 6) 스크립트 전송
#         self.send_scripts_to_clients()
#
#
#
#
#
#
#
#     def send_scripts_to_clients(self):
#         """
#         연결된 클라이언트에게 스크립트를 전송 (UDP 버전)
#         - Bank1~4의 주소를 sysInfo.xml에서 읽어와 직접 UDP 전송
#         - 'Script send' 알림 → 파일 청크 전송(1024 bytes) → 'EOF' 전송 순서
#         """
#         try:
#             family_name = util_base.get_xml_info(self.baseDir, 'recent/familyName')
#             model_name = util_base.get_xml_info(self.baseDir, 'recent/modelName')
#
#             if not family_name or not model_name:
#                 self.signalMessage.emit(self.objectName(), 'ui',
#                                         {'msg': "[Error] familyName 또는 modelName 정보를 가져오지 못했습니다."})
#                 return
#
#             settings_file_path = os.path.join(self.baseDir, 'models', family_name, model_name, 'settings.xml')
#             if not os.path.exists(settings_file_path):
#                 self.signalMessage.emit(self.objectName(), 'ui', {'msg': f"[Error] settings.xml 파일을 찾을 수 없습니다."})
#                 self.signalMessage.emit(self.objectName(), 'ui', {'msg': f"[Error] {settings_file_path}"})
#                 return
#
#             try:
#                 tree = ET.parse(settings_file_path)
#                 root = tree.getroot()
#
#                 script_element = root.find(".//script")
#                 if script_element is None or 'filePath' not in script_element.attrib:
#                     self.signalMessage.emit(self.objectName(), 'ui',
#                                             {'msg': "[Error] settings.xml에서 script 태그 또는 filePath키를 찾을 수 없습니다."})
#                     return
#
#                 script_file_path = os.path.join(self.baseDir, 'models', family_name, model_name,
#                                                 f"{model_name}_script.txt")
#                 # script_file_path = os.path.join(self.baseDir, 'models', family_name, model_name,
#                 #                                 script_element.get('filePath'))
#                 if not os.path.exists(script_file_path):
#                     self.signalMessage.emit(self.objectName(), 'ui',
#                                             {'msg': f"[Error] 스크립트 파일을 찾을 수 없습니다. : {script_file_path}"})
#                     return
#             except ET.ParseError as e:
#                 self.signalMessage.emit(self.objectName(), 'ui',
#                                         {'msg': f"[Error] settings.xml 파일을 파싱하는 중 오류 발생: {str(e)}"})
#                 return
#
#             # Bank1~4 주소 수집
#             bank_addrs = {}
#             for bank_no in (1, 2, 3, 4):
#                 addr = self._get_bank_addr_from_config(bank_no)
#                 if addr:
#                     bank_addrs[f"Bank{bank_no}"] = addr
#                 else:
#                     self.signalMessage.emit(self.objectName(), 'ui',
#                                             {'msg': f"[Warning] Bank{bank_no} address not found. Skip script send."})
#
#             if not bank_addrs:
#                 self.signalMessage.emit(self.objectName(), 'ui', {'msg': "[Error] 전송 가능한 Bank 주소가 없습니다."})
#                 return
#
#             # 1) 사전 알림: "Script send"
#             for bank_name, addr in bank_addrs.items():
#                 try:
#                     ok = self.writeCard.send_data(client_socket=addr, data="Script send")
#                     time.sleep(0.05)
#                     if ok:
#                         self.signalMessage.emit(self.objectName(), 'ui',
#                                                 {'msg': f"{bank_name} 'Script send' 메시지 전송 완료"})
#                     else:
#                         self.signalMessage.emit(self.objectName(), 'ui',
#                                                 {'msg': f"[Error] {bank_name} 'Script send' 전송 실패"})
#                 except Exception:
#                     self.signalMessage.emit(self.objectName(), 'ui',
#                                             {'msg': f"[Error] {bank_name} 'Script send' 전송 중 예외 발생"})
#
#             # 2) 스크립트 파일 청크 전송
#             chunks = []
#             with open(script_file_path, 'rb') as script_file:
#                 while True:
#                     chunk = script_file.read(768)
#                     if not chunk:
#                         break
#                     chunks.append(chunk)
#             total_chunks = len(chunks)
#
#             failed_banks = set()
#             for idx, chunk in enumerate(chunks, 1):
#                 for bank_name, addr in list(bank_addrs.items()):
#                     if bank_name in failed_banks:
#                         continue
#                     try:
#                         ok = self.writeCard.send_chunk_to_clients(client_socket=addr, chunk=chunk)
#                         print(f'chunk: {chunk}')
#                         time.sleep(0.05)
#                         if ok:
#                             self.signalMessage.emit(self.objectName(), 'ui',
#                                                     {'msg': f"{bank_name} 스크립트 ({idx}/{total_chunks}) 전송 완료"})
#                         else:
#                             failed_banks.add(bank_name)
#                             self.signalMessage.emit(self.objectName(), 'ui',
#                                                     {'msg': f"[Error] {bank_name} 데이터 전송 실패"})
#                     except Exception:
#                         failed_banks.add(bank_name)
#                         self.signalMessage.emit(self.objectName(), 'ui',
#                                                 {'msg': f"[Error] {bank_name} 데이터 전송 중 예외 발생"})
#
#             # 3) EOF 전송
#             for bank_name, addr in bank_addrs.items():
#                 if bank_name in failed_banks:
#                     continue
#                 ok = self.writeCard.send_data(client_socket=addr, data="EOF")
#                 if ok:
#                     print(f"[UDPServer] {bank_name}에 종료 시그널 전송 완료")
#                 else:
#                     self.signalMessage.emit(self.objectName(), 'ui',
#                                             {'msg': f"[Error] {bank_name} 종료 시그널 전송 실패"})
#
#         except Exception as e:
#             self.signalMessage.emit(self.objectName(), 'ui', {'msg': f"[Error] 스크립트 전송 중 오류 발생: {str(e)}"})
#             print(f"[JobController Error] 스크립트 전송 중 오류 발생: {e}")
#
#
#
#
#
#
#
#     # def send_scripts_to_clients(self):
#     #     """
#     #     연결된 클라이언트에게 스크립트를 전송
#     #     """
#     #     send_chunks = set()
#     #
#     #     try:
#     #         family_name = util_base.get_xml_info(self.baseDir, 'recent/familyName')
#     #         model_name = util_base.get_xml_info(self.baseDir, 'recent/modelName')
#     #
#     #         if not family_name or not model_name:
#     #             self.signalMessage.emit(self.objectName(), 'ui',
#     #                                     {'msg': "[Error] familyName 또는 modelName 정보를 가져오지 못했습니다."})
#     #             return
#     #
#     #         settings_file_path = os.path.join(self.baseDir, 'models', family_name, model_name, 'settings.xml')
#     #         if not os.path.exists(settings_file_path):
#     #             self.signalMessage.emit(self.objectName(), 'ui', {'msg': f"[Error] settings.xml 파일을 찾을 수 없습니다."})
#     #             self.signalMessage.emit(self.objectName(), 'ui', {'msg': f"[Error] {settings_file_path}"})
#     #
#     #         try:
#     #             tree = ET.parse(settings_file_path)
#     #             root = tree.getroot()
#     #
#     #             script_element = root.find(".//script")
#     #             if script_element is None or 'filePath' not in script_element.attrib:
#     #                 self.signalMessage.emit(self.objectName(), 'ui',
#     #                                         {'msg': "[Error] settings.xml에서 script 태그 또는 filePath키를 찾을 수 없습니다."})
#     #                 return
#     #
#     #             script_file_path = os.path.join(self.baseDir, 'models', family_name, model_name,
#     #                                             script_element.get('filePath'))
#     #             if not os.path.exists(script_file_path):
#     #                 self.signalMessage.emit(self.objectName(), 'ui',
#     #                                         {'msg': f"[Error] 스크립트 파일을 찾을 수 없습니다. : {script_file_path}"})
#     #                 return
#     #         except ET.ParseError as e:
#     #             self.signalMessage.emit(self.objectName(), 'ui',
#     #                                     {'msg': f"[Error] settings.xml 파일을 파싱하는 중 오류 발생: {str(e)}"})
#     #             return
#     #
#     #         clients_info = util_base.get_xml_info(self.baseDir, 'clients')
#     #         connected_sockets = self.writeCard.get_connected_sockets(clients_info)
#     #
#     #         for client_name, client_socket in connected_sockets.items():
#     #             try:
#     #                 if client_socket:
#     #                     self.writeCard.send_data(client_socket=client_socket, data="Script send")
#     #                     self.signalMessage.emit(self.objectName(), 'ui',
#     #                                             {'msg': f"클라이언트 {client_name} 'Script send' 메시지 전송 완료"})
#     #                 else:
#     #                     self.signalMessage.emit(self.objectName(), 'ui',
#     #                                             {'msg': f"[Error] 클라이언트 {client_name}의 IP 또는 포트 정보가 누락되었습니다."})
#     #             except Exception as e:
#     #                 self.signalMessage.emit(self.objectName(), 'ui',
#     #                                         {'msg': f"[Error] 클라이언트에게 'Script send' 메시지 전송 중 오류가 발생하였습니다."})
#     #
#     #         chunks = []
#     #         with open(script_file_path, 'rb') as script_file:
#     #             while chunk := script_file.read(1024):
#     #                 chunks.append(chunk)
#     #             total_chunks = len(chunks)
#     #
#     #             failed_clients = set()
#     #
#     #             for idx, chunk in enumerate(chunks, 1):
#     #                 for client_name, client_socket in list(connected_sockets.items()):
#     #                     if client_name in failed_clients:
#     #                         continue
#     #                     try:
#     #                         if client_socket:
#     #                             ok = self.writeCard.send_chunk_to_clients(client_socket=client_socket, chunk=chunk)
#     #                             if ok:
#     #                                 self.signalMessage.emit(self.objectName(), 'ui', {
#     #                                     'msg': f"클라이언트 {client_name} 스크립트 ({idx}/{total_chunks}) 전송 완료"
#     #                                 })
#     #                             else:
#     #                                 failed_clients.add(client_name)
#     #                                 self.signalMessage.emit(self.objectName(), 'ui', {
#     #                                     'msg': f"[Error] 클라이언트 {client_name} 데이터 전송 실패(연결 끊김)"
#     #                                 })
#     #                         else:
#     #                             failed_clients.add(client_name)
#     #                             self.signalMessage.emit(self.objectName(), 'ui', {
#     #                                 'msg': f"[Error] 클라이언트 {client_name} 의 IP 또는 포트 정보가 누락되었습니다."
#     #                             })
#     #                     except Exception:
#     #                         failed_clients.add(client_name)
#     #                         self.signalMessage.emit(self.objectName(), 'ui', {
#     #                             'msg': f"[Error] 클라이언트 {client_name} 데이터 전송 중 예외 발생"
#     #                         })
#     #
#     #             for client_name, client_socket in connected_sockets.items():
#     #                 if client_name in failed_clients:
#     #                     continue
#     #                 ok = self.writeCard.send_data(client_socket, b"EOF")
#     #                 if ok:
#     #                     print(f"[TCPServer] 클라이언트 {client_name}에 종료 시그널 전송 완료")
#     #                 else:
#     #                     self.signalMessage.emit(self.objectName(), 'ui', {
#     #                         'msg': f"[Error] 클라이언트 {client_name} 종료 시그널 전송 실패(연결 끊김)"
#     #                     })
#     #
#     #     except Exception as e:
#     #         self.signalMessage.emit(self.objectName(), 'ui', {'msg': f"[Error] 스크립트 전송 중 오류 발생: {str(e)}"})
#     #         print(f"[JobController Error] 스크립트 전송 중 오류 발생: {e}")
#
#
#
#     def _get_carrier_columns(self) -> int:
#         """
#         settings.xml의 <carrier columns="..."> 값을 반환.
#         - 파싱 실패 시 기본값 2를 반환.
#         """
#         settings_path = getattr(self, "settings_xml_info", None)
#         if not settings_path or not os.path.exists(settings_path):
#             return 2
#
#         # 1차: util_base.parse_settings_xml 사용
#         try:
#             attrs = util_base.parse_settings_xml(settings_path, "carrier")
#             if attrs and "columns" in attrs:
#                 return int(attrs.get("columns"))
#         except Exception:
#             pass
#
#         # 2차: XPath로 직접 파싱
#         try:
#             tree = ET.parse(settings_path)
#             root = tree.getroot()
#             node = root.find(".//carrier")
#             if node is not None and node.get("columns") is not None:
#                 return int(node.get("columns"))
#         except Exception:
#             pass
#
#         return 2
#
#     def update_job_module_status(self, values):
#         """
#         'Script save finished: MCU n' 메시지에 따라 job_modules_Left/Right의 모듈 준비 상태를 갱신.
#         - <carrier columns="1"> 인 경우: writecard1~4 모두 Left 딕셔너리(self.job_modules_Left)에 매핑.
#         - <carrier columns="2"> (또는 기본): Left=writecard1/2, Right=writecard3/4로 매핑.
#         - 전체 준비 완료 시:
#           - columns=1 → self.clsInfo['Left_Recon_Script'] = True
#           - columns>=2 → Left/Right 각각에 대해 해당 플래그 True
#         """
#         msg = values.get('msg', '')
#         where = values.get('where', '')
#
#         if msg.startswith('Script save Failed: MCU'):
#             print(msg)
#             return
#
#         if msg.startswith('Script save finished: MCU') and where.startswith('Write Card '):
#             # MCU 번호 파싱
#             try:
#                 mcu_part = msg.split('MCU')[1]
#                 mcu_num = int(mcu_part.strip().replace(',', ''))
#             except Exception as e:
#                 self.logger.error(f"MCU 파싱 실패: {msg} : {e}")
#                 return
#
#             # Write Card 번호 파싱
#             try:
#                 writecard_num = int(where.replace('Write Card', '').strip())
#             except Exception as e:
#                 self.logger.error(f"Write Card 파싱 실패: {where} : {e}")
#                 return
#
#             # carrier columns 확인
#             carrier_cols = self._get_carrier_columns()
#
#             # 타깃 dict/플래그 결정
#             if carrier_cols == 1:
#                 # 모든 writecard(1~4)를 Left dict로 처리
#                 target_dict = self.job_modules_Left
#                 writecard = f'writecard{writecard_num}'
#                 ready_flag_attr = 'left_ready_printed'
#             else:
#                 # 기본(기존): 1,2 → Left / 3,4 → Right
#                 if writecard_num in [1, 2]:
#                     target_dict = self.job_modules_Left
#                     writecard = f'writecard{writecard_num}'
#                     ready_flag_attr = 'left_ready_printed'
#                 elif writecard_num in [3, 4]:
#                     target_dict = self.job_modules_Right
#                     writecard = f'writecard{writecard_num}'
#                     ready_flag_attr = 'right_ready_printed'
#                 else:
#                     self.logger.error(f"잘못된 Write Card 번호: {writecard_num}")
#                     return
#
#             # writecard별 MCU → ModuleN 매핑
#             def get_module_key_by_writecard_and_mcu(target_dict, writecard, mcu_num):
#                 module_keys = [k for k, v in target_dict.items() if v[1] == writecard]
#                 module_keys.sort(key=lambda x: int(x.replace('Module', '')))
#                 if 0 < mcu_num <= len(module_keys):
#                     return module_keys[mcu_num - 1]
#                 return None
#
#             module_key = get_module_key_by_writecard_and_mcu(target_dict, writecard, mcu_num)
#             if module_key and module_key in target_dict:
#                 entry = target_dict[module_key]
#                 entry[0] = True  # ready 플래그 True
#                 target_dict[module_key] = entry
#                 side = 'Left' if (carrier_cols == 1 or writecard_num in [1, 2]) else 'Right'
#                 self.logger.info(f"Updated {entry[1]}-{module_key} in {side} to True")
#                 print(
#                     f"job_Left: {self.job_modules_Left}' if side == 'Left' else f'job_Right: {self.job_modules_Right}")
#             else:
#                 self.logger.warning(
#                     f"Write Card/MCU 매칭 실패: Write Card {writecard}, MCU {mcu_num} → 해당 Module 없음 (ignored)")
#
#             # 전체 준비 완료 판정 및 clsInfo 갱신
#             all_ready = all(val[0] is True for val in target_dict.values()) and len(target_dict) > 0
#             if all_ready and not getattr(self, ready_flag_attr):
#                 if carrier_cols == 1:
#                     # 단일 컬럼: Left만 존재 → Left_Recon_Script True
#                     self.clsInfo['Left_Recon_Script'] = True
#                     print(f"Left recon script status : {self.clsInfo['Left_Recon_Script']}")
#                     setattr(self, ready_flag_attr, True)
#                     self.signalMessage.emit(self.objectName(), 'ui', {'msg':'Left Recon Script Loaded'})
#                 else:
#                     if writecard_num in [1, 2]:
#                         self.clsInfo['Left_Recon_Script'] = True
#                         print(f"Left recon script status : {self.clsInfo['Left_Recon_Script']}")
#                         self.signalMessage.emit(self.objectName(), 'ui', {'msg':"Left Recon Script Loaded"})
#                     else:
#                         self.clsInfo['Right_Recon_Script'] = True
#                         print(f"Left recon script status : {self.clsInfo['Right_Recon_Script']}")
#                         self.signalMessage.emit(self.objectName(), 'ui', {'msg':"Right Recon Script Loaded"})
#                     setattr(self, ready_flag_attr, True)
#             if self.clsInfo['Left_Recon_Script'] and self.clsInfo['Right_Recon_Script'] == True:
#                 self.signalMessage.emit(self.objectName(), 'job', {'msg':'Script all loaded'})
#
#     def _get_io_socket_cached_or_lookup(self):
#         """
#         IO 보드 소켓을 캐시에서 우선 반환하고, 없으면 ioBoard 모듈에서 조회.
#         """
#         if self.io_socket:
#             return self.io_socket
#         try:
#             sockets = self.ioBoard.get_connected_sockets()
#             return sockets.get('Bank5')
#         except Exception:
#             return None
#
#     def get_writecard_qty(self, settings_path, card_number):
#         # card_number: int (1~4)
#         tag = f'writecard{card_number}'
#         try:
#             tree = ET.parse(settings_path)
#             root = tree.getroot()
#             qty_tag = root.find(f".//{tag}")
#             if qty_tag is not None and qty_tag.attrib.get('qty') is not None:
#                 return int(qty_tag.attrib['qty'])
#         except Exception as e:
#             print(f"settings.xml 파싱 실패: {e}")
#         return 0
#
#
#     # do_test를 감싸는 래퍼: 시작 시 억제(True), 종료 시 항상 복구(False)
#     def _do_test_wrapped(self, *args, **kwargs):
#         try:
#             if getattr(self, 'writeCard', None):
#                 self.writeCard.set_ping_suppressed(True)
#         except Exception:
#             pass
#         try:
#             # 기존 do_test 본문 호출
#             return self.do_test(*args, **kwargs)
#         finally:
#             try:
#                 if getattr(self, 'writeCard', None):
#                     self.writeCard.set_ping_suppressed(False)
#             except Exception:
#                 pass
#
#     def start_do_test_thread(self):
#         # 스레드 시작부: target을 _do_test_wrapped로 지정
#         if not self.clsInfo['is_examine']:
#             self.clsInfo['sensorID'] = str()
#             self.clsInfo['is_examine'] = True
#             self.clsInfo['is_abortTest'] = False
#             self._test_thread = threading.Thread(target=self._do_test_wrapped, daemon=True)
#             self._test_thread.start()
#
#
#
#     def do_test(self):
#         TIME_INTERVAL = 0.1
#         TIME_OUT_SEC = 10
#         idx_examine = 0
#         cnt_timeOut = 0
#         finalResult = None
#         reasonOfFail = None
#
#         family_name = util_base.get_xml_info(self.baseDir, 'recent/familyName')
#         model_name = util_base.get_xml_info(self.baseDir, 'recent/modelName')
#         settings_path = os.path.join(self.baseDir, "models", family_name, model_name, "settings.xml")
#
#         self._barcode_read_requested = False
#         self.clsInfo['barcode_stop'] = False
#
#         # carrier columns 파싱
#         try:
#             tree = ET.parse(settings_path)
#             root = tree.getroot()
#             carrier_el = root.find(".//carrier")
#             carrier_columns = int((carrier_el.attrib.get('columns') or carrier_el.attrib.get('column') or 2))
#         except Exception:
#             carrier_columns = 2
#
#         # Bank5 주소 해석(캐시 우선, 없으면 sysInfo.xml)
#         def _resolve_bank5_addr():
#             try:
#                 sockets = self.ioBoard.get_connected_sockets()
#                 addr = sockets.get('Bank5')
#                 if addr:
#                     return addr
#             except Exception:
#                 pass
#             return self._get_bank_addr_from_config(5)
#
#         while self.clsInfo['is_examine']:
#             # 스캔 중단 즉시 복귀
#             if bool(self.clsInfo.get('barcode_stop')):
#                 addr = _resolve_bank5_addr()
#                 if addr:
#                     self.writeCard.send_data(client_socket=addr, data='Pusher back')
#                     print("[do_test] Sent 'Pusher back' due to barcode_stop")
#                 if finalResult is None:
#                     finalResult = 'Fail'
#                 if not reasonOfFail:
#                     reasonOfFail = 'Scan Failed'
#                 idx_examine = 100
#                 self.clsInfo['barcode_stop'] = False
#                 continue
#
#             if idx_examine == 0:
#                 try:
#                     rt_left = bool(self.p_mainWindow.clsInfo.get('RealTimeLeft'))
#                     rt_right = bool(self.p_mainWindow.clsInfo.get('RealTimeRight'))
#                     if rt_left or rt_right:
#                         print("[do_test] RealTimeLeft/Right detected → terminate test immediately")
#                         idx_examine = 100
#                         continue
#                 except Exception as e:
#                     print(f"[do_test] RealTime flags check error: {e}")
#
#                 if self.p_mainWindow.clsInfo['qty pass'] == False:
#                     reasonOfFail = f'Module quantity error: Please check settings.xml'
#                     finalResult = 'Fail'
#                     idx_examine = 100
#                 else:
#                     self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': 'STStart'})
#                     idx_examine += 1
#
#             if idx_examine == 1:
#                 # any_fail = False
#                 # for i in range(1, 5):
#                 #     ready_key = f'Writecard {i} Ready'
#                 #     qty = self.get_writecard_qty(settings_path, i)
#                 #     if qty != 0 and self.clsInfo[ready_key] == False:
#                 #         reasonOfFail = f'Writecard {i} is not connected'
#                 #         finalResult = 'Fail'
#                 #         idx_examine = 100
#                 #         any_fail = True
#                 #         break
#                 # if any_fail:
#                 #     time.sleep(TIME_INTERVAL)
#                 #     continue
#                 self.signalMessage.emit(self.objectName(), 'job',
#                                         {'where': 'do_test', 'msg': 'Write cards connection OK'})
#                 idx_examine += 1
#
#             elif idx_examine == 2:
#                 if carrier_columns == 1:
#                     if self.clsInfo['Left_Recon_Script'] is True:
#                         self.signalMessage.emit(self.objectName(), 'job',
#                                                 {'where': 'do_test', 'msg': 'Script loaded OK'})
#                         idx_examine += 1
#                     else:
#                         reasonOfFail = 'Left Recon Script is not loaded'
#                         finalResult = 'Fail'
#                         idx_examine = 100
#                 else:
#                     if self.clsInfo['Left_Recon_Script'] is True and self.clsInfo['Right_Recon_Script'] is True:
#                         self.signalMessage.emit(self.objectName(), 'job',
#                                                 {'where': 'do_test', 'msg': 'Script loaded OK'})
#                         idx_examine += 1
#                     elif self.clsInfo['Left_Recon_Script'] is False:
#                         reasonOfFail = 'Left Recon Script is not loaded'
#                         finalResult = 'Fail'
#                         idx_examine = 100
#                     elif self.clsInfo['Right_Recon_Script'] is False:
#                         reasonOfFail = 'Right Recon Script is not loaded'
#                         finalResult = 'Fail'
#                         idx_examine = 100
#
#             elif idx_examine == 3:
#                 self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': 'Mapping start'})
#                 idx_examine += 1
#
#             elif idx_examine == 4:
#                 if not self._barcode_read_requested:
#                     self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': 'Barcode read'})
#                     self._barcode_read_requested = True
#                 if self.clsInfo.get('1st_image_scan') is True:
#                     cnt_timeOut = 0
#                     idx_examine += 1
#                     self._barcode_read_requested = False
#
#             elif idx_examine == 5:
#                 # 파일 정리
#                 barcode_dir = os.path.join(self.baseDir, "barcode")
#                 for p in (
#                         os.path.join(barcode_dir, "Left.txt"),
#                         os.path.join(barcode_dir, "Right.txt"),
#                         os.path.join(barcode_dir, "Left_Recon.txt"),
#                         os.path.join(barcode_dir, "Right_Recon.txt"),
#                 ):
#                     if os.path.exists(p):
#                         try:
#                             with open(p, 'w') as f:
#                                 f.write("")
#                             print(f"Cleared content of file: {p}")
#                         except Exception as e:
#                             print(f"[Error] Unable to clear content of {p}: {e}")
#
#                 # IO 보드로 'Pusher front' 전송(UDP)
#                 addr = _resolve_bank5_addr()
#                 if addr and self.writeCard.send_data(client_socket=addr, data='Pusher front'):
#                     self.signalMessage.emit(self.objectName(), 'ui',
#                                             {'msg': "Sent 'Pusher front' to IO Board (Bank5)"})
#                     self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': 'Pusher front'})
#                     cnt_timeOut = 0
#                     idx_examine += 1
#                 else:
#                     self.signalMessage.emit(self.objectName(), 'ui',
#                                             {
#                                                 'msg': "[Warning] IO Board (Bank5) is not connected. Could not send 'Pusher front'."})
#                     reasonOfFail = 'Cannot send message to IO board'
#                     finalResult = 'Fail'
#                     idx_examine = 100
#
#             elif idx_examine == 6:
#                 if not self.clsInfo.get('1st_left_barcode'):
#                     time.sleep(TIME_INTERVAL)
#                     continue
#
#                 def _all_scanned(modules_dict: dict) -> bool:
#                     for v in modules_dict.values():
#                         try:
#                             if bool(v[0]) and v[2] is None:
#                                 return False
#                         except Exception:
#                             return False
#                     return True
#
#                 left_done = _all_scanned(self.p_mainWindow.modules_Left)
#                 if not left_done:
#                     time.sleep(TIME_INTERVAL)
#                     continue
#
#                 settings_path2 = os.path.join(
#                     self.baseDir, "models",
#                     self.p_mainWindow.cb_customerName.currentText(),
#                     self.p_mainWindow.cb_selectedModel.currentText(),
#                     "settings.xml"
#                 )
#                 barcode_length = None
#                 try:
#                     tree = ET.parse(settings_path2)
#                     root = tree.getroot()
#                     barcode_tag = root.find(".//barcode")
#                     if barcode_tag is not None and barcode_tag.attrib.get('length') is not None:
#                         barcode_length = int(barcode_tag.attrib['length'])
#                 except Exception as e:
#                     print(f"settings.xml barcode 길이 파싱 실패: {e}")
#
#                 def check_left(modules_dict, barcode_length):
#                     for v in modules_dict.values():
#                         if v[0] is True:
#                             if v[2] is None:
#                                 return 'Barcode reading error'
#                             if isinstance(v[2], str):
#                                 if v[2] != 'Scan Failed' and barcode_length is not None and len(v[2]) != barcode_length:
#                                     return 'Barcode reading error'
#                             if v[2] == 'Scan Failed':
#                                 return 'Scan Failed'
#                     return None
#
#                 left_result = check_left(self.p_mainWindow.modules_Left, barcode_length)
#                 print(f'job (Left only at idx 6): {self.p_mainWindow.modules_Left}')
#
#                 if left_result == 'Barcode reading error':
#                     reasonOfFail = 'Barcode reading error'
#                     finalResult = 'Fail'
#                     idx_examine = 100
#                     addr = _resolve_bank5_addr()
#                     if addr:
#                         self.writeCard.send_data(client_socket=addr, data='go_init')
#                     continue
#                 elif left_result == 'Scan Failed':
#                     reasonOfFail = 'Scan Failed'
#                     finalResult = 'Fail'
#                     idx_examine = 100
#                     addr = _resolve_bank5_addr()
#                     if addr:
#                         self.writeCard.send_data(client_socket=addr, data='go_init')
#                     continue
#                 else:
#                     idx_examine += 1
#                     cnt_timeOut = 0
#                     self.signalMessage.emit(
#                         self.objectName(), 'job',
#                         {'where': 'do_test', 'msg': '1st_left_barcode_OK'}
#                     )
#                     continue
#
#             elif idx_examine == 7:
#                 if not self.clsInfo.get('1st_right_barcode'):
#                     time.sleep(TIME_INTERVAL)
#                     continue
#
#                 def _all_scanned(modules_dict: dict) -> bool:
#                     for v in modules_dict.values():
#                         try:
#                             if bool(v[0]) and v[2] is None:
#                                 return False
#                         except Exception:
#                             return False
#                     return True
#
#                 right_done = _all_scanned(self.p_mainWindow.modules_Right)
#                 if not right_done:
#                     time.sleep(TIME_INTERVAL)
#                     continue
#
#                 settings_path2 = os.path.join(
#                     self.baseDir, "models",
#                     self.p_mainWindow.cb_customerName.currentText(),
#                     self.p_mainWindow.cb_selectedModel.currentText(),
#                     "settings.xml"
#                 )
#                 barcode_length = None
#                 try:
#                     tree = ET.parse(settings_path2)
#                     root = tree.getroot()
#                     barcode_tag = root.find(".//barcode")
#                     if barcode_tag is not None and barcode_tag.attrib.get('length') is not None:
#                         barcode_length = int(barcode_tag.attrib['length'])
#                 except Exception as e:
#                     print(f"settings.xml barcode 길이 파싱 실패: {e}")
#
#                 def check_right(modules_dict, barcode_length):
#                     for v in modules_dict.values():
#                         if v[0] is True:
#                             if v[2] is None:
#                                 return 'Barcode reading error'
#                             if isinstance(v[2], str):
#                                 if v[2] != 'Scan Failed' and barcode_length is not None and len(v[2]) != barcode_length:
#                                     return 'Barcode reading error'
#                             if v[2] == 'Scan Failed':
#                                 return 'Scan Failed'
#                     return None
#
#                 right_result = check_right(self.p_mainWindow.modules_Right, barcode_length)
#                 print(f'job (Right only at idx 7): {self.p_mainWindow.modules_Right}')
#
#                 if right_result == 'Barcode reading error':
#                     reasonOfFail = 'Barcode reading error'
#                     finalResult = 'Fail'
#                     idx_examine = 100
#                     addr = _resolve_bank5_addr()
#                     if addr:
#                         self.writeCard.send_data(client_socket=addr, data='go_init')
#                     continue
#                 elif right_result == 'Scan Failed':
#                     reasonOfFail = 'Scan Failed'
#                     finalResult = 'Fail'
#                     idx_examine = 100
#                     addr = _resolve_bank5_addr()
#                     if addr:
#                         self.writeCard.send_data(client_socket=addr, data='go_init')
#                     continue
#                 else:
#                     self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': '1st Scan OK'})
#                     self.send_barcodes_to_clients()
#                     print('send_barcodes_clients executed')
#                     idx_examine += 1
#                     cnt_timeOut = 0
#                     self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': '1st_Scan_OK'})
#                     continue
#
#             elif idx_examine == 8:
#                 idx_examine += 1
#
#             elif idx_examine == 9:
#                 self.send_signal_to_clients(msg='barcode sending finished')
#                 idx_examine += 1
#
#             elif idx_examine == 10:
#                 if self.clsInfo['sensor_data1'] == True and self.clsInfo['sensor_data2'] == True and self.clsInfo[
#                     'sensor_data3'] == True and self.clsInfo['sensor_data4'] == True:
#                     self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': 'sensor ID recieved'})
#                     idx_examine += 1
#                     cnt_timeOut = 0
#
#             elif idx_examine == 11:
#                 if (self.clsInfo['barcode_data1'] and self.clsInfo['barcode_data2']
#                         and self.clsInfo['barcode_data3'] and self.clsInfo['barcode_data4']):
#                     self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': '2nd barcode OK'})
#                     addr = _resolve_bank5_addr()
#                     if addr and self.writeCard.send_data(client_socket=addr, data='Pusher back'):
#                         self.clsInfo['pusher back'] = True
#                         self.signalMessage.emit(self.objectName(), 'ui',
#                                                 {'msg': "Sent 'Pusher back' to IO Board (Bank5)"})
#                     else:
#                         self.signalMessage.emit(self.objectName(), 'ui',
#                                                 {
#                                                     'msg': "[Warning] IO Board (Bank5) is not connected. Could not send 'Pusher back'."})
#                     idx_examine += 1
#
#             elif idx_examine == 12:
#                 if self.clsInfo['c_save'] == True:
#                     self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': 'c_save finished'})
#                     idx_examine += 1
#                     cnt_timeOut = 0
#
#             elif idx_examine == 13:
#                 if self.clsInfo['sensor_dict update'] == True:
#                     self.signalMessage.emit(self.objectName(), 'job',
#                                             {'where': 'do_test', 'msg': 'sensor_dict updated'})
#                     idx_examine += 1
#                     cnt_timeOut = 0
#
#             elif idx_examine == 14:
#                 def _has_pairwise_mismatch(modules_dict: dict) -> bool:
#                     for v in modules_dict.values():
#                         try:
#                             if v[0] is True:
#                                 a = v[2] if len(v) > 2 else None
#                                 b = v[4] if len(v) > 4 else None
#                                 if a is not None and b is not None and str(a) != str(b):
#                                     return True
#                         except Exception:
#                             continue
#                     return False
#
#                 try:
#                     left_mismatch = _has_pairwise_mismatch(self.p_mainWindow.modules_Left)
#                     right_mismatch = _has_pairwise_mismatch(self.p_mainWindow.modules_Right)
#                 except Exception as e:
#                     print(f"[do_test] pairwise compare error: {e}")
#                     left_mismatch = right_mismatch = True
#
#                 if left_mismatch or right_mismatch:
#                     finalResult = finalResult or 'Fail'
#                     reasonOfFail = reasonOfFail or 'Barcode mismatch (col3 vs col5)'
#                     idx_examine = 100
#                     continue
#
#                 idx_examine += 1
#                 cnt_timeOut = 0
#
#             elif idx_examine == 15:
#                 if self.clsInfo['2nd show update'] == True and self.clsInfo['pusher back'] == True:
#                     self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': '3rd Barcode shot'})
#
#                     # 파일 정리
#                     barcode_dir = os.path.join(self.baseDir, "barcode")
#                     for p in [
#                         os.path.join(barcode_dir, "Left.txt"),
#                         os.path.join(barcode_dir, "Right.txt"),
#                         os.path.join(barcode_dir, "Left_Recon.txt"),
#                         os.path.join(barcode_dir, "Right_Recon.txt"),
#                     ]:
#                         if os.path.exists(p):
#                             try:
#                                 with open(p, 'w') as f:
#                                     f.write("")
#                                 print(f"Cleared content of file: {p}")
#                             except Exception as e:
#                                 print(f"[Error] Unable to clear content of {p}: {e}")
#
#                     idx_examine += 1
#                     cnt_timeOut = 0
#
#             elif idx_examine == 16:
#                 if self.clsInfo['3rd_shot'] == True:
#                     self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': '3rd left barcode'})
#                     idx_examine += 1
#                     cnt_timeOut = 0
#
#             elif idx_examine == 17:
#                 if self.clsInfo['3rd_left_barcode'] == True:
#                     settings_path2 = os.path.join(self.baseDir, "models",
#                                                   self.p_mainWindow.cb_customerName.currentText(),
#                                                   self.p_mainWindow.cb_selectedModel.currentText(), "settings.xml")
#                     barcode_length = None
#                     try:
#                         tree = ET.parse(settings_path2)
#                         root = tree.getroot()
#                         barcode_tag = root.find(".//barcode")
#                         if barcode_tag is not None and barcode_tag.attrib.get('length') is not None:
#                             barcode_length = int(barcode_tag.attrib['length'])
#                     except Exception as e:
#                         print(f"settings.xml barcode 길이 파싱 실패: {e}")
#
#                     def check_barcode_length(modules_dict, barcode_length):
#                         for v in modules_dict.values():
#                             if v[0] is True and v[5] is not None and isinstance(v[5], str):
#                                 if v[2] != 'Scan Failed' and barcode_length is not None and len(v[5]) != barcode_length:
#                                     return 'Barcode reading error'
#                             if v[0] is True and v[5] == 'Scan Failed':
#                                 return 'Scan Failed'
#                         return None
#
#                     left_result = check_barcode_length(self.p_mainWindow.modules_Left, barcode_length)
#                     print(f'do_test (3rd, Left only at idx 15) : modules_Left : {self.p_mainWindow.modules_Left}')
#
#                     if left_result == 'Barcode reading error':
#                         reasonOfFail = 'Barcode reading error'
#                         finalResult = 'Fail'
#                         idx_examine = 100
#                     elif left_result == 'Scan Failed':
#                         reasonOfFail = 'Scan Failed'
#                         finalResult = 'Fail'
#                         idx_examine = 100
#                     else:
#                         idx_examine += 1
#                         cnt_timeOut = 0
#                         self.signalMessage.emit(self.objectName(), 'job',
#                                                 {'where': 'do_test', 'msg': '3rd_left_barcode_OK'})
#
#             elif idx_examine == 18:
#                 if self.clsInfo['3rd_right_barcode'] == True:
#                     settings_path2 = os.path.join(self.baseDir, "models",
#                                                   self.p_mainWindow.cb_customerName.currentText(),
#                                                   self.p_mainWindow.cb_selectedModel.currentText(), "settings.xml")
#                     barcode_length = None
#                     try:
#                         tree = ET.parse(settings_path2)
#                         root = tree.getroot()
#                         barcode_tag = root.find(".//barcode")
#                         if barcode_tag is not None and barcode_tag.attrib.get('length') is not None:
#                             barcode_length = int(barcode_tag.attrib['length'])
#                     except Exception as e:
#                         print(f"settings.xml barcode 길이 파싱 실패: {e}")
#
#                     def check_barcode_length(modules_dict, barcode_length):
#                         for v in modules_dict.values():
#                             if v[0] is True and v[5] is not None and isinstance(v[5], str):
#                                 if v[2] != 'Scan Failed' and barcode_length is not None and len(v[5]) != barcode_length:
#                                     return 'Barcode reading error'
#                             if v[0] is True and v[5] == 'Scan Failed':
#                                 return 'Scan Failed'
#                         return None
#
#                     right_result = check_barcode_length(self.p_mainWindow.modules_Right, barcode_length)
#                     print(f'do_test (3rd, Right only at idx 16) : modules_Right : {self.p_mainWindow.modules_Right}')
#
#                     if right_result == 'Barcode reading error':
#                         reasonOfFail = 'Barcode reading error'
#                         finalResult = 'Fail'
#                         idx_examine = 100
#                     elif right_result == 'Scan Failed':
#                         reasonOfFail = 'Scan Failed'
#                         finalResult = 'Fail'
#                         idx_examine = 100
#                     else:
#                         self.signalMessage.emit(self.objectName(), 'job',
#                                                 {'here': 'do_test', 'msg': 'examine finished'})
#                         idx_examine += 1
#                         cnt_timeOut = 0
#
#             elif idx_examine == 19:
#                 print('this message only should be shown when all OK')
#                 idx_examine = 100
#
#             elif idx_examine == 100:
#                 self.signalMessage.emit(self.objectName(), 'job',
#                                         {'where': 'do_test', 'finalResult': finalResult, 'reasonOfFail': reasonOfFail})
#                 self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': 'STStop'})
#
#                 # 종료 시 IO 보드 초기화(UDP)
#                 addr = _resolve_bank5_addr()
#                 if addr:
#                     self.writeCard.send_data(client_socket=addr, data='ManualPusherInitial')
#
#                 self.clsInfo['is_examine'] = False
#                 self._test_thread = None
#                 print('idx_examine = 100. test finished')
#
#                 # 플래그 리셋
#                 self.clsInfo['barcode_stop'] = False
#                 self._barcode_read_requested = False
#                 self.clsInfo['1st_image_scan'] = False
#                 self.clsInfo['1st_left_barcode'] = False
#                 self.clsInfo['1st_right_barcode'] = False
#                 self.clsInfo['sensor data'] = False
#                 self.clsInfo['2nd_barcode'] = False
#                 self.clsInfo['3rd_shot'] = False
#                 self.clsInfo['3rd_left_barcode'] = False
#                 self.clsInfo['3rd_right_barcode'] = False
#                 self.clsInfo['c_save'] = False
#                 self.clsInfo['sensor_dic update'] = False
#                 self.clsInfo['sensor_data1'] = False
#                 self.clsInfo['sensor_data2'] = False
#                 self.clsInfo['sensor_data3'] = False
#                 self.clsInfo['sensor_data4'] = False
#                 self.clsInfo['barcode_data1'] = False
#                 self.clsInfo['barcode_data2'] = False
#                 self.clsInfo['barcode_data3'] = False
#                 self.clsInfo['barcode_data4'] = False
#                 self.clsInfo['2nd show update'] = False
#                 self.clsInfo['3rd_shot'] = False
#                 self.clsInfo['1st_image_scan'] = False
#                 self.clsInfo['pusher back'] = False
#                 self.clsInfo['Scan Stop'] = False
#                 self.clsInfo['pusher_down_started'] = False
#                 self.clsInfo['pusher_down_finished'] = False
#                 self.clsInfo['button_unpushed'] = False
#                 self.clsInfo['pusher_down_ts'] = None
#                 self.clsInfo['button_unpushed_ts'] = None
#                 self.clsInfo['pusher_sequence_decided'] = False
#                 self.clsInfo['early_button_unpushed'] = False
#
#                 for v in self.job_modules_Left.values():
#                     v[0] = False
#                     v[2] = None
#                     v[3] = None
#                 for v in self.job_modules_Right.values():
#                     v[0] = False
#                     v[2] = None
#                     v[3] = None
#                 return
#
#             # Abort Test
#             if self.clsInfo['is_abortTest']:
#                 finalResult = 'Fail'
#                 reasonOfFail = 'Abort Test'
#                 idx_examine = 100
#
#             # Test TimeOut
#             cnt_timeOut += 1
#             if cnt_timeOut > (TIME_OUT_SEC / TIME_INTERVAL):
#                 finalResult = 'Fail'
#                 reasonOfFail = 'Timeout'
#                 idx_examine = 100
#
#             time.sleep(TIME_INTERVAL)
#
#
#
#     # def do_test(self):
#     #     TIME_INTERVAL = 0.1
#     #     TIME_OUT_SEC = 10
#     #     idx_examine = 0
#     #     cnt_timeOut = 0
#     #     finalResult = None
#     #     reasonOfFail = None
#     #
#     #     family_name = util_base.get_xml_info(self.baseDir, 'recent/familyName')
#     #     model_name = util_base.get_xml_info(self.baseDir, 'recent/modelName')
#     #     settings_path = os.path.join(self.baseDir, "models", family_name, model_name, "settings.xml")
#     #
#     #     self._barcode_read_requested = False
#     #
#     #     def get_io_socket():
#     #         return self._get_io_socket_cached_or_lookup()
#     #
#     #     self.clsInfo['barcode_stop'] = False
#     #
#     #     # carrier columns 파싱(기존 동일)
#     #     try:
#     #         tree = ET.parse(settings_path)
#     #         root = tree.getroot()
#     #         carrier_el = root.find(".//carrier")
#     #         carrier_columns = int((carrier_el.attrib.get('columns') or carrier_el.attrib.get('column') or 2))
#     #     except Exception:
#     #         carrier_columns = 2
#     #
#     #     while self.clsInfo['is_examine']:
#     #         if bool(self.clsInfo.get('barcode_stop')):
#     #             try:
#     #                 io_socket = get_io_socket()
#     #                 if io_socket:
#     #                     self.ioBoard.send_data(client_socket=io_socket, data='Pusher back')
#     #                     print("[do_test] Sent 'Pusher back' due to barcode_stop")
#     #             except Exception as e:
#     #                 print(f"[do_test] Failed to send 'Pusher back' on barcode_stop: {e}")
#     #
#     #             if finalResult is None:
#     #                 finalResult = 'Fail'
#     #             if not reasonOfFail:
#     #                 reasonOfFail = 'Scan Failed'
#     #             idx_examine = 100
#     #             self.clsInfo['barcode_stop'] = False
#     #             continue
#     #
#     #         if idx_examine == 0:
#     #             try:
#     #                 rt_left = bool(self.p_mainWindow.clsInfo.get('RealTimeLeft'))
#     #                 rt_right = bool(self.p_mainWindow.clsInfo.get('RealTimeRight'))
#     #                 if rt_left or rt_right:
#     #                     print("[do_test] RealTimeLeft/Right detected → terminate test immediately")
#     #                     idx_examine = 100
#     #                     continue
#     #             except Exception as e:
#     #                 print(f"[do_test] RealTime flags check error: {e}")
#     #
#     #             if self.p_mainWindow.clsInfo['qty pass'] == False:
#     #                 reasonOfFail = f'Module quantity error: Please check settings.xml'
#     #                 finalResult = 'Fail'
#     #                 idx_examine = 100
#     #             else:
#     #                 self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': 'STStart'})
#     #                 idx_examine += 1
#     #
#     #         if idx_examine == 1:
#     #             any_fail = False
#     #             for i in range(1, 5):
#     #                 ready_key = f'Writecard {i} Ready'
#     #                 qty = self.get_writecard_qty(settings_path, i)
#     #                 if qty != 0 and self.clsInfo[ready_key] == False:
#     #                     reasonOfFail = f'Writecard {i} is not connected'
#     #                     finalResult = 'Fail'
#     #                     idx_examine = 100
#     #                     any_fail = True
#     #                     break
#     #             if any_fail:
#     #                 time.sleep(TIME_INTERVAL)
#     #                 continue
#     #             self.signalMessage.emit(self.objectName(), 'job',
#     #                                     {'where': 'do_test', 'msg': 'Write cards connection OK'})
#     #             idx_examine += 1
#     #
#     #         elif idx_examine == 2:
#     #             if carrier_columns == 1:
#     #                 if self.clsInfo['Left_Recon_Script'] is True:
#     #                     self.signalMessage.emit(self.objectName(), 'job',
#     #                                             {'where': 'do_test', 'msg': 'Script loaded OK'})
#     #                     idx_examine += 1
#     #                 else:
#     #                     reasonOfFail = 'Left Recon Script is not loaded'
#     #                     finalResult = 'Fail'
#     #                     idx_examine = 100
#     #             else:
#     #                 if self.clsInfo['Left_Recon_Script'] is True and self.clsInfo['Right_Recon_Script'] is True:
#     #                     self.signalMessage.emit(self.objectName(), 'job',
#     #                                             {'where': 'do_test', 'msg': 'Script loaded OK'})
#     #                     idx_examine += 1
#     #                 elif self.clsInfo['Left_Recon_Script'] is False:
#     #                     reasonOfFail = 'Left Recon Script is not loaded'
#     #                     finalResult = 'Fail'
#     #                     idx_examine = 100
#     #                 elif self.clsInfo['Right_Recon_Script'] is False:
#     #                     reasonOfFail = 'Right Recon Script is not loaded'
#     #                     finalResult = 'Fail'
#     #                     idx_examine = 100
#     #
#     #         elif idx_examine == 3:
#     #             self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': 'Mapping start'})
#     #             idx_examine += 1
#     #
#     #         elif idx_examine == 4:
#     #             if not self._barcode_read_requested:
#     #                 self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': 'Barcode read'})
#     #                 self._barcode_read_requested = True
#     #             if self.clsInfo.get('1st_image_scan') is True:
#     #                 cnt_timeOut = 0
#     #                 idx_examine += 1
#     #                 self._barcode_read_requested = False
#     #
#     #         elif idx_examine == 5:
#     #             # [NEW] Bank5(IO Board)로 'Pusher front' 전송 (안전 조회)
#     #             # Left.txt와 Right.txt 파일 내용 삭제
#     #             barcode_dir = os.path.join(self.baseDir, "barcode")
#     #             left_file_path = os.path.join(barcode_dir, "Left.txt")
#     #             right_file_path = os.path.join(barcode_dir, "Right.txt")
#     #             left_recon_path = os.path.join(barcode_dir, "Left_Recon.txt")
#     #             right_recon_path = os.path.join(barcode_dir, "Right_Recon.txt")
#     #
#     #             # Left.txt 내용 삭제
#     #             if os.path.exists(left_file_path):
#     #                 try:
#     #                     with open(left_file_path, 'w') as file:
#     #                         file.write("")  # 파일 내용을 비움
#     #                     print(f"Cleared content of file: {left_file_path}")
#     #                 except Exception as e:
#     #                     print(f"[Error] Unable to clear content of {left_file_path}: {e}")
#     #
#     #             # Right.txt 내용 삭제
#     #             if os.path.exists(right_file_path):
#     #                 try:
#     #                     with open(right_file_path, 'w') as file:
#     #                         file.write("")  # 파일 내용을 비움
#     #                     print(f"Cleared content of file: {right_file_path}")
#     #                 except Exception as e:
#     #                     print(f"[Error] Unable to clear content of {right_file_path}: {e}")
#     #
#     #                 # NEW: Left_Recon.txt 내용 삭제
#     #             if os.path.exists(left_recon_path):
#     #                 try:
#     #                     with open(left_recon_path, 'w') as file:
#     #                         file.write("")  # 파일 내용을 비움
#     #                     print(f"Cleared content of file: {left_recon_path}")
#     #                 except Exception as e:
#     #                     print(f"[Error] Unable to clear content of {left_recon_path}: {e}")
#     #
#     #                 # NEW: Right_Recon.txt 내용 삭제
#     #             if os.path.exists(right_recon_path):
#     #                 try:
#     #                     with open(right_recon_path, 'w') as file:
#     #                         file.write("")  # 파일 내용을 비움
#     #                     print(f"Cleared content of file: {right_recon_path}")
#     #                 except Exception as e:
#     #                     print(f"[Error] Unable to clear content of {right_recon_path}: {e}")
#     #
#     #             try:
#     #                 io_socket = get_io_socket()
#     #                 if io_socket:
#     #                     self.ioBoard.send_data(client_socket=io_socket, data='Pusher front')
#     #                     self.signalMessage.emit(self.objectName(), 'ui',
#     #                                             {'msg': "Sent 'Pusher front' to IO Board (Bank5)"})
#     #                     self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': 'Pusher front'})
#     #                     cnt_timeOut = 0
#     #                     idx_examine += 1
#     #                 else:
#     #                     self.signalMessage.emit(self.objectName(), 'ui',
#     #                                             {
#     #                                                 'msg': "[Warning] IO Board (Bank5) is not connected. Could not send 'Pusher front'."})
#     #                     reasonOfFail = 'Cannot send message to IO board'
#     #                     finalResult = 'Fail'
#     #                     idx_examine = 100
#     #             except Exception as e:
#     #                 self.signalMessage.emit(self.objectName(), 'ui',
#     #                                         {'msg': f"[Error] Failed to send 'Pusher front' to IO Board: {e}"})
#     #                 reasonOfFail = 'Cannot send message to IO board'
#     #                 finalResult = 'Fail'
#     #                 idx_examine = 100
#     #
#     #         elif idx_examine == 6:
#     #             if not self.clsInfo.get('1st_left_barcode'):
#     #                 time.sleep(TIME_INTERVAL)
#     #                 continue
#     #
#     #             # 2) Left 스캔(1차) 완료 플래그가 아직 아니면 대기
#     #             if not self.clsInfo.get('1st_left_barcode'):
#     #                 time.sleep(TIME_INTERVAL)
#     #                 continue
#     #
#     #             # 3) Left 전체 모듈 스캔 완료 여부 확인
#     #             def _all_scanned(modules_dict: dict) -> bool:
#     #                 for v in modules_dict.values():
#     #                     try:
#     #                         if bool(v[0]) and v[2] is None:
#     #                             return False
#     #                     except Exception:
#     #                         return False
#     #                 return True
#     #
#     #             left_done = _all_scanned(self.p_mainWindow.modules_Left)
#     #             if not left_done:
#     #                 time.sleep(TIME_INTERVAL)
#     #                 continue
#     #
#     #             # 4) 길이/실패 검사 준비
#     #             settings_path = os.path.join(
#     #                 self.baseDir, "models",
#     #                 self.p_mainWindow.cb_customerName.currentText(),
#     #                 self.p_mainWindow.cb_selectedModel.currentText(),
#     #                 "settings.xml"
#     #             )
#     #             barcode_length = None
#     #             try:
#     #                 tree = ET.parse(settings_path)
#     #                 root = tree.getroot()
#     #                 barcode_tag = root.find(".//barcode")
#     #                 if barcode_tag is not None and barcode_tag.attrib.get('length') is not None:
#     #                     barcode_length = int(barcode_tag.attrib['length'])
#     #             except Exception as e:
#     #                 print(f"settings.xml barcode 길이 파싱 실패: {e}")
#     #
#     #             def check_left(modules_dict, barcode_length):
#     #                 for v in modules_dict.values():
#     #                     if v[0] is True:
#     #                         if v[2] is None:
#     #                             return 'Barcode reading error'
#     #                         if isinstance(v[2], str):
#     #                             if v[2] != 'Scan Failed' and barcode_length is not None and len(v[2]) != barcode_length:
#     #                                 return 'Barcode reading error'
#     #                         if v[2] == 'Scan Failed':
#     #                             return 'Scan Failed'
#     #                 return None
#     #
#     #             left_result = check_left(self.p_mainWindow.modules_Left, barcode_length)
#     #             print(f'job (Left only at idx 6): {self.p_mainWindow.modules_Left}')
#     #
#     #             if left_result == 'Barcode reading error':
#     #                 reasonOfFail = 'Barcode reading error'
#     #                 finalResult = 'Fail'
#     #                 idx_examine = 100
#     #                 io_socket = get_io_socket()
#     #                 if io_socket:
#     #                     self.writeCard.send_data(client_socket=io_socket, data='go_init')
#     #                 continue  # 종료 루프 처리 대기
#     #             elif left_result == 'Scan Failed':
#     #                 reasonOfFail = 'Scan Failed'
#     #                 finalResult = 'Fail'
#     #                 idx_examine = 100
#     #                 io_socket = get_io_socket()
#     #                 if io_socket:
#     #                     self.writeCard.send_data(client_socket=io_socket, data='go_init')
#     #                 continue
#     #             else:
#     #                 # 정상 → 다음 단계
#     #                 idx_examine += 1
#     #                 cnt_timeOut = 0
#     #                 self.signalMessage.emit(
#     #                     self.objectName(), 'job',
#     #                     {'where': 'do_test', 'msg': '1st_left_barcode_OK'}
#     #                 )
#     #                 continue  # 명시적 continue (가독성)
#     #
#     #         elif idx_examine == 7:
#     #             if not self.clsInfo.get('1st_right_barcode'):
#     #                 time.sleep(TIME_INTERVAL)
#     #                 continue
#     #
#     #             # 2) Right 1차 바코드 완료 플래그가 아직 False면 대기
#     #             if not self.clsInfo.get('1st_right_barcode'):
#     #                 time.sleep(TIME_INTERVAL)
#     #                 continue
#     #
#     #             # 3) Right 전체 모듈 스캔 완료 여부 확인
#     #             def _all_scanned(modules_dict: dict) -> bool:
#     #                 for v in modules_dict.values():
#     #                     try:
#     #                         if bool(v[0]) and v[2] is None:
#     #                             return False
#     #                     except Exception:
#     #                         return False
#     #                 return True
#     #
#     #             right_done = _all_scanned(self.p_mainWindow.modules_Right)
#     #             if not right_done:
#     #                 time.sleep(TIME_INTERVAL)
#     #                 continue
#     #
#     #             # 4) 길이/실패 검사 준비
#     #             settings_path = os.path.join(
#     #                 self.baseDir, "models",
#     #                 self.p_mainWindow.cb_customerName.currentText(),
#     #                 self.p_mainWindow.cb_selectedModel.currentText(),
#     #                 "settings.xml"
#     #             )
#     #             barcode_length = None
#     #             try:
#     #                 tree = ET.parse(settings_path)
#     #                 root = tree.getroot()
#     #                 barcode_tag = root.find(".//barcode")
#     #                 if barcode_tag is not None and barcode_tag.attrib.get('length') is not None:
#     #                     barcode_length = int(barcode_tag.attrib['length'])
#     #             except Exception as e:
#     #                 print(f"settings.xml barcode 길이 파싱 실패: {e}")
#     #
#     #             def check_right(modules_dict, barcode_length):
#     #                 for v in modules_dict.values():
#     #                     if v[0] is True:
#     #                         if v[2] is None:
#     #                             return 'Barcode reading error'
#     #                         if isinstance(v[2], str):
#     #                             if v[2] != 'Scan Failed' and barcode_length is not None and len(v[2]) != barcode_length:
#     #                                 return 'Barcode reading error'
#     #                         if v[2] == 'Scan Failed':
#     #                             return 'Scan Failed'
#     #                 return None
#     #
#     #             right_result = check_right(self.p_mainWindow.modules_Right, barcode_length)
#     #             print(f'job (Right only at idx 7): {self.p_mainWindow.modules_Right}')
#     #
#     #             if right_result == 'Barcode reading error':
#     #                 reasonOfFail = 'Barcode reading error'
#     #                 finalResult = 'Fail'
#     #                 idx_examine = 100
#     #                 io_socket = get_io_socket()
#     #                 if io_socket:
#     #                     self.writeCard.send_data(client_socket=io_socket, data='go_init')
#     #                 continue
#     #             elif right_result == 'Scan Failed':
#     #                 reasonOfFail = 'Scan Failed'
#     #                 finalResult = 'Fail'
#     #                 idx_examine = 100
#     #                 io_socket = get_io_socket()
#     #                 if io_socket:
#     #                     self.writeCard.send_data(client_socket=io_socket, data='go_init')
#     #                 continue
#     #             else:
#     #                 # Left(6) OK + Right(7) OK → 메시지 송신 후 다음 단계
#     #                 self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': '1st Scan OK'})
#     #                 self.send_barcodes_to_clients()
#     #                 print('send_barcodes_clients executed')
#     #                 idx_examine += 1
#     #                 cnt_timeOut = 0
#     #                 self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': '1st_Scan_OK'})
#     #                 continue  # 명시적 continue
#     #
#     #         elif idx_examine == 8:
#     #             idx_examine += 1
#     #
#     #         elif idx_examine == 9:
#     #             self.send_signal_to_clients(msg='barcode sending finished')
#     #             idx_examine += 1
#     #
#     #         elif idx_examine == 10:
#     #             if self.clsInfo['sensor_data1'] == True and self.clsInfo['sensor_data2'] == True and self.clsInfo[
#     #                 'sensor_data3'] == True and self.clsInfo['sensor_data4'] == True:
#     #                 self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': 'sensor ID recieved'})
#     #                 idx_examine += 1
#     #                 cnt_timeOut = 0
#     #
#     #         elif idx_examine == 11:
#     #             if (self.clsInfo['barcode_data1'] and self.clsInfo['barcode_data2']
#     #                     and self.clsInfo['barcode_data3'] and self.clsInfo['barcode_data4']):
#     #                 self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': '2nd barcode OK'})
#     #                 try:
#     #                     io_socket = get_io_socket()
#     #                     if io_socket:
#     #                         self.clsInfo['pusher back'] = False
#     #                         self.ioBoard.send_data(client_socket=io_socket, data='Pusher back')
#     #                         self.signalMessage.emit(self.objectName(), 'ui',
#     #                                                 {'msg': "Sent 'Pusher back' to IO Board (Bank5)"})
#     #                     else:
#     #                         self.signalMessage.emit(self.objectName(), 'ui',
#     #                                                 {
#     #                                                     'msg': "[Warning] IO Board (Bank5) is not connected. Could not send 'Pusher back'."})
#     #                 except Exception as e:
#     #                     self.signalMessage.emit(self.objectName(), 'ui',
#     #                                             {'msg': f"[Error] Failed to send 'Pusher back' to IO Board: {e}"})
#     #                 idx_examine += 1
#     #
#     #         elif idx_examine == 12:
#     #             if self.clsInfo['c_save'] == True:
#     #                 self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': 'c_save finished'})
#     #                 idx_examine += 1
#     #                 cnt_timeOut = 0
#     #
#     #         elif idx_examine == 13:
#     #             if self.clsInfo['sensor_dict update'] == True:
#     #                 self.signalMessage.emit(self.objectName(), 'job',
#     #                                         {'where': 'do_test', 'msg': 'sensor_dict updated'})
#     #                 idx_examine += 1
#     #                 cnt_timeOut = 0
#     #
#     #         # [NEW] 14단계: 3열(인덱스2) vs 5열(인덱스4) 비교. 불일치 시 즉시 종료로 전환.
#     #         elif idx_examine == 14:
#     #             def _has_pairwise_mismatch(modules_dict: dict) -> bool:
#     #                 for v in modules_dict.values():
#     #                     try:
#     #                         if v[0] is True:
#     #                             a = v[2] if len(v) > 2 else None
#     #                             b = v[4] if len(v) > 4 else None
#     #                             # 둘 다 값이 있을 때만 비교해 불일치 판정
#     #                             if a is not None and b is not None:
#     #                                 if str(a) != str(b):
#     #                                     return True
#     #                     except Exception:
#     #                         # 방어적 무시
#     #                         continue
#     #                 return False
#     #
#     #             try:
#     #                 left_mismatch = _has_pairwise_mismatch(self.p_mainWindow.modules_Left)
#     #                 right_mismatch = _has_pairwise_mismatch(self.p_mainWindow.modules_Right)
#     #             except Exception as e:
#     #                 print(f"[do_test] pairwise compare error: {e}")
#     #                 left_mismatch = right_mismatch = True  # 예외 시 보수적으로 종료
#     #
#     #             if left_mismatch or right_mismatch:
#     #                 finalResult = finalResult or 'Fail'
#     #                 reasonOfFail = reasonOfFail or 'Barcode mismatch (col3 vs col5)'
#     #                 idx_examine = 100
#     #                 continue
#     #
#     #             # 불일치가 없으면 다음 단계로 진행
#     #             idx_examine += 1
#     #             cnt_timeOut = 0
#     #
#     #         elif idx_examine == 15:
#     #             if self.clsInfo['2nd show update'] == True and self.clsInfo['pusher back'] == True:
#     #                 self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': '3rd Barcode shot'})
#     #
#     #                 # Left.txt와 Right.txt 파일 내용 삭제
#     #                 barcode_dir = os.path.join(self.baseDir, "barcode")
#     #                 left_file_path = os.path.join(barcode_dir, "Left.txt")
#     #                 right_file_path = os.path.join(barcode_dir, "Right.txt")
#     #                 left_recon_path = os.path.join(barcode_dir, "Left_Recon.txt")
#     #                 right_recon_path = os.path.join(barcode_dir, "Right_Recon.txt")
#     #
#     #                 # Left.txt 내용 삭제
#     #                 if os.path.exists(left_file_path):
#     #                     try:
#     #                         with open(left_file_path, 'w') as file:
#     #                             file.write("")  # 파일 내용을 비움
#     #                         print(f"Cleared content of file: {left_file_path}")
#     #                     except Exception as e:
#     #                         print(f"[Error] Unable to clear content of {left_file_path}: {e}")
#     #
#     #                 # Right.txt 내용 삭제
#     #                 if os.path.exists(right_file_path):
#     #                     try:
#     #                         with open(right_file_path, 'w') as file:
#     #                             file.write("")  # 파일 내용을 비움
#     #                         print(f"Cleared content of file: {right_file_path}")
#     #                     except Exception as e:
#     #                         print(f"[Error] Unable to clear content of {right_file_path}: {e}")
#     #
#     #                     # NEW: Left_Recon.txt 내용 삭제
#     #                 if os.path.exists(left_recon_path):
#     #                     try:
#     #                         with open(left_recon_path, 'w') as file:
#     #                             file.write("")  # 파일 내용을 비움
#     #                         print(f"Cleared content of file: {left_recon_path}")
#     #                     except Exception as e:
#     #                         print(f"[Error] Unable to clear content of {left_recon_path}: {e}")
#     #
#     #                     # NEW: Right_Recon.txt 내용 삭제
#     #                 if os.path.exists(right_recon_path):
#     #                     try:
#     #                         with open(right_recon_path, 'w') as file:
#     #                             file.write("")  # 파일 내용을 비움
#     #                         print(f"Cleared content of file: {right_recon_path}")
#     #                     except Exception as e:
#     #                         print(f"[Error] Unable to clear content of {right_recon_path}: {e}")
#     #
#     #                 idx_examine += 1
#     #                 cnt_timeOut = 0
#     #
#     #         elif idx_examine == 16:
#     #             if self.clsInfo['3rd_shot'] == True:
#     #                 self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': '3rd left barcode'})
#     #                 idx_examine += 1
#     #                 cnt_timeOut = 0
#     #
#     #         elif idx_examine == 17:
#     #             # 3rd barcode 수신 이후, Left만 검사
#     #             if self.clsInfo['3rd_left_barcode'] == True:
#     #                 settings_path = os.path.join(self.baseDir, "models",
#     #                                              self.p_mainWindow.cb_customerName.currentText(),
#     #                                              self.p_mainWindow.cb_selectedModel.currentText(), "settings.xml")
#     #                 barcode_length = None
#     #                 try:
#     #                     tree = ET.parse(settings_path)
#     #                     root = tree.getroot()
#     #                     barcode_tag = root.find(".//barcode")
#     #                     if barcode_tag is not None and barcode_tag.attrib.get('length') is not None:
#     #                         barcode_length = int(barcode_tag.attrib['length'])
#     #                 except Exception as e:
#     #                     print(f"settings.xml barcode 길이 파싱 실패: {e}")
#     #
#     #                 # 검사 함수 (기존 15 단계의 검사 로직을 좌측만 대상으로 사용)
#     #                 def check_barcode_length(modules_dict, barcode_length):
#     #                     for v in modules_dict.values():
#     #                         # 1. Barcode 길이 에러: 첫번째 값이 True일 때만 체크
#     #                         if v[0] is True and v[5] is not None and isinstance(v[5], str):
#     #                             # 'Scan failed' case는 아래에서 따로 처리
#     #                             if v[2] != 'Scan Failed' and barcode_length is not None and len(v[5]) != barcode_length:
#     #                                 return 'Barcode reading error'
#     #                         # 2. 'Scan failed' 에러: 첫번째 값이 True일 때만 체크
#     #                         if v[0] is True and v[5] == 'Scan Failed':
#     #                             return 'Scan Failed'
#     #                     return None
#     #
#     #                 # Left만 검사
#     #                 left_result = check_barcode_length(self.p_mainWindow.modules_Left, barcode_length)
#     #                 print(f'do_test (3rd, Left only at idx 15) : modules_Left : {self.p_mainWindow.modules_Left}')
#     #
#     #                 if left_result == 'Barcode reading error':
#     #                     reasonOfFail = 'Barcode reading error'
#     #                     finalResult = 'Fail'
#     #                     idx_examine = 100
#     #                 elif left_result == 'Scan Failed':
#     #                     reasonOfFail = 'Scan Failed'
#     #                     finalResult = 'Fail'
#     #                     idx_examine = 100
#     #                 else:
#     #                     # Left OK → 다음 단계에서 Right 검사
#     #                     idx_examine += 1
#     #                     cnt_timeOut = 0
#     #
#     #                     # 필요 시 Left OK 신호를 별도로 보낼 수 있음
#     #                     self.signalMessage.emit(self.objectName(), 'job',
#     #                                             {'where': 'do_test', 'msg': '3rd_left_barcode_OK'})
#     #
#     #         elif idx_examine == 18:
#     #             # 3rd barcode 수신 이후, Right만 검사
#     #             if self.clsInfo['3rd_right_barcode'] == True:
#     #                 settings_path = os.path.join(self.baseDir, "models",
#     #                                              self.p_mainWindow.cb_customerName.currentText(),
#     #                                              self.p_mainWindow.cb_selectedModel.currentText(), "settings.xml")
#     #                 barcode_length = None
#     #                 try:
#     #                     tree = ET.parse(settings_path)
#     #                     root = tree.getroot()
#     #                     barcode_tag = root.find(".//barcode")
#     #                     if barcode_tag is not None and barcode_tag.attrib.get('length') is not None:
#     #                         barcode_length = int(barcode_tag.attrib['length'])
#     #                 except Exception as e:
#     #                     print(f"settings.xml barcode 길이 파싱 실패: {e}")
#     #
#     #                 # 검사 함수 (동일 로직을 Right 대상으로 사용)
#     #                 def check_barcode_length(modules_dict, barcode_length):
#     #                     for v in modules_dict.values():
#     #                         # 1. Barcode 길이 에러: 첫번째 값이 True일 때만 체크
#     #                         if v[0] is True and v[5] is not None and isinstance(v[5], str):
#     #                             # 'Scan failed' case는 아래에서 따로 처리
#     #                             if v[2] != 'Scan Failed' and barcode_length is not None and len(v[5]) != barcode_length:
#     #                                 return 'Barcode reading error'
#     #                         # 2. 'Scan failed' 에러: 첫번째 값이 True일 때만 체크
#     #                         if v[0] is True and v[5] == 'Scan Failed':
#     #                             return 'Scan Failed'
#     #                     return None
#     #
#     #                 # Right만 검사
#     #                 right_result = check_barcode_length(self.p_mainWindow.modules_Right, barcode_length)
#     #                 print(f'do_test (3rd, Right only at idx 16) : modules_Right : {self.p_mainWindow.modules_Right}')
#     #
#     #                 if right_result == 'Barcode reading error':
#     #                     reasonOfFail = 'Barcode reading error'
#     #                     finalResult = 'Fail'
#     #                     idx_examine = 100
#     #                 elif right_result == 'Scan Failed':
#     #                     reasonOfFail = 'Scan Failed'
#     #                     finalResult = 'Fail'
#     #                     idx_examine = 100
#     #                 else:
#     #                     # Right OK → 3rd 전체 OK 처리 후 종료 단계로
#     #                     self.signalMessage.emit(self.objectName(), 'job',
#     #                                             {'here': 'do_test', 'msg': 'examine finished'})
#     #                     idx_examine += 1
#     #                     cnt_timeOut = 0
#     #
#     #         elif idx_examine == 19:
#     #             print('this message only should be shown when all OK')
#     #             idx_examine = 100
#     #
#     #         elif idx_examine == 100:
#     #             self.signalMessage.emit(self.objectName(), 'job',
#     #                                     {'where': 'do_test', 'finalResult': finalResult, 'reasonOfFail': reasonOfFail})
#     #             self.signalMessage.emit(self.objectName(), 'job', {'where': 'do_test', 'msg': 'STStop'})
#     #
#     #             try:
#     #                 io_socket = get_io_socket()
#     #                 if io_socket:
#     #                     self.ioBoard.send_data(client_socket=io_socket, data='ManualPusherInitial')
#     #                 else:
#     #                     print("[do_test] Warning: IO Board not connected.")
#     #             except Exception as e:
#     #                 print(f"[do_test] Error while sending 'ManualPusherInitial': {e}")
#     #
#     #             self.clsInfo['is_examine'] = False
#     #             self._test_thread = None
#     #             print('idx_examine = 100. test finished')
#     #
#     #             # [ADD] 종료 시 바코드 중단 플래그 리셋
#     #             self.clsInfo['barcode_stop'] = False
#     #
#     #             self._barcode_read_requested = False
#     #             self.clsInfo['1st_image_scan'] = False
#     #             self.clsInfo['1st_left_barcode'] = False
#     #             self.clsInfo['1st_right_barcode'] = False
#     #             self.clsInfo['sensor data'] = False
#     #             self.clsInfo['2nd_barcode'] = False
#     #             self.clsInfo['3rd_shot'] = False
#     #             self.clsInfo['3rd_left_barcode'] = False
#     #             self.clsInfo['3rd_right_barcode'] = False
#     #             self.clsInfo['c_save'] = False
#     #             self.clsInfo['sensor_dic update'] = False
#     #             self.clsInfo['sensor_data1'] = False
#     #             self.clsInfo['sensor_data2'] = False
#     #             self.clsInfo['sensor_data3'] = False
#     #             self.clsInfo['sensor_data4'] = False
#     #             self.clsInfo['barcode_data1'] = False
#     #             self.clsInfo['barcode_data2'] = False
#     #             self.clsInfo['barcode_data3'] = False
#     #             self.clsInfo['barcode_data4'] = False
#     #             self.clsInfo['2nd show update'] = False
#     #             self.clsInfo['3rd_shot'] = False
#     #             self.clsInfo['1st_image_scan'] = False
#     #             self.clsInfo['pusher back'] = False
#     #             self.clsInfo['Scan Stop'] = False
#     #             self.clsInfo['pusher_down_started'] = False
#     #             self.clsInfo['pusher_down_finished'] = False
#     #             self.clsInfo['button_unpushed'] = False
#     #             self.clsInfo['pusher_down_ts'] = None
#     #             self.clsInfo['button_unpushed_ts'] = None
#     #             self.clsInfo['pusher_sequence_decided'] = False
#     #             self.clsInfo['early_button_unpushed'] = False
#     #
#     #             for v in self.job_modules_Left.values():
#     #                 v[0] = False
#     #                 v[2] = None
#     #                 v[3] = None
#     #                 # v[4] = None
#     #                 # v[5] = None
#     #                 # v[6] = None
#     #             for v in self.job_modules_Right.values():
#     #                 v[0] = False
#     #                 v[2] = None
#     #                 v[3] = None
#     #                 # v[4] = None
#     #                 # v[5] = None
#     #                 # v[6] = None
#     #             return
#     #
#     #         # Abort Test
#     #         if self.clsInfo['is_abortTest']:
#     #             finalResult = 'Fail'
#     #             reasonOfFail = 'Abort Test'
#     #             idx_examine = 100
#     #
#     #         # Test TimeOut
#     #         cnt_timeOut += 1
#     #         if cnt_timeOut > (TIME_OUT_SEC / TIME_INTERVAL):
#     #             finalResult = 'Fail'
#     #             reasonOfFail = 'Timeout'
#     #             idx_examine = 100
#     #
#     #         time.sleep(TIME_INTERVAL)
#
#     def send_barcodes_to_clients(self):
#         """
#         p_mainWindow.modules_Left/Right 자료를 파싱해 각 writecard(1~4)별로
#         바코드 정보를 '한 번에' 딕셔너리로 전송한다.
#
#         변경 사항(UDP):
#         - 연결/소켓 캐시 확인 없이 sysInfo.xml 기반 주소 해석 함수(_get_bank_addr_from_config)를 사용해
#           각 Bank(1~4)의 (ip, port)로 직접 UDP 전송한다.
#         """
#         # writecard → Bank 번호 매핑
#         writecard_bank_no = {
#             'writecard1': 1,
#             'writecard2': 2,
#             'writecard3': 3,
#             'writecard4': 4,
#         }
#
#         # Left/Right에서 수집한 항목을 writecard 단위로 합치기
#         combined_by_writecard: dict[str, list[tuple[int, object]]] = {}
#
#         def collect_from_modules(modules_dict):
#             for module_key, v in modules_dict.items():
#                 try:
#                     # v: [ready_flag, writecardN, <여기(2)에 값>, ...]
#                     if not isinstance(v, (list, tuple)) or len(v) < 3:
#                         continue
#                     writecard = v[1]
#                     val_3rd = v[2]  # 3번째 값(index 2) 전송 요구사항
#                     # 원본 모듈 번호를 파싱해서 정렬 기준으로 사용
#                     mk_num = 999999
#                     try:
#                         mk_str = str(module_key)
#                         if mk_str.startswith('Module'):
#                             mk_num = int(mk_str.replace('Module', '').strip())
#                     except Exception:
#                         mk_num = 999999
#                     combined_by_writecard.setdefault(writecard, []).append((mk_num, val_3rd))
#                 except Exception:
#                     # 안전장치: 한 모듈에서 문제가 생겨도 전체 전송은 계속
#                     continue
#
#         # Left/Right 모두 수집
#         try:
#             collect_from_modules(self.p_mainWindow.modules_Left)
#         except Exception:
#             pass
#         try:
#             collect_from_modules(self.p_mainWindow.modules_Right)
#         except Exception:
#             pass
#
#         # 각 writecard 그룹에 대해 한 번씩 딕셔너리 전송
#         for writecard, items in combined_by_writecard.items():
#             bank_no = writecard_bank_no.get(writecard)
#             if not bank_no:
#                 continue
#
#             # sysInfo.xml 기반으로 Bank 주소 해석
#             addr = self._get_bank_addr_from_config(bank_no)
#             if not addr:
#                 self.signalMessage.emit(self.objectName(), 'ui', {
#                     'msg': f"[Warning] Bank{bank_no} address not found. Skip barcode send."
#                 })
#                 continue
#
#             # 원본 모듈 번호 기준 정렬 후, Bank마다 'Module1'부터 재번호
#             items.sort(key=lambda x: x[0])
#             payload = {}
#             for idx, (_orig_mk_num, val) in enumerate(items, 1):
#                 payload[f"Module{idx}"] = val
#
#             # 요구 형식: "barcode_info: { ... }"
#             msg = f"barcode_info: {payload}"
#
#             # UDP 전송
#             ok = self.writeCard.send_data(client_socket=addr, data=msg)
#             if ok:
#                 print(f"[send_barcodes_to_clients] Sent to Bank{bank_no} ({writecard}) @ {addr}: {msg}")
#                 self.signalMessage.emit(self.objectName(), 'ui', {
#                     'msg': f"Sent barcode_info to Bank{bank_no} ({addr[0]}:{addr[1]})"
#                 })
#             else:
#                 print(f"[send_barcodes_to_clients] Failed to send to Bank{bank_no} ({writecard}) @ {addr}")
#                 self.signalMessage.emit(self.objectName(), 'ui', {
#                     'msg': f"[Error] Failed to send barcode_info to Bank{bank_no} ({addr[0]}:{addr[1]})"
#                 })
#
#
#     # def send_barcodes_to_clients(self):
#     #     """
#     #     p_mainWindow.modules_Left/Right 자료를 파싱해 각 writecard(1~4)별로
#     #     바코드 정보를 '한 번에' 딕셔너리로 전송한다.
#     #
#     #     변경 사항:
#     #     - Bank(=writecard)마다 전송 키는 항상 'Module1'부터 시작하여 재번호 부여한다.
#     #       (원본 모듈 키가 Module6~10이어도, Bank2로 보내는 데이터는 'Module1'부터 시작)
#     #     - value는 각 모듈 value 리스트의 3번째 값(index 2)을 사용한다.
#     #     - 메시지 형식: "barcode_info: { 'Module1': <v[2]>, 'Module2': <v[2]>, ... }"
#     #     - 클라이언트 매핑은 기존과 동일:
#     #         writecard1 -> Bank1
#     #         writecard2 -> Bank2
#     #         writecard3 -> Bank3
#     #         writecard4 -> Bank4
#     #     """
#     #     # 기존과 동일한 writecard -> Bank 매핑
#     #     writecard_bank_map = {
#     #         'writecard1': 'Bank1',
#     #         'writecard2': 'Bank2',
#     #         'writecard3': 'Bank3',
#     #         'writecard4': 'Bank4',
#     #     }
#     #
#     #     # 연결 소켓 조회
#     #     clients_info = util_base.get_xml_info(self.baseDir, 'clients')
#     #     sockets = self.writeCard.get_connected_sockets(clients_info)  # { 'Bank1': socket, ... }
#     #
#     #     # Left/Right에서 수집한 항목을 writecard 단위로 합치기
#     #     combined_by_writecard: dict[str, list[tuple[int, object]]] = {}
#     #
#     #     def collect_from_modules(modules_dict):
#     #         for module_key, v in modules_dict.items():
#     #             try:
#     #                 # v: [ready_flag, writecardN, <여기(2)에 값>, ...]
#     #                 if not isinstance(v, (list, tuple)) or len(v) < 3:
#     #                     continue
#     #                 writecard = v[1]
#     #                 val_3rd = v[2]  # 3번째 값(index 2) 전송 요구사항
#     #                 # 원본 모듈 번호를 파싱해서 정렬 기준으로 사용
#     #                 mk_num = 999999
#     #                 try:
#     #                     mk_str = str(module_key)
#     #                     if mk_str.startswith('Module'):
#     #                         mk_num = int(mk_str.replace('Module', '').strip())
#     #                 except Exception:
#     #                     mk_num = 999999
#     #                 combined_by_writecard.setdefault(writecard, []).append((mk_num, val_3rd))
#     #             except Exception:
#     #                 # 안전장치: 한 모듈에서 문제가 생겨도 전체 전송은 계속
#     #                 continue
#     #
#     #     # Left/Right 모두 수집
#     #     try:
#     #         collect_from_modules(self.p_mainWindow.modules_Left)
#     #     except Exception:
#     #         pass
#     #     try:
#     #         collect_from_modules(self.p_mainWindow.modules_Right)
#     #     except Exception:
#     #         pass
#     #
#     #     # 각 writecard 그룹에 대해 한 번씩 딕셔너리 전송
#     #     for writecard, items in combined_by_writecard.items():
#     #         bank = writecard_bank_map.get(writecard)
#     #         if not bank:
#     #             continue
#     #         client_socket = sockets.get(bank)
#     #         if not client_socket:
#     #             continue
#     #
#     #         # 원본 모듈 번호 기준 정렬 후, Bank마다 'Module1'부터 재번호
#     #         items.sort(key=lambda x: x[0])
#     #         payload = {}
#     #         for idx, (_orig_mk_num, val) in enumerate(items, 1):
#     #             payload[f"Module{idx}"] = val
#     #
#     #         # 요구 형식: "barcode_info: { ... }"
#     #         msg = f"barcode_info: {payload}"
#     #
#     #         # 한 writecard(=한 Bank)에 대해 한 번만 전송
#     #         self.writeCard.send_data(client_socket, msg)
#     #         print(f'Send barcode: {client_socket}, {msg}')
#     #
#     #         # 디버그 로그
#     #         try:
#     #             print(f"[send_barcodes_to_clients] Sent to {bank} ({writecard}): {msg}")
#     #         except Exception:
#     #             pass
#
#     def send_signal_to_clients(self, msg=None):
#         """
#         Bank1~4에 동일한 신호(msg)를 전송.
#         - UDP: _get_bank_addr_from_config(bank_no)로 각 Bank의 (ip, port)를 구해 직접 전송
#         """
#         if msg is None:
#             return
#
#         for bank_no in (1, 2, 3, 4):
#             addr = self._get_bank_addr_from_config(bank_no)
#             if not addr:
#                 self.signalMessage.emit(self.objectName(), 'ui',
#                                         {'msg': f"[Warning] Bank{bank_no} address not found. Could not send '{msg}'."})
#                 continue
#
#             ok = self.writeCard.send_data(client_socket=addr, data=msg)
#             if ok:
#                 print(f"[send_signal_to_clients] Sent '{msg}' to Bank{bank_no} @ {addr}")
#             else:
#                 self.signalMessage.emit(self.objectName(), 'ui',
#                                         {
#                                             'msg': f"[Error] Failed to send '{msg}' to Bank{bank_no} ({addr[0]}:{addr[1]})"})
#
#
#
#     # def send_signal_to_clients(self, msg=None):
#     #     bank_names = ['Bank1', 'Bank2', 'Bank3', 'Bank4']
#     #     clients_info = util_base.get_xml_info(self.baseDir, 'clients')
#     #     sockets = self.writeCard.get_connected_sockets(clients_info)  # {Bank1: socket, ...}
#     #
#     #     for bank in bank_names:
#     #         client_socket = sockets.get(bank)
#     #         if client_socket:
#     #             self.writeCard.send_data(client_socket, msg)
#     #             print(f"sent '{msg}' to {bank}")
#
#     def _get_bank_addr_from_config(self, bank_number: int):
#         """
#         sysInfo.xml에서 지정한 Bank(1~5)의 ip/port를 읽어 ('ip', port) 형태로 반환.
#         - udpPort/port가 없으면 서버의 portNumber를 최후의 수단으로 폴백(경고 메시지 출력).
#         - 어떤 경우에도 포트를 특정할 수 없으면 None.
#         """
#         try:
#             sysinfo_path = os.path.join(self.baseDir, 'sysInfo.xml')
#             tree = ET.parse(sysinfo_path)
#             root = tree.getroot()
#             clients = root.find('clients')
#             if clients is None:
#                 return None
#
#             ip, port = None, None
#             for bank in clients.findall('bank'):
#                 if str(bank.get('number')) == str(bank_number):
#                     ip = bank.get('ip')
#                     port_str = bank.get('udpPort') or bank.get('port') or bank.get('udp_port')
#                     if port_str and str(port_str).isdigit():
#                         port = int(port_str)
#                     break
#
#             if not ip:
#                 return None
#
#             # 포트가 없으면 서버 포트로 폴백(주의: 대상 장치가 해당 포트에서 수신 중이어야 유효)
#             if port is None:
#                 try:
#                     server_info = util_base.get_xml_info(self.baseDir, 'server')
#                     fallback_port = int(server_info.get('portNumber')) if server_info and server_info.get(
#                         'portNumber') else None
#                 except Exception:
#                     fallback_port = None
#
#                 if fallback_port is not None:
#                     # 경고: 폴백 사용
#                     try:
#                         self.signalMessage.emit(self.objectName(), 'ui',
#                                                 {
#                                                     'msg': f"[Warning] Bank{bank_number} udpPort/port 미설정 → 서버 포트({fallback_port})로 폴백 전송 시도"})
#                     except Exception:
#                         pass
#                     return (ip, fallback_port)
#                 else:
#                     return None
#
#             return (ip, port)
#
#         except Exception as e:
#             print(f"[JobControl] Failed to read Bank{bank_number} from sysInfo.xml: {e}")
#             return None
#
#     def send_manual_to_bank5(self, msg: str):
#         """
#         Manual handling UI에서 전달된 msg 값을 Bank5(IO Board)로 전송.
#         - 1순위: ioBoard가 이미 서버로 패킷을 보냈다면, 그 주소 캐시로 전송(등록 방식).
#         - 2순위: sysInfo.xml의 Bank5 ip/port(없으면 서버 포트 폴백)로 전송.
#         - 실제 송신은 self.writeCard.send_data(UDP 소켓 보유 주체)를 사용.
#         """
#         try:
#             # 1) 등록 주소(수신 캐시) 우선
#             addr = None
#             try:
#                 sockets = self.ioBoard.get_connected_sockets()
#                 addr = sockets.get('Bank5')
#             except Exception:
#                 addr = None
#
#             # 2) 캐시가 없으면 구성 파일에서 주소 확보(필요 시 폴백 포함)
#             if not addr:
#                 addr = self._get_bank_addr_from_config(5)
#
#             if not addr:
#                 self.signalMessage.emit(self.objectName(), 'ui',
#                                         {
#                                             'msg': f"[Warning] IO Board (Bank5) address not found. Could not send '{msg}'."})
#                 return
#
#             ok = self.writeCard.send_data(client_socket=addr, data=msg)
#             if ok:
#                 self.signalMessage.emit(self.objectName(), 'ui',
#                                         {'msg': f"Sent manual command '{msg}' to IO Board (Bank5) via UDP"})
#             else:
#                 self.signalMessage.emit(self.objectName(), 'ui',
#                                         {'msg': f"[Error] Failed to send '{msg}' to IO Board (Bank5) via UDP"})
#
#         except Exception as e:
#             self.signalMessage.emit(self.objectName(), 'ui',
#                                     {'msg': f"[Error] Failed to send manual command '{msg}' to IO Board: {e}"})
#
#     # def send_manual_to_bank5(self, msg: str):
#     #     """
#     #     Manual handling UI에서 전달된 msg 값을 Bank5(IO Board)로 전송.
#     #     - UDP에서는 '연결' 개념 없이 설정된 목적지로 바로 송신 가능.
#     #     - 우선 캐시된 주소(ioBoard.get_connected_sockets())를 사용, 없으면 sysInfo.xml의 Bank5 주소로 송신.
#     #     - 실제 송신은 UDP 서버(self.writeCard.send_data)를 사용(소켓 보유 주체).
#     #     """
#     #     try:
#     #         # 1) 캐시 주소 시도(있으면 최우선)
#     #         addr = None
#     #         try:
#     #             sockets = self.ioBoard.get_connected_sockets()
#     #             addr = sockets.get('Bank5')
#     #         except Exception:
#     #             addr = None
#     #
#     #         # 2) 캐시가 없으면 sysInfo.xml의 Bank5 주소 사용
#     #         if not addr:
#     #             addr = self._get_bank5_addr_from_config()
#     #
#     #         if not addr:
#     #             # 목적지 해석 불가
#     #             self.signalMessage.emit(self.objectName(), 'ui',
#     #                                     {
#     #                                         'msg': f"[Warning] IO Board (Bank5) address not found. Could not send '{msg}'."})
#     #             return
#     #
#     #         # 3) UDP 송신: IOBoardServer는 소켓 참조가 없을 수 있으므로, 실제 UDP 소켓을 가진 writeCard 쪽으로 전송
#     #         ok = self.writeCard.send_data(client_socket=addr, data=msg)
#     #         if ok:
#     #             self.signalMessage.emit(self.objectName(), 'ui',
#     #                                     {'msg': f"Sent manual command '{msg}' to IO Board (Bank5) via UDP"})
#     #         else:
#     #             self.signalMessage.emit(self.objectName(), 'ui',
#     #                                     {'msg': f"[Error] Failed to send '{msg}' to IO Board (Bank5) via UDP"})
#     #
#     #     except Exception as e:
#     #         self.signalMessage.emit(self.objectName(), 'ui',
#     #                                 {'msg': f"[Error] Failed to send manual command '{msg}' to IO Board: {e}"})
#
#
#
#
#     # def send_manual_to_bank5(self, msg: str):
#     #     """
#     #     Manual handling UI에서 전달된 msg 값을 Bank5(IO Board)로 그대로 전송한다.
#     #     - msg 예: 'ManualPusherDown', 'ManualPusherUp', 'ManualPusherFront', 'ManualPusherBack'
#     #     - 연결이 없으면 UI에 경고 메시지 출력
#     #     """
#     #     try:
#     #         io_socket = self._get_io_socket_cached_or_lookup()
#     #         if io_socket:
#     #             self.ioBoard.send_data(client_socket=io_socket, data=msg)
#     #             self.signalMessage.emit(self.objectName(), 'ui',
#     #                                     {'msg': f"Sent manual command '{msg}' to IO Board (Bank5)"})
#     #         else:
#     #             self.signalMessage.emit(self.objectName(), 'ui',
#     #                                     {
#     #                                         'msg': f"[Warning] IO Board (Bank5) is not connected. Could not send '{msg}'."})
#     #     except Exception as e:
#     #         self.signalMessage.emit(self.objectName(), 'ui',
#     #                                 {'msg': f"[Error] Failed to send manual command '{msg}' to IO Board: {e}"})
#
#     def update_sensorID_from_client(self, Writecard_num, sensor_ID):
#         """
#         클라이언트(writecardN)에서 수신한 sensor_ID(dict)를 job_modules_Left/Right에 반영한다.
#
#         - settings.xml의 <carrier columns="...">가 1이면:
#           모든 writecard(1~4)의 데이터를 job_modules_Left에 순서대로 채운다.
#           (make_dictionary에서 writecard1→2→3→4 순서대로 job_modules_Left에 섹션이 이미 구성되어 있음)
#
#         - columns가 2 이상이면(기본):
#           Left ← writecard1,2 / Right ← writecard3,4 에 매핑한다.
#
#         - 클라이언트가 보내는 딕셔너리의 키는 'ModuleN' 또는 'sensorN' 형태 모두 지원.
#           키의 숫자 오름차순으로 정렬하여 해당 writecard 섹션의 모듈에 순서대로 매핑한다.
#
#         - 매핑 위치:
#           각 모듈 value 리스트의 세 번째 인덱스(2)에 센서(FAKEID) 문자열 저장.
#
#         - 각 카드 데이터 도착 시 self.clsInfo['sensor_dataN'] flag를 True로 설정.
#         """
#         # 1) carrier columns 확인
#         try:
#             carrier_cols = self._get_carrier_columns()
#         except Exception:
#             carrier_cols = 2
#
#         # 2) 대상 dict, card-key, 플래그 결정
#         if Writecard_num == 'Write Card 1':
#             target_dict = self.job_modules_Left if carrier_cols == 1 else self.job_modules_Left
#             card_key = 'writecard1'
#             flag_key = 'sensor_data1'
#         elif Writecard_num == 'Write Card 2':
#             target_dict = self.job_modules_Left if carrier_cols == 1 else self.job_modules_Left
#             card_key = 'writecard2'
#             flag_key = 'sensor_data2'
#         elif Writecard_num == 'Write Card 3':
#             target_dict = self.job_modules_Left if carrier_cols == 1 else self.job_modules_Right
#             card_key = 'writecard3'
#             flag_key = 'sensor_data3'
#         elif Writecard_num == 'Write Card 4':
#             target_dict = self.job_modules_Left if carrier_cols == 1 else self.job_modules_Right
#             card_key = 'writecard4'
#             flag_key = 'sensor_data4'
#         else:
#             raise ValueError(f"알 수 없는 Writecard_num: {Writecard_num}")
#
#         # 3) 해당 writecard 섹션의 모듈 키만 추출 후 Module 번호 기준 정렬
#         module_keys = [k for k, v in target_dict.items() if len(v) > 1 and v[1] == card_key]
#         try:
#             module_keys.sort(key=lambda mk: int(mk.replace('Module', '')) if mk.startswith('Module') else 9999)
#         except Exception:
#             # 문제가 생겨도 순서 유지
#             pass
#
#         # 4) 수신 데이터 정렬: 'ModuleN' 또는 'sensorN' 키 모두 지원
#         def _index_from_key(s: str) -> int:
#             try:
#                 if s.startswith('Module'):
#                     suf = s[6:]
#                     return int(suf) if suf.isdigit() else 9999
#                 if s.lower().startswith('sensor'):
#                     suf = s[len('sensor'):]
#                     return int(suf) if suf.isdigit() else 9999
#             except Exception:
#                 pass
#             return 9999
#
#         try:
#             sorted_sensor_items = sorted(sensor_ID.items(), key=lambda kv: _index_from_key(str(kv[0])))
#         except Exception:
#             # 정렬 실패 시, 기존 순서 그대로 사용(OrderedDict이면 입력 순서 보존)
#             sorted_sensor_items = list(sensor_ID.items())
#
#         # 5) 매칭 저장: writecard 섹션(module_keys) ↔ 수신 항목(sorted_sensor_items) 순서대로
#         for module_key, (recv_key, sensor_val) in zip(module_keys, sorted_sensor_items):
#             target_dict[module_key][2] = sensor_val  # 세 번째 값에 저장
#
#         # 6) 플래그 설정
#         self.clsInfo[flag_key] = True
#
#         # 7) 디버그 출력
#         print(f'self.job_modules_Left: {self.job_modules_Left}')
#         print(f'self.job_modules_Right: {self.job_modules_Right}')
#         print(f"self.clsInfo flags: {[self.clsInfo.get(f'sensor_data{i}') for i in range(1, 5)]}")
#
#
#     def update_barcode_from_client(self, Writecard_num, barcode_info):
#         """
#         클라이언트(writecardN)에서 수신한 barcode_info를 job_modules_Left/Right에 반영한다.
#
#         - settings.xml의 <carrier columns="...">가 1이면:
#           모든 writecard(1~4)의 데이터를 job_modules_Left에 순서대로 채운다.
#           (make_dictionary에서 writecard1→2→3→4 순서대로 job_modules_Left에 섹션이 이미 구성되어 있음)
#
#         - columns가 2 이상이면(기본):
#           Left ← writecard1,2 / Right ← writecard3,4 에 매핑한다.
#
#         - 클라이언트가 보내는 딕셔너리의 키는 'ModuleN' 또는 'barcodeN' 형태 모두 지원.
#           키의 숫자 오름차순으로 정렬하여 해당 writecard 섹션의 모듈에 순서대로 매핑한다.
#
#         - 매핑 위치:
#           각 모듈 value 리스트의 네 번째 인덱스(3)에 바코드 문자열 저장.
#
#         - 각 카드 데이터 도착 시 self.clsInfo['barcode_dataN'] flag를 True로 설정.
#         """
#         # 1) carrier columns 확인
#         try:
#             carrier_cols = self._get_carrier_columns()
#         except Exception:
#             carrier_cols = 2
#
#         # 2) 대상 dict, card-key, 플래그 결정
#         if Writecard_num == 'Write Card 1':
#             target_dict = self.job_modules_Left if carrier_cols == 1 else self.job_modules_Left
#             card_key = 'writecard1'
#             flag_key = 'barcode_data1'
#         elif Writecard_num == 'Write Card 2':
#             target_dict = self.job_modules_Left if carrier_cols == 1 else self.job_modules_Left
#             card_key = 'writecard2'
#             flag_key = 'barcode_data2'
#         elif Writecard_num == 'Write Card 3':
#             target_dict = self.job_modules_Left if carrier_cols == 1 else self.job_modules_Right
#             card_key = 'writecard3'
#             flag_key = 'barcode_data3'
#         elif Writecard_num == 'Write Card 4':
#             target_dict = self.job_modules_Left if carrier_cols == 1 else self.job_modules_Right
#             card_key = 'writecard4'
#             flag_key = 'barcode_data4'
#         else:
#             raise ValueError(f"알 수 없는 Writecard_num: {Writecard_num}")
#
#         # 3) 해당 writecard 섹션의 모듈 키만 추출 후 Module 번호 기준 정렬
#         module_keys = [k for k, v in target_dict.items() if len(v) > 1 and v[1] == card_key]
#         try:
#             module_keys.sort(key=lambda mk: int(mk.replace('Module', '')) if mk.startswith('Module') else 9999)
#         except Exception:
#             # 문제가 생겨도 순서 유지
#             pass
#
#         # 4) 수신 데이터 정렬: 'ModuleN' 또는 'barcodeN' 키 모두 지원
#         def _index_from_key(s: str) -> int:
#             try:
#                 if s.startswith('Module'):
#                     suf = s[6:]
#                     return int(suf) if suf.isdigit() else 9999
#                 if s.startswith('barcode'):
#                     suf = s[7:]
#                     return int(suf) if suf.isdigit() else 9999
#             except Exception:
#                 pass
#             return 9999
#
#         try:
#             sorted_barcode_items = sorted(barcode_info.items(), key=lambda kv: _index_from_key(str(kv[0])))
#         except Exception:
#             # 정렬 실패 시, 기존 순서 그대로 사용(OrderedDict이면 입력 순서 보존)
#             sorted_barcode_items = list(barcode_info.items())
#
#         # 5) 매칭 저장: writecard 섹션(module_keys) ↔ 수신 항목(sorted_barcode_items) 순서대로
#         for module_key, (recv_key, barcode_val) in zip(module_keys, sorted_barcode_items):
#             target_dict[module_key][3] = barcode_val  # 네 번째 값에 저장
#
#         # 6) 플래그 설정
#         self.clsInfo[flag_key] = True
#
#         # 7) 디버그 출력
#         print(f'self.job_modules_Left: {self.job_modules_Left}')
#         print(f'self.job_modules_Right: {self.job_modules_Right}')
#         print(f"self.clsInfo barcode flags: {[self.clsInfo.get(f'barcode_data{i}') for i in range(1, 5)]}")
#
#     def reset_job_context(self, reason: str | None = None):
#         """
#         모델/고객사 변경 등으로 JobController 상태 초기화.
#         IO 보드 초기화 전송을 UDP 주소 해석 → self.writeCard.send_data로 수행.
#         """
#         try:
#             if getattr(self, "_test_thread", None) is not None and self._test_thread.is_alive():
#                 self.clsInfo["force_abort"] = True
#                 self.clsInfo["is_abortTest"] = True
#                 self.clsInfo["is_examine"] = False
#                 self.clsInfo["barcode_stop"] = True
#
#                 # [변경] 소켓 캐시 대신 주소 해석 후 직접 송신
#                 try:
#                     addr = None
#                     try:
#                         sockets = self.ioBoard.get_connected_sockets()
#                         addr = sockets.get('Bank5')
#                     except Exception:
#                         addr = None
#                     if not addr:
#                         addr = self._get_bank_addr_from_config(5)
#
#                     if addr:
#                         self.writeCard.send_data(client_socket=addr, data="ManualPusherInitial")
#                 except Exception:
#                     pass
#
#                 self._test_thread.join(timeout=1.0)
#         except Exception:
#             pass
#         finally:
#             self._test_thread = None
#
#         try:
#             self.job_modules_Left.clear()
#             self.job_modules_Right.clear()
#         except Exception:
#             self.job_modules_Left = {}
#             self.job_modules_Right = {}
#
#         self.left_ready_printed = False
#         self.right_ready_printed = False
#         self._barcode_read_requested = False
#         self.io_socket = None  # IO 캐시 제거
#
#         was_initialized = bool(self.clsInfo.get("is_initialized"))
#         keys = list(self.clsInfo.keys())
#         for k in keys:
#             v = self.clsInfo.get(k)
#             if k in ("pusher_down_ts", "button_unpushed_ts"):
#                 self.clsInfo[k] = None
#             elif isinstance(v, bool):
#                 self.clsInfo[k] = False
#             elif isinstance(v, (int, float)):
#                 self.clsInfo[k] = 0
#             elif isinstance(v, dict):
#                 self.clsInfo[k] = {}
#             else:
#                 self.clsInfo[k] = False
#
#         force_false_keys = [
#             "is_examine", "is_abortTest",
#             "Writecard 1 Ready", "Writecard 2 Ready", "Writecard 3 Ready", "Writecard 4 Ready",
#             "Left_Recon_Script", "Right_Recon_Script",
#             "IOBoard_Ready",
#             "1st_image_scan", "1st_left_barcode", "1st_right_barcode",
#             "2nd_barcode",
#             "3rd_shot", "3rd_left_barcode", "3rd_right_barcode",
#             "sensor_data1", "sensor_data2", "sensor_data3", "sensor_data4",
#             "barcode_data1", "barcode_data2", "barcode_data3", "barcode_data4",
#             "pusher back", "pusher_down_started", "pusher_down_finished",
#             "button_unpushed", "pusher_sequence_decided", "early_button_unpushed",
#             "barcode_stop", "force_abort",
#             "Scan Stop",
#             "2nd show update",
#             "sensor_dict update", "sensor_dic update",
#         ]
#         for k in force_false_keys:
#             self.clsInfo[k] = False
#
#         self.clsInfo["writeCard_states"] = {}
#         self.clsInfo["pusher_down_ts"] = None
#         self.clsInfo["button_unpushed_ts"] = None
#
#         if was_initialized:
#             self.clsInfo["is_initialized"] = True
#
#         try:
#             if self.p_mainWindow:
#                 self.settings_xml_info = os.path.join(
#                     self.baseDir, "models",
#                     self.p_mainWindow.cb_customerName.currentText(),
#                     self.p_mainWindow.cb_selectedModel.currentText(),
#                     "settings.xml"
#                 )
#         except Exception:
#             self.settings_xml_info = None
#
#         try:
#             msg_txt = f"[JobControl] Reset job context ({reason})" if reason else "[JobControl] Reset job context"
#             self.signalMessage.emit(self.objectName(), "ui", {"msg": msg_txt})
#         except Exception:
#             pass
#
#
#     # def reset_job_context(self, reason: str | None = None):
#     #     """
#     #     모델/고객사 변경 등으로 JobController 상태 초기화.
#     #     IO 보드 초기화 전송 경로를 self.ioBoard로 변경.
#     #     """
#     #     try:
#     #         if getattr(self, "_test_thread", None) is not None and self._test_thread.is_alive():
#     #             self.clsInfo["force_abort"] = True
#     #             self.clsInfo["is_abortTest"] = True
#     #             self.clsInfo["is_examine"] = False
#     #             self.clsInfo["barcode_stop"] = True
#     #
#     #             try:
#     #                 io_socket = self._get_io_socket_cached_or_lookup()
#     #                 if io_socket:
#     #                     self.ioBoard.send_data(client_socket=io_socket, data="ManualPusherInitial")
#     #             except Exception:
#     #                 pass
#     #
#     #             self._test_thread.join(timeout=1.0)
#     #     except Exception:
#     #         pass
#     #     finally:
#     #         self._test_thread = None
#     #
#     #     # job 모듈 딕셔너리 초기화
#     #     try:
#     #         self.job_modules_Left.clear()
#     #         self.job_modules_Right.clear()
#     #     except Exception:
#     #         self.job_modules_Left = {}
#     #         self.job_modules_Right = {}
#     #
#     #     self.left_ready_printed = False
#     #     self.right_ready_printed = False
#     #     self._barcode_read_requested = False
#     #     self.io_socket = None  # IO 캐시 제거
#     #
#     #     was_initialized = bool(self.clsInfo.get("is_initialized"))
#     #     keys = list(self.clsInfo.keys())
#     #     for k in keys:
#     #         v = self.clsInfo.get(k)
#     #         if k in ("pusher_down_ts", "button_unpushed_ts"):
#     #             self.clsInfo[k] = None
#     #         elif isinstance(v, bool):
#     #             self.clsInfo[k] = False
#     #         elif isinstance(v, (int, float)):
#     #             self.clsInfo[k] = 0
#     #         elif isinstance(v, dict):
#     #             self.clsInfo[k] = {}
#     #         else:
#     #             self.clsInfo[k] = False
#     #
#     #     force_false_keys = [
#     #         "is_examine", "is_abortTest",
#     #         "Writecard 1 Ready", "Writecard 2 Ready", "Writecard 3 Ready", "Writecard 4 Ready",
#     #         "Left_Recon_Script", "Right_Recon_Script",
#     #         "IOBoard_Ready",
#     #         "1st_image_scan", "1st_left_barcode", "1st_right_barcode",
#     #         "2nd_barcode",
#     #         "3rd_shot", "3rd_left_barcode", "3rd_right_barcode",
#     #         "sensor_data1", "sensor_data2", "sensor_data3", "sensor_data4",
#     #         "barcode_data1", "barcode_data2", "barcode_data3", "barcode_data4",
#     #         "pusher back", "pusher_down_started", "pusher_down_finished",
#     #         "button_unpushed", "pusher_sequence_decided", "early_button_unpushed",
#     #         "barcode_stop", "force_abort",
#     #         "Scan Stop",
#     #         "2nd show update",
#     #         "sensor_dict update", "sensor_dic update",
#     #     ]
#     #     for k in force_false_keys:
#     #         self.clsInfo[k] = False
#     #
#     #     self.clsInfo["writeCard_states"] = {}
#     #     self.clsInfo["pusher_down_ts"] = None
#     #     self.clsInfo["button_unpushed_ts"] = None
#     #
#     #     if was_initialized:
#     #         self.clsInfo["is_initialized"] = True
#     #
#     #     try:
#     #         if self.p_mainWindow:
#     #             self.settings_xml_info = os.path.join(
#     #                 self.baseDir, "models",
#     #                 self.p_mainWindow.cb_customerName.currentText(),
#     #                 self.p_mainWindow.cb_selectedModel.currentText(),
#     #                 "settings.xml"
#     #             )
#     #     except Exception:
#     #         self.settings_xml_info = None
#     #
#     #     try:
#     #         msg_txt = f"[JobControl] Reset job context ({reason})" if reason else "[JobControl] Reset job context"
#     #         self.signalMessage.emit(self.objectName(), "ui", {"msg": msg_txt})
#     #     except Exception:
#     #         pass
#     #
#
#     def UDPTest(self):
#         """
#         Send 'UDPTest' to Writecard1~4 (Bank1~4) via UDP.
#         Uses sysInfo.xml configuration through send_signal_to_clients.
#         """
#         try:
#             self.send_signal_to_clients('UDPTest')
#             self.signalMessage.emit(self.objectName(), 'ui', {'msg': "Sent 'UDPTest' to Banks 1-4"})
#         except Exception as e:
#             try:
#                 self.signalMessage.emit(self.objectName(), 'ui', {'msg': f"[Error] UDPTest failed: {e}"})
#             except Exception:
#                 pass
