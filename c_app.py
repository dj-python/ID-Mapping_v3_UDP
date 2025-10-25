# import sys
# import os
#
# if getattr(sys, 'frozen', False):
#     baseDir = sys._MEIPASS
# else:
#     baseDir = os.path.dirname(__file__)
#
# if getattr(sys, 'frozen', False):
#     dll_dir = os.path.join(sys._MEIPASS, "pylibdmtx")
#     os.environ["PATH"] = dll_dir + os.pathsep + os.environ["PATH"]

import platform
from PySide6 import QtCore
from PySide6.QtWidgets import QApplication
import datetime
import c_mainwindow
import c_jobControl
import xml.etree.ElementTree as elemenTree
import re
import time

appVer = '0.1'

import os
baseDir = os.path.dirname(__file__)
sysInfo = elemenTree.parse(os.path.join(baseDir, 'sysInfo.xml')).getroot()

c_modules_Left = {}
c_modules_Right = {}

# Persistent camera connection settings
CAMERA_PERSISTENT = True
_camera_connected = {'Left': False, 'Right': False}
_FIRST_SHOT_DONE = {'Left': False, 'right': False}

def _connect_camera(side: str):
    # Call UI connect handlers and set flags
    try:
        if side == 'Left':
            mainWindow.on_pb_Camera_Connect_Left_clicked()
            _camera_connected['Left'] = True
            print('[Camera] Left connect requested (persistent)')
        elif side == 'Right':
            mainWindow.on_pb_Camera_Connect_Right_clicked()
            _camera_connected['Right'] = True
            print('[Camera] Right connect requested (persistent)')
    except Exception as e:
        print(f'[Camera] Failed to connect {side}: {e}')

def _ensure_camera_connected(side: str):
    if not _camera_connected.get(side, False):
        _connect_camera(side)


def _capture_side(side: str):
    if CAMERA_PERSISTENT:
        _ensure_camera_connected(side)
    try:
        # 촬영 요청
        mainWindow._save_barcode_image(side)
        # NOTE:
        # - 동기(Blocking) 촬영이라면, 이 시점에서 이미 저장 완료 상태이므로 아래에서 Done 플래그를 바로 True로 세팅해도 됨.
        # - 비동기(Non-Blocking) 촬영이라면, 아래의 True 세팅을 제거하고,
        #   mainWindow._save_barcode_image 내부(또는 그 함수가 트리거하는 저장 완료 시그널의 슬롯)에서
        #   _FIRST_SHOT_DONE[side] = True, jobControl.clsInfo['left/right_capture_done']=True 를 세팅하세요.
        _FIRST_SHOT_DONE[side] = True
        if side == 'Left':
            jobControl.clsInfo['left_capture_done'] = True
        else:
            jobControl.clsInfo['right_capture_done'] = True
    except Exception as e:
        print(f'[Camera] Capture failed on {side}, retrying with connect: {e}')
        _connect_camera(side)
        mainWindow._save_barcode_image(side)
        _FIRST_SHOT_DONE[side] = True
        if side == 'Left':
            jobControl.clsInfo['left_capture_done'] = True
        else:
            jobControl.clsInfo['right_capture_done'] = True

def _capture_both():
    # 촬영 시작 전 초기화
    _FIRST_SHOT_DONE['Left'] = False
    _FIRST_SHOT_DONE['Right'] = False
    jobControl.clsInfo['left_capture_done'] = False
    jobControl.clsInfo['right_capture_done'] = False

    # 순차 촬영(필요 시 병렬화 가능하나, 하드웨어/스레딩 안전성 검토 필요)
    _capture_side("Left")
    _capture_side("Right")


@QtCore.Slot(str, str, dict)
def slotParse(objName, msgType, values):
    date_time = datetime.datetime.now().strftime("%y-%m-%d %H:%M:%S")
    global c_modules_Left, c_modules_Right

    if objName == 'mainWindow':
        mainWindow.update_text_browser(objName, msgType, values)
        if msgType == 'job':
            if values['msg'] == 'UDPTest':
                jobControl.UDPTest()
            if values['msg'] == 'UDPTest_2':
                jobControl.send_scripts_to_clients()
            if values['msg'] == 'ManualTestStart':
                jobControl.slotManualTestStart()
            if values['msg'] == 'getReady':
                jobControl.send_manual_to_bank5('ManualPusherInitial')
                selected_family = values.get('family', '')
                selected_model = values.get('model', '')
                jobControl.make_dictionary(baseDir=baseDir, selected_family=selected_family, selected_model=selected_model)
            # if values['msg'] == 'send script to clients':
            #     jobControl.send_scripts_to_clients()
            # if values['msg'] == 'Pusher front':
            #     try:
            #         jobControl.send_manual_to_bank5(values.get('msg'))
            #     except Exception as e:
            #         mainWindow.update_text_browser(objName='jobManager', msgType='ui',
            #                                        values={'msg':f"[Error] Faled to send command: {str(e)}"})
            #     return

                # Persistent camera: connect once at getReady

                # 신규 추가
                if CAMERA_PERSISTENT:
                    _ensure_camera_connected('Left')
                    _ensure_camera_connected('Right')
                # 신규 추가 end

            elif 'msg' in values and values['msg'] == 'Scan Stop':
                jobControl.clsInfo['barcode_stop'] = True
            elif 'msg' in values and values['msg'] == 'Scan Failed':
                jobControl.clsInfo['barcode_stop'] = True
            elif 'msg' in values and values['msg'] == 'model changed':
                jobControl.reset_job_context()

            elif values.get('where') == 'Manual handling':
                try:
                    jobControl.send_manual_to_bank5(values.get('msg'))
                except Exception as e:
                    mainWindow.update_text_browser(objName='jobManager', msgType='ui',
                                                   values={'msg':f"[Error] Failed to manual command: {str(e)}"})
                return

            elif values['msg'] == 'Ready_pushed':
                pass

        elif msgType == 'ui':
            mainWindow.update_text_browser(objName=objName, msgType=msgType, values=values)

    elif objName == 'writeCard':
        mainWindow.update_text_browser(objName=objName, msgType=msgType, values=values)
        print('mainWindow 1 signal')
        mainWindow.update_module_array_ui(values=values)
        print('mainWindow 2 signal')


    elif objName == 'jobManager':
        # region 클라이언트 연결/끊김과 관련된 Main UI 로그 업데이트
        if msgType == 'ui':
            mainWindow.update_text_browser(objName=objName, msgType=msgType, values=values)
        if msgType == 'connection':
            msg = values.get('msg', '')
            if values.get('where','').startswith('Bank'):
                if re.search(r'\bdisconnected\b', msg):
                    mainWindow.update_text_browser(objName=objName, msgType=msgType, values=values, color='red')
                    mainWindow.update_table_widget(objName, msgType, values)
                    if values['where'] == 'Bank 5':
                        mainWindow.lbl_IOBoard.setText(
                            f'<font color=red>IO Board disconnected</font>&nbsp;&nbsp;&nbsp;&nbsp;')
                    else:
                        mainWindow.lbl_usable_idMapping.setText(
                            f'<font color=red>Write Cards: Awaiting...</font>&nbsp;&nbsp;&nbsp;&nbsp;')
                elif re.search(r'\bconnected\b', msg):
                    mainWindow.update_text_browser(objName=objName, msgType=msgType, values=values, color='blue')
                    mainWindow.update_table_widget(objName, msgType, values)
                    if values['where'] == 'Bank 5':
                        mainWindow.lbl_IOBoard.setText(
                            f'<font color=blue>IO Board connected</font>&nbsp;&nbsp;&nbsp;&nbsp;')
            else:
                # Camera connection state sync (for logs like "Left Camera connected")
                if msg == 'Left Camera connected':
                    _camera_connected['Left'] = True
                elif msg == 'Right Camera connected':
                    _camera_connected['Right'] = True
                elif msg == 'Left Camera disconnected':
                    _camera_connected['Left'] = False
                elif msg == 'Right Camera disconnected':
                    _camera_connected['Right'] = False

            if msg == 'All Write Cards connected.':
                mainWindow.lbl_usable_idMapping.setText(
                    f'<font color=blue>Write Cards all connected</font>&nbsp;&nbsp;&nbsp;&nbsp;')
                mainWindow.pb_getReady.setEnabled(True)
        elif msgType == 'client':
            mainWindow.update_text_browser(objName=objName, msgType=msgType, values=values, color=None)
        # end region

        # region 검사 실행
        elif msgType == 'job':
            if 'msg' in values and values['msg'] == 'Script all loaded':
                mainWindow.updateScriptLoaded()
            if 'reasonOfFail' in values and values['reasonOfFail']:
                mainWindow.show_notice(values['reasonOfFail'], title='Fail', status='NG')
            elif 'msg' in values and values['msg'] == 'STStart':
                mainWindow.control_timer(action='STStart')
            elif 'msg' in values and values['msg'] == 'STStop':
                mainWindow.control_timer(action='STStop')
                mainWindow.pb_ManualPusherBack.setEnabled(True)
                mainWindow.pb_ManualPusherDown.setEnabled(True)
                mainWindow.pb_ManualPusherFront.setEnabled(True)
                mainWindow.pb_ManualPusherUp.setEnabled(True)
                # 선택사항: 종료 시 카메라를 유지하려면 아무 것도 하지 않음
                # if not CAMERA_PERSISTENT:
                #     mainWindow.on_pb_Camera_Disconnect_Left_clicked()
                #     mainWindow.on_pb_Camera_Disconnect_Right_clicked()
                #     _camera_connected['Left'] = False
                #     _camera_connected['Right'] = False
            elif 'msg' in values and values['msg'] == 'Scan Stop':
                jobControl.clsInfo['barcode_stop'] = True
            elif 'msg' in values and values['msg'] == 'Scan Failed':
                jobControl.clsInfo['barcode_stop'] = True
            elif 'msg' in values and values['msg'] == 'Mapping start':
                mainWindow.update_text_browser(objName=objName, msgType=msgType, values=values)
                mainWindow.clear_barcode_compare_table()
                mainWindow.dict_reset()
                mainWindow.update_ui_array(side='Left', step=0)
                mainWindow.pb_ManualPusherBack.setEnabled(False)
                mainWindow.pb_ManualPusherDown.setEnabled(False)
                mainWindow.pb_ManualPusherFront.setEnabled(False)
                mainWindow.pb_ManualPusherUp.setEnabled(False)

                _FIRST_SHOT_DONE['Left' ] = False
                _FIRST_SHOT_DONE['Right'] = False
                jobControl.clsInfo['left_capture_done'] = False
                jobControl.clsInfo['right_capture_done'] = False
                jobControl.clsInfo['1st_image_scan'] = False

            elif 'msg' in values and values['msg'] == 'Barcode read':
                mainWindow.update_text_browser(objName=objName, msgType=msgType, values=values)
                # Persistent: capture only (connect is ensured)
                if CAMERA_PERSISTENT:
                    _capture_both()
                else:
                    mainWindow._save_barcode_image("Left")
                    _FIRST_SHOT_DONE['Left'] = True
                    jobControl.clsInfo['left_capture_done'] = True

                    mainWindow._save_barcode_image("Right")
                    _FIRST_SHOT_DONE['Right'] = True
                    jobControl.clsInfo['right_capture_done'] = True

                if jobControl.clsInfo.get('left_capture_done') and jobControl.clsInfo.get('right_capture_done'):
                    jobControl.clsInfo['1st_image_scan'] = True


            elif 'msg' in values and values['msg'] == 'Pusher front':
                mainWindow.SaveAndScan_Left()
                mainWindow.Parse_Barcode_File(side='Left', value_order=3)
                mainWindow.update_ui_array(side='Left', step=2)
                mainWindow.open_barcode_compare()
                jobControl.clsInfo['1st_left_barcode'] = True

            elif 'msg' in values and values['msg'] == '1st_left_barcode_OK':
                mainWindow.SaveAndScan_Right()
                mainWindow.Parse_Barcode_File(side='Right', value_order=3)
                mainWindow.update_ui_array(side='Right', step=2)
                mainWindow.open_barcode_compare()
                jobControl.clsInfo['1st_right_barcode'] = True

            elif 'msg' in values and values['msg'] == '1st Scan OK':
                mainWindow.update_text_browser(objName=objName, msgType=msgType, values=values)

            elif 'msg' in values and values['msg'] == 'sensor ID recieved':
                get_sensorID_and_eepromData()
                update_mainwindow_dict()
                mainWindow.update_ui_array(side='Left', step=3)
                mainWindow.update_ui_array(side='Right', step=3)

            elif 'msg' in values and values['msg'] == '2nd barcode OK':
                get_sensorID_and_eepromData()
            elif 'msg' in values and values['msg'] == 'c_save finished':
                update_mainwindow_dict()
            elif 'msg' in values and values['msg'] == 'sensor_dict updated':
                mainWindow.update_ui_array(side='Left', step=4)
                mainWindow.update_ui_array(side='Right', step=4)
                mainWindow.open_barcode_compare()
                jobControl.clsInfo['2nd show update'] = True
            elif 'msg' in values and values['msg'] == '3rd Barcode shot':
                print('start 3rd shot')
                # Persistent: capture only
                if CAMERA_PERSISTENT:
                    _capture_both()
                else:
                    # mainWindow.on_pb_Camera_Connect_Left_clicked()
                    mainWindow._save_barcode_image("Left")
                    # mainWindow.on_pb_Camera_Connect_Right_clicked()
                    mainWindow._save_barcode_image("Right")
                jobControl.clsInfo['3rd_shot'] = True

            elif 'msg' in values and values['msg'] == '3rd left barcode':
                mainWindow.SaveAndScan_Left()
                mainWindow.Parse_Barcode_File(side='Left', value_order=6)
                mainWindow.update_ui_array(side='Left', step=5)
                mainWindow.open_barcode_compare()
                jobControl.clsInfo['3rd_left_barcode'] = True

            elif 'msg' in values and values['msg'] == '3rd_left_barcode_OK':
                mainWindow.SaveAndScan_Right()
                mainWindow.Parse_Barcode_File(side='Right', value_order=6)
                mainWindow.update_ui_array(side='Right', step=5)
                mainWindow.open_barcode_compare()
                jobControl.clsInfo['3rd_right_barcode'] = True

            elif 'msg' in values and values['msg'] == 'examine finished':
                mainWindow.update_text_browser(objName=objName, msgType=msgType, values=values)
                try:
                    ok = mainWindow.reset_modules_dict_from_settings()
                    if not ok:
                        print("[c_app] reset_modules_dict_from_settings returned False", flush=True)
                except Exception as e:
                    print(f"[c_app] Failed to reset modules dicts on 'examine finished': {e}", flush=True)
        # end region

        # elif msgType == 'client':
        #     mainWindow.update_module_array_ui(values=values)

        elif msgType == 'job':
            if values['where'] == 'pushButton':
                if values['msg'] == 'REPORT_USER_COMMAND':
                    pass
    mainWindow.update()


def get_sensorID_and_eepromData():
    """
    c_jobControl.JobController의 job_modules_Left/Right 딕셔너리 데이터를
    c_app.py의 c_modules_Left, c_modules_Right에 복사하여 저장하는 함수.
    - key 값과 value 전체를 그대로 복사(값은 얕은 복사)
    - 호출 예: get_sensorID_and_eepromData()
    """
    global c_modules_Left, c_modules_Right, jobControl

    # jobControl의 딕셔너리를 그대로 복사 (필요시 deepcopy로 변경 가능)
    c_modules_Left = jobControl.job_modules_Left.copy()
    c_modules_Right = jobControl.job_modules_Right.copy()
    jobControl.clsInfo['c_save'] = True





def update_mainwindow_dict():
    """
    c_modules_Left/Right의 value 리스트 3,4번째 데이터를 mainWindow.modules_Left/Right의
    value 리스트 4,5번 인덱스에 저장.
    - key(ModuleN)로 1:1 매칭
    - c_modules_Left/Right에만 존재하는 key만 처리하며, mainWindow.modules_Left/Right에만
      존재하는 key는 무시(추가X)
    """
    global c_modules_Left, c_modules_Right, mainWindow

    # Left side
    for module_key, c_val in c_modules_Left.items():
        if module_key in mainWindow.modules_Left:
            mw_val = mainWindow.modules_Left[module_key]
            # c_val[2], c_val[3] → mw_val[3], mw_val[4]
            if len(c_val) > 2 and len(mw_val) > 3:
                mw_val[3] = c_val[2]
            if len(c_val) > 3 and len(mw_val) > 4:
                mw_val[4] = c_val[3]
    print(f'c_app_left: {mainWindow.modules_Left}')

    # Right side
    for module_key, c_val in c_modules_Right.items():
        if module_key in mainWindow.modules_Right:
            mw_val = mainWindow.modules_Right[module_key]
            if len(c_val) > 2 and len(mw_val) > 3:
                mw_val[3] = c_val[2]
            if len(c_val) > 3 and len(mw_val) > 4:
                mw_val[4] = c_val[3]
    print(f'c_app_right: {mainWindow.modules_Right}')
    jobControl.clsInfo['sensor_dict update'] = True

app = QApplication([])
mainWindow = c_mainwindow.MainWindow(appVer, baseDir, objName= 'mainWindow')
mainWindow.signalMessage.connect(slotParse)

systemName = platform.system()

if systemName == 'Linux' :
    mainWindow.showFullscreen()

elif systemName == 'Windows':
    pass


jobControl = c_jobControl.JobController(baseDir, objName='jobManager', mainWindow=mainWindow)
jobControl.signalMessage.connect(slotParse)


mainWindow.show()
app.exec()
