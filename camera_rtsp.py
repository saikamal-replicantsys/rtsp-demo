# camera_rtsp.py
import cv2
import gi
import threading

gi.require_version('Gst', '1.0')
gi.require_version('GstRtspServer', '1.0')
from gi.repository import Gst, GstRtspServer, GObject

Gst.init(None)

shared_frame = None
lock = threading.Lock()

class RTSPServerFactory(GstRtspServer.RTSPMediaFactory):
    def __init__(self, **properties):
        super(RTSPServerFactory, self).__init__(**properties)
        # Adjust width/height/framerate to match your detection STREAM_SIZE
        self.launch_string = (
            'appsrc name=source is-live=true block=true format=GST_FORMAT_TIME '
            'caps=video/x-raw,format=BGR,width=960,height=600,framerate=15/1 ! '
            'videoconvert ! x264enc speed-preset=ultrafast tune=zerolatency ! '
            'rtph264pay config-interval=1 name=pay0 pt=96'
        )

    def on_need_data(self, src, length):
        global shared_frame
        with lock:
            if shared_frame is None:
                return
            frame = shared_frame.copy()

        data = frame.tobytes()
        buf = Gst.Buffer.new_allocate(None, len(data), None)
        buf.fill(0, data)
        buf.pts = buf.dts = Gst.CLOCK_TIME_NONE
        buf.duration = Gst.util_uint64_scale_int(1, Gst.SECOND, 15)
        retval = src.emit("push-buffer", buf)
        if retval != Gst.FlowReturn.OK:
            print(f"⚠️ Push buffer failed: {retval}")

    def do_create_element(self, url):
        return Gst.parse_launch(self.launch_string)

    def do_configure(self, rtsp_media):
        appsrc = rtsp_media.get_element().get_child_by_name("source")
        appsrc.connect("need-data", self.on_need_data)

def start_rtsp_server():
    server = GstRtspServer.RTSPServer()
    factory = RTSPServerFactory()
    factory.set_shared(True)
    server.get_mount_points().add_factory("/stream", factory)
    server.attach(None)
    print("✅ RTSP stream available at rtsp://192.168.1.29:8554/stream")
    loop = GObject.MainLoop()
    loop.run()

def update_frame(frame):
    """Call this from your detection loop to publish a new frame."""
    global shared_frame
    with lock:
        shared_frame = frame
