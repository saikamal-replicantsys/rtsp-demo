#!/usr/bin/env python3
import gi
import cv2
import threading

gi.require_version('Gst', '1.0')
gi.require_version('GstRtspServer', '1.0')
from gi.repository import Gst, GstRtspServer, GObject, GLib

# Initialize GStreamer
Gst.init(None)

class CameraFactory(GstRtspServer.RTSPMediaFactory):
    def __init__(self):
        super(CameraFactory, self).__init__()
        self.cap = cv2.VideoCapture(0)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        self.cap.set(cv2.CAP_PROP_FPS, 15)
        self.frame_counter = 0

    def do_create_element(self, url):
        # Build a pipeline: appsrc (BGR frames) → videoconvert → x264enc → rtph264pay
        pipeline_desc = (
            "appsrc name=mysrc is-live=true block=true format=GST_FORMAT_TIME "
            "caps=video/x-raw,format=BGR,width=640,height=480,framerate=15/1 "
            "! videoconvert ! video/x-raw,format=I420 "
            "! x264enc tune=zerolatency bitrate=400 speed-preset=superfast "
            "! rtph264pay name=pay0 pt=96"
        )
        return Gst.parse_launch(pipeline_desc)

    def do_configure(self, rtsp_media):
        appsrc = rtsp_media.get_element().get_child_by_name("mysrc")

        def push_frames():
            while True:
                ret, frame = self.cap.read()
                if not ret:
                    continue

                data = frame.tobytes()
                buf = Gst.Buffer.new_allocate(None, len(data), None)
                buf.fill(0, data)

                # Duration = 1/15 second per frame
                duration = Gst.util_uint64_scale_int(1, Gst.SECOND, 15)
                buf.duration = duration
                timestamp = self.frame_counter * duration
                buf.pts = buf.dts = int(timestamp)
                buf.offset = self.frame_counter
                self.frame_counter += 1

                result = appsrc.emit("push-buffer", buf)
                if result != Gst.FlowReturn.OK:
                    break

        threading.Thread(target=push_frames, daemon=True).start()

class RtspServer:
    def __init__(self):
        self.server = GstRtspServer.RTSPServer()
        self.factory = CameraFactory()
        self.factory.set_shared(True)
        mounts = self.server.get_mount_points()
        mounts.add_factory("/test", self.factory)
        self.server.attach(None)

if __name__ == "__main__":
    loop = GLib.MainLoop()
    s = RtspServer()
    print("RTSP stream available at rtsp://0.0.0.0:8554/test")
    loop.run()
