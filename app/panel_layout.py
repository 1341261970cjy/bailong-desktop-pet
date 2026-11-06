"""Deterministic anchors for the pet's independent floating windows."""
from PySide6.QtCore import QPoint, QRect


def _clamp(point, size, area):
    return QPoint(max(area.left(), min(point.x(), area.right()-size.width()+1)),
                  max(area.top(), min(point.y(), area.bottom()-size.height()+1)))


def _overlap(first, second):
    rect=first.intersected(second)
    return max(0,rect.width())*max(0,rect.height())


def _place(preferred,size,pet,area,occupied,gap,xs=None):
    """Keep an anchor unless an actual surface obstructs it."""
    x_values=list(xs) if xs is not None else [preferred.x(),area.left(),area.right()-size.width()+1,
                                             pet.left()-size.width()-gap,pet.right()+gap+1]
    y_values=[preferred.y(),area.top(),area.bottom()-size.height()+1,
              pet.top()-size.height()-gap,pet.bottom()+gap+1]
    for rect in occupied:
        if xs is None:x_values.extend((rect.left()-size.width()-gap,rect.right()+gap+1))
        y_values.extend((rect.top()-size.height()-gap,rect.bottom()+gap+1))
    candidates={(point.x(),point.y()) for x in x_values for y in y_values
                for point in [_clamp(QPoint(x,y),size,area)]}
    def score(point):
        rect=QRect(QPoint(*point),size)
        return (_overlap(rect,pet.adjusted(-gap,-gap,gap,gap)),
                sum(_overlap(rect,other) for other in occupied),
                (point[0]-preferred.x())**2+(point[1]-preferred.y())**2,
                point[1],point[0])
    return QPoint(*min(candidates,key=score))


def hover_position(pet,size,area,gap=8):
    """Prefer below the pet, moving to another edge when it cannot fit."""
    center_x=pet.x()+(pet.width()-size.width())//2
    center_y=pet.y()+(pet.height()-size.height())//2
    candidates=[QPoint(center_x,pet.bottom()+gap+1),
                QPoint(center_x,pet.top()-size.height()-gap),
                QPoint(pet.left()-size.width()-gap,center_y),
                QPoint(pet.right()+gap+1,center_y)]
    for point in candidates:
        point=_clamp(point,size,area)
        if not QRect(point,size).intersects(pet):return point
    return min((_clamp(point,size,area) for point in candidates),
               key=lambda point:_overlap(QRect(point,size),pet))


def layout_panels(pet,sizes,area,blockers=(),gap=10,scale=1.0,preferred_side=None):
    """Return (speech/report/quota points, quota side), using logical pixels.

    The caller retains ``side`` between layouts. A side changes only when it
    stops fitting, so a one-pixel move across a monitor's center cannot flip
    the quota window. Reports sit beside the head instead of above a stack of
    invisible reserved windows.
    """
    speech_size,report_size,quota_size=sizes
    occupied=[QRect(rect) for rect in blockers]
    side_space={'left':pet.left()-area.left(),'right':area.right()-pet.right()}
    fits={side:space>=quota_size.width()+gap for side,space in side_space.items()}
    if preferred_side in fits and fits[preferred_side]:side=preferred_side
    elif fits['left']!=fits['right']:side='left' if fits['left'] else 'right'
    elif preferred_side in fits:side=preferred_side
    else:side=max(('right','left'),key=lambda candidate:side_space[candidate])

    quota_x=pet.left()-quota_size.width()-gap if side=='left' else pet.right()+gap+1
    quota_y=pet.y()+(pet.height()-quota_size.height())//2
    quota=_place(QPoint(quota_x,quota_y),quota_size,pet,area,occupied,gap,
                 xs=[quota_x] if fits[side] else None)
    occupied.append(QRect(quota,quota_size))

    # Choose a head direction using a bounded common interaction height;
    # differing one-line/multiline greetings retain the same bottom anchor.
    head_height=max(round(140*scale),speech_size.height())
    above=pet.top()-area.top()>=head_height+gap
    below=area.bottom()-pet.bottom()>=head_height+gap
    if above or not below:speech_y=pet.top()-speech_size.height()-gap
    else:speech_y=pet.bottom()+gap+1
    speech_x=pet.x()+(pet.width()-speech_size.width())//2
    speech=_place(QPoint(speech_x,speech_y),speech_size,pet,area,occupied,gap)
    occupied.append(QRect(speech,speech_size))

    # The horizontal head lane supports messages and the 300px size preview.
    # This small width allowance does not move the report for 40..140px text.
    side_gap=max(gap,round(10*scale))
    head_width=max(pet.width(),round(280*scale),speech_size.width()-2*side_gap)
    head_left=pet.x()+(pet.width()-head_width)//2
    head_right=head_left+head_width-1
    report_x=head_left-report_size.width()-side_gap if side=='left' else head_right+side_gap+1
    report_y=pet.top()-report_size.height()-gap
    report=_place(QPoint(report_x,report_y),report_size,pet,area,occupied,gap)
    return [speech,report,quota],side
