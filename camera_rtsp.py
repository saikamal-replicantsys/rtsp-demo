import cv2
import gi
import threading

gi.require_version('Gst', '1.0')
gi.require_version('GstRtspServer', '1.0')
from gi.repository import Gst, GstRtspServer, GObject

Gst.init(None)

class RTSPServerFactory(GstRtspServer.RTSPMediaFactory):
    def __init__(self, **properties):
        super(RTSPServerFactory, self).__init__(**properties)
        self.cap = cv2.VideoCapture("rtsp://admin:replisense@192.168.1.126:554/unicaststream/1", cv2.CAP_FFMPEG)
        self.launch_string = (
            'appsrc name=source is-live=true block=true format=GST_FORMAT_TIME '
            'caps=video/x-raw,format=BGR,width=640,height=480,framerate=30/1 ! '
            'videoconvert ! x264enc speed-preset=ultrafast tune=zerolatency ! rtph264pay config-interval=1 name=pay0 pt=96'
        )

    def on_need_data(self, src, length):
        ret, frame = self.cap.read()
        if not ret:
            print("⚠️ Could not read frame.")
            return

        data = frame.tobytes()
        buf = Gst.Buffer.new_allocate(None, len(data), None)
        buf.fill(0, data)
        timestamp = Gst.util_uint64_scale(self.cap.get(cv2.CAP_PROP_POS_MSEC), Gst.SECOND, 1000)
        buf.pts = buf.dts = int(timestamp)
        buf.duration = Gst.util_uint64_scale_int(1, Gst.SECOND, 30)

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
    mount_points = server.get_mount_points()
    mount_points.add_factory("/stream", factory)
    server.attach(None)
    # Jetson IP
    print("✅ RTSP server is live at rtsp://192.168.2.6:8554/stream")
    loop = GObject.MainLoop()
    loop.run()

if __name__ == '__main__':
    t = threading.Thread(target=start_rtsp_server)
    t.start()
