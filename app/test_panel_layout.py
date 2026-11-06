import unittest
from PySide6.QtCore import QRect,QSize
from panel_layout import layout_panels,hover_position


class PanelLayoutTests(unittest.TestCase):
    def assert_clear(self,pet,sizes,area,blockers=(),**kwargs):
        points,side=layout_panels(pet,sizes,area,blockers,**kwargs)
        rects=[QRect(point,size) for point,size in zip(points,sizes)]
        for index,rect in enumerate(rects):
            self.assertTrue(area.contains(rect),(area,rect))
            self.assertFalse(rect.intersects(pet),(pet,rect))
            for other in rects[index+1:]:self.assertFalse(rect.intersects(other),(rect,other))
            for other in blockers:self.assertFalse(rect.intersects(other),(rect,other))
        quota=rects[2]
        self.assertTrue(quota.right()<pet.left() or quota.left()>pet.right())
        return points,side

    def test_actual_desktop_report_stays_near_the_head(self):
        pet=QRect(1674,696,256,256);area=QRect(0,0,2048,1104)
        sizes=[QSize(256,52),QSize(300,152),QSize(280,130)]
        points,side=self.assert_clear(pet,sizes,area)
        report=QRect(points[1],sizes[1]);self.assertEqual(side,'left')
        self.assertLessEqual(pet.left()-report.right(),30)
        self.assertLessEqual(pet.top()-report.bottom(),15)

    def test_one_pixel_across_monitor_center_keeps_quota_side(self):
        sizes=[QSize(256,52),QSize(300,152),QSize(280,130)]
        area=QRect(0,0,2048,1104)
        first,side=self.assert_clear(QRect(896,500,256,256),sizes,area,preferred_side='right')
        second,next_side=self.assert_clear(QRect(897,500,256,256),sizes,area,preferred_side=side)
        self.assertEqual(next_side,'right');self.assertEqual(second[2].x()-first[2].x(),1)
        _,next_side=self.assert_clear(QRect(1782,500,256,256),sizes,area,preferred_side=side)
        self.assertEqual(next_side,'left')

    def test_multiline_greetings_and_preview_do_not_move_report_or_quota(self):
        pet=QRect(800,500,256,256);area=QRect(0,0,1920,1040)
        normal,_=layout_panels(pet,[QSize(256,40),QSize(300,154),QSize(280,146)],area)
        preview,_=layout_panels(pet,[QSize(300,130),QSize(300,154),QSize(280,146)],area)
        self.assertEqual(normal[1:],preview[1:])

    def test_monitor_edges_and_scaling(self):
        for area in (QRect(0,0,1920,1040),QRect(-1920,-1040,1920,1040),QRect(0,0,1024,768)):
            for scale in (.5,1,1.5):
                sizes=[QSize(round(w*scale),round(h*scale)) for w,h in ((256,80),(300,200),(280,120))]
                for x in (area.left()+10,area.right()-265):
                    for y in (area.top()+10,area.bottom()-265):
                        with self.subTest(area=area,scale=scale,x=x,y=y):
                            self.assert_clear(QRect(x,y,256,256),sizes,area,scale=scale)

    def test_toolbar_does_not_cover_pet_at_screen_bottom(self):
        area=QRect(0,0,2048,1104);pet=QRect(1674,848,256,256);size=QSize(240,38)
        point=hover_position(pet,size,area)
        self.assertTrue(area.contains(QRect(point,size)))
        self.assertFalse(QRect(point,size).intersects(pet))
        self.assertLess(point.y(),pet.top())
        self.assert_clear(pet,[QSize(256,52),QSize(300,152),QSize(280,130)],area,[QRect(point,size)])

    def test_chat_composer_and_toolbar_leave_all_panels_separate(self):
        area=QRect(0,0,2048,1104);pet=QRect(1674,696,256,256)
        toolbar=QRect(hover_position(pet,QSize(240,38),area),QSize(240,38))
        composer=QRect(1622,960,360,130)
        self.assert_clear(pet,[QSize(256,52),QSize(300,226),QSize(280,130)],area,[toolbar,composer])


if __name__=='__main__':unittest.main()
