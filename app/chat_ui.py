"""Pet composer, local transcript window and a background Codex worker."""
import html
import json
from pathlib import Path
import queue
import threading

from PySide6.QtCore import Qt, QThread, Signal, QObject, QTimer, QEvent, QPoint
from PySide6.QtGui import QPainter, QFontMetrics, QFont
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QPlainTextEdit, QTextBrowser, QListWidget, QSplitter, QFileDialog, QMessageBox,
    QInputDialog, QDialog, QDialogButtonBox, QFrame, QSizePolicy, QSizeGrip)

from chat_backend import HistoryStore, CodexSession, TaskCancelled

FLAGS = Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
STYLE = '''QWidget {font-family:"Microsoft YaHei","Segoe UI";font-size:12px;color:#38294d;background:#f7f2fc;}
QPushButton {background:#efe6f8;border:1px solid #dfd0ed;border-radius:10px;padding:6px 11px;color:#5b3e70;}
QPushButton:hover {background:#e4d3f2;} QPushButton:pressed {background:#d8c0e9;} QPushButton:disabled {color:#92869d;}
QPlainTextEdit,QTextBrowser,QListWidget {background:#fff;border:1px solid #e5d9ef;border-radius:12px;padding:8px;}
QListWidget {padding:6px;} QListWidget::item {padding:8px 9px;border-radius:9px;margin:2px 0;}
QListWidget::item:selected {background:#eadcf5;color:#38294d;}'''

CHAT_STYLE = '''
QWidget#chatWindow {background:transparent;}
QWidget#chatWindow QLabel {background:transparent;}
QFrame#windowCard {background:#fff;border:1px solid #d8c6e7;border-radius:20px;}
QFrame#contentShell {background:#fcfaff;border:1px solid #e5d9ee;border-radius:12px;}
QFrame#titleBar {background:transparent;border:0;}
QFrame#sidebarCard,QFrame#contentCard {background:#fff;border:1px solid #e7dcef;border-radius:13px;}
QFrame#composerCard {background:#fcfaff;border:1px solid #e4d6ee;border-radius:12px;}
QLabel#windowTitle {font-size:14px;font-weight:700;color:#65457e;}
QLabel#sectionTitle {font-size:13px;font-weight:700;color:#563b6b;}
QLabel#muted {color:#9888a5;font-size:10px;}
QWidget#chatWindow QLabel#syncBadge {background:#f1e8f8;color:#76528c;border-radius:9px;padding:4px 8px;font-size:10px;}
QLabel#pathLabel {color:#9584a2;font-size:10px;}
QPushButton#windowControl {background:transparent;border:0;border-radius:8px;padding:0;color:#806b90;font-size:14px;}
QPushButton#windowControl:hover {background:#eadcf5;color:#503768;}
QPushButton#newChat,QPushButton#folderButton {background:#f2eaf8;border:0;border-radius:8px;padding:5px 9px;font-size:11px;}
QPushButton#sendButton {background:#9a76b8;color:white;border:0;border-radius:8px;padding:5px 20px;}
QPushButton#sendButton:hover {background:#8763a4;}
QPushButton#sendButton:disabled {background:#d5c3e2;color:#fcfaff;}
QListWidget#conversationList {background:white;border:0;border-radius:0;padding:0;}
QListWidget#conversationList::item {padding:7px 8px;border-radius:7px;margin:2px 0;}
QListWidget#conversationList::item:selected {background:#eadef3;color:#674881;}
QPlainTextEdit#chatEditor {border:0;background:transparent;padding:6px;color:#574567;selection-background-color:#dfcbee;}
QTextBrowser#transcript {border:0;border-radius:0;background:white;padding:2px;color:#463550;selection-background-color:#dfcbee;}
QScrollBar:vertical {background:#f3edf8;width:7px;margin:0;border-radius:3px;}
QScrollBar::handle:vertical {background:#cbb2df;min-height:34px;border-radius:3px;}
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {height:0;}
QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical {background:none;}
QScrollBar:horizontal {background:#f3edf8;height:7px;margin:0;border-radius:3px;}
QScrollBar::handle:horizontal {background:#cbb2df;min-width:34px;border-radius:3px;}
QScrollBar::add-line:horizontal,QScrollBar::sub-line:horizontal {width:0;}
QSizeGrip {background:transparent;width:12px;height:12px;}
'''


class ElidedLabel(QLabel):
    """Keep long conversation names and paths inside their allotted row."""
    def __init__(self,text='',parent=None):
        super().__init__(text,parent)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred)

    def setText(self,text):
        super().setText(text);self.setToolTip(text)

    def paintEvent(self,event):
        painter=QPainter(self);painter.setPen(self.palette().windowText().color())
        painter.drawText(self.contentsRect(),Qt.AlignmentFlag.AlignVCenter|Qt.AlignmentFlag.AlignLeft,
                         QFontMetrics(self.font()).elidedText(self.text(),Qt.TextElideMode.ElideRight,self.contentsRect().width()))


class Composer(QWidget):
    send = Signal(str)
    closed = Signal()
    history = Signal()
    stop = Signal()
    folder = Signal()
    new_chat = Signal()

    def __init__(self):
        super().__init__(None, FLAGS)
        self.setWindowTitle('白龙娘 · 对话模式')
        self.setStyleSheet(STYLE)
        self.setFixedSize(280, 152)
        self.setStyleSheet(STYLE+'QPushButton {padding:4px 5px;font-size:11px;} QComboBox {padding:2px;font-size:11px;}')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(7, 5, 7, 6)
        layout.setSpacing(3)
        head = QHBoxLayout()
        self.status = QLabel('主人，今天想让我帮什么忙呀？')
        self.status.setWordWrap(True);self.status.setMaximumHeight(30)
        head.addWidget(self.status, 1)
        close = QPushButton('×'); close.setFixedWidth(22); close.clicked.connect(self.close)
        head.addWidget(close); layout.addLayout(head)
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText('主人，把想做的事告诉我吧…\nCtrl+Enter 发送，Enter 换行')
        layout.addWidget(self.editor, 1)
        row = QHBoxLayout()
        row.setSpacing(3)
        self.folder_button = QPushButton('文件夹'); self.folder_button.clicked.connect(self.folder)
        history = QPushButton('历史'); history.clicked.connect(self.history)
        self.stop_button = QPushButton('停止'); self.stop_button.clicked.connect(self.stop);self.stop_button.hide()
        self.new_button=QPushButton('开启新对话');self.new_button.clicked.connect(self.new_chat)
        self.send_button = QPushButton('发送');self.send_button.clicked.connect(lambda:self.stop.emit() if self.send_button.text()=='停止' else self.submit())
        for button in (self.folder_button, history, self.new_button, self.send_button):row.addWidget(button)
        layout.addLayout(row)
        from PySide6.QtGui import QShortcut, QKeySequence
        self.shortcut = QShortcut(QKeySequence('Ctrl+Return'), self)
        self.shortcut.activated.connect(self.submit)

    def submit(self):
        text = self.editor.toPlainText().strip()
        if text and self.send_button.isEnabled() and self.send_button.text()!='停止':
            self.send.emit(text)

    def busy(self, value):
        self.send_button.setText('停止' if value else '发送')
        self.folder_button.setEnabled(not value)
        self.new_button.setEnabled(not value)
        self.status.setText('主人，我正在认真忙着呢…' if value else '主人，今天想让我帮什么忙呀？')

    def closeEvent(self, event):
        self.hide(); self.closed.emit(); event.ignore()


class ChatWindow(QWidget):
    selected = Signal(str)
    new_chat = Signal()
    send = Signal(str)
    stop = Signal()
    folder = Signal()

    def __init__(self):
        super().__init__(None, Qt.WindowType.Window|Qt.WindowType.FramelessWindowHint)
        self.setWindowTitle('白龙聊天')
        self.setObjectName('chatWindow')
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFont(QFont('Microsoft YaHei UI',9))
        self.resize(920, 550)
        self.setMinimumSize(660, 440)
        self.setStyleSheet(STYLE + CHAT_STYLE)
        self._drag_offset=None
        layout = QVBoxLayout(self); layout.setContentsMargins(2, 2, 2, 2); layout.setSpacing(0)
        card = QFrame(); card.setObjectName('windowCard'); layout.addWidget(card)
        outer=QVBoxLayout(card);outer.setContentsMargins(10,10,10,10)
        shell=QFrame();shell.setObjectName('contentShell');outer.addWidget(shell)
        card_layout = QVBoxLayout(shell); card_layout.setContentsMargins(14, 8, 14, 8); card_layout.setSpacing(8)
        self.title_bar = QFrame(); self.title_bar.setObjectName('titleBar')
        self.title_bar.installEventFilter(self)
        self.title_bar.setFixedHeight(28)
        header = QHBoxLayout(self.title_bar); header.setContentsMargins(0,0,0,0); header.setSpacing(4)
        title = QLabel('白龙聊天'); title.setObjectName('windowTitle'); header.addWidget(title)
        title.installEventFilter(self);header.addStretch(1)
        self.status = QLabel('主人的叮嘱，我都记在这台电脑里啦');self.status.setObjectName('muted');self.status.hide()
        self.min_button=QPushButton('−');self.max_button=QPushButton('□');self.close_button=QPushButton('×')
        for button in (self.min_button,self.max_button,self.close_button):
            button.setObjectName('windowControl');button.setFixedSize(28,24);header.addWidget(button)
        self.min_button.setToolTip('最小化');self.max_button.setToolTip('最大化 / 还原');self.close_button.setToolTip('关闭聊天窗口')
        self.min_button.clicked.connect(self.showMinimized)
        self.max_button.clicked.connect(self._toggle_maximized)
        self.close_button.clicked.connect(self.close)
        card_layout.addWidget(self.title_bar)

        body = QHBoxLayout(); body.setSpacing(10); card_layout.addLayout(body, 1)
        sidebar = QFrame(); sidebar.setObjectName('sidebarCard'); sidebar.setFixedWidth(188); sidebar_layout = QVBoxLayout(sidebar); sidebar_layout.setContentsMargins(10, 10, 10, 10); sidebar_layout.setSpacing(8)
        side_header=QHBoxLayout()
        side_title = QLabel('会话'); side_title.setObjectName('sectionTitle');side_header.addWidget(side_title,1)
        new=QPushButton('+ 新建');new.setObjectName('newChat');new.clicked.connect(self.new_chat);side_header.addWidget(new);sidebar_layout.addLayout(side_header)
        self.list = QListWidget(); self.list.setObjectName('conversationList');self.list.setMinimumWidth(0); self.list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff);self.list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.list.currentItemChanged.connect(lambda item, old: self.selected.emit(item.data(Qt.ItemDataRole.UserRole)) if item else None)
        sidebar_layout.addWidget(self.list, 1)
        side_hint = QLabel('与 Codex 中的会话记录同步'); side_hint.setObjectName('muted'); side_hint.setWordWrap(True); sidebar_layout.addWidget(side_hint)
        body.addWidget(sidebar)

        content = QFrame(); content.setObjectName('contentCard'); content_layout = QVBoxLayout(content); content_layout.setContentsMargins(12, 12, 12, 10); content_layout.setSpacing(8)
        content_header=QHBoxLayout()
        self.chat_title = ElidedLabel('白龙聊天'); self.chat_title.setObjectName('sectionTitle');content_header.addWidget(self.chat_title,1)
        sync=QLabel('会话同步');sync.setObjectName('syncBadge');content_header.addWidget(sync);content_layout.addLayout(content_header)
        self.view = QTextBrowser(); self.view.setObjectName('transcript'); self.view.setOpenExternalLinks(False); self.view.setOpenLinks(False); content_layout.addWidget(self.view, 1)
        self.view.document().setDefaultFont(QFont('Microsoft YaHei UI',9))
        self.view.document().setDefaultStyleSheet('body {color:#463550;font-family:"Microsoft YaHei UI";} h3 {font-size:13px;margin:12px 0 6px;} p {margin:4px 0 10px;}')
        composer = QFrame(); composer.setObjectName('composerCard'); composer_layout = QVBoxLayout(composer); composer_layout.setContentsMargins(8, 6, 8, 6); composer_layout.setSpacing(4)
        composer.setFixedHeight(128)
        self.editor = QPlainTextEdit(); self.editor.setObjectName('chatEditor'); self.editor.setPlaceholderText('继续这段对话… Ctrl+Enter 发送'); composer_layout.addWidget(self.editor,1)
        composer_row = QHBoxLayout(); composer_row.setSpacing(6)
        self.folder_button=QPushButton('文件夹');self.folder_button.setObjectName('folderButton');self.folder_button.clicked.connect(self.folder);composer_row.addWidget(self.folder_button)
        composer_hint = QLabel('Enter 换行'); composer_hint.setObjectName('muted'); composer_row.addWidget(composer_hint, 1)
        self.stop_button = QPushButton('停止任务');self.stop_button.clicked.connect(self.stop);self.stop_button.hide()
        self.send_button = QPushButton('发送');self.send_button.setObjectName('sendButton');self.send_button.clicked.connect(self.submit)
        composer_row.addWidget(self.stop_button);composer_row.addWidget(self.send_button);composer_layout.addLayout(composer_row)
        content_layout.addWidget(composer)
        body.addWidget(content, 1)
        self.path_label = ElidedLabel(); self.path_label.setObjectName('pathLabel'); card_layout.addWidget(self.path_label)
        self.size_grip=QSizeGrip(self);self.size_grip.setToolTip('拖动调整窗口大小')
        from PySide6.QtGui import QShortcut, QKeySequence
        self.shortcut = QShortcut(QKeySequence('Ctrl+Return'), self); self.shortcut.activated.connect(self.submit)

    def _toggle_maximized(self):
        if self.isMaximized():self.showNormal()
        else:self.showMaximized()

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,'size_grip'):self.size_grip.move(self.width()-17,self.height()-17)

    def eventFilter(self, obj, event):
        if obj is getattr(self,'title_bar',None) or (isinstance(obj,QLabel) and obj.parent() is getattr(self,'title_bar',None)):
            if event.type()==QEvent.Type.MouseButtonDblClick and event.button()==Qt.MouseButton.LeftButton:
                self._toggle_maximized();self._drag_offset=None;return True
            if event.type()==QEvent.Type.MouseButtonPress and event.button()==Qt.MouseButton.LeftButton:
                handle=self.windowHandle()
                if handle and handle.startSystemMove():return True
                self._drag_offset=event.globalPosition().toPoint()-self.frameGeometry().topLeft();return True
            if event.type()==QEvent.Type.MouseMove and getattr(self,'_drag_offset',None) is not None and event.buttons() & Qt.MouseButton.LeftButton:
                self.move(event.globalPosition().toPoint()-self._drag_offset);return True
            if event.type()==QEvent.Type.MouseButtonRelease:
                self._drag_offset=None;return True
        return super().eventFilter(obj,event)

    def submit(self):
        text=self.editor.toPlainText().strip()
        if text and self.send_button.isEnabled():self.send.emit(text)

    def busy(self, value):
        self.send_button.setEnabled(not value);self.folder_button.setEnabled(not value)
        self.stop_button.setVisible(value)
        self.status.setText('主人，我正忙着呢，进展会记在这里哦' if value else '主人的叮嘱，我都记在这台电脑里啦')

    def closeEvent(self, event):
        # Closing this primary window must not quit the Tool-type desktop pet.
        self.hide();event.ignore()


class TaskWorker(QThread):
    event = Signal(str, object)
    ready = Signal(object)
    failed = Signal(str, bool)
    request = Signal(str, object)

    def __init__(self, cwd, thread_id, prompt, parent=None, mode='work', session=None):
        super().__init__(parent)
        self.cancel = threading.Event();self.responses=queue.Queue()
        self.cwd, self.thread_id, self.prompt = cwd, thread_id, prompt
        self.mode=mode;self.session=session

    def ask(self, method, params):
        self.request.emit(method, params)
        while not self.cancel.is_set():
            try:return self.responses.get(timeout=.1)
            except queue.Empty:continue
        raise TaskCancelled('主人，我先停下来啦。')

    def run(self):
        session=self.session or CodexSession(self.cwd,self.thread_id,self.cancel,self.event.emit,self.ask,mode=self.mode)
        self.session=session
        session.cancel=self.cancel;session.emit=self.event.emit;session.ask=self.ask
        try:self.ready.emit(session.run(self.prompt))
        except TaskCancelled as error:session.close();self.failed.emit(str(error), True)
        except Exception as error:session.close();self.failed.emit(str(error), False)


class ChatController(QObject):
    def __init__(self, pet, directory=None):
        super().__init__(pet)
        self.pet=pet;self.store=HistoryStore(directory)
        self.composer=Composer();self.window=ChatWindow()
        self.active=None;self.running_chat=None;self.running_turn=None;self.worker=None
        self.approval_dialog=None;self.enabled=False;self.closing=False
        self.dirty=False;self.save_error_shown=False
        self.default_cwd=self.store.directory/'任务文件'
        self.save_timer=QTimer(self);self.save_timer.setInterval(1000);self.save_timer.timeout.connect(self.flush);self.save_timer.start()
        self.render_timer=QTimer(self);self.render_timer.setSingleShot(True);self.render_timer.setInterval(80);self.render_timer.timeout.connect(self.render)
        self.composer.send.connect(self.submit);self.window.send.connect(self.submit)
        self.composer.closed.connect(self.hide)
        self.composer.history.connect(self.open_history)
        self.composer.new_chat.connect(self.new_chat)
        self.preview_timer=QTimer(self);self.preview_timer.setInterval(180);self.preview_timer.setSingleShot(True)
        self.preview_timer.timeout.connect(self.preview_reply)
        self.latest_preview_kind='output'
        self.window.selected.connect(self.select)
        self.window.new_chat.connect(self.new_chat)
        for source in (self.composer,self.window):
            source.stop.connect(self.stop);source.folder.connect(self.choose_folder)
        if self.store.conversations:self.active=self.store.conversations[-1]
        self.refresh_list()

    @property
    def busy(self):return self.running_turn is not None

    def toggle(self):
        if self.enabled:self.hide()
        else:
            self.enabled=True;self.composer.show();self.pet.ensure_on_screen();self.position()
            self.composer.activateWindow();self.composer.editor.setFocus()
            self.pet.sync_dialogue_visibility()

    def hide(self):
        self.enabled=False;self.composer.hide()

    def position(self):
        if self.enabled:
            self.composer.move(self.pet.x()+(self.pet.width()-self.composer.width())//2,self.pet.y()+self.pet.height()+8)

    def flush(self):
        if not self.dirty:return
        try:self.store.save();self.dirty=False
        except OSError as error:
            self.composer.status.setText('主人，记忆暂时存不下啦，请看看磁盘空间')
            if not self.save_error_shown:
                self.save_error_shown=True
                QMessageBox.warning(self.window,'历史保存失败','主人，这段话我还记得，但暂时没能存进电脑里：'+str(error))

    def refresh_list(self):
        from PySide6.QtWidgets import QListWidgetItem
        self.window.list.blockSignals(True);self.window.list.clear()
        for chat in reversed(self.store.conversations):
            item=QListWidgetItem(chat['title']);item.setData(Qt.ItemDataRole.UserRole,chat['id']);self.window.list.addItem(item)
            if self.active and chat['id']==self.active['id']:self.window.list.setCurrentItem(item)
        self.window.list.blockSignals(False);self.render()

    def select(self, chat_id):
        self.active=next((c for c in self.store.conversations if c['id']==chat_id),None);self.render()

    def new_chat(self):
        if self.busy:
            self.pet.say('主人，手头这件事还没忙完呢，完成或停止后，我们再开一段新对话好不好？');return
        self.active=self.store.new(self.active['cwd'] if self.active else self.default_cwd)
        self.refresh_list();self.window.editor.clear();self.composer.editor.clear()
        self.composer.status.setText('主人，新对话准备好啦。')

    def choose_folder(self):
        if self.busy:return
        folder=QFileDialog.getExistingDirectory(self.window if self.window.isVisible() else self.composer,'选择任务文件夹',str(self.active['cwd'] if self.active else self.default_cwd))
        if folder:
            # A different project gets its own Codex context.
            self.active=self.store.new(folder);self.refresh_list()
            self.composer.folder_button.setToolTip(folder)

    def submit(self, prompt):
        if self.busy or self.closing:return
        from quota import find_codex
        try:
            find_codex()
            if self.active is None:self.active=self.store.new(self.default_cwd)
            Path(self.active['cwd']).mkdir(parents=True,exist_ok=True)
        except Exception as error:
            self.pet.say('唔，主人，我暂时没能连上Codex：'+str(error),10000);return
        if self.worker and self.worker.isRunning():return
        turn={'prompt':prompt,'created':HistoryStore.now(),'status':'running','output':'','result':'','events':[]}
        self.active['turns'].append(turn)
        if len(self.active['turns'])==1:self.active['title']=prompt.replace('\n',' ')[:28]
        self.running_chat=self.active;self.running_turn=turn
        self.dirty=True;self.flush();self.refresh_list()
        self.composer.editor.clear();self.window.editor.clear()
        self.composer.busy(True);self.window.busy(True)
        self.pet.task_started()
        if not self.pet.task_bubbles_disabled:
            chat_id=self.active['id']
            self.pet.task_progress('主人，我收到啦！','这就认真帮你处理。',lambda:self.open_history(chat_id))
        previous=getattr(self.worker,'session',None) if self.worker else None
        mode='work'
        if previous and (previous.thread_id!=self.active.get('threadId') or previous.mode!=mode):previous.close();previous=None
        self.worker=TaskWorker(self.active['cwd'],self.active.get('threadId'),prompt,self,mode=mode,session=previous)
        self.worker.event.connect(self.receive);self.worker.ready.connect(self.completed);self.worker.failed.connect(self.failed)
        self.worker.request.connect(self.request);self.worker.start()

    def receive(self, kind, value):
        if not self.running_turn:return
        if kind=='thread':self.running_chat['threadId']=value
        elif kind=='output':
            self.running_turn['output']=value
            self.latest_preview_kind='output'
            if not self.preview_timer.isActive():self.preview_timer.start()
        elif kind in ('summary','summary_update'):
            self.running_turn['summary']=str(value) if kind=='summary_update' else self.running_turn.get('summary','')+str(value)
            self.latest_preview_kind='summary'
            if not self.preview_timer.isActive():self.preview_timer.start()
        elif kind=='progress':
            events=self.running_turn['events'];events.append(str(value));self.running_turn['events']=events[-250:]
            self.latest_preview_kind='progress'
            if not self.preview_timer.isActive():self.preview_timer.start()
        self.dirty=True
        if not self.render_timer.isActive():self.render_timer.start()

    def preview_reply(self):
        if not self.running_turn:return
        kind=self.latest_preview_kind
        if kind=='summary':
            text=self.running_turn.get('summary','');title='主人，我正在理清思路…（公开摘要）'
        elif kind=='progress':
            text='\n\n'.join(self.running_turn.get('events',[])[-2:]);title='主人，我忙到这一步啦…'
        else:
            text=self.running_turn.get('output','');title='主人，我正在回复你…'
        if text:
            chat_id=self.running_chat['id']
            self.pet.task_progress(title,text,lambda:self.open_history(chat_id))

    def finish(self):
        self.preview_timer.stop()
        self.pet.task_finished()
        self.running_turn=None;self.running_chat=None
        self.composer.busy(False);self.window.busy(False)
        self.dirty=True;self.flush();self.render()

    def completed(self, result):
        if not self.running_turn:return
        chat=self.running_chat
        self.running_turn.update(status='completed',result=result['text'],output=result['transcript'])
        self.running_chat['threadId']=result['threadId']
        self.finish()
        self.pet.task_completed(result['text'],lambda:self.open_history(chat['id']))

    def failed(self, error, cancelled):
        if not self.running_turn:return
        chat=self.running_chat
        self.running_turn.update(status='interrupted' if cancelled else 'failed',error=error)
        self.finish()
        self.pet.task_result('主人，我先停下来啦。' if cancelled else '唔，主人，这次遇到一点困难，还没有完成。',error,lambda:self.open_history(chat['id']))

    def stop(self):
        if self.worker and self.worker.isRunning():
            self.worker.cancel.set();self.composer.status.setText('主人，我正在收好手头的工作…')
            if self.approval_dialog:self.approval_dialog.reject()

    def request(self, method, params):
        worker=self.worker
        if not worker or worker.cancel.is_set():
            if worker:worker.responses.put(None)
            return
        if method in ('item/commandExecution/requestApproval','item/fileChange/requestApproval'):
            box=QMessageBox(self.window if self.window.isVisible() else self.composer)
            self.approval_dialog=box;box.setWindowTitle('主人，这一步想请你点点头')
            box.setText('主人，我需要执行下面这一步操作。' if 'commandExecution' in method else '主人，我需要修改这些文件。')
            box.setInformativeText(str(params.get('reason') or '请主人看看具体内容，再决定要不要让我继续哦。'))
            box.setDetailedText(json.dumps(params,ensure_ascii=False,indent=2))
            allow=box.addButton('允许这一次',QMessageBox.ButtonRole.AcceptRole)
            reject=box.addButton('拒绝',QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(reject);box.setEscapeButton(reject);box.exec()
            result={'decision':'accept' if box.clickedButton()==allow and not worker.cancel.is_set() else 'decline'}
            self.approval_dialog=None;worker.responses.put(result)
        elif method=='item/tool/requestUserInput':
            answers={}
            for question in params.get('questions',[]):
                options=[o['label'] for o in question.get('options') or []]
                dialog=QInputDialog(self.window if self.window.isVisible() else self.composer)
                self.approval_dialog=dialog;dialog.setWindowTitle(question.get('header','主人，我想再问清楚一点'))
                dialog.setLabelText(question['question'])
                if options:dialog.setComboBoxItems(options);dialog.setComboBoxEditable(True)
                if question.get('isSecret'):
                    from PySide6.QtWidgets import QLineEdit
                    dialog.setTextEchoMode(QLineEdit.EchoMode.Password)
                accepted=dialog.exec()==QDialog.DialogCode.Accepted
                if options:
                    from PySide6.QtWidgets import QComboBox
                    text=dialog.findChild(QComboBox).currentText()
                else:text=dialog.textValue()
                if not accepted:self.stop();break
                answers[question['id']]={'answers':[text]}
            self.approval_dialog=None;worker.responses.put({'answers':answers})
        elif method=='item/permissions/requestApproval':
            # Permission-profile expansion is not implemented by this client.
            self.receive('progress','主人，这一步需要更大的访问权限，我现在还不能处理，所以没有放行哦。')
            worker.responses.put({'permissions':{},'scope':'turn'})
        else:
            self.receive('progress','主人，这种操作我还没学会呢：'+method)
            worker.responses.put(None)

    def open_history(self, chat_id=None):
        if isinstance(chat_id,str):self.select(chat_id)
        self.refresh_list();self.window.show();self.window.raise_();self.window.activateWindow()

    def render(self):
        chat=self.active
        if not chat:
            self.window.chat_title.setText('白龙聊天')
            self.window.view.setPlainText('主人，我在这里等你呀。把想做的事写在下方，我们就可以开始啦。')
            self.window.path_label.setText('我会把任务文件放在这里：'+str(self.default_cwd));return
        self.window.chat_title.setText(chat.get('title') or '整理项目说明')
        self.window.path_label.setText('任务文件夹：'+chat['cwd'])
        def block(title,text,color='#38294d'):
            return '<h3 style="color:'+color+'">'+html.escape(title)+'</h3><p style="white-space:pre-wrap">'+html.escape(text)+'</p>'
        content=[]
        for turn in chat['turns']:
            content.append(block('主人 · '+turn['created'],turn['prompt'],'#6f4697'))
            if turn.get('summary'):content.append(block('思考摘要（公开）',turn['summary']))
            if turn.get('events'):content.append(block('执行记录','\n\n'.join(turn['events'])))
            output=turn.get('output','')
            result=turn.get('result','')
            if turn['status']=='completed' and result and output.endswith(result):
                output=output[:-len(result)].rstrip()
            if output:content.append(block('白龙娘 · 给主人的进展',output))
            if turn['status']=='completed':
                content.append(block('主人，完成啦！',turn.get('result','')))
            elif turn['status']=='running':content.append(block('主人，我正在努力处理呢…','进展都会写在这里哦。如果主人想让我先停一停，点击“停止任务”就好。'))
            else:content.append(block('主人，我先停下来啦' if turn['status']=='interrupted' else '主人，这件事还没做完呢',turn.get('error','')))
        bar=self.window.view.verticalScrollBar();at_bottom=bar.value()>=bar.maximum()-20;old=bar.value()
        self.window.view.setHtml(''.join(content) or '<p>主人，把想做的事写在下面吧，我准备好啦。</p>')
        bar.setValue(bar.maximum() if at_bottom else min(old,bar.maximum()))

    def shutdown(self):
        self.closing=True;self.stop();self.save_timer.stop();self.render_timer.stop()
        if self.worker and self.worker.isRunning():self.worker.wait(5000)
        if self.worker and not self.worker.isRunning() and getattr(self.worker,'session',None):self.worker.session.close()
        self.preview_timer.stop()
        self.flush();self.composer.hide();self.window.hide()
        return not self.worker or not self.worker.isRunning()
