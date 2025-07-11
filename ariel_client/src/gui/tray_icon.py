import logging
from PySide6.QtWidgets import QSystemTrayIcon, QMenu, QMessageBox, QApplication
from PySide6.QtGui import QIcon, QAction
from PySide6.QtCore import Slot, QObject, QRect, QThread, QTimer

from ..utils import resource_path
from ..config_manager import ConfigManager
from .setup_window import SetupWindow
from .overlay_manager import OverlayManager
from .ocr_capturer import OcrCapturer
from ..core.screen_monitor import ScreenMonitor
from ..core.translation_worker import TranslationWorker
from ..core.hotkey_manager import HotkeyManager
from ..core.audio_processor import AudioProcessor
from ..core.sound_player import SoundPlayer

logger = logging.getLogger(__name__)

class TrayIcon(QObject):
    # [수정] __init__ 시그니처 변경: icon_path: str -> icon: QIcon
    def __init__(self, config_manager: ConfigManager, icon: QIcon, app: QApplication):
        super().__init__()
        self.app, self.config_manager = app, config_manager
        
        self.is_setup_required = self.check_if_setup_is_required()
        
        # [수정] 전달받은 QIcon 객체를 생성자에 바로 사용
        self.tray_icon = QSystemTrayIcon(icon, self)
        self.tray_icon.setToolTip("Ariel by Seeth")

        self.worker_thread = QThread(self)
        self.worker = None

        self.hotkey_manager = HotkeyManager(self.config_manager, self)
        self.overlay_manager = OverlayManager(self.config_manager, self)
        self.sound_player = SoundPlayer(self)

        self.setup_window, self.ocr_capturer = None, None
        self.ocr_monitor_thread, self.screen_monitor = None, None
        self.stt_processor_thread, self.audio_processor = None, None
        self.ocr_capture_rect = None

        self.create_menu()
        self.tray_icon.setContextMenu(self.menu)

        self.hotkey_manager.hotkey_pressed.connect(self.on_hotkey_pressed)
        
        self.tray_icon.show()
        logger.info("트레이 아이콘 및 핵심 컴포넌트 준비 완료.")
        
        self.hotkey_manager.start()
        
        if self.is_setup_required:
            logger.warning("필수 설정이 누락되어 설정 창을 먼저 실행합니다.")
            QTimer.singleShot(100, self.open_setup_window)
        else:
            self.initialize_worker_and_start()

    def check_if_setup_is_required(self):
        api_key = self.config_manager.get("deepl_api_key")
        return self.config_manager.get("is_first_run") or not api_key

    def initialize_worker_and_start(self):
        if self.worker and self.worker_thread.isRunning(): return

        logger.info("필수 설정 확인 완료. 번역 엔진을 초기화합니다.")
        try:
            self.worker = TranslationWorker(self.config_manager)
            self.worker.moveToThread(self.worker_thread)
            
            self.worker.stt_translation_ready.connect(self.overlay_manager.add_stt_translation)
            self.worker.ocr_translation_ready.connect(self.overlay_manager.show_ocr_patches)
            self.worker.error_occurred.connect(self.on_worker_error)
            self.worker.status_updated.connect(self.overlay_manager.add_system_message_to_stt)
            
            self.worker_thread.start()
        except (ValueError, ConnectionError) as e:
            logger.critical(f"번역 엔진 초기화 실패: {e}")
            QMessageBox.critical(None, "초기화 실패", f"번역 엔진 초기화에 실패했습니다.\n\n{e}\n\n프로그램을 종료합니다.")
            self.quit_application()
            return

    def create_menu(self):
        self.menu = QMenu()
        self.voice_translation_action = QAction("음성 번역", self)
        self.voice_translation_action.setCheckable(True)
        self.ocr_translation_action = QAction("화면 번역", self)
        self.ocr_translation_action.setCheckable(True)
        self.setup_action = QAction("설정...", self)
        self.quit_action = QAction("종료", self)
        
        self.menu.addAction(self.voice_translation_action)
        self.menu.addAction(self.ocr_translation_action)
        self.menu.addSeparator()
        self.menu.addAction(self.setup_action)
        self.menu.addAction(self.quit_action)
        
        self.voice_translation_action.toggled.connect(self.toggle_voice_translation)
        self.ocr_translation_action.toggled.connect(self.toggle_ocr_translation)
        self.setup_action.triggered.connect(self.open_setup_window)
        self.quit_action.triggered.connect(self.quit_application)

    @Slot(str)
    def on_hotkey_pressed(self, action_name: str):
        actions = {
            "hotkey_toggle_stt": self.voice_translation_action.toggle,
            "hotkey_toggle_ocr": self.ocr_translation_action.toggle,
            "hotkey_toggle_setup": self.toggle_setup_window,
            "hotkey_quit_app": self.quit_application
        }
        if (action := actions.get(action_name)):
            logger.info(f"단축키 액션 '{action_name}'을 실행합니다.")
            action()
        else:
            logger.warning(f"수신된 단축키 액션 '{action_name}'에 해당하는 동작이 없습니다.")

    def open_setup_window(self):
        if self.setup_window and self.setup_window.isVisible():
            self.setup_window.activateWindow()
            return
        #self.hotkey_manager.stop()
        self.setup_window = SetupWindow(self.config_manager)
        self.setup_window.closed.connect(self.on_setup_window_closed)
        self.setup_window.show()
        self.setup_window.activateWindow()

    @Slot()
    def on_setup_window_closed(self):
        logger.info("설정 창이 닫혔습니다. 설정을 다시 확인하고 적용합니다.")
        self.hotkey_manager.reload_hotkeys()
        self.hotkey_manager.start()
        
        is_setup_now_complete = not self.check_if_setup_is_required()
        if self.is_setup_required and is_setup_now_complete:
            self.is_setup_required = False
            self.initialize_worker_and_start()
        
        self.setup_window = None

    def toggle_setup_window(self):
        if not self.setup_window or not self.setup_window.isVisible():
            self.open_setup_window()
        else:
            self.setup_window.close()

    @Slot(bool)
    def toggle_voice_translation(self, checked: bool):
        if self.is_setup_required:
            QMessageBox.warning(None, "준비 필요", "아직 번역 엔진이 준비되지 않았습니다.\n설정에서 API 키를 먼저 입력해주세요.")
            self.voice_translation_action.setChecked(False)
            return
        if checked: 
            self.start_voice_translation()
        else: 
            self.stop_voice_translation()
        self.voice_translation_action.setChecked(checked)

    def start_voice_translation(self):
        if self.stt_processor_thread and self.stt_processor_thread.isRunning(): return
        self.stt_processor_thread = QThread(self)
        self.audio_processor = AudioProcessor(self.config_manager)
        self.audio_processor.moveToThread(self.stt_processor_thread)
        self.audio_processor.audio_chunk_ready.connect(self.worker.process_stt_audio)
        self.audio_processor.status_updated.connect(self.overlay_manager.add_system_message_to_stt)
        self.stt_processor_thread.started.connect(self.audio_processor.start_processing)
        self.audio_processor.finished.connect(self.stt_processor_thread.quit)
        self.stt_processor_thread.finished.connect(self.stt_processor_thread.wait)
        self.stt_processor_thread.finished.connect(self.audio_processor.deleteLater)
        self.stt_processor_thread.finished.connect(self.stt_processor_thread.deleteLater)
        self.stt_processor_thread.start()
        self.overlay_manager.show_stt_overlay()
        self.sound_player.play(self.config_manager.get("sound_stt_start"))

    def stop_voice_translation(self):
        if self.audio_processor:
            self.audio_processor.stop()
        if self.stt_processor_thread:
            self.stt_processor_thread.quit()
            self.stt_processor_thread.wait()
        self.overlay_manager.hide_stt_overlay()
        self.sound_player.play(self.config_manager.get("sound_stt_stop"))
        # Clean up references to prevent dangling pointers
        self.audio_processor = None
        self.stt_processor_thread = None
        logger.info("음성 번역 기능이 중지되었습니다.")

    @Slot(bool)
    def toggle_ocr_translation(self, checked: bool):
        if self.is_setup_required:
            QMessageBox.warning(None, "준비 필요", "아직 번역 엔진이 준비되지 않았습니다.\n설정에서 API 키를 먼저 입력해주세요.")
            self.ocr_translation_action.setChecked(False)
            return
        if checked:
            self.select_ocr_region()
        else: 
            self.stop_ocr_monitoring()
        self.ocr_translation_action.setChecked(checked)

    def select_ocr_region(self):
        if self.ocr_capturer: self.ocr_capturer.activateWindow(); return
        self.ocr_capturer = OcrCapturer()
        self.ocr_capturer.region_selected.connect(self.start_ocr_monitoring_on_region)
        self.ocr_capturer.cancelled.connect(lambda: self.ocr_translation_action.setChecked(False))
        self.ocr_capturer.finished.connect(lambda: setattr(self, 'ocr_capturer', None))
        self.ocr_capturer.show()

    @Slot(QRect)
    def start_ocr_monitoring_on_region(self, rect: QRect):
        if rect.isNull():
            self.ocr_translation_action.setChecked(False)
            return
            
        self.ocr_capture_rect = rect
        self.ocr_monitor_thread = QThread(self)
        self.screen_monitor = ScreenMonitor(rect, self.overlay_manager.get_stt_overlay_geometry)
        self.screen_monitor.moveToThread(self.ocr_monitor_thread)
        self.ocr_monitor_thread.started.connect(self.screen_monitor.start_monitoring)
        self.screen_monitor.image_changed.connect(self.worker.process_ocr_image)
        self.screen_monitor.stopped.connect(self.ocr_monitor_thread.quit)
        self.ocr_monitor_thread.finished.connect(self.ocr_monitor_thread.wait)
        self.ocr_monitor_thread.finished.connect(self.screen_monitor.deleteLater)
        self.ocr_monitor_thread.finished.connect(self.ocr_monitor_thread.deleteLater)
        self.ocr_monitor_thread.start()
        self.sound_player.play(self.config_manager.get("sound_ocr_start"))

    def stop_ocr_monitoring(self):
        if self.screen_monitor: self.screen_monitor.stop()
        if self.ocr_monitor_thread:
            self.ocr_monitor_thread.quit()
            self.ocr_monitor_thread.wait()
        self.overlay_manager.hide_ocr_overlay()
        self.sound_player.play(self.config_manager.get("sound_ocr_stop"))
        self.screen_monitor = None
        self.ocr_monitor_thread = None
        logger.info("화면 번역 기능이 중지되었습니다.")

    @Slot(str)
    def on_worker_error(self, message: str):
        QMessageBox.warning(None, "오류", message)
        if "STT" in message or "오디오" in message: self.stop_voice_translation()
        if "OCR" in message or "화면" in message: self.stop_ocr_monitoring()

    def quit_application(self):
        self.hotkey_manager.stop()
        if self.stt_processor_thread and self.stt_processor_thread.isRunning(): self.stop_voice_translation()
        if self.ocr_monitor_thread and self.ocr_monitor_thread.isRunning(): self.stop_ocr_monitoring()
        if self.worker_thread and self.worker_thread.isRunning():
            self.worker_thread.quit()
            self.worker_thread.wait()
        
        self.tray_icon.hide()
        logger.info("모든 리소스 정리 및 종료 절차 시작.")
        QApplication.instance().quit()