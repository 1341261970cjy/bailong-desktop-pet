import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import unittest
from unittest.mock import Mock

from PySide6.QtCore import QTimer,QRect,QSize
from PySide6.QtWidgets import QApplication,QWidget,QLabel
from pet import Bubble,Pet,format_money,panel_positions


class PanelHarness(QWidget):
    panels=Pet.panels
    position_bubble=Pet.position_bubble
    say=Pet.say
    task_result=Pet.task_result
    task_progress=Pet.task_progress
    dismiss_bubble=Pet.dismiss_bubble
    dismiss_task_bubble=Pet.dismiss_task_bubble
    dismiss_quota_bubble=Pet.dismiss_quota_bubble
    desktop_hidden=Pet.desktop_hidden
    dialogue_hidden=Pet.dialogue_hidden
    speech_hidden=Pet.speech_hidden
    sync_dialogue_visibility=Pet.sync_dialogue_visibility
    _render_dashboard=Pet._render_dashboard

    def __init__(self):
        super().__init__()
        self.setGeometry(350,350,200,200)
        self.bubble=Bubble();self.task_bubble=Bubble();self.quota_bubble=Bubble()
        self.hover_bar=QWidget();self.chat=None
        self.mode='idle';self.current={};self.manual_hidden=False;self.fullscreen_hidden=False
        self.task_bubbles_disabled=False
        self.bubble_timer=QTimer(self);self.bubble_timer.setSingleShot(True);self.bubble_timer.timeout.connect(self.dismiss_bubble)
        self.task_timer=QTimer(self);self.task_timer.setSingleShot(True);self.task_timer.timeout.connect(self.dismiss_task_bubble)
        self.quota_timer=QTimer(self);self.quota_timer.setSingleShot(True);self.quota_timer.timeout.connect(self.dismiss_quota_bubble)
        self.task_bubble.dismiss_requested.connect(self.dismiss_task_bubble)
        self.quota_bubble.dismiss_requested.connect(self.dismiss_quota_bubble)
        self.dashboard_requested=False;self.current_quota_windows=None
        self.station_snapshot=None;self.station_error=None;self.quota_error=None
        self.show_codex_panel=False;self.show_station_panel=True
        self.quota_inflight=False;self.station_inflight=False;self.idle=Mock()


class IndependentPanelsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.pet=PanelHarness()

    def tearDown(self):
        for panel in self.pet.panels():panel.close()
        self.pet.hover_bar.close();self.pet.close();self.app.processEvents()

    def test_interaction_timeout_and_quota_refresh_preserve_work_report(self):
        p=self.pet;p.task_result('工作汇报','已完成的报告不能被互动替换',lambda:None)
        result_view=p.task_bubble.result_view
        p.say('互动提示',4000)
        p.station_snapshot={'currency':'¥','walletBalance':'12.34567','todayUsed':'0.456789'}
        p.dashboard_requested=True;p._render_dashboard()
        self.assertTrue(all(panel.isVisible() for panel in p.panels()))
        p.bubble_timer.timeout.emit()
        self.assertFalse(p.bubble.isVisible())
        self.assertTrue(p.task_bubble.isVisible());self.assertTrue(p.quota_bubble.isVisible())
        self.assertIs(p.task_bubble.result_view,result_view)
        self.assertEqual(result_view.toPlainText(),'已完成的报告不能被互动替换')
        self.assertFalse(p.task_timer.isActive())

    def test_closing_report_does_not_close_other_panels(self):
        p=self.pet;p.task_progress('任务进行中','保留公开进展',lambda:None)
        p.say('互动提示');p.quota_bubble.dashboard(None,{'walletBalance':'10','todayUsed':'0'},show_codex=False)
        p.task_bubble.close_button.click()
        self.assertFalse(p.task_bubble.isVisible())
        self.assertTrue(p.bubble.isVisible());self.assertTrue(p.quota_bubble.isVisible())

    def test_working_animation_does_not_hide_report_or_quota(self):
        p=self.pet;p.task_result('工作汇报','完整结果',lambda:None)
        p.quota_bubble.dashboard(None,{'walletBalance':'10','todayUsed':'0'},show_codex=False)
        p.say('短暂互动');p.mode='working';p.current={'id':'work01'}
        p.sync_dialogue_visibility()
        self.assertFalse(p.bubble.isVisible())
        self.assertTrue(p.task_bubble.isVisible());self.assertTrue(p.quota_bubble.isVisible())

    def test_compact_money_panel_has_three_decimals_and_no_time_metadata(self):
        panel=self.pet.quota_bubble
        panel.dashboard(None,{'currency':'¥','walletBalance':'12.34567','todayUsed':'0.456789','checkedAtText':'12:34:56'},show_codex=False)
        self.app.processEvents()
        text='\n'.join(label.text() for label in panel.findChildren(QLabel))
        self.assertIn('钱包余额：¥12.346',text);self.assertIn('今日消耗：¥0.457',text)
        self.assertNotIn('12:34:56',text);self.assertNotIn('北京时间',text)
        self.assertNotIn('白龙额度面板',text);self.assertLessEqual(panel.width(),280)
        self.assertLess(panel.height(),170)
        self.assertEqual(format_money('0','¥'),'¥0.000')
        self.assertEqual(format_money('1.2345'),'1.235')

    def test_call_latency_uses_log_durations_and_omits_missing_first_token(self):
        panel=self.pet.quota_bubble
        snapshot={'walletBalance':'12','todayUsed':'0','latencyFirstTokenMs':1780,'latencyTotalMs':18300}
        panel.dashboard(None,snapshot,show_codex=False)
        text='\n'.join(label.text() for label in panel.findChildren(QLabel))
        self.assertIn('首字≈1.8s',text);self.assertIn('总耗时≈18.3s',text)
        snapshot.update(latencyFirstTokenMs=None,latencyTotalMs=0)
        panel.dashboard(None,snapshot,show_codex=False)
        text='\n'.join(label.text() for label in panel.findChildren(QLabel))
        self.assertNotIn('首字≈',text);self.assertIn('总耗时≈0.0s',text)

    def test_refresh_uses_header_spinner_without_extra_loading_row(self):
        from PySide6.QtWidgets import QPushButton
        panel=self.pet.quota_bubble
        panel.dashboard(None,{'walletBalance':'12','todayUsed':'0'},show_codex=False,station_loading=True)
        busy=panel.findChild(QPushButton,'refreshStation')
        self.assertTrue(busy.property('loading'));self.assertFalse(busy.isEnabled())
        self.assertNotIn('正在刷新','\n'.join(label.text() for label in panel.findChildren(QLabel)))
        panel.dashboard(None,{'walletBalance':'12','todayUsed':'0'},show_codex=False)
        requests=[];panel.refresh_requested.connect(requests.append)
        panel.findChild(QPushButton,'refreshStation').click()
        self.assertEqual(requests,['station'])

    def test_panels_remain_inside_screen_and_outside_pet_at_edges(self):
        sizes=[QSize(256,80),QSize(300,200),QSize(280,120)]
        for area,pet in ((QRect(0,0,1920,1040),QRect(1654,774,256,256)),
                         (QRect(-1920,0,1920,1040),QRect(-360,10,256,256)),
                         (QRect(0,0,1024,768),QRect(758,502,256,256))):
            with self.subTest(area=area,pet=pet):
                rects=[QRect(point,size) for point,size in zip(panel_positions(pet,sizes,area),sizes)]
                for rect in rects:
                    self.assertTrue(area.contains(rect));self.assertFalse(rect.intersects(pet))
                for index,rect in enumerate(rects):
                    self.assertFalse(any(rect.intersects(other) for other in rects[index+1:]))


if __name__=='__main__':unittest.main()
