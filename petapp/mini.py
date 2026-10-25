"""提供迷你聊天、任务进度、草稿和权限操作。"""
import json
from pathlib import Path
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QTextCursor
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QPlainTextEdit, QPushButton, QScrollArea,
                               QSizeGrip, QStackedWidget, QTextBrowser, QVBoxLayout, QWidget)

from .common import fmt_ago
from .panel import QSS, Bar, clear_layout, make_label, make_box
from .common import remain_rgb
from .sessions import STATE_RGB, clip


class PromptEdit(QPlainTextEdit):
    submitted = Signal()
    def keyPressEvent(self,event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and event.modifiers() & Qt.ControlModifier:
            self.submitted.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class MiniWindow(QWidget):
    def __init__(self, pet):
        super().__init__(None)
        self.pet, self.hub = pet, pet.chat
        self.setWindowTitle('Claude + Codex Mini')
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setStyleSheet(QSS + '''
          QPlainTextEdit, QTextBrowser { background: #fffcfe; border: 1px solid #f1d5e2; border-radius: 9px; padding: 6px; }
          QLineEdit, QComboBox { background: white; border: 1px solid #efc5d8; border-radius: 7px; padding: 5px; }
          QPushButton { background: white; border: 1px solid #edc4d6; border-radius: 8px; padding: 5px 9px; }
          QPushButton:hover { background: #fce7f0; }
          QPushButton:checked { background: #fce1ed; color: #d65c8c; }
          QPushButton:disabled { color: #baafb5; }
          QPushButton[primary="true"] { background: #d65c8c; color: white; border: none; }
        ''')
        self._drag = None
        self._dirty = True
        self._chat_sig = None
        self._tasks_sig = None
        self._approval_sig = None
        self._last_tasks = 0.
        self._previous_provider = 'claude'
        default_cwd = 'D:/Claude' if Path('D:/Claude').is_dir() else str(Path.home())
        self.cwd = str(pet.settings.value('mini_cwd', default_cwd))
        if not Path(self.cwd).is_dir():
            self.cwd = str(Path.home())
        self.provider = str(pet.settings.value('mini_provider','claude'))
        if self.provider not in ('claude','codex'):
            self.provider = 'claude'
        self._previous_provider = self.provider
        outer = QVBoxLayout(self)
        outer.setContentsMargins(7,7,7,7)
        frame = QFrame()
        frame.setObjectName('card')
        outer.addWidget(frame)
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(12,10,12,10)
        lay.setSpacing(7)
        top = QHBoxLayout()
        top.addWidget(make_label('小蕾米 Mini','title'))
        top.addStretch(1)
        self.pin = QPushButton('置顶')
        self.pin.setCheckable(True)
        self.pin.setChecked(True)
        self.pin.clicked.connect(self._pin)
        top.addWidget(self.pin)
        close = QPushButton('×')
        close.clicked.connect(self.hide)
        top.addWidget(close)
        lay.addLayout(top)
        tabs = QHBoxLayout()
        self.chat_tab, self.tasks_tab = QPushButton('聊天'), QPushButton('任务')
        for i,b in enumerate((self.chat_tab,self.tasks_tab)):
            b.setCheckable(True)
            b.clicked.connect(lambda _=False,index=i:self.show_tab(index))
            tabs.addWidget(b)
        tabs.addStretch(1)
        full = QPushButton('完整面板')
        full.clicked.connect(lambda:self.pet.open_panel(force_show=True))
        tabs.addWidget(full)
        lay.addLayout(tabs)
        self.quotas = make_label('','tiny',wrap=True)
        lay.addWidget(self.quotas)
        self.stack = QStackedWidget()
        lay.addWidget(self.stack,1)
        self._build_chat()
        self._build_tasks()
        foot = QHBoxLayout()
        self.status = make_label('','tiny',wrap=True)
        foot.addWidget(self.status,1)
        foot.addWidget(QSizeGrip(self))
        lay.addLayout(foot)
        self.setMinimumSize(390,450)
        size = pet.settings.value('mini_size')
        self.resize(size if size is not None else self.sizeHint())
        if size is None:
            self.resize(460,610)
        self.hub.changed.connect(self._changed)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(100)
        self.show_tab(0)

    def _build_chat(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0,0,0,0)
        lay.setSpacing(7)
        row = QHBoxLayout()
        self.selector = QComboBox()
        self.selector.addItem('Claude','claude')
        self.selector.addItem('Codex','codex')
        self.selector.setCurrentIndex(0 if self.provider == 'claude' else 1)
        self.selector.currentIndexChanged.connect(self._switch_provider)
        row.addWidget(self.selector)
        row.addWidget(make_label('Mini 会话','tiny'))
        row.addStretch(1)
        self.fresh = QPushButton('新对话')
        self.fresh.clicked.connect(self._new)
        row.addWidget(self.fresh)
        lay.addLayout(row)
        row = QHBoxLayout()
        self.folder = QLineEdit(self.cwd)
        self.folder.setReadOnly(True)
        self.folder.setToolTip('助手的工作目录；同一目录会接着上次 Mini 会话聊')
        row.addWidget(self.folder)
        self.pick_folder = QPushButton('目录')
        self.pick_folder.clicked.connect(self._choose_folder)
        row.addWidget(self.pick_folder)
        lay.addLayout(row)
        self.history = QTextBrowser()
        self.history.setOpenExternalLinks(False)
        self.history.setPlaceholderText('选 Claude 或 Codex，给她发第一条消息。')
        lay.addWidget(self.history,1)
        self.approval_box, self.approval_layout = make_box()
        self.approval_box.hide()
        lay.addWidget(self.approval_box)
        self.prompt = PromptEdit()
        self.prompt.setPlaceholderText('发给助手…  Ctrl+Enter 发送，Enter 换行')
        self.prompt.setMaximumHeight(95)
        self.prompt.submitted.connect(self._send)
        lay.addWidget(self.prompt)
        row = QHBoxLayout()
        self.activity = make_label('准备好了','tiny')
        row.addWidget(self.activity,1)
        self.stop = QPushButton('停止')
        self.stop.clicked.connect(lambda:self.hub.stop(self.provider))
        row.addWidget(self.stop)
        self.send = QPushButton('发送')
        self.send.setProperty('primary',True)
        self.send.clicked.connect(self._send)
        row.addWidget(self.send)
        lay.addLayout(row)
        self.stack.addWidget(page)
        record=self.hub.select(self.provider,self.cwd)
        self.prompt.setPlainText(record.get('draft') or '')

    def _build_tasks(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        page = QWidget()
        page.setObjectName('holder')
        self.task_layout = QVBoxLayout(page)
        self.task_layout.setContentsMargins(0,0,4,0)
        self.task_layout.setSpacing(7)
        scroll.setWidget(page)
        page.setAutoFillBackground(False)
        scroll.viewport().setAutoFillBackground(False)
        scroll.viewport().setStyleSheet('background:transparent;')
        self.stack.addWidget(scroll)

    def _switch_provider(self):
        self._save_draft(self._previous_provider)
        self.provider = self.selector.currentData()
        self._previous_provider = self.provider
        self.pet.settings.setValue('mini_provider',self.provider)
        try:
            record=self.hub.select(self.provider,self.cwd)
            self.prompt.setPlainText(record.get('draft') or '')
        except RuntimeError as err:
            self.status.setText(str(err))
        self._chat_sig = None
        self._approval_sig = None
        self._dirty = True
        self.refresh()

    def _choose_folder(self):
        path = QFileDialog.getExistingDirectory(self,'选择助手的工作目录',self.cwd)
        if path:
            if any(self.hub.busy.values()):
                self.status.setText('还有任务在运行，等它完成后再切换目录。')
                return
            self._save_draft()
            self.cwd = path
            self.folder.setText(path)
            self.pet.settings.setValue('mini_cwd',path)
            record=self.hub.select(self.provider,path)
            self.prompt.setPlainText(record.get('draft') or '')
            self._chat_sig = None
            self._dirty = True

    def _new(self):
        try:
            self.hub.new_chat(self.provider,self.cwd)
            self.hub.record(self.provider)['draft']=''
            self.prompt.clear()
            self._chat_sig = None
        except RuntimeError as err:
            self.status.setText(str(err))

    def _send(self):
        try:
            text = self.prompt.toPlainText().strip()
            if not text:
                return
            self.hub.send(self.provider,text,self.cwd)
            self.prompt.clear()
            self.hub.record(self.provider)['draft']=''
            self.hub._save()
            self._chat_sig = None
            self._dirty = True
        except (OSError,RuntimeError) as err:
            self.status.setText(str(err))

    def _changed(self):
        self._dirty = True

    def _save_draft(self,provider=None):
        record=self.hub.record(provider or self.provider)
        if record:
            record['draft']=self.prompt.toPlainText()
            self.hub._save()

    def refresh(self):
        if not self.isVisible():
            return
        p = self.provider
        busy = self.hub.busy[p]
        self.activity.setText(self.hub.activity[p])
        self.send.setEnabled(not busy)
        self.stop.setEnabled(busy)
        self.fresh.setEnabled(not busy)
        self.pick_folder.setEnabled(not any(self.hub.busy.values()))
        parts=[]
        for name,hub in (('Claude',self.pet.usage),('Codex',self.pet.codex)):
            wins=[]
            for key,label in (('five_hour','5h'),('seven_day','本周')):
                w=hub.window(key)
                if w:
                    suffix='（待更新）' if w.get('expired') else ''
                    wins.append(f"{label} 剩 {max(0,100-w['used']):.0f}%"+suffix)
            parts.append(name+'  '+(' · '.join(wins) if wins else '额度待更新'))
        self.quotas.setText('\n'.join(parts))
        if self._dirty:
            self._render_chat()
            self._render_request()
            self._dirty = False
        if time.time() - self._last_tasks > .8:
            self._last_tasks = time.time()
            self._render_tasks()
        hotkey = getattr(self.pet,'mini_hotkey',None)
        if not self.status.text() or self.status.text().startswith('呼出'):
            self.status.setText('呼出 / 收起：' + (hotkey.label if hotkey else '点桌宠'))

    def _render_chat(self):
        messages = self.hub.record(self.provider).get('messages') or []
        signature = tuple((m.get('role'),m.get('text')) for m in messages)
        if signature == self._chat_sig:
            return
        self._chat_sig = signature
        import html
        labels = {'user':'你','assistant':self.provider.capitalize(),'system':'提示'}
        parts=[]
        for m in messages[-80:]:
            color = '#d65c8c' if m['role'] == 'user' else '#665361'
            text = html.escape(str(m.get('text') or '…'))
            parts.append(f'<p style="color:{color};font-weight:600">{labels.get(m["role"],m["role"])}</p>'
                         f'<div style="white-space:pre-wrap">{text}</div>')
        bar = self.history.verticalScrollBar()
        at_bottom = bar.maximum() - bar.value() < 35
        old = bar.value()
        self.history.setHtml(''.join(parts))
        bar.setValue(bar.maximum() if at_bottom else old)

    def _render_tasks(self):
        sessions = self.pet.tracker.ordered()
        active_ids={('codex:' if p=='codex' else '')+self.hub.record(p).get('session_id','')
                    for p in ('claude','codex') if self.hub.busy[p]}
        sessions=[s for s in sessions if s.id not in active_ids]
        signature = tuple((s.id,s.state,s.activity,s.facts(),s.status_text()) for s in sessions)
        pending = tuple(self.pet.pending)
        own = tuple((p,self.hub.busy[p],self.hub.activity[p]) for p in ('claude','codex'))
        requests = tuple(self.hub.requests)
        plans=tuple((provider,str(plan)) for provider,plan in self.hub.plans.items())
        sig = signature,pending,own,requests,plans,self.pet.privacy
        if sig == self._tasks_sig:
            return
        self._tasks_sig = sig
        clear_layout(self.task_layout)
        for (provider,rid),request in self.hub.requests.items():
            box,col = make_box()
            col.addWidget(make_label(provider.capitalize()+' Mini · 等你处理','h'))
            b=QPushButton('在聊天页处理')
            b.clicked.connect(lambda _=False,p=provider:self.select_provider(p))
            col.addWidget(b)
            self.task_layout.addWidget(box)
        for rid,req in list(self.pet.pending.items()):
            box,col=make_box()
            col.addWidget(make_label('Claude · 等你点允许','h'))
            payload=req.get('payload') or {}
            col.addWidget(make_label('等待确认' if self.pet.privacy else str(payload.get('tool_name') or '请求'),'sub'))
            b=QPushButton('打开确认卡片')
            b.clicked.connect(self.pet._show_next_card)
            col.addWidget(b)
            self.task_layout.addWidget(box)
        for provider in ('claude','codex'):
            if self.hub.busy[provider]:
                box,col=make_box()
                col.addWidget(make_label(provider.capitalize()+' Mini · '+self.hub.activity[provider],'h'))
                plan=self.hub.plans.get(provider) or []
                if plan:
                    done=sum(step.get('status')=='completed' for step in plan)
                    col.addWidget(make_label(f'进度 {done}/{len(plan)}','tiny'))
                    bar=Bar(5)
                    bar.set(done/len(plan),(82,190,130))
                    col.addWidget(bar)
                    for step in plan[:8]:
                        prefix={'completed':'✓','inProgress':'●'}.get(step.get('status'),'·')
                        col.addWidget(make_label(prefix+' '+str(step.get('step') or ''),'tiny',wrap=True))
                b=QPushButton('查看对话')
                b.clicked.connect(lambda _=False,p=provider:self.select_provider(p))
                col.addWidget(b)
                self.task_layout.addWidget(box)
        for s in sessions[:10]:
            box,col=make_box()
            provider='Codex' if s.provider=='codex' else 'Claude'
            title='会话' if self.pet.privacy else clip(s.name(),24)
            col.addWidget(make_label(f'{provider} · {title}','h'))
            col.addWidget(make_label(s.status_text()+' · '+s.activity_text(self.pet.privacy),'sub',wrap=True))
            if s.facts():
                col.addWidget(make_label(s.facts(),'tiny',wrap=True))
            b=QPushButton('打开原窗口')
            b.clicked.connect(lambda _=False,sid=s.id:self.pet.focus_session(sid))
            col.addWidget(b)
            self.task_layout.addWidget(box)
        if not sessions and not any(self.hub.busy.values()):
            self.task_layout.addWidget(make_label('暂时没有运行中的任务。可以在聊天页开始。','sub',wrap=True))
        self.task_layout.addStretch(1)

    def _render_request(self):
        requests=[r for (p,_),r in self.hub.requests.items() if p==self.provider]
        req=requests[0] if requests else None
        sig=(self.provider,req['id']) if req else None
        if sig==self._approval_sig:
            return
        self._approval_sig=sig
        clear_layout(self.approval_layout)
        self.approval_box.setVisible(req is not None)
        if not req:
            return
        params=req['params']
        method=req['method']
        self.approval_layout.addWidget(make_label('Codex 在等你','h'))
        if method.endswith('requestUserInput'):
            inputs={}
            for q in params.get('questions') or []:
                self.approval_layout.addWidget(make_label(q.get('question') or q.get('header') or '问题','sub',wrap=True))
                edit=QLineEdit()
                options=q.get('options') or []
                if options:
                    combo=QComboBox()
                    combo.addItem('选择一项或在下面填写…','')
                    for opt in options:
                        combo.addItem(opt.get('label') or '',opt.get('label') or '')
                    combo.currentIndexChanged.connect(lambda _,c=combo,e=edit:e.setText(c.currentData() or ''))
                    self.approval_layout.addWidget(combo)
                self.approval_layout.addWidget(edit)
                inputs[str(q['id'])]=edit
            b=QPushButton('提交回答')
            def answer():
                if not all(e.text().strip() for e in inputs.values()):
                    self.status.setText('请把每个问题都填好。')
                    return
                self.hub.answer('codex',req['id'],{'answers':{qid:{'answers':[e.text().strip()]} for qid,e in inputs.items()}})
            b.clicked.connect(answer)
            self.approval_layout.addWidget(b)
        else:
            text=params.get('command') or params.get('reason') or params.get('grantRoot') or json.dumps(params.get('permissions') or {},ensure_ascii=False)
            preview=QPlainTextEdit(str(text))
            preview.setReadOnly(True)
            preview.setMaximumHeight(90)
            self.approval_layout.addWidget(preview)
            row=QHBoxLayout()
            for label,allow in (('允许这一次',True),('拒绝',False)):
                b=QPushButton(label)
                def decide(_=False,a=allow):
                    if method=='item/permissions/requestApproval':
                        result={'permissions':(params.get('permissions') or {}) if a else {},'scope':'turn'}
                    else:
                        result={'decision':'accept' if a else 'decline'}
                    self.hub.answer('codex',req['id'],result)
                b.clicked.connect(decide)
                row.addWidget(b)
            self.approval_layout.addLayout(row)

    def select_provider(self,provider):
        self.selector.setCurrentIndex(0 if provider=='claude' else 1)
        self.show_tab(0)

    def show_tab(self,index):
        self.stack.setCurrentIndex(index)
        self.chat_tab.setChecked(index==0)
        self.tasks_tab.setChecked(index==1)
        self._dirty=True
        self.refresh()

    def show_near(self,rect):
        screen=QGuiApplication.screenAt(rect.center()) or QGuiApplication.primaryScreen()
        area=screen.availableGeometry()
        saved=self.pet.settings.value('mini_pos')
        x=saved.x() if saved else rect.left()-self.width()-10
        y=saved.y() if saved else rect.bottom()-self.height()
        self.move(max(area.left(),min(x,area.right()-self.width())),
                  max(area.top(),min(y,area.bottom()-self.height())))
        self.show()
        self.raise_()
        self.activateWindow()
        self._dirty=True
        self.refresh()
        self.prompt.setFocus()

    def _pin(self,checked):
        self.setWindowFlag(Qt.WindowStaysOnTopHint,checked)
        self.show()

    def hideEvent(self,event):
        self._save_draft()
        self.pet.settings.setValue('mini_pos',self.pos())
        self.pet.settings.setValue('mini_size',self.size())
        from .server import STATUS
        STATUS.setdefault('mini',{})['visible']=False
        super().hideEvent(event)

    def closeEvent(self,event):
        self.hide()
        event.ignore()

    def keyPressEvent(self,event):
        if event.key()==Qt.Key_Escape:
            self.hide()
            event.accept()
        else:
            super().keyPressEvent(event)

    def mousePressEvent(self,event):
        if event.button()==Qt.LeftButton and event.position().y()<47:
            self._drag=event.globalPosition().toPoint()-self.pos()
            event.accept()

    def mouseMoveEvent(self,event):
        if self._drag is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint()-self._drag)
            event.accept()

    def mouseReleaseEvent(self,event):
        self._drag=None
