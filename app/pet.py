import json, random, sys, time, threading, gzip, os
from pathlib import Path
from collections import OrderedDict
from datetime import datetime,timezone,timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from PIL import Image
from PySide6.QtCore import Qt, QTimer, QThread, Signal, QPoint, QPointF, QRect, QRectF, QSize, QSettings, QEvent, QProcess
from PySide6.QtGui import QImage, QPixmap, QPainter, QPen, QColor, QRegion, QCursor, QIcon, QTransform
from PySide6.QtWidgets import QApplication, QWidget, QLabel, QVBoxLayout, QProgressBar, QMenu, QMessageBox, QProgressDialog, QPushButton, QSystemTrayIcon, QTextBrowser, QSlider, QDialog, QCheckBox, QLineEdit, QFormLayout, QDialogButtonBox, QHBoxLayout, QFrame, QFileDialog
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from quota import read_quota, quota_windows
from station import DEFAULT_BASE_URL, StationFailure, fetch_station, normalize_base_url
from version import VERSION
from movement import plan_movement, movement_fraction
from panel_layout import layout_panels,hover_position

from platform_support import resource_root,state_root,configure_mac_window
ROOT=resource_root()
STATE_ROOT=state_root()
ASSETS=ROOT/'outputs/白龙桌宠素材'
FLAGS=Qt.WindowType.FramelessWindowHint|Qt.WindowType.WindowStaysOnTopHint|Qt.WindowType.Tool

def clamp_position(position,size,area):
    """Keep the full pet window inside one monitor's usable rectangle."""
    max_x=max(area.left(),area.right()-size.width()+1)
    max_y=max(area.top(),area.bottom()-size.height()+1)
    return QPoint(max(area.left(),min(position.x(),max_x)),
                  max(area.top(),min(position.y(),max_y)))

def format_money(value,currency=''):
    """Round display amounts only; keep the original snapshot for calculations."""
    if value is None:return '—'
    try:
        number=Decimal(str(value))
        if not number.is_finite():return '—'
        return currency+format(number.quantize(Decimal('0.001'),rounding=ROUND_HALF_UP),'.3f')
    except (InvalidOperation,ValueError,TypeError):return '—'


def format_latency(milliseconds):
    if milliseconds is None or isinstance(milliseconds,bool):return None
    try:
        value=Decimal(str(milliseconds))
        if not value.is_finite() or value<0:return None
        return format((value/1000).quantize(Decimal('0.1'),rounding=ROUND_HALF_UP),'.1f')+'s'
    except (InvalidOperation,ValueError,TypeError):return None


def panel_positions(pet,sizes,area,blockers=(),gap=10,scale=1.0):
    return layout_panels(pet,sizes,area,blockers,gap,scale)[0]


class QuotaWorker(QThread):
    ready=Signal(object)
    failed=Signal(str)
    def __init__(self,parent=None):
        super().__init__(parent);self.cancel=threading.Event()
    def run(self):
        try: self.ready.emit(quota_windows(read_quota(cancel=self.cancel)))
        except Exception as error: self.failed.emit(str(error))

class StationWorker(QThread):
    ready=Signal(object)
    failed=Signal(str)
    def __init__(self,api_key,base_url,parent=None,billing_access_token='',account_id=''):
        super().__init__(parent);self.api_key=api_key;self.base_url=base_url;self.billing_access_token=billing_access_token;self.account_id=account_id;self.cancel=threading.Event()
    def run(self):
        try:
            if self.cancel.is_set():return
            self.ready.emit(fetch_station(self.api_key,self.base_url,billing_access_token=self.billing_access_token,account_id=self.account_id))
        except Exception as error:self.failed.emit(str(error))

class HoverBar(QWidget):
    entered=Signal()
    left=Signal()
    def __init__(self):
        super().__init__(None, FLAGS)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.layout=QHBoxLayout(self);self.layout.setContentsMargins(10,6,10,6);self.layout.setSpacing(4)
        self.setStyleSheet('QPushButton { color:#60358c; background:#fbf9ff; border:0; border-radius:9px; padding:5px 9px; font-size:11px; } QPushButton:hover { background:#eadff5; }')
        for text in ('聊天','额度','声音','⋯'):
            button=QPushButton(text,self);button.setCursor(Qt.CursorShape.PointingHandCursor);self.layout.addWidget(button)
            setattr(self,'more_button' if text=='⋯' else text,button)
        self.adjustSize()
    def enterEvent(self,event):
        self.entered.emit();super().enterEvent(event)
    def leaveEvent(self,event):
        self.left.emit();super().leaveEvent(event)

class RefreshButton(QPushButton):
    def __init__(self,loading=False,parent=None):
        super().__init__('',parent)
        self.setProperty('isRefresh',True);self.setProperty('loading',bool(loading))
        self.loading=bool(loading);self.angle=0;self.setFixedSize(24,24)
        self.setCursor(Qt.CursorShape.PointingHandCursor);self.setEnabled(not self.loading)
        self.setToolTip('正在刷新…' if self.loading else '刷新')
        self.timer=QTimer(self);self.timer.setInterval(60);self.timer.timeout.connect(self.advance)
        if self.loading:self.timer.start()
    def advance(self):
        self.angle=(self.angle+24)%360;self.update()
    def hideEvent(self,event):
        self.timer.stop();super().hideEvent(event)
    def showEvent(self,event):
        if self.loading:self.timer.start()
        super().showEvent(event)
    def paintEvent(self,event):
        super().paintEvent(event)
        painter=QPainter(self);painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen=QPen(QColor('#9a7ab3'),2.2);pen.setCapStyle(Qt.PenCapStyle.RoundCap);painter.setPen(pen)
        if self.loading:painter.drawArc(QRectF(5,5,self.width()-10,self.height()-10),-self.angle*16,270*16)
        else:
            painter.drawArc(QRectF(5,5,self.width()-10,self.height()-10),45*16,285*16)
            painter.drawLine(QPointF(18,16),QPointF(13,16));painter.drawLine(QPointF(18,16),QPointF(18,21))


class Bubble(QWidget):
    dismiss_requested=Signal()
    refresh_requested=Signal(str)
    placement_requested=Signal()
    def __init__(self):
        super().__init__(None, FLAGS)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.scale_percent=100;self.base_width=256;self.is_task_bubble=False;self.is_size_preview=False
        self.is_station_result=False
        self.layout=QVBoxLayout(self); self.layout.setContentsMargins(13,10,13,10);self.layout.setSpacing(5)
        self.setFixedWidth(256)
        self.setStyleSheet('QLabel {color:#332644;font-size:12px;font-family:"Microsoft YaHei","Segoe UI";} QProgressBar {background:#eee8f5;border:1px solid #c8b5dc;border-radius:7px;height:14px;} QProgressBar::chunk {background:#a47bc9;border-radius:6px;}')
        self.close_button=QPushButton('×',self);self.close_button.setFixedSize(24,24)
        self.close_button.setToolTip('关闭气泡')
        self.close_button.setStyleSheet('QPushButton {color:#776783;background:transparent;border:0;border-radius:10px;font-size:19px;} QPushButton:hover {background:#eee5f7;color:#503768;}')
        self.close_button.clicked.connect(self.hide)
        self.close_button.clicked.connect(lambda:self.dismiss_requested.emit())
    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,'close_button'):
            self.close_button.move(self.width()-self.close_button.width()-5,5);self.close_button.raise_()
    def set_scale_percent(self,value):
        self.scale_percent=max(50,min(150,int(value)));s=self.scale_percent/100
        self.setFixedWidth(round(self.base_width*s))
        self.layout.setContentsMargins(round(13*s),round(10*s),round(13*s),round(10*s));self.layout.setSpacing(max(2,round(5*s)))
        self.setStyleSheet(f'QLabel {{color:#332644;font-size:{round(12*s)}px;font-family:"Microsoft YaHei","Segoe UI";}} QPushButton {{font-size:{round(12*s)}px;}} QProgressBar {{background:#eee8f5;border:1px solid #c8b5dc;border-radius:5px;height:{round(14*s)}px;}} QProgressBar::chunk {{background:#a47bc9;border-radius:5px;}}')
        for view in self.findChildren(QTextBrowser):
            view.setFixedHeight(round((view.property('baseHeight') or 76)*s))
            view.setStyleSheet(f'QTextBrowser {{color:#332644;background:transparent;border:0;font-size:{round(12*s)}px;font-family:"Microsoft YaHei","Segoe UI";}}')
        self.close_button.setFixedSize(max(16,round(24*s)),max(16,round(24*s)))
        if self.layout.count() and isinstance(self.layout.itemAt(0).widget(),QLabel):
            self.layout.itemAt(0).widget().setContentsMargins(0,0,self.close_button.width()+4,0)
        for button in self.findChildren(QPushButton):
            if button.property('isRefresh'):
                button.setStyleSheet('QPushButton {color:#8d6ca6;background:transparent;border:0;border-radius:8px;padding:0;font-size:20px;} QPushButton:hover {background:#eee5f7;}')
            elif button is not self.close_button:
                button.setStyleSheet(f'color:#60358c;background:#eee5f7;border:0;border-radius:7px;padding:{max(3,round(7*s))}px;')
        self.close_button.setStyleSheet(f'QPushButton {{color:#776783;background:transparent;border:0;border-radius:8px;font-size:{max(12,round(19*s))}px;}} QPushButton:hover {{background:#eee5f7;color:#503768;}}')
        self.adjustSize();self.close_button.move(self.width()-self.close_button.width()-5,5);self.close_button.raise_()
    def paintEvent(self,event):
        painter=QPainter(self);painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(Qt.GlobalColor.white);painter.setPen(Qt.GlobalColor.transparent)
        painter.drawRoundedRect(self.rect(),16,16)
    def clear(self):
        self._updating_content=True;self.setUpdatesEnabled(False)
        while self.layout.count():
            widget=self.layout.takeAt(0).widget();widget.hide();widget.setParent(None);widget.deleteLater()
    def present(self):
        # A new native tool window must receive its final position before show;
        # otherwise Windows paints one frame at its previous/default position.
        self.set_scale_percent(self.scale_percent);self.layout.activate()
        self._updating_content=False;self.placement_requested.emit();self.show();self.setUpdatesEnabled(True)
    def label(self,text):
        label=QLabel(text);label.setWordWrap(True);label.setTextFormat(Qt.TextFormat.PlainText)
        if not self.layout.count():label.setContentsMargins(0,0,20,0)
        self.layout.addWidget(label)
    def message(self,text):
        self.is_size_preview=False
        self.is_task_bubble=False
        self.is_task_progress=False
        self.is_quota_result=False
        self.is_station_result=False
        self.base_width=256;self.setFixedWidth(round(256*self.scale_percent/100))
        self.clear();self.label(text);self.present()
    def task_result(self,title,text,details):
        self.is_size_preview=False
        self.is_task_bubble=True
        self.is_task_progress=False
        self.is_quota_result=True
        self.is_station_result=False
        self.base_width=300;self.setFixedWidth(round(300*self.scale_percent/100))
        self.clear();self.label(title)
        self.result_title=self.layout.itemAt(0).widget()
        result=QTextBrowser();result.setPlainText(text);result.setOpenLinks(False)
        result.setStyleSheet('QTextBrowser {color:#332644;background:transparent;border:0;font-size:12px;font-family:"Microsoft YaHei","Segoe UI";}')
        base_height=150 if len(text)>180 or len(text.splitlines())>6 else 76
        result.setProperty('baseHeight',base_height);result.setFixedHeight(base_height)
        self.result_view=result
        self.layout.addWidget(result)
        button=QPushButton('详情');button.setStyleSheet('color:#60358c;background:#eee5f7;border:0;border-radius:7px;padding:7px;')
        button.clicked.connect(lambda checked=False:details());self.layout.addWidget(button)
        self.present()
    def quota(self,windows):
        self.is_size_preview=False
        self.is_task_bubble=False
        self.is_task_progress=False
        self.is_quota_result=True
        self.is_station_result=False
        self.base_width=256;self.setFixedWidth(round(256*self.scale_percent/100))
        self.clear();self.label('Codex 剩余额度')
        for window in windows:
            value=window['remaining']
            self.label(window['name']+' · '+(f'剩余 {value:g}%' if value is not None else '暂未提供'))
            bar=QProgressBar();bar.setRange(0,100)
            bar.setValue(round(value) if value is not None else 0);bar.setTextVisible(False);self.layout.addWidget(bar)
            if window['resetsAt']:
                self.label('重置：'+datetime.fromtimestamp(window['resetsAt']).strftime('%m月%d日 %H:%M'))
        self.present()

    def _card(self,title,loading=False,provider='station'):
        card=QFrame(self);card.setObjectName('dashboardCard')
        card.setStyleSheet('#dashboardCard {background:#fbf9ff;border:1px solid #eadff5;border-radius:11px;}')
        layout=QVBoxLayout(card);layout.setContentsMargins(10,8,10,8);layout.setSpacing(4)
        heading=QLabel(title,card)
        heading.setStyleSheet('font-size:14px;font-weight:700;color:#503768;')
        header=QHBoxLayout();header.setSpacing(4);header.addWidget(heading,1)
        refresh=RefreshButton(loading,card);refresh.setObjectName('refreshStation' if provider=='station' else 'refreshCodex')
        refresh.setAccessibleName('刷新 '+title);refresh.clicked.connect(lambda checked=False:self.refresh_requested.emit(provider))
        header.addWidget(refresh)
        if not self.layout.count():header.addSpacing(24)
        layout.addLayout(header)
        return card,layout

    def dashboard(self,windows,station=None,station_error=None,show_codex=True,show_station=True,codex_error=None,codex_loading=False,station_loading=False):
        """Compact values and actionable errors, without decorative metadata."""
        self.is_size_preview=False;self.is_task_bubble=False;self.is_task_progress=False
        self.is_quota_result=True;self.is_station_result=True;self.base_width=280
        self.clear();self.close_button.setToolTip('关闭额度面板')
        def line(card,text,error=False):
            label=QLabel(text,card);label.setWordWrap(True);label.setTextFormat(Qt.TextFormat.PlainText)
            label.setStyleSheet('font-size:13px;color:'+('#a34c67' if error else '#5d496e')+';')
            card.layout().addWidget(label)
        if show_codex:
            codex,_=self._card('Codex 额度',codex_loading,'codex')
            for window in windows or []:
                value=window.get('remaining');name=window.get('name','额度窗口')
                line(codex,f'{name}  '+(f'{value:g}%' if value is not None else '暂未提供'))
                bar=QProgressBar(codex);bar.setRange(0,100);bar.setValue(round(value) if value is not None else 0)
                bar.setTextVisible(False);bar.setFixedHeight(6);codex.layout().addWidget(bar)
            if codex_error:line(codex,('刷新失败，保留上次结果。\n' if windows else '')+'Codex 额度查询失败：'+codex_error,True)
            elif not windows and not codex_loading:line(codex,'当前未返回官方额度。')
            self.layout.addWidget(codex)
        if show_station:
            card,_=self._card('DogAPI',station_loading,'station')
            if station:
                currency=station.get('currency') or ''
                today=station.get('todayUsed')
                today_date=datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d')
                stale_day=station.get('todayDate') not in (None,today_date)
                if stale_day:today=None
                for name,value in (('钱包余额',station.get('walletBalance')),('今日消耗',today)):
                    value_label=QLabel(name+'：'+format_money(value,currency),card)
                    value_label.setStyleSheet('font-size:14px;color:#503768;');card.layout().addWidget(value_label)
                if today is None:
                    detail=('今日统计尚未更新，请刷新。' if stale_day else station.get('todayError'))
                    if detail:line(card,detail)
                first=format_latency(station.get('latencyFirstTokenMs'));total=format_latency(station.get('latencyTotalMs'))
                times=[]
                if first is not None:times.append('首字≈'+first)
                if total is not None:times.append('总耗时≈'+total)
                line(card,'最近调用  '+' · '.join(times) if times else '调用耗时：暂未提供')
            if station_error:line(card,('刷新失败，保留上次结果。\n' if station else '')+station_error,True)
            elif not station and not station_loading:line(card,'尚无查询结果，请打开中转站设置。')
            self.layout.addWidget(card)
        if not show_codex and not show_station:self.label('额度卡片已关闭。右键菜单可重新打开。')
        self.present()

    def size_preview(self):
        self.clear();self.is_size_preview=True;self.is_task_bubble=False
        self.is_task_progress=False;self.is_quota_result=True;self.base_width=300
        self.is_station_result=False
        blank=QTextBrowser();blank.setProperty('baseHeight',110)
        self.layout.addWidget(blank);self.present()

class Pet(QWidget):
    def __init__(self):
        super().__init__(None,FLAGS)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowTitle('白龙桌宠');self.setFixedSize(256,256)
        self.manifest=json.loads((ASSETS/'素材清单.json').read_text(encoding='utf-8'))
        self.variants=[v for v in self.manifest['variants'] if self.asset_path(v).is_file()]
        self.idles=[self.manifest['idle']]+self.manifest.get('additionalIdleAnimations',[])[:1]
        self.extra_idles=[v for v in self.manifest.get('additionalIdleAnimations',[]) if v.get('id') in ('idle03','idle04','idle05','idle06') and self.asset_path(v).is_file()]
        self.next_leisure_group='idle';self.next_leisure_at=time.monotonic()+15.0
        self.cache=OrderedDict();self.pinned_frames={};self.frames=[];self.current=None;self.mode='idle';self.index=0
        self.frame_size=min(512,max(256,round(256*max(s.devicePixelRatio() for s in QApplication.screens()))))
        self.transition=None
        self.bubble=Bubble();self.task_bubble=Bubble();self.quota_bubble=Bubble();self.generation=0;self.quota_generation=0;self.dashboard_requested=False;self.worker=None;self.quota_inflight=False;self.quota_error=None;self.station_worker=None;self.station_inflight=False;self.station_snapshot=None;self.station_error=None;self.station_request_context=None;self.station_snapshot_context=None;self.current_quota_windows=None;self.dragging=False;self.last_click=None
        self.setMouseTracking(True)
        self.hover_bar=HoverBar()
        self.hover_bar.entered.connect(self._keep_hover_bar)
        self.hover_bar.left.connect(self._schedule_hover_bar_hide)
        self.hover_hide_timer=QTimer(self);self.hover_hide_timer.setSingleShot(True);self.hover_hide_timer.setInterval(650);self.hover_hide_timer.timeout.connect(self._hide_hover_bar)
        self.hover_bar.聊天.clicked.connect(self.open_history)
        self.hover_bar.额度.clicked.connect(self.quota)
        self.hover_bar.声音.clicked.connect(self.show_sound_menu)
        self.hover_bar.more_button.clicked.connect(self.show_hover_menu)
        self.bubble.dismiss_requested.connect(self.dismiss_bubble)
        self.task_bubble.dismiss_requested.connect(self.dismiss_task_bubble)
        self.quota_bubble.dismiss_requested.connect(self.dismiss_quota_bubble)
        self.quota_bubble.refresh_requested.connect(self.refresh_card)
        for panel in self.panels():panel.placement_requested.connect(self.position_bubble)
        self.chat=None
        self.task_active=False
        self.manual_hidden=False;self.fullscreen_hidden=False;self.hidden_since=None
        self.settings=QSettings('BailongPet','DesktopPet')
        self.bubble_percent=max(50,min(150,self.settings.value('bubblePercent',100,type=int)))
        self.task_bubbles_disabled=self.settings.value('taskBubblesDisabled',False,type=bool)
        self.sound_disabled=self.settings.value('soundDisabled',False,type=bool)
        default_sound='duck' if (ROOT/'app/sounds/duck_squeak.wav').is_file() else 'click'
        self.sound_mode=str(self.settings.value('soundMode',default_sound)).strip() or default_sound
        self.sound_path=str(self.settings.value('soundPath','')).strip()
        self.sound_name=str(self.settings.value('soundName','')).strip()
        self.sound_import=None
        if self.sound_mode not in ('click','duck','custom') and self.sound_mode not in self._sound_presets():self.sound_mode=default_sound
        if self.sound_mode=='duck' and default_sound=='click':self.sound_mode='click'
        self._update_sound_button()
        self.show_codex_panel=self.settings.value('showCodexPanel',True,type=bool)
        self.show_station_panel=self.settings.value('showStationPanel',True,type=bool)
        for panel in self.panels():panel.set_scale_percent(self.bubble_percent)
        self.size_percent=max(50,min(200,self.settings.value('sizePercent',100,type=int)))
        pixels=round(256*self.size_percent/100);self.setFixedSize(pixels,pixels)
        self.auto_hide_fullscreen=self.settings.value('autoHideFullscreen',True,type=bool)
        self.auto_movement_disabled=self.settings.value('autoMovementDisabled',False,type=bool)
        self.dock_edge=None
        self.work_slots=self.manifest.get('workAnimations',[])[:6]
        self.movement_slots=[v for v in self.manifest.get('movementAnimations',[]) if v and self.asset_path(v).is_file()]
        self.movement_plan=None;self.movement_mirrored=False;self.pointer_down=False
        self.last_interaction=time.monotonic();self.movement_ready_at=0.0
        self.click_timer=QTimer(self);self.click_timer.setTimerType(Qt.TimerType.PreciseTimer);self.click_timer.setSingleShot(True);self.click_timer.setInterval(250);self.click_timer.timeout.connect(self.single_click)
        self.dock_click_timer=QTimer(self);self.dock_click_timer.setSingleShot(True)
        self.dock_click_timer.setTimerType(Qt.TimerType.PreciseTimer);self.dock_click_timer.setInterval(250)
        self.dock_click_timer.timeout.connect(self.begin_undock)
        self.dock_double_click=False
        self.bubble_timer=QTimer(self);self.bubble_timer.setSingleShot(True);self.bubble_timer.timeout.connect(self.dismiss_bubble)
        self.task_timer=QTimer(self);self.task_timer.setSingleShot(True);self.task_timer.timeout.connect(self.dismiss_task_bubble)
        self.quota_timer=QTimer(self);self.quota_timer.setSingleShot(True);self.quota_timer.timeout.connect(self.dismiss_quota_bubble)
        self.station_timer=QTimer(self);self.station_timer.setInterval(60000);self.station_timer.timeout.connect(self.refresh_station_silently);self.station_timer.start()
        self.tick=QTimer(self);self.tick.setTimerType(Qt.TimerType.PreciseTimer);self.tick.setInterval(16);self.tick.timeout.connect(self.animate)
        area=QApplication.primaryScreen().availableGeometry()
        self.move(self.constrained_position(QPoint(area.right()-360,area.bottom()-340)))
        for screen in QApplication.screens():self.watch_screen(screen)
        QApplication.instance().screenAdded.connect(self.watch_screen)
        QApplication.instance().screenRemoved.connect(lambda screen:QTimer.singleShot(0,self.ensure_on_screen))
        self.idle()
        # User prefers immediate interaction: decode all actions once at startup
        # and retain them even while hidden, without running the animation timer.
        items=self.idles+self.extra_idles+self.variants+self.movement_slots+[v for v in self.work_slots if v and self.asset_path(v).is_file()]+[self.manifest[name] for name in ('dragAnimation','landingAnimation','quotaAnimation') if self.manifest.get(name)]
        items += [self.return_transition_item(v) for v in self.variants if v.get('returnTransition')]
        progress=None
        if QApplication.platformName()!='offscreen':
            progress=QProgressDialog('主人，等我整理好裙摆和小翅膀，马上就来…','',0,len(items))
            progress.setCancelButton(None);progress.setWindowTitle('白龙桌宠');progress.setMinimumDuration(0);progress.show()
        for i,item in enumerate(items):
            key=str(self.asset_path(item));self.pinned_frames[key]=self.load(item);self.cache.pop(key,None)
            if progress:progress.setValue(i+1);QApplication.processEvents()
        if progress:progress.close()
        self.transition=None
        self.started=time.monotonic();self.tick.start()
        self.last_interaction=self.started
        self.next_leisure_at=self.started+15.0
        QApplication.instance().installEventFilter(self)
        self.setup_tray()
        from desktop_visibility import FullscreenDetector
        self.fullscreen_detector=FullscreenDetector()
        self.fullscreen_candidate=False;self.fullscreen_candidate_since=time.monotonic()
        self.visibility_timer=QTimer(self);self.visibility_timer.setInterval(500)
        self.visibility_timer.timeout.connect(self.check_fullscreen)
        if QApplication.platformName()!='offscreen':self.visibility_timer.start()
        from desktop_task_sync import DesktopTaskSync
        self.desktop_sync=DesktopTaskSync(self)
        if QApplication.platformName()!='offscreen':QTimer.singleShot(1500,self.desktop_sync.start)
    def setup_tray(self):
        # Use the default idle's first frame, independent of the current action.
        icon=QIcon(self.pinned_frames[str(self.asset_path(self.idles[0]))][0])
        self.setWindowIcon(icon)
        self.tray=QSystemTrayIcon(icon,self);self.tray.setToolTip('GPT白龙娘桌宠')
        self.tray_menu=self.context_menu();self.tray.setContextMenu(self.tray_menu)
        self.tray_menu.aboutToShow.connect(self.refresh_tray_menu)
        self.tray.activated.connect(self.tray_activated)
        if QSystemTrayIcon.isSystemTrayAvailable():self.tray.show()
    def refresh_tray_menu(self):
        self.note_interaction(cancel_movement=False)
        # Rebuild through the same factory used by the pet's right-click menu.
        fresh=self.context_menu()
        self.tray_menu.clear()
        # Keep submenu/action owners alive until the next refresh.
        previous=getattr(self,'tray_menu_contents',None)
        self.tray_menu_contents=fresh
        for action in fresh.actions():self.tray_menu.addAction(action)
        if previous:previous.deleteLater()
    def tray_activated(self,reason):
        self.note_interaction(cancel_movement=False)
        if reason==QSystemTrayIcon.ActivationReason.DoubleClick:self.set_manual_hidden(False)
    def eventFilter(self,obj,event):
        if event.type() in (QEvent.Type.MouseButtonPress,QEvent.Type.MouseButtonRelease,
                            QEvent.Type.MouseMove,QEvent.Type.KeyPress,QEvent.Type.Wheel):
            roots=[self,*self.panels()]
            if self.chat:roots.extend((self.chat.composer,self.chat.window))
            owned=isinstance(obj,QWidget) and any(obj is root or root.isAncestorOf(obj) for root in roots)
            if owned:self.note_interaction(cancel_movement=False)
        return super().eventFilter(obj,event)
    def note_interaction(self,cancel_movement=True):
        self.last_interaction=time.monotonic()
        self.next_leisure_at=self.last_interaction+15.0;self.next_leisure_group='idle'
        if cancel_movement and self.mode=='movement':
            self.finish_movement()
            self.play(self.idles[0],'idle')
    def finish_movement(self):
        if self.movement_plan is not None:
            self.movement_ready_at=time.monotonic()+10.0
        self.movement_plan=None;self.movement_mirrored=False
    def movement_allowed(self,now=None):
        now=time.monotonic() if now is None else now
        return (not self.dock_edge and not self.auto_movement_disabled and bool(self.movement_slots) and now-self.last_interaction>=15.0
                and now>=self.movement_ready_at and not self.desktop_hidden()
                and not self.task_active and not self.quota_inflight and not self.dragging
                and not self.pointer_down and not self.click_timer.isActive()
                  and not (getattr(self,'size_dialog',None) and self.size_dialog.isVisible())
                  and not (getattr(self,'bubble_settings_dialog',None) and self.bubble_settings_dialog.isVisible())
                and QApplication.activePopupWidget() is None)
    def start_movement(self,item,manual=False):
        if self.desktop_hidden() or (self.task_active and not manual) or self.dragging:return False
        if manual:self.undock()
        if manual:
            self.note_interaction();self.click_timer.stop();self.last_click=None
        if not manual and time.monotonic()<self.movement_ready_at:return False
        self.ensure_on_screen()
        screen=QApplication.screenAt(self.frameGeometry().center()) or QApplication.primaryScreen()
        area=screen.availableGeometry();left=area.left();right=area.right()-self.width()+1
        bottom=area.bottom()-self.height()+1
        if self.chat and self.chat.enabled:
            shift=max(0,(self.chat.composer.width()-self.width())//2)
            left+=shift;right-=shift;bottom-=8+self.chat.composer.height()
        if right<left or bottom<area.top():return False
        origin=self.pos()
        facing=item.get('sourceFacing','right')
        directions=(-1,) if facing=='left' else (1,)
        run_ms=item.get('moveStopMs',5583)-item.get('moveStartMs',3000)
        speed=item.get('movementSpeed',.5 if item.get('number')=='01' else 1.0)
        route=plan_movement((origin.x(),origin.y()),(left,area.top(),right,bottom),self.width(),allowed_directions=directions,distance_scale=max(.25,run_ms/2583)*speed,angle_range=item.get('movementAngleRange'),downward_angle_range=[45,65] if item.get('movementAngleRange') else None)
        if route is None:
            if not manual:return False
            # Still allow a manual preview at an edge, without moving outwards.
            route=((origin.x(),origin.y()),False)
        self.play(item,'movement')
        target,_=route
        self.movement_plan=(origin,QPoint(round(target[0]),round(target[1])))
        self.movement_mirrored=False
        self.generation+=1;self.bubble_timer.stop();self.bubble.hide()
        return True
    def advance_movement(self,elapsed):
        if self.movement_plan is None:return
        origin,target=self.movement_plan
        fraction=movement_fraction(elapsed*1000,self.current.get('moveStartMs',0),
                                   self.current.get('moveStopMs',self.current['durationMs']))
        position=QPoint(round(origin.x()+(target.x()-origin.x())*fraction),
                        round(origin.y()+(target.y()-origin.y())*fraction))
        self.move(self.constrained_position(position))
        if any(panel.isVisible() for panel in self.panels()):self.position_bubble()
    def desktop_hidden(self):
        return self.manual_hidden or self.fullscreen_hidden
    def dialogue_hidden(self):
        return self.desktop_hidden()
    def speech_hidden(self):
        return self.desktop_hidden() or (self.mode=='working' and self.current and self.current.get('id','').startswith('work'))
    def sync_dialogue_visibility(self):
        if self.speech_hidden() and not self.bubble.is_size_preview and self.bubble.isVisible():self.bubble.hide()
        if self.dialogue_hidden():
            for panel in self.panels():panel.hide()
            if self.chat and self.chat.composer.isVisible():self.chat.composer.hide()
        elif self.chat and self.chat.enabled:
            if not self.chat.composer.isVisible():
                self.chat.composer.show();self.chat.position();self.position_bubble()
    def set_auto_hide_fullscreen(self,enabled):
        self.note_interaction()
        self.auto_hide_fullscreen=enabled
        self.settings.setValue('autoHideFullscreen',enabled)
        if not enabled:self.fullscreen_hidden=False;self.apply_visibility()
        self.fullscreen_candidate=False;self.fullscreen_candidate_since=time.monotonic()
    def set_auto_movement_disabled(self,disabled):
        self.note_interaction()
        self.auto_movement_disabled=disabled
        self.settings.setValue('autoMovementDisabled',disabled)
    def set_manual_hidden(self,hidden):
        self.note_interaction()
        if hidden and not self.tray.isVisible():
            self.say('主人，托盘暂时没准备好，我先留在这里陪你。');return
        self.manual_hidden=hidden;self.apply_visibility()
    def apply_visibility(self):
        if self.desktop_hidden():
            if self.hidden_since is None:self.hidden_since=time.monotonic()
            self.dock_click_timer.stop()
            self.tick.stop();self.click_timer.stop();self.last_click=None;self.dragging=False
            self.pointer_down=False
            self.hide()
            self._restore_panels=list(dict.fromkeys(getattr(self,'_restore_panels',[])+[panel for panel in self.panels() if panel.isVisible()]))
            for panel in self.panels():panel.hide()
            if self.chat:self.chat.composer.hide()
        else:
            if self.hidden_since is not None:
                self.started+=time.monotonic()-self.hidden_since;self.hidden_since=None
            self.ensure_on_screen();self.show();self.tick.start()
            for panel in getattr(self,'_restore_panels',[]):panel.show()
            self._restore_panels=[];self.position_bubble()
            self.sync_dialogue_visibility()
        if hasattr(self,'tray'):
            state='已隐藏' if self.manual_hidden else ('全屏时自动隐藏' if self.fullscreen_hidden else '正在陪伴主人')
            self.tray.setToolTip('GPT白龙娘桌宠 · '+state)
    def check_fullscreen(self):
        if self.tray_menu.isVisible():return
        state=self.auto_hide_fullscreen and self.fullscreen_detector.active();now=time.monotonic()
        if state!=self.fullscreen_candidate:
            self.fullscreen_candidate=state;self.fullscreen_candidate_since=now
        # Hide promptly; wait longer before restoring to avoid focus-change flicker.
        if state!=self.fullscreen_hidden and now-self.fullscreen_candidate_since>=(.25 if state else .75):
            self.fullscreen_hidden=state;self.apply_visibility()
        if self.desktop_hidden():
            for panel in self.panels():panel.hide()
            if self.chat:self.chat.composer.hide()
        self.sync_dialogue_visibility()
    def constrained_position(self,position,pointer=None):
        screens=QApplication.screens()
        # Allow crossing monitors, but never use their bounding union: it can
        # contain empty gaps or permit half the pet to hang off a monitor.
        screen=QApplication.screenAt(pointer) if pointer is not None else None
        def constrain(area):
            if self.chat and self.chat.enabled:
                width=max(self.width(),self.chat.composer.width());shift=(width-self.width())//2
                size=QSize(width,self.height()+8+self.chat.composer.height())
                return clamp_position(position-QPoint(shift,0),size,area)+QPoint(shift,0)
            return clamp_position(position,self.size(),area)
        if screen:return constrain(screen.availableGeometry())
        candidates=[constrain(s.availableGeometry()) for s in screens]
        return min(candidates,key=lambda p:(p.x()-position.x())**2+(p.y()-position.y())**2) if candidates else position
    def dock_rect(self):
        size=112;w=self.width();h=self.height()
        return QRectF({'left':0,'right':w-size}.get(self.dock_edge,(w-size)//2),
                      {'top':0,'bottom':h-size}.get(self.dock_edge,(h-size)//2),size,size)
    def try_dock(self):
        if self.chat and self.chat.enabled:return False
        screen=QApplication.screenAt(self.frameGeometry().center()) or QApplication.primaryScreen()
        area=screen.availableGeometry();p=self.pos()
        gaps={'left':abs(p.x()-area.left()),'right':abs(area.right()+1-p.x()-self.width())}
        edge=min(gaps,key=gaps.get)
        if gaps[edge]>24:return False
        self.finish_movement();self.dock_edge=edge
        self.dock_started=time.monotonic()
        if edge=='left':p.setX(area.left())
        elif edge=='right':p.setX(area.right()+1-self.width())
        self.move(p);self.bubble_timer.stop();self.bubble.hide()
        self.play(self.idles[0],'idle');return True
    def undock(self):
        if not self.dock_edge:return
        self.dock_click_timer.stop()
        self.dock_exit=None
        self.dock_edge=None;self.last_interaction=time.monotonic();self.update()
    def begin_undock(self):
        self.dock_click_timer.stop()
        if not self.dock_edge or getattr(self,'dock_exit',None):return
        elapsed=min(1,max(0,(time.monotonic()-self.dock_started)/.3))
        self.dock_exit=(time.monotonic(),1-(1-elapsed)**3)
        self.click_timer.stop();self.last_click=None;self.last_interaction=time.monotonic()
        self.update()
    def watch_screen(self,screen):
        screen.availableGeometryChanged.connect(lambda area:self.ensure_on_screen())
        QTimer.singleShot(0,self.ensure_on_screen)
    def ensure_on_screen(self):
        position=self.constrained_position(self.pos())
        if position!=self.pos():
            if self.mode=='movement':self.finish_movement();self.play(self.idles[0],'idle')
            self.move(position)
            if self.dragging:self.origin=self.pos();self.press_pos=QCursor.pos()
        if self.chat:self.chat.position()
        if any(panel.isVisible() for panel in self.panels()):self.position_bubble()
    def moveEvent(self,event):
        super().moveEvent(event)
        if getattr(self,'chat',None):self.chat.position()
        if hasattr(self,'quota_bubble'):self.position_bubble()
    def asset_path(self,item):
        # Optional playbackFile allows future formats without changing the manifest's video source.
        return ASSETS/(item.get('playbackFile') or item['file'].replace('.webm','.webp'))
    def load(self,item):
        path=self.asset_path(item);key=str(path)
        if key in self.pinned_frames:return self.pinned_frames[key]
        if key in self.cache: self.cache.move_to_end(key);return self.cache[key]
        frames=[]
        packed=ROOT/'app/cache'/f'{item["id"]}-hq.rgba.gz';metadata=packed.with_suffix('.json')
        if packed.is_file() and metadata.is_file():
            meta=json.loads(metadata.read_text(encoding='utf-8'));stat=path.stat()
            if meta['sourceSize']==stat.st_size and meta['sourceMtime']==stat.st_mtime_ns:
                raw=gzip.decompress(packed.read_bytes());stride=512*512*4
                if len(raw)==meta['frames']*stride:
                    for offset in range(0,len(raw),stride):
                        pixmap=QPixmap.fromImage(QImage(raw[offset:offset+stride],512,512,2048,QImage.Format.Format_RGBA8888).copy())
                        frames.append(pixmap.scaled(self.frame_size,self.frame_size,Qt.AspectRatioMode.IgnoreAspectRatio,Qt.TransformationMode.SmoothTransformation) if self.frame_size!=512 else pixmap)
        if frames:
            self.cache[key]=frames
            while len(self.cache)>2:self.cache.popitem(last=False)
            return frames
        if path.suffix.lower()=='.webm':
            from video_frames import rgba_frames
            for raw in rgba_frames(path,self.frame_size):
                qimage=QImage(raw,self.frame_size,self.frame_size,self.frame_size*4,QImage.Format.Format_RGBA8888).copy()
                frames.append(QPixmap.fromImage(qimage))
        else:
            with Image.open(path) as source:
                for i in range(source.n_frames):
                    source.seek(i);im=source.convert('RGBA').resize((self.frame_size,self.frame_size),Image.Resampling.LANCZOS)
                    raw=im.tobytes();qimage=QImage(raw,self.frame_size,self.frame_size,self.frame_size*4,QImage.Format.Format_RGBA8888).copy()
                    frames.append(QPixmap.fromImage(qimage))
        if not frames: raise RuntimeError('动画没有可播放帧：'+str(path))
        self.cache[key]=frames
        while len(self.cache)>2:self.cache.popitem(last=False)
        return frames
    def play(self,item,mode):
        if self.dock_edge and mode!='idle':return
        if self.movement_plan is not None:self.finish_movement()
        self.frames=self.load(item);self.current=item;self.mode=mode;self.index=0;self.started=time.monotonic();self.update_frame()
        if getattr(self,'hidden_since',None) is not None:self.hidden_since=self.started
        self.transition=None
        self.sync_dialogue_visibility()
    def idle(self):
        if self.dock_edge:
            self.play(random.choices(self.idles,weights=[7,3][:len(self.idles)])[0],'idle')
            return
        if self.task_active:
            work=self.regular_work_animations()
            previous=self.current.get('id') if self.current else None
            groups=[('working',work),('idle',self.idles)] if work else [('idle',self.idles)]
            candidates=[(mode,[v for v in pool if v['id']!=previous]) for mode,pool in groups]
            candidates=[g for g in candidates if g[1]]
            if not candidates:return
            mode,pool=random.choices(candidates,weights=[35 if mode=='working' else 65 for mode,pool in candidates])[0]
            self.last_task_group=mode
            self.play(random.choice(pool),mode)
            return
        if self.try_leisure():return
        item=random.choices(self.idles,weights=[4,1][:len(self.idles)])[0]
        self.play(item,'idle')
    def try_leisure(self):
        now=time.monotonic()
        if (now<self.next_leisure_at or now-self.last_interaction<15 or self.dock_edge
                or self.desktop_hidden() or self.task_active or self.quota_inflight or self.dragging
                or self.pointer_down or self.click_timer.isActive() or QApplication.activePopupWidget() is not None
                or any(getattr(self,n,None) and getattr(self,n).isVisible() for n in ('size_dialog','bubble_settings_dialog'))):return False
        if self.next_leisure_group=='movement' and self.movement_allowed(now):
            candidates=random.sample(self.movement_slots,len(self.movement_slots))
            for item in candidates:
                if self.start_movement(item):
                    self.next_leisure_group='idle';self.next_leisure_at=now+15;return True
        if self.extra_idles:
            self.play(random.choice(self.extra_idles),'leisure')
            self.next_leisure_group='movement' if not self.auto_movement_disabled else 'idle'
            self.next_leisure_at=now+15;return True
        return False
    def regular_work_animations(self):
        return [v for v in self.work_slots if v and v['id']!='work01' and self.asset_path(v).is_file()]
    def task_started(self):
        self.note_interaction()
        self.task_active=True
        self.last_task_group=None
        self.click_timer.stop();self.last_click=None
        work=[v for v in self.work_slots if v and self.asset_path(v).is_file()]
        opening=next((v for v in work if v['id']=='work01'),None)
        if opening and not self.dock_edge:self.play(opening,'work-start')
        else:self.idle()
    def task_finished(self):
        self.task_active=False
        if self.mode in ('work-start','working','idle'):self.idle()
    def return_transition_item(self,item):
        transition=item['returnTransition']
        return {'id':'return-'+item['id'],'file':transition['file'],
                'durationMs':transition['durationMs'],'renderScale':1.0,'renderOffset':[0,0]}
    def update_frame(self):
        # Windows layered windows use per-pixel alpha. A binary mask would
        # dither the soft character edges and damage transparent rendering.
        self.update()
    def animate(self):
        if getattr(self,'dock_exit',None) and time.monotonic()-self.dock_exit[0]>=.3:
            self.undock();self.idle()
        if self.dock_edge:self.update()
        duration=self.current.get('durationMs',4000)/1000
        elapsed=time.monotonic()-self.started
        # Automatic actions are selected by idle() only at clip boundaries.
        # Reaching the inactivity deadline must not cut an idle clip short.
        if self.mode=='movement':self.advance_movement(elapsed)
        if elapsed>=duration:
            if self.mode=='movement':self.finish_movement()
            if self.mode=='drag':self.play(self.manifest.get('dragAnimation') or self.idles[0],'drag')
            elif self.mode=='work-start' and self.task_active:
                work=self.regular_work_animations()
                if work:
                    self.last_task_group='working';self.play(random.choice(work),'working')
                else:self.idle()
            elif self.mode=='variant' and self.current.get('returnTransition'):
                self.play(self.return_transition_item(self.current),'return-idle')
            elif self.mode=='return-idle':self.play(self.idles[0],'idle')
            else:self.idle()
            return
        index=min(len(self.frames)-1,int(elapsed/duration*len(self.frames)))
        if self.transition:self.update()
        if index!=self.index:self.index=index;self.update_frame()
    def paintEvent(self,event):
        if self.frames:
            painter=QPainter(self);painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            self.draw_frame(painter,QRectF(self.rect()))
    def draw_frame(self,painter,canvas):
        painter.save()
        if self.dock_edge:
            # Rotate and slide the COMPLETE sprite beyond the edge. The
            # viewport clips only the off-screen body, never a head crop.
            t=min(1,max(0,(time.monotonic()-getattr(self,'dock_started',0))/.3))
            t=1-(1-t)**3
            if getattr(self,'dock_exit',None):
                progress=min(1,max(0,(time.monotonic()-self.dock_exit[0])/.3))
                t=self.dock_exit[1]*(1-progress)**3
            w,h=canvas.width(),canvas.height()
            pivot=QPointF(w*.5,h*.5)
            angle={'left':45,'right':-45,'top':180,'bottom':0}[self.dock_edge]
            rotation=QTransform();rotation.translate(pivot.x(),pivot.y());rotation.rotate(angle*t);rotation.translate(-pivot.x(),-pivot.y())
            # Match the reference's 55% exposure of the rotated visible bounds,
            # rather than anchoring a guessed head crop to a square viewport.
            stable=self.pinned_frames[str(self.asset_path(self.idles[0]))][0]
            if not hasattr(self,'dock_visible_bounds'):self.dock_visible_bounds=QRegion(stable.mask()).boundingRect()
            bounds=self.dock_visible_bounds
            local=QRectF(bounds.x()*w/stable.width(),bounds.y()*h/stable.height(),bounds.width()*w/stable.width(),bounds.height()*h/stable.height())
            rotated=rotation.mapRect(local);exposure=1-.45*t
            dx=dy=0.0
            if self.dock_edge=='left':dx=exposure*rotated.width()-rotated.right()
            elif self.dock_edge=='right':dx=w-exposure*rotated.width()-rotated.left()
            elif self.dock_edge=='top':dy=exposure*rotated.height()-rotated.bottom()
            else:dy=h-exposure*rotated.height()-rotated.top()
            painter.translate(dx*t,dy*t);painter.setTransform(rotation,True)
        if self.movement_mirrored:
            painter.translate(canvas.x()*2+canvas.width(),0);painter.scale(-1,1)
        scale=self.current.get('renderScale',1.0);offset=self.current.get('renderOffset',[0,0])
        width,height=canvas.width()*scale,canvas.height()*scale
        target=QRectF(canvas.x()+(canvas.width()-width)/2+canvas.width()*offset[0],canvas.y()+canvas.height()-height+canvas.height()*offset[1],width,height)
        frame=self.frames[self.index];painter.drawPixmap(target,frame,QRectF(frame.rect()))
        painter.restore()
    def snapshot_frame(self):
        pixmap=QPixmap(self.frame_size,self.frame_size);pixmap.fill(Qt.GlobalColor.transparent)
        painter=QPainter(pixmap);self.draw_frame(painter,QRectF(pixmap.rect()));painter.end();return pixmap
    def panels(self):
        return (self.bubble,self.task_bubble,self.quota_bubble)
    def position_bubble(self):
        if getattr(self,'_positioning_panels',False):return
        self._positioning_panels=True
        try:
            screen=QApplication.screenAt(self.frameGeometry().center()) or QApplication.primaryScreen()
            area=screen.availableGeometry();pet=self.frameGeometry()
            blockers=[]
            if self.chat and self.chat.enabled and self.chat.composer.isVisible():blockers.append(self.chat.composer.frameGeometry())
            # Reserve the toolbar even while hidden so hovering cannot move a
            # report that the user is reading.
            bar_size=self.hover_bar.size()
            bar_point=hover_position(pet,bar_size,area)
            if self.hover_bar.isVisible():self.hover_bar.move(bar_point)
            blockers.append(QRect(bar_point,bar_size))
            scale=getattr(self,'bubble_percent',100)/100
            defaults=(QSize(round(256*scale),round(52*scale)),QSize(round(300*scale),round(152*scale)),QSize(round(280*scale),round(146*scale)))
            sizes=[panel.size() if panel.layout.count() else default for panel,default in zip(self.panels(),defaults)]
            screen_key=(area.x(),area.y(),area.width(),area.height())
            side=getattr(self,'_panel_side',None) if getattr(self,'_panel_screen',None)==screen_key else None
            points,self._panel_side=layout_panels(pet,sizes,area,blockers,scale=scale,preferred_side=side)
            self._panel_screen=screen_key
            for panel,point in zip(self.panels(),points):panel.move(point)
        finally:self._positioning_panels=False
    def position_hover_bar(self):
        screen=QApplication.screenAt(self.frameGeometry().center()) or QApplication.primaryScreen()
        self.hover_bar.move(hover_position(self.frameGeometry(),self.hover_bar.size(),screen.availableGeometry()))
        self.position_bubble()
    def _keep_hover_bar(self):
        self.hover_hide_timer.stop();self.position_hover_bar()
    def _schedule_hover_bar_hide(self):
        self.hover_hide_timer.start()
    def _hide_hover_bar(self):
        self.hover_hide_timer.stop();self.hover_bar.hide();self.position_bubble()
    def _show_hover_bar(self):
        if self.desktop_hidden() or self.dock_edge:return
        self.hover_hide_timer.stop();self.position_hover_bar();self.hover_bar.show();self.hover_bar.raise_()
    def _hover_action(self):
        pool=self.interaction_animations()
        if pool:self.choose(random.choice(pool))
        self._hide_hover_bar()
    def enterEvent(self,event):
        self._show_hover_bar();super().enterEvent(event)
    def leaveEvent(self,event):
        self._schedule_hover_bar_hide();super().leaveEvent(event)
    def say(self,text,ms=4000):
        if self.speech_hidden():return
        self.bubble_timer.stop();self.bubble.message(text);self.position_bubble()
        if ms:self.bubble_timer.start(ms)
    def dismiss_bubble(self):
        self.bubble_timer.stop();self.bubble.hide()
        self._restore_panels=[panel for panel in getattr(self,'_restore_panels',[]) if panel is not self.bubble]
        self.position_bubble()
    def dismiss_task_bubble(self):
        self.task_timer.stop();self.task_bubble.hide()
        self._restore_panels=[panel for panel in getattr(self,'_restore_panels',[]) if panel is not self.task_bubble]
        self.position_bubble()
    def dismiss_quota_bubble(self):
        self.quota_timer.stop();self.quota_bubble.hide()
        self._restore_panels=[panel for panel in getattr(self,'_restore_panels',[]) if panel is not self.quota_bubble]
        self.position_bubble()
        if self.mode=='quota':self.idle()
    def task_result(self,title,text,details):
        if self.task_bubbles_disabled or self.desktop_hidden():return
        self.task_timer.stop()
        self.task_bubble.task_result(title,text,details);self.position_bubble()
        self.task_timer.start(30000 if len(text)>180 or len(text.splitlines())>6 else 15000)
    def task_progress(self,title,text,details):
        if self.task_bubbles_disabled or self.desktop_hidden():return
        panel=self.task_bubble;self.task_timer.stop()
        if getattr(panel,'is_task_progress',False) and panel.isVisible():
            view=panel.result_view;bar=view.verticalScrollBar()
            bottom=bar.value()>=bar.maximum()-8;old=bar.value()
            panel.result_title.setText(title);view.setPlainText(text)
            bar.setValue(bar.maximum() if bottom else min(old,bar.maximum()))
            panel.present()
        else:
            panel.task_result(title,text,details);panel.is_task_progress=True
        self.position_bubble()
        self.task_timer.start(15000)
    def task_completed(self,text,details):
        celebration=next((v for v in self.variants if v['id']=='18'),None)
        if celebration and not self.dock_edge:self.play(celebration,'task-celebration')
        # Play directly so the celebration's interaction line is never shown.
        self.task_result('主人，完成啦！',text,details)
    def chat_controller(self):
        if self.chat is None:
            from chat_ui import ChatController
            try:self.chat=ChatController(self)
            except Exception as error:
                self.say('唔，主人，对话模式暂时打不开啦：'+str(error),10000);return None
        return self.chat
    def toggle_chat(self):
        self.undock()
        self.note_interaction()
        controller=self.chat_controller()
        if controller:controller.toggle()
        self.sync_dialogue_visibility()
    def open_history(self):
        self.note_interaction()
        controller=self.chat_controller()
        if controller:controller.open_history()
    def context_menu(self):
        menu=QMenu(self)
        if self.dock_edge:menu.addAction('展开桌宠',self.begin_undock)
        action=menu.addAction('显示桌宠' if self.manual_hidden or self.fullscreen_hidden else '隐藏桌宠')
        action.triggered.connect(lambda checked=False:self.set_manual_hidden(not self.manual_hidden if not self.fullscreen_hidden else False))
        if self.fullscreen_hidden:
            menu.addAction('全屏结束后自动显示').setEnabled(False)
        menu.addSeparator()
        automatic=menu.addAction('全屏时自动隐藏');automatic.setCheckable(True)
        automatic.setChecked(self.auto_hide_fullscreen);automatic.toggled.connect(self.set_auto_hide_fullscreen)
        menu.addSeparator()
        menu.addAction('关闭对话模式' if self.chat and self.chat.enabled else '打开对话模式',self.toggle_chat)
        menu.addAction('查看历史对话',self.open_history)
        menu.addAction('刷新额度与中转站',self.quota)
        menu.addAction('中转站设置…',self.show_station_settings)
        codex_panel=menu.addAction('显示 Codex 额度卡片');codex_panel.setCheckable(True);codex_panel.setChecked(self.show_codex_panel);codex_panel.toggled.connect(self.set_codex_panel)
        station_panel=menu.addAction('显示 DogAPI 卡片');station_panel.setCheckable(True);station_panel.setChecked(self.show_station_panel);station_panel.toggled.connect(self.set_station_panel)
        menu.addAction('对话框大小…',self.show_bubble_settings)
        menu.addSeparator()
        interaction=QMenu('互动差分动画',menu);menu.addMenu(interaction)
        for item in self.variants:
            action=interaction.addAction(item['id']+' · '+item['name'])
            action.triggered.connect(lambda checked=False,v=item:self.choose(v))
        work=QMenu('工作差分动画',menu);menu.addMenu(work)
        available=[item for item in self.work_slots if item and self.asset_path(item).is_file()]
        for item in available:
            action=work.addAction(item['id']+' · '+item['name'])
            action.triggered.connect(lambda checked=False,v=item:self.choose(v))
        if not available:work.addAction('暂无动作（已预留6个位置）').setEnabled(False)
        idle=QMenu('待机差分动画',menu);menu.addMenu(idle)
        for index,item in enumerate(self.idles+self.extra_idles):
            name='默认待机' if index==0 else ('待机动作02' if index==1 else item['name'])
            action=idle.addAction(name);action.triggered.connect(lambda checked=False,v=item:self.preview_idle(v))
        movement=QMenu('移动差分',menu);menu.addMenu(movement)
        for item in self.movement_slots:
            action=movement.addAction(item.get('number',item['id'])+' · '+item['name'])
            action.triggered.connect(lambda checked=False,v=item:self.start_movement(v,manual=True))
        if not self.movement_slots:movement.addAction('暂无动作').setEnabled(False)
        movement_switch=menu.addAction('禁用自移动');movement_switch.setCheckable(True)
        movement_switch.setChecked(self.auto_movement_disabled)
        movement_switch.toggled.connect(self.set_auto_movement_disabled)
        # Keep Python wrappers alive as well as Qt's native ownership tree.
        menu._submenus=(interaction,work,idle,movement)
        menu.addSeparator();menu.addAction('桌宠大小…',self.show_size_dialog)
        muted=menu.addAction('禁用音效');muted.setCheckable(True)
        muted.setChecked(self.sound_disabled);muted.toggled.connect(self.set_sound_disabled)
        menu.addSeparator()
        menu.addAction('退出桌宠',self.close)
        return menu

    def show_station_settings(self):
        """Configure the read-only DogAPI API key without exposing it in the UI."""
        dialog=QDialog(self,Qt.WindowType.Tool|Qt.WindowType.WindowStaysOnTopHint)
        dialog.setWindowTitle('DogAPI 中转站设置')
        layout=QVBoxLayout(dialog);layout.setContentsMargins(18,16,18,16);layout.setSpacing(11)
        form=QFormLayout();form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        base=QLineEdit(str(self.settings.value('stationBaseUrl',DEFAULT_BASE_URL)),dialog);base.setMinimumWidth(300)
        key=QLineEdit(dialog);key.setEchoMode(QLineEdit.EchoMode.Password);key.setPlaceholderText('输入 API Key（sk-…），留空保持已保存密钥')
        clear=QCheckBox('清除已保存的 API Key',dialog)
        billing=QLineEdit(dialog);billing.setEchoMode(QLineEdit.EchoMode.Password);billing.setPlaceholderText('网站用户 Access Token；留空保留已保存的令牌')
        account=QLineEdit(str(self.settings.value('stationAccountId','')),dialog);account.setPlaceholderText('个人资料中的数字账号 ID')
        clear_billing=QCheckBox('清除已保存的账单访问令牌',dialog)
        form.addRow('接口地址：',base);form.addRow('API Key：',key);form.addRow('',clear)
        form.addRow('账单访问令牌：',billing);form.addRow('账号 ID：',account);form.addRow('',clear_billing);layout.addLayout(form)
        hint=QLabel('钱包余额使用 API Key；完整的今日消耗需要网站用户 Access Token。\n'
                    '获取位置：DogAPI 网站 → Profile（个人资料）→ Security（安全）→ Access Token。'
                    '网站打开此项会重新生成令牌，可能使旧令牌失效。账号 ID 可在个人资料姓名旁复制。\n'
                    '桌宠只使用这些设置查询余额和今日账单，不会生成或修改网站令牌。',dialog)
        hint.setMaximumWidth(450);hint.setWordWrap(True);hint.setStyleSheet('color:#7d6d8c;font-size:11px;');layout.addWidget(hint)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Save|QDialogButtonBox.StandardButton.Cancel,dialog);layout.addWidget(buttons)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText('保存');buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        def save():
            try:url=normalize_base_url(base.text())
            except StationFailure as error:QMessageBox.warning(dialog,'地址无效',str(error));return
            account_id=account.text().strip()
            if account_id and (not account_id.isascii() or not account_id.isdecimal() or int(account_id)<=0):
                QMessageBox.warning(dialog,'账号 ID 无效','请填写个人资料中的数字账号 ID。');return
            self.settings.setValue('stationBaseUrl',url)
            if clear.isChecked():self.settings.remove('stationApiKey')
            elif key.text().strip():self.settings.setValue('stationApiKey',key.text().strip())
            self.settings.setValue('stationAccountId',account_id)
            if clear_billing.isChecked():self.settings.remove('stationBillingToken')
            elif billing.text().strip():self.settings.setValue('stationBillingToken',billing.text().strip())
            dialog.accept();self.say('DogAPI 设置已保存。正在刷新面板…',2500);self.quota()
        buttons.accepted.connect(save);buttons.rejected.connect(dialog.reject)
        dialog.adjustSize();self.position_size_dialog(dialog=dialog);dialog.show();dialog.raise_();dialog.activateWindow();dialog.exec()
    def set_sound_disabled(self,disabled):
        self.sound_disabled=bool(disabled);self.settings.setValue('soundDisabled',self.sound_disabled);self._update_sound_button()
        if self.sound_disabled:
            if sys.platform=='win32':
                import winsound
                winsound.PlaySound(None,0)
            elif hasattr(self,'click_sound'):self.click_sound.stop()
    def _sound_presets(self):
        """List only installed WAV presets; metadata alone is not an audio asset."""
        folder=(ROOT/'app/sounds').resolve()
        try:
            entries=json.loads((folder/'presets.json').read_text(encoding='utf-8-sig'))
        except (OSError,ValueError):return {}
        if not isinstance(entries,list):return {}
        presets={}
        for entry in entries:
            if not isinstance(entry,dict):continue
            sound_id=entry.get('id');name=entry.get('name');file=entry.get('file')
            if not isinstance(sound_id,str) or not sound_id.strip() or sound_id in ('click','duck','custom'):continue
            if not isinstance(name,str) or not name.strip() or not isinstance(file,str):continue
            try:
                path=(folder/file).resolve()
                if path.suffix.lower()!='.wav' or not path.is_relative_to(folder) or not path.is_file():continue
            except (OSError,ValueError):continue
            presets[sound_id]={'name':name,'file':path}
        return presets
    def _sound_file(self):
        if self.sound_mode not in ('click','duck','custom'):
            preset=self._sound_presets().get(self.sound_mode)
            if preset:return preset['file']
            self.sound_mode='duck';self.settings.setValue('soundMode','duck');self._update_sound_button()
        if self.sound_mode=='custom':
            candidate=Path(self.sound_path).expanduser() if self.sound_path else None
            if candidate and candidate.is_file():return candidate
            self.sound_mode='duck';self.settings.setValue('soundMode','duck');self._update_sound_button()
        if self.sound_mode=='duck':
            candidate=ROOT/'app/sounds/duck_squeak.wav'
            if candidate.is_file():return candidate
            self.sound_mode='click';self.settings.setValue('soundMode','click');self._update_sound_button()
        return ROOT/'app/sounds/click.wav'
    def _update_sound_button(self):
        if not hasattr(self,'hover_bar') or not hasattr(self.hover_bar,'声音'):return
        self.hover_bar.声音.setText('声音')
        preset=self._sound_presets().get(self.sound_mode)
        name='已静音' if self.sound_disabled else (preset['name'] if preset else '小黄鸭 · 爱给网' if self.sound_mode=='duck' else (self.sound_name or '自定义音效') if self.sound_mode=='custom' else '原点击声')
        self.hover_bar.声音.setToolTip('当前：'+name+'\n点击更换、添加或试听声音')
    def set_sound_mode(self,mode,path=''):
        self.sound_mode=mode if mode in ('click','duck','custom') or mode in self._sound_presets() else 'duck'
        if self.sound_mode=='custom':
            candidate=Path(path or self.sound_path)
            if not candidate.is_file():
                self.sound_mode='duck';self.settings.setValue('soundMode','duck');self._update_sound_button()
                QMessageBox.warning(self,'音效文件不存在','之前的音效文件已经移走，已恢复小黄鸭声音。请重新添加音效。');return
            self.sound_path=str(candidate)
        self.sound_disabled=False
        self.settings.setValue('soundMode',self.sound_mode);self.settings.setValue('soundPath',self.sound_path);self.settings.setValue('soundDisabled',False)
        self._update_sound_button();self.play_click_sound()
    def show_sound_menu(self):
        menu=QMenu(self)
        if (ROOT/'app/sounds/duck_squeak.wav').is_file():
            duck=menu.addAction('小黄鸭 · 爱给网');duck.setCheckable(True);duck.setChecked(not self.sound_disabled and self.sound_mode=='duck');duck.triggered.connect(lambda:self.set_sound_mode('duck'))
        click=menu.addAction('原点击声');click.setCheckable(True);click.setChecked(not self.sound_disabled and self.sound_mode=='click');click.triggered.connect(lambda:self.set_sound_mode('click'))
        for sound_id,preset in self._sound_presets().items():
            action=menu.addAction(preset['name']);action.setCheckable(True);action.setChecked(not self.sound_disabled and self.sound_mode==sound_id)
            action.triggered.connect(lambda checked=False,key=sound_id:self.set_sound_mode(key))
        if self.sound_path:
            custom=menu.addAction(self.sound_name or Path(self.sound_path).stem);custom.setCheckable(True);custom.setChecked(not self.sound_disabled and self.sound_mode=='custom');custom.triggered.connect(lambda:self.set_sound_mode('custom'))
        menu.addSeparator()
        preview=menu.addAction('试听当前音效');preview.triggered.connect(lambda:self.play_click_sound(preview=True))
        add=menu.addAction('添加或更换本地音效…');add.setEnabled(self.sound_import is None);add.triggered.connect(self.choose_sound_file)
        menu.addSeparator();mute=menu.addAction('关闭声音');mute.setCheckable(True);mute.setChecked(self.sound_disabled);mute.triggered.connect(self.set_sound_disabled)
        menu.exec(self.hover_bar.mapToGlobal(QPoint(0,self.hover_bar.height()+4)))
        menu.deleteLater()
    def choose_sound_file(self):
        if self.sound_import is not None:return
        path,_=QFileDialog.getOpenFileName(self,'选择音效文件','', '音频文件 (*.wav *.mp3 *.ogg *.flac);;所有文件 (*.*)')
        if path:self.import_sound_file(path)
    def import_sound_file(self,path):
        """Decode and keep an imported sound without blocking the UI."""
        if self.sound_import is not None:return
        import re
        from video_frames import ffmpeg_path
        try:
            source=Path(path)
            if not source.is_file():raise ValueError('所选音效文件不存在，请重新选择。')
            ffmpeg=ffmpeg_path()
            # Keep imported media outside both the C drive and release trees.
            if sys.platform=='win32':
                if not Path('D:/').is_dir():raise ValueError('D 盘不可用，暂时无法保存导入音效。')
                folder=Path('D:/CodexDragonBuild/UserData/sounds')
            else:folder=STATE_ROOT/'sounds'
            folder.mkdir(parents=True,exist_ok=True)
            stem=re.sub(r'[^\w\- ]','_',source.stem)[:60].strip() or '音效'
            target=folder/f'{stem}-{time.time_ns()}.wav'
            pending=target.with_suffix('.importing.wav')
        except (OSError,ValueError,RuntimeError) as error:
            QMessageBox.warning(self,'音效导入失败',str(error));return
        process=QProcess(self)
        progress=QProgressDialog('正在添加音效…','取消',0,0,self);progress.setWindowTitle('添加声音');progress.setWindowModality(Qt.WindowModality.NonModal);progress.setMinimumDuration(0)
        timer=QTimer(self);timer.setSingleShot(True);timer.setInterval(60000)
        self.sound_import={'process':process,'progress':progress,'timer':timer,'pending':pending,'target':target,'name':source.stem,'cancelled':False,'timeout':False}
        progress.canceled.connect(self.cancel_sound_import)
        timer.timeout.connect(self._sound_import_timeout)
        process.finished.connect(self._finish_sound_import)
        process.errorOccurred.connect(lambda error:self._finish_sound_import(-1) if error==QProcess.ProcessError.FailedToStart else None)
        process.start(ffmpeg,['-hide_banner','-loglevel','error','-nostdin','-i',str(source),'-map','0:a:0','-vn','-ac','1','-ar','44100','-c:a','pcm_s16le','-threads','1','-y',str(pending)])
        timer.start();progress.show()
    def _sound_import_timeout(self):
        if self.sound_import:
            self.sound_import['timeout']=True;self.sound_import['process'].kill()
    def cancel_sound_import(self,closing=False):
        if self.sound_import:
            job=self.sound_import;job['cancelled']=True
            job['process'].kill()
            if closing:job['process'].waitForFinished(1500)
    def _finish_sound_import(self,code,*args):
        job=self.sound_import
        if not job:return
        self.sound_import=None;job['timer'].stop();job['timer'].deleteLater();job['progress'].close();job['progress'].deleteLater();job['process'].deleteLater()
        import wave
        error=None
        try:
            if job['cancelled']:return
            if job['timeout']:raise ValueError('音效处理超时，请选择较短的音频。')
            if code!=0:raise ValueError('无法读取这个音频，请换用有效的 WAV、MP3、OGG 或 FLAC 文件。')
            with wave.open(str(job['pending']),'rb') as audio:
                if audio.getnframes()==0:raise ValueError('所选音频没有可播放的声音。')
            job['pending'].replace(job['target'])
            self.sound_name=job['name'];self.settings.setValue('soundName',self.sound_name)
            self.set_sound_mode('custom',str(job['target']))
        except (OSError,ValueError,EOFError,wave.Error) as problem:error=str(problem)
        finally:
            try:job['pending'].unlink(missing_ok=True)
            except OSError:pass
        if error:QMessageBox.warning(self,'音效导入失败',error)
    def show_hover_menu(self):
        menu=self.context_menu();menu.exec(self.hover_bar.mapToGlobal(QPoint(self.hover_bar.width(),self.hover_bar.height()+4)));menu.deleteLater()
    def set_codex_panel(self,enabled):
        self.show_codex_panel=bool(enabled);self.settings.setValue('showCodexPanel',self.show_codex_panel)
        if self.quota_bubble.isVisible():self.quota()
    def set_station_panel(self,enabled):
        self.show_station_panel=bool(enabled);self.settings.setValue('showStationPanel',self.show_station_panel)
        if self.quota_bubble.isVisible():self.quota()
    def set_task_bubbles_disabled(self,disabled):
        self.task_bubbles_disabled=bool(disabled)
        self.settings.setValue('taskBubblesDisabled',self.task_bubbles_disabled)
        if disabled:self.dismiss_task_bubble()
    def set_bubble_percent(self,value):
        self.bubble_percent=max(50,min(150,int(value)))
        for panel in self.panels():panel.set_scale_percent(self.bubble_percent)
        self.settings.setValue('bubblePercent',self.bubble_percent)
        if any(panel.isVisible() for panel in self.panels()):self.position_bubble()
    def show_bubble_settings(self):
        if not hasattr(self,'bubble_settings_dialog'):
            dialog=QDialog(self,Qt.WindowType.Tool|Qt.WindowType.WindowStaysOnTopHint)
            dialog.setWindowTitle('对话框大小与提醒');layout=QVBoxLayout(dialog)
            layout.setContentsMargins(18,16,18,16);layout.setSpacing(14)
            label=QLabel(dialog);slider=QSlider(Qt.Orientation.Horizontal,dialog)
            slider.setRange(50,150);slider.setMinimumWidth(280)
            slider.valueChanged.connect(self.set_bubble_percent)
            slider.valueChanged.connect(lambda value:label.setText(f'对话框大小：{value}%（50%—150%）'))
            reset=QPushButton('恢复默认大小',dialog);reset.clicked.connect(lambda:slider.setValue(100))
            mute=QCheckBox('禁用聊天气泡提醒',dialog)
            mute.setToolTip('关闭任务接收、进展、异常和完成气泡；互动与额度气泡不受影响。')
            mute.toggled.connect(self.set_task_bubbles_disabled)
            done=QPushButton('完成',dialog);done.clicked.connect(dialog.accept)
            dialog.finished.connect(self.finish_bubble_preview)
            for widget in (label,slider,reset,mute,done):layout.addWidget(widget)
            self.bubble_settings_dialog=dialog;self.bubble_slider=slider;self.bubble_size_label=label
            self.bubble_reset=reset;self.task_bubble_checkbox=mute
        if not self.bubble.isVisible():
            self.bubble_timer.stop();self.bubble.size_preview();self.position_bubble()
        self.bubble_size_label.setText(f'对话框大小：{self.bubble_percent}%（50%—150%）')
        self.bubble_slider.setValue(self.bubble_percent)
        self.task_bubble_checkbox.setChecked(self.task_bubbles_disabled)
        self.bubble_settings_dialog.adjustSize()
        self.position_size_dialog(reserve=True,dialog=self.bubble_settings_dialog)
        self.bubble_settings_dialog.show()
        self.position_size_dialog(reserve=True,dialog=self.bubble_settings_dialog)
        self.bubble_settings_dialog.raise_();self.bubble_settings_dialog.activateWindow()
    def finish_bubble_preview(self):
        if self.bubble.is_size_preview:
            self.bubble.hide();self.bubble.is_size_preview=False;self.bubble.clear()
    def show_size_dialog(self):
        if not hasattr(self,'size_dialog'):
            dialog=QDialog(self,Qt.WindowType.Tool|Qt.WindowType.WindowStaysOnTopHint)
            dialog.setWindowTitle('调节桌宠大小');layout=QVBoxLayout(dialog)
            layout.setContentsMargins(18,16,18,16);layout.setSpacing(14)
            label=QLabel(dialog);slider=QSlider(Qt.Orientation.Horizontal,dialog)
            slider.setRange(50,200);slider.setMinimumWidth(280)
            slider.valueChanged.connect(self.set_size_percent)
            slider.valueChanged.connect(lambda value:label.setText(f'桌宠大小：{value}%（50%—200%）'))
            close=QPushButton('完成',dialog);close.clicked.connect(dialog.accept)
            reset=QPushButton('恢复默认',dialog);reset.clicked.connect(lambda:slider.setValue(100))
            layout.addWidget(label);layout.addWidget(slider);layout.addWidget(reset);layout.addWidget(close)
            self.size_dialog=dialog;self.size_slider=slider;self.size_label=label
            self.size_reset=reset
        self.size_label.setText(f'桌宠大小：{self.size_percent}%（50%—200%）')
        self.size_slider.setValue(self.size_percent)
        self.size_dialog.adjustSize()
        self.position_size_dialog(reserve=True)
        self.size_dialog.show();self.position_size_dialog(reserve=True)
        self.size_dialog.raise_();self.size_dialog.activateWindow()
    def position_size_dialog(self,reserve=False,dialog=None):
        dialog=dialog or getattr(self,'size_dialog',None)
        if dialog is None:return
        pet=self.frameGeometry()
        screen=QApplication.screenAt(pet.center()) or QApplication.primaryScreen()
        area=screen.availableGeometry();size=dialog.frameGeometry().size()
        if not reserve and not dialog.frameGeometry().intersects(pet.adjusted(-12,-12,12,12)):return
        occupied=QRect(pet)
        if reserve:
            for pixels in (128,512):
                point=clamp_position(QPoint(pet.center().x()-pixels//2,pet.bottom()+1-pixels),QSize(pixels,pixels),area)
                occupied=occupied.united(QRect(point,QSize(pixels,pixels)))
            if dialog is getattr(self,'bubble_settings_dialog',None) and self.bubble.isVisible():
                # Keep the options panel away from the bubble being previewed,
                # including the extra space needed when scaled up to 150%.
                bubble=self.bubble.frameGeometry()
                occupied=occupied.united(bubble.adjusted(-150,-bubble.height()//2,150,bubble.height()//2))
        gap=20;w,h=size.width(),size.height()
        points=[QPoint(occupied.right()+gap,pet.center().y()-h//2),QPoint(occupied.left()-w-gap,pet.center().y()-h//2),
                QPoint(pet.center().x()-w//2,occupied.top()-h-gap),QPoint(pet.center().x()-w//2,occupied.bottom()+gap)]
        points=[clamp_position(point,size,area) for point in points]
        chosen=next((point for point in points if not QRect(point,size).intersects(occupied)),None)
        if chosen is None:
            # At extreme screen sizes choose the least overlap with the pet.
            def overlap(point):
                r=QRect(point,size).intersected(pet);return max(0,r.width())*max(0,r.height())
            chosen=min(points,key=overlap)
        dialog.move(chosen)
    def set_size_percent(self,value):
        value=max(50,min(200,int(value)))
        if value==self.size_percent:return
        self.note_interaction();center=self.frameGeometry().center();bottom=self.frameGeometry().bottom()
        self.size_percent=value;pixels=round(256*value/100)
        self.setFixedSize(pixels,pixels)
        self.move(self.constrained_position(QPoint(center.x()-pixels//2,bottom+1-pixels)))
        if self.dock_edge:
            screen=QApplication.screenAt(self.frameGeometry().center()) or QApplication.primaryScreen()
            area=screen.availableGeometry();position=self.pos()
            if self.dock_edge=='left':position.setX(area.left())
            elif self.dock_edge=='right':position.setX(area.right()+1-pixels)
            elif self.dock_edge=='top':position.setY(area.top())
            else:position.setY(area.bottom()+1-pixels)
            self.move(position)
        self.settings.setValue('sizePercent',value)
        if self.chat:self.chat.position()
        self.position_bubble()
        self.update()
    def preview_idle(self,item):
        self.note_interaction()
        self.click_timer.stop();self.last_click=None;self.generation+=1;self.play(item,'idle')
    def play_click_sound(self,preview=False):
        if self.sound_disabled and not preview:return
        sound=self._sound_file()
        # Ignore duplicate dispatch within one UI event; repeated clicks after
        # this short guard still play their own single, non-looping sound.
        now=time.monotonic();last=getattr(self,'_last_sound_play',None)
        if not preview and last and last[0]==sound and now-last[1]<.06:return
        if sys.platform=='darwin':
            from PySide6.QtMultimedia import QSoundEffect
            from PySide6.QtCore import QUrl
            if not hasattr(self,'click_sound'):
                self.click_sound=QSoundEffect(self)
            self.click_sound.setLoopCount(1)
            source=QUrl.fromLocalFile(str(sound))
            if self.click_sound.source()!=source:self.click_sound.setSource(source)
            self.click_sound.play();self._last_sound_play=(sound,now);return
        if sys.platform=='win32':
            import winsound
            if sound.is_file():
                try:
                    # Deliberately omit SND_LOOP: one input means one play.
                    winsound.PlaySound(str(sound),winsound.SND_FILENAME|winsound.SND_ASYNC|winsound.SND_NODEFAULT)
                    self._last_sound_play=(sound,now)
                except RuntimeError:
                    if preview:QMessageBox.warning(self,'音效无法播放','这个音效无法播放，请重新导入或切换到小黄鸭声音。')
    def choose(self,item):
        self.note_interaction()
        self.click_timer.stop();self.last_click=None;self.generation+=1
        try:self.play_click_sound();self.play(item,'variant');self.say(item.get('line',''),4000)
        except Exception as error:self.idle();self.say('唔，主人，这个动作暂时没能准备好：'+str(error))
    def single_click(self):
        self.last_click=None
        pool=self.interaction_animations()
        previous=self.current.get('id') if self.current else None
        available=[v for v in pool if v['id']!=previous] or pool
        if available:self.choose(random.choice(available))
    def interaction_animations(self):
        return self.variants+[v for v in self.work_slots if v and self.asset_path(v).is_file()]
    def quota(self):
        self.note_interaction()
        self.click_timer.stop();self.last_click=None;self.generation+=1
        self.play_click_sound()
        if not self.dock_edge:self.play(self.manifest.get('quotaAnimation') or self.idles[0],'quota')
        self.dashboard_requested=True
        # Reuse requests already in progress and keep their last successful
        # results visible. Animation clicks must not invalidate quota replies.
        if self.show_codex_panel and not self.quota_inflight:
            self.quota_inflight=True;self.quota_error=None
            self.worker=QuotaWorker(self);self.worker.ready.connect(self.quota_ready);self.worker.failed.connect(self.quota_failed);self.worker.start()
        if self.show_station_panel:self.refresh_station_silently()
        self._render_dashboard()
    def refresh_station_silently(self):
        """Refresh DogAPI every minute without opening a hidden dashboard."""
        if not self.show_station_panel:return
        context=self._station_context()
        base_url,api_key,billing_token,account_id=context
        # A response for an old key must never replace the newly configured
        # account's card. Credentials stay in memory, outside UI snapshots.
        if self.station_snapshot_context!=context:
            self.station_snapshot=None
        if not api_key:
            self.station_error='未配置 DogAPI API Key。右键桌宠 → 中转站设置…'
            self._render_dashboard();return
        if self.station_inflight:return
        self.station_inflight=True;self.station_error=None;self.station_request_context=context
        self.station_worker=StationWorker(api_key,base_url,self,billing_access_token=billing_token,account_id=account_id)
        self.station_worker.ready.connect(lambda snapshot,source=context:self.station_ready(snapshot,source))
        self.station_worker.failed.connect(lambda error,source=context:self.station_failed(error,source))
        self.station_worker.start();self._render_dashboard()
    def _station_context(self):
        return tuple(str(self.settings.value(name,default)).strip() for name,default in (
            ('stationBaseUrl',DEFAULT_BASE_URL),('stationApiKey',''),('stationBillingToken',''),('stationAccountId','')))
    def refresh_card(self,provider):
        self.note_interaction();self.dashboard_requested=True
        if provider=='station':self.refresh_station_silently()
        elif provider=='codex' and self.show_codex_panel and not self.quota_inflight:
            self.quota_inflight=True;self.quota_error=None
            self.worker=QuotaWorker(self);self.worker.ready.connect(self.quota_ready);self.worker.failed.connect(self.quota_failed);self.worker.start()
        self._render_dashboard()
    def quota_ready(self,windows):
        self.quota_inflight=False;self.quota_error=None
        self.current_quota_windows=windows;self._render_dashboard()
    def quota_failed(self,error):
        self.quota_inflight=False;self.quota_error=str(error);self._render_dashboard()
    def station_ready(self,snapshot,context=None):
        self.station_inflight=False
        current=self._station_context()
        if context is not None and context!=current:
            self.refresh_station_silently();self._render_dashboard();return
        self.station_snapshot=snapshot;self.station_snapshot_context=current;self.station_error=None;self._render_dashboard()
    def station_failed(self,error,context=None):
        self.station_inflight=False
        current=self._station_context()
        if context is not None and context!=current:
            self.refresh_station_silently();self._render_dashboard();return
        self.station_error=str(error);self._render_dashboard()
    def _render_dashboard(self):
        # Background refreshes update a card that is already open. They must
        # not reopen a dismissed panel or replace a conversation bubble.
        if self.desktop_hidden():
            self.dashboard_requested=False;return
        if not self.dashboard_requested and not self.quota_bubble.isVisible():return
        self.quota_bubble.dashboard(self.current_quota_windows,self.station_snapshot,self.station_error,self.show_codex_panel,self.show_station_panel,codex_error=self.quota_error,codex_loading=self.quota_inflight,station_loading=self.station_inflight)
        self.dashboard_requested=False
        self.position_bubble()
        if self.quota_inflight or self.station_inflight:self.quota_timer.stop()
        else:self.quota_timer.start(20000)
    def mousePressEvent(self,event):
        self._hide_hover_bar()
        if event.button()==Qt.MouseButton.LeftButton and self.dock_edge:
            self.consume_dock_release=True;self.pointer_down=False
            self.note_interaction(cancel_movement=False)
            self.dock_double_click=self.dock_click_timer.isActive()
            if self.dock_double_click:
                self.dock_click_timer.stop();self.quota()
            return
        self.note_interaction()
        if event.button()==Qt.MouseButton.RightButton:
            self.dock_click_timer.stop()
            self.click_timer.stop();self.last_click=None
            menu=self.context_menu();menu.exec(event.globalPosition().toPoint());menu.deleteLater();return
        if event.button()==Qt.MouseButton.LeftButton:
            self.pointer_down=True
            self.press_pos=event.globalPosition().toPoint();self.origin=self.pos();self.dragging=False
    def mouseDoubleClickEvent(self,event):
        # Qt may emit this before mousePressEvent for the second physical press.
        self.mousePressEvent(event)
    def mouseMoveEvent(self,event):
        if getattr(self,'consume_dock_release',False):return
        if event.buttons()&Qt.MouseButton.LeftButton and hasattr(self,'press_pos'):
            delta=event.globalPosition().toPoint()-self.press_pos
            if delta.manhattanLength()>8 and not self.dragging:
                self.dragging=True;self.click_timer.stop();self.last_click=None;self.generation+=1
                self.bubble_timer.stop();self.bubble.hide()
                self.play(self.manifest.get('dragAnimation') or self.idles[0],'drag')
            if self.dragging:
                self.move(self.constrained_position(self.origin+delta,event.globalPosition().toPoint()))
                self.position_bubble()
    def mouseReleaseEvent(self,event):
        if event.button()!=Qt.MouseButton.LeftButton:return
        if getattr(self,'consume_dock_release',False):
            self.consume_dock_release=False;self.pointer_down=False
            self.click_timer.stop();self.last_click=None
            if self.dock_edge and not self.dock_double_click:self.dock_click_timer.start()
            self.dock_double_click=False;return
        # A menu's closing mouse release can land on the moving pet, without
        # a matching press on the pet. It is not a new click or stop command.
        if not self.pointer_down and not self.dragging:return
        self.pointer_down=False;self.note_interaction()
        if self.dragging:
            self.dragging=False;self.last_click=None;self.click_timer.stop()
            if self.try_dock():return
            landing=self.manifest.get('landingAnimation')
            if landing:self.play(landing,'landing')
            else:self.idle()
            return
        now=time.monotonic()
        if self.last_click is not None and now-self.last_click<=.25:self.quota()
        else:self.last_click=now;self.click_timer.start()
    def closeEvent(self,event):
        if self.chat and not self.chat.shutdown():
            event.ignore();QTimer.singleShot(500,self.close);return
        self.cancel_sound_import(closing=True)
        self.desktop_sync.stop()
        self.tick.stop();self.visibility_timer.stop();self.station_timer.stop();self.tray.hide()
        self.bubble_timer.stop();self.task_timer.stop();self.quota_timer.stop()
        for panel in self.panels():panel.close()
        self.hover_bar.close()
        if self.worker and self.worker.isRunning():self.worker.cancel.set();self.worker.wait(3000)
        if self.station_worker and self.station_worker.isRunning():self.station_worker.cancel.set();self.station_worker.wait(3000)
        QApplication.quit();event.accept()

def main():
    if '--autostart' in sys.argv:
        from autostart import main as monitor
        return monitor()
    if '--self-test' in sys.argv:
        from selftest import run
        return run(ROOT)
    app=QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    if '--enable-autostart' in sys.argv or '--disable-autostart' in sys.argv:
        from autostart import enable_autostart,disable_autostart
        try:
            if '--enable-autostart' in sys.argv:
                enable_autostart();message='已开启：以后登录系统时，助手会自动运行；打开GPT后启动桌宠。\n如要移动桌宠文件夹，请先关闭自动启动，移好后再开启。'
            else:disable_autostart();message='已关闭桌宠自动启动。'
            QMessageBox.information(None,'白龙桌宠',message);return 0
        except Exception as error:QMessageBox.critical(None,'设置失败',str(error));return 1
    socket=QLocalSocket();socket.connectToServer('bailong-desktop-pet-v1')
    if socket.waitForConnected(150):return 0
    QLocalServer.removeServer('bailong-desktop-pet-v1');server=QLocalServer();server.listen('bailong-desktop-pet-v1')
    try:
        pet=Pet();pet.show();configure_mac_window(pet)
        for panel in pet.panels():configure_mac_window(panel)
        def reveal_existing_pet():
            while server.hasPendingConnections():
                connection=server.nextPendingConnection()
                connection.disconnectFromServer();connection.deleteLater()
            screen=QApplication.primaryScreen()
            if screen:
                area=screen.availableGeometry()
                pet.move(clamp_position(QPoint(area.center().x()-pet.width()//2,area.center().y()-pet.height()//2),pet.size(),area))
            pet.set_manual_hidden(False)
            if pet.desktop_hidden():return
            pet.showNormal();pet.show();pet.raise_()
            if sys.platform=='win32':
                import ctypes
                ctypes.windll.user32.ShowWindow(int(pet.winId()),4)
        server.newConnection.connect(reveal_existing_pet)
        def record_startup():
            # pythonw can inherit a hidden startup state from a launcher.
            # Explicitly show this app's own window without stealing focus.
            native_visible=pet.isVisible()
            if sys.platform=='win32':
                import ctypes
                handle=int(pet.winId())
                ctypes.windll.user32.ShowWindow(handle,4)
                native_visible=bool(ctypes.windll.user32.IsWindowVisible(handle))
            (STATE_ROOT/'app').mkdir(parents=True,exist_ok=True)
            (STATE_ROOT/'app/运行状态.json').write_text(json.dumps({'version':VERSION,'pid':os.getpid(),'visible':pet.isVisible(),'nativeVisible':native_visible,'windowId':int(pet.winId()),'variants':len(pet.variants),'position':[pet.x(),pet.y()],'displaySize':[pet.width(),pet.height()],'frameSize':[self_size for self_size in (pet.frames[0].width(),pet.frames[0].height())]},ensure_ascii=False),encoding='utf-8')
        QTimer.singleShot(1000,record_startup)
        if '--show-quota' in sys.argv:QTimer.singleShot(1200,pet.quota)
        def record_visibility():
            (STATE_ROOT/'app/显示诊断.json').write_text(json.dumps({'pid':os.getpid(),'visible':pet.isVisible(),'manualHidden':pet.manual_hidden,'fullscreenHidden':pet.fullscreen_hidden,'trayVisible':pet.tray.isVisible(),'trayAvailable':QSystemTrayIcon.isSystemTrayAvailable(),'syncWatching':pet.desktop_sync.timer.isActive(),'syncActive':len(pet.desktop_sync.running),'syncObserved':len(pet.desktop_sync.known),'platform':QApplication.platformName(),'position':[pet.x(),pet.y()],'windowId':int(pet.winId()),'quotaSide':getattr(pet,'_panel_side',None),'panels':{name:{'visible':panel.isVisible(),'rect':[panel.x(),panel.y(),panel.width(),panel.height()]} for name,panel in zip(('speech','report','quota','toolbar'),(*pet.panels(),pet.hover_bar))}},ensure_ascii=False),encoding='utf-8')
        diagnostic_timer=QTimer(pet);diagnostic_timer.setInterval(2000);diagnostic_timer.timeout.connect(record_visibility);diagnostic_timer.start()
        if '--preview' in sys.argv:
            QTimer.singleShot(500,lambda:pet.grab().save(str(STATE_ROOT/'app/桌宠窗口预览.png')))
            QTimer.singleShot(900,app.quit)
    except Exception as error:QMessageBox.critical(None,'白龙桌宠启动失败',str(error));return 1
    return app.exec()

if __name__=='__main__':sys.exit(main())
