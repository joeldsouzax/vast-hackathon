"""Bounded native/proxy mapping from FFmpeg's actual decoded frame metadata.

FFmpeg fps selects a nearby decoded input frame. A conservative uncertainty of
one input/output frame is retained. A missing trace never creates a mapping.
"""
from collections import deque
from fractions import Fraction
import re
import threading
import av


class DecoderTrace:
    def __init__(self,fps,width,height):
        self.fps,self.width,self.height=fps,width,height
        self.lock=threading.Lock()
        self.changed=threading.Condition(self.lock)
        self.native=deque(maxlen=4096)
        self.scaled=deque(maxlen=4096)
        self.proxy={}
        self.native_time_base=None
        self.failure=None
        self.last_pts=None
        self.revision=1
        self.proxy_sequence=0
        self.source_discontinuity=False

    def accept(self,line):
        if 'showinfo@native' in line:
            clock=re.search(r'config in time_base: (\d+/\d+)',line)
            if clock:
                with self.lock:
                    if self.native_time_base is not None and self.native_time_base!=clock[1]:
                        self.failure='Native time base changed';self.source_discontinuity=True
                        self.revision+=1;self.native.clear();self.last_pts=None
                    self.native_time_base=clock[1]
            match=re.search(r'\bn:\s*(\d+)\s+pts:\s*(-?\d+)\s+pts_time:([\d.e+-]+).*?\bs:(\d+)x(\d+)',line)
            if match:
                _,pts,seconds,width,height=match.groups()
                with self.lock:
                    pts=int(pts)
                    if self.last_pts is not None and pts<self.last_pts:
                        self.failure=f'Native timestamp reset: {self.last_pts} to {pts}'
                        self.source_discontinuity=True
                        self.revision+=1;self.native.clear()
                    if self.last_pts is not None and pts==self.last_pts:return
                    if self.native and (int(width),int(height)) != self.native[-1][2:4]:
                        self.failure='Native geometry changed';self.source_discontinuity=True;self.revision+=1;self.native.clear()
                    self.last_pts=pts
                    self.native.append((pts,float(seconds),int(width),int(height),self.native_time_base))
        elif 'showinfo@scaled' in line:
            match=re.search(r'\bpts_time:([\d.e+-]+).*?\bs:(\d+)x(\d+)',line)
            if match:
                seconds,width,height=match.groups()
                pixel_format=re.search(r'\bfmt:(\S+)',line)
                with self.lock:self.scaled.append((float(seconds),int(width),int(height),pixel_format[1] if pixel_format else None))
        elif 'showinfo@proxy' in line:
            match=re.search(r'\bn:\s*(\d+)\s+pts:\s*(-?\d+)\s+pts_time:([\d.e+-]+)',line)
            if match:
                index,pts,seconds=match.groups();seconds=float(seconds)
                with self.lock:
                    self.proxy_sequence+=1
                    self.changed.notify_all()
                    if not self.native or not self.scaled:return
                    native=min(self.native,key=lambda frame:abs(frame[1]-seconds))
                    if not native[4] or abs(native[1]-seconds)>2/self.fps:return
                    scaled=min(self.scaled,key=lambda frame:abs(frame[0]-seconds))
                    if abs(scaled[0]-seconds)>1/self.fps:return
                    geometry=None
                    if scaled[3]:
                        pixel_format=av.VideoFormat(scaled[3])
                        x_alignment=4//pixel_format.chroma_width(4)
                        y_alignment=4//pixel_format.chroma_height(4)
                        geometry={'native_width':native[2],'native_height':native[3],'rotation':0,
                            'output_width':self.width,'output_height':self.height,
                            'scaled_width':scaled[1],'scaled_height':scaled[2],
                            'pad_x':((self.width-scaled[1])//2//x_alignment)*x_alignment,
                            'pad_y':((self.height-scaled[2])//2//y_alignment)*y_alignment}
                    uncertainty=max(1000/self.fps,float(Fraction(native[4]))*1000)
                    self.proxy[self.proxy_sequence]={'timeline_revision':self.revision,'source_discontinuity':self.source_discontinuity,'native_pts':native[0],'native_time_base':native[4],
                        'native_width':native[2],'native_height':native[3], 'proxy_s':seconds,
                        'uncertainty_ms':uncertainty,'geometry':geometry,
                        'orientation_basis':'FFmpeg decoded oriented input; sensor rotation unknown'}
                    while len(self.proxy)>4096:self.proxy.pop(next(iter(self.proxy)))

    def take(self,sequence,timeout=0):
        with self.changed:
            if timeout:self.changed.wait_for(lambda:sequence in self.proxy or self.proxy_sequence>=sequence,timeout)
            return self.proxy.pop(sequence,None)
